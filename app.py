import time
import markdown2
import requests
import base64
import io
import json # 确保导入 json
from flask import Flask, Response, render_template, request, jsonify, send_from_directory, render_template_string # 引入 Response 和 render_template_string
from config import key_manager, MODEL_BASE_URL, PORT, BASE_PROMPT, MODELS, NoAvailableKeysError
from PIL import Image
from threading import Lock
from datetime import datetime
from urllib.parse import quote

# ----------------- 基础配置 -----------------
HEADERS = {"Content-Type": "application/json"}
chat_history_lock = Lock()  # 全局锁
# 用于实现“能力保持”的变量，记录上一次成功请求的key
last_successful_key = None

app = Flask(__name__, template_folder='templates')
chat_history = [] # 直接保存成 Google Gemini 兼容格式（role / parts）
# 用于存储流式传输中积累的完整机器人回复
current_bot_response_full = ""

# ------- 在原有 Flask 路由后面加上两个新接口 --------
@app.route('/reset', methods=['POST'])
def reset():
    global chat_history, current_bot_response_full, last_successful_key
    chat_history.clear()
    current_bot_response_full = "" # 重置时也清空
    last_successful_key = None
    return jsonify({'ok': True})

# ----------------- 调用大模型 (流式) -----------------
def stream_gemini_response(history, model, tools=None):
    global current_bot_response_full, chat_history, last_successful_key
    current_bot_response_full = ""

    # 获取可用密钥数作为最大重试次数
    max_retries = key_manager.get_status()["available_keys"]
    if max_retries == 0:
        error_msg = "错误：密钥池中没有可用的密钥。"
        current_bot_response_full = error_msg
        yield f"data: {json.dumps({'text': error_msg})}\n\n"
        yield f"event: end\ndata: [DONE]\n\n"
        return

    for attempt in range(max_retries):
        api_key = None  # 在 try 外部定义，便于 except 块中引用
        try:
            # 调用新的 get_key 方法，并传入上一次成功的 key 作为首选
            api_key = key_manager.get_key(preferred_key=last_successful_key, force_paid=False)

            # 获取密钥详细信息
            key_status = key_manager.get_detailed_key_status(api_key)
            key_type = "未知"
            if key_status.get('details') and len(key_status['details']) > 0:
                key_type = key_status['details'][0].get('key_type', '未知')

            print(f"正在使用 API Key: {api_key} (尝试 {attempt + 1}/{max_retries})"
                  f"\n当前key层级：{key_type}"
                  f"\n免费层级失败次数：{key_manager.get_status()['free_key_consecutive_failures']}")

            url = f"{MODEL_BASE_URL}{model}:streamGenerateContent?alt=sse&key={api_key}"
            payload = {"contents": history}
            if tools:
                payload["tools"] = tools

            with requests.post(url, headers=HEADERS, json=payload, stream=True, timeout=300) as resp:
                resp.raise_for_status()

                if 'text/event-stream' not in resp.headers.get('Content-Type', ''):
                    raise ValueError("响应非流式格式，请检查API端点或密钥权限。")

                # 流式处理响应
                for line in resp.iter_lines():
                    if line:
                        decoded_line = line.decode('utf-8')
                        if decoded_line.startswith('data: '):
                            try:
                                data = json.loads(decoded_line[6:])
                                text_chunk = data['candidates'][0]['content']['parts'][0]['text']
                                current_bot_response_full += text_chunk
                                yield f"data: {json.dumps({'text': text_chunk})}\n\n"
                            except (json.JSONDecodeError, KeyError, IndexError) as e:
                                print(f"解析响应数据块失败: {e}")
                                pass  # 忽略解析失败的行

                # 成功完成流，记录成功并跳出重试循环
                key_manager.record_success(api_key)
                last_successful_key = api_key
                print(f"API调用成功，使用密钥: {api_key}")
                break

        except NoAvailableKeysError as e:
            # 捕获自定义的异常
            print(f"获取密钥失败: {e}")
            error_msg = f"错误: {e}"
            current_bot_response_full = error_msg
            yield f"data: {json.dumps({'text': error_msg})}\n\n"
            break  # 没有可用密钥了，直接结束

        except requests.exceptions.HTTPError as e:
            print(f"请求失败 (API Key: {api_key}): {e}")
            status_code = e.response.status_code

            # 只记录一次失败，并根据错误代码处理密钥
            if api_key:
                # 先记录失败
                key_manager.record_failure(api_key, status_code)

                # 然后根据错误代码进行相应处理
                if status_code == 429:
                    key_manager.temporarily_suspend_key(api_key)
                    print(f"密钥 {api_key} 因达到速率限制被临时挂起")
                elif status_code in [400, 403]:
                    key_manager.mark_key_invalid(api_key)
                    print(f"密钥 {api_key} 因认证失败被永久移除")
                elif status_code >= 500:
                    key_manager.temporarily_suspend_key(api_key)
                    print(f"密钥 {api_key}因服务器错误被临时挂起")
                else:
                    # 对于其他错误，临时挂起而不是永久移除
                    key_manager.temporarily_suspend_key(api_key)
                    print(f"密钥 {api_key} 因错误 {status_code} 被临时挂起")

            if attempt >= max_retries - 1:  # 如果是最后一次尝试
                error_msg = f"所有密钥均尝试失败。最后错误状态码: {status_code}"
                current_bot_response_full = error_msg
                yield f"data: {json.dumps({'text': error_msg})}\n\n"

        except Exception as e:
            print(f"处理流时发生未知错误: {e}")
            # 记录未知错误
            if api_key:
                key_manager.record_failure(api_key, 0)  # 使用 0 表示未知错误
                key_manager.temporarily_suspend_key(api_key)  # 临时挂起而不是永久移除

            error_msg = f"处理流失败: {e}"
            current_bot_response_full = error_msg
            yield f"data: {json.dumps({'text': error_msg})}\n\n"

            if attempt >= max_retries - 1:  # 如果是最后一次尝试
                break

    # 输出当前密钥池状态
    status = key_manager.get_status()
    print(f"密钥池状态汇总:")
    print(f"- 可用密钥总数: {status['available_keys']}")
    print(f"- 挂起密钥总数: {status['suspended_keys']}")
    print(f"- 免费密钥连续失败: {status['free_key_consecutive_failures']}/{status['max_free_key_failures']}")

    for key_type, stats in status['key_statistics'].items():
        print(f"- {key_type}密钥: 总数：{stats['total']}, 可用：{stats['available']}, 挂起：{stats['suspended']}")

    # 将模型回复添加到 chat_history
    if current_bot_response_full:
        with chat_history_lock:
            # 只有当历史记录的最后一条是'user'时才添加'model'回复，防止重复添加
            if not chat_history or chat_history[-1]['role'] == 'user':
                chat_history.append({'role': 'model', 'parts': [{'text': current_bot_response_full}]})

    yield f"event: end\ndata: [DONE]\n\n"
    time.sleep(0.1)




# ----------------- 图片压缩（> 18.5 MB 自动压） -----------------
def maybe_compress_image(b64, target_kb=189400):
    # ... (代码不变) ...
    raw = base64.b64decode(b64)
    if len(raw) <= target_kb * 1024:
        return b64

    img = Image.open(io.BytesIO(raw))
    quality = 90
    while True:
        buff = io.BytesIO()
        img.save(buff, format='JPEG', quality=quality)
        if buff.tell() / 1024 <= target_kb or quality < 35:
            return base64.b64encode(buff.getvalue()).decode()
        quality -= 5


# ----------------- Flask 路由 -----------------
@app.route('/')
def index():
    return render_template('index.html', models=MODELS)



@app.route('/history')
def history():
    """
    给前端读取完整历史 → 前端自己渲染 markdown
    (这个接口保持不变，用于页面加载时恢复历史)
    """
    md_list = []
    # 遍历 history 时要小心，流式传输过程中可能 history 还没完全更新
    # 但这个接口主要用于初始加载，问题不大
    temp_history = list(chat_history) # 复制一份以防迭代时修改
    for msg in temp_history:
        if msg['role'] == 'user':
            md = ''
            for part in msg['parts']:
                if 'text' in part:
                    md += part['text'] + '\n'
                if 'inline_data' in part:
                    # 保持不变，只给占位符
                    md += '![图片](data:%s;base64,%s)\n' % (
                        part['inline_data']['mime_type'],
                        part['inline_data']['data'][:30]+'...')
            md_list.append({'who': 'user', 'md': md})
        elif msg['role'] == 'model': # 确保是 model 角色
             # 检查 parts 是否存在且不为空
             if 'parts' in msg and msg['parts'] and 'text' in msg['parts'][0]:
                 md_list.append({'who': 'bot',  'md': msg['parts'][0]['text']})
             else:
                 # 处理可能的空 parts 或格式问题
                 print(f"警告：历史记录中发现格式异常的 model 消息: {msg}")
                 md_list.append({'who': 'bot', 'md': '[空回复或格式错误]'})

    return jsonify(md_list)


@app.route('/chat', methods=['POST'])
def chat_initiate():
    """
    修改后的 /chat 接口: 只接收用户消息，存入历史，不调用 LLM。
    !! 在 chat_history 为空时，自动添加 BASE_PROMPT 到第一条用户消息中 !!
    """
    global chat_history, chat_history_lock # 确保可以修改全局变量
    with chat_history_lock:
        data = request.get_json(force=True)
        text  = (data.get('text') or '').strip()
        img_b64 = data.get('image')

        if not text and not img_b64:
            # 如果用户既没有输入文本也没有上传图片，则不允许发送
            # （即使是第一条消息，也需要用户触发，不能仅靠 BASE_PROMPT 发起）
            return jsonify({'error': '请输入内容或添加图片/截图'}), 400

        # -------- 组装 user 消息 --------
        parts = []

        # !! 核心改动：检查 chat_history 是否为空 !!
        is_first_message = not chat_history
        if is_first_message and BASE_PROMPT: # 确保 BASE_PROMPT 有内容
            # 如果是第一条消息，并且 BASE_PROMPT 非空，则将其作为第一个 part 添加
            parts.append({'text': BASE_PROMPT})

        # 添加用户实际输入的文本 (如果存在)
        if text:
            parts.append({'text': text})

        # 添加用户实际上传的图片 (如果存在)
        if img_b64:
            mime = 'image/png' # 假设截图和上传都是 png 或会被转为 png/jpeg
            try:
                # 尝试压缩图片，如果失败则记录错误但可能继续（取决于你的需求）
                img_b64 = maybe_compress_image(img_b64)
                parts.append({'inline_data': {'mime_type': mime, 'data': img_b64}})
            except Exception as e:
                print(f"Error compressing image: {e}")
                # 根据需要决定是否返回错误或继续（不带图片）
                # return jsonify({'error': '图片处理失败'}), 500
                # 这里选择继续，但不添加图片 part
                pass


        # 再次检查 parts 是否为空 (理论上，如果 text 或 img_b64 至少有一个，就不会为空)
        # 但如果 BASE_PROMPT 是唯一内容且用户未输入，前面的检查会阻止
        if not parts:
             print("Warning: Attempting to send an empty message after processing.")
             return jsonify({'error': '处理后内容为空'}), 400

        # 构造最终的用户消息
        user_msg = {'role': 'user', 'parts': parts}
        # 将构造好的用户消息添加到历史记录中
        chat_history.append(user_msg)

        # print("Chat history updated:", chat_history[-1]) # Debugging: 打印最后添加的消息

        # -------- 不再调用 LLM，直接返回成功 --------
        return jsonify({'ok': True})


@app.route('/stream')
def stream():
    """
    新的 SSE 端点，负责调用流式 LLM 并转发结果
    """
    # 从查询参数获取模型和搜索设置
    model = request.args.get('model', MODELS[0])
    enable_search_str = request.args.get('enable_search', 'false').lower()
    enable_search = enable_search_str == 'true'
    tools = [{"google_search": {}}] if enable_search else None

    # 检查 chat_history 是否为空或最后一个不是 user 消息 (理论上不应发生)
    if not chat_history or chat_history[-1]['role'] != 'user':
        def error_stream():
            yield f"event: error\ndata: {json.dumps({'text': '错误：无法开始流，聊天历史状态异常。'})}\n\n"
            yield f"event: end\ndata: [DONE]\n\n"
        return Response(error_stream(), mimetype='text/event-stream')

    # 使用流式生成器函数作为响应体
    # 传递当前的 chat_history 副本，避免在生成过程中被外部修改影响
    return Response(stream_gemini_response(list(chat_history), model, tools), mimetype='text/event-stream')




# ----------------- 主入口 -----------------
if __name__ == '__main__':
    # threaded=True 对于 SSE 是必要的
    app.run(debug=True, threaded=True, port=PORT, host='0.0.0.0')