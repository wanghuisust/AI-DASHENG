import subprocess
r = subprocess.run(
    ["git", "add", "-A"],
    encoding="utf-8", errors="replace", cwd=r"G:\AI-DASHENG"
)
r2 = subprocess.run(
    ["git", "commit", "-m", "fix: complete hermes->dasheng replacement in all skill files"],
    encoding="utf-8", errors="replace", cwd=r"G:\AI-DASHENG"
)
print("RC:", r2.returncode)
if r2.stdout: print("OUT:", r2.stdout[:300])
if r2.stderr: print("ERR:", r2.stderr[:300])
