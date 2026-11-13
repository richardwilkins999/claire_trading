"""Claire's own Telegram bot (app/telegram_bot.py): Telegram updates are
turned into the same handle_decision calls Bob's relay makes. Offline: a fake
Bot API `call`, the fake transport/market from test_telegram_approvals."""
import json

import pytest

from app import alerts
from app import telegram_approvals as ta
from app import telegram_bot as tb

from .test_telegram_approvals import (ENV, OWNER, SETTINGS, T0, TOKEN,
                                      FakeMarket, FakeTg, awaiting, conn,
                                      live, nid, push)  # noqa: F401

ST = {**SETTINGS, "poll": True}


class FakeApi:
    def __init__(self, updates=()):
        self.calls, self.updates = [], list(updates)

    def __call__(self, token, method, data=None, **k):
        self.calls.append((method, dict(data or {})))
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        return True

    def of(self, method):
        return [d for m, d in self.calls if m == method]


def tap(conn, data, *, from_id=OWNER, uid=1):
    n = conn.execute("SELECT * FROM telegram_notices ORDER BY id DESC"
                     " LIMIT 1").fetchone()
    return {"update_id": uid, "callback_query": {
        "id": f"cq{uid}", "from": {"id": int(from_id)}, "data": data,
        "message": {"message_id": n["message_id"],
                    "chat": {"id": int(OWNER), "type": "private"}}}}


def text(t, *, from_id=OWNER, chat_type="private", uid=1):
    return {"update_id": uid, "message": {
        "message_id": 900 + uid, "text": t, "from": {"id": int(from_id)},
        "chat": {"id": int(from_id), "type": chat_type}}}


@pytest.fixture
def tg(monkeypatch):
    t = FakeTg()
    monkeypatch.setattr(ta, "Telegram", lambda token: t)
    return t


def run(conn, upd, api, resume=None, clock=T0 + 120):
    return tb.handle_update(conn, FakeMarket(), upd, ST,
                            resume or (lambda b: (200, {"ok": True})),
                            env=ENV, call=api, clock=lambda: clock)


def test_taps_go_through_the_two_step_approval(conn, tg):
    awaiting(conn)
    push(conn, tg)
    api, sent = FakeApi(), []
    resume = (lambda b: sent.append(b) or (200, {"ok": True}))
    assert run(conn, tap(conn, ta.cb(nid(conn), "s", 1)), api, resume) \
        == "callback:200"
    assert sent == []                                   # only a confirmation
    [ans] = api.of("answerCallbackQuery")
    assert ans["callback_query_id"] == "cq1" and "tap ✔" in ans["text"]
    ok = tg.edits[-1][3][0][0]["callback_data"]
    run(conn, tap(conn, ok, uid=2), api, resume)
    [b] = sent
    assert b["token"] == TOKEN and b["qty"] == 9 and b["size_base"] == 933.03


def test_someone_else_tapping_decides_nothing(conn, tg):
    awaiting(conn)
    push(conn, tg)
    api, sent = FakeApi(), []
    assert run(conn, tap(conn, ta.cb(nid(conn), "rj"), from_id="999"), api,
               lambda b: sent.append(b)) == "callback:403"
    assert sent == []
    assert api.of("answerCallbackQuery")[0]["show_alert"] == "true"


def test_live_reply_is_forwarded_only_from_the_approver(conn, tg, live):
    awaiting(conn)
    push(conn, tg)
    api, sent = FakeApi(), []
    resume = (lambda b: sent.append(b) or (200, {}))
    run(conn, tap(conn, ta.cb(nid(conn), "a", 2)), api)
    run(conn, tap(conn, ta.cb(nid(conn), "s", 0)), api)
    run(conn, tap(conn, tg.edits[-1][3][0][0]["callback_data"]), api)
    assert run(conn, text("LIVE", from_id="999"), api, resume) == "ignored"
    assert run(conn, text("LIVE", chat_type="group"), api, resume) \
        == "ignored"
    assert run(conn, text("hello"), api, resume) == "ignored"
    assert sent == []
    assert run(conn, text("LIVE"), api, resume, clock=T0 + 150) \
        == "live:200"
    assert sent[0]["broker"] == "saxo_live"


def test_start_tells_anyone_their_id_and_nothing_else(conn, tg):
    api = FakeApi()
    assert run(conn, text("/start", from_id="555"), api) == "id"
    [m] = api.of("sendMessage")
    assert "555" in m["text"] and TOKEN not in m["text"]


def test_poller_idles_without_poll_flag(conn, tmp_path):
    api = FakeApi()
    tb.run_poller(conn, FakeMarket(), ENV, None, tmp_path / "off",
                  call=api, settings_fn=lambda: {**ST, "poll": False},
                  once=True)
    assert api.calls == []


def test_poller_advances_and_persists_the_offset(conn, tg, tmp_path):
    awaiting(conn)
    push(conn, tg)
    off = tmp_path / "off"
    api = FakeApi([tap(conn, ta.cb(nid(conn), "rj"), uid=41)])
    tb.run_poller(conn, FakeMarket(), ENV, lambda b: (200, {}), off,
                  call=api, settings_fn=lambda: ST, once=True)
    assert off.read_text() == "42"
    assert "offset" not in api.of("getUpdates")[0]
    tb.run_poller(conn, FakeMarket(), ENV, lambda b: (200, {}), off,
                  call=api, settings_fn=lambda: ST, once=True)
    assert api.of("getUpdates")[1]["offset"] == "42"


def test_poll_flag_is_read_from_telegram_env(tmp_path):
    p = tmp_path / "telegram.env"
    p.write_text("TELEGRAM_BOT_TOKEN=x\nTELEGRAM_CHAT_ID=42\n")
    assert alerts.telegram_settings(p)["poll"] is False
    p.write_text("TELEGRAM_BOT_TOKEN=x\nTELEGRAM_CHAT_ID=42\nTELEGRAM_POLL=1\n")
    assert alerts.telegram_settings(p)["poll"] is True


def test_without_an_approver_id_only_answers_and_decides_nothing(conn, tg):
    st = {"token": "bot:x", "approver_id": None, "poll": True}
    api = FakeApi([text("/id", from_id="777", uid=1),
                   text("LIVE", from_id="777", uid=2)])
    for upd in list(api.updates):
        tb.handle_update(conn, FakeMarket(), upd, st, lambda b: 1 / 0,
                         env=ENV, call=api)
    [m] = api.of("sendMessage")
    assert "777" in m["text"] and "TELEGRAM_CHAT_ID" in m["text"]
