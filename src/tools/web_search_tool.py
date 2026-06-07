"""网络搜索工具 — 多源并行搜索，取最快有结果的

支持三种搜索源：
1. Bing HTML 搜索（通过代理，稳定）
2. DuckDuckGo Instant Answer API（快速但可能被反爬）
3. DuckDuckGo HTML 搜索（备用）

并行调用所有可用源，取最先返回有效结果的。
"""

import json
import re
import ssl
import urllib.request
import urllib.parse
import concurrent.futures
from langchain_core.tools import tool


def _make_opener():
    """创建带代理和 SSL 不验证的 opener"""
    ssl_ctx = ssl._create_unverified_context()
    proxy = urllib.request.ProxyHandler({"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl_ctx),
        proxy
    )


def _search_bing(query, count=5):
    """用 Bing HTML 搜索"""
    try:
        opener = _make_opener()
        url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}&count={count}&setlang=en"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        
        resp = opener.open(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")
        
        results = []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, 'html.parser')
            for block in soup.find_all('li', class_='b_algo'):
                title_tag = block.find('a')
                snippet_tag = block.find('p', class_='b_line')
                if title_tag:
                    title = title_tag.get_text(strip=True)
                    href = title_tag.get('href', '')
                    snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""
                    results.append({"title": title, "url": href, "snippet": snippet})
        except ImportError:
            blocks = re.findall(r'<li class="b_algo">(.*?)</li>', html, re.S)
            for block in blocks:
                title_match = re.search(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', block, re.S)
                snippet_match = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
                if title_match:
                    title = re.sub(r'<[^>]+>', '', title_match.group(2)).strip()
                    link_url = title_match.group(1)
                    snippet = re.sub(r'<[^>]+>', '', snippet_match.group(1)).strip() if snippet_match else ""
                    results.append({"title": title, "url": link_url, "snippet": snippet})
        
        return results if results else None
        
    except Exception:
        return None


def _search_ddg_api(query, count=5):
    """用 DuckDuckGo Instant Answer API"""
    try:
        opener = _make_opener()
        url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        
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
        
        return results if results else None
        
    except Exception:
        return None


def _search_ddg_html(query, count=5):
    """用 DuckDuckGo HTML 搜索"""
    try:
        opener = _make_opener()
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        
        resp = opener.open(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")
        
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
        
        return results if results else None
        
    except Exception:
        return None


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """搜索互联网获取信息。当用户问的问题需要最新资讯、事实查证或你不了解的知识时使用。

    并行调用 Bing 和 DuckDuckGo，取最先返回有效结果的。

    Args:
        query: 搜索关键词
        max_results: 最多返回的结果数，默认5
    """
    try:
        best_results = None
        best_source = ""
        
        sources = {
            "Bing": _search_bing,
            "DDG-API": _search_ddg_api,
            "DDG-HTML": _search_ddg_html,
        }
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(func, query, max_results): name
                for name, func in sources.items()
            }
            
            for future in concurrent.futures.as_completed(futures, timeout=20):
                source = futures[future]
                try:
                    results = future.result()
                    if results:
                        best_results = results
                        best_source = source
                        # Bing 是最可靠的，拿到就停止
                        if source == "Bing":
                            break
                except Exception:
                    continue
        
        if not best_results:
            return "搜索失败，所有搜索源均无响应。"
        
        # 格式化结果
        lines = []
        for i, r in enumerate(best_results[:max_results]):
            title = r.get("title", "无标题")
            url = r.get("url", "")
            snippet = r.get("snippet", "")
            
            lines.append(f"[{i+1}] {title}")
            if url:
                lines.append(f"    🔗 {url}")
            if snippet:
                lines.append(f"    {snippet[:200]}")
        
        return "\n\n".join(lines)
        
    except Exception as e:
        return f"搜索失败: {e}"
