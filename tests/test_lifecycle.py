"""Tests for the config entry lifecycle. The triggers only run while it's loaded."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import automation
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, ServiceCall
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.unavailable_aware.const import DOMAIN, SAVE_DELAY, STORAGE_KEY
from custom_components.unavailable_aware.dependents import ISSUE_NOT_SET_UP
from custom_components.unavailable_aware.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.unavailable_aware.store import get_store

from .conftest import advance, set_state, setup_integration

DOOR = "binary_sensor.door"

AUTOMATION = {
    "id": "door_opened",
    "alias": "Door opened",
    "triggers": [{"trigger": "unavailable_aware.state", "entity_id": DOOR, "to": "on"}],
    "actions": [{"action": "test.automation"}],
}
OTHER_AUTOMATION = {
    "id": "unrelated",
    "alias": "Unrelated",
    "triggers": [{"trigger": "state", "entity_id": DOOR, "to": "on"}],
    "actions": [{"action": "test.automation"}],
}


async def _automations(
    hass: HomeAssistant, *configs: dict[str, Any]
) -> list[ServiceCall]:
    calls = async_mock_service(hass, "test", "automation")
    assert await async_setup_component(
        hass, automation.DOMAIN, {automation.DOMAIN: list(configs)}
    )
    await hass.async_block_till_done()
    return calls


async def _reload_automations(hass: HomeAssistant, *configs: dict[str, Any]) -> None:
    with patch(
        "homeassistant.config.load_yaml_config_file",
        return_value={automation.DOMAIN: list(configs)},
    ):
        await hass.services.async_call(automation.DOMAIN, "reload", blocking=True)
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_NOT_SET_UP)


async def test_triggers_need_the_config_entry(hass: HomeAssistant) -> None:
    """Without the integration set up, the trigger doesn't attach.

    A repair lists the automation that uses it.
    """
    await set_state(hass, DOOR, "off")
    calls = await _automations(hass, AUTOMATION)

    await set_state(hass, DOOR, "on")

    assert calls == []
    issue = _issue(hass)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert (
        "Door opened (`automation.door_opened`)"
        in (issue.translation_placeholders or {})["dependents"]
    )


async def test_setting_up_clears_the_repair(hass: HomeAssistant) -> None:
    """Adding the integration back clears the repair."""
    await _automations(hass, AUTOMATION)
    assert _issue(hass) is not None

    await setup_integration(hass)

    assert _issue(hass) is None


async def test_removal_deletes_everything(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """Removing the integration deletes its data, and a repair lists what uses it."""
    entry = await setup_integration(hass)
    await set_state(hass, DOOR, "off")
    calls = await _automations(hass, AUTOMATION, OTHER_AUTOMATION)
    await set_state(hass, DOOR, "unavailable")
    await advance(hass, freezer, SAVE_DELAY + 1)
    assert STORAGE_KEY in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert STORAGE_KEY not in hass_storage
    assert get_store(hass) is None
    dependents = (_issue(hass).translation_placeholders or {})["dependents"]
    assert "Door opened" in dependents
    assert "Unrelated" not in dependents

    # The automation is still enabled, but its trigger no longer runs.
    await set_state(hass, DOOR, "on")
    assert len(calls) == 1  # Only the unrelated automation fired.
    await advance(hass, freezer, SAVE_DELAY + 1)
    assert STORAGE_KEY not in hass_storage


async def test_removal_without_dependents_leaves_no_repair(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """If nothing uses the triggers, removal leaves no data or repair behind."""
    entry = await setup_integration(hass)
    await _automations(hass, OTHER_AUTOMATION)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert STORAGE_KEY not in hass_storage
    assert _issue(hass) is None


async def test_repair_clears_when_nothing_uses_the_triggers(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The repair clears once no automation uses the triggers."""
    entry = await setup_integration(hass)
    await _automations(hass, AUTOMATION)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass) is not None

    await _reload_automations(hass, OTHER_AUTOMATION)
    await advance(hass, freezer, 2)

    assert _issue(hass) is None


async def test_disabling_raises_the_repair(hass: HomeAssistant) -> None:
    """Disabling the integration stops the triggers, so it raises the repair too."""
    entry = await setup_integration(hass)
    await _automations(hass, AUTOMATION)

    await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert _issue(hass) is not None

    await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await hass.async_block_till_done()
    assert _issue(hass) is None


async def test_triggers_resume_after_a_reload(hass: HomeAssistant) -> None:
    """Reloading the integration pauses the triggers, and they run again afterwards."""
    entry = await setup_integration(hass)
    await set_state(hass, DOOR, "off")
    calls = await _automations(hass, AUTOMATION)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass) is None

    await set_state(hass, DOOR, "on")
    assert len(calls) == 1


async def test_reload_mid_gap_still_bridged(hass: HomeAssistant) -> None:
    """A gap that spans a reload of the integration is still bridged."""
    entry = await setup_integration(hass)
    await set_state(hass, DOOR, "on")
    calls = await _automations(hass, AUTOMATION)
    await set_state(hass, DOOR, "unavailable")

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await set_state(hass, DOOR, "on")

    assert calls == []


async def test_diagnostics_when_unloaded(hass: HomeAssistant) -> None:
    """Diagnostics work whether or not the store is loaded."""
    entry = await setup_integration(hass)
    assert (await async_get_config_entry_diagnostics(hass, entry))["store"] is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert (await async_get_config_entry_diagnostics(hass, entry))["store"] is None


async def test_wait_only_dependents_are_listed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Triggers used only in `wait_for_trigger` are listed before they ever run."""
    wait = {
        "wait_for_trigger": [
            {"trigger": "unavailable_aware.state", "entity_id": DOOR, "to": "on"}
        ],
        "timeout": 5,
    }
    await _automations(
        hass,
        {
            "id": "waiting",
            "alias": "Waiting",
            "triggers": [{"trigger": "event", "event_type": "start"}],
            "actions": [wait],
        },
    )
    assert await async_setup_component(
        hass, "script", {"script": {"waiting_script": {"sequence": [wait]}}}
    )
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)

    dependents = (_issue(hass).translation_placeholders or {})["dependents"]
    assert "Waiting (`automation.waiting`)" in dependents
    assert "`script.waiting_script`" in dependents


async def test_listed_after_startup(hass: HomeAssistant) -> None:
    """At startup the check waits until Home Assistant has started."""
    hass.set_state(CoreState.not_running)
    await _automations(
        hass,
        {
            "id": "waiting",
            "alias": "Waiting",
            "triggers": [{"trigger": "event", "event_type": "start"}],
            "actions": [
                {
                    "wait_for_trigger": [
                        {"trigger": "unavailable_aware.state", "entity_id": DOOR}
                    ]
                }
            ],
        },
    )
    assert _issue(hass) is None

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    assert "Waiting" in (_issue(hass).translation_placeholders or {})["dependents"]
