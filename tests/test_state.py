"""Tests for `unavailable_aware.state`."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
import pytest
import voluptuous as vol

from .conftest import advance, attach, remove_state, set_state

THERMOSTAT = "climate.thermostat"
WINDOW = "binary_sensor.window"


async def test_plain_change_fires(hass: HomeAssistant) -> None:
    """An ordinary change works like Home Assistant's own trigger."""
    await set_state(hass, WINDOW, "off")
    rec = await attach(hass, "state", WINDOW, to="on")

    await set_state(hass, WINDOW, "on")

    assert len(rec.fired) == 1
    fired = rec.fired[0]
    assert fired.from_value == "off"
    assert fired.to_value == "on"
    assert fired.payload["raw_from_state"].state == "off"
    assert fired.payload["gap"] is None
    assert fired.description == f"state of {WINDOW}"
    rec.detach()


@pytest.mark.parametrize("gap", ["unavailable", "unknown"])
async def test_bounce_does_not_fire(hass: HomeAssistant, gap: str) -> None:
    """A bounce doesn't fire."""
    await set_state(hass, WINDOW, "on")
    rec = await attach(hass, "state", WINDOW, to="on")

    await set_state(hass, WINDOW, gap)
    await set_state(hass, WINDOW, "on")

    assert rec.fired == []
    # Home Assistant's own trigger would have fired, so the trace says why
    # this one didn't.
    assert rec.reasons == ["bridged_gap"]
    assert rec.reports[0].data["gap"]["kind"] == gap
    rec.detach()


async def test_removal_bounce_does_not_fire(hass: HomeAssistant) -> None:
    """A template reload that adds the entity back unchanged doesn't fire."""
    await set_state(hass, "sensor.count", "0")
    rec = await attach(hass, "state", "sensor.count", to="0")

    await remove_state(hass, "sensor.count")
    await set_state(hass, "sensor.count", "0")

    assert rec.fired == []
    assert rec.reports[0].data["gap"]["kind"] == "removed"
    rec.detach()


async def test_real_change_inside_bridged_gap_fires(hass: HomeAssistant) -> None:
    """A change that happens during a bridged gap still fires."""
    await set_state(hass, WINDOW, "on")
    rec = await attach(hass, "state", WINDOW, **{"from": "on", "to": "off"})

    await set_state(hass, WINDOW, "unavailable")
    assert rec.fired == []
    await set_state(hass, WINDOW, "off")

    assert len(rec.fired) == 1
    fired = rec.fired[0]
    assert fired.from_value == "on"
    assert fired.payload["raw_from_state"].state == "unavailable"
    gap = fired.payload["gap"]
    assert gap["kind"] == "unavailable"
    assert gap["duration"] >= 0
    rec.detach()


async def test_entering_gap_is_not_leaving(hass: HomeAssistant) -> None:
    """With `from: on`, going from on to unavailable doesn't fire."""
    await set_state(hass, WINDOW, "on")
    rec = await attach(hass, "state", WINDOW, **{"from": "on"})

    await set_state(hass, WINDOW, "unavailable")
    assert rec.fired == []
    # Each gap gets one trace, written when the entity comes back.
    assert rec.reasons == []

    await set_state(hass, WINDOW, "on")
    assert rec.fired == []
    assert rec.reasons == ["entered_gap"]
    assert rec.reports[0].data["entered_gap_suppressed"] is True
    rec.detach()


async def test_one_report_per_gap(hass: HomeAssistant) -> None:
    """Without `from` or `to`, a bounce records one trace, not two."""
    await set_state(hass, WINDOW, "on")
    rec = await attach(hass, "state", WINDOW)

    await set_state(hass, WINDOW, "unavailable")
    await set_state(hass, WINDOW, "on")

    assert rec.reasons == ["bridged_gap"]
    assert rec.reports[0].data["entered_gap_suppressed"] is True
    rec.detach()


async def test_no_report_when_return_fires(hass: HomeAssistant) -> None:
    """When coming back fires, no not-triggered trace is recorded."""
    await set_state(hass, WINDOW, "on")
    rec = await attach(hass, "state", WINDOW, **{"from": "on"})

    await set_state(hass, WINDOW, "unavailable")
    await set_state(hass, WINDOW, "off")

    assert len(rec.fired) == 1
    assert rec.reports == []
    rec.detach()


async def test_gap_chain_is_one_gap(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Unavailable, then unknown, then a value is one gap, starting at unavailable."""
    await set_state(hass, WINDOW, "on")
    rec = await attach(hass, "state", WINDOW, to="off")

    await set_state(hass, WINDOW, "unavailable")
    await advance(hass, freezer, 10)
    await set_state(hass, WINDOW, "unknown")
    await advance(hass, freezer, 10)
    await set_state(hass, WINDOW, "off")

    assert len(rec.fired) == 1
    gap = rec.fired[0].payload["gap"]
    assert gap["kind"] == "unavailable"
    assert gap["duration"] == pytest.approx(20, abs=0.5)
    assert rec.fired[0].from_value == "on"
    rec.detach()


async def test_longer_gap_ignored_by_default(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A longer gap is ignored by default: the change could have been at any time."""
    await set_state(hass, WINDOW, "on")
    rec = await attach(hass, "state", WINDOW, to="off")

    await set_state(hass, WINDOW, "unavailable")
    await advance(hass, freezer, 31)
    await set_state(hass, WINDOW, "off")

    assert rec.fired == []
    assert rec.reasons == ["longer_gap_ignored"]
    assert rec.reports[0].data["gap"]["bridge_up_to"] == 30
    rec.detach()


async def test_raised_bridge_up_to_still_suppresses_bounce(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A bounce after a long gap within a higher `bridge_up_to` still doesn't fire."""
    await set_state(hass, WINDOW, "off")
    rec = await attach(hass, "state", WINDOW, to="off", bridge_up_to={"minutes": 5})

    await set_state(hass, WINDOW, "unavailable")
    await advance(hass, freezer, 120)
    await set_state(hass, WINDOW, "off")

    assert rec.fired == []
    assert rec.reasons == ["bridged_gap"]
    rec.detach()


async def test_bridge_up_to_option(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Each trigger sets its own `bridge_up_to`."""
    await set_state(hass, WINDOW, "on")
    rec = await attach(hass, "state", WINDOW, to="off", bridge_up_to={"minutes": 5})

    await set_state(hass, WINDOW, "unavailable")
    await advance(hass, freezer, 240)
    await set_state(hass, WINDOW, "off")

    assert len(rec.fired) == 1
    assert rec.fired[0].from_value == "on"
    rec.detach()


async def test_round_trip_inside_gap_is_invisible(hass: HomeAssistant) -> None:
    """A change and change back during a gap can't be seen, so nothing fires."""
    await set_state(hass, WINDOW, "off")
    rec = await attach(hass, "state", WINDOW)

    await set_state(hass, WINDOW, "unavailable")
    await set_state(hass, WINDOW, "off")

    assert rec.fired == []
    rec.detach()


async def test_missing_attribute_is_a_value(hass: HomeAssistant) -> None:
    """A missing attribute is a normal value, as in Home Assistant.

    A light's brightness is None whenever it's off, and that isn't a gap.
    """
    await set_state(hass, "light.lamp", "on", {"brightness": 128})
    rec = await attach(hass, "state", "light.lamp", attribute="brightness")

    await set_state(hass, "light.lamp", "off", {"brightness": None})
    await set_state(hass, "light.lamp", "on", {"brightness": 128})

    assert len(rec.fired) == 2
    assert rec.fired[0].payload["to_state"].attributes["brightness"] is None
    assert rec.fired[1].payload["from_state"].attributes["brightness"] is None
    assert rec.fired[1].payload["gap"] is None
    assert rec.fired[1].payload["attribute"] == "brightness"
    rec.detach()


async def test_attribute_trigger_through_unavailable(hass: HomeAssistant) -> None:
    """A reload that reports the same attribute value again doesn't fire."""
    await set_state(hass, THERMOSTAT, "heat", {"hvac_action": "heating"})
    rec = await attach(hass, "state", THERMOSTAT, attribute="hvac_action", to="heating")

    await remove_state(hass, THERMOSTAT)
    await set_state(hass, THERMOSTAT, "heat", {"hvac_action": "heating"})

    assert rec.fired == []
    assert rec.reasons == ["bridged_gap"]
    rec.detach()


async def test_attribute_change_across_same_state_gap(hass: HomeAssistant) -> None:
    """Coming back with the same state but a changed followed attribute fires."""
    await set_state(hass, "sensor.score", "active", {"points": 3})
    rec = await attach(hass, "state", "sensor.score", attribute="points")

    await set_state(hass, "sensor.score", "unavailable", {})
    await set_state(hass, "sensor.score", "active", {"points": 10})

    assert len(rec.fired) == 1
    fired = rec.fired[0]
    assert fired.payload["from_state"].attributes["points"] == 3
    assert fired.payload["to_state"].attributes["points"] == 10
    rec.detach()


async def test_collapsed_sequence_fires_once(hass: HomeAssistant) -> None:
    """A score going 3, gap, 10 fires once, from 3 to 10."""
    await set_state(hass, "sensor.score", "active", {"points": 3})
    rec = await attach(hass, "state", "sensor.score", attribute="points")

    await set_state(hass, "sensor.score", "unavailable")
    await set_state(hass, "sensor.score", "active", {"points": 10})

    assert len(rec.fired) == 1
    rec.detach()


async def test_state_trigger_ignores_attribute_shuffle_across_gap(
    hass: HomeAssistant,
) -> None:
    """Without `from` or `to`, attributes that change while reconnecting don't fire."""
    await set_state(hass, WINDOW, "on", {"rssi": -60})
    rec = await attach(hass, "state", WINDOW)

    await set_state(hass, WINDOW, "unavailable")
    await set_state(hass, WINDOW, "on", {"rssi": -70})
    assert rec.fired == []

    # Outside a gap it behaves as Home Assistant does: attribute changes fire.
    await set_state(hass, WINDOW, "on", {"rssi": -80})
    assert len(rec.fired) == 1
    rec.detach()


async def test_final_state_across_gap(hass: HomeAssistant) -> None:
    """Going running, gap, finished still fires for the change to finished."""
    await set_state(hass, "sensor.cycle", "running")
    rec = await attach(
        hass, "state", "sensor.cycle", **{"from": "running", "to": "finished"}
    )

    await set_state(hass, "sensor.cycle", "unavailable")
    await set_state(hass, "sensor.cycle", "finished")

    assert len(rec.fired) == 1
    rec.detach()


async def test_event_entity(hass: HomeAssistant) -> None:
    """Event entities: the same timestamp after a gap doesn't fire; a new one does."""
    stamp1 = "2026-01-01T10:00:00.000+00:00"
    stamp2 = "2026-01-01T10:00:05.000+00:00"
    remote = "event.button"
    await set_state(hass, remote, stamp1, {"event_type": "single"})
    rec = await attach(hass, "state", remote)

    await set_state(hass, remote, "unavailable")
    await set_state(hass, remote, stamp1, {"event_type": "single"})
    assert rec.fired == []

    await set_state(hass, remote, "unavailable")
    await set_state(hass, remote, stamp2, {"event_type": "single"})
    assert len(rec.fired) == 1
    rec.detach()


async def test_multiple_entities(hass: HomeAssistant) -> None:
    """Each targeted entity is tracked separately."""
    await set_state(hass, "binary_sensor.a", "off")
    await set_state(hass, "binary_sensor.b", "off")
    rec = await attach(hass, "state", ["binary_sensor.a", "binary_sensor.b"], to="on")

    await set_state(hass, "binary_sensor.a", "unavailable")
    await set_state(hass, "binary_sensor.b", "on")
    await set_state(hass, "binary_sensor.a", "off")

    assert [f.payload["entity_id"] for f in rec.fired] == ["binary_sensor.b"]
    rec.detach()


async def test_entity_that_does_not_exist_yet(hass: HomeAssistant) -> None:
    """An entity appearing for the first time has no earlier state to compare with."""
    rec = await attach(hass, "state", "sensor.new", to="1")

    await set_state(hass, "sensor.new", "1")

    # With `longer_gaps: ignore`, that means it doesn't fire.
    assert rec.fired == []
    assert rec.reasons == ["previous_unknown"]
    rec.detach()


async def test_detach_stops_firing(hass: HomeAssistant) -> None:
    """Nothing fires after detaching."""
    await set_state(hass, WINDOW, "off")
    rec = await attach(hass, "state", WINDOW, to="on")
    rec.detach()

    await set_state(hass, WINDOW, "on")
    assert rec.fired == []


@pytest.mark.parametrize(
    "options",
    [
        {"to": "unavailable"},
        {"from": ["on", "unknown"]},
        {"from": "on", "not_from": "off"},
        {"to": "on", "not_to": "off"},
        {"longer_gaps": "compare"},
        {"bridge_up_to": "soon"},
        {"attribute_gap": True},
        {"settle": {"seconds": 5}},
    ],
)
async def test_invalid_options(hass: HomeAssistant, options: dict) -> None:
    """Invalid options are rejected."""
    with pytest.raises(vol.Invalid):
        await attach(hass, "state", WINDOW, **options)


async def test_attribute_can_match_unknown(hass: HomeAssistant) -> None:
    """When following an attribute, `unknown` is a normal value, not a gap."""
    await set_state(hass, THERMOSTAT, "heat", {"hvac_action": "idle"})
    rec = await attach(hass, "state", THERMOSTAT, attribute="hvac_action", to="unknown")

    await set_state(hass, THERMOSTAT, "heat", {"hvac_action": "unknown"})
    assert len(rec.fired) == 1
    rec.detach()


async def test_not_from_unavailable_is_harmless(hass: HomeAssistant) -> None:
    """A converted trigger can keep `not_from: [unavailable, unknown]`."""
    await set_state(hass, WINDOW, "on")
    rec = await attach(
        hass, "state", WINDOW, to="off", not_from=["unavailable", "unknown"]
    )

    await set_state(hass, WINDOW, "unavailable")
    await set_state(hass, WINDOW, "off")

    assert len(rec.fired) == 1
    rec.detach()


async def test_empty_target_rejected(hass: HomeAssistant) -> None:
    """An empty target is rejected."""
    with pytest.raises(vol.Invalid):
        await attach(hass, "state", target={}, to="on")


# longer_gaps: home_assistant_default

HA_DEFAULT = "home_assistant_default"


async def test_home_assistant_default_fires_from_raw_state(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """After a longer gap, coming back is compared with `unavailable`."""
    await set_state(hass, WINDOW, "on")
    rec = await attach(hass, "state", WINDOW, to="off", longer_gaps=HA_DEFAULT)

    await set_state(hass, WINDOW, "unavailable")
    await advance(hass, freezer, 60)
    await set_state(hass, WINDOW, "off")

    assert len(rec.fired) == 1
    fired = rec.fired[0]
    assert fired.from_value == "unavailable"
    assert fired.payload["raw_from_state"].state == "unavailable"
    assert fired.payload["gap"]["kind"] == "unavailable"
    assert rec.reports == []
    rec.detach()


async def test_home_assistant_default_bounce_fires(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The downside: a bounce after a longer gap fires too."""
    await set_state(hass, WINDOW, "on")
    rec = await attach(hass, "state", WINDOW, to="on", longer_gaps=HA_DEFAULT)

    await set_state(hass, WINDOW, "unavailable")
    await advance(hass, freezer, 60)
    await set_state(hass, WINDOW, "on")

    assert len(rec.fired) == 1
    rec.detach()


async def test_home_assistant_default_still_bridges_short_gaps(
    hass: HomeAssistant,
) -> None:
    """Gaps within `bridge_up_to` are bridged as usual."""
    await set_state(hass, WINDOW, "on")
    rec = await attach(hass, "state", WINDOW, to="on", longer_gaps=HA_DEFAULT)

    await set_state(hass, WINDOW, "unavailable")
    await set_state(hass, WINDOW, "on")

    assert rec.fired == []
    assert rec.reasons == ["bridged_gap"]
    rec.detach()


async def test_home_assistant_default_from_matches_raw_state(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """`from:` is matched against the raw state, as Home Assistant does."""
    await set_state(hass, WINDOW, "on")
    rec = await attach(hass, "state", WINDOW, **{"from": "on"}, longer_gaps=HA_DEFAULT)

    await set_state(hass, WINDOW, "unavailable")
    await advance(hass, freezer, 60)
    await set_state(hass, WINDOW, "off")

    assert rec.fired == []
    # Home Assistant's trigger would have fired going into the gap. This one
    # doesn't, and the trace says so.
    assert rec.reasons == ["entered_gap"]
    rec.detach()


async def test_home_assistant_default_previous_unknown(hass: HomeAssistant) -> None:
    """An entity seen for the first time fires as Home Assistant's would."""
    rec = await attach(hass, "state", "sensor.new", to="1", longer_gaps=HA_DEFAULT)

    await set_state(hass, "sensor.new", "1")

    assert len(rec.fired) == 1
    assert rec.fired[0].payload["from_state"] is None
    rec.detach()
