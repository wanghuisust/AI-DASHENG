"""启动 DASHENG Web，日志写到文件"""
import subprocess, sys, os, time

os.chdir(r"G:\AI-DASHENG")
sys.path.insert(0, r"G:\AI-DASHENG\src")

log_file = r"G:\AI-DASHENG\web_server.log"

# 清空旧日志
with open(log_file, "w") as f:
    f.write("")

proc = subprocess.Popen(
    [sys.executable, "-u", r"src\web_server.py"],
    stdout=open(log_file, "w"),
    stderr=subprocess.STDOUT,
    cwd=r"G:\AI-DASHENG"
)

print(f"Started PID={proc.pid}, waiting for ready...")
for i in range(30):
    time.sleep(1)
    try:
        import urllib.request
        r = urllib.request.urlopen("http://127.0.0.1:7860/", timeout=2)
        print(f"Server ready! (took {i+1}s)")
        break
    except:
        if i % 5 == 4:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                print(f"[{i+1}s] log: {f.read()[-200:]}")
else:
    print("Timeout! Last log:")
    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
        print(f.read()[-500:])
    proc.terminate()
