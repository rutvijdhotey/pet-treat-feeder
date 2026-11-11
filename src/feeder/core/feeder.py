"""Treat dispensing (PRD 3.4): cap check, drop verification, jam detection."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum

from feeder.core.policy import DailyCapPolicy
from feeder.hardware.base import DropSensor, Motor
from feeder.models import DispenseResult, DispenseStatus, EventType, Source
from feeder.ports import Alerts, NowFn
from feeder.storage.db import Database

log = logging.getLogger(__name__)


class FeederState(StrEnum):
    READY = "ready"
    EMPTY_OR_JAMMED = "empty_or_jammed"


class Feeder:
    def __init__(
        self,
        motor: Motor,
        drop_sensor: DropSensor,
        policy: DailyCapPolicy,
        db: Database,
        alerts: Alerts,
        now: NowFn,
        *,
        drop_timeout_s: float = 1.5,
        before_dispense: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._motor = motor
        self._drop_sensor = drop_sensor
        self._policy = policy
        self._db = db
        self._alerts = alerts
        self._now = now
        self._drop_timeout_s = drop_timeout_s
        self._before_dispense = before_dispense
        self._lock = asyncio.Lock()
        self.state = FeederState.READY
        self.on_state_change: Callable[[FeederState], None] | None = None

    def _set_state(self, state: FeederState) -> None:
        if state is self.state:
            return
        self.state = state
        if self.on_state_change is not None:
            try:
                self.on_state_change(state)
            except Exception:
                log.exception("on_state_change hook failed")

    def _result(self, status: DispenseStatus, source: Source, treats: int) -> DispenseResult:
        return DispenseResult(
            status=status,
            source=source,
            treats=treats,
            treats_today=self._policy.treats_today(),
            cap=self._policy.cap,
        )

    async def _attempt(self) -> int:
        """Advance one pocket and count drops. A motor fault counts as 0 drops."""
        try:
            self._drop_sensor.arm()
            await self._motor.advance_pocket()
            return await self._drop_sensor.wait_for_drop(self._drop_timeout_s)
        except Exception:
            log.exception("dispense attempt failed")
            return 0

    async def dispense(self, source: Source) -> DispenseResult:
        async with self._lock:
            if not self._policy.allows(1):
                self._db.add_event(EventType.CAP_REACHED, source, ts=self._now())
                return self._result(DispenseStatus.CAP_REACHED, source, 0)

            was_jammed = self.state is FeederState.EMPTY_OR_JAMMED
            if was_jammed and source is Source.SCHEDULE:
                return self._result(DispenseStatus.EMPTY_OR_JAMMED, source, 0)

            if self._before_dispense is not None:
                try:
                    await self._before_dispense()
                except Exception:
                    log.exception("before_dispense failed")

            drops = 0
            try:
                for _ in range(2):
                    drops = await self._attempt()
                    if drops >= 1:
                        break
            finally:
                self._motor.release()

            if drops >= 1:
                self._db.add_event(EventType.DISPENSE, source, treats=drops, ts=self._now())
                self._set_state(FeederState.READY)
                result = self._result(DispenseStatus.OK, source, drops)
                if source in (Source.SCHEDULE, Source.BUTTON):
                    kind = "Scheduled" if source is Source.SCHEDULE else "Button"
                    try:
                        await self._alerts.silent(
                            f"{source.icon} {kind}"
                            f" treat dispensed ({result.treats_today}/{result.cap} today)"
                        )
                    except Exception:
                        log.exception("silent alert failed")
                return result

            self._db.add_event(
                EventType.DISPENSE_FAILED, source, detail="no drop after 2 pockets", ts=self._now()
            )
            if not was_jammed:
                self._db.add_event(EventType.JAM, source, ts=self._now())
                self._set_state(FeederState.EMPTY_OR_JAMMED)
                try:
                    await self._alerts.push(
                        "🚨 Feeder empty or jammed: no treat dropped after 2 pockets. "
                        "Refill/clear it, then try again."
                    )
                except Exception:
                    log.exception("push alert failed")
            return self._result(DispenseStatus.EMPTY_OR_JAMMED, source, 0)

    def clear_jam(self) -> None:
        self._set_state(FeederState.READY)
