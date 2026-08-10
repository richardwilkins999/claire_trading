"""Web search (DESIGN.md §9): Tavily when TAVILY_API_KEY is set, else
DuckDuckGo (free, keyless, noticeably weaker — the recommended upgrade is a
Tavily key)."""
import os
import re

import httpx


def web_search(query: str, *, max_results=8, env=os.environ,
               _client=None) -> list[dict]:
    client = _client or httpx.Client(timeout=15, follow_redirects=True)
    key = env.get("TAVILY_API_KEY")
    if key:
        r = client.post("https://api.tavily.com/search",
                        json={"api_key": key, "query": query,
                              "max_results": max_results})
        r.raise_for_status()
        return [{"title": x.get("title"), "url": x.get("url"),
                 "snippet": (x.get("content") or "")[:400]}
                for x in r.json().get("results", [])]
    # DuckDuckGo HTML fallback
    r = client.get("https://html.duckduckgo.com/html/", params={"q": query},
                   headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    out = []
    for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            r.text, re.S):
        url, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        out.append({"title": title, "url": url, "snippet": ""})
        if len(out) >= max_results:
            break
    return out


def fetch_page(url: str, *, max_chars=20000, _client=None) -> str:
    """Size-capped readability-ish extraction: strip tags, collapse space."""
    client = _client or httpx.Client(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"})
    r = client.get(url)
    r.raise_for_status()
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]
