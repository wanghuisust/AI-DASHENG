"""Skill 管理工具 — 安装/列出/搜索技能 (DashengTool 版)"""

from pydantic import BaseModel, Field
from tools.tool_base import build_tool, DEFAULT_MAX_RESULT_SIZE_CHARS


# ── 参数 Schema ──

class SkillInstallInput(BaseModel):
    source: str = Field(description="安装来源: ClawHub技能名 / GitHub URL / owner/repo")
    name: str = Field(default="", description="可选，自定义技能名")


class SkillListInput(BaseModel):
    pass


class SkillSearchInput(BaseModel):
    query: str = Field(description="搜索关键词")
    limit: int = Field(default=10, description="返回结果数量上限")


class SkillViewInput(BaseModel):
    name: str = Field(description="技能名称")


class SkillRemoveInput(BaseModel):
    name: str = Field(description="技能名称")


# ── 辅助 ──

def _get_skill_manager():
    """获取全局 SkillManager 实例"""
    try:
        from graph import _skill_manager
        return _skill_manager
    except ImportError:
        return None


# ── 实现 ──

def _skill_install_impl(source: str, name: str = "") -> str:
    _sm = _get_skill_manager()
    if not _sm:
        return "[错误] SkillManager 未初始化"

    if source.startswith("http") or "/" in source.split()[0]:
        result = _sm.install_from_github(source)
    else:
        result = _sm.install_from_clawhub(source)

    return result.get("message", str(result))


def _skill_list_impl() -> str:
    _sm = _get_skill_manager()
    if not _sm:
        return "[错误] SkillManager 未初始化"

    skills = _sm.list_skills()
    if not skills:
        return "📋 暂无已安装技能。\n使用 skill_install 安装技能，如：skill_install(source=\"github-code-review\")"

    lines = ["📋 已安装技能："]
    for s in skills:
        triggers = ", ".join(s.get("triggers", []))
        desc = s.get("description", "")
        lines.append(f"  • {s['name']} — {desc}")
        if triggers:
            lines.append(f"    触发词: {triggers}")
    lines.append(f"\n共 {len(skills)} 个技能")
    return "\n".join(lines)


def _skill_search_impl(query: str, limit: int = 10) -> str:
    _sm = _get_skill_manager()
    if not _sm:
        return "[错误] SkillManager 未初始化"

    results = _sm.search_clawhub(query, limit)
    if not results:
        return f"未在 ClawHub 找到匹配 '{query}' 的技能"

    lines = [f"🔍 ClawHub 搜索 '{query}'："]
    for r in results[:limit]:
        name = r.get("name", "?")
        desc = r.get("description", "")
        lines.append(f"  • {name} — {desc}")
    lines.append("\n使用 skill_install(source=\"技能名\") 安装")
    return "\n".join(lines)


def _skill_view_impl(name: str) -> str:
    _sm = _get_skill_manager()
    if not _sm:
        return "[错误] SkillManager 未初始化"

    skill = _sm.get(name)
    if not skill:
        candidates = [s for s in _sm.skills if name.lower() in s.lower()]
        if len(candidates) == 1:
            skill = _sm.skills[candidates[0]]
        elif candidates:
            return f"未找到 '{name}'，相似技能: {', '.join(candidates[:5])}"
        else:
            return f"未找到技能 '{name}'。用 skill_list() 查看所有可用技能"

    return skill.to_prompt()


def _skill_remove_impl(name: str) -> str:
    _sm = _get_skill_manager()
    if not _sm:
        return "[错误] SkillManager 未初始化"

    if _sm.delete(name):
        return f"✅ 已删除技能 '{name}'"
    return f"❌ 技能 '{name}' 不存在"


# ── 注册 ──

skill_install = build_tool(
    name="skill_install",
    description=(
        "安装技能。支持从 ClawHub 或 GitHub 安装。\n"
        "Args:\n"
        "  source: 安装来源(ClawHub技能名/GitHub URL/owner/repo)\n"
        "  name: 可选，自定义技能名"
    ),
    func=_skill_install_impl,
    args_schema=SkillInstallInput,
    max_result_size=DEFAULT_MAX_RESULT_SIZE_CHARS,
    is_read_only=False,
    is_concurrency_safe=False,
)

skill_list = build_tool(
    name="skill_list",
    description="列出所有已安装的技能。",
    func=_skill_list_impl,
    args_schema=SkillListInput,
    max_result_size=DEFAULT_MAX_RESULT_SIZE_CHARS,
    is_read_only=True,
    is_concurrency_safe=True,
)

skill_search = build_tool(
    name="skill_search",
    description="搜索 ClawHub 技能库。",
    func=_skill_search_impl,
    args_schema=SkillSearchInput,
    max_result_size=DEFAULT_MAX_RESULT_SIZE_CHARS,
    is_read_only=True,
    is_concurrency_safe=True,
)

skill_view = build_tool(
    name="skill_view",
    description="加载并查看技能的详细内容。当 system prompt 中的技能索引有匹配时，必须先用此工具加载技能详情。",
    func=_skill_view_impl,
    args_schema=SkillViewInput,
    max_result_size=float("inf"),  # 技能内容可能很长，不持久化
    is_read_only=True,
    is_concurrency_safe=True,
)

skill_remove = build_tool(
    name="skill_remove",
    description="删除已安装的技能。",
    func=_skill_remove_impl,
    args_schema=SkillRemoveInput,
    max_result_size=DEFAULT_MAX_RESULT_SIZE_CHARS,
    is_read_only=False,
    is_concurrency_safe=False,
)