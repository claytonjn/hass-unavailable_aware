"""Keeps the last valid state of each entity the triggers watch.

When an entity comes back from a gap, Home Assistant reports its old state as
`unavailable`, `unknown`, or nothing at all, so the state from before the gap
is lost. This store keeps it, and saves it to disk so it survives a restart.

The store records every state change of a watched entity, works out what the
change means (an ordinary change, the start of a gap, or the end of one), and
passes it to the triggers that use that entity.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
import logging
from typing import Any

from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Context,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import (
    RETENTION,
    SAVE_DELAY,
    STARTUP_RETENTION,
    STORAGE_KEY,
    STORAGE_VERSION,
    GapKind,
)

_LOGGER = logging.getLogger(__name__)

DATA_STORE: HassKey[AwareStore] = HassKey(f"{STORAGE_KEY}_store")

type Subscriber = Callable[[Change], None]


class Phase(StrEnum):
    """Where a change leaves the entity, from the store's point of view."""

    VALID = "valid"  # valid before and after
    GAP_START = "gap_start"  # valid before, a gap now
    IN_GAP = "in_gap"  # a gap before and after
    GAP_END = "gap_end"  # a gap before, valid now


@dataclass(frozen=True, slots=True)
class Gap:
    """A stretch of time during which the entity's state is not useful."""

    kind: GapKind
    started: datetime
    ended: datetime

    @property
    def duration(self) -> timedelta:
        """Return how long the gap lasted."""
        return self.ended - self.started


@dataclass(frozen=True, slots=True)
class Change:
    """One state change, with what the store knows about it."""

    entity_id: str
    phase: Phase
    # What Home Assistant reported.
    old_state: State | None
    new_state: State | None
    context: Context | None
    # The last valid state before this change. For an ordinary change this is
    # the old state; at the end of a gap it is the state before the gap, or
    # None when that is not known.
    previous: State | None
    # Set at the end of a gap.
    gap: Gap | None = None
    # The previous state came from before a restart, read back from disk.
    restored: bool = False


def gap_kind(state: State | None) -> GapKind | None:
    """Return the cause of a gap."""
    if state is None:
        return GapKind.REMOVED
    if state.state == STATE_UNAVAILABLE:
        return GapKind.UNAVAILABLE
    if state.state == STATE_UNKNOWN:
        return GapKind.UNKNOWN
    return None


@dataclass(slots=True, kw_only=True)
class _Entry:
    """What the store knows about one entity."""

    entity_id: str
    previous: State | None = None
    gap_kind: GapKind | None = None
    gap_started: datetime | None = None
    restored: bool = False
    # Loaded from disk and not yet used by a trigger. Nothing is tracking its
    # state yet, so it's checked against the current state once a trigger is.
    dormant: bool = False
    subscribers: list[Subscriber] = field(default_factory=list)
    release: CALLBACK_TYPE | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the entry as it is written to disk."""
        return {
            "entity_id": self.entity_id,
            "previous": None if self.previous is None else self.previous.as_dict(),
            "gap_kind": self.gap_kind,
            "gap_started": (
                None if self.gap_started is None else self.gap_started.isoformat()
            ),
        }

    def as_diagnostics(self) -> dict[str, Any]:
        """Return the entry for diagnostics."""
        return {
            **self.as_dict(),
            "restored": self.restored,
            "dormant": self.dormant,
            "subscribers": len(self.subscribers),
            "releasing": self.release is not None,
        }


@callback
def get_store(hass: HomeAssistant) -> AwareStore | None:
    """Return the store, or None when the config entry is not loaded."""
    return hass.data.get(DATA_STORE)


async def async_remove_storage(hass: HomeAssistant) -> None:
    """Delete the stored states from disk."""
    await Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True).async_remove()


class AwareStore:
    """Keeps the last valid state of every watched entity."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the store."""
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True
        )
        self._entries: dict[str, _Entry] = {}
        self._listeners: dict[str, CALLBACK_TYPE] = {}
        self._load_task: Any = None
        self._frozen = False
        self._save_pending = False
        self._unsubs: list[CALLBACK_TYPE] = []
        self._unsub_stop: CALLBACK_TYPE | None = None

    async def async_load(self) -> None:
        """Load the store from disk once."""
        if self._load_task is None:
            self._load_task = self._hass.async_create_task(
                self._async_load(), "unavailable_aware store load", eager_start=True
            )
        await self._load_task

    async def _async_load(self) -> None:
        """Read the entries written before the last shutdown."""
        try:
            data = await self._store.async_load()
        except Exception:
            _LOGGER.warning("Could not read the stored states; starting empty")
            data = None

        if data:
            saved_at = dt_util.parse_datetime(data.get("saved_at") or "")
            for raw in data.get("entries", []):
                try:
                    entry = self._entry_from_disk(raw, saved_at)
                except KeyError, TypeError, ValueError:
                    continue
                if entry is not None:
                    self._entries[entry.entity_id] = entry

        self._unsub_stop = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_stop
        )
        self._unsubs.append(
            self._hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_updated
            )
        )
        self._unsubs.append(async_at_started(self._hass, self._schedule_startup_purge))

    @staticmethod
    def _entry_from_disk(
        raw: dict[str, Any], saved_at: datetime | None
    ) -> _Entry | None:
        """Load one entity's record from the saved store data.

        Anything on disk was saved before a restart, so every entry comes back
        in a restart gap. The gap starts when the entity became unavailable if
        it already was before shutdown, or otherwise when the store was last
        saved. Returns None if neither time is known.
        """
        previous = State.from_dict(raw["previous"]) if raw.get("previous") else None
        started = (
            dt_util.parse_datetime(raw["gap_started"])
            if raw.get("gap_started")
            else None
        ) or saved_at
        if started is None:
            return None
        return _Entry(
            entity_id=raw["entity_id"],
            previous=previous,
            gap_kind=GapKind.RESTART,
            gap_started=started,
            restored=previous is not None,
            dormant=True,
        )

    @callback
    def _schedule_startup_purge(self, _hass: HomeAssistant) -> None:
        """Shortly after startup, drop entities not used in any aware triggers."""

        @callback
        def purge(_now: datetime) -> None:
            for key in [k for k, e in self._entries.items() if e.dormant]:
                del self._entries[key]
            self._schedule_save()

        self._unsubs.append(async_call_later(self._hass, STARTUP_RETENTION, purge))

    async def _async_stop(self, _event: Event) -> None:
        """Write everything out before Home Assistant stops.

        From here on, the store stops listening. Integrations unloading make
        their entities unavailable, which isn't worth recording.
        """
        self._unsub_stop = None
        await self.async_unload()

    async def async_unload(self) -> None:
        """Stop listening and write everything out."""
        if self._frozen:
            return
        self._frozen = True
        data = self._data_to_save()
        self.async_shutdown()
        await self._store.async_save(data)

    @callback
    def async_shutdown(self) -> None:
        """Stop every listener and timer."""
        if self._unsub_stop is not None:
            self._unsub_stop()
            self._unsub_stop = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for unsub in self._listeners.values():
            unsub()
        self._listeners.clear()
        for entry in self._entries.values():
            if entry.release is not None:
                entry.release()
                entry.release = None

    # Subscriptions

    @callback
    def async_subscribe(self, entity_id: str, subscriber: Subscriber) -> CALLBACK_TYPE:
        """Call `subscriber` with every change to one entity.

        Returns a function that stops the calls. Once the last subscriber for
        an entity is gone, the store keeps tracking it for `RETENTION` so a
        reloaded trigger can pick up where the old one left off.
        """
        entry = self._track(entity_id)
        entry.subscribers.append(subscriber)

        @callback
        def unsubscribe() -> None:
            if subscriber in entry.subscribers:
                entry.subscribers.remove(subscriber)
            if not entry.subscribers and self._entries.get(entity_id) is entry:
                self._start_release(entity_id, entry)

        return unsubscribe

    @callback
    def _track(self, entity_id: str) -> _Entry:
        """Get or create an entity's record and start tracking its state.

        A new record starts from the entity's current state. A record loaded
        from disk is refreshed if the entity has come back since. Either way,
        the store subscribes to the entity's state changes.
        """
        entry = self._entries.get(entity_id)

        if entry is not None and entry.release is not None:
            # Still tracked within `RETENTION`, for example during a reload.
            entry.release()
            entry.release = None
            return entry

        current = self._hass.states.get(entity_id)
        kind = gap_kind(current)

        if entry is None:
            entry = self._entries[entity_id] = _Entry(
                entity_id=entity_id,
                previous=current if kind is None else None,
                gap_kind=kind,
                gap_started=None if kind is None else dt_util.utcnow(),
            )
        elif entry.dormant:
            entry.dormant = False
            if kind is None:
                # The entity came back before any trigger used it, so there's no
                # gap left to bridge. Start from its current state.
                entry.previous = current
                entry.gap_kind = None
                entry.gap_started = None
                entry.restored = False

        self._listen(entity_id)
        self._schedule_save()
        return entry

    @callback
    def _start_release(self, entity_id: str, entry: _Entry) -> None:
        """Keep listening a while after the last trigger was removed."""
        if self._frozen:
            return

        @callback
        def release(_now: datetime) -> None:
            entry.release = None
            if self._entries.get(entity_id) is entry and not entry.subscribers:
                del self._entries[entity_id]
                if (unsub := self._listeners.pop(entity_id, None)) is not None:
                    unsub()
                self._schedule_save()

        entry.release = async_call_later(self._hass, RETENTION, release)

    @callback
    def _listen(self, entity_id: str) -> None:
        """Make sure state changes of the entity come here."""
        if entity_id not in self._listeners:
            self._listeners[entity_id] = async_track_state_change_event(
                self._hass, [entity_id], self._state_changed
            )

    # Events

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Determine what a change means and inform triggers."""
        if self._frozen:
            return
        entity_id = event.data["entity_id"]
        if (entry := self._entries.get(entity_id)) is None or entry.dormant:
            return
        change = self._process(entry, event)
        if change.phase is not Phase.IN_GAP:
            self._schedule_save()

        for subscriber in list(entry.subscribers):
            try:
                subscriber(change)
            except Exception:
                _LOGGER.exception("Error handling a change of %s", entity_id)

    def _process(self, entry: _Entry, event: Event[EventStateChangedData]) -> Change:
        """Update one entry for a change."""
        old = event.data["old_state"]
        new = event.data["new_state"]
        now = event.time_fired
        new_kind = gap_kind(new)

        def change(phase: Phase, previous: State | None, **kwargs: Any) -> Change:
            return Change(
                entity_id=entry.entity_id,
                phase=phase,
                old_state=old,
                new_state=new,
                context=event.context,
                previous=previous,
                **kwargs,
            )

        if entry.gap_kind is None:
            previous = entry.previous if entry.previous is not None else old
            if new_kind is None:
                entry.previous = new
                return change(Phase.VALID, previous)
            entry.gap_kind = new_kind
            entry.gap_started = now
            return change(Phase.GAP_START, previous)

        if new_kind is not None:
            return change(Phase.IN_GAP, entry.previous)

        assert entry.gap_started is not None
        gap = Gap(kind=entry.gap_kind, started=entry.gap_started, ended=now)
        previous = entry.previous
        restored = entry.restored

        entry.previous = new
        entry.gap_kind = None
        entry.gap_started = None
        entry.restored = False
        return change(Phase.GAP_END, previous, gap=gap, restored=restored)

    @callback
    def _registry_updated(
        self, event: Event[er.EventEntityRegistryUpdatedData]
    ) -> None:
        """Forget entities removed from the registry.

        A device paired again later may get the same entity ID, and probably
        shouldn't be compared with its old state.
        """
        data = event.data
        if data["action"] == "remove":
            gone = data["entity_id"]
        elif data["action"] == "update" and "old_entity_id" in data:
            gone = data["old_entity_id"]
        else:
            return
        entry = self._entries.get(gone)
        if entry is not None and entry.previous is not None:
            entry.previous = None
            entry.restored = False
            self._schedule_save()

    # Persistence

    @callback
    def _schedule_save(self) -> None:
        """Write entries to disk a little later.

        Only one write is scheduled at a time. The store's own delay slides
        with every call, and an entity that changes every few seconds would
        otherwise keep pushing the write off until shutdown.
        """
        if self._frozen or self._save_pending:
            return
        self._save_pending = True
        self._store.async_delay_save(self._data_for_delayed_save, SAVE_DELAY)

    @callback
    def _data_for_delayed_save(self) -> dict[str, Any]:
        """Return the data for a scheduled write, as of the moment it's written."""
        self._save_pending = False
        return self._data_to_save()

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        """Return everything worth remembering across a restart."""
        return {
            "saved_at": dt_util.utcnow().isoformat(),
            "entries": [entry.as_dict() for entry in self._entries.values()],
        }

    @callback
    def async_diagnostics(self) -> dict[str, Any]:
        """Return the store's contents for diagnostics."""
        return {
            "listening": sorted(self._listeners),
            "entries": [entry.as_diagnostics() for entry in self._entries.values()],
        }
