"""启动 DASHENG Web 服务，日志写文件"""
import subprocess, sys, time, os

log_path = r"G:\AI-DASHENG\web_server.log"

proc = subprocess.Popen(
    [sys.executable, "-u", r"src\web_server.py"],
    cwd=r"G:\AI-DASHENG",
    stdout=open(log_path, "w", encoding="utf-8"),
    stderr=subprocess.STDOUT,
    bufsize=1
)

# 等待服务就绪
for i in range(20):
    time.sleep(1)
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if "7860" in content:
            print("✅ 服务启动成功")
            print(content.strip())
            break

# 验证端口
import urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:7860/", timeout=3)
    print("✅ 端口可达")
except Exception as e:
    print(f"❌ 端口不可达: {e}")

print(f"\nPID: {proc.pid}")
print("http://127.0.0.1:7860")

# 保持运行
proc.wait()
