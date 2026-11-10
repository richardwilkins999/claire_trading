"""Daily/intraday price bars from Saxo's chart API — the fallback when Yahoo
rate-limits this machine (Twelve Data's free tier covers US listings only, so
an SGX/HKEX/... chart otherwise has no source at all).

Read-only: two GET endpoints (instrument search, chart bars), on the SIM
sign-in by default (CLAIRE_SAXO_CHART_ENV=live to use the live one). Nothing
here touches /trade/. Where Saxo lists a market only as a CFD (e.g. SGX
shares on SIM), the CFD's bars are used: a CFD on a stock tracks the
stock's own price, which is all a chart needs.
"""
import os
import threading
import time
from datetime import datetime

import httpx

from . import saxo_live
from .saxo_broker import MIC_BY_SUFFIX, split_symbol

ASSET_TYPES = ("Stock", "Etf", "CfdOnStock", "CfdOnEtf")
HORIZON = {"1d": 1440, "1wk": 10080, "60m": 60, "1h": 60, "30m": 30,
           "15m": 15, "5m": 5, "1m": 1}
SESSIONS = {"1d": 1, "5d": 5, "1mo": 22, "3mo": 66, "6mo": 130, "1y": 260,
            "2y": 520, "5y": 1300, "max": 1300}
MAX_COUNT = 1200                       # Saxo's per-request ceiling

_lock = threading.Lock()
_last = [0.0]
_uic: dict = {}


class SaxoChartError(Exception):
    pass


def _env():
    w = (os.environ.get("CLAIRE_SAXO_CHART_ENV") or "sim").lower()
    return w if w in saxo_live.ENVS else "sim"


def _get(path, params, *, which, client):
    with _lock:                         # be gentle: ~2 requests a second
        wait = _last[0] + 0.5 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
    token = saxo_live.access_token(which)
    r = client.get(saxo_live.api_base(which) + path, params=params,
                   headers={"Authorization": f"Bearer {token}"})
    if r.status_code >= 400:
        raise SaxoChartError(f"Saxo {path}: HTTP {r.status_code}")
    return r.json()


def _instrument(symbol, *, which, client):
    if symbol in _uic:
        return _uic[symbol]
    ticker, suffix = split_symbol(symbol)
    if suffix not in MIC_BY_SUFFIX:
        raise SaxoChartError(f"no Saxo exchange mapping for {symbol}")
    if suffix == "HK" and ticker.isdigit():   # Yahoo 0700.HK, Saxo 00700
        ticker = ticker.zfill(5)
    wanted = [f"{ticker}:{m}".lower() for m in MIC_BY_SUFFIX[suffix]]
    found = _get("/ref/v1/instruments/",
                 {"Keywords": ticker, "AssetTypes": ",".join(ASSET_TYPES),
                  "$top": 50}, which=which, client=client).get("Data", [])
    rank = {a: i for i, a in enumerate(ASSET_TYPES)}
    hits = sorted((i for i in found
                   if (i.get("Symbol") or "").lower() in wanted
                   and i.get("AssetType") in rank),
                  key=lambda i: rank[i["AssetType"]])
    if not hits:
        raise SaxoChartError(f"Saxo lists no instrument {' or '.join(wanted)}")
    h = hits[0]
    _uic[symbol] = (h["Identifier"], h["AssetType"], h.get("CurrencyCode"))
    return _uic[symbol]


# Saxo market code → (desk exchange, Yahoo suffix): the inverse of
# MIC_BY_SUFFIX, so a search hit becomes the symbol every other page uses
_DESK_EX = {"xnas": ("NASDAQ", ""), "xnys": ("NYSE", ""), "xses": ("SGX", "SI"),
            "xhkg": ("HKEX", "HK"), "xtks": ("TSE", "T"), "xasx": ("ASX", "AX"),
            "xlon": ("LSE", "L"), "xetr": ("XETRA", "DE"), "xpar": ("PARIS", "PA"),
            "xnse": ("NSE", "NS")}


def search(q, *, client=None, limit=20):
    """Worldwide instrument search at Saxo (the desk's SIM sign-in) — the
    watchlist's search when Yahoo refuses us. Only markets the desk trades
    are returned, as Yahoo-style symbols ("IBM", "DBK.DE", "0700.HK")."""
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        found = _get("/ref/v1/instruments/",
                     {"Keywords": q, "AssetTypes": "Stock,Etf", "$top": 50},
                     which=_env(), client=client).get("Data", [])
    finally:
        if own:
            client.close()
    out = []
    for i in found:
        tk, _, mic = (i.get("Symbol") or "").partition(":")
        if mic.lower() not in _DESK_EX or not tk:
            continue
        ex, suf = _DESK_EX[mic.lower()]
        if suf == "HK" and tk.isdigit():        # Saxo 00700 → Yahoo 0700.HK
            tk = str(int(tk)).zfill(4)
        out.append({"symbol": tk.upper() + (f".{suf}" if suf else ""),
                    "name": i.get("Description"), "exchange": ex,
                    "currency": i.get("CurrencyCode"),
                    "type": i.get("AssetType"), "src": "saxo"})
        if len(out) >= limit:
            break
    return out


def chart(symbol, range_="6mo", interval="1d", *, client=None):
    """Bars in the desk's chart shape (same keys as Market._chart_yahoo)."""
    if interval not in HORIZON:
        raise SaxoChartError(f"interval {interval} not supported by Saxo")
    horizon = HORIZON[interval]
    sessions = SESSIONS.get(range_, 130)
    per_day = 1 if horizon >= 1440 else max(1, 8 * 60 // horizon)
    count = sessions * per_day if horizon < 10080 else sessions // 5 + 1
    which = _env()
    c = client or httpx.Client(timeout=20)
    try:
        uic, asset, ccy = _instrument(symbol, which=which, client=c)
        data = _get("/chart/v3/charts",
                    {"Uic": uic, "AssetType": asset, "Horizon": horizon,
                     "Count": min(max(count, 2), MAX_COUNT)},
                    which=which, client=c).get("Data", [])
    finally:
        if client is None:
            c.close()
    if not data:
        raise SaxoChartError(f"Saxo returned no bars for {symbol}")
    ts = [int(datetime.fromisoformat(b["Time"].replace("Z", "+00:00"))
              .timestamp()) for b in data]
    # CFD bars carry Bid/Ask variants on some feeds; take the mid when so
    pick = lambda b, k: (b.get(k) if b.get(k) is not None else  # noqa: E731
                         ((b.get(k + "Bid") or 0) + (b.get(k + "Ask") or 0)) / 2
                         if b.get(k + "Bid") is not None else None)
    return {"symbol": symbol, "timestamps": ts,
            "open": [pick(b, "Open") for b in data],
            "high": [pick(b, "High") for b in data],
            "low": [pick(b, "Low") for b in data],
            "close": [pick(b, "Close") for b in data],
            "volume": [b.get("Volume") for b in data],
            "currency": ccy, "src": "saxo" + ("-cfd" if "Cfd" in asset else ""),
            "stale": False}
