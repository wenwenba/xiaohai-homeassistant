"""Let the Home app rename and move accessories that this bridge already has.

iOS writes the Configured Name characteristic when a room or name is saved.
HAP-python advertises Name as read-only and does not add Configured Name, so
the Home app rejects the save. The same bridge and the same accessory ids are
kept; nothing new is paired.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

_INSTALLED = "xiaohai_homekit_rename"
_SKIP_SERVICES = {"HAPProtocolInformation"}
_NAME_LIMIT = 64


def _remember_name(accessory: object, value: object) -> None:
    """Store a Home app rename on the existing entity."""
    hass = getattr(accessory, "hass", None)
    entity_id = getattr(accessory, "entity_id", None)
    if hass is None or not entity_id or not isinstance(value, str):
        return
    name = value.strip()[:_NAME_LIMIT]
    if not name:
        return
    state = hass.states.get(entity_id)
    if state is not None and state.name == name:
        return

    async def _store() -> None:
        registry = er.async_get(hass)
        if registry.async_get(entity_id) is None:
            return
        registry.async_update_entity(entity_id, name=name)

    hass.async_create_task(_store())


def _attach_configured_name(accessory: object, service: object) -> None:
    display_name = getattr(service, "display_name", None)
    if display_name in _SKIP_SERVICES:
        return
    try:
        service.get_characteristic("ConfiguredName")
        return
    except ValueError:
        pass
    char = accessory.driver.loader.get_char("ConfiguredName")
    char.setter_callback = lambda value, acc=accessory: _remember_name(acc, value)
    service.add_characteristic(char)
    current = getattr(accessory, "display_name", None)
    if isinstance(current, str) and current:
        char.set_value(current[:_NAME_LIMIT], should_notify=False)


def _install_patch() -> bool:
    """Add a writable Configured Name before HomeKit assigns characteristic ids."""
    try:
        from pyhap.accessory import Accessory
    except ImportError:
        return False
    if getattr(Accessory.add_service, "_xiaohai_configured_name", False):
        return True
    original = Accessory.add_service

    def add_service(self: Accessory, *services: object) -> None:
        for service in services:
            _attach_configured_name(self, service)
        original(self, *services)

    add_service._xiaohai_configured_name = True
    Accessory.add_service = add_service
    return True


async def _reload_bridges(hass: HomeAssistant) -> None:
    reloaded = False
    for entry in hass.config_entries.async_entries("homekit"):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        await hass.config_entries.async_reload(entry.entry_id)
        reloaded = True
    if reloaded:
        _LOGGER.warning("HomeKit 桥已按原配件重载，家庭 App 现在可以修改名称和房间")


async def async_enable_homekit_rename(hass: HomeAssistant) -> None:
    """Patch the running bridge once, then reload it in place."""
    if not _install_patch():
        return
    if hass.data.get(_INSTALLED):
        return
    hass.data[_INSTALLED] = True

    def _schedule(_event: object = None) -> None:
        hass.async_create_task(_reload_bridges(hass))

    if hass.is_running:
        _schedule()
        return
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _schedule)
