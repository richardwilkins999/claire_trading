"""A throwaway dashboard for looking at the pages: `python tests/preview_server.py`
serves http://127.0.0.1:7799 from a THROWAWAY database of made-up data
(two currencies, a few positions, orders and events). Dashboard key:
"preview-key".

It can reach NOTHING real. The same guards as tests/conftest.py are applied
before the server starts — Saxo sign-in files, Telegram, desktop
notifications, web search, the Claude SDK — because a page that loads calls
its API on its own: on 2026-11-11 a preview without these guards read the
real Saxo LIVE account just by opening the Portfolio and Saxo pages.
No watcher, no scheduler, no custodian, no broker adapters.
"""
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TMP = Path(tempfile.mkdtemp(prefix="claire-preview-"))


def isolate():
    import claude_agent_sdk

    from app import alerts
    from app.providers import claude_sdk
    from app.tools import saxo_live, search
    saxo_live.token_file = lambda which: TMP / f"saxo_{which}_token.json"
    saxo_live.alert_state_file = lambda which: TMP / f"saxo_{which}_alert.json"
    saxo_live.TOKEN_FILE = TMP / "saxo_default_token.json"
    alerts.TELEGRAM_ENV = TMP / "telegram.env"
    real_run = alerts.subprocess.run

    def no_notify(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "notify-send":
            class Done:
                returncode = 0
            return Done()
        return real_run(cmd, *a, **kw)
    alerts.subprocess.run = no_notify

    def blocked(*a, **k):
        raise RuntimeError("preview: nothing real is reachable from here")
    search._search_client = blocked
    claude_agent_sdk.query = blocked
    claude_sdk.not_ready = lambda home=None: "preview — not signed in"


isolate()

from app.accounting import db                       # noqa: E402
from app.accounting.money import to_micro           # noqa: E402
from app.accounting.repo import Repo                # noqa: E402
from app.dashboards import create_server            # noqa: E402
from app.providers import seeds                     # noqa: E402
from tests.test_dashboards import FakeMarket        # noqa: E402

import json                                         # noqa: E402
import time                                         # noqa: E402

NOW = int(time.time())
PRICES = {"NVDA": ("120", "USD"), "D05.SI": ("33", "SGD"),
          "U11.SI": ("34.5", "SGD"), "AAPL": ("231", "USD")}


class Market(FakeMarket):
    def _q(self, s):
        px, ccy = PRICES.get(s, ("101", "USD"))
        return {"symbol": s, "name": f"{s} Ltd", "price": Decimal(px),
                "currency": ccy, "stale": False, "series": [99, 100, 101],
                "previous_close": 100, "change_pct": 1.2}

    def spark(self, symbols):
        return {s: self._q(s) for s in symbols}

    def metrics(self, symbols):
        return {s: self._q(s) for s in symbols}

    def quote(self, s):
        return self._q(s)

    def fx(self, a, b):
        return Decimal(1) if a == b else Decimal("1.3")

    def search(self, q):
        return [{"symbol": "NVDA", "name": "NVIDIA", "exchange": "NMS"},
                {"symbol": "AIR.PA", "name": "Airbus", "exchange": "PAR"},
                {"symbol": "SHOP.TO", "name": "Shopify", "exchange": "Toronto"},
                {"symbol": "D05.SI", "name": "DBS Group", "exchange": "SES"}]


def build():
    # a file in the temp dir, not :memory: — pages fire several requests at
    # once, and the server gives each request thread its own connection
    conn = db.connect(TMP / "desk.db")
    db.init(conn)
    seeds.seed(conn, ts=NOW)
    r = Repo(conn)
    r.create_account("alpaca-paper", "alpaca", "paper", "USD",
                     fee_model={"type": "flat", "per_trade": "1"}, ts=NOW)
    r.create_account("saxo-paper", "saxo", "paper", "SGD",
                     fee_model={"type": "flat", "per_trade": "2"}, ts=NOW)
    for iid, tk, ex, ccy in (("NASDAQ:NVDA", "NVDA", "NASDAQ", "USD"),
                             ("NASDAQ:AAPL", "AAPL", "NASDAQ", "USD"),
                             ("SGX:D05", "D05", "SGX", "SGD"),
                             ("SGX:U11", "U11", "SGX", "SGD")):
        r.add_instrument(iid, tk, ex, ccy)
    r.deposit("alpaca-paper", to_micro("100000"), ts=NOW)
    r.deposit("saxo-paper", to_micro("60000"), ts=NOW)
    thesis = {"direction": "buy", "conviction": 0.62, "entry_low": 99,
              "entry_high": 101, "stop_loss": 92, "take_profit": 130}
    for wi, tk, state, ccy in (("wi_2026-11-10_NVDA_ab", "NVDA", "done", "USD"),
                               ("wi_2026-11-11_AAPL_cd", "AAPL",
                                "awaiting_approval", "USD")):
        conn.execute(
            "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
            " approval_token, token_state, expires_at, thesis_json, trigger,"
            " created_at, updated_at) VALUES (?, 'pipeline', ?, ?, ?, 'tok',"
            " 'minted', ?, ?, 'preview data', ?, ?)",
            (wi, tk, state, wi, NOW + 3600,
             json.dumps({**thesis, "ticker": tk, "currency": ccy}),
             NOW - 7200, NOW - 600))
    for oid, acct, inst, qty, px, wi in (
            ("o1", "alpaca-paper", "NASDAQ:NVDA", "10", "100",
             "wi_2026-11-10_NVDA_ab"),
            ("o2", "saxo-paper", "SGX:D05", "100", "30", None),
            ("o3", "saxo-paper", "SGX:U11", "200", "36", None)):
        r.create_order(id=oid, account_id=acct, instrument_id=inst,
                       side="buy", qty=to_micro(qty), work_item_id=wi,
                       expires_at=NOW + 999, ts=NOW - 3600)
        r.record_fill(oid, broker_fill_id="f" + oid, qty=to_micro(qty),
                      price_native=to_micro(px), fx_rate=to_micro("1"),
                      ts=NOW - 3600)
    # one buy still working at the venue
    r.create_order(id="o4", account_id="alpaca-paper",
                   instrument_id="NASDAQ:AAPL", side="buy",
                   qty=to_micro("5"), limit_price=to_micro("228"),
                   expires_at=NOW + 20000, ts=NOW - 900,
                   reserved_base=to_micro("1140"))
    for i in range(40):
        conn.execute(
            "INSERT INTO events (item_id, ts, actor, from_state, to_state,"
            " payload) VALUES ('wi_2026-11-10_NVDA_ab', ?, 'preview', 'a', ?,"
            " '{}')", (NOW - (40 - i) * 300, "running" if i % 2 else "done"))
    return conn, r


def main(port=7799):
    conn, repo = build()
    srv = create_server(conn, repo, Market(), secret="preview-secret",
                        dash_key="preview-key",
                        api_base="http://127.0.0.1:9",      # discard port
                        port=port, sse_interval=None)
    print(f"preview on http://127.0.0.1:{port} (key: preview-key) — "
          "made-up data, nothing real reachable", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 7799)
