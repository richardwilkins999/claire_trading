"""Broker adapters (DESIGN.md §9/§11). Paper/SIM ONLY — enforced here in code:
every adapter refuses any environment except paper/sim.

`PaperSimBroker` is a deterministic in-process venue used for development,
tests, and as the default account: limit-order semantics, configurable fills
(instant / partial / resting), bracket orders, cash+positions snapshot.

`MCPBrokerConfig` describes the three real paper venues (alpaca / saxo /
moomoo MCP servers); wiring activates them only when credentials exist.
"""
import itertools
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from ..accounting import db


class BrokerError(Exception):
    pass


@dataclass
class PlacedOrder:
    broker_order_id: str
    status: str                     # accepted | rejected
    detail: str = ""
    # protective orders the venue created WITH this one and holds on its own
    # (Saxo's related stop-loss / take-profit): [{"leg": "stop"|"target",
    # "order_id": venue id, "price": venue price, "verified": bool}].
    # submit_order persists each as an orders row (DESIGN.md §11a); a venue
    # that keeps no separate leg orders (the sim without protect=True) returns [].
    legs: list = field(default_factory=list)


@dataclass
class _Book:
    side: str
    qty: Decimal
    limit: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    symbol: str
    filled: Decimal = Decimal(0)
    fills: list = field(default_factory=list)
    status: str = "placed"          # placed | partially_filled | filled | cancelled
    # a protective order (protect=True): 'stop' or 'target', resting until
    # the price reaches `trigger`; `parent` is the entry it protects (armed
    # only for what that entry has filled), `oco` the group whose other
    # members are cancelled when one fills
    kind: str = "entry"             # entry | stop | target
    trigger: Decimal | None = None
    parent: str | None = None
    oco: str | None = None


SIM_TABLE = db.table_ddl("sim_orders")      # schema.sql is the one copy
_DEC = ("qty", "limit", "stop_loss", "take_profit", "filled", "trigger")


class PaperSimBroker:
    """Deterministic paper venue. `fill_mode`: 'instant' fills at the limit
    (or `mark` price) on placement; 'manual' rests until `simulate_fill` is
    called — which is how tests exercise async/partial fills.

    `protect=True` makes it keep a bracket's stop-loss / take-profit as
    orders of their own, like Saxo: returned as `legs` (the desk books them,
    DESIGN.md §11a), one-cancels-other, and filled by `check_triggers` when
    the live price reaches them — the custodian calls it with quotes while
    the market is open. A stop sells at the price seen (a gap is not
    softened); a take-profit at its own price. Without it (the default) a
    bracket's levels are only remembered, as before.

    `store` (a desk.db connection) keeps every order in `sim_orders`, so a
    resting stop survives a claire-api restart; `venue` names the sim."""

    environment = "paper"

    def __init__(self, *, fill_mode="instant", marks=None, protect=False,
                 store=None, venue="sim", clock=time.time):
        if self.environment not in ("paper", "sim"):    # unrepresentable, but
            raise BrokerError("live trading is not representable")
        self.fill_mode = fill_mode
        self.marks = marks or {}                        # symbol -> Decimal price
        self.protect = protect
        self.store, self.venue, self.clock = store, venue, clock
        self.orders: dict[str, _Book] = {}
        self._by_client: dict[str, str] = {}             # client id -> order id
        self._seq = itertools.count(1)
        if store is not None:
            self._load()

    # ── persistence (optional) ───────────────────────────────────────────
    def _load(self):
        self.store.execute(SIM_TABLE)
        n = 0
        for r in self.store.execute(
                "SELECT id, client_id, book_json FROM sim_orders WHERE venue=?",
                (self.venue,)).fetchall():
            d = json.loads(r[2])
            for k in _DEC:
                if d.get(k) is not None:
                    d[k] = Decimal(d[k])
            self.orders[r[0]] = _Book(**d)
            if r[1]:
                self._by_client[r[1]] = r[0]
            n += len(d.get("fills") or [])
        self._seq = itertools.count(n + 1)              # fill ids stay unique

    def _save(self, oid):
        if self.store is None:
            return
        b = self.orders[oid]
        d = {k: (str(v) if isinstance(v, Decimal) else v)
             for k, v in asdict(b).items()}
        client = next((c for c, o in self._by_client.items() if o == oid),
                      None)
        self.store.execute(
            "INSERT INTO sim_orders (id, venue, client_id, book_json,"
            " updated_at) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
            " book_json=excluded.book_json, updated_at=excluded.updated_at",
            (oid, self.venue, client, json.dumps(d), int(self.clock())))

    def place_bracket(self, *, symbol, side, qty, limit=None, stop_loss=None,
                      take_profit=None, client_order_id=None) -> PlacedOrder:
        # A replay of the same client order id is the SAME order (review M4):
        # hand back the original instead of placing a second one.
        if client_order_id and client_order_id in self._by_client:
            return PlacedOrder(self._by_client[client_order_id], "accepted",
                               "duplicate client_order_id")
        qty = Decimal(str(qty))
        if qty <= 0:
            return PlacedOrder("", "rejected", "qty must be positive")
        oid = f"sim-{uuid.uuid4().hex[:10]}"
        if client_order_id:
            self._by_client[client_order_id] = oid
        book = _Book(side=side, qty=qty,
                     limit=Decimal(str(limit)) if limit else None,
                     stop_loss=Decimal(str(stop_loss)) if stop_loss else None,
                     take_profit=Decimal(str(take_profit)) if take_profit else None,
                     symbol=symbol)
        self.orders[oid] = book
        legs = self._legs(oid, book) if self.protect else []
        self._save(oid)
        if self.fill_mode == "instant":
            px = book.limit or self.marks.get(symbol)
            if px is None:
                for lg in legs:                         # nothing to protect
                    self.orders[lg["order_id"]].status = "cancelled"
                    self._save(lg["order_id"])
                return PlacedOrder(oid, "rejected", "no price for instant fill")
            self.simulate_fill(oid, qty, px)
        return PlacedOrder(oid, "accepted", legs=legs)

    def _new_leg(self, *, kind, symbol, side, qty, price, parent=None,
                 oco=None):
        lid = f"sim-{uuid.uuid4().hex[:10]}"
        self.orders[lid] = _Book(side=side, qty=Decimal(str(qty)), limit=None,
                                 stop_loss=None, take_profit=None,
                                 symbol=symbol, kind=kind,
                                 trigger=Decimal(str(price)), parent=parent,
                                 oco=oco)
        self._save(lid)
        return {"leg": kind, "order_id": lid, "price": str(price),
                "verified": True}

    def _legs(self, oid, book):
        """A bracket's stop-loss / take-profit as resting orders of their
        own on the exit side, armed as the entry fills."""
        out = []
        exit_side = "sell" if book.side == "buy" else "buy"
        for kind, px in (("stop", book.stop_loss), ("target", book.take_profit)):
            if px:
                out.append(self._new_leg(kind=kind, symbol=book.symbol,
                                         side=exit_side, qty=book.qty,
                                         price=px, parent=oid, oco=oid))
        return out

    def place_oco(self, *, symbol, side, qty, stop_loss=None,
                  take_profit=None, client_order_id=None) -> PlacedOrder:
        """Protect an EXISTING position (re-protection after a partial desk
        sell): a stop-loss + take-profit pair, one-cancels-other. `side` is
        the protective orders' side ('sell' for a long)."""
        if not self.protect:
            return PlacedOrder("", "rejected", "this sim keeps no resting "
                               "stop/target")
        if client_order_id and client_order_id in self._by_client:
            return PlacedOrder(self._by_client[client_order_id], "accepted",
                               "duplicate client_order_id")
        if not (stop_loss or take_profit) or Decimal(str(qty)) <= 0:
            return PlacedOrder("", "rejected", "nothing to protect")
        group = f"oco-{uuid.uuid4().hex[:8]}"
        legs = [self._new_leg(kind=k, symbol=symbol, side=side, qty=qty,
                              price=px, oco=group)
                for k, px in (("stop", stop_loss), ("target", take_profit))
                if px]
        first = legs[0]["order_id"]
        if client_order_id:
            self._by_client[client_order_id] = first
            self._save(first)
        return PlacedOrder(first, "accepted", legs=legs)

    def _armed(self, b) -> Decimal:
        """How many shares a resting leg may sell (buy back) right now."""
        if b.parent:
            p = self.orders.get(b.parent)
            cap = p.filled if p else Decimal(0)
        else:
            cap = b.qty
        return max(Decimal(0), min(cap, b.qty) - b.filled)

    def resting(self) -> list:
        """Working protective orders: [(order_id, book)]."""
        return [(k, b) for k, b in self.orders.items()
                if b.kind != "entry" and b.status in ("placed",
                                                      "partially_filled")]

    def check_triggers(self, prices) -> list:
        """Fill every armed stop / take-profit the price has reached.
        `prices` = {symbol: last price}. A sell stop fires at or below its
        price and fills at the price seen (it is a market order once
        triggered); a take-profit fills at its own price. The other orders
        of its one-cancels-other group are cancelled. Returns
        [{order_id, leg, symbol, side, qty, price}] for what filled."""
        out = []
        for oid, b in self.resting():
            px = prices.get(b.symbol)
            if px is None or b.status not in ("placed", "partially_filled"):
                continue                                # (an OCO sibling)
            px = Decimal(str(px))
            sell = b.side == "sell"
            if b.kind == "stop":
                hit = px <= b.trigger if sell else px >= b.trigger
                fill_px = px
            else:
                hit = px >= b.trigger if sell else px <= b.trigger
                fill_px = b.trigger
            q = self._armed(b)
            if not hit or q <= 0:
                continue
            b.filled += q
            b.fills.append({"fill_id": f"fill-{next(self._seq)}",
                            "qty": str(q), "price": str(fill_px)})
            b.status = "filled"
            self._save(oid)
            for sid, sb in self.resting():
                if sid != oid and sb.oco and sb.oco == b.oco:
                    sb.status = "cancelled"
                    self._save(sid)
            out.append({"order_id": oid, "leg": b.kind, "symbol": b.symbol,
                        "side": b.side, "qty": str(q), "price": str(fill_px)})
        return out

    def list_working(self) -> list:
        """Working orders in the shape SaxoBroker.working_orders uses (for
        the custodian's leg-drift check through SaxoSimWithLocalMarkets)."""
        return [{"order_id": k, "type": {"stop": "StopIfTraded",
                                         "target": "Limit"}.get(b.kind, "Limit"),
                 "price": str(b.trigger or b.limit or ""),
                 "amount": str(b.qty), "side": b.side, "relation": None,
                 "status": "Working", "symbol": b.symbol,
                 "desk_symbol": b.symbol}
                for k, b in self.orders.items()
                if b.status in ("placed", "partially_filled")]

    def simulate_fill(self, broker_order_id, qty, price):
        b = self.orders[broker_order_id]
        qty = min(Decimal(str(qty)), b.qty - b.filled)
        if qty <= 0 or b.status in ("filled", "cancelled"):
            return
        b.filled += qty
        b.fills.append({"fill_id": f"fill-{next(self._seq)}",
                        "qty": str(qty), "price": str(price)})
        b.status = "filled" if b.filled == b.qty else "partially_filled"
        self._save(broker_order_id)

    def order_status(self, broker_order_id) -> dict:
        b = self.orders.get(broker_order_id)
        if b is None:
            raise BrokerError(f"unknown order {broker_order_id}")
        out = {"status": b.status, "fills": list(b.fills)}
        if b.kind != "entry":
            out.update(order_type="StopIfTraded" if b.kind == "stop"
                       else "Limit", price=str(b.trigger))
        return out

    def cancel(self, broker_order_id):
        b = self.orders.get(broker_order_id)
        if b and b.status not in ("filled",):
            b.status = "cancelled"
            self._save(broker_order_id)
            if b.kind == "entry" and b.filled == 0:
                # an entry that never filled leaves nothing to protect
                for lid, lb in self.resting():
                    if lb.parent == broker_order_id:
                        lb.status = "cancelled"
                        self._save(lid)

    def related_order_ids(self, order_id) -> list:
        return [k for k, b in self.orders.items() if b.parent == order_id]

    def snapshot(self) -> dict:
        return {"orders": {k: v.status for k, v in self.orders.items()}}


@dataclass
class MCPBrokerConfig:
    """How to reach a real paper/SIM venue over MCP (langchain-mcp-adapters).
    Activated by wiring ONLY when the referenced env vars are present."""
    broker: str
    command: list
    env_refs: list                    # env var NAMES the server needs
    note: str = ""


MCP_BROKERS = {
    "alpaca": MCPBrokerConfig(
        broker="alpaca",
        command=["uvx", "alpaca-mcp-server"],
        env_refs=["ALPACA_PAPER_KEY", "ALPACA_PAPER_SECRET"],
        note="official server; paper endpoint only"),
    "saxo": MCPBrokerConfig(
        broker="saxo",
        command=["python", "mcp/saxo_mcp.py"],
        env_refs=["SAXO_SIM_TOKEN"],
        note="custom FastMCP server; SIM only, token expires every 24h"),
    "moomoo": MCPBrokerConfig(
        broker="moomoo",
        command=["moomoo-api-mcp"],
        env_refs=[],
        note="needs the local OpenD gateway running"),
}


def seed_mcps(conn):
    """The dict above is only the SEED — the mcp_servers table is the source
    of truth, editable from the Providers page."""
    import json
    for cfg in MCP_BROKERS.values():
        conn.execute(
            "INSERT OR IGNORE INTO mcp_servers (id, broker, command,"
            " env_refs, note) VALUES (?,?,?,?,?)",
            (cfg.broker, cfg.broker, json.dumps(cfg.command),
             json.dumps(cfg.env_refs), cfg.note))
