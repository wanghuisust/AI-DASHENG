"""工具注册表 - 统一管理所有可用工具"""

from .terminal_tool import terminal_execute
from .file_tool import read_file, write_file
from .search_tool import search_files

# 所有工具列表，供 LLM schema 生成和 ToolNode 使用
ALL_TOOLS = [terminal_execute, read_file, write_file, search_files]

__all__ = ["ALL_TOOLS"]
