"""文件搜索工具"""

from langchain_core.tools import tool


@tool
def search_files(pattern: str, directory: str = ".", target: str = "files", file_glob: str = None) -> str:
    """搜索文件或文件内容。

    Args:
        pattern: 搜索模式。
            - target="files" 时：文件名匹配模式（支持 * 通配符，如 *.py, *config*）
            - target="content" 时：文件内容搜索（正则表达式，如 "train_run", "loss=\\d+\\.\\d+"）
        directory: 搜索目录，默认当前目录
        target: 搜索类型，"files" 按文件名搜索，"content" 按文件内容搜索（默认 "files"）
        file_glob: content 模式下限定文件类型（如 "*.py", "*.log"），不指定则搜索所有文本文件

    Returns:
        匹配的文件列表或内容匹配结果
    """
    import glob
    import os
    import re

    try:
        if target == "content":
            return _search_content(pattern, directory, file_glob)
        else:
            return _search_filenames(pattern, directory)
    except Exception as e:
        return f"[错误] 搜索失败: {e}"


def _search_filenames(pattern: str, directory: str) -> str:
    """按文件名搜索"""
    import glob
    import os

    search_path = os.path.join(directory, "**", pattern)
    matches = glob.glob(search_path, recursive=True)
    if not matches:
        return f"未找到匹配 '{pattern}' 的文件"
    # 限制结果数量
    if len(matches) > 50:
        return "\n".join(matches[:50]) + f"\n... 共 {len(matches)} 个结果，仅显示前50个"
    return "\n".join(matches)


def _search_content(pattern: str, directory: str, file_glob: str = None) -> str:
    """按文件内容搜索（ripgrep 风格）"""
    import os
    import re

    results = []
    MAX_RESULTS = 30
    MAX_LINE_LEN = 200

    # 默认搜索的文本文件扩展名
    TEXT_EXTS = {
        ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
        ".md", ".txt", ".log", ".csv", ".xml", ".html", ".css", ".sh", ".bat",
        ".env", ".gitignore", ".dockerfile", ".rs", ".go", ".java", ".c", ".cpp",
    }

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        # 正则语法错误，退化为纯文本搜索
        regex = re.compile(re.escape(pattern), re.IGNORECASE)

    for root, dirs, files in os.walk(directory):
        # 跳过隐藏目录和常见大目录
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in (
            "node_modules", "__pycache__", ".git", ".venv", "venv",
            "dist", "build", ".mypy_cache", ".tox",
        )]

        for fname in files:
            if len(results) >= MAX_RESULTS:
                break

            # 文件类型过滤
            _, ext = os.path.splitext(fname)
            if file_glob:
                # 支持 *.py 这类 glob
                from fnmatch import fnmatch
                if not fnmatch(fname, file_glob):
                    continue
            elif ext.lower() not in TEXT_EXTS:
                continue

            fpath = os.path.join(root, fname)
            try:
                # 只读前 500KB，避免读大文件卡住
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for line_no, line in enumerate(f, 1):
                        if regex.search(line):
                            rel_path = os.path.relpath(fpath, directory)
                            stripped = line.strip()[:MAX_LINE_LEN]
                            results.append(f"{rel_path}:{line_no}: {stripped}")
                            if len(results) >= MAX_RESULTS:
                                break
                        # 限制每个文件最多扫 5000 行
                        if line_no > 5000:
                            break
            except (OSError, PermissionError):
                continue

        if len(results) >= MAX_RESULTS:
            break

    if not results:
        return f"未在 '{directory}' 中找到匹配 '{pattern}' 的内容"
    output = "\n".join(results)
    if len(results) >= MAX_RESULTS:
        output += f"\n... (仅显示前 {MAX_RESULTS} 条结果)"
    return output
