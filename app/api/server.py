"""claire-api entrypoint: python -m app.api.server (systemd: claire-api.service).
Builds the production world from var/ + etc/claire.env and serves :7788."""
import os
import sqlite3
import sys
import time
from pathlib import Path
import logging
from ..exchanges import lot_size as board_lot

log = logging.getLogger(__name__)

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
    from ..tools.market import build_market
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

    market = build_market()
    narratives = Narratives(var / "narratives")
    # claire-api owns the built-in sims: their resting stop-loss /
    # take-profit orders are kept in desk.db (sim_orders)
    brokers = wiring.build_brokers(conn=conn, sim_store=True)
    deps = wiring.build_deps(conn, repo, narratives, market, brokers=brokers)
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    serde = JsonPlusSerializer(allowed_msgpack_modules=[
        ("app.graph.state", n) for n in
        ("Instrument", "AnalystReport", "DebateCase", "Thesis", "Approval")])
    saver = SqliteSaver(sqlite3.connect(var / "checkpoints.db",
                                        check_same_thread=False), serde=serde)
    desk = Desk(conn, deps, saver, repo, session_cal=sessions.load(conn) or None)
    claire = Claire(conn, repo, desk, market, env=os.environ)
    return conn, repo, desk, claire, brokers, market


def build_custodian(conn, repo, brokers, market, secret):
    """In-process custodian (fills need the broker adapters this process
    owns); cadence comes from the schedules table via the Scheduler."""
    from .. import alerts, sessions
    from ..approvals import default_resume_post
    from ..custodian import Custodian, _account_broker_from_db

    return Custodian(conn, repo, brokers,
                     resume_post=default_resume_post("http://127.0.0.1:7788",
                                                     secret),
                     account_broker=_account_broker_from_db(conn),
                     fx_rate_for=make_fx_rate_for(conn, market),
                     cal=sessions.load(conn) or None,
                     # an unprotected position reaches Richard on Telegram,
                     # not only as a flag on a dashboard tile (§11a)
                     alert=alerts.send,
                     # the built-in sims' stop-loss / take-profit orders are
                     # checked against the live price every run
                     quote=market.quote)


class FxUnavailable(RuntimeError):
    pass


def make_fx_rate_for(conn, market):
    """(instrument_id, account_id) -> fx rate ×1e6 for booking a fill.

    A failed lookup RAISES. It used to return 1.0, so a ¥300,000 fill would
    have been booked as $300,000 (H6). Raising leaves the fill unbooked; the
    custodian flags it and books it on a later run once FX is back — late
    is recoverable, a wrong rate in an immutable execution row is not."""
    from ..accounting.money import to_micro

    def fx_rate_for(instrument_id, account_id):
        inst = conn.execute("SELECT currency FROM instruments WHERE id=?",
                            (instrument_id,)).fetchone()
        acct = conn.execute("SELECT base_currency FROM broker_accounts"
                            " WHERE id=?", (account_id,)).fetchone()
        if inst is None or acct is None:
            raise FxUnavailable(f"unknown instrument/account "
                                f"{instrument_id}/{account_id}")
        if inst["currency"] == acct["base_currency"]:
            return to_micro("1")
        try:
            rate = market.fx(inst["currency"], acct["base_currency"])
        except Exception as e:                  # noqa: BLE001
            raise FxUnavailable(
                f"no {inst['currency']}→{acct['base_currency']} rate: "
                f"{e}") from e
        if rate is None or rate <= 0:
            raise FxUnavailable(f"bad {inst['currency']}→"
                                f"{acct['base_currency']} rate {rate!r}")
        return to_micro(str(rate))
    return fx_rate_for


def choose_picks(conn, market, exchanges, held):
    """(picker label, [(ticker, exchange, reason)]) for a scheduled run.

    The SCREENER AGENT judges the shortlist, and "nothing worth a run today"
    is a legitimate answer: an empty list starts no paid runs. Only a real
    failure (ScreenerError: provider outage, unparseable output) falls back
    to the deterministic top-of-book pick, so an outage never kills a
    scheduled run (§1.7). Previously zero picks raised too, and the fallback
    started runs the screener had just declined (M13)."""
    from ..autonomous import pick_candidates
    from ..screener import ScreenerError, screener_pick
    try:
        picks = [(p.ticker, p.exchange, p.reason) for p in
                 screener_pick(conn, market, exchanges, held)]
    except ScreenerError as e:
        return (f"deterministic fallback ({e})",
                [(t, ex, "top of book") for t, ex in
                 pick_candidates(market, exchanges, held)])
    return ("screener agent" if picks else "screener agent (no picks)",
            picks)


def start_runs(desk, jobs):
    """Start one run per (ticker, exchange, trigger, reason). Each start is
    isolated: start_run refuses an invalid symbol (ValueError) and an
    unknown venue has no currency (KeyError), and one bad pick from a
    screener or scan used to abort every pick after it. Skipped picks are
    returned so the schedule's last_result records them."""
    from ..autonomous import CCY
    from ..graph.state import Instrument
    started, skipped = [], []
    for ticker, ex, trigger, reason in jobs:
        try:
            inst = Instrument(id=f"{ex}:{ticker}", ticker=ticker, exchange=ex,
                              currency=CCY[ex],
                              lot_size=board_lot(ex))
            wi = desk.start_run(inst, trigger=trigger)
        except (ValueError, KeyError) as e:
            log.warning("skipped run for %r on %r: %s", ticker, ex, e)
            skipped.append({"ticker": str(ticker)[:40],
                            "exchange": str(ex)[:20], "error": str(e)[:200]})
            continue
        started.append({"work_item": wi, "ticker": ticker, "exchange": ex,
                        "reason": reason})
    return started, skipped


def build_maintainer(conn):
    """The maintainer (DESIGN.md §23): plain-code checks and housekeeping,
    plus the maintainer agent's status note; Telegram when it turns bad."""
    import httpx

    from .. import alerts, maintenance

    def probe(url):
        try:
            return httpx.get(url, timeout=5).status_code < 500
        except Exception:                       # noqa: BLE001
            return False
    return maintenance.Maintainer(
        conn, summarize=maintenance.make_summarize(conn), alert=alerts.send,
        probe=probe)


def start_scheduler(conn, repo, desk, market, custodian, maintainer=None):
    """DB-backed schedules (app/scheduler.py): analysis regions, custodian,
    reconcile. The screener only scans markets that are OPEN at fire time."""
    from datetime import datetime, timezone

    from .. import scheduler, sessions
    from ..autonomous import REGIONS

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
        picker, picks = choose_picks(conn, market, exchanges, held)
        started, rejected = start_runs(
            desk, [(t, ex, f"{picker}: {reason}", reason)
                   for t, ex, reason in picks])
        return {"started": started, "picker": picker,
                "skipped_closed": skipped, "skipped_invalid": rejected}

    def run_reconcile(spec):
        report = custodian.run_once()
        custodian.snapshot_brokers()
        backup = ROOT / "var" / "desk.backup.db"
        backup.unlink(missing_ok=True)
        conn.execute("VACUUM INTO ?", (str(backup),))
        return {**report, "backup": str(backup)}

    def run_event_scan(spec):
        """Cheap hourly sweep across every open market; wakes the pipeline
        only when relative volume crosses the threshold (no LLM in the scan
        itself)."""
        from ..screener import event_scan
        cal = sessions.load(conn) or None
        now = datetime.now(timezone.utc)
        open_ex = [ex for region in REGIONS.values() for ex in region
                   if sessions.is_open(ex, now, cal)]
        if not open_ex:
            return {"skipped": "all markets closed"}
        held = {r["ticker"] for r in conn.execute(
            "SELECT DISTINCT i.ticker FROM lots l JOIN instruments i"
            " ON i.id=l.instrument_id WHERE l.qty_remaining > 0")}
        hits = event_scan(conn, open_ex, held,
                          threshold=float(spec.get("threshold", 3.0)))
        started, rejected = start_runs(desk, [
            (c["ticker"], c["exchange"],
             f"event scan: relative volume {c['rel_volume']:.1f}x its "
             f"10-day average today", f"rel-vol {c['rel_volume']:.1f}x")
            for c in hits[:int(spec.get("max_runs", 1))]])
        return {"scanned": open_ex, "hits": len(hits), "started": started,
                "skipped_invalid": rejected,
                "picker": "event scan (unusual volume)"}

    def run_price_triggers(spec):
        """Watchlist price triggers (DESIGN.md §15a). Runs here because
        claire-api owns the desk: a hit becomes desk.manual_trade, and a
        pre-approved (paper-only) rule is approved through the same
        token-checked resume as a click on the card."""
        from .. import alerts, price_triggers
        from ..approvals import default_resume_post
        return price_triggers.run_once(
            conn, desk, market,
            resume_post=default_resume_post(
                "http://127.0.0.1:7788",
                os.environ.get("CLAIRE_INTERNAL_SECRET", "")),
            alert=alerts.send, cal=sessions.load(conn) or None,
            env=os.environ, require_open=spec.get("require_open", True))

    sched = scheduler.Scheduler(conn, {
        "custodian": lambda spec: custodian.run_once(),
        "reconcile": run_reconcile,
        "analysis_asia": run_analysis,
        "analysis_eu": run_analysis,
        "analysis_us": run_analysis,
        "event_scan": run_event_scan,
        "price_triggers": run_price_triggers,
        **({"maintenance": lambda spec: _brief(
            maintainer.run(trigger="schedule"))} if maintainer else {}),
    })
    sched.start()
    # keep the read-only Saxo LIVE sign-in alive (refresh tokens rotate and
    # expire in ~40 min); silent until bin/saxo login has been run
    import threading

    from ..tools import saxo_live
    threading.Thread(target=saxo_live.keepalive_loop, daemon=True,
                     name="saxo-keepalive").start()
    return sched


def main():
    import uvicorn
    from fastapi.responses import StreamingResponse

    from .main import create_app

    load_env()
    from .. import logsetup
    logsetup.configure("claire-api")
    secret = os.environ.get("CLAIRE_INTERNAL_SECRET", "")
    if not secret:
        log.critical("CLAIRE_INTERNAL_SECRET not set in etc/claire.env")
        sys.exit(2)
    conn, repo, desk, claire, brokers, market = build_world()
    custodian = build_custodian(conn, repo, brokers, market, secret)
    maintainer = build_maintainer(conn)
    start_scheduler(conn, repo, desk, market, custodian, maintainer)

    def ask_handler(body):
        text = (body or {}).get("text", "")
        thread = (body or {}).get("thread", "main")
        return StreamingResponse(claire.ask_stream(text, thread),
                                 media_type="application/x-ndjson")

    from ..tools.files import Narratives
    from .agent_ask import make_agent_ask
    agent_ask_gen = make_agent_ask(conn, Narratives(ROOT / "var" / "narratives"))

    def agent_ask_handler(body):
        return StreamingResponse(agent_ask_gen(body),
                                 media_type="application/x-ndjson")

    app = create_app(desk, conn, secret, ask_handler=ask_handler,
                     agent_ask_handler=agent_ask_handler, brokers=brokers,
                     maintenance_runner=maintainer.run)
    _quiet_status_polls()
    # log_config=None: uvicorn keeps its own loggers but writes through the
    # desk's handler, so its lines carry timestamps like everything else
    uvicorn.run(app, host="127.0.0.1", port=7788, log_level="info",
                log_config=None)


def _brief(result):
    """What the schedules table keeps as last_result: small, not the whole
    report (that lives in maintenance_runs)."""
    return {k: result.get(k) for k in ("id", "status", "headline", "skipped")
            if result.get(k) is not None}


def _quiet_status_polls():
    """The dashboards poll GET /status every few seconds; logging each one
    was 99% of api.log (63k lines in 4 days). Everything else still logs."""
    import logging

    class _NoStatus(logging.Filter):
        def filter(self, record):
            return '"GET /status' not in record.getMessage()
    logging.getLogger("uvicorn.access").addFilter(_NoStatus())


if __name__ == "__main__":
    main()
