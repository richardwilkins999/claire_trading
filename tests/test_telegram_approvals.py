"""Telegram trade approvals (app/telegram_approvals.py, app/chart_png.py,
POST /api/telegram-decision). Offline: a fake Telegram transport and a fake
market; the claire-api resume is a recorded callable.

The properties that matter most:
* the approval token never appears in callback data (Telegram sees it);
* only the approver's USER id decides — not anyone in an allowed chat;
* nothing is submitted without the second (✔) tap, and live money needs
  LIVE typed within 2 minutes;
* stale buttons say so and change nothing;
* the audit event lands BEFORE claire-api's own rows, so a refusal stays
  parked (never auto-resent by the custodian).
"""
import json
import threading
from decimal import Decimal

import httpx
import pytest

from app import alerts, approvals, live_trading
from app import telegram_approvals as ta
from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.chart_png import render_chart

T0 = 1_793_725_000                  # 2026-11-04
OWNER = "123456789"
TOKEN = "tok_SECRET_abc123"
BASE = "http://192.168.1.50:7787"
ENV = {"SAXO_SIM_EXECUTION": "enabled", "SAXO_SIM_ACCOUNT_ID": "11111111",
       "CLAIRE_DASHBOARD_URL": BASE}


def bars(n=120, start=100.0):
    ts = [T0 - (n - i) * 86400 for i in range(n)]
    close = [start + (i % 7) - 3 + i * 0.05 for i in range(n)]
    return {"timestamps": ts, "open": [c - 0.5 for c in close],
            "high": [c + 1 for c in close], "low": [c - 1 for c in close],
            "close": close, "volume": [1e6 + i * 1e3 for i in range(n)],
            "currency": "USD"}


class FakeMarket:
    def quote(self, sym):
        return {"price": Decimal("103.67"), "previous_close": "101.61",
                "change_pct": 2.03, "currency": "USD"}

    def chart(self, sym, range_="6mo", interval="1d"):
        return bars()

    def fx(self, a, b):
        return Decimal(1)


class FakeTg:
    def __init__(self):
        self.photos, self.messages, self.edits = [], [], []
        self._mid = 100

    def photo(self, chat, png, caption=None):
        self._mid += 1
        self.photos.append((chat, png, caption))
        return {"message_id": self._mid}

    def message(self, chat, html, keyboard=None):
        self._mid += 1
        self.messages.append((chat, html, keyboard))
        return {"message_id": self._mid}

    def edit(self, chat, mid, html, keyboard=None):
        self.edits.append((chat, mid, html, keyboard))


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    r = Repo(c)
    for acct, broker in (("saxo-paper", "saxo"), ("alpaca-paper", "alpaca")):
        r.create_account(acct, broker, "paper", "USD",
                         fee_model={"type": "flat", "per_trade": "0"}, ts=T0)
        r.deposit(acct, to_micro("5000"), ts=T0)
    c.execute("INSERT INTO instruments (id, ticker, exchange, currency, name)"
              " VALUES ('NASDAQ:CSCO','CSCO','NASDAQ','USD','Cisco <Systems>')")
    return c


def awaiting(conn, wi="wi_1", direction="buy", kind="pipeline", trigger=None,
             conditions=None, entry=103.15, stop=96.4):
    Repo(conn).create_work_item(wi, kind, "CSCO", ts=T0)
    th = {"ticker": "CSCO", "direction": direction, "conviction": 0.72,
          "entry_low": min(102.5, entry), "entry_high": entry, "stop_loss": stop,
          "take_profit": 116.1, "currency": "USD",
          "conditions": conditions or ["Hold above SMA50 & <b>volume</b>",
                                       "Earnings on 12 Nov"]}
    conn.execute("UPDATE work_items SET state='awaiting_approval',"
                 " approval_token=?, token_state='minted', expires_at=?,"
                 " thesis_json=?, trigger=? WHERE id=?",
                 (TOKEN, T0 + 7200, json.dumps(th), trigger, wi))
    for agent, kind_, d in (
            ("technical", "analyst", {"signal": "bullish", "conviction": 0.6,
                                      "summary": "Reclaimed SMA20 <fast>"}),
            ("news", "analyst", {"signal": "neutral", "conviction": 0.3,
                                 "summary": "Quiet tape"}),
            ("bull", "debate", {"side": "bull", "conviction": 0.7}),
            ("bear", "debate", {"side": "bear", "conviction": 0.45})):
        conn.execute("INSERT INTO agent_reports (work_item_id, agent_id, kind,"
                     " payload, created_at) VALUES (?,?,?,?,?)",
                     (wi, agent, kind_, json.dumps(d), T0))


def failed(conn, wi="wi_f", broker="saxo"):
    Repo(conn).create_work_item(wi, "override", "CSCO", ts=T0)
    conn.execute("UPDATE work_items SET state='approved', approval_token=?,"
                 " token_state='pending_resume', thesis_json=? WHERE id=?",
                 (TOKEN, json.dumps({"direction": "buy", "entry_high": 103.15,
                                     "stop_loss": 96.97, "currency": "USD"}),
                  wi))
    for to, payload in (
            ("authorize:approve", {"size_base": 1000, "qty": None,
                                   "trail_pct": 8, "broker": broker}),
            ("approved", {"size_base": 1000.0}),
            ("resume_failed", {"error": "broker rejected order: quantity 9.6"
                                        " is not a whole multiple of the "
                                        "Saxo lot size 1"})):
        conn.execute("INSERT INTO events (item_id, ts, actor, from_state,"
                     " to_state, payload) VALUES (?,?,?,?,?,?)",
                     (wi, T0, "human", "approved", to, json.dumps(payload)))


SETTINGS = {"token": "bot:x", "approver_id": OWNER}


def push(conn, tg, clock=T0 + 60):
    return ta.push_once(conn, FakeMarket(), tg, OWNER, ENV,
                        clock=lambda: clock, base_url=BASE,
                        render=lambda f: b"\x89PNG fake")


def decide(conn, tg, data=None, *, from_id=OWNER, mid=None, live=None,
           resume=None, clock=T0 + 120):
    body = {"from_id": from_id, "chat_id": OWNER}
    if live is not None:
        body["live"] = live
    else:
        n = conn.execute("SELECT * FROM telegram_notices ORDER BY id DESC"
                         " LIMIT 1").fetchone()
        body.update(data=data, message_id=mid or n["message_id"])
    return ta.handle_decision(conn, FakeMarket(), body,
                              resume or (lambda b: (200, {"ok": True})),
                              env=ENV, clock=lambda: clock, tg=tg,
                              settings=SETTINGS)


def buttons(kb):
    return [b for row in kb for b in row]


def nid(conn):
    return conn.execute("SELECT MAX(id) FROM telegram_notices").fetchone()[0]


# ── chart ─────────────────────────────────────────────────────────────────
def test_chart_renderer_produces_a_png():
    png = render_chart(bars(), title="CSCO · NASDAQ", subtitle="6 months",
                       levels={"entry": 103.15, "stop": 96.4,
                               "target": 140.0, "last": 103.67},
                       currency="USD")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000


def test_chart_survives_gaps_and_refuses_no_data():
    c = bars(30)
    c["close"][5] = None                        # Yahoo nulls a bar now and then
    assert render_chart(c, title="x")[:4] == b"\x89PNG"
    with pytest.raises(ValueError):
        render_chart(bars(3), title="x")


# ── message ───────────────────────────────────────────────────────────────
def test_message_carries_the_decision_and_escapes_everything(conn):
    awaiting(conn, trigger="event scan: relative volume 5.2x <avg>")
    tg = FakeTg()
    push(conn, tg)
    assert len(tg.photos) == 1 and tg.photos[0][0] == OWNER
    [(chat, html, kb)] = tg.messages
    assert chat == OWNER and len(html) < 4096
    assert "<b>BUY CSCO</b>" in html and "NASDAQ" in html
    assert "Volume scan — relative volume 5.2x &lt;avg&gt;" in html
    assert "conviction high (0.72)" in html
    assert "103.67" in html and "+2.0% today" in html
    assert "Stop-loss:</b> 96.40 (-6.5%)" in html and "Take-profit:</b> 116.10 (+12.6%)" in html
    assert "Reward : risk</b> 1.9 : 1" in html
    # the purchase price is below the market: it waits for it
    assert "Purchase price:</b> 103.15 (-0.5% vs now — waits for that price or better)" in html
    # sized in shares, with what those shares could lose / make
    assert "9 shares ≈ 928.35 USD (whole shares)" in html
    assert "most you can lose <b>−60.75 USD</b>" in html
    assert "&lt;b&gt;volume&lt;/b&gt;" in html          # condition escaped
    assert "Cisco &lt;Systems&gt;" in html
    assert "technical</b> bullish 0.60 — Reclaimed SMA20 &lt;fast&gt;" in html
    assert "bull 0.70 vs bear 0.45" in html
    assert "Saxo SIM 11111111" in html and "paper" in html
    assert "SGT" in html and "left" in html
    assert "<script" not in html and TOKEN not in html


def test_origin_words():
    assert ta.origin("pipeline", None).startswith("Manual")
    assert ta.origin("sell_review", "watcher: trail 8% hit").startswith(
        "Exit review")
    assert ta.origin("override", "human override of wi_x").startswith(
        "Your override")
    assert ta.origin("pipeline", "screener agent: momentum").startswith(
        "Daily screen")
    assert ta.origin("pipeline", "event scan: 5x").startswith("Volume scan")


# ── keyboard ──────────────────────────────────────────────────────────────
def test_keyboard_has_sizes_accounts_reject_link_and_no_token(conn):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    kb = tg.messages[0][2]
    bs = buttons(kb)
    texts = [b["text"] for b in bs]
    # shares first, at the price the order fills at
    assert any(t.startswith("4 sh ≈") for t in texts)
    assert "9 sh ≈ 928 USD" in texts                              # whole shares
    assert "✓ Saxo SIM" in texts and "Claire sim (alpaca)" in texts       # default first
    assert "✖ Reject" in texts
    assert {"text": "Open Approvals ↗", "url": BASE + "/approvals"} in bs
    for b in bs:
        data = b.get("callback_data", "")
        assert TOKEN not in data and "tok" not in data
        assert len(data.encode()) <= 64
    # the token is not stored in what the notice remembers either
    assert TOKEN not in conn.execute("SELECT options_json FROM"
                                     " telegram_notices").fetchone()[0]


def ensure_suggested_column(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(work_items)")}
    if "suggested_json" not in cols:
        conn.execute("ALTER TABLE work_items ADD COLUMN suggested_json TEXT")


def test_suggested_size_is_read_defensively(conn):
    awaiting(conn)
    row = conn.execute("SELECT id, ticker FROM work_items").fetchone()
    assert ta.suggested_size(row) is None              # column absent
    ensure_suggested_column(conn)
    row = conn.execute("SELECT * FROM work_items").fetchone()
    assert ta.suggested_size(row) is None              # NULL
    conn.execute("UPDATE work_items SET suggested_json=?",
                 (json.dumps({"size_base": 1500}),))
    row = conn.execute("SELECT * FROM work_items").fetchone()
    assert ta.suggested_size(row) == 1500
    conn.execute("UPDATE work_items SET suggested_json='not json'")
    assert ta.suggested_size(conn.execute(
        "SELECT * FROM work_items").fetchone()) is None


def test_suggested_size_drives_the_buttons(conn):
    awaiting(conn)
    ensure_suggested_column(conn)
    conn.execute("UPDATE work_items SET suggested_json='{\"size_base\": 1500}'")
    tg = FakeTg()
    push(conn, tg)
    texts = [b["text"] for b in buttons(tg.messages[0][2])]
    assert texts[0] == "14 sh ≈ 1,444 USD" and "Suggested:</b> 14 shares" in tg.messages[0][1]


def test_sell_offers_all_and_half_of_what_the_account_holds(conn, monkeypatch):
    awaiting(conn, direction="sell", kind="sell_review",
             trigger="watcher: trailing floor hit")
    monkeypatch.setattr(ta, "holdings", lambda c, t: {"saxo-paper": 13.0})
    tg = FakeTg()
    push(conn, tg)
    texts = [b["text"] for b in buttons(tg.messages[0][2])]
    assert "All (13 sh ≈ 1,348 USD)" in texts and "Half (6 sh ≈ 622 USD)" in texts   # whole shares
    decide(conn, tg, ta.cb(nid(conn), "s", 1))
    sent = []
    p = json.loads(conn.execute("SELECT options_json FROM telegram_notices"
                                ).fetchone()[0])["pending"]
    decide(conn, tg, ta.cb(nid(conn), "ok", p["nonce"]),
           resume=lambda b: sent.append(b) or (200, {}))
    assert sent[0]["qty"] == 6.0 and sent[0]["size_base"] is None


def test_suggestion_preselects_account_qty_and_trailing_floor(
        conn, monkeypatch):
    awaiting(conn, direction="sell", kind="sell_review")
    ensure_suggested_column(conn)
    conn.execute("UPDATE work_items SET suggested_json=?", (json.dumps(
        {"qty": 4, "account_id": "alpaca-paper", "trail_pct": 5}),))
    monkeypatch.setattr(ta, "holdings",
                        lambda c, t: {"saxo-paper": 13.0, "alpaca-paper": 9.0})
    tg = FakeTg()
    push(conn, tg)
    html, kb = tg.messages[0][1], tg.messages[0][2]
    texts = [b["text"] for b in buttons(kb)]
    assert "✓ Claire sim (alpaca)" in texts and "Suggested (4 sh ≈ 415 USD)" in texts
    # an exit closes the position: no alert to trail (as on the dashboard)
    assert "Trailing alert" not in html
    assert not any(t.endswith("%") for t in texts)
    decide(conn, tg, ta.cb(nid(conn), "s", 0))
    assert "SELL 4 CSCO" in tg.edits[-1][2]
    assert "Trailing alert" not in tg.edits[-1][2]


# ── push bookkeeping ──────────────────────────────────────────────────────
def test_each_item_is_sent_once_per_state(conn):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    push(conn, tg)
    assert len(tg.messages) == 1
    failed(conn)
    push(conn, tg)
    assert len(tg.messages) == 2
    assert "REFUSED" in tg.messages[1][1]
    assert "whole lot" in tg.messages[1][1]               # plain words
    texts = [b["text"] for b in buttons(tg.messages[1][2])]
    assert "🔁 Retry same order" in texts and "✖ Cancel trade" in texts


def test_a_failed_send_is_retried_a_few_times(conn):
    awaiting(conn)

    class Down(FakeTg):
        def message(self, *a, **k):
            raise alerts.TelegramError(0, "ConnectError")
    push(conn, Down())
    tg = FakeTg()
    push(conn, tg, clock=T0 + 300)                       # after the backoff
    assert len(tg.messages) == 1


def test_decided_elsewhere_closes_the_message(conn):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    conn.execute("UPDATE work_items SET state='rejected',"
                 " token_state='burned'")
    push(conn, tg)
    [(_, _, html, kb)] = tg.edits
    assert "Closed:" in html and "rejected" in html and kb is None


# ── authorization ─────────────────────────────────────────────────────────
def test_only_the_approvers_user_id_may_decide(conn):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    sent = []
    for who in ("999", "-1009876543", "", "1234567890"):
        code, out = decide(conn, tg, ta.cb(nid(conn), "rj"), from_id=who,
                           resume=lambda b: sent.append(b) or (200, {}))
        assert code == 403 and not out["ok"]
    assert tg.edits == [] and sent == []
    assert conn.execute("SELECT COUNT(*) FROM events WHERE to_state LIKE"
                        " 'telegram:%'").fetchone()[0] == 0


def test_approver_defaults_to_the_private_chat_id(tmp_path):
    p = tmp_path / "telegram.env"
    p.write_text("TELEGRAM_BOT_TOKEN=x\nTELEGRAM_CHAT_ID=-1009876543,"
                 "123456789\n")
    assert alerts.telegram_settings(p)["approver_id"] == "123456789"
    p.write_text("TELEGRAM_BOT_TOKEN=x\nTELEGRAM_CHAT_ID=-1\n"
                 "TELEGRAM_APPROVER_ID=42\n")
    assert alerts.telegram_settings(p)["approver_id"] == "42"
    p.write_text("TELEGRAM_BOT_TOKEN=x\nTELEGRAM_CHAT_ID=-1\n")
    assert alerts.telegram_settings(p)["approver_id"] is None


def test_a_button_from_another_message_is_refused(conn):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    code, _ = decide(conn, tg, ta.cb(nid(conn), "rj"), mid=12345)
    assert code == 404 and tg.edits == []


# ── two-step confirm ──────────────────────────────────────────────────────
def test_size_tap_only_confirms_then_tick_approves(conn):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    sent = []

    def resume(b):
        # what claire-api does: append its own rows after the resume
        conn.execute("INSERT INTO events (item_id, ts, actor, from_state,"
                     " to_state) VALUES ('wi_1', ?, 'human',"
                     " 'awaiting_approval', 'approved')", (T0,))
        sent.append(b)
        return 200, {"ok": True}
    code, out = decide(conn, tg, ta.cb(nid(conn), "s", 1), resume=resume)
    assert code == 200 and sent == []                    # nothing submitted
    html, kb = tg.edits[-1][2], tg.edits[-1][3]
    assert ("Confirm:</b> BUY 9 CSCO ≈ 928.35 USD, purchase price 103.15 USD,"
            " stop-loss 96.40, take-profit 116.10") in html
    assert "Saxo SIM 11111111" in html and "paper" in html
    ok = kb[0][0]
    assert ok["text"] == "✔ Place it" and kb[0][1]["text"] == "✖ Back"
    assert TOKEN not in ok["callback_data"]
    code, out = decide(conn, tg, ok["callback_data"], resume=resume)
    assert code == 200 and out["ok"]
    [b] = sent
    # the exact shares are bought; the cash check holds them at the worse
    # of the purchase price and the live price (9 × 103.67)
    assert b["token"] == TOKEN and b["qty"] == 9 and b["size_base"] == 933.03
    assert b["broker"] == "saxo" and b["trail_pct"] == 8
    rows = [dict(r) for r in conn.execute(
        "SELECT actor, to_state, payload FROM events WHERE item_id='wi_1'"
        " ORDER BY id")]
    states = [r["to_state"] for r in rows]
    # gate's authorize row, then ours, then claire-api's — in that order
    assert states[-3:] == ["authorize:approve", "telegram:approve",
                           "approved"]
    ev = rows[-2]
    assert ev["actor"] == "human"
    assert json.loads(ev["payload"])["channel"] == "telegram"
    assert "APPROVED via Telegram" in tg.edits[-1][2]
    assert tg.edits[-1][3] is None                       # buttons gone


def test_back_restores_and_a_stale_nonce_is_refused(conn):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    decide(conn, tg, ta.cb(nid(conn), "s", 0))
    old_ok = tg.edits[-1][3][0][0]["callback_data"]
    decide(conn, tg, ta.cb(nid(conn), "bk"))
    assert any(b["text"] == "✖ Reject" for b in buttons(tg.edits[-1][3]))
    sent = []
    code, out = decide(conn, tg, old_ok,
                       resume=lambda b: sent.append(b) or (200, {}))
    assert code == 409 and sent == []


def test_reject_is_two_step_too(conn):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    sent = []
    r = (lambda b: sent.append(b) or (200, {}))
    decide(conn, tg, ta.cb(nid(conn), "rj"), resume=r)
    assert sent == []
    decide(conn, tg, tg.edits[-1][3][0][0]["callback_data"], resume=r)
    assert sent[0]["status"] == "rejected" and sent[0]["token"] == TOKEN


def test_switching_account_changes_the_order(conn):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    decide(conn, tg, ta.cb(nid(conn), "a", 1))          # alpaca built-in sim
    assert "Claire sim · alpaca (simulated, not a broker)" in tg.edits[-1][2]
    assert any(b["text"] == "✓ Claire sim (alpaca)" for b in buttons(tg.edits[-1][3]))
    decide(conn, tg, ta.cb(nid(conn), "s", 1))
    assert "BUY 9.6 CSCO" in tg.edits[-1][2]             # 0.1-share fractions


def test_a_refused_size_keeps_the_buttons(conn):
    awaiting(conn)
    Repo(conn).withdraw("saxo-paper", to_micro("4500"), ts=T0)
    tg = FakeTg()
    push(conn, tg)
    # only 500 fits now; make it not fit after the message went out
    Repo(conn).withdraw("saxo-paper", to_micro("400"), ts=T0)
    decide(conn, tg, ta.cb(nid(conn), "s", 0))
    code, out = decide(conn, tg, tg.edits[-1][3][0][0]["callback_data"])
    assert "exceeds" in out["answer"]
    assert "Not placed" in tg.edits[-1][2] and tg.edits[-1][3]   # still live
    assert conn.execute("SELECT status FROM telegram_notices").fetchone()[0] \
        == "sent"


# ── stale buttons ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("change", [
    "UPDATE work_items SET state='rejected', token_state='burned'",
    "UPDATE work_items SET expires_at=1",
    "UPDATE work_items SET thesis_json='{\"direction\":\"buy\"}'",
])
def test_stale_buttons_say_already_decided(conn, change):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    decide(conn, tg, ta.cb(nid(conn), "s", 0))
    ok = tg.edits[-1][3][0][0]["callback_data"]
    conn.execute(change)
    sent = []
    code, out = decide(conn, tg, ok,
                       resume=lambda b: sent.append(b) or (200, {}))
    assert sent == [] and out["answer"].startswith("Already decided")
    assert "Closed:" in tg.edits[-1][2] and tg.edits[-1][3] is None
    code, out = decide(conn, tg, ok)                     # and stays closed
    assert out["answer"].startswith("Already decided")


# ── live money ────────────────────────────────────────────────────────────
@pytest.fixture
def live(conn, monkeypatch):
    Repo(conn).create_account("saxo-live", "saxo_live", "live", "USD",
                              fee_model={"type": "flat", "per_trade": "0"},
                              ts=T0, allow_live=True)
    Repo(conn).deposit("saxo-live", to_micro("3000"), ts=T0)
    st = {"armed": True, "summary": "LIVE TRADING ARMED"}
    monkeypatch.setattr(live_trading, "status", lambda c, e, **k: st)
    return st


def test_live_needs_live_typed_within_two_minutes(conn, live):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    texts = [b["text"] for b in buttons(tg.messages[0][2])]
    assert "⚠ LIVE" in texts and texts.index("⚠ LIVE") > texts.index(
        "✓ Saxo SIM")                                   # never the default
    decide(conn, tg, ta.cb(nid(conn), "a", 2))
    decide(conn, tg, ta.cb(nid(conn), "s", 0))
    assert "REAL MONEY" in tg.edits[-1][2]
    sent = []
    r = (lambda b: sent.append(b) or (200, {}))
    code, out = decide(conn, tg, tg.edits[-1][3][0][0]["callback_data"],
                       resume=r)
    assert out.get("await_live") and sent == []
    assert "Reply <code>LIVE</code>" in tg.edits[-1][2]
    # wrong word / wrong person do nothing
    assert decide(conn, tg, live="live", resume=r)[0] == 400
    assert decide(conn, tg, live="LIVE", from_id="999", resume=r)[0] == 403
    assert sent == []
    code, out = decide(conn, tg, live="LIVE", resume=r, clock=T0 + 150)
    assert out["ok"] and sent[0]["broker"] == "saxo_live"
    ev = conn.execute("SELECT payload FROM events WHERE item_id='wi_1' AND"
                      " to_state='authorize:approve'").fetchone()
    assert json.loads(ev["payload"])["confirm_live"] == "LIVE"


def test_live_typed_too_late_places_nothing(conn, live):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    decide(conn, tg, ta.cb(nid(conn), "a", 2))
    decide(conn, tg, ta.cb(nid(conn), "s", 0))
    decide(conn, tg, tg.edits[-1][3][0][0]["callback_data"])
    sent = []
    code, out = decide(conn, tg, live="LIVE", clock=T0 + 120 + 121,
                       resume=lambda b: sent.append(b) or (200, {}))
    assert sent == [] and "Too late" in out["answer"]


# ── refused trades: retry / cancel ────────────────────────────────────────
def test_retry_resends_the_same_approval_and_stays_parked_if_refused(conn):
    failed(conn)
    tg = FakeTg()
    push(conn, tg)

    def refused_again(b):
        conn.execute("INSERT INTO events (item_id, ts, actor, from_state,"
                     " to_state, payload) VALUES ('wi_f', ?, 'system',"
                     " 'approved', 'resume_failed', ?)",
                     (T0 + 200, json.dumps({"error": "still refused"})))
        return 500, {"detail": "resume failed: still refused"}
    decide(conn, tg, ta.cb(nid(conn), "rt"))
    code, out = decide(conn, tg, tg.edits[-1][3][0][0]["callback_data"],
                       resume=refused_again)
    assert "still refused" in out["answer"]
    states = [r[0] for r in conn.execute(
        "SELECT to_state FROM events WHERE item_id='wi_f' ORDER BY id")]
    assert states[-2:] == ["telegram:retry", "resume_failed"]
    # still parked for the human: listed, and NOT auto-resent
    assert [f["id"] for f in approvals.failed_approvals(
        conn, clock=lambda: T0 + 300)] == ["wi_f"]
    auto = []
    approvals.retry_pending(conn, lambda b: auto.append(b) or (200, {}),
                            clock=lambda: T0 + 300)
    assert auto == []
    push(conn, tg, clock=T0 + 300)                       # new refusal → new msg
    assert len(tg.messages) == 2


def test_cancel_a_refused_trade(conn):
    failed(conn)
    tg = FakeTg()
    push(conn, tg)
    decide(conn, tg, ta.cb(nid(conn), "cx"))
    code, out = decide(conn, tg, tg.edits[-1][3][0][0]["callback_data"])
    assert out["ok"]
    assert conn.execute("SELECT state FROM work_items WHERE id='wi_f'"
                        ).fetchone()[0] == "rejected"
    ev = conn.execute("SELECT actor, payload FROM events WHERE"
                      " to_state='telegram:cancel'").fetchone()
    assert ev["actor"] == "human" and \
        json.loads(ev["payload"])["channel"] == "telegram"


# ── the keyed endpoint ────────────────────────────────────────────────────
DKEY = "dash-key-tg"


@pytest.fixture
def web(conn, monkeypatch):
    from app.dashboards import create_server
    from app.providers import seeds
    seeds.seed(conn, ts=T0)
    monkeypatch.setattr(alerts, "telegram_settings", lambda *a, **k: SETTINGS)
    tg = FakeTg()
    monkeypatch.setattr(ta, "Telegram", lambda token: tg)
    srv = create_server(conn, Repo(conn), FakeMarket(), secret="s",
                        dash_key=DKEY, api_base="http://127.0.0.1:9",
                        clock=lambda: T0 + 120, port=0, sse_interval=None,
                        env=ENV)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tg
    srv.shutdown()


def test_decision_endpoint_requires_the_key_and_the_approver(conn, web):
    base, tg = web
    awaiting(conn)
    push(conn, tg)
    n = conn.execute("SELECT * FROM telegram_notices").fetchone()
    body = {"from_id": OWNER, "chat_id": OWNER, "message_id": n["message_id"],
            "data": ta.cb(n["id"], "rj")}
    with httpx.Client(timeout=5) as c:
        for hdr in ({}, {"X-Dash-Key": "wrong"}):
            assert c.post(base + "/api/telegram-decision", json=body,
                          headers=hdr).status_code == 403
        r = c.post(base + "/api/telegram-decision",
                   json={**body, "from_id": "999"},
                   headers={"X-Dash-Key": DKEY})
        assert r.status_code == 403
        r = c.post(base + "/api/telegram-decision", json=body,
                   headers={"X-Dash-Key": DKEY})
        assert r.status_code == 200 and r.json()["ok"]
        assert "Reject</b> BUY CSCO?" in tg.edits[-1][2]
        # ✔ reaches thesis_action; claire-api (port 9) is down → recorded,
        # custodian finishes it
        r = c.post(base + "/api/telegram-decision", json={
            **body, "data": tg.edits[-1][3][0][0]["callback_data"]},
            headers={"X-Dash-Key": DKEY})
        assert r.status_code == 200 and "unreachable" in r.json()["answer"]
    assert conn.execute("SELECT token_state FROM work_items").fetchone()[0] \
        == "pending_resume"


def test_route_name_stays_clear_of_the_override_prefix():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app" / "dash" /
           "handler.py").read_text()
    i = src.index("/api/telegram-decision")
    assert "_paired()" in src[i:i + 400]
    assert not "/api/telegram-decision".startswith("/api/override")


# ── price triggers (app/price_triggers.py) ───────────────────────────────
def trigger_card(conn, wi="wi_t", mode="ask", fired=True):
    """What Desk.manual_trade(source="trigger") + the engine leave behind."""
    awaiting(conn, wi=wi, kind="manual",
             trigger="manual buy from trigger: trigger #7 ≤ 5.00 hit at 4.98")
    conn.execute("UPDATE work_items SET created_at=? WHERE id=?", (T0, wi))
    conn.execute(
        "INSERT INTO price_triggers (id, ticker, exchange, direction, kind,"
        " condition, trigger_price, limit_price, qty, broker, account_id,"
        " mode, status, created_at, created_by, expires_at, fired_at,"
        " fired_price, work_item_id, updated_at) VALUES (7,'CSCO','NASDAQ',"
        "'buy','buy','lte',?,?,?,'saxo','saxo-paper',?,'fired',?,'human',?,"
        "?,?,?,?)",
        (to_micro("5"), to_micro("5"), to_micro("50"), mode, T0 - 3600,
         T0 + 86400, T0, to_micro("4.98") if fired else None, wi, T0))


def test_trigger_cards_wait_out_the_grace_period(conn):
    trigger_card(conn)
    tg = FakeTg()
    push(conn, tg, clock=T0 + 10)                 # auto mode may still claim it
    assert tg.messages == []
    assert conn.execute("SELECT COUNT(*) FROM telegram_notices"
                        ).fetchone()[0] == 0
    push(conn, tg, clock=T0 + ta.TRIGGER_GRACE_S + 1)
    assert len(tg.messages) == 1


def test_trigger_card_by_text_alone_also_waits(conn):
    # the engine links price_triggers.work_item_id a moment AFTER the card
    # exists: the trigger text alone must already hold it back
    awaiting(conn, wi="wi_x", kind="manual",
             trigger="manual buy from trigger: trigger #9 ≥ 7.00 hit at 7.01")
    conn.execute("UPDATE work_items SET created_at=?", (T0,))
    tg = FakeTg()
    push(conn, tg, clock=T0 + 5)
    assert tg.messages == []


def test_pre_approved_cards_are_never_pushed(conn):
    trigger_card(conn, mode="auto")
    conn.execute("INSERT INTO events (item_id, ts, actor, from_state,"
                 " to_state, payload) VALUES ('wi_t', ?, 'human',"
                 " 'awaiting_approval', 'pre_approved', '{}')", (T0,))
    tg = FakeTg()
    push(conn, tg, clock=T0 + 600)               # long after the grace
    assert tg.messages == [] and tg.photos == []


def test_state_is_rechecked_right_before_sending(conn, monkeypatch):
    trigger_card(conn, mode="auto")
    real = ta.gather

    def slow_gather(*a, **k):                    # the engine wins the race
        out = real(*a, **k)
        conn.execute("INSERT INTO events (item_id, ts, actor, from_state,"
                     " to_state) VALUES ('wi_t', ?, 'human',"
                     " 'awaiting_approval', 'pre_approved')", (T0,))
        return out
    monkeypatch.setattr(ta, "gather", slow_gather)
    tg = FakeTg()
    push(conn, tg, clock=T0 + 600)
    assert tg.messages == [] and tg.photos == []
    assert conn.execute("SELECT COUNT(*) FROM telegram_notices"
                        ).fetchone()[0] == 0


def test_an_ask_mode_trigger_card_shows_the_rule(conn):
    trigger_card(conn, mode="ask")
    tg = FakeTg()
    push(conn, tg, clock=T0 + 600)
    html = tg.messages[0][1]
    assert "Your trigger #7:</b> Buy 50 CSCO when price ≤ 5.00" in html
    assert "fired at 4.98" in html and "asks before trading" in html
    assert "Your price trigger" in html                 # started by
    assert "your manual trade" in html and "conviction" not in html


def test_a_card_approved_elsewhere_after_the_push_is_stale(conn):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    conn.execute("UPDATE work_items SET token_state='pending_resume'")
    code, out = decide(conn, tg, ta.cb(nid(conn), "rj"))
    assert "being placed" in out["answer"] and tg.edits[-1][3] is None


# ── trailing floor on the card ────────────────────────────────────────────
def floor_buttons(kb, word="trail"):
    """The trailing-% buttons. Claire: on an account whose stops rest at
    Saxo (the fixtures' default, Saxo SIM) they read "trail …" because the %
    becomes a trailing STOP there; elsewhere "alert …"."""
    return [b for b in buttons(kb)
            if b["text"].lstrip("✓ ").startswith(word + " ")]


def test_buy_card_offers_floors_and_the_choice_is_what_gets_approved(conn):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    html, kb = tg.messages[0][1], tg.messages[0][2]
    assert "Trailing stop 8%" in html and "sells by itself" in html
    assert "never sells" not in html
    assert [b["text"] for b in floor_buttons(kb)] == [
        "trail 5%", "✓ trail 8%", "trail 12%", "trail 15%"]
    code, out = decide(conn, tg, ta.cb(nid(conn), "tf", "12"))
    assert code == 200 and out["answer"] == "Trailing stop: 12%"
    assert "✓ trail 12%" in [b["text"] for b in floor_buttons(tg.edits[-1][3])]
    decide(conn, tg, ta.cb(nid(conn), "s", 1))
    assert "Trailing stop <b>12%</b> — it sells by itself" in tg.edits[-1][2]
    sent = []
    decide(conn, tg, tg.edits[-1][3][0][0]["callback_data"],
           resume=lambda b: sent.append(b) or (200, {"ok": True}))
    assert sent[0]["trail_pct"] == 12


def test_the_trailing_words_follow_where_the_stop_rests(conn):
    """Claire only (Saxo trailing stops): the same % is an ORDER that sells
    by itself on a Saxo account and only an alert on the built-in sim, and
    the card must not promise "never sells" where Saxo will sell."""
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    assert "Trailing stop 8% on Saxo SIM" in tg.messages[0][1]
    assert "never sells" not in tg.messages[0][1]
    decide(conn, tg, ta.cb(nid(conn), "a", 1))          # alpaca built-in sim
    html, kb = tg.edits[-1][2], tg.edits[-1][3]
    note = html[html.rindex("<b>Account:</b>"):]        # under the card
    assert "<b>Trailing alert:</b> 8%" in note and "it never sells" in note
    assert "sells by itself" not in note
    assert [b["text"] for b in floor_buttons(kb, "alert")] == [
        "alert 5%", "✓ alert 8%", "alert 12%", "alert 15%"]
    assert floor_buttons(kb) == []
    code, out = decide(conn, tg, ta.cb(nid(conn), "tf", "12"))
    assert out["answer"] == "Trailing alert: 12%"
    decide(conn, tg, ta.cb(nid(conn), "s", 1))
    assert "Trailing alert <b>12%</b>." in tg.edits[-1][2]


def test_no_stop_loss_means_an_alert_even_at_saxo():
    """Saxo's trailing stop starts from the stop-loss; without one nothing
    is sent there, so the % is the watcher's alert."""
    acct = {"venue_stops": True, "local_exchanges": ["SGX"]}
    f = {"exchange": "NASDAQ", "thesis": {"stop_loss": 96.4}}
    assert ta.trail_is_stop(f, acct) and ta.trail_word(f, acct) == "trailing stop"
    assert not ta.trail_is_stop({**f, "thesis": {}}, acct)
    assert not ta.trail_is_stop({**f, "exchange": "SGX"}, acct)   # desk-held
    assert not ta.trail_is_stop(f, {"venue_stops": False})
    assert not ta.trail_is_stop(f, None)
    assert ta.trail_word(f, None) == "trailing alert"


def test_a_floor_that_is_not_offered_is_refused(conn):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    assert decide(conn, tg, ta.cb(nid(conn), "tf", "33"))[0] == 400
    assert decide(conn, tg, ta.cb(nid(conn), "tf", "x"))[0] == 400


def test_suggested_floor_is_preselected_and_offered(conn):
    awaiting(conn, kind="manual")
    ensure_suggested_column(conn)
    conn.execute("UPDATE work_items SET suggested_json=?",
                 (json.dumps({"trail_pct": 10}),))
    tg = FakeTg()
    push(conn, tg)
    texts = [b["text"] for b in floor_buttons(tg.messages[0][2])]
    assert "✓ trail 10%" in texts and "trail 8%" in texts


def test_picking_a_floor_drops_a_pending_confirmation(conn):
    awaiting(conn)
    tg = FakeTg()
    push(conn, tg)
    decide(conn, tg, ta.cb(nid(conn), "s", 1))
    old_ok = tg.edits[-1][3][0][0]["callback_data"]
    decide(conn, tg, ta.cb(nid(conn), "tf", "5"))
    assert decide(conn, tg, old_ok)[0] == 409      # must confirm again


def test_retry_can_change_the_floor(conn):
    failed(conn)
    tg = FakeTg()
    push(conn, tg)
    kb = tg.messages[0][2]
    assert "✓ trail 8%" in [b["text"] for b in floor_buttons(kb)]
    decide(conn, tg, ta.cb(nid(conn), "tf", "15"))
    decide(conn, tg, ta.cb(nid(conn), "rt"))
    assert "trailing stop <b>15%</b>" in tg.edits[-1][2]
    sent = []
    decide(conn, tg, tg.edits[-1][3][0][0]["callback_data"],
           resume=lambda b: sent.append(b) or (200, {"ok": True}))
    assert sent[0]["trail_pct"] == 15 and sent[0]["size_base"] == 1000


# ── a purchase price above the market; levels on the wrong side ───────────
def test_a_purchase_price_above_the_market_is_sized_at_the_market(conn):
    """A buy limit above the market fills at once at about the market: the
    shares and cost are counted there (as on the dashboard card), the cash
    check holds them at the purchase price."""
    awaiting(conn, entry=110.0)
    tg = FakeTg()
    push(conn, tg)
    html = tg.messages[0][1]
    assert ("Purchase price:</b> 110.00 — above the current price, so it"
            " fills straight away at about 103.67") in html
    decide(conn, tg, ta.cb(nid(conn), "s", 1))                 # ~1,000 USD
    assert "BUY 9 CSCO ≈ 933.03 USD, purchase price 110.00 USD (fills now" \
        " at about 103.67)" in tg.edits[-1][2]
    sent = []
    decide(conn, tg, tg.edits[-1][3][0][0]["callback_data"],
           resume=lambda b: sent.append(b) or (200, {"ok": True}))
    assert sent[0]["qty"] == 9 and sent[0]["size_base"] == 990.0


def test_a_stop_loss_above_where_it_fills_is_flagged_and_not_offered(conn):
    awaiting(conn, entry=110.0, stop=105.0)
    tg = FakeTg()
    push(conn, tg)
    html = tg.messages[0][1]
    assert "stop-loss 105.00 is not below the current price, where it fills" \
        " (103.67)" in html.replace("The stop-loss", "stop-loss")
    code, out = decide(conn, tg, ta.cb(nid(conn), "s", 0))
    assert code == 400 and "straight away" in out["answer"]
