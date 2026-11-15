"""What may be changed on a position AFTER its buy has filled — per broker.

One table, one row per broker. A part of an order's protection (stop-loss,
take-profit, a trailing stop held by the broker) can be moved only where
its row says so; everything else is refused by the desk and shown as fixed
on the pages. Adding a broker means adding a row here (and giving its
adapter a `change_leg` method — a row that says yes without one is refused
when used, never guessed at).

The rows:

- ``builtin``  the desk's own paper simulation. It holds the resting
               orders itself, so nothing outside the desk limits a change.
               Every paper account is this unless a real venue is connected
               (today: alpaca-paper, moomoo-paper, and saxo-paper's SGX
               shares).
- ``saxo``     Saxo SIM (saxo-paper with SAXO_SIM_EXECUTION=enabled). A
               working order is changed by its id (PATCH /trade/v2/orders).
               Changing a TRAILING stop's distance is not documented and
               has not been tried on SIM: off until it has.
- ``saxo_live`` real money. Off until the change has been proven on SIM.
- ``alpaca``, ``moomoo``  what those venues allow, for the day one of them
               is connected for real. Unused while their accounts are the
               built-in simulation. Alpaca: a bracket's stop and target can
               be replaced; a trailing stop cannot be part of a bracket.
               moomoo paper accounts take only limit and market orders, so
               no stop can rest there at all.

The desk's own trailing ALERT is not in the table: it is the desk's, on
every account, and is always changeable (Portfolio → Edit, /floor).
"""

PARTS = ("stop_loss", "take_profit", "trailing_stop")

CAN_CHANGE = {
    "builtin":   {"stop_loss": True,  "take_profit": True,
                  "trailing_stop": False},     # the sim has no trailing stop
    "saxo":      {"stop_loss": True,  "take_profit": True,
                  "trailing_stop": False},
    "saxo_live": {"stop_loss": False, "take_profit": False,
                  "trailing_stop": False},
    "alpaca":    {"stop_loss": True,  "take_profit": True,
                  "trailing_stop": False},
    "moomoo":    {"stop_loss": False, "take_profit": True,
                  "trailing_stop": False},
}
NOTHING = {p: False for p in PARTS}

# brokers whose orders really go to the venue; any other paper account is
# simulated by the desk (wiring.build_brokers)
LABEL = {"builtin": "the desk's simulation", "saxo": "Saxo SIM",
         "saxo_live": "Saxo (real money)", "alpaca": "Alpaca",
         "moomoo": "moomoo"}


def venue_of(broker, environment, exchange, *, sim_on, order_id=None):
    """Which row of the table holds this position's resting orders.
    `sim_on`: SAXO_SIM_EXECUTION is enabled. `order_id` (a resting order's
    venue id) settles it when known: the built-in sim's ids start "sim-"."""
    if order_id is not None and str(order_id).startswith("sim-"):
        return "builtin"
    if broker == "saxo_live" or environment == "live":
        return broker if broker in CAN_CHANGE else "unknown"
    if broker == "saxo" and sim_on and exchange != "SGX":
        return "saxo"
    return "builtin"            # every other paper account: the desk's sim


def can_change(venue) -> dict:
    """{stop_loss, take_profit, trailing_stop, trailing_alert} for a venue.
    An unknown broker gets nothing until it has a row."""
    return {**CAN_CHANGE.get(venue, NOTHING), "trailing_alert": True}


def for_position(account, exchange, env, legs=()) -> dict:
    """What the pages may offer for one held position. With resting orders
    the venue is read from them (a position can only be changed where its
    orders really are); `venue`/`venue_label` say where that is."""
    sim_on = ((env or {}).get("SAXO_SIM_EXECUTION") or "").lower() == "enabled"
    oid = next((l.get("venue_order") for l in legs if l.get("venue_order")),
               None)
    venue = venue_of(account["broker"], account["environment"], exchange,
                     sim_on=sim_on, order_id=oid)
    return {**can_change(venue), "venue": venue,
            "venue_label": LABEL.get(venue, venue)}
