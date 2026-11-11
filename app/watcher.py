"""The price watcher (DESIGN.md §15): deterministic loop, session-aware,
escalating re-alerts, own health tile. On breach it LAUNCHES a sell_review —
selling still requires the same human approval as buying.
"""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import health, sessions
from .accounting.money import from_micro
from .tools.market import yahoo_symbol
import logging
from .logsetup import swallowed

log = logging.getLogger(__name__)

AUTO_DROP_PCT = 8       # default trail: 8% off the best price since entry
ESCALATE_PCT = 3        # re-fire per further 3% adverse move
MAX_FIRES_PER_DAY = 6
GRACE = timedelta(minutes=30)   # still watch briefly after the close


def position_floor(conn, instrument_id, account_id, side=1):
    """(trail %, peak_base) the watcher uses for one position. No rule yet
    means the default it will seed on its next tick."""
    r = conn.execute("SELECT threshold, peak_base FROM price_alerts WHERE"
                     " rule='trail_pct' AND instrument_id=? AND account_id=?"
                     " AND side=?", (instrument_id, account_id, side)
                     ).fetchone()
    if r is None:
        return float(AUTO_DROP_PCT), None
    return float(r["threshold"]), r["peak_base"]


def set_position_floor(conn, instrument_id, account_id, pct, side=1):
    """Change the trailing floor of a position already held (Telegram
    /floor). Only the rule's threshold changes: the high-water mark is kept,
    so the floor still trails from the best price since entry. Returns the
    previous %. An alert threshold, not an order — breaching it still only
    starts a sell review that needs approval."""
    pct = float(pct)
    if not 0 < pct <= 90:
        raise ValueError("the trailing alert must be between 0 and 90%")
    before, _ = position_floor(conn, instrument_id, account_id, side)
    cur = conn.execute(
        "UPDATE price_alerts SET threshold=?, armed=1 WHERE rule='trail_pct'"
        " AND instrument_id=? AND account_id=? AND side=?",
        (pct, instrument_id, account_id, side))
    if cur.rowcount == 0:
        try:
            conn.execute(
                "INSERT INTO price_alerts (instrument_id, account_id, side,"
                " rule, threshold, armed) VALUES (?,?,?,'trail_pct',?,1)",
                (instrument_id, account_id, side, pct))
        except sqlite3.IntegrityError:      # the watcher seeded it meanwhile
            conn.execute(
                "UPDATE price_alerts SET threshold=?, armed=1 WHERE"
                " rule='trail_pct' AND instrument_id=? AND account_id=?"
                " AND side=?", (pct, instrument_id, account_id, side))
    return before


class Watcher:
    def __init__(self, conn, repo, market, start_sell_review, *,
                 clock=time.time, cal=None, alert=None):
        self.conn, self.repo, self.market = conn, repo, market
        # fn(instrument_row, trigger=) -> work item id. No id (or an
        # exception) means the review did NOT start: the alert is then not
        # counted as fired and is tried again on the next tick.
        self.start_sell_review = start_sell_review
        self.clock, self.cal = clock, cal
        # out-of-band channel (app.alerts.send) for a breach whose review
        # could not be started; None = the health row only
        self.alert = alert
        self._launch_alerted = {}       # rule id -> day already alerted

    def tick(self) -> dict:
        report = {"checked": 0, "fired": [], "closed": [], "unpriced": [],
                  "launch_failed": []}
        now = datetime.fromtimestamp(int(self.clock()), tz=timezone.utc)
        today = now.date().isoformat()
        positions = self._positions()
        self._reset_flat_rules(positions)
        for pos in positions:
            inst = pos["instrument"]
            if not self._in_session_or_grace(inst["exchange"], now):
                report["closed"].append(inst["id"])
                continue
            report["checked"] += 1
            self._ensure_auto_rule(pos)
            try:
                price_base = self._price_base(pos)
            except Exception as e:              # noqa: BLE001
                report["unpriced"].append(f"{inst['id']}: {e}")
                continue
            # this position's rules only: a trailing rule belongs to one
            # (instrument, account, side) — a long and a short in the same
            # name, or two accounts, used to share one rule and one peak.
            # Price-level rules (below/above) with no account stay global.
            for rule in self.conn.execute(
                    "SELECT * FROM price_alerts WHERE instrument_id=?"
                    " AND armed=1 AND (account_id IS NULL OR account_id=?)"
                    " AND (side IS NULL OR side=?)",
                    (inst["id"], pos["account_id"], pos["side"])).fetchall():
                peak = (self._track_peak(rule, price_base, pos)
                        if rule["rule"] == "trail_pct" else None)
                if self._breached(rule, price_base, pos, today, peak):
                    self._fire(rule, inst, price_base, today, report, peak)
        status = ("degraded" if report["unpriced"] or report["launch_failed"]
                  else "ok")
        health.record(self.conn, "watcher", status, report, int(self.clock()))
        return report

    # ── helpers ──────────────────────────────────────────────────────────
    def _positions(self):
        """Longs AND shorts. A short is stored as a negative qty_opened with a
        positive qty_remaining, and it was previously filtered out entirely —
        which meant the one direction where a RISING price is the emergency
        had no safety net at all. Grouped by side so a name held both ways
        keeps two independent rules."""
        out = []
        for r in self.conn.execute(
                "SELECT l.account_id, l.instrument_id,"
                "       CASE WHEN l.qty_opened < 0 THEN -1 ELSE 1 END AS side,"
                "       SUM(l.qty_remaining) AS qty,"
                "       SUM(l.qty_remaining * l.cost_per_share_base"
                "           / 1000000.0) AS cost"
                " FROM lots l WHERE l.qty_remaining > 0"
                " GROUP BY l.account_id, l.instrument_id, side").fetchall():
            inst = self.conn.execute("SELECT * FROM instruments WHERE id=?",
                                     (r["instrument_id"],)).fetchone()
            if inst is None:
                continue
            avg = from_micro(int(r["cost"])) / from_micro(r["qty"])
            out.append({"account_id": r["account_id"], "instrument": inst,
                        "qty": r["qty"], "avg_cost_base": avg,
                        "side": r["side"]})
        return out

    def _in_session_or_grace(self, exchange, now):
        try:
            if sessions.is_open(exchange, now, self.cal):
                return True
            close = sessions.close_of(exchange, now - GRACE, self.cal)
            return close <= now < close + GRACE
        except KeyError:
            return True                          # unknown venue: watch anyway

    def _ensure_auto_rule(self, pos):
        """One trailing rule per position. A legacy row (no account/side,
        from before rules were keyed) is adopted rather than duplicated;
        otherwise the default is seeded with the peak at entry, so it behaves
        sensibly from its first tick, before the price has moved."""
        iid, acct, side = (pos["instrument"]["id"], pos["account_id"],
                           pos["side"])
        if self.conn.execute(
                "SELECT 1 FROM price_alerts WHERE rule='trail_pct'"
                " AND instrument_id=? AND account_id=? AND side=?",
                (iid, acct, side)).fetchone():
            return
        legacy = self.conn.execute(
            "SELECT id FROM price_alerts WHERE rule='trail_pct'"
            " AND instrument_id=? AND account_id IS NULL"
            " ORDER BY id DESC LIMIT 1", (iid,)).fetchone()
        try:
            if legacy:
                self.conn.execute(
                    "UPDATE price_alerts SET account_id=?, side=? WHERE id=?",
                    (acct, side, legacy["id"]))
            else:
                self.conn.execute(
                    "INSERT INTO price_alerts (instrument_id, account_id,"
                    " side, rule, threshold, armed, peak_base)"
                    " VALUES (?,?,?,'trail_pct',?,1,?)",
                    (iid, acct, side, AUTO_DROP_PCT,
                     float(pos["avg_cost_base"])))
        except sqlite3.IntegrityError:       # execute seeded it meanwhile
            pass

    def _reset_flat_rules(self, positions):
        """A position that has gone flat loses its trailing alert: the shares
        are gone, so there is nothing left to guard (CSCO, 2026-11-06: its
        stop sold the shares and the alert sat on the Portfolio page as if
        it still watched something). A re-buy gets a fresh rule seeded at
        the new entry — never the old peak, which may sit far above it.

        Kept while an ENTRY order for that name/account/side is still
        working: execute writes the approval's own trailing % before the
        fill, and deleting it in that gap would quietly reset it to 8%."""
        live = {(p["instrument"]["id"], p["account_id"], p["side"])
                for p in positions}
        for r in self.conn.execute(
                "SELECT id, instrument_id, account_id, side FROM price_alerts"
                " WHERE rule='trail_pct' AND account_id IS NOT NULL"
                ).fetchall():
            if (r["instrument_id"], r["account_id"], r["side"]) in live:
                continue
            side = "buy" if (r["side"] or 1) > 0 else "sell"
            if self.conn.execute(
                    "SELECT 1 FROM orders WHERE instrument_id=? AND"
                    " account_id=? AND side=? AND leg IS NULL AND status IN"
                    " ('pending_session','placed','partially_filled')",
                    (r["instrument_id"], r["account_id"], side)).fetchone():
                self.conn.execute(
                    "UPDATE price_alerts SET peak_base=NULL,"
                    " fire_count_today=0 WHERE id=?", (r["id"],))
                continue
            self.conn.execute("DELETE FROM price_alerts WHERE id=?",
                              (r["id"],))

    def _track_peak(self, rule, price_base, pos) -> Decimal:
        """Ratchet the high-water mark — the whole point of a TRAILING stop.
        Measured from the best price SINCE ENTRY, so gains are protected as
        they accrue; previously the floor was pinned to average cost and a
        position could round-trip a 30% gain without ever alerting.
        For a short the best price is the LOWEST one."""
        peak = rule["peak_base"]
        seeded = peak is None           # new (or re-opened) position: start
        peak = Decimal(str(peak)) if peak is not None \
            else Decimal(pos["avg_cost_base"])         # from its own entry
        better = price_base > peak if pos["side"] > 0 else price_base < peak
        if better:
            peak = price_base
        if better or seeded:
            self.conn.execute(
                "UPDATE price_alerts SET peak_base=? WHERE id=?",
                (float(peak), rule["id"]))
        return peak

    def _price_base(self, pos) -> Decimal:
        inst = pos["instrument"]
        acct = self.repo.account(pos["account_id"])
        q = self.market.quote(yahoo_symbol(inst["ticker"], inst["exchange"]))
        return Decimal(q["price"]) * self.market.fx(q["currency"],
                                                    acct["base_currency"])

    def _count_today(self, rule, today):
        return rule["fire_count_today"] if rule["fire_count_date"] == today else 0

    def _breached(self, rule, price_base, pos, today, peak=None) -> bool:
        count = self._count_today(rule, today)
        if count >= MAX_FIRES_PER_DAY:
            return False
        kind, thr = rule["rule"], Decimal(str(rule["threshold"]))
        if kind == "below_price":
            return count == 0 and price_base < thr
        if kind == "above_price":
            return count == 0 and price_base > thr
        if kind == "trail_pct":
            # escalating: 8%, then 11%, 14% … (§15) — a worsening position
            # keeps nudging without spamming
            needed = thr + ESCALATE_PCT * count
            ref = peak if peak is not None else pos["avg_cost_base"]
            if pos["side"] > 0:
                return price_base <= ref * (1 - needed / 100)
            # short: the loss is the price RISING away from the best price
            return price_base >= ref * (1 + needed / 100)
        return False

    def _fire(self, rule, inst, price_base, today, report, peak=None):
        open_review = self.conn.execute(
            "SELECT id, state FROM work_items WHERE kind='sell_review'"
            " AND ticker=? AND state IN ('running','awaiting_approval',"
            "'approved','executing') ORDER BY created_at DESC LIMIT 1",
            (inst["ticker"],)).fetchone()
        count = self._count_today(rule, today)
        trig = self._trigger_text(rule, price_base, peak, count)
        wi, escalated = None, False
        if open_review:
            # Don't stack reviews — but don't swallow the escalation either.
            # This used to burn a fire and produce NOTHING: the position got
            # worse, the counter advanced toward its daily cap, and the card
            # in front of the human still quoted the first breach.
            wi = open_review["id"]
            escalated = self._escalate(open_review, trig, price_base, count)
        else:
            try:
                wi = self.start_sell_review(inst, trigger=trig)
                error = None if wi else "no work item came back"
            except Exception as e:              # noqa: BLE001 — api down etc
                wi, error = None, str(e)[:200] or type(e).__name__
            if not wi:
                # The review did not start, so this is NOT a fire. It used
                # to be counted anyway: nothing reached the human, one of
                # the day's fires was spent, and the next alert needed a
                # further 3% fall. Left uncounted, the next tick tries again.
                self._launch_failed(rule, inst, trig, error, today, report)
                return
        self.conn.execute(
            "UPDATE price_alerts SET last_fired_at=?, fire_count_today=?,"
            " fire_count_date=? WHERE id=?",
            (int(self.clock()), count + 1, today, rule["id"]))
        report["fired"].append({"instrument": inst["id"], "rule": rule["rule"],
                                "price_base": str(price_base),
                                "peak_base": str(peak) if peak else None,
                                "escalated": escalated,
                                "sell_review": wi})

    def _launch_failed(self, rule, inst, trig, error, today, report):
        """A breached alert whose sell review could not be started. Recorded
        on the health row every tick it persists; sent out of band once per
        rule per day, so an outage of claire-api does not message every
        ten minutes."""
        report["launch_failed"].append({"instrument": inst["id"],
                                        "rule": rule["rule"], "error": error})
        log.error("alert breached on %s but the sell review did not start "
                  "(%s) — retrying next tick", inst["id"], error)
        if self.alert is None or self._launch_alerted.get(rule["id"]) == today:
            return
        self._launch_alerted[rule["id"]] = today
        try:
            self.alert(f"{inst['ticker']}: {trig} — but the sell review could "
                       f"not be started ({error}). Trying again each check; "
                       "look at the position yourself.",
                       title="Claire — sell review NOT started")
        except Exception as e:                  # noqa: BLE001 — best effort
            swallowed(log, "launch-failure alert was not sent", e)
            pass

    def _trigger_text(self, rule, price_base, peak, count) -> str:
        if rule["rule"] != "trail_pct" or peak is None:
            return (f"watcher: {rule['rule']} breached — last {price_base:.2f} "
                    f"vs threshold {rule['threshold']}")
        needed = Decimal(str(rule["threshold"])) + ESCALATE_PCT * count
        off = (price_base / peak - 1) * 100
        return (f"watcher: trailing alert hit — last {price_base:.2f}, "
                f"{off:+.1f}% from the best price since entry ({peak:.2f}); "
                f"the alert is {needed}%")

    def _escalate(self, review, trig, price_base, count) -> bool:
        """A worsening position must reach the human, not just the counter.
        The open review keeps its identity — restacking would lose the
        approval token — but its trigger is rewritten to the CURRENT number,
        which is what the approval card displays, and the deterioration is
        recorded as an event on the run."""
        self.conn.execute(
            "UPDATE work_items SET trigger=?, updated_at=? WHERE id=?",
            (f"{trig} (escalation {count + 1})", int(self.clock()),
             review["id"]))
        self.conn.execute(
            "INSERT INTO events (item_id, ts, actor, from_state, to_state,"
            " payload) VALUES (?,?,?,?,?,?)",
            (review["id"], int(self.clock()), "watcher", review["state"],
             "watcher_escalation",
             json.dumps({"price_base": str(price_base),
                         "escalation": count + 1, "detail": trig})))
        return True
