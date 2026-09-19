"""Config flow for iCSee Playback."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CHANNEL,
    DEFAULT_CHANNEL,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DOMAIN,
)
from .helper import CannotConnect, InvalidAuth, validate_login

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default=DEFAULT_NAME): TextSelector(),
        vol.Required(CONF_HOST): TextSelector(),
        vol.Required(CONF_PORT, default=DEFAULT_PORT): NumberSelector(
            NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): TextSelector(),
        vol.Required(CONF_PASSWORD, default="admin"): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_CHANNEL, default=DEFAULT_CHANNEL): NumberSelector(
            NumberSelectorConfig(min=0, max=31, mode=NumberSelectorMode.BOX)
        ),
    }
)


async def _validate(hass: HomeAssistant, data: dict[str, Any]) -> None:
    await hass.async_add_executor_job(
        validate_login,
        data[CONF_HOST],
        int(data[CONF_PORT]),
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
    )


class IcseePlaybackConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for iCSee Playback."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input[CONF_PORT] = int(user_input[CONF_PORT])
            user_input[CONF_CHANNEL] = int(
                user_input.get(CONF_CHANNEL, DEFAULT_CHANNEL)
            )
            await self.async_set_unique_id(user_input[CONF_HOST].lower())
            self._abort_if_unique_id_configured()
            try:
                await _validate(self.hass, user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
