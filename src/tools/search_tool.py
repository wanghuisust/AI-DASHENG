"""文件搜索工具 — ripgrep 加速版 (DashengTool 版)

优先使用 rg（ripgrep）搜索，回退到 grep，最终回退到纯 Python os.walk。
支持分页（offset/limit）、多种输出模式、上下文行。

v2 改进（借鉴 Hermes）：
- 搜索结果返回绝对路径（避免 read_file 拼接错误）
- 当默认目录无结果时，自动扫描多个候选工作目录（工作目录发现）
- 搜索无结果时给出可用目录提示
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

# ── 工作目录发现 ──────────────────────────────────────────
# 当 directory="." 且搜不到结果时，自动扫描这些候选路径

def _get_project_roots() -> list[str]:
    """获取项目根目录列表（动态发现，不硬编码具体项目路径）

    策略（只加入看起来像代码/AI项目的目录，避免扫描无关大目录）：
    1. 当前进程 cwd
    2. 盘符根目录下有 .git 或含代码指示的目录（AI-* / code / src / project 等）
    3. 用户 HOME 下的项目目录
    """
    roots = []
    seen = set()

    def _add(path: str):
        p = os.path.normpath(path)
        if p not in seen and os.path.isdir(p):
            seen.add(p)
            roots.append(p)

    # 项目目录名关键词（只扫描看起来像代码/AI项目的）
    _PROJECT_KEYWORDS = {"ai", "code", "src", "project", "github", "git", "repo",
                          "llm", "tts", "dasheng", "claude", "hermes", "comfyui",
                          "llama", "vllm", "model", "dify", "openclaw", "torch",
                          "hugging", "qwen"}
    
    def _looks_like_project(name: str) -> bool:
        """目录名看起来像代码/AI项目"""
        lower = name.lower()
        # 含关键词
        for kw in _PROJECT_KEYWORDS:
            if kw in lower:
                return True
        # 以大写字母开头+横线（如 AI-DASHENG, AI-LLM）
        if re.match(r'^[A-Z]-', name):
            return True
        # 含 .git（精确判定）
        return False

    # 1. 当前工作目录
    _add(os.getcwd())
    # 也加入 cwd 的父目录（如果是子目录）
    parent = os.path.dirname(os.getcwd())
    if parent and os.path.isdir(parent):
        _add(parent)

    # 2. 扫描盘符根目录 — 只加入看起来像项目的
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.isdir(drive):
                try:
                    for entry in os.scandir(drive):
                        if not entry.is_dir():
                            continue
                        name = entry.name
                        # 跳过系统和隐藏目录
                        if name.startswith(".") or name.startswith("$") or name in {
                            "Windows", "Program Files", "Program Files (x86)",
                            "ProgramData", "Users", "Recovery", "Boot",
                            "Documents and Settings", "System Volume Information",
                            "msys64", "miniconda3", "conda",
                        }:
                            continue
                        if _looks_like_project(name) or os.path.isdir(os.path.join(entry.path, ".git")):
                            _add(entry.path)
                except (PermissionError, OSError):
                    pass
    else:
        home = os.path.expanduser("~")
        _add(home)
        for base in [home, "/opt", "/srv"]:
            if os.path.isdir(base):
                try:
                    for entry in os.scandir(base):
                        if not entry.is_dir() or entry.name.startswith("."):
                            continue
                        if _looks_like_project(entry.name) or os.path.isdir(os.path.join(entry.path, ".git")):
                            _add(entry.path)
                except (PermissionError, OSError):
                    pass

    # 3. 用户 HOME 下的常见项目目录
    home = os.path.expanduser("~")
    for subdir in ["projects", "code", "src", "workspace", "repos"]:
        p = os.path.join(home, subdir)
        if os.path.isdir(p):
            _add(p)

    return roots


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


def _to_absolute(path: str, base: str) -> str:
    """将路径转为绝对路径（如果还不是的话）"""
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(base, path))


def _format_results(
    matches: list[tuple[str, int, str]],
    files: list[str],
    counts: dict[str, int],
    output_mode: str,
    total_count: int,
    limit: int,
    offset: int,
    truncated: bool,
    base_dir: str = "",
) -> str:
    """将搜索结果格式化为字符串"""
    if output_mode == "files_only":
        if not files:
            return "未找到匹配的文件"
        # 转为绝对路径
        abs_files = [_to_absolute(f, base_dir) for f in files]
        lines = abs_files
        if truncated:
            lines.append(f"... (共 {total_count} 个结果，仅显示 {len(abs_files)} 个)")
        return "\n".join(lines)

    elif output_mode == "count":
        if not counts:
            return "未找到匹配的内容"
        total = sum(counts.values())
        lines = [f"{_to_absolute(p, base_dir)}: {cnt}" for p, cnt in sorted(counts.items(), key=lambda x: -x[1])]
        lines.append(f"\n共 {len(counts)} 个文件，{total} 处匹配")
        return "\n".join(lines)

    else:  # content mode
        if not matches:
            return "未找到匹配的内容"
        lines = []
        for path, lineno, content in matches:
            abs_path = _to_absolute(path, base_dir)
            display = content.strip()[:200]
            lines.append(f"{abs_path}:{lineno}: {display}")
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


# ── 多目录搜索 ──────────────────────────────────────────

def _search_single_dir(
    pattern: str, directory: str, target: str,
    file_glob: Optional[str], limit: int, offset: int,
    output_mode: str, context: int,
) -> str:
    """在单个目录中搜索，返回格式化结果（绝对路径）"""
    abs_dir = os.path.abspath(directory)

    if target == "files":
        if _has_rg():
            result = _search_files_rg(pattern, abs_dir, limit, offset)
            engine = "rg"
        else:
            result = _search_files_glob(pattern, abs_dir, limit, offset)
            engine = "glob"

        files = result.get("files", [])
        total = result.get("total_count", 0)
        truncated = result.get("truncated", False)

        if not files:
            return ""  # 空字符串表示无结果

        abs_files = [_to_absolute(f, abs_dir) for f in files]
        output = "\n".join(abs_files)
        if truncated or total > len(files):
            output += f"\n... (共 {total} 个结果，显示第 {offset+1}-{offset+len(files)} 个)"

        logger.info(f"[search_files] files mode: engine={engine}, pattern={pattern}, dir={abs_dir}, total={total}")
        return output

    else:  # content mode
        if _has_rg():
            result = _search_content_rg(pattern, abs_dir, file_glob, limit, offset, output_mode, context)
            engine = "rg"
        elif _has_grep():
            result = _search_content_grep(pattern, abs_dir, file_glob, limit, offset, output_mode, context)
            engine = "grep"
        else:
            result = _search_content_python(pattern, abs_dir, file_glob, limit, offset, output_mode)
            engine = "python"

        if result.get("error"):
            return f"[搜索错误] {result['error']}"

        matches = result.get("matches", [])
        files = result.get("files", [])
        counts = result.get("counts", {})
        total = result.get("total_count", 0)
        truncated = result.get("truncated", False)

        output = _format_results(matches, files, counts, output_mode, total, limit, offset, truncated, abs_dir)

        if not matches and not files and not counts:
            return ""  # 空字符串表示无结果

        logger.info(f"[search_files] content mode: engine={engine}, pattern={pattern}, dir={abs_dir}, total={total}")
        return output


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
    """核心逻辑 — 搜索文件或文件内容

    改进：当默认目录无结果时，自动扫描多个候选工作目录（工作目录发现），
    避免因 cwd 固定导致搜不到其他盘的项目文件。
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    try:
        abs_dir = os.path.abspath(directory)

        # 第一步：在指定目录搜索
        result = _search_single_dir(pattern, abs_dir, target, file_glob, limit, offset, output_mode, context)
        if result:
            return result

        # 第二步：指定目录无结果 → 自动扩展搜索范围
        # 只有当用户传的是默认值 "." 或相对路径时才扩展
        is_default_dir = (directory == "." or directory == os.getcwd()
                          or os.path.abspath(directory) == os.getcwd())

        if not is_default_dir:
            # 用户明确指定了目录，尊重选择
            if target == "files":
                return f"未找到匹配 '{pattern}' 的文件"
            else:
                return f"未在 '{directory}' 中找到匹配 '{pattern}' 的内容"

        # 自动扫描多个候选目录
        project_roots = _get_project_roots()
        all_results = []
        total_found = 0

        for root_dir in project_roots:
            if root_dir == abs_dir:
                continue  # 已经搜过了
            sub_result = _search_single_dir(
                pattern, root_dir, target, file_glob,
                max(limit - total_found, 5), 0, output_mode, context
            )
            if sub_result:
                all_results.append((root_dir, sub_result))
                # 估算结果数
                total_found += sub_result.count('\n') + 1
                if total_found >= limit:
                    break

        if all_results:
            # 拼接多目录结果
            parts = []
            for root_dir, sub_result in all_results:
                parts.append(f"📂 {root_dir}\n{sub_result}")
            combined = "\n\n".join(parts)
            if total_found > limit:
                combined += f"\n\n... (结果已截断，可用 directory 参数指定目录精确搜索)"
            return combined

        # 完全无结果
        # 列出可用目录帮助用户定位
        available = [d for d in project_roots if os.path.isdir(d)]
        hint = ""
        if available:
            dirs_str = "\n  ".join(available[:10])
            hint = f"\n\n💡 可用搜索目录：\n  {dirs_str}\n  使用 directory=参数 指定搜索路径"

        if target == "files":
            return f"未找到匹配 '{pattern}' 的文件{hint}"
        else:
            return f"未在默认目录中找到匹配 '{pattern}' 的内容{hint}"

    except Exception as e:
        logger.error(f"[search_files] error: {e}", exc_info=True)
        return f"[错误] 搜索失败: {e}"


search_files = build_tool(
    name="search_files",
    description=(
        "搜索文件或文件内容。优先使用 ripgrep 加速，自动回退。\n"
        "当默认目录无结果时，自动扫描本机所有项目目录。\n"
        "结果始终返回绝对路径，可直接用于 read_file。\n"
        "Args:\n"
        "  pattern: 搜索模式(target='files'时为文件名匹配，target='content'时为正则)\n"
        "  directory: 搜索目录，默认当前目录（无结果时自动扩展）\n"
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
