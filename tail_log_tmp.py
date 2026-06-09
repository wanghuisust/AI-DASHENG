import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Read last 60 lines of agent_api.log
with open('G:/AI-DASHENG/agent_api.log', 'r', encoding='utf-8', errors='replace') as f:
    lines = f.readlines()
for l in lines[-60:]:
    print(l, end='')
