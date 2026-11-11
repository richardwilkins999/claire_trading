"""Logging for both services (REVIEW-2026-11-11 §3.5).

Until now the desk had none: `var/log/*.log` was whatever uvicorn and a few
print() calls happened to write, with no timestamps, and ~150 broad
`except` handlers carried on in silence. A dead web search looked like "no
results" and a failed sell-review launch like a fired alert.

Two pieces:

  configure(service)   once, at process start: every line gets a timestamp,
                       level and logger name; uncaught exceptions in any
                       thread are logged; chatty HTTP libraries are quietened
                       (they log full request URLs, which carry API keys).
  swallowed(log, ...)  for a handler that deliberately carries on: one line
                       per kind of failure per few minutes, with a count of
                       the ones held back — so a source that fails on every
                       poll is visible without filling the log.

Lines go to stderr; bin/claire points that at var/log/<service>.log, and the
maintainer already rotates those files.
"""
import logging
import os
import sys
import threading
import time

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
# INFO from these is one line per HTTP request, full URL included — and the
# desk's URLs carry secrets (Twelve Data's apikey=, the Telegram bot token)
QUIET = ("httpx", "httpcore", "urllib3", "asyncio")

_configured = None


def configure(service, *, level=None, stream=None):
    """Idempotent. Level from CLAIRE_LOG_LEVEL (default INFO)."""
    global _configured
    name = (level or os.environ.get("CLAIRE_LOG_LEVEL") or "INFO").upper()
    lvl = getattr(logging, name, None)
    if not isinstance(lvl, int):
        lvl = logging.INFO
    root = logging.getLogger()
    if _configured is None:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(logging.Formatter(FORMAT, DATEFMT))
        root.addHandler(handler)
        _configured = handler
        sys.excepthook = _uncaught
        threading.excepthook = _uncaught_in_thread
        logging.captureWarnings(True)
    root.setLevel(lvl)
    for noisy in QUIET:
        logging.getLogger(noisy).setLevel(max(lvl, logging.WARNING))
    logging.getLogger("claire").info("%s starting (pid %s, log level %s)",
                                     service, os.getpid(),
                                     logging.getLevelName(lvl))
    return root


def _uncaught(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        return sys.__excepthook__(exc_type, exc, tb)
    logging.getLogger("claire").critical("uncaught exception",
                                         exc_info=(exc_type, exc, tb))


def _uncaught_in_thread(args):
    if issubclass(args.exc_type, SystemExit):
        return
    logging.getLogger("claire").critical(
        "uncaught exception in thread %s — the thread has died",
        args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


# ── failures a caller carries on from ────────────────────────────────────
_lock = threading.Lock()
_seen = {}                      # (logger, what) -> [last logged at, held back]
EVERY = 300


def swallowed(log, what, exc=None, *, every=EVERY, trace=False,
              level=logging.WARNING, clock=time.monotonic) -> bool:
    """Record a failure the caller has chosen to survive.

    `what` names the KIND of failure and should not vary from call to call
    (put the variable part in the exception): it is the throttle key. The
    first one is logged at once; repeats within `every` seconds are counted
    and reported with the next line. trace=True adds the traceback — for a
    failure that should not happen, not for a network hiccup.
    Returns True when a line was written."""
    key = (log.name, what)
    now = clock()
    with _lock:
        last = _seen.get(key)
        if last is not None and now - last[0] < every:
            last[1] += 1
            return False
        held = last[1] if last else 0
        _seen[key] = [now, 0]
    detail = f": {type(exc).__name__}: {str(exc)[:300]}" if exc is not None \
        else ""
    more = f" ({held} more like this since the last line)" if held else ""
    log.log(level, "%s%s%s", what, detail, more,
            exc_info=exc if (trace and exc is not None) else None)
    return True


def reset():
    """Forget throttle state (tests)."""
    with _lock:
        _seen.clear()
