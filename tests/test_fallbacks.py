"""Multi-source market data + direct agent-ask tests — all offline."""
import json
from decimal import Decimal

import pytest

from app.accounting import db
from app.api.agent_ask import make_agent_ask
from app.providers import seeds
from app.tools import tradingview
from app.tools.market import Market, MarketError


class FakeTVClient:
    def __init__(self):
        self.bodies = []

    def post(self, url, json=None):
        self.bodies.append((url, json))

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [
                    {"s": "HKEX:700", "d": ["700", 481.4, 0.54, 15508724]}]}
        return R()


def test_tv_quotes_maps_hk_tickers_both_ways():
    c = FakeTVClient()
    out = tradingview.quotes(["0700.HK"], _client=c)
    url, body = c.bodies[0]
    assert "hongkong/scan" in url
    assert body["filter"][0]["right"] == ["700"]        # Yahoo 0700 → TV 700
    assert out["0700.HK"]["price"] == Decimal("481.4")  # keyed back to Yahoo
    assert out["0700.HK"]["currency"] == "HKD"
    assert out["0700.HK"]["src"] == "tradingview"


def test_spark_falls_back_to_tradingview_when_yahoo_throttled():
    def yahoo_down(url, params=None, need_crumb=False):
        raise MarketError("429 Too Many Requests")

    calls = []

    def tv(symbols):
        calls.append(symbols)
        return {s: {"symbol": s, "price": Decimal("100"), "currency": "USD",
                    "stale": False, "series": [], "src": "tradingview"}
                for s in symbols}

    m = Market(_get=yahoo_down, tv_quotes=tv, clock=lambda: 1000)
    q = m.spark(["NVDA"])
    assert q["NVDA"]["src"] == "tradingview"
    m.spark(["NVDA"])
    assert len(calls) == 1                              # fallback cached too
    # symbol no source knows → loud error, not silence
    with pytest.raises(MarketError, match="any source"):
        Market(_get=yahoo_down, tv_quotes=lambda s: {},
               clock=lambda: 1000).spark(["GHOST"])


def test_fx_falls_back_to_ecb():
    def yahoo_down(url, params=None, need_crumb=False):
        raise MarketError("throttled")
    m = Market(_get=yahoo_down, fx_fallback=lambda a, b: "0.78154",
               clock=lambda: 1000)
    assert m.fx("SGD", "USD") == Decimal("0.78154")
    assert m.fx("USD", "USD") == Decimal(1)


class FakeStreamLLM:
    def stream(self, messages):
        self.messages = messages

        class Chunk:
            def __init__(self, content):
                self.content = content
        yield Chunk("I last analysed ")
        yield Chunk("NVDA and found it bullish.")


@pytest.fixture
def world(tmp_path):
    from app.tools.files import Narratives
    conn = db.connect(":memory:")
    db.init(conn)
    seeds.seed(conn, ts=1_786_456_800)
    conn.execute("INSERT INTO work_items (id, kind, ticker, state, thread_id,"
                 " created_at, updated_at) VALUES ('wi_9', 'pipeline', 'NVDA',"
                 " 'done', 'wi_9', 1, 1)")
    conn.execute("INSERT INTO agent_runs (id, agent_id, work_item_id,"
                 " started_at, status, cost_usd) VALUES ('r9', 'technical',"
                 " 'wi_9', 1786456000, 'ok', 0.02)")
    n = Narratives(tmp_path)
    n.write("wi_9", "technical", "# RSI was 71, overbought but trending")
    return conn, n


def test_agent_ask_streams_grounded_answer(world):
    conn, narratives = world
    llm = FakeStreamLLM()
    handler = make_agent_ask(conn, narratives, llm_factory=lambda a: llm,
                             clock=lambda: 1_786_456_800)
    lines = [json.loads(x) for x in handler(
        {"agent_id": "technical", "text": "what did you find?"})]
    kinds = [x["kind"] for x in lines]
    assert kinds[-1] == "done"
    assert lines[-2]["text"] == "I last analysed NVDA and found it bullish."
    system = llm.messages[0][1]
    assert "RSI was 71" in system                   # narrative grounded
    assert "NVDA" in system                         # run history grounded
    assert "DIRECT question" in system


def test_agent_ask_unknown_agent_and_no_key_are_honest(world):
    conn, narratives = world
    handler = make_agent_ask(conn, narratives, clock=lambda: 1_786_456_800)
    lines = [json.loads(x) for x in handler(
        {"agent_id": "nonexistent", "text": "hi"})]
    assert lines[0]["kind"] == "error"
    # real registry path with no key: error, not crash
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        lines = [json.loads(x) for x in handler(
            {"agent_id": "technical", "text": "hi"})]
        assert lines[0]["kind"] == "error"
        assert "ANTHROPIC_API_KEY" in lines[0]["text"]
