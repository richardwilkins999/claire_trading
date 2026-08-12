"""The price watcher (DESIGN.md §15): deterministic loop, session-aware,
escalating re-alerts, own health tile. On breach it LAUNCHES a sell_review —
selling still requires the same human approval as buying.
"""
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import sessions
from .accounting.money import from_micro
from .tools.market import yahoo_symbol

AUTO_DROP_PCT = 8       # default trail: 8% off the best price since entry
ESCALATE_PCT = 3        # re-fire per further 3% adverse move
MAX_FIRES_PER_DAY = 6
GRACE = timedelta(minutes=30)   # still watch briefly after the close


class Watcher:
    def __init__(self, conn, repo, market, start_sell_review, *,
                 clock=time.time, cal=None):
        self.conn, self.repo, self.market = conn, repo, market
        self.start_sell_review = start_sell_review  # fn(instrument_row) -> wi id
        self.clock, self.cal = clock, cal

    def tick(self) -> dict:
        report = {"checked": 0, "fired": [], "closed": [], "unpriced": []}
        now = datetime.fromtimestamp(int(self.clock()), tz=timezone.utc)
        today = now.date().isoformat()
        for pos in self._positions():
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
            for rule in self.conn.execute(
                    "SELECT * FROM price_alerts WHERE instrument_id=?"
                    " AND armed=1", (inst["id"],)).fetchall():
                peak = (self._track_peak(rule, price_base, pos)
                        if rule["rule"] == "trail_pct" else None)
                if self._breached(rule, price_base, pos, today, peak):
                    self._fire(rule, inst, price_base, today, report, peak)
        status = ("degraded" if report["unpriced"] else "ok")
        self.conn.execute(
            "INSERT INTO service_health (service, checked_at, ok, detail)"
            " VALUES ('watcher', ?, ?, ?)",
            (int(self.clock()), status, json.dumps(report, default=str)))
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
        """Seed the peak at entry so a rule behaves sensibly from its first
        tick, before the price has moved anywhere."""
        self.conn.execute(
            "INSERT INTO price_alerts (instrument_id, rule, threshold, armed,"
            " peak_base) SELECT ?, 'trail_pct', ?, 1, ? WHERE NOT EXISTS"
            " (SELECT 1 FROM price_alerts WHERE instrument_id=?"
            "  AND rule='trail_pct')",
            (pos["instrument"]["id"], AUTO_DROP_PCT,
             float(pos["avg_cost_base"]), pos["instrument"]["id"]))

    def _track_peak(self, rule, price_base, pos) -> Decimal:
        """Ratchet the high-water mark — the whole point of a TRAILING stop.
        Measured from the best price SINCE ENTRY, so gains are protected as
        they accrue; previously the floor was pinned to average cost and a
        position could round-trip a 30% gain without ever alerting.
        For a short the best price is the LOWEST one."""
        peak = rule["peak_base"]
        peak = Decimal(str(peak)) if peak is not None \
            else Decimal(pos["avg_cost_base"])
        better = price_base > peak if pos["side"] > 0 else price_base < peak
        if better:
            peak = price_base
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
            wi = self.start_sell_review(inst, trigger=trig)
        self.conn.execute(
            "UPDATE price_alerts SET last_fired_at=?, fire_count_today=?,"
            " fire_count_date=? WHERE id=?",
            (int(self.clock()), count + 1, today, rule["id"]))
        report["fired"].append({"instrument": inst["id"], "rule": rule["rule"],
                                "price_base": str(price_base),
                                "peak_base": str(peak) if peak else None,
                                "escalated": escalated,
                                "sell_review": wi})

    def _trigger_text(self, rule, price_base, peak, count) -> str:
        if rule["rule"] != "trail_pct" or peak is None:
            return (f"watcher: {rule['rule']} breached — last {price_base:.2f} "
                    f"vs threshold {rule['threshold']}")
        needed = Decimal(str(rule["threshold"])) + ESCALATE_PCT * count
        off = (price_base / peak - 1) * 100
        return (f"watcher: trailing stop breached — last {price_base:.2f}, "
                f"{off:+.1f}% from the best price since entry ({peak:.2f}); "
                f"floor is {needed}%")

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
