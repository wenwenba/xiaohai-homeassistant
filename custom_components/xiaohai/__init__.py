"""Xiaohai custom integration."""

from __future__ import annotations

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import XiaohaiAuthError, XiaohaiClient, XiaohaiError
from .const import CONF_COUNTRY_CODE, DOMAIN, PLATFORMS
from .coordinator import XiaohaiCoordinator

type XiaohaiConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: XiaohaiConfigEntry) -> bool:
    """Set up one Xiaohai account."""
    session: aiohttp.ClientSession = async_get_clientsession(hass)
    client = XiaohaiClient(
        session,
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_COUNTRY_CODE],
    )
    try:
        await client.async_login()
    except XiaohaiAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except XiaohaiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = XiaohaiCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: XiaohaiConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
