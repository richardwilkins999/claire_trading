"""Screener-agent wiring tests — offline with fake structured LLMs."""
import pytest

from app.accounting import db
from app.providers import seeds
from app.screener import (ScreenerError, Shortlist, gather_candidates,
                          screener_pick)


class FakeMarket:
    def __init__(self, fail=False):
        self.fail = fail

    def screener(self, exchange, sort="intradaymarketcap", start=0, count=100):
        if self.fail:
            raise RuntimeError("429")
        return {"total": 100, "rows": [
            {"symbol": "D05.SI", "name": "DBS Group", "price": 41.2,
             "change_pct": 1.1, "mcap": 8.2e10},
            {"symbol": "C07.SI", "name": "Jardine C&C", "price": 27.0,
             "change_pct": -0.4, "mcap": 1.1e10}]}


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    seeds.seed(c, ts=1)
    return c


def fake_factory(shortlist):
    class LLM:
        def invoke(self, messages):
            self.messages = messages
            return shortlist
    llm = LLM()

    def factory(agent_id, schema, wi):
        assert agent_id == "screener"
        assert schema is Shortlist
        return llm, "you are the screener"
    factory.llm = llm
    return factory


def test_gather_excludes_held(monkeypatch):
    monkeypatch.setattr("app.screener.tradingview.recommendations",
                        lambda ex, count=20: {"rows": []})
    cands = gather_candidates(FakeMarket(), ["SGX"], held={"D05"})
    assert [c["ticker"] for c in cands] == ["C07"]


def test_screener_pick_validates_against_universe(conn, monkeypatch):
    monkeypatch.setattr("app.screener.tradingview.recommendations",
                        lambda ex, count=20: {"rows": []})
    factory = fake_factory(Shortlist(picks=[
        {"ticker": "C07", "exchange": "SGX", "reason": "cheap, catalyst"},
        {"ticker": "HALLUCINATED", "exchange": "SGX", "reason": "made up"}]))
    picks = screener_pick(conn, FakeMarket(), ["SGX"], set(),
                          structured_factory=factory)
    assert [(p.ticker, p.reason) for p in picks] == \
        [("C07", "cheap, catalyst")]                # fabricated pick dropped
    prompt = factory.llm.messages[1][1]
    assert "DBS Group" in prompt                    # real universe shown
    assert "Open markets right now: SGX" in prompt


def test_screener_pick_raises_for_fallback(conn, monkeypatch):
    monkeypatch.setattr("app.screener.tradingview.recommendations",
                        lambda ex, count=20: {"rows": []})
    # all picks hallucinated → error, so caller falls back
    factory = fake_factory(Shortlist(picks=[
        {"ticker": "NOPE", "exchange": "SGX", "reason": "x"}]))
    with pytest.raises(ScreenerError, match="no valid picks"):
        screener_pick(conn, FakeMarket(), ["SGX"], set(),
                      structured_factory=factory)
    # no data from any source → error too
    with pytest.raises(ScreenerError, match="no candidate data"):
        screener_pick(conn, FakeMarket(fail=True), ["SGX"], set(),
                      structured_factory=fake_factory(Shortlist(picks=[])))
    # empty exchange list (all closed) → empty, no LLM call
    assert screener_pick(conn, FakeMarket(), [], set(),
                         structured_factory=None) == []


def test_schedule_rows_name_their_agent(conn):
    from app import scheduler
    scheduler.seed(conn)
    rows = {r["job"]: r for r in scheduler.rows_with_next(conn,
                                                          clock=lambda: 1000)}
    assert rows["analysis_asia"]["agent"] == "screener"
    assert rows["analysis_asia"]["then"] == "full pipeline per pick"
    assert rows["custodian"]["agent"] is None
    assert rows["watcher"]["agent"] is None
    assert "sell_review" in rows["watcher"]["then"]
