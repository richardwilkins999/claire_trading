"""Saxo OpenAPI broker adapter — the same interface as PaperSimBroker
(place_bracket / order_status / cancel / snapshot), against Saxo's SIM or
LIVE environment (DESIGN.md §22).

Placement is the only dangerous call, so it is the one that is guarded:
- `environment="live"` adapters call `guard()` before every placement and
  return a definitive REJECTED (never raise) when it refuses — a refusal is
  certain, unlike a network failure, and must not be flagged "unknown".
- cancels are always allowed: they only ever reduce risk.

Instrument resolution never guesses. The desk's Yahoo-style symbol
(D05.SI, AAPL, 7203.T) is mapped to Saxo's "TICKER:mic" symbol and the
search result must match it EXACTLY; anything else refuses the order.
"""
import json
import time
import uuid
from urllib.parse import quote
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import httpx

from .brokers import BrokerError, PlacedOrder

API = {"sim": "https://gateway.saxobank.com/sim/openapi",
       "live": "https://gateway.saxobank.com/openapi"}

# Yahoo suffix -> Saxo market identifier code(s), in preference order
MIC_BY_SUFFIX = {
    "": ("xnas", "xnys"), "SI": ("xses",), "HK": ("xhkg",), "T": ("xtks",),
    "AX": ("xasx",), "L": ("xlon",), "DE": ("xetr",), "PA": ("xpar",),
    "NS": ("xnse",),
}

# Saxo order-activity Status -> the desk's order status vocabulary
_DONE = {"Cancelled", "Expired", "DoneForDay"}

# Saxo throttles the order endpoints (place / cancel / precheck) to about one
# request a second per session. A desk sell is three back-to-back calls
# (cancel stop, cancel target, place), so they are spaced rather than left to
# trip a 429 halfway through — a refused cancel would refuse the sell.
ORDER_GAP_S = 1.1


def leg_kind(order_type):
    """Saxo OrderType of a protective order -> the desk's leg name. A stop
    is any stop flavour; a resting Limit on the exit side is the target."""
    t = (order_type or "")
    if t.startswith("Stop") or t.startswith("Trailing"):
        return "stop"
    if t == "Limit":
        return "target"
    return None


def split_symbol(symbol: str):
    """'D05.SI' -> ('D05', 'SI'); 'AAPL' -> ('AAPL', ''); 'BRK-B' stays."""
    if "." in symbol:
        t, suffix = symbol.rsplit(".", 1)
        if suffix in MIC_BY_SUFFIX:
            return t, suffix
    return symbol, ""


def desk_symbol(saxo_symbol):
    """'D05:xses' -> 'D05.SI', 'AAPL:xnas' -> 'AAPL' — the reverse of
    split_symbol + MIC_BY_SUFFIX, for charting a Saxo position with the
    desk's own market data. None when the market is not one the desk maps."""
    if not saxo_symbol or ":" not in saxo_symbol:
        return None
    ticker, mic = saxo_symbol.rsplit(":", 1)
    for suffix, mics in MIC_BY_SUFFIX.items():
        if mic.lower() in mics:
            if suffix == "HK" and ticker.isdigit():   # Saxo 00700 -> 0700.HK
                ticker = ticker.lstrip("0").zfill(4)
            return ticker + (f".{suffix}" if suffix else "")
    return None


def _tick_for(details, price: Decimal) -> Decimal:
    scheme = details.get("TickSizeScheme")
    if scheme and scheme.get("Elements"):
        for el in sorted(scheme["Elements"], key=lambda e: e["HighPrice"]):
            if price <= Decimal(str(el["HighPrice"])):
                return Decimal(str(el["TickSize"]))
        return Decimal(str(scheme.get("DefaultTickSize")
                           or scheme["Elements"][-1]["TickSize"]))
    return Decimal(str(details.get("TickSize") or "0.01"))


def round_price(details, price, *, direction):
    """Round onto Saxo's tick grid in the SAFE direction: 'down' never pays
    more than approved (buy limit, sell stop), 'up' never sells for less
    (sell limit, buy stop). Saxo rejects off-grid prices outright."""
    p = Decimal(str(price))
    tick = _tick_for(details, p)
    mode = ROUND_FLOOR if direction == "down" else ROUND_CEILING
    out = (p / tick).to_integral_value(rounding=mode) * tick
    decimals = (details.get("Format") or {}).get("OrderDecimals")
    if decimals is not None:
        out = out.quantize(Decimal(1).scaleb(-int(decimals)), rounding=mode)
    return float(out)


class SaxoBroker:
    fractional = False                  # Saxo equities trade whole shares

    def __init__(self, environment, *, token, account_key, guard=None,
                 client=None, order_gap_s=ORDER_GAP_S, sleep=time.sleep,
                 monotonic=time.monotonic):
        if environment not in ("sim", "live"):
            raise BrokerError(f"unknown Saxo environment {environment!r}")
        self.environment = environment
        self._token = token                 # () -> bearer token
        self.account_key = account_key
        self._guard = guard                 # () -> None | raises (live only)
        self._client = client or httpx.Client(timeout=20)
        self._uic = {}                      # symbol -> (uic, details)
        self._orders = {}                   # broker order id -> placement info
        self._gap, self._sleep, self._mono = order_gap_s, sleep, monotonic
        self._last_order_call = None

    # ── HTTP ─────────────────────────────────────────────────────────────
    def _req(self, method, path, *, body=None, request_id=None):
        if method != "GET" and path.startswith("/trade/v2/orders"):
            self._throttle()
        headers = {"Authorization": f"Bearer {self._token()}"}
        if request_id:
            # Saxo rejects an identical order inside 15s unless the request
            # id differs; a REPLAY with the same id is thereby deduplicated
            headers["x-request-id"] = request_id
        r = self._client.request(method, API[self.environment] + path,
                                 json=body, headers=headers)
        if r.status_code >= 400:
            try:
                info = r.json().get("ErrorInfo") or r.json()
            except ValueError:
                info = r.text[:300]
            raise BrokerError(f"Saxo {method} {path.split('?')[0]}: HTTP "
                              f"{r.status_code} {json.dumps(info)[:300]}")
        return r.json() if r.content else {}

    def _throttle(self):
        now = self._mono()
        if self._last_order_call is not None and self._gap:
            wait = self._last_order_call + self._gap - now
            if wait > 0:
                self._sleep(wait)
                now = self._mono()
        self._last_order_call = now

    # ── instruments ──────────────────────────────────────────────────────
    def resolve(self, symbol):
        if symbol in self._uic:
            return self._uic[symbol]
        ticker, suffix = split_symbol(symbol)
        if suffix == "HK" and ticker.isdigit():   # Yahoo 0700.HK, Saxo 00700
            ticker = ticker.zfill(5)
        wanted = [f"{ticker}:{mic}".lower() for mic in MIC_BY_SUFFIX[suffix]]
        found = self._req("GET", "/ref/v1/instruments/?Keywords="
                          f"{quote(ticker)}&AssetTypes=Stock&$top=50"
                          ).get("Data", [])
        by_sym = {(i.get("Symbol") or "").lower(): i for i in found}
        hit = next((by_sym[w] for w in wanted if w in by_sym), None)
        if hit is None:
            raise BrokerError(f"no exact Saxo instrument for {symbol} (wanted "
                              f"{' or '.join(wanted)}) — refusing to guess")
        uic = hit["Identifier"]
        details = self._req("GET", f"/ref/v1/instruments/details/{uic}/Stock")
        if details.get("IsTradable") is False:
            raise BrokerError(f"{symbol} is not tradable on this Saxo account")
        self._uic[symbol] = (uic, details)
        return uic, details

    def _bracket_body(self, *, symbol, side, qty, limit, stop_loss,
                      take_profit, trail_distance=None):
        """The exact Saxo order a placement sends: (body, None), or
        (None, rejected PlacedOrder) when it cannot be built. Shared by
        place_bracket and precheck, so a precheck tests the real thing."""
        if limit is None:
            return None, PlacedOrder("", "rejected",
                                     "Saxo adapter places limit orders only")
        try:
            uic, details = self.resolve(symbol)
        except BrokerError as e:
            return None, PlacedOrder("", "rejected", str(e))
        amount = Decimal(str(qty))
        lot = Decimal(str(details.get("LotSize") or 1))
        if amount <= 0 or amount != amount.to_integral_value() or \
                amount % lot:
            return None, PlacedOrder(
                "", "rejected", f"quantity {qty} is not a whole multiple of "
                                f"the Saxo lot size {lot}")
        buy = side == "buy"
        order = {
            "AccountKey": self.account_key, "Uic": uic, "AssetType": "Stock",
            "BuySell": "Buy" if buy else "Sell", "Amount": int(amount),
            "OrderType": "Limit",
            "OrderPrice": round_price(details, limit,
                                      direction="down" if buy else "up"),
            "OrderDuration": {"DurationType": "DayOrder"},
            "ManualOrder": False,
        }
        legs = self._protective(uic, details, protect_side="sell" if buy
                                else "buy", qty=int(amount),
                                stop_loss=stop_loss, take_profit=take_profit,
                                trail_distance=trail_distance)
        if legs:
            order["Orders"] = legs
        return order, None

    def _protective(self, uic, details, *, protect_side, qty, stop_loss,
                    take_profit, trail_distance=None):
        """The stop-loss and take-profit orders that protect a position —
        used both as a bracket's related legs and as a standalone OCO.
        `protect_side` is the side of these orders: 'sell' protects a long.
        Rounding is in the safe direction: a long's stop and a short's
        target round DOWN, the others UP (round_price).

        `trail_distance` (price units) makes the stop a Saxo TRAILING stop
        (TrailingStopIfTraded): it starts at `stop_loss` and follows the
        market at that distance, never moving back — the desk's trailing
        floor, held and executed at Saxo. The distance and its step are on
        the instrument's tick grid at the stop price (distance rounded UP:
        never tighter than asked)."""
        sell = protect_side == "sell"
        legs = []
        base = {"AssetType": "Stock", "Uic": uic,
                "AccountKey": self.account_key, "Amount": int(qty),
                "BuySell": "Sell" if sell else "Buy",
                "OrderDuration": {"DurationType": "GoodTillCancel"},
                "ManualOrder": False}
        if stop_loss:
            leg = {**base, "OrderType": "StopIfTraded",
                   "OrderPrice": round_price(
                       details, stop_loss, direction="down" if sell else "up")}
            if trail_distance:
                tick = _tick_for(details, Decimal(str(stop_loss)))
                dist = (Decimal(str(trail_distance)) / tick).to_integral_value(
                    rounding=ROUND_CEILING) * tick
                leg.update(OrderType="TrailingStopIfTraded",
                           TrailingStopDistanceToMarket=float(dist),
                           TrailingStopStep=float(tick))
            legs.append(leg)
        if take_profit:
            legs.append({**base, "OrderType": "Limit",
                         "OrderPrice": round_price(
                             details, take_profit,
                             direction="up" if sell else "down")})
        return legs

    def _order_info(self, order_id):
        """What Saxo's audit trail says an order IS (type, price, amount,
        side, related orders) — from its latest activity."""
        acts = self._req("GET", "/cs/v1/audit/orderactivities?OrderId="
                         f"{order_id}&EntryType=All").get("Data", [])
        if not acts:
            return None
        acts.sort(key=lambda a: (a.get("ActivityTime") or "",
                                 int(a.get("LogId") or 0)))
        a = acts[-1]
        return {"order_type": a.get("OrderType"), "price": a.get("Price"),
                "amount": a.get("Amount"), "buy_sell": a.get("BuySell"),
                "related": [str(x) for x in (a.get("RelatedOrders") or [])],
                "status": a.get("Status")}

    def _label_legs(self, ids, sent):
        """[{leg, order_id, price, verified}] for the protective orders Saxo
        just created. Saxo's response lists the new ids but NOT which is the
        stop: on SIM (2026-11-03) a bracket sent as [stop, target] came back
        with the target holding the lower id. So each id's type is read back
        from the audit trail; only if that read fails is the request order
        assumed, marked unverified — the custodian corrects it from the
        venue on its next poll, and re-protection refuses levels that do not
        make sense (protection.reprotect)."""
        legs = []
        for i, oid in enumerate(ids):
            if not oid:
                continue
            info = None
            try:
                info = self._order_info(oid)
            except Exception:                   # noqa: BLE001 — fall back
                info = None
            kind = leg_kind(info and info.get("order_type"))
            if kind is not None:
                legs.append({"leg": kind, "order_id": str(oid),
                             "price": info.get("price"), "verified": True})
            elif i < len(sent):
                legs.append({"leg": leg_kind(sent[i]["OrderType"]),
                             "order_id": str(oid),
                             "price": sent[i]["OrderPrice"],
                             "verified": False})
        return legs

    # ── the broker interface ─────────────────────────────────────────────
    def place_bracket(self, *, symbol, side, qty, limit=None, stop_loss=None,
                      take_profit=None, client_order_id=None,
                      trail_distance=None) -> PlacedOrder:
        if self.environment == "live" and self._guard is not None:
            try:
                self._guard()
            except Exception as e:              # noqa: BLE001 — definitive
                return PlacedOrder("", "rejected", f"live trading: {e}")
        order, refused = self._bracket_body(
            symbol=symbol, side=side, qty=qty, limit=limit,
            stop_loss=stop_loss, take_profit=take_profit,
            trail_distance=trail_distance)
        if refused is not None:
            return refused
        try:
            resp = self._req("POST", "/trade/v2/orders", body=order,
                             request_id=client_order_id or uuid.uuid4().hex)
        except BrokerError as e:
            if "HTTP 4" in str(e) and "HTTP 429" not in str(e):
                # a 4xx is Saxo refusing THIS order (bad price, no funds,
                # market closed) — definitive, so the row is marked rejected
                return PlacedOrder("", "rejected", str(e))
            raise                                # 5xx / network: unknown
        oid = resp.get("OrderId")
        if not oid:
            return PlacedOrder("", "rejected",
                               f"Saxo returned no OrderId: {json.dumps(resp)[:200]}")
        ids = [o.get("OrderId") for o in resp.get("Orders", [])]
        self._orders[oid] = {"legs": ids}
        return PlacedOrder(oid, "accepted",
                           legs=self._label_legs(ids, order.get("Orders", [])))

    def place_oco(self, *, symbol, side, qty, stop_loss=None,
                  take_profit=None, client_order_id=None,
                  trail_distance=None) -> PlacedOrder:
        """Protect an EXISTING position: a standalone stop-loss + take-profit
        pair, one-cancels-other, GTC — Saxo's "two orders in the Orders
        array with no parent". `side` is the side of the protective orders
        ('sell' protects a long). Used to re-protect what is left after a
        partial desk sell (the sell had to cancel the original legs: Saxo
        refuses a new sell while they rest). broker_order_id is the stop's
        id (or the only leg's); `legs` carries both."""
        if self.environment == "live" and self._guard is not None:
            try:
                self._guard()
            except Exception as e:              # noqa: BLE001 — definitive
                return PlacedOrder("", "rejected", f"live trading: {e}")
        if not (stop_loss or take_profit):
            return PlacedOrder("", "rejected", "nothing to protect with")
        try:
            uic, details = self.resolve(symbol)
        except BrokerError as e:
            return PlacedOrder("", "rejected", str(e))
        amount = Decimal(str(qty))
        lot = Decimal(str(details.get("LotSize") or 1))
        if amount <= 0 or amount != amount.to_integral_value() or amount % lot:
            return PlacedOrder("", "rejected", f"quantity {qty} is not a whole"
                                               f" multiple of the Saxo lot "
                                               f"size {lot}")
        legs = self._protective(uic, details, protect_side=side,
                                qty=int(amount), stop_loss=stop_loss,
                                take_profit=take_profit,
                                trail_distance=trail_distance)
        # one leg is a plain order; two are an OCO pair
        body = legs[0] if len(legs) == 1 else {"AccountKey": self.account_key,
                                               "Orders": legs}
        try:
            resp = self._req("POST", "/trade/v2/orders", body=body,
                             request_id=client_order_id or uuid.uuid4().hex)
        except BrokerError as e:
            if "HTTP 4" in str(e) and "HTTP 429" not in str(e):
                return PlacedOrder("", "rejected", str(e))
            raise                                # 5xx / network: unknown
        ids = [o.get("OrderId") for o in resp.get("Orders", [])] or \
            [resp.get("OrderId")]
        ids = [i for i in ids if i]
        if not ids:
            return PlacedOrder("", "rejected",
                               f"Saxo returned no OrderId: {json.dumps(resp)[:200]}")
        labelled = self._label_legs(ids, legs)
        stop = next((x["order_id"] for x in labelled if x["leg"] == "stop"),
                    labelled[0]["order_id"] if labelled else ids[0])
        return PlacedOrder(str(stop), "accepted", legs=labelled)

    def working_orders(self) -> list:
        """Every order working at Saxo on this account, flattened: top-level
        orders and the related ones listed under them (an unfilled entry's
        legs only appear there). Read-only."""
        data = self._req("GET", "/port/v1/orders/me?FieldGroups="
                         "DisplayAndFormat&$top=200").get("Data", [])
        out, seen = [], set()
        for o in data:
            if o.get("AccountKey") and o["AccountKey"] != self.account_key:
                continue
            sym = (o.get("DisplayAndFormat") or {}).get("Symbol")
            rows = [{"order_id": str(o.get("OrderId")),
                     "type": o.get("OpenOrderType"),
                     "price": o.get("Price"), "amount": o.get("Amount"),
                     "side": (o.get("BuySell") or "").lower(),
                     "relation": o.get("OrderRelation"),
                     "status": o.get("Status")}]
            for r in o.get("RelatedOpenOrders") or []:
                rows.append({"order_id": str(r.get("OrderId")),
                             "type": r.get("OpenOrderType"),
                             "price": r.get("OrderPrice"),
                             "amount": r.get("Amount"), "side": None,
                             "relation": "related", "status": r.get("Status")})
            for row in rows:
                if row["order_id"] in seen:
                    continue
                seen.add(row["order_id"])
                row.update(symbol=sym, uic=o.get("Uic"),
                           desk_symbol=desk_symbol(sym))
                out.append(row)
        return out

    def related_order_ids(self, order_id) -> list:
        """The orders Saxo created with `order_id` (a bracket's legs), from
        the audit trail. [] when Saxo lists none."""
        info = self._order_info(order_id)
        return info["related"] if info else []

    def precheck(self, *, symbol, side, qty, limit=None, stop_loss=None,
                 take_profit=None, trail_distance=None) -> dict:
        """Ask Saxo whether this exact bracket would be accepted, WITHOUT
        placing it (/trade/v2/orders/precheck). Allowed whether or not live
        trading is armed: it cannot create an order."""
        order, refused = self._bracket_body(
            symbol=symbol, side=side, qty=qty, limit=limit,
            stop_loss=stop_loss, take_profit=take_profit,
            trail_distance=trail_distance)
        if refused is not None:
            return {"ok": False, "error": refused.detail}
        try:
            out = self._req("POST", "/trade/v2/orders/precheck", body=order)
        except BrokerError as e:
            return {"ok": False, "error": str(e)}
        errs = [out.get("ErrorInfo")] + [o.get("ErrorInfo")
                                         for o in out.get("Orders", [])]
        errs = [e for e in errs if e and e.get("ErrorCode") != "OrderNotPlaced"]
        return {"ok": out.get("PreCheckResult") == "Ok" and not errs,
                "error": "; ".join(f"{e.get('ErrorCode')}: {e.get('Message')}"
                                   for e in errs) or None,
                "estimated_cash": out.get("EstimatedCashRequired"),
                "currency": out.get("EstimatedCashRequiredCurrency"),
                "amount": order["Amount"], "price": order["OrderPrice"],
                "legs": [{"type": o["OrderType"], "price": o["OrderPrice"],
                          "trail": o.get("TrailingStopDistanceToMarket")}
                         for o in order.get("Orders", [])]}

    def order_status(self, broker_order_id) -> dict:
        """Fills reconstructed from Saxo's order-activity audit trail. Saxo
        reports a cumulative FilledAmount and running AveragePrice; each
        fill's own price is recovered from the change in (avg × filled)."""
        acts = self._req("GET", "/cs/v1/audit/orderactivities?OrderId="
                         f"{broker_order_id}&EntryType=All").get("Data", [])
        if not acts:
            raise BrokerError(f"Saxo has no activity for order "
                              f"{broker_order_id}")
        acts.sort(key=lambda a: (a.get("ActivityTime") or "",
                                 int(a.get("LogId") or 0)))
        fills, prev_filled, prev_value = [], Decimal(0), Decimal(0)
        status, ended = "placed", None
        for a in acts:
            st, sub = a.get("Status"), a.get("SubStatus")
            if st in ("Fill", "FinalFill") and sub != "Rejected":
                filled = Decimal(str(a.get("FilledAmount") or 0))
                avg = Decimal(str(a.get("AveragePrice") or 0))
                qty = filled - prev_filled
                if qty > 0:
                    value = avg * filled
                    fills.append({"fill_id": f"saxo-{a.get('LogId')}",
                                  "qty": str(qty),
                                  "price": str((value - prev_value) / qty)})
                    prev_filled, prev_value = filled, value
                status = "filled" if st == "FinalFill" else "partially_filled"
            elif st == "Placed" and sub == "Rejected":
                status = "rejected"
            elif st in _DONE and status not in ("filled",):
                status, ended = "cancelled", st
        last = acts[-1]
        # what the order IS at the venue, so the custodian can keep a leg's
        # row (type, price, working amount) in step with Saxo
        return {"status": status, "fills": fills,
                # why a "cancelled" order ended: Expired / DoneForDay (a day
                # order at the close) or Cancelled (by someone, e.g. on
                # Saxo's own platform)
                "ended": ended,
                "order_type": last.get("OrderType"),
                "price": last.get("Price"), "amount": last.get("Amount")}

    def cancel(self, broker_order_id):
        out = self._req("DELETE", f"/trade/v2/orders/{broker_order_id}/"
                                  f"?AccountKey={self.account_key}")
        # a 200 can still carry a per-order refusal
        errs = [o.get("ErrorInfo") for o in (out or {}).get("Orders", [])
                if isinstance(o, dict) and o.get("ErrorInfo")]
        if errs:
            raise BrokerError(f"Saxo refused to cancel {broker_order_id}: "
                              f"{json.dumps(errs)[:300]}")

    def snapshot(self) -> dict:
        bal = self._req("GET", "/port/v1/balances/me")
        return {"environment": self.environment,
                "cash": bal.get("CashBalance"),
                "currency": bal.get("Currency"),
                "total_value": bal.get("TotalValue")}


# Markets Saxo's SIM environment does not offer as shares (2026-11-06: every
# SGX name — D05, O39, U11, Z74, C6L … — is listed there only as
# CfdOnStock). Never applied to the LIVE adapter: Richard's real Saxo
# account trades SGX shares.
SIM_LOCAL_SUFFIXES = ("SI",)


class SaxoSimWithLocalMarkets:
    """The saxo-paper venue: Saxo SIM for everything it offers as shares,
    the desk's own in-process paper sim for the markets it does not
    (SIM_LOCAL_SUFFIXES). Same interface as SaxoBroker. Orders are routed by
    symbol on the way in and by order id after that (the local sim's ids
    start "sim-"; Saxo's are numeric). A locally simulated order behaves
    exactly like the Alpaca/moomoo paper books: it fills at its limit on
    placement, keeps its stop-loss / take-profit as resting orders of the
    built-in sim (checked against the live price by the custodian) and does
    not exist at Saxo — so Saxo's balances never include it."""

    def __init__(self, venue, local, *, suffixes=SIM_LOCAL_SUFFIXES):
        self.venue, self.local = venue, local
        self.suffixes = tuple(suffixes)
        self.environment = venue.environment
        self.fractional = venue.fractional
        self.venue_label = (f"Saxo {venue.environment} + built-in paper sim "
                            f"for {', '.join('.' + s for s in self.suffixes)}")

    def is_local(self, symbol) -> bool:
        return split_symbol(symbol or "")[1] in self.suffixes

    def _owner(self, broker_order_id):
        return self.local if str(broker_order_id or "").startswith("sim-") \
            else self.venue

    def place_bracket(self, *, symbol, side, qty, limit=None, stop_loss=None,
                      take_profit=None, client_order_id=None,
                      trail_distance=None) -> PlacedOrder:
        kw = dict(symbol=symbol, side=side, qty=qty, limit=limit,
                  stop_loss=stop_loss, take_profit=take_profit,
                  client_order_id=client_order_id)
        if self.is_local(symbol):           # the local sim has no venue stop
            return self.local.place_bracket(**kw)
        return self.venue.place_bracket(**kw, trail_distance=trail_distance)

    def place_oco(self, *, symbol, side, qty, stop_loss=None,
                  take_profit=None, client_order_id=None,
                  trail_distance=None) -> PlacedOrder:
        if self.is_local(symbol):           # the built-in sim holds these
            return self.local.place_oco(symbol=symbol, side=side, qty=qty,
                                        stop_loss=stop_loss,
                                        take_profit=take_profit,
                                        client_order_id=client_order_id)
        return self.venue.place_oco(symbol=symbol, side=side, qty=qty,
                                    stop_loss=stop_loss,
                                    take_profit=take_profit,
                                    client_order_id=client_order_id,
                                    trail_distance=trail_distance)

    def precheck(self, *, symbol, side, qty, limit=None, stop_loss=None,
                 take_profit=None, trail_distance=None) -> dict:
        if self.is_local(symbol):
            return {"ok": None, "simulated": True, "note":
                    "Saxo's practice account offers these shares only as "
                    "CFDs, so the desk simulates this order itself: it fills "
                    "at your limit and never reaches Saxo; the built-in sim "
                    "holds its stop-loss / take-profit"}
        return self.venue.precheck(symbol=symbol, side=side, qty=qty,
                                   limit=limit, stop_loss=stop_loss,
                                   take_profit=take_profit,
                                   trail_distance=trail_distance)

    def order_status(self, broker_order_id) -> dict:
        return self._owner(broker_order_id).order_status(broker_order_id)

    def cancel(self, broker_order_id):
        return self._owner(broker_order_id).cancel(broker_order_id)

    def related_order_ids(self, order_id) -> list:
        if self._owner(order_id) is self.local:
            return self.local.related_order_ids(order_id)
        return self.venue.related_order_ids(order_id)

    def working_orders(self) -> list:
        # the locally simulated legs are working orders of this venue too
        local = self.local.list_working() if hasattr(
            self.local, "list_working") else []
        return self.venue.working_orders() + local

    # the built-in sim's resting stops / take-profits (custodian)
    def resting(self) -> list:
        return self.local.resting() if hasattr(self.local, "resting") else []

    def check_triggers(self, prices) -> list:
        return self.local.check_triggers(prices) if hasattr(
            self.local, "check_triggers") else []

    def resolve(self, symbol):
        return self.venue.resolve(symbol)

    def snapshot(self) -> dict:
        return {**self.venue.snapshot(), "local": self.local.snapshot()}
