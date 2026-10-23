"""Unavailable-Aware Triggers.

State and numeric state triggers that see through unavailable and unknown: a
device dropping out and coming back unchanged doesn't fire an automation, but
a real change while it was gone still does.

The triggers only work while the config entry is loaded. Removing the
integration deletes everything it stored.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, SIGNAL_STORE_LOADED, SIGNAL_STORE_UNLOADED
from .dependents import async_update_issue
from .store import DATA_STORE, AwareStore, async_remove_storage

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load the remembered states and let the triggers run."""
    store = AwareStore(hass)
    await store.async_load()
    hass.data[DATA_STORE] = store
    async_update_issue(hass)
    async_dispatcher_send(hass, SIGNAL_STORE_LOADED, store)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Pause the triggers and save the remembered states.

    On a reload the triggers pick up again once the entry is set up. When the
    entry is being disabled, anything that still uses the triggers is listed
    in a repair, because it stops working.
    """
    store = hass.data.pop(DATA_STORE, None)
    async_dispatcher_send(hass, SIGNAL_STORE_UNLOADED)
    if store is not None:
        await store.async_unload()
    if entry.disabled_by is not None:
        async_update_issue(hass)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete everything the integration stored.

    Anything that still uses the triggers stops working, and is listed in a
    repair until it no longer does.
    """
    await async_remove_storage(hass)
    async_update_issue(hass)
