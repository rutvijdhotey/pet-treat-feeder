"""Supply (treat level) monitoring with low-supply alerts and refill detection (PRD 3.5)."""

from __future__ import annotations

import asyncio
import logging
import statistics
from collections.abc import Awaitable, Callable
from datetime import timedelta

from feeder.config import SupplyConfig
from feeder.hardware.base import LevelSensor
from feeder.models import EventType, Source, SupplyStatus
from feeder.ports import Alerts, NowFn
from feeder.storage.db import Database

log = logging.getLogger(__name__)

_RETENTION = timedelta(days=30)
_CONSUMPTION_WINDOW = timedelta(days=7)


class SupplyMonitor:
    def __init__(
        self,
        sensor: LevelSensor,
        db: Database,
        alerts: Alerts,
        cfg: SupplyConfig,
        capacity_treats: int,
        now: NowFn,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        samples: int = 5,
        refill_confirm_delay_s: float = 30.0,
    ) -> None:
        self._sensor = sensor
        self._db = db
        self._alerts = alerts
        self._cfg = cfg
        self._capacity = capacity_treats
        self._now = now
        self._sleep = sleep
        self._samples = samples
        self._confirm_delay_s = refill_confirm_delay_s
        self._status = SupplyStatus(
            pct=None, treats_left=None, days_left=None, low=False, last_reading_ts=None
        )
        self._armed = True
        self._listeners: list[Callable[[], None]] = []

    def add_refill_listener(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def status(self) -> SupplyStatus:
        return self._status

    def distance_to_pct(self, distance_mm: float) -> float | None:
        empty, full = self._cfg.empty_mm, self._cfg.full_mm
        if empty is None or full is None:
            return None
        frac = (empty - distance_mm) / (empty - full)
        return min(1.0, max(0.0, frac)) * 100.0

    async def _measure(self) -> tuple[float, float | None] | None:
        """Median of several samples; stores the reading. None if every sample failed."""
        values: list[int] = []
        for _ in range(self._samples):
            v = await self._sensor.read_distance_mm()
            if v is not None:
                values.append(v)
        if not values:
            return None
        mm = statistics.median(values)
        pct = self.distance_to_pct(mm)
        ts = self._now()
        self._db.add_level_reading(ts, round(mm), pct)
        try:
            self._db.prune_level_readings(ts - _RETENTION)
        except Exception:
            log.exception("pruning level readings failed")
        return mm, pct

    def _days_left(self, treats_left: int) -> float | None:
        now = self._now()
        used = self._db.treats_between(now - _CONSUMPTION_WINDOW, now)
        if used <= 0:
            return None
        return treats_left / (used / 7)

    async def read(self) -> SupplyStatus:
        m = await self._measure()
        if m is None:
            log.warning("all level sensor samples failed; keeping previous status")
            return self._status
        _, pct = m
        ts = self._now()
        if pct is None:  # uncalibrated
            self._status = SupplyStatus(
                pct=None, treats_left=None, days_left=None, low=False, last_reading_ts=ts
            )
            return self._status

        prev = self._status.pct
        if prev is not None and pct - prev >= self._cfg.refill_rise_pct:
            await self._sleep(self._confirm_delay_s)
            confirm = await self._measure()
            if (
                confirm is not None
                and confirm[1] is not None
                and confirm[1] - prev >= self._cfg.refill_rise_pct
            ):
                pct = confirm[1]
                ts = self._now()
                await self._on_refill(prev, pct)
            else:
                log.info("level jump %.0f%% -> %.0f%% not confirmed; ignoring", prev, pct)
                return self._status

        treats_left = round(pct / 100 * self._capacity)
        low = pct <= self._cfg.low_threshold_pct
        self._status = SupplyStatus(
            pct=pct,
            treats_left=treats_left,
            days_left=self._days_left(treats_left),
            low=low,
            last_reading_ts=ts,
        )
        await self._update_low_alert(pct)
        return self._status

    async def _on_refill(self, prev: float, pct: float) -> None:
        detail = f"{prev:.0f}% -> {pct:.0f}%"
        self._db.add_event(EventType.REFILL, Source.SYSTEM, detail=detail, ts=self._now())
        self._armed = True
        await self._alerts.silent(f"Refill detected ({detail}).")
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                log.exception("refill listener failed")

    async def _update_low_alert(self, pct: float) -> None:
        if pct >= self._cfg.low_clear_pct:
            self._armed = True
        if pct <= self._cfg.low_threshold_pct and self._armed:
            self._armed = False
            self._db.add_event(
                EventType.LOW_SUPPLY, Source.SYSTEM, detail=f"{pct:.0f}%", ts=self._now()
            )
            await self._alerts.push(
                f"Treat supply is low: about {pct:.0f}% "
                f"(~{self._status.treats_left} treats) left. Time to refill."
            )

    async def run(self) -> None:
        while True:
            try:
                await self.read()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("supply read cycle failed")
            await self._sleep(self._cfg.poll_interval_s)
