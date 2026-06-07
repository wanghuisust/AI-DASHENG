import requests
import re
from bs4 import BeautifulSoup

proxies = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 测试 Bing
url = "https://www.bing.com/search?q=Python+3.12+new+features&count=3&setlang=en"
resp = requests.get(url, headers=headers, proxies=proxies, timeout=15, verify=False)
html = resp.text

soup = BeautifulSoup(html, 'html.parser')

# 查找 b_algo 块
results = []
for block in soup.find_all('li', class_='b_algo'):
    title_tag = block.find('a')
    snippet_tag = block.find('p', class_='b_line')
    
    if title_tag:
        title = title_tag.get_text(strip=True)
        href = title_tag.get('href', '')
        snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""
        
        # 解析 Bing 重定向链接
        real_url = href
        if 'bing.com/ck/a' in href:
            # 从 RedirectUrl 属性获取真实链接
            real_url = title_tag.get('RedirectUrl', href)
        
        results.append({
            "title": title,
            "url": real_url if real_url.startswith('http') else href,
            "snippet": snippet
        })

print(f"Found {len(results)} results:\n")
for i, r in enumerate(results[:5]):
    print(f"{i+1}. {r['title']}")
    print(f"   URL: {r['url'][:100]}")
    if r['snippet']:
        print(f"   Snippet: {r['snippet'][:100]}")
    print()
