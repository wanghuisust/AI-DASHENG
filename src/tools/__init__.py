"""工具注册表 - 统一管理所有可用工具 (DashengTool 版)

所有工具现在都是 DashengTool 实例（通过 build_tool 创建），
统一具备：name/description/validate_input/process_tool_result/is_read_only 等能力。
"""

from .terminal_tool import terminal_execute
from .file_tool import read_file, write_file, cleanup_tmp_files
from .search_tool import search_files
from .web_search_tool import web_search
from .memory_tool import memory_save, memory_search, memory_forget
from .skill_tool import skill_install, skill_list, skill_search, skill_remove, skill_view
from .disk_tool import disk_analyze

# FileStateCache 重置函数（graph.py 在新 session 开始时调用）
from .file_state_cache import reset_file_state_cache

# 所有工具列表，供 LLM schema 生成和 ToolNode 使用
ALL_TOOLS = [
    terminal_execute, read_file, write_file, search_files,
    web_search, memory_save, memory_search, memory_forget,
    skill_install, skill_list, skill_search, skill_remove, skill_view,
    cleanup_tmp_files, disk_analyze
]

__all__ = ["ALL_TOOLS", "reset_file_state_cache"]