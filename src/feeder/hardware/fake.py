"""In-memory scriptable hardware fakes for tests and dev."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from feeder.hardware.base import (
    Buttons,
    DropSensor,
    Led,
    LedMode,
    LevelSensor,
    Motor,
    StatusLeds,
)


class FakeDropSensor(DropSensor):
    def __init__(self) -> None:
        self._count = 0
        self._script: deque[int] = deque()
        self._default = 1

    def script(self, *drops_per_advance: int) -> None:
        self._script = deque(drops_per_advance)

    def set_default_drops(self, n: int) -> None:
        self._default = n

    def on_advance(self) -> None:
        """Called by the linked motor on each advance."""
        self._count += self._script.popleft() if self._script else self._default

    def arm(self) -> None:
        self._count = 0

    async def wait_for_drop(self, timeout_s: float) -> int:
        return self._count


class FakeMotor(Motor):
    def __init__(self, drop_sensor: FakeDropSensor | None = None) -> None:
        self.advances = 0
        self.released = 0
        self.fail_next = False
        self.drop_sensor = drop_sensor

    async def advance_pocket(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("motor fault (fail_next)")
        self.advances += 1
        if self.drop_sensor is not None:
            self.drop_sensor.on_advance()

    def release(self) -> None:
        self.released += 1


class FakeLevelSensor(LevelSensor):
    def __init__(self) -> None:
        self.reads = 0
        self._value: int | None = None
        self._script: deque[int | None] = deque()

    def set_distance(self, mm: int | None) -> None:
        self._value = mm
        self._script.clear()

    def script(self, *values: int | None) -> None:
        self._script = deque(values)

    async def read_distance_mm(self) -> int | None:
        self.reads += 1
        if self._script:
            self._value = self._script.popleft()
        return self._value


class FakeStatusLeds(StatusLeds):
    def __init__(self) -> None:
        self.state: dict[Led, tuple[LedMode, float]] = {}
        self.history: list[tuple[Led, LedMode, float]] = []

    def set(self, led: Led, mode: LedMode, brightness: float = 1.0) -> None:
        if not 0.0 <= brightness <= 1.0:
            raise ValueError(f"brightness must be in 0..1, got {brightness}")
        self.state[led] = (mode, brightness)
        self.history.append((led, mode, brightness))


class FakeButtons(Buttons):
    def __init__(self) -> None:
        self._callback: Callable[[], None] | None = None

    def on_treat_press(self, callback: Callable[[], None]) -> None:
        self._callback = callback

    def press_treat(self) -> None:
        if self._callback is not None:
            self._callback()


@dataclass
class FakeHardware:
    motor: FakeMotor
    drop_sensor: FakeDropSensor
    level_sensor: FakeLevelSensor
    leds: FakeStatusLeds
    buttons: FakeButtons

    @classmethod
    def create(cls) -> FakeHardware:
        drop = FakeDropSensor()
        return cls(FakeMotor(drop), drop, FakeLevelSensor(), FakeStatusLeds(), FakeButtons())
