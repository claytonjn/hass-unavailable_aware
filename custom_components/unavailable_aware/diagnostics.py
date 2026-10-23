"""Diagnostics: what the store remembers right now."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .store import get_store


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the config entry."""
    store = get_store(hass)
    return {"store": None if store is None else store.async_diagnostics()}
