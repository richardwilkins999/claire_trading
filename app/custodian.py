"""The custodian (DESIGN.md §12): fills, expiry, retries, orphans, snapshots.
Deterministic; cannot approve or execute — /internal/resume accepts only
status:"expired" from actor:"reaper" on this path.
"""
import json
import time
from datetime import datetime, timezone

from . import approvals, sessions
from .accounting.money import from_micro, to_micro


class Custodian:
    def __init__(self, conn, repo, brokers, *, resume_post, account_broker,
                 fx_rate_for, clock=time.time, cal=None):
        self.conn, self.repo = conn, repo
        self.brokers = brokers                  # broker name -> adapter
        self.resume_post = resume_post
        self.account_broker = account_broker    # account_id -> broker name
        self.fx_rate_for = fx_rate_for          # (instrument_id, account_id) -> micro rate
        self.clock, self.cal = clock, cal

    def run_once(self) -> dict:
        report = {"placed": [], "fills": 0, "completed": [], "order_expired": [],
                  "approvals_expired": [], "resume_retried": [], "flags": []}
        try:
            self._place_pending_session(report)
            self._poll_fills(report)
            self._expire_orders(report)
            self._expire_approvals(report)
            report["resume_retried"] = approvals.retry_pending(
                self.conn, self.resume_post, clock=self.clock)
            self._orphans(report)
            ok = "degraded" if report["flags"] else "ok"
        except Exception as e:                  # noqa: BLE001 — net must not die silent
            report["flags"].append(f"custodian error: {e}")
            ok = "degraded"
        self.conn.execute(
            "INSERT INTO service_health (service, checked_at, ok, detail)"
            " VALUES ('custodian', ?, ?, ?)",
            (int(self.clock()), ok, json.dumps(report, default=str)))
        return report

    # ── orders ───────────────────────────────────────────────────────────
    def _adapter(self, account_id):
        return self.brokers[self.account_broker(account_id)]

    def _place_pending_session(self, report):
        now = datetime.fromtimestamp(int(self.clock()), tz=timezone.utc)
        for o in self.conn.execute(
                "SELECT o.*, i.ticker, i.exchange FROM orders o"
                " JOIN instruments i ON i.id=o.instrument_id"
                " WHERE o.status='pending_session'").fetchall():
            if not sessions.is_open(o["exchange"], now, self.cal):
                continue
            from .tools.market import yahoo_symbol
            placed = self._adapter(o["account_id"]).place_bracket(
                symbol=yahoo_symbol(o["ticker"], o["exchange"]),
                side=o["side"], qty=from_micro(o["qty"]),
                limit=from_micro(o["limit_price"]) if o["limit_price"] else None,
                stop_loss=from_micro(o["stop_loss"]) if o["stop_loss"] else None,
                take_profit=from_micro(o["take_profit"]) if o["take_profit"]
                else None)
            if placed.status == "accepted":
                self.repo.mark_order(o["id"], "placed", ts=int(self.clock()),
                                     broker_order_id=placed.broker_order_id)
                report["placed"].append(o["id"])
            else:
                self.repo.mark_order(o["id"], "rejected", ts=int(self.clock()))
                report["flags"].append(f"order {o['id']} rejected: {placed.detail}")

    def _poll_fills(self, report):
        for o in self.conn.execute(
                "SELECT * FROM orders WHERE status IN"
                " ('placed','partially_filled')").fetchall():
            if not o["broker_order_id"]:
                continue
            status = self._adapter(o["account_id"]).order_status(
                o["broker_order_id"])
            for f in status["fills"]:
                ex = self.repo.record_fill(
                    o["id"], broker_fill_id=f["fill_id"],
                    qty=to_micro(f["qty"]), price_native=to_micro(f["price"]),
                    fx_rate=self.fx_rate_for(o["instrument_id"],
                                             o["account_id"]),
                    ts=int(self.clock()))
                if ex:
                    report["fills"] += 1
            self._maybe_complete(o["id"], report)

    def _maybe_complete(self, order_id, report):
        o = self.repo.order(order_id)
        if o["status"] == "filled" and o["work_item_id"]:
            wi = self.conn.execute("SELECT state FROM work_items WHERE id=?",
                                   (o["work_item_id"],)).fetchone()
            if wi and wi["state"] == "executing":
                self.repo.set_state(o["work_item_id"], "done", actor="custodian",
                                    ts=int(self.clock()))
                report["completed"].append(o["work_item_id"])

    def _expire_orders(self, report):
        now = int(self.clock())
        for o in self.conn.execute(
                "SELECT * FROM orders WHERE status IN"
                " ('placed','partially_filled','pending_session')"
                " AND expires_at < ?", (now,)).fetchall():
            if o["broker_order_id"]:
                self._adapter(o["account_id"]).cancel(o["broker_order_id"])
            (fills,) = self.conn.execute(
                "SELECT COUNT(*) FROM executions WHERE order_id=?",
                (o["id"],)).fetchone()
            self.repo.mark_order(o["id"], "expired", ts=now)
            report["order_expired"].append(o["id"])
            if o["work_item_id"]:
                wi = self.conn.execute(
                    "SELECT state FROM work_items WHERE id=?",
                    (o["work_item_id"],)).fetchone()
                if wi and wi["state"] == "executing":
                    self.repo.set_state(
                        o["work_item_id"], "done" if fills else "expired",
                        actor="custodian", ts=now,
                        payload={"partial_fill": bool(fills),
                                 "order": o["id"]})

    # ── approvals ────────────────────────────────────────────────────────
    def _expire_approvals(self, report):
        now = int(self.clock())
        for r in self.conn.execute(
                "SELECT id, approval_token FROM work_items"
                " WHERE state='awaiting_approval' AND expires_at < ?",
                (now,)).fetchall():
            code, _ = self.resume_post({
                "work_item_id": r["id"], "status": "expired",
                "actor": "reaper", "token": r["approval_token"]})
            if code == 200:
                report["approvals_expired"].append(r["id"])
            else:
                report["flags"].append(f"expiry resume failed for {r['id']}: {code}")

    # ── orphans & guards ─────────────────────────────────────────────────
    def _orphans(self, report):
        now = int(self.clock())
        for r in self.conn.execute(
                "SELECT id FROM work_items WHERE state='running'"
                " AND updated_at < ?", (now - 7200,)):
            report["flags"].append(f"work item {r['id']} running > 2h")
        for r in self.conn.execute(
                "SELECT id FROM work_items WHERE state='approved'"
                " AND updated_at < ?", (now - 600,)):
            report["flags"].append(f"approved item {r['id']} has no order after 10m")
        for r in self.conn.execute(
                "SELECT l.id FROM lots l WHERE l.qty_remaining < 0"):
            report["flags"].append(f"lot {r['id']} negative remainder")

    def snapshot_brokers(self):
        ts = int(self.clock())
        for acct in self.conn.execute("SELECT id, broker FROM broker_accounts"):
            try:
                snap = self.brokers[acct["broker"]].snapshot()
            except Exception as e:              # noqa: BLE001
                snap = {"error": str(e)}
            self.conn.execute(
                "INSERT INTO broker_snapshots (account_id, taken_at, cash,"
                " positions_json) VALUES (?,?,?,?)",
                (acct["id"], ts, None, json.dumps(snap, default=str)))
