"""Starting trades from Telegram (phase 1 + 2), on Claire's own bot.

Every command ends at the SAME approval gate as the dashboard: nothing here
places, sizes or confirms an order. A command either
  * starts a research run (/analyze → claire-api /api/run), whose card
    reaches Telegram through the pusher if the desk recommends a trade, or
  * puts a trade you decided on in front of the gate (/buy, /sell, the
    position buttons → /internal/manual, exactly what the Markets "Buy now"
    and Portfolio "Sell" buttons do), whose card then arrives like any
    other — two-step ✔, LIVE typed for real money, all checks unchanged, or
  * cancels a working entry order (/orders → [Cancel] → ✔ → /internal/order),
    the Orders page's Cancel, behind its own confirmation tap.

Rules (on top of the bot's: approver user id, private chat only):
* /analyze is rate-limited (RUNS_PER_HOUR); manual trades are de-duplicated
  by the desk itself (a second /buy CSCO while one is pending is refused).
* /sell only sells what the desk holds — Telegram never opens a short.
* No command ever suggests the real-money account for a BUY; a /sell of a
  live holding names it, and the card then demands LIVE as usual.
* Buttons carry an opaque, expiring token (rb:<token>), never ids or tokens
  of the desk; an expired one just says "run the command again".
"""
import json
import logging
import math
import secrets
import threading
import time
from collections import deque
from html import escape

import httpx

from . import exchanges, portfolio, telegram_approvals, watcher
from .tools.market import desk_ticker, normalize_price, yahoo_symbol

log = logging.getLogger(__name__)

CB_PREFIX = "rb"
CCY = exchanges.CCY                      # one registry (app/exchanges.py)
DEFAULT_EXCHANGE = "NASDAQ"
RUNS_PER_HOUR = 10
BUTTON_TTL_S = 3600
MAX_LIST = 10
SOURCE = "telegram"
RUN_TRIGGER = "started from Telegram (/analyze)"
FLOOR_CHOICES = telegram_approvals.FLOOR_CHOICES


def parse_floor(tok):
    """'trail=10' / 'floor=10' / '10%' → 10.0; None if tok is not a floor."""
    t = tok.lower()
    for pre in ("trail=", "floor="):
        if t.startswith(pre):
            t = t[len(pre):].rstrip("%")
            break
    else:
        if not t.endswith("%"):
            return None
        t = t[:-1]
    try:
        v = float(t)
    except ValueError:
        raise CommandError(f"not a trailing alert %: {tok}")
    if not math.isfinite(v) or not 0.5 <= v <= 90:
        raise CommandError("the trailing alert must be between 0.5% and 90%")
    return v

HELP = (
    "<b>Claire desk</b> — every trade still needs your ✔ on its card.\n\n"
    "/analyze <code>TICKER [EXCHANGE]</code> — research it; a card comes"
    " here if the desk recommends a trade\n"
    "/buy <code>TICKER [EXCHANGE] [SIZE] [trail=PCT]</code> — buy at the"
    " current price (stop-loss and take-profit suggested; size in the"
    " account currency; trail = the trailing %, default 8%, also on the"
    " card — on a Saxo account it is a trailing stop that sells by itself,"
    " elsewhere an alert)\n"
    "/sell <code>TICKER [all|half|QTY]</code> — sell shares the desk holds\n"
    "/positions — what the desk holds, with Sell buttons\n"
    "/orders — working entry orders, with Cancel buttons\n"
    "/floor <code>[TICKER] [PCT]</code> — see or change the trailing alert"
    " of a position you hold (it alerts you, never sells; a trailing stop"
    " already at Saxo keeps its own distance)\n"
    "/id — your Telegram user id\n\n"
    f"Exchanges: {', '.join(CCY)} (default {DEFAULT_EXCHANGE}, or the one the"
    " desk already knows the ticker on).")


def h(x):
    return escape(str(x), quote=False)


def num(x, d=2):
    try:
        return f"{float(x):,.{d}f}"
    except (TypeError, ValueError):
        return "—"


def qty_s(q):
    """Shares without noise: 9, 9.5, 0.1234."""
    return f"{float(q):,.4f}".rstrip("0").rstrip(".")


class CommandError(Exception):
    """Shown to the user as-is (plain text, escaped)."""


# ── claire-api client (injectable for tests) ───────────────────────────
class DeskApi:
    def __init__(self, api_base, secret):
        self.base, self.secret = api_base, secret

    def _post(self, path, body, timeout=30):
        try:
            r = httpx.post(f"{self.base}{path}", json=body, timeout=timeout,
                           headers={"x-claire-secret": self.secret})
        except Exception as e:                  # noqa: BLE001
            return 502, {"detail": f"claire-api unreachable ({e})"}
        try:
            out = r.json()
        except ValueError:
            out = {}
        return r.status_code, out

    def start_run(self, body):
        return self._post("/api/run", body, timeout=10)

    def manual(self, body):
        return self._post("/internal/manual", {**body, "actor": "human"})

    def cancel_order(self, work_item_id):
        return self._post("/internal/order", {"work_item_id": work_item_id,
                                              "actor": "human",
                                              "action": "cancel"}, timeout=60)


def _detail(out, fallback):
    d = (out or {}).get("detail") or (out or {}).get("error") or fallback
    if isinstance(d, list):                     # pydantic 422 shape
        d = "; ".join(str(x.get("msg")) for x in d if isinstance(x, dict))
    return str(d)[:300]


class Commands:
    """State: issued button tokens and the /analyze rate window. One
    instance per bot thread; thread-safe for the pusher's nudge."""

    def __init__(self, conn, market, env, desk_api, *, clock=time.time,
                 nudge=None):
        self.conn, self.market, self.env = conn, market, env
        self.api, self.clock = desk_api, clock
        self.nudge = nudge or (lambda: None)   # wake the card pusher
        self._buttons = {}
        self._runs = deque()
        self._lock = threading.Lock()

    # ── buttons ──────────────────────────────────────────────────────────
    def button(self, text, action):
        tok = secrets.token_urlsafe(6)
        now = self.clock()
        with self._lock:
            for k in [k for k, (_, exp) in self._buttons.items() if exp < now]:
                del self._buttons[k]
            self._buttons[tok] = (action, now + BUTTON_TTL_S)
        return {"text": text, "callback_data": f"{CB_PREFIX}:{tok}"}

    def _take(self, tok, *, keep=False):
        with self._lock:
            got = self._buttons.get(tok)
            if not got or got[1] < self.clock():
                self._buttons.pop(tok, None)
                return None
            if not keep:
                del self._buttons[tok]
            return got[0]

    # ── entry points (return [(html, keyboard|None)]) ───────────────────
    def command(self, text):
        parts = text.strip().split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        try:
            if cmd in ("/start", "/help"):
                return [(HELP, None)]
            if cmd == "/analyze":
                return self.analyze(args)
            if cmd == "/buy":
                return self.buy(args)
            if cmd == "/sell":
                return self.sell(args)
            if cmd == "/positions":
                return self.positions()
            if cmd == "/orders":
                return self.orders()
            if cmd == "/floor":
                return self.floor(args)
        except CommandError as e:
            return [(f"✖ {h(e)}", None)]
        return [("Unknown command — /help lists them.", None)]

    def callback(self, data):
        """(answer text, [(html, keyboard)] to send) for an rb: button."""
        tok = data.split(":", 1)[1] if ":" in data else ""
        a = self._take(tok)
        if a is None:
            return "That button has expired — run the command again.", []
        try:
            op = a["op"]
            if op == "analyze":
                return "Starting…", self._start_run(a["ticker"], a["exchange"])
            if op == "buy":
                return "Sending…", self._buy(a["ticker"], a["exchange"],
                                             a.get("size"), a.get("trail"))
            if op == "floor_pick":
                return "Pick a floor.", self._floor_pick(a["pos"])
            if op == "floor_ask":
                return "Confirm below.", self._floor_ask(a["pos"], a["pct"])
            if op == "floor_ok":
                return "Saved.", self._floor_set(a["pos"], a["pct"])
            if op == "sell":
                return "Sending…", self._sell(a["pos"], a["mode"])
            if op == "cancel_ask":
                return "Confirm below.", self._cancel_ask(a)
            if op == "cancel_ok":
                return "Cancelling…", self._cancel(a)
            if op == "noop":
                return a.get("say", "OK."), []
        except CommandError as e:
            return str(e)[:190], [(f"✖ {h(e)}", None)]
        return "Unknown button.", []

    # ── instrument resolution ───────────────────────────────────────────
    def resolve(self, ticker, exchange, op, extra=None):
        """(ticker, exchange) or raise _Choose with buttons per exchange."""
        if exchange:
            exchange = exchange.upper()
            if exchange not in CCY:
                raise CommandError(f"unknown exchange {exchange} — one of "
                                   f"{', '.join(CCY)}")
            return desk_ticker(ticker.upper(), exchange), exchange
        t = ticker.upper()
        known = [r["exchange"] for r in self.conn.execute(
            "SELECT DISTINCT exchange FROM instruments WHERE ticker=?",
            (t,)) if r["exchange"] in CCY]
        if len(known) == 1:
            return desk_ticker(t, known[0]), known[0]
        if len(known) > 1:
            kb = [[self.button(ex, {"op": op, "ticker": desk_ticker(t, ex),
                                    "exchange": ex, **(extra or {})})
                   for ex in known]]
            raise _Choose(f"{h(t)} is on more than one exchange — which?", kb)
        return t, DEFAULT_EXCHANGE

    def _price(self, ticker, exchange):
        sym = yahoo_symbol(ticker, exchange)
        try:
            q = self.market.quote(sym)
        except Exception as e:                  # noqa: BLE001
            raise CommandError(f"no price for {sym} right now ({e})"[:200])
        if not q or q.get("price") is None:
            raise CommandError(f"no price for {sym} right now")
        p, _ = normalize_price(q["price"], q.get("currency") or "")
        return float(p)

    @staticmethod
    def _round(v, price):
        f = 1e4 if price < 1 else 100
        return math.floor(v * f + 0.5) / f

    def _atr14(self, ticker, exchange, price=None):
        try:
            c = self.market.chart(yahoo_symbol(ticker, exchange), "3mo", "1d")
        except Exception:                       # noqa: BLE001 — 8% fallback
            return None
        H, L, C = c.get("high") or [], c.get("low") or [], c.get("close") or []
        tr = [max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))
              for i in range(1, len(C))
              if None not in (H[i], L[i], C[i - 1])]
        last = tr[-14:]
        if len(last) < 5:
            return None
        atr = sum(last) / len(last)
        # London bars come in pence while the price is in pounds
        cl = next((x for x in reversed(C) if x), None)
        if price and cl and 30 < cl / price < 300:
            atr /= 100
        return atr

    # ── /analyze ─────────────────────────────────────────────────────────
    def analyze(self, args):
        if not args:
            raise CommandError("usage: /analyze TICKER [EXCHANGE]")
        try:
            t, ex = self.resolve(args[0], args[1] if len(args) > 1 else None,
                                 "analyze")
        except _Choose as c:
            return [(c.html, c.kb)]
        return self._start_run(t, ex)

    def _start_run(self, ticker, exchange):
        now = self.clock()
        with self._lock:
            while self._runs and self._runs[0] < now - 3600:
                self._runs.popleft()
            if len(self._runs) >= RUNS_PER_HOUR:
                raise CommandError(f"at most {RUNS_PER_HOUR} runs an hour from"
                                   " Telegram — try again later")
            self._runs.append(now)
        code, out = self.api.start_run({
            "ticker": ticker, "exchange": exchange, "currency": CCY[exchange],
            "lot_size": exchanges.lot_size(exchange),
            "trigger": RUN_TRIGGER})
        if code != 200:
            with self._lock:
                if self._runs and self._runs[-1] == now:
                    self._runs.pop()
            raise CommandError(f"could not start the run: "
                               f"{_detail(out, f'HTTP {code}')}")
        return [(f"🔎 Researching <b>{h(ticker)}</b> ({h(exchange)}) — run "
                 f"<code>{h(out.get('work_item_id'))}</code>. If the desk "
                 "recommends a trade, its approval card comes here; if it "
                 "passes, I'll say why.", None)]

    # ── /buy ─────────────────────────────────────────────────────────────
    def buy(self, args):
        if not args:
            raise CommandError("usage: /buy TICKER [EXCHANGE] [SIZE] "
                               "[trail=PCT]")
        ex, size, trail = None, None, None
        for a in args[1:]:
            fl = parse_floor(a)
            if fl is not None:
                trail = fl
                continue
            if a.upper() in CCY:
                ex = a.upper()
                continue
            try:
                size = float(a.replace(",", ""))
            except ValueError:
                raise CommandError(f"not an exchange or a size: {a}")
            if not math.isfinite(size) or size <= 0 or size > 1e9:
                raise CommandError("size must be a positive amount")
        try:
            t, ex = self.resolve(args[0], ex, "buy",
                                 {"size": size, "trail": trail})
        except _Choose as c:
            return [(c.html, c.kb)]
        return self._buy(t, ex, size, trail)

    def _buy(self, ticker, exchange, size, trail=None):
        price = self._price(ticker, exchange)
        atr = self._atr14(ticker, exchange, price)
        r = min(2 * atr if atr else price * 0.08, price * 0.24)
        stop = self._round(price - r, price)
        target = self._round(price + min(2 * r, price * 0.24), price)
        sug = {"order_type": "market"}
        if size:
            sug["size_base"] = size
        if trail:
            sug["trail_pct"] = trail
        code, out = self.api.manual({
            "ticker": ticker, "exchange": exchange, "direction": "buy",
            "entry": self._round(price, price), "stop": stop,
            "target": target, "source": SOURCE, "suggested": sug})
        if code != 200:
            raise CommandError(f"BUY {ticker} not sent: "
                               f"{_detail(out, f'HTTP {code}')}")
        self.nudge()
        how = "2 × the daily move" if atr else "8% — no chart data"
        return [(f"🛒 <b>BUY {h(ticker)}</b> ({h(exchange)}) is on its way to"
                 f" approval at the current price ≈ {num(price)}"
                 f" {h(CCY[exchange])}. Suggested stop-loss {num(stop)}, take-profit "
                 f"{num(target)} ({how}, 2:1)"
                 + (f", size {num(size, 0)}" if size else "")
                 + f", trail {trail or telegram_approvals.DEFAULT_TRAIL_PCT:g}%"
                 + ("" if trail else " (default — change it on the card)")
                 + ". On a Saxo account the trail is a trailing stop that "
                 "sells by itself; elsewhere it is an alert — the card says "
                 "which. It follows in a few seconds — nothing trades "
                 "until you tap ✔.", None)]

    # ── /sell and /positions ─────────────────────────────────────────────
    def _held(self, ticker=None):
        out = []
        for p in portfolio.open_positions(self.conn):
            if p["side"] != "long" or (ticker and p["ticker"] != ticker):
                continue
            out.append({k: p[k] for k in ("account_id", "broker", "ticker",
                                          "exchange", "qty", "currency",
                                          "instrument_id")})
        return out

    def sell(self, args):
        if not args:
            raise CommandError("usage: /sell TICKER [all|half|QTY]")
        t = args[0].upper()
        mode = (args[1].lower() if len(args) > 1 else "all")
        if mode not in ("all", "half"):
            try:
                q = float(mode.replace(",", ""))
            except ValueError:
                raise CommandError("say all, half or a number of shares")
            if not math.isfinite(q) or q <= 0:
                raise CommandError("the quantity must be positive")
        held = self._held(t) or [p for p in self._held()
                                 if p["ticker"] == desk_ticker(t, p["exchange"])]
        if not held:
            raise CommandError(f"the desk holds no {t} — Telegram only sells "
                               "what the desk bought (no shorts)")
        if len(held) > 1:
            kb = [[self.button(f"{p['account_id']} · {qty_s(p['qty'])}",
                               {"op": "sell", "pos": p, "mode": mode})]
                  for p in held]
            return [(f"{h(t)} is held on more than one account — which?", kb)]
        return self._sell(held[0], mode)

    @staticmethod
    def _whole(p):
        return p["broker"] in ("saxo", "saxo_live", "moomoo")

    def _qty(self, p, mode):
        held = float(p["qty"])
        lot = exchanges.lot_size(p["exchange"])
        if mode == "all":
            return held
        if mode == "half":
            if lot > 1:
                q = math.floor(held / 2 / lot) * lot
            elif self._whole(p):
                q = math.floor(held / 2)
            else:
                q = math.floor(held * 5) / 10
            if q <= 0:
                raise CommandError(f"half of {num(held, 4)} rounds to 0 — "
                                   "sell all instead")
            return float(q)
        q = float(mode.replace(",", ""))
        if q > held + 1e-9:
            raise CommandError(f"the desk holds only {num(held, 4)} "
                               f"{p['ticker']} on {p['account_id']}")
        if (self._whole(p) or lot > 1) and (q != int(q) or int(q) % lot):
            raise CommandError(f"{p['account_id']} trades whole lots of "
                               f"{lot} — pick a quantity that fits")
        return q

    def _sell(self, p, mode):
        qty = self._qty(p, mode)
        price = self._price(p["ticker"], p["exchange"])
        code, out = self.api.manual({
            "ticker": p["ticker"], "exchange": p["exchange"],
            "direction": "sell", "entry": self._round(price, price),
            "source": SOURCE,
            "suggested": {"qty": qty, "broker": p["broker"]}})
        if code != 200:
            raise CommandError(f"SELL {p['ticker']} not sent: "
                               f"{_detail(out, f'HTTP {code}')}")
        self.nudge()
        live = p["broker"] == "saxo_live"
        return [(f"💰 <b>SELL {qty_s(qty)} "
                 f"{h(p['ticker'])}</b> from {h(p['account_id'])} is on its "
                 f"way to approval, sale price ≈ {num(price)} {h(p['currency'])}."
                 + (" ⚠️ REAL MONEY — the card will ask for LIVE." if live
                    else "")
                 + " The card follows in a few seconds — nothing is sold "
                 "until you tap ✔.", None)]

    def positions(self):
        held = self._held()
        if not held:
            return [("The desk holds no positions.", None)]
        syms = sorted({yahoo_symbol(p["ticker"], p["exchange"]) for p in held})
        try:
            quotes = self.market.spark(syms)
        except Exception:                       # noqa: BLE001 — best effort
            quotes = {}
        lines, kb = [], []
        for p in held[:MAX_LIST]:
            q = quotes.get(yahoo_symbol(p["ticker"], p["exchange"])) or {}
            last = None
            if q.get("price") is not None:
                last = float(normalize_price(q["price"],
                                             q.get("currency") or "")[0])
            lines.append(
                f"• <b>{h(p['ticker'])}</b> {h(p['exchange'])} — "
                f"{qty_s(p['qty'])} on "
                f"{h(p['account_id'])}"
                + (f" · last {num(last)} {h(p['currency'])}" if last else ""))
            kb.append([self.button(f"Sell half {p['ticker']}",
                                   {"op": "sell", "pos": p, "mode": "half"}),
                       self.button(f"Sell all {p['ticker']}",
                                   {"op": "sell", "pos": p, "mode": "all"})])
        more = (f"\n…and {len(held) - MAX_LIST} more on the dashboard."
                if len(held) > MAX_LIST else "")
        return [("<b>Positions</b>\n" + "\n".join(lines) + more +
                 "\n\nA Sell button only puts the trade on approval.", kb)]

    # ── /floor ───────────────────────────────────────────────────────────
    def floor(self, args):
        pct = None
        if len(args) > 1:
            pct = parse_floor(args[1] if args[1].endswith("%") or "=" in args[1]
                              else args[1] + "%")
        held = self._held(args[0].upper()) if args else self._held()
        if args and not held:
            raise CommandError(f"the desk holds no {args[0].upper()}")
        if not held:
            return [("The desk holds no positions — a trailing alert is set"
                     " when you buy (on the card, or /buy … trail=10).", None)]
        if len(held) > 1:
            lines, kb = [], []
            for p in held[:MAX_LIST]:
                cur, _ = watcher.position_floor(self.conn, p["instrument_id"],
                                                p["account_id"])
                lines.append(f"• <b>{h(p['ticker'])}</b> on "
                             f"{h(p['account_id'])} — trailing alert {cur:g}%")
                act = ({"op": "floor_ask", "pos": p, "pct": pct} if pct
                       else {"op": "floor_pick", "pos": p})
                kb.append([self.button(f"{p['ticker']} · {p['account_id']}",
                                       act)])
            return [("<b>Trailing alerts</b>\n" + "\n".join(lines) +
                     "\n\nWhich position?", kb)]
        if pct:
            return self._floor_ask(held[0], pct)
        return self._floor_pick(held[0])

    SAXO_TRAILS = (" <i>Saxo holds a trailing stop on this position: this "
                   "changes only the desk's backup alert — the stop at Saxo "
                   "keeps its distance.</i>")

    def _saxo_note(self, p):
        """Claire only: a buy on a Saxo account may have left a trailing
        STOP there (portfolio.legs_for marks it `trails`). /floor does not
        touch that order — same warning as Portfolio's Edit."""
        legs = portfolio.legs_for(self.conn, p["account_id"],
                                  p["instrument_id"])
        return self.SAXO_TRAILS if any(l.get("trails") for l in legs) else ""

    def _floor_pick(self, p):
        cur, peak = watcher.position_floor(self.conn, p["instrument_id"],
                                           p["account_id"])
        choices = sorted({float(x) for x in FLOOR_CHOICES} | {cur})
        kb = [[self.button(("✓ " if v == cur else "") + f"{v:g}%",
                           {"op": "floor_ask", "pos": p, "pct": v})
               for v in choices]]
        return [(f"<b>{h(p['ticker'])}</b> on {h(p['account_id'])}: trailing "
                 f"alert <b>{cur:g}%</b> — the watcher alerts you (and starts"
                 " a sell review) if the price falls this far below its best "
                 "since you bought; it never sells. Pick a new %, or type "
                 f"<code>/floor {h(p['ticker'])} 10</code> for any value."
                 + self._saxo_note(p), kb)]

    def _floor_ask(self, p, pct):
        cur, _ = watcher.position_floor(self.conn, p["instrument_id"],
                                        p["account_id"])
        if pct == cur:
            return [(f"{h(p['ticker'])}'s trailing alert is already {cur:g}%.", None)]
        kb = [[self.button(f"✔ Set {pct:g}%", {"op": "floor_ok", "pos": p,
                                               "pct": pct}),
               self.button("✖ Keep it", {"op": "noop",
                                         "say": "Kept — nothing changed."})]]
        return [(f"Change <b>{h(p['ticker'])}</b>'s trailing alert on "
                 f"{h(p['account_id'])} from {cur:g}% to <b>{pct:g}%</b>?"
                 + self._saxo_note(p), kb)]

    def _floor_set(self, p, pct):
        try:
            before = watcher.set_position_floor(
                self.conn, p["instrument_id"], p["account_id"], pct)
        except ValueError as e:
            raise CommandError(str(e))
        log.info("telegram: floor %s on %s %g%% -> %g%% (owner via Telegram)",
                 p["ticker"], p["account_id"], before, pct)
        return [(f"✔ <b>{h(p['ticker'])}</b> trailing alert on "
                 f"{h(p['account_id'])}: {before:g}% → <b>{pct:g}%</b>. It "
                 "still trails from the best price since you bought."
                 + self._saxo_note(p), None)]

    # ── /orders ──────────────────────────────────────────────────────────
    def _open_orders(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT o.work_item_id, o.side, o.qty/1e6 qty,"
            " o.limit_price/1e6 limit_price, o.status, o.account_id,"
            " i.ticker, i.exchange, i.currency,"
            " (SELECT COUNT(*) FROM orders x WHERE x.parent_order_id=o.id"
            "  AND x.status IN ('pending_session','placed',"
            "  'partially_filled')) legs"
            " FROM orders o JOIN instruments i ON i.id=o.instrument_id"
            " WHERE o.leg IS NULL AND o.status IN ('pending_session',"
            " 'placed','partially_filled') ORDER BY o.created_at DESC")]

    @staticmethod
    def _order_line(o):
        where = {"pending_session": "queued for the open",
                 "placed": "working at the broker",
                 "partially_filled": "part filled"}.get(o["status"],
                                                        o["status"])
        return (f"{h(o['side'].upper())} {qty_s(o['qty'])}"
                f" <b>{h(o['ticker'])}</b> @ {num(o['limit_price'])} "
                f"{h(o['currency'])} · {where} · {h(o['account_id'])}")

    def orders(self):
        rows = self._open_orders()
        if not rows:
            return [("No working entry orders.", None)]
        lines, kb = [], []
        for o in rows[:MAX_LIST]:
            lines.append("• " + self._order_line(o))
            kb.append([self.button(f"Cancel {o['side']} {o['ticker']}",
                                   {"op": "cancel_ask", "order": o})])
        return [("<b>Working orders</b>\n" + "\n".join(lines) +
                 "\n\nCancel asks once more before it does anything.", kb)]

    def _cancel_ask(self, a):
        o = a["order"]
        kb = [[self.button("✔ Cancel it", {"op": "cancel_ok", "order": o}),
               self.button("✖ Keep it", {"op": "noop",
                                         "say": "Kept — nothing changed."})]]
        return [("<b>Cancel</b> " + self._order_line(o) + "?", kb)]

    def _cancel(self, a):
        o = a["order"]
        code, out = self.api.cancel_order(o["work_item_id"])
        if code != 200:
            raise CommandError(f"not cancelled: {_detail(out, f'HTTP {code}')}")
        return [(f"✔ Cancelled {h(o['side'].upper())} <b>{h(o['ticker'])}</b>"
                 f" (<code>{h(o['work_item_id'])}</code>).", None)]

    # ── run outcomes for /analyze ───────────────────────────────────────
    def finished_runs(self):
        """Telegram-started runs that ended without a card: [(wi, html)],
        each reported once (telegram_run_notices)."""
        telegram_approvals.ensure_table(self.conn)
        self.conn.execute("CREATE TABLE IF NOT EXISTS telegram_run_notices ("
                          " work_item_id TEXT PRIMARY KEY, sent_at INTEGER)")
        since = int(self.clock()) - 2 * 86400
        out = []
        for w in self.conn.execute(
                "SELECT w.id, w.ticker, w.state, w.thesis_json FROM work_items"
                " w WHERE w.trigger=? AND w.created_at > ? AND w.state IN"
                " ('done','failed','expired','rejected') AND NOT EXISTS"
                " (SELECT 1 FROM telegram_run_notices n WHERE"
                " n.work_item_id=w.id) AND NOT EXISTS (SELECT 1 FROM"
                " telegram_notices t WHERE t.work_item_id=w.id)",
                (RUN_TRIGGER, since)).fetchall():
            out.append((w["id"], self._outcome_html(w)))
        return out

    def mark_reported(self, wi):
        self.conn.execute("INSERT OR IGNORE INTO telegram_run_notices"
                          " VALUES (?,?)", (wi, int(self.clock())))

    def _outcome_html(self, w):
        th = {}
        r = self.conn.execute("SELECT payload FROM agent_reports WHERE"
                              " work_item_id=? AND agent_id='arbiter'"
                              " ORDER BY id DESC LIMIT 1", (w["id"],)).fetchone()
        try:
            th = json.loads((r and r["payload"]) or w["thesis_json"] or "{}")
        except ValueError:
            pass
        if w["state"] == "failed":
            return (f"⚠️ The run on <b>{h(w['ticker'])}</b> failed — see "
                    f"<code>{h(w['id'])}</code> on the Trading page.")
        d = str(th.get("direction") or "").lower()
        why = next(iter(th.get("conditions") or []), "")
        why = why.replace("PASS rationale:", "").strip()
        why = (why[:400] + "…") if len(why) > 400 else why
        if d == "pass" or not d:
            head = f"🟰 <b>{h(w['ticker'])}: the desk passes</b>"
        else:
            head = (f"<b>{h(w['ticker'])}</b>: {h(d.upper())} — ended "
                    f"{h(w['state'])} without a card")
        conv = th.get("conviction")
        return (head + (f" (conviction {num(conv)})" if conv is not None
                        else "") + (f"\n{h(why)}" if why else "")
                + f"\n<code>{h(w['id'])}</code>")


class _Choose(Exception):
    def __init__(self, html, kb):
        super().__init__(html)
        self.html, self.kb = html, kb
