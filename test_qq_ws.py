"""Quick test: connect to QQ Bot WebSocket gateway, receive Hello, send Identify, wait for Ready"""
import sys, json, time, threading
sys.path.insert(0, 'G:/AI-DASHENG/src')
from gateway.qq_adapter import TokenManager
import websocket

APP_ID = '1903881299'
APP_SECRET = 'QTXbglrx4CKTcmw7IUgt6KYn2IYp6Oh0'

tm = TokenManager(APP_ID, APP_SECRET)
token = tm.get_token()
print(f"Token: {token[:20]}...")

gateway_url = "wss://api.sgroup.qq.com/websocket/"
print(f"Connecting to: {gateway_url}")

ready_event = threading.Event()
result = {"hello": False, "ready": False, "session_id": "", "user": ""}

def on_open(ws):
    print("[WS] Connected! Waiting for Hello...")

def on_message(ws, raw):
    try:
        payload = json.loads(raw)
        op = payload.get("op")
        d = payload.get("d", {})
        t = payload.get("t", "")
        print(f"[WS] op={op} t={t}")
        
        if op == 10:  # Hello
            heartbeat_interval = d.get("heartbeat_interval", 41250)
            print(f"[WS] Hello! heartbeat_interval={heartbeat_interval}ms")
            result["hello"] = True
            # Send Identify
            identify = {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": 1 << 25 | 1 << 30,  # GROUP_AT_MESSAGE_CREATE | C2C_MESSAGE_CREATE
                    "shard": [0, 1],
                }
            }
            ws.send(json.dumps(identify))
            print("[WS] Identify sent!")
            
            # Start heartbeat
            def heartbeat():
                while not ready_event.is_set():
                    time.sleep(heartbeat_interval / 1000)
                    if ws.sock and ws.sock.connected:
                        ws.send(json.dumps({"op": 1, "d": result.get("seq", None)}))
                        print("[WS] Heartbeat sent")
            threading.Thread(target=heartbeat, daemon=True).start()
            
        elif op == 0:  # Dispatch
            result["seq"] = payload.get("s")
            if t == "READY":
                user = d.get("user", {})
                result["ready"] = True
                result["session_id"] = d.get("session_id", "")
                result["user"] = f"{user.get('username', '?')}#{user.get('id', '?')}"
                print(f"[WS] READY! Bot: {result['user']}, session: {result['session_id'][:10]}...")
                ready_event.set()
            else:
                print(f"[WS] Dispatch event: {t}, data keys: {list(d.keys())}")
                
        elif op == 11:  # Heartbeat ACK
            print("[WS] Heartbeat ACK")
            
        elif op == 9:  # Invalid Session
            print(f"[WS] Invalid Session! data={d}")
            ready_event.set()
            
        elif op == 7:  # Reconnect
            print(f"[WS] Reconnect requested!")
            ready_event.set()
            
        else:
            print(f"[WS] Unknown op={op}, data={str(d)[:200]}")
            
    except Exception as e:
        print(f"[WS] Error: {e}")

def on_error(ws, error):
    print(f"[WS] Error: {error}")

def on_close(ws, code, msg):
    print(f"[WS] Closed: code={code} msg={msg}")

ws = websocket.WebSocketApp(
    gateway_url,
    on_open=on_open,
    on_message=on_message,
    on_error=on_error,
    on_close=on_close,
)

# Run in thread
ws_thread = threading.Thread(target=ws.run_forever, kwargs={"ping_interval": 0}, daemon=True)
ws_thread.start()

# Wait for Ready or timeout
ready_event.wait(timeout=15)
if result["ready"]:
    print(f"\n=== SUCCESS ===")
    print(f"Bot: {result['user']}")
    print(f"Session: {result['session_id']}")
elif result["hello"]:
    print(f"\n=== Hello received but no READY (timeout) ===")
else:
    print(f"\n=== FAILED: No Hello received ===")

ws.close()
print("Done.")
