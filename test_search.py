import urllib.request
import urllib.parse
import ssl
import json
import re
import concurrent.futures

def search_bing(query, count=5):
    """用 Bing HTML 搜索"""
    try:
        url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}&count={count}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        
        proxy = urllib.request.ProxyHandler({"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
        opener = urllib.request.build_opener(proxy)
        
        # 创建 SSL 上下文
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        
        resp = opener.open(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")
        
        results = []
        # 匹配搜索结果
        pattern = r'<li class="b_algo">(.*?)</li>'
        blocks = re.findall(pattern, html, re.S)
        
        for block in blocks:
            title_match = re.search(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', block, re.S)
            snippet_match = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
            
            if title_match:
                title = re.sub(r'<[^>]+>', '', title_match.group(2)).strip()
                url = title_match.group(1)
                snippet = re.sub(r'<[^>]+>', '', snippet_match.group(1)).strip() if snippet_match else ""
                results.append({"title": title, "url": url, "snippet": snippet})
        
        return results
    except Exception as e:
        return [{"error": f"Bing search failed: {e}"}]


def search_ddg_html(query, count=5):
    """用 DuckDuckGo HTML 搜索"""
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        
        proxy = urllib.request.ProxyHandler({"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
        opener = urllib.request.build_opener(proxy)
        
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        
        resp = opener.open(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")
        
        results = []
        pattern = r'<a class="result__a"[^>]*>(.*?)</a>'
        titles = re.findall(pattern, html, re.S)
        
        snippet_pattern = r'<a class="result__snippet"[^>]*>(.*?)</a>'
        snippets = re.findall(snippet_pattern, html, re.S)
        
        url_pattern = r'<a class="result__a"[^>]*href="([^"]+)"'
        urls = re.findall(url_pattern, html)
        
        for i in range(min(count, len(titles))):
            results.append({
                "title": re.sub(r'<[^>]+>', '', titles[i]).strip(),
                "url": urls[i] if i < len(urls) else "",
                "snippet": re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
            })
        
        return results
    except Exception as e:
        return [{"error": f"DDG HTML search failed: {e}"}]


def search_ddg_api(query, count=5):
    """用 DuckDuckGo Instant Answer API"""
    try:
        url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        
        proxy = urllib.request.ProxyHandler({"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
        opener = urllib.request.build_opener(proxy)
        
        resp = opener.open(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))
        
        results = []
        if data.get("AbstractText"):
            results.append({
                "title": "DuckDuckGo Instant Answer",
                "url": data.get("AbstractURL", ""),
                "snippet": data["AbstractText"]
            })
        
        for topic in data.get("RelatedTopics", [])[:count]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append({
                    "title": topic["Text"],
                    "url": topic.get("FirstURL", ""),
                    "snippet": ""
                })
        
        return results
    except Exception as e:
        return [{"error": f"DDG API search failed: {e}"}]


# 测试
query = "Python 3.12 新特性"
print(f"搜索: {query}\n")

with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
    futures = {
        executor.submit(search_bing, query, 5): "Bing",
        executor.submit(search_ddg_html, query, 5): "DDG HTML",
        executor.submit(search_ddg_api, query, 5): "DDG API",
    }
    
    for future in concurrent.futures.as_completed(futures, timeout=20):
        source = futures[future]
        try:
            results = future.result()
            print(f"\n=== {source} ({len(results)} results) ===")
            for r in results[:3]:
                if "error" in r:
                    print(f"  ❌ {r['error']}")
                else:
                    print(f"  ✅ {r['title']}")
                    print(f"     {r['url']}")
                    if r.get('snippet'):
                        print(f"     {r['snippet'][:100]}")
        except concurrent.futures.TimeoutError:
            print(f"\n=== {source} === 超时")
        except Exception as e:
            print(f"\n=== {source} === 异常: {e}")
