"""Small Protocols that let core components depend on each other without importing them."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

# Injectable "current time" source. Must return an aware datetime (UTC preferred).
NowFn = Callable[[], datetime]


class Alerts(Protocol):
    """What core components use to talk to the owner. Implemented by notify.Notifier."""

    async def push(self, text: str) -> None:
        """Audible notification (low supply, jam, back online, schedule skipped)."""

    async def silent(self, text: str) -> None:
        """Silent notification (scheduled/button dispense, refill, daily summary)."""


class TreatCounter(Protocol):
    """Counts dispensed treats in a half-open UTC window [start_utc, end_utc)."""

    def treats_between(self, start_utc: datetime, end_utc: datetime) -> int: ...
