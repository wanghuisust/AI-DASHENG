"""文件搜索工具"""

from langchain_core.tools import tool


@tool
def search_files(pattern: str, directory: str = ".") -> str:
    """在指定目录下搜索文件名匹配的文件。

    Args:
        pattern: 文件名匹配模式（支持 * 通配符，如 *.py, *config*）
        directory: 搜索目录，默认当前目录

    Returns:
        匹配的文件列表
    """
    import glob
    import os

    try:
        search_path = os.path.join(directory, "**", pattern)
        matches = glob.glob(search_path, recursive=True)
        if not matches:
            return f"未找到匹配 '{pattern}' 的文件"
        # 限制结果数量
        if len(matches) > 50:
            return "\n".join(matches[:50]) + f"\n... 共 {len(matches)} 个结果，仅显示前50个"
        return "\n".join(matches)
    except Exception as e:
        return f"[错误] 搜索失败: {e}"
