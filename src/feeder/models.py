"""Shared value types used across all components.

Every datetime in this module is timezone-aware UTC unless stated otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Source(StrEnum):
    SCHEDULE = "schedule"
    TELEGRAM = "telegram"
    BUTTON = "button"
    VOICE = "voice"
    SYSTEM = "system"

    @property
    def icon(self) -> str:
        return {
            Source.SCHEDULE: "⏰",
            Source.TELEGRAM: "📱",
            Source.BUTTON: "🔘",
            Source.VOICE: "🗣️",
            Source.SYSTEM: "⚙️",
        }[self]


class EventType(StrEnum):
    DISPENSE = "dispense"
    DISPENSE_FAILED = "dispense_failed"
    REFILL = "refill"
    LOW_SUPPLY = "low_supply"
    JAM = "jam"
    BOOT = "boot"
    SHUTDOWN = "shutdown"
    ONLINE = "online"
    OFFLINE = "offline"
    CAP_REACHED = "cap_reached"
    SCHEDULE_SKIPPED = "schedule_skipped"
    UNAUTHORIZED = "unauthorized"


@dataclass(frozen=True)
class Event:
    id: int
    ts: datetime  # aware UTC
    type: EventType
    source: Source | None
    treats: int
    detail: str | None


@dataclass(frozen=True)
class Schedule:
    id: int
    time_local: str  # "HH:MM" in the configured time zone
    count: int
    jitter_min: int
    enabled: bool
    created_by: int | None


class DispenseStatus(StrEnum):
    OK = "ok"  # treat(s) dropped and confirmed
    CAP_REACHED = "cap_reached"  # refused: daily cap
    EMPTY_OR_JAMMED = "empty_or_jammed"  # refused or just failed: no drop detected
    BUSY = "busy"  # reserved (dispenses are serialised, so normally never returned)


@dataclass(frozen=True)
class DispenseResult:
    status: DispenseStatus
    source: Source
    treats: int  # drops counted for this request (0 unless OK)
    treats_today: int  # count today AFTER this request
    cap: int

    @property
    def ok(self) -> bool:
        return self.status is DispenseStatus.OK


@dataclass(frozen=True)
class SupplyStatus:
    pct: float | None  # 0..100, None when uncalibrated / no reading yet
    treats_left: int | None
    days_left: float | None  # None when no consumption data
    low: bool
    last_reading_ts: datetime | None
