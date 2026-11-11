"""Daily treat cap (PRD 3.4): counts dispensed treats in the local calendar day."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from feeder.ports import NowFn, TreatCounter


class DailyCapPolicy:
    def __init__(self, counter: TreatCounter, tz: ZoneInfo, cap: int, now: NowFn) -> None:
        if cap < 1:
            raise ValueError(f"cap must be >= 1, got {cap}")
        self._counter = counter
        self._tz = tz
        self.cap = cap
        self._now = now

    def _resolve(self, at: datetime | None) -> datetime:
        at = self._now() if at is None else at
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("`at` must be timezone-aware")
        return at

    def local_date(self, at: datetime | None = None) -> date:
        return self._resolve(at).astimezone(self._tz).date()

    def day_bounds(self, at: datetime | None = None) -> tuple[datetime, datetime]:
        """UTC [local midnight, next local midnight); DST-safe."""
        d = self.local_date(at)
        start = datetime.combine(d, time.min, tzinfo=self._tz)
        end = datetime.combine(d + timedelta(days=1), time.min, tzinfo=self._tz)
        return start.astimezone(UTC), end.astimezone(UTC)

    def treats_today(self) -> int:
        start, end = self.day_bounds()
        return self._counter.treats_between(start, end)

    def remaining(self) -> int:
        return max(self.cap - self.treats_today(), 0)

    def allows(self, n: int = 1) -> bool:
        return self.treats_today() + n <= self.cap
