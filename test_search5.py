"""测试用 requests 库搜索"""
import requests
import re
import json

def search_bing_requests(query, count=5):
    """用 requests 搜索 Bing"""
    try:
        proxies = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        
        url = f"https://www.bing.com/search?q={requests.utils.quote(query)}&count={count}&setlang=en"
        resp = requests.get(url, headers=headers, proxies=proxies, timeout=15, verify=False)
        html = resp.text
        
        results = []
        # 查找 b_algo 块
        blocks = re.findall(r'<li class="b_algo">(.*?)</li>', html, re.S)
        
        for block in blocks:
            title_match = re.search(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', block, re.S)
            snippet_match = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
            
            if title_match:
                title = re.sub(r'<[^>]+>', '', title_match.group(2)).strip()
                link_url = title_match.group(1)
                snippet = re.sub(r'<[^>]+>', '', snippet_match.group(1)).strip() if snippet_match else ""
                results.append({"title": title, "url": link_url, "snippet": snippet})
        
        return results
    except Exception as e:
        return [{"error": str(e)}]


def search_ddg_requests(query, count=5):
    """用 requests 搜索 DuckDuckGo HTML"""
    try:
        proxies = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        
        url = f"https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}"
        resp = requests.get(url, headers=headers, proxies=proxies, timeout=15, verify=False)
        html = resp.text
        
        results = []
        titles = re.findall(r'<a class="result__a"[^>]*>(.*?)</a>', html, re.S)
        snippets = re.findall(r'<a class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
        urls = re.findall(r'<a class="result__a"[^>]*href="([^"]+)"', html)
        
        for i in range(min(count, len(titles))):
            results.append({
                "title": re.sub(r'<[^>]+>', '', titles[i]).strip(),
                "url": urls[i] if i < len(urls) else "",
                "snippet": re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
            })
        
        return results
    except Exception as e:
        return [{"error": str(e)}]


def search_ddg_api(query):
    """用 requests 调用 DuckDuckGo API"""
    try:
        proxies = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
        
        url = f"https://api.duckduckgo.com/?q={requests.utils.quote(query)}&format=json&no_html=1"
        resp = requests.get(url, proxies=proxies, timeout=10)
        data = resp.json()
        
        results = []
        if data.get("AbstractText"):
            results.append({
                "title": "Instant Answer",
                "url": data.get("AbstractURL", ""),
                "snippet": data["AbstractText"]
            })
        
        for topic in data.get("RelatedTopics", [])[:5]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append({
                    "title": topic["Text"],
                    "url": topic.get("FirstURL", ""),
                    "snippet": ""
                })
        
        return results
    except Exception as e:
        return [{"error": str(e)}]


query = "Python 3.12 new features"
print(f"搜索: {query}\n")

for name, func in [("Bing (requests)", search_bing_requests),
                    ("DDG HTML (requests)", search_ddg_requests),
                    ("DDG API (requests)", search_ddg_api)]:
    try:
        results = func(query)
        print(f"\n=== {name} ({len(results)} results) ===")
        for r in results[:3]:
            if "error" in r:
                print(f"  ❌ {r['error']}")
            else:
                print(f"  ✅ {r['title']}")
                print(f"     {r['url'][:80]}")
                if r.get('snippet'):
                    print(f"     {r['snippet'][:100]}")
    except Exception as e:
        print(f"\n=== {name} === 异常: {e}")
