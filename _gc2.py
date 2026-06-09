import subprocess
r = subprocess.run(["git", "add", "-A"], encoding="utf-8", errors="replace", cwd=r"G:\AI-DASHENG")
r2 = subprocess.run(["git", "commit", "-m", "fix: empty-intent false positive + compress threshold too high"], encoding="utf-8", errors="replace", cwd=r"G:\AI-DASHENG")
print("RC:", r2.returncode)
if r2.stdout: print(r2.stdout[:300])