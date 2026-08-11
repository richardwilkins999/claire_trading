"""Screener v2 tests — multi-factor universe, context, catalysts, watchlist,
event scan. All offline with fake scans and fake structured LLMs."""
import pytest

from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.providers import seeds
from app.screener import (ScreenerError, Shortlist, catalyst_notes,
                          event_scan, gather_candidates, portfolio_context,
                          recent_tickers, screener_pick, track_record)
from app.tools import tradingview

T0 = 1_786_456_800


def cand(ticker, ex="SGX", **kw):
    base = {"ticker": ticker, "exchange": ex, "name": f"{ticker} Ltd",
            "price": 10.0, "change_pct": 1.0, "volume": 2e6, "mcap": 5e9,
            "rel_volume": None, "off_52w_high_pct": None, "perf_1m": None,
            "rsi": None, "sector": "Finance", "tv_rating": None}
    base.update(kw)
    return base


def fake_scans(mapping):
    """mapping: preset -> list of candidates"""
    def factor_scan(ex, preset, count=10, **kw):
        return [c for c in mapping.get(preset, []) if c["exchange"] == ex]
    return factor_scan


NO_RECS = lambda ex, count=10: {"rows": []}  # noqa: E731


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    seeds.seed(c, ts=T0)
    return c


def test_gather_merges_scans_tags_and_dedupes(conn):
    scans = fake_scans({
        "momentum": [cand("D05", perf_1m=12.0)],
        "unusual_volume": [cand("D05", rel_volume=3.4), cand("C07")],
        "oversold": [cand("HELD")],
    })
    out = {c["ticker"]: c for c in gather_candidates(
        None, ["SGX"], held={"HELD"}, conn=conn,
        _factor_scan=scans, _recs=NO_RECS)}
    assert set(out) == {"D05", "C07"}              # held name excluded
    assert sorted(out["D05"]["scans"]) == ["momentum", "unusual_volume"]
    assert out["D05"]["perf_1m"] == 12.0           # fields merged across scans
    assert out["D05"]["rel_volume"] == 3.4
    assert out["C07"]["scans"] == ["unusual_volume"]


def test_gather_includes_watchlist_and_survives_a_dead_scan(conn):
    conn.execute("INSERT INTO watchlist (ticker, exchange, note, added_at)"
                 " VALUES ('C6L', 'SGX', 'SIA', ?)", (T0,))

    def half_broken(ex, preset, count=10, **kw):
        if preset == "momentum":
            raise RuntimeError("scanner down")
        return [cand("D05")] if preset == "unusual_volume" else []
    out = {c["ticker"]: c for c in gather_candidates(
        None, ["SGX"], held=set(), conn=conn,
        _factor_scan=half_broken, _recs=NO_RECS)}
    assert set(out) == {"D05", "C6L"}              # one dead scan ≠ no universe
    assert out["C6L"]["scans"] == ["watchlist"]


def test_context_helpers(conn):
    repo = Repo(conn)
    repo.create_account("acct", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    repo.add_instrument("SGX:D05", "D05", "SGX", "SGD")
    repo.deposit("acct", to_micro("10000"), ts=T0)
    repo.create_order(id="o1", account_id="acct", instrument_id="SGX:D05",
                      side="buy", qty=to_micro("100"), expires_at=T0 + 999,
                      ts=T0)
    repo.record_fill("o1", broker_fill_id="f1", qty=to_micro("100"),
                     price_native=to_micro("40"), fx_rate=to_micro("1"),
                     ts=T0)
    assert "D05" in portfolio_context(conn)
    assert "diversify" in portfolio_context(conn)
    assert "no completed theses" in track_record(conn)

    conn.execute("INSERT INTO work_items (id, kind, ticker, state, thread_id,"
                 " created_at, updated_at) VALUES ('wi_1','pipeline','NVDA',"
                 " 'done','wi_1',?,?)", (T0 - 3600, T0))
    assert recent_tickers(conn, clock=lambda: T0) == {"NVDA"}
    assert recent_tickers(conn, clock=lambda: T0 + 10 * 86400) == set()


def test_catalyst_notes_are_bounded_and_ranked():
    asked = []

    def search(q):
        asked.append(q)
        return [{"title": f"headline for {q.split()[0]}"}]
    cands = [cand(f"T{i}", rel_volume=i) for i in range(12)]
    cands[3]["scans"] = ["momentum", "unusual_volume"]   # strongest signal
    for c in cands:
        c.setdefault("scans", ["momentum"])
    notes = catalyst_notes(cands, search_fn=search, limit=4)
    assert len(asked) == 4                          # bounded, not 12
    assert "T3" in notes                            # multi-scan name ranked in
    # a failing search never breaks screening
    def boom(q):
        raise RuntimeError("no network")
    assert catalyst_notes(cands, search_fn=boom, limit=2) == {}


def fake_factory(shortlist, capture):
    class LLM:
        def invoke(self, messages):
            capture.append(messages)
            return shortlist
    return lambda agent_id, schema, wi: (LLM(), "you are the screener")


def test_screener_pick_uses_context_and_validates(conn):
    conn.execute("INSERT INTO work_items (id, kind, ticker, state, thread_id,"
                 " created_at, updated_at) VALUES ('wi_1','pipeline','STALE',"
                 " 'done','wi_1',?,?)", (T0 - 3600, T0))
    scans = fake_scans({"momentum": [cand("D05", perf_1m=12.0,
                                          rel_volume=3.1),
                                     cand("STALE")]})
    seen = []
    factory = fake_factory(Shortlist(picks=[
        {"ticker": "D05", "exchange": "SGX", "reason": "rel-vol 3.1x"},
        {"ticker": "FAKE", "exchange": "SGX", "reason": "invented"}]), seen)
    picks = screener_pick(
        conn, None, ["SGX"], set(), structured_factory=factory,
        clock=lambda: T0, with_catalysts=False,
        _gather=lambda *a, **k: gather_candidates(
            None, ["SGX"], set(), conn=conn, _factor_scan=scans,
            _recs=NO_RECS))
    assert [(p.ticker, p.reason) for p in picks] == [("D05", "rel-vol 3.1x")]
    prompt = seen[0][1][1]
    assert "found by: momentum" in prompt            # scan provenance shown
    assert "rel-vol 3.1x" in prompt                  # numbers shown
    assert "STALE" in prompt.split("Analysed in the last")[1][:80]
    assert "track record" in prompt.lower()
    assert "Portfolio" in prompt


def test_screener_pick_raises_so_caller_falls_back(conn):
    scans = fake_scans({"momentum": [cand("D05")]})
    gather = lambda *a, **k: gather_candidates(  # noqa: E731
        None, ["SGX"], set(), conn=conn, _factor_scan=scans, _recs=NO_RECS)
    with pytest.raises(ScreenerError, match="no valid picks"):
        screener_pick(conn, None, ["SGX"], set(), with_catalysts=False,
                      structured_factory=fake_factory(
                          Shortlist(picks=[{"ticker": "NOPE",
                                            "exchange": "SGX",
                                            "reason": "x"}]), []),
                      _gather=gather)
    with pytest.raises(ScreenerError, match="no candidate data"):
        screener_pick(conn, None, ["SGX"], set(), with_catalysts=False,
                      structured_factory=fake_factory(Shortlist(picks=[]), []),
                      _gather=lambda *a, **k: [])
    assert screener_pick(conn, None, [], set()) == []   # all markets closed


def test_event_scan_threshold_and_dedupe(conn):
    conn.execute("INSERT INTO work_items (id, kind, ticker, state, thread_id,"
                 " created_at, updated_at) VALUES ('wi_1','pipeline','SEEN',"
                 " 'done','wi_1',?,?)", (T0 - 600, T0))
    scans = fake_scans({"unusual_volume": [
        cand("SPIKE", rel_volume=4.5), cand("MILD", rel_volume=1.2),
        cand("SEEN", rel_volume=9.9), cand("HELD", rel_volume=8.0)]})
    hits = event_scan(conn, ["SGX"], held={"HELD"}, threshold=3.0,
                      _factor_scan=scans)
    assert [h["ticker"] for h in hits] == ["SPIKE"]


def test_factor_scan_parses_and_drops_preferred_lines():
    class FakeClient:
        def post(self, url, json=None):
            self.body = json

            class R:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return {"data": [
                        {"s": "NASDAQ:AAA", "d": ["AAA", "Alpha Inc", 50.0,
                                                  1.2, 3e6, 8e9, 2.5, 55.0,
                                                  20.0, 12.0, 3.0, 61.0,
                                                  "Technology", 0.6]},
                        {"s": "NYSE:JPM/PM", "d": ["JPM/PM", "Preferred",
                                                   16.0, 0.1, 1e6, 5e9, 1.0,
                                                   17.0, 15.0, 1.0, 0.5,
                                                   31.0, "Finance", 0.1]}]}
            return R()
    assert len(tradingview.FACTOR_COLUMNS) == 14     # rows above match cols
    c = FakeClient()
    rows = tradingview.factor_scan("NASDAQ", "momentum", _client=c)
    assert [r["ticker"] for r in rows] == ["AAA"]    # preferred line dropped
    assert rows[0]["scan"] == "momentum"
    assert rows[0]["off_52w_high_pct"] == pytest.approx(-9.09, abs=0.1)
    assert rows[0]["rel_volume"] == 2.5
    assert any(f.get("left") == "typespecs" for f in c.body["filter"])


def test_schedule_rows_name_their_agent(conn):
    from app import scheduler
    scheduler.seed(conn)
    rows = {r["job"]: r for r in scheduler.rows_with_next(conn,
                                                          clock=lambda: 1000)}
    assert rows["analysis_asia"]["agent"] == "screener"
    assert rows["custodian"]["agent"] is None
    assert "sell_review" in rows["watcher"]["then"]
    assert rows["event_scan"]["spec"]["threshold"] == 3.0
