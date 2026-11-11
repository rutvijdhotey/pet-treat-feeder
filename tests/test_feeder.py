from __future__ import annotations

import asyncio
from zoneinfo import ZoneInfo

import pytest

from feeder.core.feeder import Feeder, FeederState
from feeder.core.policy import DailyCapPolicy
from feeder.hardware.fake import FakeHardware, FakeMotor
from feeder.models import DispenseStatus, EventType, Source
from feeder.storage.db import Database


class RecAlerts:
    def __init__(self) -> None:
        self.pushes: list[str] = []
        self.silents: list[str] = []
        self.raise_on_send = False

    async def push(self, text: str) -> None:
        if self.raise_on_send:
            raise RuntimeError("telegram down")
        self.pushes.append(text)

    async def silent(self, text: str) -> None:
        if self.raise_on_send:
            raise RuntimeError("telegram down")
        self.silents.append(text)


class Env:
    def __init__(self, now) -> None:
        self.db = Database.open(":memory:")
        self.hw = FakeHardware.create()
        self.alerts = RecAlerts()
        self.policy = DailyCapPolicy(self.db, ZoneInfo("America/Los_Angeles"), 8, now)
        self.now = now
        self.feeder = self.make()

    def make(self, **kw) -> Feeder:
        return Feeder(
            self.hw.motor, self.hw.drop_sensor, self.policy, self.db, self.alerts, self.now, **kw
        )

    def count(self, t: EventType) -> int:
        start, end = self.policy.day_bounds()
        return sum(1 for e in self.db.events_between(start, end) if e.type == t)


@pytest.fixture
def env(now) -> Env:
    return Env(now)


async def test_happy_path(env: Env):
    r = await env.feeder.dispense(Source.TELEGRAM)
    assert r.status is DispenseStatus.OK and r.treats == 1 and r.treats_today == 1 and r.cap == 8
    assert env.hw.motor.advances == 1 and env.hw.motor.released == 1
    assert env.count(EventType.DISPENSE) == 1
    assert env.feeder.state is FeederState.READY
    assert env.alerts.silents == []


async def test_cap_reached_on_ninth(env: Env):
    for _ in range(8):
        assert (await env.feeder.dispense(Source.TELEGRAM)).ok
    adv = env.hw.motor.advances
    r = await env.feeder.dispense(Source.TELEGRAM)
    assert r.status is DispenseStatus.CAP_REACHED and r.treats == 0 and r.treats_today == 8
    assert env.hw.motor.advances == adv
    assert env.count(EventType.CAP_REACHED) == 1


async def test_retry_on_second_pocket(env: Env):
    env.hw.drop_sensor.script(0, 1)
    r = await env.feeder.dispense(Source.TELEGRAM)
    assert r.ok and r.treats == 1
    assert env.hw.motor.advances == 2 and env.hw.motor.released == 1
    assert env.count(EventType.DISPENSE) == 1
    assert env.count(EventType.DISPENSE_FAILED) == 0
    assert env.feeder.state is FeederState.READY


async def test_jam_after_two_empty_pockets(env: Env):
    env.hw.drop_sensor.script(0, 0)
    r = await env.feeder.dispense(Source.TELEGRAM)
    assert r.status is DispenseStatus.EMPTY_OR_JAMMED and r.treats == 0 and r.treats_today == 0
    assert env.hw.motor.advances == 2 and env.hw.motor.released == 1
    assert env.count(EventType.DISPENSE_FAILED) == 1
    assert env.count(EventType.JAM) == 1
    assert env.feeder.state is FeederState.EMPTY_OR_JAMMED
    assert len(env.alerts.pushes) == 1 and "🚨" in env.alerts.pushes[0]
    assert env.policy.treats_today() == 0


async def test_scheduled_refused_while_jammed(env: Env):
    env.hw.drop_sensor.script(0, 0)
    await env.feeder.dispense(Source.TELEGRAM)
    adv = env.hw.motor.advances
    r = await env.feeder.dispense(Source.SCHEDULE)
    assert r.status is DispenseStatus.EMPTY_OR_JAMMED
    assert env.hw.motor.advances == adv
    assert len(env.alerts.pushes) == 1


async def test_manual_retry_clears_jam(env: Env):
    changes = []
    env.feeder.on_state_change = changes.append
    env.hw.drop_sensor.script(0, 0)
    await env.feeder.dispense(Source.TELEGRAM)
    r = await env.feeder.dispense(Source.BUTTON)
    assert r.ok
    assert env.feeder.state is FeederState.READY
    assert changes == [FeederState.EMPTY_OR_JAMMED, FeederState.READY]


async def test_failed_manual_retry_does_not_repush(env: Env):
    env.hw.drop_sensor.script(0, 0, 0, 0)
    await env.feeder.dispense(Source.TELEGRAM)
    r = await env.feeder.dispense(Source.TELEGRAM)
    assert r.status is DispenseStatus.EMPTY_OR_JAMMED
    assert len(env.alerts.pushes) == 1
    assert env.count(EventType.JAM) == 1
    assert env.count(EventType.DISPENSE_FAILED) == 2
    assert env.feeder.state is FeederState.EMPTY_OR_JAMMED


async def test_clear_jam(env: Env):
    changes = []
    env.feeder.on_state_change = changes.append
    env.feeder.clear_jam()  # not jammed: no-op
    assert changes == []
    env.hw.drop_sensor.script(0, 0)
    await env.feeder.dispense(Source.TELEGRAM)
    env.feeder.clear_jam()
    assert env.feeder.state is FeederState.READY
    assert changes == [FeederState.EMPTY_OR_JAMMED, FeederState.READY]
    assert (await env.feeder.dispense(Source.SCHEDULE)).ok


async def test_multi_drop_counts_two(env: Env):
    env.hw.drop_sensor.script(2)
    r = await env.feeder.dispense(Source.TELEGRAM)
    assert r.ok and r.treats == 2 and r.treats_today == 2
    assert env.hw.motor.advances == 1


class SlowMotor(FakeMotor):
    def __init__(self, drop_sensor) -> None:
        super().__init__(drop_sensor)
        self.active = 0
        self.max_active = 0

    async def advance_pocket(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await super().advance_pocket()
        self.active -= 1


async def test_concurrent_dispenses_serialise(env: Env):
    motor = SlowMotor(env.hw.drop_sensor)
    feeder = Feeder(motor, env.hw.drop_sensor, env.policy, env.db, env.alerts, env.now)
    results = await asyncio.gather(*(feeder.dispense(Source.TELEGRAM) for _ in range(10)))
    assert motor.max_active == 1
    assert sum(r.ok for r in results) == 8
    assert sum(r.status is DispenseStatus.CAP_REACHED for r in results) == 2
    assert motor.advances == 8
    assert env.policy.treats_today() == 8


async def test_silent_alert_only_for_schedule_and_button(env: Env):
    await env.feeder.dispense(Source.SCHEDULE)
    assert len(env.alerts.silents) == 1
    assert "Scheduled treat dispensed (1/8 today)" in env.alerts.silents[0]
    await env.feeder.dispense(Source.BUTTON)
    assert len(env.alerts.silents) == 2
    for s in (Source.TELEGRAM, Source.VOICE, Source.SYSTEM):
        await env.feeder.dispense(s)
    assert len(env.alerts.silents) == 2


async def test_alert_failures_do_not_break(env: Env):
    env.alerts.raise_on_send = True
    r = await env.feeder.dispense(Source.SCHEDULE)
    assert r.ok
    env.hw.drop_sensor.script(0, 0)
    r = await env.feeder.dispense(Source.TELEGRAM)
    assert r.status is DispenseStatus.EMPTY_OR_JAMMED
    assert env.feeder.state is FeederState.EMPTY_OR_JAMMED


async def test_before_dispense_called_once_per_request(env: Env):
    calls = 0

    async def before() -> None:
        nonlocal calls
        calls += 1

    feeder = env.make(before_dispense=before)
    env.hw.drop_sensor.script(0, 1)
    await feeder.dispense(Source.TELEGRAM)
    assert calls == 1
    await feeder.dispense(Source.TELEGRAM)
    assert calls == 2


async def test_before_dispense_failure_ignored(env: Env):
    async def before() -> None:
        raise RuntimeError("sensor boom")

    r = await env.make(before_dispense=before).dispense(Source.TELEGRAM)
    assert r.ok


async def test_before_dispense_not_called_when_capped(env: Env):
    calls = 0

    async def before() -> None:
        nonlocal calls
        calls += 1

    feeder = env.make(before_dispense=before)
    for _ in range(8):
        await feeder.dispense(Source.TELEGRAM)
    await feeder.dispense(Source.TELEGRAM)
    assert calls == 8


async def test_motor_released_after_every_attempt(env: Env):
    await env.feeder.dispense(Source.TELEGRAM)
    assert env.hw.motor.released == 1
    env.hw.drop_sensor.script(0, 0)
    await env.feeder.dispense(Source.TELEGRAM)
    assert env.hw.motor.released == 2
    await env.feeder.dispense(Source.SCHEDULE)  # refused without moving: nothing to release
    assert env.hw.motor.released == 2


async def test_motor_exception_is_failed_attempt(env: Env):
    env.hw.motor.fail_next = True
    r = await env.feeder.dispense(Source.TELEGRAM)
    # first pocket faulted, second pocket succeeds
    assert r.ok and env.hw.motor.advances == 1 and env.hw.motor.released == 1
    env.hw.motor.fail_next = True
    env.hw.drop_sensor.script(0)
    r = await env.feeder.dispense(Source.TELEGRAM)
    assert r.status is DispenseStatus.EMPTY_OR_JAMMED
    assert env.hw.motor.released == 2
