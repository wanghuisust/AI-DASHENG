"""文件读写工具"""

import os
import re
from langchain_core.tools import tool
from tools.tmp_manager import get_tmp_file, get_tmp_dir_path


# 需要自动重定向到临时目录的文件扩展名
TEMP_EXTENSIONS = {'.py', '.sh', '.bat', '.cmd', '.ps1', '.js', '.ts', '.jsx', '.tsx', '.vue', '.html', '.css', '.json', '.yaml', '.yml', '.xml', '.sql', '.log', '.txt'}


@tool
def read_file(path: str, encoding: str = "utf-8") -> str:
    """读取文件内容。支持文本文件，自动截断超长文件。

    Args:
        path: 文件路径
        encoding: 文件编码，默认 utf-8

    Returns:
        文件内容字符串
    """
    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            content = f.read()
        # 截断超长文件
        max_chars = 50000
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n... (文件过长，已截断，共 {len(content)} 字符)"
        return content
    except FileNotFoundError:
        return f"[错误] 文件不存在: {path}"
    except Exception as e:
        return f"[错误] 读取失败: {e}"


@tool
def write_file(path: str, content: str) -> str:
    """写入文件。如果文件不存在则创建，存在则覆盖。

    注意：写入临时脚本文件（.py/.sh/.bat 等）时，会自动重定向到临时缓冲目录，
    任务完成后自动清理。

    Args:
        path: 文件路径
        content: 要写入的内容

    Returns:
        操作结果
    """
    try:
        # 判断是否为临时脚本文件
        ext = os.path.splitext(path)[1].lower()
        if ext in TEMP_EXTENSIONS:
            # 检查是否是绝对路径或包含目录路径
            if os.path.isabs(path) or '/' in path or '\\' in path:
                # 如果用户明确指定了目录（如 data/xxx.py），则使用指定路径
                # 如果只是文件名（如 script.py），则重定向到临时目录
                dirname = os.path.dirname(path)
                if not dirname:
                    # 纯文件名，重定向到临时目录
                    real_path = get_tmp_file(ext)
                    return _write_content(real_path, content)
            
            # 有目录路径，使用用户指定的路径
            return _write_content(path, content)
        else:
            # 非临时文件类型，直接写入
            return _write_content(path, content)
    except Exception as e:
        return f"[错误] 写入失败: {e}"


def _write_content(path: str, content: str) -> str:
    """实际写入文件内容"""
    import os
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"成功写入 {len(content)} 字符到 {path}"


@tool
def cleanup_tmp_files() -> str:
    """清理临时缓冲目录中的所有文件。

    当 AI Agent 完成所有临时脚本任务后，可以调用此工具清理临时文件。
    
    Returns:
        清理结果
    """
    try:
        from tools.tmp_manager import cleanup_tmp_dir
        cleanup_tmp_dir(max_age_hours=0)  # 立即清理所有文件
        tmp_dir = get_tmp_dir_path()
        return f"已清理临时目录: {tmp_dir}"
    except Exception as e:
        return f"[错误] 清理失败: {e}"
