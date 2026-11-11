from __future__ import annotations

import random
from datetime import time
from zoneinfo import ZoneInfo

import pytest

from feeder.adapters.scheduler import FeedScheduler
from feeder.core.feeder import Feeder
from feeder.core.policy import DailyCapPolicy
from feeder.hardware.fake import FakeHardware
from feeder.models import EventType, Source
from feeder.storage.db import Database

TZ = ZoneInfo("America/Los_Angeles")


class RecAlerts:
    def __init__(self) -> None:
        self.pushes: list[str] = []
        self.silents: list[str] = []

    async def push(self, text: str) -> None:
        self.pushes.append(text)

    async def silent(self, text: str) -> None:
        self.silents.append(text)


class StubClock:
    def __init__(self, ok: bool = True, why: str = "clock reads 1970, before 2026") -> None:
        self.ok = ok
        self.why = why

    def is_trustworthy(self) -> bool:
        return self.ok

    def reason(self) -> str:
        return self.why


class Env:
    def __init__(self, now, cap: int = 8, **kw) -> None:
        self.now = now
        self.db = Database.open(":memory:")
        self.hw = FakeHardware.create()
        self.alerts = RecAlerts()
        self.clock = StubClock()
        self.policy = DailyCapPolicy(self.db, TZ, cap, now)
        self.feeder = Feeder(
            self.hw.motor, self.hw.drop_sensor, self.policy, self.db, self.alerts, now
        )
        self.sleeps: list[float] = []
        self.summary_calls = 0

        async def fake_sleep(s: float) -> None:
            self.sleeps.append(s)

        async def summary() -> None:
            self.summary_calls += 1

        args = dict(sleep=fake_sleep, rng=random.Random(42))
        args.update(kw)
        self.sched = FeedScheduler(self.db, self.feeder, self.clock, self.alerts, TZ, now, **args)
        self.summary = summary

    def events(self, t: EventType):
        return [e for e in self.db.events_between(*self.policy.day_bounds()) if e.type == t]


@pytest.fixture
def env(now) -> Env:
    return Env(now)


def fields(job) -> tuple[str, str]:
    f = {x.name: str(x) for x in job.trigger.fields}
    return f["hour"], f["minute"]


async def test_run_slot_happy_path(env: Env):
    s = env.db.add_schedule("08:00", count=3)
    await env.sched.run_slot(s)
    assert env.hw.motor.advances == 3
    assert len(env.events(EventType.DISPENSE)) == 3
    assert env.events(EventType.SCHEDULE_SKIPPED) == []
    assert env.alerts.pushes == []


async def test_run_slot_cap_hits_mid_slot(now):
    env = Env(now, cap=2)
    s = env.db.add_schedule("08:00", count=3)
    await env.sched.run_slot(s)
    assert env.hw.motor.advances == 2
    [ev] = env.events(EventType.SCHEDULE_SKIPPED)
    assert ev.source is Source.SCHEDULE
    assert "cap_reached" in ev.detail and "2 of 3" in ev.detail
    assert ev.ts == now()
    assert env.alerts.pushes == ["⏭️ Scheduled treat at 08:00 skipped: daily cap reached"]


async def test_run_slot_jam_stops(env: Env):
    s = env.db.add_schedule("08:00", count=3)
    env.hw.drop_sensor.script(1, 0, 0)
    await env.sched.run_slot(s)
    assert len(env.events(EventType.DISPENSE)) == 1
    [ev] = env.events(EventType.SCHEDULE_SKIPPED)
    assert "empty_or_jammed" in ev.detail and "1 of 3" in ev.detail
    assert any("skipped: feeder empty or jammed" in p for p in env.alerts.pushes)


async def test_run_slot_untrusted_clock(env: Env):
    env.clock.ok = False
    s = env.db.add_schedule("08:00", count=2)
    await env.sched.run_slot(s)
    assert env.hw.motor.advances == 0
    [ev] = env.events(EventType.SCHEDULE_SKIPPED)
    assert ev.detail == env.clock.why
    assert len(env.alerts.pushes) == 1 and "08:00" in env.alerts.pushes[0]


async def test_run_slot_swallows_exceptions(env: Env):
    s = env.db.add_schedule("08:00")

    async def boom(source):
        raise RuntimeError("kaboom")

    env.feeder.dispense = boom  # type: ignore[method-assign]
    await env.sched.run_slot(s)  # must not raise

    env.clock.is_trustworthy = lambda: (_ for _ in ()).throw(ValueError("x"))  # type: ignore[method-assign]
    await env.sched.run_slot(s)


def test_reload_builds_jobs(env: Env):
    a = env.db.add_schedule("08:00")
    b = env.db.add_schedule("17:30")
    c = env.db.add_schedule("12:00")
    env.db.set_schedule_enabled(c.id, False)
    env.sched.reload()
    jobs = {j.id: j for j in env.sched._sched.get_jobs()}
    assert set(jobs) == {f"slot-{a.id}", f"slot-{b.id}"}
    assert fields(jobs[f"slot-{a.id}"]) == ("8", "0")
    assert fields(jobs[f"slot-{b.id}"]) == ("17", "30")
    j = jobs[f"slot-{a.id}"]
    assert j.coalesce is True and j.max_instances == 1 and j.misfire_grace_time == 60
    assert str(j.trigger.timezone) == "America/Los_Angeles"


def test_reload_reflects_changes_and_idempotent(env: Env):
    a = env.db.add_schedule("08:00")
    env.sched.reload()
    env.sched.reload()
    assert [j.id for j in env.sched._sched.get_jobs()] == [f"slot-{a.id}"]
    b = env.db.add_schedule("09:15")
    env.db.remove_schedule(a.id)
    env.sched.reload()
    assert [j.id for j in env.sched._sched.get_jobs()] == [f"slot-{b.id}"]


@pytest.mark.parametrize(
    ("slot", "jitter", "expected"),
    [("08:00", 5, ("7", "55")), ("00:03", 5, ("23", "58")), ("10:02", 10, ("9", "52"))],
)
def test_jitter_arithmetic(env: Env, slot, jitter, expected):
    s = env.db.add_schedule(slot, jitter_min=jitter)
    env.sched.reload()
    [job] = env.sched._sched.get_jobs()
    assert job.id == f"slot-{s.id}"
    assert fields(job) == expected


async def test_jitter_sleep_range(env: Env):
    s = env.db.add_schedule("08:00", count=1, jitter_min=5)
    rng = random.Random(7)
    expected = rng.uniform(0, 10) * 60
    env.sched._rng = random.Random(7)
    await env.sched._run_slot_job(s)
    assert env.sleeps == [pytest.approx(expected)]
    assert 0 <= env.sleeps[0] <= 600
    assert env.hw.motor.advances == 1


async def test_no_jitter_no_sleep(env: Env):
    s = env.db.add_schedule("08:00")
    await env.sched._run_slot_job(s)
    assert env.sleeps == []
    assert env.hw.motor.advances == 1


async def test_summary_job(now):
    env = Env(now)
    env.sched._daily_summary = env.summary
    env.sched._summary_time = time(21, 0)
    env.sched.reload()
    [job] = env.sched._sched.get_jobs()
    assert job.id == "daily-summary"
    assert fields(job) == ("21", "0")
    await env.sched._run_summary()
    assert env.summary_calls == 1

    async def bad() -> None:
        raise RuntimeError("x")

    env.sched._daily_summary = bad
    await env.sched._run_summary()  # swallowed


async def test_start_next_run_times_and_shutdown(env: Env):
    a = env.db.add_schedule("08:00")
    env.sched.start()
    try:
        nrt = env.sched.next_run_times()
        assert set(nrt) == {f"slot-{a.id}"}
        assert nrt[f"slot-{a.id}"].astimezone(TZ).strftime("%H:%M") == "08:00"
        env.db.add_schedule("09:00")
        env.sched.reload()
        assert len(env.sched.next_run_times()) == 2
    finally:
        env.sched.shutdown()
    env.sched.shutdown()


def test_shutdown_without_start(env: Env):
    env.sched.shutdown()
