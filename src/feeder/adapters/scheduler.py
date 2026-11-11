"""Feed scheduler (PRD S2/S9): cron slots from the DB, jitter, no catch-up."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from feeder.core.clock import ClockGuard
from feeder.core.feeder import Feeder
from feeder.models import DispenseStatus, EventType, Schedule, Source
from feeder.ports import Alerts, NowFn
from feeder.storage.db import Database

log = logging.getLogger(__name__)

SLOT_PREFIX = "slot-"
SUMMARY_JOB_ID = "daily-summary"
_DAY_MIN = 24 * 60


class FeedScheduler:
    def __init__(
        self,
        db: Database,
        feeder: Feeder,
        clock: ClockGuard,
        alerts: Alerts,
        tz: ZoneInfo,
        now: NowFn,
        *,
        daily_summary: Callable[[], Awaitable[None]] | None = None,
        summary_time: time | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._db = db
        self._feeder = feeder
        self._clock = clock
        self._alerts = alerts
        self._tz = tz
        self._now = now
        self._daily_summary = daily_summary
        self._summary_time = summary_time
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._sched = AsyncIOScheduler(timezone=tz)
        self._started = False

    # lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._sched.configure(event_loop=asyncio.get_running_loop())
        self.reload()
        self._sched.start()
        self._started = True

    def shutdown(self) -> None:
        if not self._started:
            return
        self._started = False
        try:
            self._sched.shutdown(wait=False)
        except Exception:
            log.exception("scheduler shutdown failed")

    # jobs -----------------------------------------------------------------

    def reload(self) -> None:
        """Rebuild all jobs from enabled DB schedules (+ summary job). Idempotent."""
        for job in self._sched.get_jobs():
            if job.id == SUMMARY_JOB_ID or job.id.startswith(SLOT_PREFIX):
                job.remove()
        for sched in self._db.list_schedules(enabled_only=True):
            try:
                self._add_slot_job(sched)
            except Exception:
                log.exception("could not add job for schedule %s", sched)
        if self._daily_summary is not None and self._summary_time is not None:
            self._sched.add_job(
                self._run_summary,
                CronTrigger(
                    hour=self._summary_time.hour,
                    minute=self._summary_time.minute,
                    timezone=self._tz,
                ),
                id=SUMMARY_JOB_ID,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=60,
                replace_existing=True,
            )

    def _add_slot_job(self, sched: Schedule) -> None:
        hh, mm = (int(p) for p in sched.time_local.split(":"))
        jitter = max(0, sched.jitter_min)
        total = (hh * 60 + mm - jitter) % _DAY_MIN
        self._sched.add_job(
            self._run_slot_job,
            CronTrigger(hour=total // 60, minute=total % 60, timezone=self._tz),
            args=[sched],
            id=f"{SLOT_PREFIX}{sched.id}",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
            replace_existing=True,
        )

    def next_run_times(self) -> dict[str, datetime]:
        out: dict[str, datetime] = {}
        for job in self._sched.get_jobs():
            nrt = getattr(job, "next_run_time", None)
            if nrt is not None:
                out[job.id] = nrt
        return out

    async def _run_slot_job(self, schedule: Schedule) -> None:
        try:
            if schedule.jitter_min > 0:
                delay_min = self._rng.uniform(0, 2 * schedule.jitter_min)
                await self._sleep(delay_min * 60)
        except Exception:
            log.exception("jitter sleep failed for slot %s", schedule.id)
        await self.run_slot(schedule)

    async def _run_summary(self) -> None:
        try:
            if self._daily_summary is not None:
                await self._daily_summary()
        except Exception:
            log.exception("daily summary failed")

    async def run_slot(self, schedule: Schedule) -> None:
        """Job body. Never raises."""
        try:
            await self._run_slot(schedule)
        except Exception:
            log.exception("scheduled slot %s crashed", schedule.id)

    async def _run_slot(self, schedule: Schedule) -> None:
        hhmm = schedule.time_local
        if not self._clock.is_trustworthy():
            reason = self._clock.reason()
            self._db.add_event(
                EventType.SCHEDULE_SKIPPED, Source.SCHEDULE, detail=reason, ts=self._now()
            )
            await self._alerts.push(f"⏭️ Scheduled treat at {hhmm} skipped: {reason}")
            return
        for done in range(schedule.count):
            result = await self._feeder.dispense(Source.SCHEDULE)
            if result.ok:
                continue
            if result.status is DispenseStatus.CAP_REACHED:
                why = "daily cap reached"
            else:
                why = "feeder empty or jammed"
            self._db.add_event(
                EventType.SCHEDULE_SKIPPED,
                Source.SCHEDULE,
                detail=(f"{hhmm}: {result.status.value}; dispensed {done} of {schedule.count}"),
                ts=self._now(),
            )
            await self._alerts.push(f"⏭️ Scheduled treat at {hhmm} skipped: {why}")
            return
