import requests
import re

proxies = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 测试 Bing
url = "https://www.bing.com/search?q=Python+3.12+new+features&count=3&setlang=en"
resp = requests.get(url, headers=headers, proxies=proxies, timeout=15, verify=False)
print(f"Bing status: {resp.status_code}")
print(f"Bing length: {len(resp.text)}")

# 保存 HTML
with open("bing_test.html", "w", encoding="utf-8") as f:
    f.write(resp.text)

# 看看 HTML 里有什么关键词
for keyword in ["b_algo", "b_title", "b_line", "searchResults", "b_results", "result"]:
    count = resp.text.lower().count(keyword.lower())
    print(f"  '{keyword}' found: {count} times")

# 看看前 2000 字符
print(f"\n=== First 2000 chars ===")
print(resp.text[:2000])
