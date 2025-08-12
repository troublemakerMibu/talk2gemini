# config.py
import yaml
import pathlib
import logging
from api_key_manager import APIKeyManager, NoAvailableKeysError

# ================== 日志配置 ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s'
)

# ================== 路径配置 ==================
BASE_DIR = pathlib.Path(__file__).resolve().parent
CFG_PATH = BASE_DIR / "config.yaml"
KEY_PATH = BASE_DIR / "key.txt"
DB_PATH = BASE_DIR / "api_keys.db"  # 统一的SQLite数据库

# ================== 配置加载 ==================
def load_yaml(path=CFG_PATH):
    """加载YAML配置文件"""
    if not path.exists():
        raise FileNotFoundError(f"缺少配置文件 {path}")
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

# 加载配置
_yaml = load_yaml()

# ================== 初始化 APIKeyManager ==================
try:
    api_key_config = {
        'cooldown_seconds': _yaml.get('cooldown_seconds', 300),
        'requests_per_minute': _yaml.get('requests_per_minute', 5),
        'requests_per_day': _yaml.get('requests_per_day', 100)
    }

    key_manager = APIKeyManager(
        free_key_path=pathlib.Path("freekey.txt"),
        paid_key_path=pathlib.Path("paidkey.txt"),
        db_path=pathlib.Path("api_keys.db"),
        config={
            'max_free_key_failures': 2,  # 免费密钥连续失败6次后切换到付费
            'cooldown_seconds': 300,
            'requests_per_minute': 5,
            'requests_per_day': 100
        }
    )

except FileNotFoundError as e:
    logging.critical(f"密钥管理器初始化失败: {e}")
    exit(1)
except Exception as e:
    logging.critical(f"初始化时发生未知错误: {e}", exc_info=True)
    exit(1)

# ================== 导出配置项 ==================
MODEL_BASE_URL = _yaml.get("base_url", "")
THRESHOLD_KB = int(_yaml.get("threshold_kb", 3600))
PORT = int(_yaml.get("port", 5000))
BASE_PROMPT = _yaml.get("base_prompt", "")
MODELS = _yaml.get("models", [])

# ================== 导出实用函数 ==================
def get_api_key(preferred_key=None):
    """获取可用的API密钥"""
    return key_manager.get_key(preferred_key)

def record_api_success(key):
    """记录API调用成功"""
    key_manager.record_success(key)

def record_api_failure(key, error_code):
    """记录API调用失败"""
    key_manager.record_failure(key, error_code)

def mark_key_as_invalid(key):
    """标记密钥为永久无效"""
    key_manager.mark_key_invalid(key)

def suspend_key_temporarily(key, duration=None):
    """临时挂起密钥"""
    key_manager.temporarily_suspend_key(key, duration)

def get_key_manager_status():
    """获取密钥管理器的状态"""
    return key_manager.get_status()

def get_key_details(key_prefix):
    """获取特定密钥的详细信息"""
    return key_manager.get_detailed_key_status(key_prefix)

# 导出
__all__ = [
    'MODEL_BASE_URL',
    'THRESHOLD_KB',
    'PORT',
    'BASE_PROMPT',
    'MODELS',
    'get_api_key',
    'record_api_success',
    'record_api_failure',
    'mark_key_as_invalid',
    'suspend_key_temporarily',
    'get_key_manager_status',
    'get_key_details',
    'NoAvailableKeysError',
    'key_manager'
]
