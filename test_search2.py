import urllib.request
import urllib.parse
import ssl

url = f"https://www.bing.com/search?q={urllib.parse.quote('Python 3.12 新特性')}&count=3"
req = urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
})

proxy = urllib.request.ProxyHandler({"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
opener = urllib.request.build_opener(proxy)

resp = opener.open(req, timeout=15)
html = resp.read().decode("utf-8", errors="replace")

# 保存 HTML 到文件查看
with open("bing_result.html", "w", encoding="utf-8") as f:
    f.write(html)

print(f"HTML length: {len(html)}")

# 查找 b_algo
import re
b_algo = re.findall(r'<li class="b_algo">(.*?)</li>', html, re.S)
print(f"b_algo count: {len(b_algo)}")

# 查找所有包含 href 的 a 标签
all_links = re.findall(r'<a[^>]*href="([^"]*)"[^>]*>([^<]*)</a>', html)
print(f"\nAll links ({len(all_links)}):")
for url, title in all_links[:20]:
    if 'bing.com' not in url and len(title.strip()) > 3:
        print(f"  {title.strip()[:60]} -> {url[:80]}")

# 查找 b_line
b_line = re.findall(r'class="b_line">(.*?)</', html, re.S)
print(f"\nb_line count: {len(b_line)}")
for line in b_line[:5]:
    print(f"  {line.strip()[:100]}")

# 查找所有包含结果的 class
classes = re.findall(r'class="([^"]*result[^"]*)"', html)
print(f"\nResult classes: {set(classes)}")
