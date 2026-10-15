"""Online status for each device."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XiaohaiConfigEntry
from .const import DOMAIN
from .coordinator import XiaohaiCoordinator
from .dp import device_online
from .entity import XiaohaiEntity


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
        known.update(dev_id for dev_id in coordinator.data)
        if added:
            async_add_entities(added)

    coordinator.async_add_listener(_add_new)
    _add_new()


class XiaohaiOnlineSensor(XiaohaiEntity, BinarySensorEntity):
    """Whether any cloud module reports the device online."""

    _attr_name = "在线"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: XiaohaiCoordinator, dev_id: str) -> None:
        super().__init__(coordinator, dev_id)
        self._attr_unique_id = f"{dev_id}_online"

    @property
    def is_on(self) -> bool | None:
        if not self.device:
            return None
        return device_online(self.device)
