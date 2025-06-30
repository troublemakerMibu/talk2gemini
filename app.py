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
# app.py 中 stream_gemini_response 的新版本

def stream_gemini_response(history, model, tools=None):
    global current_bot_response_full, chat_history, last_successful_key
    current_bot_response_full = ""

    # 【变化】从新的 status 字典获取总密钥数作为最大重试次数
    max_retries = key_manager.get_status()["total_keys_in_pool"]
    if max_retries == 0:
        error_msg = "错误：密钥池中没有可用的密钥。"
        current_bot_response_full = error_msg
        yield f"data: {json.dumps({'text': error_msg})}\n\n"
        yield f"event: end\ndata: [DONE]\n\n"
        return

    for attempt in range(max_retries):
        api_key = None  # 在 try 外部定义，便于 except 块中引用
        try:
            # 【核心改动】调用新的 get_key 方法，并传入上一次成功的 key 作为首选
            api_key = key_manager.get_key(preferred_key=last_successful_key)

            print(f"正在使用 API Key: {api_key}... (尝试 {attempt + 1}/{max_retries})")
            url = f"{MODEL_BASE_URL}{model}:streamGenerateContent?alt=sse&key={api_key}"
            payload = {"contents": history}
            if tools:
                payload["tools"] = tools

            with requests.post(url, headers=HEADERS, json=payload, stream=True, timeout=300) as resp:
                resp.raise_for_status()

                if 'text/event-stream' not in resp.headers.get('Content-Type', ''):
                    raise ValueError("响应非流式格式，请检查API端点或密钥权限。")

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

                # 成功完成流，直接跳出重试循环
                # 【核心改动】请求成功，记录下这个key
                last_successful_key = api_key
                break

        except NoAvailableKeysError as e:
            # 【变化】捕获我们自定义的异常
            print(f"获取密钥失败: {e}")
            error_msg = f"错误: {e}"
            current_bot_response_full = error_msg
            yield f"data: {json.dumps({'text': error_msg})}\n\n"
            break  # 没有可用密钥了，直接结束

        except requests.exceptions.HTTPError as e:
            # 【变化】调用 suspend/mark_invalid 时不再需要传入 cooldown 参数
            print(f"请求失败 (API Key: {api_key}...): {e}")
            if e.response.status_code == 429:
                key_manager.temporarily_suspend_key(api_key)
            elif e.response.status_code in [400, 403]:
                key_manager.mark_key_invalid(api_key)
            elif e.response.status_code >= 500:
                key_manager.temporarily_suspend_key(api_key)
            else:
                key_manager.mark_key_invalid(api_key)

            if attempt >= max_retries - 1:  # 如果是最后一次尝试
                error_msg = f"所有密钥均尝试失败。最后错误状态码: {e.response.status_code}"
                current_bot_response_full = error_msg
                yield f"data: {json.dumps({'text': error_msg})}\n\n"

        except Exception as e:
            print(f"处理流时发生未知错误: {e}")
            error_msg = f"处理流失败: {e}"
            current_bot_response_full = error_msg
            yield f"data: {json.dumps({'text': error_msg})}\n\n"
            break  # 发生未知严重错误，终止重试

    print(f"当前密钥状态: {key_manager.get_status()}")

    # 将模型回复添加到 chat_history
    if current_bot_response_full:
        with chat_history_lock:
            # 只有当历史记录的最后一条是'user'时才添加'model'回复，防止重复添加
            if not chat_history or chat_history[-1]['role'] == 'user':
                chat_history.append({'role': 'model', 'parts': [{'text': current_bot_response_full}]})

    yield f"event: end\ndata: [DONE]\n\n"
    time.sleep(0.1)

@app.route('/export', methods=['GET'])
def export_history():
    """
    导出对话历史为HTML格式
    """
    global chat_history

    # 获取当前时间
    now = datetime.now()
    timestamp = now.strftime("%y%m%d_%H%M")
    # 使用英文文件名避免编码问题
    filename_display = f"{timestamp}对话历史.html"
    # URL编码的文件名
    filename_encoded = quote(filename_display.encode('utf-8'))

    # 改进后的HTML模板，添加代码复制功能
    html_template = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>

    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            background-color: #f5f7fa;
            padding: 20px;
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            background-color: white;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }}
        .header h1 {{
            font-size: 28px;
            margin-bottom: 10px;
        }}
        .header .meta {{
            font-size: 14px;
            opacity: 0.9;
        }}
        .conversation {{
            padding: 40px;
        }}
        .message {{
            margin-bottom: 30px;
            display: flex;
            flex-direction: column;
            align-items: flex-start;
        }}
        .message.user {{
            align-items: flex-end;
        }}

        /* 角色标识样式 */
        .message-header {{
            display: flex;
            align-items: center;
            margin-bottom: 8px;
            gap: 10px;
        }}
        .message.user .message-header {{
            flex-direction: row-reverse;
        }}
        .avatar {{
            width: 36px;
            height: 36px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            color: white;
            font-size: 16px;
        }}
        .message.user .avatar {{
            background-color: #0084ff;
        }}
        .message.bot .avatar {{
            background-color: #7c3aed;
        }}
        .message-role {{
            font-size: 15px;
            font-weight: 600;
            color: #555;
        }}

        /* 消息内容样式 */
        .message-content {{
            max-width: 85%;
            width: 100%;
            padding: 16px 20px;
            border-radius: 12px;
            position: relative;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            font-size: 15px;
        }}
        .message.user .message-content {{
            background-color: #e3f2fd;
            color: #1565c0;
            border: 1px solid #bbdefb;
        }}
        .message.bot .message-content {{
            background-color: #f3f4f6;
            color: #1f2937;
            border: 1px solid #e5e7eb;
        }}

        /* 代码块样式 */
        .code-block-wrapper {{
            position: relative;
            margin: 15px 0;
        }}
        .message-content pre {{
            background-color: #1e293b;
            color: #e2e8f0;
            padding: 15px;
            padding-top: 40px; /* 为复制按钮留出空间 */
            border-radius: 8px;
            overflow-x: auto;
            font-size: 14px;
            margin: 0;
        }}

        /* 复制按钮样式 */
        .copy-button {{
            position: absolute;
            top: 8px;
            right: 8px;
            background-color: #475569;
            color: #e2e8f0;
            border: none;
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 12px;
            cursor: pointer;
            transition: all 0.2s ease;
            font-family: inherit;
        }}
        .copy-button:hover {{
            background-color: #64748b;
            transform: translateY(-1px);
        }}
        .copy-button:active {{
            transform: translateY(0);
        }}
        .copy-button.copied {{
            background-color: #10b981;
        }}

        /* 行内代码样式 */
        .message-content code {{
            background-color: #e2e8f0;
            color: #0f172a;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
            font-size: 0.9em;
        }}
        .message-content pre code {{
            background-color: transparent;
            color: #e2e8f0;
            padding: 0;
        }}

        /* 数学公式样式 */
        .math {{
            font-family: 'Times New Roman', Times, serif;
            font-style: italic;
            margin: 0 0.25em;
            display: inline-block;
        }}
        .math-block {{
            display: block;
            text-align: center;
            margin: 1.2em 0;
            font-family: 'Times New Roman', Times, serif;
            font-size: 1.15em;
            overflow-x: auto;
            padding: 0.5em 0;
        }}
        /* 分数样式 */
        .frac {{
            display: inline-block;
            vertical-align: middle;
            text-align: center;
            position: relative;
            font-style: normal;
        }}
        .frac .num {{
            display: block;
            border-bottom: 1px solid currentColor;
            padding-bottom: 0.1em;
        }}
        .frac .den {{
            display: block;
            padding-top: 0.1em;
        }}
        /* 上标和下标 */
        .math sup, .math sub, .math-block sup, .math-block sub {{
            font-size: 0.75em;
        }}
        .math sup {{
            vertical-align: super;
        }}
        .math sub {{
            vertical-align: sub;
        }}
        /* 根号 */
        .sqrt {{
            position: relative;
            padding-left: 0.5em;
            padding-top: 0.1em;
            margin-left: 0.2em;
        }}
        .sqrt::before {{
            content: '√';
            position: absolute;
            left: 0;
            top: 0;
            font-size: 1.2em;
        }}
        .sqrt.has-index::before {{
            font-size: 1em;
        }}
        .sqrt-index {{
            position: absolute;
            left: 0;
            top: -0.5em;
            font-size: 0.6em;
        }}

        /* 内容格式化样式 */
        .message-content img {{
            max-width: 100%;
            height: auto;
            border-radius: 8px;
            margin-top: 10px;
            display: block;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        .message-content p {{
            margin-bottom: 12px;
            line-height: 1.7;
        }}
        .message-content p:last-child {{
            margin-bottom: 0;
        }}
        .message-content ul, .message-content ol {{
            margin: 12px 0;
            padding-left: 30px;
        }}
        .message-content li {{
            margin-bottom: 6px;
        }}
        .message-content h1, .message-content h2, .message-content h3 {{
            margin: 20px 0 12px 0;
            font-weight: 600;
        }}
        .message-content h1 {{
            font-size: 24px;
        }}
        .message-content h2 {{
            font-size: 20px;
        }}
        .message-content h3 {{
            font-size: 18px;
        }}

        /* 表格样式 */
        .message-content table {{
            border-collapse: collapse;
            width: 100%;
            margin: 15px 0;
        }}
        .message-content th, .message-content td {{
            border: 1px solid #e5e7eb;
            padding: 8px 12px;
            text-align: left;
        }}
        .message-content th {{
            background-color: #f9fafb;
            font-weight: 600;
        }}

        .footer {{
            text-align: center;
            padding: 20px;
            color: #666;
            font-size: 14px;
            border-top: 1px solid #eee;
        }}

        /* 响应式设计 */
        @media (max-width: 768px) {{
            .container {{
                margin: 0;
                border-radius: 0;
            }}
            .header {{
                padding: 20px;
            }}
            .header h1 {{
                font-size: 22px;
            }}
            .conversation {{
                padding: 20px;
            }}
            .message-content {{
                max-width: 100%;
                font-size: 14px;
            }}
            .copy-button {{
                padding: 4px 8px;
                font-size: 11px;
            }}
        }}

        @media print {{
            body {{
                background: white;
                padding: 0;
            }}
            .container {{
                box-shadow: none;
                max-width: 100%;
            }}
            .copy-button {{
                display: none !important;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>对话历史记录</h1>
            <div class="meta">
                <div>导出时间：{export_time}</div>
            </div>
        </div>
        <div class="conversation">
            {messages}
        </div>
        <div class="footer">
            <p>成功导出共 {message_count} 条消息</p>
        </div>
    </div>

    <script>
        // 代码复制功能
        function copyCode(button, codeId) {{
            const codeElement = document.getElementById(codeId);
            if (!codeElement) {{
                console.error('找不到代码元素:', codeId);
                return;
            }}

            // 获取代码文本，去除HTML标签
            const codeText = codeElement.textContent || codeElement.innerText;

            // 使用现代的Clipboard API（如果可用）
            if (navigator.clipboard && navigator.clipboard.writeText) {{
                navigator.clipboard.writeText(codeText).then(() => {{
                    // 更新按钮状态
                    const originalText = button.textContent;
                    button.textContent = '已复制';
                    button.classList.add('copied');

                    setTimeout(() => {{
                        button.textContent = originalText;
                        button.classList.remove('copied');
                    }}, 2000);
                }}).catch(err => {{
                    console.error('复制失败:', err);
                    fallbackCopy(codeText, button);
                }});
            }} else {{
                // 降级方案
                fallbackCopy(codeText, button);
            }}
        }}

        // 降级的复制方案
        function fallbackCopy(text, button) {{
            const textarea = document.createElement('textarea');
            textarea.value = text;
            textarea.style.position = 'fixed';
            textarea.style.top = '0';
            textarea.style.left = '-999999px';
            textarea.setAttribute('readonly', '');
            document.body.appendChild(textarea);

            try {{
                textarea.select();
                textarea.setSelectionRange(0, 99999); // 移动设备兼容

                const successful = document.execCommand('copy');
                if (successful) {{
                    const originalText = button.textContent;
                    button.textContent = '已复制';
                    button.classList.add('copied');

                    setTimeout(() => {{
                        button.textContent = originalText;
                        button.classList.remove('copied');
                    }}, 2000);
                }} else {{
                    alert('复制失败，请手动选择并复制代码');
                }}
            }} catch (err) {{
                console.error('复制失败:', err);
                alert('复制失败，请手动选择并复制代码');
            }} finally {{
                document.body.removeChild(textarea);
            }}
        }}

        // 简单的数学公式渲染器
        function renderMath() {{
            // 处理块级公式
            document.querySelectorAll('.math-block').forEach(function(elem) {{
                let content = elem.innerHTML;
                // 处理分数
                content = content.replace(/\\\\\\\\frac{{([^}}]+)}}{{([^}}]+)}}/g, '<span class="frac"><span class="num">$1</span><span class="den">$2</span></span>');
                // 处理平方根
                content = content.replace(/\\\\\\\\sqrt{{([^}}]+)}}/g, '<span class="sqrt">$1</span>');
                // 处理带指数的根号
                content = content.replace(/\\\\\\\\sqrt\\[([^\\]]+)\\]{{([^}}]+)}}/g, '<span class="sqrt has-index"><span class="sqrt-index">$1</span>$2</span>');
                elem.innerHTML = content;
            }});

            // 处理行内公式
            document.querySelectorAll('.math').forEach(function(elem) {{
                let content = elem.innerHTML;
                // 处理分数
                content = content.replace(/\\\\\\\\frac{{([^}}]+)}}{{([^}}]+)}}/g, '<span class="frac"><span class="num">$1</span><span class="den">$2</span></span>');
                // 处理平方根
                content = content.replace(/\\\\\\\\sqrt{{([^}}]+)}}/g, '<span class="sqrt">$1</span>');
                elem.innerHTML = content;
            }});
        }}

        // 页面加载完成后执行
        document.addEventListener('DOMContentLoaded', renderMath);
    </script>
</body>
</html>"""

    # 处理数学公式的函数
    def process_math_formulas(text):
        """处理文本中的数学公式"""
        import re

        # 首先保护代码块中的内容
        code_blocks = []
        def save_code_block(match):
            code_blocks.append(match.group(0))
            return f"__CODE_BLOCK_{len(code_blocks)-1}__"

        # 保存代码块
        text = re.sub(r'```[\\s\\S]*?```', save_code_block, text)
        text = re.sub(r'`[^`]+`', save_code_block, text)

        # 处理块级公式
        text = re.sub(r'\\$\\$(.*?)\\$\\$', lambda m: f'<div class="math-block">{m.group(1)}</div>', text, flags=re.DOTALL)

        # 处理行内公式
        text = re.sub(r'\\$([^\\$]+)\\$', lambda m: f'<span class="math">{m.group(1)}</span>', text)

        # 替换常见的数学符号
        math_symbols = {
            r'\\\\alpha': 'α', r'\\\\beta': 'β', r'\\\\gamma': 'γ', r'\\\\delta': 'δ',
            r'\\\\epsilon': 'ε', r'\\\\zeta': 'ζ', r'\\\\eta': 'η', r'\\\\theta': 'θ',
            r'\\\\iota': 'ι', r'\\\\kappa': 'κ', r'\\\\lambda': 'λ', r'\\\\mu': 'μ',
            r'\\\\nu': 'ν', r'\\\\xi': 'ξ', r'\\\\pi': 'π', r'\\\\rho': 'ρ',
            r'\\\\sigma': 'σ', r'\\\\tau': 'τ', r'\\\\upsilon': 'υ', r'\\\\phi': 'φ',
            r'\\\\chi': 'χ', r'\\\\psi': 'ψ', r'\\\\omega': 'ω',
            r'\\\\Alpha': 'Α', r'\\\\Beta': 'Β', r'\\\\Gamma': 'Γ', r'\\\\Delta': 'Δ',
            r'\\\\Epsilon': 'Ε', r'\\\\Zeta': 'Ζ', r'\\\\Eta': 'Η', r'\\\\Theta': 'Θ',
            r'\\\\Iota': 'Ι', r'\\\\Kappa': 'Κ', r'\\\\Lambda': 'Λ', r'\\\\Mu': 'Μ',
            r'\\\\Nu': 'Ν', r'\\\\Xi': 'Ξ', r'\\\\Pi': 'Π', r'\\\\Rho': 'Ρ',
            r'\\\\Sigma': 'Σ', r'\\\\Tau': 'Τ', r'\\\\Upsilon': 'Υ', r'\\\\Phi': 'Φ',
            r'\\\\Chi': 'Χ', r'\\\\Psi': 'Ψ', r'\\\\Omega': 'Ω',
            r'\\\\sum': '∑', r'\\\\prod': '∏', r'\\\\int': '∫', r'\\\\oint': '∮',
            r'\\\\partial': '∂', r'\\\\nabla': '∇', r'\\\\pm': '±', r'\\\\mp': '∓',
            r'\\\\times': '×', r'\\\\div': '÷', r'\\\\cdot': '·', r'\\\\circ': '∘',
            r'\\\\bullet': '•', r'\\\\ldots': '…', r'\\\\cdots': '⋯', r'\\\\vdots': '⋮',
            r'\\\\ddots': '⋱', r'\\\\leq': '≤', r'\\\\geq': '≥', r'\\\\neq': '≠',
            r'\\\\approx': '≈', r'\\\\equiv': '≡', r'\\\\sim': '∼', r'\\\\simeq': '≃',
            r'\\\\propto': '∝', r'\\\\infty': '∞', r'\\\\in': '∈', r'\\\\notin': '∉',
            r'\\\\subset': '⊂', r'\\\\supset': '⊃', r'\\\\subseteq': '⊆', r'\\\\supseteq': '⊇',
            r'\\\\cup': '∪', r'\\\\cap': '∩', r'\\\\emptyset': '∅', r'\\\\forall': '∀',
            r'\\\\exists': '∃', r'\\\\neg': '¬', r'\\\\land': '∧', r'\\\\lor': '∨',
            r'\\\\rightarrow': '→', r'\\\\leftarrow': '←', r'\\\\leftrightarrow': '↔',
            r'\\\\Rightarrow': '⇒', r'\\\\Leftarrow': '⇐', r'\\\\Leftrightarrow': '⇔',
            r'\\\\uparrow': '↑', r'\\\\downarrow': '↓', r'\\\\updownarrow': '↕',
            r'\\\\angle': '∠', r'\\\\perp': '⊥', r'\\\\parallel': '∥',
        }

        for pattern, symbol in math_symbols.items():
            text = text.replace(pattern, symbol)

        # 处理上标和下标
        text = re.sub(r'\\^{{([^}}]+)}}', r'<sup>\\1</sup>', text)
        text = re.sub(r'\\^(\\w)', r'<sup>\\1</sup>', text)
        text = re.sub(r'_{{([^}}]+)}}', r'<sub>\\1</sub>', text)
        text = re.sub(r'_(\\w)', r'<sub>\\1</sub>', text)

        # 恢复代码块
        for i, code in enumerate(code_blocks):
            text = text.replace(f"__CODE_BLOCK_{i}__", code)

        return text

    # 修改markdown2的处理，为代码块添加包装器
    def process_code_blocks(html_content):
        """为代码块添加复制按钮"""
        import re
        import uuid

        def wrap_code_block(match):
            code_block = match.group(0)
            code_id = f"code-{uuid.uuid4().hex[:8]}"

            # 提取代码内容
            code_match = re.search(r'<pre><code[^>]*?>(.*?)</code></pre>', code_block, re.DOTALL)
            if code_match:
                code_content = code_match.group(1)
                # 创建包装器
                wrapped = f'''<div class="code-block-wrapper">
                    <button class="copy-button" onclick="copyCode(this, '{code_id}')">
                        复制
                    </button>
                    <pre><code id="{code_id}">{code_content}</code></pre>
                </div>'''
                return wrapped
            return code_block

        # 查找所有代码块并添加包装器
        html_content = re.sub(r'<pre><code[^>]*?>.*?</code></pre>', wrap_code_block, html_content, flags=re.DOTALL)
        return html_content

    # 构建消息HTML
    messages_html = []
    message_count = 0

    for msg in chat_history:
        message_count += 1
        role = msg['role']
        role_display = '用户' if role == 'user' else 'AI助手'
        message_class = 'user' if role == 'user' else 'bot'
        avatar_text = '我' if role == 'user' else 'AI'

        content_parts = []
        for part in msg.get('parts', []):
            if 'text' in part:
                # 先处理数学公式
                text_with_math = process_math_formulas(part['text'])

                # 转换Markdown到HTML
                text_html = markdown2.markdown(
                    text_with_math,
                    extras=['fenced-code-blocks', 'tables', 'break-on-newline', 'code-friendly']
                )

                # 为代码块添加复制按钮
                text_html = process_code_blocks(text_html)

                content_parts.append(text_html)
            elif 'inline_data' in part:
                # 嵌入图片
                img_html = f'<img src="data:{part["inline_data"]["mime_type"]};base64,{part["inline_data"]["data"]}" alt="图片">'
                content_parts.append(img_html)

        # 改进的消息HTML结构
        message_html = f'''
        <div class="message {message_class}">
            <div class="message-header">
                <div class="avatar">{avatar_text}</div>
                <div class="message-role">{role_display}</div>
            </div>
            <div class="message-content">
                {''.join(content_parts)}
            </div>
        </div>
        '''
        messages_html.append(message_html)

    # 填充模板
    html_content = html_template.format(
        title=now.strftime("%Y年%m月%d日 %H:%M") + " 对话历史",
        export_time=now.strftime("%Y年%m月%d日 %H:%M:%S"),
        messages=''.join(messages_html),
        message_count=message_count
    )

    timestamp = now.strftime("%y%m%d_%H%M")
    filename = f"chat_history_{timestamp}.html"

    # 创建响应，使用URL编码的文件名
    response = Response(
        html_content,
        mimetype='text/html',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Type': 'text/html; charset=utf-8'
        }
    )

    return response


    # 修改markdown2的处理，为代码块添加包装器
    def process_code_blocks(html_content):
        """为代码块添加复制按钮"""
        import re
        import uuid

        def wrap_code_block(match):
            code_block = match.group(0)
            code_id = f"code-{uuid.uuid4().hex[:8]}"

            # 提取代码内容（去除<pre><code>标签）
            code_match = re.search(r'<pre><code[^>]*>(.*?)</code></pre>', code_block, re.DOTALL)
            if code_match:
                # 创建包装器
                wrapped = f'''<div class="code-block-wrapper">
                    <button class="copy-button" onclick="copyCode(this, '{code_id}')">
                        复制
                    </button>
                    <pre><code id="{code_id}">{code_match.group(1)}</code></pre>
                </div>'''
                return wrapped
            return code_block

        # 查找所有代码块并添加包装器
        html_content = re.sub(r'<pre><code[^>]*>.*?</code></pre>', wrap_code_block, html_content, flags=re.DOTALL)
        return html_content

    # 构建消息HTML
    messages_html = []
    message_count = 0

    for msg in chat_history:
        message_count += 1
        role = msg['role']
        role_display = '用户' if role == 'user' else 'AI助手'
        message_class = 'user' if role == 'user' else 'bot'
        avatar_text = '我' if role == 'user' else 'AI'

        content_parts = []
        for part in msg.get('parts', []):
            if 'text' in part:
                # 先处理数学公式
                text_with_math = process_math_formulas(part['text'])

                # 转换Markdown到HTML
                text_html = markdown2.markdown(
                    text_with_math,
                    extras=['fenced-code-blocks', 'tables', 'break-on-newline', 'code-friendly']
                )

                # 为代码块添加复制按钮
                text_html = process_code_blocks(text_html)

                content_parts.append(text_html)
            elif 'inline_data' in part:
                # 嵌入图片
                img_html = f'<img src="data:{part["inline_data"]["mime_type"]};base64,{part["inline_data"]["data"]}" alt="图片">'
                content_parts.append(img_html)

        # 改进的消息HTML结构
        message_html = f'''
        <div class="message {message_class}">
            <div class="message-header">
                <div class="avatar">{avatar_text}</div>
                <div class="message-role">{role_display}</div>
            </div>
            <div class="message-content">
                {''.join(content_parts)}
            </div>
        </div>
        '''
        messages_html.append(message_html)

    # 填充模板
    html_content = html_template.format(
        title=now.strftime("%Y年%m月%d日 %H:%M") + " 对话历史",
        export_time=now.strftime("%Y年%m月%d日 %H:%M:%S"),
        messages=''.join(messages_html),
        message_count=message_count
    )

    timestamp = now.strftime("%y%m%d_%H%M")
    filename = f"chat_history_{timestamp}.html"

    # 创建响应，使用URL编码的文件名
    response = Response(
        html_content,
        mimetype='text/html',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Type': 'text/html; charset=utf-8'
        }
    )

    return response




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