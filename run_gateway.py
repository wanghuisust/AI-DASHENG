"""启动 Gateway 并将所有日志写到文件"""
import sys
import os

# 确保项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# 重定向 stdout/stderr 到日志文件
log_path = os.path.join(os.path.dirname(__file__), 'gateway_stdout.log')
log_file = open(log_path, 'w', encoding='utf-8', buffering=1)
sys.stdout = log_file
sys.stderr = log_file

from gateway.server import main
main()
