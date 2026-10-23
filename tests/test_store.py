"""Tests for the store: reloads, restarts, persistence, and invalidation."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest

from custom_components.unavailable_aware.const import (
    RETENTION,
    SAVE_DELAY,
    STARTUP_RETENTION,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from custom_components.unavailable_aware.store import DATA_STORE

from .conftest import advance, attach, set_state, setup_integration

DOOR = "binary_sensor.door"


def _stored(
    entity_id: str,
    state: str,
    *,
    attributes: dict | None = None,
    saved_ago: timedelta = timedelta(minutes=2),
    gap_started_ago: timedelta | None = None,
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
                    "gap_kind": None if gap_started_ago is None else "unavailable",
                    "gap_started": (
                        None
                        if gap_started_ago is None
                        else (now - gap_started_ago).isoformat()
                    ),
                }
            ],
        },
    }


# Gaps that span an automation reload


async def test_reload_mid_gap_same(hass: HomeAssistant) -> None:
    """Re-attaching during a gap, then coming back unchanged, doesn't fire."""
    await set_state(hass, DOOR, "on")
    first = await attach(hass, "state", DOOR, to="on")
    await set_state(hass, DOOR, "unavailable")
    first.detach()

    second = await attach(hass, "state", DOOR, to="on")
    await set_state(hass, DOOR, "on")

    assert first.fired == second.fired == []
    second.detach()


async def test_reload_mid_gap_different(hass: HomeAssistant) -> None:
    """Re-attaching during a gap and coming back changed fires from before the gap."""
    await set_state(hass, DOOR, "on")
    first = await attach(hass, "state", DOOR, to="off")
    await set_state(hass, DOOR, "unavailable")
    first.detach()

    second = await attach(hass, "state", DOOR, to="off")
    await set_state(hass, DOOR, "off")

    assert len(second.fired) == 1
    assert second.fired[0].from_value == "on"
    second.detach()


async def test_gap_while_detached_within_retention(hass: HomeAssistant) -> None:
    """The store keeps tracking an entity for `RETENTION` after the last detach."""
    await set_state(hass, DOOR, "on")
    first = await attach(hass, "state", DOOR, to="off")
    first.detach()
    await set_state(hass, DOOR, "unavailable")

    second = await attach(hass, "state", DOOR, to="off")
    await set_state(hass, DOOR, "off")

    assert len(second.fired) == 1
    assert second.fired[0].from_value == "on"
    second.detach()


async def test_entry_released_after_retention(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The store forgets an entity once `RETENTION` passes with no trigger using it."""
    await set_state(hass, DOOR, "on")
    first = await attach(hass, "state", DOOR, to="off")
    first.detach()
    await advance(hass, freezer, RETENTION.total_seconds() + 1)

    store = hass.data[DATA_STORE]
    assert store.async_diagnostics()["entries"] == []
    assert store.async_diagnostics()["listening"] == []

    await set_state(hass, DOOR, "unavailable")
    second = await attach(hass, "state", DOOR, to="off")
    await set_state(hass, DOOR, "off")
    # The store has no state from before the gap, so it's ignored.
    assert second.fired == []
    second.detach()


# Gaps that span a restart


async def test_restart_same_value(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Coming back unchanged after a restart doesn't fire."""
    hass_storage[STORAGE_KEY] = _stored(DOOR, "on")
    await set_state(hass, DOOR, "unavailable", {"restored": True})
    rec = await attach(hass, "state", DOOR, to="on", bridge_up_to={"minutes": 10})

    await set_state(hass, DOOR, "on")

    assert rec.fired == []
    assert rec.reasons == ["bridged_gap"]
    assert rec.reports[0].data["gap"]["kind"] == "restart"
    rec.detach()


async def test_restart_different_value_ignored(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A restart longer than `bridge_up_to` is ignored by default."""
    hass_storage[STORAGE_KEY] = _stored(DOOR, "on")
    await set_state(hass, DOOR, "unavailable")
    rec = await attach(hass, "state", DOOR, to="off")

    await set_state(hass, DOOR, "off")

    assert rec.fired == []
    assert rec.reasons == ["longer_gap_ignored"]
    rec.detach()


async def test_quick_restart_is_bridged(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A restart is timed like any other gap, so a quick one is bridged."""
    hass_storage[STORAGE_KEY] = _stored(DOOR, "on", saved_ago=timedelta(seconds=5))
    await set_state(hass, DOOR, "unavailable")
    rec = await attach(hass, "state", DOOR, to="off")

    await set_state(hass, DOOR, "off")

    assert len(rec.fired) == 1
    assert rec.fired[0].from_value == "on"
    assert rec.fired[0].payload["gap"]["kind"] == "restart"
    rec.detach()


async def test_restart_within_raised_bridge_up_to(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A restart within a higher `bridge_up_to` compares with the saved state."""
    hass_storage[STORAGE_KEY] = _stored(
        DOOR, "on", gap_started_ago=timedelta(minutes=3)
    )
    await remove_door(hass)
    rec = await attach(
        hass,
        "state",
        DOOR,
        **{"from": "on", "to": "off"},
        bridge_up_to={"minutes": 10},
    )

    await set_state(hass, DOOR, "off")

    assert len(rec.fired) == 1
    fired = rec.fired[0]
    assert fired.from_value == "on"
    assert fired.payload["raw_from_state"] is None
    gap = fired.payload["gap"]
    assert gap["kind"] == "restart"
    assert gap["duration"] == pytest.approx(180, abs=2)
    rec.detach()


async def remove_door(hass: HomeAssistant) -> None:
    """Remove the door's state, as it is before its integration loads."""
    hass.states.async_remove(DOOR)
    await hass.async_block_till_done()


async def test_restart_already_back_when_attaching(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """If the entity is back before a trigger attaches, its current state is used."""
    hass_storage[STORAGE_KEY] = _stored(DOOR, "on")
    await set_state(hass, DOOR, "off")
    rec = await attach(hass, "state", DOOR, to="off", bridge_up_to={"minutes": 10})

    await set_state(hass, DOOR, "unavailable")
    await set_state(hass, DOOR, "off")

    assert rec.fired == []
    rec.detach()


async def test_restart_attribute_entry(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A followed attribute is kept across a restart and compared after loading."""
    hass_storage[STORAGE_KEY] = _stored(
        "climate.thermostat",
        "heat",
        attributes={"hvac_action": "idle"},
    )
    await set_state(hass, "climate.thermostat", "unavailable")
    rec = await attach(
        hass,
        "state",
        "climate.thermostat",
        attribute="hvac_action",
        bridge_up_to={"minutes": 10},
    )

    await set_state(hass, "climate.thermostat", "heat", {"hvac_action": "idle"})
    assert rec.fired == []
    rec.detach()


async def test_unused_entries_dropped(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """Saved entities that no trigger uses shortly after startup are dropped."""
    hass_storage[STORAGE_KEY] = _stored(DOOR, "on")
    await setup_integration(hass)
    store = hass.data[DATA_STORE]
    assert len(store.async_diagnostics()["entries"]) == 1

    await advance(hass, freezer, STARTUP_RETENTION.total_seconds() + 1)
    assert store.async_diagnostics()["entries"] == []


async def test_corrupt_storage_starts_empty(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Broken saved entries are skipped instead of failing the load."""
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "saved_at": None,
            "entries": [{"nonsense": True}, {"entity_id": DOOR}],
        },
    }
    await set_state(hass, DOOR, "on")
    rec = await attach(hass, "state", DOOR, to="off")
    await set_state(hass, DOOR, "off")
    assert len(rec.fired) == 1
    rec.detach()


# Persistence


async def test_written_to_disk(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """Changes are written after a short delay."""
    await set_state(hass, DOOR, "on")
    rec = await attach(hass, "state", DOOR)
    await set_state(hass, DOOR, "unavailable")
    await advance(hass, freezer, SAVE_DELAY + 1)

    entries = hass_storage[STORAGE_KEY]["data"]["entries"]
    assert entries[0]["entity_id"] == DOOR
    assert entries[0]["previous"]["state"] == "on"
    assert entries[0]["gap_kind"] == "unavailable"
    rec.detach()


async def test_stop_saves_and_stops_recording(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Shutdown saves immediately, then ignores entities going unavailable."""
    await set_state(hass, DOOR, "on")
    rec = await attach(hass, "state", DOOR)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    await set_state(hass, DOOR, "unavailable")

    entries = hass_storage[STORAGE_KEY]["data"]["entries"]
    assert entries[0]["previous"]["state"] == "on"
    assert entries[0]["gap_kind"] is None
    rec.detach()


# Invalidation


async def test_old_state_still_trusted_within_bridge_up_to(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Saved states have no age limit; only `bridge_up_to` decides."""
    await set_state(hass, DOOR, "on")
    rec = await attach(hass, "state", DOOR, **{"from": "on"}, bridge_up_to={"days": 30})

    await set_state(hass, DOOR, "unavailable")
    await advance(hass, freezer, 14 * 86400)
    await set_state(hass, DOOR, "off")

    assert len(rec.fired) == 1
    assert rec.fired[0].from_value == "on"
    rec.detach()


async def test_registry_removal_forgets(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """An entity removed from the registry isn't compared with its old state later."""
    entry = entity_registry.async_get_or_create(
        "binary_sensor", "test", "door-1", suggested_object_id="door"
    )
    assert entry.entity_id == DOOR
    await set_state(hass, DOOR, "on")
    rec = await attach(hass, "state", DOOR, to="on")

    await set_state(hass, DOOR, "unavailable")
    entity_registry.async_remove(DOOR)
    await hass.async_block_till_done()
    await set_state(hass, DOOR, "on")

    # There's no earlier state to compare with, so it's ignored even after a short gap.
    assert rec.fired == []
    assert rec.reasons == ["previous_unknown"]
    rec.detach()


async def test_registry_rename_forgets_old_id(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """Renaming an entity ID clears the state saved under the old ID."""
    entity_registry.async_get_or_create(
        "binary_sensor", "test", "door-1", suggested_object_id="door"
    )
    await set_state(hass, DOOR, "on")
    rec = await attach(hass, "state", DOOR, to="on")
    await set_state(hass, DOOR, "unavailable")

    entity_registry.async_update_entity(DOOR, new_entity_id="binary_sensor.front")
    await hass.async_block_till_done()
    entries = hass.data[DATA_STORE].async_diagnostics()["entries"]
    assert entries[0]["previous"] is None
    rec.detach()


async def test_two_triggers_share_the_store(hass: HomeAssistant) -> None:
    """Triggers on the same entity share one listener."""
    await set_state(hass, DOOR, "on")
    first = await attach(hass, "state", DOOR, to="off")
    second = await attach(hass, "numeric_state", "sensor.other", above=1)
    third = await attach(hass, "state", DOOR, to="on")

    diagnostics = hass.data[DATA_STORE].async_diagnostics()
    assert diagnostics["listening"] == [DOOR, "sensor.other"]

    await set_state(hass, DOOR, "off")
    assert len(first.fired) == 1
    assert third.fired == []
    for rec in (first, second, third):
        rec.detach()


async def test_frequent_changes_still_saved(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """An entity that changes every few seconds doesn't keep delaying the save."""
    await set_state(hass, "sensor.power", "1")
    rec = await attach(hass, "numeric_state", "sensor.power", above=100)
    for value in range(2, 8):
        await set_state(hass, "sensor.power", str(value))
        await advance(hass, freezer, 3)

    entries = hass_storage[STORAGE_KEY]["data"]["entries"]
    assert int(entries[0]["previous"]["state"]) >= 4
    rec.detach()
