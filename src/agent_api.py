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
load_dotenv(env_path)

from graph import build_graph
from persistence import get_store
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

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

# 启动时间
_start_time = time.time()


def get_messages(thread_id: str) -> list:
    if thread_id not in thread_messages:
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
        if role == "ai" and hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_calls_str = json.dumps(
                [{"name": tc["name"], "args": tc["args"]} for tc in msg.tool_calls],
                ensure_ascii=False,
            )
        elif role == "tool":
            tool_name = getattr(msg, "name", "")
        store.save_message(thread_id, role, content, tool_calls_str, tool_name)


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

    # ── 状态 ─────────────────────────────────────────────

    def _handle_status(self):
        model_name = os.getenv("MODEL_NAME", "unknown")
        base_url = os.getenv("OPENAI_BASE_URL", "")
        context_length = int(os.getenv("MODEL_CONTEXT_LENGTH", "50000"))
        enable_tools = os.getenv("ENABLE_TOOLS", "true").lower() == "true"

        gateway_status = check_gateway_status()

        self.send_json({
            "agent": {
                "status": "running",
                "uptime": int(time.time() - _start_time),
                "model": model_name,
                "base_url": base_url,
                "context_length": context_length,
                "enable_tools": enable_tools,
                "active_threads": len(thread_messages),
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
            if not acquire_thread_lock(thread_id, timeout=300):
                self.send_json({"response": "当前会话正在处理中，请稍后再试", "thread_id": thread_id})
                return
            try:
                messages = get_messages(thread_id)
                prev_count = len(messages)
                store.create_thread(thread_id)
                messages.append(HumanMessage(content=user_msg))

                invoke_result = [None]
                invoke_error = [None]

                def _run_graph():
                    try:
                        invoke_result[0] = graph.invoke(
                            {"messages": messages},
                            config={"recursion_limit": 128},
                        )
                    except Exception as e:
                        invoke_error[0] = e

                t = threading.Thread(target=_run_graph)
                t.start()
                t.join(timeout=300)

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

    # ── SSE 流式对话 ─────────────────────────────────────

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
            if not acquire_thread_lock(thread_id, timeout=300):
                sse_send("error", {"message": "当前会话正在处理中，请稍后再试"})
                self.close_connection = True
                return
            try:
                messages = get_messages(thread_id)
                prev_count = len(messages)
                store.create_thread(thread_id)
                messages.append(HumanMessage(content=user_msg))

                sse_send("status", {"step": "thinking", "message": "正在思考..."})

                all_new_messages = []
                tool_calls_seen = set()

                for event in graph.stream(
                    {"messages": messages},
                    config={"recursion_limit": 128},
                ):
                    for node_name, node_output in event.items():
                        if node_name == "agent":
                            msgs = node_output.get("messages", [])
                            for msg in msgs:
                                all_new_messages.append(msg)
                                # 如果有推理/思考内容，发 reasoning 事件
                                if hasattr(msg, "additional_kwargs") and msg.additional_kwargs:
                                    reasoning = msg.additional_kwargs.get("reasoning_content", "") or \
                                                msg.additional_kwargs.get("reasoning", "")
                                    if reasoning:
                                        sse_send("status", {
                                            "step": "reasoning",
                                            "content": reasoning,
                                        })
                                if hasattr(msg, "tool_calls") and msg.tool_calls:
                                    for tc in msg.tool_calls:
                                        tc_id = tc.get("id", "")
                                        if tc_id not in tool_calls_seen:
                                            tool_calls_seen.add(tc_id)
                                            sse_send("status", {
                                                "step": "tool_call",
                                                "tool": tc["name"],
                                                "message": f"正在调用 {tc['name']}...",
                                            })
                        elif node_name == "tools":
                            msgs = node_output.get("messages", [])
                            for msg in msgs:
                                all_new_messages.append(msg)
                                tool_name = getattr(msg, "name", "未知工具")
                                sse_send("status", {
                                    "step": "tool_done",
                                    "tool": tool_name,
                                    "message": f"{tool_name} 执行完成",
                                })

                result_messages = messages + all_new_messages
                thread_messages[thread_id] = result_messages
                save_ai_messages(thread_id, result_messages[prev_count:])

                response_text, tool_calls_info = extract_response(result_messages, prev_count)

                sse_send("result", {
                    "response": response_text,
                    "tool_calls": tool_calls_info if tool_calls_info else None,
                    "thread_id": thread_id,
                })
                self.close_connection = True
            finally:
                release_thread_lock(thread_id)
        except Exception as e:
            sse_send("error", {"message": str(e)})
        finally:
            _stop_keepalive.set()

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
    server = ThreadingHTTPServer((host, port), AgentAPIHandler)

    # 日志
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
