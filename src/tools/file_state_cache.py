"""文件状态缓存 — Read-Before-Edit 核心安全机制

迁移自 Claude Code 的 FileStateCache 设计：
- read_file 调用时记录文件路径 + mtime + 内容摘要
- write_file / edit_file 调用时检查：该文件是否已读取且 mtime 未变
- 已存在文件必须先 Read 才能写（防止 LLM 基于过时记忆覆写）
- 新文件不限制（mtime=None 表示是新文件）

设计原则：
- LRU 缓存，避免长 session 中缓存无限增长
- Windows 兼容：mtime 精度到秒，内容比对防误报
"""

import os
import time
from collections import OrderedDict


class _FileReadRecord:
    """单条文件读取记录"""
    __slots__ = ("mtime", "content_hash", "is_partial", "offset", "limit")

    def __init__(self, mtime: float, content_hash: str, is_partial: bool = False,
                 offset: int = None, limit: int = None):
        self.mtime = mtime            # 文件最后修改时间
        self.content_hash = content_hash  # 内容前 4KB 的哈希（快速比对）
        self.is_partial = is_partial  # 是否只读了部分（offset/limit）
        self.offset = offset
        self.limit = limit


def _quick_hash(content: str) -> str:
    """内容快速哈希（前 4KB），用于 mtime 变化时的内容比对"""
    import hashlib
    snippet = content[:4096]
    return hashlib.sha256(snippet.encode("utf-8", errors="replace")).hexdigest()[:16]


class FileStateCache:
    """已读文件状态缓存 — 核心：Edit/Write 前必须 Read

    LRU 缓存，最多保留 200 条记录（防止长 session 内存膨胀）。
    """

    MAX_ENTRIES = 200

    def __init__(self):
        self._state: OrderedDict[str, _FileReadRecord] = OrderedDict()

    def record_read(self, path: str, content: str,
                    offset: int = None, limit: int = None) -> None:
        """read_file 调用后记录文件状态

        Args:
            path: 文件绝对路径（已规范化）
            content: 文件完整内容
            offset: 读取起始行（None=全文）
            limit: 读取行数限制（None=全文）
        """
        path = os.path.normpath(os.path.abspath(path))
        is_partial = offset is not None or limit is not None

        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0

        self._state[path] = _FileReadRecord(
            mtime=mtime,
            content_hash=_quick_hash(content),
            is_partial=is_partial,
            offset=offset,
            limit=limit,
        )

        # LRU 淘汰
        if len(self._state) > self.MAX_ENTRIES:
            self._state.popitem(last=False)

    def has_full_read(self, path: str) -> bool:
        """检查是否已完整读取该文件（非部分读取）"""
        path = os.path.normpath(os.path.abspath(path))
        record = self._state.get(path)
        return record is not None and not record.is_partial

    def has_any_read(self, path: str) -> bool:
        """检查是否已读取该文件（包括部分读取）"""
        path = os.path.normpath(os.path.abspath(path))
        return path in self._state

    def is_stale(self, path: str) -> bool:
        """检查文件在 Read 后是否被外部修改

        返回 True 表示文件已过时，需要重新 Read。
        Windows 兼容：mtime 精度到秒 + 内容比对防误报。
        """
        path = os.path.normpath(os.path.abspath(path))
        record = self._state.get(path)
        if record is None:
            return True  # 从未读取 = 过时

        try:
            current_mtime = os.path.getmtime(path)
        except OSError:
            return True  # 文件不存在 = 过时

        # mtime 未变 → 未过时
        if current_mtime == record.mtime:
            return False

        # mtime 变了 → 但 Windows 上可能只是元数据变化，比对内容
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                current_content = f.read()
            current_hash = _quick_hash(current_content)
            if current_hash == record.content_hash:
                # 内容没变，只是 mtime 变了（Windows 文件系统特性）
                # 更新 mtime 记录，下次不再误报
                record.mtime = current_mtime
                return False
            return True  # 内容确实变了
        except OSError:
            return True

    def check_write_allowed(self, path: str, is_new_file: bool = False) -> tuple[bool, str]:
        """检查是否允许写入文件

        返回 (allowed, reason):
        - (True, "") — 允许写入
        - (False, "错误信息") — 不允许

        规则（迁移自 Claude Code）：
        1. 新文件（不存在） → 允许
        2. 已存在文件 → 必须先 Read 且 mtime 未变
        """
        path = os.path.normpath(os.path.abspath(path))

        # 新文件 → 允许
        if is_new_file or not os.path.exists(path):
            return True, ""

        # 已存在文件 → 检查是否已读取
        if not self.has_any_read(path):
            return False, (
                f"文件 {path} 已存在但尚未读取。"
                "请先调用 read_file 读取该文件，确认其当前内容后再写入。"
                "这是为了防止你基于过时信息覆写文件。"
            )

        # 已读取 → 检查 mtime
        if self.is_stale(path):
            return False, (
                f"文件 {path} 在上次读取后被修改了。"
                "请重新调用 read_file 读取最新内容后再写入。"
            )

        # 部分读取但非全文 → 提示但不阻止（LLM 可能只读了几行然后追加）
        if not self.has_full_read(path):
            # 只警告，不阻止 — 因为很多场景只读几行就写是合理的
            pass

        return True, ""

    def clear(self):
        """清空缓存（新 session 开始时调用）"""
        self._state.clear()


# ── 模块级单例 ──
# 每次 graph invoke 前由 agent_api.py 调用 reset()
_file_state_cache = FileStateCache()


def get_file_state_cache() -> FileStateCache:
    """获取全局 FileStateCache 实例"""
    return _file_state_cache


def reset_file_state_cache():
    """重置 FileStateCache（新 turn/session 开始时调用）"""
    _file_state_cache.clear()