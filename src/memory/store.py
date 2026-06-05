"""记忆系统 - 跨会话持久化存储"""

import json
import os
from pathlib import Path


class Memory:
    """简单的 JSON 文件记忆存储，类似 Hermes 的 memory 系统"""

    def __init__(self, path: str = "data/memory.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {"user_profile": [], "notes": []}
        return {"user_profile": [], "notes": []}

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def add_note(self, content: str):
        """添加一条记忆笔记"""
        self._data["notes"].append(content)
        self._save()

    def add_user_profile(self, content: str):
        """添加用户画像信息"""
        self._data["user_profile"].append(content)
        self._save()

    def get_context(self) -> str:
        """获取用于注入 system prompt 的记忆上下文"""
        parts = []
        if self._data["user_profile"]:
            parts.append("## 用户画像\n" + "\n".join(f"- {p}" for p in self._data["user_profile"]))
        if self._data["notes"]:
            parts.append("## 记忆笔记\n" + "\n".join(f"- {n}" for n in self._data["notes"]))
        return "\n\n".join(parts) if parts else ""

    def list_all(self) -> dict:
        """返回所有记忆数据"""
        return self._data.copy()
