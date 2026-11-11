from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import Any

import pytest

from feeder.core.clock import ClockGuard, default_ntp_synced, default_rtc_present


def _guard(now, ntp=False, rtc=False, **kw):
    return ClockGuard(now, ntp_synced=lambda: ntp, rtc_present=lambda: rtc, **kw)


def test_synced_is_trusted(now):
    g = _guard(now, ntp=True)
    assert g.is_trustworthy()
    assert g.reason() == "clock OK"


def test_rtc_only_is_trusted(now):
    g = _guard(now, rtc=True)
    assert g.is_trustworthy()
    assert g.reason() == "clock OK"


def test_neither_not_trusted(now):
    g = _guard(now)
    assert not g.is_trustworthy()
    assert g.reason() == "system clock not synced (no NTP, no RTC)"


def test_year_too_old_not_trusted(now):
    now.set(datetime(1970, 1, 1, tzinfo=UTC))
    g = _guard(now, ntp=True, rtc=True)
    assert not g.is_trustworthy()
    assert g.reason() == "clock reads 1970, before 2026"


def test_min_year_configurable(now):
    g = _guard(now, ntp=True, min_year=2030)
    assert not g.is_trustworthy()
    assert "2030" in g.reason()


def test_probe_exceptions_are_false(now):
    def boom() -> bool:
        raise RuntimeError("x")

    g = ClockGuard(now, ntp_synced=boom, rtc_present=boom)
    assert not g.is_trustworthy()
    g2 = ClockGuard(now, ntp_synced=boom, rtc_present=lambda: True)
    assert g2.is_trustworthy()


def test_probe_results_cached_30s(now):
    calls = {"n": 0}

    def ntp() -> bool:
        calls["n"] += 1
        return True

    g = ClockGuard(now, ntp_synced=ntp, rtc_present=lambda: False)
    g.is_trustworthy()
    g.is_trustworthy()
    g.reason()
    assert calls["n"] == 1
    now.advance(seconds=31)
    g.is_trustworthy()
    assert calls["n"] == 2


def test_cache_refreshes_when_clock_goes_backwards(now):
    calls = {"n": 0}

    def ntp() -> bool:
        calls["n"] += 1
        return True

    g = ClockGuard(now, ntp_synced=ntp, rtc_present=lambda: False)
    g.is_trustworthy()
    now.advance(seconds=-10)
    g.is_trustworthy()
    assert calls["n"] == 2


class _Proc:
    def __init__(self, out: str, rc: int = 0) -> None:
        self.stdout = out
        self.returncode = rc


def _runner(result: Any):
    def run(cmd, **kw):
        assert cmd[0] == "timedatectl"
        assert kw.get("timeout")
        if isinstance(result, Exception):
            raise result
        return result

    return run


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_Proc("yes\n"), True),
        (_Proc("no\n"), False),
        (_Proc("garbage"), False),
        (_Proc(""), False),
        (_Proc("yes\n", rc=1), False),
        (FileNotFoundError(), False),
        (subprocess.TimeoutExpired("timedatectl", 2), False),
        (OSError("nope"), False),
    ],
)
def test_default_ntp_synced(result, expected):
    assert default_ntp_synced(runner=_runner(result)) is expected


def test_default_rtc_present():
    seen = []

    def exists(p):
        seen.append(p)
        return True

    assert default_rtc_present(exists=exists) is True
    assert seen == ["/dev/rtc0"]
    assert default_rtc_present(exists=lambda p: False) is False

    def boom(p):
        raise OSError

    assert default_rtc_present(exists=boom) is False
