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
        "在本地终端执行 shell 命令并返回输出。\n\n"
        "## 何时使用\n"
        "- 运行测试、构建项目、安装依赖\n"
        "- 启动/停止服务、git 操作\n"
        "- 执行需要 shell 的操作\n\n"
        "## 何时不要使用\n"
        "- ❌ 读取文件内容 → 用 read_file（有行号、分页、自动截断）\n"
        "- ❌ 搜索文件内容 → 用 search_files（基于ripgrep，秒搜）\n"
        "- ❌ 查找文件名 → 用 search_files(target='files')\n"
        "- ❌ 编辑/创建文件 → 用 write_file\n\n"
        "## Windows 环境注意\n"
        "- 默认 shell 是 cmd.exe，不是 bash/PowerShell\n"
        "- 需要 PowerShell：powershell -Command \"命令\"\n"
        "- 需要 Python：python -c \"代码\"\n"
        "- 不要用 bash 语法（&&、||、$()）\n\n"
        "Args:\n"
        "  command: 要执行的 shell 命令\n"
        "  timeout: 超时秒数，默认180"
    ),
    func=_terminal_execute_impl,
    args_schema=TerminalExecuteInput,
    max_result_size=DEFAULT_MAX_RESULT_SIZE_CHARS,
    is_read_only=False,
    is_concurrency_safe=False,
)