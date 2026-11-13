"""Starting trades from Telegram (app/telegram_commands.py + the bot's
routing). Offline: a fake claire-api, a fake market, a fake Bot API.

What matters most:
* every command ends at the approval gate — /buy and /sell only call
  /internal/manual, /analyze only /api/run; nothing places an order;
* only the approver, only in the private chat, can use commands or buttons;
* /sell never sells more than the desk holds and never opens a short;
* a BUY never suggests the real-money account;
* order cancel takes two taps; buttons expire and are single-use.
"""
import json
from decimal import Decimal

import pytest

from app import portfolio
from app import telegram_bot as tb
from app import telegram_commands as tc
from app.accounting.repo import Repo

from .test_telegram_approvals import ENV, OWNER, T0, conn  # noqa: F401
from .test_telegram_bot import FakeApi, text

ST = {"token": "bot:x", "approver_id": OWNER, "poll": True}


class Market:
    def __init__(self, price="103.67", ccy="USD"):
        self.price, self.ccy = price, ccy

    def quote(self, sym):
        return {"price": Decimal(self.price), "currency": self.ccy}

    def spark(self, syms):
        return {s: self.quote(s) for s in syms}

    def chart(self, sym, range_="3mo", interval="1d"):
        n = 30
        c = [100 + i * 0.1 for i in range(n)]
        return {"high": [x + 1 for x in c], "low": [x - 1 for x in c],
                "close": c}


class Desk:
    def __init__(self, code=200, out=None):
        self.calls, self.code = [], code
        self.out = out or {"ok": True, "work_item_id": "wi_x"}

    def start_run(self, body):
        self.calls.append(("run", body))
        return self.code, self.out

    def manual(self, body):
        self.calls.append(("manual", body))
        return self.code, self.out

    def cancel_order(self, wi):
        self.calls.append(("cancel", wi))
        return self.code, self.out


def make(conn, desk=None, market=None, nudges=None, clock=lambda: T0):
    return tc.Commands(conn, market or Market(), ENV, desk or Desk(),
                       clock=clock,
                       nudge=(lambda: nudges.append(1)) if nudges is not None
                       else None)


def pos(ticker="CSCO", qty=9.0, broker="saxo", acct="saxo-paper",
        exchange="NASDAQ", side="long"):
    return {"account_id": acct, "broker": broker, "ticker": ticker,
            "exchange": exchange, "qty": qty, "currency": "USD",
            "side": side, "instrument_id": f"{exchange}:{ticker}"}


@pytest.fixture
def held(monkeypatch):
    rows = []
    monkeypatch.setattr(portfolio, "open_positions", lambda c: rows)
    return rows


def press(cmds, kb, row=0, col=0):
    return cmds.callback(kb[row][col]["callback_data"])


# ── /analyze ──────────────────────────────────────────────────────────────
def test_analyze_starts_a_run_and_nothing_else(conn):
    d = Desk()
    [(html, kb)] = make(conn, d).command("/analyze csco")
    [(kind, body)] = d.calls
    assert kind == "run" and body["ticker"] == "CSCO"
    assert body["exchange"] == "NASDAQ" and body["currency"] == "USD"
    assert body["trigger"] == tc.RUN_TRIGGER
    assert "Researching" in html and kb is None


def test_analyze_sgx_uses_board_lots(conn):
    d = Desk()
    make(conn, d).command("/analyze D05 SGX")
    assert d.calls[0][1]["lot_size"] == 100
    assert d.calls[0][1]["currency"] == "SGD"


def test_ticker_on_two_exchanges_asks_which(conn):
    conn.execute("INSERT INTO instruments (id, ticker, exchange, currency)"
                 " VALUES ('LSE:CSCO','CSCO','LSE','GBP')")
    d = Desk()
    cmds = make(conn, d)
    [(html, kb)] = cmds.command("/analyze CSCO")
    assert "more than one exchange" in html and d.calls == []
    assert {b["text"] for b in kb[0]} == {"NASDAQ", "LSE"}
    lse = next(i for i, b in enumerate(kb[0]) if b["text"] == "LSE")
    press(cmds, kb, 0, lse)
    assert d.calls[0][1]["exchange"] == "LSE"


def test_analyze_is_rate_limited(conn):
    d = Desk()
    cmds = make(conn, d)
    for _ in range(tc.RUNS_PER_HOUR):
        cmds.command("/analyze CSCO")
    [(html, _)] = cmds.command("/analyze CSCO")
    assert "at most" in html and len(d.calls) == tc.RUNS_PER_HOUR


def test_unknown_exchange_is_refused(conn):
    d = Desk()
    [(html, _)] = make(conn, d).command("/analyze CSCO MOON")
    assert "unknown exchange" in html and d.calls == []


# ── /buy ──────────────────────────────────────────────────────────────────
def test_buy_puts_a_manual_trade_on_the_gate(conn):
    d, nudges = Desk(), []
    [(html, _)] = make(conn, d, nudges=nudges).command("/buy CSCO 1,000")
    [(kind, b)] = d.calls
    assert kind == "manual" and b["direction"] == "buy"
    assert b["source"] == "telegram" and b["entry"] == 103.67
    assert b["stop"] < b["entry"] < b["target"]
    assert b["suggested"] == {"order_type": "market", "size_base": 1000.0}
    assert "broker" not in b["suggested"]            # never pre-picks LIVE
    assert nudges == [1] and "nothing trades until you tap ✔" in html


def test_buy_stop_is_capped_inside_25_percent(conn):
    class Wild(Market):
        def chart(self, *a, **k):
            return {"high": [200] * 30, "low": [10] * 30, "close": [100] * 30}
    d = Desk()
    make(conn, d, market=Wild()).command("/buy CSCO")
    b = d.calls[0][1]
    assert b["stop"] >= round(b["entry"] * 0.76, 2) - 0.01


def test_lse_pence_become_pounds(conn):
    d = Desk()
    make(conn, d, market=Market("12345", "GBp")).command("/buy BARC LSE")
    assert d.calls[0][1]["entry"] == 123.45


def test_desk_refusal_is_shown(conn):
    d = Desk(409, {"detail": "a manual buy of CSCO is already awaiting"})
    [(html, _)] = make(conn, d).command("/buy CSCO")
    assert html.startswith("✖") and "already awaiting" in html


def test_buy_rejects_nonsense_size(conn):
    d = Desk()
    [(html, _)] = make(conn, d).command("/buy CSCO lots")
    assert "not an exchange or a size" in html and d.calls == []


# ── /sell and /positions ─────────────────────────────────────────────────
def test_sell_all_of_what_the_desk_holds(conn, held):
    held.append(pos(qty=9))
    d = Desk()
    [(html, _)] = make(conn, d).command("/sell csco")
    b = d.calls[0][1]
    assert b["direction"] == "sell" and b["suggested"] == {"qty": 9.0,
                                                          "broker": "saxo"}
    assert "SELL 9 CSCO" in html


def test_sell_half_rounds_to_whole_shares_and_lots(conn, held):
    held.extend([pos(qty=9), pos("D05", 300, exchange="SGX",
                                 acct="saxo-sgd")])
    d = Desk()
    cmds = make(conn, d)
    cmds.command("/sell CSCO half")
    cmds.command("/sell D05 half")
    assert d.calls[0][1]["suggested"]["qty"] == 4
    assert d.calls[1][1]["suggested"]["qty"] == 100


def test_sell_never_more_than_held_and_never_short(conn, held):
    held.append(pos(qty=9))
    d = Desk()
    cmds = make(conn, d)
    [(html, _)] = cmds.command("/sell CSCO 10")
    assert "holds only 9" in html
    [(html, _)] = cmds.command("/sell AAPL")
    assert "no shorts" in html
    [(html, _)] = cmds.command("/sell CSCO 2.5")
    assert "whole lots" in html
    assert d.calls == []


def test_sell_on_two_accounts_asks_which(conn, held):
    held.extend([pos(qty=9), pos(qty=3, broker="alpaca", acct="alpaca-paper")])
    d = Desk()
    cmds = make(conn, d)
    [(html, kb)] = cmds.command("/sell CSCO")
    assert len(kb) == 2 and d.calls == []
    press(cmds, kb, 1)
    assert d.calls[0][1]["suggested"] == {"qty": 3.0, "broker": "alpaca"}


def test_live_holding_sell_warns_real_money(conn, held):
    held.append(pos(qty=5, broker="saxo_live", acct="saxo-live"))
    d = Desk()
    [(html, _)] = make(conn, d).command("/sell CSCO")
    assert "REAL MONEY" in html
    assert d.calls[0][1]["suggested"]["broker"] == "saxo_live"


def test_positions_lists_with_sell_buttons(conn, held):
    held.extend([pos(qty=9), pos("SHRT", -5, side="short")])
    d = Desk()
    cmds = make(conn, d)
    [(html, kb)] = cmds.command("/positions")
    assert "CSCO" in html and "SHRT" not in html and "last 103.67" in html
    assert [b["text"] for b in kb[0]] == ["Sell half CSCO", "Sell all CSCO"]
    press(cmds, kb, 0, 1)
    assert d.calls[0][1]["suggested"]["qty"] == 9.0
    # single use: the same button again does nothing
    ans, replies = press(cmds, kb, 0, 1)
    assert "expired" in ans and len(d.calls) == 1


def test_buttons_expire(conn, held):
    held.append(pos(qty=9))
    now = [T0]
    d = Desk()
    cmds = make(conn, d, clock=lambda: now[0])
    [(_, kb)] = cmds.command("/positions")
    now[0] += tc.BUTTON_TTL_S + 1
    ans, _ = press(cmds, kb, 0, 0)
    assert "expired" in ans and d.calls == []


# ── /orders ──────────────────────────────────────────────────────────────
ORDER = {"work_item_id": "wi_o", "side": "buy", "qty": 9.0,
         "limit_price": 103.15, "status": "placed", "account_id": "saxo-paper",
         "ticker": "CSCO", "exchange": "NASDAQ", "currency": "USD", "legs": 0}


def test_cancel_takes_two_taps(conn, monkeypatch):
    monkeypatch.setattr(tc.Commands, "_open_orders", lambda self: [ORDER])
    d = Desk()
    cmds = make(conn, d)
    [(html, kb)] = cmds.command("/orders")
    assert "BUY 9 <b>CSCO</b> @ 103.15" in html
    _, [(confirm, kb2)] = press(cmds, kb)
    assert "Cancel</b>" in confirm and d.calls == []
    ans, _ = press(cmds, kb2, 0, 1)                       # ✖ Keep it
    assert "Kept" in ans and d.calls == []
    _, [(done, _)] = press(cmds, kb2, 0, 0)               # ✔ Cancel it
    assert d.calls == [("cancel", "wi_o")] and "Cancelled" in done


def test_no_orders(conn):
    [(html, _)] = make(conn).command("/orders")
    assert html == "No working entry orders."


# ── routing through the bot ──────────────────────────────────────────────
def test_only_the_owner_in_private_can_command(conn):
    d = Desk()
    cmds = make(conn, d)
    api = FakeApi()

    def run(upd):
        return tb.handle_update(conn, Market(), upd, ST, None, env=ENV,
                                call=api, commands=cmds)
    assert run(text("/buy CSCO", from_id="999")) == "ignored"
    assert run(text("/buy CSCO", chat_type="group")) == "ignored"
    assert d.calls == []
    assert run(text("/buy CSCO")) == "command"
    assert d.calls[0][0] == "manual"
    sent = api.of("sendMessage")[-1]
    assert sent["parse_mode"] == "HTML" and "BUY" in sent["text"]


def test_command_buttons_refuse_anyone_else(conn, held):
    held.append(pos(qty=9))
    d = Desk()
    cmds = make(conn, d)
    [(_, kb)] = cmds.command("/positions")
    api = FakeApi()
    upd = {"update_id": 5, "callback_query": {
        "id": "c", "from": {"id": 999}, "data": kb[0][1]["callback_data"],
        "message": {"message_id": 1, "chat": {"id": 999,
                                              "type": "private"}}}}
    assert tb.handle_update(conn, Market(), upd, ST, None, env=ENV, call=api,
                            commands=cmds) == "command-button:403"
    assert d.calls == []
    upd["callback_query"]["from"]["id"] = int(OWNER)
    upd["callback_query"]["message"]["chat"]["id"] = int(OWNER)
    assert tb.handle_update(conn, Market(), upd, ST, None, env=ENV, call=api,
                            commands=cmds) == "command-button"
    assert d.calls[0][1]["suggested"]["qty"] == 9.0


def test_start_for_the_owner_is_help(conn):
    api = FakeApi()
    tb.handle_update(conn, Market(), text("/start"), ST, None, env=ENV,
                     call=api, commands=make(conn))
    assert "/analyze" in api.of("sendMessage")[0]["text"]


# ── run outcomes ─────────────────────────────────────────────────────────
def _run(conn, wi, state, thesis, trigger=tc.RUN_TRIGGER):
    Repo(conn).create_work_item(wi, "pipeline", "CSCO", ts=T0)
    conn.execute("UPDATE work_items SET state=?, trigger=?, thesis_json=?"
                 " WHERE id=?", (state, trigger, json.dumps(thesis), wi))


def test_a_pass_is_reported_once(conn):
    _run(conn, "wi_p", "done", {"direction": "pass", "conviction": 0.12,
                                "conditions": ["PASS rationale: no edge"]})
    _run(conn, "wi_dash", "done", {"direction": "pass"},
         trigger="launched from the Trading page")
    _run(conn, "wi_live", "running", {})
    cmds = make(conn)
    api = FakeApi()
    tb.report_finished_runs(cmds, ST, api)
    [m] = api.of("sendMessage")
    assert "the desk passes" in m["text"] and "no edge" in m["text"]
    assert "wi_p" in m["text"]
    tb.report_finished_runs(cmds, ST, api)
    assert len(api.of("sendMessage")) == 1


# ── trailing floor: /buy trail=, /floor ──────────────────────────────────
def test_buy_carries_a_trailing_floor(conn):
    d = Desk()
    cmds = make(conn, d)
    [(html, _)] = cmds.command("/buy CSCO 1000 trail=10")
    assert d.calls[0][1]["suggested"]["trail_pct"] == 10
    assert "trail 10%" in html
    cmds.command("/buy AAPL 7.5%")
    assert d.calls[1][1]["suggested"]["trail_pct"] == 7.5
    [(html, _)] = cmds.command("/buy MSFT")
    assert "trail_pct" not in d.calls[2][1]["suggested"]
    assert "trail 8% (default" in html
    [(html, _)] = cmds.command("/buy CSCO trail=95")
    assert "between 0.5% and 90%" in html and len(d.calls) == 3


iid_pos = pos


def floor_of(conn, p):
    from app import watcher
    return watcher.position_floor(conn, p["instrument_id"],
                                  p["account_id"])[0]


def test_floor_shows_default_then_changes_after_confirm(conn, held):
    p = iid_pos(qty=9)
    held.append(p)
    cmds = make(conn)
    [(html, kb)] = cmds.command("/floor CSCO")
    assert "trailing alert <b>8%</b>" in html
    assert [b["text"] for b in kb[0]] == ["5%", "✓ 8%", "12%", "15%"]
    _, [(ask, kb2)] = press(cmds, kb, 0, 2)
    assert "from 8% to <b>12%</b>" in ask and floor_of(conn, p) == 8
    _, [(done, _)] = press(cmds, kb2, 0, 0)
    assert "8% → <b>12%</b>" in done and floor_of(conn, p) == 12


def test_floor_any_value_and_keeps_the_peak(conn, held):
    p = iid_pos(qty=9)
    held.append(p)
    conn.execute("INSERT INTO price_alerts (instrument_id, account_id, side,"
                 " rule, threshold, armed, peak_base) VALUES"
                 " (?,?,1,'trail_pct',8,1,120.5)",
                 (p["instrument_id"], p["account_id"]))
    cmds = make(conn)
    [(ask, kb)] = cmds.command("/floor CSCO 6.5")
    press(cmds, kb, 0, 0)
    r = conn.execute("SELECT threshold, peak_base FROM price_alerts").fetchone()
    assert r["threshold"] == 6.5 and r["peak_base"] == 120.5
    [(html, _)] = cmds.command("/floor CSCO 6.5")
    assert "already 6.5%" in html


def test_floor_refusals(conn, held):
    held.append(iid_pos(qty=9))
    cmds = make(conn)
    assert "holds no AAPL" in cmds.command("/floor AAPL 10")[0][0]
    assert "between 0.5% and 90%" in cmds.command("/floor CSCO 0")[0][0]
    assert "not a trailing alert" in cmds.command("/floor CSCO x")[0][0]


def test_floor_with_several_positions_asks_which(conn, held):
    held.extend([iid_pos(qty=9), iid_pos(ticker="AAPL", qty=3)])
    cmds = make(conn)
    [(html, kb)] = cmds.command("/floor")
    assert "CSCO" in html and "AAPL" in html and len(kb) == 2
    _, [(pick, kb2)] = press(cmds, kb, 1)
    assert "<b>AAPL</b>" in pick


def test_buy_on_london_uses_pounds_not_pence_for_the_stop(conn):
    class Pence(Market):                  # quote 9.48 GBP, bars in pence
        def chart(self, *a, **k):
            c = [930 + (i % 5) for i in range(30)]
            return {"high": [x + 15 for x in c], "low": [x - 15 for x in c],
                    "close": c}
    d = Desk()
    make(conn, d, market=Pence("9.48", "GBP")).command("/buy BNC LSE")
    b = d.calls[0][1]
    # ATR ≈ 0.32 GBP → stop ≈ 9.48 − 0.64, not capped at 24% below
    assert 8.7 < b["stop"] < 9.0 and b["target"] < 10.9


def test_floor_says_when_saxo_holds_its_own_trailing_stop(conn, held,
                                                          monkeypatch):
    """Claire only: /floor changes the desk's alert, never the trailing
    stop already resting at Saxo — and says so, like Portfolio's Edit."""
    held.append(iid_pos(qty=9))
    cmds = make(conn)
    monkeypatch.setattr(tc.portfolio, "legs_for", lambda *a: [])
    assert "Saxo holds a trailing stop" not in cmds.command("/floor CSCO")[0][0]
    monkeypatch.setattr(tc.portfolio, "legs_for", lambda *a: [
        {"leg": "stop", "price": 96.4, "trails": 8.25, "trail_pct": 8}])
    html, kb = cmds.command("/floor CSCO 12")[0]
    assert "Saxo holds a trailing stop" in html and "backup alert" in html
    _, sent = press(cmds, kb)
    assert "→ <b>12%</b>" in sent[0][0] and "keeps its distance" in sent[0][0]


def test_buy_reply_does_not_promise_an_alert_only(conn):
    html = make(conn).command("/buy CSCO")[0][0]
    assert "trailing stop that sells by itself" in html
    assert "never sells" not in html
