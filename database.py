"""astrbot_plugin_cyber_tombstone - SQLite 数据层

表结构：
- messages  原始群消息（定期清理，默认保留 90 天）
- users     用户聚合表（冗余加速查询，永久保留）
- tombs     已立墓碑记录
"""

import time
from typing import Dict, List, Optional, Tuple

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_name TEXT,
    content TEXT,
    timestamp INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_group_user ON messages (group_id, user_id);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages (timestamp);

CREATE TABLE IF NOT EXISTS users (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_name TEXT,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_message TEXT,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_users_group_last ON users (group_id, last_seen);

CREATE TABLE IF NOT EXISTS tombs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tomb_no INTEGER,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_name TEXT,
    first_seen INTEGER,
    last_seen INTEGER,
    last_message TEXT,
    message_count INTEGER,
    epitaph TEXT,
    bury_time INTEGER NOT NULL,
    UNIQUE (group_id, user_id)
);
"""


class TombDatabase:
    """aiosqlite 异步封装。所有写操作均使用参数化查询。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None

    # ---------- 生命周期 ----------

    async def init(self):
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self):
        if self.db is not None:
            try:
                await self.db.close()
            except Exception:
                pass
            self.db = None

    async def _execute(self, sql: str, params: Tuple = ()):
        assert self.db is not None, "database not initialized"
        return await self.db.execute(sql, params)

    # ---------- 消息写入（批量） ----------

    async def insert_messages(self, rows: List[Tuple]):
        """rows: [(group_id, user_id, user_name, content, timestamp), ...]"""
        if not rows:
            return
        await self.db.executemany(
            "INSERT INTO messages (group_id, user_id, user_name, content, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        await self.db.commit()

    async def upsert_users(self, agg: Dict[Tuple[str, str], dict]):
        """批量 UPSERT 聚合表。

        agg: {(group_id, user_id): {"user_name", "first_seen", "last_seen",
                                     "count", "last_message"}}
        """
        if not agg:
            return
        rows = [
            (
                g, u, v.get("user_name") or u,
                v.get("first_seen") or 0,
                v.get("last_seen") or 0,
                v.get("count") or 0,
                v.get("last_message"),
            )
            for (g, u), v in agg.items()
        ]
        await self.db.executemany(
            "INSERT INTO users (group_id, user_id, user_name, first_seen, last_seen, "
            "message_count, last_message) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (group_id, user_id) DO UPDATE SET "
            "user_name = COALESCE(NULLIF(excluded.user_name, ''), users.user_name), "
            "first_seen = MIN(users.first_seen, excluded.first_seen), "
            "last_seen = MAX(users.last_seen, excluded.last_seen), "
            "message_count = users.message_count + excluded.message_count, "
            "last_message = COALESCE(NULLIF(excluded.last_message, ''), users.last_message)",
            rows,
        )
        await self.db.commit()

    # ---------- 查询 ----------

    async def get_profile(self, group_id: str, user_id: str) -> Optional[dict]:
        cur = await self._execute(
            "SELECT group_id, user_id, user_name, first_seen, last_seen, "
            "message_count, last_message FROM users WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_inactive(self, group_id: str, cutoff: int, limit: int = 0) -> List[dict]:
        """last_seen 早于 cutoff 的用户，按最后发言时间升序（最早优先）。"""
        sql = ("SELECT group_id, user_id, user_name, first_seen, last_seen, "
               "message_count, last_message FROM users "
               "WHERE group_id = ? AND last_seen < ? ORDER BY last_seen ASC")
        params: list = [group_id, cutoff]
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        cur = await self._execute(sql, tuple(params))
        return [dict(r) for r in await cur.fetchall()]

    async def count_inactive(self, group_id: str, cutoff: int) -> int:
        cur = await self._execute(
            "SELECT COUNT(*) AS c FROM users WHERE group_id = ? AND last_seen < ?",
            (group_id, cutoff),
        )
        row = await cur.fetchone()
        return row["c"] if row else 0

    async def distinct_groups(self) -> List[str]:
        cur = await self._execute("SELECT DISTINCT group_id FROM users")
        return [r["group_id"] for r in await cur.fetchall()]

    # ---------- 墓碑 ----------

    async def next_tomb_no(self, group_id: str) -> int:
        cur = await self._execute(
            "SELECT COUNT(*) AS c FROM tombs WHERE group_id = ?", (group_id,)
        )
        row = await cur.fetchone()
        return (row["c"] if row else 0) + 1

    async def has_tomb(self, group_id: str, user_id: str) -> bool:
        cur = await self._execute(
            "SELECT 1 FROM tombs WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        return await cur.fetchone() is not None

    async def add_tomb(self, group_id: str, user_id: str, profile: dict,
                       epitaph: str) -> int:
        """立碑（幂等：已存在则更新），返回群内墓碑编号。"""
        tomb_no = await self.next_tomb_no(group_id)
        await self._execute(
            "INSERT INTO tombs (tomb_no, group_id, user_id, user_name, first_seen, "
            "last_seen, last_message, message_count, epitaph, bury_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (group_id, user_id) DO UPDATE SET "
            "user_name = excluded.user_name, first_seen = excluded.first_seen, "
            "last_seen = excluded.last_seen, last_message = excluded.last_message, "
            "message_count = excluded.message_count, epitaph = excluded.epitaph, "
            "bury_time = excluded.bury_time, tomb_no = excluded.tomb_no",
            (
                tomb_no, group_id, user_id,
                profile.get("user_name") or user_id,
                profile.get("first_seen") or 0,
                profile.get("last_seen") or 0,
                profile.get("last_message"),
                profile.get("message_count") or 0,
                epitaph, int(time.time()),
            ),
        )
        await self.db.commit()
        return tomb_no

    async def list_tombs(self, group_id: str) -> List[dict]:
        cur = await self._execute(
            "SELECT * FROM tombs WHERE group_id = ? ORDER BY bury_time DESC",
            (group_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def remove_tomb(self, group_id: str, user_id: str) -> bool:
        cur = await self._execute(
            "DELETE FROM tombs WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    # ---------- 隐私 / 维护 ----------

    async def forget(self, group_id: str, user_id: str) -> int:
        """删除某人全部记录（消息/聚合/墓碑），返回删除条数。"""
        deleted = 0
        for table in ("messages", "users", "tombs"):
            cur = await self._execute(
                f"DELETE FROM {table} WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )
            deleted += max(cur.rowcount or 0, 0)
        await self.db.commit()
        return deleted

    async def forget_all(self, group_id: str) -> int:
        """清空本群所有记录，返回删除条数。"""
        deleted = 0
        for table in ("messages", "users", "tombs"):
            cur = await self._execute(
                f"DELETE FROM {table} WHERE group_id = ?", (group_id,)
            )
            deleted += max(cur.rowcount or 0, 0)
        await self.db.commit()
        return deleted

    async def cleanup_messages(self, retention_days: int) -> int:
        cutoff = int(time.time()) - max(retention_days, 1) * 86400
        cur = await self._execute(
            "DELETE FROM messages WHERE timestamp < ?", (cutoff,)
        )
        await self.db.commit()
        return max(cur.rowcount or 0, 0)

    # ---------- 调试 ----------

    async def stats(self) -> dict:
        out = {}
        for table in ("messages", "users", "tombs"):
            cur = await self._execute(f"SELECT COUNT(*) AS c FROM {table}")
            row = await cur.fetchone()
            out[table] = row["c"] if row else 0
        return out
