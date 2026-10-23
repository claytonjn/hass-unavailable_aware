"""Config flow for Unavailable-Aware Triggers."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
import voluptuous as vol

from .const import DOMAIN


class UnavailableAwareConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add the integration. There is only ever one."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding the integration."""
        if user_input is not None:
            return self.async_create_entry(title="Unavailable-Aware Triggers", data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
