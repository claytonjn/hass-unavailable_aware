"""`unavailable_aware.numeric_state`: crossing a threshold, minus the bounces."""

from __future__ import annotations

from typing import Any, override

from homeassistant.const import (
    CONF_ABOVE,
    CONF_ATTRIBUTE,
    CONF_BELOW,
    CONF_VALUE_TEMPLATE,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ConditionError
from homeassistant.helpers import condition, config_validation as cv
from homeassistant.helpers.template import Template
from homeassistant.helpers.trigger import TriggerConfig
import voluptuous as vol

from .base import SHARED_OPTIONS, GapAwareTrigger

_NUMERIC_OPTIONS: dict[vol.Marker, Any] = {
    vol.Optional(CONF_ATTRIBUTE): cv.string,
    vol.Optional(CONF_ABOVE): cv.NUMERIC_STATE_THRESHOLD_SCHEMA,
    vol.Optional(CONF_BELOW): cv.NUMERIC_STATE_THRESHOLD_SCHEMA,
    vol.Optional(CONF_VALUE_TEMPLATE): cv.template,
    **SHARED_OPTIONS,
}


def _above_below(value: dict[str, Any]) -> dict[str, Any]:
    """Reject an invalid range."""
    above = value.get(CONF_ABOVE)
    below = value.get(CONF_BELOW)
    if isinstance(above, float) and isinstance(below, float) and above > below:
        raise vol.Invalid(
            f"A value can never be above {above} and below {below} at the same "
            "time. You probably want two different triggers."
        )
    return value


_validate_options = vol.All(
    vol.Schema(_NUMERIC_OPTIONS),
    cv.has_at_least_one_key(CONF_ABOVE, CONF_BELOW),
    _above_below,
)


class NumericStateTrigger(GapAwareTrigger):
    """Fires when a value enters a range, compared to the state before a gap.

    20.1, then a gap, then 20.3 against `below: 20` is not a crossing: the
    value was already out of range before the gap and still is.
    """

    options_schema_dict = _NUMERIC_OPTIONS
    options_validator = staticmethod(_validate_options)
    description_prefix = "numeric state"

    def __init__(self, hass: HomeAssistant, config: TriggerConfig) -> None:
        """Initialize the trigger."""
        super().__init__(hass, config)
        self._above: float | str | None = self._options.get(CONF_ABOVE)
        self._below: float | str | None = self._options.get(CONF_BELOW)
        self._value_template: Template | None = self._options.get(CONF_VALUE_TEMPLATE)
        if self._value_template is not None:
            self._value_template.hass = hass

    def in_range(self, state: State | None) -> bool:
        """Return whether a state is inside the range."""
        if state is None:
            return False
        try:
            return condition.async_numeric_state(
                self._hass,
                state,
                self._below,
                self._above,
                self._value_template,
                {"trigger": {"entity_id": state.entity_id}},
                self.attribute,
            )
        except ConditionError:
            return False

    @override
    def qualifies(
        self, previous: State | None, new: State, *, across_gap: bool, restored: bool
    ) -> bool:
        return not self.in_range(previous) and self.in_range(new)

    @override
    def hold_value(self, state: State) -> Any:
        return None

    @override
    def holds(self, hold: Any, state: State) -> bool:
        return self.in_range(state)

    @override
    def native_would_fire(
        self, old: State | None, new: State | None, *, last_seen: State | None = None
    ) -> bool:
        # Home Assistant's numeric state trigger fires on entering the range
        # from outside it, and skips removals: an entity added again is
        # judged against the last state it saw, and one it never saw is not
        # outside the range yet.
        if not self.in_range(new):
            return False
        if old is not None:
            return not self.in_range(old)
        return last_seen is not None and not self.in_range(last_seen)

    @override
    def summary(self, state: State | None) -> Any:
        if state is None:
            return None
        if self.attribute is None:
            return state.state
        return state.attributes.get(self.attribute)

    @override
    def extra_payload(self) -> dict[str, Any]:
        return {"above": self._above, "below": self._below}
