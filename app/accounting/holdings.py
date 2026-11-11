"""How many shares the book holds — asked one way.

"Net shares the desk holds in X" was written out as SQL in eleven places,
with three ways of turning micro-units into a number (integer truncation,
float division in SQL, Decimal) — REVIEW-2026-11-11 §3.1. The lots table is
the truth: a lot's qty_remaining is positive, and a short is a lot whose
qty_opened is negative. These three functions are that rule, in exact
Decimal shares, signed (a short is negative).
"""
from decimal import Decimal

from .money import from_micro

_NET = ("SUM(CASE WHEN l.qty_opened < 0 THEN -l.qty_remaining"
        " ELSE l.qty_remaining END)")


def _where(ticker, exchange, instrument_id, account_id):
    cond, args = ["l.qty_remaining > 0"], []
    for col, val in (("i.ticker", ticker), ("i.exchange", exchange),
                     ("l.instrument_id", instrument_id),
                     ("l.account_id", account_id)):
        if val is not None:
            cond.append(f"{col}=?")
            args.append(val)
    return " AND ".join(cond), args


def by_account(conn, *, ticker=None, exchange=None, instrument_id=None) -> dict:
    """{account id: net shares} for one name, accounts holding none left out.
    A ticker alone matches that ticker on any exchange."""
    where, args = _where(ticker, exchange, instrument_id, None)
    rows = conn.execute(
        f"SELECT l.account_id, {_NET} AS q FROM lots l JOIN instruments i"
        f" ON i.id = l.instrument_id WHERE {where} GROUP BY l.account_id",
        args).fetchall()
    return {r["account_id"]: from_micro(r["q"]) for r in rows if r["q"]}


def net(conn, *, ticker=None, exchange=None, instrument_id=None,
        account_id=None) -> Decimal:
    """Net shares held in one name — on one account, or across all of them."""
    where, args = _where(ticker, exchange, instrument_id, account_id)
    (q,) = conn.execute(
        f"SELECT COALESCE({_NET}, 0) FROM lots l JOIN instruments i"
        f" ON i.id = l.instrument_id WHERE {where}", args).fetchone()
    return from_micro(q or 0)


def positions(conn, account_id=None) -> list:
    """Every open position: one row per (account, instrument) with its net
    shares. [{account_id, instrument_id, ticker, exchange, qty}]"""
    where, args = _where(None, None, None, account_id)
    return [{"account_id": r["account_id"], "instrument_id": r["instrument_id"],
             "ticker": r["ticker"], "exchange": r["exchange"],
             "qty": from_micro(r["q"])}
            for r in conn.execute(
                f"SELECT l.account_id, l.instrument_id, i.ticker, i.exchange,"
                f" {_NET} AS q FROM lots l JOIN instruments i"
                f" ON i.id = l.instrument_id WHERE {where}"
                " GROUP BY l.account_id, l.instrument_id"
                " ORDER BY l.account_id, i.ticker", args) if r["q"]]
