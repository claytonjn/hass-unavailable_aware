"""Tests for how `for:` waits behave across gaps."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from .conftest import advance, attach, set_state

FAN = "fan.ceiling"


async def test_for_fires_after_holding(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Without a gap, `for:` works as it does in Home Assistant."""
    await set_state(hass, FAN, "off")
    rec = await attach(hass, "state", FAN, to="on", **{"for": {"seconds": 60}})

    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 59)
    assert rec.fired == []
    await advance(hass, freezer, 2)
    assert len(rec.fired) == 1
    assert rec.fired[0].payload["for"].total_seconds() == 60
    rec.detach()


async def test_for_cancelled_by_change(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Changing to a value that doesn't match during `for:` cancels the wait."""
    await set_state(hass, FAN, "off")
    rec = await attach(hass, "state", FAN, to="on", **{"for": {"seconds": 60}})

    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 30)
    await set_state(hass, FAN, "off")
    await advance(hass, freezer, 60)
    assert rec.fired == []
    rec.detach()


async def test_for_held_through_bridged_gap(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A bridged gap that comes back unchanged doesn't restart the `for:` timer."""
    await set_state(hass, FAN, "off")
    rec = await attach(hass, "state", FAN, to="on", **{"for": {"seconds": 60}})

    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 20)
    await set_state(hass, FAN, "unavailable")
    await advance(hass, freezer, 5)
    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 36)

    assert len(rec.fired) == 1
    rec.detach()


async def test_for_runs_out_during_gap(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """If `for:` runs out during a bridged gap, it fires when the entity comes back."""
    await set_state(hass, FAN, "off")
    rec = await attach(hass, "state", FAN, to="on", **{"for": {"seconds": 60}})

    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 55)
    await set_state(hass, FAN, "unavailable")
    await advance(hass, freezer, 10)
    assert rec.fired == []
    await set_state(hass, FAN, "on")

    assert len(rec.fired) == 1
    rec.detach()


async def test_for_starts_over_after_longer_gap(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """After a longer gap, a pending wait starts over if the entity still matches."""
    await set_state(hass, FAN, "off")
    rec = await attach(hass, "state", FAN, to="on", **{"for": {"seconds": 60}})

    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 10)
    await set_state(hass, FAN, "unavailable")
    await advance(hass, freezer, 40)
    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 30)
    # 80 s since the fan turned on, but only 30 s since it came back.
    assert rec.fired == []
    await advance(hass, freezer, 31)

    assert len(rec.fired) == 1
    fired = rec.fired[0]
    assert fired.from_value == "off"
    assert fired.payload["gap"]["kind"] == "unavailable"
    rec.detach()


async def test_for_ends_after_longer_gap_changed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """After a longer gap, coming back with a non-matching value cancels the wait."""
    await set_state(hass, FAN, "off")
    rec = await attach(hass, "state", FAN, to="on", **{"for": {"seconds": 60}})

    await set_state(hass, FAN, "on")
    await set_state(hass, FAN, "unavailable")
    await advance(hass, freezer, 40)
    await set_state(hass, FAN, "off")
    await advance(hass, freezer, 120)

    assert rec.fired == []
    rec.detach()


async def test_no_wait_starts_after_ignored_longer_gap(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """With `ignore`, coming back after a longer gap doesn't start a wait."""
    await set_state(hass, FAN, "off")
    rec = await attach(hass, "state", FAN, to="on", **{"for": {"seconds": 60}})

    await set_state(hass, FAN, "unavailable")
    await advance(hass, freezer, 40)
    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 120)

    assert rec.fired == []
    rec.detach()


async def test_for_cancelled_by_change_inside_gap(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Coming back from a bridged gap with a non-matching value cancels the wait."""
    await set_state(hass, FAN, "off")
    rec = await attach(hass, "state", FAN, not_to="off", **{"for": {"seconds": 60}})

    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 10)
    await set_state(hass, FAN, "unavailable")
    await set_state(hass, FAN, "off")
    await advance(hass, freezer, 60)

    assert rec.fired == []
    rec.detach()


async def test_for_restarts_on_new_matching_value_across_gap(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Coming back from a bridged gap with another matching value starts a new wait."""
    await set_state(hass, "sensor.mode", "a")
    rec = await attach(
        hass, "state", "sensor.mode", not_to="a", **{"for": {"seconds": 60}}
    )

    await set_state(hass, "sensor.mode", "b")
    await advance(hass, freezer, 30)
    await set_state(hass, "sensor.mode", "unavailable")
    await set_state(hass, "sensor.mode", "c")
    await advance(hass, freezer, 40)
    assert rec.fired == []
    await advance(hass, freezer, 21)
    assert len(rec.fired) == 1
    assert rec.fired[0].to_value == "c"
    assert rec.fired[0].from_value == "b"
    rec.detach()


async def test_numeric_for_held_through_gap(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A bridged gap doesn't interrupt a `numeric_state` `for:` while in range."""
    await set_state(hass, "sensor.temp", "25")
    rec = await attach(
        hass, "numeric_state", "sensor.temp", below=20, **{"for": {"seconds": 60}}
    )

    await set_state(hass, "sensor.temp", "19")
    await advance(hass, freezer, 20)
    await set_state(hass, "sensor.temp", "unavailable")
    await set_state(hass, "sensor.temp", "18")
    await advance(hass, freezer, 41)

    assert len(rec.fired) == 1
    rec.detach()


async def test_raised_bridge_up_to_keeps_wait(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """With a higher `bridge_up_to`, a 90 s gap is bridged and `for:` still finishes."""
    await set_state(hass, FAN, "off")
    rec = await attach(
        hass,
        "state",
        FAN,
        to="on",
        bridge_up_to={"minutes": 2},
        **{"for": {"seconds": 60}},
    )

    await set_state(hass, FAN, "on")
    await set_state(hass, FAN, "unavailable")
    await advance(hass, freezer, 90)
    assert rec.fired == []
    await set_state(hass, FAN, "on")

    assert len(rec.fired) == 1
    rec.detach()


async def test_bridge_restarts_for_timer(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """With `bridge_restarts_for_timer`, a bridged gap restarts the `for:` timer."""
    await set_state(hass, FAN, "off")
    rec = await attach(
        hass,
        "state",
        FAN,
        to="on",
        bridge_restarts_for_timer=True,
        **{"for": {"seconds": 60}},
    )

    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 20)
    await set_state(hass, FAN, "unavailable")
    await advance(hass, freezer, 5)
    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 40)
    # 65 s since the fan first turned on, but only 40 s since it came back.
    assert rec.fired == []
    await advance(hass, freezer, 21)

    assert len(rec.fired) == 1
    fired = rec.fired[0]
    assert fired.from_value == "off"
    assert fired.payload["gap"]["kind"] == "unavailable"
    rec.detach()


async def test_bridge_restarts_for_timer_after_time_ran_out(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """With `bridge_restarts_for_timer`, `for:` ending during a gap doesn't count."""
    await set_state(hass, FAN, "off")
    rec = await attach(
        hass,
        "state",
        FAN,
        to="on",
        bridge_restarts_for_timer=True,
        **{"for": {"seconds": 60}},
    )

    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 55)
    await set_state(hass, FAN, "unavailable")
    await advance(hass, freezer, 10)
    await set_state(hass, FAN, "on")
    assert rec.fired == []
    await advance(hass, freezer, 61)

    assert len(rec.fired) == 1
    rec.detach()


async def test_home_assistant_default_drops_pending_wait(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """With `home_assistant_default`, the gap ends the wait.

    Coming back is then judged from `unavailable`, which doesn't match `from: off`.
    """
    await set_state(hass, FAN, "off")
    rec = await attach(
        hass,
        "state",
        FAN,
        **{"from": "off", "to": "on", "for": {"seconds": 60}},
        longer_gaps="home_assistant_default",
    )

    await set_state(hass, FAN, "on")
    await set_state(hass, FAN, "unavailable")
    await advance(hass, freezer, 90)
    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 120)

    assert rec.fired == []
    rec.detach()


async def test_ignore_keeps_pending_wait_judged_on_previous(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """With `ignore`, the wait starts over, still judged from `off`."""
    await set_state(hass, FAN, "off")
    rec = await attach(
        hass, "state", FAN, **{"from": "off", "to": "on", "for": {"seconds": 60}}
    )

    await set_state(hass, FAN, "on")
    await set_state(hass, FAN, "unavailable")
    await advance(hass, freezer, 90)
    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 30)
    assert rec.fired == []
    await advance(hass, freezer, 31)

    assert len(rec.fired) == 1
    assert rec.fired[0].from_value == "off"
    rec.detach()


async def test_home_assistant_default_starts_wait_without_pending_one(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """With `home_assistant_default`, coming back starts a new wait."""
    await set_state(hass, FAN, "on")
    rec = await attach(
        hass,
        "state",
        FAN,
        to="on",
        longer_gaps="home_assistant_default",
        **{"for": {"seconds": 60}},
    )

    await set_state(hass, FAN, "unavailable")
    await advance(hass, freezer, 90)
    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 61)

    assert len(rec.fired) == 1
    assert rec.fired[0].from_value == "unavailable"
    rec.detach()


async def test_longer_gap_ignores_bridge_restarts_for_timer(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A longer gap restarts the wait, even with `bridge_restarts_for_timer` off."""
    await set_state(hass, FAN, "off")
    rec = await attach(
        hass,
        "state",
        FAN,
        to="on",
        bridge_restarts_for_timer=False,
        **{"for": {"seconds": 60}},
    )

    await set_state(hass, FAN, "on")
    await set_state(hass, FAN, "unavailable")
    await advance(hass, freezer, 90)
    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 30)
    assert rec.fired == []
    await advance(hass, freezer, 31)

    assert len(rec.fired) == 1
    rec.detach()
