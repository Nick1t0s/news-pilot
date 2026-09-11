from __future__ import annotations

import datetime as dt

from app.publish.service import (
    _seconds_until_quiet_end,
    in_quiet_hours,
    parse_quiet_hours,
)


def test_parse_quiet_hours() -> None:
    window = parse_quiet_hours("01:00-07:00")
    assert window == (dt.time(1, 0), dt.time(7, 0))
    assert parse_quiet_hours(None) is None


def test_in_quiet_hours_same_day() -> None:
    window = (dt.time(1, 0), dt.time(7, 0))
    assert in_quiet_hours(dt.datetime(2026, 9, 1, 3, 0, tzinfo=dt.timezone.utc), window)
    assert in_quiet_hours(dt.datetime(2026, 9, 1, 1, 0, tzinfo=dt.timezone.utc), window)
    assert not in_quiet_hours(dt.datetime(2026, 9, 1, 7, 0, tzinfo=dt.timezone.utc), window)
    assert not in_quiet_hours(dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc), window)


def test_in_quiet_hours_overnight() -> None:
    window = (dt.time(23, 0), dt.time(7, 0))
    assert in_quiet_hours(dt.datetime(2026, 9, 1, 2, 0, tzinfo=dt.timezone.utc), window)
    assert in_quiet_hours(dt.datetime(2026, 9, 1, 23, 30, tzinfo=dt.timezone.utc), window)
    assert not in_quiet_hours(dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc), window)


def test_seconds_until_quiet_end() -> None:
    tz = dt.timezone.utc
    window = (dt.time(1, 0), dt.time(7, 0))
    now = dt.datetime(2026, 9, 1, 3, 0, tzinfo=tz)
    seconds = _seconds_until_quiet_end(now, window, tz)
    assert 3 * 3600 - 5 < seconds < 4 * 3600 + 5

    overnight = (dt.time(23, 0), dt.time(7, 0))
    late = dt.datetime(2026, 9, 1, 23, 0, tzinfo=tz)
    seconds = _seconds_until_quiet_end(late, overnight, tz)
    assert 7 * 3600 - 5 < seconds < 8 * 3600 + 5
