"""Constants for Unavailable-Aware Triggers."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Final

DOMAIN: Final = "unavailable_aware"

# Sent when the config entry is set up (with the store) and unloaded.
SIGNAL_STORE_LOADED: Final = f"{DOMAIN}_store_loaded"
SIGNAL_STORE_UNLOADED: Final = f"{DOMAIN}_store_unloaded"

# Options shared by both triggers.
CONF_BRIDGE_UP_TO: Final = "bridge_up_to"
CONF_LONGER_GAPS: Final = "longer_gaps"
CONF_BRIDGE_RESTARTS_FOR_TIMER: Final = "bridge_restarts_for_timer"

DEFAULT_BRIDGE_UP_TO: Final = timedelta(seconds=30)

# How long the store keeps tracking an entity after the last trigger using it
# detaches. Automation reloads detach and re-attach within milliseconds, so
# this only needs to cover that, with plenty of room to spare.
RETENTION: Final = timedelta(minutes=5)

# How long after startup an entity loaded from disk waits for a trigger to use
# it before it's dropped.
STARTUP_RETENTION: Final = timedelta(minutes=15)

STORAGE_KEY: Final = DOMAIN
STORAGE_VERSION: Final = 1
SAVE_DELAY: Final = 10  # seconds


class LongerGaps(StrEnum):
    """What happens when an entity comes back after a gap longer than `bridge_up_to`."""

    # Don't fire.
    IGNORE = "ignore"
    # Do what Home Assistant's own trigger does, based on the raw
    # unavailable, unknown, or removed state.
    HOME_ASSISTANT_DEFAULT = "home_assistant_default"


class GapKind(StrEnum):
    """Why an entity had no valid state during a gap."""

    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    REMOVED = "removed"
    RESTART = "restart"
