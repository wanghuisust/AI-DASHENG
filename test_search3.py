import urllib.request
import urllib.parse
import ssl
import re

# 用英文搜索试试
url = "https://www.bing.com/search?q=Python+3.12+new+features&count=5&setlang=en"
req = urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "SRCHD=AF=NOFORM",
})

proxy = urllib.request.ProxyHandler({"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
opener = urllib.request.build_opener(proxy)

resp = opener.open(req, timeout=15)
html = resp.read().decode("utf-8", errors="replace")

print(f"HTML length: {len(html)}")

# 查找 b_algo
b_algo = re.findall(r'<li class="b_algo">(.*?)</li>', html, re.S)
print(f"b_algo count: {len(b_algo)}")

for i, block in enumerate(b_algo[:5]):
    title_match = re.search(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', block, re.S)
    snippet_match = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
    
    if title_match:
        title = re.sub(r'<[^>]+>', '', title_match.group(2)).strip()
        link_url = title_match.group(1)
        snippet = re.sub(r'<[^>]+>', '', snippet_match.group(1)).strip() if snippet_match else ""
        print(f"\n{i+1}. {title}")
        print(f"   URL: {link_url}")
        print(f"   Snippet: {snippet[:100]}")

# 也看看 HTML 里有什么
print("\n\n=== HTML snippet ===")
print(html[5000:6000])
