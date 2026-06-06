"""记忆工具 — 让 LLM 可以主动保存和检索跨会话记忆"""

from langchain_core.tools import tool
from memory import Memory

# 模块级 Memory 实例（与 graph.py 共享）
_memory = Memory()


@tool
def memory_save(content: str, category: str = "general") -> str:
    """保存一条持久记忆。当你发现需要跨会话保留的信息时使用，例如：
    - 用户偏好和习惯
    - 环境信息（路径、版本、配置）
    - 经验教训（踩过的坑、成功的方案）
    - 用户纠正你的记录

    Args:
        content: 要记忆的内容
        category: 分类 (general/preference/environment/correction)
    """
    return _memory.add_note(content, category)


@tool
def memory_search(query: str) -> str:
    """搜索持久记忆。当你需要回忆之前会话中保存的信息时使用。

    Args:
        query: 搜索关键词
    """
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


@tool
def memory_forget(text: str) -> str:
    """删除匹配的持久记忆。当信息过时或错误时使用。

    Args:
        text: 要删除的记忆内容（模糊匹配）
    """
    return _memory.remove_note(text)
