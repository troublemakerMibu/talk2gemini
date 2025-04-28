import os, yaml, pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent
CFG_PATH = BASE_DIR / "config.yaml"

def load_yaml(path=CFG_PATH):
    if not path.exists():
        raise FileNotFoundError(f"缺少配置文件 {path}")
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

_yaml = load_yaml()

# ============ 供外部直接 import 使用 =============
API_KEY        = _yaml.get("api_key")
MODEL_BASE_URL = _yaml["base_url"]  # 只保留基础URL
THRESHOLD_KB   = int(_yaml.get("threshold_kb"))
PORT           = int(_yaml.get("port"))
BASE_PROMPT    = _yaml.get("base_prompt")
MODELS         = _yaml.get("models", [])  # 读取模型列表
if not API_KEY:
    raise RuntimeError("请在 .env 中设置 GEMINI_KEY 或在 config.yaml 中写 api_key")
