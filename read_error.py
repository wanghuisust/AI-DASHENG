import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
with open(r"C:\Users\Administrator\Desktop\错误.txt", "r", encoding='utf-8', errors='replace') as f:
    print(f.read())
