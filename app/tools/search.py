"""Web search (DESIGN.md §9): Tavily when TAVILY_API_KEY is set — a real
web search. Without a key: recent NEWS headlines from two published RSS
feeds (Bing News, then Google News). Good for "what happened to X"; it is
not a general web search, and a Tavily key remains the upgrade.

The keyless path used to scrape DuckDuckGo's HTML page. DuckDuckGo answers
automated clients with its bot check (HTTP 202, no results), so every search
came back empty — 165 of 167 calls when it was measured (2026-11-11). That
check is theirs to make; the desk now asks feeds that are published for
programs, under its own name.
"""
import functools
import html
import ipaddress
import logging
import os
import re
import socket
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse

import httpx

from ..logsetup import swallowed

log = logging.getLogger(__name__)

NEWS_UA = {"User-Agent": "claire-desk/1.0 (news search)"}
MAX_FEED_BYTES = 2_000_000


class SearchError(Exception):
    """No search source could be reached. Distinct from "no results": an
    empty list means a source answered and found nothing."""


# Shared pooled clients: a Client per call was never closed and leaked its
# socket until garbage collection (see tradingview._shared_client).
@functools.cache
def _search_client() -> httpx.Client:
    return httpx.Client(timeout=15, follow_redirects=True)


@functools.cache
def _page_client() -> httpx.Client:
    return httpx.Client(timeout=20, follow_redirects=False,
                        headers={"User-Agent": "Mozilla/5.0"})


def web_search(query: str, *, max_results=8, env=None,
               _client=None) -> list[dict]:
    """[{title, url, snippet, ...}]. Raises SearchError when no source could
    be reached, so an outage is never mistaken for "nothing found"."""
    env = os.environ if env is None else env
    client = _client or _search_client()
    key = env.get("TAVILY_API_KEY")
    if key:
        r = client.post("https://api.tavily.com/search",
                        json={"api_key": key, "query": query,
                              "max_results": max_results})
        r.raise_for_status()
        return [{"title": x.get("title"), "url": x.get("url"),
                 "snippet": (x.get("content") or "")[:400]}
                for x in r.json().get("results", [])]
    return news_search(query, max_results=max_results, _client=client)


# ── keyless: published news feeds ────────────────────────────────────────
def _text(s) -> str:
    """Feed text → plain text: entities decoded, tags dropped, one line."""
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _feed_items(client, url, params) -> list:
    r = client.get(url, params=params, headers=NEWS_UA)
    r.raise_for_status()
    data = r.content
    if len(data) > MAX_FEED_BYTES:
        raise SearchError(f"feed too large ({len(data)} bytes)")
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        # an RSS feed declares neither; entity tricks are not worth parsing
        raise SearchError("feed is not plain RSS")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise SearchError(f"feed is not XML: {e}") from e
    if root.tag != "rss":
        # e.g. an HTML "verify you are human" page that happens to parse:
        # that is a refusal, and must not read as "no news found"
        raise SearchError(f"not an RSS feed (<{root.tag[:20]}>)")
    return root.findall(".//item")


def _bing_news(client, query, n) -> list[dict]:
    """Title, summary, date and the publisher's own URL (Bing wraps it in a
    click-through link; the real address is its `url` parameter)."""
    out = []
    for it in _feed_items(client, "https://www.bing.com/news/search",
                          {"q": query, "format": "rss"}):
        link = it.findtext("link") or ""
        real = parse_qs(urlparse(link).query).get("url")
        out.append({"title": _text(it.findtext("title")),
                    "url": real[0] if real else link,
                    "snippet": _text(it.findtext("description"))[:400],
                    "published": it.findtext("pubDate"),
                    "source": "bing-news"})
        if len(out) >= n:
            break
    return out


def _google_news(client, query, n) -> list[dict]:
    """Headline, publisher and date. The link is Google's own redirect page,
    which fetch_page cannot read through — the headline is the content."""
    out = []
    for it in _feed_items(client, "https://news.google.com/rss/search",
                          {"q": query, "hl": "en-US", "gl": "US",
                           "ceid": "US:en"}):
        out.append({"title": _text(it.findtext("title")),
                    "url": it.findtext("link") or "",
                    "snippet": _text(it.findtext("source")),
                    "published": it.findtext("pubDate"),
                    "source": "google-news",
                    "note": "headline only — this link is a redirect page"})
        if len(out) >= n:
            break
    return out


NEWS_SOURCES = (("bing-news", _bing_news), ("google-news", _google_news))


def news_search(query: str, *, max_results=8, _client=None,
                sources=NEWS_SOURCES) -> list[dict]:
    """First source that has anything. [] only when a source answered with
    nothing; SearchError when none of them could be reached."""
    client = _client or _search_client()
    errors, answered = [], False
    for name, fn in sources:
        try:
            hits = fn(client, query, max_results)
        except Exception as e:                  # noqa: BLE001 — try the next
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:80]}")
            swallowed(log, f"news feed {name} failed", e)
            continue
        answered = True
        if hits:
            return hits
    if not answered:
        raise SearchError("news search unavailable — " + "; ".join(errors))
    return []


class BlockedURL(ValueError):
    """fetch_page refused a URL that points at a non-public address."""


MAX_REDIRECTS = 5


def _check_public(url: str, resolve=socket.getaddrinfo) -> None:
    """Refuse anything but http(s) to public addresses: loopback (claire-api,
    the dashboards), RFC1918/ULA, link-local incl. cloud metadata
    169.254.169.254, CGNAT, multicast, reserved, unspecified. Hostnames are
    resolved and EVERY address must be public."""
    u = httpx.URL(url)
    if u.scheme not in ("http", "https"):
        raise BlockedURL(f"scheme not allowed: {u.scheme!r}")
    host = u.host
    if not host:
        raise BlockedURL("URL has no host")
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        infos = resolve(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as e:
        raise BlockedURL(f"cannot resolve {host}: {e}") from e
    addrs = {i[4][0] for i in infos}
    if not addrs:
        raise BlockedURL(f"cannot resolve {host}")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise BlockedURL(f"{host} resolves to non-public address {ip}")


def fetch_page(url: str, *, max_chars=20000, _client=None,
               _resolve=socket.getaddrinfo) -> str:
    """Size-capped readability-ish extraction: strip tags, collapse space.
    Redirects are followed by hand so every hop is re-checked against
    _check_public (an open redirect must not bounce us onto loopback)."""
    client = _client or _page_client()
    for _ in range(MAX_REDIRECTS + 1):
        _check_public(url, _resolve)
        r = client.get(url, follow_redirects=False)
        if r.is_redirect and "location" in r.headers:
            url = str(r.url.join(r.headers["location"]))
            continue
        break
    else:
        raise BlockedURL(f"more than {MAX_REDIRECTS} redirects")
    r.raise_for_status()
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]
