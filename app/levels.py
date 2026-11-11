"""Price levels worked out from daily bars (recent range, moving averages),
and the units a quote arrives in. Lived in dashboards.py, where watchlist.py
reached in for the private names (REVIEW-2026-11-11 §3.2, step 2)."""


def _levels(c):
    """Reference levels from daily bars: SMA20/50, the 20-day swing low and
    high, the day's range, and ATR14 (Wilder) — the typical daily move that
    the suggested stop is sized from."""
    hi, lo, cl = c.get("high") or [], c.get("low") or [], c.get("close") or []
    bars = [(h, l, x) for h, l, x in zip(hi, lo, cl)
            if None not in (h, l, x)]
    if len(bars) < 15:
        return {}
    closes = [b[2] for b in bars]
    trs = [max(h - l, abs(h - pc), abs(l - pc))
           for (h, l, _), (_, _, pc) in zip(bars[1:], bars[:-1])]
    atr = sum(trs[:14]) / 14
    for tr in trs[14:]:
        atr = (atr * 13 + tr) / 14

    def sma(n):
        return round(sum(closes[-n:]) / n, 4) if len(closes) >= n else None
    last20 = bars[-20:]
    return {"sma20": sma(20), "sma50": sma(50), "atr14": round(atr, 4),
            "low20": round(min(b[1] for b in last20), 4),
            "high20": round(max(b[0] for b in last20), 4),
            "day_high": round(bars[-1][0], 4), "day_low": round(bars[-1][1], 4),
            "bars": len(bars)}


LEVEL_KEYS = ("sma20", "sma50", "atr14", "low20", "high20", "day_high",
              "day_low")


def quote_units(d, price, keys=LEVEL_KEYS, probe=None):
    """Price levels in the same unit as the live quote. London bars arrive
    in PENCE — from Yahoo (labelled GBp) and from Saxo's chart (mislabelled
    GBP) — while the quote is normalised to pounds, so a 9.48 GBP stock had
    an SMA of 934.6 and an ATR of 31.4 (2026-11-10). When the levels sit
    ~100× the price, they are pence: divide them by 100."""
    if not d or not price:
        return d
    ref = d.get(probe or keys[0]) or next(
        (d[k] for k in keys if d.get(k)), None)
    if not ref or not 30 < float(ref) / float(price) < 300:
        return d
    return {**d, **{k: round(d[k] / 100, 6) for k in keys
                    if isinstance(d.get(k), (int, float))}}


def _range(c):
    """{high, low, high_at, low_at} over a chart's bars (weekly bars: their
    high/low include every day of the week) — the 2-year range on the card."""
    ts = c.get("timestamps") or []
    pts = [(h, l, t) for h, l, t in zip(c.get("high") or [], c.get("low") or [],
                                         ts or [None] * len(c.get("high") or []))
           if h is not None and l is not None]
    if len(pts) < 10:
        return None
    hi = max(pts, key=lambda p: p[0])
    lo = min(pts, key=lambda p: p[1])
    return {"high": round(hi[0], 4), "low": round(lo[1], 4),
            "high_at": hi[2], "low_at": lo[2], "bars": len(pts)}
