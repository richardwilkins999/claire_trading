"""Session calendar tests (DESIGN.md §18 layer 5). 2026-08-11 is a Tuesday."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import sessions
from app.sessions import Session, close_of, is_open, next_open

SGT = ZoneInfo("Asia/Singapore")
NY = ZoneInfo("America/New_York")
HK = ZoneInfo("Asia/Hong_Kong")


def at(tz, y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=tz)


def test_sgx_hours():
    assert is_open("SGX", at(SGT, 2026, 8, 11, 10))
    assert not is_open("SGX", at(SGT, 2026, 8, 11, 8))       # pre-open
    assert not is_open("SGX", at(SGT, 2026, 8, 11, 17, 30))  # post-close
    assert not is_open("SGX", at(SGT, 2026, 8, 8, 10))       # Saturday


def test_cross_timezone_input():
    # 02:00 UTC on a Tuesday is 10:00 in Singapore — open regardless of the
    # timezone the caller happens to hold
    assert is_open("SGX", datetime(2026, 8, 11, 2, 0, tzinfo=ZoneInfo("UTC")))


def test_hkex_lunch_break():
    assert is_open("HKEX", at(HK, 2026, 8, 11, 11))
    assert not is_open("HKEX", at(HK, 2026, 8, 11, 12, 30))
    assert is_open("HKEX", at(HK, 2026, 8, 11, 13, 30))


def test_sgx_and_nyse_never_overlap():
    for hour in range(24):
        ts = datetime(2026, 8, 11, hour, 0, tzinfo=ZoneInfo("UTC"))
        assert not (is_open("SGX", ts) and is_open("NYSE", ts))


def test_next_open_over_weekend():
    fri_evening = at(SGT, 2026, 8, 7, 18)
    assert next_open("SGX", fri_evening) == at(SGT, 2026, 8, 10, 9)


def test_next_open_during_lunch_is_lunch_end():
    assert next_open("HKEX", at(HK, 2026, 8, 11, 12, 15)) == at(HK, 2026, 8, 11, 13)


def test_next_open_when_open_is_now():
    now = at(SGT, 2026, 8, 11, 10)
    assert next_open("SGX", now) == now


def test_close_of_session():
    assert close_of("SGX", at(SGT, 2026, 8, 11, 10)) == at(SGT, 2026, 8, 11, 17)
    # Friday post-close → Monday's close (weekend-safe approval expiry)
    assert close_of("SGX", at(SGT, 2026, 8, 7, 18)) == at(SGT, 2026, 8, 10, 17)


def test_holidays_respected():
    cal = {"SGX": Session("SGX", "Asia/Singapore", "09:00", "17:00",
                          holidays=frozenset({"2026-08-10"}))}  # Monday off
    assert not is_open("SGX", at(SGT, 2026, 8, 10, 10), cal)
    assert next_open("SGX", at(SGT, 2026, 8, 7, 18), cal) == at(SGT, 2026, 8, 11, 9)


def test_unknown_exchange_fails_loudly():
    with pytest.raises(KeyError):
        is_open("MOON", at(SGT, 2026, 8, 11, 10))


def test_db_roundtrip():
    from app.accounting import db
    conn = db.connect(":memory:")
    db.init(conn)
    sessions.seed_db(conn)
    loaded = sessions.load(conn)
    assert is_open("SGX", at(SGT, 2026, 8, 11, 10), loaded)
    assert loaded["HKEX"].lunch_break == "12:00-13:00"
