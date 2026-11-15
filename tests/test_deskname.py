"""The desk's name is a setting (app/deskname.py), applied where words leave
the desk: pages and their data, Telegram, notifications, model prompts."""
import contextlib
import threading

import httpx
import pytest

from app import alerts, deskname
from app.accounting import db
from app.accounting.repo import Repo
from app.providers import registry

RON = {"CLAIRE_ASSISTANT_NAME": "Ronathan"}


def test_default_is_claire_and_nothing_is_touched():
    assert deskname.check({}) == ("Claire", "Claire")
    assert deskname.version({}) == ""
    text = "Claire asks you before selling"
    assert deskname.brand(text, {}) is text
    assert deskname.brand_bytes(b"Claire", {}) == b"Claire"


def test_only_the_whole_word_is_renamed():
    b = lambda s: deskname.brand(s, RON)                    # noqa: E731
    assert b("Claire asks you") == "Ronathan asks you"
    assert b("Claire's sim — ask Claire.") == "Ronathan's sim — ask Ronathan."
    # identifiers, settings, routes and files keep the word
    for same in ("claire_chat", "x-claire-secret", "/claire", "claire.html",
                 "CLAIRE_DESK_NAME", "askClaire()", "Claire_sim", "bin/claire"):
        assert b(same) == same, same
    assert deskname.brand_bytes(b'{"t":"Claire \xe2\x80\x94 Portfolio"}', RON) \
        == b'{"t":"Ronathan \xe2\x80\x94 Portfolio"}'
    assert b(None) is None and b(12) == 12


@pytest.mark.parametrize("bad", ["<b>x</b>", "9lives", "a" * 25, "O'Brien",
                                 'x"; alert(1)//', "Ron\nathan"])
def test_a_name_that_is_not_plain_is_refused_at_start(bad):
    for key in ("CLAIRE_DESK_NAME", "CLAIRE_ASSISTANT_NAME"):
        with pytest.raises(ValueError, match=key):
            deskname.check({key: bad})
        # ...and a running desk falls back to the default, never dies on it
        assert deskname.brand("Claire", {key: bad}) == "Claire"


def test_names_with_spaces_and_hyphens_are_fine():
    assert deskname.desk({"CLAIRE_DESK_NAME": " Desk Two "}) == "Desk Two"
    assert deskname.brand("Claire", {"CLAIRE_ASSISTANT_NAME": "Anne-Marie"}) \
        == "Anne-Marie"


def test_the_chat_prompt_and_telegram_say_the_name(monkeypatch):
    monkeypatch.setenv("CLAIRE_ASSISTANT_NAME", "Ronathan")
    assert registry.render_prompt("You are Claire, the desk's voice.") == \
        "You are Ronathan, the desk's voice."
    sent = []

    class C:
        def post(self, url, data=None, files=None):
            sent.append(data)
            return httpx.Response(200, json={"ok": True, "result": 1})
    alerts.tg_call("t", "sendMessage", client=C(), data={
        "chat_id": "1", "text": "Claire desk bot. Claire's sim sold it.",
        "parse_mode": "HTML"})
    assert sent[0]["text"] == "Ronathan desk bot. Ronathan's sim sold it."
    assert sent[0]["chat_id"] == "1" and sent[0]["parse_mode"] == "HTML"


@contextlib.contextmanager
def serve(env):
    from app.dashboards import create_server

    class M:
        def spark(self, syms):
            return {}

        def quote(self, s):
            return {}
    conn = db.connect(":memory:")
    db.init(conn)
    srv = create_server(conn, Repo(conn), M(), secret="s", dash_key="k",
                        api_base="http://127.0.0.1:9", port=0,
                        sse_interval=None, env=env)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def test_pages_scripts_and_data_carry_the_name():
    with serve(RON) as base:
        page = httpx.get(base + "/portfolio", timeout=5)
        nav = httpx.get(base + "/nav.js", timeout=5)
        ov = httpx.get(base + "/api/overview", timeout=5)
    assert "<title>Ronathan — Portfolio</title>" in page.text
    assert "Claire" not in page.text.replace("claire", "")
    assert "cannot reach Ronathan" in nav.text
    assert "x-claire-secret" not in nav.text and "/claire" in nav.text
    assert any("claire-api" in f for f in ov.json()["flags"])   # an identifier
    # the name is part of the page's version, so a rename is not served stale
    assert "Ronathan" in page.headers["ETag"]
    with serve({}) as base:
        page = httpx.get(base + "/portfolio", timeout=5)
    assert "<title>Claire — Portfolio</title>" in page.text
    assert "Claire" not in page.headers["ETag"]


# ── the desk and the assistant that runs it can have different names ──────
BOTH = {"CLAIRE_DESK_NAME": "Jaydn Trading", "CLAIRE_ASSISTANT_NAME": "Ronathan"}


def test_the_desk_and_its_assistant_are_named_separately():
    b = lambda s: deskname.brand(s, BOTH)                   # noqa: E731
    assert deskname.check(BOTH) == ("Jaydn Trading", "Ronathan")
    # the desk: page titles, the menu's label, "… desk bot", "Claire Trading"
    assert b("<title>Claire — Portfolio</title>") == \
        "<title>Jaydn Trading — Portfolio</title>"
    assert b('<nav aria-label="Claire">') == '<nav aria-label="Jaydn Trading">'
    assert b("Claire desk bot. Your Telegram user id is 5.") == \
        "Jaydn Trading desk bot. Your Telegram user id is 5."
    assert b("Claire Trading — review") == "Jaydn Trading — review"
    # the assistant: everything else
    assert b("Ask Claire") == "Ask Ronathan"
    assert b("cannot reach Claire — timeout") == "cannot reach Ronathan — timeout"
    assert b("Claire asks you before selling") == "Ronathan asks you before selling"
    assert b("You are Claire, the desk's voice.") == \
        "You are Ronathan, the desk's voice."
    assert b("Claire's sim") == "Ronathan's sim"


def test_the_desk_takes_the_assistants_name_unless_it_has_its_own():
    only = {"CLAIRE_ASSISTANT_NAME": "Ronathan"}
    assert deskname.check(only) == ("Ronathan", "Ronathan")
    assert deskname.brand("<title>Claire — Orders</title>", only) == \
        "<title>Ronathan — Orders</title>"
    desk_only = {"CLAIRE_DESK_NAME": "Desk Two"}
    assert deskname.check(desk_only) == ("Desk Two", "Claire")
    assert deskname.brand("<title>Claire — Orders</title> Ask Claire",
                          desk_only) == \
        "<title>Desk Two — Orders</title> Ask Claire"
    assert deskname.version(desk_only) == "Desk_Two~Claire"


def test_alerts_are_titled_with_the_desk_and_worded_by_the_assistant(
        monkeypatch, tmp_path):
    for k, v in BOTH.items():
        monkeypatch.setenv(k, v)
    env = tmp_path / "telegram.env"
    env.write_text("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=1\n")
    sent, notes = [], []

    class C:
        def post(self, url, data=None):
            sent.append(data)
            return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(alerts.subprocess, "run",
                        lambda cmd, **kw: notes.append(cmd))
    alerts.send("Claire's sim sold 9 CSCO", path=env, client=C())
    assert sent[0]["text"] == "Jaydn Trading: Ronathan's sim sold 9 CSCO"
    assert notes[0][3:] == ["Jaydn Trading", "Ronathan's sim sold 9 CSCO"]


def test_pages_use_both_names():
    with serve(BOTH) as base:
        page = httpx.get(base + "/portfolio", timeout=5)
        nav = httpx.get(base + "/nav.js", timeout=5)
    assert "<title>Jaydn Trading — Portfolio</title>" in page.text
    assert "'Ask Ronathan'" in nav.text
    assert "cannot reach Ronathan" in nav.text


def test_renaming_only_the_desk_still_changes_the_pages_version():
    with serve({"CLAIRE_DESK_NAME": "Desk Two"}) as base:
        page = httpx.get(base + "/portfolio", timeout=5)
    assert "Desk_Two~Claire" in page.headers["ETag"]
    assert "<title>Desk Two — Portfolio</title>" in page.text


def test_the_telegram_settings_file_is_looked_up_when_called(monkeypatch,
                                                              tmp_path):
    """Tests and the preview point alerts.TELEGRAM_ENV at a scratch file.
    That must reach callers that pass no path — the default used to be
    bound at import, so they read the real file."""
    f = tmp_path / "t.env"
    f.write_text("TELEGRAM_BOT_TOKEN=tok\nTELEGRAM_CHAT_ID=42\nTELEGRAM_POLL=1\n")
    monkeypatch.setattr(alerts, "TELEGRAM_ENV", f)
    assert alerts.telegram_settings() == {"token": "tok", "approver_id": "42",
                                          "poll": True}
    assert alerts._telegram_cfg() == ("tok", "42")
    monkeypatch.setattr(alerts, "TELEGRAM_ENV", tmp_path / "missing.env")
    assert alerts.telegram_settings()["token"] is None
