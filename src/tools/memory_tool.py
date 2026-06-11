"""记忆工具 — 让 LLM 可以主动保存和检索跨会话记忆 (DashengTool 版)"""

from pydantic import BaseModel, Field
from tools.tool_base import build_tool, DEFAULT_MAX_RESULT_SIZE_CHARS
from memory import Memory

# 模块级 Memory 实例（与 graph.py 共享）
_memory = Memory()


# ── 参数 Schema ──

class MemorySaveInput(BaseModel):
    content: str = Field(description="要记忆的内容")
    category: str = Field(default="general", description="分类: general/preference/environment/correction")


class MemorySearchInput(BaseModel):
    query: str = Field(description="搜索关键词")


class MemoryForgetInput(BaseModel):
    text: str = Field(description="要删除的记忆内容（模糊匹配）")


# ── 实现 ──

def _memory_save_impl(content: str, category: str = "general") -> str:
    return _memory.add_note(content, category)


def _memory_search_impl(query: str) -> str:
    results = _memory.search(query)
    if not results:
        return "未找到相关记忆"

    lines = []
    for r in results:
        rtype = r.get("type", "note")
        if rtype == "note":
            lines.append(f"[{r.get('category', '')}] {r['content']}")
        elif rtype == "profile":
            lines.append(f"[用户] {r['content']}")
        elif rtype == "correction":
            lines.append(f"[纠正] ❌ {r.get('wrong', '')} → ✅ {r.get('correct', '')}")
    return "\n".join(lines)


def _memory_forget_impl(text: str) -> str:
    return _memory.remove_note(text)


# ── 注册 ──

memory_save = build_tool(
    name="memory_save",
    description=(
        "保存一条持久记忆。当你发现需要跨会话保留的信息时使用。\n"
        "Args:\n"
        "  content: 要记忆的内容\n"
        "  category: 分类(general/preference/environment/correction)"
    ),
    func=_memory_save_impl,
    args_schema=MemorySaveInput,
    max_result_size=DEFAULT_MAX_RESULT_SIZE_CHARS,
    is_read_only=False,
    is_concurrency_safe=False,
)

memory_search = build_tool(
    name="memory_search",
    description="搜索持久记忆。当你需要回忆之前会话中保存的信息时使用。",
    func=_memory_search_impl,
    args_schema=MemorySearchInput,
    max_result_size=DEFAULT_MAX_RESULT_SIZE_CHARS,
    is_read_only=True,
    is_concurrency_safe=True,
)

memory_forget = build_tool(
    name="memory_forget",
    description="删除匹配的持久记忆。当信息过时或错误时使用。",
    func=_memory_forget_impl,
    args_schema=MemoryForgetInput,
    max_result_size=DEFAULT_MAX_RESULT_SIZE_CHARS,
    is_read_only=False,
    is_concurrency_safe=False,
)