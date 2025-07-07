# api_key_manager.py
import sqlite3
import logging
import pathlib
from datetime import datetime, timedelta
from threading import RLock
from typing import Dict, List, Optional, Tuple
from contextlib import contextmanager
import json


class NoAvailableKeysError(Exception):
    """当所有API密钥都不可用时抛出此异常"""
    pass


class APIKeyManager:
    """使用SQLite的线程安全API密钥管理器"""

    def __init__(self, key_path: pathlib.Path, db_path: pathlib.Path, config: dict = None):
        if not key_path.exists():
            raise FileNotFoundError(f"密钥文件不存在: {key_path}")

        self.key_path = key_path
        self.db_path = db_path
        self.config = config or {}
        self.lock = RLock()

        # 加载配置
        self.cooldown_seconds = self.config.get('cooldown_seconds', 300)
        self.requests_per_minute = self.config.get('requests_per_minute', 5)
        self.requests_per_day = self.config.get('requests_per_day', 100)

        # 初始化数据库
        self._init_database()

        # 加载并同步密钥
        self._sync_keys_with_file()

        # 清理过期数据
        self._cleanup_expired_data()

        logging.info(f"APIKeyManager 初始化完成: {self._get_total_keys()} 个密钥")

    @contextmanager
    def _get_db_connection(self):
        """获取数据库连接的上下文管理器"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_database(self):
        """初始化数据库表"""
        with self._get_db_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS api_keys (
                    key TEXT PRIMARY KEY,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            conn.execute('''
                CREATE TABLE IF NOT EXISTS key_stats (
                    key TEXT PRIMARY KEY,
                    total_requests INTEGER DEFAULT 0,
                    successful_requests INTEGER DEFAULT 0,
                    failed_requests INTEGER DEFAULT 0,
                    last_used TIMESTAMP,
                    last_success TIMESTAMP,
                    last_error_code INTEGER,
                    last_error_time TIMESTAMP,
                    error_counts TEXT DEFAULT '{}',
                    FOREIGN KEY (key) REFERENCES api_keys(key)
                )
            ''')

            conn.execute('''
                CREATE TABLE IF NOT EXISTS rate_limits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT,
                    request_time TIMESTAMP,
                    FOREIGN KEY (key) REFERENCES api_keys(key)
                )
            ''')

            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_rate_limits_key_time
                ON rate_limits(key, request_time)
            ''')

            conn.execute('''
                CREATE TABLE IF NOT EXISTS suspended_keys (
                    key TEXT PRIMARY KEY,
                    resume_time TIMESTAMP,
                    reason TEXT,
                    FOREIGN KEY (key) REFERENCES api_keys(key)
                )
            ''')

            conn.commit()

    def _sync_keys_with_file(self):
        """同步文件中的密钥到数据库"""
        # 读取文件中的密钥
        with open(self.key_path, 'r', encoding='utf-8') as f:
            file_keys = set(line.strip() for line in f if line.strip())

        # 去重并更新文件
        if len(file_keys) < sum(1 for line in open(self.key_path, 'r', encoding='utf-8') if line.strip()):
            with open(self.key_path, 'w', encoding='utf-8') as f:
                for key in file_keys:
                    f.write(f"{key}\n")
            logging.info(f"已清理文件中的重复密钥")

        with self._get_db_connection() as conn:
            # 获取数据库中的活跃密钥
            db_keys = set(row['key'] for row in
                         conn.execute("SELECT key FROM api_keys WHERE is_active = 1"))

            # 添加新密钥
            new_keys = file_keys - db_keys
            for key in new_keys:
                conn.execute("INSERT OR IGNORE INTO api_keys (key) VALUES (?)", (key,))
                conn.execute("INSERT OR IGNORE INTO key_stats (key) VALUES (?)", (key,))

            # 标记已删除的密钥为非活跃
            deleted_keys = db_keys - file_keys
            for key in deleted_keys:
                conn.execute("UPDATE api_keys SET is_active = 0 WHERE key = ?", (key,))

            conn.commit()

            if new_keys:
                logging.info(f"添加了 {len(new_keys)} 个新密钥")
            if deleted_keys:
                logging.info(f"标记了 {len(deleted_keys)} 个密钥为非活跃")

    def _cleanup_expired_data(self):
        """清理过期的数据"""
        with self._get_db_connection() as conn:
            # 清理过期的挂起状态
            conn.execute(
                "DELETE FROM suspended_keys WHERE resume_time <= ?",
                (datetime.now(),)
            )

            # 清理超过24小时的速率限制记录
            cutoff_time = datetime.now() - timedelta(hours=24)
            conn.execute(
                "DELETE FROM rate_limits WHERE request_time < ?",
                (cutoff_time,)
            )

            conn.commit()

    def _get_total_keys(self) -> int:
        """获取活跃密钥总数"""
        with self._get_db_connection() as conn:
            result = conn.execute("SELECT COUNT(*) FROM api_keys WHERE is_active = 1").fetchone()
            return result[0]

    def _check_rate_limit(self, key: str) -> Tuple[bool, Optional[str]]:
        """检查密钥是否超过速率限制"""
        with self._get_db_connection() as conn:
            now = datetime.now()

            # 检查分钟级限制
            minute_ago = now - timedelta(minutes=1)
            minute_count = conn.execute(
                "SELECT COUNT(*) FROM rate_limits WHERE key = ? AND request_time > ?",
                (key, minute_ago)
            ).fetchone()[0]

            if minute_count >= self.requests_per_minute:
                return False, f"已达到每分钟限制 ({self.requests_per_minute}次/分钟)"

            # 检查日级限制
            day_ago = now - timedelta(days=1)
            day_count = conn.execute(
                "SELECT COUNT(*) FROM rate_limits WHERE key = ? AND request_time > ?",
                (key, day_ago)
            ).fetchone()[0]

            if day_count >= self.requests_per_day:
                return False, f"已达到每日限制 ({self.requests_per_day}次/天)"

            return True, None

    def _is_key_suspended(self, key: str) -> bool:
        """检查密钥是否被挂起"""
        with self._get_db_connection() as conn:
            result = conn.execute(
                "SELECT resume_time FROM suspended_keys WHERE key = ? AND resume_time > ?",
                (key, datetime.now())
            ).fetchone()
            return result is not None

    def get_key(self, preferred_key: Optional[str] = None) -> str:
        """获取一个可用的API密钥"""
        with self.lock:
            # 清理过期数据
            self._cleanup_expired_data()

            with self._get_db_connection() as conn:
                # 尝试使用首选密钥
                if preferred_key:
                    if self._is_key_available(preferred_key, conn):
                        self._mark_key_used(preferred_key, conn)
                        return preferred_key

                # 获取所有可用密钥并按使用情况排序
                query = '''
                    SELECT
                        k.key,
                        COALESCE(s.successful_requests, 0) as success_count,
                        COALESCE(s.failed_requests, 0) as fail_count,
                        COALESCE(s.total_requests, 0) as total_count,
                        (SELECT COUNT(*) FROM rate_limits r
                         WHERE r.key = k.key AND r.request_time > ?) as recent_requests
                    FROM api_keys k
                    LEFT JOIN key_stats s ON k.key = s.key
                    WHERE k.is_active = 1
                    AND k.key NOT IN (SELECT key FROM suspended_keys WHERE resume_time > ?)
                    ORDER BY recent_requests ASC, total_count ASC
                '''

                day_ago = datetime.now() - timedelta(days=1)
                rows = conn.execute(query, (day_ago, datetime.now())).fetchall()

                # 找到第一个未超过速率限制的密钥
                for row in rows:
                    key = row['key']
                    if key == preferred_key:
                        continue

                    allowed, _ = self._check_rate_limit(key)
                    if allowed:
                        self._mark_key_used(key, conn)
                        return key

                raise NoAvailableKeysError("所有密钥都不可用（速率限制或挂起中）")

    def _is_key_available(self, key: str, conn: sqlite3.Connection) -> bool:
        """检查密钥是否可用"""
        # 检查是否为活跃密钥
        result = conn.execute(
            "SELECT 1 FROM api_keys WHERE key = ? AND is_active = 1",
            (key,)
        ).fetchone()

        if not result:
            return False

        # 检查是否被挂起
        if self._is_key_suspended(key):
            return False

        # 检查速率限制
        allowed, _ = self._check_rate_limit(key)
        return allowed

    def _mark_key_used(self, key: str, conn: sqlite3.Connection):
        """标记密钥被使用"""
        conn.execute(
            '''UPDATE key_stats
               SET total_requests = total_requests + 1,
                   last_used = ?
               WHERE key = ?''',
            (datetime.now(), key)
        )
        conn.commit()

    def record_success(self, key: str):
        """记录成功的API调用"""
        with self.lock:
            with self._get_db_connection() as conn:
                # 记录到速率限制表
                conn.execute(
                    "INSERT INTO rate_limits (key, request_time) VALUES (?, ?)",
                    (key, datetime.now())
                )

                # 更新统计信息
                conn.execute(
                    '''UPDATE key_stats
                       SET successful_requests = successful_requests + 1,
                           last_success = ?
                       WHERE key = ?''',
                    (datetime.now(), key)
                )

                conn.commit()
                logging.debug(f"密钥成功完成请求")

    def record_failure(self, key: str, error_code: int):
        """记录失败的API调用"""
        with self.lock:
            with self._get_db_connection() as conn:
                # 获取当前错误统计
                result = conn.execute(
                    "SELECT error_counts FROM key_stats WHERE key = ?",
                    (key,)
                ).fetchone()

                error_counts = json.loads(result['error_counts'] if result else '{}')
                error_counts[str(error_code)] = error_counts.get(str(error_code), 0) + 1

                # 更新统计信息
                conn.execute(
                    '''UPDATE key_stats
                       SET failed_requests = failed_requests + 1,
                           last_error_code = ?,
                           last_error_time = ?,
                           error_counts = ?
                       WHERE key = ?''',
                    (error_code, datetime.now(), json.dumps(error_counts), key)
                )

                conn.commit()

    def temporarily_suspend_key(self, key: str, duration_seconds: Optional[int] = None):
        """临时挂起密钥"""
        duration = duration_seconds or self.cooldown_seconds
        with self.lock:
            with self._get_db_connection() as conn:
                resume_time = datetime.now() + timedelta(seconds=duration)
                conn.execute(
                    '''INSERT OR REPLACE INTO suspended_keys (key, resume_time, reason)
                       VALUES (?, ?, ?)''',
                    (key, resume_time, f"临时挂起 {duration} 秒")
                )
                conn.commit()
                logging.info(f"密钥已被挂起 {duration} 秒")

    def mark_key_invalid(self, key: str):
        """标记密钥为永久无效"""
        with self.lock:
            with self._get_db_connection() as conn:
                # 标记为非活跃
                conn.execute("UPDATE api_keys SET is_active = 0 WHERE key = ?", (key,))

                # 从挂起列表中移除
                conn.execute("DELETE FROM suspended_keys WHERE key = ?", (key,))

                conn.commit()

                # 更新文件
                self._update_key_file()

                logging.warning(f"密钥已被永久移除")

    def _update_key_file(self):
        """更新密钥文件，移除无效密钥"""
        with self._get_db_connection() as conn:
            active_keys = [row['key'] for row in
                          conn.execute("SELECT key FROM api_keys WHERE is_active = 1")]

        with open(self.key_path, 'w', encoding='utf-8') as f:
            for key in active_keys:
                f.write(f"{key}\n")

    def get_status(self) -> Dict:
        """获取管理器状态"""
        with self.lock:
            self._cleanup_expired_data()

            with self._get_db_connection() as conn:
                # 获取总体统计
                stats = conn.execute('''
                    SELECT
                        COUNT(CASE WHEN is_active = 1 THEN 1 END) as total_keys,
                        COUNT(CASE WHEN k.is_active = 1 AND k.key NOT IN
                              (SELECT key FROM suspended_keys WHERE resume_time > ?) THEN 1 END) as available_keys
                    FROM api_keys k
                ''', (datetime.now(),)).fetchone()

                # 获取请求统计
                request_stats = conn.execute('''
                    SELECT
                        SUM(successful_requests) as total_success,
                        SUM(failed_requests) as total_failed
                    FROM key_stats
                ''').fetchone()

                total_success = request_stats['total_success'] or 0
                total_failed = request_stats['total_failed'] or 0
                total_requests = total_success + total_failed

                # 获取错误分布
                error_dist = {}
                for row in conn.execute("SELECT error_counts FROM key_stats WHERE error_counts != '{}'"):
                    counts = json.loads(row['error_counts'])
                    for code, count in counts.items():
                        error_dist[code] = error_dist.get(code, 0) + count

                return {
                    "total_keys": stats['total_keys'],
                    "available_keys": stats['available_keys'],
                    "suspended_keys": stats['total_keys'] - stats['available_keys'],
                    "total_successful_requests": total_success,
                    "total_failed_requests": total_failed,
                    "rate_limits": {
                        "requests_per_minute": self.requests_per_minute,
                        "requests_per_day": self.requests_per_day
                    },
                    "error_distribution": error_dist
                }

    def get_detailed_key_status(self, key_prefix: str) -> Dict:
        """获取特定密钥的详细状态"""
        with self.lock:
            with self._get_db_connection() as conn:
                query = '''
                    SELECT
                        k.key, k.is_active,
                        s.total_requests, s.successful_requests, s.failed_requests,
                        s.last_used, s.last_success, s.last_error_code, s.last_error_time,
                        sk.resume_time,
                        (SELECT COUNT(*) FROM rate_limits r
                         WHERE r.key = k.key AND r.request_time > ?) as requests_today
                    FROM api_keys k
                    LEFT JOIN key_stats s ON k.key = s.key
                    LEFT JOIN suspended_keys sk ON k.key = sk.key
                    WHERE k.key LIKE ?
                    LIMIT 5
                '''

                day_ago = datetime.now() - timedelta(days=1)
                rows = conn.execute(query, (day_ago, f"{key_prefix}%")).fetchall()

                if not rows:
                    return {"error": "未找到匹配的密钥"}

                details = []
                for row in rows:
                    details.append({
                        "key": row['key'][:8] + "...",
                        "is_active": bool(row['is_active']),
                        "is_suspended": row['resume_time'] is not None and row['resume_time'] > datetime.now(),
                        "stats": {
                            "total_requests": row['total_requests'] or 0,
                            "successful_requests": row['successful_requests'] or 0,
                            "failed_requests": row['failed_requests'] or 0,
                            "requests_today": row['requests_today'],
                            "last_used": row['last_used'],
                            "last_success": row['last_success'],
                            "last_error": {
                                "code": row['last_error_code'],
                                "time": row['last_error_time']
                            } if row['last_error_code'] else None
                        }
                    })

                return {
                    "matching_keys_count": len(rows),
                    "details": details
                }
