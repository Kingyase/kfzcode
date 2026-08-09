"""SQLite 异步存储 — 对话历史 + 元数据"""
import json
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Conversation:
    id: str
    title: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    messages: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class Database:
    """基于 JSON 文件的轻量存储（避免引入 aiosqlite 依赖，内网可能不方便安装）"""

    def __init__(self, data_dir: str = ""):
        self.data_dir = Path(data_dir or str(Path.home() / ".kfzcode"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.conversations_file = self.data_dir / "conversations.json"
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        if not self.conversations_file.exists():
            self.conversations_file.write_text("{}", encoding="utf-8")

    def _read(self) -> dict:
        try:
            return json.loads(self.conversations_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict) -> None:
        self.conversations_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save(self, conv: Conversation) -> None:
        data = self._read()
        data[conv.id] = {
            "title": conv.title,
            "created_at": conv.created_at or time.time(),
            "updated_at": time.time(),
            "messages": conv.messages,
            "metadata": conv.metadata,
        }
        self._write(data)

    def load(self, conv_id: str) -> Conversation | None:
        data = self._read()
        if conv_id not in data:
            return None
        d = data[conv_id]
        return Conversation(
            id=conv_id,
            title=d.get("title", ""),
            created_at=d.get("created_at", 0),
            updated_at=d.get("updated_at", 0),
            messages=d.get("messages", []),
            metadata=d.get("metadata", {}),
        )

    def list_recent(self, limit: int = 20) -> list[Conversation]:
        data = self._read()
        convs = [
            Conversation(
                id=cid, title=d.get("title", ""),
                created_at=d.get("created_at", 0),
                updated_at=d.get("updated_at", 0),
            )
            for cid, d in data.items()
        ]
        convs.sort(key=lambda c: c.updated_at, reverse=True)
        return convs[:limit]

    def delete(self, conv_id: str) -> bool:
        data = self._read()
        if conv_id in data:
            del data[conv_id]
            self._write(data)
            return True
        return False

    def clear_all(self) -> int:
        data = self._read()
        count = len(data)
        self._write({})
        return count
