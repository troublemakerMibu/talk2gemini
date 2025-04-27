import markdown2
import pygetwindow as gw
import pyautogui
import tkinter as tk
import logging
import requests
import base64
import io
from flask import Flask, render_template, request, jsonify, send_from_directory
from PIL import Image
from config import API_KEY, MODEL_BASE_URL, THRESHOLD_KB, PORT, BASE_PROMPT
URL = f"{MODEL_BASE_URL}?key={API_KEY}"
# compress_threshold_kb 用 THRESHOLD_KB

# ----------------- 基础配置 -----------------
HEADERS = {"Content-Type": "application/json"}

app = Flask(__name__, template_folder='templates')
chat_history = []          # 直接保存成 Google Gemini 兼容格式（role / parts）




# ------- 在原有 Flask 路由后面加上两个新接口 --------
@app.route('/reset', methods=['POST'])
def reset():
    chat_history.clear()
    return jsonify({'ok': True})
# ----------------- 调用大模型 -----------------
def call_gemini(history):
    resp = requests.post(URL, headers=HEADERS, json={"contents": history}, timeout=60)
    if resp.status_code == 200:
        return resp.json()['candidates'][0]['content']['parts'][0]['text']
    raise RuntimeError(f'Gemini Error {resp.status_code}: {resp.text[:200]}')

# ----------------- 图片压缩（> 3.6 MB 自动压） -----------------
def maybe_compress_image(b64, target_kb=3600):
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
    """
    交互式截图，返回 base64_png；若用户 ESC/取消则返回 None
    依赖：pyautogui、Pillow
    """

    # 1) 最小化当前前台窗口
    try:
        import pygetwindow as gw
        win = gw.getActiveWindow();  win.minimize()
    except Exception:  win = None

    # 2) 全屏透明窗口，记录鼠标框选
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

    def on_escape(e):                 # 允许 ESC 取消
        root.destroy()

    cv.bind('<ButtonPress-1>', on_down)
    cv.bind('<B1-Motion>',      on_move)
    cv.bind('<ButtonRelease-1>',on_up)
    root.bind('<Escape>', on_escape)
    root.mainloop()

    # 3) 还原窗口
    try:
        if win: win.restore()
    except Exception:
        pass

    # 4) 真正截图
    if 'val' not in bbox:      # 用户取消
        return None
    x1,y1,x2,y2 = bbox['val']
    w,h = x2-x1, y2-y1
    if w<5 or h<5:             # 面积太小视为误操作
        return None
    img = pyautogui.screenshot(region=(x1, y1, w, h))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()

# ----------------- Flask 路由 -----------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/history')
def history():
    """
    给前端读取完整历史 → 前端自己渲染 markdown
    """
    md_list = []
    for msg in chat_history:
        if msg['role'] == 'user':
            # 混合文字 + 图片
            md = ''
            for part in msg['parts']:
                if 'text' in part:
                    md += part['text'] + '\n'
                if 'inline_data' in part:
                    md += '![图片](data:%s;base64,%s)\n' % (
                        part['inline_data']['mime_type'],
                        part['inline_data']['data'][:30]+'...')  # 只给前端一个占位（节省流量）
            md_list.append({'who': 'user', 'md': md})
        else:
            md_list.append({'who': 'bot',  'md': msg['parts'][0]['text']})
    return jsonify(md_list)

@app.route('/chat', methods=['POST'])
def chat():
    """
    data = {text: str, image: base64 or null}
    """
    data = request.get_json(force=True)
    text  = (data.get('text') or '').strip()
    img_b64 = data.get('image')          # 可能为空

    if not text and not img_b64:
        return jsonify({'error': 'empty'}), 400

    # -------- 组装 user 消息 --------
    parts = []
    if not chat_history:
        parts.append({'text': BASE_PROMPT})
    if text:
        parts.append({'text': text})
    if img_b64:
        mime = 'image/png'               # 前端截图是 PNG；上传图片也按 accept="image/*" 读作 dataURL
        img_b64 = maybe_compress_image(img_b64)
        parts.append({'inline_data': {'mime_type': mime, 'data': img_b64}})
    user_msg = {'role': 'user', 'parts': parts}
    chat_history.append(user_msg)

    # -------- 调 LLM --------
    try:
        answer = call_gemini(chat_history)
    except Exception as e:
        answer = f"出错了：{e}"

    # -------- 保存 model 消息并返回 --------
    bot_msg = {'role': 'model', 'parts': [{'text': answer}]}
    chat_history.append(bot_msg)
    return jsonify({'answer': answer})

@app.route('/screenshot', methods=['POST'])
def screenshot():
    img_b64 = grab_screen_interactive()
    if img_b64:
        return jsonify({'img': img_b64})
    return jsonify({'img': None}), 500

# ----------------- 主入口 -----------------
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    app.run(debug=True, threaded=True, port=PORT, host='127.0.0.1')