import yaml, pathlib
from threading import Lock
from datetime import timedelta, datetime

BASE_DIR = pathlib.Path(__file__).resolve().parent
CFG_PATH = BASE_DIR / "config.yaml"
KEY_PATH = BASE_DIR / "key.txt"
# 线程安全的API密钥管理器
class APIKeyManager:
    def __init__(self):
        self.lock = Lock()
        self.api_keys = []
        self.current_index = 0
        self.invalid_keys = set()
        self.suspended_keys = {}
        self.load_keys()

    def load_keys(self):
        """从 key.txt 加载去重后的密钥"""
        with self.lock:
            try:
                with open(KEY_PATH, 'r') as f:
                    raw_keys = [line.strip() for line in f if line.strip()]
                    # 保留顺序去重
                    self.api_keys = []
                    [self.api_keys.append(k) for k in raw_keys if k not in self.api_keys]
            except Exception as e:
                raise RuntimeError(f"读取密钥文件失败: {e}")

    def get_next_key(self):
        """获取下一个有效密钥，跳过失效和挂起的密钥"""
        with self.lock:
            now = datetime.now()
            valid_keys = [
                k for k in self.api_keys
                if k not in self.invalid_keys and
                (k not in self.suspended_keys or self.suspended_keys[k] <= now)
            ]
            if not valid_keys:
                raise RuntimeError("所有密钥均已失效或处于冷却期，请稍后再试")
            key = valid_keys[self.current_index % len(valid_keys)]
            self.current_index += 1
            return key


    def mark_key_invalid(self, key):
        """标记密钥为失效，并从 key.txt 中删除"""
        with self.lock:
            if key in self.api_keys:
                self.api_keys.remove(key)
            self.invalid_keys.add(key)
            self.save_keys()  # 保存更新后的密钥列表

    def get_status(self):
        """获取当前密钥状态（用于监控）"""
        return {
            "valid_keys": len(self.api_keys) - len(self.invalid_keys),
            "total_keys": len(self.api_keys),
            "invalid_keys": len(self.invalid_keys),
            "suspended_keys": len(self.suspended_keys),
        }
    def save_keys(self):
        try:
            with open(KEY_PATH, 'w') as f:
                for key in self.api_keys:
                    f.write(f"{key}\n")
        except Exception as e:
            print(f"[错误] 保存密钥文件失败: {e}")

    def temporarily_suspend_key(self, key, cooldown=300):
        """
        将密钥临时挂起，设定恢复时间
        :param key: 被挂起的密钥
        :param cooldown: 挂起时间（秒）
        """
        with self.lock:
            if key in self.api_keys:
                self.suspended_keys[key] = datetime.now() + timedelta(seconds=cooldown)


# 初始化密钥管理器
key_manager = APIKeyManager()
def load_yaml(path=CFG_PATH):
    if not path.exists():
        raise FileNotFoundError(f"缺少配置文件 {path}")
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

_yaml = load_yaml()

# ============ 供外部直接 import 使用 =============
# API_KEY        = _yaml.get("api_key")
MODEL_BASE_URL = _yaml["base_url"]  # 只保留基础URL
THRESHOLD_KB   = int(_yaml.get("threshold_kb"))
PORT           = int(_yaml.get("port"))
BASE_PROMPT    = _yaml.get("base_prompt")
MODELS         = _yaml.get("models", [])  # 读取模型列表
# if not API_KEY:
#     raise RuntimeError("请配置api_key")
