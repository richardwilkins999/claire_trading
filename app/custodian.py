"""The custodian (DESIGN.md §12): fills, expiry, retries, orphans, snapshots.
Deterministic; cannot approve or execute — /internal/resume accepts only
status:"expired" from actor:"reaper" on this path.
"""
import json
import time
from datetime import datetime, timezone

from . import approvals, health, sessions
from .accounting.money import to_micro
import logging
from .logsetup import swallowed

log = logging.getLogger(__name__)


class Custodian:
    def __init__(self, conn, repo, brokers, *, resume_post, account_broker,
                 fx_rate_for, clock=time.time, cal=None, alert=None,
                 quote=None):
        self.conn, self.repo = conn, repo
        # symbol -> {price, stale} (market.quote): the built-in sims' resting
        # stop-loss / take-profit orders are checked against it
        self.quote = quote
        # out-of-band channel for what must reach a human NOW (an unprotected
        # position): app.alerts.send in claire-api; None = flags only
        self.alert = alert
        self.brokers = brokers                  # broker name -> adapter
        self.resume_post = resume_post
        self.account_broker = account_broker    # account_id -> broker name
        self.fx_rate_for = fx_rate_for          # (instrument_id, account_id) -> micro rate
        self.clock, self.cal = clock, cal
        self._drift_seen = {}       # (account, symbol) -> last mismatch seen

    # each duty runs on its own: one failing duty (or one bad order inside
    # it) used to abort run_once, so expiry, retries, orphan checks and drift
    # all stopped — on every run, for as long as the bad order existed (H8)
    DUTIES = ("_place_pending_session", "_sim_triggers", "_poll_fills",
              "_expire_orders",
              "_reprotect", "_expire_approvals", "_retry_resumes", "_orphans",
              "_ledger", "_drift", "_position_drift", "_leg_drift",
              "_archive", "_broker_sync")

    def run_once(self) -> dict:
        report = {"placed": [], "fills": 0, "completed": [], "order_expired": [],
                  "sim_triggered": [],
                  "approvals_expired": [], "resume_retried": [],
                  "reprotected": [], "flags": [], "sync": {}}
        for duty in self.DUTIES:
            try:
                getattr(self, duty)(report)
            except Exception as e:              # noqa: BLE001 — net must not die silent
                report["flags"].append(f"custodian {duty.strip('_')} error: {e}")
                swallowed(log, f"custodian duty {duty.strip('_')} failed", e,
                          trace=True)
        self._live_alarm(report)
        for flag in report["flags"]:            # each distinct flag: hourly
            swallowed(log, f"custodian flag: {flag[:300]}", every=3600)
        ok = "degraded" if report["flags"] else "ok"
        health.record(self.conn, "custodian", ok, report, int(self.clock()))
        return report

    def _sim_triggers(self, report):
        """The built-in sims hold a bracket's stop-loss / take-profit as
        resting orders (tools/brokers.PaperSimBroker, protect=True) but see
        no market: here each one is checked against the live price, only
        while its exchange is open and the quote is fresh. What fires is
        filled AT THE SIM; _poll_fills (next) books it like a Saxo leg fill,
        and the OCO sibling is synced as cancelled. Tells you on Telegram."""
        if self.quote is None:
            return
        from datetime import datetime, timezone

        from . import sessions
        from .tools.market import yahoo_symbol
        now = int(self.clock())
        when = datetime.fromtimestamp(now, timezone.utc)
        seen = {}
        for o in self.conn.execute(
                "SELECT o.account_id, i.ticker, i.exchange FROM orders o"
                " JOIN instruments i ON i.id=o.instrument_id"
                " WHERE o.leg IS NOT NULL AND o.status IN"
                " ('placed','partially_filled')").fetchall():
            adapter = self._adapter(o["account_id"])
            if adapter is None or not hasattr(adapter, "check_triggers"):
                continue
            seen.setdefault(id(adapter), (adapter, {}))[1][
                yahoo_symbol(o["ticker"], o["exchange"])] = o["exchange"]
        for adapter, syms in seen.values():
            mine = {b.symbol for _, b in adapter.resting()}
            prices = {}
            for sym, ex in syms.items():
                if sym not in mine:
                    continue                    # a Saxo leg: Saxo watches it
                try:
                    if not sessions.is_open(ex, when, self.cal):
                        continue
                    q = self.quote(sym) or {}
                except Exception as e:          # noqa: BLE001 — next run
                    report["flags"].append(f"sim stop check {sym}: {e}")
                    continue
                if q.get("price") is None or q.get("stale"):
                    continue                    # never act on an old price
                prices[sym] = q["price"]
            if not prices:
                continue
            for f in adapter.check_triggers(prices):
                report["sim_triggered"].append(f)
                if self.alert is not None:
                    what = "Stop-loss" if f["leg"] == "stop" else "Take-profit"
                    try:
                        self.alert(f"{'🛑' if f['leg'] == 'stop' else '🎯'} "
                                   f"{what} reached on the built-in sim: "
                                   f"{f['side']} {f['qty']} {f['symbol']} at "
                                   f"{f['price']} (paper)",
                                   title=f"Claire — {what.lower()} filled")
                    except Exception as e:      # noqa: BLE001 — booked anyway
                        swallowed(log, "alert for a sim stop/target fill was not sent",
                                  e)
                        pass

    def _live_alarm(self, report):
        """Any doubt about a LIVE order disarms live trading (DESIGN.md §22):
        a fill the book could not record, an order Saxo does not know, a
        placement with an unknown outcome. Real money waits for a human."""
        from . import live_trading
        live_ids = [r["id"] for r in self.conn.execute(
            "SELECT o.id FROM orders o JOIN broker_accounts a"
            " ON a.id=o.account_id WHERE a.environment='live'")]
        live_accts = [r["id"] for r in self.conn.execute(
            "SELECT id FROM broker_accounts WHERE environment='live'")]
        hits = [f for f in report["flags"]
                if any(oid in f for oid in live_ids)
                # shares the book counts on that the live account does not
                # hold: the book is wrong about real money
                or (f.startswith("POSITION DRIFT ") and any(
                    f.startswith(f"POSITION DRIFT {a}:") for a in live_accts))]
        if hits and live_trading.disarm(self.conn, actor="custodian",
                                        reason=hits[0][:250],
                                        clock=self.clock):
            report["flags"].append("LIVE TRADING DISARMED by the custodian: "
                                   + hits[0][:200])

    def _retry_resumes(self, report):
        report["resume_retried"] = approvals.retry_pending(
            self.conn, self.resume_post, clock=self.clock)

    # ── orders ───────────────────────────────────────────────────────────
    def _adapter(self, account_id):
        """None when this custodian instance has no adapter for the venue —
        the timer-run custodian does DB-only duties; fill polling happens in
        the claire-api process, which owns the (possibly in-memory) venues."""
        return self.brokers.get(self.account_broker(account_id))

    def _place_pending_session(self, report):
        from .graph.nodes_execution import SUBMIT_STALE_S, submit_order
        from .tools.market import yahoo_symbol
        ts = int(self.clock())
        now = datetime.fromtimestamp(ts, tz=timezone.utc)
        for o in self.conn.execute(
                "SELECT o.*, i.ticker, i.exchange FROM orders o"
                " JOIN instruments i ON i.id=o.instrument_id"
                # a protective leg is born at the venue; it is never placed
                " WHERE o.status='pending_session' AND o.leg IS NULL"
                ).fetchall():
            try:
                if o["submitted_at"] is not None:
                    # claimed but never acknowledged: the placer died mid-call
                    # and the venue may hold it. Placing again could double
                    # the position — a human reconciles (M4).
                    if o["submitted_at"] < ts - SUBMIT_STALE_S:
                        report["flags"].append(
                            f"RECONCILE order {o['id']}: sent to the venue at "
                            f"{o['submitted_at']} but never acknowledged — "
                            "check the venue for client id "
                            f"{o['client_order_id'] or o['id']}")
                    continue
                if not sessions.is_open(o["exchange"], now, self.cal):
                    continue
                adapter = self._adapter(o["account_id"])
                if adapter is None:
                    continue
                placed = submit_order(
                    self.repo, adapter, o["id"],
                    symbol=yahoo_symbol(o["ticker"], o["exchange"]), ts=ts)
                if placed is None:
                    continue                    # another placer holds the claim
                if placed.status == "accepted":
                    report["placed"].append(o["id"])
                else:
                    report["flags"].append(
                        f"order {o['id']} rejected: {placed.detail}")
                    self._close_item(o, "failed", report, reason=(
                        f"venue rejected the queued order: {placed.detail}"))
            except Exception as e:              # noqa: BLE001 — per order
                report["flags"].append(f"order {o['id']} placement error: {e}")

    def _close_item(self, o, to_state, report, *, reason, **payload):
        """Move an order's work item out of `executing` when the order can no
        longer complete it. A rejected pending_session order used to leave
        its item `executing` forever, with no orphan check to notice (M6)."""
        if not o["work_item_id"]:
            return
        wi = self.conn.execute("SELECT state FROM work_items WHERE id=?",
                               (o["work_item_id"],)).fetchone()
        if wi and wi["state"] == "executing":
            self.repo.set_state(o["work_item_id"], to_state, actor="custodian",
                                ts=int(self.clock()),
                                payload={"order": o["id"], "reason": reason,
                                         **payload})

    def _poll_fills(self, report):
        """Every working order, protective legs included: a stop or target
        that fills at the venue is a desk SELL (or buy-to-cover) booked
        against the position like any other fill, and a leg the venue has
        cancelled (its OCO sibling filled, or its entry was cancelled) is
        marked so (protection.sync_from_venue)."""
        from . import protection
        from .tools.brokers import BrokerError
        for o in self.conn.execute(
                "SELECT * FROM orders WHERE status IN"
                " ('placed','partially_filled')").fetchall():
            adapter = self._adapter(o["account_id"])
            if adapter is None or not o["broker_order_id"]:
                continue
            sync = report["sync"].setdefault(o["account_id"],
                                             {"orders": 0, "errors": []})
            sync["orders"] += 1
            try:
                status = adapter.order_status(o["broker_order_id"])
            except BrokerError as e:
                sync["errors"].append(f"order {o['id']}: {e}"[:200])
                # the in-memory sim forgets every order on a claire-api
                # restart; a real venue can lose one too. The book cannot know
                # what filled, so it waits for a human rather than guessing.
                report["flags"].append(
                    f"RECONCILE order {o['id']}: venue does not know "
                    f"{o['broker_order_id']} ({e}) — fills unknown")
                continue
            except Exception as e:              # noqa: BLE001 — per order
                sync["errors"].append(f"order {o['id']}: {e}"[:200])
                report["flags"].append(f"order {o['id']} poll error: {e}")
                continue
            unbooked = self._book_fills(o, status, report)
            if o["leg"]:
                if unbooked:
                    # a terminal status now would refuse the fill the book
                    # still owes (record_fill rejects fills on a cancelled
                    # order): book first, sync on a later run
                    continue
                try:
                    protection.sync_from_venue(self.repo, o, status,
                                               ts=int(self.clock()))
                except Exception as e:          # noqa: BLE001 — per order
                    report["flags"].append(f"leg {o['id']} sync error: {e}")
                continue                        # a leg completes no item
            if not unbooked and status["status"] in ("cancelled", "rejected"):
                self._venue_ended(o, status, report)
                continue
            self._maybe_complete(o["id"], report)

    def _book_fills(self, o, status, report) -> bool:
        """Record the venue's fills for one order (idempotent per fill).
        True if any fill could NOT be booked — the book then still owes it,
        so nothing may close the order yet."""
        unbooked = False
        for f in status["fills"]:
            try:
                # a failed FX lookup raises here and the fill waits for
                # the next run — it is never booked at 1.0 (H6)
                ex = self.repo.record_fill(
                    o["id"], broker_fill_id=f["fill_id"],
                    qty=to_micro(f["qty"]),
                    price_native=to_micro(f["price"]),
                    fx_rate=self.fx_rate_for(o["instrument_id"],
                                             o["account_id"]),
                    # → slippage metric; a stop leg's intent is its stop
                    intended_price=o["limit_price"] or o["stop_loss"],
                    ts=int(self.clock()))
            except Exception as e:          # noqa: BLE001 — per fill
                report["flags"].append(
                    f"FILL NOT BOOKED order {o['id']} fill "
                    f"{f['fill_id']}: {e} — the venue filled it, the book "
                    "has not; reconcile")
                unbooked = True
                continue
            if ex:
                report["fills"] += 1
        return unbooked

    def _venue_ended(self, o, status, report):
        """An entry (buy or sell, not a leg) the venue has finished with
        without filling it all: a day order that expired at the close, one
        cancelled at the venue (e.g. on Saxo's own platform), or one the
        venue rejected. The venue is presumed right (§12), so the book
        follows — its fills were booked first by the caller. It used to
        stay `placed` forever: shown as working on the Orders page, and its
        TTL cancel then failed every run with "order not found"."""
        o = self.repo.order(o["id"])
        if o["status"] not in ("placed", "partially_filled"):
            return
        ended = status.get("ended")
        new = ("rejected" if status["status"] == "rejected" else
               "cancelled" if ended == "Cancelled" else "expired")
        ts = int(self.clock())
        self.repo.mark_order(o["id"], new, ts=ts)
        report["order_expired"].append(o["id"])
        (fills,) = self.conn.execute(
            "SELECT COUNT(*) FROM executions WHERE order_id=?",
            (o["id"],)).fetchone()
        self._close_item(
            o, "done" if fills else {"expired": "expired",
                                     "cancelled": "cancelled",
                                     "rejected": "failed"}[new],
            report, reason=f"{new} at the venue"
            + (f" ({ended})" if ended else ""), partial_fill=bool(fills))

    def _maybe_complete(self, order_id, report):
        o = self.repo.order(order_id)
        if o["status"] == "filled" and o["work_item_id"] and not o["leg"]:
            wi = self.conn.execute("SELECT state FROM work_items WHERE id=?",
                                   (o["work_item_id"],)).fetchone()
            if wi and wi["state"] == "executing":
                self.repo.set_state(o["work_item_id"], "done", actor="custodian",
                                    ts=int(self.clock()))
                report["completed"].append(o["work_item_id"])

    def _expire_orders(self, report):
        """An order is marked expired only once it can no longer fill: never
        sent, or cancelled at its venue. The timer custodian has no adapters
        and used to mark live orders expired anyway — then refuse the fills
        that followed, so book and broker diverged (H8). Without an adapter
        it now skips and flags; the in-process custodian does the cancel."""
        from .tools.brokers import BrokerError
        now = int(self.clock())
        # protective legs are GTC at the venue and are excluded outright:
        # cancelling a stop because a day order's clock ran out would strip
        # the position of its protection
        for o in self.conn.execute(
                "SELECT * FROM orders WHERE status IN"
                " ('placed','partially_filled','pending_session')"
                " AND leg IS NULL AND expires_at < ?", (now,)).fetchall():
            try:
                if o["broker_order_id"]:
                    adapter = self._adapter(o["account_id"])
                    if adapter is None:
                        report["flags"].append(
                            f"order {o['id']} is past its TTL but this "
                            "custodian has no adapter to cancel it at the "
                            "venue — left open")
                        continue
                    try:
                        adapter.cancel(o["broker_order_id"])
                    except BrokerError:
                        # "order not found" and the like: the venue may
                        # already have ended it (a day order at the close).
                        # Ask it; if it is over, close the book to match.
                        if self._settle_ended(o, adapter, report):
                            continue
                        raise
                    # The cancel went through — but a fill can land between
                    # this run's poll and the cancel, and an expired order
                    # is never polled again. Take the venue's final word
                    # before the book lets go of it.
                    if not self._final_fills(o, adapter, report):
                        continue
                elif o["submitted_at"] is not None:
                    report["flags"].append(
                        f"RECONCILE order {o['id']}: past its TTL, sent but "
                        "never acknowledged — cannot cancel what the book "
                        "cannot name; left open")
                    continue
                (fills,) = self.conn.execute(
                    "SELECT COUNT(*) FROM executions WHERE order_id=?",
                    (o["id"],)).fetchone()
                self.repo.mark_order(o["id"], "expired", ts=now)
                report["order_expired"].append(o["id"])
                self._close_item(o, "done" if fills else "expired", report,
                                 reason="order TTL", partial_fill=bool(fills))
            except Exception as e:              # noqa: BLE001 — per order
                report["flags"].append(f"order {o['id']} expiry error: {e}")

    def _final_fills(self, o, adapter, report) -> bool:
        """After a successful TTL cancel: book whatever the venue filled
        before the cancel reached it. True = the order is over at the venue
        and every fill is on the book, so it may be marked expired. False =
        it stays open (already completed here, or flagged) and the next run
        looks again — late is recoverable, a lost fill is not."""
        try:
            status = adapter.order_status(o["broker_order_id"])
        except Exception as e:                  # noqa: BLE001
            report["flags"].append(
                f"order {o['id']}: cancelled at the venue at its TTL, but "
                f"its final status could not be read ({e}) — left open, "
                "checked again next run")
            return False
        if self._book_fills(o, status, report):
            return False                        # flagged; book it next run
        if status["status"] == "filled":
            # it filled in full before the cancel arrived
            self._maybe_complete(o["id"], report)
            return False
        if status["status"] not in ("cancelled", "rejected"):
            report["flags"].append(
                f"order {o['id']}: cancelled at the venue at its TTL, but "
                f"the venue still reports it {status['status']} — left "
                "open, checked again next run")
            return False
        return True

    def _settle_ended(self, o, adapter, report) -> bool:
        """True if the venue says this order is over (and the book now
        agrees); False if it is still working there or cannot be read."""
        try:
            status = adapter.order_status(o["broker_order_id"])
        except Exception as e:                  # noqa: BLE001
            swallowed(log, "venue status read failed while settling an ended order",
                      e)
            return False
        if status["status"] not in ("cancelled", "rejected", "filled"):
            return False
        if self._book_fills(o, status, report):
            return True                         # flagged; try again next run
        if status["status"] == "filled":
            self._maybe_complete(o["id"], report)
        else:
            self._venue_ended(o, status, report)
        return True

    # ── approvals ────────────────────────────────────────────────────────
    APPROVED_TTL_S = 3600

    def _expire_approvals(self, report):
        now = int(self.clock())
        for r in self.conn.execute(
                "SELECT id, approval_token FROM work_items"
                " WHERE state='awaiting_approval' AND expires_at < ?",
                (now,)).fetchall():
            try:
                code, _ = self.resume_post({
                    "work_item_id": r["id"], "status": "expired",
                    "actor": "reaper", "token": r["approval_token"]})
            except Exception as e:              # noqa: BLE001 — api down
                code = f"unreachable ({e})"
            if code == 200:
                report["approvals_expired"].append(r["id"])
            else:
                report["flags"].append(f"expiry resume failed for {r['id']}: {code}")
        # DESIGN §12: `approved` with no order past 60 min → expired, token
        # burned. An approval whose execution kept failing used to sit
        # `approved` forever, retried on every run (M5). Measured from the
        # approval event, not updated_at, which every retry bumps.
        for r in self.conn.execute(
                "SELECT w.id FROM work_items w WHERE w.state='approved'"
                " AND NOT EXISTS (SELECT 1 FROM orders o WHERE"
                "  o.work_item_id=w.id AND o.leg IS NULL AND o.status NOT IN"
                "  ('rejected','cancelled','expired'))"
                " AND COALESCE((SELECT MAX(e.ts) FROM events e WHERE"
                "  e.item_id=w.id AND e.to_state='approved'), w.updated_at)"
                " < ?", (now - self.APPROVED_TTL_S,)).fetchall():
            cur = self.conn.execute(
                "UPDATE work_items SET state='expired', token_state='burned',"
                " updated_at=? WHERE id=? AND state='approved'"
                " AND COALESCE(token_state,'') != 'resuming'", (now, r["id"]))
            if cur.rowcount == 1:
                self.repo._event(r["id"], now, "custodian", "approved",
                                 "expired",
                                 {"reason": "approved but no order after "
                                            f"{self.APPROVED_TTL_S // 60} min"})
                report["approvals_expired"].append(r["id"])

    # ── orphans & guards ─────────────────────────────────────────────────
    def _orphans(self, report):
        now = int(self.clock())
        for r in self.conn.execute(
                "SELECT id FROM work_items WHERE state='running'"
                " AND updated_at < ?", (now - 7200,)):
            report["flags"].append(f"work item {r['id']} running > 2h")
        # an approval refused at placement is NOT an orphan: it is parked on
        # the Approvals page ("Approved but not placed — needs you"), and
        # flagging it marked the custodian degraded for no reason
        parked = {f["id"] for f in approvals.failed_approvals(
            self.conn, clock=self.clock)}
        for r in self.conn.execute(
                "SELECT id FROM work_items WHERE state='approved'"
                " AND updated_at < ?", (now - 600,)):
            if r["id"] in parked:
                continue
            report["flags"].append(f"approved item {r['id']} has no order after 10m")
        # executing with nothing left that could complete it (M6): every
        # order terminal and none filled — the item would never move again
        for r in self.conn.execute(
                "SELECT w.id FROM work_items w WHERE w.state='executing'"
                " AND NOT EXISTS (SELECT 1 FROM orders o WHERE"
                "  o.work_item_id=w.id AND o.leg IS NULL AND o.status IN"
                "  ('pending_session','placed','partially_filled','filled'))"
                " AND w.updated_at < ?", (now - 600,)):
            report["flags"].append(
                f"executing item {r['id']} has no live or filled order")
        for r in self.conn.execute(
                "SELECT l.id FROM lots l WHERE l.qty_remaining < 0"):
            report["flags"].append(f"lot {r['id']} negative remainder")
        self._unprotected(report)

    def _unprotected(self, report):
        """UNPROTECTED: a position with no resting stop at its venue — the
        watcher is then its only safety net, and you should know (§11).

        Where the venue holds the protective legs as orders (Saxo), the book
        tracks them (§11a), so the question is answered from the legs: are
        the working stop legs enough for every share held? Elsewhere (the
        in-process sim keeps a bracket's stop inside the entry) it is the old
        test: did the order that opened a long carry a stop at all."""
        for p in self.conn.execute(
                "SELECT account_id, instrument_id, SUM(CASE WHEN qty_opened<0"
                " THEN -qty_remaining ELSE qty_remaining END) AS q FROM lots"
                " WHERE qty_remaining > 0 GROUP BY account_id, instrument_id"
                ).fetchall():
            q = p["q"]
            if not q:
                continue
            tracked = self.conn.execute(
                "SELECT 1 FROM orders WHERE account_id=? AND instrument_id=?"
                " AND leg IS NOT NULL LIMIT 1",
                (p["account_id"], p["instrument_id"])).fetchone()
            if tracked:
                (covered,) = self.conn.execute(
                    "SELECT COALESCE(SUM(o.qty - COALESCE((SELECT SUM(e.qty)"
                    " FROM executions e WHERE e.order_id=o.id),0)),0)"
                    " FROM orders o WHERE o.account_id=? AND o.instrument_id=?"
                    " AND o.leg='stop' AND o.side=? AND o.status IN"
                    " ('placed','partially_filled')",
                    (p["account_id"], p["instrument_id"],
                     "sell" if q > 0 else "buy")).fetchone()
                if covered < abs(q):
                    report["flags"].append(
                        f"UNPROTECTED position {p['instrument_id']} on "
                        f"{p['account_id']}: {(abs(q) - covered) / 1e6:g} of "
                        f"{abs(q) / 1e6:g} shares have no resting stop at the "
                        "venue; watcher alerts are the only backstop")
                continue
            if q < 0:
                continue
            bare = self.conn.execute(
                "SELECT 1 FROM lots l"
                " JOIN executions e ON e.id = l.open_execution_id"
                " LEFT JOIN orders o ON o.id = e.order_id"
                " WHERE l.account_id=? AND l.instrument_id=?"
                " AND l.qty_remaining > 0 AND l.qty_opened > 0"
                " AND (o.stop_loss IS NULL OR o.id IS NULL) LIMIT 1",
                (p["account_id"], p["instrument_id"])).fetchone()
            if bare:
                report["flags"].append(
                    f"UNPROTECTED position {p['instrument_id']} — no resting "
                    "stop at the venue; watcher alerts are the only backstop")

    # ── protective legs (§11a) ───────────────────────────────────────────
    def _reprotect(self, report):
        """A desk order that had to cancel a position's stop/target has
        finished: re-protect whatever it left, at the original levels
        (protection.reprotect). Waits until the order is terminal — while a
        sell is still working, Saxo would refuse a second resting sell and
        the two could together sell more than is held. A failure is flagged
        every run while shares remain bare, and alerted once."""
        from . import protection
        from .tools.market import yahoo_symbol
        ts = int(self.clock())
        for o in self.conn.execute(
                "SELECT o.*, i.ticker, i.exchange FROM orders o"
                " JOIN instruments i ON i.id=o.instrument_id"
                " WHERE o.protect_state='pending' AND o.status IN"
                " ('filled','cancelled','expired','rejected')").fetchall():
            adapter = self._adapter(o["account_id"])
            if adapter is None:
                continue                        # the in-process custodian will
            state, note = protection.reprotect(
                self.repo, adapter, o["id"],
                symbol=yahoo_symbol(o["ticker"], o["exchange"]), ts=ts)
            report["reprotected"].append({"order": o["id"], "state": state,
                                          "note": note})
        for o in self.conn.execute(
                "SELECT * FROM orders WHERE protect_state='failed'").fetchall():
            held, _, _ = self.repo.position(o["account_id"],
                                            o["instrument_id"])
            if held == 0:
                self.conn.execute(
                    "UPDATE orders SET protect_state='not_needed',"
                    " protect_note=?, updated_at=? WHERE id=?",
                    ((o["protect_note"] or "") + " — resolved: position "
                     "closed", ts, o["id"]))
                continue
            report["flags"].append(f"RE-PROTECTION FAILED after order "
                                   f"{o['id']}: {o['protect_note']}")
            if self.alert is not None and o["protect_alerted_at"] is None:
                try:
                    self.alert(f"⚠ {o['protect_note']} (after desk order "
                               f"{o['id']})", title="Claire — UNPROTECTED")
                except Exception as e:          # noqa: BLE001 — flag stands
                    swallowed(log, "UNPROTECTED-position alert was not sent",
                              e)
                    continue
                self.conn.execute("UPDATE orders SET protect_alerted_at=?"
                                  " WHERE id=?", (ts, o["id"]))

    def _leg_drift(self, report):
        """The venue's working orders vs the book's legs, for venues that can
        list them (Saxo). Flagged, never corrected (§12):
        - a leg the book thinks is working that the venue no longer lists
          (it filled or was cancelled where the poll could not see it);
        - an order at the venue on the EXIT side of a desk position that the
          book does not track — it could sell the desk's shares."""
        from .tools.market import yahoo_symbol
        for acct in self.conn.execute(
                "SELECT id, broker FROM broker_accounts").fetchall():
            adapter = self.brokers.get(acct["broker"])
            if adapter is None or not hasattr(adapter, "working_orders"):
                continue
            book = self.conn.execute(
                "SELECT * FROM orders WHERE account_id=? AND leg IS NOT NULL"
                " AND status IN ('placed','partially_filled')",
                (acct["id"],)).fetchall()
            positions = self.conn.execute(
                "SELECT l.instrument_id, i.ticker, i.exchange, SUM(CASE WHEN"
                " l.qty_opened<0 THEN -l.qty_remaining ELSE l.qty_remaining"
                " END) AS q FROM lots l JOIN instruments i ON"
                " i.id=l.instrument_id WHERE l.account_id=? AND"
                " l.qty_remaining>0 GROUP BY l.instrument_id",
                (acct["id"],)).fetchall()
            if not book and not positions:
                continue
            try:
                venue = adapter.working_orders()
            except Exception as e:              # noqa: BLE001
                report["flags"].append(f"leg drift {acct['id']}: cannot list "
                                       f"the venue's working orders ({e})")
                continue
            ids = {w["order_id"] for w in venue}
            for o in book:
                if o["broker_order_id"] not in ids:
                    report["flags"].append(
                        f"LEG DRIFT {acct['id']}: {o['leg']} leg {o['id']} "
                        f"(venue {o['broker_order_id']}) is working on the "
                        "book but not at the venue — reconcile")
            known = {r["broker_order_id"] for r in self.conn.execute(
                "SELECT broker_order_id FROM orders WHERE account_id=? AND"
                " broker_order_id IS NOT NULL", (acct["id"],))}
            for p in positions:
                if not p["q"]:
                    continue
                exit_side = "sell" if p["q"] > 0 else "buy"
                sym = yahoo_symbol(p["ticker"], p["exchange"])
                for w in venue:
                    if w.get("desk_symbol") != sym or w["order_id"] in known:
                        continue
                    if w.get("side") not in (None, exit_side):
                        continue
                    report["flags"].append(
                        f"UNTRACKED venue order {w['order_id']} on "
                        f"{acct['id']}: {w.get('type')} {w.get('side') or ''} "
                        f"{w.get('amount')} {w.get('symbol')} @ "
                        f"{w.get('price')} — not on the book; it could sell "
                        "the desk's shares (bin/claire legs-backfill adopts "
                        "a bracket's legs)")

    def _ledger(self, report):
        """The book's own consistency, every run: cash never negative at any
        point in its history, and every lot's shares reconcile with its
        closures (repo.assert_invariants). Its docstring always said the
        custodian ran it; until 2026-11-11 only the tests did. A failure is
        FLAGGED, never repaired here."""
        for acct in self.conn.execute(
                "SELECT id FROM broker_accounts").fetchall():
            try:
                self.repo.assert_invariants(acct["id"])
            except Exception as e:              # noqa: BLE001 — per account
                report["flags"].append(
                    f"LEDGER INCONSISTENT {acct['id']}: {e} — reconcile "
                    "before trading this account")

    def _position_drift(self, report):
        """Does the venue hold the shares the book says it does? For venues
        that can list positions (Saxo). Flagged, never corrected (§12).

        Only one direction is an error: the book holding MORE than the
        venue (a sale the book never heard of — the lost fill of B2, a
        position closed on Saxo's own platform). The venue holding more is
        normal: the owner's own shares sit on the same account. Names the
        venue simulates itself are skipped. A difference must be seen on
        two runs in a row before it is flagged, so a fill that lands
        between this run's poll and this check is not reported as drift."""
        from decimal import Decimal

        from .tools.market import yahoo_symbol
        seen_now = {}
        for acct in self.conn.execute(
                "SELECT id, broker FROM broker_accounts").fetchall():
            adapter = self.brokers.get(acct["broker"])
            if adapter is None or not hasattr(adapter, "positions"):
                continue
            book = self.conn.execute(
                "SELECT i.ticker, i.exchange, SUM(CASE WHEN l.qty_opened<0"
                " THEN -l.qty_remaining ELSE l.qty_remaining END) AS q"
                " FROM lots l JOIN instruments i ON i.id=l.instrument_id"
                " WHERE l.account_id=? AND l.qty_remaining>0"
                " GROUP BY l.instrument_id", (acct["id"],)).fetchall()
            if not book:
                continue
            try:
                venue = adapter.positions()
            except Exception as e:              # noqa: BLE001 — per account
                report["flags"].append(
                    f"position check {acct['id']}: cannot list the venue's "
                    f"positions ({str(e)[:120]})")
                continue
            is_local = getattr(adapter, "is_local", lambda s: False)
            for p in book:
                sym = yahoo_symbol(p["ticker"], p["exchange"])
                if not p["q"] or is_local(sym):
                    continue
                have = Decimal(str(p["q"])) / 1_000_000
                held = Decimal(str(venue.get(sym, 0)))
                short = have < 0
                missing = held > have if short else held < have
                if not missing:
                    continue
                key = (acct["id"], sym)
                seen_now[key] = (have, held)
                if self._drift_seen.get(key) == (have, held):
                    report["flags"].append(
                        f"POSITION DRIFT {acct['id']}: the book holds "
                        f"{have.normalize():f} {sym}, the venue holds "
                        f"{held.normalize():f} — a sale the book does not "
                        "know about; reconcile before trading this name")
        self._drift_seen = seen_now

    def _drift(self, report):
        """Compare the latest broker snapshot with the book (§12): the
        broker is presumed right; drift is FLAGGED, never silently fixed.

        Only a snapshot row that CARRIES cash or positions is compared, and
        today none does: snapshot_brokers stores the venue's raw answer with
        cash NULL, and no adapter reports positions. That is deliberate for
        cash — a paper account's book is an allocation the desk invented,
        not the venue's balance (Saxo SIM holds ~EUR 1m, the saxo-paper book
        is in SGD) — so comparing them would flag on every run. A real
        book-vs-venue POSITION comparison still has to be built; until then
        the checks that run are _ledger (the book against itself) and
        _leg_drift (working orders against the venue)."""
        for acct in self.conn.execute("SELECT id FROM broker_accounts"):
            snap = self.conn.execute(
                "SELECT * FROM broker_snapshots WHERE account_id=?"
                " ORDER BY taken_at DESC LIMIT 1", (acct["id"],)).fetchone()
            if snap is None:
                continue
            if snap["cash"] is not None:
                book = self.repo.cash_balance(acct["id"])
                if abs(snap["cash"] - book) > 1_000_000:      # > $1 drift
                    report["flags"].append(
                        f"CASH DRIFT {acct['id']}: broker reports "
                        f"{snap['cash'] / 1e6:.2f}, book says {book / 1e6:.2f}")
            positions = {}
            try:
                positions = (json.loads(snap["positions_json"] or "{}")
                             .get("positions") or {})
            except (TypeError, ValueError):
                pass
            for inst_id, broker_qty in positions.items():
                held, _, _ = self.repo.position(acct["id"], inst_id)
                if abs(held / 1e6 - float(broker_qty)) > 1e-6:
                    report["flags"].append(
                        f"POSITION DRIFT {acct['id']} {inst_id}: broker "
                        f"{broker_qty}, book {held / 1e6}")

    def _broker_sync(self, report):
        """Every account, every run: one cheap read from its venue (Saxo:
        balances; the built-in paper sim: its in-memory book) to prove the
        desk can still reach it, plus how many working orders were checked
        this run. One row per account in broker_sync, overwritten in place.
        A custodian with no adapter for a venue (the systemd timer one) does
        not touch that account's row — it never talked to the broker."""
        now = int(self.clock())
        for acct in self.conn.execute(
                "SELECT id, broker FROM broker_accounts").fetchall():
            adapter = self.brokers.get(acct["broker"])
            if adapter is None:
                continue
            seen = report["sync"].get(acct["id"], {"orders": 0, "errors": []})
            venue = getattr(adapter, "venue_label", None) or (
                f"Saxo {adapter.environment}"
                if type(adapter).__name__ == "SaxoBroker"
                else "built-in paper sim")
            try:
                adapter.snapshot()
                error = "; ".join(seen["errors"])[:300] or None
            except Exception as e:              # noqa: BLE001 — per account
                error = f"cannot reach {venue}: {e}"[:300]
            reached = not (error or "").startswith("cannot reach")
            self.conn.execute(
                "INSERT INTO broker_sync (account_id, venue, checked_at,"
                " contact_at, ok, orders_checked, error) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(account_id) DO UPDATE SET venue=excluded.venue,"
                " checked_at=excluded.checked_at,"
                " contact_at=COALESCE(excluded.contact_at, contact_at),"
                " ok=excluded.ok, orders_checked=excluded.orders_checked,"
                " error=excluded.error",
                (acct["id"], venue, now, now if reached else None,
                 int(error is None), seen["orders"], error))

    def _archive(self, report=None) -> int:
        """Terminal items > 30 days old get archived (hidden from the default
        run list; nothing is deleted — the audit trail is forever)."""
        now = int(self.clock())
        cur = self.conn.execute(
            "UPDATE work_items SET archived_at=? WHERE archived_at IS NULL"
            " AND state IN ('done','rejected','expired','failed','cancelled')"
            " AND updated_at < ?", (now, now - 30 * 86400))
        if report is not None:
            report["archived"] = cur.rowcount
        return cur.rowcount

    def snapshot_brokers(self):
        ts = int(self.clock())
        for acct in self.conn.execute("SELECT id, broker FROM broker_accounts"):
            adapter = self.brokers.get(acct["broker"])
            if adapter is None:
                continue
            try:
                snap = adapter.snapshot()
            except Exception as e:              # noqa: BLE001
                snap = {"error": str(e)}
            self.conn.execute(
                "INSERT INTO broker_snapshots (account_id, taken_at, cash,"
                " positions_json) VALUES (?,?,?,?)",
                (acct["id"], ts, None, json.dumps(snap, default=str)))


def _account_broker_from_db(conn):
    def account_broker(account_id):
        row = conn.execute("SELECT broker FROM broker_accounts WHERE id=?",
                           (account_id,)).fetchone()
        return row["broker"] if row else ""
    return account_broker
