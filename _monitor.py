"""Monitor agent_api.log for new lines with keyword filtering"""
import time, os

LOG_API = r"G:\AI-DASHENG\agent_api.log"
LOG_GW = r"G:\AI-DASHENG\src\gateway.log"

def get_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0

off_api = get_size(LOG_API)
off_gw = get_size(LOG_GW)

KEYWORDS = [
    "LLM", "COMPRESS", "MSG-FIX", "TOOL", "error", "Error",
    "timeout", "400", "500", "invoke", "stream OK", "tool_call",
    "progress", "回复", "推送", "COMPACT", "EMPTY", "CANCEL",
    "RECURSION", "SSE", "Gateway", "wechat", "qq_adapter",
    "cancel", "dispatch", "handle_message"
]

print(f"Monitoring started. API offset={off_api}, GW offset={off_gw}", flush=True)

while True:
    # Check agent_api.log
    s = get_size(LOG_API)
    if s > off_api:
        with open(LOG_API, "r", encoding="utf-8", errors="replace") as f:
            f.seek(off_api)
            new = f.read()
            off_api = s
            for line in new.splitlines():
                if any(kw.lower() in line.lower() for kw in KEYWORDS):
                    print(f"[API] {line.strip()}", flush=True)

    # Check gateway.log
    s = get_size(LOG_GW)
    if s > off_gw:
        with open(LOG_GW, "r", encoding="utf-8", errors="replace") as f:
            f.seek(off_gw)
            new = f.read()
            off_gw = s
            for line in new.splitlines():
                if any(kw.lower() in line.lower() for kw in KEYWORDS):
                    print(f"[GW]  {line.strip()}", flush=True)

    time.sleep(1)
