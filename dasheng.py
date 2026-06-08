#!/usr/bin/env python3
"""DASHENG CLI — 大圣命令行工具

用法:
    python dasheng.py install   # 安装依赖
    python dasheng.py setup     # 交互式配置向导
    python dasheng.py start     # 启动服务
    python dasheng.py stop      # 停止服务
    python dasheng.py restart   # 重启服务
    python dasheng.py status    # 查看状态
    python dasheng.py chat      # 直接对话测试
"""

import os
import sys
import json
import time
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

import click

# ── 自动使用 .venv Python 重启 ──────────────────────────────────────────────
# 如果当前 Python 不是 .venv 的，自动用 .venv 重启自身（确保依赖一致）
_VENV_PY = Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe"
if (
    sys.platform == "win32"
    and _VENV_PY.exists()
    and Path(sys.executable).resolve() != _VENV_PY.resolve()
):
    os.execv(str(_VENV_PY), [str(_VENV_PY)] + sys.argv)

# Windows UTF-8 输出支持
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── 项目路径常量 ──────────────────────────────────────────────────────────────

# 支持 DASHENG_HOME 环境变量指定项目目录（pip install 后全局可用）
# 默认用 __file__ 所在目录（开发模式下直接在项目根目录运行）
PROJECT_DIR = Path(os.environ.get("DASHENG_HOME", "")).resolve() if os.environ.get("DASHENG_HOME") else Path(__file__).resolve().parent
SRC_DIR = PROJECT_DIR / "src"
VENV_DIR = PROJECT_DIR / ".venv"
ENV_FILE = PROJECT_DIR / ".env"
VBS_FILE = PROJECT_DIR / "start_all_hidden.vbs"

# Python 解释器路径（统一使用 .venv）
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
VENV_PYTHONW = VENV_DIR / "Scripts" / "pythonw.exe"

# Conda python 仅作为 fallback（如果 .venv 不可用）
CONDA_PYTHON = Path("G:/miniconda3/python.exe")
CONDA_PYTHONW = Path("G:/miniconda3/pythonw.exe")


def get_gateway_python() -> Path:
    """获取 Gateway 使用的 pythonw.exe（优先 .venv）"""
    if VENV_PYTHONW.exists():
        return VENV_PYTHONW
    if CONDA_PYTHONW.exists():
        return CONDA_PYTHONW
    return Path("pythonw.exe")


def get_gateway_python_plain() -> Path:
    """获取 Gateway 使用的 python.exe（优先 .venv，用于安装依赖）"""
    if VENV_PYTHON.exists():
        return VENV_PYTHON
    if CONDA_PYTHON.exists():
        return CONDA_PYTHON
    return Path("python.exe")

# 服务入口
AGENT_API_ENTRY = SRC_DIR / "agent_api.py"
WEB_SERVER_ENTRY = SRC_DIR / "web_server.py"
GATEWAY_ENTRY = SRC_DIR / "gateway" / "server.py"

# 服务端口
AGENT_API_PORT = 8900
WEB_SERVER_PORT = 7860
GATEWAY_PORT = 9090


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def echo_info(msg: str):
    """蓝色信息"""
    click.echo(click.style(f"ℹ {msg}", fg="cyan"))


def echo_ok(msg: str):
    """绿色成功"""
    click.echo(click.style(f"✔ {msg}", fg="green"))


def echo_warn(msg: str):
    """黄色警告"""
    click.echo(click.style(f"⚠ {msg}", fg="yellow"))


def echo_error(msg: str):
    """红色错误"""
    click.echo(click.style(f"✖ {msg}", fg="red"))


def load_env() -> dict:
    """读取 .env 文件为字典（不注入环境变量）"""
    env = {}
    if not ENV_FILE.exists():
        return env
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip()
    return env


def save_env(env: dict):
    """保存字典到 .env 文件"""
    lines = []
    # 保留注释和空行
    if ENV_FILE.exists():
        existing_keys = set()
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    lines.append(line.rstrip("\n"))
                elif "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    existing_keys.add(key)
                    if key in env:
                        lines.append(f"{key}={env[key]}")
                    else:
                        lines.append(line.rstrip("\n"))
        # 添加新增的 key
        for key, val in env.items():
            if key not in existing_keys:
                lines.append(f"{key}={val}")
    else:
        lines.append("# DASHENG 配置文件 — 由 dasheng.py setup 生成")
        lines.append("")
        for key, val in env.items():
            lines.append(f"{key}={val}")

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def is_venv_ready() -> bool:
    """检查 .venv 是否存在且可用"""
    return VENV_PYTHON.exists() and VENV_PYTHONW.exists()


def is_conda_ready() -> bool:
    """检查 conda python 是否存在"""
    return CONDA_PYTHON.exists()


def check_port(port: int) -> bool:
    """检查端口是否被占用"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def find_pythonw_processes() -> list:
    """查找所有 pythonw.exe 进程"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10
        )
        procs = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line or "INFO:" in line:
                continue
            # CSV 格式: "pythonw.exe","12345","Console","1","12,345 K"
            parts = line.split('","')
            if len(parts) >= 2:
                name = parts[0].strip('"')
                pid = parts[1].strip('"')
                if name.lower() == "pythonw.exe" and pid.isdigit():
                    procs.append(int(pid))
        return procs
    except Exception:
        return []


def find_python_processes() -> list:
    """查找所有 python.exe / pythonw.exe 进程"""
    procs = []
    for imagename in ["python.exe", "pythonw.exe"]:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {imagename}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if not line or "INFO:" in line:
                    continue
                parts = line.split('","')
                if len(parts) >= 2:
                    pid = parts[1].strip('"')
                    if pid.isdigit():
                        procs.append(int(pid))
        except Exception:
            pass
    return procs


def generate_vbs(env: dict = None) -> str:
    """生成 VBS 启动脚本（三进程：Agent API + WebServer + Gateway）"""
    if env is None:
        env = load_env()

    qq_app_id = env.get("QQ_APP_ID", "")
    qq_app_secret = env.get("QQ_APP_SECRET", "")
    gateway_port = env.get("GATEWAY_PORT", str(GATEWAY_PORT))
    enable_tools = env.get("ENABLE_TOOLS", "true")

    vbs_lines = [
        'Set ws = CreateObject("WScript.Shell")',
        '',
        f"\' Agent API ({AGENT_API_PORT})",
        f'ws.Environment("Process").Item("ENABLE_TOOLS") = "{enable_tools}"',
    ]

    # Agent API 用 .venv pythonw（必须先启动，Gateway/WebServer 依赖它）
    if VENV_PYTHONW.exists():
        vbs_lines.append(
            f'ws.Run "{VENV_PYTHONW} {AGENT_API_ENTRY}", 0, False'
        )
    else:
        vbs_lines.append(
            f'ws.Run "pythonw.exe {AGENT_API_ENTRY}", 0, False'
        )

    # 等待 Agent API 就绪
    vbs_lines.append('WScript.Sleep 3000')
    vbs_lines.append('')

    vbs_lines.append(f"\' WebServer ({WEB_SERVER_PORT})")
    if VENV_PYTHONW.exists():
        vbs_lines.append(
            f'ws.Run "{VENV_PYTHONW} {WEB_SERVER_ENTRY}", 0, False'
        )
    else:
        vbs_lines.append(
            f'ws.Run "pythonw.exe {WEB_SERVER_ENTRY}", 0, False'
        )

    vbs_lines.append('')
    vbs_lines.append(f"\' Gateway ({gateway_port})")

    # Gateway 环境变量
    if gateway_port:
        vbs_lines.append(
            f'ws.Environment("Process").Item("GATEWAY_PORT") = "{gateway_port}"'
        )
    if qq_app_id:
        vbs_lines.append(
            f'ws.Environment("Process").Item("QQ_APP_ID") = "{qq_app_id}"'
        )
    if qq_app_secret:
        vbs_lines.append(
            f'ws.Environment("Process").Item("QQ_APP_SECRET") = "{qq_app_secret}"'
        )

    # Gateway 用 pythonw（优先 .venv，fallback conda）
    gw_pythonw = get_gateway_python()
    vbs_lines.append(
        f'ws.Run "{gw_pythonw} {GATEWAY_ENTRY}", 0, False'
    )

    return "\n".join(vbs_lines) + "\n"


# ── CLI 命令 ──────────────────────────────────────────────────────────────────

@click.group()
@click.version_option(version="1.0.0", prog_name="DASHENG")
def cli():
    """[DASHENG] 本地 AI Agent 命令行工具"""
    pass


# ── install ───────────────────────────────────────────────────────────────────

@cli.command()
def install():
    """安装项目依赖（创建 venv + pip install）"""

    echo_info("开始安装 DASHENG 依赖...")

    # 1. 检查/创建 .venv
    if is_venv_ready():
        echo_ok(f".venv 已存在: {VENV_DIR}")
    else:
        echo_info(f"创建虚拟环境: {VENV_DIR}")
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", str(VENV_DIR)],
                check=True, timeout=120
            )
            echo_ok("虚拟环境创建成功")
        except subprocess.CalledProcessError as e:
            echo_error(f"创建虚拟环境失败: {e}")
            sys.exit(1)
        except FileNotFoundError:
            echo_error("找不到 python，请确保 Python 已安装并在 PATH 中")
            sys.exit(1)

    # 2. pip install
    pip_exe = VENV_DIR / "Scripts" / "pip.exe"
    if not pip_exe.exists():
        echo_error(f"pip 不存在: {pip_exe}")
        sys.exit(1)

    req_file = PROJECT_DIR / "requirements.txt"
    if not req_file.exists():
        echo_warn("requirements.txt 不存在，跳过依赖安装")
    else:
        echo_info("安装 Python 依赖（可能需要几分钟）...")
        result = subprocess.run(
            [str(VENV_PYTHON), "-m", "pip", "install", "-r", str(req_file)],
            timeout=600,
            capture_output=True, text=True
        )
        if result.returncode != 0:
            echo_error(f"依赖安装失败 (code={result.returncode}):\n{result.stderr[-500:]}\n{result.stdout[-500:]}")
            sys.exit(1)
        echo_ok("Python 依赖安装完成")

    # 3. 确保 click 已安装（CLI 自身依赖）
    echo_info("确保 click 已安装...")
    subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", "-q", "click"],
        capture_output=True, timeout=60
    )

    # 3.5 确保 qrcode 已安装（微信扫码绑定需要）
    echo_info("确保 qrcode 已安装（微信扫码绑定需要）...")
    subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", "-q", "qrcode"],
        capture_output=True, timeout=60
    )

    # 4. 确保 websocket-client 已安装（Gateway 依赖，装到 .venv）
    echo_info("确保 websocket-client 已安装...")
    subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", "-q", "websocket-client"],
        capture_output=True, timeout=60
    )

    echo_ok("所有依赖安装完成")

    # 5. 创建必要目录
    data_dir = PROJECT_DIR / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "skills").mkdir(exist_ok=True)

    # 6. 可编辑安装 → 注册 dasheng 全局命令
    echo_info("注册 dasheng 全局命令...")
    result = subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", "-e", str(PROJECT_DIR), "-q"],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode == 0:
        echo_ok("dasheng 命令已注册（在 .venv 中可用: dasheng --help）")
    else:
        echo_warn(f"全局命令注册失败（不影响使用，仍可用 python dasheng.py）: {result.stderr[-200:]}")

    # 7. 设置 DASHENG_HOME 环境变量（确保任何位置都能找到项目）
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "DASHENG_HOME", 0, winreg.REG_SZ, str(PROJECT_DIR))
        winreg.CloseKey(key)
        # 通知系统刷新环境变量
        subprocess.run(["cmd", "/c", "echo", "setenv"], capture_output=True, timeout=5)
        echo_ok(f"环境变量 DASHENG_HOME = {PROJECT_DIR}")
    except Exception as e:
        echo_warn(f"设置 DASHENG_HOME 失败: {e}（可手动设置）")

    echo_ok("安装完成！下一步: dasheng setup")


# ── 微信扫码辅助 ──────────────────────────────────────────────────────────

def _do_wechat_qr_login(env: dict, cred_store):
    """在 setup 阶段执行微信扫码绑定（也可跳过）"""
    from src.gateway.wechat_adapter import qr_login

    do_scan = click.prompt(
        "是否现在扫码绑定微信？(y=扫码 / s=跳过稍后绑定)",
        type=str, default="y"
    )

    if do_scan.lower() in ("s", "skip", "跳过"):
        echo_info("已跳过微信扫码，Gateway 启动时将自动触发扫码登录")
        env["ILINK_BOT_TOKEN"] = ""
        env["ILINK_ACCOUNT_ID"] = ""
        click.echo("")
        return

    # 执行扫码登录
    echo_info("正在获取微信二维码...")
    echo_info("请打开微信扫一扫，扫描下方二维码")
    click.echo("")

    import asyncio
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(qr_login(str(PROJECT_DIR / "data")))
        loop.close()
    except Exception as e:
        echo_error(f"扫码登录异常: {e}")
        result = None

    if result:
        env["ILINK_BOT_TOKEN"] = result.get("token", "")
        env["ILINK_ACCOUNT_ID"] = result.get("account_id", "")
        echo_ok(f"微信扫码绑定成功！account={result.get('account_id', '')[:8]}...")
    else:
        echo_warn("微信扫码未完成，Gateway 启动时将重试")
        env["ILINK_BOT_TOKEN"] = ""
        env["ILINK_ACCOUNT_ID"] = ""

    click.echo("")


# ── setup ─────────────────────────────────────────────────────────────────────

@cli.command()
def setup():
    """交互式配置向导（QQ / 微信 / LLM API）"""

    echo_info("🧌 DASHENG 配置向导")
    click.echo("=" * 50)

    env = load_env()

    # ── 1. LLM 配置 ──────────────────────────────────────────────────────
    click.echo("")
    echo_info("【第一步】配置 LLM 大模型 API")
    click.echo("")

    # 选择提供商
    providers = {
        "1": ("OpenAI 官方", "https://api.openai.com/v1", "gpt-4o-mini"),
        "2": ("Claude / Anthropic", "https://api.anthropic.com/v1", "claude-sonnet-4-20250514"),
        "3": ("Qwen / 通义千问（阿里云）", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
        "4": ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
        "5": ("智谱 GLM（ChatGLM）", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
        "6": ("月之暗面 Moonshot", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
        "7": ("小米 MiLM", "https://api.milongdexiangce.com/v1", "MiLM-6B"),
        "8": ("硅基流动 SiliconFlow", "https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct"),
        "9": ("Agnes AI", "https://apihub.agnes-ai.com/v1", "agnes-2.0-flash"),
        "10": ("自定义", "", ""),
    }

    click.echo("请选择 LLM 提供商:")
    for k, (name, _, _) in providers.items():
        click.echo(f"  {k}. {name}")
    click.echo("  0. 跳过（稍后在 .env 文件中手动配置）")

    current_provider_hint = ""
    current_base_url = env.get("OPENAI_BASE_URL", "")
    if current_base_url:
        for k, (name, url, _) in providers.items():
            if url and url in current_base_url:
                current_provider_hint = f" (当前: {name})"
                break

    choice = click.prompt(
        f"请输入编号{current_provider_hint}",
        type=str, default="1"
    )

    # 跳过 LLM 配置
    if choice == "0":
        echo_warn("已跳过 LLM 配置")
        echo_info("稍后可在项目根目录的 .env 文件中配置以下项：")
        click.echo("  OPENAI_API_KEY=你的API密钥")
        click.echo("  OPENAI_BASE_URL=https://api.openai.com/v1")
        click.echo("  MODEL_NAME=gpt-4o-mini")
        click.echo("  MODEL_CONTEXT_LENGTH=128000")
        # 保留已有配置不覆盖
        for key in ["OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL_NAME", "MODEL_CONTEXT_LENGTH"]:
            if key not in env:
                env[key] = ""
    elif choice in providers and choice != "10":
        _, default_url, default_model = providers[choice]
    else:
        default_url = ""
        default_model = ""

    if choice != "0":
        # API Key
        current_key = env.get("OPENAI_API_KEY", "")
        key_hint = f" (当前: {current_key[:8]}...)" if current_key else ""
        api_key = click.prompt(
            f"请输入 API Key{key_hint}",
            type=str, default=current_key or "", show_default=False
        )
        if not api_key:
            echo_warn("未设置 API Key，LLM 调用将失败")
        env["OPENAI_API_KEY"] = api_key

        # Base URL
        base_url = click.prompt(
            "请输入 API Base URL",
            type=str, default=current_base_url or default_url or "https://api.openai.com/v1"
        )
        env["OPENAI_BASE_URL"] = base_url

        # Model
        current_model = env.get("MODEL_NAME", "")
        model = click.prompt(
            "请输入模型名称",
            type=str, default=current_model or default_model or "gpt-4o-mini"
        )
        env["MODEL_NAME"] = model

        # 上下文长度：自定义模型必须设置，内置模型有默认值
        context_defaults = {
            "1": 128000,   # OpenAI gpt-4o-mini
            "2": 200000,   # Claude
            "3": 131072,   # Qwen qwen-plus
            "4": 65536,    # DeepSeek
            "5": 128000,   # GLM-4
            "6": 8192,     # Moonshot v1-8k
            "7": 32768,    # 小米 MiLM
            "8": 32768,    # SiliconFlow
            "9": 128000,   # Agnes
        }
        current_ctx = env.get("MODEL_CONTEXT_LENGTH", "")
        if choice == "10":
            # 自定义：必须输入
            ctx_len = click.prompt(
                "请输入模型上下文长度（token 数）",
                type=int, default=int(current_ctx) if current_ctx else 32768
            )
        else:
            ctx_len = context_defaults.get(choice, 32768)
            if current_ctx:
                ctx_len = int(current_ctx)
        env["MODEL_CONTEXT_LENGTH"] = str(ctx_len)

        echo_ok(f"LLM 配置: {model} @ {base_url} (上下文: {ctx_len} tokens, 压缩阈值: {ctx_len // 2})")

    # ── 2. QQ Bot 配置 ───────────────────────────────────────────────────
    click.echo("")
    echo_info("【第二步】配置 QQ Bot（可选，按 Enter 跳过）")
    click.echo("")

    current_qq_id = env.get("QQ_APP_ID", "")
    current_qq_secret = env.get("QQ_APP_SECRET", "")

    qq_app_id = click.prompt(
        f"QQ App ID{f' (当前: {current_qq_id})' if current_qq_id else ' (在 q.qq.com 创建机器人获取)'}",
        type=str, default=current_qq_id, show_default=False
    )
    env["QQ_APP_ID"] = qq_app_id

    if qq_app_id:
        qq_app_secret = click.prompt(
            f"QQ App Secret{f' (当前: {current_qq_secret[:6]}...)' if current_qq_secret else ''}",
            type=str, default=current_qq_secret, show_default=False
        )
        env["QQ_APP_SECRET"] = qq_app_secret

        qq_sandbox = click.prompt(
            "是否沙箱环境？(y/n)",
            type=str, default="n"
        )
        env["QQ_IS_SANDBOX"] = "true" if qq_sandbox.lower() in ("y", "yes", "1") else "false"

        echo_ok("QQ Bot 配置完成")
    else:
        echo_info("跳过 QQ 配置")

    # ── 3. 微信配置 ──────────────────────────────────────────────────────
    click.echo("")
    echo_info("【第三步】配置微信接入（可选，按 Enter 跳过）")
    click.echo("")

    current_wechat = env.get("WECHAT_ENABLED", "false")
    wechat_enabled = click.prompt(
        "是否启用微信接入？(y/n)",
        type=str, default="y" if current_wechat == "true" else "n"
    )

    if wechat_enabled.lower() in ("y", "yes", "1"):
        env["WECHAT_ENABLED"] = "true"
        click.echo("")
        echo_info("微信接入使用 iLink Bot API（微信官方开放协议）")

        # 检查依赖
        try:
            import aiohttp
            import cryptography
        except ImportError as e:
            echo_error(f"微信适配器依赖缺失: {e}")
            echo_info("正在自动安装缺失依赖...")
            subprocess.run(
                [str(VENV_PYTHON), "-m", "pip", "install", "-q", "aiohttp", "cryptography"],
                capture_output=True, timeout=120
            )
            # 重试导入
            try:
                import aiohttp
                import cryptography
            except ImportError as e2:
                echo_error(f"依赖安装失败: {e2}")
                echo_info("请手动安装: pip install aiohttp cryptography")
                env["WECHAT_ENABLED"] = "false"
                click.echo("")
                click.echo("")
                return  # 跳过微信配置

        # 确保 qrcode 可用（终端显示二维码）
        try:
            import qrcode as _test_qr
        except ImportError:
            echo_info("正在安装 qrcode（终端二维码显示需要）...")
            subprocess.run(
                [str(VENV_PYTHON), "-m", "pip", "install", "-q", "qrcode"],
                capture_output=True, timeout=60
            )

        # 检查是否已有有效凭证
        from src.gateway.wechat_adapter import CredentialStore
        cred_store = CredentialStore(str(PROJECT_DIR / "data"))
        active_creds = cred_store.load_active()

        if active_creds:
            echo_ok(f"已有微信凭证: account={active_creds.get('account_id', '')[:8]}...")
            rebind = click.prompt("是否重新扫码绑定？(y/n)", type=str, default="n")
            if rebind.lower() not in ("y", "yes"):
                # 保留现有凭证，写入 .env
                env["ILINK_BOT_TOKEN"] = active_creds.get("token", "")
                env["ILINK_ACCOUNT_ID"] = active_creds.get("account_id", "")
                echo_ok("保留现有微信绑定")
                click.echo("")
                click.echo("")
            else:
                # 清除旧凭证，重新扫码
                _do_wechat_qr_login(env, cred_store)
        else:
            # 首次绑定 — 直接扫码
            _do_wechat_qr_login(env, cred_store)
    else:
        env["WECHAT_ENABLED"] = "false"
        echo_info("跳过微信配置")

    # ── 4. 其他配置 ──────────────────────────────────────────────────────
    click.echo("")
    echo_info("【第四步】其他配置")
    click.echo("")

    current_gateway_port = env.get("GATEWAY_PORT", str(GATEWAY_PORT))
    gateway_port = click.prompt(
        "Gateway 端口",
        type=str, default=current_gateway_port
    )
    env["GATEWAY_PORT"] = gateway_port

    current_enable_tools = env.get("ENABLE_TOOLS", "true")
    enable_tools = click.prompt(
        "启用工具调用？(y/n)",
        type=str, default="y" if current_enable_tools == "true" else "n"
    )
    env["ENABLE_TOOLS"] = "true" if enable_tools.lower() in ("y", "yes", "1") else "false"

    # ── 5. 保存 ──────────────────────────────────────────────────────────
    click.echo("")
    save_env(env)
    echo_ok(f"配置已保存到 {ENV_FILE}")

    # 生成 VBS
    vbs_content = generate_vbs(env)
    with open(VBS_FILE, "w", encoding="utf-8") as f:
        f.write(vbs_content)
    echo_ok(f"启动脚本已生成: {VBS_FILE}")

    click.echo("")
    echo_ok("配置完成！运行 `python dasheng.py start` 启动服务")


# ── start ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--web-only", is_flag=True, help="仅启动 WebServer")
@click.option("--gateway-only", is_flag=True, help="仅启动 Gateway")
@click.option("--agent-only", is_flag=True, help="仅启动 Agent API")
def start(web_only, gateway_only, agent_only):
    """启动 DASHENG 服务"""

    echo_info("启动 DASHENG 服务...")

    # 检查 .env
    if not ENV_FILE.exists():
        echo_warn(".env 不存在，请先运行 setup")
        env = {}
    else:
        env = load_env()

    # 检查 venv
    if not web_only and not gateway_only and not agent_only:
        if not is_venv_ready():
            echo_error(".venv 未就绪，请先运行 install")
            sys.exit(1)

    # 检查端口占用
    if not gateway_only and not web_only and check_port(AGENT_API_PORT):
        echo_warn(f"端口 {AGENT_API_PORT} 已被占用，Agent API 可能已在运行")
    if not gateway_only and not agent_only and check_port(WEB_SERVER_PORT):
        echo_warn(f"端口 {WEB_SERVER_PORT} 已被占用，WebServer 可能已在运行")
    if not web_only and not agent_only and check_port(int(env.get("GATEWAY_PORT", GATEWAY_PORT))):
        echo_warn(f"端口 {env.get('GATEWAY_PORT', GATEWAY_PORT)} 已被占用，Gateway 可能已在运行")

    # 生成 VBS
    vbs_content = generate_vbs(env)
    with open(VBS_FILE, "w", encoding="utf-8") as f:
        f.write(vbs_content)

    def _start_process(name, entry, pythonw_path=None):
        """启动单个进程"""
        if pythonw_path is None:
            pythonw_path = VENV_PYTHONW if VENV_PYTHONW.exists() else Path("pythonw.exe")
        cmd = f'"{pythonw_path}" "{entry}"'
        try:
            subprocess.Popen(cmd, shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
            echo_ok(f"{name} 启动命令已发送")
        except Exception as e:
            echo_error(f"启动 {name} 失败: {e}")

    if agent_only:
        echo_info("仅启动 Agent API...")
        os.environ["ENABLE_TOOLS"] = env.get("ENABLE_TOOLS", "true")
        _start_process("Agent API", AGENT_API_ENTRY)

    elif web_only:
        echo_info("仅启动 WebServer...")
        _start_process("WebServer", WEB_SERVER_ENTRY)

    elif gateway_only:
        echo_info("仅启动 Gateway...")
        gw_pythonw = get_gateway_python()
        for key in ["GATEWAY_PORT", "QQ_APP_ID", "QQ_APP_SECRET", "QQ_IS_SANDBOX",
                     "WECHAT_ENABLED", "ILINK_BOT_TOKEN", "ILINK_ACCOUNT_ID"]:
            if key in env:
                os.environ[key] = env[key]
        _start_process("Gateway", GATEWAY_ENTRY, gw_pythonw)

    else:
        # 启动全部 — 使用 VBS
        echo_info("通过 VBS 启动全部服务（Agent API + WebServer + Gateway）...")

        try:
            subprocess.Popen(f'wscript.exe "{VBS_FILE}"', shell=True)
            echo_ok("VBS 启动脚本已执行")
        except Exception as e:
            echo_warn(f"VBS 启动失败: {e}，尝试直接启动...")
            # Agent API（先启动）
            os.environ["ENABLE_TOOLS"] = env.get("ENABLE_TOOLS", "true")
            _start_process("Agent API", AGENT_API_ENTRY)
            time.sleep(3)
            # WebServer
            _start_process("WebServer", WEB_SERVER_ENTRY)
            # Gateway
            gw_pythonw = get_gateway_python()
            for key in ["GATEWAY_PORT", "QQ_APP_ID", "QQ_APP_SECRET", "QQ_IS_SANDBOX",
                         "WECHAT_ENABLED", "ILINK_BOT_TOKEN", "ILINK_ACCOUNT_ID"]:
                if key in env:
                    os.environ[key] = env[key]
            _start_process("Gateway", GATEWAY_ENTRY, gw_pythonw)

    # 等待服务就绪
    click.echo("")
    echo_info("等待服务启动...")

    if not gateway_only and not web_only:
        for i in range(20):
            if check_port(AGENT_API_PORT):
                echo_ok(f"Agent API 就绪 → http://127.0.0.1:{AGENT_API_PORT}")
                break
            time.sleep(1)
        else:
            echo_warn("Agent API 未在 20s 内就绪，请检查日志")

    if not gateway_only and not agent_only:
        for i in range(15):
            if check_port(WEB_SERVER_PORT):
                echo_ok(f"WebServer 就绪 → http://127.0.0.1:{WEB_SERVER_PORT}")
                break
            time.sleep(1)
        else:
            echo_warn("WebServer 未在 15s 内就绪，请检查日志")

    if not web_only and not agent_only:
        gw_port = int(env.get("GATEWAY_PORT", GATEWAY_PORT))
        for i in range(15):
            if check_port(gw_port):
                echo_ok(f"Gateway 就绪 → http://127.0.0.1:{gw_port}/health")
                break
            time.sleep(1)
        else:
            echo_warn("Gateway 未在 15s 内就绪，请检查日志")

    click.echo("")
    echo_info("提示: 使用 `python dasheng.py status` 查看服务状态")
    echo_info("提示: 使用 `python dasheng.py stop` 停止服务")


# ── stop ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--force", is_flag=True, help="强制终止所有 python/pythonw 进程")
def stop(force):
    """停止 DASHENG 服务"""

    echo_info("停止 DASHENG 服务...")

    if force:
        # 强制模式：终止所有 pythonw.exe
        pids = find_pythonw_processes()
        if not pids:
            echo_info("没有运行中的 pythonw.exe 进程")
            return

        echo_warn(f"将终止 {len(pids)} 个 pythonw.exe 进程: {pids}")
        if not click.confirm("确认终止？"):
            echo_info("已取消")
            return

        for pid in pids:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=5)
                echo_ok(f"已终止 PID {pid}")
            except Exception as e:
                echo_error(f"终止 PID {pid} 失败: {e}")
        return

    # 正常模式：按端口查找并终止
    stopped = False

    # 检查 Agent API 端口
    if check_port(AGENT_API_PORT):
        echo_info(f"端口 {AGENT_API_PORT} 被占用，尝试停止 Agent API...")
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.split("\n"):
                if f":{AGENT_API_PORT}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1]
                    if pid.isdigit():
                        subprocess.run(["taskkill", "/PID", pid, "/F"],
                                       capture_output=True, timeout=5)
                        echo_ok(f"Agent API 已停止 (PID {pid})")
                        stopped = True
        except Exception as e:
            echo_warn(f"自动停止失败: {e}")

    # 检查 WebServer 端口
    if check_port(WEB_SERVER_PORT):
        echo_info(f"端口 {WEB_SERVER_PORT} 被占用，尝试停止 WebServer...")
        # 查找占用端口的进程
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.split("\n"):
                if f":{WEB_SERVER_PORT}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1]
                    if pid.isdigit():
                        subprocess.run(["taskkill", "/PID", pid, "/F"],
                                       capture_output=True, timeout=5)
                        echo_ok(f"WebServer 已停止 (PID {pid})")
                        stopped = True
        except Exception as e:
            echo_warn(f"自动停止失败: {e}，尝试终止所有 pythonw.exe...")

    # 检查 Gateway 端口
    env = load_env()
    gw_port = int(env.get("GATEWAY_PORT", GATEWAY_PORT))
    if check_port(gw_port):
        echo_info(f"端口 {gw_port} 被占用，尝试停止 Gateway...")
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.split("\n"):
                if f":{gw_port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1]
                    if pid.isdigit():
                        subprocess.run(["taskkill", "/PID", pid, "/F"],
                                       capture_output=True, timeout=5)
                        echo_ok(f"Gateway 已停止 (PID {pid})")
                        stopped = True
        except Exception as e:
            echo_warn(f"自动停止失败: {e}")

    # 如果端口方式没找到，回退到 pythonw.exe
    if not stopped:
        pids = find_pythonw_processes()
        if pids:
            echo_info(f"发现 {len(pids)} 个 pythonw.exe 进程: {pids}")
            for pid in pids:
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                   capture_output=True, timeout=5)
                except Exception:
                    pass
            echo_ok("已终止所有 pythonw.exe 进程")
        else:
            echo_info("没有运行中的服务进程")

    # 验证
    time.sleep(1)
    agent_ok = not check_port(AGENT_API_PORT)
    web_ok = not check_port(WEB_SERVER_PORT)
    gw_ok = not check_port(gw_port)

    if agent_ok and web_ok and gw_ok:
        echo_ok("所有服务已停止")
    else:
        if not agent_ok:
            echo_warn(f"端口 {AGENT_API_PORT} 仍被占用，可使用 --force 强制终止")
        if not web_ok:
            echo_warn(f"端口 {WEB_SERVER_PORT} 仍被占用，可使用 --force 强制终止")
        if not gw_ok:
            echo_warn(f"端口 {gw_port} 仍被占用，可使用 --force 强制终止")


# ── restart ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--web-only", is_flag=True, help="仅重启 WebServer")
@click.option("--gateway-only", is_flag=True, help="仅重启 Gateway")
@click.pass_context
def restart(ctx, web_only, gateway_only):
    """重启 DASHENG 服务（stop + start）"""

    echo_info("重启 DASHENG 服务...")
    click.echo("")

    # 停止
    echo_info("[1/2] 停止服务...")
    ctx.invoke(stop, force=False)
    time.sleep(2)

    # 启动
    click.echo("")
    echo_info("[2/2] 启动服务...")
    ctx.invoke(start, web_only=web_only, gateway_only=gateway_only)


# ── status ────────────────────────────────────────────────────────────────────

@cli.command()
def status():
    """查看 DASHENG 服务状态"""

    click.echo(click.style("🧌 DASHENG 服务状态", fg="cyan", bold=True))
    click.echo("=" * 50)

    env = load_env()
    gw_port = int(env.get("GATEWAY_PORT", GATEWAY_PORT))

    # ── Agent API 状态 ────────────────────────────────────────────────────
    click.echo("")
    click.echo("【Agent API】")

    agent_running = check_port(AGENT_API_PORT)
    if agent_running:
        click.echo(f"  状态:     {click.style('运行中 ✔', fg='green')}")
        click.echo(f"  端口:     {AGENT_API_PORT}")
        click.echo(f"  地址:     http://127.0.0.1:{AGENT_API_PORT}")

        # 尝试健康检查
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{AGENT_API_PORT}/v1/status",
                headers={"Accept": "application/json"},
                method="GET"
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                click.echo(f"  响应:     {click.style('正常', fg='green')}")
                agent_info = data.get("agent", {})
                model = agent_info.get("model", "-")
                ctx_len = agent_info.get("context_length", "-")
                uptime = agent_info.get("uptime", 0)
                click.echo(f"  模型:     {model}")
                click.echo(f"  上下文:   {ctx_len}")
                click.echo(f"  运行时间: {uptime}s")
        except Exception:
            click.echo(f"  响应:     {click.style('异常', fg='yellow')}")
    else:
        click.echo(f"  状态:     {click.style('未运行 ✖', fg='red')}")
        click.echo(f"  端口:     {AGENT_API_PORT} (未监听)")

    # ── WebServer 状态 ────────────────────────────────────────────────────
    click.echo("")
    click.echo("【WebServer】")

    web_running = check_port(WEB_SERVER_PORT)
    if web_running:
        click.echo(f"  状态:     {click.style('运行中 ✔', fg='green')}")
        click.echo(f"  端口:     {WEB_SERVER_PORT}")
        click.echo(f"  地址:     http://127.0.0.1:{WEB_SERVER_PORT}")

        # 尝试健康检查
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{WEB_SERVER_PORT}/",
                headers={"Accept": "text/html"},
                method="GET"
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    click.echo(f"  响应:     {click.style('正常', fg='green')}")
        except Exception:
            click.echo(f"  响应:     {click.style('异常', fg='yellow')}")
    else:
        click.echo(f"  状态:     {click.style('未运行 ✖', fg='red')}")
        click.echo(f"  端口:     {WEB_SERVER_PORT} (未监听)")

    # ── Gateway 状态 ──────────────────────────────────────────────────────
    click.echo("")
    click.echo("【Gateway】")

    gw_running = check_port(gw_port)
    if gw_running:
        click.echo(f"  状态:     {click.style('运行中 ✔', fg='green')}")
        click.echo(f"  端口:     {gw_port}")
        click.echo(f"  地址:     http://127.0.0.1:{gw_port}/health")

        # 尝试健康检查
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{gw_port}/health",
                headers={"Accept": "application/json"},
                method="GET"
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                click.echo(f"  响应:     {click.style('正常', fg='green')}")

                # QQ 状态
                qq_info = data.get("qq", {})
                qq_connected = qq_info.get("connected", False)
                if qq_connected:
                    click.echo(f"  QQ Bot:   {click.style('已连接 ✔', fg='green')}")
                else:
                    qq_app_id = qq_info.get("app_id", "")
                    if qq_app_id:
                        click.echo(f"  QQ Bot:   {click.style('未连接', fg='yellow')} (AppID: {qq_app_id}***)")
                    else:
                        click.echo(f"  QQ Bot:   {click.style('未配置', fg='red')}")

                # 微信状态
                wechat_info = data.get("wechat", {})
                wechat_connected = wechat_info.get("connected", False)
                if wechat_connected:
                    click.echo(f"  微信:     {click.style('已连接 ✔', fg='green')}")
                else:
                    click.echo(f"  微信:     {click.style('未连接', fg='yellow')}")

        except urllib.error.URLError:
            click.echo(f"  响应:     {click.style('连接失败', fg='red')}")
        except Exception as e:
            click.echo(f"  响应:     {click.style(f'异常: {e}', fg='yellow')}")
    else:
        click.echo(f"  状态:     {click.style('未运行 ✖', fg='red')}")
        click.echo(f"  端口:     {gw_port} (未监听)")

    # ── 配置概览 ──────────────────────────────────────────────────────────
    click.echo("")
    click.echo("【配置概览】")

    # LLM
    model = env.get("MODEL_NAME", "未设置")
    base_url = env.get("OPENAI_BASE_URL", "未设置")
    api_key = env.get("OPENAI_API_KEY", "")
    key_display = f"{api_key[:8]}..." if len(api_key) > 8 else ("已设置" if api_key else "未设置")
    ctx_len = env.get("MODEL_CONTEXT_LENGTH", "50000")
    compress_threshold = int(ctx_len) // 2
    click.echo(f"  LLM:      {model}")
    click.echo(f"  Base URL: {base_url}")
    click.echo(f"  API Key:  {key_display}")
    click.echo(f"  上下文:   {ctx_len} tokens (压缩阈值: {compress_threshold})")

    # QQ
    qq_id = env.get("QQ_APP_ID", "")
    click.echo(f"  QQ AppID: {qq_id or '未设置'}")

    # 微信
    wechat = env.get("WECHAT_ENABLED", "false")
    click.echo(f"  微信:     {'已启用' if wechat == 'true' else '未启用'}")

    # ── 进程信息 ──────────────────────────────────────────────────────────
    click.echo("")
    click.echo("【进程信息】")

    pids = find_pythonw_processes()
    if pids:
        click.echo(f"  pythonw.exe: {len(pids)} 个进程 (PID: {', '.join(str(p) for p in pids)})")
    else:
        click.echo(f"  pythonw.exe: 无运行中进程")

    # ── 日志 ──────────────────────────────────────────────────────────────
    click.echo("")
    click.echo("【日志文件】")

    web_log = PROJECT_DIR / "web_server.log"
    gw_log = SRC_DIR / "gateway.log"

    for name, log_path in [("WebServer", web_log), ("Gateway", gw_log)]:
        if log_path.exists():
            size = log_path.stat().st_size
            mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(log_path.stat().st_mtime))
            click.echo(f"  {name}: {log_path} ({size / 1024:.1f} KB, 更新: {mtime})")
        else:
            click.echo(f"  {name}: 无日志文件")

    click.echo("")


# ── chat ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--message", "-m", help="直接发送消息（非交互模式）")
@click.option("--thread", "-t", default="cli_test", help="会话 ID")
def chat(message, thread):
    """直接对话测试（与 Agent 交互）"""

    # 检查服务是否运行
    if not check_port(AGENT_API_PORT) and not check_port(WEB_SERVER_PORT):
        echo_error(f"Agent API ({AGENT_API_PORT}) 和 WebServer ({WEB_SERVER_PORT}) 均未运行")
        echo_info("请先运行: python dasheng.py start")
        sys.exit(1)

    if message:
        # 非交互模式：单条消息
        _send_chat(message, thread)
        return

    # 交互模式
    echo_info("🧌 DASHENG 对话测试 (输入 /quit 退出)")
    click.echo("=" * 50)

    while True:
        try:
            user_input = click.prompt(click.style("你", fg="cyan"), type=str)
        except (EOFError, KeyboardInterrupt):
            click.echo("")
            echo_info("退出对话")
            break

        if user_input.strip().lower() in ("/quit", "/exit", "/q"):
            echo_info("退出对话")
            break

        if not user_input.strip():
            continue

        _send_chat(user_input, thread)


def _send_chat(message: str, thread_id: str):
    """发送消息到 Agent API 并显示回复"""
    # 优先使用 Agent API，fallback WebServer
    if check_port(AGENT_API_PORT):
        url = f"http://127.0.0.1:{AGENT_API_PORT}/v1/chat"
    elif check_port(WEB_SERVER_PORT):
        url = f"http://127.0.0.1:{WEB_SERVER_PORT}/api/chat"
    else:
        echo_error("Agent API 和 WebServer 均未运行")
        echo_info("请先运行: python dasheng.py start")
        return
    payload = json.dumps({
        "message": message,
        "thread_id": thread_id,
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with click.progressbar(length=100, label=click.style("思考中", fg="yellow"),
                               show_percent=False, show_pos=False) as bar:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                bar.update(100)

        if "error" in result:
            echo_error(f"错误: {result['error']}")
            if result.get("auto_cleared"):
                echo_info("上下文已自动清除，可以继续提问")
        else:
            response = result.get("response", "(无回复)")
            tool_calls = result.get("tool_calls")

            click.echo(click.style("大圣", fg="green", bold=True) + ": " + response)

            if tool_calls:
                for tc in tool_calls:
                    click.echo(click.style(f"  🔧 {tc['name']}", fg="yellow") +
                               f"({json.dumps(tc.get('args', {}), ensure_ascii=False)})")

    except urllib.error.URLError as e:
        echo_error(f"连接失败: {e}")
        echo_info("请确认 WebServer 正在运行")
    except Exception as e:
        echo_error(f"请求异常: {e}")


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
