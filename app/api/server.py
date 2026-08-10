"""claire-api entrypoint: python -m app.api.server (systemd: claire-api.service).
Builds the production world from var/ + etc/claire.env and serves :7788."""
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_env(path=None):
    p = Path(path or ROOT / "etc" / "claire.env")
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def build_world():
    from langgraph.checkpoint.sqlite import SqliteSaver

    from .. import sessions, wiring
    from ..accounting import db
    from ..accounting.money import to_micro
    from ..accounting.repo import Repo
    from ..graph.claire_agent import Claire
    from ..providers import seeds
    from ..tools.files import Narratives
    from ..tools.market import Market
    from .desk import Desk

    var = ROOT / "var"
    var.mkdir(exist_ok=True)
    conn = db.connect(var / "desk.db")
    db.init(conn)
    seeds.seed(conn)
    sessions.seed_db(conn)
    repo = Repo(conn)
    ts = int(time.time())
    for broker, fee in (("alpaca", {"type": "flat", "per_trade": "0"}),
                        ("saxo", {"type": "pct", "pct": "0.0008", "min": "5"}),
                        ("moomoo", {"type": "flat", "per_trade": "0.99"})):
        acct = f"{broker}-paper"
        if conn.execute("SELECT 1 FROM broker_accounts WHERE id=?",
                        (acct,)).fetchone() is None:
            repo.create_account(
                acct, broker, "paper", "USD", fee_model=fee,
                risk_limits={"max_order_base": 25000,
                             "max_trades_per_day": 20}, ts=ts)
            repo.deposit(acct, to_micro("100000"), ts=ts,
                         note="opening paper balance")

    market = Market()
    narratives = Narratives(var / "narratives")
    brokers = wiring.build_brokers()
    deps = wiring.build_deps(conn, repo, narratives, market, brokers=brokers)
    saver = SqliteSaver(sqlite3.connect(var / "checkpoints.db",
                                        check_same_thread=False))
    desk = Desk(conn, deps, saver, repo, session_cal=sessions.load(conn) or None)
    claire = Claire(conn, repo, desk, market, env=os.environ)
    return conn, repo, desk, claire, brokers, market


def start_custodian_thread(conn, repo, brokers, market, secret, *,
                           interval=300):
    """In-process custodian (fills need the broker adapters this process
    owns); the systemd timer variant covers DB-only duties redundantly."""
    import threading

    from .. import sessions
    from ..approvals import default_resume_post
    from ..custodian import Custodian, _account_broker_from_db

    def fx_rate_for(instrument_id, account_id):
        from ..accounting.money import to_micro
        inst = conn.execute("SELECT currency FROM instruments WHERE id=?",
                            (instrument_id,)).fetchone()
        acct = conn.execute("SELECT base_currency FROM broker_accounts"
                            " WHERE id=?", (account_id,)).fetchone()
        try:
            return to_micro(str(market.fx(inst["currency"],
                                          acct["base_currency"])))
        except Exception:                       # noqa: BLE001 — degrade loudly
            return to_micro("1")

    c = Custodian(conn, repo, brokers,
                  resume_post=default_resume_post("http://127.0.0.1:7788",
                                                  secret),
                  account_broker=_account_broker_from_db(conn),
                  fx_rate_for=fx_rate_for, cal=sessions.load(conn) or None)

    def loop():
        while True:
            time.sleep(interval)
            try:
                c.run_once()
            except Exception:                   # noqa: BLE001
                import traceback
                traceback.print_exc()

    threading.Thread(target=loop, daemon=True, name="custodian").start()


def main():
    import uvicorn
    from fastapi.responses import StreamingResponse

    from .main import create_app

    load_env()
    secret = os.environ.get("CLAIRE_INTERNAL_SECRET", "")
    if not secret:
        print("FATAL: CLAIRE_INTERNAL_SECRET not set in etc/claire.env",
              file=sys.stderr)
        sys.exit(2)
    conn, repo, desk, claire, brokers, market = build_world()
    start_custodian_thread(conn, repo, brokers, market, secret)

    def ask_handler(body):
        text = (body or {}).get("text", "")
        thread = (body or {}).get("thread", "main")
        return StreamingResponse(claire.ask_stream(text, thread),
                                 media_type="application/x-ndjson")

    app = create_app(desk, conn, secret, ask_handler=ask_handler)
    uvicorn.run(app, host="127.0.0.1", port=7788, log_level="info")


if __name__ == "__main__":
    main()
