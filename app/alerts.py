"""Out-of-band alerts to Richard: Telegram via Bob's bot, plus a desktop
notification. Used for things that need a human BEFORE they bite — e.g. the
Saxo sign-in about to lapse (app/tools/saxo_live.py).

Reuses Bob's bot (~/bob/telegram.env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
rather than a second bot; the message goes to the first allowed chat, the
same one Bob's own notifications use. Never raises: an alert that cannot be
delivered is logged to stderr and the desk carries on.
"""
import json
import os
import subprocess
from pathlib import Path

import httpx
import logging
from .logsetup import swallowed

log = logging.getLogger(__name__)

TELEGRAM_ENV = Path(os.environ.get("CLAIRE_TELEGRAM_ENV",
                                   Path.home() / "bob" / "telegram.env"))


def _telegram_cfg(path=TELEGRAM_ENV):
    cfg = {}
    try:
        for line in path.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and not k.strip().startswith("#"):
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return None, None
    chat = next((c.strip() for c in cfg.get("TELEGRAM_CHAT_ID", "").split(",")
                 if c.strip()), None)
    return cfg.get("TELEGRAM_BOT_TOKEN") or None, chat


def telegram_settings(path=TELEGRAM_ENV):
    """{token, approver_id} for the interactive (button) messages. The
    approver is TELEGRAM_APPROVER_ID, else the first POSITIVE id in
    TELEGRAM_CHAT_ID — a positive chat id is a private chat, and a private
    chat's id equals the user's id. Group ids are negative and never qualify:
    a trade decision must come from one person, not from a chat."""
    cfg = {}
    try:
        for line in path.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and not k.strip().startswith("#"):
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return {"token": None, "approver_id": None}
    approver = (cfg.get("TELEGRAM_APPROVER_ID") or "").strip()
    if not approver:
        approver = next((c.strip() for c in
                         cfg.get("TELEGRAM_CHAT_ID", "").split(",")
                         if c.strip().isdigit()), "")
    if not approver.isdigit():
        approver = None
    return {"token": cfg.get("TELEGRAM_BOT_TOKEN") or None,
            "approver_id": approver}


class TelegramError(Exception):
    def __init__(self, status, description):
        self.status, self.description = status, description
        super().__init__(f"telegram HTTP {status}: {description}")


def tg_call(token, method, *, data=None, files=None, client=None,
            timeout=20):
    """One Bot API call; returns `result` or raises TelegramError. The token
    never appears in the exception text (it is part of the URL)."""
    c = client or httpx.Client(timeout=timeout)
    try:
        r = c.post(f"https://api.telegram.org/bot{token}/{method}",
                   data=data, files=files)
    except httpx.HTTPError as e:
        raise TelegramError(0, type(e).__name__) from None
    finally:
        if client is None:
            c.close()
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or not body.get("ok"):
        raise TelegramError(r.status_code,
                            str(body.get("description") or "")[:300])
    return body.get("result")


def _markup(keyboard):
    return {} if keyboard is None else {
        "reply_markup": json.dumps({"inline_keyboard": keyboard})}


def send_photo(token, chat_id, png: bytes, *, caption=None, client=None):
    data = {"chat_id": str(chat_id)}
    if caption:
        data.update(caption=caption[:1024], parse_mode="HTML")
    return tg_call(token, "sendPhoto", data=data,
                   files={"photo": ("chart.png", png, "image/png")},
                   client=client, timeout=40)


def send_message(token, chat_id, html, *, keyboard=None, reply_to=None,
                 client=None):
    """HTML parse mode — the CALLER escapes every interpolated value.
    `keyboard`: inline keyboard rows ([[{text, callback_data|url}]])."""
    data = {"chat_id": str(chat_id), "text": html, "parse_mode": "HTML",
            "disable_web_page_preview": "true", **_markup(keyboard)}
    if reply_to:
        data["reply_to_message_id"] = str(reply_to)
    return tg_call(token, "sendMessage", data=data, client=client)


def edit_message(token, chat_id, message_id, html, *, keyboard=None,
                 client=None):
    """Replace a message's text and buttons; keyboard=None removes them.
    'message is not modified' is success, not an error."""
    data = {"chat_id": str(chat_id), "message_id": str(message_id),
            "text": html, "parse_mode": "HTML",
            "disable_web_page_preview": "true"}
    data["reply_markup"] = json.dumps({"inline_keyboard": keyboard or []})
    try:
        return tg_call(token, "editMessageText", data=data, client=client)
    except TelegramError as e:
        if "not modified" in e.description:
            return None
        raise


def send(text, *, title="Claire", path=TELEGRAM_ENV, client=None):
    """Best effort on every channel; returns which ones accepted it."""
    sent = []
    token, chat = _telegram_cfg(path)
    if token and chat:
        c = client or httpx.Client(timeout=10)
        try:
            r = c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       data={"chat_id": chat, "text": f"{title}: {text}"})
            if r.status_code == 200:
                sent.append("telegram")
            else:
                log.warning("telegram refused an alert: HTTP %s",
                            r.status_code)
        except Exception as e:                  # noqa: BLE001 — best effort
            log.warning("telegram alert failed: %s: %s",
                        type(e).__name__, str(e)[:200])
        finally:
            if client is None:
                c.close()
    try:
        subprocess.run(["notify-send", "-u", "critical", title, text],
                       timeout=5, check=False, capture_output=True)
        sent.append("desktop")
    except Exception as e:                      # noqa: BLE001 — headless etc
        swallowed(log, "desktop notification (notify-send) failed",
                  e, every=3600)
        pass
    return sent
