"""AI-DASHENG Web 界面 — 纯标准库，零额外依赖"""

import sys
import os
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from graph import build_graph
from langchain_core.messages import HumanMessage

# 全局 graph 实例（启动时构建一次）
print("Loading AI-DASHENG agent...")
graph = build_graph()
print("Agent ready!")

# 对话历史
messages = []


class ChatHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    def do_GET(self):
        """提供前端页面"""
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            self.serve_file("web/index.html", "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        """处理聊天 API"""
        if self.path == "/api/chat":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode("utf-8"))
                user_msg = data.get("message", "").strip()
            except (json.JSONDecodeError, KeyError):
                self.send_json({"error": "无效请求"}, 400)
                return

            if not user_msg:
                self.send_json({"error": "消息不能为空"}, 400)
                return

            # 调用 Agent
            try:
                global messages
                messages.append(HumanMessage(content=user_msg))
                result = graph.invoke({"messages": messages})
                messages = result["messages"]

                # 提取结果
                response_text = ""
                tool_calls_info = []

                for msg in messages:
                    if hasattr(msg, "type") and msg.type == "ai":
                        if hasattr(msg, "tool_calls") and msg.tool_calls:
                            for tc in msg.tool_calls:
                                tool_calls_info.append({
                                    "name": tc["name"],
                                    "args": tc["args"]
                                })
                        if msg.content:
                            response_text = msg.content

                self.send_json({
                    "response": response_text,
                    "tool_calls": tool_calls_info if tool_calls_info else None
                })
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        else:
            self.send_error(404)

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
        # 简化日志
        pass


def main():
    host = "127.0.0.1"
    port = 7860
    server = HTTPServer((host, port), ChatHandler)
    print(f"\n{'='*50}")
    print(f"  AI-DASHENG Web UI")
    print(f"  http://{host}:{port}")
    print(f"{'='*50}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
