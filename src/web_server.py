"""DASHENG Web Server — Dashboard UI + Chat（调用 Agent API）

v3: 不再直接运行 graph，而是调用 Agent API (:8900)
    新增 Dashboard：模型状态、Gateway 存活、历史对话管理
"""

import sys
import os
import json
import time
import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from pathlib import Path

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path, override=True)

# Agent API 地址
AGENT_API = os.getenv("AGENT_API_URL", "http://127.0.0.1:8900")


def call_agent_api(method: str, path: str, data: dict = None, timeout: int = 300) -> dict:
    """调用 Agent API"""
    url = f"{AGENT_API}{path}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
            return {"error": f"Agent API {e.code}: {err_body[:200]}"}
        except Exception:
            return {"error": f"Agent API error: {e.code}"}
    except Exception as e:
        return {"error": f"Agent API unreachable: {e}"}


class WebHandler(BaseHTTPRequestHandler):
    _log_f = None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # ── 静态文件 ──
        if path == "/" or path == "/index.html":
            self.serve_file("web/index.html", "text/html; charset=utf-8")

        # ── Dashboard API ──
        elif path == "/api/status":
            # 聚合 Agent API + Gateway 状态
            result = call_agent_api("GET", "/v1/status", timeout=5)
            self.send_json(result)

        elif path == "/api/threads":
            result = call_agent_api("GET", "/v1/threads", timeout=5)
            self.send_json(result)

        elif path.startswith("/api/threads/"):
            tid = path.split("/api/threads/")[1].split("?")[0]
            qs = parse_qs(parsed.query)
            limit = int(qs.get("limit", [100])[0])
            result = call_agent_api("GET", f"/v1/threads/{tid}?limit={limit}", timeout=5)
            self.send_json(result)

        elif path == "/api/history":
            qs = parse_qs(parsed.query)
            tid = qs.get("thread_id", ["default"])[0]
            result = call_agent_api("GET", f"/v1/threads/{tid}", timeout=5)
            self.send_json(result)

        elif path == "/api/clear":
            qs = parse_qs(parsed.query)
            tid = qs.get("thread_id", ["default"])[0]
            result = call_agent_api("DELETE", f"/v1/threads/{tid}", timeout=5)
            self.send_json(result)

        # ── 兼容旧接口 ──
        elif path == "/api/skills":
            self.send_json({"skills": []})
        elif path == "/api/memory":
            self.send_json({})

        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)

        # ── 聊天（转发到 Agent API） ──
        if parsed.path == "/api/chat/stream":
            self._proxy_stream()
        elif parsed.path == "/api/chat":
            self._proxy_sync()
        elif parsed.path == "/api/new_thread":
            # 转发创建会话
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode("utf-8", errors="replace"))
            except Exception:
                data = {}
            result = call_agent_api("POST", "/v1/threads", data, timeout=5)
            self.send_json(result)
        else:
            self.send_error(404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/threads/"):
            tid = parsed.path.split("/api/threads/")[1]
            result = call_agent_api("DELETE", f"/v1/threads/{tid}", timeout=5)
            self.send_json(result)
        else:
            self.send_error(404)

    # ── 同步聊天代理 ─────────────────────────────────────

    def _proxy_sync(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            self.send_json({"error": "Invalid request"}, 400)
            return

        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] POST /api/chat → Agent API message={data.get('message', '')[:80]}")

        result = call_agent_api("POST", "/v1/chat", data, timeout=300)
        if "error" in result:
            self.send_json(result, 500)
        else:
            self.send_json(result)

    # ── SSE 流式代理 ─────────────────────────────────────

    def _proxy_stream(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            self.send_json({"error": "Invalid request"}, 400)
            return

        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] POST /api/chat/stream → Agent API message={data.get('message', '')[:80]}")

        # SSE 响应头
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        # 请求 Agent API 的 SSE
        url = f"{AGENT_API}/v1/chat/stream"
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )

        try:
            import http.client as _http_client
            _parsed = urlparse(url)
            _conn = _http_client.HTTPConnection(_parsed.hostname, _parsed.port or 80, timeout=30)
            _conn.request("POST", _parsed.path, body=payload, headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            })
            _resp = _conn.getresponse()
            if _resp.status != 200:
                err_text = _resp.read().decode("utf-8", errors="replace")[:200]
                _conn.close()
                self.wfile.write(f"event: error\ndata: {json.dumps({'message': f'Agent API error: {_resp.status} - {err_text}'})}\n\n".encode("utf-8"))
                self.wfile.flush()
                return

            if _conn.sock:
                _conn.sock.settimeout(300)  # 每个 chunk 最多等 300s

            start_time = time.time()
            buf = b""
            while True:
                # 10 分钟整体超时
                if time.time() - start_time > 600:
                    break
                try:
                    chunk = _resp.read(4096)
                except socket.timeout:
                    continue
                except Exception:
                    break
                if not chunk:
                    break
                # 透传 SSE 事件
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except Exception:
                    break
            _conn.close()
            self.close_connection = True
        except Exception as e:
            err_data = f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
            try:
                self.wfile.write(err_data.encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

    # ── 工具方法 ─────────────────────────────────────────

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


def main():
    host = "127.0.0.1"
    port = 7860
    server = ThreadingHTTPServer((host, port), WebHandler)

    # 日志
    log_path = Path(__file__).resolve().parent.parent / "web_server.log"
    log_f = open(log_path, "a", encoding="utf-8", buffering=1)
    log_f.write(f"\n=== WebServer started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    log_f.flush()

    sys.stdout = log_f
    sys.stderr = log_f
    WebHandler._log_f = log_f

    print(f"\n{'='*50}")
    print(f"  DASHENG Web Server (Dashboard + Chat)")
    print(f"  http://{host}:{port}")
    print(f"  Agent API: {AGENT_API}")
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
