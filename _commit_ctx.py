import subprocess
r = subprocess.run(
    ["git", "add", "-A"],
    encoding="utf-8", errors="replace", cwd=r"G:\AI-DASHENG"
)
r2 = subprocess.run(
    ["git", "commit", "-m", "feat: context compression overhaul + memory injection + 3-tier system prompt"],
    encoding="utf-8", errors="replace", cwd=r"G:\AI-DASHENG"
)
print("add RC:", r.returncode)
print("commit RC:", r2.returncode)
if r2.stdout: print("OUT:", r2.stdout[:300])
if r2.stderr: print("ERR:", r2.stderr[:300])
