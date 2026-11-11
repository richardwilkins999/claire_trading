"""Market sessions (DESIGN.md §14a) — pure functions over session data.
No LLM, no network, no clock reads: callers pass aware datetimes.
"""
from dataclasses import dataclass, replace
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
    # half days: ((ISO date, "HH:MM" early close), ...). Not stored in the
    # exchange_sessions table (no column); load() takes them from DEFAULTS.
    early_closes: tuple = ()


# Holiday lists are HAND-MAINTAINED, one block per year (DESIGN.md §14a), and
# deliberately conservative — only well-established dates; a missed holiday
# delays a queued order by a day, nothing more. The 2026 dates are inline in
# _SESSIONS below; later years are added in their own tables underneath and
# merged in. calendar_check() (run daily by the maintainer) warns before the
# data runs out — until 2026-11-11 nothing did, and every exchange would
# have read as open on every 2027 holiday (REVIEW-2026-11-11 B14).
_US26 = ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
         "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"]
# NYSE/Nasdaq: day after Thanksgiving and Christmas Eve close 13:00 ET
_US26_HALF = (("2026-11-27", "13:00"), ("2026-12-24", "13:00"))

_SESSIONS = [
    Session("NASDAQ", "America/New_York", "09:30", "16:00",
            holidays=frozenset(_US26), early_closes=_US26_HALF),
    Session("NYSE",   "America/New_York", "09:30", "16:00",
            holidays=frozenset(_US26), early_closes=_US26_HALF),
    Session("LSE",    "Europe/London",    "08:00", "16:30",
            holidays=frozenset(["2026-01-01", "2026-04-03", "2026-04-06",
                                "2026-05-04", "2026-05-25", "2026-08-31",
                                "2026-12-25", "2026-12-28"]),
            early_closes=(("2026-12-24", "12:30"), ("2026-12-31", "12:30"))),
    Session("XETRA",  "Europe/Berlin",    "09:00", "17:30",
            holidays=frozenset(["2026-01-01", "2026-04-03", "2026-04-06",
                                "2026-05-01", "2026-12-24", "2026-12-25",
                                "2026-12-31"])),
    Session("PARIS",  "Europe/Paris",     "09:00", "17:30",
            holidays=frozenset(["2026-01-01", "2026-04-03", "2026-04-06",
                                "2026-05-01", "2026-12-25"]),
            early_closes=(("2026-12-24", "14:05"), ("2026-12-31", "14:05"))),
    # SGX: Hari Raya Puasa (Sat 21 Mar) needs no weekday closure
    Session("SGX",    "Asia/Singapore",   "09:00", "17:00",
            holidays=frozenset(["2026-01-01", "2026-02-17", "2026-02-18",
                                "2026-04-03", "2026-05-01",
                                "2026-05-27",               # Hari Raya Haji
                                "2026-06-01",               # Vesak (Sun 31 May)
                                "2026-08-10",               # National Day obs.
                                "2026-11-09",               # Deepavali (Sun 8 Nov)
                                "2026-12-25"]),
            early_closes=(("2026-12-24", "12:00"), ("2026-12-31", "12:00"))),
    Session("HKEX",   "Asia/Hong_Kong",   "09:30", "16:00", "12:00-13:00",
            holidays=frozenset(["2026-01-01", "2026-02-17", "2026-02-18",
                                "2026-02-19", "2026-04-03", "2026-04-06",
                                "2026-04-07",               # day after Easter Mon
                                "2026-05-01",               # Labour Day
                                "2026-05-25",               # Buddha's B'day obs.
                                "2026-06-19",               # Tuen Ng
                                "2026-07-01", "2026-10-01",
                                "2026-10-19",               # Chung Yeung obs.
                                "2026-12-25"]),
            # Christmas Eve / New Year's Eve: morning session only
            early_closes=(("2026-12-24", "12:00"), ("2026-12-31", "12:00"))),
    Session("TSE",    "Asia/Tokyo",       "09:00", "15:30", "11:30-12:30",
            holidays=frozenset(["2026-01-01", "2026-01-02",
                                "2026-01-12",               # Coming of Age Day
                                "2026-02-11", "2026-02-23",
                                "2026-03-20",               # Vernal Equinox
                                "2026-04-29", "2026-05-04", "2026-05-05",
                                "2026-05-06",
                                "2026-07-20",               # Marine Day
                                "2026-08-11",
                                "2026-09-21", "2026-09-22",  # Aged / Kokumin
                                "2026-09-23",               # Autumnal Equinox
                                "2026-10-12",               # Sports Day
                                "2026-11-03", "2026-11-23", "2026-12-31"])),
    Session("ASX",    "Australia/Sydney", "10:00", "16:00",
            holidays=frozenset(["2026-01-01", "2026-01-26", "2026-04-03",
                                "2026-04-06", "2026-06-08", "2026-12-25",
                                "2026-12-28"]),
            early_closes=(("2026-12-24", "14:10"), ("2026-12-31", "14:10"))),
    # NSE 2026 trading-holiday circular (weekday closures only)
    Session("NSE",    "Asia/Kolkata",     "09:15", "15:30",
            holidays=frozenset(["2026-01-15",               # Maharashtra polls
                                "2026-01-26",               # Republic Day
                                "2026-03-03",               # Holi
                                "2026-03-26",               # Ram Navami
                                "2026-03-31",               # Mahavir Jayanti
                                "2026-04-03",               # Good Friday
                                "2026-04-14",               # Ambedkar Jayanti
                                "2026-05-01",               # Maharashtra Day
                                "2026-05-28",               # Bakri Id
                                "2026-06-26",               # Muharram
                                "2026-09-14",               # Ganesh Chaturthi
                                "2026-10-02",               # Gandhi Jayanti
                                "2026-10-20",               # Dussehra
                                "2026-11-10",               # Diwali Balipratipada
                                "2026-11-24",               # Guru Nanak Jayanti
                                "2026-12-25"])),            # Christmas
]

# ── 2027 ─────────────────────────────────────────────────────────────────
# Weekday closures only. Sources (read 2026-11-11): NYSE hours-calendars;
# LSE business days; Deutsche Börse trading calendar 2027; Euronext trading
# hours & holidays; Singapore MOM public holidays 2027; Hong Kong general
# holidays 2027 + HKEX holiday schedule; JPX market holidays; ASX trading
# calendar. Every weekday was computed, not remembered.
_US27 = ["2027-01-01", "2027-01-18", "2027-02-15",
         "2027-03-26",                  # Good Friday
         "2027-05-31",
         "2027-06-18",                  # Juneteenth obs. (19 Jun is Sat)
         "2027-07-05",                  # Independence Day obs. (4 Jul is Sun)
         "2027-09-06", "2027-11-25",
         "2027-12-24"]                  # Christmas obs. (25 Dec is Sat): FULL day
_H27 = {
    "NASDAQ": _US27, "NYSE": _US27,
    "LSE": ["2027-01-01", "2027-03-26", "2027-03-29", "2027-05-03",
            "2027-05-31", "2027-08-30",
            "2027-12-27", "2027-12-28"],        # Christmas / Boxing Day obs.
    # 1 May, 25 and 26 Dec fall on a weekend: no weekday closure
    "XETRA": ["2027-01-01", "2027-03-26", "2027-03-29", "2027-12-24",
              "2027-12-31"],
    "PARIS": ["2027-01-01", "2027-03-26", "2027-03-29"],
    # CNY is Sat 6 / Sun 7 Feb: only Mon 8 Feb is a weekday closure
    "SGX": ["2027-01-01", "2027-02-08",
            "2027-03-10",                       # Hari Raya Puasa
            "2027-03-26",
            "2027-05-17",                       # Hari Raya Haji
            "2027-05-20",                       # Vesak Day
            "2027-08-09",                       # National Day
            "2027-10-28"],                      # Deepavali
    "HKEX": ["2027-01-01", "2027-02-08",
             "2027-02-09",                      # 4th day of LNY (substitute)
             "2027-03-26", "2027-03-29",
             "2027-04-05",                      # Ching Ming
             "2027-05-13",                      # Buddha's Birthday
             "2027-06-09",                      # Tuen Ng
             "2027-07-01",
             "2027-09-16",                      # day after Mid-Autumn
             "2027-10-01",
             "2027-10-08",                      # Chung Yeung
             "2027-12-27"],                     # first weekday after Christmas
    "TSE": ["2027-01-01", "2027-01-11", "2027-02-11", "2027-02-23",
            "2027-03-22",                       # Vernal Equinox obs.
            "2027-04-29", "2027-05-03", "2027-05-04", "2027-05-05",
            "2027-07-19", "2027-08-11", "2027-09-20", "2027-09-23",
            "2027-10-11", "2027-11-03", "2027-11-23", "2027-12-31"],
    # ANZAC Day (Sun 25 Apr): ASX lists Mon 26 Apr as OPEN
    "ASX": ["2027-01-01", "2027-01-26", "2027-03-26", "2027-03-29",
            "2027-06-14", "2027-12-27", "2027-12-28"],
    # NSE publishes its circular in December: only the fixed-date holidays
    # are here (Republic Day, Good Friday, Ambedkar Jayanti). See PARTIAL.
    "NSE": ["2027-01-26", "2027-03-26", "2027-04-14"],
}
_HALF27 = {
    # the day after Thanksgiving only: Christmas Eve 2027 is a full closure
    "NASDAQ": (("2027-11-26", "13:00"),), "NYSE": (("2027-11-26", "13:00"),),
    "LSE": (("2027-12-24", "12:30"), ("2027-12-31", "12:30")),
    "PARIS": (("2027-12-24", "14:05"), ("2027-12-31", "14:05")),
    # Lunar New Year's eve (Fri 5 Feb) is a morning-only session on both
    "SGX": (("2027-02-05", "12:00"), ("2027-12-24", "12:00"),
            ("2027-12-31", "12:00")),
    "HKEX": (("2027-02-05", "12:00"), ("2027-12-24", "12:00"),
             ("2027-12-31", "12:00")),
    "ASX": (("2027-12-24", "14:10"), ("2027-12-31", "14:10")),
}
# (exchange, year) whose list is known to be incomplete, and what to do
PARTIAL = {("NSE", 2027): "only the fixed-date holidays are listed — add the "
                          "rest (Holi, Id, Diwali…) from NSE's 2027 holiday "
                          "circular, published in December"}

DEFAULTS = {s.exchange: replace(
    s, holidays=s.holidays | frozenset(_H27.get(s.exchange, ())),
    early_closes=s.early_closes + _HALF27.get(s.exchange, ()))
    for s in _SESSIONS}


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


def _close_on(s: Session, d) -> time:
    """That day's close: the early close on a half day, else close_time."""
    iso = d.isoformat()
    for day, hhmm in s.early_closes:
        if day == iso:
            return _t(hhmm)
    return _t(s.close_time)


def is_open(exchange: str, ts: datetime, sessions=None) -> bool:
    s = _session(exchange, sessions)
    local = ts.astimezone(ZoneInfo(s.tz))
    if not _is_trading_day(s, local.date()):
        return False
    t = local.time()
    if not (_t(s.open_time) <= t < _close_on(s, local.date())):
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
            if c >= _close_on(s, d):           # half day: no afternoon session
                continue
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
        close = datetime.combine(d, _close_on(s, d), tzinfo=ZoneInfo(s.tz))
        if close > local:
            return close
    raise ValueError(f"no session for {exchange} within 15 days — holiday data broken?")


# ── how far the holiday data reaches ────────────────────────────────────
WARN_DAYS = 45


def coverage(sessions=None) -> dict:
    """exchange -> the last year it has any holiday listed for."""
    return {ex: max((int(d[:4]) for d in s.holidays), default=None)
            for ex, s in (sessions or DEFAULTS).items()}


def calendar_check(today, sessions=None) -> tuple:
    """(status, value, detail) for the maintainer. bad: an exchange has no
    holidays for THIS year, so it reads as open on every one of them. warn:
    next year is under WARN_DAYS away and not in yet, or a year in use is
    known to be incomplete."""
    cov = coverage(sessions)
    behind = sorted(ex for ex, y in cov.items() if y is None or y < today.year)
    if behind:
        return ("bad", f"no {today.year} holidays",
                f"{', '.join(behind)}: every {today.year} holiday reads as "
                f"an open day. Add the {today.year} dates to app/sessions.py "
                "and restart.")
    days_left = (today.replace(month=12, day=31) - today).days
    soon = sorted(ex for ex, y in cov.items() if y <= today.year)
    if soon and days_left <= WARN_DAYS:
        return ("warn", f"ends 31 Dec {today.year}",
                f"{', '.join(soon)}: no {today.year + 1} holidays yet "
                f"({days_left} days left). Add them to app/sessions.py.")
    notes = [f"{ex} {y}: {why}" for (ex, y), why in sorted(PARTIAL.items())
             if ex in cov and (y == today.year or (
                 y == today.year + 1 and (today.month, today.day) >= (12, 15)))]
    if notes:
        return ("warn", "incomplete", "; ".join(notes))
    return ("ok", f"through {min(cov.values())}", "")


# ── DB bridge: the exchange_sessions table is the deployed source of truth ──
def seed_db(conn):
    for s in DEFAULTS.values():
        conn.execute(
            "INSERT OR IGNORE INTO exchange_sessions"
            " (exchange, tz, open_time, close_time, lunch_break, holidays)"
            " VALUES (?,?,?,?,?,?)",
            (s.exchange, s.tz, s.open_time, s.close_time, s.lunch_break,
             json.dumps(sorted(s.holidays))))
        # holidays added to DEFAULTS reach an already-seeded DB: union the
        # default dates into the stored list. Operator-added dates are kept;
        # (trade-off) a default date an operator deleted comes back.
        row = conn.execute("SELECT holidays FROM exchange_sessions"
                           " WHERE exchange=?", (s.exchange,)).fetchone()
        stored = set(json.loads(row["holidays"] or "[]")) if row else set()
        merged = sorted(stored | set(s.holidays))
        if merged != sorted(stored):
            conn.execute("UPDATE exchange_sessions SET holidays=?"
                         " WHERE exchange=?", (json.dumps(merged), s.exchange))


def load(conn) -> dict:
    # half days have no DB column: they always come from DEFAULTS
    return {
        r["exchange"]: Session(r["exchange"], r["tz"], r["open_time"],
                               r["close_time"], r["lunch_break"],
                               frozenset(json.loads(r["holidays"])),
                               DEFAULTS[r["exchange"]].early_closes
                               if r["exchange"] in DEFAULTS else ())
        for r in conn.execute("SELECT * FROM exchange_sessions")
    }
