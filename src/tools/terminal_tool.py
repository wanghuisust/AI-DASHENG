"""终端命令执行工具 — DashengTool 版"""

import subprocess
import platform

from pydantic import BaseModel, Field
from tools.tool_base import build_tool, ValidationResult, DEFAULT_MAX_RESULT_SIZE_CHARS


class TerminalExecuteInput(BaseModel):
    """terminal_execute 参数 Schema"""
    command: str = Field(description="要执行的 shell 命令")
    timeout: int = Field(default=180, description="超时秒数，默认180")


def _terminal_execute_impl(command: str, timeout: int = 180) -> str:
    """核心逻辑 — 不做手动截断，交给框架 process_tool_result"""
    is_windows = platform.system() == "Windows"
    enc = "gbk" if is_windows else "utf-8"

    try:
        result = subprocess.run(
            command if is_windows else ["/bin/bash", "-c", command],
            shell=is_windows,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding=enc,
            errors="replace",
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr] {result.stderr}"
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output.strip() or "(无输出)"
    except subprocess.TimeoutExpired:
        return f"[错误] 命令超时（{timeout}秒）"
    except Exception as e:
        return f"[错误] 执行失败: {e}"


terminal_execute = build_tool(
    name="terminal_execute",
    description=(
        "在本地终端执行 shell 命令并返回输出。\n"
        "Args:\n"
        "  command: 要执行的 shell 命令\n"
        "  timeout: 超时秒数，默认180（与 Hermes 对齐）"
    ),
    func=_terminal_execute_impl,
    args_schema=TerminalExecuteInput,
    max_result_size=DEFAULT_MAX_RESULT_SIZE_CHARS,  # 框架级截断替代手动 3000
    is_read_only=False,
    is_concurrency_safe=False,  # shell 命令可能有副作用，不可并行
)