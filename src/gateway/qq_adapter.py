"""QQ 适配器 — QQ Bot 官方 API v2

零外部依赖方案：仅用 Python 标准库 + websocket-client（conda 已有）
流程：
  1. POST /app/getAppAccessToken → 获取 access_token
  2. GET  /gateway/bot → 获取 WebSocket 网关地址
  3. 连接 WSS → 收到 OpCode 10 Hello → 回 OpCode 2 Identify
  4. 定期发 OpCode 1 Heartbeat
  5. 收到 OpCode 0 Dispatch（C2C_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE）
  6. POST /v2/users/{openid}/messages 或 /v2/groups/{group_openid}/messages 回复

配置（.env）：
  QQ_APP_ID=         → QQ 开放平台 AppID（q.qq.com 创建机器人获取）
  QQ_APP_SECRET=     → QQ 开放平台 AppSecret
  QQ_IS_SANDBOX=false → 是否沙箱环境

依赖：websocket-client（conda 已有 1.9.0）
"""

from __future__ import annotations

import json
import logging
import os
import struct
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("gateway.qq")

# ── 可选依赖 ────────────────────────────────────────────────────────────────

try:
    import websocket
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False


def check_requirements() -> tuple:
    """检查依赖"""
    if not WS_AVAILABLE:
        return False, "websocket-client 未安装 (pip install websocket-client)"
    app_id = os.getenv("QQ_APP_ID", "")
    app_secret = os.getenv("QQ_APP_SECRET", "")
    if not app_id or not app_secret:
        return False, "QQ_APP_ID / QQ_APP_SECRET 未配置（q.qq.com 创建机器人获取）"
    return True, "OK"


# ── QQ Bot API 常量 ────────────────────────────────────────────────────────

# 正式环境 / 沙箱环境
_API_BASE = "https://api.sgroup.qq.com"
_SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"

# OpCode
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# 事件类型
EVENT_C2C_MSG = "C2C_MESSAGE_CREATE"
EVENT_GROUP_MSG = "GROUP_AT_MESSAGE_CREATE"


# ── Access Token 管理 ──────────────────────────────────────────────────────

class TokenManager:
    """管理 QQ Bot access_token，自动刷新"""

    def __init__(self, app_id: str, app_secret: str, sandbox: bool = False):
        self._app_id = app_id
        self._app_secret = app_secret
        self._base = _SANDBOX_API_BASE if sandbox else _API_BASE
        self._token: str = ""
        self._expires_at: float = 0
        self._lock = threading.Lock()

    @property
    def api_base(self) -> str:
        return self._base

    def get_token(self) -> str:
        """获取有效的 access_token，过期自动刷新"""
        with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            return self._refresh()

    def _refresh(self) -> str:
        """刷新 access_token"""
        # Token URL 固定为 bots.qq.com，不是 api.sgroup.qq.com
        url = "https://bots.qq.com/app/getAppAccessToken"
        payload = json.dumps({
            "appId": self._app_id,
            "clientSecret": self._app_secret,
        }).encode("utf-8")

        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self._token = data["access_token"]
                expires_in = int(data.get("expires_in", 7200))
                self._expires_at = time.time() + expires_in
                logger.info(f"QQ: access_token 刷新成功，有效期 {expires_in}s")
                return self._token
        except Exception as e:
            logger.error(f"QQ: access_token 获取失败: {e}")
            return ""

    def auth_header(self) -> dict:
        """返回 Authorization header"""
        token = self.get_token()
        if token:
            return {"Authorization": f"QQBot {token}"}
        return {}


# ── 消息解析 ──────────────────────────────────────────────────────────────

def parse_event(event_type: str, data: dict) -> Optional[dict]:
    """
    解析 QQ Bot 事件，返回统一格式：
    {
        "event_type": "C2C_MESSAGE_CREATE" | "GROUP_AT_MESSAGE_CREATE",
        "is_group": bool,
        "user_openid": str,     # 用户 openid（群聊为 author.member_openid）
        "chat_id": str,         # 私聊=用户openid，群聊=群openid
        "content": str,         # 消息文本
        "msg_id": str,          # 消息 ID（回复需要）
        "seq": int,             # 消息序号（回复需要）
        "raw": dict,
    }
    """
    if event_type == EVENT_C2C_MSG:
        author = data.get("author", {})
        user_openid = author.get("user_openid", "")
        content = data.get("content", "").strip()
        msg_id = data.get("id", "")
        seq = data.get("seq", 0)
        return {
            "event_type": event_type,
            "is_group": False,
            "user_openid": user_openid,
            "chat_id": user_openid,
            "content": content,
            "msg_id": msg_id,
            "seq": seq,
            "raw": data,
        }

    elif event_type == EVENT_GROUP_MSG:
        author = data.get("author", {})
        group_openid = data.get("group_openid", "")
        member_openid = author.get("member_openid", "")
        content = data.get("content", "").strip()
        msg_id = data.get("id", "")
        seq = data.get("seq", 0)
        return {
            "event_type": event_type,
            "is_group": True,
            "user_openid": member_openid,
            "chat_id": group_openid,
            "content": content,
            "msg_id": msg_id,
            "seq": seq,
            "raw": data,
        }

    return None


def parse_message(data: dict):
    """Gateway 统一接口 — 解析 QQ Bot 事件为 PlatformMessage"""
    from .models import PlatformMessage

    event_type = data.get("t", "")
    event_data = data.get("d", {})

    parsed = parse_event(event_type, event_data)
    if not parsed:
        return None

    is_group = parsed["is_group"]
    user_id = parsed["user_openid"]
    chat_id = f"qq_group_{parsed['chat_id']}" if is_group else f"qq_c2c_{parsed['chat_id']}"
    text = parsed["content"]

    return PlatformMessage(
        platform="qq",
        user_id=user_id,
        chat_id=chat_id,
        text=text,
        is_group=is_group,
        at_me=True,  # QQ Bot API 只有 @机器人 或私聊才推事件
        raw=data,
    )


# ── QQ Bot Adapter 主类 ──────────────────────────────────────────────────

class QQBotAdapter:
    """
    DASHENG QQ 适配器 — QQ Bot 官方 API v2

    流程：access_token → gateway URL → WebSocket 连接 → 心跳 + 事件监听
    """

    def __init__(self, on_message: Callable = None):
        self._on_message = on_message
        self._app_id = os.getenv("QQ_APP_ID", "")
        self._app_secret = os.getenv("QQ_APP_SECRET", "")
        self._sandbox = os.getenv("QQ_IS_SANDBOX", "false").lower() in ("true", "1", "yes")

        self._token_mgr: Optional[TokenManager] = None
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._connected = False
        self._session_id: str = ""
        self._seq: Optional[int] = None
        self._heartbeat_interval: int = 41250  # ms，Hello 消息中指定
        self._last_heartbeat_ack = False
        self._running = False

        # ── 重连状态（参考 Hermes QQAdapter） ──
        self._close_code: Optional[int] = None    # 最近一次 WS 关闭码
        self._close_reason: str = ""               # 关闭原因
        self._reconnect_needed = False             # on_close 设标志，主循环统一重连
        self._connect_time: float = 0.0            # 连接建立时间（快速断连检测）
        self._quick_disconnect_count: int = 0      # 短时间内反复断连次数
        self._gateway_url: str = ""                # 保存网关 URL 供重连用

    @property
    def is_connected(self):
        return self._connected

    def get_config(self):
        return {
            "mode": "qq_bot_api",
            "app_id": self._app_id[:6] + "***" if self._app_id else "",
            "sandbox": self._sandbox,
            "connected": self._connected,
        }

    # ── 启动 ─────────────────────────────────────────────────────────────

    def start_listener(self, on_message: Callable) -> bool:
        """Gateway 统一接口 — 启动主循环（含自动重连）"""
        self._on_message = on_message
        ok, msg = check_requirements()
        if not ok:
            logger.warning(f"QQ Bot API 依赖缺失: {msg}")
            return False

        # 初始化 Token 管理器
        self._token_mgr = TokenManager(self._app_id, self._app_secret, self._sandbox)
        token = self._token_mgr.get_token()
        if not token:
            logger.error("QQ: 无法获取 access_token，请检查 QQ_APP_ID / QQ_APP_SECRET")
            return False

        # 获取 WebSocket 网关地址
        gateway_url = self._get_gateway()
        if not gateway_url:
            logger.error("QQ: 无法获取网关地址")
            return False

        logger.info(f"QQ: 网关地址 {gateway_url}")
        self._gateway_url = gateway_url
        self._running = True

        # ── 重连主循环（参考 Hermes _listen_loop） ──
        backoff_idx = 0
        MAX_RECONNECT = 5
        QUICK_DISCONNECT_THRESHOLD = 30  # 30秒内断开算快速断连
        MAX_QUICK_DISCONNECT = 3
        BACKOFF_DELAYS = [1, 2, 5, 10, 30]  # 指数退避（秒）

        while self._running:
            self._reconnect_needed = False
            self._connect_time = time.monotonic()
            self._connect_ws(self._gateway_url)

            # 等待 WS 断开或停止（on_close / 心跳超时会设 _reconnect_needed=True）
            while self._running and not self._reconnect_needed:
                time.sleep(0.5)

            if not self._running:
                break

            self._connected = False
            self._stop_heartbeat()
            self._close_ws()  # 安全关闭旧连接

            # ── 关闭码分类处理（参考 Hermes QQAdapter） ──
            code = self._close_code
            reason = self._close_reason
            logger.warning(f"QQ: WebSocket 断开: code={code} reason={reason}")

            # 不可恢复的错误码 → 停止重连
            FATAL_CODES = {
                4001,   # Invalid opcode
                4002,   # Invalid payload
                4010,   # Invalid shard
                4011,   # Sharding required
                4012,   # Invalid API version
                4013,   # Invalid intent
                4014,   # Intent not authorized
                4914,   # Bot offline/sandbox-only
                4915,   # Bot banned
            }
            if code in FATAL_CODES:
                logger.error(f"QQ: 致命关闭码 {code}，停止重连。请检查 QQ 开放平台配置。")
                break

            # Token 无效 → 刷新 token
            if code == 4004:
                logger.info("QQ: Token 无效(4004)，刷新后重连")
                self._token_mgr._token = ""
                self._token_mgr._expires_at = 0

            # Session 错误 → 清空 session，全量 Identify
            if code in {4006, 4007, 4900, 4901, 4902, 4903}:
                logger.info(f"QQ: Session 错误({code})，清空 session 重连")
                self._session_id = ""
                self._seq = None

            # 限流 → 等 60 秒
            if code == 4008:
                logger.info("QQ: 限流(4008)，等待 60 秒")
                time.sleep(60)

            # ── 快速断连检测 ──
            duration = time.monotonic() - self._connect_time
            if duration < QUICK_DISCONNECT_THRESHOLD and self._connect_time > 0:
                self._quick_disconnect_count += 1
                logger.warning(f"QQ: 快速断连({duration:.1f}s)，次数: {self._quick_disconnect_count}")
                if self._quick_disconnect_count >= MAX_QUICK_DISCONNECT:
                    logger.error("QQ: 连续快速断连，可能 AppID/Secret 错误或 Bot 权限问题，停止重连")
                    break
            else:
                self._quick_disconnect_count = 0

            # ── 指数退避重连 ──
            if backoff_idx >= MAX_RECONNECT:
                logger.error("QQ: 超过最大重连次数，停止")
                break

            delay = BACKOFF_DELAYS[min(backoff_idx, len(BACKOFF_DELAYS) - 1)]
            logger.info(f"QQ: {delay}秒后重连 (第 {backoff_idx + 1}/{MAX_RECONNECT} 次)...")
            time.sleep(delay)

            # 重连：刷新 token + 获取网关
            try:
                token = self._token_mgr.get_token()
                if not token:
                    logger.error("QQ: 重连失败，无法获取 token")
                    backoff_idx += 1
                    continue
                gateway_url = self._get_gateway()
                if not gateway_url:
                    logger.error("QQ: 重连失败，无法获取网关")
                    backoff_idx += 1
                    continue
                self._gateway_url = gateway_url
                backoff_idx = 0  # 重连成功后重置
            except Exception as e:
                logger.error(f"QQ: 重连准备失败: {e}")
                backoff_idx += 1

        self._running = False
        self._connected = False
        logger.info("QQ: 主循环退出")
        return True

    # ── 获取网关地址 ─────────────────────────────────────────────────────

    def _get_gateway(self) -> str:
        """GET /gateway/bot → 获取 WebSocket URL"""
        url = f"{self._token_mgr.api_base}/gateway/bot"
        headers = self._token_mgr.auth_header()

        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                ws_url = data.get("url", "")
                logger.info(f"QQ: 网关剩余连接数: {data.get('shards', '?')}, "
                            f"session_start_limit: {data.get('session_start_limit', {})}")
                return ws_url
        except Exception as e:
            logger.error(f"QQ: 获取网关地址失败: {e}")
            return ""

    # ── WebSocket 连接 ───────────────────────────────────────────────────

    def _connect_ws(self, gateway_url: str):
        """连接 WebSocket 网关 — on_close 只设标志，由主循环统一重连"""
        # 清理旧连接
        if self._ws:
            try:
                self._ws.close()
            except:
                pass
            self._ws = None

        # 确保网关 URL 以 / 结尾（QQ Bot 要求）
        if not gateway_url.endswith("/"):
            gateway_url += "/"

        def on_open(ws):
            logger.info("QQ: WebSocket 已连接，等待 Hello...")

        def on_message(ws, raw):
            try:
                payload = json.loads(raw)
                self._handle_payload(payload)
            except Exception as e:
                logger.error(f"QQ: 消息解析失败: {e}")

        def on_error(ws, error):
            logger.error(f"QQ: WebSocket 错误: {error}")

        def on_close(ws, code, reason):
            # ✅ 只设标志，不直接重连——由 start_listener 主循环统一处理
            self._close_code = code
            self._close_reason = reason or ""
            self._connected = False
            self._reconnect_needed = True
            logger.warning(f"QQ: WebSocket 断开: code={code} reason={reason}")

        self._ws = websocket.WebSocketApp(
            gateway_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        self._ws_thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 0, "ping_timeout": None},  # 禁用底层 ping，用 QQ 应用层心跳
            daemon=True,
        )
        self._ws_thread.start()

    def _handle_payload(self, payload: dict):
        """处理 QQ Bot WebSocket OpCode"""
        op = payload.get("op")
        t = payload.get("t")
        d = payload.get("d", {})
        s = payload.get("s")

        if s is not None:
            self._seq = s

        # OpCode 10: Hello → 发送 Identify
        if op == OP_HELLO:
            self._heartbeat_interval = d.get("heartbeat_interval", 41250)
            logger.info(f"QQ: Hello 收到，心跳间隔 {self._heartbeat_interval}ms")
            self._send_identify()

        # OpCode 11: Heartbeat ACK
        elif op == OP_HEARTBEAT_ACK:
            self._last_heartbeat_ack = True
            logger.debug("QQ: Heartbeat ACK")

        # OpCode 0: Dispatch → 事件分发
        elif op == OP_DISPATCH:
            self._handle_dispatch(t, d)

        # OpCode 7: Reconnect → 触发重连
        elif op == OP_RECONNECT:
            logger.warning("QQ: 收到 Reconnect，触发重连...")
            self._reconnect_needed = True

        # OpCode 9: Invalid Session → 重新 Identify
        elif op == OP_INVALID_SESSION:
            logger.warning("QQ: Invalid Session，重新 Identify...")
            self._session_id = ""
            self._seq = None
            time.sleep(3)
            self._send_identify()

        # OpCode 1: 服务端要求 Heartbeat
        elif op == OP_HEARTBEAT:
            self._send_heartbeat()

    # ── Identify / Resume ────────────────────────────────────────────────

    def _send_identify(self):
        """OpCode 2: Identify 鉴权"""
        if not self._token_mgr:
            return

        token = self._token_mgr.get_token()
        identify_payload = {
            "op": OP_IDENTIFY,
            "d": {
                "token": f"QQBot {token}",
                "intents": self._get_intents(),
                "shard": [0, 1],
            }
        }

        self._ws.send(json.dumps(identify_payload))
        logger.info("QQ: Identify 已发送")

    def _try_resume(self, gateway_url: str):
        """OpCode 6: Resume 恢复会话"""
        if not self._token_mgr or not self._session_id:
            self._connect_ws(gateway_url)
            return

        token = self._token_mgr.get_token()
        resume_payload = {
            "op": OP_RESUME,
            "d": {
                "token": f"QQBot {token}",
                "session_id": self._session_id,
                "seq": self._seq,
            }
        }

        # 重新连接并发 Resume
        def on_open(ws):
            ws.send(json.dumps(resume_payload))
            logger.info("QQ: Resume 已发送")

        def on_message(ws, raw):
            try:
                payload = json.loads(raw)
                self._handle_payload(payload)
            except Exception as e:
                logger.error(f"QQ: Resume 消息解析失败: {e}")

        def on_error(ws, error):
            logger.error(f"QQ: Resume WS 错误: {error}")

        def on_close(ws, code, reason):
            self._connected = False
            self._running = False
            logger.warning(f"QQ: Resume WS 断开: code={code}")
            # Resume 失败，5秒后从头重连
            logger.info("QQ: Resume 失败，5秒后全新连接...")
            time.sleep(5)
            self._full_reconnect()

        self._ws = websocket.WebSocketApp(
            gateway_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        self._thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 0, "ping_timeout": None},  # 禁用底层 ping，用 QQ 应用层心跳
            daemon=True,
        )
        self._thread.start()

    def _get_intents(self) -> int:
        """
        计算 intents 位掩码：
          PUBLIC_GUILD_MESSAGES = 1 << 0  = 1   (频道@机器人消息)
          GUILD_MESSAGES        = 1 << 12 = 4096 (频道全部消息，需私域)
          GROUP_AT_MESSAGE_CREATE = 1 << 25 = 33554432 (群@机器人消息)
          C2C_MESSAGE_CREATE      = 1 << 25 = 33554432 (私聊消息)
        
        注意：C2C 和 GROUP_AT 在同一个 bit(25)，实际上：
          PUBLIC_GUILD_MESSAGES  = 1 << 0  = 1
          C2C_GROUP_AT_MESSAGES  = 1 << 25 = 33554432
        
        合计 = 1 + 33554432 = 33554433
        """
        intent_c2c_group = 1 << 25  # 33554432：私聊 + 群@机器人
        intent_guild_public = 1 << 0  # 1：频道公开消息
        return intent_c2c_group | intent_guild_public  # 33554433

    # ── 心跳 ─────────────────────────────────────────────────────────────

    def _start_heartbeat(self):
        """启动心跳线程"""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return

        self._missed_acks = 0  # 连续未收到 ACK 的次数

        def heartbeat_loop():
            while self._running and self._connected:
                time.sleep(self._heartbeat_interval / 1000.0)
                if not self._running or not self._connected:
                    break

                if not self._last_heartbeat_ack:
                    self._missed_acks += 1
                    logger.warning(f"QQ: Heartbeat 未收到 ACK ({self._missed_acks}/3)")
                    if self._missed_acks >= 3:
                        logger.error("QQ: 连续 3 次心跳无 ACK，触发重连")
                        # 设标志让主循环处理重连，不直接关 WS
                        self._reconnect_needed = True
                        break
                else:
                    self._missed_acks = 0

                self._send_heartbeat()
                self._last_heartbeat_ack = False

        self._heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        logger.info(f"QQ: 心跳线程启动，间隔 {self._heartbeat_interval}ms")

    def _stop_heartbeat(self):
        """停止心跳线程"""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            # 心跳线程检查 self._connected，设 False 即可让它退出
            self._connected = False
            self._heartbeat_thread.join(timeout=3)
        self._heartbeat_thread = None

    def _send_heartbeat(self):
        """OpCode 1: Heartbeat"""
        if self._ws and self._connected:
            payload = {"op": OP_HEARTBEAT, "d": self._seq}
            try:
                self._ws.send(json.dumps(payload))
                logger.debug("QQ: Heartbeat 已发送")
            except Exception as e:
                logger.error(f"QQ: Heartbeat 发送失败: {e}")

    # ── 全量重连（已由 start_listener 主循环统一处理） ──────────────────

    # ── 事件分发 ─────────────────────────────────────────────────────────

    def _handle_dispatch(self, event_type: str, data: dict):
        """处理 Dispatch 事件"""
        # READY 事件 → 连接成功
        if event_type == "READY":
            self._connected = True
            self._session_id = data.get("session_id", "")
            self._last_heartbeat_ack = True  # 初始化 ACK 状态
            self._start_heartbeat()  # 鉴权通过后才启动心跳
            user = data.get("user", {})
            logger.info(f"QQ: 连接成功！Bot: {user.get('username', '?')} "
                        f"session_id: {self._session_id[:8]}...")
            return

        # RESUMED 事件 → 恢复成功
        if event_type == "RESUMED":
            self._connected = True
            logger.info("QQ: Resume 成功")
            return

        # 消息事件
        if event_type in (EVENT_C2C_MSG, EVENT_GROUP_MSG):
            msg = parse_message({"t": event_type, "d": data})
            if msg and self._on_message:
                self._on_message(msg)
            return

        # 其他事件
        logger.debug(f"QQ: 忽略事件 {event_type}")

    # ── 发送回复 ─────────────────────────────────────────────────────────

    def send_reply(self, reply) -> bool:
        """Gateway 统一接口 — 发送 PlatformReply"""
        from .models import PlatformReply

        if not self._token_mgr:
            return False

        if reply.is_group:
            group_openid = reply.chat_id.replace("qq_group_", "")
            return self._send_group_msg(group_openid, reply.text)
        else:
            user_openid = reply.chat_id.replace("qq_c2c_", "")
            return self._send_c2c_msg(user_openid, reply.text)

    def _send_c2c_msg(self, openid: str, text: str) -> bool:
        """POST /v2/users/{openid}/messages"""
        url = f"{self._token_mgr.api_base}/v2/users/{openid}/messages"
        return self._post_message(url, text, msg_type=0, msg_seq=int(time.time()) % 2147483647)

    def _send_group_msg(self, group_openid: str, text: str) -> bool:
        """POST /v2/groups/{group_openid}/messages"""
        url = f"{self._token_mgr.api_base}/v2/groups/{group_openid}/messages"
        return self._post_message(url, text, msg_type=0, msg_seq=int(time.time()) % 2147483647)

    def _post_message(self, url: str, text: str, msg_type: int = 0, msg_seq: int = 1) -> bool:
        """发送消息 HTTP 请求"""
        headers = self._token_mgr.auth_header()
        headers["Content-Type"] = "application/json"

        payload = json.dumps({
            "content": text,
            "msg_type": msg_type,
            "msg_seq": msg_seq,
        }).encode("utf-8")

        req = urllib.request.Request(
            url, data=payload, headers=headers, method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                # 成功返回消息 ID
                msg_id = result.get("id", "")
                if msg_id:
                    logger.info(f"QQ: 消息发送成功 id={msg_id}")
                    return True
                # 检查错误码
                code = result.get("code", -1)
                if code == 0:
                    return True
                logger.error(f"QQ: 消息发送失败: {result}")
                return False
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.error(f"QQ: 消息发送 HTTP 错误 {e.code}: {body}")
            return False
        except Exception as e:
            logger.error(f"QQ: 消息发送失败: {e}")
            return False

    # ── 停止 ─────────────────────────────────────────────────────────────

    def stop(self):
        """停止适配器"""
        self._running = False
        self._reconnect_needed = True  # 打断等待循环
        self._close_ws()
        self._connected = False
        logger.info("QQ: 已停止")

    def _close_ws(self):
        """安全关闭 WebSocket"""
        if self._ws:
            try:
                self._ws.close()
            except:
                pass
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=3)
        self._ws = None
        self._ws_thread = None


# ── 模块级实例 ────────────────────────────────────────────────────────────

_adapter: Optional[QQBotAdapter] = None


def start_listener(on_message: Callable) -> bool:
    """Gateway 统一入口 — 在独立线程中启动重连主循环"""
    global _adapter
    _adapter = QQBotAdapter(on_message=on_message)
    # 在独立线程中启动主循环（含自动重连），不阻塞 server.py
    t = threading.Thread(target=_adapter.start_listener, args=(on_message,), daemon=True)
    t.start()
    # 等待首次连接结果（最多 15 秒）
    for _ in range(30):
        if _adapter.is_connected:
            return True
        time.sleep(0.5)
    return _adapter.is_connected


def send_reply(reply) -> bool:
    """Gateway 统一发送"""
    if _adapter:
        return _adapter.send_reply(reply)
    return False


def get_config() -> dict:
    """Gateway 统一状态"""
    if _adapter:
        return _adapter.get_config()
    return {"mode": "qq_bot_api", "connected": False}
