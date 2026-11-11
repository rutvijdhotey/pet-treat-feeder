"""Clock trust guard (PRD S11, A7): refuse scheduled dispenses on an untrusted clock."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from typing import Any

from feeder.ports import NowFn

log = logging.getLogger(__name__)

_CACHE_TTL_S = 30.0
_RTC_PATH = "/dev/rtc0"


def default_ntp_synced(
    *, runner: Callable[..., Any] = subprocess.run, timeout_s: float = 2.0
) -> bool:
    """True only if `timedatectl` reports NTPSynchronized=yes. Never raises."""
    try:
        proc = runner(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            return False
        return str(proc.stdout).strip().lower() == "yes"
    except Exception:
        return False


def default_rtc_present(*, exists: Callable[[str], bool] = os.path.exists) -> bool:
    """Presence of /dev/rtc0 is treated as a trusted battery-backed RTC. Never raises."""
    try:
        return bool(exists(_RTC_PATH))
    except Exception:
        return False


class ClockGuard:
    def __init__(
        self,
        now: NowFn,
        *,
        ntp_synced: Callable[[], bool] | None = None,
        rtc_present: Callable[[], bool] | None = None,
        min_year: int = 2026,
    ) -> None:
        self._now = now
        self._ntp = ntp_synced or default_ntp_synced
        self._rtc = rtc_present or default_rtc_present
        self._min_year = min_year
        self._cached_at: float | None = None  # epoch seconds of last probe
        self._ntp_val = False
        self._rtc_val = False

    @staticmethod
    def _safe(probe: Callable[[], bool], name: str) -> bool:
        try:
            return bool(probe())
        except Exception:
            log.warning("clock probe %s failed", name, exc_info=True)
            return False

    def _probe(self) -> tuple[bool, bool]:
        t = self._now().timestamp()
        stale = self._cached_at is None or not (0 <= t - self._cached_at < _CACHE_TTL_S)
        if stale:
            self._ntp_val = self._safe(self._ntp, "ntp_synced")
            self._rtc_val = self._safe(self._rtc, "rtc_present")
            self._cached_at = t
        return self._ntp_val, self._rtc_val

    def is_trustworthy(self) -> bool:
        ntp, rtc = self._probe()
        return (ntp or rtc) and self._now().year >= self._min_year

    def reason(self) -> str:
        ntp, rtc = self._probe()
        if not (ntp or rtc):
            return "system clock not synced (no NTP, no RTC)"
        year = self._now().year
        if year < self._min_year:
            return f"clock reads {year}, before {self._min_year}"
        return "clock OK"
