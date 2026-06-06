"""AI-DASHENG Web 界面 — 纯标准库，零额外依赖

v2: streaming 模式 + 工具调用支持
- /api/chat: 同步调用（Web UI 用），120s 超时
- /api/chat/stream: SSE 流式调用（Gateway 用），先发进度再发结果
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

# 全局 graph 实例
print("Loading AI-DASHENG agent...")
graph = build_graph()
store = get_store()
print("Agent ready!")

# 对话状态 — 按 thread_id 隔离
thread_messages: dict[str, list] = {}  # thread_id → messages list
current_thread = "default"


def get_messages(thread_id: str) -> list:
    """获取指定会话的消息列表"""
    if thread_id not in thread_messages:
        thread_messages[thread_id] = []
    return thread_messages[thread_id]


def save_ai_messages(thread_id: str, new_messages: list):
    """将新增消息保存到 SQLite"""
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
                ensure_ascii=False
            )
        elif role == "tool":
            tool_name = getattr(msg, "name", "")

        store.save_message(thread_id, role, content, tool_calls_str, tool_name)


def extract_response(messages: list, prev_count: int) -> tuple[str, list]:
    """从消息中提取最终回复文本和工具调用信息"""
    response_text = ""
    tool_calls_info = []

    for msg in messages[prev_count:]:
        if hasattr(msg, "type") and msg.type == "ai":
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls_info.append({
                        "name": tc["name"],
                        "args": tc["args"]
                    })
            if msg.content:
                response_text = msg.content

    return response_text, tool_calls_info


class ChatHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self.serve_file("web/index.html", "text/html; charset=utf-8")
        elif path == "/api/clear":
            qs = parse_qs(urlparse(self.path).query)
            tid = qs.get("thread_id", [current_thread])[0]
            if tid in thread_messages:
                thread_messages[tid] = []
            self.send_json({"status": "ok", "message": "对话已清除"})
        elif path == "/api/threads":
            threads = store.list_threads()
            self.send_json({"threads": threads})
        elif path == "/api/history":
            qs = parse_qs(parsed.query)
            tid = qs.get("thread_id", [current_thread])[0]
            msgs = store.get_messages(tid)
            self.send_json({"messages": msgs})
        elif path == "/api/skills":
            from skills import SkillManager
            sm = SkillManager(os.path.join(os.path.dirname(__file__), "..", "data", "skills"))
            self.send_json({"skills": sm.list_skills()})
        elif path == "/api/memory":
            from memory import Memory
            m = Memory()
            self.send_json(m.list_all())
        else:
            self.send_error(404)

    def do_POST(self):
        global current_thread
        parsed = urlparse(self.path)

        if parsed.path == "/api/chat/stream":
            # ── SSE 流式接口（Gateway 用） ──
            self._handle_stream_chat()
        elif parsed.path == "/api/chat":
            # ── 同步接口（Web UI 用） ──
            self._handle_sync_chat()
        elif parsed.path == "/api/new_thread":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode("utf-8", errors="replace"))
                title = data.get("title", "新对话")
            except:
                title = "新对话"

            tid = str(uuid.uuid4())[:8]
            store.create_thread(tid, title)
            current_thread = tid
            thread_messages[tid] = []
            self.send_json({"thread_id": tid, "title": title})
        else:
            self.send_error(404)

    def _handle_sync_chat(self):
        """同步聊天（Web UI 用）— 带 120s 超时"""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
            user_msg = data.get("message", "").strip()
            thread_id = data.get("thread_id", current_thread)
        except (json.JSONDecodeError, KeyError):
            self.send_json({"error": "Invalid request"}, 400)
            return

        if not user_msg:
            self.send_json({"error": "Message is empty"}, 400)
            return

        try:
            messages = get_messages(thread_id)
            prev_count = len(messages)
            store.create_thread(thread_id)
            messages.append(HumanMessage(content=user_msg))

            # 带超时的 Agent 调用
            invoke_result = [None]
            invoke_error = [None]

            def _run_graph():
                try:
                    invoke_result[0] = graph.invoke(
                        {"messages": messages},
                        config={"recursion_limit": 128}
                    )
                except Exception as e:
                    invoke_error[0] = e

            t = threading.Thread(target=_run_graph)
            t.start()
            t.join(timeout=120)

            if t.is_alive():
                self.send_json({"response": "抱歉，处理超时了，请换个简单点的问题试试。", "thread_id": thread_id})
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
                "tool_calls": tool_calls_info if tool_calls_info else None
            })
        except Exception as e:
            err_str = str(e)
            if "exceed" in err_str.lower() and "context" in err_str.lower():
                thread_messages[thread_id] = []
                self.send_json({"error": "上下文过长已自动清除，请重新提问", "auto_cleared": True})
            else:
                self.send_json({"error": err_str}, 500)

    def _handle_stream_chat(self):
        """SSE 流式聊天（Gateway 用）— 逐步发送进度和结果"""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
            user_msg = data.get("message", "").strip()
            thread_id = data.get("thread_id", "default")
        except (json.JSONDecodeError, KeyError):
            self.send_json({"error": "Invalid request"}, 400)
            return

        if not user_msg:
            self.send_json({"error": "Message is empty"}, 400)
            return

        # SSE 响应头
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def sse_send(event: str, data: dict):
            """发送 SSE 事件"""
            try:
                payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass  # 客户端断开

        try:
            messages = get_messages(thread_id)
            prev_count = len(messages)
            store.create_thread(thread_id)
            messages.append(HumanMessage(content=user_msg))

            sse_send("status", {"step": "thinking", "message": "正在思考..."})

            # 用 stream() 逐步获取，每步发 SSE 事件
            # 同时收集所有消息构建完整 state
            all_new_messages = []
            tool_calls_seen = set()

            for event in graph.stream(
                {"messages": messages},
                config={"recursion_limit": 128}
            ):
                # event 是 {node_name: output_dict}
                for node_name, node_output in event.items():
                    if node_name == "agent":
                        msgs = node_output.get("messages", [])
                        for msg in msgs:
                            all_new_messages.append(msg)
                            if hasattr(msg, "tool_calls") and msg.tool_calls:
                                for tc in msg.tool_calls:
                                    tc_id = tc.get("id", "")
                                    if tc_id not in tool_calls_seen:
                                        tool_calls_seen.add(tc_id)
                                        sse_send("status", {
                                            "step": "tool_call",
                                            "tool": tc["name"],
                                            "message": f"正在调用 {tc['name']}..."
                                        })
                    elif node_name == "tools":
                        msgs = node_output.get("messages", [])
                        for msg in msgs:
                            all_new_messages.append(msg)
                            tool_name = getattr(msg, "name", "未知工具")
                            sse_send("status", {
                                "step": "tool_done",
                                "tool": tool_name,
                                "message": f"{tool_name} 执行完成"
                            })

            # 构建完整消息列表
            result_messages = messages + all_new_messages
            thread_messages[thread_id] = result_messages
            save_ai_messages(thread_id, result_messages[prev_count:])

            response_text, tool_calls_info = extract_response(result_messages, prev_count)

            sse_send("result", {
                "response": response_text,
                "tool_calls": tool_calls_info if tool_calls_info else None,
            })
            # 流结束，关闭连接
            self.close_connection = True

        except Exception as e:
            sse_send("error", {"message": str(e)})

    def serve_file(self, filepath, content_type):
        full_path = os.path.join(os.path.dirname(__file__), filepath)
        if os.path.exists(full_path):
            with open(full_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404)

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main():
    host = "127.0.0.1"
    port = 7860
    server = ThreadingHTTPServer((host, port), ChatHandler)

    # 写日志到文件（pythonw.exe 无 stdout）
    log_path = Path(__file__).resolve().parent.parent / "web_server.log"
    log_f = open(log_path, "a", encoding="utf-8", buffering=1)  # line-buffered
    log_f.write(f"\n=== WebServer started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    log_f.flush()

    # 替换 stdout/stderr 让 print() 写到日志
    sys.stdout = log_f
    sys.stderr = log_f

    print(f"\n{'='*50}")
    print(f"  AI-DASHENG Web UI")
    print(f"  http://{host}:{port}")
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
