"""Telegram bot adapter: commands, inline buttons, allow-list, connectivity heartbeat."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
    filters,
)

from feeder.core.feeder import Feeder
from feeder.core.policy import DailyCapPolicy
from feeder.models import DispenseStatus, EventType, Source, SupplyStatus
from feeder.ports import NowFn
from feeder.storage.db import Database

log = logging.getLogger(__name__)

MAX_SCHEDULE_COUNT = 8
_TIME_ARG_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def read_wifi_signal(path: str = "/proc/net/wireless") -> str | None:
    """Return e.g. '-52 dBm' from /proc/net/wireless, or None on any failure."""
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()[2:]
        for line in lines:
            parts = line.split()
            if len(parts) >= 4:
                level = float(parts[3].rstrip("."))
                return f"{int(level)} dBm"
    except Exception:
        return None
    return None


@dataclass
class BotDeps:
    feeder: Feeder
    policy: DailyCapPolicy
    db: Database
    supply_status: Callable[[], SupplyStatus]
    reload_schedules: Callable[[], None]
    tz: ZoneInfo
    now: NowFn
    started_at: datetime
    default_jitter_min: int
    allowed_user_ids: frozenset[int] | set[int] | list[int] | tuple[int, ...] = field(
        default_factory=frozenset
    )
    wifi_signal: Callable[[], str | None] = read_wifi_signal


# ---------------------------------------------------------------- formatting


def _local_hhmm(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


def humanise(seconds: float) -> str:
    s = max(int(seconds), 0)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def help_text() -> str:
    return (
        "🐾 Pet Treat Feeder\n\n"
        "/treat - dispense a treat\n"
        "/status - hopper level, uptime, etc.\n"
        "/today - today's treats\n"
        "/last - last treat\n"
        "/schedule add HH:MM [count] - add a daily schedule\n"
        "/schedule list - list schedules\n"
        "/schedule remove <id> - remove a schedule\n"
        "/help - this message"
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🐟 Treat", callback_data="treat"),
                InlineKeyboardButton("📊 Status", callback_data="status"),
                InlineKeyboardButton("📅 Today", callback_data="today"),
            ]
        ]
    )


# ------------------------------------------------------------- command logic


async def handle_treat(deps: BotDeps) -> str:
    try:
        res = await deps.feeder.dispense(Source.TELEGRAM)
    except Exception:
        log.exception("dispense failed")
        return "Something went wrong 😕 - please try again."
    if res.status is DispenseStatus.OK:
        return f"Treat dispensed ✅ ({res.treats_today}/{res.cap} today)"
    if res.status is DispenseStatus.CAP_REACHED:
        return f"Daily limit reached ({res.treats_today}/{res.cap}) 🚫"
    if res.status is DispenseStatus.EMPTY_OR_JAMMED:
        return (
            "Feeder looks empty or jammed 🚨 - no treat dropped (not counted). "
            "Refill/check it, then try /treat again."
        )
    return "Feeder is busy, try again in a moment."


async def handle_status(deps: BotDeps) -> str:
    try:
        sup = deps.supply_status()
        now = deps.now()
        lines = []
        if sup.pct is None:
            lines.append("Hopper: not calibrated")
        else:
            lines.append(f"Hopper: {sup.pct:.0f}%{' ⚠️ low' if sup.low else ''}")
        if sup.treats_left is not None:
            lines.append(f"Treats left: ~{sup.treats_left}")
        if sup.days_left is not None:
            lines.append(f"Days left: ~{sup.days_left:.1f}")
        last = deps.db.last_event(EventType.DISPENSE)
        if last is None:
            lines.append("Last treat: none yet")
        else:
            src = last.source.icon if last.source else ""
            lines.append(f"Last treat: {_local_hhmm(last.ts, deps.tz)} {src}".rstrip())
        lines.append(f"Today: {deps.policy.treats_today()}/{deps.policy.cap}")
        lines.append(f"Feeder: {deps.feeder.state.value}")
        lines.append(f"Uptime: {humanise((now - deps.started_at).total_seconds())}")
        try:
            wifi = deps.wifi_signal()
        except Exception:
            wifi = None
        if wifi:
            lines.append(f"Wi-Fi: {wifi}")
        return "\n".join(lines)
    except Exception:
        log.exception("status failed")
        return "Something went wrong 😕"


async def handle_today(deps: BotDeps) -> str:
    try:
        start, end = deps.policy.day_bounds()
        events = deps.db.events_between(start, end, [EventType.DISPENSE])
        head = f"Today: {deps.policy.treats_today()}/{deps.policy.cap}"
        if not events:
            return f"{head}\nNo treats yet today"
        rows = [
            f"{_local_hhmm(e.ts, deps.tz)} {e.source.icon if e.source else ''}".rstrip()
            for e in events
        ]
        return head + "\n" + "\n".join(rows)
    except Exception:
        log.exception("today failed")
        return "Something went wrong 😕"


async def handle_last(deps: BotDeps) -> str:
    try:
        last = deps.db.last_event(EventType.DISPENSE)
        if last is None:
            return "No treats dispensed yet"
        ago = humanise((deps.now() - last.ts).total_seconds())
        src = last.source.value if last.source else "unknown"
        icon = last.source.icon if last.source else ""
        when = last.ts.astimezone(deps.tz).strftime("%a %H:%M")
        return f"Last treat: {when} ({ago} ago) {icon} {src}"
    except Exception:
        log.exception("last failed")
        return "Something went wrong 😕"


SCHEDULE_USAGE = (
    "Usage:\n/schedule add HH:MM [count]  (count 1-8)\n/schedule list\n/schedule remove <id>"
)


async def handle_schedule(deps: BotDeps, args: list[str], user_id: int | None) -> str:
    try:
        if not args:
            return SCHEDULE_USAGE
        sub = args[0].lower()
        if sub == "add":
            if len(args) not in (2, 3) or not _TIME_ARG_RE.match(args[1]):
                return "Invalid time or arguments (use 24h HH:MM, e.g. 08:00).\n" + SCHEDULE_USAGE
            count = 1
            if len(args) == 3:
                if not args[2].isdigit() or not 1 <= int(args[2]) <= MAX_SCHEDULE_COUNT:
                    return f"Count must be 1-{MAX_SCHEDULE_COUNT}.\n" + SCHEDULE_USAGE
                count = int(args[2])
            sched = deps.db.add_schedule(
                args[1], count, jitter_min=deps.default_jitter_min, created_by=user_id
            )
            deps.reload_schedules()
            return f"Schedule #{sched.id} added: {sched.time_local} x{sched.count} ✅"
        if sub == "list":
            items = deps.db.list_schedules()
            if not items:
                return "No schedules yet. Add one with /schedule add HH:MM [count]"
            return "\n".join(
                f"#{s.id} {s.time_local} x{s.count} {'enabled' if s.enabled else 'disabled'}"
                for s in items
            )
        if sub == "remove":
            if len(args) != 2 or not args[1].isdigit():
                return SCHEDULE_USAGE
            sid = int(args[1])
            if not deps.db.remove_schedule(sid):
                return f"No schedule with id {sid}."
            deps.reload_schedules()
            return f"Schedule #{sid} removed ✅"
        return SCHEDULE_USAGE
    except Exception:
        log.exception("schedule command failed")
        return "Something went wrong 😕"


# ------------------------------------------------------------------ handlers

_CALLBACK_LOGIC: dict[str, Callable[[BotDeps], Awaitable[str]]] = {
    "treat": handle_treat,
    "status": handle_status,
    "today": handle_today,
    "last": handle_last,
}


def _user_id(update: Update) -> int | None:
    user = update.effective_user
    return user.id if user is not None else None


def is_allowed(deps: BotDeps, update: Update) -> bool:
    uid = _user_id(update)
    return uid is not None and uid in set(deps.allowed_user_ids)


async def _reply(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(text, reply_markup=markup)


def make_command_handler(
    deps: BotDeps, logic: Callable[[BotDeps], Awaitable[str]], *, keyboard: bool = False
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_allowed(deps, update):
            return
        await _reply(update, await logic(deps), main_keyboard() if keyboard else None)

    return handler


def make_help_handler(deps: BotDeps):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_allowed(deps, update):
            return
        await _reply(update, help_text(), main_keyboard())

    return handler


def make_schedule_handler(deps: BotDeps):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_allowed(deps, update):
            return
        args = list(getattr(context, "args", None) or [])
        await _reply(update, await handle_schedule(deps, args, _user_id(update)))

    return handler


def make_callback_handler(deps: BotDeps):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_allowed(deps, update):
            return
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        logic = _CALLBACK_LOGIC.get(query.data or "")
        if logic is None:
            return
        text = await logic(deps)
        msg = query.message
        if msg is not None:
            await msg.reply_text(text)

    return handler


def make_unauthorized_handler(deps: BotDeps):
    """Catch-all (later group): records stray users; never replies or answers."""

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = _user_id(update)
        if uid is None or is_allowed(deps, update):
            return
        log.warning("unauthorized telegram user_id=%s", uid)
        try:
            deps.db.add_event(
                EventType.UNAUTHORIZED, Source.TELEGRAM, detail=f"user_id={uid}", ts=deps.now()
            )
        except Exception:
            log.exception("failed to record unauthorized event")

    return handler


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, NetworkError):
        log.warning("telegram network error: %s", err)
    else:
        log.error("telegram handler error", exc_info=err)


def build_application(token: str, deps: BotDeps) -> Application:
    app = Application.builder().token(token).build()
    allowed = filters.User(user_id=list(deps.allowed_user_ids))
    app.add_handler(CommandHandler(["start", "help"], make_help_handler(deps), filters=allowed))
    app.add_handler(
        CommandHandler("treat", make_command_handler(deps, handle_treat), filters=allowed)
    )
    app.add_handler(
        CommandHandler("status", make_command_handler(deps, handle_status), filters=allowed)
    )
    app.add_handler(
        CommandHandler("today", make_command_handler(deps, handle_today), filters=allowed)
    )
    app.add_handler(
        CommandHandler("last", make_command_handler(deps, handle_last), filters=allowed)
    )
    app.add_handler(CommandHandler("schedule", make_schedule_handler(deps), filters=allowed))
    app.add_handler(CallbackQueryHandler(make_callback_handler(deps)))
    app.add_handler(TypeHandler(Update, make_unauthorized_handler(deps)), group=1)
    app.add_error_handler(error_handler)
    return app


# ----------------------------------------------------------------- the bot


class TelegramBot:
    def __init__(
        self,
        token: str,
        deps: BotDeps,
        *,
        on_connectivity: Callable[[bool], Awaitable[None] | None] | None = None,
        heartbeat_s: float = 30.0,
        _application_factory: Callable[[str, BotDeps], Any] = build_application,
    ) -> None:
        self._token = token
        self._deps = deps
        self._on_connectivity = on_connectivity
        self._heartbeat_s = heartbeat_s
        self._factory = _application_factory
        self._app: Any = None
        self._hb_task: asyncio.Task[None] | None = None
        self._last_ok: bool | None = None
        self._polling = False

    async def start(self) -> None:
        app = self._factory(self._token, self._deps)
        self._app = app
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=False)
        self._polling = True
        self._hb_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        task, self._hb_task = self._hb_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        app, self._app = self._app, None
        if app is None:
            return
        for step in (
            app.updater.stop if self._polling else None,
            app.stop,
            app.shutdown,
        ):
            if step is None:
                continue
            try:
                await step()
            except Exception:
                log.exception("error while stopping telegram bot")
        self._polling = False

    async def send(self, chat_id: int, text: str, silent: bool) -> None:
        if self._app is None:
            raise RuntimeError("TelegramBot not started")
        await self._app.bot.send_message(chat_id, text, disable_notification=silent)

    async def _check_once(self) -> None:
        try:
            await self._app.bot.get_me()
            ok = True
        except Exception as exc:
            log.warning("telegram heartbeat failed: %s", exc)
            ok = False
        if ok != self._last_ok:
            self._last_ok = ok
            if self._on_connectivity is not None:
                try:
                    res = self._on_connectivity(ok)
                    if inspect.isawaitable(res):
                        await res
                except Exception:
                    log.exception("on_connectivity callback failed")

    async def _heartbeat_loop(self) -> None:
        while True:
            await self._check_once()
            await asyncio.sleep(self._heartbeat_s)
