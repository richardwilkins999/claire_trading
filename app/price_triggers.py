"""Price triggers (DESIGN.md §15a): "buy 50 shares of X when it falls to
5.00", "sell my 20 when it reaches 9.50".

A trigger is a ONE-SHOT rule the owner sets on the Watchlist page. While the
instrument's market is open, the `price_triggers` schedule (claire-api, every
2 minutes) prices every active rule; on a hit it claims the rule atomically
and hands the trade to `Desk.manual_trade` — the same path as the Buy and
Sell buttons — so from the gate on it is an ordinary approval: token, cash
and risk caps, price band, the live locks, whole-share sizing, leg handling.

Two modes:
  ask  (default) — the hit only creates the approval card (which also
         reaches Telegram); nothing trades until the owner approves it.
  auto — the owner PRE-APPROVED the trade when creating the rule. The engine
         approves the card itself through `approvals.thesis_action`, with the
         card's own single-use token, recording who pre-approved it and
         when. Allowed only on paper accounts (built-in sim, Saxo SIM). A
         real-money account is refused at creation (here, and by a CHECK in
         the table) and again at fire time — and even if both were bypassed,
         thesis_action refuses saxo_live without a typed "LIVE".

Every refusal is a message, never an exception that stops the other rules.
Money is integer micro-units; Decimal only at the boundary.
"""
import json
import math
import time
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal

from . import sessions
from .accounting.money import from_micro, to_micro
import logging
from .logsetup import swallowed
from .accounting import holdings

log = logging.getLogger(__name__)

DEFAULT_EXPIRY_DAYS = 30
MAX_EXPIRY_DAYS = 365
MAX_ACTIVE = 100            # a runaway script must not fill the table
MAX_PRICE = Decimal("1e9")
# the limit is the price the order is sent at; execution refuses one more
# than PRICE_BAND (25%) from the live price, so a limit that far from the
# trigger price could never be placed — refuse it now, not at fire time
LIMIT_BAND = Decimal("0.25")
SYMBOL = {"lte": "≤", "gte": "≥"}
STATUSES = ("active", "fired", "cancelled", "expired", "failed")


class TriggerError(ValueError):
    """A refused request; `code` is the HTTP status the API answers with."""

    def __init__(self, message, code=400):
        super().__init__(message)
        self.code, self.message = code, message


# ── small helpers ────────────────────────────────────────────────────────
def _dec(body, key, *, required=False):
    """A positive, finite price/quantity from JSON, as Decimal (or None)."""
    v = body.get(key)
    if v is None or v == "":
        if required:
            raise TriggerError(f"{key.replace('_', ' ')} is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise TriggerError(f"{key.replace('_', ' ')} must be a number")
    try:
        d = Decimal(str(v))
    except ArithmeticError:
        raise TriggerError(f"{key.replace('_', ' ')} must be a number") \
            from None
    if not d.is_finite() or d <= 0 or d > MAX_PRICE:
        raise TriggerError(f"{key.replace('_', ' ')} must be a positive "
                           "number")
    return d


def _m(d):
    return None if d is None else to_micro(d)


def _f(n):
    """micro int → float for JSON/UI (display only, never booked)."""
    return None if n is None else float(from_micro(n))


def fmt_price(n_micro) -> str:
    """At least two decimals (5.00), more only when the price has them
    (0.1234) — never 5 or 5.000000."""
    d = from_micro(n_micro).normalize()
    s = f"{d:f}"
    if "." not in s or len(s.split(".")[1]) < 2:
        s = f"{d:.2f}"
    return s


def account_for(conn, broker):
    """The desk account behind a broker name — the same lookup the approval
    path uses (thesis_action: broker_accounts WHERE broker=? LIMIT 1)."""
    return conn.execute("SELECT * FROM broker_accounts WHERE broker=?"
                        " LIMIT 1", (broker,)).fetchone()


def auto_refusal(account) -> str | None:
    """Why automatic placement is NOT allowed on this account, or None.
    Only paper money may be traded without a human click at fire time."""
    if account is None:
        return "unknown account"
    if account["broker"] == "saxo_live" or account["environment"] not in (
            "paper", "sim"):
        return ("automatic placement is only for paper accounts (built-in "
                "sim, Saxo SIM) — on the real-money account a trigger can "
                "only ask you: it creates an approval card")
    return None


def whole_shares(account, env) -> bool:
    """Saxo fills whole shares only (as _approval_account says on the card)."""
    env = env or {}
    return account["environment"] == "live" or (
        account["broker"] == "saxo" and
        (env.get("SAXO_SIM_EXECUTION") or "").lower() == "enabled")


def held_on(conn, account_id, ticker, exchange) -> Decimal:
    """Signed shares the desk's book holds in this name on one account."""
    return holdings.net(conn, account_id=account_id, ticker=ticker,
                        exchange=exchange)


def held_total(conn, ticker, exchange) -> Decimal:
    """Net shares across accounts in this instrument — what
    Desk.manual_trade uses to decide EXIT vs SHORT."""
    return holdings.net(conn, ticker=ticker, exchange=exchange)


def row_dict(r) -> dict:
    """A trigger for the API/UI: prices as floats, plus a readable rule."""
    d = dict(r)
    for k in ("trigger_price", "limit_price", "stop_loss", "take_profit",
              "fired_price", "last_price", "qty", "amount_base"):
        d[k] = _f(d[k])
    d["rule"] = describe(r)
    return d


def describe(t) -> str:
    """'Buy 50 STVN when price ≤ 5.00 (purchase price 5.00)' — for cards
    and alerts (a sale price for a sell)."""
    size = (f"{from_micro(t['qty']).normalize():f} "
            if t["qty"] is not None
            else f"{fmt_price(t['amount_base'])} worth of ")
    verb = {"buy": "Buy", "exit": "Sell", "short": "Short-sell"}[t["kind"]]
    return (f"{verb} {size}{t['ticker']} when price "
            f"{SYMBOL[t['condition']]} {fmt_price(t['trigger_price'])}"
            f" ({'purchase' if t['kind'] == 'buy' else 'sale'} price"
            f" {fmt_price(t['limit_price'])})")


# ── create / cancel / list ───────────────────────────────────────────────
def create(conn, body: dict, *, created_by: str, env=None, clock=time.time):
    """Validate a new trigger up front — the same rules Desk.manual_trade
    applies at fire time, plus what only makes sense for a rule that waits
    (expiry, holdings on the chosen account, the paper-only auto mode) — and
    store it. Returns the stored row as a dict. Raises TriggerError."""
    from .api.desk import validate_symbol
    if not isinstance(body, dict):
        raise TriggerError("body must be an object")
    ticker = str(body.get("ticker") or "").upper().strip()
    exchange = str(body.get("exchange") or "").upper().strip()
    try:
        validate_symbol(ticker, exchange)
    except ValueError as e:
        raise TriggerError(str(e)) from None
    if exchange not in sessions.DEFAULTS:
        raise TriggerError(f"no trading hours known for {exchange} — a "
                           "trigger only runs while its market is open")
    direction = body.get("direction")
    if direction not in ("buy", "sell"):
        raise TriggerError("direction must be buy or sell")
    condition = body.get("condition")
    if condition not in SYMBOL:
        raise TriggerError("condition must be lte (price falls to) or gte "
                           "(price rises to)")
    trig = _dec(body, "trigger_price", required=True)
    limit = _dec(body, "limit_price") or trig
    if abs(limit / trig - 1) > LIMIT_BAND:
        raise TriggerError(f"the limit {limit} is more than "
                           f"{LIMIT_BAND * 100:.0f}% from the trigger price "
                           f"{trig} — execution would refuse it")
    qty, amount = _dec(body, "qty"), _dec(body, "amount")
    stop, target = _dec(body, "stop"), _dec(body, "target")

    broker = body.get("broker")
    acct = account_for(conn, broker) if isinstance(broker, str) else None
    if acct is None:
        raise TriggerError("choose an account (broker) the desk has")
    mode = body.get("mode") or "ask"
    if mode not in ("ask", "auto"):
        raise TriggerError("mode must be ask or auto")
    if mode == "auto":
        why = auto_refusal(acct)
        if why:
            raise TriggerError(f"Refused: {why}.", 403)

    if direction == "buy":
        if (qty is None) == (amount is None):
            raise TriggerError("a buy is sized by shares OR by an amount — "
                               "give exactly one")
        kind = "buy"
        if stop is None:
            raise TriggerError("a buy needs a stop below the limit price")
        if stop >= limit:
            raise TriggerError("for a buy the stop must sit BELOW the limit "
                               "price")
        if target is not None and target <= limit:
            raise TriggerError("for a buy the target must sit ABOVE the limit"
                               " price")
    else:
        if amount is not None:
            raise TriggerError("a sell is sized in shares — give shares, not "
                               "an amount")
        if qty is None:
            raise TriggerError("how many shares to sell?")
        here = held_on(conn, acct["id"], ticker, exchange)
        if here > 0:
            kind = "exit"
            if qty > here:
                raise TriggerError(f"the desk holds only {here.normalize()} "
                                   f"{ticker} on {acct['id']} — it can only "
                                   "sell what it bought")
            if stop is not None or target is not None:
                raise TriggerError("selling shares the desk holds is an EXIT "
                                   "— it takes no stop or target")
        else:
            elsewhere = held_total(conn, ticker, exchange)
            if elsewhere > 0:
                raise TriggerError(
                    f"the desk holds {elsewhere.normalize()} {ticker} on "
                    f"another account, not on {acct['id']} — choose the "
                    "account that holds them")
            kind = "short"
            if acct["environment"] == "live" or acct["broker"] == "saxo_live":
                raise TriggerError("no shorting on the real-money account "
                                   "(DESIGN.md §22.2)")
            if stop is None:
                raise TriggerError(f"the desk holds no {ticker}, so this sell "
                                   "is a SHORT — it needs a stop ABOVE the "
                                   "limit price")
            if stop <= limit:
                raise TriggerError("for a short the stop must sit ABOVE the "
                                   "limit price")
            if target is not None and target >= limit:
                raise TriggerError("for a short the target must sit BELOW the "
                                   "limit price")
    if qty is not None and whole_shares(acct, env) and qty != qty.to_integral():
        raise TriggerError(f"{acct['id']} trades whole shares only")
    # execution floors a buy to the board lot (prepare: SGX lots of 100), so
    # "buy 50" there would size to zero shares at fire time
    if direction == "buy" and qty is not None and exchange == "SGX" and \
            qty % 100:
        raise TriggerError("SGX trades in board lots of 100 shares")

    trail = body.get("trail_pct")
    if trail is not None:
        if isinstance(trail, bool) or not isinstance(trail, (int, float)) or \
                not math.isfinite(trail) or not 0 < trail <= 90:
            raise TriggerError("trailing floor must be a percentage 0–90")
        trail = float(trail)
    days = body.get("expires_in_days", DEFAULT_EXPIRY_DAYS)
    if isinstance(days, bool) or not isinstance(days, int) or \
            not 1 <= days <= MAX_EXPIRY_DAYS:
        raise TriggerError(f"expiry must be 1–{MAX_EXPIRY_DAYS} days")
    note = body.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > 120):
        raise TriggerError("note must be text of at most 120 characters")
    note = (note or "").strip() or None

    (n_active,) = conn.execute("SELECT COUNT(*) FROM price_triggers WHERE"
                               " status='active'").fetchone()
    if n_active >= MAX_ACTIVE:
        raise TriggerError(f"{MAX_ACTIVE} triggers are already active — "
                           "cancel some first", 409)
    now = int(clock())
    cur = conn.execute(
        "INSERT INTO price_triggers (ticker, exchange, direction, kind,"
        " condition, trigger_price, limit_price, qty, amount_base, stop_loss,"
        " take_profit, trail_pct, broker, account_id, mode, note, status,"
        " created_at, created_by, expires_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?)",
        (ticker, exchange, direction, kind, condition, _m(trig), _m(limit),
         _m(qty), _m(amount), _m(stop), _m(target), trail, broker,
         acct["id"], mode, note, now, str(created_by)[:120],
         now + days * 86400, now))
    return row_dict(get(conn, cur.lastrowid))


def get(conn, tid):
    return conn.execute("SELECT * FROM price_triggers WHERE id=?",
                        (tid,)).fetchone()


def cancel(conn, tid, *, actor="human", clock=time.time):
    """Only an ACTIVE trigger can be cancelled; one that already fired has a
    card or an order, which are cancelled where they live (Approvals)."""
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        raise TriggerError("id must be a number") from None
    row = get(conn, tid)
    if row is None:
        raise TriggerError("unknown trigger", 404)
    now = int(clock())
    cur = conn.execute(
        "UPDATE price_triggers SET status='cancelled', status_reason=?,"
        " updated_at=? WHERE id=? AND status='active'",
        (f"cancelled by {actor}"[:120], now, tid))
    if cur.rowcount != 1:
        raise TriggerError(f"trigger #{tid} is {row['status']}, not active",
                           409)
    return row_dict(get(conn, tid))


def replace(conn, tid, body: dict, *, created_by: str, env=None,
            clock=time.time):
    """Edit an ACTIVE trigger: the new rule passes every check `create`
    applies, and the old one is cancelled ("replaced by #N") in the same
    transaction — so a refused edit changes nothing, and a trigger that fires
    while you edit it is not silently re-armed (the edit is refused)."""
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        raise TriggerError("id must be a number") from None
    old = get(conn, tid)
    if old is None:
        raise TriggerError("unknown trigger", 404)
    now = int(clock())
    conn.execute("SAVEPOINT trigger_edit")
    try:
        cur = conn.execute(
            "UPDATE price_triggers SET status='cancelled', status_reason="
            "'edited', updated_at=? WHERE id=? AND status='active'",
            (now, tid))
        if cur.rowcount != 1:
            raise TriggerError(f"trigger #{tid} is {old['status']}, not "
                               "active — it can no longer be edited", 409)
        new = create(conn, body, created_by=created_by, env=env, clock=clock)
        conn.execute("UPDATE price_triggers SET status_reason=? WHERE id=?",
                     (f"edited — replaced by #{new['id']}", tid))
        conn.execute("RELEASE trigger_edit")
    except BaseException:
        conn.execute("ROLLBACK TO trigger_edit")
        conn.execute("RELEASE trigger_edit")
        raise
    new["replaces"] = tid
    return new


def list_triggers(conn, *, status=None, ticker=None, since=None, limit=300):
    q, args = "SELECT * FROM price_triggers WHERE 1=1", []
    if status:
        statuses = [s for s in str(status).split(",") if s in STATUSES]
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
    if ticker:
        q += " AND ticker=?"
        args.append(str(ticker).upper())
    if since:
        q += " AND (status='active' OR updated_at >= ?)"
        args.append(int(since))
    q += " ORDER BY status='active' DESC, updated_at DESC LIMIT ?"
    args.append(int(limit))
    return [row_dict(r) for r in conn.execute(q, args).fetchall()]


def expire_due(conn, now) -> list[int]:
    """Active past expires_at → expired. Runs every tick, open market or
    not: an expired rule must never fire on the next open."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM price_triggers WHERE status='active' AND"
        " expires_at <= ?", (now,)).fetchall()]
    for tid in ids:
        conn.execute("UPDATE price_triggers SET status='expired',"
                     " status_reason='expired unfired', updated_at=?"
                     " WHERE id=? AND status='active'", (now, tid))
    return ids


# ── the engine ───────────────────────────────────────────────────────────
def hit(condition, trigger_micro, price: Decimal) -> bool:
    trig = from_micro(trigger_micro)
    return price <= trig if condition == "lte" else price >= trig


def _quotes(market, symbols):
    """One batch call; if the batch fails (one bad symbol makes the free
    chain raise for all), price each symbol on its own so one name cannot
    blind the rest."""
    try:
        return dict(market.spark(list(symbols))), {}
    except Exception as e:                      # noqa: BLE001
        swallowed(log, "batch quote failed; pricing each symbol on its own", e)
        pass
    out, errors = {}, {}
    for s in symbols:
        try:
            out[s] = market.quote(s)
        except Exception as e:                  # noqa: BLE001
            errors[s] = str(e)[:200]
    return out, errors


def run_once(conn, desk, market, *, resume_post, alert=None, cal=None,
             clock=time.time, env=None, require_open=True):
    """One pass: expire, price the rules whose market is open, fire hits.
    Returns a summary for the schedule's last_result. Never raises for one
    rule's sake."""
    from .tools.market import yahoo_symbol
    now = int(clock())
    nowdt = datetime.fromtimestamp(now, tz=timezone.utc)
    expired = expire_due(conn, now)
    active = conn.execute("SELECT * FROM price_triggers WHERE"
                          " status='active' ORDER BY id").fetchall()
    out = {"active": len(active), "expired": expired, "closed": 0,
           "stale": [], "fired": [], "failed": [], "deferred": [],
           "errors": []}
    live = []
    for t in active:
        try:
            is_open = sessions.is_open(t["exchange"], nowdt, cal)
        except KeyError:                       # unknown venue: never "open"
            is_open = False
        # a closed market's quote is yesterday's close: it must never fire
        # a rule overnight. require_open is the schedule's flag; the engine
        # treats "closed" as "do not evaluate" regardless of it.
        if not is_open:
            out["closed"] += 1
            continue
        live.append(t)
    if not live:
        return out
    syms = {t["id"]: yahoo_symbol(t["ticker"], t["exchange"]) for t in live}
    quotes, qerr = _quotes(market, sorted(set(syms.values())))
    for t in live:
        try:
            res = evaluate(conn, desk, market, t, quotes.get(syms[t["id"]]),
                           qerr.get(syms[t["id"]]), resume_post=resume_post,
                           alert=alert, clock=clock, env=env)
        except Exception as e:                  # noqa: BLE001 — isolate
            out["errors"].append({"id": t["id"], "error": str(e)[:200]})
            try:
                conn.execute("UPDATE price_triggers SET last_error=?"
                             " WHERE id=?", (str(e)[:300], t["id"]))
            except Exception:                   # noqa: BLE001
                pass
            continue
        if res["result"] in ("fired", "failed", "deferred"):
            out[res["result"]].append({k: v for k, v in res.items()
                                       if k != "result"})
        elif res["result"] == "stale":
            out["stale"].append(t["id"])
    return out


def evaluate(conn, desk, market, t, q, qerr=None, *, resume_post,
             alert=None, clock=time.time, env=None):
    """Price one rule against its quote; on a hit, claim it and fire."""
    now = int(clock())
    if q is None or q.get("price") is None:
        conn.execute("UPDATE price_triggers SET last_checked_at=?,"
                     " last_error=? WHERE id=?",
                     (now, f"no quote: {qerr or 'unavailable'}"[:300],
                      t["id"]))
        return {"result": "stale", "id": t["id"]}
    if q.get("stale"):
        # a last-good cached value, not a price someone can trade at now
        conn.execute("UPDATE price_triggers SET last_checked_at=?,"
                     " last_error='stale quote ignored' WHERE id=?",
                     (now, t["id"]))
        return {"result": "stale", "id": t["id"]}
    price = Decimal(str(q["price"]))
    if not price.is_finite() or price <= 0:
        return {"result": "stale", "id": t["id"]}
    conn.execute("UPDATE price_triggers SET last_checked_at=?, last_price=?,"
                 " last_error=NULL WHERE id=?", (now, to_micro(price),
                                                  t["id"]))
    if not hit(t["condition"], t["trigger_price"], price):
        return {"result": "waiting", "id": t["id"]}
    # ONE-SHOT, enforced by the database: only the caller whose conditional
    # UPDATE matched fires. A second engine (or a cancel racing the hit)
    # finds the row no longer active and does nothing.
    cur = conn.execute(
        "UPDATE price_triggers SET status='fired', fired_at=?, fired_price=?,"
        " updated_at=? WHERE id=? AND status='active'",
        (now, to_micro(price), now, t["id"]))
    if cur.rowcount != 1:
        return {"result": "lost_race", "id": t["id"]}
    return fire(conn, desk, market, get(conn, t["id"]), price, q,
                resume_post=resume_post, alert=alert, clock=clock, env=env)


def _finish(conn, tid, now, **cols):
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE price_triggers SET {sets}, updated_at=? WHERE id=?",
                 (*cols.values(), now, tid))


def _alert(alert, text):
    if alert is None:
        return
    try:
        alert(text, title="Claire trigger")
    except Exception as e:                      # noqa: BLE001 — best effort
        swallowed(log, "price-trigger alert was not sent", e)
        pass


def fire(conn, desk, market, t, price: Decimal, q, *, resume_post,
         alert=None, clock=time.time, env=None):
    """The rule has been claimed (status 'fired'). Create the trade at the
    gate; in auto mode, approve it on the owner's standing pre-approval."""
    from pydantic import ValidationError

    from .api.desk import Conflict
    now = int(clock())
    tid, ticker = t["id"], t["ticker"]
    head = (f"{ticker} hit {fmt_price(to_micro(price))} (your trigger "
            f"{SYMBOL[t['condition']]} {fmt_price(t['trigger_price'])})")

    def fail(reason):
        _finish(conn, tid, now, status="failed", status_reason=reason[:300])
        _alert(alert, f"{head} — trigger #{tid} FAILED: {reason}")
        return {"result": "failed", "id": tid, "reason": reason}

    acct = conn.execute("SELECT * FROM broker_accounts WHERE id=?",
                        (t["account_id"],)).fetchone()
    if acct is None:
        return fail(f"account {t['account_id']} no longer exists")
    qty = from_micro(t["qty"]) if t["qty"] is not None else None
    if t["kind"] == "exit":
        here = held_on(conn, acct["id"], ticker, t["exchange"])
        if here < qty:
            return fail(f"the desk now holds {here.normalize()} {ticker} on "
                        f"{acct['id']}, fewer than the {qty.normalize()} "
                        "this trigger sells — nothing was sent")
    elif t["kind"] == "short" and \
            held_total(conn, ticker, t["exchange"]) > 0:
        return fail(f"the desk now holds {ticker}, so this short-sell would "
                    "become an exit — nothing was sent; set a new trigger")

    suggested = {"broker": t["broker"]}
    if qty is not None:
        suggested["qty"] = float(qty)
    else:
        suggested["size_base"] = float(from_micro(t["amount_base"]))
    if t["trail_pct"]:
        suggested["trail_pct"] = float(t["trail_pct"])
    note = (f"trigger #{tid} {SYMBOL[t['condition']]} "
            f"{fmt_price(t['trigger_price'])} hit at "
            f"{fmt_price(to_micro(price))}" +
            (f" — {t['note']}" if t["note"] else ""))[:200]
    try:
        wi = desk.manual_trade(
            ticker, t["exchange"], t["direction"],
            float(from_micro(t["limit_price"])),
            stop=_f(t["stop_loss"]), target=_f(t["take_profit"]), note=note,
            suggested=suggested, source="trigger", background=False)
    except Conflict as e:
        # the same trade is already in front of the gate (e.g. a second
        # trigger on this name). Not this rule's failure: put it back and
        # try again next tick, until that one is decided or this expires.
        conn.execute("UPDATE price_triggers SET status='active',"
                     " fired_at=NULL, fired_price=NULL, last_error=?,"
                     " updated_at=? WHERE id=? AND status='fired' AND"
                     " work_item_id IS NULL", (f"waiting: {e}"[:300], now,
                                               tid))
        return {"result": "deferred", "id": tid, "reason": str(e)[:200]}
    except (ValidationError, ValueError) as e:
        return fail(f"the desk refused the trade: {str(e)[:200]}")
    except Exception as e:                      # noqa: BLE001
        return fail(f"could not create the trade: {str(e)[:200]}")
    _finish(conn, tid, now, work_item_id=wi)

    row = desk.work_item(wi)
    if row is None or row["state"] != "awaiting_approval" or \
            not row["approval_token"]:
        state = row["state"] if row else "missing"
        return fail(f"the trade ({wi}) did not reach the approval gate "
                    f"(it is {state})")

    if t["mode"] != "auto":
        _finish(conn, tid, now, outcome="card")
        _alert(alert, f"{head} — approval card ready ({describe(t)})")
        return {"result": "fired", "id": tid, "work_item_id": wi,
                "outcome": "card"}

    # ── auto: the owner's standing pre-approval ─────────────────────────
    why = auto_refusal(acct)            # re-checked: never trust the row alone
    if why:
        _finish(conn, tid, now, outcome=f"card (auto refused: {why})"[:300])
        _alert(alert, f"{head} — NOT placed automatically ({why}); the "
                      "approval card is waiting for you")
        return {"result": "fired", "id": tid, "work_item_id": wi,
                "outcome": "card"}
    return _auto_approve(conn, market, t, acct, wi, row, price, q, head,
                         resume_post=resume_post, alert=alert, now=now,
                         env=env)


def size_for_shares(qty: Decimal, quote: Decimal, fx: Decimal, whole: bool):
    """The spend (account currency) that execution turns back into `qty`
    shares. Execution sizes a buy as floor(size / live price), so the amount
    is qty plus HALF a share-step at the quote it fired on: the same price
    gives exactly qty, and a small move either way within that half step
    still does. Rounded UP to the cent, like the approval card's prefill."""
    step = Decimal(1) if whole else Decimal("0.1")
    size = (qty + step / 2) * quote * fx
    return size.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def _auto_approve(conn, market, t, acct, wi, row, price, q, head, *,
                  resume_post, alert, now, env):
    from . import approvals
    tid = t["id"]
    payload = {"work_item_id": wi, "action": "approve",
               "token": row["approval_token"], "broker": t["broker"],
               "trail_pct": t["trail_pct"]}
    if t["direction"] == "sell":
        payload["qty"] = float(from_micro(t["qty"]))
    elif t["amount_base"] is not None:
        payload["size_base"] = float(from_micro(t["amount_base"]))
    else:
        try:
            q_ccy = q.get("currency") or acct["base_currency"]
            fx = Decimal(1) if q_ccy == acct["base_currency"] else \
                Decimal(str(market.fx(q_ccy, acct["base_currency"])))
            if not fx.is_finite() or fx <= 0:
                raise ValueError(f"bad fx {fx}")
        except Exception as e:                  # noqa: BLE001 — honest: ask
            reason = f"no {q.get('currency')}→{acct['base_currency']} rate " \
                     f"to size it ({str(e)[:80]})"
            _finish(conn, tid, now, outcome=f"card (auto: {reason})"[:300])
            _alert(alert, f"{head} — could not place automatically "
                          f"({reason}); the approval card is waiting")
            return {"result": "fired", "id": tid, "work_item_id": wi,
                    "outcome": "card"}
        payload["size_base"] = float(size_for_shares(
            from_micro(t["qty"]), price, fx, whole_shares(acct, env)))
    # the audit trail says WHO approved and WHEN: the human, when they
    # created this rule — not the engine. Written before the approval so
    # it precedes thesis_action's own authorize:approve row.
    conn.execute(
        "INSERT INTO events (item_id, ts, actor, from_state, to_state,"
        " payload) VALUES (?,?,?,?,?,?)",
        (wi, now, "human", "awaiting_approval", "pre_approved", json.dumps({
            "pre_approved_by": t["created_by"],
            "pre_approved_at": t["created_at"],
            "pre_approved_at_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(t["created_at"])),
            "trigger_id": tid, "rule": describe(t),
            "fired_price": str(price), "mode": "auto",
            "approved_by_engine_on_behalf_of": "human"})))
    try:
        res = approvals.thesis_action(conn, payload, resume_post,
                                      clock=lambda: now)
    except approvals.AuthError as e:
        # refused before the resume (cash, a broker rule) → the card is
        # still awaiting the human; refused AT execution (price band, risk
        # caps, the venue) → it waits under "Approved but not placed".
        _finish(conn, tid, now, outcome=f"refused: {e.message}"[:300])
        _alert(alert, f"{head} — automatic placement refused: "
                      f"{e.message[:160]}. See Approvals.")
        return {"result": "fired", "id": tid, "work_item_id": wi,
                "outcome": "refused", "reason": e.message[:200]}
    _finish(conn, tid, now, outcome="placed")
    _alert(alert, f"{head} — order placed automatically ({describe(t)}, "
                  f"pre-approved by you)")
    return {"result": "fired", "id": tid, "work_item_id": wi,
            "outcome": "placed", "state": res.get("state")}
