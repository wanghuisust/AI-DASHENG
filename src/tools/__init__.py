"""工具注册表 - 统一管理所有可用工具"""

from .terminal_tool import terminal_execute
from .file_tool import read_file, write_file, cleanup_tmp_files
from .search_tool import search_files
from .web_search_tool import web_search
from .memory_tool import memory_save, memory_search, memory_forget
from .skill_tool import skill_install, skill_list, skill_search, skill_remove, skill_view

# 所有工具列表，供 LLM schema 生成和 ToolNode 使用
ALL_TOOLS = [
    terminal_execute, read_file, write_file, search_files,
    web_search, memory_save, memory_search, memory_forget,
    skill_install, skill_list, skill_search, skill_remove, skill_view,
    cleanup_tmp_files
]

__all__ = ["ALL_TOOLS"]
