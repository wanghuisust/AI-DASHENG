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
    """调用 DASHENG Agent 处理消息（SSE 流式，先发进度再发结果）"""
    url = f"{DASHENG_API}/v1/chat/stream"
    payload = json.dumps({
        "message": message.text,
        "thread_id": message.chat_id,
    }).encode("utf-8")

    import http.client
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"

    try:
        conn = http.client.HTTPConnection(host, port, timeout=30)
        conn.request("POST", path, body=payload, headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        })
        resp = conn.getresponse()
        # SSE 长连接：设 socket 级别超时 300s（每个 chunk 的等待上限）
        # Agent API 每15秒发 keepalive 心跳，超时不会触发
        if conn.sock:
            conn.sock.settimeout(300)

        if resp.status != 200:
            logger.error(f"[Agent] SSE 返回 {resp.status}")
            conn.close()
            return f"抱歉，处理消息时出错(HTTP {resp.status})"

        response_text = ""
        event_type = ""
        got_result = False
        start_time = time.time()

        # 逐行读 SSE，每行超时用 socket 级别控制
        buf = b""
        while True:
            # 整体超时保护：10 分钟
            if time.time() - start_time > 600:
                logger.warning("[Agent] Agent 执行超 300s，强制结束")
                if response_text:
                    response_text += "\n\n⚠️ 响应超时，部分结果可能不完整。"
                else:
                    response_text = "抱歉，处理时间过长，请稍后重试。"
                break

            try:
                chunk = resp.read(4096)
            except socket.timeout:
                # 单次读取超时，但整体还没超，继续等
                continue
            except Exception as e:
                logger.warning(f"[Agent] SSE read exception: {type(e).__name__}: {e}")
                break
            if not chunk:
                logger.info(f"[Agent] SSE connection closed by server after {time.time()-start_time:.0f}s")
                break

            buf += chunk
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    if event_type == "status":
                        step = data.get("step", "")
                        if step == "tool_call":
                            logger.info(f"[Agent] 调用工具: {data.get('tool', '?')}")
                        elif step == "tool_done":
                            logger.info(f"[Agent] 工具完成: {data.get('tool', '?')}")
                    elif event_type == "result":
                        response_text = data.get("response", "")
                        got_result = True
                    elif event_type == "error":
                        conn.close()
                        return f"处理出错: {data.get('message', '未知错误')}"

        conn.close()

        if got_result:
            return response_text
        if not response_text:
            return "抱歉，处理超时，请稍后重试。"
        return response_text
    except Exception as e:
        logger.error(f"Agent SSE 调用异常: {e}")
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
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("response", "")
    except Exception as e:
        logger.error(f"Agent 同步调用也失败: {e}")
        return f"抱歉，处理消息时出错"


# ── 消息处理 ─────────────────────────────────────────────────────────────────

def handle_message(message: PlatformMessage):
    """统一消息处理入口 — 异步处理，不阻塞 WebSocket 事件循环"""
    logger.info(f"[{message.platform}] {message.user_id}@{message.chat_id}: {message.text[:80]}")
    # 在新线程中处理，避免阻塞 QQ 心跳/事件循环
    threading.Thread(target=_process_and_reply, args=(message,), daemon=True).start()


def _process_and_reply(message: PlatformMessage):
    """实际处理：调用 Agent → 发送回复"""
    try:
        logger.info(f"[{message.platform}] 开始调用 Agent...")
        reply_text = call_agent(message)
        if not reply_text:
            logger.warning(f"[{message.platform}] Agent 返回空回复")
            return

        logger.info(f"[{message.platform}] Agent 回复: {reply_text[:80]}...")

        # 发送回复
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
        level=logging.INFO,
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
