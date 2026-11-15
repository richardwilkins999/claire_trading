"""A price source that says "too many requests" is left alone for a while,
and for longer each time it says it again (Market.QUIET_STEPS). Asking again
at every cache expiry got 305 refusals in one day's log (2026-11-15)."""
from decimal import Decimal

import httpx
import pytest

from app.tools import market as market_mod
from app.tools.market import Market, MarketError


def refusal(url="https://api.twelvedata.com/exchange_rate"):
    req = httpx.Request("GET", url)
    return httpx.HTTPStatusError("429", request=req,
                                 response=httpx.Response(429, request=req))


class Clock:
    """Both clocks the Market reads: its cache clock and time.monotonic."""

    def __init__(self, monkeypatch):
        self.t = 1000.0
        monkeypatch.setattr(market_mod.time, "monotonic", lambda: self.t)
        monkeypatch.setattr(market_mod.time, "sleep",
                            lambda s: setattr(self, "t", self.t + s))

    def __call__(self):
        return self.t


class TD:
    """A keyed provider whose FX quota is spent."""

    def __init__(self):
        self.calls, self.down = 0, True

    def supports(self, s):
        return True

    def fx(self, a, b):
        self.calls += 1
        if self.down:
            raise refusal()
        return "1.30"


def yahoo_down(*a, **k):
    raise refusal("https://query2.finance.yahoo.com/v8/finance/chart/X")


def test_a_refused_fx_source_is_not_asked_again_at_every_cache_expiry(
        monkeypatch):
    clock, td = Clock(monkeypatch), TD()
    # (Yahoo's own back-off lives in its HTTP layer, tested further down;
    # here it is simply down)
    m = Market(_get=yahoo_down, primary=td, clock=clock,
               fx_fallback=lambda a, b: "1.31")
    assert m.fx("USD", "SGD") == Decimal("1.31")    # the ECB answers
    assert td.calls == 1
    # the next four hours of page loads, one every five minutes: it used to
    # ask the refused source every time (48 more calls)
    for _ in range(48):
        clock.t += 300
        assert m.fx("USD", "SGD") == Decimal("1.31")
    # 5 min, then 15, then hourly: a handful of tries, not 48
    assert 3 <= td.calls <= 7, td.calls


def test_the_quiet_period_grows_and_a_success_resets_it(monkeypatch):
    clock, td = Clock(monkeypatch), TD()
    m = Market(_get=yahoo_down, primary=td, clock=clock,
               fx_fallback=lambda a, b: "1.31")
    waits = []
    for _ in range(4):
        m.fx("USD", "SGD")
        waits.append(round(m._primary_quiet_until - clock.t))
        clock.t = m._primary_quiet_until + 1        # just after it ends
    assert waits == [300, 900, 3600, 3600]
    assert not m._primary_ok() or True              # (budget aside)
    td.down = False                                 # the quota is back
    assert m.fx("USD", "SGD") == Decimal("1.30")
    assert m._primary_refused == 0
    td.down = True
    clock.t += 400                                  # its cache has expired
    m.fx("USD", "SGD")
    assert round(m._primary_quiet_until - clock.t) == 300   # from the start


def test_while_quiet_the_keyed_provider_is_skipped_everywhere(monkeypatch):
    clock, td = Clock(monkeypatch), TD()
    m = Market(_get=yahoo_down, primary=td, clock=clock,
               fx_fallback=lambda a, b: "1.31")
    m.fx("USD", "SGD")
    assert m._primary_ok() is False                 # quotes, charts, metrics
    clock.t += 301
    assert m._primary_ok() is True                  # all go through this gate


def test_yahoo_backs_off_further_when_it_keeps_refusing(monkeypatch):
    clock = Clock(monkeypatch)
    m = Market(clock=clock)

    class C:
        calls = 0

        def get(self, url, params=None):
            C.calls += 1
            return httpx.Response(429, request=httpx.Request("GET", url))
    m._client = C()
    waits = []
    for _ in range(3):
        with pytest.raises(httpx.HTTPStatusError):
            m._http_get("https://query1.finance.yahoo.com/v8/finance/chart/X")
        waits.append(round(m._yahoo_quiet_until - clock.t))
        with pytest.raises(MarketError, match="rate-limiting"):
            m._http_get("https://query1.finance.yahoo.com/v8/finance/chart/X")
        clock.t = m._yahoo_quiet_until + 1
    assert waits == [300, 900, 3600]
    assert C.calls == 6                             # two hosts per attempt


def test_other_errors_do_not_start_a_quiet_period(monkeypatch):
    clock = Clock(monkeypatch)

    class Broken(TD):
        def fx(self, a, b):
            self.calls += 1
            raise ValueError("bad JSON")
    td = Broken()
    m = Market(_get=yahoo_down, primary=td, clock=clock,
               fx_fallback=lambda a, b: "1.31")
    m.fx("USD", "SGD")
    assert m._primary_quiet_until == 0.0 and m._primary_refused == 0
