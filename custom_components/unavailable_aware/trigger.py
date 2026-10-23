"""Trigger platform for Unavailable-Aware Triggers."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.trigger import Trigger

from .triggers.numeric_state import NumericStateTrigger
from .triggers.state import StateTrigger

TRIGGERS: dict[str, type[Trigger]] = {
    "state": StateTrigger,
    "numeric_state": NumericStateTrigger,
}


async def async_get_triggers(hass: HomeAssistant) -> dict[str, type[Trigger]]:
    """Return the triggers provided by this integration."""
    return TRIGGERS
