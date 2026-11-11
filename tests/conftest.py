from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


class FakeNow:
    """Callable fake wall clock (aware UTC). Use `.advance()` / `.set()` in tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self.value = start or datetime(2026, 11, 17, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)

    def set(self, value: datetime) -> None:
        self.value = value


@pytest.fixture
def now() -> FakeNow:
    return FakeNow()
