"""`unavailable_aware.state`: Home Assistant's state trigger, minus the bounces."""

from __future__ import annotations

from typing import Any, override

from homeassistant.const import (
    CONF_ATTRIBUTE,
    MATCH_ALL,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import process_state_match
from homeassistant.helpers.trigger import TriggerConfig
import voluptuous as vol

from .base import SHARED_OPTIONS, GapAwareTrigger, values_equal

CONF_FROM = "from"
CONF_TO = "to"
CONF_NOT_FROM = "not_from"
CONF_NOT_TO = "not_to"

_NOT_A_VALUE = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})


def _options(values: Any) -> dict[vol.Marker, Any]:
    return {
        vol.Optional(CONF_ATTRIBUTE): cv.string,
        # Strings on purpose for states, as Home Assistant does: it catches
        # YAML turning `on` into a boolean.
        vol.Exclusive(CONF_FROM, CONF_FROM): values,
        vol.Exclusive(CONF_NOT_FROM, CONF_FROM): values,
        vol.Exclusive(CONF_TO, CONF_TO): values,
        vol.Exclusive(CONF_NOT_TO, CONF_TO): values,
        **SHARED_OPTIONS,
    }


_STATE_OPTIONS = _options(vol.Any(str, [str], None))
_ATTRIBUTE_OPTIONS = _options(cv.match_all)


def _no_gap_states(options: dict[str, Any]) -> dict[str, Any]:
    """Reject `from` or `to` values that are gap states.

    This trigger never fires into or out of a gap, so a trigger on
    `unavailable` would load and then never fire. To react to unavailability,
    use Home Assistant's own state trigger.
    """
    if CONF_ATTRIBUTE in options:
        return options
    for key in (CONF_FROM, CONF_TO):
        named = options.get(key)
        named = {named} if isinstance(named, str) else set(named or ())
        if gaps := named & _NOT_A_VALUE:
            raise vol.Invalid(
                f"{key} names {', '.join(sorted(gaps))}: this trigger never fires "
                "into or out of a gap. Use Home Assistant's own state trigger to "
                "react to an entity becoming unavailable or unknown"
            )
    return options


def _validate_options(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise vol.Invalid("Expected a dictionary")
    schema = _ATTRIBUTE_OPTIONS if CONF_ATTRIBUTE in value else _STATE_OPTIONS
    return _no_gap_states(vol.Schema(schema)(value))


class StateTrigger(GapAwareTrigger):
    """Fires on state (or attribute) changes, seeing through gaps."""

    options_schema_dict = _STATE_OPTIONS
    options_validator = staticmethod(_validate_options)
    description_prefix = "state"

    def __init__(self, hass: HomeAssistant, config: TriggerConfig) -> None:
        """Initialize the trigger."""
        super().__init__(hass, config)
        options = self._options
        if (from_value := options.get(CONF_FROM)) is not None:
            self._match_from = process_state_match(from_value)
        elif (not_from := options.get(CONF_NOT_FROM)) is not None:
            self._match_from = process_state_match(not_from, invert=True)
        else:
            self._match_from = process_state_match(MATCH_ALL)

        if (to_value := options.get(CONF_TO)) is not None:
            self._match_to = process_state_match(to_value)
        elif (not_to := options.get(CONF_NOT_TO)) is not None:
            self._match_to = process_state_match(not_to, invert=True)
        else:
            self._match_to = process_state_match(MATCH_ALL)

        # Without from or to, Home Assistant fires on every change, attribute
        # changes included.
        self._match_all = not any(
            key in options for key in (CONF_FROM, CONF_NOT_FROM, CONF_TO, CONF_NOT_TO)
        )

    def value(self, state: State | None) -> Any:
        """Return what is being followed."""
        if state is None:
            return None
        if self.attribute is None:
            return state.state
        return state.attributes.get(self.attribute)

    @override
    def qualifies(
        self, previous: State | None, new: State, *, across_gap: bool, restored: bool
    ) -> bool:
        old_value = self.value(previous)
        new_value = self.value(new)
        # Across a gap, only the followed value counts. Attributes changing on
        # reconnect aren't a change.
        if (
            self.attribute is not None or across_gap or not self._match_all
        ) and values_equal(old_value, new_value, restored=restored):
            return False
        return bool(self._match_from(old_value) and self._match_to(new_value))

    @override
    def hold_value(self, state: State) -> Any:
        return self.value(state)

    @override
    def holds(self, hold: Any, state: State) -> bool:
        return bool(self.value(state) == hold)

    @override
    def native_would_fire(
        self, old: State | None, new: State | None, *, last_seen: State | None = None
    ) -> bool:
        # Home Assistant's state trigger reads an entity added again as a
        # change from nothing, so `last_seen` does not matter here.
        old_value = self.value(old)
        new_value = self.value(new)
        if self.attribute is not None and old_value == new_value:
            return False
        return bool(
            self._match_from(old_value)
            and self._match_to(new_value)
            and (self._match_all or old_value != new_value)
        )

    @override
    def summary(self, state: State | None) -> Any:
        return self.value(state)
