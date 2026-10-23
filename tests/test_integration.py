"""End-to-end tests: automations, scripts, config flow, and descriptions."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.components import automation
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv, script
from homeassistant.helpers.trigger import async_get_all_descriptions
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.unavailable_aware.const import DOMAIN
from custom_components.unavailable_aware.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import remove_state, set_state, setup_integration

COUNT = "sensor.count"
THERMOSTAT = "climate.thermostat"

_ACTION = {
    "action": "test.automation",
    "data": {
        "from": "{{ trigger.from_state.state if trigger.from_state else 'none' }}",
        "raw": "{{ trigger.raw_from_state.state if trigger.raw_from_state else 'none' }}",
        "to": "{{ trigger.to_state.state }}",
        "gap": "{{ trigger.gap.kind if trigger.gap else 'none' }}",
        "platform": "{{ trigger.platform }}",
        "id": "{{ trigger.id }}",
    },
}


def _automation(trigger: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "id": "test_automation",
        "alias": "Test",
        "triggers": [trigger],
        "actions": [_ACTION],
        **extra,
    }


async def _setup(
    hass: HomeAssistant, *automations: dict[str, Any]
) -> list[ServiceCall]:
    await setup_integration(hass)
    calls = async_mock_service(hass, "test", "automation")
    assert await async_setup_component(
        hass, automation.DOMAIN, {automation.DOMAIN: list(automations)}
    )
    await hass.async_block_till_done()
    return calls


async def test_old_style_syntax(hass: HomeAssistant) -> None:
    """Home Assistant's trigger syntax works after changing only the trigger's name."""
    await set_state(hass, COUNT, "1")
    calls = await _setup(
        hass,
        _automation(
            {
                "trigger": "unavailable_aware.numeric_state",
                "entity_id": COUNT,
                "below": 1,
            }
        ),
    )

    await remove_state(hass, COUNT)
    await set_state(hass, COUNT, "1")
    assert calls == []

    await set_state(hass, COUNT, "unavailable")
    await set_state(hass, COUNT, "0")
    assert len(calls) == 1
    # Templates render to native types, so "1" becomes 1.
    assert calls[0].data == {
        "from": 1,
        "raw": "unavailable",
        "to": 0,
        "gap": "unavailable",
        "platform": "unavailable_aware.numeric_state",
        "id": 0,
    }


async def test_new_style_syntax(hass: HomeAssistant) -> None:
    """The `target` and `options` syntax the automation editor writes also works."""
    await set_state(hass, THERMOSTAT, "heat", {"hvac_action": "idle"})
    calls = await _setup(
        hass,
        _automation(
            {
                "trigger": "unavailable_aware.state",
                "id": "thermostat",
                "target": {"entity_id": THERMOSTAT},
                "options": {"attribute": "hvac_action", "to": "heating"},
            }
        ),
    )

    await set_state(hass, THERMOSTAT, "unavailable")
    await set_state(hass, THERMOSTAT, "heat", {"hvac_action": "idle"})
    assert calls == []
    await set_state(hass, THERMOSTAT, "heat", {"hvac_action": "heating"})
    assert len(calls) == 1
    assert calls[0].data["id"] == "thermostat"


async def test_old_style_with_for_and_list(hass: HomeAssistant) -> None:
    """Old-style lists of entities and `for:` are converted too."""
    await set_state(hass, "binary_sensor.a", "off")
    await set_state(hass, "binary_sensor.b", "off")
    await _setup(
        hass,
        _automation(
            {
                "trigger": "unavailable_aware.state",
                "entity_id": ["binary_sensor.a", "binary_sensor.b"],
                "to": "on",
                "for": "00:00:05",
                "bridge_up_to": {"seconds": 10},
            }
        ),
    )
    assert hass.states.get("automation.test").state == "on"


async def test_bounce_leaves_a_trace(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A bounce that doesn't fire is recorded in the trace as not triggered."""
    await set_state(hass, COUNT, "0")
    calls = await _setup(
        hass,
        _automation(
            {
                "trigger": "unavailable_aware.numeric_state",
                "entity_id": COUNT,
                "below": 1,
            }
        ),
    )

    # What a template reload does: removed, then unavailable, then back at 0.
    await remove_state(hass, COUNT)
    await set_state(hass, COUNT, "unavailable")
    await set_state(hass, COUNT, "0")
    assert calls == []

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "trace/list", "domain": "automation"})
    response = await client.receive_json()
    assert response["success"]
    traces = response["result"]
    assert len(traces) == 1
    assert traces[0]["script_execution"] == "not_triggered"

    await client.send_json_auto_id(
        {
            "type": "trace/get",
            "domain": "automation",
            "item_id": "test_automation",
            "run_id": traces[0]["run_id"],
        }
    )
    response = await client.receive_json()
    step = response["result"]["trace"]["trigger/0"][0]
    assert step["result"]["reason"] == "bridged_gap"
    assert step["result"]["data"]["gap"]["kind"] == "removed"


async def test_automation_reload_mid_gap(hass: HomeAssistant) -> None:
    """An automation reload during a gap still compares with the state before it."""
    config = _automation(
        {"trigger": "unavailable_aware.state", "entity_id": COUNT, "to": "0"}
    )
    await set_state(hass, COUNT, "2")
    calls = await _setup(hass, config)

    await set_state(hass, COUNT, "unavailable")
    with patch(
        "homeassistant.config.load_yaml_config_file",
        return_value={automation.DOMAIN: [config]},
    ):
        await hass.services.async_call(automation.DOMAIN, "reload", blocking=True)
    await hass.async_block_till_done()
    await set_state(hass, COUNT, "2")
    assert calls == []

    await set_state(hass, COUNT, "unavailable")
    with patch(
        "homeassistant.config.load_yaml_config_file",
        return_value={automation.DOMAIN: [config]},
    ):
        await hass.services.async_call(automation.DOMAIN, "reload", blocking=True)
    await hass.async_block_till_done()
    await set_state(hass, COUNT, "0")
    assert len(calls) == 1
    assert calls[0].data["from"] == 2


async def test_wait_for_trigger_survives_gap(hass: HomeAssistant) -> None:
    """A bounce doesn't end a `wait_for_trigger`, and a later change does."""
    await setup_integration(hass)
    await set_state(hass, THERMOSTAT, "heat", {"hvac_action": "idle"})
    sequence = await script.async_validate_actions_config(
        hass,
        cv.SCRIPT_SCHEMA(
            [
                {
                    "wait_for_trigger": [
                        {
                            "trigger": "unavailable_aware.state",
                            "entity_id": THERMOSTAT,
                            "attribute": "hvac_action",
                            "from": "idle",
                        }
                    ],
                    "timeout": 60,
                }
            ]
        ),
    )
    script_obj = script.Script(hass, sequence, "Wait", "test")
    task = hass.async_create_background_task(
        script_obj.async_run(context=Context()), "wait"
    )
    for _ in range(50):
        if script_obj.last_action is not None:
            break
        await asyncio.sleep(0)
    assert script_obj.is_running

    await set_state(hass, THERMOSTAT, "unavailable")
    await set_state(hass, THERMOSTAT, "heat", {"hvac_action": "idle"})
    assert not task.done()

    await set_state(hass, THERMOSTAT, "heat", {"hvac_action": "heating"})
    result = await asyncio.wait_for(task, 5)
    wait = result.variables["wait"]
    assert wait["trigger"]["to_state"].attributes["hvac_action"] == "heating"
    assert wait["trigger"]["from_state"].attributes["hvac_action"] == "idle"


async def test_config_flow(hass: HomeAssistant) -> None:
    """The config flow creates one entry with no options and refuses a second."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["store"]["entries"] == []

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_descriptions_load(hass: HomeAssistant) -> None:
    """`triggers.yaml` loads, and both triggers have editor descriptions."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    descriptions = await async_get_all_descriptions(hass)
    state = descriptions[f"{DOMAIN}.state"]
    numeric = descriptions[f"{DOMAIN}.numeric_state"]
    assert state is not None and numeric is not None
    assert "bridge_up_to" in state["fields"]
    assert "bridge_restarts_for_timer" in state["fields"]
    assert state["fields"]["longer_gaps"]["selector"]["select"]["options"] == [
        "ignore",
        "home_assistant_default",
    ]
    assert "above" in numeric["fields"]
