"""Skill 管理工具 — 安装/列出/搜索技能"""

from langchain_core.tools import tool


@tool
def skill_install(source: str, name: str = "") -> str:
    """安装技能。支持从 ClawHub 或 GitHub 安装。

    Args:
        source: 安装来源。
            - ClawHub 技能名（如 "github-code-review"）：自动从 ClawHub 下载
            - GitHub URL（如 "https://github.com/user/repo"）：从 GitHub 仓库下载
            - GitHub 短格式（如 "user/repo"）：从 GitHub 仓库下载
            - GitHub 子目录（如 "https://github.com/user/repo/tree/main/skills/github-auth"）：下载子目录
        name: 可选，自定义技能名（留空则从 SKILL.md 自动读取）

    重要提示：
        - 如果 ClawHub 不可达，会自动走代理重试
        - 如果 ClawHub 上找不到，请尝试用 GitHub URL 安装
        - 安装后技能会自动注入到 system prompt，下次对话生效

    Returns:
        安装结果信息
    """
    from skills import SkillManager
    import os

    # 获取全局 skill_manager 实例
    _sm = _get_skill_manager()
    if not _sm:
        return "[错误] SkillManager 未初始化"

    # 判断来源类型
    if source.startswith("http") or "/" in source.split()[0]:
        # GitHub URL 或 owner/repo 格式
        result = _sm.install_from_github(source)
    else:
        # ClawHub 技能名
        result = _sm.install_from_clawhub(source)

    return result.get("message", str(result))


@tool
def skill_list() -> str:
    """列出所有已安装的技能。

    Returns:
        已安装技能列表
    """
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


@tool
def skill_search(query: str, limit: int = 10) -> str:
    """搜索 ClawHub 技能库。如果 ClawHub 不可达，会返回空结果，请改用 GitHub 搜索。

    Args:
        query: 搜索关键词（如 "github", "deploy", "debug"）
        limit: 返回结果数量上限（默认 10）

    Returns:
        搜索结果列表。如果为空，建议用 skill_install(source="GitHub URL") 直接安装
    """
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
    lines.append(f"\n使用 skill_install(source=\"技能名\") 安装")
    return "\n".join(lines)


@tool
def skill_view(name: str) -> str:
    """加载并查看技能的详细内容。当 system prompt 中的技能索引有匹配时，必须先用此工具加载技能详情。

    Args:
        name: 技能名称（如 "github-repo-management"、"systematic-debugging"）

    Returns:
        技能的完整内容（含步骤、命令、陷阱等），用于指导执行
    """
    _sm = _get_skill_manager()
    if not _sm:
        return "[错误] SkillManager 未初始化"

    skill = _sm.get(name)
    if not skill:
        # 模糊匹配：名称部分包含
        candidates = [s for s in _sm.skills if name.lower() in s.lower()]
        if len(candidates) == 1:
            skill = _sm.skills[candidates[0]]
        elif candidates:
            return f"未找到 '{name}'，相似技能: {', '.join(candidates[:5])}"
        else:
            return f"未找到技能 '{name}'。用 skill_list() 查看所有可用技能"

    return skill.to_prompt()


@tool
def skill_remove(name: str) -> str:
    """删除已安装的技能。

    Args:
        name: 技能名称

    Returns:
        删除结果
    """
    _sm = _get_skill_manager()
    if not _sm:
        return "[错误] SkillManager 未初始化"

    if _sm.delete(name):
        return f"✅ 已删除技能 '{name}'"
    return f"❌ 技能 '{name}' 不存在"


def _get_skill_manager():
    """获取全局 SkillManager 实例（从 graph.py 模块级变量）"""
    try:
        from graph import _skill_manager
        return _skill_manager
    except ImportError:
        return None
