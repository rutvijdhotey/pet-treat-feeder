"""Application wiring, logging and CLI (M1-12). Runs on a laptop with fake hardware."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from feeder.adapters.buttons import ButtonAdapter
from feeder.adapters.leds import LedController, PowerState
from feeder.adapters.scheduler import FeedScheduler
from feeder.adapters.telegram_bot import BotDeps, TelegramBot
from feeder.config import TOKEN_ENV, Config, ConfigError, load_config
from feeder.core.clock import ClockGuard
from feeder.core.feeder import Feeder, FeederState
from feeder.core.policy import DailyCapPolicy
from feeder.core.supply import SupplyMonitor
from feeder.hardware.fake import FakeHardware
from feeder.models import EventType, Source
from feeder.notify import MessageSender, Notifier, build_daily_summary
from feeder.ports import NowFn
from feeder.storage.db import Database

log = logging.getLogger(__name__)

BOT_RETRY_S = 30.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ logging


class RedactingFilter(logging.Filter):
    """Replaces any registered secret in log messages with '***'."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()

    def add(self, secrets: Iterable[str]) -> None:
        self._secrets.update(s for s in secrets if s)

    def redact(self, text: str) -> str:
        for s in self._secrets:
            if s in text:
                text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            try:
                message = record.getMessage()
            except Exception:
                return True
            redacted = self.redact(message)
            if redacted != message:
                record.msg = redacted
                record.args = None
        return True


class _RedactingFormatter(logging.Formatter):
    def __init__(self, redactor: RedactingFilter) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor.redact(super().format(record))


_redactor = RedactingFilter()
_handler: logging.Handler | None = None


def configure_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    """Idempotent: log to stderr, redact secrets, quiet noisy libraries."""
    global _handler
    _redactor.add(secrets)
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_RedactingFormatter(_redactor))
    handler.addFilter(_redactor)
    root.addHandler(handler)
    _handler = handler
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------- app


class FeederApp:
    def __init__(self) -> None:  # use FeederApp.build()
        self.config: Config
        self.hardware: Any
        self.db: Database
        self.now: NowFn
        self.policy: DailyCapPolicy
        self.clock: ClockGuard
        self.notifier: Notifier
        self.supply: SupplyMonitor
        self.feeder: Feeder
        self.led: LedController
        self.scheduler: FeedScheduler
        self.bot: TelegramBot | None
        self.buttons: ButtonAdapter
        self._tasks: list[asyncio.Task[Any]] = []
        self._started = False
        self._stopped = False

    @classmethod
    def build(
        cls,
        config: Config,
        hardware: Any,
        *,
        sender: MessageSender | None = None,
        now: NowFn | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: ClockGuard | None = None,
    ) -> FeederApp:
        self = cls()
        now = now or _utcnow
        self.config, self.hardware, self.now = config, hardware, now
        self.bot = None

        self.db = Database.open(config.db_path)
        self.policy = DailyCapPolicy(self.db, config.tz, config.treats.daily_cap, now)
        self.clock = clock or ClockGuard(now)

        async def _late_send(chat_id: int, text: str, silent: bool) -> None:
            if self.bot is None:
                raise RuntimeError("telegram bot not available")
            await self.bot.send(chat_id, text, silent)

        self.notifier = Notifier(
            sender or _late_send, config.telegram.allowed_user_ids, self.db, now
        )

        sleep_kw: dict[str, Any] = {"sleep": sleep} if sleep is not None else {}
        self.supply = SupplyMonitor(
            hardware.level_sensor,
            self.db,
            self.notifier,
            config.supply,
            config.treats.capacity,
            now,
            **sleep_kw,
        )

        async def _before_dispense() -> None:
            try:
                await self.supply.read()
            except Exception:
                log.exception("pre-dispense supply read failed")

        self.feeder = Feeder(
            hardware.motor,
            hardware.drop_sensor,
            self.policy,
            self.db,
            self.notifier,
            now,
            before_dispense=_before_dispense,
        )
        self.led = LedController(
            hardware.leds,
            config.leds.brightness,
            now,
            supply_pct=lambda: self.supply.status().pct,
            is_jammed=lambda: self.feeder.state is FeederState.EMPTY_OR_JAMMED,
            low_threshold_pct=config.supply.low_threshold_pct,
        )
        self.supply.add_refill_listener(self.feeder.clear_jam)
        self.supply.add_refill_listener(self._refresh_leds)
        self.feeder.on_state_change = lambda _state: self._refresh_leds()

        async def _daily_summary() -> None:
            text = build_daily_summary(self.policy, self.db, self.supply.status(), config.tz, now)
            await self.notifier.silent(text)

        self.scheduler = FeedScheduler(
            self.db,
            self.feeder,
            self.clock,
            self.notifier,
            config.tz,
            now,
            daily_summary=_daily_summary,
            summary_time=config.notifications.daily_summary,
            **sleep_kw,
        )

        if sender is None:
            deps = BotDeps(
                feeder=self.feeder,
                policy=self.policy,
                db=self.db,
                supply_status=self.supply.status,
                reload_schedules=self.scheduler.reload,
                tz=config.tz,
                now=now,
                started_at=now(),
                default_jitter_min=config.schedule.default_jitter_min,
                allowed_user_ids=frozenset(config.telegram.allowed_user_ids),
            )
            self.bot = TelegramBot(
                config.telegram_token, deps, on_connectivity=self._on_connectivity
            )

        self.buttons = ButtonAdapter(hardware.buttons, self.feeder, now=lambda: now().timestamp())
        return self

    # helpers ----------------------------------------------------------------

    def _refresh_leds(self) -> None:
        try:
            self.led.refresh()
        except Exception:
            log.exception("LED refresh failed")

    async def _on_connectivity(self, ok: bool) -> None:
        if ok:
            self.led.mark_poll_ok()
        else:
            self.led.mark_poll_failed()
        await self.notifier.set_connectivity(ok)

    async def _run_bot(self) -> None:
        """Start the bot, retrying while the network is unavailable."""
        bot = self.bot
        if bot is None:
            return
        while True:
            try:
                await bot.start()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram bot failed to start; will retry")
                with contextlib.suppress(Exception):
                    await self._on_connectivity(False)
                with contextlib.suppress(Exception):
                    await bot.stop()
                await asyncio.sleep(BOT_RETRY_S)

    def _set_power(self, state: PowerState) -> None:
        self.led.set_power(state)
        self._refresh_leds()

    def _event(self, type_: EventType) -> None:
        try:
            self.db.add_event(type_, Source.SYSTEM, ts=self.now())
        except Exception:
            log.exception("could not record %s event", type_)

    # lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        loop = asyncio.get_running_loop()
        self._set_power(PowerState.BOOTING)
        self._event(EventType.BOOT)
        self._tasks.append(asyncio.create_task(self.supply.run(), name="supply"))
        self._tasks.append(asyncio.create_task(self.led.run(), name="leds"))
        if self.bot is not None:
            self._tasks.append(asyncio.create_task(self._run_bot(), name="bot"))
        try:
            self.scheduler.start()
        except Exception:
            log.exception("scheduler failed to start; continuing without it")
        try:
            self.buttons.bind_loop(loop)
        except Exception:
            log.exception("button adapter failed to start; continuing without it")
        self._set_power(PowerState.RUNNING)
        log.info("feeder app started")

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._set_power(PowerState.SHUTTING_DOWN)
        self._event(EventType.SHUTDOWN)
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        with contextlib.suppress(Exception):
            self.scheduler.shutdown()
        if self.bot is not None:
            try:
                await self.bot.stop()
            except Exception:
                log.exception("bot stop failed")
        try:
            self.hardware.motor.release()
        except Exception:
            log.exception("motor release failed")
        self._set_power(PowerState.OFF)
        try:
            self.db.close()
        except Exception:
            log.exception("db close failed")
        log.info("feeder app stopped")


# ---------------------------------------------------------------------- CLI


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="feeder", description="Pet Treat Feeder")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")
    p.add_argument("--env", default=".env", help="path to .env holding the Telegram token")
    p.add_argument("--fake", action="store_true", help="use fake hardware (laptop mode)")
    p.add_argument(
        "--no-telegram", action="store_true", help="log notifications instead of sending"
    )
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


async def _log_sender(chat_id: int, text: str, silent: bool) -> None:
    log.info("[notify chat=%s %s] %s", chat_id, "silent" if silent else "push", text)


def _fake_hardware(config: Config) -> FakeHardware:
    hw = FakeHardware.create()
    empty, full = config.supply.empty_mm, config.supply.full_mm
    if empty is not None and full is not None:
        hw.level_sensor.set_distance(round(empty - 0.7 * (empty - full)))
    return hw


async def main(
    argv: Sequence[str] | None = None, *, stop_event: asyncio.Event | None = None
) -> int:
    args = _parse_args(argv)
    configure_logging(args.log_level)
    if not args.fake:
        print(
            "error: the Raspberry Pi hardware layer is not implemented yet (milestone M2); "
            "run with --fake.",
            file=sys.stderr,
        )
        return 2

    environ: dict[str, str] = dict(os.environ)
    if args.no_telegram and not environ.get(TOKEN_ENV):
        environ[TOKEN_ENV] = "no-telegram-placeholder"
    try:
        config = load_config(args.config, args.env, environ)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    configure_logging(args.log_level, secrets=[config.telegram_token])

    app = FeederApp.build(
        config,
        _fake_hardware(config),
        sender=_log_sender if args.no_telegram else None,
    )
    stop = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            log.debug("signal handler for %s unavailable on this platform", sig)
    try:
        await app.start()
        await stop.wait()
    finally:
        for sig in installed:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)
        await app.stop()
    return 0


def cli() -> None:
    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        code = 0
    raise SystemExit(code)
