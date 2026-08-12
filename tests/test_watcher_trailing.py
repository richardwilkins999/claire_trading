"""The watcher after the trailing/shorts/escalation fixes.

Three things were wrong: a "trailing" floor that never trailed, shorts that
were not watched at all, and an escalation on an already-open review that
burned a fire and told nobody.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal

from app.accounting import db
from app.accounting.repo import Repo
from app.watcher import AUTO_DROP_PCT, ESCALATE_PCT, Watcher

T0 = 1_786_456_800          # a Tuesday, US session open
NOW = datetime.fromtimestamp(T0, tz=timezone.utc)


class FakeMarket:
    def __init__(self, price):
        self.price = price

    def quote(self, sym):
        return {"symbol": sym, "price": str(self.price), "currency": "USD"}

    def fx(self, a, b):
        return Decimal("1")


def _desk(price, *, qty_opened=10_000_000, cost=100_000_000):
    """One position at 10 shares, average cost 100. Negative qty_opened is a
    short, exactly as the lots table stores one."""
    conn = db.connect(":memory:")
    db.init(conn)
    repo = Repo(conn)
    repo.create_account("a", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
    conn.execute("INSERT INTO instruments (id,ticker,exchange,currency)"
                 " VALUES ('NASDAQ:X','X','NASDAQ','USD')")
    conn.execute(
        "INSERT INTO executions (id, account_id, instrument_id, side, qty,"
        " price_native, currency, fx_rate, gross_base, net_base, executed_at,"
        " recorded_at) VALUES ('ex1','a','NASDAQ:X',?,?,?, 'USD', 1000000,"
        " ?, ?, ?, ?)",
        ("buy" if qty_opened > 0 else "sell", abs(qty_opened), cost,
         cost, cost, T0, T0))
    conn.execute(
        "INSERT INTO lots (id, open_execution_id, account_id, instrument_id,"
        " qty_opened, qty_remaining, cost_per_share_base, commission_allocated,"
        " opened_at) VALUES ('lot1','ex1','a','NASDAQ:X',?,?,?,0,?)",
        (qty_opened, abs(qty_opened), cost, T0))
    launched = []

    def start_sell_review(inst, trigger=None):
        wi = f"wi_sr_{len(launched)}"
        conn.execute("INSERT INTO work_items (id,kind,ticker,state,thread_id,"
                     "trigger,created_at,updated_at) VALUES"
                     " (?,'sell_review','X','running',?,?,?,?)",
                     (wi, wi, trigger, T0, T0))
        launched.append(trigger)
        return wi
    w = Watcher(conn, repo, FakeMarket(price), start_sell_review,
                clock=lambda: T0, cal=None)
    return conn, w, launched


def _rule(conn):
    return conn.execute("SELECT * FROM price_alerts WHERE rule='trail_pct'"
                        ).fetchone()


# ── 1. the floor trails ─────────────────────────────────────────────────────
def test_floor_ratchets_up_with_the_price():
    conn, w, launched = _desk(Decimal("100"))
    w.tick()
    assert float(_rule(conn)["peak_base"]) == 100.0
    assert not launched                       # at entry, nothing fires

    w.market.price = Decimal("130")           # a 30% gain
    w.tick()
    assert float(_rule(conn)["peak_base"]) == 130.0   # peak followed it up
    assert not launched

    # 8% below the PEAK is 119.6 — under the old rule the floor sat at 92 and
    # this whole gain could have been given back in silence
    w.market.price = Decimal("119")
    w.tick()
    assert launched, "a fall from the peak must fire even while in profit"
    assert "119.00" in launched[0] and "130.00" in launched[0]


def test_the_peak_never_ratchets_down():
    conn, w, _ = _desk(Decimal("100"))
    w.tick()
    w.market.price = Decimal("130")
    w.tick()
    w.market.price = Decimal("125")           # a dip is not a new peak
    w.tick()
    assert float(_rule(conn)["peak_base"]) == 130.0


def test_a_position_still_under_water_uses_entry_as_the_reference():
    """It has never traded above entry, so entry IS the best price seen."""
    conn, w, launched = _desk(Decimal("100"))
    w.tick()
    w.market.price = Decimal("100") * (1 - Decimal(AUTO_DROP_PCT) / 100)
    w.tick()
    assert launched


# ── 2. shorts are watched, and watched the right way round ─────────────────
def test_a_short_fires_when_the_price_RISES():
    conn, w, launched = _desk(Decimal("100"), qty_opened=-10_000_000)
    w.tick()
    assert _rule(conn) is not None, "a short must get a rule at all"
    w.market.price = Decimal("90")            # a short in profit
    w.tick()
    assert float(_rule(conn)["peak_base"]) == 90.0    # best price = LOWEST
    assert not launched

    w.market.price = Decimal("98")            # 8.9% back up from 90
    w.tick()
    assert launched, "a short losing money must alert"


def test_a_short_does_not_fire_on_a_falling_price():
    conn, w, launched = _desk(Decimal("100"), qty_opened=-10_000_000)
    w.tick()
    w.market.price = Decimal("50")            # deeply profitable short
    w.tick()
    assert not launched


# ── 3. escalation reaches the human instead of the counter ─────────────────
def test_escalation_on_an_open_review_updates_it_rather_than_vanishing():
    conn, w, launched = _desk(Decimal("100"))
    w.tick()
    w.market.price = Decimal("92")                       # first breach: 8%
    w.tick()
    assert len(launched) == 1
    wi = conn.execute("SELECT id, trigger FROM work_items").fetchone()
    first_trigger = wi["trigger"]

    w.market.price = Decimal("88")                       # now 12% down
    w.tick()
    assert len(launched) == 1, "must not stack a second review"
    after = conn.execute("SELECT trigger FROM work_items WHERE id=?",
                         (wi["id"],)).fetchone()
    # the card in front of the human now quotes the CURRENT price, not the
    # stale first breach — this is the bug: the fire used to be swallowed
    assert after["trigger"] != first_trigger
    assert "88.00" in after["trigger"] and "escalation 2" in after["trigger"]
    ev = conn.execute(
        "SELECT actor, payload FROM events WHERE item_id=? AND"
        " to_state='watcher_escalation'", (wi["id"],)).fetchone()
    assert ev is not None and ev["actor"] == "watcher"
    assert json.loads(ev["payload"])["escalation"] == 2


def test_the_ladder_still_spaces_the_escalations():
    conn, w, launched = _desk(Decimal("100"))
    w.tick()
    w.market.price = Decimal("92")
    w.tick()
    r = _rule(conn)
    assert r["fire_count_today"] == 1
    # 10% down is past the first rung but short of the second (8+3=11%)
    w.market.price = Decimal("90")
    w.tick()
    assert _rule(conn)["fire_count_today"] == 1, "fired too eagerly"
    w.market.price = Decimal("100") * (1 - Decimal(AUTO_DROP_PCT
                                                   + ESCALATE_PCT) / 100)
    w.tick()
    assert _rule(conn)["fire_count_today"] == 2


def test_health_records_the_peak_it_fired_against():
    conn, w, _ = _desk(Decimal("100"))
    w.tick()
    w.market.price = Decimal("130")
    w.tick()
    w.market.price = Decimal("119")
    w.tick()
    h = conn.execute("SELECT detail FROM service_health WHERE service='watcher'"
                     " ORDER BY checked_at DESC, rowid DESC LIMIT 1").fetchone()
    fired = json.loads(h["detail"])["fired"]
    assert fired and float(fired[0]["peak_base"]) == 130.0
