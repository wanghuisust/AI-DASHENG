import sys
sys.path.insert(0, 'G:/AI-DASHENG/src')

from tools.web_search_tool import web_search

# 测试
result = web_search.invoke({"query": "Python 3.12 new features", "max_results": 3})
print(result)
