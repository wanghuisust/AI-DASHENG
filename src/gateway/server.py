"""DASHENG Gateway — 统一消息入口

架构：
  ┌──────────────────────────────────────────────────────┐
  │              DASHENG Gateway (:9090)                  │
  │                                                      │
  │  微信 ──iLink Bot API──▶ WeChatAdapter (扫码登录)     │
  │     长轮询 getUpdates → 消息推过来                     │
  │                                                      │
  │  QQ  ──OneBot v11──▶    QQAdapter (正向WS)            │
  │     主动连 WS → 消息推过来                             │
  │                                                      │
  │  → PlatformMessage → Agent 处理 → PlatformReply → 发回│
  └──────────────────────────────────────────────────────┘

微信接入：iLink Bot API（腾讯官方开放协议）
  - 无需安装 WeChatFerry / ComWeChatRobot 等第三方框架
  - 只需微信扫码一次，之后自动重连
  - pip install aiohttp cryptography qrcode

QQ 接入：QQ Bot 官方 API v2
  - WebSocket 网关接收事件（私聊 C2C_MESSAGE_CREATE / 群聊 GROUP_AT_MESSAGE_CREATE）
  - HTTP API 发送回复
  - 零额外安装：q.qq.com 注册获取 AppID + AppSecret 即可
"""

import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse
from http.server import HTTPServer, BaseHTTPRequestHandler

# 加载 .env 配置
from dotenv import load_dotenv
load_dotenv()

# 确保项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from gateway.models import PlatformMessage, PlatformReply
from gateway import qq_adapter, wechat_adapter

logger = logging.getLogger("gateway")

# ── 配置 ────────────────────────────────────────────────────────────────────

DASHENG_API = os.getenv("AGENT_API_URL", "http://127.0.0.1:8900")
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "data")

# 全局适配器实例
_wechat: wechat_adapter.WeChatAdapter | None = None


# ── Agent 调用 ───────────────────────────────────────────────────────────────

def call_agent(message: PlatformMessage) -> str:
    """调用 DASHENG Agent 处理消息（同步调用，有超时保护）"""
    url = f"{DASHENG_API}/v1/chat"
    payload = json.dumps({
        "message": message.text,
        "thread_id": message.chat_id,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=1800) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("response", "")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
            logger.error(f"[Agent] HTTP {e.code}: {err_body[:200]}")
        except Exception:
            logger.error(f"[Agent] HTTP {e.code}")
        return f"抱歉，处理消息时出错(HTTP {e.code})"
    except urllib.error.URLError as e:
        logger.error(f"[Agent] 连接失败: {e.reason}")
        return "抱歉，Agent 服务暂时不可用，请稍后再试。"
    except Exception as e:
        logger.error(f"[Agent] 调用异常: {e}")
        return f"抱歉，处理消息时出错"


def _call_agent_sync(message: PlatformMessage) -> str:
    """同步调用 Agent（fallback）"""
    url = f"{DASHENG_API}/v1/chat"
    payload = json.dumps({
        "message": message.text,
        "thread_id": message.chat_id,
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("response", "")
    except Exception as e:
        logger.error(f"Agent 同步调用也失败: {e}")
        return f"抱歉，处理消息时出错"


def _call_agent_stream(message: PlatformMessage, current_status: dict, status_lock: threading.Lock, cancel_event: threading.Event = None) -> str:
    """调用 Agent 流式接口（SSE），实时更新 current_status
    cancel_event: 如果被 set，中止 SSE 读取
    """
    url = f"{DASHENG_API}/v1/chat/stream"
    payload = json.dumps({
        "message": message.text,
        "thread_id": message.chat_id,
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=1800)
        # 记录 resp 到 active_requests，以便取消时关闭
        with _active_requests_lock:
            entry = _active_requests.get(message.chat_id)
            if entry:
                entry["sse_resp"] = resp
        response_text = ""
        logger.debug(f"[SSE] Connected to {url}, reading events...")
        for line_bytes in resp:
            # 检查是否被取消
            if cancel_event and cancel_event.is_set():
                logger.info(f"[SSE] 请求被取消，停止读取 (chat_id={message.chat_id})")
                try:
                    resp.close()
                except Exception:
                    pass
                return ""
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith(": "):
                logger.debug(f"[SSE] keepalive")
                continue
            if line.startswith("event: "):
                event_type = line[7:]
                logger.debug(f"[SSE] event: {event_type}")
            elif line.startswith("data: "):
                data_str = line[6:]
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    logger.debug(f"[SSE] non-json data: {data_str[:100]}")
                    continue

                logger.debug(f"[SSE] {event_type}: {json.dumps(data, ensure_ascii=False)[:200]}")

                if event_type == "status":
                    step = data.get("step", "")
                    msg = data.get("message", "")
                    content = data.get("content", "")
                    tool = data.get("tool", "")
                    with status_lock:
                        if step == "thinking":
                            current_status["message"] = "🤔 正在思考..."
                        elif step == "reasoning":
                            # 推理内容可能很长，只取前50字
                            short = content[:50] + ("..." if len(content) > 50 else "")
                            current_status["message"] = f"💭 推理中: {short}"
                        elif step == "tool_call":
                            current_status["message"] = f"🔧 正在调用: {tool}"
                        elif step == "tool_done":
                            current_status["message"] = f"✅ {tool} 执行完成\n▶️ 继续下一步…"
                        current_status["step"] = step
                elif event_type == "result":
                    response_text = data.get("response", "")
                elif event_type == "error":
                    logger.error(f"[Agent SSE] 错误: {data.get('message', '')}")
                    response_text = f"抱歉，处理消息时出错: {data.get('message', '')}"
        return response_text
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
            logger.error(f"[Agent SSE] HTTP {e.code}: {err_body[:200]}")
        except Exception:
            logger.error(f"[Agent SSE] HTTP {e.code}")
        return f"抱歉，处理消息时出错(HTTP {e.code})"
    except urllib.error.URLError as e:
        logger.error(f"[Agent SSE] 连接失败: {e.reason}")
        return "抱歉，Agent 服务暂时不可用，请稍后再试。"
    except Exception as e:
        logger.error(f"[Agent SSE] 流式调用异常: {e}")
        return f"抱歉，处理消息时出错"


# ── 消息处理 ─────────────────────────────────────────────────────────────────
# ── 消息处理 ─────────────────────────────────────────────────────────────────

# 按 chat_id 跟踪正在处理的请求，用于取消旧请求
_active_requests = {}        # chat_id → {"thread": Thread, "cancel_event": Event, "sse_resp": ...}
_active_requests_lock = threading.Lock()


def _cancel_active_request(chat_id: str, platform: str = "", is_group: bool = False, user_id: str = "") -> bool:
    """取消同一 chat_id 的正在处理的请求，返回是否有请求被取消"""
    with _active_requests_lock:
        entry = _active_requests.get(chat_id)
        if not entry:
            return False
        logger.info(f"[{chat_id}] 取消前一个正在处理的请求")
        entry["cancel_event"].set()  # 通知 SSE 读取线程停止
        # 关闭 SSE 连接
        if entry.get("sse_resp"):
            try:
                entry["sse_resp"].close()
            except Exception:
                pass
        # 通知 Agent API 取消
        try:
            cancel_url = f"{DASHENG_API}/v1/chat/cancel"
            payload = json.dumps({"thread_id": chat_id}).encode("utf-8")
            req = urllib.request.Request(cancel_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # Agent API 取消失败不阻塞

        # 给客户端发"正在取消任务…"通知
        if platform == "qq":
            cancel_reply = PlatformReply(
                platform=platform,
                chat_id=chat_id,
                text="⏳ 正在取消任务…",
                is_group=is_group,
                at_user=user_id,
            )
            try:
                qq_adapter.send_reply(cancel_reply)
            except Exception:
                pass
        return True


def handle_message(message: PlatformMessage):
    """统一消息处理入口 — 异步处理，不阻塞 WebSocket 事件循环"""
    logger.info(f"[{message.platform}] {message.user_id}@{message.chat_id}: {message.text[:80]}")

    # 取消同一 chat_id 的前一个请求
    had_active = _cancel_active_request(
        message.chat_id, message.platform, message.is_group, message.user_id
    )

    # 在新线程中处理，避免阻塞 QQ 心跳/事件循环
    cancel_event = threading.Event()
    t = threading.Thread(target=_process_and_reply, args=(message, cancel_event), daemon=True)
    with _active_requests_lock:
        _active_requests[message.chat_id] = {"thread": t, "cancel_event": cancel_event}
    t.start()


def _process_and_reply(message: PlatformMessage, cancel_event: threading.Event):
    """实际处理：调用 Agent 流式接口，每60s推送当前步骤状态到 QQ
    cancel_event: 如果被 set，表示有新请求到来，应取消当前请求
    """
    chat_id = message.chat_id
    try:
        progress_stop = threading.Event()
        current_status = {"step": "thinking", "message": "正在思考..."}
        status_lock = threading.Lock()

        # ── 发送开始处理通知（如果取消了前一个，_cancel_active_request 已发过通知） ──

        def _send_progress():
            """每60s发一次进度消息，附带当前步骤状态"""
            count = 0
            while not progress_stop.wait(60):
                if cancel_event.is_set():
                    return  # 新请求到来，停止进度
                count += 1
                with status_lock:
                    step_text = current_status.get("message", "")
                if count <= 3:
                    text = f"⏳ 正在处理，请稍候...（{count * 60}秒）\n{step_text}"
                else:
                    text = f"⏳ 正在处理，请稍候...（{count * 60}秒）\n{step_text}\n任务流程长，请等待"
                progress_reply = PlatformReply(
                    platform=message.platform,
                    chat_id=message.chat_id,
                    text=text,
                    is_group=message.is_group,
                    at_user=message.user_id,
                )
                qq_adapter.send_reply(progress_reply)
                logger.info(f"[QQ] 进度消息 #{count}: {step_text}")

        progress_thread = None
        if message.platform == "qq":
            progress_thread = threading.Thread(target=_send_progress, daemon=True)
            progress_thread.start()

        # ── 调用 Agent 流式接口 ──
        logger.info(f"[{message.platform}] 开始调用 Agent（流式）...")
        reply_text = _call_agent_stream(message, current_status, status_lock, cancel_event)

        # ── 停止进度消息 ──
        if progress_thread:
            progress_stop.set()
            progress_thread.join(timeout=3)

        # ── 如果被取消，不发回复（新请求会自己发） ──
        if cancel_event.is_set():
            logger.info(f"[{chat_id}] 请求被新请求取消，跳过回复")
            return

        if not reply_text:
            logger.warning(f"[{message.platform}] Agent 返回空回复")
            return

        logger.info(f"[{message.platform}] Agent 回复: {reply_text[:80]}...")

        # ── 发送正式回复 ──
        reply = PlatformReply(
            platform=message.platform,
            chat_id=message.chat_id,
            text=reply_text,
            is_group=message.is_group,
            at_user=message.user_id,
        )

        if message.platform == "wechat" and _wechat:
            _wechat.send_reply(reply)
        elif message.platform == "qq":
            qq_adapter.send_reply(reply)
        else:
            logger.warning(f"未知平台: {message.platform}")
    except Exception as e:
        logger.error(f"[{message.platform}] 处理消息异常: {e}", exc_info=True)
    finally:
        # 清理 active_requests
        with _active_requests_lock:
            entry = _active_requests.get(chat_id)
            if entry and entry.get("thread") is threading.current_thread():
                _active_requests.pop(chat_id, None)


# ── HTTP Handler (QQ 上报备选 + 健康检查) ─────────────────────────────────────

class GatewayHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            status = {
                "status": "ok",
                "wechat": _wechat.get_config() if _wechat else {"connected": False},
                "qq": qq_adapter.get_config(),
            }
            self.wfile.write(json.dumps(status).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/onebot":
            # QQ OneBot v11 HTTP 上报
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
                msg = qq_adapter.parse_message(data)
                if msg:
                    handle_message(msg)
            except Exception as e:
                logger.error(f"OneBot 解析失败: {e}")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        logger.debug(f"HTTP: {format % args}")


# ── 启动 ─────────────────────────────────────────────────────────────────────

def load_env(env_path: str = None):
    """手动加载 .env 文件（零依赖）"""
    if env_path is None:
        env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    env_path = os.path.abspath(env_path)
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if key and key not in os.environ:
                    os.environ[key] = val


def main():
    load_env()
    # 日志：FileHandler(UTF-8) + StreamHandler(安全编码)
    log_format = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    log_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Windows GBK 控制台遇到 Unicode 字符会崩溃，用 SafeStream 包装
    class SafeStream:
        """包装 sys.stdout，遇到编码错误用 replace 而非崩溃"""
        def __init__(self, stream):
            self._stream = stream
        def write(self, msg):
            try:
                self._stream.write(msg)
            except UnicodeEncodeError:
                self._stream.write(msg.encode(self._stream.encoding or 'utf-8', errors='replace').decode(self._stream.encoding or 'utf-8', errors='replace'))
        def flush(self):
            self._stream.flush()

    sh = logging.StreamHandler(SafeStream(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG,
        format=log_format,
        handlers=[
            sh,
            logging.FileHandler(
                os.path.join(log_dir, "gateway.log"),
                encoding="utf-8",
            ),
        ],
    )

    port = int(os.getenv("GATEWAY_PORT", "8080"))
    host = os.getenv("GATEWAY_HOST", "0.0.0.0")

    # ── 微信：iLink Bot API 扫码登录 ─────────────────────────────────────
    # 不需要 WeChatFerry！不需要 DLL 注入！
    # 启动时如果没有保存的凭证，会弹出二维码让微信扫码
    global _wechat
    _wechat = None  # 暂不启动微信，先测 QQ
    wechat_ok = False

    # ── QQ：Bot 官方 API v2 ──────────────────────────────────────────
    qq_ok = qq_adapter.start_listener(handle_message)
    if qq_ok:
        logger.info("QQ Bot API connected")
    else:
        logger.warning("QQ not connected (请配置 QQ_APP_ID / QQ_APP_SECRET)")

    # ── HTTP Server ───────────────────────────────────────────────────────
    server = HTTPServer((host, port), GatewayHandler)
    logger.info(f"DASHENG Gateway: {host}:{port}")
    logger.info(f"  WeChat: iLink Bot API (扫码登录)")
    logger.info(f"  QQ:     Bot API v2 (AppID + WebSocket)")
    logger.info(f"  Agent:  {DASHENG_API}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Gateway stopped")
        server.server_close()


if __name__ == "__main__":
    main()
