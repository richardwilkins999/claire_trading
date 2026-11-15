"""The desk's name — a setting, so a second desk can be called something
else (the code, its settings and its commands keep the word "claire").

  CLAIRE_DESK_NAME   what the desk calls itself to you   (default Claire)

The name is applied where words LEAVE the desk, not at each of the hundreds
of places that say "Claire": the pages and their data (the dashboards'
response writer), Telegram messages and desktop notifications
(app/alerts.py), and the prompts given to the models (registry.render_prompt
— so the chat introduces itself by the right name). With the default name
nothing is touched at all.

Only the whole word is replaced ("Claire", "Claire's"), never part of a
longer word or an identifier (claire_chat, x-claire-secret, /claire). The
name is limited to letters, digits, spaces and hyphens so it is safe to
drop into a page, a script string or a Telegram message as it stands.
"""
import os
import re

DEFAULT = "Claire"
_WORD = re.compile(r"(?<![A-Za-z0-9_])Claire(?![A-Za-z0-9_])")
_WORD_B = re.compile(rb"(?<![A-Za-z0-9_])Claire(?![A-Za-z0-9_])")
_VALID = re.compile(r"[A-Za-z][A-Za-z0-9 \-]{0,23}")


class NameError_(ValueError):
    pass


def name(env=None) -> str:
    """The desk's name. A value that is not a plain short name raises —
    checked once at start (check()); callers on a hot path use brand()."""
    raw = ((os.environ if env is None else env).get("CLAIRE_DESK_NAME")
           or "").strip()
    if not raw:
        return DEFAULT
    if not _VALID.fullmatch(raw):
        raise NameError_(
            f"CLAIRE_DESK_NAME={raw!r}: use 1–24 letters, digits, spaces or "
            "hyphens, starting with a letter")
    return raw


check = name            # at start: raises on a bad value, returns the name


def _safe(env=None) -> str:
    try:
        return name(env)
    except NameError_:
        return DEFAULT          # a running desk never dies on its own name


def brand(text, env=None):
    """`text` with the desk's name in place of "Claire" (whole word)."""
    n = _safe(env)
    if n == DEFAULT or not isinstance(text, str) or "Claire" not in text:
        return text
    return _WORD.sub(n, text)


def brand_bytes(data: bytes, env=None) -> bytes:
    n = _safe(env)
    if n == DEFAULT or b"Claire" not in data:
        return data
    return _WORD_B.sub(n.encode(), data)
