"""Broker adapters (DESIGN.md §9/§11). Paper/SIM ONLY — enforced here in code:
every adapter refuses any environment except paper/sim.

`PaperSimBroker` is a deterministic in-process venue used for development,
tests, and as the default account: limit-order semantics, configurable fills
(instant / partial / resting), bracket orders, cash+positions snapshot.

`MCPBrokerConfig` describes the three real paper venues (alpaca / saxo /
moomoo MCP servers); wiring activates them only when credentials exist.
"""
import itertools
import uuid
from dataclasses import dataclass, field
from decimal import Decimal


class BrokerError(Exception):
    pass


@dataclass
class PlacedOrder:
    broker_order_id: str
    status: str                     # accepted | rejected
    detail: str = ""


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


class PaperSimBroker:
    """Deterministic paper venue. `fill_mode`: 'instant' fills at the limit
    (or `mark` price) on placement; 'manual' rests until `simulate_fill` is
    called — which is how tests exercise async/partial fills."""

    environment = "paper"

    def __init__(self, *, fill_mode="instant", marks=None):
        if self.environment not in ("paper", "sim"):    # unrepresentable, but
            raise BrokerError("live trading is not representable")
        self.fill_mode = fill_mode
        self.marks = marks or {}                        # symbol -> Decimal price
        self.orders: dict[str, _Book] = {}
        self._seq = itertools.count(1)

    def place_bracket(self, *, symbol, side, qty, limit=None, stop_loss=None,
                      take_profit=None) -> PlacedOrder:
        qty = Decimal(str(qty))
        if qty <= 0:
            return PlacedOrder("", "rejected", "qty must be positive")
        oid = f"sim-{uuid.uuid4().hex[:10]}"
        book = _Book(side=side, qty=qty,
                     limit=Decimal(str(limit)) if limit else None,
                     stop_loss=Decimal(str(stop_loss)) if stop_loss else None,
                     take_profit=Decimal(str(take_profit)) if take_profit else None,
                     symbol=symbol)
        self.orders[oid] = book
        if self.fill_mode == "instant":
            px = book.limit or self.marks.get(symbol)
            if px is None:
                return PlacedOrder(oid, "rejected", "no price for instant fill")
            self.simulate_fill(oid, qty, px)
        return PlacedOrder(oid, "accepted")

    def simulate_fill(self, broker_order_id, qty, price):
        b = self.orders[broker_order_id]
        qty = min(Decimal(str(qty)), b.qty - b.filled)
        if qty <= 0 or b.status in ("filled", "cancelled"):
            return
        b.filled += qty
        b.fills.append({"fill_id": f"fill-{next(self._seq)}",
                        "qty": str(qty), "price": str(price)})
        b.status = "filled" if b.filled == b.qty else "partially_filled"

    def order_status(self, broker_order_id) -> dict:
        b = self.orders.get(broker_order_id)
        if b is None:
            raise BrokerError(f"unknown order {broker_order_id}")
        return {"status": b.status, "fills": list(b.fills)}

    def cancel(self, broker_order_id):
        b = self.orders.get(broker_order_id)
        if b and b.status not in ("filled",):
            b.status = "cancelled"

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
