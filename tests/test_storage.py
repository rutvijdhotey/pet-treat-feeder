from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta, timezone

import pytest

from feeder.models import Event, EventType, Source
from feeder.ports import TreatCounter
from feeder.storage.db import MIGRATIONS, Database

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def db():
    d = Database.open(":memory:")
    yield d
    d.close()


# migrations ---------------------------------------------------------------


def test_fresh_db_is_at_latest_version(db):
    assert db.schema_version == len(MIGRATIONS)


def test_reopen_is_noop_and_keeps_data(tmp_path):
    p = tmp_path / "sub" / "dir" / "f.db"
    d = Database.open(p)
    d.add_event(EventType.BOOT, ts=T0)
    d.close()
    d2 = Database.open(p)
    assert d2.schema_version == len(MIGRATIONS)
    assert len(d2.events_between(T0, T0 + timedelta(seconds=1))) == 1
    d2.close()


def test_newer_version_rejected(tmp_path):
    p = tmp_path / "f.db"
    Database.open(p).close()
    conn = sqlite3.connect(p)
    conn.execute(f"PRAGMA user_version={len(MIGRATIONS) + 1}")
    conn.close()
    with pytest.raises(RuntimeError):
        Database.open(p)


def test_file_db_uses_wal_and_fk(tmp_path):
    d = Database.open(tmp_path / "w.db")
    assert d._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert d._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert d._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    d.close()


def test_indexes_exist(db):
    names = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_events_ts", "idx_events_type_ts"} <= names


# events -------------------------------------------------------------------


def test_add_event_roundtrip(db):
    e = db.add_event(EventType.DISPENSE, Source.BUTTON, treats=2, detail="x", ts=T0)
    assert isinstance(e, Event)
    assert e == Event(e.id, T0, EventType.DISPENSE, Source.BUTTON, 2, "x")
    assert db.last_event(EventType.DISPENSE) == e
    assert e.ts.tzinfo is not None and e.ts.utcoffset() == timedelta(0)


def test_add_event_defaults(db):
    before = datetime.now(UTC)
    e = db.add_event(EventType.BOOT)
    assert before <= e.ts <= datetime.now(UTC)
    assert e.source is None and e.treats == 0 and e.detail is None
    assert db.last_event([EventType.BOOT]) == e


def test_non_utc_aware_is_normalised(db):
    tz = timezone(timedelta(hours=2))
    e = db.add_event(EventType.BOOT, ts=datetime(2026, 11, 17, 14, 0, tzinfo=tz))
    assert e.ts == T0 and e.ts.utcoffset() == timedelta(0)


def test_naive_datetimes_rejected(db):
    naive = datetime(2026, 11, 17, 12, 0)
    with pytest.raises(ValueError):
        db.add_event(EventType.BOOT, ts=naive)
    with pytest.raises(ValueError):
        db.events_between(naive, T0)
    with pytest.raises(ValueError):
        db.events_between(T0, naive)
    with pytest.raises(ValueError):
        db.treats_between(naive, T0)
    with pytest.raises(ValueError):
        db.last_event(EventType.BOOT) or db.add_level_reading(naive, 1, None)
    with pytest.raises(ValueError):
        db.level_readings_since(naive)
    with pytest.raises(ValueError):
        db.prune_level_readings(naive)


def test_events_between_window_and_order(db):
    a = db.add_event(EventType.DISPENSE, treats=1, ts=T0)
    b = db.add_event(EventType.REFILL, ts=T0)  # same ts: ordered by id
    c = db.add_event(EventType.DISPENSE, treats=1, ts=T0 + timedelta(hours=1))
    db.add_event(EventType.BOOT, ts=T0 - timedelta(microseconds=1))
    assert db.events_between(T0, T0 + timedelta(hours=1)) == [a, b]  # end exclusive
    assert db.events_between(T0, T0 + timedelta(hours=1, microseconds=1)) == [a, b, c]
    assert db.events_between(T0, T0 + timedelta(hours=2), [EventType.DISPENSE]) == [a, c]
    assert db.events_between(T0, T0 + timedelta(hours=2), []) == []


def test_ordering_with_whole_second_and_fractional_ts(db):
    late = db.add_event(EventType.BOOT, ts=T0 + timedelta(milliseconds=500))
    early = db.add_event(EventType.BOOT, ts=T0)
    assert db.events_between(T0, T0 + timedelta(seconds=1)) == [early, late]


def test_last_event(db):
    assert db.last_event(EventType.REFILL) is None
    assert db.last_event([]) is None
    db.add_event(EventType.REFILL, ts=T0)
    r2 = db.add_event(EventType.REFILL, ts=T0 + timedelta(days=1))
    j = db.add_event(EventType.JAM, ts=T0 + timedelta(hours=1))
    assert db.last_event(EventType.REFILL) == r2
    assert db.last_event({EventType.REFILL, EventType.JAM}) == r2
    assert db.last_event([EventType.JAM]) == j


def test_treats_between(db):
    db.add_event(EventType.DISPENSE, treats=1, ts=T0)
    db.add_event(EventType.DISPENSE, treats=2, ts=T0 + timedelta(hours=1))
    db.add_event(EventType.DISPENSE_FAILED, treats=5, ts=T0 + timedelta(minutes=5))
    db.add_event(EventType.DISPENSE, treats=4, ts=T0 + timedelta(hours=2))
    assert db.treats_between(T0, T0 + timedelta(hours=2)) == 3  # start incl, end excl
    assert db.treats_between(T0 + timedelta(seconds=1), T0 + timedelta(hours=3)) == 6
    assert db.treats_between(T0 + timedelta(days=1), T0 + timedelta(days=2)) == 0


def test_satisfies_treat_counter(db):
    def use(counter: TreatCounter) -> int:
        return counter.treats_between(T0, T0 + timedelta(days=1))

    db.add_event(EventType.DISPENSE, treats=3, ts=T0)
    assert use(db) == 3


# schedules ----------------------------------------------------------------


def test_schedules_crud(db):
    s2 = db.add_schedule("18:30", count=2, jitter_min=5, created_by=42)
    s1 = db.add_schedule("07:00")
    assert (s1.count, s1.jitter_min, s1.enabled, s1.created_by) == (1, 0, True, None)
    assert s2.created_by == 42
    assert db.list_schedules() == [s1, s2]
    assert db.set_schedule_enabled(s1.id, False) is True
    assert [s.id for s in db.list_schedules(enabled_only=True)] == [s2.id]
    assert db.list_schedules()[0].enabled is False
    assert db.set_schedule_enabled(999, True) is False
    assert db.remove_schedule(s2.id) is True
    assert db.remove_schedule(s2.id) is False
    assert [s.id for s in db.list_schedules()] == [s1.id]


def test_schedule_order_by_time_then_id(db):
    a = db.add_schedule("09:00")
    b = db.add_schedule("08:00")
    c = db.add_schedule("09:00")
    assert [s.id for s in db.list_schedules()] == [b.id, a.id, c.id]


@pytest.mark.parametrize("bad", ["", "7:00", "24:00", "12:60", "12:5", "ab:cd", "12:00:00", "1200"])
def test_schedule_bad_time(db, bad):
    with pytest.raises(ValueError):
        db.add_schedule(bad)


@pytest.mark.parametrize("good", ["00:00", "23:59", "09:05"])
def test_schedule_good_time(db, good):
    assert db.add_schedule(good).time_local == good


def test_schedule_bad_numbers(db):
    with pytest.raises(ValueError):
        db.add_schedule("08:00", count=0)
    with pytest.raises(ValueError):
        db.add_schedule("08:00", jitter_min=-1)
    assert db.list_schedules() == []


# settings -----------------------------------------------------------------


def test_settings(db):
    assert db.get_setting("k") is None
    assert db.get_setting("k", "d") == "d"
    db.set_setting("k", "v1")
    db.set_setting("k", "v2")
    assert db.get_setting("k", "d") == "v2"


# level readings -----------------------------------------------------------


def test_level_readings(db):
    db.add_level_reading(T0, 100, 50.0)
    db.add_level_reading(T0 + timedelta(hours=1), 120, None)
    db.add_level_reading(T0 - timedelta(days=40), 90, 80.0)
    assert db.level_readings_since(T0) == [(T0, 100, 50.0), (T0 + timedelta(hours=1), 120, None)]
    assert len(db.level_readings_since(T0 - timedelta(days=60))) == 3
    assert db.prune_level_readings(T0) == 1  # strictly older than cutoff
    assert db.prune_level_readings(T0) == 0
    assert len(db.level_readings_since(T0 - timedelta(days=60))) == 2


# concurrency --------------------------------------------------------------


def test_concurrent_threads(tmp_path):
    d = Database.open(tmp_path / "c.db")
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(50):
                d.add_event(EventType.DISPENSE, Source.BUTTON, treats=1, ts=T0)
                d.treats_between(T0, T0 + timedelta(days=1))
                d.set_setting(f"k{n}", str(i))
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert d.treats_between(T0, T0 + timedelta(days=1)) == 200
    d.close()
