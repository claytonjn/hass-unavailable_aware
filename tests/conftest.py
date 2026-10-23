"""Fixtures and helpers for Unavailable-Aware Triggers tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.helpers.trigger import NotTriggeredInfo, TriggerConfig
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.unavailable_aware.const import DOMAIN
from custom_components.unavailable_aware.store import get_store
from custom_components.unavailable_aware.trigger import TRIGGERS


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load the integration from this repository."""


@dataclass
class Fired:
    """The payload, description, and context from a single trigger firing."""

    payload: dict[str, Any]
    description: str
    context: Context | None

    @property
    def from_value(self) -> Any:
        state: State | None = self.payload["from_state"]
        return None if state is None else state.state

    @property
    def to_value(self) -> Any:
        return self.payload["to_state"].state


@dataclass
class Recorder:
    """Collects what an attached trigger reports during a test.

    `fired` has each time it fired. `reports` has each change it skipped that
    Home Assistant's own trigger would have fired for, with the reason.
    """

    hass: HomeAssistant
    fired: list[Fired] = field(default_factory=list)
    reports: list[NotTriggeredInfo] = field(default_factory=list)
    detach: Any = None

    def run(
        self,
        payload: dict[str, Any],
        description: str,
        context: Context | None = None,
    ) -> asyncio.Task[Any]:
        self.fired.append(Fired(payload, description, context))
        return self.hass.async_create_task(asyncio.sleep(0))

    def not_triggered(
        self, info: NotTriggeredInfo, context: Context | None = None
    ) -> None:
        self.reports.append(info)

    @property
    def reasons(self) -> list[str]:
        return [info.reason for info in self.reports]


async def setup_integration(hass: HomeAssistant) -> MockConfigEntry:
    """Add and set up the config entry the triggers need."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def attach(
    hass: HomeAssistant,
    key: str,
    entity_id: str | list[str] | None = None,
    *,
    target: dict[str, Any] | None = None,
    **options: Any,
) -> Recorder:
    """Validate and attach a trigger the way an automation would.

    Sets up the config entry first if it isn't yet.
    """
    if get_store(hass) is None:
        await setup_integration(hass)
    if target is None:
        target = {"entity_id": entity_id}
    cls = TRIGGERS[key]
    config = await cls.async_validate_complete_config(
        hass, {"platform": f"{DOMAIN}.{key}", "target": target, "options": options}
    )
    trigger = cls(
        hass,
        TriggerConfig(
            key=f"{DOMAIN}.{key}",
            target=config.get("target"),
            options=config.get("options"),
        ),
    )
    recorder = Recorder(hass)
    recorder.detach = await trigger.async_attach_runner(
        recorder.run, recorder.not_triggered
    )
    return recorder


async def advance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    """Move the clock forward and let timers run."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def set_state(
    hass: HomeAssistant, entity_id: str, state: str, attributes: dict | None = None
) -> None:
    """Set a state and let every listener run."""
    hass.states.async_set(entity_id, state, attributes)
    await hass.async_block_till_done()


async def remove_state(hass: HomeAssistant, entity_id: str) -> None:
    """Remove an entity from the state machine, as a template reload does."""
    hass.states.async_remove(entity_id)
    await hass.async_block_till_done()
