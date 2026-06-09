import subprocess
r = subprocess.run(["git", "add", "-A"], encoding="utf-8", errors="replace", cwd=r"G:\AI-DASHENG")
r2 = subprocess.run(["git", "commit", "-m", "chore: remove hermes-agent dir, update gitignore"], encoding="utf-8", errors="replace", cwd=r"G:\AI-DASHENG")
print("commit RC:", r2.returncode)
if r2.stdout: print("OUT:", r2.stdout[:300])
