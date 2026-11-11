from __future__ import annotations

import pytest

from feeder.hardware.base import Led, LedMode
from feeder.hardware.fake import FakeHardware


async def test_default_one_drop_per_advance() -> None:
    hw = FakeHardware.create()
    hw.drop_sensor.arm()
    await hw.motor.advance_pocket()
    assert hw.motor.advances == 1
    assert await hw.drop_sensor.wait_for_drop(1.0) == 1


async def test_no_drop_returns_zero() -> None:
    hw = FakeHardware.create()
    hw.drop_sensor.arm()
    assert await hw.drop_sensor.wait_for_drop(5.0) == 0


async def test_script_then_default() -> None:
    hw = FakeHardware.create()
    hw.drop_sensor.script(0, 0)
    results = []
    for _ in range(3):
        hw.drop_sensor.arm()
        await hw.motor.advance_pocket()
        results.append(await hw.drop_sensor.wait_for_drop(1.0))
    assert results == [0, 0, 1]


async def test_drops_accumulate_since_arm_and_default_change() -> None:
    hw = FakeHardware.create()
    hw.drop_sensor.set_default_drops(2)
    hw.drop_sensor.arm()
    await hw.motor.advance_pocket()
    await hw.motor.advance_pocket()
    assert await hw.drop_sensor.wait_for_drop(1.0) == 4
    hw.drop_sensor.arm()
    assert await hw.drop_sensor.wait_for_drop(1.0) == 0


async def test_motor_fail_next_and_release() -> None:
    hw = FakeHardware.create()
    hw.motor.fail_next = True
    with pytest.raises(RuntimeError):
        await hw.motor.advance_pocket()
    await hw.motor.advance_pocket()
    hw.motor.release()
    assert hw.motor.released == 1
    assert hw.motor.advances == 1


async def test_level_sensor_script_and_set() -> None:
    s = FakeHardware.create().level_sensor
    assert await s.read_distance_mm() is None
    s.set_distance(100)
    assert await s.read_distance_mm() == 100
    s.script(10, None, 30)
    assert [await s.read_distance_mm() for _ in range(5)] == [10, None, 30, 30, 30]
    assert s.reads == 7
    s.set_distance(50)
    assert await s.read_distance_mm() == 50


def test_leds_record_and_validate() -> None:
    leds = FakeHardware.create().leds
    leds.set(Led.POWER, LedMode.SOLID)
    leds.set(Led.SUPPLY, LedMode.BLINK, 0.5)
    assert leds.state[Led.POWER] == (LedMode.SOLID, 1.0)
    assert leds.state[Led.SUPPLY] == (LedMode.BLINK, 0.5)
    assert len(leds.history) == 2
    with pytest.raises(ValueError):
        leds.set(Led.POWER, LedMode.SOLID, 1.5)
    with pytest.raises(ValueError):
        leds.set(Led.POWER, LedMode.SOLID, -0.1)
    assert len(leds.history) == 2


def test_buttons() -> None:
    b = FakeHardware.create().buttons
    b.press_treat()  # no callback: no-op
    calls: list[int] = []
    b.on_treat_press(lambda: calls.append(1))
    b.press_treat()
    assert calls == [1]
