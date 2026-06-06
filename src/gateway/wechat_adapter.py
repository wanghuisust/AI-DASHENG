"""微信适配器 — iLink Bot API（微信官方开放协议）

工作方式（与 Hermes Agent 官方 weixin adapter 一致）：
  1. 首次：调用 get_bot_qrcode → 终端显示二维码 → 微信扫码 → 获得 token + account_id
  2. 后续：长轮询 getUpdates (35s) → 实时收到消息
  3. 回复：sendMessage + context_token 回显
  4. 媒体：AES-128-ECB 加密 → CDN 上传

不需要安装任何第三方框架（WeChatFerry/ComWeChatRobot 都不需要）！
只需要微信扫码一次，之后自动重连。

依赖：aiohttp, cryptography, qrcode(可选, 终端显示二维码)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import struct
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("gateway.wechat")

# ── iLink 协议常量 ─────────────────────────────────────────────────────────

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.1.3"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (1 << 8) | 3

# API endpoints
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

# Timeouts
LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
QR_POLL_TIMEOUT_MS = 35_000

# Retry
MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_S = 2
BACKOFF_DELAY_S = 30
SESSION_EXPIRED_ERRCODE = -14

# Item types
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2


# ── 可选依赖 ────────────────────────────────────────────────────────────────

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False


def check_requirements() -> Tuple[bool, str]:
    """检查依赖是否满足"""
    if not AIOHTTP_AVAILABLE:
        return False, "aiohttp 未安装 (pip install aiohttp)"
    if not CRYPTO_AVAILABLE:
        return False, "cryptography 未安装 (pip install cryptography)"
    return True, "OK"


# ── AES-128-ECB ──────────────────────────────────────────────────────────────

def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    padded = _pkcs7_pad(plaintext)
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    dec = cipher.decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    pad_len = padded[-1]
    if pad_len < 1 or pad_len > 16:
        return padded
    return padded[:-pad_len]


# ── 凭证持久化 ───────────────────────────────────────────────────────────────

class CredentialStore:
    """微信凭证持久化（token + account_id + context_tokens）"""

    def __init__(self, data_dir: str):
        self._dir = Path(data_dir) / "wechat"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _account_file(self, account_id: str) -> Path:
        return self._dir / f"{account_id}.json"

    def save(self, account_id: str, token: str, base_url: str = "", user_id: str = ""):
        data = {
            "account_id": account_id,
            "token": token,
            "base_url": base_url or ILINK_BASE_URL,
            "user_id": user_id,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        f = self._account_file(account_id)
        f.write_text(json.dumps(data, indent=2), "utf-8")
        # 记录当前活跃账号
        (self._dir / "_active").write_text(account_id, "utf-8")

    def load(self, account_id: str) -> Optional[Dict]:
        f = self._account_file(account_id)
        if f.exists():
            try:
                return json.loads(f.read_text("utf-8"))
            except Exception:
                pass
        return None

    def load_active(self) -> Optional[Dict]:
        active = self._dir / "_active"
        if active.exists():
            account_id = active.read_text("utf-8").strip()
            return self.load(account_id)
        return None

    def save_context_token(self, account_id: str, user_id: str, token: str):
        tokens = self._load_context_tokens(account_id)
        tokens[user_id] = token
        self._save_context_tokens(account_id, tokens)

    def get_context_token(self, account_id: str, user_id: str) -> Optional[str]:
        tokens = self._load_context_tokens(account_id)
        return tokens.get(user_id)

    def _context_file(self, account_id: str) -> Path:
        return self._dir / f"{account_id}.context.json"

    def _load_context_tokens(self, account_id: str) -> Dict[str, str]:
        f = self._context_file(account_id)
        if f.exists():
            try:
                return json.loads(f.read_text("utf-8"))
            except Exception:
                pass
        return {}

    def _save_context_tokens(self, account_id: str, tokens: Dict[str, str]):
        self._context_file(account_id).write_text(json.dumps(tokens), "utf-8")

    def save_sync_buf(self, account_id: str, buf: str):
        f = self._dir / f"{account_id}.sync.json"
        f.write_text(json.dumps({"get_updates_buf": buf}), "utf-8")

    def load_sync_buf(self, account_id: str) -> str:
        f = self._dir / f"{account_id}.sync.json"
        if f.exists():
            try:
                return json.loads(f.read_text("utf-8")).get("get_updates_buf", "")
            except Exception:
                pass
        return ""


# ── iLink HTTP ───────────────────────────────────────────────────────────────

def _random_wechat_uin() -> str:
    val = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(val).encode()).decode()


def _build_headers(token: Optional[str], body: str) -> Dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _api_post(session, base_url: str, endpoint: str,
                    payload: Dict, token: Optional[str],
                    timeout_ms: int = API_TIMEOUT_MS) -> Dict:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    body = json.dumps({**payload, "base_info": {"channel_version": CHANNEL_VERSION}})
    headers = _build_headers(token, body)
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.post(url, data=body, headers=headers, timeout=timeout) as resp:
        raw = await resp.text()
        if not resp.ok:
            raise RuntimeError(f"iLink {endpoint} HTTP {resp.status}: {raw[:200]}")
        return json.loads(raw)


async def _api_get(session, base_url: str, endpoint: str,
                   timeout_ms: int = QR_POLL_TIMEOUT_MS) -> Dict:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.get(url, headers=headers, timeout=timeout) as resp:
        raw = await resp.text()
        if not resp.ok:
            raise RuntimeError(f"iLink GET {endpoint} HTTP {resp.status}: {raw[:200]}")
        return json.loads(raw)


# ── 扫码登录 ─────────────────────────────────────────────────────────────────

async def qr_login(data_dir: str, timeout_s: int = 480) -> Optional[Dict[str, str]]:
    """交互式扫码登录流程"""
    if not AIOHTTP_AVAILABLE:
        logger.error("aiohttp 未安装，无法扫码登录")
        return None

    store = CredentialStore(data_dir)
    async with aiohttp.ClientSession() as session:
        # Step 1: 获取二维码
        try:
            resp = await _api_get(session, ILINK_BASE_URL,
                                  f"{EP_GET_BOT_QR}?bot_type=3",
                                  QR_POLL_TIMEOUT_MS)
        except Exception as e:
            logger.error(f"获取二维码失败: {e}")
            return None

        qrcode = resp.get("qrcode", "")
        qrcode_url = resp.get("qrcode_img_content", "")
        if not qrcode:
            logger.error("响应中无二维码")
            return None

        print("\n" + "=" * 50)
        print("  请使用微信扫描以下二维码登录")
        print("=" * 50)
        print(f"\n  二维码链接: {qrcode_url}\n")

        # 终端 ASCII 二维码
        try:
            import qrcode as _qrcode
            qr = _qrcode.QRCode()
            qr.add_data(qrcode_url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            print("  （安装 qrcode 包可在终端显示二维码: pip install qrcode）")

        # Step 2: 轮询扫码状态
        deadline = time.time() + timeout_s
        current_base = ILINK_BASE_URL
        refresh_count = 0

        while time.time() < deadline:
            try:
                status_resp = await _api_get(
                    session, current_base,
                    f"{EP_GET_QR_STATUS}?qrcode={qrcode}",
                    QR_POLL_TIMEOUT_MS)
            except asyncio.TimeoutError:
                await asyncio.sleep(1)
                continue
            except Exception as e:
                logger.warning(f"扫码轮询错误: {e}")
                await asyncio.sleep(1)
                continue

            status = status_resp.get("status", "wait")

            if status == "wait":
                print(".", end="", flush=True)

            elif status == "scaned":
                print("\n已扫码，请在微信中确认...")

            elif status == "scaned_but_redirect":
                redirect_host = status_resp.get("redirect_host", "")
                if redirect_host:
                    current_base = f"https://{redirect_host}"
                    logger.info(f"IDC redirect: {current_base}")

            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    print("\n二维码多次过期，请重新运行")
                    return None
                print(f"\n二维码已过期，刷新中... ({refresh_count}/3)")
                try:
                    resp2 = await _api_get(session, ILINK_BASE_URL,
                                           f"{EP_GET_BOT_QR}?bot_type=3",
                                           QR_POLL_TIMEOUT_MS)
                    qrcode = resp2.get("qrcode", "")
                    qrcode_url = resp2.get("qrcode_img_content", "")
                    print(f"新二维码: {qrcode_url}")
                except Exception as e:
                    logger.error(f"刷新二维码失败: {e}")
                    return None

            elif status == "confirmed":
                bot_token = status_resp.get("bot_token", "")
                account_id = status_resp.get("ilink_bot_id", "")
                base_url = status_resp.get("baseurl", "") or ILINK_BASE_URL
                user_id = status_resp.get("ilink_user_id", "")
                if not account_id:
                    logger.error("登录确认但无 account_id")
                    return None

                # 持久化凭证
                store.save(account_id, bot_token, base_url, user_id)
                print(f"\n微信连接成功！account_id={account_id[:8]}...")
                return {
                    "account_id": account_id,
                    "token": bot_token,
                    "base_url": base_url,
                    "user_id": user_id,
                }

            await asyncio.sleep(1)

        print("\n登录超时")
        return None


# ── 微信适配器主类 ──────────────────────────────────────────────────────────

class WeChatAdapter:
    """
    DASHENG 微信适配器 — iLink Bot API

    使用方式：
      adapter = WeChatAdapter(data_dir, on_message_callback)
      adapter.start()  # 首次会触发扫码登录

    消息流：
      iLink 长轮询 → 解析消息 → on_message(PlatformMessage) → Agent处理
      → send_reply(PlatformReply) → sendMessage → 微信用户收到回复
    """

    MAX_MESSAGE_LENGTH = 4000

    def __init__(self, data_dir: str, on_message=None):
        self._data_dir = data_dir
        self._store = CredentialStore(data_dir)
        self._on_message = on_message

        self._account_id = ""
        self._token = ""
        self._base_url = ILINK_BASE_URL
        self._user_id = ""
        self._connected = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._consecutive_failures = 0
        self._seen_msgs: Dict[Any, float] = {}
        self._typing_cache: Dict[str, Tuple[str, float]] = {}

        # 去重窗口
        self._dedup_window = 300  # 5分钟

    @property
    def is_connected(self):
        return self._connected

    def get_config(self):
        return {
            "account_id": self._account_id[:8] + "..." if self._account_id else "未登录",
            "base_url": self._base_url,
            "connected": self._connected,
        }

    # ── 启动 ─────────────────────────────────────────────────────────────────

    def start(self):
        """启动微信适配器（同步入口，内部跑 asyncio）"""
        ok, msg = check_requirements()
        if not ok:
            logger.error(f"微信适配器依赖缺失: {msg}")
            logger.error("  安装: pip install aiohttp cryptography")
            return False

        self._loop = asyncio.new_event_loop()

        # 尝试加载已保存的凭证
        creds = self._store.load_active()
        if creds:
            self._account_id = creds["account_id"]
            self._token = creds["token"]
            self._base_url = creds.get("base_url", ILINK_BASE_URL)
            self._user_id = creds.get("user_id", "")
            logger.info(f"微信: 加载已保存凭证 account={self._account_id[:8]}...")
        else:
            # 首次登录 — 扫码
            logger.info("微信: 无已保存凭证，启动扫码登录...")
            result = self._loop.run_until_complete(qr_login(self._data_dir))
            if not result:
                logger.error("微信: 扫码登录失败")
                return False
            self._account_id = result["account_id"]
            self._token = result["token"]
            self._base_url = result.get("base_url", ILINK_BASE_URL)
            self._user_id = result.get("user_id", "")

        # 启动长轮询
        self._loop.run_until_complete(self._start_polling())
        return True

    def start_listener(self, on_message) -> bool:
        """Gateway 统一接口"""
        self._on_message = on_message
        return self.start()

    async def _start_polling(self):
        """启动长轮询循环"""
        self._session = aiohttp.ClientSession()
        self._connected = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("微信: 长轮询已启动")

    async def _poll_loop(self):
        """长轮询主循环"""
        sync_buf = self._store.load_sync_buf(self._account_id)

        while self._connected:
            try:
                resp = await _api_post(
                    self._session, self._base_url, EP_GET_UPDATES,
                    {"get_updates_buf": sync_buf},
                    self._token, LONG_POLL_TIMEOUT_MS)

                # 检查 session 过期
                ret = resp.get("ret", 0)
                if ret == SESSION_EXPIRED_ERRCODE:
                    logger.warning("微信: session 已过期，暂停10分钟...")
                    self._connected = False
                    await asyncio.sleep(600)
                    self._connected = True
                    continue

                # 更新 sync_buf
                new_buf = resp.get("get_updates_buf", "")
                if new_buf:
                    sync_buf = new_buf
                    self._store.save_sync_buf(self._account_id, sync_buf)

                # 处理消息
                msgs = resp.get("msgs", [])
                if msgs:
                    self._consecutive_failures = 0
                    for msg_data in msgs:
                        await self._process_message(msg_data)

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                self._consecutive_failures += 1
                logger.error(f"微信: getUpdates 错误 ({self._consecutive_failures}): {e}")
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.warning(f"微信: 连续失败{MAX_CONSECUTIVE_FAILURES}次，等待{BACKOFF_DELAY_S}s...")
                    await asyncio.sleep(BACKOFF_DELAY_S)
                    self._consecutive_failures = 0
                else:
                    await asyncio.sleep(RETRY_DELAY_S)

    async def _process_message(self, msg_data: Dict):
        """解析并派发一条消息"""
        from .models import PlatformMessage, PlatformReply

        msg_id = msg_data.get("message_id") or msg_data.get("client_id")
        now = time.time()

        # 去重
        if msg_id and msg_id in self._seen_msgs:
            if now - self._seen_msgs[msg_id] < self._dedup_window:
                return
        if msg_id:
            self._seen_msgs[msg_id] = now

        # 清理过期去重条目
        expired = [k for k, v in self._seen_msgs.items() if now - v > self._dedup_window]
        for k in expired:
            del self._seen_msgs[k]

        # 解析消息内容
        from_user_id = msg_data.get("from_user_id", "")
        to_user_id = msg_data.get("to_user_id", "")
        context_token = msg_data.get("context_token", "")

        # 保存 context_token
        if context_token and from_user_id:
            self._store.save_context_token(self._account_id, from_user_id, context_token)

        # 提取文本
        text = ""
        item_list = msg_data.get("item_list", [])
        for item in item_list:
            if item.get("type") == ITEM_TEXT:
                text += item.get("text_item", {}).get("text", "")
            # TODO: 图片/语音/文件处理

        if not text.strip():
            return

        # 判断群聊/私聊
        is_group = bool(msg_data.get("group_id"))
        at_me = False
        if is_group:
            # 群聊中检测 @机器人
            if f"@{self._user_id}" in text or f"@{self._account_id}" in text:
                at_me = True
                # 去掉 @ 部分
                text = text.replace(f"@{self._user_id}", "").replace(f"@{self._account_id}", "").strip()
            else:
                # 群聊未 @，忽略
                return

        chat_id = f"wc_{msg_data.get('group_id', from_user_id)}"

        platform_msg = PlatformMessage(
            platform="wechat",
            user_id=from_user_id,
            chat_id=chat_id,
            text=text,
            is_group=is_group,
            at_me=at_me,
            raw=msg_data,
        )

        logger.info(f"微信消息: {from_user_id} -> {text[:50]}...")

        if self._on_message:
            self._on_message(platform_msg)

    # ── 发送回复 ──────────────────────────────────────────────────────────────

    async def send_text(self, to_user_id: str, text: str, context_token: str = "") -> bool:
        """发送文本消息"""
        if not self._session or not self._connected:
            return False

        # 获取 context_token
        if not context_token:
            context_token = self._store.get_context_token(self._account_id, to_user_id) or ""

        # 超长消息分段
        chunks = self._split_message(text)

        for chunk in chunks:
            client_id = f"dasheng-wc-{uuid.uuid4().hex}"
            item_list = [{"type": ITEM_TEXT, "text_item": {"text": chunk}}]
            msg = {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": client_id,
                "message_type": MSG_TYPE_BOT,
                "message_state": MSG_STATE_FINISH,
                "item_list": item_list,
            }
            if context_token:
                msg["context_token"] = context_token

            try:
                await _api_post(self._session, self._base_url, EP_SEND_MESSAGE,
                                {"msg": msg}, self._token, API_TIMEOUT_MS)
            except Exception as e:
                logger.error(f"微信发送失败: {e}")
                return False

        return True

    def send_reply(self, reply) -> bool:
        """Gateway 统一接口 — 发送 PlatformReply"""
        from .models import PlatformReply

        user_id = reply.chat_id.replace("wc_", "") if reply.is_group else reply.user_id
        # 群聊时 to_user_id 用群 ID
        if reply.is_group:
            user_id = reply.chat_id.replace("wc_", "")

        if not self._loop or self._loop.is_closed():
            return False

        future = asyncio.run_coroutine_threadsafe(
            self.send_text(user_id, reply.text), self._loop)
        try:
            return future.result(timeout=30)
        except Exception as e:
            logger.error(f"微信发送异常: {e}")
            return False

    def _split_message(self, text: str) -> List[str]:
        """超长消息分段（4000字/段，段落边界切分）"""
        if len(text) <= self.MAX_MESSAGE_LENGTH:
            return [text]

        chunks = []
        while text:
            if len(text) <= self.MAX_MESSAGE_LENGTH:
                chunks.append(text)
                break
            # 在段落边界切分
            cut = text.rfind("\n", 0, self.MAX_MESSAGE_LENGTH)
            if cut < self.MAX_MESSAGE_LENGTH // 2:
                cut = text.rfind("。", 0, self.MAX_MESSAGE_LENGTH)
            if cut < self.MAX_MESSAGE_LENGTH // 2:
                cut = self.MAX_MESSAGE_LENGTH
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")

        return chunks

    # ── Typing 指示 ──────────────────────────────────────────────────────────

    async def send_typing(self, to_user_id: str, typing_ticket: str):
        """发送"正在输入"指示"""
        try:
            await _api_post(self._session, self._base_url, EP_SEND_TYPING,
                            {"ilink_user_id": to_user_id, "typing_ticket": typing_ticket, "status": 1},
                            self._token, CONFIG_TIMEOUT_MS)
        except Exception:
            pass

    # ── 停止 ─────────────────────────────────────────────────────────────────

    async def stop(self):
        self._connected = False
        if self._poll_task:
            self._poll_task.cancel()
        if self._session:
            await self._session.close()
        if self._loop and not self._loop.is_closed():
            self._loop.close()
        logger.info("微信: 已停止")
