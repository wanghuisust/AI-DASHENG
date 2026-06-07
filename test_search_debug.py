import urllib.request
import urllib.parse
import ssl
import concurrent.futures
import re

def test_bing():
    try:
        url = "https://www.bing.com/search?q=Python+3.12+new+features&count=3&setlang=en"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        proxy = urllib.request.ProxyHandler({"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
        opener = urllib.request.build_opener(proxy)
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        resp = opener.open(req, timeout=15, context=ssl_ctx)
        html = resp.read().decode("utf-8", errors="replace")
        
        # 用 BeautifulSoup
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
        
        print(f"Bing: {len(results)} results")
        for r in results[:3]:
            print(f"  {r['title'][:50]} -> {r['url'][:60]}")
        return results
    except Exception as e:
        print(f"Bing error: {e}")
        return []

def test_ddg_api():
    try:
        url = "https://api.duckduckgo.com/?q=Python+3.12+new+features&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        proxy = urllib.request.ProxyHandler({"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
        opener = urllib.request.build_opener(proxy)
        resp = opener.open(req, timeout=10)
        import json
        data = json.loads(resp.read().decode("utf-8"))
        print(f"DDG API: AbstractText={data.get('AbstractText', '')[:50]}")
        return data
    except Exception as e:
        print(f"DDG API error: {e}")
        return {}

def test_ddg_html():
    try:
        url = "https://html.duckduckgo.com/html/?q=Python+3.12+new+features"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        proxy = urllib.request.ProxyHandler({"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
        opener = urllib.request.build_opener(proxy)
        resp = opener.open(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")
        print(f"DDG HTML: status={resp.status}, length={len(html)}")
        return html
    except Exception as e:
        print(f"DDG HTML error: {e}")
        return ""

print("Testing search sources...\n")

with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
    f_bing = executor.submit(test_bing)
    f_ddg_api = executor.submit(test_ddg_api)
    f_ddg_html = executor.submit(test_ddg_html)
    
    f_bing.result()
    f_ddg_api.result()
    f_ddg_html.result()
