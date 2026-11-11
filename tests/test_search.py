"""Web search without a key (REVIEW-2026-11-11 B4).

The keyless path scraped DuckDuckGo, which answers programs with its bot
check — every search came back empty and the news agent worked from
nothing. It now reads two published news feeds, and an outage raises
instead of looking like "no results".
"""
import json

import httpx
import pytest

from app.tools import search
from app.tools.search import SearchError, news_search, web_search

BING = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:News="https://www.bing.com/news/search"><channel>
<title>nvidia - BingNews</title>
<item><title>Nvidia &amp; the chip rally</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;tid=abc&amp;url=https%3a%2f%2fexample.com%2fnews%2fnvda%3fa%3d1&amp;c=1</link>
<description>Shares &lt;b&gt;rose&lt;/b&gt; 4%   after results.</description>
<pubDate>Tue, 10 Nov 2026 15:08:02 GMT</pubDate></item>
<item><title>Second story</title><link>https://example.org/direct</link>
<description>Plain link.</description>
<pubDate>Tue, 10 Nov 2026 14:00:00 GMT</pubDate></item>
<item><title>Third</title><link>https://example.org/3</link>
<description>x</description></item>
</channel></rss>"""

GOOGLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>"nvidia" - Google News</title>
<item><title>Chip stocks rally - CNBC</title>
<link>https://news.google.com/rss/articles/CBMiABC</link>
<pubDate>Tue, 10 Nov 2026 13:19:13 GMT</pubDate>
<description>&lt;a href="x"&gt;Chip stocks rally&lt;/a&gt;</description>
<source url="https://www.cnbc.com">CNBC</source></item>
</channel></rss>"""


class Feeds:
    """A fake httpx client: host fragment -> bytes, status code or error."""
    def __init__(self, **by_host):
        self.by_host, self.calls = by_host, []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        for frag, answer in self.by_host.items():
            if frag in url:
                if isinstance(answer, Exception):
                    raise answer
                req = httpx.Request("GET", url)
                if isinstance(answer, int):
                    return httpx.Response(answer, request=req, content=b"")
                return httpx.Response(200, request=req, content=answer)
        raise AssertionError(f"unexpected URL {url}")

    def post(self, *a, **k):
        raise AssertionError("Tavily must not be called without a key")


def test_keyless_search_reads_the_news_feed():
    c = Feeds(bing=BING)
    hits = web_search("Nvidia earnings", env={}, _client=c)
    assert [h["title"] for h in hits] == ["Nvidia & the chip rally",
                                          "Second story", "Third"]
    # the publisher's own address, not Bing's click-through link
    assert hits[0]["url"] == "https://example.com/news/nvda?a=1"
    assert hits[1]["url"] == "https://example.org/direct"
    assert hits[0]["snippet"] == "Shares rose 4% after results."
    assert hits[0]["published"].startswith("Tue, 10 Nov 2026")
    url, params, headers = c.calls[0]
    assert params == {"q": "Nvidia earnings", "format": "rss"}
    # asks under its own name — no browser impersonation
    assert headers["User-Agent"].startswith("claire-desk/")
    assert "duckduckgo" not in json.dumps([u for u, _, _ in c.calls])


def test_max_results_is_respected():
    assert len(news_search("x", max_results=2, _client=Feeds(bing=BING))) == 2


def test_second_feed_is_used_when_the_first_fails():
    c = Feeds(bing=503, google=GOOGLE)
    hits = news_search("nvidia", _client=c)
    assert hits[0]["title"] == "Chip stocks rally - CNBC"
    assert hits[0]["snippet"] == "CNBC" and hits[0]["source"] == "google-news"
    assert "redirect" in hits[0]["note"]        # tells the agent not to fetch
    assert len(c.calls) == 2


def test_second_feed_is_used_when_the_first_is_empty():
    empty = b"<rss><channel><title>t</title></channel></rss>"
    hits = news_search("nvidia", _client=Feeds(bing=empty, google=GOOGLE))
    assert hits and hits[0]["source"] == "google-news"


def test_an_outage_raises_instead_of_looking_like_no_results():
    c = Feeds(bing=httpx.ConnectError("refused"), google=429)
    with pytest.raises(SearchError, match="unavailable") as e:
        web_search("nvidia", env={}, _client=c)
    assert "bing-news" in str(e.value) and "google-news" in str(e.value)


def test_no_results_is_an_empty_list_not_an_error():
    empty = b"<rss><channel><title>t</title></channel></rss>"
    assert news_search("zzzz", _client=Feeds(bing=empty, google=empty)) == []
    # one feed down, the other answered with nothing: still "no results"
    assert news_search("zzzz", _client=Feeds(bing=500, google=empty)) == []


@pytest.mark.parametrize("body", [
    b"<html><body>Please verify you are human</body></html>",   # a bot check
    b"not xml at all <<<",
    b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss>&a;</rss>',
    b"<rss>" + b"x" * (search.MAX_FEED_BYTES + 1) + b"</rss>",
])
def test_a_feed_that_is_not_plain_rss_counts_as_unreachable(body):
    """A bot-check page is a refusal. Reading it as "no news found" is the
    very failure this replaced, so it must raise, not return []."""
    with pytest.raises(SearchError, match="unavailable"):
        news_search("nvidia", _client=Feeds(bing=body, google=body))
    # and one good feed is still enough
    hits = news_search("nvidia", _client=Feeds(bing=body, google=GOOGLE))
    assert hits[0]["source"] == "google-news"


def test_tavily_is_still_used_when_a_key_is_set():
    class Tavily:
        def post(self, url, json=None):
            assert url == "https://api.tavily.com/search"
            assert json["api_key"] == "tv-test" and json["max_results"] == 3
            return httpx.Response(
                200, request=httpx.Request("POST", url),
                json={"results": [{"title": "T", "url": "https://t.example",
                                   "content": "c" * 900}]})

        def get(self, *a, **k):
            raise AssertionError("the feeds are not asked when a key is set")
    hits = web_search("q", max_results=3, env={"TAVILY_API_KEY": "tv-test"},
                      _client=Tavily())
    assert hits == [{"title": "T", "url": "https://t.example",
                     "snippet": "c" * 400}]


def test_env_none_means_the_process_environment(monkeypatch):
    """lc_tools and the screener passed `env or {}`, so a key in claire.env
    was never seen. None now reads os.environ."""
    seen = {}

    class C:
        def post(self, url, json=None):
            seen["key"] = json["api_key"]
            return httpx.Response(200, request=httpx.Request("POST", url),
                                  json={"results": []})
    monkeypatch.setenv("TAVILY_API_KEY", "tv-from-env")
    web_search("q", _client=C())
    assert seen["key"] == "tv-from-env"


def test_screener_catalyst_check_uses_the_process_environment(monkeypatch):
    from app import screener
    seen = {}

    def fake(q, max_results=8, env=None, _client=None):
        seen.update(q=q, env=env, n=max_results)
        return [{"title": "Nvidia beats", "url": "u", "snippet": ""}]
    monkeypatch.setattr(search, "web_search", fake)
    notes = screener.catalyst_notes(
        [{"ticker": "NVDA", "name": "Nvidia", "scans": ["a"]}])
    assert notes == {"NVDA": "Nvidia beats"}
    assert seen["env"] is None and seen["n"] == 3   # not {} any more


def test_real_search_is_blocked_in_the_suite():
    with pytest.raises(SearchError, match="offline test"):
        web_search("anything", env={})
