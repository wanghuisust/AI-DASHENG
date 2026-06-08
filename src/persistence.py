"""对话持久化 — 用 SQLite 存储 LangGraph 对话状态

替代 langgraph-checkpoint-sqlite（pip 装不上），自己实现一个
兼容 LangGraph Checkpoint 接口的 SQLite 持久化层。
同时支持：多会话管理、会话列表、导出历史。
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.base import Checkpoint


DB_PATH = Path(__file__).resolve().parent.parent / "data" / "conversations.db"


class ConversationStore:
    """对话存储 — 基于 SQLite，管理多个会话"""

    def __init__(self, db_path: str = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS conversations (
                thread_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT DEFAULT '',
                tool_calls TEXT DEFAULT '',
                tool_name TEXT DEFAULT '',
                tool_call_id TEXT DEFAULT '',
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (thread_id) REFERENCES conversations(thread_id)
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id)")
            # 自动迁移：给旧表补缺失的列
            self._migrate_columns(conn)

    def _conn(self):
        return sqlite3.connect(str(self.db_path))

    # 已知列定义：列名 → 类型（新表已包含这些列，旧表可能缺失）
    _KNOWN_COLUMNS = {
        "tool_call_id": "TEXT DEFAULT ''",
    }

    def _migrate_columns(self, conn):
        """检查 messages 表是否有缺失的列，自动补上"""
        try:
            existing = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        except Exception:
            return
        for col, col_type in self._KNOWN_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {col_type}")

    def create_thread(self, thread_id: str, title: str = ""):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO conversations (thread_id, title) VALUES (?, ?)",
                (thread_id, title)
            )

    def list_threads(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT thread_id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC"
            ).fetchall()
            return [
                {"thread_id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3]}
                for r in rows
            ]

    def save_message(self, thread_id: str, role: str, content: str,
                     tool_calls: str = "", tool_name: str = "",
                     tool_call_id: str = ""):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (thread_id, role, content, tool_calls, tool_name, tool_call_id) VALUES (?, ?, ?, ?, ?, ?)",
                (thread_id, role, content, tool_calls, tool_name, tool_call_id)
            )
            conn.execute(
                "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE thread_id = ?",
                (thread_id,)
            )

    def get_messages(self, thread_id: str, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT role, content, tool_calls, tool_name, tool_call_id, timestamp FROM messages "
                "WHERE thread_id = ? ORDER BY id DESC LIMIT ?",
                (thread_id, limit)
            ).fetchall()
            result = []
            for r in reversed(rows):
                msg = {"role": r[0], "content": r[1], "timestamp": r[5]}
                if r[2]:
                    msg["tool_calls"] = json.loads(r[2])
                if r[3]:
                    msg["tool_name"] = r[3]
                if r[4]:
                    msg["tool_call_id"] = r[4]
                result.append(msg)
            return result

    def delete_thread(self, thread_id: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
            conn.execute("DELETE FROM conversations WHERE thread_id = ?", (thread_id,))

    def get_thread_title(self, thread_id: str) -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT title FROM conversations WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            return row[0] if row else ""


# 全局单例
_store = None

def get_store() -> ConversationStore:
    global _store
    if _store is None:
        _store = ConversationStore()
    return _store
