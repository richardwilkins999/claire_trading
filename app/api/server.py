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


def build_custodian(conn, repo, brokers, market, secret):
    """In-process custodian (fills need the broker adapters this process
    owns); cadence comes from the schedules table via the Scheduler."""
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

    return Custodian(conn, repo, brokers,
                     resume_post=default_resume_post("http://127.0.0.1:7788",
                                                     secret),
                     account_broker=_account_broker_from_db(conn),
                     fx_rate_for=fx_rate_for,
                     cal=sessions.load(conn) or None)


def start_scheduler(conn, repo, desk, market, custodian):
    """DB-backed schedules (app/scheduler.py): analysis regions, custodian,
    reconcile. The screener only scans markets that are OPEN at fire time."""
    from datetime import datetime, timezone

    from .. import scheduler, sessions
    from ..autonomous import CCY, REGIONS, pick_candidates
    from ..graph.state import Instrument

    scheduler.seed(conn)

    def run_analysis(spec):
        cal = sessions.load(conn) or None
        now = datetime.now(timezone.utc)
        exchanges = REGIONS[spec.get("region", "us")]
        skipped = []
        if spec.get("require_open", True):
            open_ex = [ex for ex in exchanges
                       if sessions.is_open(ex, now, cal)]
            skipped = sorted(set(exchanges) - set(open_ex))
            exchanges = open_ex
        held = {r["ticker"] for r in conn.execute(
            "SELECT DISTINCT i.ticker FROM lots l JOIN instruments i"
            " ON i.id=l.instrument_id WHERE l.qty_remaining > 0")}
        started = []
        for ticker, ex in pick_candidates(market, exchanges, held):
            inst = Instrument(id=f"{ex}:{ticker}", ticker=ticker, exchange=ex,
                              currency=CCY[ex],
                              lot_size=100 if ex == "SGX" else 1)
            started.append(desk.start_run(inst))
        return {"started": started, "skipped_closed": skipped}

    def run_reconcile(spec):
        report = custodian.run_once()
        custodian.snapshot_brokers()
        backup = ROOT / "var" / "desk.backup.db"
        backup.unlink(missing_ok=True)
        conn.execute("VACUUM INTO ?", (str(backup),))
        return {**report, "backup": str(backup)}

    sched = scheduler.Scheduler(conn, {
        "custodian": lambda spec: custodian.run_once(),
        "reconcile": run_reconcile,
        "analysis_asia": run_analysis,
        "analysis_eu": run_analysis,
        "analysis_us": run_analysis,
    })
    sched.start()
    return sched


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
    custodian = build_custodian(conn, repo, brokers, market, secret)
    start_scheduler(conn, repo, desk, market, custodian)

    def ask_handler(body):
        text = (body or {}).get("text", "")
        thread = (body or {}).get("thread", "main")
        return StreamingResponse(claire.ask_stream(text, thread),
                                 media_type="application/x-ndjson")

    app = create_app(desk, conn, secret, ask_handler=ask_handler)
    uvicorn.run(app, host="127.0.0.1", port=7788, log_level="info")


if __name__ == "__main__":
    main()
