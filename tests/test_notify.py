from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from feeder.core.policy import DailyCapPolicy
from feeder.models import EventType, Source, SupplyStatus
from feeder.notify import Notifier, build_daily_summary
from feeder.storage.db import Database

LA = ZoneInfo("America/Los_Angeles")


class FakeSender:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, bool]] = []
        self.fail = False
        self.fail_chats: set[int] = set()
        self.fail_after: int | None = None  # fail once this many successes happened
        self.delay = 0.0

    async def __call__(self, chat: int, text: str, silent: bool) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail or chat in self.fail_chats:
            raise ConnectionError("down")
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise ConnectionError("down mid-flush")
        self.calls.append((chat, text, silent))


@pytest.fixture
def db():
    d = Database.open(":memory:")
    yield d
    d.close()


@pytest.fixture
def sender():
    return FakeSender()


def make(sender, db, now, chats=(1,), **kw):
    return Notifier(sender, chats, db, now, **kw)


def types(db):
    return [
        e.type
        for e in db.events_between(
            datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)
        )
    ]


async def test_routing_push_vs_silent(sender, db, now):
    n = make(sender, db, now)
    await n.push("a")
    await n.silent("b")
    assert sender.calls == [(1, "a", False), (1, "b", True)]
    assert n.online and n.queue_size == 0


async def test_multi_chat(sender, db, now):
    n = make(sender, db, now, chats=(1, 2))
    await n.push("x")
    assert sender.calls == [(1, "x", False), (2, "x", False)]


async def test_one_chat_failing_does_not_block_other(sender, db, now):
    sender.fail_chats = {1}
    n = make(sender, db, now, chats=(1, 2))
    await n.push("x")
    assert sender.calls == [(2, "x", False)]
    assert n.queue_size == 1 and not n.online


async def test_failure_queues_and_marks_offline_once(sender, db, now):
    sender.fail = True
    n = make(sender, db, now)
    await n.push("a")
    await n.push("b")
    assert not n.online
    assert n.queue_size == 2
    assert types(db) == [EventType.OFFLINE]


async def test_recovery_flushes_in_order_then_back_online(sender, db, now):
    sender.fail = True
    n = make(sender, db, now)
    await n.push("a")
    await n.silent("b")
    now.advance(minutes=12)
    sender.fail = False
    await n.set_connectivity(True)
    assert [c[1:] for c in sender.calls] == [
        ("(queued 12:00 UTC) a", False),
        ("(queued 12:00 UTC) b", True),
        ("✅ Back online (down for 12 min)", False),
    ]
    assert n.online and n.queue_size == 0
    evs = db.events_between(datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC))
    assert [e.type for e in evs] == [EventType.OFFLINE, EventType.ONLINE]
    assert evs[1].source is Source.SYSTEM and evs[1].detail == "720"


async def test_next_successful_push_recovers_and_keeps_order(sender, db, now):
    sender.fail = True
    n = make(sender, db, now)
    await n.push("a")
    sender.fail = False
    now.advance(seconds=30)
    await n.push("b")
    texts = [c[1] for c in sender.calls]
    assert texts == [
        "(queued 12:00 UTC) a",
        "(queued 12:00 UTC) b",
        "✅ Back online (down for 30 s)",
    ]
    assert n.online


async def test_downtime_humanize_hours(sender, db, now):
    await make(sender, db, now).set_connectivity(False)  # separate notifier, no effect
    n = make(sender, db, now)
    await n.set_connectivity(False)
    now.advance(hours=2, minutes=5)
    await n.set_connectivity(True)
    assert sender.calls[-1][1] == "✅ Back online (down for 2 h 5 min)"


async def test_bounded_queue_drops_oldest(sender, db, now):
    sender.fail = True
    n = make(sender, db, now, max_queue=2)
    for t in ("a", "b", "c"):
        await n.push(t)
    assert n.queue_size == 2
    sender.fail = False
    await n.set_connectivity(True)
    assert [c[1] for c in sender.calls][:2] == ["(queued 12:00 UTC) b", "(queued 12:00 UTC) c"]


async def test_set_connectivity_false_once_and_true_noop_when_online(sender, db, now):
    n = make(sender, db, now)
    await n.set_connectivity(True)
    assert sender.calls == [] and types(db) == []
    await n.set_connectivity(False)
    await n.set_connectivity(False)
    assert not n.online and types(db) == [EventType.OFFLINE]


async def test_offline_without_queue_sends_directly_and_recovers(sender, db, now):
    n = make(sender, db, now)
    await n.set_connectivity(False)
    await n.push("hi")
    assert [c[1] for c in sender.calls][0] == "hi"
    assert n.online
    assert types(db) == [EventType.OFFLINE, EventType.ONLINE]


async def test_flush_failure_keeps_remainder_and_offline(sender, db, now):
    sender.fail = True
    n = make(sender, db, now)
    for t in ("a", "b", "c"):
        await n.push(t)
    sender.fail = False
    sender.fail_after = 1  # one succeeds, then fail
    await n.set_connectivity(True)
    assert [c[1] for c in sender.calls] == ["(queued 12:00 UTC) a"]
    assert n.queue_size == 2 and not n.online
    assert types(db) == [EventType.OFFLINE]
    sender.fail_after = None
    await n.flush()
    assert [c[1] for c in sender.calls][1:3] == ["(queued 12:00 UTC) b", "(queued 12:00 UTC) c"]
    assert n.online and types(db) == [EventType.OFFLINE, EventType.ONLINE]


async def test_never_raises(db, now):
    async def boom(chat, text, silent):
        raise RuntimeError("x")

    n = Notifier(boom, [1], db, now)
    await n.push("a")
    await n.silent("b")
    await n.set_connectivity(True)
    await n.flush()
    assert not n.online


async def test_never_raises_when_db_broken(sender, now):
    class BadDb:
        def add_event(self, *a, **k):
            raise RuntimeError("db")

    sender.fail = True
    n = Notifier(sender, [1], BadDb(), now)  # type: ignore[arg-type]
    await n.push("a")
    assert n.queue_size == 1


async def test_timeout_path(sender, db, now):
    sender.delay = 1.0
    n = make(sender, db, now, send_timeout_s=0.01)
    await n.push("slow")
    assert n.queue_size == 1 and not n.online


# daily summary ---------------------------------------------------------


def policy_for(db, now, cap=8):
    return DailyCapPolicy(db, LA, cap, now)


def test_summary_content(db, now):
    # 2026-11-17 12:00 UTC == 04:00 PST; day is Nov 17 local
    t = lambda h, m: datetime(2026, 11, 17, h, m, tzinfo=UTC)  # noqa: E731
    for src, ts in [
        (Source.SCHEDULE, t(8, 0)),
        (Source.SCHEDULE, t(9, 0)),
        (Source.SCHEDULE, t(10, 0)),
        (Source.TELEGRAM, t(10, 30)),
        (Source.BUTTON, t(11, 2)),
    ]:
        db.add_event(EventType.DISPENSE, src, treats=1, ts=ts)
    db.add_event(EventType.SCHEDULE_SKIPPED, Source.SCHEDULE, ts=t(11, 10))
    db.add_event(EventType.JAM, Source.SCHEDULE, ts=t(11, 11))
    db.add_event(
        EventType.DISPENSE, Source.BUTTON, treats=1, ts=datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    )
    supply = SupplyStatus(pct=34.0, treats_left=27, days_left=5.2, low=False, last_reading_ts=None)
    s = build_daily_summary(policy_for(db, now), db, supply, LA, now)
    assert s.startswith("📅 Daily summary\n")
    assert "🐟 5/8 treats today (⏰ 3 · 📱 1 · 🔘 1)" in s
    assert "Last treat 03:02" in s
    assert "🥣 Hopper 34% (~27 treats, ~5 days left)" in s
    assert "⚠️ 1 skipped · 1 jam" in s


def test_summary_zero_day_and_missing_supply(db, now):
    supply = SupplyStatus(
        pct=None, treats_left=None, days_left=None, low=False, last_reading_ts=None
    )
    s = build_daily_summary(policy_for(db, now), db, supply, LA, now)
    assert "🐟 0/8 treats today" in s and "(" not in s.split("\n")[1]
    assert "Last treat" not in s
    assert "🥣 Hopper level unknown" in s
    assert "⚠️" not in s


def test_summary_partial_supply(db, now):
    supply = SupplyStatus(
        pct=10.0, treats_left=None, days_left=None, low=True, last_reading_ts=None
    )
    s = build_daily_summary(policy_for(db, now), db, supply, LA, now)
    assert "🥣 Hopper 10%" in s and "~" not in s
