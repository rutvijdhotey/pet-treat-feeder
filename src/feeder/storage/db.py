"""SQLite storage: events, schedules, level readings and settings."""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path

from feeder.models import Event, EventType, Schedule, Source

MIGRATIONS: list[str] = [
    """
    CREATE TABLE events (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc  TEXT NOT NULL,
        type    TEXT NOT NULL,
        source  TEXT,
        treats  INTEGER NOT NULL DEFAULT 0,
        detail  TEXT
    );
    CREATE INDEX idx_events_ts ON events(ts_utc);
    CREATE INDEX idx_events_type_ts ON events(type, ts_utc);
    CREATE TABLE schedules (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        time_local  TEXT NOT NULL,
        count       INTEGER NOT NULL DEFAULT 1,
        jitter_min  INTEGER NOT NULL DEFAULT 0,
        enabled     INTEGER NOT NULL DEFAULT 1,
        created_by  INTEGER
    );
    CREATE TABLE level_readings (
        ts_utc       TEXT NOT NULL,
        distance_mm  INTEGER NOT NULL,
        pct          REAL
    );
    CREATE INDEX idx_level_ts ON level_readings(ts_utc);
    CREATE TABLE settings (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );
    """,
]

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _to_str(dt: datetime) -> str:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def _from_str(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(UTC)


def _event(row: sqlite3.Row | tuple) -> Event:
    id_, ts, type_, source, treats, detail = row
    return Event(
        id=id_,
        ts=_from_str(ts),
        type=EventType(type_),
        source=Source(source) if source is not None else None,
        treats=treats,
        detail=detail,
    )


def _schedule(row: tuple) -> Schedule:
    id_, time_local, count, jitter, enabled, created_by = row
    return Schedule(id_, time_local, count, jitter, bool(enabled), created_by)


_EVENT_COLS = "id, ts_utc, type, source, treats, detail"
_SCHED_COLS = "id, time_local, count, jitter_min, enabled, created_by"


class Database:
    """Thread-safe SQLite wrapper. Implements the TreatCounter protocol."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: str | Path = ":memory:") -> Database:
        p = str(path)
        is_memory = p == ":memory:"
        if not is_memory:
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            if not is_memory:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            db = cls(conn)
            db._migrate()
        except BaseException:
            conn.close()
            raise
        return db

    def _migrate(self) -> None:
        with self._lock:
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if current > len(MIGRATIONS):
                raise RuntimeError(
                    f"database schema version {current} is newer than supported "
                    f"version {len(MIGRATIONS)}"
                )
            for version in range(current, len(MIGRATIONS)):
                try:
                    self._conn.execute("BEGIN")
                    for stmt in MIGRATIONS[version].split(";"):
                        if stmt.strip():
                            self._conn.execute(stmt)
                    self._conn.execute(f"PRAGMA user_version={version + 1}")
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    # events -------------------------------------------------------------

    def add_event(
        self,
        type: EventType,
        source: Source | None = None,
        treats: int = 0,
        detail: str | None = None,
        ts: datetime | None = None,
    ) -> Event:
        when = _to_str(ts if ts is not None else datetime.now(UTC))
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (ts_utc, type, source, treats, detail) VALUES (?,?,?,?,?)",
                (when, EventType(type).value, source.value if source else None, treats, detail),
            )
            return Event(
                id=cur.lastrowid or 0,
                ts=_from_str(when),
                type=EventType(type),
                source=Source(source) if source else None,
                treats=treats,
                detail=detail,
            )

    def events_between(
        self,
        start_utc: datetime,
        end_utc: datetime,
        types: Collection[EventType] | None = None,
    ) -> list[Event]:
        sql = f"SELECT {_EVENT_COLS} FROM events WHERE ts_utc >= ? AND ts_utc < ?"
        params: list[str] = [_to_str(start_utc), _to_str(end_utc)]
        if types is not None:
            vals = [EventType(t).value for t in types]
            if not vals:
                return []
            sql += f" AND type IN ({','.join('?' * len(vals))})"
            params += vals
        sql += " ORDER BY ts_utc, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_event(r) for r in rows]

    def last_event(self, types: Collection[EventType] | EventType) -> Event | None:
        if isinstance(types, EventType):
            types = [types]
        vals = [EventType(t).value for t in types]
        if not vals:
            return None
        sql = (
            f"SELECT {_EVENT_COLS} FROM events WHERE type IN ({','.join('?' * len(vals))}) "
            "ORDER BY ts_utc DESC, id DESC LIMIT 1"
        )
        with self._lock:
            row = self._conn.execute(sql, vals).fetchone()
        return _event(row) if row else None

    def treats_between(self, start_utc: datetime, end_utc: datetime) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(treats), 0) FROM events "
                "WHERE type = ? AND ts_utc >= ? AND ts_utc < ?",
                (EventType.DISPENSE.value, _to_str(start_utc), _to_str(end_utc)),
            ).fetchone()
        return int(row[0])

    # schedules ----------------------------------------------------------

    def add_schedule(
        self,
        time_local: str,
        count: int = 1,
        jitter_min: int = 0,
        created_by: int | None = None,
    ) -> Schedule:
        if not isinstance(time_local, str) or not _TIME_RE.match(time_local):
            raise ValueError(f"time_local must be HH:MM (00:00-23:59), got {time_local!r}")
        if count < 1:
            raise ValueError("count must be >= 1")
        if jitter_min < 0:
            raise ValueError("jitter_min must be >= 0")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO schedules (time_local, count, jitter_min, enabled, created_by) "
                "VALUES (?,?,?,1,?)",
                (time_local, count, jitter_min, created_by),
            )
            return Schedule(cur.lastrowid or 0, time_local, count, jitter_min, True, created_by)

    def list_schedules(self, enabled_only: bool = False) -> list[Schedule]:
        sql = f"SELECT {_SCHED_COLS} FROM schedules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY time_local, id"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [_schedule(r) for r in rows]

    def remove_schedule(self, schedule_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            return cur.rowcount > 0

    def set_schedule_enabled(self, schedule_id: int, enabled: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE schedules SET enabled = ? WHERE id = ?", (int(enabled), schedule_id)
            )
            return cur.rowcount > 0

    # settings -----------------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # level readings -----------------------------------------------------

    def add_level_reading(self, ts: datetime, distance_mm: int, pct: float | None) -> None:
        when = _to_str(ts)
        with self._lock:
            self._conn.execute(
                "INSERT INTO level_readings (ts_utc, distance_mm, pct) VALUES (?,?,?)",
                (when, distance_mm, pct),
            )

    def level_readings_since(self, since_utc: datetime) -> list[tuple[datetime, int, float | None]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts_utc, distance_mm, pct FROM level_readings "
                "WHERE ts_utc >= ? ORDER BY ts_utc, rowid",
                (_to_str(since_utc),),
            ).fetchall()
        return [(_from_str(r[0]), r[1], r[2]) for r in rows]

    def prune_level_readings(self, older_than_utc: datetime) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM level_readings WHERE ts_utc < ?", (_to_str(older_than_utc),)
            )
            return cur.rowcount
