"""Notifier (push / silent Telegram messages with offline queue) and the daily summary."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from feeder.core.policy import DailyCapPolicy
from feeder.models import EventType, Source, SupplyStatus
from feeder.ports import NowFn
from feeder.storage.db import Database

log = logging.getLogger(__name__)

# (chat_id, text, silent); raises on failure.
MessageSender = Callable[[int, str, bool], Awaitable[None]]


@dataclass(frozen=True)
class _Queued:
    text: str
    silent: bool
    chat_id: int
    ts: datetime


def _humanize(seconds: float) -> str:
    s = max(int(seconds), 0)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    h, m = divmod(s // 60, 60)
    return f"{h} h" if m == 0 else f"{h} h {m} min"


class Notifier:
    """Implements `Alerts`. Never raises into callers; queues while the network is down."""

    def __init__(
        self,
        send: MessageSender,
        chat_ids: Sequence[int],
        db: Database,
        now: NowFn,
        *,
        max_queue: int = 100,
        send_timeout_s: float = 15.0,
    ) -> None:
        self._send = send
        self._chat_ids = list(chat_ids)
        self._db = db
        self._now = now
        self._timeout = send_timeout_s
        self._queue: deque[_Queued] = deque(maxlen=max_queue)
        self._lock = asyncio.Lock()
        self._offline = False
        self._offline_since: datetime | None = None

    @property
    def online(self) -> bool:
        return not self._offline

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    # public API ---------------------------------------------------------

    async def push(self, text: str) -> None:
        await self._notify(text, silent=False)

    async def silent(self, text: str) -> None:
        await self._notify(text, silent=True)

    async def set_connectivity(self, ok: bool) -> None:
        try:
            async with self._lock:
                if ok:
                    if self._offline:
                        await self._recover()
                else:
                    self._mark_offline()
        except Exception:
            log.exception("set_connectivity failed")

    async def flush(self) -> None:
        """Try to deliver everything queued (and go back online if that succeeds)."""
        try:
            async with self._lock:
                if self._offline:
                    await self._recover()
        except Exception:
            log.exception("flush failed")

    # internals (call with the lock held) --------------------------------

    async def _notify(self, text: str, *, silent: bool) -> None:
        try:
            async with self._lock:
                ts = self._now()
                if self._offline and self._queue:
                    for chat in self._chat_ids:
                        self._enqueue(_Queued(text, silent, chat, ts))
                    await self._recover()
                    return
                was_offline = self._offline
                any_ok = False
                for chat in self._chat_ids:
                    if await self._try_send(chat, text, silent):
                        any_ok = True
                    else:
                        self._enqueue(_Queued(text, silent, chat, ts))
                        self._mark_offline()
                if any_ok and was_offline and self._offline:
                    await self._recover()
        except Exception:
            log.exception("notify failed")

    async def _try_send(self, chat: int, text: str, silent: bool) -> bool:
        try:
            await asyncio.wait_for(self._send(chat, text, silent), self._timeout)
            return True
        except Exception as exc:
            log.warning("send to chat %s failed: %r", chat, exc)
            return False

    def _enqueue(self, item: _Queued) -> None:
        if self._queue.maxlen is not None and len(self._queue) >= self._queue.maxlen:
            log.warning("notify queue full (%d); dropping oldest message", self._queue.maxlen)
        self._queue.append(item)

    def _mark_offline(self) -> None:
        if self._offline:
            return
        self._offline = True
        self._offline_since = self._now()
        log.warning("notifier offline")
        self._add_event(EventType.OFFLINE, None)

    def _add_event(self, type_: EventType, detail: str | None) -> None:
        try:
            self._db.add_event(type_, Source.SYSTEM, detail=detail, ts=self._now())
        except Exception:
            log.exception("could not record %s event", type_)

    async def _flush_queue(self) -> bool:
        while self._queue:
            item = self._queue.popleft()
            stamp = item.ts.astimezone(UTC).strftime("%H:%M")
            if not await self._try_send(
                item.chat_id, f"(queued {stamp} UTC) {item.text}", item.silent
            ):
                self._queue.appendleft(item)
                return False
        return True

    async def _recover(self) -> None:
        """Flush queued messages in order; on full success go online and announce it."""
        if not await self._flush_queue():
            return
        since = self._offline_since or self._now()
        down = (self._now() - since).total_seconds()
        self._offline = False
        self._offline_since = None
        log.info("notifier back online after %.0f s", down)
        self._add_event(EventType.ONLINE, str(int(down)))
        text = f"✅ Back online (down for {_humanize(down)})"
        ts = self._now()
        for chat in self._chat_ids:
            if not await self._try_send(chat, text, False):
                self._enqueue(_Queued(text, False, chat, ts))
                self._mark_offline()


def build_daily_summary(
    policy: DailyCapPolicy,
    db: Database,
    supply: SupplyStatus,
    tz: ZoneInfo,
    now: NowFn,
) -> str:
    start, end = policy.day_bounds(now())
    events = db.events_between(start, end)
    dispenses = [e for e in events if e.type is EventType.DISPENSE]
    total = sum(e.treats for e in dispenses)
    lines = ["📅 Daily summary"]

    treat_line = f"🐟 {total}/{policy.cap} treats today"
    by_source: dict[Source, int] = {}
    for e in dispenses:
        if e.source is not None:
            by_source[e.source] = by_source.get(e.source, 0) + e.treats
    parts = [f"{src.icon} {n}" for src, n in by_source.items() if n > 0]
    if parts:
        treat_line += f" ({' · '.join(parts)})"
    lines.append(treat_line)
    if dispenses:
        last = max(dispenses, key=lambda e: (e.ts, e.id))
        lines.append(f"🕒 Last treat {last.ts.astimezone(tz).strftime('%H:%M')}")

    if supply.pct is None:
        lines.append("🥣 Hopper level unknown")
    else:
        hopper = f"🥣 Hopper {supply.pct:.0f}%"
        extras = []
        if supply.treats_left is not None:
            extras.append(f"~{supply.treats_left} treats")
        if supply.days_left is not None:
            n = round(supply.days_left)
            extras.append(f"~{n} {'day' if n == 1 else 'days'} left")
        if extras:
            hopper += f" ({', '.join(extras)})"
        lines.append(hopper)

    labels = [
        (EventType.SCHEDULE_SKIPPED, "skipped"),
        (EventType.JAM, "jam"),
        (EventType.CAP_REACHED, "cap reached"),
        (EventType.REFILL, "refill"),
    ]
    flags = []
    for etype, label in labels:
        n = sum(1 for e in events if e.type is etype)
        if n:
            flags.append(f"{n} {label}")
    if flags:
        lines.append("⚠️ " + " · ".join(flags))
    return "\n".join(lines)
