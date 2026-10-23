"""Shared by all triggers: following entities through their gaps.

A subclass determines what is being followed and when a change counts. This
base class takes changes from the store, bridges or ignores gaps, waits out
`for`, and reports why it didn't fire.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from typing import Any, ClassVar, cast, override

from homeassistant.const import (
    CONF_ATTRIBUTE,
    CONF_ENTITY_ID,
    CONF_FOR,
    CONF_OPTIONS,
    CONF_TARGET,
)
from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.automation import move_top_level_schema_fields_to_options
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.json import json_dumps
from homeassistant.helpers.target import TargetEntityChangeTracker, TargetSelection
from homeassistant.helpers.trigger import (
    NotTriggeredInfo,
    Trigger,
    TriggerActionRunner,
    TriggerConfig,
    TriggerNotTriggeredReporter,
)
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from ..const import (
    CONF_BRIDGE_RESTARTS_FOR_TIMER,
    CONF_BRIDGE_UP_TO,
    CONF_LONGER_GAPS,
    DEFAULT_BRIDGE_UP_TO,
    SIGNAL_STORE_LOADED,
    SIGNAL_STORE_UNLOADED,
    GapKind,
    LongerGaps,
)
from ..dependents import async_check_soon, async_update_issue
from ..store import AwareStore, Change, Gap, Phase, get_store

SHARED_OPTIONS: dict[vol.Marker, Any] = {
    vol.Optional(CONF_FOR): cv.positive_time_period,
    vol.Optional(
        CONF_BRIDGE_UP_TO, default=DEFAULT_BRIDGE_UP_TO
    ): cv.positive_time_period,
    vol.Optional(CONF_LONGER_GAPS, default=LongerGaps.IGNORE.value): vol.In(
        [policy.value for policy in LongerGaps]
    ),
    vol.Optional(CONF_BRIDGE_RESTARTS_FOR_TIMER, default=False): cv.boolean,
}

# Reasons reported to the automation trace when a change was evaluated but
# did not fire, in the cases where Home Assistant's own trigger would have.
REASON_ENTERED_GAP = "entered_gap"
REASON_BRIDGED_GAP = "bridged_gap"
REASON_LONGER_GAP_IGNORED = "longer_gap_ignored"
REASON_PREVIOUS_UNKNOWN = "previous_unknown"
# Internal: the return was handled (fired, or a `for` wait carries on).
_HANDLED = "handled"

_TARGET_SCHEMA = vol.Schema(cv.TARGET_FIELDS)


def _watchable_target(value: Any) -> ConfigType:
    """Validate the trigger's target and reject if it's empty.

    The target must include at least one entity, device, area, floor, or label.
    """
    target: ConfigType = _TARGET_SCHEMA(value)
    if not TargetSelection(target).has_any_target:
        raise vol.Invalid(
            "The target must name at least one entity, device, area, floor, or label"
        )
    return target


def _normalized(value: Any) -> Any:
    """Round-trip a value through JSON so it has the types it would after a restart.

    For example, a tuple becomes a list and a datetime becomes a string.
    """
    try:
        return json.loads(json_dumps(value))
    except TypeError, ValueError:
        return value


@dataclass(slots=True, kw_only=True)
class _Waiting:
    """A change that matches and now has to hold for `for`."""

    hold: Any
    payload: dict[str, Any]
    context: Context | None
    timer: CALLBACK_TYPE | None = None
    # Inside a gap right now; whether it held is decided on return.
    away: bool = False
    # `for` ran out while away: fire on return, if it held.
    due: bool = False

    def cancel(self) -> None:
        """Stop the timer."""
        if self.timer is not None:
            self.timer()
            self.timer = None


@dataclass(slots=True)
class _Followed:
    """What an `_AttachedTrigger` keeps for each entity its trigger is watching."""

    waiting: _Waiting | None = None
    unsubscribe: CALLBACK_TYPE | None = None
    # Home Assistant's own trigger would have fired going into the gap.
    entered_suppressed: bool = False

    def cancel(self) -> None:
        """Drop everything pending."""
        if self.waiting is not None:
            self.waiting.cancel()
            self.waiting = None


class _Targets(TargetEntityChangeTracker):
    """Keep the set of targeted entities up to date."""

    def __init__(
        self,
        hass: HomeAssistant,
        selection: TargetSelection,
        on_update: Callable[[set[str]], None],
    ) -> None:
        super().__init__(hass, selection, entity_filter=lambda ids: ids)
        self._on_update = on_update

    @callback
    def _handle_entities_update(self, tracked_entities: set[str]) -> None:
        self._on_update(tracked_entities)


class GapAwareTrigger(Trigger):
    """A trigger that sees through unavailable, unknown, and removed states."""

    options_schema_dict: ClassVar[dict[vol.Marker, Any]]
    # Validates the options dict (a voluptuous validator).
    options_validator: ClassVar[Any]
    description_prefix: ClassVar[str]

    _options: dict[str, Any]
    _target: ConfigType

    @classmethod
    @override
    async def async_validate_complete_config(
        cls, hass: HomeAssistant, complete_config: ConfigType
    ) -> ConfigType:
        """Accept Home Assistant's own trigger syntax as well.

        `entity_id`, `to`, `from` and the rest at the top level, as written for
        `trigger: state`, are moved into `target` and `options`. Converting an
        automation is then a matter of changing the trigger's name.
        """
        async_check_soon(hass)
        if CONF_OPTIONS not in complete_config and CONF_TARGET not in complete_config:
            complete_config = dict(complete_config)
            entity_ids = complete_config.pop(CONF_ENTITY_ID, None)
            complete_config = move_top_level_schema_fields_to_options(
                complete_config, cls.options_schema_dict
            )
            if entity_ids is not None:
                registry = er.async_get(hass)
                complete_config[CONF_TARGET] = {
                    CONF_ENTITY_ID: er.async_validate_entity_ids(
                        registry, cv.entity_ids_or_uuids(entity_ids)
                    )
                }
        return await super().async_validate_complete_config(hass, complete_config)

    @classmethod
    @override
    async def async_validate_config(
        cls, hass: HomeAssistant, config: ConfigType
    ) -> ConfigType:
        """Validate the trigger config."""
        schema = vol.Schema(
            {
                vol.Required(CONF_TARGET): _watchable_target,
                vol.Optional(CONF_OPTIONS, default=dict): cls.options_validator,
            }
        )
        return cast(ConfigType, schema(config))

    def __init__(self, hass: HomeAssistant, config: TriggerConfig) -> None:
        """Initialize the trigger."""
        super().__init__(hass, config)
        self._options = dict(config.options or {})
        self._target = config.target or {}
        self.attribute: str | None = self._options.get(CONF_ATTRIBUTE)
        self.duration: timedelta | None = self._options.get(CONF_FOR) or None
        self.bridge_up_to: timedelta = self._options.get(
            CONF_BRIDGE_UP_TO, DEFAULT_BRIDGE_UP_TO
        )
        self.longer_gaps = LongerGaps(
            self._options.get(CONF_LONGER_GAPS, LongerGaps.IGNORE)
        )
        self.bridge_restarts_for_timer: bool = self._options.get(
            CONF_BRIDGE_RESTARTS_FOR_TIMER, False
        )

    # What a subclass decides

    @abstractmethod
    def qualifies(
        self, previous: State | None, new: State, *, across_gap: bool, restored: bool
    ) -> bool:
        """Return whether going from previous to new fires (durations aside).

        `previous` is None when the state before a gap is not known.
        """

    @abstractmethod
    def hold_value(self, state: State) -> Any:
        """Return what has to stay the same while waiting out `for`."""

    @abstractmethod
    def holds(self, hold: Any, state: State) -> bool:
        """Return whether a state still holds what `for` is waiting on."""

    @abstractmethod
    def native_would_fire(
        self, old: State | None, new: State | None, *, last_seen: State | None = None
    ) -> bool:
        """Return whether Home Assistant's own trigger fires for this change.

        When `old` is None (the entity was added again), `last_seen` is the
        last state Home Assistant's trigger saw before it was removed, or None
        when it never saw one.
        """

    @abstractmethod
    def summary(self, state: State | None) -> Any:
        """Return the followed value of a state, for a trace."""

    def extra_payload(self) -> dict[str, Any]:
        """Return trigger variables added by a subclass."""
        return {}

    @override
    async def async_attach_runner(
        self,
        run_action: TriggerActionRunner,
        did_not_trigger: TriggerNotTriggeredReporter | None = None,
    ) -> CALLBACK_TYPE:
        """Attach the trigger to an action runner.

        Only while the config entry is loaded: the triggers depend on it, so
        removing the integration removes everything it stored.
        """
        if (store := get_store(self._hass)) is None:
            async_update_issue(self._hass)
            raise HomeAssistantError(
                "Unavailable-Aware Triggers isn't set up. Add it under "
                "Settings > Devices & services to use its triggers"
            )
        attached = _AttachedTrigger(self, store, run_action, did_not_trigger)
        targets = _Targets(self._hass, TargetSelection(self._target), attached.retarget)
        unsubscribe_targets = await targets.async_setup()
        unsubscribe_signals = [
            async_dispatcher_connect(
                self._hass, SIGNAL_STORE_UNLOADED, attached.store_unloaded
            ),
            async_dispatcher_connect(
                self._hass, SIGNAL_STORE_LOADED, attached.store_loaded
            ),
        ]

        @callback
        def detach() -> None:
            for unsubscribe in unsubscribe_signals:
                unsubscribe()
            unsubscribe_targets()
            attached.detach()

        return detach


class _AttachedTrigger:
    """One attachment of a trigger: its entities, waits, and reports."""

    def __init__(
        self,
        trigger: GapAwareTrigger,
        store: AwareStore,
        run_action: TriggerActionRunner,
        did_not_trigger: TriggerNotTriggeredReporter | None,
    ) -> None:
        self._trigger = trigger
        self._hass = trigger._hass
        # None while the config entry is unloaded (reloading or disabled).
        self._store: AwareStore | None = store
        self._run_action = run_action
        self._did_not_trigger = did_not_trigger
        self._targeted: set[str] = set()
        self._followed: dict[str, _Followed] = {}

    @callback
    def retarget(self, entity_ids: set[str]) -> None:
        """Follow exactly the entities the target covers now."""
        self._targeted = set(entity_ids)
        for entity_id in set(self._followed) - entity_ids:
            self._drop(entity_id)
        if self._store is None:
            return
        for entity_id in entity_ids - set(self._followed):
            followed = self._followed[entity_id] = _Followed()
            followed.unsubscribe = self._store.async_subscribe(
                entity_id, self._on_change
            )

    @callback
    def store_unloaded(self) -> None:
        """Pause while the config entry is unloaded.

        Pending `for` waits are dropped: nothing is watching until the entry
        is back, so they cannot be known to hold.
        """
        self.detach()
        self._store = None

    @callback
    def store_loaded(self, store: AwareStore) -> None:
        """Resume with the store from the config entry that was set up again."""
        self._store = store
        self.retarget(self._targeted)

    def _drop(self, entity_id: str) -> None:
        followed = self._followed.pop(entity_id)
        followed.cancel()
        if followed.unsubscribe is not None:
            followed.unsubscribe()

    @callback
    def detach(self) -> None:
        """Stop following everything."""
        for entity_id in list(self._followed):
            self._drop(entity_id)

    # Changes

    @callback
    def _on_change(self, change: Change) -> None:
        followed = self._followed.get(change.entity_id)
        if followed is None:
            return

        match change.phase:
            case Phase.VALID:
                self._valid(followed, change)
            case Phase.GAP_START:
                self._gap_started(followed, change)
            case Phase.GAP_END:
                assert change.gap is not None and change.new_state is not None
                self._gap_ended(
                    followed,
                    previous=change.previous,
                    new=change.new_state,
                    gap=change.gap,
                    context=change.context,
                    raw_old=change.old_state,
                    restored=change.restored,
                )
            case Phase.IN_GAP:
                pass

    def _valid(self, followed: _Followed, change: Change) -> None:
        trigger = self._trigger
        new = change.new_state
        assert new is not None
        if (waiting := followed.waiting) is not None and not trigger.holds(
            waiting.hold, new
        ):
            waiting.cancel()
            followed.waiting = None

        if trigger.qualifies(change.previous, new, across_gap=False, restored=False):
            self._qualified(
                followed,
                previous=change.previous,
                new=new,
                context=change.context,
                raw_old=change.old_state,
                gap=None,
            )

    def _gap_started(self, followed: _Followed, change: Change) -> None:
        """Handle an entity becoming unavailable, unknown, or removed.

        This doesn't count as a change away from its last state, so nothing
        fires and a pending `for` wait keeps running (unless
        `bridge_restarts_for_timer` is on). If Home Assistant's own trigger
        would have fired, that's noted for the trace when the gap ends.
        """
        if (waiting := followed.waiting) is not None:
            waiting.away = True
            if self._trigger.bridge_restarts_for_timer:
                # The hold is broken; it starts over if the entity comes back
                # as it was.
                waiting.cancel()
        # Home Assistant's own trigger would have fired here. That goes on
        # record once the gap is over, together with how it ended (one trace
        # per gap).
        if self._trigger.native_would_fire(change.old_state, change.new_state):
            followed.entered_suppressed = True

    def _gap_ended(
        self,
        followed: _Followed,
        *,
        previous: State | None,
        new: State,
        gap: Gap,
        context: Context | None,
        raw_old: State | None,
        restored: bool,
    ) -> None:
        trigger = self._trigger
        gap_vars = _gap_variables(gap)
        entered_suppressed = followed.entered_suppressed
        followed.entered_suppressed = False

        # A restart is measured like any other gap: from when the entity was
        # last heard before shutdown to when it is back.
        last_seen = _last_seen(previous, raw_old, gap, new.entity_id)
        cannot_bridge = None
        if previous is None:
            cannot_bridge = REASON_PREVIOUS_UNKNOWN
        elif gap.duration > trigger.bridge_up_to:
            cannot_bridge = REASON_LONGER_GAP_IGNORED

        if cannot_bridge and trigger.longer_gaps is LongerGaps.HOME_ASSISTANT_DEFAULT:
            reason: str | None = self._as_home_assistant(
                followed,
                new=new,
                context=context,
                raw_old=raw_old,
                last_seen=last_seen,
                gap_vars=gap_vars,
            )
        elif cannot_bridge and self._restart_wait(
            followed, new=new, context=context, raw_old=raw_old, gap_vars=gap_vars
        ):
            reason = _HANDLED
        elif cannot_bridge:
            reason = self._ignore_return(
                followed,
                cannot_bridge,
                previous=previous,
                new=new,
                raw_old=raw_old,
                last_seen=last_seen,
                restored=restored,
            )
        else:
            assert previous is not None  # cannot_bridge is set whenever it's None
            reason = self._bridge(
                followed,
                previous=previous,
                new=new,
                context=context,
                raw_old=raw_old,
                last_seen=last_seen,
                restored=restored,
                gap_vars=gap_vars,
            )

        if reason == _HANDLED:
            return
        if reason is None and entered_suppressed:
            reason = REASON_ENTERED_GAP
        if reason is not None:
            self._report(
                reason,
                new.entity_id,
                context,
                previous=previous,
                new=new,
                raw_old=raw_old,
                gap={
                    **gap_vars,
                    "bridge_up_to": trigger.bridge_up_to.total_seconds(),
                },
                entered_suppressed=entered_suppressed,
            )

    def _restart_wait(
        self,
        followed: _Followed,
        *,
        new: State,
        context: Context | None,
        raw_old: State | None,
        gap_vars: dict[str, Any],
    ) -> bool:
        """Restart a pending `for` wait, if the entity came back still matching.

        For a gap that cannot be bridged, with `longer_gaps: ignore`: the
        change that started the wait was seen before the gap, and firing still
        needs the whole duration to be seen after it. Nothing during the gap is
        assumed. Returns whether a wait was started over.
        """
        waiting = followed.waiting
        if waiting is None:
            return False
        waiting.cancel()
        followed.waiting = None
        if not self._trigger.holds(waiting.hold, new):
            return False
        self._qualified(
            followed,
            previous=waiting.payload["from_state"],
            new=new,
            context=context,
            raw_old=raw_old,
            gap=gap_vars,
        )
        return True

    def _as_home_assistant(
        self,
        followed: _Followed,
        *,
        new: State,
        context: Context | None,
        raw_old: State | None,
        last_seen: State | None,
        gap_vars: dict[str, Any],
    ) -> str | None:
        """Judge a return the way Home Assistant's own trigger does.

        From the raw unavailable, unknown, or removed state, which is reflected
        in `trigger.from_state`. As in Home Assistant, a pending `for`
        wait ended with the gap, and a new one starts when the entity comes
        back, if the change from the raw state qualifies.
        """
        if followed.waiting is not None:
            followed.waiting.cancel()
            followed.waiting = None
        if not self._trigger.native_would_fire(raw_old, new, last_seen=last_seen):
            return None
        self._qualified(
            followed,
            previous=raw_old,
            new=new,
            context=context,
            raw_old=raw_old,
            gap=gap_vars,
        )
        return _HANDLED

    def _ignore_return(
        self,
        followed: _Followed,
        reason: str,
        *,
        previous: State | None,
        new: State,
        raw_old: State | None,
        last_seen: State | None,
        restored: bool,
    ) -> str | None:
        """Do nothing for an entity coming back that cannot be compared.

        Returns the reason when it would otherwise have fired, here or in Home
        Assistant's own trigger; else None.
        """
        trigger = self._trigger
        if (
            previous is not None
            and trigger.qualifies(previous, new, across_gap=True, restored=restored)
        ) or trigger.native_would_fire(raw_old, new, last_seen=last_seen):
            return reason
        return None

    def _bridge(
        self,
        followed: _Followed,
        *,
        previous: State,
        new: State,
        context: Context | None,
        raw_old: State | None,
        last_seen: State | None,
        restored: bool,
        gap_vars: dict[str, Any],
    ) -> str | None:
        """Compare a return with the state before the gap.

        Returns `_HANDLED` when it fired or a `for` wait carries on; else why
        nothing fired, when Home Assistant's own trigger would have fired for
        the return; else None.
        """
        trigger = self._trigger
        if (waiting := followed.waiting) is not None:
            waiting.away = False
            if trigger.holds(waiting.hold, new):
                if not trigger.bridge_restarts_for_timer:
                    # Held right through the gap.
                    if waiting.due:
                        followed.waiting = None
                        self._fire(waiting.payload, waiting.context)
                    return _HANDLED
                # Back as it was: the wait starts over from now.
                waiting.cancel()
                followed.waiting = None
                self._qualified(
                    followed,
                    previous=waiting.payload["from_state"],
                    new=new,
                    context=context,
                    raw_old=raw_old,
                    gap=gap_vars,
                )
                return _HANDLED
            waiting.cancel()
            followed.waiting = None

        if trigger.qualifies(previous, new, across_gap=True, restored=restored):
            self._qualified(
                followed,
                previous=previous,
                new=new,
                context=context,
                raw_old=raw_old,
                gap=gap_vars,
            )
            return _HANDLED
        if trigger.native_would_fire(raw_old, new, last_seen=last_seen):
            return REASON_BRIDGED_GAP
        return None

    # Firing

    def _qualified(
        self,
        followed: _Followed,
        *,
        previous: State | None,
        new: State,
        context: Context | None,
        raw_old: State | None,
        gap: dict[str, Any] | None,
    ) -> None:
        trigger = self._trigger
        payload = {
            "entity_id": new.entity_id,
            "from_state": previous,
            "to_state": new,
            "raw_from_state": raw_old,
            "gap": gap,
            "for": trigger.duration,
            "attribute": trigger.attribute,
            **trigger.extra_payload(),
        }
        if not trigger.duration:
            self._fire(payload, context)
            return

        if followed.waiting is not None:
            followed.waiting.cancel()
        waiting = followed.waiting = _Waiting(
            hold=trigger.hold_value(new), payload=payload, context=context
        )

        @callback
        def held(_now: datetime) -> None:
            waiting.timer = None
            if followed.waiting is not waiting:
                return
            if waiting.away:
                waiting.due = True
                return
            followed.waiting = None
            self._fire(waiting.payload, waiting.context)

        waiting.timer = async_call_later(self._hass, trigger.duration, held)

    def _fire(self, payload: dict[str, Any], context: Context | None) -> None:
        self._run_action(
            payload,
            f"{self._trigger.description_prefix} of {payload['entity_id']}",
            context,
        )

    def _report(
        self,
        reason: str,
        entity_id: str,
        context: Context | None,
        *,
        previous: State | None,
        new: State | None,
        raw_old: State | None,
        gap: dict[str, Any] | None = None,
        entered_suppressed: bool = False,
    ) -> None:
        """Tell the automation trace why a change did not fire."""
        if self._did_not_trigger is None:
            return
        summary = self._trigger.summary
        data: dict[str, Any] = {
            "entity_id": entity_id,
            "from": summary(previous),
            "raw_from": None if raw_old is None else raw_old.state,
            "to": None if new is None else summary(new),
            # Home Assistant's own trigger would also have fired going in.
            "entered_gap_suppressed": entered_suppressed,
        }
        if gap is not None:
            data["gap"] = {
                **gap,
                "started": gap["started"].isoformat(),
                "ended": gap["ended"].isoformat(),
            }
        self._did_not_trigger(NotTriggeredInfo(reason=reason, data=data), context)


def _last_seen(
    previous: State | None, raw_old: State | None, gap: Gap, entity_id: str
) -> State | None:
    """Return the last state Home Assistant's own trigger saw before this one.

    Its triggers skip removals, so for an entity added again that is the state
    before the removal: the valid state when it was removed straight away,
    the gap state when it went unavailable or unknown first, and nothing when
    it did not exist when Home Assistant started.
    """
    if raw_old is not None:
        return raw_old
    if gap.kind is GapKind.REMOVED:
        return previous
    if gap.kind is GapKind.RESTART:
        return None
    return State(entity_id, gap.kind.value)


def _gap_variables(gap: Gap) -> dict[str, Any]:
    """Return `trigger.gap`."""
    return {
        "kind": gap.kind.value,
        "started": gap.started,
        "ended": gap.ended,
        "duration": gap.duration.total_seconds(),
    }


def values_equal(old: Any, new: Any, *, restored: bool) -> bool:
    """Compare an old and new value of what the trigger is watching.

    If the old value was loaded from disk after a restart, both are
    round-tripped through JSON first so a tuple and the list it was saved as
    still compare properly.
    """
    if restored:
        return _normalized(old) == _normalized(new)
    return bool(old == new)
