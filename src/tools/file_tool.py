"""文件读写工具 — DashengTool 版 + Read-Before-Edit 安全机制

迁移自 Claude Code 的 FileStateCache + validate_input 设计：
- write_file 在覆写已存在文件前，必须先 read_file（防止过时覆写）
- read_file 结果不过期（max_result_size=inf，防循环读取持久化文件）
- edit_file 同理需要先读取
"""

import os
import re

from pydantic import BaseModel, Field
from tools.tool_base import build_tool, ValidationResult, DEFAULT_MAX_RESULT_SIZE_CHARS
from tools.file_state_cache import get_file_state_cache
from tools.tmp_manager import get_tmp_file, get_tmp_dir_path

# 需要自动重定向到临时目录的文件扩展名
TEMP_EXTENSIONS = {'.py', '.sh', '.bat', '.cmd', '.ps1', '.js', '.ts', '.jsx', '.tsx',
                   '.vue', '.html', '.css', '.json', '.yaml', '.yml', '.xml', '.sql', '.log', '.txt'}


# ── 参数 Schema ──

class ReadFileInput(BaseModel):
    path: str = Field(description="文件路径")
    encoding: str = Field(default="utf-8", description="文件编码，默认 utf-8")


class WriteFileInput(BaseModel):
    path: str = Field(description="文件路径")
    content: str = Field(description="要写入的内容")


class CleanupTmpInput(BaseModel):
    """无参数工具，但 Pydantic 需要一个空 Schema"""
    pass


# ── read_file ──

def _read_file_impl(path: str, encoding: str = "utf-8") -> str:
    """核心逻辑 — 不做手动截断，交给框架
    
    改进（借鉴 Hermes）：
    - 文件不存在时，尝试在常见项目目录中搜索同名文件
    - 返回绝对路径提示，帮助 LLM 定位
    """
    try:
        # 尝试解析路径：如果是相对路径，先转绝对路径
        abs_path = os.path.abspath(path)
        
        with open(abs_path, "r", encoding=encoding, errors="replace") as f:
            content = f.read()

        # 记录到 FileStateCache（Read-Before-Edit 核心）
        cache = get_file_state_cache()
        cache.record_read(abs_path, content)

        return content
    except FileNotFoundError:
        # 文件不存在 → 给出路径提示
        hint = ""
        abs_path = os.path.abspath(path)
        
        # 如果是相对路径拼错了，提示用绝对路径
        if not os.path.isabs(path):
            hint = f"\n\n💡 提示：当前工作目录为 {os.getcwd()}，相对路径解析为 {abs_path}\n" \
                   f"如果文件在其他位置，请使用绝对路径或先用 search_files 搜索。"
        else:
            # 绝对路径也不存在 → 检查父目录是否存在
            parent = os.path.dirname(abs_path)
            if os.path.isdir(parent):
                # 父目录存在，文件名可能拼错，列出相似文件
                fname = os.path.basename(path)
                try:
                    similar = [f for f in os.listdir(parent) 
                               if fname.lower() in f.lower() or f.lower() in fname.lower()]
                    if similar:
                        hint = f"\n\n💡 父目录存在，相似文件：\n  " + "\n  ".join(similar[:5])
                except (OSError, PermissionError):
                    pass
            else:
                hint = f"\n\n💡 提示：目录 {parent} 不存在。请先用 search_files 搜索文件。"
        
        return f"[错误] 文件不存在: {path}{hint}"
    except Exception as e:
        return f"[错误] 读取失败: {e}"


read_file = build_tool(
    name="read_file",
    description=(
        "读取文件内容。支持文本文件。\n"
        "Args:\n"
        "  path: 文件路径\n"
        "  encoding: 文件编码，默认 utf-8"
    ),
    func=_read_file_impl,
    args_schema=ReadFileInput,
    max_result_size=float("inf"),   # ← 关键：永不持久化，防循环读取
    is_read_only=True,
    is_concurrency_safe=True,
)


# ── write_file ──

def _write_file_validate(**kwargs) -> ValidationResult:
    """Read-Before-Edit 验证 — 已存在文件必须先读取"""
    path = kwargs.get("path", "")
    if not path:
        return ValidationResult.deny("路径不能为空")

    # 规范化路径
    path = os.path.normpath(os.path.abspath(path))

    # 判断是否会重定向到临时文件
    ext = os.path.splitext(path)[1].lower()
    will_redirect = False
    if ext in TEMP_EXTENSIONS:
        if not os.path.isabs(path) and '/' not in path and '\\' not in path:
            dirname = os.path.dirname(path)
            if not dirname:
                will_redirect = True

    # 重定向到临时文件的不需要 Read-Before-Edit 检查
    if will_redirect:
        return ValidationResult.ok()

    # 新文件不需要检查
    if not os.path.exists(path):
        return ValidationResult.ok()

    # 已存在文件 → 检查 Read-Before-Edit
    cache = get_file_state_cache()
    allowed, reason = cache.check_write_allowed(path)
    if not allowed:
        return ValidationResult.deny(reason)

    return ValidationResult.ok()


def _write_content(path: str, content: str) -> str:
    """实际写入文件内容"""
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"成功写入 {len(content)} 字符到 {path}"


def _write_file_impl(path: str, content: str) -> str:
    """核心逻辑 — 临时文件重定向逻辑保留"""
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext in TEMP_EXTENSIONS:
            if os.path.isabs(path) or '/' in path or '\\' in path:
                dirname = os.path.dirname(path)
                if not dirname:
                    real_path = get_tmp_file(ext)
                    return _write_content(real_path, content)
            return _write_content(path, content)
        else:
            return _write_content(path, content)
    except Exception as e:
        return f"[错误] 写入失败: {e}"


write_file = build_tool(
    name="write_file",
    description=(
        "写入文件。如果文件不存在则创建，存在则覆盖。\n"
        "注意：写入临时脚本文件（.py/.sh/.bat 等）时，会自动重定向到临时缓冲目录。\n"
        "注意：覆写已存在文件前，必须先调用 read_file 读取该文件。\n"
        "Args:\n"
        "  path: 文件路径\n"
        "  content: 要写入的内容"
    ),
    func=_write_file_impl,
    args_schema=WriteFileInput,
    max_result_size=DEFAULT_MAX_RESULT_SIZE_CHARS,
    is_read_only=False,
    is_concurrency_safe=False,
    validate_input=_write_file_validate,
)


# ── cleanup_tmp_files ──

def _cleanup_tmp_impl() -> str:
    try:
        from tools.tmp_manager import cleanup_tmp_dir
        cleanup_tmp_dir(max_age_hours=0)
        tmp_dir = get_tmp_dir_path()
        return f"已清理临时目录: {tmp_dir}"
    except Exception as e:
        return f"[错误] 清理失败: {e}"


cleanup_tmp_files = build_tool(
    name="cleanup_tmp_files",
    description="清理临时缓冲目录中的所有文件。当完成所有临时脚本任务后调用。",
    func=_cleanup_tmp_impl,
    args_schema=CleanupTmpInput,
    max_result_size=DEFAULT_MAX_RESULT_SIZE_CHARS,
    is_read_only=False,
    is_concurrency_safe=True,
)