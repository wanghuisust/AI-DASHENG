"""文件读写工具"""

from langchain_core.tools import tool


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

    Args:
        path: 文件路径
        content: 要写入的内容

    Returns:
        操作结果
    """
    try:
        import os
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"成功写入 {len(content)} 字符到 {path}"
    except Exception as e:
        return f"[错误] 写入失败: {e}"
