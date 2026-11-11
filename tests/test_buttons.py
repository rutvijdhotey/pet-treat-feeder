from __future__ import annotations

import asyncio
import threading

from feeder.adapters.buttons import ButtonAdapter
from feeder.hardware.fake import FakeButtons
from feeder.models import DispenseResult, DispenseStatus, Source


class StubFeeder:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[Source] = []
        self.gate: asyncio.Event | None = None
        self.fail = fail

    async def dispense(self, source: Source) -> DispenseResult:
        self.calls.append(source)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("boom")
        return DispenseResult(DispenseStatus.OK, source, 1, 1, 8)


async def _yield(n: int = 10) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


async def test_press_dispenses_with_button_source() -> None:
    btn, feeder, clk = FakeButtons(), StubFeeder(), Clock()
    ButtonAdapter(btn, feeder, asyncio.get_running_loop(), now=clk)  # type: ignore[arg-type]
    btn.press_treat()
    await _yield()
    assert feeder.calls == [Source.BUTTON]


async def test_debounce_and_inflight() -> None:
    btn, feeder, clk = FakeButtons(), StubFeeder(), Clock()
    feeder.gate = asyncio.Event()
    ButtonAdapter(btn, feeder, asyncio.get_running_loop(), now=clk)  # type: ignore[arg-type]
    btn.press_treat()
    await _yield()
    clk.t += 5
    btn.press_treat()  # in flight -> ignored
    await _yield()
    assert len(feeder.calls) == 1
    feeder.gate.set()
    await _yield()
    btn.press_treat()  # finished but < 0.5 s since... clock advanced 5 s -> accepted
    await _yield()
    assert len(feeder.calls) == 2
    btn.press_treat()  # same instant -> debounced
    await _yield()
    assert len(feeder.calls) == 2
    clk.t += 0.6
    btn.press_treat()
    await _yield()
    assert len(feeder.calls) == 3


async def test_press_from_other_thread() -> None:
    btn, feeder, clk = FakeButtons(), StubFeeder(), Clock()
    ButtonAdapter(btn, feeder, asyncio.get_running_loop(), now=clk)  # type: ignore[arg-type]
    th = threading.Thread(target=btn.press_treat)
    th.start()
    th.join()
    await _yield()
    assert feeder.calls == [Source.BUTTON]


async def test_never_raises_and_recovers_after_failure() -> None:
    btn, feeder, clk = FakeButtons(), StubFeeder(fail=True), Clock()
    ButtonAdapter(btn, feeder, asyncio.get_running_loop(), now=clk)  # type: ignore[arg-type]
    btn.press_treat()
    await _yield()
    clk.t += 1
    feeder.fail = False
    btn.press_treat()
    await _yield()
    assert len(feeder.calls) == 2


def test_press_without_loop_is_ignored() -> None:
    btn, feeder = FakeButtons(), StubFeeder()
    ButtonAdapter(btn, feeder)  # type: ignore[arg-type]
    btn.press_treat()  # must not raise
    assert feeder.calls == []
