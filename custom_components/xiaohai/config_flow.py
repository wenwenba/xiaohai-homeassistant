"""Config flow for Xiaohai."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import XiaohaiAuthError, XiaohaiClient, XiaohaiError
from .const import CONF_COUNTRY_CODE, DEFAULT_COUNTRY_CODE, DOMAIN


class XiaohaiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Ask for the Xiaohai account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username)
            self._abort_if_unique_id_configured()
            client = XiaohaiClient(
                async_get_clientsession(self.hass),
                username,
                user_input[CONF_PASSWORD],
                user_input[CONF_COUNTRY_CODE],
            )
            try:
                await client.async_login()
            except XiaohaiAuthError:
                errors["base"] = "invalid_auth"
            except XiaohaiError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=username,
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_COUNTRY_CODE: user_input[CONF_COUNTRY_CODE].strip() or DEFAULT_COUNTRY_CODE,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_COUNTRY_CODE, default=DEFAULT_COUNTRY_CODE): str,
                }
            ),
            errors=errors,
        )
