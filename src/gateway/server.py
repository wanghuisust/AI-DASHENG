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
import urllib.parse
import urllib.error
from urllib.parse import urlparse
from http.server import HTTPServer, BaseHTTPRequestHandler

# 加载 .env 配置
from dotenv import load_dotenv
load_dotenv(override=True)

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


def _call_agent_stream(message: PlatformMessage, current_status: dict, status_lock: threading.Lock, cancel_event: threading.Event = None, on_status=None) -> str:
    """调用 Agent 流式接口（SSE），实时更新 current_status
    cancel_event: 如果被 set，中止 SSE 读取
    on_status: 回调函数 on_status(step, data)，收到 status 事件时调用，用于实时推送进度
    """
    import httpx
    url = f"{DASHENG_API}/v1/chat/stream"
    payload = {
        "message": message.text,
        "thread_id": message.chat_id,
    }

    try:
        with httpx.Client(timeout=httpx.Timeout(1800.0, connect=10.0)) as client:
            with client.stream("POST", url, json=payload) as resp:
                # 记录 resp 到 active_requests，以便取消时关闭
                with _active_requests_lock:
                    entry = _active_requests.get(message.chat_id)
                    if entry:
                        entry["sse_resp"] = resp

                response_text = ""
                logger.debug(f"[SSE] Connected to {url}, reading events...")
                event_type = ""
                for line_bytes in resp.iter_lines():
                    # 检查是否被取消
                    if cancel_event and cancel_event.is_set():
                        logger.info(f"[SSE] 请求被取消，停止读取 (chat_id={message.chat_id})")
                        return ""

                    line = line_bytes.decode("utf-8", errors="replace").strip() if isinstance(line_bytes, bytes) else line_bytes.strip()
                    if not line:
                        continue
                    if line.startswith("event: "):
                        event_type = line[7:]
                        logger.debug(f"[SSE] event: {event_type}")
                    elif line.startswith("data: "):
                        data_str = line[6:]
                        if not data_str:
                            continue
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            logger.debug(f"[SSE] non-json data: {data_str[:100]}")
                            continue

                        logger.debug(f"[SSE] {event_type}: {json.dumps(data, ensure_ascii=False)[:200]}")

                        if event_type == "text_delta":
                            # 逐 token 增量，拼接到 response_text
                            delta = data.get("delta", "")
                            full_so_far = data.get("text", "")
                            response_text += delta
                            # 回调给微信增量推送
                            if on_status:
                                try:
                                    on_status("streaming_text", {"delta": delta, "text": full_so_far})
                                except Exception as e:
                                    logger.warning(f"[on_status] streaming_text 回调异常: {e}")
                        elif event_type == "status":
                            step = data.get("step", "")
                            msg = data.get("message", "")
                            content = data.get("content", "")
                            tool = data.get("tool", "")
                            with status_lock:
                                if step == "thinking":
                                    current_status["message"] = content[:80] if content else "🤔 正在思考..."
                                elif step == "reasoning":
                                    short = content[:50] + ("..." if len(content) > 50 else "")
                                    current_status["message"] = f"💭 推理中: {short}"
                                elif step == "tool_call":
                                    _args = data.get("args", {})
                                    _action = {
                                        "terminal_execute": "执行命令", "search_files": "搜索文件",
                                        "read_file": "读取文件", "write_file": "写入文件",
                                        "patch": "编辑文件", "web_search": "搜索网页",
                                    }.get(tool, tool)
                                    _preview = ""
                                    if _args:
                                        for k in ("command", "pattern", "path", "query", "text", "name"):
                                            v = _args.get(k, "")
                                            if isinstance(v, str) and v:
                                                _preview = f"：\"{v[:30]}\""
                                                break
                                    current_status["message"] = f"⚙️ {_action}{_preview}…"
                                elif step == "tool_done":
                                    current_status["message"] = f"▶️ 继续下一步…"
                                current_status["step"] = step
                            # 回调：实时推送进度给客户端
                            if on_status:
                                try:
                                    on_status(step, data)
                                except Exception as e:
                                    logger.warning(f"[on_status] 回调异常: {e}")
                        elif event_type == "tool_started":
                            # 独立事件：工具开始执行，映射到 tool_call 步骤
                            tool = data.get("tool", "")
                            with status_lock:
                                _args = data.get("args", {})
                                _action = {
                                    "terminal_execute": "执行命令", "search_files": "搜索文件",
                                    "read_file": "读取文件", "write_file": "写入文件",
                                    "patch": "编辑文件", "web_search": "搜索网页",
                                }.get(tool, tool)
                                current_status["message"] = f"⚙️ {_action}…"
                                current_status["step"] = "tool_call"
                            if on_status:
                                try:
                                    on_status("tool_call", data)
                                except Exception as e:
                                    logger.warning(f"[on_status] tool_call 回调异常: {e}")
                        elif event_type == "tool_done":
                            # 独立事件：工具执行完毕
                            with status_lock:
                                current_status["message"] = "▶️ 继续下一步…"
                                current_status["step"] = "tool_done"
                            if on_status:
                                try:
                                    on_status("tool_done", data)
                                except Exception as e:
                                    logger.warning(f"[on_status] tool_done 回调异常: {e}")
                        elif event_type == "result":
                            response_text = data.get("response", "")
                            # 收到最终回复，清掉 thinking 类的 pending steps
                            if on_status:
                                try:
                                    on_status("final_result", data)
                                except Exception:
                                    pass
                            # result 是最终回复，收到后立即跳出
                            break
                        elif event_type == "done":
                            # 服务端显式结束标记
                            break
                        elif event_type == "error":
                            logger.error(f"[Agent SSE] 错误: {data.get('message', '')}")
                            response_text = f"抱歉，处理消息时出错: {data.get('message', '')}"
        return response_text
    except httpx.TimeoutException:
        logger.error(f"[Agent SSE] 连接超时: {url}")
        return "抱歉，Agent 服务暂时不可用，请稍后再试。"
    except httpx.HTTPStatusError as e:
        try:
            err_body = e.response.text[:200] if e.response else ""
            logger.error(f"[Agent SSE] HTTP {e.response.status_code}: {err_body}")
        except Exception:
            logger.error(f"[Agent SSE] HTTP {e.response.status_code}")
        return f"抱歉，处理消息时出错(HTTP {e.response.status_code})"
    except httpx.ConnectError:
        logger.error(f"[Agent SSE] 连接失败: {url}")
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


def _handle_slash_command(message: PlatformMessage, text: str):
    """处理斜杠命令：/new /reset /models /model /compact"""
    target = _wechat if message.platform == "wechat" else (qq_adapter if message.platform == "qq" else None)
    if not target:
        return

    cmd = text.lower().split()[0]
    args = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
    chat_id = message.chat_id

    def reply(text: str):
        try:
            r = PlatformReply(
                platform=message.platform,
                chat_id=chat_id,
                text=text,
                is_group=message.is_group,
                at_user=message.user_id,
            )
            target.send_reply(r)
        except Exception as e:
            logger.warning(f"[slash-cmd] 回复失败: {e}")

    try:
        if cmd in ("/new", "/reset"):
            # 删除 thread 历史，重新开始
            url = f"{DASHENG_API}/v1/threads/{urllib.parse.quote(chat_id, safe='')}"
            req = urllib.request.Request(url, method="DELETE")
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            logger.info(f"[slash-cmd] /new thread={chat_id}: {result}")
            # 同时清除 per-thread 模型覆盖
            try:
                set_url = f"{DASHENG_API}/v1/thread/model"
                payload = json.dumps({"thread_id": chat_id, "model": ""}).encode("utf-8")
                req2 = urllib.request.Request(set_url, data=payload,
                                              headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req2, timeout=5)
            except Exception:
                pass
            reply("🔄 已开启新会话")

        elif cmd == "/models":
            # 获取可用模型列表
            url = f"{DASHENG_API}/v1/models"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            models = result.get("models", [])
            default = result.get("default", "unknown")
            if not models:
                reply("📋 暂无可用模型")
            else:
                lines = ["📋 可用模型列表："]
                for m in models:
                    marker = " ← 当前" if m == default else ""
                    lines.append(f"  • {m}{marker}")
                reply("\n".join(lines))

        elif cmd == "/model":
            if not args:
                # 显示当前模型
                url = f"{DASHENG_API}/v1/status"
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = json.loads(resp.read().decode("utf-8"))
                current = status.get("agent", {}).get("model", "unknown")
                overrides = status.get("agent", {}).get("thread_model_overrides", {})
                thread_model = overrides.get(chat_id)
                if thread_model:
                    reply(f"🤖 当前模型：{thread_model}（本会话覆盖）\n默认模型：{current}")
                else:
                    reply(f"🤖 当前模型：{current}\n用 /model 模型名 切换")
            else:
                # 切换模型
                model_name = args
                url = f"{DASHENG_API}/v1/thread/model"
                payload = json.dumps({"thread_id": chat_id, "model": model_name}).encode("utf-8")
                req = urllib.request.Request(url, data=payload,
                                             headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                if result.get("status") == "ok":
                    reply(f"✅ 已切换模型：{model_name}")
                else:
                    err = result.get("message", "未知错误")
                    available = result.get("available", [])
                    avail_text = "\n可用模型：" + ", ".join(available) if available else ""
                    reply(f"❌ {err}{avail_text}")

        elif cmd == "/compact":
            # 手动压缩上下文
            url = f"{DASHENG_API}/v1/thread/compact"
            payload = json.dumps({"thread_id": chat_id}).encode("utf-8")
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            if result.get("status") == "ok":
                before = result.get("before", "?")
                after = result.get("after", "?")
                msg_text = result.get("message", "")
                if msg_text:
                    reply(f"📦 {msg_text}")
                else:
                    reply(f"📦 上下文已压缩：{before} → {after} 条消息")
            else:
                reply(f"❌ 压缩失败：{result.get('message', '未知错误')}")

        elif cmd == "/skills":
            # 列出已安装技能
            from skills import SkillManager
            sm = SkillManager(os.path.join(os.path.dirname(__file__), "..", "..", "data", "skills"))
            skills = sm.list_skills()
            if not skills:
                reply("📋 暂无已安装技能\n用 /skill install 技能名 安装\n用 /skill search 关键词 搜索 ClawHub")
            else:
                lines = ["📋 已安装技能："]
                for s in skills:
                    desc = s.get("description", "")
                    lines.append(f"  • {s['name']} — {desc}")
                lines.append(f"\n共 {len(skills)} 个技能")
                reply("\n".join(lines))

        elif cmd == "/skill":
            if not args:
                reply("🔧 技能管理命令：\n  /skills — 列出已安装技能\n  /skill install 技能名 — 从 ClawHub 安装\n  /skill install GitHub_URL — 从 GitHub 安装\n  /skill search 关键词 — 搜索 ClawHub\n  /skill remove 技能名 — 删除技能")
            else:
                # 解析子命令
                parts = args.split(None, 1)
                sub_cmd = parts[0].lower()
                sub_args = parts[1] if len(parts) > 1 else ""

                if sub_cmd == "install":
                    if not sub_args:
                        reply("❌ 请指定技能名或 GitHub URL\n例: /skill install github-code-review\n例: /skill install https://github.com/user/repo")
                    else:
                        from skills import SkillManager
                        sm = SkillManager(os.path.join(os.path.dirname(__file__), "..", "..", "data", "skills"))
                        # 判断来源
                        if sub_args.startswith("http") or "/" in sub_args.split()[0]:
                            result = sm.install_from_github(sub_args)
                        else:
                            result = sm.install_from_clawhub(sub_args)
                        reply(result.get("message", str(result)))

                elif sub_cmd == "search":
                    if not sub_args:
                        reply("❌ 请指定搜索关键词\n例: /skill search github")
                    else:
                        from skills import SkillManager
                        sm = SkillManager(os.path.join(os.path.dirname(__file__), "..", "..", "data", "skills"))
                        results = sm.search_clawhub(sub_args, limit=10)
                        if not results:
                            reply(f"🔍 未在 ClawHub 找到匹配 '{sub_args}' 的技能")
                        else:
                            lines = [f"🔍 ClawHub 搜索 '{sub_args}'："]
                            for r in results[:10]:
                                name = r.get("name", "?")
                                desc = r.get("description", "")
                                lines.append(f"  • {name} — {desc}")
                            lines.append("\n用 /skill install 技能名 安装")
                            reply("\n".join(lines))

                elif sub_cmd == "remove":
                    if not sub_args:
                        reply("❌ 请指定技能名\n例: /skill remove python-debug")
                    else:
                        from skills import SkillManager
                        sm = SkillManager(os.path.join(os.path.dirname(__file__), "..", "..", "data", "skills"))
                        if sm.delete(sub_args):
                            reply(f"✅ 已删除技能 '{sub_args}'")
                        else:
                            reply(f"❌ 技能 '{sub_args}' 不存在")

                else:
                    reply(f"❌ 未知的 skill 子命令: {sub_cmd}\n可用: install, search, remove")

        else:
            # 未知命令，不拦截——当普通消息处理
            # 取消同一 chat_id 的前一个请求
            had_active = _cancel_active_request(
                message.chat_id, message.platform, message.is_group, message.user_id
            )
            cancel_event = threading.Event()
            t = threading.Thread(target=_process_and_reply, args=(message, cancel_event), daemon=True)
            with _active_requests_lock:
                _active_requests[message.chat_id] = {"thread": t, "cancel_event": cancel_event}
            t.start()

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            err_data = json.loads(body)
            err_msg = err_data.get("message", body[:200])
        except Exception:
            err_msg = body[:200]
        logger.warning(f"[slash-cmd] HTTP {e.code}: {err_msg}")
        reply(f"❌ 命令执行失败：{err_msg}")
    except Exception as e:
        logger.warning(f"[slash-cmd] 异常: {e}")
        reply(f"❌ 命令执行出错：{e}")


def handle_message(message: PlatformMessage):
    """统一消息处理入口 — 斜杠命令在此拦截，普通消息异步处理"""
    logger.info(f"[{message.platform}] {message.user_id}@{message.chat_id}: {message.text[:80]}")

    # ── 斜杠命令拦截 ──
    text = message.text.strip()
    if text.startswith("/"):
        _handle_slash_command(message, text)
        return

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

        # ── 微信：发送"正在输入"指示 + 周期续命 ──
        def _wechat_typing_loop():
            """周期性发送 typing 指示（iLink typing 有时效，需每4秒续一次）"""
            count = 0
            while not progress_stop.wait(4):
                if cancel_event.is_set():
                    return
                count += 1
                try:
                    _wechat.send_typing(message.user_id, message.raw.get("context_token", ""))
                except Exception:
                    pass  # typing 失败不影响主流程

        wechat_typing_thread = None
        if message.platform == "wechat" and _wechat:
            # 立即发一次 typing
            try:
                _ctx_token = message.raw.get("context_token", "")
                logger.info(f"[wechat-typing] 立即发送: user={message.user_id[:20]}... context_token={'有' if _ctx_token else '无'}")
                _wechat.send_typing(message.user_id, _ctx_token)
            except Exception as e:
                logger.warning(f"[wechat-typing] 首次发送失败: {e}")
            wechat_typing_thread = threading.Thread(target=_wechat_typing_loop, daemon=True)
            wechat_typing_thread.start()

        # ── 发送开始处理通知（如果取消了前一个，_cancel_active_request 已发过通知） ──

        # ── 实时进度推送回调（微信/QQ 通用） ──
        _target = _wechat if message.platform == "wechat" else (qq_adapter if message.platform == "qq" else None)
        _progress_history = []           # 已推送的进度列表
        PROGRESS_FLUSH_INTERVAL = 5.0    # 合并发送间隔（秒）
        _pending_steps = []              # 待发送的步骤
        _last_flush_time = [0.0]         # 上次发送时间
        _flush_timer = [None]            # 定时器

        # ── 微信增量推送状态 ──
        _streaming_text_sent = [0]       # 已推送的文本字符数
        _streaming_last_push = [0.0]     # 上次推送时间戳
        STREAMING_PUSH_MIN_CHARS = 150   # 最少累积 150 字才推
        STREAMING_PUSH_MIN_INTERVAL = 4.0  # 两次推送最少间隔 4 秒

        # ── 工具自然语言描述映射（Hermes 风格：半思考半行动）──
        _TOOL_ACTION = {
            "terminal_execute": "执行命令",
            "search_files": "搜索文件",
            "read_file": "读取文件",
            "write_file": "写入文件",
            "patch": "编辑文件",
            "web_search": "搜索网页",
            "web_extract": "提取网页内容",
        }
        _TOOL_EMOJI = {
            "terminal_execute": "💻",
            "search_files": "🔍",
            "read_file": "📄",
            "write_file": "✏️",
            "patch": "✏️",
            "web_search": "🌐",
            "web_extract": "🌐",
        }

        def _tool_preview(tool: str, args: dict) -> str:
            """从工具参数中提取预览文本（类似 Hermes build_tool_preview）"""
            if not args:
                return ""
            if tool == "terminal_execute":
                cmd = args.get("command", "")
                return cmd[:40] if cmd else ""
            if tool == "search_files":
                pattern = args.get("pattern", "")
                return pattern[:40] if pattern else ""
            if tool == "read_file":
                path = args.get("path", "")
                return path[:50] if path else ""
            if tool == "write_file":
                path = args.get("path", "")
                return path[:50] if path else ""
            if tool == "patch":
                path = args.get("path", "")
                return path[:50] if path else ""
            if tool == "web_search":
                query = args.get("query", "")
                return query[:40] if query else ""
            # 通用：取第一个有值的字符串参数
            for key in ("query", "text", "command", "path", "name", "prompt"):
                val = args.get(key, "")
                if isinstance(val, str) and val:
                    return val[:40]
            return ""

        def _flush_progress():
            """合并发送积攒的步骤——Hermes 风格：自然语言描述，半思考半行动"""
            if not _pending_steps:
                return
            steps = _pending_steps.copy()
            _pending_steps.clear()
            _last_flush_time[0] = time.time()

            # ── 合并同类步骤 ──
            # 把连续的同类 tool_call/tool_done 合并
            merged = []
            for s in steps:
                last = merged[-1] if merged else None
                if last and last["type"] == s["type"] and last.get("tool") == s.get("tool"):
                    last["count"] = last.get("count", 1) + 1
                    if s.get("result"):
                        last["result"] = s["result"]
                else:
                    merged.append(s.copy())

            # ── 构造文本 ──
            # Hermes 风格：tool_call 显示"正在做什么"（自然语言+preview），
            # tool_done 不单独显示（已在 tool_call 中预告了动作），只在出错时提示
            lines = []
            for m in merged:
                if m["type"] == "tool_call":
                    cnt = m.get("count", 1)
                    tool = m.get("tool", "")
                    args = m.get("args", {})
                    action = _TOOL_ACTION.get(tool, tool)
                    emoji = _TOOL_EMOJI.get(tool, "⚙️")
                    preview = _tool_preview(tool, args)
                    if cnt > 1:
                        if preview:
                            lines.append(f"{emoji} {action}中 ×{cnt}：\"{preview}\"…")
                        else:
                            lines.append(f"{emoji} {action}中 ×{cnt}…")
                    else:
                        if preview:
                            lines.append(f"{emoji} {action}：\"{preview}\"…")
                        else:
                            lines.append(f"{emoji} {action}…")
                elif m["type"] == "tool_done":
                    # 正常完成不显示（避免刷屏），只显示错误
                    result = m.get("result", "")
                    is_error = result and any(kw in result[:200] for kw in ["[stderr]", "error", "Error", "不是内部或外部命令", "拒绝访问"])
                    if is_error:
                        result_lines = [l for l in result.split("\n") if l.strip()]
                        err_line = result_lines[0][:30] if result_lines else "执行出错"
                        lines.append(f"⚠️ {err_line}")
                elif m["type"] == "thinking":
                    lines.append(f"💭 {m.get('text', '')[:80]}")

            text = "\n".join(lines)
            if not text:
                return

            _progress_history.append(text)
            logger.info(f"[{message.platform}-progress] 合并发送: {text[:80]}")
            try:
                progress_reply = PlatformReply(
                    platform=message.platform,
                    chat_id=message.chat_id,
                    text=text,
                    is_group=message.is_group,
                    at_user=message.user_id,
                    raw={"is_progress": True},
                )
                _target.send_reply(progress_reply)
            except Exception as e:
                logger.warning(f"[{message.platform}-progress] 发送失败: {e}")

        def _on_status(step: str, data: dict):
            """SSE status 回调：攒步骤，定时合并推送给客户端（微信/QQ）"""
            if not _target:
                return
            if cancel_event.is_set():
                return

            now = time.time()
            tool = data.get("tool", "")
            content = data.get("content", "")

            # ── 收到最终回复，清掉 thinking 避免和 result 重复 ──
            if step == "final_result":
                # 只保留 tool 类步骤，清掉 thinking（因为 result 会包含完整回复）
                _pending_steps[:] = [s for s in _pending_steps if s["type"] in ("tool_call", "tool_done")]
                # 如果还有 tool 步骤没发，立即 flush
                if _pending_steps:
                    _flush_progress()
                # 推送 streaming 尾部文本（最后一段可能不够 150 字但也是有效输出）
                # 注意：result 事件包含完整回复，微信最终会再发一次完整消息
                # 这里只推送 streaming 期间漏掉的尾部，避免用户看到断档
                # 但如果 streaming 推送总量已经覆盖了回复主体，最终回复可以跳过（在下面处理）
                return

            # ── streaming_text：LLM 逐 token 输出，增量推送给客户端 ──
            if step == "streaming_text":
                full_text = data.get("text", "")
                if not full_text:
                    return
                sent = _streaming_text_sent[0]
                new_chars = len(full_text) - sent
                elapsed = now - _streaming_last_push[0]
                # 条件1: 新增字符足够多（>=150字）
                # 条件2: 距上次推送已过 4 秒（避免频率过高被限流）
                should_push = new_chars >= STREAMING_PUSH_MIN_CHARS and elapsed >= STREAMING_PUSH_MIN_INTERVAL
                # 在句号/换行处优先切割（避免断句）
                if should_push and new_chars > 0:
                    # 找最近的一个句子结束点（句号、问号、叹号、换行）
                    cut_pos = len(full_text)
                    # 从 sent + STREAMING_PUSH_MIN_CHARS 开始往前找断句点
                    search_start = min(sent + STREAMING_PUSH_MIN_CHARS, len(full_text))
                    for i in range(search_start, min(search_start + 50, len(full_text))):
                        if full_text[i] in "。！？\n":
                            cut_pos = i + 1
                            break
                    # 提取新片段
                    chunk_text = full_text[sent:cut_pos].strip()
                    if chunk_text:
                        try:
                            reply = PlatformReply(
                                platform=message.platform,
                                chat_id=message.chat_id,
                                text=chunk_text,
                                is_group=message.is_group,
                                at_user=message.user_id,
                                raw={"is_progress": True, "is_streaming": True},
                            )
                            _target.send_reply(reply)
                            _streaming_text_sent[0] = cut_pos
                            _streaming_last_push[0] = now
                            logger.debug(f"[{message.platform}-streaming] 推送 {len(chunk_text)} 字, 累计 {cut_pos}/{len(full_text)}")
                        except Exception as e:
                            logger.warning(f"[{message.platform}-streaming] 推送失败: {e}")
                return

            # ── 构造步骤条目 ──
            if step == "tool_call":
                _pending_steps.append({"type": "tool_call", "tool": tool, "args": data.get("args", {})})
            elif step == "tool_done":
                # 工具结果，取前500字用于智能摘要（不能只取100字，否则行数统计不准）
                tool_content = data.get("content", "") or data.get("message", "")
                short_result = tool_content[:500].strip() if tool_content else ""
                _pending_steps.append({"type": "tool_done", "tool": tool, "result": short_result})
            elif step in ("thinking", "reasoning"):
                if not content or len(content) < 5:
                    return
                # 过滤纯意图声明
                import re
                _skip_patterns = [r"^(好的[，。]?\s*(主人|亲)?[，。]?\s*(我来|让我|我帮你))",
                                  r"^(我来|让我|我来帮你|我来查|让我查)"]
                if any(re.search(p, content) for p in _skip_patterns) and len(content) < 30:
                    return
                _pending_steps.append({"type": "thinking", "text": content[:80]})
            else:
                return

            # ── 5秒定时器：到了就合并发送 ──
            # 取消上一个定时器，重新计时
            if _flush_timer[0]:
                try:
                    _flush_timer[0].cancel()
                except Exception:
                    pass
            _flush_timer[0] = threading.Timer(PROGRESS_FLUSH_INTERVAL, _flush_progress)
            _flush_timer[0].daemon = True
            _flush_timer[0].start()

            # ── 如果积攒超过 8 条，立即发送（避免积压太久） ──
            if len(_pending_steps) >= 8:
                if _flush_timer[0]:
                    try:
                        _flush_timer[0].cancel()
                    except Exception:
                        pass
                _flush_progress()

        # ── 调用 Agent 流式接口 ──
        logger.info(f"[{message.platform}] 开始调用 Agent（流式）...")
        reply_text = _call_agent_stream(message, current_status, status_lock, cancel_event, on_status=_on_status)

        # ── flush 剩余的进度步骤（最后一批可能还在 pending） ──
        if _flush_timer[0]:
            try:
                _flush_timer[0].cancel()
            except Exception:
                pass
        if _pending_steps:
            _flush_progress()

        # ── 停止 typing loop ──
        progress_stop.set()  # 通知 typing loop 停止
        if wechat_typing_thread:
            wechat_typing_thread.join(timeout=3)

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

        if _target:
            # streaming 增量推送：避免和已推送内容重复
            streaming_sent = _streaming_text_sent[0]
            if streaming_sent > 0:
                # 已有 streaming 推送，只发尚未推送的部分
                remaining = reply_text[streaming_sent:].strip()
                if remaining:
                    try:
                        tail_reply = PlatformReply(
                            platform=message.platform,
                            chat_id=message.chat_id,
                            text=remaining,
                            is_group=message.is_group,
                            at_user=message.user_id,
                        )
                        _target.send_reply(tail_reply)
                        logger.info(f"[{message.platform}-streaming] 推送剩余 {len(remaining)} 字（streaming 已推送 {streaming_sent}/{len(reply_text)}）")
                    except Exception as e:
                        logger.warning(f"[{message.platform}-streaming] 剩余推送失败: {e}")
                else:
                    logger.info(f"[{message.platform}-streaming] 完整已推送 {streaming_sent}/{len(reply_text)}，跳过最终消息")
            else:
                # 无 streaming 推送，发完整消息
                try:
                    _target.send_reply(reply)
                except Exception as e:
                    logger.error(f"{message.platform}发送回复异常: {e}")
        else:
            logger.warning(f"{message.platform}消息但适配器未启动，无法回复")
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
    _wechat = None
    wechat_ok = False
    WECHAT_ENABLED = os.getenv("WECHAT_ENABLED", "true").lower() == "true"
    if WECHAT_ENABLED:
        try:
            _wechat = wechat_adapter.WeChatAdapter(DATA_DIR)
            wechat_ok = _wechat.start_listener(handle_message)
            if wechat_ok:
                logger.info("WeChat iLink Bot connected")
            else:
                logger.warning("WeChat not connected (扫码登录失败，继续运行QQ通道)")
                _wechat = None  # 避免后续调用失败的适配器
        except Exception as e:
            logger.error(f"WeChat 启动异常: {e}，跳过微信通道")
            _wechat = None
    else:
        logger.info("WeChat disabled (WECHAT_ENABLED != true)")

    # ── QQ：Bot 官方 API v2 ──────────────────────────────────────────
    # QQ start_listener 是阻塞式（内含重连循环），必须在独立线程中运行
    qq_ok = False
    QQ_ENABLED = os.getenv("QQ_ENABLED", "true").lower() == "true"
    if QQ_ENABLED:
        try:
            ok, msg = qq_adapter.check_requirements()
            if ok:
                # 预先获取 token 验证配置是否正确
                _qq_token_mgr = qq_adapter.TokenManager(
                    os.getenv("QQ_APP_ID", ""),
                    os.getenv("QQ_APP_SECRET", ""),
                    os.getenv("QQ_IS_SANDBOX", "false").lower() in ("true", "1", "yes"),
                )
                _test_token = _qq_token_mgr.get_token()
                if _test_token:
                    qq_ok = True
                    # 在后台线程启动 QQ 适配器（start_listener 内含重连循环，会阻塞）
                    qq_thread = threading.Thread(
                        target=qq_adapter.start_listener,
                        args=(handle_message,),
                        daemon=True,
                        name="qq-listener",
                    )
                    qq_thread.start()
                    logger.info("QQ Bot API 启动中（后台线程）")
                else:
                    logger.warning("QQ not connected (access_token 获取失败)，继续运行微信通道")
            else:
                logger.warning(f"QQ 依赖缺失: {msg}，继续运行微信通道")
        except Exception as e:
            logger.error(f"QQ 启动异常: {e}，跳过QQ通道")
    else:
        logger.info("QQ disabled (QQ_ENABLED != true)")

    # ── HTTP Server ───────────────────────────────────────────────────────
    server = HTTPServer((host, port), GatewayHandler)
    logger.info(f"DASHENG Gateway: {host}:{port}")
    if _wechat:
        logger.info(f"  WeChat: iLink Bot API ✓ (已连接)")
    elif WECHAT_ENABLED:
        logger.info(f"  WeChat: iLink Bot API ✗ (连接失败)")
    else:
        logger.info(f"  WeChat: disabled")
    if qq_ok:
        logger.info(f"  QQ:     Bot API v2 ✓ (已连接)")
    else:
        logger.info(f"  QQ:     Bot API v2 ✗ (未连接)")
    logger.info(f"  Agent:  {DASHENG_API}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Gateway stopped")
        server.server_close()


if __name__ == "__main__":
    main()
