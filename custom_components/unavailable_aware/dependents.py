"""The repair raised when automations or scripts need the missing integration.

The triggers only work while the config entry is loaded. If it is removed or
disabled while automations or scripts still use them, those stop working, and
this repair lists them. It goes away when the integration is added back or
nothing uses the triggers any more, so an uninstall leaves nothing behind.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HassJob,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.json import json_dumps
from homeassistant.helpers.start import async_at_started
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .store import get_store

ISSUE_NOT_SET_UP = "not_set_up"

# Automations and scripts that changed are re-created, so we need to
# watch their entities.
_WATCHED_DOMAINS = ("automation", "script")
_RECHECK_DELAY = 1  # seconds, to look once after a reload rather than per entity

DATA_WATCH: HassKey[_Watch] = HassKey(f"{DOMAIN}_dependents_watch")
DATA_CHECK_PENDING: HassKey[bool] = HassKey(f"{DOMAIN}_dependents_check_pending")

_TRIGGER_PREFIX = f'"{DOMAIN}.'


@callback
def find_dependents(hass: HomeAssistant) -> list[str]:
    """Return the automations and scripts that use these triggers."""
    found: list[str] = []
    for domain in _WATCHED_DOMAINS:
        if (component := hass.data.get(domain)) is None:
            continue
        for entity in getattr(component, "entities", ()):
            raw_config = getattr(entity, "raw_config", None)
            if raw_config and _TRIGGER_PREFIX in json_dumps(raw_config):
                found.append(
                    f"{entity.name or entity.entity_id} (`{entity.entity_id}`)"
                )
    return sorted(found)


@callback
def async_check_soon(hass: HomeAssistant) -> None:
    """Check for the repair once the config being validated is in use.

    Called whenever a trigger config is validated, which happens for every
    use, `wait_for_trigger` inside an action included. A wait only attaches
    when it runs, so without this an automation using only `wait_for_trigger`
    would not be listed until then.

    At startup the check waits until Home Assistant has started, when the
    config entry is set up if there is one. Later it waits a moment, until
    the automation or script being validated exists.
    """
    if get_store(hass) is not None or hass.data.get(DATA_CHECK_PENDING):
        return
    hass.data[DATA_CHECK_PENDING] = True

    @callback
    def check(*_args: object) -> None:
        hass.data.pop(DATA_CHECK_PENDING, None)
        async_update_issue(hass)

    if hass.is_running:
        async_call_later(hass, _RECHECK_DELAY, HassJob(check, cancel_on_shutdown=True))
    else:
        async_at_started(hass, check)


@callback
def async_update_issue(hass: HomeAssistant) -> None:
    """Raise, update, or clear the repair, as things stand right now."""
    dependents = [] if get_store(hass) is not None else find_dependents(hass)
    if not dependents:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_NOT_SET_UP)
        if (watch := hass.data.pop(DATA_WATCH, None)) is not None:
            watch.stop()
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_NOT_SET_UP,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_NOT_SET_UP,
        translation_placeholders={
            "dependents": "\n".join(f"- {name}" for name in dependents)
        },
    )
    if DATA_WATCH not in hass.data:
        hass.data[DATA_WATCH] = _Watch(hass)


class _Watch:
    """Look again whenever an automation or script is added or removed."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._recheck: CALLBACK_TYPE | None = None
        self._unsubs = [
            hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._changed, event_filter=self._added_or_removed
            ),
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._stopping),
        ]

    @staticmethod
    @callback
    def _added_or_removed(event_data: EventStateChangedData) -> bool:
        return event_data["entity_id"].split(".", 1)[0] in _WATCHED_DOMAINS and (
            event_data["old_state"] is None or event_data["new_state"] is None
        )

    @callback
    def _changed(self, _event: Event[EventStateChangedData]) -> None:
        if self._recheck is None:
            self._recheck = async_call_later(self._hass, _RECHECK_DELAY, self._check)

    @callback
    def _check(self, _now: datetime) -> None:
        self._recheck = None
        async_update_issue(self._hass)

    @callback
    def _stopping(self, _event: Event) -> None:
        self._unsubs.pop()  # the stop listener is gone once it has fired
        self._hass.data.pop(DATA_WATCH, None)
        self.stop()

    @callback
    def stop(self) -> None:
        """Stop watching."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._recheck is not None:
            self._recheck()
            self._recheck = None
