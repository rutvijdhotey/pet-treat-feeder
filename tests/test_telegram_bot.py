from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from telegram.error import NetworkError

from feeder.adapters import telegram_bot as tb
from feeder.core.feeder import FeederState
from feeder.core.policy import DailyCapPolicy
from feeder.models import (
    DispenseResult,
    DispenseStatus,
    EventType,
    Source,
    SupplyStatus,
)
from feeder.storage.db import Database

TZ = ZoneInfo("Europe/Berlin")
OWNER = 111
STRANGER = 999


class StubFeeder:
    def __init__(self, result: DispenseResult | Exception) -> None:
        self.result = result
        self.state = FeederState.READY
        self.calls: list[Source] = []

    async def dispense(self, source: Source) -> DispenseResult:
        self.calls.append(source)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def res(status: DispenseStatus, n: int = 3, cap: int = 8) -> DispenseResult:
    return DispenseResult(status, Source.TELEGRAM, 1 if status is DispenseStatus.OK else 0, n, cap)


@pytest.fixture
def db():
    d = Database.open(":memory:")
    yield d
    d.close()


@pytest.fixture
def reload_mock():
    return MagicMock()


@pytest.fixture
def deps(db, now, reload_mock):
    return tb.BotDeps(
        feeder=StubFeeder(res(DispenseStatus.OK)),
        policy=DailyCapPolicy(db, TZ, 8, now),
        db=db,
        supply_status=lambda: SupplyStatus(62.4, 31, 4.5, False, None),
        reload_schedules=reload_mock,
        tz=TZ,
        now=now,
        started_at=now() - timedelta(days=1, hours=3),
        default_jitter_min=2,
        allowed_user_ids=frozenset({OWNER}),
        wifi_signal=lambda: "-52 dBm",
    )


def add_dispense(db, ts, source=Source.TELEGRAM):
    db.add_event(EventType.DISPENSE, source, treats=1, ts=ts)


# ------------------------------------------------------------------ logic


async def test_treat_ok(deps):
    assert await tb.handle_treat(deps) == "Treat dispensed ✅ (3/8 today)"
    assert deps.feeder.calls == [Source.TELEGRAM]


async def test_treat_cap(deps):
    deps.feeder.result = res(DispenseStatus.CAP_REACHED, 8)
    assert await tb.handle_treat(deps) == "Daily limit reached (8/8) 🚫"


async def test_treat_empty(deps):
    deps.feeder.result = res(DispenseStatus.EMPTY_OR_JAMMED)
    text = await tb.handle_treat(deps)
    assert "empty or jammed" in text and "not counted" in text and "/treat again" in text


async def test_treat_exception_is_swallowed(deps):
    deps.feeder.result = RuntimeError("boom")
    assert "Something went wrong" in await tb.handle_treat(deps)


async def test_status(deps, db, now):
    add_dispense(db, now() - timedelta(hours=2), Source.SCHEDULE)
    text = await tb.handle_status(deps)
    assert "Hopper: 62%" in text
    assert "~31" in text and "~4.5" in text
    assert "11:00 ⏰" in text
    assert "Today: 1/8" in text
    assert "ready" in text
    assert "Uptime: 1d 3h" in text
    assert "-52 dBm" in text


async def test_status_uncalibrated_no_wifi(deps):
    deps.supply_status = lambda: SupplyStatus(None, None, None, False, None)
    deps.wifi_signal = lambda: None
    text = await tb.handle_status(deps)
    assert "not calibrated" in text
    assert "Wi-Fi" not in text
    assert "Last treat: none yet" in text


async def test_status_wifi_raises(deps):
    def boom():
        raise OSError

    deps.wifi_signal = boom
    assert "Wi-Fi" not in await tb.handle_status(deps)


async def test_today_empty(deps):
    assert await tb.handle_today(deps) == "Today: 0/8\nNo treats yet today"


async def test_today_list(deps, db, now):
    add_dispense(db, datetime(2026, 11, 17, 7, 0, tzinfo=UTC), Source.SCHEDULE)
    add_dispense(db, datetime(2026, 11, 17, 9, 30, tzinfo=UTC), Source.BUTTON)
    add_dispense(db, datetime(2026, 11, 16, 9, 30, tzinfo=UTC), Source.BUTTON)  # yesterday
    text = await tb.handle_today(deps)
    assert text == "Today: 2/8\n08:00 ⏰\n10:30 🔘"


async def test_last_none(deps):
    assert await tb.handle_last(deps) == "No treats dispensed yet"


async def test_last(deps, db, now):
    add_dispense(db, now() - timedelta(hours=2, minutes=5), Source.TELEGRAM)
    text = await tb.handle_last(deps)
    assert "2h 5m ago" in text and "📱" in text and "telegram" in text
    assert "10:55" in text


def test_humanise():
    assert tb.humanise(30) == "0m"
    assert tb.humanise(125 * 60) == "2h 5m"
    assert tb.humanise(90000) == "1d 1h"
    assert tb.humanise(-5) == "0m"


def test_help_and_keyboard():
    assert "/schedule add" in tb.help_text()
    kb = tb.main_keyboard()
    data = [b.callback_data for b in kb.inline_keyboard[0]]
    assert data == ["treat", "status", "today"]
    assert "🐟" in kb.inline_keyboard[0][0].text


def test_wifi_reader(tmp_path):
    f = tmp_path / "wireless"
    f.write_text(
        "Inter-| sta-|   Quality        |   Discarded packets\n"
        " face | tus | link level noise |  nwid\n"
        " wlan0: 0000   60.  -52.  -256        0\n"
    )
    assert tb.read_wifi_signal(str(f)) == "-52 dBm"
    assert tb.read_wifi_signal(str(tmp_path / "missing")) is None
    f.write_text("garbage")
    assert tb.read_wifi_signal(str(f)) is None


# --------------------------------------------------------------- schedule


async def test_schedule_add_default_count(deps, db, reload_mock):
    text = await tb.handle_schedule(deps, ["add", "08:00"], OWNER)
    assert "#1" in text and "08:00" in text
    s = db.list_schedules()[0]
    assert (s.time_local, s.count, s.jitter_min, s.created_by) == ("08:00", 1, 2, OWNER)
    reload_mock.assert_called_once()


async def test_schedule_add_count(deps, db):
    await tb.handle_schedule(deps, ["add", "21:30", "3"], OWNER)
    assert db.list_schedules()[0].count == 3


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["bogus"],
        ["add"],
        ["add", "24:00"],
        ["add", "9:5"],
        ["add", "08:60"],
        ["add", "abc"],
        ["add", "08:00", "0"],
        ["add", "08:00", "9"],
        ["add", "08:00", "x"],
        ["add", "08:00", "-1"],
        ["add", "08:00", "1", "extra"],
        ["remove"],
        ["remove", "abc"],
    ],
)
async def test_schedule_bad_args(deps, db, reload_mock, args):
    text = await tb.handle_schedule(deps, args, OWNER)
    assert "Usage" in text or "/schedule" in text
    assert db.list_schedules() == []
    reload_mock.assert_not_called()


async def test_schedule_list(deps, db):
    assert "No schedules" in await tb.handle_schedule(deps, ["list"], OWNER)
    db.add_schedule("08:00", 2)
    s2 = db.add_schedule("18:00", 1)
    db.set_schedule_enabled(s2.id, False)
    text = await tb.handle_schedule(deps, ["list"], OWNER)
    assert "#1 08:00 x2 enabled" in text
    assert "#2 18:00 x1 disabled" in text


async def test_schedule_remove(deps, db, reload_mock):
    db.add_schedule("08:00")
    assert "removed" in await tb.handle_schedule(deps, ["remove", "1"], OWNER)
    assert db.list_schedules() == []
    reload_mock.assert_called_once()


async def test_schedule_remove_unknown(deps, reload_mock):
    assert "No schedule with id 42" in await tb.handle_schedule(deps, ["remove", "42"], OWNER)
    reload_mock.assert_not_called()


async def test_schedule_db_error_does_not_crash(deps, monkeypatch):
    monkeypatch.setattr(deps.db, "add_schedule", MagicMock(side_effect=RuntimeError("x")))
    assert "Something went wrong" in await tb.handle_schedule(deps, ["add", "08:00"], OWNER)


# --------------------------------------------------------------- wrappers


def make_update(user_id: int | None, *, callback: str | None = None):
    update = MagicMock()
    if user_id is None:
        update.effective_user = None
    else:
        update.effective_user = MagicMock(id=user_id)
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    if callback is None:
        update.callback_query = None
    else:
        q = MagicMock()
        q.data = callback
        q.answer = AsyncMock()
        q.message = MagicMock()
        q.message.reply_text = AsyncMock()
        update.callback_query = q
    return update


def make_context(args=None):
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


async def test_command_wrapper_replies(deps):
    h = tb.make_command_handler(deps, tb.handle_treat)
    u = make_update(OWNER)
    await h(u, make_context())
    u.effective_message.reply_text.assert_awaited_once()
    assert u.effective_message.reply_text.await_args.args[0].startswith("Treat dispensed")


async def test_help_wrapper_has_keyboard(deps):
    u = make_update(OWNER)
    await tb.make_help_handler(deps)(u, make_context())
    kw = u.effective_message.reply_text.await_args.kwargs
    assert kw["reply_markup"] is not None


async def test_schedule_wrapper(deps, db):
    u = make_update(OWNER)
    await tb.make_schedule_handler(deps)(u, make_context(["add", "07:15", "2"]))
    assert db.list_schedules()[0].time_local == "07:15"
    assert db.list_schedules()[0].created_by == OWNER
    u.effective_message.reply_text.assert_awaited_once()


async def test_wrappers_ignore_strangers(deps, db):
    for h in (
        tb.make_command_handler(deps, tb.handle_treat),
        tb.make_help_handler(deps),
        tb.make_schedule_handler(deps),
        tb.make_callback_handler(deps),
    ):
        u = make_update(STRANGER, callback="treat")
        await h(u, make_context(["add", "08:00"]))
        u.effective_message.reply_text.assert_not_awaited()
        u.callback_query.answer.assert_not_awaited()
        u.callback_query.message.reply_text.assert_not_awaited()
    assert deps.feeder.calls == []
    assert db.list_schedules() == []


async def test_unauthorized_message_recorded_no_reply(deps, db, now):
    u = make_update(STRANGER)
    await tb.make_unauthorized_handler(deps)(u, make_context())
    u.effective_message.reply_text.assert_not_awaited()
    ev = db.last_event(EventType.UNAUTHORIZED)
    assert ev is not None
    assert ev.detail == f"user_id={STRANGER}"
    assert ev.source is Source.TELEGRAM
    assert ev.ts == now()


async def test_unauthorized_callback_recorded_not_answered(deps, db):
    u = make_update(STRANGER, callback="treat")
    await tb.make_unauthorized_handler(deps)(u, make_context())
    u.callback_query.answer.assert_not_awaited()
    u.callback_query.message.reply_text.assert_not_awaited()
    assert db.last_event(EventType.UNAUTHORIZED) is not None


async def test_catch_all_ignores_allowed_and_userless(deps, db):
    h = tb.make_unauthorized_handler(deps)
    await h(make_update(OWNER), make_context())
    await h(make_update(None), make_context())
    assert db.last_event(EventType.UNAUTHORIZED) is None


async def test_catch_all_survives_db_error(deps, monkeypatch):
    monkeypatch.setattr(deps.db, "add_event", MagicMock(side_effect=RuntimeError("x")))
    await tb.make_unauthorized_handler(deps)(make_update(STRANGER), make_context())


@pytest.mark.parametrize(
    ("data", "expect"),
    [("treat", "Treat dispensed"), ("status", "Hopper"), ("today", "Today: 0/8")],
)
async def test_callback_buttons(deps, data, expect):
    u = make_update(OWNER, callback=data)
    await tb.make_callback_handler(deps)(u, make_context())
    u.callback_query.answer.assert_awaited_once()
    u.callback_query.message.reply_text.assert_awaited_once()
    assert expect in u.callback_query.message.reply_text.await_args.args[0]


async def test_callback_unknown_data_answers_only(deps):
    u = make_update(OWNER, callback="nope")
    await tb.make_callback_handler(deps)(u, make_context())
    u.callback_query.answer.assert_awaited_once()
    u.callback_query.message.reply_text.assert_not_awaited()


# ------------------------------------------------------------ application


def test_build_application_dummy_token(deps):
    app = tb.build_application("123456:dummy-token", deps)
    assert app.handlers[0]
    assert 1 in app.handlers  # catch-all group
    assert tb.error_handler in app.error_handlers


async def test_error_handler_network_error_no_raise():
    ctx = MagicMock()
    ctx.error = NetworkError("down")
    await tb.error_handler(None, ctx)
    ctx.error = RuntimeError("bug")
    await tb.error_handler(None, ctx)


# -------------------------------------------------------------- TelegramBot


def fake_app():
    app = MagicMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.updater.start_polling = AsyncMock()
    app.updater.stop = AsyncMock()
    app.bot.get_me = AsyncMock()
    app.bot.send_message = AsyncMock()
    return app


def make_bot(deps, app, **kw):
    return tb.TelegramBot("tok", deps, _application_factory=lambda t, d: app, **kw)


async def test_start_stop_lifecycle(deps):
    app = fake_app()
    bot = make_bot(deps, app, heartbeat_s=3600)
    await bot.start()
    app.initialize.assert_awaited_once()
    app.start.assert_awaited_once()
    app.updater.start_polling.assert_awaited_once_with(drop_pending_updates=False)
    await bot.stop()
    app.updater.stop.assert_awaited_once()
    app.stop.assert_awaited_once()
    app.shutdown.assert_awaited_once()
    await bot.stop()  # idempotent
    app.shutdown.assert_awaited_once()


async def test_stop_without_start(deps):
    await make_bot(deps, fake_app()).stop()


async def test_stop_survives_errors(deps):
    app = fake_app()
    app.updater.stop.side_effect = RuntimeError("x")
    bot = make_bot(deps, app, heartbeat_s=3600)
    await bot.start()
    await bot.stop()
    app.shutdown.assert_awaited_once()


async def test_send(deps):
    app = fake_app()
    bot = make_bot(deps, app, heartbeat_s=3600)
    await bot.start()
    await bot.send(42, "hi", True)
    app.bot.send_message.assert_awaited_once_with(42, "hi", disable_notification=True)
    await bot.send(42, "yo", False)
    assert app.bot.send_message.await_args.kwargs == {"disable_notification": False}
    app.bot.send_message.side_effect = NetworkError("down")
    with pytest.raises(NetworkError):
        await bot.send(42, "x", False)
    await bot.stop()


async def test_send_before_start_raises(deps):
    with pytest.raises(RuntimeError):
        await make_bot(deps, fake_app()).send(1, "x", False)


async def test_heartbeat_transitions(deps):
    app = fake_app()
    seen: list[bool] = []
    app.bot.get_me.side_effect = [
        MagicMock(),
        MagicMock(),
        NetworkError("down"),
        NetworkError("down"),
        MagicMock(),
    ]

    async def cb(ok: bool) -> None:
        seen.append(ok)

    bot = make_bot(deps, app, on_connectivity=cb)
    bot._app = app
    for _ in range(5):
        await bot._check_once()
    assert seen == [True, False, True]


async def test_heartbeat_sync_callback_and_cb_errors(deps):
    app = fake_app()
    seen: list[bool] = []

    def cb(ok: bool) -> None:
        seen.append(ok)
        raise RuntimeError("cb bug")

    bot = make_bot(deps, app, on_connectivity=cb)
    bot._app = app
    await bot._check_once()
    assert seen == [True]


async def test_heartbeat_loop_runs_and_cancels(deps):
    app = fake_app()
    seen: list[bool] = []
    bot = make_bot(deps, app, on_connectivity=seen.append, heartbeat_s=0.01)
    await bot.start()
    await asyncio.sleep(0.08)
    assert app.bot.get_me.await_count >= 2
    assert seen == [True]
    await bot.stop()
    n = app.bot.get_me.await_count
    await asyncio.sleep(0.05)
    assert app.bot.get_me.await_count == n
