import time
import markdown2
import pygetwindow as gw # 截图部分保持不变
import pyautogui
import tkinter as tk
import requests
import base64
import io
import json # 确保导入 json
from flask import Flask, Response, render_template, request, jsonify, send_from_directory, render_template_string # 引入 Response 和 render_template_string
from config import key_manager, MODEL_BASE_URL, PORT, BASE_PROMPT, MODELS
from PIL import Image
from threading import Lock
# from jinja2 import Template

# ----------------- 基础配置 -----------------
HEADERS = {"Content-Type": "application/json"}
chat_history_lock = Lock()  # 全局锁
last_used_key = None
last_used_key_lock = Lock()

app = Flask(__name__, template_folder='templates')
chat_history = [] # 直接保存成 Google Gemini 兼容格式（role / parts）
# 用于存储流式传输中积累的完整机器人回复
current_bot_response_full = ""

# ------- 在原有 Flask 路由后面加上两个新接口 --------
@app.route('/reset', methods=['POST'])
def reset():
    global chat_history, current_bot_response_full
    chat_history.clear()
    current_bot_response_full = "" # 重置时也清空
    return jsonify({'ok': True})

# ----------------- 调用大模型 (流式) -----------------
def stream_gemini_response(history, model, tools=None):
    global current_bot_response_full, chat_history, last_used_key_lock, last_used_key
    current_bot_response_full = ""
    max_retries = key_manager.get_status()["valid_keys"]
    for attempt in range(max_retries+1):
        try:
            with last_used_key_lock:
                # 优先使用上一次的key
                if last_used_key is not None:
                    api_key = last_used_key
                else:
                    api_key = key_manager.get_next_key()
            print(f"正在使用 API Key: {api_key}")
            url = f"{MODEL_BASE_URL}{model}:streamGenerateContent?alt=sse&key={api_key}"
            payload = {"contents": history}
            if tools:
                payload["tools"] = tools

            with requests.post(url, headers=HEADERS, json=payload, stream=True, timeout=300) as resp:
                resp.raise_for_status()

                if 'text/event-stream' not in resp.headers.get('Content-Type', ''):
                    raise ValueError("非流式响应")

                for line in resp.iter_lines():
                    if line:
                        decoded_line = line.decode('utf-8')
                        if decoded_line.startswith('data: '):
                            try:
                                data = json.loads(decoded_line[6:])
                                text_chunk = data['candidates'][0]['content']['parts'][0]['text']
                                current_bot_response_full += text_chunk
                                yield f"data: {json.dumps({'text': text_chunk})}\n\n"
                            except Exception as e:
                                print(f"解析响应失败: {e}")
                                pass

                # 成功完成流，更新 last_used_key
                with last_used_key_lock:
                    last_used_key = api_key
                break

        except requests.exceptions.RequestException as e:
            print(f"请求失败 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries:
                if e.response.status_code == 429:
                    key_manager.temporarily_suspend_key(api_key, cooldown=300)
                    print(f"[速率限制] API Key '{api_key}' 已被挂起，将在 300 秒后恢复。")
                    with last_used_key_lock:
                        last_used_key = None
                elif e.response.status_code in [400, 403]:
                    key_manager.mark_key_invalid(api_key)
                    with last_used_key_lock:
                        last_used_key  = None
                else:
                    key_manager.mark_key_invalid(api_key)
                    with last_used_key_lock:
                        last_used_key  = None
            else:
                error_msg = f"请求失败: {e}"
                current_bot_response_full = f"请求失败,错误状态码： {e.response.status_code}"
                yield f"data: {json.dumps({'text': error_msg})}\n\n"

        except Exception as e:
            print(f"处理流失败: {e}")
            error_msg = f"处理流失败: {e}"
            current_bot_response_full = error_msg
            yield f"data: {json.dumps({'text': error_msg})}\n\n"
    print(key_manager.get_status())
    # 将模型回复添加到 chat_history
    if current_bot_response_full:
        with chat_history_lock:
            chat_history.append({'role': 'model', 'parts': [{'text': current_bot_response_full}]})

    yield f"event: end\ndata: [DONE]\n\n"
    time.sleep(0.1)







# ----------------- 图片压缩（> 3.6 MB 自动压） -----------------
def maybe_compress_image(b64, target_kb=3600):
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

# ----------------- 截图（最小化浏览器 + 框选区域） -----------------
def grab_screen_interactive():
    # ... (代码不变) ...
    try:
        import pygetwindow as gw
        win = gw.getActiveWindow();  win.minimize()
    except Exception:  win = None
    start = {}; bbox = {}
    root = tk.Tk()
    root.attributes('-fullscreen', True)
    root.attributes('-alpha', 0.25)
    root.configure(bg='black')
    cv = tk.Canvas(root, cursor='crosshair')
    cv.pack(fill='both', expand=True)
    rect = None
    def on_down(e):
        start['x'] = root.winfo_pointerx()
        start['y'] = root.winfo_pointery()
        nonlocal rect
        if rect: cv.delete(rect)
        rect = cv.create_rectangle(e.x, e.y, e.x, e.y, outline='red', width=2)
    def on_move(e):
        if rect:
            cv.coords(rect,
                      start['x']-root.winfo_rootx(), start['y']-root.winfo_rooty(),
                      e.x, e.y)
    def on_up(e):
        endx, endy = root.winfo_pointerx(), root.winfo_pointery()
        bbox['val'] = (min(start['x'],endx), min(start['y'],endy),
                       max(start['x'],endx), max(start['y'],endy))
        root.destroy()
    def on_escape(e):
        root.destroy()
    cv.bind('<ButtonPress-1>', on_down)
    cv.bind('<B1-Motion>',      on_move)
    cv.bind('<ButtonRelease-1>',on_up)
    root.bind('<Escape>', on_escape)
    root.mainloop()
    try:
        if win: win.restore()
    except Exception:
        pass
    if 'val' not in bbox:
        return None
    x1,y1,x2,y2 = bbox['val']
    w,h = x2-x1, y2-y1
    if w<5 or h<5:
        return None
    img = pyautogui.screenshot(region=(x1, y1, w, h))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()

# ----------------- Flask 路由 -----------------
@app.route('/')
def index():
    return render_template('index.html', models=MODELS)
    # 为了简单起见，直接渲染字符串模板，避免文件依赖问题
    # 在实际项目中，推荐使用 render_template
    # with open('templates/index.html', 'r', encoding='utf-8') as f:
    #     html_content = f.read()
    # # 手动替换模板变量（如果 index.html 中有 {{ models }}）
    # model_options = "".join([f'<option value="{model}">{model}</option>' for model in MODELS])
    # html_content = html_content.replace('{% for model in models %}{% endfor %}', model_options) # 简陋替换
    # # 或者更健壮的方式是用 Jinja2 渲染字符串
    #
    # template = Template(html_content)
    # return template.render(models=MODELS)


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


@app.route('/screenshot', methods=['POST'])
def screenshot():
    # ... (代码不变) ...
    img_b64 = grab_screen_interactive()
    if img_b64:
        return jsonify({'img': img_b64})
    return jsonify({'img': None}), 500

# ----------------- 主入口 -----------------
if __name__ == '__main__':
    # threaded=True 对于 SSE 是必要的
    app.run(debug=True, threaded=True, port=PORT, host='127.0.0.1')