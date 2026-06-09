import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 看 agent_api.log 中最近的对话上下文
f = open('agent_api.log', 'r', encoding='utf-8', errors='replace')
size = os.path.getsize('agent_api.log')
f.seek(max(0, size - 80000))
t = f.read()

lines = t.split('\n')
# 找从 msg[127]（用户问通用任务.bat）开始的完整交互
capture = False
for l in lines:
    l = l.rstrip()
    if not l:
        continue
    if 'msg[127]' in l or 'msg[128]' in l or 'msg[129]' in l or 'msg[130]' in l or 'msg[131]' in l:
        capture = True
    if capture:
        print(l[:400])
