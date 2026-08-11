"""The ONLY desk.db writer (DESIGN.md §7). Nodes and API handlers call this;
nothing else touches SQL; no LLM ever writes here.

All quantities and money are micro-unit ints (money.py). Timestamps are unix
seconds, passed in by callers — this module never reads the clock, which keeps
every operation replayable in tests.
"""
import json
import sqlite3
import uuid
from decimal import Decimal

from .lots import OpenLot, match
from .money import MICRO, mul_micro, to_micro

TERMINAL_ORDER = {"filled", "cancelled", "expired", "rejected"}


class LedgerError(Exception):
    pass


class InsufficientCash(LedgerError):
    pass


class RiskLimitExceeded(LedgerError):
    pass


class Repo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── setup ────────────────────────────────────────────────────────────
    def create_account(self, id, broker, environment, base_currency,
                       fee_model: dict, risk_limits: dict | None = None, *, ts):
        if environment not in ("paper", "sim"):
            raise LedgerError(f"live trading is unrepresentable: {environment!r}")
        self.conn.execute(
            "INSERT INTO broker_accounts (id, broker, environment, base_currency,"
            " fee_model, risk_limits, opened_at) VALUES (?,?,?,?,?,?,?)",
            (id, broker, environment, base_currency, json.dumps(fee_model),
             json.dumps(risk_limits or {}), ts))

    def add_instrument(self, id, ticker, exchange, currency, name=None):
        self.conn.execute(
            "INSERT OR IGNORE INTO instruments (id, ticker, exchange, currency, name)"
            " VALUES (?,?,?,?,?)", (id, ticker, exchange, currency, name))

    def account(self, account_id) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM broker_accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no account {account_id!r}")
        return row

    def instrument(self, instrument_id) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM instruments WHERE id=?", (instrument_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no instrument {instrument_id!r}")
        return row

    # ── cash ─────────────────────────────────────────────────────────────
    def cash_balance(self, account_id) -> int:
        (bal,) = self.conn.execute(
            "SELECT COALESCE(SUM(amount_base),0) FROM cash_transactions"
            " WHERE account_id=?", (account_id,)).fetchone()
        return bal

    def deposit(self, account_id, amount: int, *, ts, note=None):
        if amount <= 0:
            raise LedgerError("deposit must be positive")
        self._cash(account_id, "deposit", amount, ts, note=note)

    def withdraw(self, account_id, amount: int, *, ts, note=None):
        if amount <= 0:
            raise LedgerError("withdrawal must be positive")
        if self.cash_balance(account_id) < amount:
            raise InsufficientCash("withdrawal exceeds balance")
        self._cash(account_id, "withdrawal", -amount, ts, note=note)

    def _cash(self, account_id, kind, amount, ts, execution_id=None, note=None):
        self.conn.execute(
            "INSERT INTO cash_transactions (account_id, kind, amount_base,"
            " execution_id, note, occurred_at) VALUES (?,?,?,?,?,?)",
            (account_id, kind, amount, execution_id, note, ts))

    # ── work items ───────────────────────────────────────────────────────
    def create_work_item(self, id, kind, ticker, *, ts, thread_id=None):
        self.conn.execute(
            "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (id, kind, ticker, "running", thread_id or id, ts, ts))
        self._event(id, ts, "system", None, "running")

    def set_state(self, item_id, to_state, *, actor, ts, payload=None):
        row = self.conn.execute(
            "SELECT state FROM work_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no work item {item_id!r}")
        self.conn.execute(
            "UPDATE work_items SET state=?, updated_at=? WHERE id=?",
            (to_state, ts, item_id))
        self._event(item_id, ts, actor, row["state"], to_state, payload)

    def _event(self, item_id, ts, actor, from_state, to_state, payload=None):
        self.conn.execute(
            "INSERT INTO events (item_id, ts, actor, from_state, to_state, payload)"
            " VALUES (?,?,?,?,?,?)",
            (item_id, ts, actor, from_state, to_state,
             json.dumps(payload) if payload is not None else None))

    # ── orders ───────────────────────────────────────────────────────────
    def create_order(self, *, id, account_id, instrument_id, side, qty,
                     expires_at, ts, status="placed", work_item_id=None,
                     limit_price=None, stop_loss=None, take_profit=None,
                     broker_order_id=None):
        if qty <= 0:
            raise LedgerError("order qty must be positive")
        self.check_risk(account_id, instrument_id, side, qty, limit_price, ts)
        self.conn.execute(
            "INSERT INTO orders (id, work_item_id, account_id, instrument_id, side,"
            " qty, limit_price, stop_loss, take_profit, status, broker_order_id,"
            " expires_at, placed_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (id, work_item_id, account_id, instrument_id, side, qty, limit_price,
             stop_loss, take_profit, status, broker_order_id,
             expires_at, ts if status == "placed" else None, ts))
        return id

    def mark_order(self, order_id, status, *, ts, broker_order_id=None):
        sets, args = ["status=?", "updated_at=?"], [status, ts]
        if broker_order_id is not None:
            sets.append("broker_order_id=?")
            args.append(broker_order_id)
        if status == "placed":
            sets.append("placed_at=?")
            args.append(ts)
        args.append(order_id)
        self.conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=?", args)

    def order(self, order_id) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no order {order_id!r}")
        return row

    def open_orders(self):
        return self.conn.execute(
            "SELECT * FROM orders WHERE status IN"
            " ('pending_session','placed','partially_filled')").fetchall()

    def check_risk(self, account_id, instrument_id, side, qty, limit_price, ts):
        """Public so the executor can validate BEFORE the venue sees the
        order; create_order runs it again (idempotent, cheap)."""
        acct = self.account(account_id)
        limits = json.loads(acct["risk_limits"])
        if not limits:
            return
        notional = mul_micro(qty, limit_price) if limit_price else None
        cap = limits.get("max_order_base")
        if cap is not None and notional is not None and notional > to_micro(str(cap)):
            raise RiskLimitExceeded(
                f"order notional {notional} exceeds max_order_base {cap}")
        cap = limits.get("max_open_positions")
        if cap is not None and side == "buy":
            (n,) = self.conn.execute(
                "SELECT COUNT(DISTINCT instrument_id) FROM lots"
                " WHERE account_id=? AND qty_remaining > 0"
                " AND instrument_id != ?", (account_id, instrument_id)).fetchone()
            if n + 1 > cap:
                raise RiskLimitExceeded(f"would exceed max_open_positions {cap}")
        cap = limits.get("max_position_pct")
        if cap is not None and side == "buy" and notional is not None:
            (open_cost,) = self.conn.execute(
                "SELECT COALESCE(SUM(qty_remaining*cost_per_share_base/1e6),0)"
                " FROM lots WHERE account_id=? AND qty_remaining>0"
                " AND qty_opened>0", (account_id,)).fetchone()
            (inst_cost,) = self.conn.execute(
                "SELECT COALESCE(SUM(qty_remaining*cost_per_share_base/1e6),0)"
                " FROM lots WHERE account_id=? AND instrument_id=?"
                " AND qty_remaining>0 AND qty_opened>0",
                (account_id, instrument_id)).fetchone()
            equity = self.cash_balance(account_id) + int(open_cost)
            if equity > 0 and \
                    (int(inst_cost) + notional) / equity * 100 > cap:
                raise RiskLimitExceeded(
                    f"position would exceed max_position_pct {cap}% of equity")
        cap = limits.get("max_trades_per_day")
        if cap is not None:
            day = ts - (ts % 86400)
            (n,) = self.conn.execute(
                "SELECT COUNT(*) FROM orders WHERE account_id=?"
                " AND placed_at IS NOT NULL AND placed_at >= ? AND placed_at < ?",
                (account_id, day, day + 86400)).fetchone()
            if n + 1 > cap:
                raise RiskLimitExceeded(f"would exceed max_trades_per_day {cap}")

    # ── fills: the money path ────────────────────────────────────────────
    def record_fill(self, order_id, *, broker_fill_id, qty, price_native,
                    fx_rate, ts, commission=None, other_fees=0,
                    intended_price=None):
        """Record one broker fill: execution row + lots/closures + cash, in one
        transaction. Idempotent on (broker_order_id, broker_fill_id) — replaying
        a fill returns the existing execution id and writes nothing.

        `commission=None` computes it from the account's fee_model, charged
        cumulatively across an order's fills (a flat fee bills once, a pct fee
        bills each increment, a min tops up only what earlier fills didn't
        cover). Brokers that report real commissions pass them explicitly.
        """
        o = self.order(order_id)
        if o["status"] in TERMINAL_ORDER and o["status"] != "filled":
            raise LedgerError(f"order {order_id} is {o['status']}; fill refused")
        if qty <= 0:
            raise LedgerError("fill qty must be positive")
        acct = self.account(o["account_id"])
        broker_order_id = o["broker_order_id"] or order_id

        existing = self.conn.execute(
            "SELECT id FROM executions WHERE broker_order_id=? AND broker_fill_id=?",
            (broker_order_id, broker_fill_id)).fetchone()
        if existing:
            return existing["id"]

        gross_base = mul_micro(mul_micro(qty, price_native), fx_rate)
        price_base = mul_micro(price_native, fx_rate)
        if commission is None:
            commission = self._fee_increment(o, acct, gross_base)
        fees = commission + other_fees
        ex_id = f"ex_{uuid.uuid4().hex[:12]}"

        cur = self.conn
        cur.execute("BEGIN IMMEDIATE")
        try:
            (filled,) = cur.execute(
                "SELECT COALESCE(SUM(qty),0) FROM executions WHERE order_id=?",
                (order_id,)).fetchone()
            if filled + qty > o["qty"]:
                raise LedgerError(
                    f"fill overruns order: {filled}+{qty} > {o['qty']}")

            if o["side"] == "buy":
                net_base = gross_base + fees
                if self.cash_balance(o["account_id"]) < net_base:
                    raise InsufficientCash(
                        f"buy needs {net_base}, balance is short")
            else:
                net_base = gross_base - fees

            cur.execute(
                "INSERT INTO executions (id, order_id, account_id, instrument_id,"
                " work_item_id, side, qty, price_native, currency, fx_rate,"
                " commission, other_fees, gross_base, net_base, intended_price,"
                " broker_order_id, broker_fill_id, executed_at, recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ex_id, order_id, o["account_id"], o["instrument_id"],
                 o["work_item_id"], o["side"], qty, price_native,
                 self.instrument(o["instrument_id"])["currency"], fx_rate,
                 commission, other_fees, gross_base, net_base, intended_price,
                 broker_order_id, broker_fill_id, ts, ts))

            self._apply_fill_to_lots(cur, o, ex_id, qty, price_base, fees, ts)

            if o["side"] == "buy":
                self._cash(o["account_id"], "trade_buy", -gross_base, ts, ex_id)
            else:
                self._cash(o["account_id"], "trade_sell", gross_base, ts, ex_id)
            if fees:
                self._cash(o["account_id"], "commission", -fees, ts, ex_id)

            new_status = "filled" if filled + qty == o["qty"] else "partially_filled"
            cur.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?",
                        (new_status, ts, order_id))
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        return ex_id

    def _fee_increment(self, order_row, acct, gross_base_this_fill) -> int:
        fee = json.loads(acct["fee_model"])
        rows = self.conn.execute(
            "SELECT COALESCE(SUM(gross_base),0), COALESCE(SUM(commission),0)"
            " FROM executions WHERE order_id=?", (order_row["id"],)).fetchone()
        prior_gross, prior_comm = rows
        total = self._fee_for(fee, prior_gross + gross_base_this_fill)
        return max(0, total - prior_comm)

    @staticmethod
    def _fee_for(fee: dict, notional_base: int) -> int:
        kind = fee.get("type", "flat")
        if kind == "flat":
            return to_micro(str(fee.get("per_trade", "0")))
        if kind == "pct":
            pct = Decimal(str(fee["pct"]))
            raw = int((Decimal(notional_base) * pct).quantize(Decimal(1)))
            return max(raw, to_micro(str(fee.get("min", "0"))))
        raise LedgerError(f"unknown fee model type {kind!r}")

    def _apply_fill_to_lots(self, cur, order_row, ex_id, qty, price_base,
                            fees, ts):
        account_id, instrument_id = order_row["account_id"], order_row["instrument_id"]
        side = order_row["side"]
        opposing_short = (side == "buy")   # a buy closes SHORT lots first
        open_lots = [
            OpenLot(id=r["id"], qty_remaining=r["qty_remaining"],
                    cost_per_share_base=r["cost_per_share_base"],
                    commission_allocated=r["commission_allocated"],
                    is_short=r["qty_opened"] < 0, opened_at=r["opened_at"])
            for r in cur.execute(
                "SELECT * FROM lots WHERE account_id=? AND instrument_id=?"
                " AND qty_remaining > 0 AND (qty_opened < 0) = ?"
                " ORDER BY opened_at, id",
                (account_id, instrument_id, opposing_short)).fetchall()
        ]
        closures, unmatched = match(open_lots, qty, price_base, fees,
                                    closing_short=opposing_short)
        matched_qty = qty - unmatched
        for c in closures:
            opened_at = cur.execute("SELECT opened_at FROM lots WHERE id=?",
                                    (c.lot_id,)).fetchone()["opened_at"]
            cur.execute(
                "INSERT INTO lot_closures (lot_id, close_execution_id, qty,"
                " proceeds_base, cost_base, commission_base, realized_pl_base,"
                " holding_days, closed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (c.lot_id, ex_id, c.qty, c.proceeds_base, c.cost_base,
                 c.commission_base, c.realized_pl_base,
                 (ts - opened_at) // 86400, ts))
            cur.execute(
                "UPDATE lots SET qty_remaining = qty_remaining - ?,"
                " commission_allocated = commission_allocated - ? WHERE id=?",
                (c.qty, c.open_commission_consumed, c.lot_id))
        if unmatched > 0:
            # remainder opens a fresh lot: long for a buy, short for a sell;
            # it carries the slice of this fill's fees the closures didn't take
            from .money import pro_rata
            lot_fees = fees - pro_rata(fees, matched_qty, qty) if qty else fees
            cur.execute(
                "INSERT INTO lots (id, open_execution_id, account_id,"
                " instrument_id, qty_opened, qty_remaining, cost_per_share_base,"
                " commission_allocated, opened_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"lot_{uuid.uuid4().hex[:12]}", ex_id, account_id, instrument_id,
                 unmatched if side == "buy" else -unmatched, unmatched,
                 price_base, lot_fees, ts))

    # ── corporate actions ────────────────────────────────────────────────
    def apply_split(self, instrument_id, ratio, *, ex_date, ts, detail=None):
        """Apply a share split to open lots as an auditable row (§7): a 2:1
        split doubles remaining shares and halves per-share cost; total cost
        basis is conserved to the micro."""
        r = Decimal(str(ratio))
        if r <= 0:
            raise LedgerError("split ratio must be positive")
        cur = self.conn
        cur.execute("BEGIN IMMEDIATE")
        try:
            for lot in cur.execute(
                    "SELECT * FROM lots WHERE instrument_id=?"
                    " AND qty_remaining > 0", (instrument_id,)).fetchall():
                new_rem = int((Decimal(lot["qty_remaining"]) * r)
                              .quantize(Decimal(1)))
                new_opened = int((Decimal(lot["qty_opened"]) * r)
                                 .quantize(Decimal(1)))
                # conserve basis exactly: recompute per-share from the total
                total_cost = lot["qty_remaining"] * lot["cost_per_share_base"]
                new_cps = int(Decimal(total_cost) / new_rem) if new_rem else 0
                cur.execute(
                    "UPDATE lots SET qty_remaining=?, qty_opened=?,"
                    " cost_per_share_base=? WHERE id=?",
                    (new_rem, new_opened, new_cps, lot["id"]))
            cur.execute(
                "INSERT INTO corporate_actions (instrument_id, kind, ratio,"
                " ex_date, applied_at, detail) VALUES (?,?,?,?,?,?)",
                (instrument_id, "split", float(r), ex_date, ts, detail))
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise

    # ── queries the tests and views build on ─────────────────────────────
    def position(self, account_id, instrument_id):
        """(signed qty, total cost ex-comm, total commission attached)."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN qty_opened < 0 THEN -qty_remaining"
            "                         ELSE qty_remaining END), 0) AS qty,"
            "       COALESCE(SUM(CASE WHEN qty_opened < 0 THEN 0 ELSE 1 END *"
            "                    qty_remaining * cost_per_share_base / ?), 0) AS cost,"
            "       COALESCE(SUM(commission_allocated), 0) AS comm"
            " FROM lots WHERE account_id=? AND instrument_id=? AND qty_remaining>0",
            (MICRO, account_id, instrument_id)).fetchone()
        return row["qty"], int(row["cost"]), row["comm"]

    def realized_pl(self, account_id, instrument_id=None) -> int:
        q = ("SELECT COALESCE(SUM(c.realized_pl_base),0) FROM lot_closures c"
             " JOIN lots l ON l.id = c.lot_id WHERE l.account_id=?")
        args = [account_id]
        if instrument_id:
            q += " AND l.instrument_id=?"
            args.append(instrument_id)
        (pl,) = self.conn.execute(q, args).fetchone()
        return pl

    def assert_invariants(self, account_id):
        """Raise if the books are inconsistent. Cheap; tests call it after
        every operation, the custodian calls it on a timer."""
        bal = 0
        for r in self.conn.execute(
                "SELECT amount_base FROM cash_transactions WHERE account_id=?"
                " ORDER BY occurred_at, id", (account_id,)):
            bal += r["amount_base"]
            if bal < 0:
                raise LedgerError("cash went negative mid-history")
        for r in self.conn.execute(
                "SELECT l.id, ABS(l.qty_opened) AS opened, l.qty_remaining,"
                "       l.commission_allocated,"
                "       COALESCE((SELECT SUM(qty) FROM lot_closures c"
                "                 WHERE c.lot_id = l.id), 0) AS closed"
                " FROM lots l WHERE l.account_id=?", (account_id,)):
            if r["opened"] != r["qty_remaining"] + r["closed"]:
                raise LedgerError(f"lot {r['id']} shares don't reconcile")
            if r["commission_allocated"] < 0:
                raise LedgerError(f"lot {r['id']} negative commission")
