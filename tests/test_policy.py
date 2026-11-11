from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from feeder.core.policy import DailyCapPolicy

LA = ZoneInfo("America/Los_Angeles")


class StubCounter:
    def __init__(self) -> None:
        self.rows: list[tuple[datetime, int]] = []

    def add(self, ts: datetime, treats: int = 1) -> None:
        self.rows.append((ts, treats))

    def treats_between(self, start_utc: datetime, end_utc: datetime) -> int:
        return sum(t for ts, t in self.rows if start_utc <= ts < end_utc)


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


@pytest.fixture
def counter() -> StubCounter:
    return StubCounter()


def make(counter, now, cap=8):
    return DailyCapPolicy(counter, LA, cap, now)


def test_eight_allowed_ninth_refused(counter, now):
    p = make(counter, now)
    for i in range(8):
        assert p.allows(), i
        counter.add(now())
    assert p.treats_today() == 8
    assert not p.allows()
    assert p.remaining() == 0


def test_remaining_and_allows_n(counter, now):
    p = make(counter, now)
    counter.add(now(), 6)
    assert p.remaining() == 2
    assert p.allows(2)
    assert not p.allows(3)


def test_multi_treat_event_counts_as_two(counter, now):
    p = make(counter, now)
    counter.add(now(), 2)
    assert p.treats_today() == 2


def test_custom_cap(counter, now):
    p = make(counter, now, cap=2)
    counter.add(now())
    assert p.allows()
    counter.add(now())
    assert not p.allows()
    assert p.cap == 2


@pytest.mark.parametrize("cap", [0, -1])
def test_invalid_cap(counter, now, cap):
    with pytest.raises(ValueError):
        make(counter, now, cap=cap)


def test_naive_at_rejected(counter, now):
    p = make(counter, now)
    with pytest.raises(ValueError):
        p.day_bounds(datetime(2026, 11, 17, 12, 0))
    with pytest.raises(ValueError):
        p.local_date(datetime(2026, 11, 17, 12, 0))


def test_at_defaults_to_now(counter, now):
    p = make(counter, now)
    assert p.day_bounds() == p.day_bounds(now())
    assert p.local_date() == p.local_date(now())


def test_normal_day_bounds(counter, now):
    p = make(counter, now)
    s, e = p.day_bounds(utc(2026, 11, 17, 12))
    assert s == utc(2026, 11, 17, 8)  # PST = UTC-8
    assert e == utc(2026, 11, 18, 8)


def test_boundary_at_local_midnight(counter, now):
    p = make(counter, now)
    counter.add(utc(2026, 11, 17, 7, 59, 59), 5)  # 23:59:59 local Nov 16
    counter.add(utc(2026, 11, 17, 8, 0, 0), 1)  # 00:00:00 local Nov 17
    now.set(utc(2026, 11, 17, 20))
    assert p.treats_today() == 1
    now.set(utc(2026, 11, 17, 7, 59, 59))
    assert p.local_date().day == 16
    assert p.treats_today() == 5
    now.set(utc(2026, 11, 17, 8, 0, 0))
    assert p.local_date().day == 17


def test_before_utc_midnight_same_local_day(counter, now):
    p = make(counter, now)
    # 2026-11-17 22:00 PST == 2026-11-18 06:00 UTC: UTC date differs, local day same
    counter.add(utc(2026, 11, 17, 9), 3)  # 01:00 local Nov 17
    counter.add(utc(2026, 11, 18, 6), 4)  # 22:00 local Nov 17
    now.set(utc(2026, 11, 18, 6, 30))
    assert p.local_date().day == 17
    assert p.treats_today() == 7


def test_dst_spring_forward_is_23h(counter, now):
    p = make(counter, now)
    s, e = p.day_bounds(utc(2026, 3, 8, 20))  # Mar 8 2026 local
    assert s == utc(2026, 3, 8, 8)  # PST
    assert e == utc(2026, 3, 9, 7)  # PDT
    assert (e - s).total_seconds() == 23 * 3600
    counter.add(utc(2026, 3, 9, 6, 59, 59))
    counter.add(utc(2026, 3, 9, 7, 0, 0))
    now.set(utc(2026, 3, 8, 20))
    assert p.treats_today() == 1


def test_dst_fall_back_is_25h(counter, now):
    p = make(counter, now)
    s, e = p.day_bounds(utc(2026, 11, 1, 20))  # Nov 1 2026 local
    assert s == utc(2026, 11, 1, 7)  # PDT
    assert e == utc(2026, 11, 2, 8)  # PST
    assert (e - s).total_seconds() == 25 * 3600
    counter.add(utc(2026, 11, 2, 7, 59, 59))
    counter.add(utc(2026, 11, 2, 8, 0, 0))
    now.set(utc(2026, 11, 1, 20))
    assert p.treats_today() == 1
