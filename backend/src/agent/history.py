"""对话历史持久化 — SQLite 存储"""
import json
import time
from pathlib import Path

from ..llm.types import LLMMessage, strip_images_from_content


class ConversationStore:
    """SQLite 对话历史存储"""

    def __init__(self, db_path: str = ""):
        self.db_path = db_path or str(Path.home() / ".kfzcode" / "conversations.db")
        self._ensure_db()

    def _ensure_db(self) -> None:
        """初始化数据库"""
        import sqlite3
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT DEFAULT '',
                    created_at REAL,
                    updated_at REAL,
                    messages TEXT DEFAULT '[]',
                    metadata TEXT DEFAULT '{}'
                )
            """)
            conn.commit()

    def save(self, conv_id: str, title: str,
             messages: list[LLMMessage],
             metadata: dict | None = None) -> None:
        """保存对话"""
        import sqlite3
        messages_json = json.dumps([
            {
                "role": m.role,
                "content": strip_images_from_content(m.content),
                "reasoning_content": m.reasoning_content,
                "tool_calls": m.tool_calls,
                "tool_call_id": m.tool_call_id,
                "name": m.name,
            }
            for m in messages
        ], ensure_ascii=False)

        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO conversations
                   (id, title, created_at, updated_at, messages, metadata)
                   VALUES (?, ?, COALESCE((SELECT created_at FROM conversations WHERE id=?), ?), ?, ?, ?)""",
                (conv_id, title, conv_id, now, now, messages_json,
                 json.dumps(metadata or {}, ensure_ascii=False))
            )
            conn.commit()

    def load(self, conv_id: str) -> dict | None:
        """加载对话"""
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id, title, messages, metadata, created_at, updated_at FROM conversations WHERE id=?",
                (conv_id,)
            ).fetchone()

        if not row:
            return None

        return {
            "id": row[0],
            "title": row[1],
            "messages": json.loads(row[2]),
            "metadata": json.loads(row[3]),
            "created_at": row[4],
            "updated_at": row[5],
        }

    def list_recent(self, limit: int = 20) -> list[dict]:
        """列出最近的对话"""
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC LIMIT ?",
                (limit,)
            ).fetchall()

        return [
            {"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3]}
            for r in rows
        ]

    def list_all(self) -> list[dict]:
        """列出所有对话（含 metadata，用于按 workspace 过滤）"""
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at, messages, metadata "
                "FROM conversations ORDER BY updated_at DESC"
            ).fetchall()

        result = []
        for r in rows:
            try:
                metadata = json.loads(r[5])
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            result.append({
                "id": r[0],
                "title": r[1],
                "created_at": r[2],
                "updated_at": r[3],
                "message_count": len(json.loads(r[4])) if r[4] else 0,
                "metadata": metadata,
            })
        return result

    def delete(self, conv_id: str) -> bool:
        """删除对话"""
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
            conn.commit()
            return cursor.rowcount > 0

    def clear_all(self) -> int:
        """清空所有对话"""
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM conversations")
            conn.commit()
            return cursor.rowcount
