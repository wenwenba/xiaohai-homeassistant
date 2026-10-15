"""Xiaohai custom integration."""

from __future__ import annotations

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import XiaohaiAuthError, XiaohaiClient, XiaohaiError
from .const import CONF_COUNTRY_CODE, DOMAIN, PLATFORMS
from .coordinator import XiaohaiCoordinator, async_assign_areas, async_sync_devices
from .homekit_rename import async_enable_homekit_rename

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

    def _apply_device_list() -> None:
        async_sync_devices(hass, entry.entry_id, coordinator.data)
        async_assign_areas(hass, coordinator.data)

    _apply_device_list()
    entry.async_on_unload(coordinator.async_add_listener(_apply_device_list))
    entry.async_on_unload(coordinator.async_shutdown)
    _remove_legacy_switches(hass, entry.entry_id)
    await async_enable_homekit_rename(hass)
    if hass.services.has_service("persistent_notification", "dismiss"):
        await hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": "xiaohai_debug"},
            blocking=False,
        )
    return True


def _remove_legacy_switches(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the old boolean-datapoint switches. Climate and fan replace them."""
    registry = er.async_get(hass)
    for entity in list(registry.entities.values()):
        if entity.config_entry_id == entry_id and entity.domain == "switch":
            registry.async_remove(entity.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: XiaohaiConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
