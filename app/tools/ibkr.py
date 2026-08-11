"""Interactive Brokers provider — licensed, direct-from-exchange data at
retail prices, via a running TWS / IB Gateway (paper login is fine).

Setup, when you're ready:
  1. Install IB Gateway, log in with the paper account, enable API access
     (Configure → API → Settings: socket port 4002 for paper).
  2. `venv/bin/pip install ib-insync` (deliberately not pinned in
     requirements.txt until you activate this).
  3. In etc/claire.env: IBKR_ENABLED=1, IBKR_HOST=127.0.0.1, IBKR_PORT=4002
  4. Subscribe to market data per exchange in IBKR account management.
Restart claire-api/dashboards; the Providers page data-sources panel goes
green when the gateway answers.
"""
from decimal import Decimal

# yahoo suffix → (IB exchange, currency); SMART routes US symbols
SUFFIX_IB = {".SI": ("SGX", "SGD"), ".HK": ("SEHK", "HKD"),
             ".T": ("TSEJ", "JPY"), ".AX": ("ASX", "AUD"),
             ".DE": ("IBIS", "EUR"), ".PA": ("SBF", "EUR"),
             ".NS": ("NSE", "INR"), ".L": ("LSE", "GBP")}


class IBKRError(Exception):
    pass


class Client:
    def __init__(self, host="127.0.0.1", port=4002, client_id=17):
        try:
            from ib_insync import IB
        except ImportError as e:
            raise IBKRError(
                "ib-insync is not installed — venv/bin/pip install ib-insync"
            ) from e
        self._ib = IB()
        try:
            self._ib.connect(host, int(port), clientId=int(client_id),
                             timeout=5)
        except Exception as e:                  # noqa: BLE001
            raise IBKRError(
                f"cannot reach IB Gateway at {host}:{port} — is it running "
                f"with API enabled? ({e})") from e

    def _contract(self, yahoo_symbol: str):
        from ib_insync import Stock
        for suf, (exch, ccy) in SUFFIX_IB.items():
            if yahoo_symbol.upper().endswith(suf):
                ticker = yahoo_symbol[: -len(suf)]
                if suf == ".HK":
                    ticker = ticker.lstrip("0") or "0"
                return Stock(ticker, exch, ccy)
        return Stock(yahoo_symbol, "SMART", "USD")

    def quote(self, symbols: list[str]) -> dict:
        out = {}
        for sym in symbols:
            [t] = self._ib.reqTickers(self._contract(sym))
            price = t.last or t.close
            if not price or price != price:     # NaN guard
                continue
            out[sym] = {"symbol": sym, "price": Decimal(str(price)),
                        "currency": t.contract.currency, "stale": False,
                        "series": [], "change_pct": None,
                        "volume": t.volume if t.volume == t.volume else None,
                        "previous_close": t.close, "src": "ibkr"}
        return out

    RANGE_IB = {"1mo": "1 M", "3mo": "3 M", "6mo": "6 M", "1y": "1 Y",
                "2y": "2 Y"}

    def chart(self, yahoo_symbol: str, range_="6mo", interval="1d") -> dict:
        bars = self._ib.reqHistoricalData(
            self._contract(yahoo_symbol), endDateTime="",
            durationStr=self.RANGE_IB.get(range_, "6 M"),
            barSizeSetting="1 day" if interval == "1d" else "5 mins",
            whatToShow="TRADES", useRTH=True)
        if not bars:
            raise IBKRError(f"no history for {yahoo_symbol}")
        return {"symbol": yahoo_symbol,
                "timestamps": [int(b.date.strftime("%s")) if hasattr(
                    b.date, "strftime") else 0 for b in bars],
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
                "currency": None, "src": "ibkr"}

    def fx(self, from_ccy: str, to_ccy: str) -> str:
        from ib_insync import Forex
        [t] = self._ib.reqTickers(Forex(f"{from_ccy}{to_ccy}"))
        rate = t.marketPrice()
        if not rate or rate != rate:
            raise IBKRError(f"no FX rate for {from_ccy}{to_ccy}")
        return str(rate)
