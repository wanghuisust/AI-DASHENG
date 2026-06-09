import subprocess
r = subprocess.run(["git", "add", "-A"], encoding="utf-8", errors="replace", cwd=r"G:\AI-DASHENG")
r2 = subprocess.run(["git", "commit", "-m", "fix: pre-compress before graph.invoke to prevent recursion_limit exhaustion"], encoding="utf-8", errors="replace", cwd=r"G:\AI-DASHENG")
print("RC:", r2.returncode)
