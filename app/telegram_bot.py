"""Claire's own Telegram bot: reads button taps and LIVE replies itself.

With Bob's shared bot, Bob is the only getUpdates poller and relays taps to
POST /api/telegram-decision. With a DEDICATED bot (telegram.env has
TELEGRAM_POLL=1) nobody else reads the token's updates, so this thread in the
dashboards process long-polls getUpdates and hands each update straight to
telegram_approvals.handle_decision — the same function the HTTP route calls,
so every rule there (approver user id, same chat + message, two-step ✔,
LIVE within 2 minutes, stale buttons) applies unchanged.

* callback_query  → handle_decision(data=…) → answerCallbackQuery
* "LIVE" typed    → handle_decision(live=…) → reply in the chat
* /start or /id   → replies with the sender's user id (setup aid; reveals
                    nothing about the desk); /start for the owner = /help
* /analyze /buy /sell /positions /orders (owner only, private chat) and
  their rb: buttons → app/telegram_commands.py — every one of them ends at
  the approval gate, never past it
* anything else from anyone else → ignored

The update offset is kept in var/telegram_offset so a restart neither
replays old taps nor skips new ones. Replayed taps would be harmless anyway
(nonces are single-use and decided notices answer "already decided").
"""
import json
import threading
import logging
import time
from pathlib import Path

from . import alerts, telegram_approvals, telegram_commands

log = logging.getLogger(__name__)

POLL_TIMEOUT_S = 25


def _from_approver(user, approver):
    return bool(approver) and str((user or {}).get("id") or "") == str(approver)


def _send_all(call, token, chat_id, replies):
    for html, kb in replies:
        data = {"chat_id": chat_id, "text": html, "parse_mode": "HTML",
                "disable_web_page_preview": "true"}
        if kb:
            data["reply_markup"] = json.dumps({"inline_keyboard": kb})
        call(token, "sendMessage", data=data)


def handle_update(conn, market, upd, settings, resume_post, *, env,
                  call=alerts.tg_call, clock=time.time, commands=None):
    """Process one Telegram update. Returns a short label (for logs/tests)."""
    token, approver = settings["token"], settings["approver_id"]
    cq = upd.get("callback_query")
    if cq and commands is not None and str(cq.get("data") or "").startswith(
            telegram_commands.CB_PREFIX + ":"):
        # a command button (/positions, /orders, exchange pick): approver,
        # private chat — the same bar as typing the command
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private" or not _from_approver(
                cq.get("from"), approver):
            answer, replies = "Only the desk owner can use these.", []
            label = "command-button:403"
        else:
            answer, replies = commands.callback(cq["data"])
            label = "command-button"
        try:
            call(token, "answerCallbackQuery",
                 data={"callback_query_id": cq["id"], "text": answer[:190]})
        except alerts.TelegramError as e:
            log.warning("telegram bot: answerCallbackQuery: %s", e)
        _send_all(call, token, str(chat.get("id") or ""), replies)
        return label
    if cq:
        msg = cq.get("message") or {}
        body = {"from_id": str((cq.get("from") or {}).get("id") or ""),
                "chat_id": str((msg.get("chat") or {}).get("id") or ""),
                "message_id": msg.get("message_id"),
                "data": cq.get("data")}
        code, out = telegram_approvals.handle_decision(
            conn, market, body, resume_post, env=env, clock=clock,
            settings=settings)
        try:
            call(token, "answerCallbackQuery",
                 data={"callback_query_id": cq["id"],
                       "text": out.get("answer", "")[:190],
                       "show_alert": "true" if out.get("alert") else "false"})
        except alerts.TelegramError as e:      # tap already timed out etc.
            log.warning("telegram bot: answerCallbackQuery: %s", e)
        return f"callback:{code}"
    m = upd.get("message")
    if not m:
        return "ignored"
    chat = (m.get("chat") or {})
    chat_id = str(chat.get("id") or "")
    text = (m.get("text") or "").strip()
    user = m.get("from") or {}
    is_owner = chat.get("type") == "private" and _from_approver(user, approver)
    if text.split("@")[0] == "/id" or (text.split("@")[0] == "/start"
                                       and not is_owner):
        call(token, "sendMessage", data={
            "chat_id": chat_id,
            "text": f"Claire desk bot. Your Telegram user id is "
                    f"{user.get('id')}."
                    + ("" if _from_approver(user, approver) else
                       " Put it in TELEGRAM_CHAT_ID to receive trade cards.")})
        return "id"
    if chat.get("type") != "private" or not _from_approver(user, approver):
        return "ignored"
    if text.upper() == "LIVE":
        body = {"from_id": str(user["id"]), "chat_id": chat_id, "live": text}
        code, out = telegram_approvals.handle_decision(
            conn, market, body, resume_post, env=env, clock=clock,
            settings=settings)
        call(token, "sendMessage", data={
            "chat_id": chat_id, "text": out.get("answer", ""),
            "reply_to_message_id": str(m.get("message_id") or "")})
        return f"live:{code}"
    if text.startswith("/") and commands is not None:
        _send_all(call, token, chat_id, commands.command(text))
        return "command"
    return "ignored"


def _load_offset(path):
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _save_offset(path, offset):
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(str(offset))
    tmp.replace(p)


def report_finished_runs(commands, settings, call):
    """Tell the owner how each /analyze run that produced no card ended."""
    if commands is None or not settings.get("approver_id"):
        return
    for wi, html in commands.finished_runs():
        try:
            _send_all(call, settings["token"], settings["approver_id"],
                      [(html, None)])
        except alerts.TelegramError as e:
            log.warning("telegram bot: run outcome %s: %s", wi, e)
            continue
        commands.mark_reported(wi)


BEAT_EVERY_S = 600


def _beat(conn, state, ok, detail, clock=time.time):
    """A service_health row for the maintainer (`telegram_bot`): at most one
    every BEAT_EVERY_S, and at once when the bot goes from working to
    failing or back — a poller that died or is refused shows up there."""
    now = int(clock())
    if state.get("ok") == ok and now - state.get("at", 0) < BEAT_EVERY_S:
        return
    state.update(ok=ok, at=now)
    try:
        conn.execute("INSERT INTO service_health (service, checked_at, ok,"
                     " detail) VALUES ('telegram_bot', ?, ?, ?)",
                     (now, "ok" if ok else "degraded",
                      json.dumps({"state": detail})))
    except Exception:                          # noqa: BLE001 — a nicety
        pass


def run_poller(conn, market, env, resume_post, offset_path, *,
               call=alerts.tg_call, settings_fn=alerts.telegram_settings,
               sleep=time.sleep, once=False, commands=None):
    """Thread body. Idle (re-checking every 30 s) until telegram.env has a
    token and TELEGRAM_POLL=1. No approver id yet is fine: /id still answers
    (that is how the owner finds theirs) and every decision is refused."""
    offset = _load_offset(offset_path)
    state = {"note": None}
    beat = {}

    def note(msg):
        if state["note"] != msg:
            log.info("telegram bot: %s", msg)
            state["note"] = msg

    while True:
        try:
            st = settings_fn()
            if not (st.get("poll") and st.get("token")):
                note("not polling (needs TELEGRAM_BOT_TOKEN and"
                     f" TELEGRAM_POLL=1 in {alerts.TELEGRAM_ENV})")
                if once:
                    return
                sleep(30)
                continue
            data = {"timeout": str(POLL_TIMEOUT_S),
                    "allowed_updates": '["callback_query","message"]'}
            if offset is not None:
                data["offset"] = str(offset)
            try:
                updates = call(st["token"], "getUpdates", data=data,
                               timeout=POLL_TIMEOUT_S + 10) or []
            except alerts.TelegramError as e:
                # 409: a webhook is set or another program polls this token
                note(f"getUpdates failed ({e}) — retrying in 30 s")
                _beat(conn, beat, False, f"getUpdates failed: {e}"[:300])
                if once:
                    return
                sleep(30)
                continue
            note("polling for button taps")
            _beat(conn, beat, True, "polling for button taps")
            for upd in updates:
                offset = int(upd["update_id"]) + 1
                _save_offset(offset_path, offset)  # before: never re-run one
                try:
                    handle_update(conn, market, upd, st, resume_post,
                                  env=env, call=call, commands=commands)
                except Exception:              # noqa: BLE001 — next update
                    log.exception("telegram bot: update failed")
            report_finished_runs(commands, st, call)
        except Exception:                      # noqa: BLE001 — keep looping
            log.exception("telegram bot: poll failed")
            if not once:
                sleep(10)
        if once:
            return


def start_poller_thread(conn, market, env, resume_post, offset_path, *,
                        commands=None):
    t = threading.Thread(target=run_poller,
                         args=(conn, market, env, resume_post, offset_path),
                         kwargs={"commands": commands},
                         daemon=True, name="telegram-bot")
    t.start()
    return t
