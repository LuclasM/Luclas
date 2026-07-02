"""
tools/web.py — 网络搜索和页面抓取
"""

import re
import requests

WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web via DuckDuckGo, returning a list of titles, snippets, and URLs.",
        "parameters": {
            "type": "object",
            "properties": {
                "query":       {"type": "string", "description": "Search keyword"},
                "max_results": {"type": "integer", "description": "Max number of results, default 5", "default": 5},
            },
            "required": ["query"],
        },
    },
}

WEB_FETCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": "Fetch the plain-text content of a given URL, truncated past 6000 characters.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Target page URL"},
            },
            "required": ["url"],
        },
    },
}


def web_search(query: str, max_results: int = 5) -> dict:
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return {"results": results, "count": len(results)}
    except ImportError:
        return {"error": "Missing dependency, install with: pip install ddgs"}
    except Exception as e:
        return {"error": str(e)}


def web_fetch(url: str, max_chars: int = 6000) -> dict:
    try:
        resp = requests.get(
            url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; EVA4-bot/1.0)"},
        )
        resp.raise_for_status()
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", resp.text,
                      flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        truncated = len(text) > max_chars
        return {
            "content":   text[:max_chars],
            "url":       url,
            "status":    resp.status_code,
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": str(e)}
