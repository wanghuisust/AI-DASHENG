"""文件搜索工具 — ripgrep 加速版 (DashengTool 版)

优先使用 rg（ripgrep）搜索，回退到 grep，最终回退到纯 Python os.walk。
支持分页（offset/limit）、多种输出模式、上下文行。
"""

import logging
import os
import re
import shutil
import subprocess
from typing import Optional

from pydantic import BaseModel, Field
from tools.tool_base import build_tool, ValidationResult, DEFAULT_MAX_RESULT_SIZE_CHARS

logger = logging.getLogger(__name__)

# ── 工具函数 ──────────────────────────────────────────────

def _has_rg() -> bool:
    """检测系统是否安装了 ripgrep"""
    return shutil.which("rg") is not None


def _has_grep() -> bool:
    """检测系统是否有 grep"""
    return shutil.which("grep") is not None


def _run_cmd(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """执行命令，返回 (exit_code, stdout, stderr)"""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)


def _parse_match_line(line: str) -> Optional[tuple[str, int, str]]:
    """解析 rg/grep 输出的 match 行: 'path:lineno:content'

    兼容 Windows 盘符路径（如 C:\\path）。
    返回 (path, line_number, content) 或 None。
    """
    _match_re = re.compile(r'^([A-Za-z]:)?(.*?):(\d+):(.*)$')
    m = _match_re.match(line)
    if m:
        path = (m.group(1) or '') + m.group(2)
        try:
            lineno = int(m.group(3))
        except ValueError:
            return None
        content = m.group(4)
        return path, lineno, content
    return None


def _format_results(
    matches: list[tuple[str, int, str]],
    files: list[str],
    counts: dict[str, int],
    output_mode: str,
    total_count: int,
    limit: int,
    offset: int,
    truncated: bool,
) -> str:
    """将搜索结果格式化为字符串"""
    if output_mode == "files_only":
        if not files:
            return "未找到匹配的文件"
        lines = files
        if truncated:
            lines.append(f"... (共 {total_count} 个结果，仅显示 {len(files)} 个)")
        return "\n".join(lines)

    elif output_mode == "count":
        if not counts:
            return "未找到匹配的内容"
        total = sum(counts.values())
        lines = [f"{path}: {cnt}" for path, cnt in sorted(counts.items(), key=lambda x: -x[1])]
        lines.append(f"\n共 {len(counts)} 个文件，{total} 处匹配")
        return "\n".join(lines)

    else:  # content mode
        if not matches:
            return "未找到匹配的内容"
        lines = []
        for path, lineno, content in matches:
            display = content.strip()[:200]
            lines.append(f"{path}:{lineno}: {display}")
        if truncated:
            lines.append(f"... (共 {total_count} 条结果，显示第 {offset+1}-{offset+len(matches)} 条)")
        return "\n".join(lines)


# ── 文件名搜索 ──────────────────────────────────────────

def _search_files_rg(pattern: str, directory: str, limit: int, offset: int) -> dict:
    """用 rg --files 按文件名搜索（快速，尊重 .gitignore）"""
    if '/' not in pattern and not pattern.startswith('*'):
        glob_pattern = f"*{pattern}*"
    else:
        glob_pattern = pattern

    fetch_limit = limit + offset + 50

    cmd = ["rg", "--files", "--sortr=modified", "-g", glob_pattern, directory]
    exit_code, stdout, stderr = _run_cmd(cmd, timeout=30)

    if exit_code == 2 or not stdout.strip():
        cmd = ["rg", "--files", "-g", glob_pattern, directory]
        exit_code, stdout, stderr = _run_cmd(cmd, timeout=30)

    all_files = [f for f in stdout.strip().split('\n') if f]
    page = all_files[offset:offset + limit]

    return {
        "files": page,
        "total_count": len(all_files),
        "truncated": len(all_files) > offset + limit,
    }


def _search_files_glob(pattern: str, directory: str, limit: int, offset: int) -> dict:
    """纯 Python glob 按文件名搜索（回退方案）"""
    import glob as glob_mod

    search_path = os.path.join(directory, "**", pattern)
    matches = glob_mod.glob(search_path, recursive=True)

    matches.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)

    total = len(matches)
    page = matches[offset:offset + limit]

    return {
        "files": page,
        "total_count": total,
        "truncated": total > offset + limit,
    }


# ── 内容搜索 ──────────────────────────────────────────

def _search_content_rg(
    pattern: str, directory: str, file_glob: Optional[str],
    limit: int, offset: int, output_mode: str, context: int,
) -> dict:
    """用 ripgrep 按内容搜索"""
    cmd = ["rg", "--line-number", "--no-heading", "--with-filename"]

    if context > 0:
        cmd.extend(["-C", str(context)])

    if file_glob:
        cmd.extend(["--glob", file_glob])

    if output_mode == "files_only":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")

    fetch_limit = limit + offset + (200 if context > 0 else 0)
    cmd.extend(["-m", str(fetch_limit)])

    cmd.extend([pattern, directory])

    exit_code, stdout, stderr = _run_cmd(cmd, timeout=30)

    if exit_code == 2 and not stdout.strip():
        return {"matches": [], "files": [], "counts": {}, "total_count": 0,
                "error": f"rg error: {stderr.strip()}"}
    if exit_code == 1 or not stdout.strip():
        return {"matches": [], "files": [], "counts": {}, "total_count": 0}

    if output_mode == "files_only":
        all_files = [f for f in stdout.strip().split('\n') if f]
        page = all_files[offset:offset + limit]
        return {"files": page, "total_count": len(all_files),
                "truncated": len(all_files) > offset + limit}

    elif output_mode == "count":
        counts = {}
        for line in stdout.strip().split('\n'):
            if ':' in line:
                parts = line.rsplit(':', 1)
                if len(parts) == 2:
                    try:
                        counts[parts[0]] = int(parts[1])
                    except ValueError:
                        pass
        return {"counts": counts, "total_count": sum(counts.values())}

    else:  # content mode
        matches = []
        for line in stdout.strip().split('\n'):
            if not line or line == "--":
                continue
            parsed = _parse_match_line(line)
            if parsed:
                matches.append(parsed)

        total = len(matches)
        page = matches[offset:offset + limit]
        return {
            "matches": page,
            "total_count": total,
            "truncated": total > offset + limit,
        }


def _search_content_grep(
    pattern: str, directory: str, file_glob: Optional[str],
    limit: int, offset: int, output_mode: str, context: int,
) -> dict:
    """用 grep 按内容搜索（回退方案）"""
    cmd = ["grep", "-rnH", "--exclude-dir=.*"]

    if file_glob:
        cmd.extend(["--include", file_glob])

    if output_mode == "files_only":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")

    if context > 0:
        cmd.extend(["-C", str(context)])

    cmd.extend(["--", pattern, directory])

    exit_code, stdout, stderr = _run_cmd(cmd, timeout=30)

    if exit_code != 0 or not stdout.strip():
        return {"matches": [], "files": [], "counts": {}, "total_count": 0}

    if output_mode == "files_only":
        all_files = [f for f in stdout.strip().split('\n') if f]
        page = all_files[offset:offset + limit]
        return {"files": page, "total_count": len(all_files),
                "truncated": len(all_files) > offset + limit}

    elif output_mode == "count":
        counts = {}
        for line in stdout.strip().split('\n'):
            if ':' in line:
                parts = line.rsplit(':', 1)
                if len(parts) == 2:
                    try:
                        counts[parts[0]] = int(parts[1])
                    except ValueError:
                        pass
        return {"counts": counts, "total_count": sum(counts.values())}

    else:
        matches = []
        for line in stdout.strip().split('\n'):
            if not line or line == "--":
                continue
            parsed = _parse_match_line(line)
            if parsed:
                matches.append(parsed)

        total = len(matches)
        page = matches[offset:offset + limit]
        return {
            "matches": page,
            "total_count": total,
            "truncated": total > offset + limit,
        }


def _search_content_python(
    pattern: str, directory: str, file_glob: Optional[str],
    limit: int, offset: int, output_mode: str,
) -> dict:
    """纯 Python os.walk 按内容搜索（最终回退方案）"""
    TEXT_EXTS = {
        ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
        ".md", ".txt", ".log", ".csv", ".xml", ".html", ".css", ".sh", ".bat",
        ".env", ".gitignore", ".dockerfile", ".rs", ".go", ".java", ".c", ".cpp",
    }

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE)

    all_matches = []
    all_files = []
    counts = {}

    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in (
            "node_modules", "__pycache__", ".git", ".venv", "venv",
            "dist", "build", ".mypy_cache", ".tox",
        )]

        for fname in files:
            _, ext = os.path.splitext(fname)
            if file_glob:
                from fnmatch import fnmatch
                if not fnmatch(fname, file_glob):
                    continue
            elif ext.lower() not in TEXT_EXTS:
                continue

            fpath = os.path.join(root, fname)
            try:
                file_count = 0
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for line_no, line in enumerate(f, 1):
                        if regex.search(line):
                            rel_path = os.path.relpath(fpath, directory)
                            stripped = line.strip()[:200]
                            all_matches.append((rel_path, line_no, stripped))
                            file_count += 1
                            if len(all_matches) >= limit + offset + 200:
                                break
                        if line_no > 5000:
                            break
                if file_count > 0:
                    all_files.append(fpath)
                    counts[fpath] = file_count
            except (OSError, PermissionError):
                continue

            if len(all_matches) >= limit + offset + 200:
                break

    if output_mode == "files_only":
        page = all_files[offset:offset + limit]
        return {"files": page, "total_count": len(all_files),
                "truncated": len(all_files) > offset + limit}
    elif output_mode == "count":
        return {"counts": counts, "total_count": sum(counts.values())}
    else:
        total = len(all_matches)
        page = all_matches[offset:offset + limit]
        return {
            "matches": page,
            "total_count": total,
            "truncated": total > offset + limit,
        }


# ── 主入口 ──────────────────────────────────────────────

class SearchFilesInput(BaseModel):
    pattern: str = Field(description="搜索模式。target='files'时为文件名匹配(支持*通配符)，target='content'时为正则表达式")
    directory: str = Field(default=".", description="搜索目录，默认当前目录")
    target: str = Field(default="files", description="搜索类型: files=按文件名, content=按文件内容")
    file_glob: Optional[str] = Field(default=None, description="限定文件类型(如*.py,*.log)")
    limit: int = Field(default=50, description="返回结果数量上限")
    offset: int = Field(default=0, description="跳过前N条结果，用于分页")
    output_mode: str = Field(default="content", description="内容搜索输出: content/files_only/count")
    context: int = Field(default=0, description="显示匹配行上下各N行(仅rg/grep)")


def _search_files_impl(
    pattern: str,
    directory: str = ".",
    target: str = "files",
    file_glob: str = None,
    limit: int = 50,
    offset: int = 0,
    output_mode: str = "content",
    context: int = 0,
) -> str:
    """核心逻辑 — 搜索文件或文件内容"""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    try:
        if target == "files":
            return _do_search_files(pattern, directory, limit, offset)
        else:
            return _do_search_content(pattern, directory, file_glob, limit, offset, output_mode, context)
    except Exception as e:
        logger.error(f"[search_files] error: {e}", exc_info=True)
        return f"[错误] 搜索失败: {e}"


def _do_search_files(pattern: str, directory: str, limit: int, offset: int) -> str:
    """按文件名搜索"""
    if _has_rg():
        result = _search_files_rg(pattern, directory, limit, offset)
        engine = "rg"
    else:
        result = _search_files_glob(pattern, directory, limit, offset)
        engine = "glob"

    files = result.get("files", [])
    total = result.get("total_count", 0)
    truncated = result.get("truncated", False)

    if not files:
        return f"未找到匹配 '{pattern}' 的文件"

    output = "\n".join(files)
    if truncated or total > len(files):
        output += f"\n... (共 {total} 个结果，显示第 {offset+1}-{offset+len(files)} 个)"

    logger.info(f"[search_files] files mode: engine={engine}, pattern={pattern}, total={total}")
    return output


def _do_search_content(
    pattern: str, directory: str, file_glob: Optional[str],
    limit: int, offset: int, output_mode: str, context: int,
) -> str:
    """按内容搜索"""
    if _has_rg():
        result = _search_content_rg(pattern, directory, file_glob, limit, offset, output_mode, context)
        engine = "rg"
    elif _has_grep():
        result = _search_content_grep(pattern, directory, file_glob, limit, offset, output_mode, context)
        engine = "grep"
    else:
        result = _search_content_python(pattern, directory, file_glob, limit, offset, output_mode)
        engine = "python"

    if result.get("error"):
        return f"[搜索错误] {result['error']}"

    matches = result.get("matches", [])
    files = result.get("files", [])
    counts = result.get("counts", {})
    total = result.get("total_count", 0)
    truncated = result.get("truncated", False)

    output = _format_results(matches, files, counts, output_mode, total, limit, offset, truncated)

    if not matches and not files and not counts:
        return f"未在 '{directory}' 中找到匹配 '{pattern}' 的内容"

    logger.info(f"[search_files] content mode: engine={engine}, pattern={pattern}, total={total}")
    return output


search_files = build_tool(
    name="search_files",
    description=(
        "搜索文件或文件内容。优先使用 ripgrep 加速，自动回退。\n"
        "Args:\n"
        "  pattern: 搜索模式(target='files'时为文件名匹配，target='content'时为正则)\n"
        "  directory: 搜索目录，默认当前目录\n"
        "  target: 'files'按文件名 / 'content'按内容(默认files)\n"
        "  file_glob: 限定文件类型(如*.py)\n"
        "  limit: 结果数量上限(默认50)\n"
        "  offset: 跳过前N条(默认0)\n"
        "  output_mode: content/files_only/count(默认content)\n"
        "  context: 上下文行数(默认0，仅rg/grep)"
    ),
    func=_search_files_impl,
    args_schema=SearchFilesInput,
    max_result_size=DEFAULT_MAX_RESULT_SIZE_CHARS,
    is_read_only=True,
    is_concurrency_safe=True,   # 搜索无副作用，可并行
)