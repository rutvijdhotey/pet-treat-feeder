"""Status LED state machine (PRD 3.8)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum

from feeder.hardware.base import Led, LedMode, StatusLeds
from feeder.ports import NowFn

log = logging.getLogger(__name__)

CONNECTED_WINDOW_S = 60.0
OFFLINE_AFTER_S = 300.0


class PowerState(StrEnum):
    BOOTING = "booting"
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"
    OFF = "off"


class ConnState(StrEnum):
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    OFFLINE = "offline"


_POWER_MODES = {
    PowerState.RUNNING: LedMode.SOLID,
    PowerState.BOOTING: LedMode.BLINK,
    PowerState.SHUTTING_DOWN: LedMode.BLINK,
    PowerState.OFF: LedMode.OFF,
}
_CONN_MODES = {
    ConnState.CONNECTED: LedMode.SOLID,
    ConnState.RECONNECTING: LedMode.BLINK,
    ConnState.OFFLINE: LedMode.OFF,
}


def compute_led_states(
    power: PowerState,
    connected: ConnState,
    supply_pct: float | None,
    jammed: bool,
    low_threshold_pct: float,
) -> dict[Led, LedMode]:
    """Pure mapping from system state to LED modes. Power OFF forces all LEDs off."""
    if power is PowerState.OFF:
        return {led: LedMode.OFF for led in Led}
    if jammed:
        supply = LedMode.BLINK
    elif supply_pct is not None and supply_pct <= low_threshold_pct:
        supply = LedMode.SOLID
    else:
        supply = LedMode.OFF
    return {
        Led.POWER: _POWER_MODES[power],
        Led.CONNECTED: _CONN_MODES[connected],
        Led.SUPPLY: supply,
    }


def _check_brightness(value: float) -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"brightness must be in 0..1, got {value}")
    return value


class LedController:
    def __init__(
        self,
        leds: StatusLeds,
        brightness: float,
        now: NowFn,
        *,
        supply_pct: Callable[[], float | None],
        is_jammed: Callable[[], bool],
        low_threshold_pct: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._leds = leds
        self._brightness = _check_brightness(brightness)
        self._now = now
        self._supply_pct = supply_pct
        self._is_jammed = is_jammed
        self._low = low_threshold_pct
        self._sleep = sleep
        self._power = PowerState.BOOTING
        self._started: datetime = now()
        self._last_ok: datetime | None = None
        self._last_failed: datetime | None = None
        self._applied: dict[Led, LedMode] = {}

    def set_power(self, state: PowerState) -> None:
        self._power = state

    def set_brightness(self, value: float) -> None:
        self._brightness = _check_brightness(value)
        self._applied.clear()  # force re-apply on next refresh

    def mark_poll_ok(self) -> None:
        self._last_ok = self._now()

    def mark_poll_failed(self) -> None:
        self._last_failed = self._now()

    def conn_state(self) -> ConnState:
        ref = self._last_ok if self._last_ok is not None else self._started
        elapsed = (self._now() - ref).total_seconds()
        if self._last_ok is not None and elapsed < CONNECTED_WINDOW_S:
            return ConnState.CONNECTED
        if elapsed > OFFLINE_AFTER_S:
            return ConnState.OFFLINE
        return ConnState.RECONNECTING

    def refresh(self) -> None:
        states = compute_led_states(
            self._power,
            self.conn_state(),
            self._supply_pct(),
            self._is_jammed(),
            self._low,
        )
        for led, mode in states.items():
            if self._applied.get(led) != mode:
                self._leds.set(led, mode, self._brightness)
                self._applied[led] = mode

    async def run(self, interval_s: float = 1.0) -> None:
        while True:
            try:
                self.refresh()
            except Exception:
                log.exception("LED refresh failed")
            await self._sleep(interval_s)
