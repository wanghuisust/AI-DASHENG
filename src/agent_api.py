"""DASHENG Agent API — 独立的 Agent 核心 API 服务

架构：
  Agent API (:8900)   ← 纯 API，无 UI
  Gateway  (:9090)    → 调 Agent API 处理消息
  WebServer(:7860)    → 调 Agent API + Dashboard UI

端点：
  POST /v1/chat           — 同步对话
  POST /v1/chat/stream    — SSE 流式对话
  GET  /v1/status         — 模型 + Gateway 存活状态
  GET  /v1/threads        — 会话列表
  GET  /v1/threads/:id    — 会话消息
  DELETE /v1/threads/:id  — 删除会话
  POST /v1/threads        — 创建会话
"""

import sys
import os
import json
import uuid
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from pathlib import Path

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path, override=True)

from graph import build_graph
from persistence import get_store
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage
from langchain_openai import ChatOpenAI

# ── 请求取消机制 ──────────────────────────────────────────────
# thread_id → Event：当 set 时，对应的流式请求应停止
_cancel_events = {}
_cancel_lock = threading.Lock()

# ── 全局 Agent ──────────────────────────────────────────────

print("Loading AI-DASHENG Agent...")
graph = build_graph()
store = get_store()
print("Agent ready!")

# 对话状态 — 按 thread_id 隔离
thread_messages: dict[str, list] = {}
thread_locks: dict[str, threading.Lock] = {}
_lock_acquire_time: dict[str, float] = {}  # 记录锁获取时间，防死锁
_global_lock = threading.Lock()

# Per-thread 模型配置（thread_id → model_name）
_thread_models: dict[str, str] = {}

# 可用模型列表（从 API 动态获取，启动时初始化）
_AVAILABLE_MODELS: list[str] = []


def _fetch_available_models():
    """启动时从 API 获取可用模型列表"""
    global _AVAILABLE_MODELS
    try:
        _env_path = Path(__file__).resolve().parent.parent / ".env"
        load_dotenv(_env_path, override=True)
        base_url = os.getenv("OPENAI_BASE_URL", "")
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not base_url or not api_key:
            return
        import urllib.request
        req = urllib.request.Request(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m["id"] for m in data.get("data", []) if "id" in m]
            _AVAILABLE_MODELS = sorted(models)
            print(f"[MODELS] 可用模型: {_AVAILABLE_MODELS}", flush=True)
    except Exception as e:
        print(f"[MODELS] 获取模型列表失败: {e}", flush=True)


_fetch_available_models()

# 启动时间
_start_time = time.time()


def get_messages(thread_id: str) -> list:
    if thread_id not in thread_messages:
        # 尝试从 SQLite 恢复历史消息
        db_msgs = store.get_messages(thread_id, limit=200)
        if db_msgs:
            restored = []
            # 收集所有有效的 tool_call_id（来自 AI 消息的 tool_calls）
            valid_tool_ids = set()
            for m in db_msgs:
                if m.get("role") == "ai":
                    tc_str = m.get("tool_calls", "")
                    if tc_str:
                        try:
                            tcs = json.loads(tc_str)
                            for tc in tcs:
                                # 从 tool_calls 中提取 id（如果有的话）
                                tc_id = tc.get("id", "")
                                if tc_id:
                                    valid_tool_ids.add(tc_id)
                        except (json.JSONDecodeError, TypeError):
                            pass

            for m in db_msgs:
                role = m.get("role", "")
                content = m.get("content", "")
                if role == "human":
                    restored.append(HumanMessage(content=content))
                elif role == "ai":
                    # 恢复 AIMessage 时带上 tool_calls，否则后续 ToolMessage 会变成孤立消息
                    ai_kwargs = {"content": content}
                    tc_str = m.get("tool_calls", "")
                    if tc_str:
                        try:
                            tcs = json.loads(tc_str)
                            if tcs:
                                ai_kwargs["tool_calls"] = tcs
                        except (json.JSONDecodeError, TypeError):
                            pass
                    restored.append(AIMessage(**ai_kwargs))
                elif role == "tool":
                    tool_call_id = m.get("tool_call_id", "")
                    # 跳过孤立的 tool 消息（tool_call_id 不在任何 AI 消息的 tool_calls 中）
                    if tool_call_id and tool_call_id not in valid_tool_ids:
                        print(f"[RESTORE] Skipping orphan tool message: {tool_call_id}")
                        continue
                    if not tool_call_id:
                        tool_call_id = f"restored_{m.get('tool_name', 'unknown')}_{int(time.time())}"
                    restored.append(ToolMessage(content=content, name=m.get("tool_name", ""), tool_call_id=tool_call_id))
            thread_messages[thread_id] = restored
            print(f"[RESTORE] Restored {len(restored)} messages for thread {thread_id}")
        else:
            thread_messages[thread_id] = []
    return thread_messages[thread_id]


def get_thread_lock(thread_id: str) -> threading.Lock:
    """每个 thread 一个锁，防止同一 thread 并发写入"""
    with _global_lock:
        if thread_id not in thread_locks:
            thread_locks[thread_id] = threading.Lock()
        return thread_locks[thread_id]


def acquire_thread_lock(thread_id: str, timeout: float = 300) -> bool:
    """获取锁，带超时和死锁保护（持有超过 300s 自动释放）"""
    lock = get_thread_lock(thread_id)
    # 死锁保护：如果锁被持有超过 300s，强制释放
    if thread_id in _lock_acquire_time:
        held_duration = time.time() - _lock_acquire_time[thread_id]
        if held_duration > 300:
            print(f"[WARN] Lock for {thread_id} held for {held_duration:.0f}s, force releasing")
            try:
                lock.release()
            except RuntimeError:
                pass  # 锁已经不在持有状态
            del _lock_acquire_time[thread_id]
    acquired = lock.acquire(timeout=timeout)
    if acquired:
        _lock_acquire_time[thread_id] = time.time()
    return acquired


def release_thread_lock(thread_id: str):
    """释放锁"""
    lock = get_thread_lock(thread_id)
    _lock_acquire_time.pop(thread_id, None)
    try:
        lock.release()
    except RuntimeError:
        pass


def save_ai_messages(thread_id: str, new_messages: list):
    for msg in new_messages:
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", "") or ""
        if isinstance(content, list):
            content = str(content)
        tool_calls_str = ""
        tool_name = ""
        tool_call_id = ""
        if role == "ai" and hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_calls_str = json.dumps(
                [{"name": tc["name"], "args": tc["args"], "id": tc.get("id", "")} for tc in msg.tool_calls],
                ensure_ascii=False,
            )
        elif role == "tool":
            tool_name = getattr(msg, "name", "")
            tool_call_id = getattr(msg, "tool_call_id", "")
        store.save_message(thread_id, role, content, tool_calls_str, tool_name, tool_call_id)


def extract_response(messages: list, prev_count: int) -> tuple[str, list]:
    response_text = ""
    tool_calls_info = []
    for msg in messages[prev_count:]:
        if hasattr(msg, "type") and msg.type == "ai":
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls_info.append({"name": tc["name"], "args": tc["args"]})
            if msg.content:
                response_text = msg.content
    return response_text, tool_calls_info


def _fix_empty_intent_response(response_text: str, messages: list) -> str:
    """检测空意图回复（只有'我来帮你查'之类的声明没有实际内容），自动用无工具 LLM 总结。
    返回修正后的 response_text。
    """
    import re
    if not response_text:
        print(f"[EMPTY-RESULT] 完全无回复，自动总结", flush=True)
        try:
            return _summarize_without_tools(messages)
        except Exception as se:
            print(f"[EMPTY-RESULT] 总结失败: {se}", flush=True)
            return "抱歉，处理完成但未能生成回复，请重试。"

    EMPTY_INTENT_PATTERNS = [
        r"^(我来|让我|我来帮你|让我来|我将|好的[，。]?\s*(我来|让我|我帮你))",
        r"^(我帮你|我来为你|我来查|让我查|让我看|我来看看|我来找)",
        r"^(好的[，。]?\s*(主人|亲|先生|女士|用户)?[，。]?\s*(我来|让我|我帮你))",
        r"^(好的[，。]?\s*我再)",
    ]
    _has_substance = any(kw in response_text for kw in [
        "：\n", ":\n", "```", "1.", "2.", "G:\\", "C:\\",
        "详细如下", "列出如下", "找到以下", "结果如下",
        # 新增：有具体动作意图的关键词（如调工具前的声明不算空意图）
        "安装", "搜索", "查找", "执行", "运行", "读取", "写入",
        "克隆", "拉取", "配置", "创建", "删除", "下载",
    ]) or len(response_text) >= 80
    is_empty_intent = not _has_substance
    if is_empty_intent:
        print(f"[EMPTY-RESULT] 回复是空意图: '{response_text[:60]}'，自动总结", flush=True)
        try:
            return _summarize_without_tools(messages)
        except Exception as se:
            print(f"[EMPTY-RESULT] 总结失败: {se}", flush=True)
            return response_text + "\n\n（Agent 执行了多步操作但未能生成完整总结，请尝试更具体的提问。）"

    return response_text


def _summarize_without_tools(messages: list) -> str:
    """到达 recursion_limit 或循环检测后，去掉工具做一次纯文本 LLM 请求让模型总结。
    参考 Hermes Agent 的 handle_max_iterations 机制。
    """
    from graph import create_llm, SYSTEM_PROMPT
    from memory import Memory
    
    # 创建不带工具的 LLM（纯文本模式）
    llm_no_tools = create_llm()
    
    # 构建总结请求：压缩工具调用为摘要，保留关键信息
    summary_messages = [SystemMessage(content=SYSTEM_PROMPT)]
    
    memory = Memory()
    memory_context = memory.get_context()
    if memory_context:
        summary_messages[0] = SystemMessage(content=SYSTEM_PROMPT + f"\n\n{memory_context}")
    
    for msg in messages:
        role = getattr(msg, "type", None)
        if role == "human":
            summary_messages.append(HumanMessage(content=msg.content if isinstance(msg.content, str) else str(msg.content)))
        elif role == "ai":
            content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
            has_tool_calls = hasattr(msg, "tool_calls") and msg.tool_calls
            if has_tool_calls:
                # 压缩：把 tool_calls 摘要为文本
                tc_names = [tc["name"] for tc in msg.tool_calls]
                tc_summary = f"[调用工具: {', '.join(tc_names)}]"
                if content:
                    summary_messages.append(AIMessage(content=f"{content[:200]} {tc_summary}"))
                else:
                    summary_messages.append(AIMessage(content=tc_summary))
            elif content:
                summary_messages.append(AIMessage(content=content[:500]))
        elif role == "tool":
            # 保留工具结果（截断到 300 字避免太长）
            tool_content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
            tool_name = getattr(msg, "name", "tool")
            summary_messages.append(AIMessage(content=f"[{tool_name}结果]: {tool_content[:300]}"))
    
    # 追加总结请求
    summary_messages.append(HumanMessage(
        content="你已经执行了多步操作但达到了迭代上限或被循环检测终止。请总结你已经完成的工作和发现，直接回复用户，不要再调用任何工具。"
    ))
    
    # 截断到合理长度
    total_chars = sum(len(str(m.content)) for m in summary_messages if hasattr(m, 'content'))
    if total_chars > 30000:
        # 只保留 system + 最后几条 + 总结请求
        summary_messages = [summary_messages[0]] + summary_messages[-5:]
    
    # 创建短超时 LLM 实例（30s），防止无工具总结卡住
    from graph import create_llm
    import os
    from dotenv import load_dotenv
    from pathlib import Path
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(_env_path, override=True)
    llm_short = ChatOpenAI(
        model=os.getenv("MODEL_NAME", "gpt-4o-mini"),
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.getenv("OPENAI_API_KEY"),
        temperature=0.3,
        request_timeout=30,
    )
    
    try:
        response = llm_short.invoke(summary_messages)
        return response.content if response.content else "抱歉，处理步骤过多已自动停止。请尝试更简单的提问方式。"
    except Exception as e:
        print(f"[RECURSION-LIMIT] 无工具总结超时或失败: {e}", flush=True)
        return "抱歉，处理步骤过多已自动停止。请尝试更简单的提问方式。"


# ── Gateway 状态检查 ────────────────────────────────────────

def check_gateway_status() -> dict:
    """检查 Gateway 是否存活"""
    import urllib.request
    import urllib.error
    gateway_url = os.getenv("GATEWAY_STATUS_URL", "http://127.0.0.1:9090/health")
    try:
        req = urllib.request.Request(gateway_url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"connected": True, "info": data}
    except Exception:
        return {"connected": False, "info": None}


# ── HTTP Handler ─────────────────────────────────────────────

class AgentAPIHandler(BaseHTTPRequestHandler):
    _log_f = None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # GET /v1/status — 模型 + Gateway 状态
        if path == "/v1/status":
            self._handle_status()
        # GET /v1/models — 可用模型列表
        elif path == "/v1/models":
            self._handle_list_models()
        # GET /v1/threads — 会话列表
        elif path == "/v1/threads":
            threads = store.list_threads()
            self.send_json({"threads": threads})
        # GET /v1/threads/:id — 会话消息
        elif path.startswith("/v1/threads/"):
            tid = path.split("/v1/threads/")[1].split("?")[0]
            qs = parse_qs(parsed.query)
            limit = int(qs.get("limit", [100])[0])
            msgs = store.get_messages(tid, limit)
            self.send_json({"thread_id": tid, "messages": msgs})
        # 兼容旧接口
        elif path == "/api/threads":
            threads = store.list_threads()
            self.send_json({"threads": threads})
        elif path == "/api/history":
            qs = parse_qs(parsed.query)
            tid = qs.get("thread_id", ["default"])[0]
            msgs = store.get_messages(tid)
            self.send_json({"messages": msgs})
        elif path == "/health":
            self.send_json({"status": "ok", "uptime": int(time.time() - _start_time)})
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)

        # POST /v1/chat/stream — SSE 流式
        if parsed.path == "/v1/chat/stream":
            self._handle_stream_chat()
        # POST /v1/chat — 同步
        elif parsed.path == "/v1/chat":
            self._handle_sync_chat()
        # POST /v1/chat/cancel — 取消正在处理的请求
        elif parsed.path == "/v1/chat/cancel":
            self._handle_cancel()
        # GET /v1/models — 可用模型列表
        elif parsed.path == "/v1/models":
            self._handle_list_models()
        # POST /v1/thread/model — 设置 thread 模型
        elif parsed.path == "/v1/thread/model":
            self._handle_set_thread_model()
        # POST /v1/thread/compact — 手动压缩 thread 上下文
        elif parsed.path == "/v1/thread/compact":
            self._handle_compact_thread()
        # POST /v1/threads — 创建会话
        elif parsed.path == "/v1/threads":
            self._handle_create_thread()
        # DELETE /v1/threads/:id — 删除会话（用 POST 模拟）
        # 兼容旧接口
        elif parsed.path == "/api/chat/stream":
            self._handle_stream_chat()
        elif parsed.path == "/api/chat":
            self._handle_sync_chat()
        elif parsed.path == "/api/new_thread":
            self._handle_create_thread()
        else:
            self.send_error(404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/v1/threads/"):
            tid = parsed.path.split("/v1/threads/")[1]
            store.delete_thread(tid)
            if tid in thread_messages:
                del thread_messages[tid]
            self.send_json({"status": "ok", "deleted": tid})
        else:
            self.send_error(404)

    # ── 取消请求 ─────────────────────────────────────────────

    def _handle_cancel(self):
        """POST /v1/chat/cancel — 取消指定 thread_id 的正在处理的请求"""
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(body) if body else {}
            thread_id = data.get("thread_id", "")
            if not thread_id:
                self.send_json({"status": "error", "message": "thread_id required"}, 400)
                return
            with _cancel_lock:
                evt = _cancel_events.get(thread_id)
                if evt:
                    evt.set()
                    del _cancel_events[thread_id]
                    self.send_json({"status": "cancelled", "thread_id": thread_id})
                else:
                    self.send_json({"status": "not_found", "thread_id": thread_id})
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)}, 500)

    # ── 模型管理 ─────────────────────────────────────────────

    def _handle_list_models(self):
        """GET /v1/models — 返回可用模型列表"""
        # 刷新模型列表（API 可能新增模型）
        _fetch_available_models()
        default_model = os.getenv("MODEL_NAME", "unknown")
        self.send_json({
            "models": _AVAILABLE_MODELS,
            "default": default_model,
        })

    def _handle_set_thread_model(self):
        """POST /v1/thread/model — 设置指定 thread 的模型
        Body: {"thread_id": "...", "model": "agnes-2.0-flash"}
        """
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(body) if body else {}
            thread_id = data.get("thread_id", "")
            model = data.get("model", "")
            if not thread_id:
                self.send_json({"status": "error", "message": "thread_id required"}, 400)
                return
            if not model:
                # 不指定 model 则恢复默认
                _thread_models.pop(thread_id, None)
                self.send_json({"status": "ok", "thread_id": thread_id, "model": "default"})
                return
            # 验证模型是否在可用列表中
            if _AVAILABLE_MODELS and model not in _AVAILABLE_MODELS:
                self.send_json({"status": "error", "message": f"模型 '{model}' 不在可用列表中", "available": _AVAILABLE_MODELS}, 400)
                return
            _thread_models[thread_id] = model
            print(f"[MODEL] thread={thread_id} 切换为 {model}", flush=True)
            self.send_json({"status": "ok", "thread_id": thread_id, "model": model})
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)}, 500)

    def _handle_compact_thread(self):
        """POST /v1/thread/compact — 手动压缩指定 thread 的上下文
        Body: {"thread_id": "..."}
        """
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(body) if body else {}
            thread_id = data.get("thread_id", "")
            if not thread_id:
                self.send_json({"status": "error", "message": "thread_id required"}, 400)
                return
            messages = get_messages(thread_id)
            before = len(messages)
            if before <= 6:
                self.send_json({"status": "ok", "message": "消息太少，无需压缩", "before": before, "after": before})
                return
            from context_compress import compress_messages
            from graph import create_llm
            _compact_llm = create_llm()
            # 创建不带工具的摘要 LLM（避免摘要时触发工具调用）
            from langchain_openai import ChatOpenAI
            _summary_llm = None
            try:
                _summary_llm = ChatOpenAI(
                    model=_compact_llm.model if hasattr(_compact_llm, 'model') else None,
                    base_url=_compact_llm.openai_api_base if hasattr(_compact_llm, 'openai_api_base') else None,
                    api_key=_compact_llm.openai_api_key if hasattr(_compact_llm, 'openai_api_key') else None,
                    temperature=0.1, request_timeout=30, max_retries=2,
                )
            except Exception:
                pass
            compressed = compress_messages(messages, llm=_summary_llm)
            thread_messages[thread_id] = compressed
            after = len(compressed)
            # 同步到持久化
            save_ai_messages(thread_id, [])
            print(f"[COMPACT] thread={thread_id} 压缩: {before} → {after} 条消息", flush=True)
            self.send_json({"status": "ok", "before": before, "after": after})
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)}, 500)

    # ── 状态 ─────────────────────────────────────────────

    def _handle_status(self):
        model_name = os.getenv("MODEL_NAME", "unknown")
        base_url = os.getenv("OPENAI_BASE_URL", "")
        context_length = int(os.getenv("MODEL_CONTEXT_LENGTH", "50000"))
        enable_tools = os.getenv("ENABLE_TOOLS", "true").lower() == "true"

        gateway_status = check_gateway_status()

        # per-thread 模型覆盖
        thread_model_overrides = {tid: m for tid, m in _thread_models.items()}

        self.send_json({
            "agent": {
                "status": "running",
                "uptime": int(time.time() - _start_time),
                "model": model_name,
                "base_url": base_url,
                "context_length": context_length,
                "enable_tools": enable_tools,
                "active_threads": len(thread_messages),
                "available_models": _AVAILABLE_MODELS,
                "thread_model_overrides": thread_model_overrides,
            },
            "gateway": gateway_status,
        })

    # ── 同步对话 ─────────────────────────────────────────

    def _handle_sync_chat(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            raw = body.decode("utf-8", errors="replace").strip()
            if raw.startswith("{") or raw.startswith("["):
                data = json.loads(raw)
            else:
                data = {"message": raw}
            user_msg = data.get("message", "").strip() if isinstance(data, dict) else data.strip()
            thread_id = data.get("thread_id", "default") if isinstance(data, dict) else "default"
        except (json.JSONDecodeError, KeyError):
            self.send_json({"error": "Invalid request"}, 400)
            return

        if not user_msg:
            self.send_json({"error": "Message is empty"}, 400)
            return

        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] POST /v1/chat message={user_msg[:80]} thread={thread_id}")

        try:
            if not acquire_thread_lock(thread_id, timeout=1800):
                self.send_json({"response": "当前会话正在处理中，请稍后再试", "thread_id": thread_id})
                return
            try:
                messages = get_messages(thread_id)
                prev_count = len(messages)
                store.create_thread(thread_id)
                messages.append(HumanMessage(content=user_msg))

                invoke_result = [None]
                invoke_error = [None]

                # 读取 per-thread 模型配置
                _thread_model = _thread_models.get(thread_id)
                _sync_graph = graph  # 默认用全局 graph
                if _thread_model:
                    from graph import create_llm as _cl
                    _sync_graph = build_graph(llm=_cl(model=_thread_model))

                def _run_graph():
                    try:
                        invoke_result[0] = _sync_graph.invoke(
                            {"messages": messages},
                            config={"recursion_limit": 80},
                        )
                    except Exception as e:
                        # GraphRecursionError: 达到 recursion_limit
                        if "recursion" in str(e).lower() or "RecursionError" in type(e).__name__:
                            print(f"[RECURSION-LIMIT] 达到迭代上限，尝试无工具总结", flush=True)
                            try:
                                summary = _summarize_without_tools(messages)
                                invoke_result[0] = {"messages": messages + [AIMessage(content=summary)]}
                            except Exception as se:
                                print(f"[RECURSION-LIMIT] 总结失败: {se}", flush=True)
                                invoke_result[0] = {"messages": messages + [AIMessage(content="抱歉，处理步骤过多已自动停止。请尝试更简单的提问方式。")]}
                        else:
                            invoke_error[0] = e

                t = threading.Thread(target=_run_graph)
                t.start()
                t.join(timeout=1800)

                if t.is_alive():
                    self.send_json({
                        "response": "抱歉，处理超时了，请换个简单点的问题试试。",
                        "thread_id": thread_id,
                    })
                    return

                if invoke_error[0]:
                    raise invoke_error[0]

                result = invoke_result[0]
                thread_messages[thread_id] = result["messages"]
                messages = thread_messages[thread_id]
                save_ai_messages(thread_id, messages[prev_count:])

                response_text, tool_calls_info = extract_response(messages, prev_count)
                # ── 空意图检测 ──
                response_text = _fix_empty_intent_response(response_text, messages)
                self.send_json({
                    "response": response_text,
                    "tool_calls": tool_calls_info if tool_calls_info else None,
                    "thread_id": thread_id,
                })
            finally:
                release_thread_lock(thread_id)
        except Exception as e:
            err_str = str(e)
            print(f"[ERROR] Agent error: {err_str[:500]}")
            import traceback
            traceback.print_exc()
            if "exceed" in err_str.lower() and "context" in err_str.lower():
                thread_messages[thread_id] = []
                self.send_json({"error": "上下文过长已自动清除，请重新提问", "auto_cleared": True})
            else:
                self.send_json({"error": err_str}, 500)

    # ── SSE 流式对话（v2: astream_events 逐 token 推送）─────────

    def _handle_stream_chat(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            raw = body.decode("utf-8", errors="replace").strip()
            if raw.startswith("{") or raw.startswith("["):
                data = json.loads(raw)
            else:
                data = {"message": raw}
            user_msg = data.get("message", "").strip() if isinstance(data, dict) else data.strip()
            thread_id = data.get("thread_id", "default") if isinstance(data, dict) else "default"
        except (json.JSONDecodeError, KeyError):
            self.send_json({"error": "Invalid request"}, 400)
            return

        # SSE 响应头
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def sse_send(event: str, data: dict):
            try:
                payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
            except Exception:
                self._sse_broken = True

        # Keepalive 心跳：防止 SSE 连接在 LLM/工具执行期间空闲超时
        self._sse_broken = False
        _stop_keepalive = threading.Event()

        def _keepalive_loop():
            """每15秒发一次 SSE keepalive 心跳"""
            while not _stop_keepalive.wait(15):
                if self._sse_broken:
                    break
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except Exception:
                    self._sse_broken = True
                    break

        _ka_thread = threading.Thread(target=_keepalive_loop, daemon=True)
        _ka_thread.start()

        try:
            # 注册 cancel_event，以便外部取消此请求
            _local_cancel = threading.Event()
            with _cancel_lock:
                old_evt = _cancel_events.get(thread_id)
                if old_evt:
                    old_evt.set()  # 取消旧请求
                _cancel_events[thread_id] = _local_cancel

            if not acquire_thread_lock(thread_id, timeout=1800):
                sse_send("error", {"message": "当前会话正在处理中，请稍后再试"})
                self.close_connection = True
                return
            try:
                messages = get_messages(thread_id)
                prev_count = len(messages)
                store.create_thread(thread_id)
                messages.append(HumanMessage(content=user_msg))
                import datetime as _dt
                _ts = _dt.datetime.now().strftime("%H:%M:%S")
                print(f"[{_ts}] [STREAM] thread={thread_id} history={prev_count} msgs, new msg={user_msg[:80]}", flush=True)

                # ── 构建带 cancel_event 的 graph 实例 ──
                # 读取 per-thread 模型配置
                _thread_model = _thread_models.get(thread_id)
                _llm_for_graph = None
                if _thread_model:
                    from graph import create_llm
                    _llm_for_graph = create_llm(model=_thread_model)
                _cancelable_graph = build_graph(llm=_llm_for_graph, cancel_event=_local_cancel)

                # ── 用队列桥接 async astream_events → 同步 SSE 发送 ──
                import queue as _queue_mod
                _event_queue = _queue_mod.Queue(maxsize=256)
                _stream_error = [None]
                _stream_done = [False]

                # 追踪 LLM 输出：每轮 agent 节点的完整文本/工具调用
                _current_llm_text = [""]      # 当前 LLM 轮次的累积文本
                _current_tool_calls = [None]  # 当前 LLM 轮次的 tool_calls
                all_new_messages = []
                tool_calls_seen = set()

                def _run_astream():
                    """在新线程中运行 asyncio 事件循环，消费 astream_events v2"""
                    import asyncio

                    async def _astream():
                        try:
                            async for event in _cancelable_graph.astream_events(
                                {"messages": messages},
                                version="v2",
                                config={"recursion_limit": 80},
                            ):
                                if _local_cancel.is_set() or _stream_done[0]:
                                    break
                                _event_queue.put(event, timeout=1800)
                        except Exception as e:
                            # GraphRecursionError: 达到 recursion_limit，尝试总结
                            if "recursion" in str(e).lower() or "RecursionError" in type(e).__name__:
                                print(f"[STREAM-RECURSION-LIMIT] 达到迭代上限，尝试无工具总结", flush=True)
                                try:
                                    summary = _summarize_without_tools(messages)
                                    _event_queue.put({
                                        "event": "on_chain_end",
                                        "name": "LangGraph",
                                        "data": {"output": {"messages": messages + [AIMessage(content=summary)]}},
                                    }, timeout=60)
                                except Exception as se:
                                    print(f"[STREAM-RECURSION-LIMIT] 总结失败: {se}", flush=True)
                                    _event_queue.put({
                                        "event": "on_chain_end",
                                        "name": "LangGraph",
                                        "data": {"output": {"messages": messages + [AIMessage(content="抱歉，处理步骤过多已自动停止。请尝试更简单的提问方式。")]}},
                                    }, timeout=60)
                            else:
                                _stream_error[0] = e
                        finally:
                            _event_queue.put(None)  # sentinel

                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(_astream())
                    finally:
                        loop.close()

                _st = threading.Thread(target=_run_astream, daemon=True)
                _st.start()

                # ── 消费事件队列，逐事件转 SSE ──
                timeout_seconds = 1800
                graph_finished = False

                while True:
                    # 检查是否被取消
                    if _local_cancel.is_set():
                        _stream_done[0] = True
                        sse_send("status", {"step": "cancelled", "message": "请求已取消"})
                        break

                    try:
                        event = _event_queue.get(timeout=5)  # 短轮询，快速响应 cancel
                    except _queue_mod.Empty:
                        timeout_seconds -= 5
                        if timeout_seconds <= 0:
                            _stream_done[0] = True
                            sse_send("error", {"message": "处理超时，请稍后重试"})
                            self.close_connection = True
                            return
                        continue

                    if event is None:  # sentinel — 流结束
                        break
                    event_kind = event.get("event", "")
                    event_name = event.get("name", "")
                    event_data = event.get("data", {})
                    # ── on_chat_model_stream：逐 token 推送 text_delta ──
                    if event_kind == "on_chat_model_stream":
                        chunk = event_data.get("chunk")
                        if chunk and hasattr(chunk, "content") and chunk.content:
                            content = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                            _current_llm_text[0] += content
                            sse_send("text_delta", {
                                "delta": content,
                                "text": _current_llm_text[0],
                            })
                        # tool_call_chunks：流式工具调用名称/参数
                        if chunk and hasattr(chunk, "tool_call_chunks") and chunk.tool_call_chunks:
                            for tc_chunk in chunk.tool_call_chunks:
                                tc_name = tc_chunk.get("name", "")
                                if tc_name:
                                    # 只在名称首次出现时推送
                                    pass  # tool_call 在 on_chat_model_end 时统一推送

                    # ── on_chat_model_end：LLM 一轮输出完毕 ──
                    elif event_kind == "on_chat_model_end":
                        output = event_data.get("output")
                        if output:
                            # 推送 reasoning_content（思考链）
                            if hasattr(output, "additional_kwargs") and output.additional_kwargs:
                                reasoning = output.additional_kwargs.get("reasoning_content", "") or \
                                            output.additional_kwargs.get("reasoning", "")
                                if reasoning:
                                    sse_send("status", {
                                        "step": "reasoning",
                                        "content": reasoning,
                                    })

                            # 推送 tool_calls（如果 LLM 决定调用工具）
                            if hasattr(output, "tool_calls") and output.tool_calls:
                                _current_tool_calls[0] = output.tool_calls
                                for tc in output.tool_calls:
                                    tc_id = tc.get("id", "")
                                    if tc_id not in tool_calls_seen:
                                        tool_calls_seen.add(tc_id)
                                        sse_send("tool_call", {
                                            "tool": tc["name"],
                                            "args": tc.get("args", {}),
                                            "message": f"正在调用 {tc['name']}...",
                                        })

                            # 如果 LLM 有纯文本且无 tool_calls，推送 thinking
                            elif _current_llm_text[0].strip():
                                sse_send("status", {
                                    "step": "thinking",
                                    "content": _current_llm_text[0].strip(),
                                })

                        # 重置本轮 LLM 追踪
                        _current_llm_text[0] = ""
                        _current_tool_calls[0] = None

                    # ── on_tool_start：工具开始执行 ──
                    elif event_kind == "on_tool_start":
                        tool_name = event_name
                        tool_input = event_data.get("input", {})
                        sse_send("tool_started", {
                            "tool": tool_name,
                            "args": tool_input if isinstance(tool_input, dict) else str(tool_input),
                            "message": f"正在执行 {tool_name}...",
                        })

                    # ── on_tool_end：工具执行完毕 ──
                    elif event_kind == "on_tool_end":
                        tool_name = event_name
                        tool_output = event_data.get("output", "")
                        # 截断到 200 字用于进度展示
                        tool_content = str(tool_output)[:200] if tool_output else ""
                        sse_send("tool_done", {
                            "tool": tool_name,
                            "message": f"{tool_name} 执行完成",
                            "content": tool_content,
                        })

                    # ── on_chain_end (LangGraph)：图执行完毕 ──
                    elif event_kind == "on_chain_end" and event_name == "LangGraph":
                        graph_finished = True
                        output = event_data.get("output", {})
                        if output and "messages" in output:
                            result_messages = output["messages"]
                            all_new_messages = result_messages[prev_count:]

                if _stream_error[0]:
                    raise _stream_error[0]

                # ── 最终结果 ──
                if not all_new_messages and _current_llm_text[0].strip():
                    # graph 没走完但 LLM 有输出文本（异常中断），用累积文本兜底
                    all_new_messages = [AIMessage(content=_current_llm_text[0].strip())]
                elif not all_new_messages and graph_finished:
                    # graph 走完但没有消息——理论上不应该发生
                    logger.warning(f"[STREAM] graph_finished=True 但 all_new_messages 为空")

                result_messages = messages + all_new_messages
                thread_messages[thread_id] = result_messages
                save_ai_messages(thread_id, result_messages[prev_count:])

                response_text, tool_calls_info = extract_response(result_messages, prev_count)

                # ── 空意图检测 ──
                response_text = _fix_empty_intent_response(response_text, result_messages)

                sse_send("result", {
                    "response": response_text,
                    "tool_calls": tool_calls_info if tool_calls_info else None,
                    "thread_id": thread_id,
                })
                sse_send("done", {})  # 显式结束标记
                self.close_connection = True
            finally:
                release_thread_lock(thread_id)
        except Exception as e:
            import traceback as _tb
            print(f"[STREAM] ERROR: {type(e).__name__}: {str(e)[:500]}", flush=True)
            _tb.print_exc()
            sse_send("error", {"message": str(e)})
        finally:
            _stop_keepalive.set()
            # 清理 cancel_event
            with _cancel_lock:
                _cancel_events.pop(thread_id, None)

    # ── 创建会话 ─────────────────────────────────────────

    def _handle_create_thread(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
            title = data.get("title", "新对话")
        except Exception:
            title = "新对话"

        tid = str(uuid.uuid4())[:8]
        store.create_thread(tid, title)
        thread_messages[tid] = []
        self.send_json({"thread_id": tid, "title": title})

    # ── 工具方法 ─────────────────────────────────────────

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

    def log_request(self, code="-", size="-"):
        try:
            import datetime
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            msg = f"{ts} {self.command} {self.path} {code}"
            if self._log_f:
                self._log_f.write(msg + "\n")
                self._log_f.flush()
        except Exception:
            pass


# ── 启动 ────────────────────────────────────────────────────

def main():
    host = "127.0.0.1"
    port = int(os.getenv("AGENT_API_PORT", "8900"))

    # 提高连接和内存上限
    import socket
    ThreadingHTTPServer.request_queue_size = 64  # TCP backlog
    ThreadingHTTPServer.timeout = 1800  # socket timeout 30min（匹配长任务）
    ThreadingHTTPServer.allow_reuse_address = True

    server = ThreadingHTTPServer((host, port), AgentAPIHandler)

    # 启动时预分配线程池，避免无限制创建线程
    from threading import Thread
    server.daemon_threads = True  # 主进程退出时子线程自动退出
    log_path = Path(__file__).resolve().parent.parent / "agent_api.log"
    log_f = open(log_path, "a", encoding="utf-8", buffering=1)
    log_f.write(f"\n=== Agent API started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    log_f.flush()

    sys.stdout = log_f
    sys.stderr = log_f
    AgentAPIHandler._log_f = log_f

    print(f"\n{'='*50}")
    print(f"  AI-DASHENG Agent API")
    print(f"  http://{host}:{port}")
    print(f"  Endpoints:")
    print(f"    POST /v1/chat          — 同步对话")
    print(f"    POST /v1/chat/stream   — SSE 流式")
    print(f"    GET  /v1/status        — 状态面板")
    print(f"    GET  /v1/threads       — 会话列表")
    print(f"{'='*50}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()
    finally:
        log_f.close()


if __name__ == "__main__":
    main()
