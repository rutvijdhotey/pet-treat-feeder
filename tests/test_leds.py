from __future__ import annotations

import itertools

import pytest

from feeder.adapters.leds import ConnState, LedController, PowerState, compute_led_states
from feeder.hardware.base import Led, LedMode
from feeder.hardware.fake import FakeStatusLeds

S, B, O = LedMode.SOLID, LedMode.BLINK, LedMode.OFF  # noqa: E741

POWER = {
    PowerState.RUNNING: S,
    PowerState.BOOTING: B,
    PowerState.SHUTTING_DOWN: B,
}
CONN = {ConnState.CONNECTED: S, ConnState.RECONNECTING: B, ConnState.OFFLINE: O}


@pytest.mark.parametrize(
    ("power", "conn", "pct", "jam"),
    list(itertools.product(PowerState, ConnState, [None, 5.0, 20.0, 20.1, 80.0], [False, True])),
)
def test_compute_table(power, conn, pct, jam):
    out = compute_led_states(power, conn, pct, jam, 20.0)
    if power is PowerState.OFF:
        assert out == {Led.POWER: O, Led.CONNECTED: O, Led.SUPPLY: O}
        return
    if jam:
        supply = B
    elif pct is not None and pct <= 20.0:
        supply = S
    else:
        supply = O
    assert out == {Led.POWER: POWER[power], Led.CONNECTED: CONN[conn], Led.SUPPLY: supply}


def test_jam_beats_low():
    assert (
        compute_led_states(PowerState.RUNNING, ConnState.CONNECTED, 1.0, True, 20)[Led.SUPPLY] == B
    )


def make(now, **kw):
    leds = FakeStatusLeds()
    state = {"pct": 50.0, "jam": False}
    sleeps: list[float] = []

    async def sleep(s):
        sleeps.append(s)

    c = LedController(
        leds,
        kw.pop("brightness", 0.5),
        now,
        supply_pct=lambda: state["pct"],
        is_jammed=lambda: state["jam"],
        low_threshold_pct=20.0,
        sleep=kw.pop("sleep", sleep),
    )
    return c, leds, state, sleeps


@pytest.mark.parametrize("b", [-0.1, 1.1])
def test_brightness_validation(now, b):
    with pytest.raises(ValueError):
        make(now, brightness=b)
    c, *_ = make(now)
    with pytest.raises(ValueError):
        c.set_brightness(b)


def test_conn_boundaries_after_ok(now):
    c, *_ = make(now)
    c.mark_poll_ok()
    now.advance(seconds=59)
    assert c.conn_state() is ConnState.CONNECTED
    now.advance(seconds=1)
    assert c.conn_state() is ConnState.RECONNECTING
    now.advance(seconds=240)  # exactly 5 min
    assert c.conn_state() is ConnState.RECONNECTING
    now.advance(seconds=1)
    assert c.conn_state() is ConnState.OFFLINE
    c.mark_poll_ok()
    assert c.conn_state() is ConnState.CONNECTED


def test_failure_only_records(now):
    c, *_ = make(now)
    c.mark_poll_ok()
    now.advance(seconds=30)
    c.mark_poll_failed()
    assert c.conn_state() is ConnState.CONNECTED
    now.advance(seconds=30)
    assert c.conn_state() is ConnState.RECONNECTING


def test_boot_without_poll(now):
    c, *_ = make(now)
    assert c.conn_state() is ConnState.RECONNECTING
    now.advance(seconds=300)
    assert c.conn_state() is ConnState.RECONNECTING
    now.advance(seconds=1)
    assert c.conn_state() is ConnState.OFFLINE


def test_first_refresh_applies_all_then_change_only(now):
    c, leds, state, _ = make(now)
    c.set_power(PowerState.RUNNING)
    c.refresh()
    assert len(leds.history) == 3
    assert leds.state[Led.POWER] == (S, 0.5)
    c.refresh()
    assert len(leds.history) == 3
    state["jam"] = True
    c.refresh()
    assert leds.history[3:] == [(Led.SUPPLY, B, 0.5)]
    c.mark_poll_ok()
    c.refresh()
    assert leds.history[4:] == [(Led.CONNECTED, S, 0.5)]


def test_set_brightness_reapplies(now):
    c, leds, *_ = make(now)
    c.refresh()
    n = len(leds.history)
    c.set_brightness(0.1)
    c.refresh()
    assert len(leds.history) == n + 3
    assert all(h[2] == 0.1 for h in leds.history[n:])


def test_power_transitions(now):
    c, leds, *_ = make(now)
    c.mark_poll_ok()
    c.refresh()
    assert leds.state[Led.POWER][0] is B
    c.set_power(PowerState.RUNNING)
    c.refresh()
    assert leds.state[Led.POWER][0] is S
    c.set_power(PowerState.SHUTTING_DOWN)
    c.refresh()
    assert leds.state[Led.POWER][0] is B
    c.set_power(PowerState.OFF)
    c.refresh()
    assert all(m is O for m, _ in leds.state.values())


async def test_run_loop_cancels_and_survives_errors(now):
    import asyncio

    calls = {"n": 0}

    async def sleep(s):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    c, leds, state, _ = make(now, sleep=sleep)
    boom = {"on": True}

    def pct():
        if boom["on"]:
            boom["on"] = False
            raise RuntimeError("x")
        return 50.0

    c._supply_pct = pct
    with pytest.raises(asyncio.CancelledError):
        await c.run(0.25)
    assert calls["n"] == 3
    assert len(leds.history) == 3  # first refresh failed, second applied
