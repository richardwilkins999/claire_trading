"""Twelve Data provider (https://twelvedata.com) — a real, keyed market-data
API covering all ten desk exchanges. Activates when TWELVEDATA_API_KEY is in
etc/claire.env; Market then prefers it over Yahoo for quotes, charts, and FX.

Free tier ≈ 800 credits/day (1 credit per symbol-request) — the Market cache
layer keeps usage well inside that; paid tiers lift the ceiling.
"""
from decimal import Decimal

import httpx

BASE = "https://api.twelvedata.com"
# yahoo suffix → Twelve Data exchange parameter
SUFFIX_EXCHANGE = {".SI": "SGX", ".HK": "HKEX", ".T": "JPX", ".AX": "ASX",
                   ".DE": "XETR", ".PA": "Euronext", ".NS": "NSE",
                   ".L": "LSE"}


class TwelveDataError(Exception):
    pass


def _split(yahoo_symbol: str):
    for suf, exch in SUFFIX_EXCHANGE.items():
        if yahoo_symbol.upper().endswith(suf):
            return yahoo_symbol[: -len(suf)], exch
    return yahoo_symbol, None


class Client:
    def __init__(self, api_key: str, *, _client=None):
        self.key = api_key
        self._client = _client or httpx.Client(timeout=15)

    def _get(self, path, **params):
        params["apikey"] = self.key
        r = self._client.get(f"{BASE}/{path}", params=params)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("status") == "error":
            raise TwelveDataError(data.get("message", "error")[:200])
        return data

    def quote(self, symbols: list[str]) -> dict:
        out = {}
        for sym in symbols:                     # 1 credit each; cache upstream
            ticker, exch = _split(sym)
            params = {"symbol": ticker}
            if exch:
                params["exchange"] = exch
            d = self._get("quote", **params)
            price = d.get("close") or d.get("previous_close")
            if price is None:
                continue
            from .market import normalize_price
            ccy = d.get("currency") or "USD"
            px, ccy = normalize_price(price, "GBp" if ccy == "GBX" else ccy)
            pct = d.get("percent_change")
            out[sym] = {"symbol": sym, "price": px, "currency": ccy,
                        "stale": not d.get("is_market_open", True),
                        "series": [],
                        "change_pct": float(pct) if pct is not None else None,
                        "volume": int(d["volume"]) if d.get("volume") else None,
                        "previous_close": d.get("previous_close"),
                        "src": "twelvedata"}
        return out

    RANGE_SIZE = {"1mo": 22, "3mo": 66, "6mo": 130, "1y": 260, "2y": 520}

    def chart(self, yahoo_symbol: str, range_="6mo", interval="1d") -> dict:
        ticker, exch = _split(yahoo_symbol)
        params = {"symbol": ticker,
                  "interval": "1day" if interval == "1d" else "5min",
                  "outputsize": self.RANGE_SIZE.get(range_, 130)}
        if exch:
            params["exchange"] = exch
        d = self._get("time_series", **params)
        values = list(reversed(d.get("values") or []))
        if not values:
            raise TwelveDataError(f"no series for {yahoo_symbol}")
        from datetime import datetime, timezone

        def epoch(s):
            return int(datetime.fromisoformat(s).replace(
                tzinfo=timezone.utc).timestamp())
        return {"symbol": yahoo_symbol,
                "timestamps": [epoch(v["datetime"]) for v in values],
                "open": [float(v["open"]) for v in values],
                "high": [float(v["high"]) for v in values],
                "low": [float(v["low"]) for v in values],
                "close": [float(v["close"]) for v in values],
                "volume": [int(v.get("volume") or 0) for v in values],
                "currency": (d.get("meta") or {}).get("currency"),
                "src": "twelvedata"}

    def fx(self, from_ccy: str, to_ccy: str) -> str:
        d = self._get("exchange_rate", symbol=f"{from_ccy}/{to_ccy}")
        return str(Decimal(str(d["rate"])))
