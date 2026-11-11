"""How the desk's accounts are named, ordered and offered on an order form.
Lived in dashboards.py, where watchlist.py reached in for them
(REVIEW-2026-11-11 §3.2, step 2)."""
import json

def venue_adapter(acct, env) -> str:
    """Which adapter claire-api builds for this account (wiring.build_brokers
    is the rule; this mirrors it for display — it places nothing)."""
    b = acct["broker"]
    if b == "saxo" and (env.get("SAXO_SIM_EXECUTION") or "").lower() == \
            "enabled" and env.get("SAXO_SIM_ACCOUNT_KEY"):
        return "saxo-sim"
    if b == "saxo_live" and acct["environment"] == "live":
        return "saxo-live"
    return "paper-sim"


def sim_label(broker):
    """A paper book Claire simulates itself — no broker ever sees it.
    Named so it can never be mistaken for a broker's own paper account."""
    return f"Claire sim · {broker} (simulated, not a broker)"


def account_rank(a, env):
    """Order of every account list, so the first (default) choice is the
    realistic one: Saxo SIM, then Claire's own sims, then real money."""
    sim_on = (env.get("SAXO_SIM_EXECUTION") or "").lower() == "enabled"
    if a["environment"] == "live":
        return 3
    if a["broker"] == "saxo" and sim_on:
        return 0
    return 2


def ordered_accounts(conn, env):
    return sorted(conn.execute("SELECT * FROM broker_accounts").fetchall(),
                  key=lambda a: (account_rank(a, env or {}), a["id"]))


def _approval_account(a, repo, env, live):
    """One broker choice on the approval card, labelled by the REAL account
    it reaches. A live account is listed always but selectable only while
    live trading is armed; the server enforces the same rule (§22)."""
    sim_on = (env.get("SAXO_SIM_EXECUTION") or "").lower() == "enabled"
    if a["environment"] == "live":
        label = (f"Saxo LIVE {env.get('SAXO_LIVE_ACCOUNT_ID') or ''}"
                 " — real money").replace("  ", " ")
        disabled = None if live["armed"] else live["summary"]
    elif a["broker"] == "saxo" and sim_on:
        label = (f"Saxo SIM {env.get('SAXO_SIM_ACCOUNT_ID') or ''}"
                 " — paper").replace("  ", " ")
        disabled = None
    else:
        label, disabled = sim_label(a["broker"]), None
    return {"id": a["id"], "broker": a["broker"], "ccy": a["base_currency"],
            "environment": a["environment"],
            # what is FREE to spend — the figure the approval gate checks
            # (approvals.authorize). It used to be the whole balance, so with
            # buy orders still open the card offered a size the gate refused
            "cash": repo.available_cash(a["id"]) / 1e6,
            "cash_reserved": repo.reserved_cash(a["id"]) / 1e6,
            "fee_model": json.loads(a["fee_model"]),
            "label": label, "disabled": disabled,
            # Saxo fills whole shares only; the estimate must say so
            "whole_shares": a["environment"] == "live" or (
                a["broker"] == "saxo" and sim_on),
            # stops (and the trailing floor) rest AT Saxo, except on the
            # markets saxo-paper simulates itself (Saxo SIM: CFDs only)
            "venue_stops": a["environment"] == "live" or (
                a["broker"] == "saxo" and sim_on),
            "local_exchanges": ["SGX"] if a["broker"] == "saxo" and sim_on
            and a["environment"] != "live" else []}
