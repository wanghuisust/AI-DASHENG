"""网络搜索工具 — 多源并行搜索，汇总去重 (DashengTool 版)

支持搜索源（按优先级）：
1. AnySearch API（直连，AI 搜索引擎，无需代理）— 优先源
2. Bing（通过代理，HTML 爬虫）— 优先源
3. 头条搜索（直连，中文主要源）— 中文内容质量好
4. DuckDuckGo (duckduckgo-search 库，通过代理) — 英文备用源
5. Brave Search API（无需代理，备用，需配置 BRAVE_API_KEY）

所有源并行调用，汇总结果去重后取前 max_results 条。
"""

import json
import os
import re
import ssl
import urllib.request
import urllib.parse
import concurrent.futures

from pydantic import BaseModel, Field
from tools.tool_base import build_tool, DEFAULT_MAX_RESULT_SIZE_CHARS

# 从环境变量读取代理和 API key
_PROXY = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "http://127.0.0.1:7897"
_BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
_ANYSEARCH_API_KEY = os.environ.get("ANYSEARCH_API_KEY", "")


def _search_anysearch(query, count=10):
    """用 AnySearch API 搜索（直连，无需代理，AI 搜索引擎）"""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search",
                "arguments": {"query": query, "max_results": min(count, 10)},
            },
        }
        headers = {"Content-Type": "application/json"}
        if _ANYSEARCH_API_KEY:
            headers["Authorization"] = f"Bearer {_ANYSEARCH_API_KEY}"

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anysearch.com/mcp",
            data=data,
            headers=headers,
            method="POST",
        )
        no_proxy = urllib.request.ProxyHandler({})
        ssl_ctx = ssl._create_unverified_context()
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl_ctx), no_proxy
        )
        resp = opener.open(req, timeout=20)
        resp_data = json.loads(resp.read().decode("utf-8"))

        if "error" in resp_data:
            return None

        content = resp_data.get("result", {}).get("content", [])
        text = ""
        for item in content:
            if item.get("type") == "text":
                text = item.get("text", "")
                break

        if not text:
            return None

        # 解析 AnySearch Markdown 格式的搜索结果
        results = []
        current = {}
        for line in text.split("\n"):
            m = re.match(r"^###\s+\d+\.\s+(.+)$", line.strip())
            if m:
                if current.get("title"):
                    results.append(current)
                current = {"title": m.group(1).strip(), "url": "", "snippet": ""}
                continue
            m = re.match(r"^-?\s*\*{0,2}URL\*{0,2}:\s*(https?://\S+)", line.strip())
            if m:
                current["url"] = m.group(1).strip()
                continue
            m = re.match(r"^-\s+(.+)$", line.strip())
            if m and "URL" not in line:
                snippet = m.group(1).strip()
                if len(snippet) > 10:
                    current["snippet"] = snippet[:300]
                continue

        if current.get("title"):
            results.append(current)

        return results if results else None
    except Exception:
        return None


def _search_ddg(query, count=10):
    """用 duckduckgo-search 库搜索（直连，不走代理）"""
    try:
        from duckduckgo_search import DDGS
        results = []
        for attempt_proxy in [None, _PROXY]:
            try:
                kwargs = {}
                if attempt_proxy:
                    kwargs["proxy"] = attempt_proxy
                with DDGS(**kwargs) as ddgs:
                    for r in ddgs.text(query, max_results=count):
                        results.append({
                            "title": r.get("title", ""),
                            "url": r.get("href", ""),
                            "snippet": r.get("body", ""),
                        })
                if results:
                    return results
            except Exception:
                continue
        return results if results else None
    except Exception:
        return None


def _search_bing(query, count=10):
    """用 Bing HTML 搜索（通过代理），用 BeautifulSoup 解析 + cite 提取真实 URL"""
    try:
        from bs4 import BeautifulSoup

        ssl_ctx = ssl._create_unverified_context()
        proxy = urllib.request.ProxyHandler({"http": _PROXY, "https": _PROXY})
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl_ctx), proxy
        )
        url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}&count={count}&setlang=zh-Hans"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        resp = opener.open(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")

        soup = BeautifulSoup(html, 'html.parser')
        results = []
        for li in soup.find_all('li', class_='b_algo'):
            h2 = li.find('h2')
            a = h2.find('a') if h2 else None
            cite = li.find('cite')
            p = li.find('p')

            if not a:
                continue

            title = a.get_text(strip=True)
            if cite:
                cite_text = cite.get_text(strip=True)
                real_url = re.sub(r'\s*›.*', '', cite_text).strip()
                if not real_url.startswith('http'):
                    real_url = 'https://' + real_url
            else:
                real_url = a.get('href', '')

            snippet = p.get_text(strip=True) if p else ""
            results.append({"title": title, "url": real_url, "snippet": snippet})

        return results if results else None
    except Exception:
        return None


def _search_toutiao(query, count=10):
    """用头条搜索（直连，中文内容质量最好）"""
    try:
        ssl_ctx = ssl._create_unverified_context()
        no_proxy = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl_ctx), no_proxy
        )
        url = f"https://so.toutiao.com/search?keyword={urllib.parse.quote(query)}&pd=information&source=input&dvpf=pc&aid=4916&page_num=0"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://so.toutiao.com/",
        })
        resp = opener.open(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")

        results = []
        seen_titles = set()
        for m in re.finditer(r'"article_url":"(https?://[^"]+)"', html):
            article_url = m.group(1)
            start = max(0, m.start() - 2000)
            end = min(len(html), m.end() + 2000)
            block = html[start:end]

            title_m = re.search(r'"title":"([^"]+)"', block)
            abstract_m = re.search(r'"abstract":"([^"]{5,300})"', block)
            source_m = re.search(r'"source":"([^"<]+)', block)

            title = title_m.group(1) if title_m else ""
            abstract = abstract_m.group(1) if abstract_m else ""
            source = source_m.group(1) if source_m else ""
            source = re.sub(r"</?em>", "", source)
            source = source.replace("\\u003c", "<").replace("\\u003e", ">")
            title = re.sub(r"</?em>", "", title.replace("\\u003c", "<").replace("\\u003e", ">"))

            if title and len(title) > 5 and title not in seen_titles:
                seen_titles.add(title)
                results.append({
                    "title": title,
                    "url": article_url,
                    "snippet": abstract[:200],
                })
            if len(results) >= count:
                break

        return results if results else None
    except Exception:
        return None


def _search_brave(query, count=10):
    """用 Brave Search API 搜索（无需代理，免费 2000 次/月）"""
    if not _BRAVE_API_KEY:
        return None
    try:
        url = f"https://api.search.brave.com/res/v1/web/search?q={urllib.parse.quote(query)}&count={count}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": _BRAVE_API_KEY,
        })
        ssl_ctx = ssl._create_unverified_context()
        no_proxy = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl_ctx), no_proxy
        )
        resp = opener.open(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))

        results = []
        for item in data.get("web", {}).get("results", [])[:count]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            })
        return results if results else None
    except Exception:
        return None


def _dedupe_results(all_results):
    """多源结果去重（按 URL 去重），保留最早出现的"""
    seen_urls = set()
    deduped = []
    for r in all_results:
        url = r.get("url", "").rstrip("/")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append(r)
    return deduped


# ── DashengTool 注册 ──

class WebSearchInput(BaseModel):
    query: str = Field(description="搜索关键词")
    max_results: int = Field(default=10, description="最多返回的结果数，默认10")


def _web_search_impl(query: str, max_results: int = 10) -> str:
    """核心逻辑 — 多源并行搜索"""
    try:
        sources = {
            "AnySearch": _search_anysearch,
            "Bing": _search_bing,
            "头条": _search_toutiao,
            "DDG": _search_ddg,
        }
        if _BRAVE_API_KEY:
            sources["Brave"] = _search_brave

        all_results = []
        source_names = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(sources)) as executor:
            futures = {
                executor.submit(func, query, max_results * 2): name
                for name, func in sources.items()
            }
            for future in concurrent.futures.as_completed(futures, timeout=20):
                source = futures[future]
                try:
                    results = future.result()
                    if results:
                        all_results.extend(results)
                        source_names.append(source)
                except Exception:
                    continue

        if not all_results:
            return "搜索失败，所有搜索源均无响应。"

        # 去重
        deduped = _dedupe_results(all_results)

        # 格式化结果
        lines = [f"🔍 搜索: {query}  (来源: {', '.join(source_names)})", ""]
        for i, r in enumerate(deduped[:max_results]):
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


web_search = build_tool(
    name="web_search",
    description=(
        "搜索互联网获取信息。并行调用 AnySearch、Bing、头条、DDG、Brave，汇总去重后返回。\n"
        "Args:\n"
        "  query: 搜索关键词\n"
        "  max_results: 最多返回结果数(默认10)"
    ),
    func=_web_search_impl,
    args_schema=WebSearchInput,
    max_result_size=DEFAULT_MAX_RESULT_SIZE_CHARS,
    is_read_only=True,
    is_concurrency_safe=True,   # 网络搜索无副作用，可并行
)