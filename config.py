import os, yaml, pathlib
from dotenv import load_dotenv     # pip install python-dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent
CFG_PATH = BASE_DIR / "config.yaml"

# 先把 .env 里的 KEY 读进来
load_dotenv(BASE_DIR / ".env", override=False)

def load_yaml(path=CFG_PATH):
    if not path.exists():
        raise FileNotFoundError(f"缺少配置文件 {path}")
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

_yaml = load_yaml()

# ============ 供外部直接 import 使用 =============
API_KEY        = os.getenv("GEMINI_KEY") or _yaml.get("api_key")
MODEL_BASE_URL = _yaml["url"]
THRESHOLD_KB   = int(_yaml.get("threshold_kb"))
PORT           = int(_yaml.get("port"))
BASE_PROMPT    = _yaml.get("base_prompt")
if not API_KEY:
    raise RuntimeError("请在 .env 中设置 GEMINI_KEY 或在 config.yaml 中写 api_key")