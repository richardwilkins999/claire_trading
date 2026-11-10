"""Deterministic execution nodes (DESIGN.md §11) — no LLM anywhere in this
file. An approved typed thesis contains every number needed; execution is a
function, not a conversation.
"""
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from .. import sessions
from ..accounting.money import from_micro, mul_micro, to_micro
from ..accounting.repo import InsufficientCash
from ..tools.brokers import PlacedOrder
from ..tools.market import yahoo_symbol
# Entry limit vs live quote: beyond this the thesis was priced for something
# else (another stock, a stale quote, pence vs pounds) — refuse, don't trade.
# One band for the gate (pipeline.vet_thesis) and the venue.
from .pipeline import PRICE_BAND


FRACTION_STEP = Decimal("0.1")   # venues that allow fractions: 1/10th share
# "buy at the current price": a limit this far through the live price fills
# at once (the exchange gives the best price on offer) yet caps a jump
MARKET_CAP = Decimal("0.01")


def at_market(th, market, symbol, inst):
    """The plan re-priced to fill NOW: limit = live price ± MARKET_CAP. The
    stop and target stay yours; if the price has already gone through one
    of them the trade is refused rather than sent the wrong way round."""
    from .state import Thesis
    q = market.quote(symbol)
    px = Decimal(str(q["price"]))
    q_ccy = q.get("currency") or inst.currency
    if q_ccy != inst.currency:
        px = px * Decimal(market.fx(q_ccy, inst.currency))
    if px <= 0:
        raise ValueError(f"no usable live price for {inst.id}")
    buy = th.direction == "buy"
    step = Decimal("0.0001") if px < 1 else Decimal("0.01")
    lim = (px * (1 + MARKET_CAP if buy else 1 - MARKET_CAP)).quantize(
        step, rounding="ROUND_UP" if buy else "ROUND_DOWN")
    data = th.model_dump()
    data.update(entry_low=float(lim), entry_high=float(lim))
    try:
        return Thesis.model_validate(data)
    except Exception as e:                      # noqa: BLE001 — explained
        raise ValueError(
            f"at the current price ({px:f}) the order no longer makes sense: "
            f"the stop {th.stop_loss} / target {th.take_profit} must sit "
            f"either side of {lim:f} — change them and retry ({e})") from None


def _fractional(adapter) -> bool:
    """Whether a venue fills fractions of a share. Adapters say so with a
    `fractional` attribute; the in-process sim does, Saxo does not."""
    return bool(getattr(adapter, "fractional", True))


def position_qty(size_base: Decimal, price_base: Decimal, lot_size: int,
                 step: Decimal = FRACTION_STEP) -> Decimal:
    """Shares purchasable for `size_base`. Board-lot venues (SGX etc.) floor
    to whole lots; elsewhere fractional trading is allowed down to `step`, so
    a small budget still buys something instead of rounding to zero."""
    if price_base <= 0:
        raise ValueError("price must be positive")
    raw = size_base / price_base
    if lot_size > 1:
        lots = int(raw / lot_size)
        return Decimal(lots * lot_size)
    return (raw / step).to_integral_value(rounding="ROUND_FLOOR") * step


# Saxo may take a moment to retire a leg it has just accepted a cancel for;
# a sell sent in that moment is refused as if the leg still rested. One
# retry after this pause (patched to 0 in tests).
LEG_SETTLE_S = 2.0
_settle_sleep = time.sleep

# How long a placement claim may sit without a venue acknowledgement before
# the custodian treats its outcome as unknown (the process died mid-call).
SUBMIT_STALE_S = 300


class PlacementUnknown(RuntimeError):
    """A placement was claimed but never acknowledged: the venue may or may
    not hold the order. Never re-place blindly — reconcile first."""


def _accepts_client_id(adapter) -> bool:
    return _accepts(adapter, "client_order_id")


def _accepts(adapter, param, method="place_bracket") -> bool:
    import inspect
    try:
        return param in inspect.signature(
            getattr(adapter, method)).parameters
    except (AttributeError, TypeError, ValueError):
        return False


def trail_distance(o) -> Decimal | None:
    """The approved trailing floor as a price distance: trail_pct of the
    entry's limit. Saxo trails by a fixed distance, not a percentage, so it
    is fixed at the entry — as the price rises it is a smaller share of it
    (8% of 150 = 12, which is 4% at 300). None for an order without a
    floor, a stop or a limit."""
    if not (o["trail_pct"] and o["stop_loss"] and o["limit_price"]):
        return None
    return from_micro(o["limit_price"]) * Decimal(str(o["trail_pct"])) / 100


def submit_order(repo, adapter, order_id, *, symbol, ts, claimed=False):
    """Send one persisted order to its venue — the ONLY place that does.

    The order row is written first (M4): a crash between the venue call and
    the bookkeeping used to leave a live order the book knew nothing about,
    or a pending_session order the custodian would place a second time.
    Placement is claimed with a conditional UPDATE (submitted_at IS NULL), so
    of two would-be placers — execute and the custodian, or two custodians —
    exactly one calls the venue. The venue is given the row's
    client_order_id when the adapter accepts one, so it can dedupe a replay.

    Returns the PlacedOrder, or None if someone else holds the claim. A
    venue exception leaves the claim in place (outcome unknown → the
    custodian flags it for reconciliation); a definitive refusal marks the
    row rejected."""
    conn = repo.conn
    if not claimed:
        cur = conn.execute(
            "UPDATE orders SET submitted_at=?, updated_at=? WHERE id=?"
            " AND status='pending_session' AND submitted_at IS NULL",
            (ts, ts, order_id))
        if cur.rowcount != 1:
            return None
    o = repo.order(order_id)
    kw = dict(symbol=symbol, side=o["side"], qty=from_micro(o["qty"]),
              limit=from_micro(o["limit_price"]) if o["limit_price"] else None,
              stop_loss=from_micro(o["stop_loss"]) if o["stop_loss"] else None,
              take_profit=from_micro(o["take_profit"]) if o["take_profit"]
              else None)
    if _accepts_client_id(adapter):
        kw["client_order_id"] = o["client_order_id"] or o["id"]
    trail = trail_distance(o)
    if trail and _accepts(adapter, "trail_distance"):
        # Saxo holds the floor itself: the bracket's stop becomes a
        # trailing stop at this distance (the watcher's alert stays as a
        # backstop). Venues without venue-side stops ignore it.
        kw["trail_distance"] = float(trail)
    live = _live_gate(repo, o, ts)               # None unless a live account
    if isinstance(live, PlacedOrder):            # refused: definitive
        repo.mark_order(order_id, "rejected", ts=ts)
        return live
    # Protective legs resting at the venue for this position must go first
    # (DESIGN.md §11a): Saxo refuses a new sell while they rest, and if it
    # did not, the stop could sell the same shares again. A cancel the venue
    # will not confirm refuses THIS order — definitively, nothing was sent.
    from .. import protection
    try:
        cancelled = protection.cancel_for(repo, adapter, o, ts=ts)
    except protection.LegCancelRefused as e:
        repo.mark_order(order_id, "rejected", ts=ts)
        _reprotect_now(repo, adapter, order_id, symbol, ts)
        return PlacedOrder("", "rejected", str(e))
    placed = adapter.place_bracket(**kw)
    if cancelled and placed.status != "accepted" and \
            "SellOrdersAlreadyExist" in (placed.detail or ""):
        # the refusal is about the legs we have just cancelled: give the
        # venue a moment, then try once more under a fresh request id (the
        # first attempt was refused, so there is nothing to deduplicate)
        _settle_sleep(LEG_SETTLE_S)
        if "client_order_id" in kw:
            kw["client_order_id"] = f"{kw['client_order_id']}-r1"
        placed = adapter.place_bracket(**kw)
    if placed.status == "accepted":
        repo.mark_order(order_id, "placed", ts=ts,
                        broker_order_id=placed.broker_order_id)
        if placed.legs:
            # the bracket's stop/target are orders of their own at the venue
            protection.persist_legs(
                repo, parent=repo.order(order_id), legs=placed.legs,
                side=protection.leg_side(o["side"]), qty=o["qty"], ts=ts)
        if live is not None and o["side"] == "buy":
            # only entries use the day's live allowance; exits are exempt
            from .. import live_trading
            live_trading.record_placed(repo.conn, account_id=o["account_id"],
                                       order_id=order_id,
                                       notional_base_micro=live,
                                       clock=lambda: ts)
    else:
        repo.mark_order(order_id, "rejected", ts=ts)
        if cancelled:
            # the venue refused the sell AFTER its legs were cancelled: the
            # whole position is bare right now, so restore it at once
            _reprotect_now(repo, adapter, order_id, symbol, ts)
    return placed


def _reprotect_now(repo, adapter, order_id, symbol, ts):
    """Put back the legs a refused order had cancelled. Failure is left on
    the row (protect_state='failed'), where the custodian flags and alerts
    on it — this path has no alert channel of its own."""
    from .. import protection
    if repo.order(order_id)["protect_state"] == "pending":
        protection.reprotect(repo, adapter, order_id, symbol=symbol, ts=ts)


def _live_gate(repo, o, ts):
    """The live-trading switch (DESIGN.md §22), applied HERE because this is
    the one function that sends any order to any venue — so an order queued
    while the market was shut is re-checked when the custodian places it,
    not only when it was approved. Returns None for non-live accounts, the
    order's base-currency notional (micro) when allowed, or a rejected
    PlacedOrder carrying the reason."""
    import os

    from .. import live_trading
    acct = repo.account(o["account_id"])
    if acct["environment"] != "live":
        return None
    limit, rate = o["limit_price"], o["fx_rate"]
    if limit and not rate:
        return PlacedOrder("", "rejected", "live trading: order has no FX "
                           "rate, so its size in base currency is unknown")
    notional = mul_micro(mul_micro(o["qty"], limit), rate) if limit else 0
    held, _, _ = repo.position(o["account_id"], o["instrument_id"])
    try:
        live_trading.check_order(
            repo.conn, os.environ, account_id=o["account_id"], side=o["side"],
            qty_micro=o["qty"], limit_micro=limit,
            notional_base_micro=notional, desk_held_micro=held,
            clock=lambda: ts)
    except live_trading.LiveTradingBlocked as e:
        return PlacedOrder("", "rejected", f"live trading: {e}")
    return notional


def build_execute(repo, brokers: dict, market, *, account_for,
                  clock=time.time, cal=None):
    """`brokers`: name -> adapter. `account_for(broker_name)` -> account_id."""

    def execute(state):
        ap, th, inst = state.approval, state.thesis, state.instrument
        ts = int(clock())
        now = datetime.fromtimestamp(ts, tz=timezone.utc)
        account_id = account_for(ap.broker)
        symbol = yahoo_symbol(inst.ticker, inst.exchange)
        is_open = sessions.is_open(inst.exchange, now, cal)       # §14a

        # Re-running execute (a retried approval whose first attempt raised,
        # M5) must not order twice: the work item's live order IS the order.
        # (protective legs share the entry's work item but are not "the
        # order" — a filled entry's resting stop must not look like one)
        prior = repo.conn.execute(
            "SELECT * FROM orders WHERE work_item_id=? AND leg IS NULL AND"
            " status NOT IN ('rejected','cancelled','expired') ORDER BY"
            " created_at DESC, rowid DESC LIMIT 1",
            (state.work_item_id,)).fetchone() \
            if state.work_item_id else None
        if prior is not None:
            if prior["status"] != "pending_session" or not is_open:
                return {"order_ids": [prior["id"]]}
            if prior["submitted_at"] is not None:
                raise PlacementUnknown(
                    f"order {prior['id']} was sent to the venue but never "
                    "acknowledged — reconcile it at the venue before retrying")
            placed = submit_order(repo, brokers[ap.broker], prior["id"],
                                  symbol=symbol, ts=ts)
            if placed is not None and placed.status != "accepted":
                raise RuntimeError(f"broker rejected order: {placed.detail}")
            return {"order_ids": [prior["id"]]}

        repriced = {}
        if getattr(ap, "order_type", "limit") == "market" and \
                th.direction in ("buy", "sell"):
            th = at_market(th, market, symbol, inst)
            repriced = {"thesis": th}
            if state.work_item_id:
                repo.conn.execute("UPDATE work_items SET thesis_json=?"
                                  " WHERE id=?", (th.model_dump_json(),
                                                  state.work_item_id))
        qty, ref_native, rate, reserve = _intended(
            ap, th, inst, market, symbol, account_id, repo, state.kind)
        _pre_checks(repo, account_id, inst, th, qty, ref_native, rate,
                    reserve, ts)
        oid = f"ord_{uuid.uuid4().hex[:12]}"
        # An order that CLOSES a position carries no bracket: a stop and
        # target belong to the position an order opens. On an exit sell they
        # would be BUY legs resting at Saxo after the shares are gone — the
        # "stop" would open a fresh long. (An exit's own protection is the
        # limit; the old legs are cancelled and, if shares remain,
        # re-protected — DESIGN.md §11a.)
        held_now, _, _ = repo.position(account_id, inst.id)
        closing = (th.direction == "sell" and held_now > 0) or \
            (th.direction == "buy" and held_now < 0)
        repo.create_order(
            id=oid, account_id=account_id, instrument_id=inst.id,
            side=th.direction, qty=to_micro(qty),
            limit_price=to_micro(str(th.entry_high)) if th.entry_high else None,
            stop_loss=to_micro(str(th.stop_loss))
            if th.stop_loss and not closing else None,
            take_profit=to_micro(str(th.take_profit))
            if th.take_profit and not closing else None,
            # persisted BEFORE the venue call (M4); while the session is
            # closed it simply rests here for the custodian (checks re-run
            # at fill). When open, the row is born already claimed, so the
            # custodian can never race execute to place it.
            status="pending_session", work_item_id=state.work_item_id,
            fx_rate=rate, ref_price=to_micro(str(ref_native))
            if ref_native is not None else None,
            reserved_base=reserve, submitted_at=ts if is_open else None,
            expires_at=int(sessions.close_of(inst.exchange, now,
                                             cal).timestamp()), ts=ts,
            # the floor rides with the stop it trails; an exit has neither
            trail_pct=float(ap.trail_pct) if getattr(ap, "trail_pct", None)
            and th.stop_loss and not closing else None)
        if not is_open:
            _set_trailing_floor(repo, inst, ap, account_id, th.direction)
            return {"order_ids": [oid], **repriced}  # custodian places at next open
        placed = submit_order(repo, brokers[ap.broker], oid, symbol=symbol,
                              ts=ts, claimed=True)
        if placed.status != "accepted":
            raise RuntimeError(f"broker rejected order: {placed.detail}")
        _set_trailing_floor(repo, inst, ap, account_id, th.direction)
        return {"order_ids": [oid], **repriced}     # fills arrive ASYNC (§12)

    def _intended(ap, th, inst, market, symbol, account_id, repo_, kind):
        """(qty, reference price native, fx rate ×1e6, cash to reserve).

        Every order is measured in the account's BASE currency (H7), so both
        sides need the live quote and an FX rate. A failed FX lookup raises
        here — it is never replaced by 1.0 (H6): a ¥300,000 order booked as
        $300,000 is worse than no order."""
        acct = repo_.account(account_id)
        base = acct["base_currency"]
        q = market.quote(symbol)
        quote = Decimal(str(q["price"]))
        q_ccy = q.get("currency") or inst.currency
        rate_i = Decimal(market.fx(inst.currency, base))
        rate_q = rate_i if q_ccy == inst.currency \
            else Decimal(market.fx(q_ccy, base))
        if quote <= 0 or rate_i <= 0 or rate_q <= 0:
            raise ValueError(f"no usable price/FX for {inst.id}: quote {quote}"
                             f" {q_ccy}, fx {rate_i}")
        ref_native = quote * rate_q / rate_i       # live price, instrument ccy
        _price_band(th, ref_native, inst)
        held_now, _, _ = repo_.position(account_id, inst.id)
        _levels_vs_live(th, ref_native, inst, held_now)
        rate = to_micro(str(rate_i))
        if th.direction == "sell":
            held, _, _ = repo_.position(account_id, inst.id)
            held_shares = Decimal(held) / 1_000_000
            qty = Decimal(str(ap.qty)) if ap.qty else held_shares
            if kind == "sell_review" or (kind == "manual" and held > 0):
                # an exit review — or your own sell of shares the desk holds
                # (the Sell button) — closes a position; it never flips short
                qty = min(qty, held_shares)
            if not _fractional(brokers.get(ap.broker)):
                qty = qty.to_integral_value(rounding="ROUND_FLOOR")
            return qty, ref_native, rate, None
        price_base = quote * rate_q
        step = FRACTION_STEP if _fractional(brokers.get(ap.broker)) \
            else Decimal(1)
        if ap.qty:
            # the approval named the SHARES (the card's share count): buy
            # exactly that, down to what the venue can fill (board lots,
            # whole shares) — never more than approved
            want = Decimal(str(ap.qty))
            if inst.lot_size > 1:
                qty = Decimal(int(want / inst.lot_size) * inst.lot_size)
            else:
                qty = (want / step).to_integral_value(
                    rounding="ROUND_FLOOR") * step
            if qty <= 0:
                raise ValueError(
                    f"{ap.qty} shares is less than one tradable "
                    f"{'lot of ' + str(inst.lot_size) if inst.lot_size > 1 else 'share'}"
                    f" of {inst.id}")
        else:
            # Saxo (and any whole-share venue) cannot fill 9.6 shares: size
            # in whole shares there, rounding DOWN so the spend never exceeds
            # the approved amount. The in-process sim keeps 0.1 fractions.
            qty = position_qty(Decimal(str(ap.size_base)), price_base,
                               inst.lot_size, step)
        # a limit buy fills at or below its limit: reserve the worse of the
        # limit and the quote, so the cash promised covers any fill
        worst = max(price_base, Decimal(str(th.entry_high)) * rate_i
                    if th.entry_high else price_base)
        cost = mul_micro(to_micro(qty), to_micro(str(worst)))
        fee = repo_._fee_for(json.loads(acct["fee_model"]), cost)
        return qty, ref_native, rate, cost + fee

    return execute


def _price_band(th, ref_native, inst):
    """The venue-side twin of the gate's check (pipeline.vet_thesis): the
    limit sent to the venue must be near the price it will meet."""
    lim = th.entry_high
    if lim is None or not ref_native:
        return
    off = abs(Decimal(str(lim)) / ref_native - 1)
    if off > PRICE_BAND:
        raise ValueError(
            f"limit {lim} {inst.currency} is {off * 100:.0f}% from the live "
            f"price {ref_native:.4f} — outside the ±{PRICE_BAND * 100:.0f}% "
            "band; the thesis was priced for something else")


def _levels_vs_live(th, ref_native, inst, held):
    """A limit order fills at the live price when that is already better
    than its limit (a buy limit of 40 with the stock at 33 buys at ~33). The
    stop-loss and take-profit were only checked against the LIMIT, so a stop
    of 35 passed — and would sit above the real fill, selling the shares the
    moment they arrive. Checked against the live price before anything is
    sent; an order that CLOSES a position carries no stop or target."""
    buy = th.direction == "buy"
    if (buy and held < 0) or (not buy and held > 0):
        return
    ccy, ref = inst.currency, Decimal(ref_native)
    stop = Decimal(str(th.stop_loss)) if th.stop_loss else None
    tgt = Decimal(str(th.take_profit)) if th.take_profit else None
    px = f"{ref:.4f}".rstrip("0").rstrip(".")
    if stop is not None and (stop >= ref if buy else stop <= ref):
        raise ValueError(
            f"the stop-loss {th.stop_loss} {ccy} is {'at or above' if buy else 'at or below'}"
            f" the live price {px} — the order fills at about the live price,"
            f" so the stop would sell straight away. Set the stop-loss"
            f" {'below' if buy else 'above'} {px} and retry")
    if tgt is not None and (tgt <= ref if buy else tgt >= ref):
        raise ValueError(
            f"the take-profit {th.take_profit} {ccy} is {'at or below' if buy else 'at or above'}"
            f" the live price {px} — it would sell straight away. Set it"
            f" {'above' if buy else 'below'} {px} and retry")


def _set_trailing_floor(repo, inst, ap, account_id, direction):
    """The trailing floor you chose at approval becomes the watcher's rule for
    THIS position (instrument, account, side) — replacing the default 8%.
    One row per position (unique index, M7): the old ON CONFLICT DO NOTHING
    had no constraint to hit, so each approval appended a duplicate.

    peak_base is cleared when the account is flat on that side: a re-bought
    stock must not inherit the old position's high-water mark (it would sit
    far above the new entry and fire at once). The watcher seeds it from the
    new average cost on its next tick and ratchets it from there."""
    if ap.status != "approved":
        return
    side = 1 if direction == "buy" else -1
    held, _, _ = repo.position(account_id, inst.id)
    flat = held == 0 or (held > 0) != (side > 0)
    conn = repo.conn
    if flat:
        conn.execute(
            "UPDATE price_alerts SET peak_base=NULL, fire_count_today=0"
            " WHERE rule='trail_pct' AND instrument_id=? AND account_id=?"
            " AND side=?", (inst.id, account_id, side))
    if not ap.trail_pct:
        return
    cur = conn.execute(
        "UPDATE price_alerts SET threshold=?, armed=1 WHERE rule='trail_pct'"
        " AND instrument_id=? AND account_id=? AND side=?",
        (float(ap.trail_pct), inst.id, account_id, side))
    if cur.rowcount == 0:
        try:
            conn.execute(
                "INSERT INTO price_alerts (instrument_id, account_id, side,"
                " rule, threshold, armed) VALUES (?,?,?,'trail_pct',?,1)",
                (inst.id, account_id, side, float(ap.trail_pct)))
        except sqlite3.IntegrityError:      # lost a race to the watcher's seed
            conn.execute(
                "UPDATE price_alerts SET threshold=?, armed=1"
                " WHERE rule='trail_pct' AND instrument_id=? AND account_id=?"
                " AND side=?", (float(ap.trail_pct), inst.id, account_id,
                                side))


def _pre_checks(repo, account_id, inst, th, qty, ref_native, rate, reserve,
                ts):
    """Everything that must hold BEFORE the venue sees the order (§11):
    a real quantity, cash to cover cost + estimated fee, and risk caps —
    all in the account's base currency."""
    if qty is None or qty <= 0:
        raise ValueError(
            "order quantity is zero — size too small for one lot, or a sell "
            "with no position and no explicit qty")
    limit = to_micro(str(th.entry_high)) if th.entry_high else None
    repo.check_risk(account_id, inst.id, th.direction, to_micro(qty),
                    limit, ts, fx_rate=rate,
                    ref_price=to_micro(str(ref_native))
                    if ref_native is not None else None)
    if th.direction == "buy" and reserve is not None:
        acct = repo.account(account_id)
        # cash already promised to other open buys is not available: two
        # approvals inside one poll interval each used to see the full
        # balance, and the second fill then failed with InsufficientCash
        balance = repo.cash_balance(account_id)
        held_back = repo.reserved_cash(account_id)
        if reserve > balance - held_back:
            raise InsufficientCash(
                f"order needs {from_micro(reserve):.2f} "
                f"{acct['base_currency']} (incl. fee) but {account_id} holds "
                f"{from_micro(balance):.2f}"
                + (f", {from_micro(held_back):.2f} of it committed to open "
                   "orders" if held_back else ""))


def build_record(repo, narratives, *, clock=time.time, prose_fn=None):
    """Terminal bookkeeping: one factual markdown summary per run. State
    transitions live in Desk._sync; money lives in repo — this node only
    writes prose ABOUT the record, never the record. `prose_fn` (the recorder
    agent, §4) adds a short human note when a provider is available; its
    failure never fails a run."""

    def record(state):
        lines = [f"# {state.ticker} — {state.kind} {state.work_item_id}", ""]
        for r in state.reports:
            lines.append(f"- **{r.agent}**: {r.signal} "
                         f"(conviction {r.conviction:.2f}) — {r.summary}")
        if state.thesis:
            t = state.thesis
            lines.append(f"\n**Arbiter**: {t.direction} "
                         f"(conviction {t.conviction:.2f}) "
                         f"entry {t.entry_low}–{t.entry_high} "
                         f"stop {t.stop_loss} target {t.take_profit}")
        if state.approval:
            lines.append(f"**Decision**: {state.approval.status} "
                         f"by {state.approval.actor}")
        if state.order_ids:
            lines.append(f"**Orders**: {', '.join(state.order_ids)}")
        if state.errors:
            lines.append("\n## Errors\n" + "\n".join(f"- {e}" for e in state.errors))
        if prose_fn is not None:
            try:
                lines.append("\n## Recorder note\n" +
                             prose_fn("\n".join(lines)))
            except Exception:                   # noqa: BLE001 — prose is a
                pass                            # nicety, never a dependency
        narratives.write(state.work_item_id, "summary", "\n".join(lines))
        return {}

    return record
