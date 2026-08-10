"""Market sessions (DESIGN.md §14a) — pure functions over session data.
No LLM, no network, no clock reads: callers pass aware datetimes.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import json


@dataclass(frozen=True)
class Session:
    exchange: str
    tz: str
    open_time: str          # local wall time, "09:30"
    close_time: str
    lunch_break: str | None = None   # "12:00-13:00"
    holidays: frozenset = frozenset()  # ISO dates


DEFAULTS = {s.exchange: s for s in [
    Session("NASDAQ", "America/New_York", "09:30", "16:00"),
    Session("NYSE",   "America/New_York", "09:30", "16:00"),
    Session("LSE",    "Europe/London",    "08:00", "16:30"),
    Session("XETRA",  "Europe/Berlin",    "09:00", "17:30"),
    Session("PARIS",  "Europe/Paris",     "09:00", "17:30"),
    Session("SGX",    "Asia/Singapore",   "09:00", "17:00"),
    Session("HKEX",   "Asia/Hong_Kong",   "09:30", "16:00", "12:00-13:00"),
    Session("TSE",    "Asia/Tokyo",       "09:00", "15:30", "11:30-12:30"),
    Session("ASX",    "Australia/Sydney", "10:00", "16:00"),
    Session("NSE",    "Asia/Kolkata",     "09:15", "15:30"),
]}


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _session(exchange, sessions) -> Session:
    table = sessions or DEFAULTS
    if exchange not in table:
        raise KeyError(f"no session calendar for exchange {exchange!r}")
    return table[exchange]


def _is_trading_day(s: Session, d) -> bool:
    return d.weekday() < 5 and d.isoformat() not in s.holidays


def is_open(exchange: str, ts: datetime, sessions=None) -> bool:
    s = _session(exchange, sessions)
    local = ts.astimezone(ZoneInfo(s.tz))
    if not _is_trading_day(s, local.date()):
        return False
    t = local.time()
    if not (_t(s.open_time) <= t < _t(s.close_time)):
        return False
    if s.lunch_break:
        lo, hi = s.lunch_break.split("-")
        if _t(lo) <= t < _t(hi):
            return False
    return True


def next_open(exchange: str, ts: datetime, sessions=None) -> datetime:
    """Earliest instant >= ts at which the exchange is open (exchange-local tz)."""
    s = _session(exchange, sessions)
    local = ts.astimezone(ZoneInfo(s.tz))
    if is_open(exchange, ts, sessions):
        return local
    for day in range(15):
        d = (local + timedelta(days=day)).date()
        if not _is_trading_day(s, d):
            continue
        candidates = [_t(s.open_time)]
        if s.lunch_break:
            candidates.append(_t(s.lunch_break.split("-")[1]))
        for c in candidates:
            candidate = datetime.combine(d, c, tzinfo=ZoneInfo(s.tz))
            if candidate >= local:
                return candidate
    raise ValueError(f"no session for {exchange} within 15 days — holiday data broken?")


def close_of(exchange: str, ts: datetime, sessions=None) -> datetime:
    """End of the session in effect: today's close while open (or pre-open on a
    trading day), else the close of the next session. Drives order TTLs and
    session-aware approval expiry."""
    s = _session(exchange, sessions)
    local = ts.astimezone(ZoneInfo(s.tz))
    for day in range(15):
        d = (local + timedelta(days=day)).date()
        if not _is_trading_day(s, d):
            continue
        close = datetime.combine(d, _t(s.close_time), tzinfo=ZoneInfo(s.tz))
        if close > local:
            return close
    raise ValueError(f"no session for {exchange} within 15 days — holiday data broken?")


# ── DB bridge: the exchange_sessions table is the deployed source of truth ──
def seed_db(conn):
    for s in DEFAULTS.values():
        conn.execute(
            "INSERT OR IGNORE INTO exchange_sessions"
            " (exchange, tz, open_time, close_time, lunch_break, holidays)"
            " VALUES (?,?,?,?,?,?)",
            (s.exchange, s.tz, s.open_time, s.close_time, s.lunch_break,
             json.dumps(sorted(s.holidays))))


def load(conn) -> dict:
    return {
        r["exchange"]: Session(r["exchange"], r["tz"], r["open_time"],
                               r["close_time"], r["lunch_break"],
                               frozenset(json.loads(r["holidays"])))
        for r in conn.execute("SELECT * FROM exchange_sessions")
    }
