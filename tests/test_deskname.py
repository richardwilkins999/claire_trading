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

RON = {"CLAIRE_DESK_NAME": "Ronathan"}


def test_default_is_claire_and_nothing_is_touched():
    assert deskname.name({}) == "Claire"
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
    with pytest.raises(ValueError, match="CLAIRE_DESK_NAME"):
        deskname.check({"CLAIRE_DESK_NAME": bad})
    # ...and a running desk falls back to the default, it never dies on it
    assert deskname.brand("Claire", {"CLAIRE_DESK_NAME": bad}) == "Claire"


def test_names_with_spaces_and_hyphens_are_fine():
    assert deskname.name({"CLAIRE_DESK_NAME": " Desk Two "}) == "Desk Two"
    assert deskname.brand("Claire", {"CLAIRE_DESK_NAME": "Anne-Marie"}) == \
        "Anne-Marie"


def test_the_chat_prompt_and_telegram_say_the_name(monkeypatch):
    monkeypatch.setenv("CLAIRE_DESK_NAME", "Ronathan")
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
