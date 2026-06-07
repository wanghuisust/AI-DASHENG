import subprocess, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
pip = sys.executable.replace("python.exe", "pip.exe")
r = subprocess.run([pip, "install", "langgraph-checkpoint-sqlite"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
print("RC:", r.returncode)
print("STDOUT:", r.stdout[-1000:] if r.stdout else "(empty)")
print("STDERR:", r.stderr[-1000:] if r.stderr else "(empty)")
