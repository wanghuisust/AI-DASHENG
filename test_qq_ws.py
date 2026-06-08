"""Quick test: connect to QQ WS and log all events for 60s"""
import json, time, threading, sys
sys.stdout.reconfigure(errors='replace')
import websocket

# Get token
with open('G:/AI-DASHENG/.env', encoding='utf-8') as f:
    for line in f:
        if 'QQ_APP_ID' in line and '=' in line:
            app_id = line.split('=',1)[1].strip()
        if 'QQ_APP_SECRET' in line and '=' in line:
            app_secret = line.split('=',1)[1].strip()

token_url = 'https://bots.qq.com/app/getAppAccessToken'
payload = json.dumps({'appId': app_id, 'clientSecret': app_secret}).encode()
req = __import__('urllib.request', fromlist=['Request']).Request(token_url, data=payload, headers={'Content-Type':'application/json'}, method='POST')
resp = __import__('urllib.request', fromlist=['urlopen']).urlopen(req, timeout=10)
token = json.loads(resp.read()).get('access_token','')

# Get gateway
gw_url = 'https://api.sgroup.qq.com/gateway/bot'
req2 = __import__('urllib.request', fromlist=['Request']).Request(gw_url, headers={'Authorization': f'QQBot {token}'})
resp2 = __import__('urllib.request', fromlist=['urlopen']).urlopen(req2, timeout=10)
ws_url = json.loads(resp2.read())['url']

print(f"Connecting to {ws_url}...")

last_seq = [None]
heartbeat_interval = [41250]
ready = [False]
session_id = [""]

def on_open(ws):
    print("WS OPEN")

def on_message(ws, raw):
    payload = json.loads(raw)
    op = payload.get('op')
    t = payload.get('t')
    d = payload.get('d', {})
    s = payload.get('s')
    
    if s is not None:
        last_seq[0] = s
    
    if op == 10:  # HELLO
        heartbeat_interval[0] = d.get('heartbeat_interval', 41250)
        print(f"HELLO: interval={heartbeat_interval[0]}ms")
        # Send Identify
        identify = {
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": (1 << 25) | (1 << 0),  # C2C + GUILD
                "shard": [0, 1]
            }
        }
        ws.send(json.dumps(identify))
        print("IDENTIFY sent")
        
        # Start heartbeat
        def hb():
            time.sleep(heartbeat_interval[0] / 1000.0)
            while True:
                if last_seq[0] is not None:
                    ws.send(json.dumps({"op": 1, "d": last_seq[0]}))
                    print(f"  HB sent (seq={last_seq[0]})")
                time.sleep(heartbeat_interval[0] / 1000.0)
        threading.Thread(target=hb, daemon=True).start()
        
    elif op == 11:  # HEARTBEAT_ACK
        print(f"  HB ACK")
        
    elif op == 0:  # DISPATCH
        print(f"DISPATCH: t={t}", flush=True)
        if t == "READY":
            session_id[0] = d.get('session_id', '')
            ready[0] = True
            print(f"  READY! session_id={session_id[0][:8]}...")
        elif t in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
            content = d.get('content', '')
            author = d.get('author', {})
            openid = author.get('user_openid', '?')
            print(f"  *** MESSAGE: {openid}: {content[:100]}", flush=True)
        else:
            print(f"  data: {json.dumps(d, ensure_ascii=False)[:200]}")
    else:
        print(f"OP={op}: {json.dumps(payload, ensure_ascii=False)[:200]}")

def on_error(ws, error):
    print(f"WS ERROR: {error}")

def on_close(ws, code, reason):
    print(f"WS CLOSE: code={code} reason={reason}")

ws = websocket.WebSocketApp(
    ws_url,
    on_open=on_open,
    on_message=on_message,
    on_error=on_error,
    on_close=on_close,
)

# Run for 90 seconds then exit
def stop_after():
    time.sleep(90)
    print("\n--- Test done, closing ---")
    ws.close()

threading.Thread(target=stop_after, daemon=True).start()
ws.run_forever(ping_interval=0, ping_timeout=None)
