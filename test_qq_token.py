import sys
sys.path.insert(0, 'G:/AI-DASHENG/src')
from gateway.qq_adapter import TokenManager

tm = TokenManager('1903881299', 'QTXbglrx4CKTcmw7IUgt6KYn2IYp6Oh0')
t = tm.get_token()
if t:
    print('TOKEN OK:', t[:20] + '...')
    # 再试获取 gateway
    url = f"{tm.api_base}/gateway/bot"
    print('Gateway URL:', url)
    import urllib.request, json
    req = urllib.request.Request(url, headers={"Authorization": f"QQBot {t}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print('Gateway response:', json.dumps(data, indent=2))
    except Exception as e:
        print('Gateway failed:', e)
else:
    print('TOKEN FAILED')
