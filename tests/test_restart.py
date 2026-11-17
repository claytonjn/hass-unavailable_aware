"""Tests for restarts: entities already back at startup, and `for` waits."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import CoreState, HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.trigger import TriggerConfig
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.unavailable_aware.const import (
    DOMAIN,
    RESTART_RETURN_WINDOW,
    SAVE_DELAY,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from custom_components.unavailable_aware.trigger import TRIGGERS

from .conftest import advance, attach, set_state, setup_integration

BATTERY = "sensor.motion_battery"
FAN = "fan.ceiling"
BRIDGE_A_DAY = {"bridge_up_to": {"days": 1}}


def _saved(
    entity_id: str,
    state: str,
    *,
    attributes: dict | None = None,
    saved_ago: timedelta = timedelta(minutes=2),
    waits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return store data as it would have been saved before a restart."""
    now = dt_util.utcnow()
    previous = State(
        entity_id,
        state,
        attributes,
        last_changed=now - timedelta(hours=1),
        last_updated=now - timedelta(hours=1),
    )
    return {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "saved_at": (now - saved_ago).isoformat(),
            "entries": [
                {
                    "entity_id": entity_id,
                    "previous": previous.as_dict(),
                    "gap_kind": None,
                    "gap_started": None,
                    "waits": waits or {},
                }
            ],
        },
    }


async def _fingerprint(hass: HomeAssistant, key: str, **options: Any) -> str:
    """Return the fingerprint a trigger with these options saves its waits under."""
    cls = TRIGGERS[key]
    config = await cls.async_validate_complete_config(
        hass,
        {
            "platform": f"{DOMAIN}.{key}",
            "target": {"entity_id": "sensor.any"},
            "options": options,
        },
    )
    trigger = cls(
        hass,
        TriggerConfig(
            key=f"{DOMAIN}.{key}", target=config["target"], options=config["options"]
        ),
    )
    return trigger.fingerprint


async def _start(hass: HomeAssistant) -> None:
    """Finish starting Home Assistant, startup jobs (armed automations) included."""
    await hass.async_start()
    await hass.async_block_till_done()


def _starting(hass: HomeAssistant) -> None:
    """Put Home Assistant back at the start of its startup."""
    hass.set_state(CoreState.not_running)


# Entities already back when the triggers subscribe at startup


async def test_back_before_subscribing_compared_with_saved_state(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A change while Home Assistant was down fires once the trigger subscribes."""
    _starting(hass)
    hass_storage[STORAGE_KEY] = _saved(BATTERY, "25")
    await set_state(hass, BATTERY, "15")
    rec = await attach(hass, "numeric_state", BATTERY, below=20, **BRIDGE_A_DAY)
    await hass.async_block_till_done()

    assert len(rec.fired) == 1
    fired = rec.fired[0]
    assert fired.from_value == "25"
    assert fired.to_value == "15"
    assert fired.payload["raw_from_state"] is None
    assert fired.payload["gap"]["kind"] == "restart"
    rec.detach()


async def test_back_before_subscribing_unchanged(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """An entity back unchanged doesn't fire or leave a trace.

    Home Assistant's own trigger saw no change either, so there's nothing to
    report.
    """
    _starting(hass)
    hass_storage[STORAGE_KEY] = _saved(FAN, "on")
    await set_state(hass, FAN, "on")
    rec = await attach(hass, "state", FAN, to="on", **BRIDGE_A_DAY)
    await hass.async_block_till_done()

    assert rec.fired == []
    assert rec.reports == []
    rec.detach()


async def test_back_before_subscribing_longer_than_bridge_up_to(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """With the default `bridge_up_to`, a restart is ignored and the trace says so."""
    _starting(hass)
    hass_storage[STORAGE_KEY] = _saved(BATTERY, "25")
    await set_state(hass, BATTERY, "15")
    rec = await attach(hass, "numeric_state", BATTERY, below=20)
    await hass.async_block_till_done()

    assert rec.fired == []
    assert rec.reasons == ["longer_gap_ignored"]
    assert rec.reports[0].data["raw_from"] is None
    rec.detach()


async def test_back_before_subscribing_home_assistant_default(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """`longer_gaps: home_assistant_default` does what Home Assistant does: nothing."""
    _starting(hass)
    hass_storage[STORAGE_KEY] = _saved(FAN, "off")
    await set_state(hass, FAN, "on")
    rec = await attach(
        hass, "state", FAN, to="on", longer_gaps="home_assistant_default"
    )
    await hass.async_block_till_done()

    assert rec.fired == []
    assert rec.reports == []
    rec.detach()


async def test_every_trigger_subscribing_at_startup_compares(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Each trigger on the entity gets the comparison, not only the first."""
    _starting(hass)
    hass_storage[STORAGE_KEY] = _saved(FAN, "off")
    await set_state(hass, FAN, "on")
    first = await attach(hass, "state", FAN, to="on", **BRIDGE_A_DAY)
    second = await attach(hass, "state", FAN, **{"from": "off"}, **BRIDGE_A_DAY)
    await hass.async_block_till_done()

    assert len(first.fired) == 1
    assert len(second.fired) == 1
    for rec in (first, second):
        rec.detach()


async def test_automations_armed_just_after_startup(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Triggers subscribing just after startup still compare.

    Some Home Assistant releases (2026.7, for one) arm automations once
    startup has finished.
    """
    _starting(hass)
    hass_storage[STORAGE_KEY] = _saved(FAN, "off")
    await setup_integration(hass)
    await set_state(hass, FAN, "on")
    await _start(hass)
    rec = await attach(hass, "state", FAN, to="on", **BRIDGE_A_DAY)
    await hass.async_block_till_done()

    assert len(rec.fired) == 1
    rec.detach()


async def test_no_comparison_once_the_window_closes(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """A trigger subscribing well after startup starts from the current state."""
    _starting(hass)
    hass_storage[STORAGE_KEY] = _saved(FAN, "off")
    await setup_integration(hass)
    await set_state(hass, FAN, "on")
    await _start(hass)
    await advance(hass, freezer, RESTART_RETURN_WINDOW.total_seconds() + 1)
    rec = await attach(hass, "state", FAN, to="on", **BRIDGE_A_DAY)
    await hass.async_block_till_done()

    assert rec.fired == []
    rec.detach()


async def test_no_second_comparison_after_a_reload(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """An automation reloaded right after startup doesn't compare again."""
    _starting(hass)
    hass_storage[STORAGE_KEY] = _saved(FAN, "off")
    await set_state(hass, FAN, "on")
    first = await attach(hass, "state", FAN, to="on", **BRIDGE_A_DAY)
    await _start(hass)
    first.detach()

    again = await attach(hass, "state", FAN, to="on", **BRIDGE_A_DAY)
    await hass.async_block_till_done()

    assert len(first.fired) == 1
    assert again.fired == []
    again.detach()


async def test_no_comparison_once_the_entity_changed(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """After the entity changes, a later trigger starts from its current state.

    The first trigger gets the restart comparison and then the change, in order.
    """
    _starting(hass)
    hass_storage[STORAGE_KEY] = _saved(FAN, "off")
    await set_state(hass, FAN, "on")
    first = await attach(hass, "state", FAN, **BRIDGE_A_DAY)
    hass.states.async_set(FAN, "off")
    await hass.async_block_till_done()
    late = await attach(hass, "state", FAN, **BRIDGE_A_DAY)
    await hass.async_block_till_done()

    assert [(f.from_value, f.to_value) for f in first.fired] == [
        ("off", "on"),
        ("on", "off"),
    ]
    assert late.fired == []
    for rec in (first, late):
        rec.detach()


async def test_still_unavailable_when_subscribing_is_unchanged(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """An entity still unavailable at startup is compared when it comes back."""
    _starting(hass)
    hass_storage[STORAGE_KEY] = _saved(BATTERY, "25")
    await set_state(hass, BATTERY, "unavailable")
    rec = await attach(hass, "numeric_state", BATTERY, below=20, **BRIDGE_A_DAY)
    await _start(hass)
    await set_state(hass, BATTERY, "15")

    assert len(rec.fired) == 1
    assert rec.fired[0].from_value == "25"
    assert rec.fired[0].payload["raw_from_state"].state == "unavailable"
    rec.detach()


async def test_automation_at_startup(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """An automation armed during startup runs for a change made while stopped."""
    _starting(hass)
    hass_storage[STORAGE_KEY] = _saved(BATTERY, "25")
    await setup_integration(hass)
    await set_state(hass, BATTERY, "15")
    calls: list[Any] = []
    hass.bus.async_listen("battery_low", calls.append)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "battery",
                "alias": "Battery",
                "triggers": [
                    {
                        "trigger": "unavailable_aware.numeric_state",
                        "entity_id": BATTERY,
                        "below": 20,
                        "bridge_up_to": {"days": 1},
                    }
                ],
                "actions": [
                    {
                        "event": "battery_low",
                        "event_data": {
                            "from": "{{ trigger.from_state.state }}",
                            "gap": "{{ trigger.gap.kind }}",
                        },
                    }
                ],
            }
        },
    )
    await _start(hass)

    assert len(calls) == 1
    assert calls[0].data == {"from": 25, "gap": "restart"}


# `for` waits across a restart


async def test_pending_wait_saved_and_cleared(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """A pending wait is written to disk, and removed once it fires."""
    await set_state(hass, BATTERY, "25")
    options = {"below": 20, "for": {"minutes": 10}}
    rec = await attach(hass, "numeric_state", BATTERY, **options)
    key = await _fingerprint(hass, "numeric_state", **options)

    await set_state(hass, BATTERY, "15")
    await advance(hass, freezer, SAVE_DELAY + 1)
    waits = hass_storage[STORAGE_KEY]["data"]["entries"][0]["waits"]
    assert list(waits) == [key]
    assert waits[key]["from_state"]["state"] == "25"

    await advance(hass, freezer, 600)
    assert len(rec.fired) == 1
    await advance(hass, freezer, SAVE_DELAY + 1)
    assert hass_storage[STORAGE_KEY]["data"]["entries"][0]["waits"] == {}
    rec.detach()


async def test_pending_wait_cleared_when_cancelled_or_detached(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """A wait that ends without firing is removed from disk too."""
    await set_state(hass, BATTERY, "25")
    options = {"below": 20, "for": {"minutes": 10}}
    rec = await attach(hass, "numeric_state", BATTERY, **options)

    await set_state(hass, BATTERY, "15")
    await set_state(hass, BATTERY, "30")
    await advance(hass, freezer, SAVE_DELAY + 1)
    assert hass_storage[STORAGE_KEY]["data"]["entries"][0]["waits"] == {}

    await set_state(hass, BATTERY, "15")
    rec.detach()
    await advance(hass, freezer, SAVE_DELAY + 1)
    assert hass_storage[STORAGE_KEY]["data"]["entries"][0]["waits"] == {}


async def test_identical_triggers_share_a_wait(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """With two identical triggers waiting, removing one keeps the saved wait."""
    await set_state(hass, BATTERY, "25")
    options = {"below": 20, "for": {"minutes": 10}}
    first = await attach(hass, "numeric_state", BATTERY, **options)
    second = await attach(hass, "numeric_state", BATTERY, **options)

    await set_state(hass, BATTERY, "15")
    first.detach()
    await advance(hass, freezer, SAVE_DELAY + 1)
    assert len(hass_storage[STORAGE_KEY]["data"]["entries"][0]["waits"]) == 1
    second.detach()


async def test_wait_starts_over_after_restart(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """A wait pending at shutdown starts over in full if the entity still holds."""
    _starting(hass)
    options = {"below": 20, "for": {"minutes": 10}}
    key = await _fingerprint(hass, "numeric_state", **options)
    from_state = State(BATTERY, "25").as_dict()
    hass_storage[STORAGE_KEY] = _saved(
        BATTERY, "15", waits={key: {"from_state": from_state, "hold": None}}
    )
    await set_state(hass, BATTERY, "15")
    rec = await attach(hass, "numeric_state", BATTERY, **options)
    await _start(hass)

    await advance(hass, freezer, 599)
    assert rec.fired == []
    await advance(hass, freezer, 2)
    assert len(rec.fired) == 1
    assert rec.fired[0].from_value == "25"
    assert rec.fired[0].payload["gap"]["kind"] == "restart"
    rec.detach()


async def test_wait_starts_over_with_raised_bridge_up_to(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """A bridged restart starts the wait over too: its timer didn't survive."""
    _starting(hass)
    options = {"below": 20, "for": {"minutes": 10}, **BRIDGE_A_DAY}
    key = await _fingerprint(hass, "numeric_state", **options)
    from_state = State(BATTERY, "25").as_dict()
    hass_storage[STORAGE_KEY] = _saved(
        BATTERY, "15", waits={key: {"from_state": from_state, "hold": None}}
    )
    await set_state(hass, BATTERY, "15")
    rec = await attach(hass, "numeric_state", BATTERY, **options)
    await _start(hass)

    assert rec.fired == []
    await advance(hass, freezer, 601)
    assert len(rec.fired) == 1
    assert rec.fired[0].from_value == "25"
    rec.detach()


async def test_wait_dropped_if_no_longer_holding(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """A wait from before the restart ends if the entity no longer matches."""
    _starting(hass)
    options = {"below": 20, "for": {"minutes": 10}}
    key = await _fingerprint(hass, "numeric_state", **options)
    from_state = State(BATTERY, "25").as_dict()
    hass_storage[STORAGE_KEY] = _saved(
        BATTERY, "15", waits={key: {"from_state": from_state, "hold": None}}
    )
    await set_state(hass, BATTERY, "100")
    rec = await attach(hass, "numeric_state", BATTERY, **options)
    await _start(hass)

    await advance(hass, freezer, 601)
    assert rec.fired == []
    rec.detach()


async def test_wait_restored_when_entity_returns_after_startup(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """An entity still unavailable at startup gets its wait back when it returns."""
    _starting(hass)
    options = {"to": "on", "for": {"minutes": 10}}
    key = await _fingerprint(hass, "state", **options)
    from_state = State(FAN, "off").as_dict()
    hass_storage[STORAGE_KEY] = _saved(
        FAN, "on", waits={key: {"from_state": from_state, "hold": "on"}}
    )
    await set_state(hass, FAN, "unavailable")
    rec = await attach(hass, "state", FAN, **options)
    await _start(hass)

    await set_state(hass, FAN, "on")
    await advance(hass, freezer, 599)
    assert rec.fired == []
    await advance(hass, freezer, 2)
    assert len(rec.fired) == 1
    assert rec.fired[0].from_value == "off"
    rec.detach()


async def test_wait_restored_for_attribute_saved_as_list(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """A held attribute saved as JSON still matches the value it came from."""
    _starting(hass)
    climate = "climate.thermostat"
    options = {"attribute": "preset_modes", "for": {"minutes": 10}}
    key = await _fingerprint(hass, "state", **options)
    from_state = State(climate, "heat", {"preset_modes": ("home",)}).as_dict()
    hass_storage[STORAGE_KEY] = _saved(
        climate,
        "heat",
        attributes={"preset_modes": ("home", "away")},
        waits={key: {"from_state": from_state, "hold": ["home", "away"]}},
    )
    await set_state(hass, climate, "heat", {"preset_modes": ("home", "away")})
    rec = await attach(hass, "state", climate, **options)
    await _start(hass)

    await advance(hass, freezer, 601)
    assert len(rec.fired) == 1
    rec.detach()


async def test_a_different_trigger_ignores_the_saved_wait(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """A wait is only taken back by a trigger with the same options."""
    _starting(hass)
    key = await _fingerprint(
        hass, "numeric_state", below=20, **{"for": {"minutes": 10}}
    )
    from_state = State(BATTERY, "25").as_dict()
    hass_storage[STORAGE_KEY] = _saved(
        BATTERY, "15", waits={key: {"from_state": from_state, "hold": None}}
    )
    await set_state(hass, BATTERY, "15")
    rec = await attach(
        hass, "numeric_state", BATTERY, below=20, **{"for": {"minutes": 5}}
    )
    await _start(hass)

    await advance(hass, freezer, 601)
    assert rec.fired == []
    rec.detach()


async def test_reading_as_a_device_is_removed_does_not_fire(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A low reading a device reports as it is deleted doesn't fire.

    The device was unavailable for a week, reported 0% and was removed a
    second later. The `for` wait never sees it held, and a replacement paired
    under the same entity ID isn't compared with the old device.
    """
    entity_registry.async_get_or_create(
        "sensor", "test", "battery-1", suggested_object_id="motion_battery"
    )
    await set_state(hass, BATTERY, "40")
    options = {"below": 20, "for": {"minutes": 10}, "bridge_up_to": {"days": 30}}
    rec = await attach(hass, "numeric_state", BATTERY, **options)

    await set_state(hass, BATTERY, "unavailable")
    await advance(hass, freezer, 7 * 86400)
    await set_state(hass, BATTERY, "0")
    await advance(hass, freezer, 1)
    entity_registry.async_remove(BATTERY)
    hass.states.async_remove(BATTERY)
    await hass.async_block_till_done()
    await advance(hass, freezer, 900)

    entity_registry.async_get_or_create(
        "sensor", "test", "battery-2", suggested_object_id="motion_battery"
    )
    await set_state(hass, BATTERY, "75")
    await advance(hass, freezer, 900)

    assert rec.fired == []
    rec.detach()
