from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from feeder.config import SupplyConfig
from feeder.core.supply import SupplyMonitor
from feeder.hardware.fake import FakeLevelSensor
from feeder.models import EventType, Source
from feeder.storage.db import Database

# empty=200mm, full=100mm -> pct = 200 - d
CFG = dict(empty_mm=200, full_mm=100)


class RecAlerts:
    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.silenced: list[str] = []

    async def push(self, text: str) -> None:
        self.pushed.append(text)

    async def silent(self, text: str) -> None:
        self.silenced.append(text)


def make(now, cfg=None, **kw):
    sensor = FakeLevelSensor()
    db = Database.open(":memory:")
    alerts = RecAlerts()
    delays: list[float] = []

    async def sleep(d: float) -> None:
        delays.append(d)

    cfg = cfg or SupplyConfig(**CFG)
    mon = SupplyMonitor(sensor, db, alerts, cfg, 80, now, sleep=sleep, **kw)
    return mon, sensor, db, alerts, delays


def set_pct(sensor, pct):
    sensor.set_distance(round(200 - pct))


def test_distance_to_pct_math_and_clamp(now):
    mon, *_ = make(now)
    assert mon.distance_to_pct(200) == 0
    assert mon.distance_to_pct(100) == 100
    assert mon.distance_to_pct(150) == 50
    assert mon.distance_to_pct(250) == 0
    assert mon.distance_to_pct(50) == 100


def test_distance_to_pct_uncalibrated(now):
    mon, *_ = make(now, SupplyConfig())
    assert mon.distance_to_pct(150) is None


def test_initial_status(now):
    mon, *_ = make(now)
    s = mon.status()
    assert s.pct is None and not s.low and s.last_reading_ts is None


async def test_median_rejects_outlier(now):
    mon, sensor, db, *_ = make(now)
    sensor.script(150, 150, 20, 150, 151)
    s = await mon.read()
    assert s.pct == 50
    assert s.treats_left == 40
    assert len(db.level_readings_since(now() - timedelta(days=1))) == 1


async def test_none_samples_ignored(now):
    mon, sensor, *_ = make(now)
    sensor.script(None, 150, None, 150, 150)
    assert (await mon.read()).pct == 50


async def test_all_none_keeps_previous(now):
    mon, sensor, db, alerts, _ = make(now)
    set_pct(sensor, 60)
    first = await mon.read()
    sensor.set_distance(None)
    assert await mon.read() == first
    assert alerts.pushed == []
    assert len(db.level_readings_since(now() - timedelta(days=1))) == 1


async def test_uncalibrated_no_alerts(now):
    mon, sensor, db, alerts, _ = make(now, SupplyConfig())
    sensor.set_distance(190)
    s = await mon.read()
    assert s.pct is None and s.treats_left is None and not s.low
    assert alerts.pushed == []
    assert db.last_event(EventType.LOW_SUPPLY) is None


async def test_low_alert_once(now):
    mon, sensor, db, alerts, _ = make(now)
    set_pct(sensor, 50)
    await mon.read()
    set_pct(sensor, 19)
    s = await mon.read()
    assert s.low
    assert len(alerts.pushed) == 1
    ev = db.last_event(EventType.LOW_SUPPLY)
    assert ev is not None and ev.source is Source.SYSTEM and ev.detail
    set_pct(sensor, 15)
    assert (await mon.read()).low
    assert len(alerts.pushed) == 1


async def test_hysteresis(now):
    mon, sensor, _, alerts, _ = make(now)
    for p in (19, 25, 19):
        set_pct(sensor, p)
        await mon.read()
    assert len(alerts.pushed) == 1
    for p in (31, 19):
        set_pct(sensor, p)
        await mon.read()
    assert len(alerts.pushed) == 2


async def test_low_flag_only_below_threshold(now):
    mon, sensor, *_ = make(now)
    set_pct(sensor, 19)
    assert (await mon.read()).low
    set_pct(sensor, 25)
    assert not (await mon.read()).low


async def test_refill_confirmed(now):
    mon, sensor, db, alerts, delays = make(now)
    called = []
    mon.add_refill_listener(lambda: called.append(1))
    mon.add_refill_listener(lambda: 1 / 0)  # must not break anything
    set_pct(sensor, 10)
    await mon.read()
    assert len(alerts.pushed) == 1
    set_pct(sensor, 90)
    s = await mon.read()
    assert delays == [30.0]
    assert s.pct == 90 and not s.low
    ev = db.last_event(EventType.REFILL)
    assert ev is not None and ev.source is Source.SYSTEM
    assert len(alerts.silenced) == 1 and "Refill detected" in alerts.silenced[0]
    assert called == [1]
    # low re-armed
    set_pct(sensor, 18)
    await mon.read()
    assert len(alerts.pushed) == 2


async def test_refill_unconfirmed_spike(now):
    mon, sensor, db, alerts, delays = make(now)
    set_pct(sensor, 40)
    await mon.read()
    # first measurement spikes to 90, confirming measurement is back to 40
    sensor.script(*([110] * 5 + [160] * 5))
    s = await mon.read()
    assert delays == [30.0]
    assert s.pct == 40
    assert db.last_event(EventType.REFILL) is None
    assert alerts.silenced == []


async def test_slow_rise_is_not_refill(now):
    mon, sensor, db, _, delays = make(now)
    for p in (30, 40, 50, 60, 70):
        set_pct(sensor, p)
        await mon.read()
    assert delays == []
    assert db.last_event(EventType.REFILL) is None


async def test_treats_and_days_left(now):
    mon, sensor, db, *_ = make(now)
    set_pct(sensor, 50)
    assert (await mon.read()).days_left is None
    for d in (1, 2, 3):
        db.add_event(EventType.DISPENSE, Source.SCHEDULE, treats=7, ts=now() - timedelta(days=d))
    db.add_event(EventType.DISPENSE, Source.SCHEDULE, treats=100, ts=now() - timedelta(days=9))
    s = await mon.read()
    assert s.treats_left == 40
    assert s.days_left == pytest.approx(40 / (21 / 7))


async def test_old_readings_pruned(now):
    mon, sensor, db, *_ = make(now)
    db.add_level_reading(now() - timedelta(days=40), 150, 50.0)
    set_pct(sensor, 50)
    await mon.read()
    assert len(db.level_readings_since(now() - timedelta(days=60))) == 1


async def test_run_loop_and_cancellation(now):
    sensor = FakeLevelSensor()
    db = Database.open(":memory:")
    alerts = RecAlerts()
    delays: list[float] = []
    mon: SupplyMonitor

    async def sleep(d: float) -> None:
        delays.append(d)
        if len(delays) >= 3:
            raise asyncio.CancelledError

    mon = SupplyMonitor(
        sensor, db, alerts, SupplyConfig(**CFG, poll_interval_s=60), 80, now, sleep=sleep
    )
    set_pct(sensor, 50)
    with pytest.raises(asyncio.CancelledError):
        await mon.run()
    assert delays == [60, 60, 60]
    assert mon.status().pct == 50


async def test_run_survives_exceptions(now):
    mon, sensor, _, _, delays = make(now)
    calls = []

    async def boom():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("x")
        raise asyncio.CancelledError

    mon.read = boom  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await mon.run()
    assert len(calls) == 3 and len(delays) == 2
