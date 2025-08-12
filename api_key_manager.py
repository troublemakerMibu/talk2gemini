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

    def __init__(self, free_key_path: pathlib.Path, paid_key_path: pathlib.Path,
                 db_path: pathlib.Path, config: dict = None):
        # 检查密钥文件是否存在
        if not free_key_path.exists():
            logging.warning(f"免费密钥文件不存在: {free_key_path}，将创建空文件")
            free_key_path.touch()
        if not paid_key_path.exists():
            logging.warning(f"付费密钥文件不存在: {paid_key_path}，将创建空文件")
            paid_key_path.touch()

        self.free_key_path = free_key_path
        self.paid_key_path = paid_key_path
        self.db_path = db_path
        self.config = config or {}
        self.lock = RLock()

        # 加载配置
        self.cooldown_seconds = self.config.get('cooldown_seconds', 300)
        self.requests_per_minute = self.config.get('requests_per_minute', 5)
        self.requests_per_day = self.config.get('requests_per_day', 100)
        self.max_free_key_failures = self.config.get('max_free_key_failures', 6)

        # 用于记录免费密钥连续失败次数
        self.free_key_consecutive_failures = 0

        # 初始化数据库
        self._init_database()

        # 从数据库加载免费密钥连续失败计数
        with self._get_db_connection() as conn:
            result = conn.execute(
                "SELECT value FROM global_state WHERE key = 'free_key_consecutive_failures'"
            ).fetchone()
            self.free_key_consecutive_failures = int(result['value']) if result else 0

        # 加载并同步密钥
        self._sync_keys_with_files()

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
            # 更新api_keys表，添加key_type字段
            conn.execute('''
                CREATE TABLE IF NOT EXISTS api_keys (
                    key TEXT PRIMARY KEY,
                    key_type TEXT DEFAULT 'free',
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # 检查是否需要添加key_type列（用于升级旧版本数据库）
            cursor = conn.execute("PRAGMA table_info(api_keys)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'key_type' not in columns:
                conn.execute("ALTER TABLE api_keys ADD COLUMN key_type TEXT DEFAULT 'free'")

            conn.execute('''
                CREATE TABLE IF NOT EXISTS key_stats (
                    key TEXT PRIMARY KEY,
                    total_requests INTEGER DEFAULT 0,
                    successful_requests INTEGER DEFAULT 0,
                    failed_requests INTEGER DEFAULT 0,
                    consecutive_failures INTEGER DEFAULT 0,
                    last_used TIMESTAMP,
                    last_success TIMESTAMP,
                    last_error_code INTEGER,
                    last_error_time TIMESTAMP,
                    error_counts TEXT DEFAULT '{}',
                    FOREIGN KEY (key) REFERENCES api_keys(key)
                )
            ''')

            # 检查是否需要添加consecutive_failures列
            cursor = conn.execute("PRAGMA table_info(key_stats)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'consecutive_failures' not in columns:
                conn.execute("ALTER TABLE key_stats ADD COLUMN consecutive_failures INTEGER DEFAULT 0")

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

            # 添加一个新表来存储全局状态
            conn.execute('''
                CREATE TABLE IF NOT EXISTS global_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')

            # 初始化免费密钥连续失败计数
            conn.execute('''
                INSERT OR IGNORE INTO global_state (key, value)
                VALUES ('free_key_consecutive_failures', '0')
            ''')

            conn.commit()


    def _sync_keys_with_files(self):
        """同步文件中的密钥到数据库"""
        # 读取免费密钥
        with open(self.free_key_path, 'r', encoding='utf-8') as f:
            free_keys = set(line.strip() for line in f if line.strip())

        # 读取付费密钥
        with open(self.paid_key_path, 'r', encoding='utf-8') as f:
            paid_keys = set(line.strip() for line in f if line.strip())

        # 检查是否有重复的密钥
        duplicate_keys = free_keys & paid_keys
        if duplicate_keys:
            logging.warning(f"发现 {len(duplicate_keys)} 个重复密钥，将从免费密钥中移除")
            free_keys -= duplicate_keys
            # 更新免费密钥文件
            with open(self.free_key_path, 'w', encoding='utf-8') as f:
                for key in free_keys:
                    f.write(f"{key}\n")

        with self._get_db_connection() as conn:
            # 获取数据库中的所有活跃密钥
            db_keys = {}
            for row in conn.execute("SELECT key, key_type FROM api_keys WHERE is_active = 1"):
                db_keys[row['key']] = row['key_type']

            # 处理免费密钥
            new_free_keys = free_keys - set(db_keys.keys())
            for key in new_free_keys:
                conn.execute("INSERT OR IGNORE INTO api_keys (key, key_type) VALUES (?, ?)",
                           (key, 'free'))
                conn.execute("INSERT OR IGNORE INTO key_stats (key) VALUES (?)", (key,))

            # 处理付费密钥
            new_paid_keys = paid_keys - set(db_keys.keys())
            for key in new_paid_keys:
                conn.execute("INSERT OR IGNORE INTO api_keys (key, key_type) VALUES (?, ?)",
                           (key, 'paid'))
                conn.execute("INSERT OR IGNORE INTO key_stats (key) VALUES (?)", (key,))

            # 更新已存在密钥的类型
            for key in free_keys:
                if key in db_keys and db_keys[key] != 'free':
                    conn.execute("UPDATE api_keys SET key_type = 'free' WHERE key = ?", (key,))

            for key in paid_keys:
                if key in db_keys and db_keys[key] != 'paid':
                    conn.execute("UPDATE api_keys SET key_type = 'paid' WHERE key = ?", (key,))

            # 标记已删除的密钥为非活跃
            all_file_keys = free_keys | paid_keys
            deleted_keys = set(db_keys.keys()) - all_file_keys
            for key in deleted_keys:
                conn.execute("UPDATE api_keys SET is_active = 0 WHERE key = ?", (key,))

            conn.commit()

            if new_free_keys:
                logging.info(f"添加了 {len(new_free_keys)} 个新的免费密钥")
            if new_paid_keys:
                logging.info(f"添加了 {len(new_paid_keys)} 个新的付费密钥")
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

    def get_key(self, preferred_key: Optional[str] = None, force_paid: bool = False) -> str:
        """
        获取一个可用的API密钥

        Args:
            preferred_key: 首选密钥
            force_paid: 是否强制使用付费密钥

        Returns:
            可用的API密钥
        """
        with self.lock:
            # 清理过期数据
            self._cleanup_expired_data()

            # 检查是否应该使用付费密钥
            use_paid = force_paid or self.free_key_consecutive_failures >= self.max_free_key_failures

            with self._get_db_connection() as conn:
                # 尝试使用首选密钥
                if preferred_key:
                    if self._is_key_available(preferred_key, conn):
                        self._mark_key_used(preferred_key, conn)
                        return preferred_key

                # 构建查询条件
                key_type_condition = "= 'paid'" if use_paid else "= 'free'"
                if use_paid and self.free_key_consecutive_failures >= self.max_free_key_failures:
                    logging.info(f"免费密钥连续失败 {self.free_key_consecutive_failures} 次，切换到付费密钥")

                # 获取指定类型的可用密钥
                query = '''
                    SELECT
                        k.key,
                        k.key_type,
                        COALESCE(s.successful_requests, 0) as success_count,
                        COALESCE(s.failed_requests, 0) as fail_count,
                        COALESCE(s.total_requests, 0) as total_count,
                        COALESCE(s.consecutive_failures, 0) as consecutive_failures,
                        (SELECT COUNT(*) FROM rate_limits r
                         WHERE r.key = k.key AND r.request_time > ?) as recent_requests
                    FROM api_keys k
                    LEFT JOIN key_stats s ON k.key = s.key
                    WHERE k.is_active = 1
                    AND k.key_type {}
                    AND k.key NOT IN (SELECT key FROM suspended_keys WHERE resume_time > ?)
                    ORDER BY consecutive_failures ASC, recent_requests ASC, total_count ASC
                '''.format(key_type_condition)

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

                # 如果免费密钥不可用，尝试付费密钥
                if not use_paid:
                    logging.warning("所有免费密钥都不可用，尝试使用付费密钥")
                    return self.get_key(preferred_key=preferred_key, force_paid=True)

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

                # 更新统计信息，重置连续失败计数
                conn.execute(
                    '''UPDATE key_stats
                       SET successful_requests = successful_requests + 1,
                           consecutive_failures = 0,
                           last_success = ?
                       WHERE key = ?''',
                    (datetime.now(), key)
                )

                # 获取密钥类型
                key_type = conn.execute(
                    "SELECT key_type FROM api_keys WHERE key = ?",
                    (key,)
                ).fetchone()['key_type']

                # 如果是免费密钥成功，重置全局连续失败计数
                if key_type == 'free':
                    conn.execute(
                        "UPDATE global_state SET value = '0' WHERE key = 'free_key_consecutive_failures'"
                    )
                    self.free_key_consecutive_failures = 0

                conn.commit()
                logging.debug(f"{key_type}密钥成功完成请求")

    def record_failure(self, key: str, error_code: int):
        """记录失败的API调用"""
        with self.lock:
            with self._get_db_connection() as conn:
                # 获取当前错误统计和密钥类型
                result = conn.execute(
                    '''SELECT s.error_counts, s.consecutive_failures, k.key_type
                       FROM key_stats s
                       JOIN api_keys k ON s.key = k.key
                       WHERE s.key = ?''',
                    (key,)
                ).fetchone()

                if result:
                    error_counts = json.loads(result['error_counts'] or '{}')
                    error_counts[str(error_code)] = error_counts.get(str(error_code), 0) + 1
                    consecutive_failures = (result['consecutive_failures'] or 0) + 1
                    key_type = result['key_type']

                    # 更新统计信息
                    conn.execute(
                        '''UPDATE key_stats
                           SET failed_requests = failed_requests + 1,
                               consecutive_failures = ?,
                               last_error_code = ?,
                               last_error_time = ?,
                               error_counts = ?
                           WHERE key = ?''',
                        (consecutive_failures, error_code, datetime.now(),
                         json.dumps(error_counts), key)
                    )

                    # 如果是免费密钥失败，原子性地增加全局连续失败计数
                    if key_type == 'free':
                        # 使用数据库事务确保原子性
                        current_failures = conn.execute(
                            "SELECT value FROM global_state WHERE key = 'free_key_consecutive_failures'"
                        ).fetchone()['value']

                        new_failures = int(current_failures) + 1

                        conn.execute(
                            "UPDATE global_state SET value = ? WHERE key = 'free_key_consecutive_failures'",
                            (str(new_failures),)
                        )

                        self.free_key_consecutive_failures = new_failures
                        logging.debug(f"免费密钥连续失败次数: {self.free_key_consecutive_failures}")

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
                # 获取密钥类型
                result = conn.execute(
                    "SELECT key_type FROM api_keys WHERE key = ?",
                    (key,)
                ).fetchone()

                if result:
                    key_type = result['key_type']

                    # 标记为非活跃
                    conn.execute("UPDATE api_keys SET is_active = 0 WHERE key = ?", (key,))

                    # 从挂起列表中移除
                    conn.execute("DELETE FROM suspended_keys WHERE key = ?", (key,))

                    conn.commit()

                    # 更新对应的密钥文件
                    self._update_key_files()

                    logging.warning(f"{key_type}密钥已被永久移除")

    def _update_key_files(self):
        """更新密钥文件，移除无效密钥"""
        with self._get_db_connection() as conn:
            # 获取活跃的免费密钥
            free_keys = [row['key'] for row in
                        conn.execute("SELECT key FROM api_keys WHERE is_active = 1 AND key_type = 'free'")]

            # 获取活跃的付费密钥
            paid_keys = [row['key'] for row in
                        conn.execute("SELECT key FROM api_keys WHERE is_active = 1 AND key_type = 'paid'")]

        # 更新免费密钥文件
        with open(self.free_key_path, 'w', encoding='utf-8') as f:
            for key in free_keys:
                f.write(f"{key}\n")

        # 更新付费密钥文件
        with open(self.paid_key_path, 'w', encoding='utf-8') as f:
            for key in paid_keys:
                f.write(f"{key}\n")

    def get_status(self) -> Dict:
        """获取管理器状态"""
        with self.lock:
            self._cleanup_expired_data()

            with self._get_db_connection() as conn:
                # 获取总的可用密钥数量
                total_available = conn.execute('''
                    SELECT COUNT(*) as count
                    FROM api_keys k
                    WHERE k.is_active = 1
                    AND k.key NOT IN (SELECT key FROM suspended_keys WHERE resume_time > ?)
                ''', (datetime.now(),)).fetchone()['count']

                # 获取被挂起的密钥数量
                total_suspended = conn.execute('''
                    SELECT COUNT(DISTINCT sk.key) as count
                    FROM suspended_keys sk
                    JOIN api_keys k ON sk.key = k.key
                    WHERE k.is_active = 1
                    AND sk.resume_time > ?
                ''', (datetime.now(),)).fetchone()['count']

                # 获取分类统计
                type_stats = {}
                for row in conn.execute('''
                    SELECT key_type,
                           COUNT(*) as total,
                           COUNT(CASE WHEN key NOT IN
                                 (SELECT key FROM suspended_keys WHERE resume_time > ?) THEN 1 END) as available
                    FROM api_keys
                    WHERE is_active = 1
                    GROUP BY key_type
                ''', (datetime.now(),)):
                    type_stats[row['key_type']] = {
                        'total': row['total'],
                        'available': row['available'],
                        'suspended': row['total'] - row['available']
                    }

                # 获取请求统计
                request_stats = conn.execute('''
                    SELECT
                        k.key_type,
                        SUM(s.successful_requests) as success,
                        SUM(s.failed_requests) as failed
                    FROM key_stats s
                    JOIN api_keys k ON s.key = k.key
                    WHERE k.is_active = 1
                    GROUP BY k.key_type
                ''').fetchall()

                type_requests = {}
                total_success = 0
                total_failed = 0

                for row in request_stats:
                    type_requests[row['key_type']] = {
                        'successful': row['success'] or 0,
                        'failed': row['failed'] or 0
                    }
                    total_success += row['success'] or 0
                    total_failed += row['failed'] or 0

                # 获取错误分布
                error_dist = {}
                for row in conn.execute("SELECT error_counts FROM key_stats WHERE error_counts != '{}'"):
                    counts = json.loads(row['error_counts'])
                    for code, count in counts.items():
                        error_dist[code] = error_dist.get(code, 0) + count

                return {
                    "available_keys": total_available,  # 所有可用密钥的总数
                    "suspended_keys": total_suspended,  # 新增字段：所有被挂起密钥的总数
                    "key_statistics": type_stats,
                    "request_statistics": type_requests,
                    "total_successful_requests": total_success,
                    "total_failed_requests": total_failed,
                    "free_key_consecutive_failures": self.free_key_consecutive_failures,
                    "max_free_key_failures": self.max_free_key_failures,
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
                        k.key, k.is_active, k.key_type,
                        s.total_requests, s.successful_requests, s.failed_requests,
                        s.consecutive_failures,                        s.last_used, s.last_success, s.last_error_code, s.last_error_time,
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
                        "key": row['key'],
                        "key_type": row['key_type'],
                        "is_active": bool(row['is_active']),
                        "is_suspended": row['resume_time'] is not None and row['resume_time'] > datetime.now(),
                        "stats": {
                            "total_requests": row['total_requests'] or 0,
                            "successful_requests": row['successful_requests'] or 0,
                            "failed_requests": row['failed_requests'] or 0,
                            "consecutive_failures": row['consecutive_failures'] or 0,
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

    def reset_free_key_failures(self):
        """手动重置免费密钥连续失败计数"""
        with self.lock:
            with self._get_db_connection() as conn:
                conn.execute(
                    "UPDATE global_state SET value = '0' WHERE key = 'free_key_consecutive_failures'"
                )
                conn.commit()
            self.free_key_consecutive_failures = 0
            logging.info("已重置免费密钥连续失败计数")

    def get_key_by_type(self, key_type: str = 'free') -> str:
        """根据类型获取密钥"""
        if key_type not in ['free', 'paid']:
            raise ValueError("key_type 必须是 'free' 或 'paid'")

        force_paid = (key_type == 'paid')
        return self.get_key(force_paid=force_paid)
