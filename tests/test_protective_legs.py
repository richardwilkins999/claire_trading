"""Exits & protective legs (DESIGN.md §11a) and manual trades — offline.

`LegVenue` behaves like Saxo where it matters here (shapes checked against
SIM on 2026-11-04): a bracket's stop and target are orders of their own,
one-cancels-other once the entry fills; cancelling one leg may cancel its
sibling, so the second cancel errors on an order that is already gone; and
a new sell is refused while sell legs rest
(SellOrdersAlreadyExistForOwnedContracts). The Saxo adapter itself is tested
against httpx.MockTransport with Saxo's real response shapes."""
import itertools
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app import live_trading, protection
from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo, RiskLimitExceeded
from app.custodian import Custodian
from app.graph.nodes_execution import build_execute, submit_order
from app.graph.state import Approval, Instrument, PipelineState, Thesis
from app.tools.brokers import BrokerError, PlacedOrder
from app.tools.saxo_broker import SaxoBroker
from tests.test_live_trading import AAPL_DETAILS, ref_handler

T0 = 1_786_456_800                       # Tue 2026-08-11 14:00 UTC, NASDAQ open
CSCO = Instrument(id="NASDAQ:CSCO", ticker="CSCO", exchange="NASDAQ",
                  currency="USD")


# ── a venue that keeps legs as orders, like Saxo ─────────────────────────
class LegVenue:
    fractional = False

    def __init__(self):
        self.orders, self.calls = {}, []
        self.refuse_cancel, self.refuse_oco, self.refuse_place = \
            set(), False, False
        self._seq = itertools.count(5000)
        self._fill = itertools.count(1)

    def _new(self, **kw):
        oid = str(next(self._seq))
        self.orders[oid] = {"status": "placed", "fills": [],
                            "filled": Decimal(0), "oco": None, **kw}
        return oid

    def _protect(self, symbol, side, qty, stop, target):
        legs, ids = [], []
        for kind, typ, px in (("stop", "StopIfTraded", stop),
                              ("target", "Limit", target)):
            if px:
                oid = self._new(symbol=symbol, side=side, qty=qty, type=typ,
                                price=float(px), leg=kind)
                ids.append(oid)
                legs.append({"leg": kind, "order_id": oid, "price": float(px),
                             "verified": True})
        if len(ids) == 2:
            self.orders[ids[0]]["oco"], self.orders[ids[1]]["oco"] = \
                ids[1], ids[0]
        return legs

    def _sell_legs_rest(self, symbol):
        return any(o["status"] in ("placed", "partially_filled")
                   and o.get("leg") and o["side"] == "sell"
                   and o["symbol"] == symbol for o in self.orders.values())

    def place_bracket(self, *, symbol, side, qty, limit=None, stop_loss=None,
                      take_profit=None, client_order_id=None):
        qty = Decimal(str(qty))
        self.calls.append(("place", side, qty, stop_loss, take_profit))
        if self.refuse_place:
            return PlacedOrder("", "rejected", "TooFarFromMarket")
        if side == "sell" and self._sell_legs_rest(symbol):
            return PlacedOrder("", "rejected",
                               "SellOrdersAlreadyExistForOwnedContracts")
        oid = self._new(symbol=symbol, side=side, qty=qty, type="Limit",
                        price=limit, leg=None)
        return PlacedOrder(oid, "accepted", legs=self._protect(
            symbol, "sell" if side == "buy" else "buy", qty, stop_loss,
            take_profit))

    def place_oco(self, *, symbol, side, qty, stop_loss=None,
                  take_profit=None, client_order_id=None):
        qty = Decimal(str(qty))
        self.calls.append(("oco", side, qty, stop_loss, take_profit))
        if self.refuse_oco:
            return PlacedOrder("", "rejected", "venue said no")
        legs = self._protect(symbol, side, qty, stop_loss, take_profit)
        return PlacedOrder(legs[0]["order_id"], "accepted", legs=legs)

    def cancel(self, oid):
        self.calls.append(("cancel", oid))
        o = self.orders[oid]
        if oid in self.refuse_cancel or o["status"] not in (
                "placed", "partially_filled"):
            raise BrokerError(f"Saxo DELETE: HTTP 409 cannot cancel {oid}")
        o["status"] = "cancelled"
        if o["oco"] and self.orders[o["oco"]]["status"] == "placed":
            self.orders[o["oco"]]["status"] = "cancelled"   # OCO cascade

    def fill(self, oid, qty, price):
        o = self.orders[oid]
        qty = Decimal(str(qty))
        o["filled"] += qty
        o["fills"].append({"fill_id": f"f{next(self._fill)}",
                           "qty": str(qty), "price": str(price)})
        o["status"] = "filled" if o["filled"] >= o["qty"] else \
            "partially_filled"
        if o["oco"]:
            sib = self.orders[o["oco"]]
            if sib["status"] == "placed":
                sib["status"] = "cancelled"

    def order_status(self, oid):
        o = self.orders.get(oid)
        if o is None:
            raise BrokerError(f"unknown {oid}")
        return {"status": o["status"], "fills": list(o["fills"]),
                "order_type": o["type"], "price": o["price"],
                "amount": float(o["qty"])}

    def working_orders(self):
        return [{"order_id": k, "type": o["type"], "price": o["price"],
                 "amount": float(o["qty"]), "side": o["side"],
                 "symbol": f"{o['symbol']}:xnas", "desk_symbol": o["symbol"]}
                for k, o in self.orders.items()
                if o["status"] in ("placed", "partially_filled")]

    def legs_of(self, status="placed"):
        return {k: o for k, o in self.orders.items()
                if o.get("leg") and o["status"] == status}


class Market:
    def __init__(self, price="103"):
        self.price = Decimal(price)

    def quote(self, symbol):
        return {"symbol": symbol, "price": self.price, "currency": "USD"}

    def fx(self, a, b):
        return Decimal(1)


class World:
    def __init__(self, *, live=False, limits=None):
        self.t = {"now": T0}
        self.clock = lambda: self.t["now"]
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.repo = Repo(self.conn)
        self.acct, self.broker = ("saxo-live", "saxo_live") if live else \
            ("saxo-paper", "saxo")
        self.repo.create_account(
            self.acct, self.broker, "live" if live else "paper", "USD",
            fee_model={"type": "flat", "per_trade": "0"},
            risk_limits=limits or {}, ts=T0, allow_live=live,
            external_ref="KEY==" if live else None)
        self.repo.add_instrument("NASDAQ:CSCO", "CSCO", "NASDAQ", "USD")
        self.repo.deposit(self.acct, to_micro("50000"), ts=T0)
        self.venue = LegVenue()
        self.market = Market()
        self.execute = build_execute(
            self.repo, {self.broker: self.venue}, self.market,
            account_for=lambda b: self.acct, clock=self.clock)
        self.alerts = []
        self.custodian = Custodian(
            self.conn, self.repo, {self.broker: self.venue},
            resume_post=lambda b: (200, {}),
            account_broker=lambda a: self.broker,
            fx_rate_for=lambda i, a: to_micro("1"), clock=self.clock,
            alert=lambda text, **kw: self.alerts.append(text))

    def item(self, wi, kind="pipeline"):
        self.conn.execute(
            "INSERT OR IGNORE INTO work_items (id, kind, ticker, state,"
            " thread_id, created_at, updated_at) VALUES"
            " (?, ?, 'CSCO', 'executing', ?, ?, ?)",
            (wi, kind, wi, T0, T0))

    def state(self, wi, direction, *, size=None, qty=None, kind="pipeline",
              entry=103.0, stop=None, target=None):
        self.item(wi, kind)
        th = Thesis(ticker="CSCO", direction=direction, conviction=0.5,
                    entry_low=entry, entry_high=entry, stop_loss=stop,
                    take_profit=target, currency="USD")
        return PipelineState(
            work_item_id=wi, kind=kind, ticker="CSCO", instrument=CSCO,
            thesis=th, approval=Approval(
                status="approved", size_base=size, qty=qty,
                broker=self.broker, actor="human", token="t",
                at=datetime.now(timezone.utc)))

    def buy_with_legs(self, wi="wi_buy", size=1000.0, fill=True):
        """Open 9 CSCO @ 103 with a stop at 97 and a target at 115."""
        out = self.execute(self.state(wi, "buy", size=size, stop=97.0,
                                      target=115.0))
        oid = out["order_ids"][0]
        if fill:
            o = self.repo.order(oid)
            self.venue.fill(o["broker_order_id"], o["qty"] / 1e6, "103")
            self.custodian.run_once()
        return oid

    def legs(self, status=None):
        q = "SELECT * FROM orders WHERE leg IS NOT NULL"
        if status:
            q += f" AND status='{status}'"
        return self.conn.execute(q + " ORDER BY leg, created_at").fetchall()

    def held(self):
        return self.repo.position(self.acct, "NASDAQ:CSCO")[0] / 1e6


# ── 1. legs become orders rows ───────────────────────────────────────────
def test_bracket_legs_are_persisted_as_their_own_orders():
    w = World()
    entry = w.buy_with_legs()
    legs = w.legs()
    assert [(l["leg"], l["side"], l["qty"], l["status"]) for l in legs] == [
        ("stop", "sell", 9_000_000, "placed"),
        ("target", "sell", 9_000_000, "placed")]
    stop, target = legs
    assert stop["stop_loss"] == to_micro("97") and stop["limit_price"] is None
    assert target["limit_price"] == to_micro("115") and not target["stop_loss"]
    for l in legs:
        assert l["parent_order_id"] == entry
        assert l["work_item_id"] == "wi_buy"
        assert l["expires_at"] == protection.LEG_EXPIRES_AT       # GTC
        assert l["reserved_base"] is None and l["submitted_at"] == T0
    assert w.held() == 9


def test_legs_skip_the_risk_caps_and_the_daily_trade_count():
    w = World(limits={"max_trades_per_day": 2, "max_order_base": 2000})
    w.buy_with_legs()                         # 1 trade (+2 legs)
    # legs do not count: a second trade still fits under a cap of 2
    w.buy_with_legs("wi_buy2", size=500.0)
    with pytest.raises(RiskLimitExceeded, match="max_trades_per_day"):
        w.buy_with_legs("wi_buy3", size=500.0)
    # and a leg is recorded even when its size is over max_order_base
    protection.persist_legs(
        w.repo, parent=w.repo.order(w.conn.execute(
            "SELECT id FROM orders WHERE leg IS NULL LIMIT 1").fetchone()[0]),
        legs=[{"leg": "stop", "order_id": "big", "price": 97}], side="sell",
        qty=to_micro("1000"), ts=T0)
    assert w.repo.order(w.conn.execute(
        "SELECT id FROM orders WHERE broker_order_id='big'").fetchone()[0])


def test_custodian_never_places_or_expires_a_leg():
    w = World()
    w.buy_with_legs()
    w.conn.execute("UPDATE orders SET expires_at=? WHERE leg IS NOT NULL",
                   (T0 - 1,))              # even with a TTL in the past
    w.t["now"] = T0 + 10 * 86400
    rep = w.custodian.run_once()
    assert rep["order_expired"] == [] and rep["placed"] == []
    assert [l["status"] for l in w.legs()] == ["placed", "placed"]
    assert not [c for c in w.venue.calls if c[0] == "cancel"]


def test_execute_retry_never_mistakes_a_leg_for_its_order():
    w = World()
    entry = w.buy_with_legs()
    st = w.state("wi_buy", "buy", size=1000.0, stop=97.0, target=115.0)
    assert w.execute(st)["order_ids"] == [entry]


# ── 2. leg fills are booked; OCO siblings cancel ─────────────────────────
def test_stop_fill_books_a_sell_and_the_target_is_cancelled():
    w = World()
    w.buy_with_legs()
    stop = w.legs()[0]
    w.venue.fill(stop["broker_order_id"], 9, "96.90")
    rep = w.custodian.run_once()
    assert rep["fills"] == 1 and w.held() == 0
    by = {l["leg"]: l["status"] for l in w.legs()}
    assert by == {"stop": "filled", "target": "cancelled"}
    ex = w.conn.execute("SELECT * FROM executions WHERE order_id=?",
                        (stop["id"],)).fetchone()
    assert ex["side"] == "sell" and ex["intended_price"] == to_micro("97")
    (pl,) = w.conn.execute("SELECT SUM(realized_pl_base) FROM lot_closures"
                           ).fetchone()
    assert pl == to_micro("-54.9")            # 9 × (96.90 − 103), exact
    w.repo.assert_invariants(w.acct)
    assert not any("UNPROTECTED" in f for f in rep["flags"])


def test_partial_leg_fill_then_cancel_keeps_what_filled():
    w = World()
    w.buy_with_legs()
    target = w.legs()[1]
    w.venue.fill(target["broker_order_id"], 4, "115.10")
    w.custodian.run_once()
    assert w.repo.order(target["id"])["status"] == "partially_filled"
    assert w.held() == 5
    w.venue.orders[target["broker_order_id"]]["status"] = "cancelled"
    rep = w.custodian.run_once()
    assert w.repo.order(target["id"])["status"] == "cancelled"
    assert w.held() == 5
    # a partly filled target cancelled the stop: 5 shares are bare now
    assert any("UNPROTECTED position NASDAQ:CSCO on saxo-paper: 5 of 5"
               in f for f in rep["flags"])


def test_an_unbooked_leg_fill_is_not_locked_out_by_the_sync():
    w = World()
    w.buy_with_legs()
    target = w.legs()[1]
    w.venue.fill(target["broker_order_id"], 4, "115.10")
    w.venue.orders[target["broker_order_id"]]["status"] = "cancelled"
    rate = {"ok": False}

    def fx(i, a):
        if not rate["ok"]:
            raise RuntimeError("fx down")
        return to_micro("1")
    w.custodian.fx_rate_for = fx
    rep = w.custodian.run_once()
    assert any("FILL NOT BOOKED" in f for f in rep["flags"])
    assert w.repo.order(target["id"])["status"] == "placed"   # not yet synced
    rate["ok"] = True
    w.custodian.run_once()
    assert w.held() == 5
    assert w.repo.order(target["id"])["status"] == "cancelled"


def test_venue_corrects_a_mislabelled_leg():
    """An unverified label (Saxo's response lists ids, not types) is put
    right from the venue on the next poll."""
    w = World()
    w.buy_with_legs()
    stop, target = w.legs()
    w.conn.execute("UPDATE orders SET leg='target', limit_price=stop_loss,"
                   " stop_loss=NULL WHERE id=?", (stop["id"],))
    w.custodian.run_once()
    fixed = w.repo.order(stop["id"])
    assert fixed["leg"] == "stop" and fixed["stop_loss"] == to_micro("97")
    assert fixed["limit_price"] is None


# ── 3. cancel before any desk sell ───────────────────────────────────────
def test_exit_cancels_the_legs_first_then_sells_without_a_bracket():
    w = World()
    w.buy_with_legs()
    w.venue.calls.clear()
    out = w.execute(w.state("wi_sell", "sell", qty=9, kind="manual",
                            stop=None))
    calls = [c[0] for c in w.venue.calls]
    assert calls[:2] == ["cancel", "cancel"] and calls[-1] == "place"
    place = w.venue.calls[-1]
    assert place[1] == "sell" and place[2] == 9
    assert place[3] is None and place[4] is None     # an exit has no bracket
    sell = w.repo.order(out["order_ids"][0])
    assert sell["status"] == "placed" and sell["protect_state"] == "pending"
    for l in w.legs():
        assert l["status"] == "cancelled" and l["cancelled_for"] == sell["id"]


def test_exit_strips_thesis_levels_from_a_closing_order():
    """A sell review's thesis may carry short-style levels; on an exit they
    would rest at Saxo as BUY legs and re-open a position."""
    w = World()
    w.buy_with_legs()
    out = w.execute(w.state("wi_sr", "sell", qty=9, kind="sell_review",
                            entry=103.0, stop=110.0, target=90.0))
    sell = w.repo.order(out["order_ids"][0])
    assert sell["stop_loss"] is None and sell["take_profit"] is None
    assert not [l for l in w.legs() if l["side"] == "buy"]


def test_a_refused_cancel_refuses_the_sell():
    w = World()
    w.buy_with_legs()
    for l in w.legs():
        w.venue.refuse_cancel.add(l["broker_order_id"])
    w.venue.calls.clear()
    with pytest.raises(RuntimeError, match="could not cancel the protective"):
        w.execute(w.state("wi_sell", "sell", qty=9, kind="manual"))
    assert not [c for c in w.venue.calls if c[0] == "place"]
    row = w.conn.execute("SELECT * FROM orders WHERE work_item_id='wi_sell'"
                         ).fetchone()
    assert row["status"] == "rejected"
    assert [l["status"] for l in w.legs()] == ["placed", "placed"]  # intact


def test_an_already_cancelled_sibling_is_not_a_refusal():
    """Cancelling the stop takes the OCO target with it; the target's own
    DELETE then fails — the venue confirms it is gone, so the sell goes."""
    w = World()
    w.buy_with_legs()
    out = w.execute(w.state("wi_sell", "sell", qty=9, kind="manual"))
    assert w.repo.order(out["order_ids"][0])["status"] == "placed"
    assert [c[0] for c in w.venue.calls].count("cancel") == 2


def test_a_leg_that_filled_meanwhile_refuses_the_sell():
    w = World()
    w.buy_with_legs()
    stop = w.legs()[0]
    w.venue.fill(stop["broker_order_id"], 9, "96.9")   # not yet polled
    with pytest.raises(RuntimeError, match="filled at the venue"):
        w.execute(w.state("wi_sell", "sell", qty=9, kind="manual"))


def test_a_sell_refused_while_saxo_settles_the_cancel_is_retried_once(
        monkeypatch):
    from app.graph import nodes_execution
    naps = []
    monkeypatch.setattr(nodes_execution, "_settle_sleep", naps.append)
    w = World()
    w.buy_with_legs()
    real = w.venue.place_bracket
    tries = {"n": 0}

    def slow(**kw):                     # the cancel lands a moment late
        tries["n"] += 1
        if tries["n"] == 1:
            w.venue.calls.append(("place", kw["side"], kw["qty"], None, None))
            return PlacedOrder("", "rejected",
                               "SellOrdersAlreadyExistForOwnedContracts")
        return real(**kw)
    w.venue.place_bracket = slow
    out = w.execute(w.state("wi_sell", "sell", qty=9, kind="manual"))
    assert naps == [nodes_execution.LEG_SETTLE_S] and tries["n"] == 2
    assert w.repo.order(out["order_ids"][0])["status"] == "placed"
    assert not [c for c in w.venue.calls if c[0] == "oco"]


def test_without_cancelling_saxo_would_refuse_the_sell():
    """Why the cancel exists: the venue refuses a sell while legs rest."""
    w = World()
    w.buy_with_legs()
    placed = w.venue.place_bracket(symbol="CSCO", side="sell", qty=9,
                                   limit=103)
    assert "SellOrdersAlreadyExist" in placed.detail


# ── re-protection after a partial sell ───────────────────────────────────
def test_partial_sell_re_protects_the_rest_at_the_original_levels():
    w = World()
    entry = w.buy_with_legs()
    out = w.execute(w.state("wi_sell", "sell", qty=5, kind="manual"))
    sell = w.repo.order(out["order_ids"][0])
    w.venue.fill(sell["broker_order_id"], 5, "104")
    rep = w.custodian.run_once()
    assert w.held() == 4
    oco = [c for c in w.venue.calls if c[0] == "oco"]
    assert oco == [("oco", "sell", Decimal(4), 97.0, 115.0)]
    assert w.repo.order(sell["id"])["protect_state"] == "done"
    new = w.legs("placed")
    assert [(l["leg"], l["qty"], l["parent_order_id"]) for l in new] == [
        ("stop", 4_000_000, entry), ("target", 4_000_000, entry)]
    assert rep["reprotected"][0]["state"] == "done"
    assert not any("UNPROTECTED" in f for f in rep["flags"])
    assert w.alerts == []


def test_full_exit_needs_no_re_protection():
    w = World()
    w.buy_with_legs()
    out = w.execute(w.state("wi_sell", "sell", qty=9, kind="manual"))
    sell = w.repo.order(out["order_ids"][0])
    w.venue.fill(sell["broker_order_id"], 9, "104")
    w.custodian.run_once()
    assert w.held() == 0
    assert w.repo.order(sell["id"])["protect_state"] == "not_needed"
    assert not [c for c in w.venue.calls if c[0] == "oco"]


def test_unfilled_sell_that_expires_re_protects_everything():
    w = World()
    w.buy_with_legs()
    out = w.execute(w.state("wi_sell", "sell", qty=9, kind="manual"))
    w.t["now"] = w.repo.order(out["order_ids"][0])["expires_at"] + 1
    w.custodian.run_once()
    assert [c for c in w.venue.calls if c[0] == "oco"] == [
        ("oco", "sell", Decimal(9), 97.0, 115.0)]
    assert len(w.legs("placed")) == 2


def test_a_sell_still_working_is_not_re_protected_yet():
    w = World()
    w.buy_with_legs()
    out = w.execute(w.state("wi_sell", "sell", qty=9, kind="manual"))
    sell = w.repo.order(out["order_ids"][0])
    w.venue.fill(sell["broker_order_id"], 3, "104")
    w.custodian.run_once()
    assert w.repo.order(sell["id"])["status"] == "partially_filled"
    assert not [c for c in w.venue.calls if c[0] == "oco"]


def test_venue_refusing_the_sell_after_the_cancel_restores_protection():
    w = World()
    w.buy_with_legs()
    w.venue.refuse_place = True
    with pytest.raises(RuntimeError, match="TooFarFromMarket"):
        w.execute(w.state("wi_sell", "sell", qty=9, kind="manual"))
    assert [c for c in w.venue.calls if c[0] == "oco"] == [
        ("oco", "sell", Decimal(9), 97.0, 115.0)]
    assert len(w.legs("placed")) == 2


def test_failed_re_protection_is_flagged_and_alerted_once():
    w = World()
    w.buy_with_legs()
    w.venue.refuse_oco = True
    out = w.execute(w.state("wi_sell", "sell", qty=5, kind="manual"))
    sell = w.repo.order(out["order_ids"][0])
    w.venue.fill(sell["broker_order_id"], 5, "104")
    rep = w.custodian.run_once()
    assert w.repo.order(sell["id"])["protect_state"] == "failed"
    assert any("RE-PROTECTION FAILED" in f and "venue said no" in f
               for f in rep["flags"])
    assert any("UNPROTECTED position NASDAQ:CSCO on saxo-paper: 4 of 4"
               in f for f in rep["flags"])
    assert len(w.alerts) == 1 and "UNPROTECTED" in w.alerts[0]
    rep2 = w.custodian.run_once()
    assert len(w.alerts) == 1                         # once, not every run
    assert any("RE-PROTECTION FAILED" in f for f in rep2["flags"])


def test_live_account_is_never_re_protected_automatically(monkeypatch):
    monkeypatch.setattr("os.environ", {"CLAIRE_LIVE_TRADING": "enabled"})
    w = World(live=True)
    live_trading.arm(w.conn, {"CLAIRE_LIVE_TRADING": "enabled"}, actor="t",
                     phrase=live_trading.ARM_PHRASE, clock=lambda: T0)
    w.buy_with_legs(size=600.0)               # 5 shares, inside live limits
    assert w.held() == 5 and len(w.legs("placed")) == 2
    out = w.execute(w.state("wi_sell", "sell", qty=2, kind="manual"))
    sell = w.repo.order(out["order_ids"][0])
    w.venue.fill(sell["broker_order_id"], 2, "104")
    rep = w.custodian.run_once()
    assert not [c for c in w.venue.calls if c[0] == "oco"]   # never by itself
    assert w.repo.order(sell["id"])["protect_state"] == "failed"
    assert "LIVE account" in w.repo.order(sell["id"])["protect_note"]
    assert len(w.alerts) == 1
    # a doubt about a live position disarms live trading (§22.3)
    assert not live_trading.status(w.conn, {"CLAIRE_LIVE_TRADING":
                                            "enabled"})["armed"]
    assert any("DISARMED" in f for f in rep["flags"])


def test_re_protection_refuses_levels_the_wrong_way_round():
    w = World()
    w.buy_with_legs()
    out = w.execute(w.state("wi_sell", "sell", qty=5, kind="manual"))
    w.conn.execute("UPDATE orders SET stop_loss=? WHERE leg='stop'",
                   (to_micro("120"),))
    sell = w.repo.order(out["order_ids"][0])
    w.venue.fill(sell["broker_order_id"], 5, "104")
    w.custodian.run_once()
    assert w.repo.order(sell["id"])["protect_state"] == "failed"
    assert "wrong way round" in w.repo.order(sell["id"])["protect_note"]
    assert not [c for c in w.venue.calls if c[0] == "oco"]


# ── unprotected & drift understand legs ──────────────────────────────────
def test_leg_drift_flags_untracked_and_vanished_orders():
    w = World()
    w.buy_with_legs()
    rep = w.custodian.run_once()
    assert not any("DRIFT" in f or "UNTRACKED" in f for f in rep["flags"])
    # an order at the venue that would sell the desk's shares, not on the book
    w.venue._new(symbol="CSCO", side="sell", qty=Decimal(9),
                 type="StopIfTraded", price=90.0, leg=None)
    # and a tracked leg the venue no longer lists
    gone = w.legs()[1]["broker_order_id"]
    w.venue.orders[gone]["status"] = "weird"
    rep = w.custodian.run_once()
    assert any(f.startswith("UNTRACKED venue order") for f in rep["flags"])
    assert any(f.startswith("LEG DRIFT saxo-paper: target") for f in
               rep["flags"])


def test_sim_positions_keep_the_old_unprotected_rule():
    """No legs tracked (the in-process sim keeps the stop inside the entry):
    the opening order's stop still decides."""
    from tests.test_execution import World as SimWorld
    w = SimWorld(fill_mode="instant")
    st = w.state(direction="buy")
    w.execute(st)
    w.custodian.run_once()
    assert not any("UNPROTECTED" in f for f in
                   w.custodian.run_once()["flags"])
    w.conn.execute("UPDATE orders SET stop_loss=NULL")
    assert any("UNPROTECTED" in f for f in w.custodian.run_once()["flags"])


# ── 4. backfill ──────────────────────────────────────────────────────────
class Backfillable(LegVenue):
    def related_order_ids(self, oid):
        return [k for k, o in self.orders.items()
                if o.get("leg") and o.get("_entry") == oid]


def _old_world():
    """A position opened before legs were tracked: the venue holds the legs,
    the book does not."""
    w = World()
    w.venue = Backfillable()
    w.execute = build_execute(w.repo, {"saxo": w.venue}, w.market,
                              account_for=lambda b: w.acct, clock=w.clock)
    w.custodian.brokers = {"saxo": w.venue}
    entry = w.buy_with_legs()
    boid = w.repo.order(entry)["broker_order_id"]
    for l in w.legs():
        w.venue.orders[l["broker_order_id"]]["_entry"] = boid
    w.conn.execute("DELETE FROM orders WHERE leg IS NOT NULL")
    return w, entry


def test_backfill_adopts_exactly_matching_legs_and_is_idempotent():
    w, entry = _old_world()
    dry = protection.backfill(w.repo, w.venue, w.acct, ts=T0, dry_run=True)
    assert [r["action"] for r in dry] == ["would insert"] * 2
    assert w.legs() == []
    rep = protection.backfill(w.repo, w.venue, w.acct, ts=T0)
    assert sorted((r["leg"], r["price"], r["qty"]) for r in rep) == [
        ("stop", 97.0, 9.0), ("target", 115.0, 9.0)]
    assert [l["parent_order_id"] for l in w.legs()] == [entry, entry]
    again = protection.backfill(w.repo, w.venue, w.acct, ts=T0)
    assert again == [{"order": entry, "venue_entry": w.repo.order(entry)[
        "broker_order_id"], "instrument": "NASDAQ:CSCO", "action": "skip",
        "why": "legs already on the book"}]
    assert len(w.legs()) == 2


def test_backfill_never_guesses():
    w, entry = _old_world()
    stop_id = next(k for k, o in w.venue.orders.items() if o.get("leg") ==
                   "stop")
    w.venue.orders[stop_id]["price"] = 90.0          # not the desk's 97
    rep = protection.backfill(w.repo, w.venue, w.acct, ts=T0)
    skipped = [r for r in rep if r["action"] == "skip"]
    assert len(skipped) == 1 and "stop price 90.0" in skipped[0]["why"]
    assert [l["leg"] for l in w.legs()] == ["target"]


# ── 5. manual trades ─────────────────────────────────────────────────────
def _desk(conn, repo, execute=None, prepare=None):
    from langgraph.checkpoint.sqlite import SqliteSaver

    from app.api.desk import Desk
    from app.graph.pipeline import ANALYSTS, Deps
    deps = Deps(analysts={a: (lambda s: None) for a in ANALYSTS},
                bull=lambda s: None, bear=lambda s: None,
                arbiter=lambda s: None,
                execute=execute or (lambda s: {"order_ids": ["x"]}),
                record=lambda s: {},
                prepare=prepare or (lambda s: {}))
    return Desk(conn, deps, SqliteSaver(sqlite3.connect(
        ":memory:", check_same_thread=False)), repo, clock=lambda: T0)


def test_manual_exit_reaches_the_gate_with_its_prefill():
    w = World()
    w.buy_with_legs()
    desk = _desk(w.conn, w.repo)
    wi = desk.manual_trade("CSCO", "NASDAQ", "sell", 104.0, note="take it",
                           suggested={"qty": 9, "broker": "saxo"},
                           source="portfolio", background=False)
    row = desk.work_item(wi)
    assert (row["kind"], row["state"]) == ("manual", "awaiting_approval")
    assert row["approval_token"] and row["token_state"] == "minted"
    assert json.loads(row["suggested_json"]) == {"qty": 9.0, "broker": "saxo"}
    t = json.loads(row["thesis_json"])
    assert t["direction"] == "sell" and t["stop_loss"] is None
    assert t["entry_high"] == 104.0 and "MANUAL EXIT" in t["conditions"][0]
    assert row["trigger"] == "manual sell from portfolio: take it"
    ev = w.conn.execute("SELECT actor, payload FROM events WHERE item_id=?"
                        " AND to_state='manual'", (wi,)).fetchone()
    assert ev["actor"] == "human"
    assert json.loads(ev["payload"])["suggested"]["qty"] == 9.0


def test_manual_trade_rules_are_the_override_rules():
    from pydantic import ValidationError

    from app.api.desk import Conflict
    w = World()
    desk = _desk(w.conn, w.repo)
    with pytest.raises(ValueError, match="a buy needs a stop"):
        desk.manual_trade("CSCO", "NASDAQ", "buy", 104.0, background=False)
    with pytest.raises(ValueError, match="SHORT"):           # nothing held
        desk.manual_trade("CSCO", "NASDAQ", "sell", 104.0, background=False)
    with pytest.raises(ValidationError):                       # stop above
        desk.manual_trade("CSCO", "NASDAQ", "buy", 104.0, stop=110.0,
                          background=False)
    with pytest.raises(ValueError, match="unknown suggested"):
        desk.manual_trade("CSCO", "NASDAQ", "buy", 104.0, stop=99.0,
                          suggested={"qtty": 1}, background=False)
    with pytest.raises(ValueError, match="invalid ticker"):
        desk.manual_trade("<b>", "NASDAQ", "buy", 104.0, stop=99.0,
                          background=False)
    short = desk.manual_trade("CSCO", "NASDAQ", "sell", 104.0, stop=110.0,
                              background=False)
    assert "MANUAL SHORT" in json.loads(desk.work_item(short)[
        "thesis_json"])["conditions"][0]
    with pytest.raises(Conflict, match="already awaiting_approval"):
        desk.manual_trade("CSCO", "NASDAQ", "sell", 105.0, stop=111.0,
                          background=False)
    buy = desk.manual_trade("CSCO", "NASDAQ", "buy", 104.0, stop=99.0,
                            target=120.0, suggested={"qty": 3},
                            source="trigger", background=False)
    assert desk.work_item(buy)["state"] == "awaiting_approval"


def test_manual_exit_is_capped_at_what_the_desk_holds():
    w = World()
    w.buy_with_legs()
    out = w.execute(w.state("wi_m", "sell", qty=50, kind="manual"))
    assert w.repo.order(out["order_ids"][0])["qty"] == 9_000_000


def test_manual_exit_end_to_end_through_the_real_gate():
    """Sell button → /internal/manual → card → token-checked approval →
    execute (legs cancelled, sell placed) → custodian books it and
    re-protects the rest."""
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.approvals import thesis_action
    from app.wiring import make_prepare
    w = World()
    w.buy_with_legs()
    desk = _desk(w.conn, w.repo, execute=w.execute,
                 prepare=make_prepare(w.repo))
    client = TestClient(create_app(desk, w.conn, "sec", clock=lambda: T0))
    hdr = {"x-claire-secret": "sec"}
    body = {"ticker": "CSCO", "exchange": "NASDAQ", "direction": "sell",
            "entry": 103.0, "suggested": {"qty": 6, "broker": "saxo"},
            "source": "portfolio", "actor": "human"}
    assert client.post("/internal/manual", json=body).status_code == 403
    assert client.post("/internal/manual", json={**body, "actor": "bob"},
                       headers=hdr).status_code == 403
    assert client.post("/internal/manual", json={**body, "suggested": {
        "broker": "nyse"}}, headers=hdr).status_code == 400
    r = client.post("/internal/manual", json=body, headers=hdr)
    assert r.status_code == 200, r.text
    wi = r.json()["work_item_id"]
    import time as _t
    for _ in range(100):                     # the gate runs in a thread
        if desk.work_item(wi)["state"] == "awaiting_approval":
            break
        _t.sleep(0.02)
    assert client.post("/internal/manual", json=body,
                       headers=hdr).status_code == 409        # double click
    row = desk.work_item(wi)

    def resume_post(b):
        rr = client.post("/internal/resume", json=b, headers=hdr)
        return rr.status_code, rr.json()
    out = thesis_action(w.conn, {"work_item_id": wi, "action": "approve",
                                 "qty": 6, "broker": "saxo",
                                 "token": row["approval_token"]},
                        resume_post, clock=lambda: T0)
    assert out["ok"] and desk.work_item(wi)["state"] == "executing"
    sell = w.conn.execute("SELECT * FROM orders WHERE work_item_id=? AND"
                          " leg IS NULL", (wi,)).fetchone()
    assert sell["side"] == "sell" and sell["qty"] == 6_000_000
    assert all(l["status"] == "cancelled" for l in w.legs())
    w.venue.fill(sell["broker_order_id"], 6, "103")
    rep = w.custodian.run_once()
    assert w.held() == 3 and desk.work_item(wi)["state"] == "done"
    assert [(l["leg"], l["qty"]) for l in w.legs("placed")] == [
        ("stop", 3_000_000), ("target", 3_000_000)]
    assert not any("UNPROTECTED" in f for f in rep["flags"])


# ── the Saxo adapter (MockTransport, Saxo's real response shapes) ────────
def saxo(handler, **kw):
    return SaxoBroker("sim", token=lambda: "T", account_key="KEY==",
                      client=httpx.Client(transport=httpx.MockTransport(
                          handler)), order_gap_s=0, **kw)


def audit(oid, order_type, price, amount=3, related=()):
    return httpx.Response(200, json={"Data": [
        {"LogId": "1", "ActivityTime": "2026-11-04T15:00:00Z",
         "Status": "Placed", "SubStatus": "Confirmed", "OrderId": oid,
         "OrderType": order_type, "Price": price, "Amount": amount,
         "BuySell": "Sell", "RelatedOrders": list(related)}]})


def test_bracket_legs_are_labelled_from_the_audit_trail():
    """SIM 2026-11-03: sent [stop, target], the TARGET got the lower id —
    the response order says nothing about which leg is which."""
    def h(req):
        if req.method == "POST":
            return httpx.Response(200, json={"OrderId": "5001", "Orders": [
                {"OrderId": "5002"}, {"OrderId": "5003"}]})
        oid = req.url.params["OrderId"]
        return {"5002": audit("5002", "Limit", 230.01),
                "5003": audit("5003", "StopIfTraded", 180.0)}[oid]
    p = saxo(ref_handler(h)).place_bracket(
        symbol="AAPL", side="buy", qty=3, limit=201, stop_loss=180,
        take_profit=230)
    assert p.status == "accepted"
    assert sorted((l["leg"], l["order_id"], l["price"], l["verified"])
                  for l in p.legs) == [("stop", "5003", 180.0, True),
                                       ("target", "5002", 230.01, True)]


def test_unreadable_audit_falls_back_to_request_order_unverified():
    def h(req):
        if req.method == "POST":
            return httpx.Response(200, json={"OrderId": "5001", "Orders": [
                {"OrderId": "5002"}, {"OrderId": "5003"}]})
        return httpx.Response(503)
    p = saxo(ref_handler(h)).place_bracket(
        symbol="AAPL", side="buy", qty=3, limit=201, stop_loss=180,
        take_profit=230)
    assert [(l["leg"], l["verified"]) for l in p.legs] == [
        ("stop", False), ("target", False)]


def test_place_oco_sends_a_standalone_gtc_pair():
    seen = {}

    def h(req):
        if req.method == "POST":
            seen["body"] = json.loads(req.content)
            seen["rid"] = req.headers.get("x-request-id")
            return httpx.Response(200, json={"Orders": [
                {"OrderId": "7001"}, {"OrderId": "7002"}]})
        oid = req.url.params["OrderId"]
        return {"7001": audit(oid, "StopIfTraded", 96.97, 4),
                "7002": audit(oid, "Limit", 115.5, 4)}[oid]
    p = saxo(ref_handler(h)).place_oco(symbol="AAPL", side="sell", qty=4,
                                       stop_loss=96.971, take_profit=115.499,
                                       client_order_id="ord_x-rp")
    assert p.status == "accepted" and p.broker_order_id == "7001"
    body = seen["body"]
    assert seen["rid"] == "ord_x-rp" and "Uic" not in body   # no parent
    stop, target = body["Orders"]
    assert (stop["OrderType"], stop["OrderPrice"], stop["BuySell"],
            stop["Amount"]) == ("StopIfTraded", 96.97, "Sell", 4)
    assert (target["OrderType"], target["OrderPrice"]) == ("Limit", 115.5)
    assert all(o["OrderDuration"] == {"DurationType": "GoodTillCancel"}
               for o in body["Orders"])
    assert {l["leg"] for l in p.legs} == {"stop", "target"}


def test_place_oco_with_one_level_is_a_plain_order_and_refuses_fractions():
    seen = {}

    def h(req):
        if req.method == "POST":
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"OrderId": "7001"})
        return audit("7001", "StopIfTraded", 96.97, 4)
    b = saxo(ref_handler(h))
    p = b.place_oco(symbol="AAPL", side="sell", qty=4, stop_loss=96.97)
    assert p.status == "accepted" and seen["body"]["OrderType"] == \
        "StopIfTraded" and "Orders" not in seen["body"]
    assert b.place_oco(symbol="AAPL", side="sell", qty=4.5,
                       stop_loss=96).status == "rejected"


def test_order_endpoints_are_spaced_out():
    naps, now = [], {"t": 100.0}

    def h(req):
        return httpx.Response(200, json={})
    b = SaxoBroker("sim", token=lambda: "T", account_key="K",
                   client=httpx.Client(transport=httpx.MockTransport(h)),
                   order_gap_s=1.1, sleep=lambda s: naps.append(round(s, 2)),
                   monotonic=lambda: now["t"])
    b.cancel("1")
    b.cancel("2")                            # same instant: waits 1.1 s
    now["t"] += 5
    b.cancel("3")                            # long after: no wait
    b._req("GET", "/port/v1/orders/me")      # reads are never throttled
    assert naps == [1.1]


def test_cancel_refusal_inside_a_200_raises():
    def h(req):
        return httpx.Response(200, json={"Orders": [{"OrderId": "1",
            "ErrorInfo": {"ErrorCode": "OrderNotFound"}}]})
    with pytest.raises(BrokerError, match="OrderNotFound"):
        saxo(h).cancel("1")


def test_working_orders_flattens_related_legs():
    def h(req):
        return httpx.Response(200, json={"Data": [
            {"AccountKey": "KEY==", "OrderId": "5041478338", "Amount": 9.0,
             "BuySell": "Sell", "OpenOrderType": "StopIfTraded",
             "Price": 96.97, "OrderRelation": "Oco", "Status": "Working",
             "Uic": 226, "DisplayAndFormat": {"Symbol": "CSCO:xnas"},
             "RelatedOpenOrders": [{"Amount": 9.0, "OpenOrderType": "Limit",
                                    "OrderId": "5041478337",
                                    "OrderPrice": 115.5,
                                    "Status": "Working"}]},
            {"AccountKey": "KEY==", "OrderId": "5041478337", "Amount": 9.0,
             "BuySell": "Sell", "OpenOrderType": "Limit", "Price": 115.5,
             "DisplayAndFormat": {"Symbol": "CSCO:xnas"}},
            {"AccountKey": "OTHER", "OrderId": "9", "Amount": 1,
             "DisplayAndFormat": {"Symbol": "X:xnas"}}]})
    rows = saxo(h).working_orders()
    assert [(r["order_id"], r["type"], r["price"], r["desk_symbol"])
            for r in rows] == [("5041478338", "StopIfTraded", 96.97, "CSCO"),
                               ("5041478337", "Limit", 115.5, "CSCO")]


def test_order_status_reports_what_the_order_is():
    st = saxo(lambda r: audit("5", "StopIfTraded", 96.97, 9)).order_status(
        "5")
    assert (st["status"], st["order_type"], st["price"], st["amount"]) == \
        ("placed", "StopIfTraded", 96.97, 9)


# ── dashboards: the Sell button's routes ─────────────────────────────────
@pytest.fixture
def dash(monkeypatch):
    import threading

    from app import dashboards
    from tests.test_dashboards import DKEY, FakeMarket
    class M(FakeMarket):
        def metrics(self, syms):
            return {s: {"price": 104.0, "currency": "USD"} for s in syms}
    w = World()
    w.buy_with_legs()
    srv = dashboards.create_server(
        w.conn, w.repo, M(), secret="s", dash_key=DKEY,
        api_base="http://127.0.0.1:9", clock=lambda: T0, port=0,
        sse_interval=None, env={"SAXO_SIM_EXECUTION": "enabled"})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", w, DKEY
    srv.shutdown()


def test_manual_trade_route_needs_the_key_and_forwards_only_known_fields(
        dash, monkeypatch):
    from app import dashboards
    base, w, key = dash
    sent = []

    class R:
        status_code = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"ok": True, "work_item_id": "wi_m"}

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, headers))
        return R()
    monkeypatch.setattr(dashboards.httpx, "post", post)
    body = {"ticker": "CSCO", "exchange": "NASDAQ", "direction": "sell",
            "entry": 104, "suggested": {"qty": 9, "broker": "saxo"},
            "actor": "reaper", "token": "x"}
    with httpx.Client(timeout=5) as c:
        assert c.post(base + "/api/manual-trade", json=body).status_code == 403
        assert sent == []
        r = c.post(base + "/api/manual-trade", json=body,
                   headers={"X-Dash-Key": key})
    assert r.status_code == 200 and r.json()["work_item_id"] == "wi_m"
    url, fwd, hdr = sent[0]
    assert url.endswith("/internal/manual") and hdr["x-claire-secret"] == "s"
    assert fwd["actor"] == "human" and "token" not in fwd
    assert fwd["suggested"] == {"qty": 9, "broker": "saxo"}


def test_manual_trade_route_is_key_gated_in_source():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "dash"
           / "handler.py").read_text()
    i = src.index('"/api/manual-trade"')
    assert "_paired()" in src[i:i + 400]
    assert not "/api/manual-trade".startswith("/api/override")


def test_trade_check_for_a_sell_not_yet_started(dash, monkeypatch):
    from app import wiring
    base, w, key = dash
    seen = {}

    class Pre:
        fractional = False

        def precheck(self, **kw):
            seen.update(kw)
            return {"ok": False, "error": "SellOrdersAlreadyExistFor"
                    "OwnedContracts: A sell order already exists",
                    "amount": 9, "price": 104.0}
    monkeypatch.setattr(wiring, "build_brokers", lambda env, conn: {
        "saxo": Pre()})
    with httpx.Client(base_url=base, timeout=5,
                      headers={"X-Dash-Key": key}) as c:
        r = c.post("/api/trade-check", json={
            "ticker": "CSCO", "exchange": "NASDAQ", "direction": "sell",
            "qty": 9, "entry": 104, "broker": "saxo", "stop": 99})
        bad = c.post("/api/trade-check", json={
            "ticker": "<x>", "exchange": "NASDAQ", "direction": "sell"})
    out = r.json()
    # an exit is checked without a bracket, and Saxo's objection to the
    # resting legs is explained, not shown as a refusal
    assert seen["side"] == "sell" and seen["stop_loss"] is None
    assert out["ok"] is None and out["legs_block"] and "cancels" in out["note"]
    assert bad.status_code == 400


def test_cards_portfolio_and_saxo_page_carry_what_the_sell_ui_needs(
        dash, monkeypatch):
    from app.tools import saxo_live
    base, w, key = dash
    desk = _desk(w.conn, w.repo)
    wi = desk.manual_trade("CSCO", "NASDAQ", "sell", 104.0,
                           suggested={"qty": 9, "broker": "saxo"},
                           background=False)
    monkeypatch.setattr(saxo_live, "summary", lambda which, **kw: {
        "env": which, "positions": [
            {"symbol": "CSCO:xnas", "amount": 9, "exposure_base": 900},
            {"symbol": "AAPL:xnas", "amount": 3, "exposure_base": 900}],
        "orders": []})
    with httpx.Client(base_url=base, timeout=5,
                      headers={"X-Dash-Key": key}) as c:
        card = next(x for x in c.get("/api/approvals").json()["cards"]
                    if x["id"] == wi)
        port = c.get("/api/portfolio").json()
        sax = c.get("/api/saxo-account?env=sim").json()
        assert c.get("/sell.js").status_code == 200
    assert card["suggested"] == {"qty": 9.0, "broker": "saxo"}
    assert card["held"] == {"saxo-paper": 9.0} and card["kind"] == "manual"
    pos = port["exchanges"]["NASDAQ"][0]
    assert sorted((l["leg"], l["price"]) for l in pos["legs"]) == [
        ("stop", 97.0), ("target", 115.0)]
    by = {p["symbol"]: p["desk"] for p in sax["positions"]}
    assert by["CSCO:xnas"]["held"] == 9 and by["CSCO:xnas"]["broker"] == "saxo"
    assert by["AAPL:xnas"] is None            # not the desk's: no Sell


def test_portfolio_edit_floor_changes_a_held_positions_trailing_alert(dash):
    """Portfolio → Trailing alert → Edit: the same as Telegram /floor. Needs
    the key, only for a position the desk holds, 0.5–90%; the best price
    since buying is kept."""
    import httpx

    from app.watcher import position_floor
    base, w, key = dash
    inst, acct = w.conn.execute(
        "SELECT instrument_id, account_id FROM lots WHERE qty_remaining > 0"
    ).fetchone()
    w.conn.execute("INSERT INTO price_alerts (instrument_id, account_id, side,"
                   " rule, threshold, armed, peak_base) VALUES"
                   " (?,?,1,'trail_pct',8,1,110.0)", (inst, acct))
    body = {"instrument_id": inst, "account_id": acct, "side": "long",
            "pct": 5}
    with httpx.Client(timeout=5) as c:
        assert c.post(base + "/api/position-floor",
                      json=body).status_code == 403
        assert position_floor(w.conn, inst, acct)[0] == 8.0
        h = {"X-Dash-Key": key}
        for bad in (0.2, 95, "x"):
            assert c.post(base + "/api/position-floor", json={
                **body, "pct": bad}, headers=h).status_code == 422
        assert c.post(base + "/api/position-floor", json={
            **body, "account_id": "nope"}, headers=h).status_code == 404
        r = c.post(base + "/api/position-floor", json=body, headers=h)
    assert r.status_code == 200 and r.json()["before"] == 8.0
    assert position_floor(w.conn, inst, acct) == (5.0, 110.0)
    lv = httpx.get(base + "/api/portfolio", headers=h).json()
    held = [p for ps in lv["exchanges"].values() for p in ps]
    assert held[0]["levels"]["alert_pct"] == 5.0
