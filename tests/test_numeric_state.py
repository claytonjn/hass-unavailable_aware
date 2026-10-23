"""Tests for `unavailable_aware.numeric_state`."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
import pytest
import voluptuous as vol

from .conftest import advance, attach, remove_state, set_state

COUNT = "sensor.count"


async def test_crossing_fires(hass: HomeAssistant) -> None:
    """Entering the range fires, matching Home Assistant."""
    await set_state(hass, COUNT, "2")
    rec = await attach(hass, "numeric_state", COUNT, below=1)

    await set_state(hass, COUNT, "1")
    assert rec.fired == []
    await set_state(hass, COUNT, "0")
    assert len(rec.fired) == 1
    assert rec.fired[0].payload["below"] == 1
    assert rec.fired[0].payload["above"] is None
    assert rec.fired[0].description == f"numeric state of {COUNT}"
    rec.detach()


async def test_template_reload_bounce(hass: HomeAssistant) -> None:
    """A template reload (0, removed, unavailable, 0) doesn't fire."""
    await set_state(hass, COUNT, "0")
    rec = await attach(hass, "numeric_state", COUNT, below=1)

    await remove_state(hass, COUNT)
    await set_state(hass, COUNT, "unavailable")
    await set_state(hass, COUNT, "0")

    assert rec.fired == []
    assert rec.reasons == ["bridged_gap"]
    rec.detach()


async def test_crossing_during_bridged_gap(hass: HomeAssistant) -> None:
    """Dropping below the threshold during a bridged gap (1, unavailable, 0) fires."""
    await set_state(hass, COUNT, "1")
    rec = await attach(hass, "numeric_state", COUNT, below=1)

    await set_state(hass, COUNT, "unavailable")
    await set_state(hass, COUNT, "0")

    assert len(rec.fired) == 1
    assert rec.fired[0].from_value == "1"
    assert rec.fired[0].payload["raw_from_state"].state == "unavailable"
    rec.detach()


async def test_already_in_range_judged_on_previous(hass: HomeAssistant) -> None:
    """With `above: 20`, going 20.1, gap, 20.3 doesn't fire: it was already above 20."""
    await set_state(hass, "sensor.temp", "20.1")
    rec = await attach(hass, "numeric_state", "sensor.temp", above=20)

    await set_state(hass, "sensor.temp", "unavailable")
    await set_state(hass, "sensor.temp", "20.3")

    assert rec.fired == []
    rec.detach()


async def test_out_of_range_previous_crossing(hass: HomeAssistant) -> None:
    """Out of range before the gap and in range after: the trigger fires."""
    await set_state(hass, "sensor.temp", "19.9")
    rec = await attach(hass, "numeric_state", "sensor.temp", above=20)

    await set_state(hass, "sensor.temp", "unknown")
    await set_state(hass, "sensor.temp", "20.3")

    assert len(rec.fired) == 1
    rec.detach()


async def test_leaving_range_into_gap(hass: HomeAssistant) -> None:
    """Going into a gap isn't leaving the range, so coming back in doesn't fire."""
    await set_state(hass, COUNT, "0")
    rec = await attach(hass, "numeric_state", COUNT, below=1)

    await set_state(hass, COUNT, "unavailable")
    await set_state(hass, COUNT, "unavailable")
    await set_state(hass, COUNT, "0")
    assert rec.fired == []

    # Leaving and re-entering the range afterwards still fires.
    await set_state(hass, COUNT, "1")
    await set_state(hass, COUNT, "0")
    assert len(rec.fired) == 1
    rec.detach()


async def test_attribute(hass: HomeAssistant) -> None:
    """A numeric attribute is compared with its value before the gap."""
    await set_state(hass, "sensor.score", "active", {"points": 3})
    rec = await attach(
        hass, "numeric_state", "sensor.score", attribute="points", above=5
    )

    await set_state(hass, "sensor.score", "unavailable")
    await set_state(hass, "sensor.score", "active", {"points": 10})

    assert len(rec.fired) == 1
    assert rec.fired[0].payload["from_state"].attributes["points"] == 3
    rec.detach()


async def test_threshold_entity(hass: HomeAssistant) -> None:
    """A threshold can come from another entity."""
    await set_state(hass, "input_number.limit", "5")
    await set_state(hass, "sensor.temp", "3")
    rec = await attach(hass, "numeric_state", "sensor.temp", above="input_number.limit")

    await set_state(hass, "sensor.temp", "unavailable")
    await set_state(hass, "sensor.temp", "6")

    assert len(rec.fired) == 1
    rec.detach()


async def test_non_numeric_is_out_of_range(hass: HomeAssistant) -> None:
    """A valid state that isn't a number is out of range, not a gap."""
    await set_state(hass, "sensor.temp", "error")
    rec = await attach(hass, "numeric_state", "sensor.temp", below=10)

    await set_state(hass, "sensor.temp", "5")
    assert len(rec.fired) == 1
    rec.detach()


async def test_longer_gap_ignored(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A crossing during a longer gap is ignored by default."""
    await set_state(hass, COUNT, "1")
    rec = await attach(hass, "numeric_state", COUNT, below=1)

    await set_state(hass, COUNT, "unavailable")
    await advance(hass, freezer, 60)
    await set_state(hass, COUNT, "0")

    assert rec.fired == []
    assert rec.reasons == ["longer_gap_ignored"]
    rec.detach()


async def test_value_template(hass: HomeAssistant) -> None:
    """`value_template` works across gaps."""
    await set_state(hass, "sensor.temp", "40")
    rec = await attach(
        hass,
        "numeric_state",
        "sensor.temp",
        below=10,
        value_template="{{ state.state | float - 25 }}",
    )

    await set_state(hass, "sensor.temp", "unavailable")
    await set_state(hass, "sensor.temp", "41")
    assert rec.fired == []
    await set_state(hass, "sensor.temp", "20")
    assert len(rec.fired) == 1
    rec.detach()


@pytest.mark.parametrize(
    "options",
    [{}, {"above": 10, "below": 5}, {"below": "light.kitchen"}],
)
async def test_invalid_options(hass: HomeAssistant, options: dict) -> None:
    """Invalid options are rejected."""
    with pytest.raises(vol.Invalid):
        await attach(hass, "numeric_state", COUNT, **options)


async def test_home_assistant_default(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """With `home_assistant_default`, coming back in range after a longer gap fires."""
    await set_state(hass, COUNT, "0")
    rec = await attach(
        hass, "numeric_state", COUNT, below=1, longer_gaps="home_assistant_default"
    )

    await set_state(hass, COUNT, "unavailable")
    await advance(hass, freezer, 60)
    await set_state(hass, COUNT, "0")

    assert len(rec.fired) == 1
    assert rec.fired[0].from_value == "unavailable"
    rec.detach()


# With home_assistant_default, a longer gap behaves like Home Assistant's own
# numeric_state trigger. That trigger ignores removals, so an entity added again
# is compared with the last state it had before the removal.


async def test_home_assistant_default_first_appearance(hass: HomeAssistant) -> None:
    """An entity with no earlier state doesn't fire when it first appears in range."""
    rec = await attach(
        hass,
        "numeric_state",
        "sensor.new",
        below=1,
        longer_gaps="home_assistant_default",
    )

    await set_state(hass, "sensor.new", "0")

    assert rec.fired == []
    rec.detach()


async def test_home_assistant_default_readded_from_in_range(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Removed while in range and added back in range: doesn't fire."""
    await set_state(hass, COUNT, "0")
    rec = await attach(
        hass, "numeric_state", COUNT, below=1, longer_gaps="home_assistant_default"
    )

    await remove_state(hass, COUNT)
    await advance(hass, freezer, 60)
    await set_state(hass, COUNT, "0")

    assert rec.fired == []
    rec.detach()


async def test_home_assistant_default_readded_from_out_of_range(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Removed while out of range and added back in range: fires."""
    await set_state(hass, COUNT, "2")
    rec = await attach(
        hass, "numeric_state", COUNT, below=1, longer_gaps="home_assistant_default"
    )

    await remove_state(hass, COUNT)
    await advance(hass, freezer, 60)
    await set_state(hass, COUNT, "0")

    assert len(rec.fired) == 1
    assert rec.fired[0].payload["from_state"] is None
    rec.detach()


async def test_home_assistant_default_readded_after_unavailable(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Unavailable before the removal counts as outside the range."""
    await set_state(hass, COUNT, "0")
    rec = await attach(
        hass, "numeric_state", COUNT, below=1, longer_gaps="home_assistant_default"
    )

    await set_state(hass, COUNT, "unavailable")
    await remove_state(hass, COUNT)
    await advance(hass, freezer, 60)
    await set_state(hass, COUNT, "0")

    assert len(rec.fired) == 1
    rec.detach()
