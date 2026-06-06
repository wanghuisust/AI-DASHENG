"""网络搜索工具 — 用 DuckDuckGo 免费 API，无需 API Key"""

import json
import urllib.request
import urllib.parse
from langchain_core.tools import tool


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """搜索互联网获取信息。当用户问的问题需要最新资讯、事实查证或你不了解的知识时使用。

    Args:
        query: 搜索关键词
        max_results: 最多返回的结果数，默认5
    """
    try:
        # DuckDuckGo Instant Answer API
        url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = []

        # 主要答案
        if data.get("AbstractText"):
            results.append(f"📝 {data['AbstractText']}")
            if data.get("AbstractURL"):
                results.append(f"🔗 {data['AbstractURL']}")

        # 相关主题
        for topic in data.get("RelatedTopics", [])[:max_results]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append(f"• {topic['Text']}")
                if topic.get("FirstURL"):
                    results.append(f"  🔗 {topic['FirstURL']}")

        if not results:
            # 备用：用 Bing 搜索结果页（只拿标题摘要）
            results.append(f"未找到直接结果，建议访问：https://www.bing.com/search?q={urllib.parse.quote(query)}")

        return "\n".join(results)

    except Exception as e:
        return f"搜索失败: {e}"
