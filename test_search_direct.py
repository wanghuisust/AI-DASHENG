import urllib.request
import urllib.parse
import ssl

url = "https://www.bing.com/search?q=Python+3.12+new+features&count=3&setlang=en"
req = urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
})

# 创建不验证 SSL 的 opener
ssl_ctx = ssl._create_unverified_context()

proxy = urllib.request.ProxyHandler({"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=ssl_ctx),
    proxy
)

try:
    resp = opener.open(req, timeout=15)
    html = resp.read().decode("utf-8", errors="replace")
    print(f"Success! HTML length: {len(html)}")
    
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    results = []
    for block in soup.find_all('li', class_='b_algo'):
        title_tag = block.find('a')
        snippet_tag = block.find('p', class_='b_line')
        if title_tag:
            title = title_tag.get_text(strip=True)
            href = title_tag.get('href', '')
            snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""
            results.append({"title": title, "url": href, "snippet": snippet})
    
    print(f"Found {len(results)} results:")
    for r in results[:3]:
        print(f"  {r['title'][:50]}")
        print(f"    {r['url'][:80]}")
        
except Exception as e:
    import traceback
    print(f"Error: {e}")
    traceback.print_exc()
