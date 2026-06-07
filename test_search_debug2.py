import sys
sys.path.insert(0, 'G:/AI-DASHENG/src')

# 直接导入测试
from tools.web_search_tool import _search_bing, _search_ddg_api, _search_ddg_html

print("Testing Bing...")
result = _search_bing("Python 3.12 new features", 3)
print(f"Bing result: {result}")

print("\nTesting DDG API...")
result = _search_ddg_api("Python 3.12 new features", 3)
print(f"DDG API result: {result}")

print("\nTesting DDG HTML...")
result = _search_ddg_html("Python 3.12 new features", 3)
print(f"DDG HTML result: {result}")
