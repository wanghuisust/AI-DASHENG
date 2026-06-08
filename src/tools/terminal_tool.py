"""终端命令执行工具"""

from langchain_core.tools import tool


@tool
def terminal_execute(command: str, timeout: int = 60) -> str:
    """在本地终端执行 shell 命令并返回输出。

    Args:
        command: 要执行的 shell 命令
        timeout: 超时秒数，默认60

    Returns:
        命令的标准输出和标准错误
    """
    import subprocess
    import platform

    try:
        is_windows = platform.system() == "Windows"
        # Windows cmd 输出是 GBK/CP936，不能硬编码 utf-8
        enc = "gbk" if is_windows else "utf-8"
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
        output = output.strip() or "(无输出)"
        # 截断过长输出，避免消息暴增导致 API 400
        MAX_OUTPUT = 3000
        if len(output) > MAX_OUTPUT:
            output = output[:MAX_OUTPUT] + f"\n... (截断，共 {len(output)} 字符)"
        return output
    except subprocess.TimeoutExpired:
        return f"[错误] 命令超时（{timeout}秒）"
    except Exception as e:
        return f"[错误] 执行失败: {e}"
