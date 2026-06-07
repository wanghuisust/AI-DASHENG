"""测试多种搜索方式"""
import subprocess
import json

# 方法1: 用 curl 调用 SearxNG (如果有的话)
# 方法2: 用 curl 调用 Brave Search API
# 方法3: 用 curl 调用 Google Custom Search (需要 API key)

# 先试试 curl 直接调用 Bing 搜索 API (通过 HTML 解析)
# 但 Bing 是 JS 渲染的，换用 DuckDuckGo 的另一个端点

import urllib.request
import urllib.parse
import ssl
import re

def search_with_curl_bing(query):
    """用 curl 调用 Bing，但用不同的方式"""
    try:
        result = subprocess.run(
            ['curl', '-x', 'http://127.0.0.1:7897', '-s',
             '-H', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
             '-H', 'Accept-Language: en-US,en;q=0.9',
             f'https://www.bing.com/search?q={urllib.parse.quote(query)}&setlang=en&count=5'],
            capture_output=True, timeout=15
        )
        html = result.stdout.decode('utf-8', errors='replace')
        
        # Bing 搜索结果在 <ol id="b_results"> 里
        # 但可能是 JS 渲染的，试试找其他模式
        results = []
        
        # 尝试匹配 b_title 类
        title_pattern = r'class="b_title"[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>'
        titles = re.findall(title_pattern, html, re.S)
        
        # 尝试匹配 h2 标签
        h2_pattern = r'<h2[^>]*>\s*<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>'
        h2s = re.findall(h2_pattern, html, re.S)
        
        if titles:
            for url, title in titles[:5]:
                results.append({"title": re.sub(r'<[^>]+>', '', title).strip(), "url": url})
        elif h2s:
            for url, title in h2s[:5]:
                results.append({"title": re.sub(r'<[^>]+>', '', title).strip(), "url": url})
        
        return results
    except Exception as e:
        return [{"error": str(e)}]


def search_with_curl_ddg(query):
    """用 curl 调用 DuckDuckGo"""
    try:
        result = subprocess.run(
            ['curl', '-x', 'http://127.0.0.1:7897', '-s',
             '-H', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
             f'https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}'],
            capture_output=True, timeout=15
        )
        html = result.stdout.decode('utf-8', errors='replace')
        
        results = []
        # DDG HTML 页面结构
        title_pattern = r'<a class="result__a"[^>]*>(.*?)</a>'
        titles = re.findall(title_pattern, html, re.S)
        
        snippet_pattern = r'<a class="result__snippet"[^>]*>(.*?)</a>'
        snippets = re.findall(snippet_pattern, html, re.S)
        
        url_pattern = r'<a class="result__a"[^>]*href="([^"]+)"'
        urls = re.findall(url_pattern, html)
        
        for i in range(min(5, len(titles))):
            results.append({
                "title": re.sub(r'<[^>]+>', '', titles[i]).strip(),
                "url": urls[i] if i < len(urls) else "",
                "snippet": re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
            })
        
        return results
    except Exception as e:
        return [{"error": str(e)}]


def search_with_curl_brave(query):
    """用 curl 调用 Brave Search (免费层)"""
    try:
        # Brave 有 HTML 搜索页面
        result = subprocess.run(
            ['curl', '-x', 'http://127.0.0.1:7897', '-s',
             '-H', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
             f'https://search.brave.com/search?q={urllib.parse.quote(query)}&source=web'],
            capture_output=True, timeout=15
        )
        html = result.stdout.decode('utf-8', errors='replace')
        
        results = []
        # Brave 搜索结果
        title_pattern = r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>'
        all_links = re.findall(title_pattern, html, re.S)
        
        for url, title in all_links[:10]:
            title_text = re.sub(r'<[^>]+>', '', title).strip()
            if title_text and len(title_text) > 5 and 'brave.com' not in url:
                results.append({"title": title_text, "url": url})
                if len(results) >= 5:
                    break
        
        return results
    except Exception as e:
        return [{"error": str(e)}]


query = "Python 3.12 new features"
print(f"搜索: {query}\n")

# 测试各方法
for name, func in [("Bing", search_with_curl_bing), 
                    ("DDG HTML", search_with_curl_ddg),
                    ("Brave", search_with_curl_brave)]:
    try:
        results = func(query)
        print(f"\n=== {name} ({len(results)} results) ===")
        for r in results[:3]:
            if "error" in r:
                print(f"  ❌ {r['error']}")
            else:
                print(f"  ✅ {r['title']}")
                print(f"     {r['url'][:80]}")
    except Exception as e:
        print(f"\n=== {name} === 异常: {e}")
