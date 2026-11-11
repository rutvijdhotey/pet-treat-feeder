from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from feeder.app import FeederApp, RedactingFilter, configure_logging, main
from feeder.config import Config, load_config
from feeder.core.clock import ClockGuard
from feeder.hardware.base import Led, LedMode
from feeder.hardware.fake import FakeHardware
from feeder.models import EventType, Source

TOKEN = "123456:SECRET-TOKEN-abc"
CONFIG_YAML = """
telegram:
  allowed_user_ids: [111]
timezone: America/Los_Angeles
treats: {daily_cap: 8, capacity: 80}
supply:
  low_threshold_pct: 20
  low_clear_pct: 30
  refill_rise_pct: 15
  poll_interval_s: 300
  empty_mm: 200
  full_mm: 40
notifications: {daily_summary: "21:00"}
leds: {brightness: 0.4}
db_path: ":memory:"
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def config(config_path: Path) -> Config:
    return load_config(config_path, None, {"TELEGRAM_BOT_TOKEN": TOKEN})


async def _sleep(s: float) -> None:
    # long sleeps (poll loops) really wait (and get cancelled); short ones just yield
    await asyncio.sleep(3600 if s >= 100 else 0)


class Rig:
    def __init__(self, config: Config, now, sender=None, clock=None) -> None:
        self.hw = FakeHardware.create()
        self.hw.level_sensor.set_distance(120)  # 50%
        self.sent: list[tuple[int, str, bool]] = []
        self.fail_send = False

        async def _send(chat: int, text: str, silent: bool) -> None:
            if self.fail_send:
                raise ConnectionError("offline")
            self.sent.append((chat, text, silent))

        trusted = ClockGuard(now, ntp_synced=lambda: True, rtc_present=lambda: False)
        self.app = FeederApp.build(
            config, self.hw, sender=sender or _send, now=now, sleep=_sleep, clock=clock or trusted
        )

    def texts(self) -> list[str]:
        return [t for _, t, _ in self.sent]

    def events(self, type_: EventType):
        return [e for e in self.app.db.events_between(*_wide()) if e.type is type_]


def _wide():
    from datetime import UTC, datetime

    return datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)


async def _yield(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def test_lifecycle(config, now) -> None:
    rig = Rig(config, now)
    assert rig.app.bot is None
    await rig.app.start()
    await _yield()
    assert rig.hw.leds.state[Led.POWER][0] is LedMode.SOLID
    assert rig.events(EventType.BOOT)
    await rig.app.stop()
    await rig.app.stop()  # idempotent
    assert all(mode is LedMode.OFF for mode, _ in rig.hw.leds.state.values())
    assert rig.hw.motor.released >= 1


async def test_shutdown_event_recorded(config, now, tmp_path) -> None:
    cfg = config.model_copy(update={"db_path": str(tmp_path / "f.db")})
    rig = Rig(cfg, now)
    await rig.app.start()
    await rig.app.stop()
    from feeder.storage.db import Database

    db = Database.open(tmp_path / "f.db")
    types = [e.type for e in db.events_between(*_wide())]
    assert EventType.BOOT in types and EventType.SHUTDOWN in types


async def test_button_press_dispenses(config, now) -> None:
    rig = Rig(config, now)
    await rig.app.start()
    rig.hw.buttons.press_treat()
    await _yield()
    ev = rig.events(EventType.DISPENSE)
    assert len(ev) == 1 and ev[0].source is Source.BUTTON
    assert any(silent and "Button" in t for _, t, silent in rig.sent)
    await rig.app.stop()


async def test_cap_enforced_across_sources(config, now) -> None:
    rig = Rig(config, now)
    await rig.app.start()
    for i in range(8):
        if i % 2:
            now.advance(seconds=1)
            rig.hw.buttons.press_treat()
            await _yield()
        else:
            res = await rig.app.feeder.dispense(Source.TELEGRAM)
            assert res.ok
    assert len(rig.events(EventType.DISPENSE)) == 8
    now.advance(seconds=1)
    rig.hw.buttons.press_treat()
    await _yield()
    assert len(rig.events(EventType.DISPENSE)) == 8
    assert rig.events(EventType.CAP_REACHED)
    await rig.app.stop()


async def test_jam_alert_led_and_refill_clears(config, now) -> None:
    rig = Rig(config, now)
    await rig.app.start()
    await rig.app.supply.read()  # baseline 50%
    rig.hw.drop_sensor.script(0, 0)
    res = await rig.app.feeder.dispense(Source.TELEGRAM)
    assert not res.ok
    assert any(not silent and "jammed" in t for _, t, silent in rig.sent)
    assert rig.hw.leds.state[Led.SUPPLY][0] is LedMode.BLINK
    rig.hw.level_sensor.set_distance(60)  # ~87%: refill
    await rig.app.supply.read()
    assert rig.events(EventType.REFILL)
    assert rig.hw.leds.state[Led.SUPPLY][0] is LedMode.OFF
    res = await rig.app.feeder.dispense(Source.SCHEDULE)
    assert res.ok
    await rig.app.stop()


async def test_low_supply_alert(config, now) -> None:
    rig = Rig(config, now)
    rig.hw.level_sensor.set_distance(180)  # 12.5%
    await rig.app.start()
    await rig.app.supply.read()
    assert rig.events(EventType.LOW_SUPPLY)
    assert any(not silent and "low" in t for _, t, silent in rig.sent)
    assert rig.hw.leds.state[Led.SUPPLY][0] is LedMode.SOLID
    await rig.app.stop()


async def test_scheduler_wiring(config, now) -> None:
    rig = Rig(config, now)
    sched = rig.app.db.add_schedule("08:00", count=1)
    await rig.app.start()
    await rig.app.scheduler.run_slot(sched)
    ev = rig.events(EventType.DISPENSE)
    assert len(ev) == 1 and ev[0].source is Source.SCHEDULE
    await rig.app.stop()


async def test_untrusted_clock_skips(config, now) -> None:
    bad = ClockGuard(now, ntp_synced=lambda: False, rtc_present=lambda: False)
    rig = Rig(config, now, clock=bad)
    sched = rig.app.db.add_schedule("08:00", count=1)
    await rig.app.start()
    await rig.app.scheduler.run_slot(sched)
    assert not rig.events(EventType.DISPENSE)
    assert rig.events(EventType.SCHEDULE_SKIPPED)
    assert any(not silent and "skipped" in t for _, t, silent in rig.sent)
    await rig.app.stop()


async def test_offline_queue_and_recovery(config, now) -> None:
    rig = Rig(config, now)
    sched = rig.app.db.add_schedule("08:00", count=1)
    await rig.app.start()
    rig.fail_send = True
    await rig.app.scheduler.run_slot(sched)
    assert rig.app.notifier.queue_size >= 1
    assert not rig.app.notifier.online
    rig.fail_send = False
    await rig.app.notifier.set_connectivity(True)
    assert rig.app.notifier.queue_size == 0 and rig.app.notifier.online
    assert any("queued" in t for t in rig.texts())
    assert any("Back online" in t for t in rig.texts())
    await rig.app.stop()


async def test_bot_start_failure_does_not_stop_app(config, now) -> None:
    rig = Rig(config, now)

    class BadBot:
        started = 0

        async def start(self) -> None:
            BadBot.started += 1
            raise ConnectionError("no internet")

        async def stop(self) -> None:
            pass

    rig.app.bot = BadBot()  # type: ignore[assignment]
    await rig.app.start()
    await _yield()
    assert BadBot.started == 1
    assert not rig.app.notifier.online
    res = await rig.app.feeder.dispense(Source.TELEGRAM)
    assert res.ok
    await rig.app.stop()


def test_token_redaction(capsys) -> None:
    configure_logging("INFO", secrets=[TOKEN])
    try:
        logging.getLogger("feeder.test").info("url https://api.telegram.org/bot%s/getMe", TOKEN)
        try:
            raise RuntimeError(f"bad {TOKEN}")
        except RuntimeError:
            logging.getLogger("feeder.test").exception("failed")
        err = capsys.readouterr().err
        assert "feeder.test" in err and "INFO" in err
        assert TOKEN not in err and "***" in err
        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        import feeder.app as a

        logging.getLogger().removeHandler(a._handler)
        a._redactor._secrets.clear()


def test_redacting_filter_unit() -> None:
    f = RedactingFilter()
    f.add(["abc"])
    rec = logging.LogRecord("x", logging.INFO, "", 0, "tok %s", ("xabcx",), None)
    f.filter(rec)
    assert rec.getMessage() == "tok x***x"


async def test_cli_fake_no_telegram(config_path) -> None:
    ev = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, ev.set)
    code = await asyncio.wait_for(
        main(
            ["--config", str(config_path), "--env", "/nonexistent", "--fake", "--no-telegram"],
            stop_event=ev,
        ),
        timeout=10,
    )
    assert code == 0


async def test_cli_without_fake_returns_2(config_path, capsys) -> None:
    assert await main(["--config", str(config_path)]) == 2
    assert "M2" in capsys.readouterr().err
