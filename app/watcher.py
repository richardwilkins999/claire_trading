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

AUTO_DROP_PCT = 8       # every open position gets a down-8%-from-entry rule
ESCALATE_PCT = 3        # re-fire per further 3% decline
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
            self._ensure_auto_rule(inst["id"])
            try:
                price_base = self._price_base(pos)
            except Exception as e:              # noqa: BLE001
                report["unpriced"].append(f"{inst['id']}: {e}")
                continue
            for rule in self.conn.execute(
                    "SELECT * FROM price_alerts WHERE instrument_id=?"
                    " AND armed=1", (inst["id"],)).fetchall():
                if self._breached(rule, price_base, pos, today):
                    self._fire(rule, inst, price_base, today, report)
        status = ("degraded" if report["unpriced"] else "ok")
        self.conn.execute(
            "INSERT INTO service_health (service, checked_at, ok, detail)"
            " VALUES ('watcher', ?, ?, ?)",
            (int(self.clock()), status, json.dumps(report, default=str)))
        return report

    # ── helpers ──────────────────────────────────────────────────────────
    def _positions(self):
        out = []
        for r in self.conn.execute(
                "SELECT l.account_id, l.instrument_id,"
                "       SUM(l.qty_remaining) AS qty,"
                "       SUM(l.qty_remaining * l.cost_per_share_base"
                "           / 1000000.0) AS cost"
                " FROM lots l WHERE l.qty_remaining > 0 AND l.qty_opened > 0"
                " GROUP BY l.account_id, l.instrument_id").fetchall():
            inst = self.conn.execute("SELECT * FROM instruments WHERE id=?",
                                     (r["instrument_id"],)).fetchone()
            if inst is None:
                continue
            avg = from_micro(int(r["cost"])) / from_micro(r["qty"])
            out.append({"account_id": r["account_id"], "instrument": inst,
                        "qty": r["qty"], "avg_cost_base": avg})
        return out

    def _in_session_or_grace(self, exchange, now):
        try:
            if sessions.is_open(exchange, now, self.cal):
                return True
            close = sessions.close_of(exchange, now - GRACE, self.cal)
            return close <= now < close + GRACE
        except KeyError:
            return True                          # unknown venue: watch anyway

    def _ensure_auto_rule(self, instrument_id):
        self.conn.execute(
            "INSERT INTO price_alerts (instrument_id, rule, threshold, armed)"
            " SELECT ?, 'drop_pct_from_entry', ?, 1 WHERE NOT EXISTS"
            " (SELECT 1 FROM price_alerts WHERE instrument_id=?"
            "  AND rule='drop_pct_from_entry')",
            (instrument_id, AUTO_DROP_PCT, instrument_id))

    def _price_base(self, pos) -> Decimal:
        inst = pos["instrument"]
        acct = self.repo.account(pos["account_id"])
        q = self.market.quote(yahoo_symbol(inst["ticker"], inst["exchange"]))
        return Decimal(q["price"]) * self.market.fx(q["currency"],
                                                    acct["base_currency"])

    def _count_today(self, rule, today):
        return rule["fire_count_today"] if rule["fire_count_date"] == today else 0

    def _breached(self, rule, price_base, pos, today) -> bool:
        count = self._count_today(rule, today)
        if count >= MAX_FIRES_PER_DAY:
            return False
        kind, thr = rule["rule"], Decimal(str(rule["threshold"]))
        if kind == "below_price":
            return count == 0 and price_base < thr
        if kind == "above_price":
            return count == 0 and price_base > thr
        if kind == "drop_pct_from_entry":
            # escalating: 8%, then 11%, 14% … (§15) — a worsening position
            # keeps nudging without spamming
            needed = thr + ESCALATE_PCT * count
            floor = pos["avg_cost_base"] * (1 - needed / 100)
            return price_base <= floor
        return False

    def _fire(self, rule, inst, price_base, today, report):
        open_review = self.conn.execute(
            "SELECT 1 FROM work_items WHERE kind='sell_review' AND ticker=?"
            " AND state IN ('running','awaiting_approval','approved',"
            "'executing')", (inst["ticker"],)).fetchone()
        wi = None
        if not open_review:                     # don't stack reviews
            wi = self.start_sell_review(inst)
        count = self._count_today(rule, today)
        self.conn.execute(
            "UPDATE price_alerts SET last_fired_at=?, fire_count_today=?,"
            " fire_count_date=? WHERE id=?",
            (int(self.clock()), count + 1, today, rule["id"]))
        report["fired"].append({"instrument": inst["id"], "rule": rule["rule"],
                                "price_base": str(price_base),
                                "sell_review": wi})
