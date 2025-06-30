import yaml
import pathlib
import logging
import json
from threading import RLock
from datetime import timedelta, datetime

# ================== 日志配置 ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')

# ================== 路径配置 ==================
BASE_DIR = pathlib.Path(__file__).resolve().parent
CFG_PATH = BASE_DIR / "config.yaml"
KEY_PATH = BASE_DIR / "key.txt"
SUSPENDED_KEYS_PATH = BASE_DIR / "suspended_keys.json"


# ================== 自定义异常 ==================
class NoAvailableKeysError(Exception):
    """当所有API密钥都不可用时抛出此异常，让上层应用可以清晰地捕获。"""
    pass


# ================== 优化的 APIKeyManager (JSON持久化版本) ==================
class APIKeyManager:
    """
    一个经过优化的、线程安全的API密钥管理器。
    增加了对挂起状态的持久化功能，并在启动时清理源密钥文件中的重复项。
    """

    def __init__(self, key_path: pathlib.Path, suspended_keys_path: pathlib.Path, config: dict = None):
        """
        通过依赖注入初始化，更灵活且易于测试。
        :param key_path: 密钥文件路径。
        :param suspended_keys_path: 挂起密钥状态的持久化文件路径。
        :param config: 从 YAML 加载的配置字典。
        """
        if not key_path.exists():
            raise FileNotFoundError(f"密钥文件不存在: {key_path}")

        self.key_path = key_path
        self.suspended_keys_path = suspended_keys_path
        self.config = config or {}
        self.lock = RLock()

        # <<< 核心改动点在此方法内部 >>>
        self.api_keys = self._load_and_clean_keys()
        self.suspended_keys = self._load_suspended_keys()
        self.current_index = 0

        self._cleanup_expired_suspensions()

    # <<< 核心改动：方法重命名并增强功能 >>>
    def _load_and_clean_keys(self) -> list:
        """
        从文件中加载密钥，并在发现重复项时，清理源文件。
        """
        try:
            # 1. 读取所有原始密钥
            with open(self.key_path, 'r', encoding='utf-8') as f:
                # 过滤掉空行
                raw_keys = [line.strip() for line in f if line.strip()]

            # 2. 在内存中去重
            unique_keys = list(dict.fromkeys(raw_keys))

            # 3. 检查是否存在重复项
            if len(raw_keys) > len(unique_keys):
                removed_count = len(raw_keys) - len(unique_keys)
                logging.info(f"在 {self.key_path.name} 中发现并处理了 {removed_count} 个重复密钥。")

                # 4. 用去重后的列表覆盖重写源文件
                try:
                    with open(self.key_path, 'w', encoding='utf-8') as f:
                        for key in unique_keys:
                            f.write(f"{key}\n")
                    logging.info(f"已成功更新 {self.key_path.name}，移除了所有重复项。")
                except Exception as e_write:
                    # 如果写入失败，程序仍可继续，但需记录错误
                    logging.error(f"清理密钥文件 {self.key_path.name} 时写入失败: {e_write}", exc_info=True)

            logging.info(f"成功加载 {len(unique_keys)} 个唯一密钥。")
            return unique_keys

        except Exception as e_read:
            logging.error(f"读取密钥文件 {self.key_path} 失败: {e_read}", exc_info=True)
            raise

    def _load_suspended_keys(self) -> dict:
        """从JSON文件中加载挂起的密钥及其恢复时间。"""
        if not self.suspended_keys_path.exists():
            return {}
        try:
            with open(self.suspended_keys_path, 'r', encoding='utf-8') as f:
                suspended_data = json.load(f)
                return {
                    key: datetime.fromisoformat(ts)
                    for key, ts in suspended_data.items()
                }
        except (json.JSONDecodeError, FileNotFoundError):
            logging.warning(f"无法加载或解析 {self.suspended_keys_path}，将从空的挂起列表开始。")
            return {}

    def _save_suspended_keys(self):
        """将当前的挂起密钥字典保存到JSON文件。"""
        with self.lock:
            try:
                data_to_save = {
                    key: dt.isoformat()
                    for key, dt in self.suspended_keys.items()
                }
                with open(self.suspended_keys_path, 'w', encoding='utf-8') as f:
                    json.dump(data_to_save, f, indent=4)
            except Exception as e:
                logging.error(f"保存挂起状态到 {self.suspended_keys_path} 失败: {e}", exc_info=True)

    def _cleanup_expired_suspensions(self):
        """清理已过期的挂起条目，并在需要时更新文件。"""
        with self.lock:
            now = datetime.now()
            initial_count = len(self.suspended_keys)

            active_suspensions = {
                key: resume_time
                for key, resume_time in self.suspended_keys.items()
                if resume_time > now
            }

            if len(active_suspensions) < initial_count:
                logging.info(f"启动时清理了 {initial_count - len(active_suspensions)} 个已过期的挂起密钥。")
                self.suspended_keys = active_suspensions
                self._save_suspended_keys()

    def get_key(self, preferred_key: str = None) -> str:
        """
        获取一个可用的API密钥。
        如果提供了 preferred_key 且其可用，则优先返回它（实现“粘性”）。
        否则，按顺序轮询获取下一个可用密钥。
        """
        with self.lock:
            if not self.api_keys:
                raise NoAvailableKeysError("密钥池为空，请在文件中添加密钥。")

            now = datetime.now()

            # 1. 检查首选密钥
            if preferred_key and preferred_key in self.api_keys:
                if not self._is_key_suspended(preferred_key, now):
                    return preferred_key

            # 2. 轮询获取可用密钥
            for i in range(len(self.api_keys)):
                key_index = (self.current_index + i) % len(self.api_keys)
                key = self.api_keys[key_index]

                if key == preferred_key:
                    continue

                if not self._is_key_suspended(key, now):
                    self.current_index = (key_index + 1) % len(self.api_keys)
                    return key

            raise NoAvailableKeysError("所有密钥均处于冷却期，请稍后再试。")

    def _is_key_suspended(self, key: str, now: datetime) -> bool:
        """检查一个密钥是否处于挂起状态（内部辅助方法）。"""
        resume_time = self.suspended_keys.get(key)
        if resume_time:
            if now >= resume_time:
                del self.suspended_keys[key]
                logging.info(f"密钥 {key} 已从挂起状态恢复。")
                self._save_suspended_keys()
                return False
            else:
                return True
        return False

    def mark_key_invalid(self, key: str):
        """将一个密钥标记为永久失效，从池中移除并更新文件。"""
        with self.lock:
            key_was_suspended = key in self.suspended_keys

            if key in self.api_keys:
                self.api_keys.remove(key)
                if key_was_suspended:
                    del self.suspended_keys[key]

                logging.warning(f"密钥 {key} 已被标记为永久失效，将从池中移除。")
                self._save_keys_to_file()

                if key_was_suspended:
                    self._save_suspended_keys()

                if self.current_index >= len(self.api_keys) and self.api_keys:
                    self.current_index = 0

    def temporarily_suspend_key(self, key: str):
        """将指定的密钥临时挂起，并将状态持久化。"""
        cooldown = self.config.get('cooldown_seconds', 300)
        with self.lock:
            if key in self.api_keys:
                resume_time = datetime.now() + timedelta(seconds=cooldown)
                self.suspended_keys[key] = resume_time
                logging.info(
                    f"密钥 {key} 已被临时挂起，将在 {cooldown} 秒后于 {resume_time.strftime('%Y-%m-%d %H:%M:%S')} 恢复。")
                self._save_suspended_keys()

    def get_status(self) -> dict:
        """获取当前密钥管理器的状态，用于监控。"""
        with self.lock:
            self._cleanup_expired_suspensions()
            return {
                "total_keys_in_pool": len(self.api_keys),
                "available_keys": len(self.api_keys) - len(self.suspended_keys),
                "suspended_keys_count": len(self.suspended_keys),
            }

    def _save_keys_to_file(self):
        """将当前有效的密钥列表写回文件（内部方法）。"""
        try:
            with open(self.key_path, 'w', encoding='utf-8') as f:
                for key in self.api_keys:
                    f.write(f"{key}\n")
            logging.info(f"成功将 {len(self.api_keys)} 个有效密钥保存到 {self.key_path}。")
        except Exception as e:
            logging.error(f"保存密钥文件 {self.key_path} 失败: {e}", exc_info=True)


# ================== 配置加载与实例化 ==================
def load_yaml(path=CFG_PATH):
    if not path.exists():
        raise FileNotFoundError(f"缺少配置文件 {path}")
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


_yaml = load_yaml()

try:
    key_manager = APIKeyManager(
        key_path=KEY_PATH,
        suspended_keys_path=SUSPENDED_KEYS_PATH,
        config=_yaml
    )
except FileNotFoundError as e:
    logging.critical(f"密钥管理器初始化失败: {e}")
    exit(1)

# ================== 供外部直接 import 使用的配置项 ==================
MODEL_BASE_URL = _yaml["base_url"]
THRESHOLD_KB = int(_yaml.get("threshold_kb", 3600))
PORT = int(_yaml.get("port", 5000))
BASE_PROMPT = _yaml.get("base_prompt", "")
MODELS = _yaml.get("models", [])
