"""Online status for each device."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XiaohaiConfigEntry
from .const import DOMAIN
from .coordinator import XiaohaiCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaohaiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one connectivity sensor per device."""
    coordinator: XiaohaiCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    def _add_new() -> None:
        added = [
            XiaohaiOnlineSensor(coordinator, dev_id)
            for dev_id in coordinator.data
            if dev_id not in known
        ]
        known.update(device.unique_id or "" for device in added)
        if added:
            async_add_entities(added)

    coordinator.async_add_listener(_add_new)
    _add_new()


class XiaohaiOnlineSensor(BinarySensorEntity):
    """Whether the cloud reports the device online."""

    _attr_has_entity_name = True
    _attr_name = "在线"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: XiaohaiCoordinator, dev_id: str) -> None:
        self.coordinator = coordinator
        self._dev_id = dev_id
        self._attr_unique_id = f"{dev_id}_online"

    @property
    def device(self) -> dict:
        return self.coordinator.data.get(self._dev_id) or {}

    @property
    def is_on(self) -> bool | None:
        if not self.device:
            return None
        return bool(self.device.get("isOnline", False))

    @property
    def device_info(self) -> dict[str, Any]:
        device = self.device
        return {
            "identifiers": {(DOMAIN, self._dev_id)},
            "name": device.get("name") or self._dev_id,
            "manufacturer": "小海智家",
            "model": device.get("productId") or device.get("category"),
            "sw_version": device.get("verSw") or device.get("sv"),
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))
