"""持久记忆系统 — 类似 Hermes Agent 的 memory

核心区别于"对话历史"：
  - 对话历史：临时上下文，会话结束即消失
  - 持久记忆：跨会话保留，存储用户偏好、环境事实、经验教训

两个存储区：
  1. user_profile — 用户画像（名字、角色、偏好）
  2. notes — 通用笔记（环境信息、经验教训、事实）

LLM 可以通过 memory_save / memory_search 工具主动读写记忆。
"""

import json
import re
from pathlib import Path
from datetime import datetime

MEMORY_DIR = Path("data/memory")


class Memory:
    """持久记忆 — JSON 文件存储，跨会话保留"""

    def __init__(self, path: str = None):
        self.path = Path(path) if path else MEMORY_DIR / "memory.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {
            "user_profile": [],
            "notes": [],
            "corrections": []  # 用户纠正 AI 的记录
        }

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    # ── 写操作 ──

    def add_note(self, content: str, category: str = "general") -> str:
        """添加一条笔记"""
        entry = {
            "content": content,
            "category": category,
            "created_at": datetime.now().isoformat()
        }
        self._data["notes"].append(entry)
        self._save()
        return f"已记忆: {content[:50]}..."

    def add_user_profile(self, content: str) -> str:
        """添加用户画像信息"""
        self._data["user_profile"].append(content)
        self._save()
        return f"已记录用户信息: {content[:50]}..."

    def add_correction(self, wrong: str, correct: str) -> str:
        """记录用户纠正"""
        entry = {
            "wrong": wrong,
            "correct": correct,
            "created_at": datetime.now().isoformat()
        }
        self._data["corrections"].append(entry)
        self._save()
        return f"已记录纠正: '{wrong}' → '{correct}'"

    def replace_note(self, old_text: str, new_text: str) -> str:
        """替换匹配的笔记"""
        for i, note in enumerate(self._data["notes"]):
            if old_text.lower() in note.get("content", "").lower():
                self._data["notes"][i]["content"] = new_text
                self._save()
                return f"已更新: {old_text[:30]}... → {new_text[:30]}..."
        return f"未找到匹配的笔记: {old_text[:30]}..."

    def remove_note(self, text: str) -> str:
        """删除匹配的笔记"""
        before = len(self._data["notes"])
        self._data["notes"] = [
            n for n in self._data["notes"]
            if text.lower() not in n.get("content", "").lower()
        ]
        removed = before - len(self._data["notes"])
        self._save()
        return f"已删除 {removed} 条匹配笔记"

    # ── 读操作 ──

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """搜索记忆"""
        query_lower = query.lower()
        results = []

        for note in self._data.get("notes", []):
            if query_lower in note.get("content", "").lower():
                results.append({"type": "note", **note})

        for profile in self._data.get("user_profile", []):
            if query_lower in profile.lower():
                results.append({"type": "profile", "content": profile})

        for corr in self._data.get("corrections", []):
            if query_lower in corr.get("wrong", "").lower() or query_lower in corr.get("correct", "").lower():
                results.append({"type": "correction", **corr})

        return results[:limit]

    def get_context(self, max_items: int = 10) -> str:
        """获取用于注入 system prompt 的记忆上下文"""
        parts = []

        # 用户画像（全量）
        profiles = self._data.get("user_profile", [])
        if profiles:
            parts.append("## 用户画像\n" + "\n".join(f"- {p}" for p in profiles))

        # 最近的笔记（限制数量）
        notes = self._data.get("notes", [])[-max_items:]
        if notes:
            parts.append("## 记忆笔记\n" + "\n".join(
                f"- [{n.get('category', 'general')}] {n['content']}" for n in notes
            ))

        # 最近的纠正
        corrections = self._data.get("corrections", [])[-5:]
        if corrections:
            parts.append("## 用户纠正\n" + "\n".join(
                f"- ❌ {c['wrong']} → ✅ {c['correct']}" for c in corrections
            ))

        return "\n\n".join(parts) if parts else ""

    def list_all(self) -> dict:
        """返回所有记忆数据"""
        return self._data.copy()
