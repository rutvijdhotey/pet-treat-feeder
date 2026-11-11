"""Hardware abstraction layer interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import StrEnum


class Led(StrEnum):
    POWER = "power"
    CONNECTED = "connected"
    SUPPLY = "supply"


class LedMode(StrEnum):
    OFF = "off"
    SOLID = "solid"
    BLINK = "blink"


class Motor(ABC):
    @abstractmethod
    async def advance_pocket(self) -> None:
        """Rotate exactly one pocket pitch, then de-energise."""

    @abstractmethod
    def release(self) -> None:
        """De-energise coils."""


class DropSensor(ABC):
    @abstractmethod
    def arm(self) -> None:
        """Reset the drop counter before a move."""

    @abstractmethod
    async def wait_for_drop(self, timeout_s: float) -> int:
        """Return drops counted since arm(); 0 on timeout. Returns once >=1 seen."""


class LevelSensor(ABC):
    @abstractmethod
    async def read_distance_mm(self) -> int | None:
        """One sample in mm; None means the read failed."""


class StatusLeds(ABC):
    @abstractmethod
    def set(self, led: Led, mode: LedMode, brightness: float = 1.0) -> None:
        """Set an LED mode; brightness is 0..1."""


class Buttons(ABC):
    @abstractmethod
    def on_treat_press(self, callback: Callable[[], None]) -> None:
        """Register the treat-button callback (may fire from any thread)."""
