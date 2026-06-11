"""磁盘分析工具 — DashengTool 版

解决 terminal_execute 扫描大文件超时的问题。
用 Python 原生 os.walk + os.path.getsize，不受 shell 限制。
通过 _progress_counter 支持进度回调，让超时机制知道工具还在跑。
"""

import os
import threading
from pydantic import BaseModel, Field
from .tool_base import build_tool, DEFAULT_MAX_RESULT_SIZE_CHARS


class DiskAnalyzeInput(BaseModel):
    path: str = Field(description="要分析的目录路径，如 C:\\ 或 D:\\projects")
    min_size_mb: int = Field(default=100, description="最小文件大小（MB），只返回大于此值的文件，默认100")
    top_n: int = Field(default=20, description="返回最大的N个文件，默认20")


# 模块级进度计数器（graph.py 的超时机制读取此值判断是否有进展）
_progress_counter = [0]
_progress_lock = threading.Lock()


def get_progress_value() -> int:
    """供外部超时机制读取当前进展值"""
    with _progress_lock:
        return _progress_counter[0]


def reset_progress():
    """每次工具调用前重置"""
    with _progress_lock:
        _progress_counter[0] = 0


def _disk_analyze_impl(path: str, min_size_mb: int = 100, top_n: int = 20) -> str:
    """扫描目录下的大文件"""
    reset_progress()

    if not os.path.isdir(path):
        return f"[错误] 路径不存在或不是目录: {path}"

    min_size_bytes = min_size_mb * 1024 * 1024
    big_files = []
    dir_count = 0

    for root, dirs, files in os.walk(path):
        # 跳过无权限/系统目录
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '$Recycle.Bin'
                   and d != 'System Volume Information' and d != 'Windows']

        # 更新进度计数器（每遍历一个目录+1，超时机制据此判断是否还在跑）
        dir_count += 1
        with _progress_lock:
            _progress_counter[0] = dir_count

        for f in files:
            fpath = os.path.join(root, f)
            try:
                size = os.path.getsize(fpath)
                if size >= min_size_bytes:
                    big_files.append((fpath, size))
            except (OSError, PermissionError):
                continue

        # 每1000个文件预排序裁剪，减少内存
        if len(big_files) > top_n * 5:
            big_files.sort(key=lambda x: -x[1])
            big_files = big_files[:top_n * 2]

    # 最终排序
    big_files.sort(key=lambda x: -x[1])
    top_files = big_files[:top_n]

    if not top_files:
        return f"在 {path} 中未找到大于 {min_size_mb}MB 的文件。\n（已扫描 {dir_count} 个目录）"

    lines = [f"## {path} 大文件分析 (>{min_size_mb}MB, Top {top_n})\n"]
    total_size = 0
    for i, (fpath, size) in enumerate(top_files, 1):
        size_mb = size / (1024 * 1024)
        size_gb = size / (1024 * 1024 * 1024)
        if size_gb >= 1:
            lines.append(f"{i}. {fpath} — {size_gb:.1f} GB")
        else:
            lines.append(f"{i}. {fpath} — {size_mb:.0f} MB")
        total_size += size

    total_gb = total_size / (1024 * 1024 * 1024)
    lines.append(f"\n共 {len(top_files)} 个文件，总计 {total_gb:.1f} GB")
    lines.append(f"（已扫描 {dir_count} 个目录）")

    # 目录级汇总
    dir_sizes = {}
    for fpath, size in big_files:
        parts = fpath.replace(path, "").split(os.sep)
        if len(parts) > 1:
            dir_key = os.path.join(path, parts[1] if parts[0] == '' else parts[0])
            dir_sizes[dir_key] = dir_sizes.get(dir_key, 0) + size

    if dir_sizes:
        lines.append("\n### 按目录汇总")
        for d, s in sorted(dir_sizes.items(), key=lambda x: -x[1])[:10]:
            s_gb = s / (1024 * 1024 * 1024)
            lines.append(f"- {d}: {s_gb:.1f} GB")

    return "\n".join(lines)


disk_analyze = build_tool(
    name="disk_analyze",
    description=(
        "扫描目录下的大文件，按大小排序返回。\n\n"
        "## 何时使用\n"
        "- 用户问\"C盘/D盘有哪些大文件\"\n"
        "- 需要清理磁盘空间\n"
        "- 查找占用空间的文件\n\n"
        "## 为什么不用 terminal_execute\n"
        "- terminal_execute 用 PowerShell/dir 扫描大目录容易超时\n"
        "- disk_analyze 用 Python os.walk 原生遍历，支持进度回调，不超时\n\n"
        "## 建议\n"
        "- 扫描整个盘符(如 C:\\) 较慢，建议扫描子目录(如 C:\\Users)\n"
        "- 可以同时调用多次扫描不同目录\n\n"
        "Args:\n"
        "  path: 要分析的目录路径\n"
        "  min_size_mb: 最小文件大小(MB)，默认100\n"
        "  top_n: 返回最大的N个文件，默认20"
    ),
    func=_disk_analyze_impl,
    args_schema=DiskAnalyzeInput,
    max_result_size=DEFAULT_MAX_RESULT_SIZE_CHARS,
    is_read_only=True,
    is_concurrency_safe=True,
)
