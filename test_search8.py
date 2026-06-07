import requests
from bs4 import BeautifulSoup

proxies = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 测试 DuckDuckGo HTML
url = "https://html.duckduckgo.com/html/?q=Python+3.12+new+features"
resp = requests.get(url, headers=headers, proxies=proxies, timeout=15, verify=False)
html = resp.text

print(f"DDG HTML length: {len(html)}")
print(f"DDG status: {resp.status_code}")

# 看看 HTML 内容
print(f"\n=== HTML content ===")
print(html[:3000])

# 用 BeautifulSoup 解析
soup = BeautifulSoup(html, 'html.parser')

# 查找结果
results = []
for a_tag in soup.find_all('a', class_='result__a'):
    title = a_tag.get_text(strip=True)
    href = a_tag.get('href', '')
    results.append({"title": title, "url": href})

print(f"\n=== Found {len(results)} DDG results ===")
for r in results[:5]:
    print(f"  {r['title']} -> {r['url'][:80]}")

# 也看看 snippet
for p_tag in soup.find_all('a', class_='result__snippet'):
    print(f"  Snippet: {p_tag.get_text(strip=True)[:100]}")
