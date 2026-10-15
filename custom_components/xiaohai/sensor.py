"""Temperature sensors."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XiaohaiConfigEntry
from .const import DOMAIN
from .coordinator import XiaohaiCoordinator
from .dp import celsius, dp_value, profile
from .entity import XiaohaiEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaohaiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a temperature sensor where the cloud reports a current temperature."""
    coordinator: XiaohaiCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    def _add_new() -> None:
        added = []
        for dev_id, device in coordinator.data.items():
            if dev_id in known:
                continue
            spec = profile(device)
            if not spec or not spec.get("current"):
                continue
            known.add(dev_id)
            added.append(XiaohaiTemperature(coordinator, dev_id))
        if added:
            async_add_entities(added)

    coordinator.async_add_listener(_add_new)
    _add_new()


class XiaohaiTemperature(XiaohaiEntity, SensorEntity):
    """Current temperature reported by an air conditioner, floor heater, or fresh-air unit."""

    _attr_name = "温度"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: XiaohaiCoordinator, dev_id: str) -> None:
        super().__init__(coordinator, dev_id)
        self._attr_unique_id = f"{dev_id}_temperature"

    @property
    def native_value(self) -> float | None:
        spec = profile(self.device) or {}
        return celsius(dp_value(self.device, spec.get("current")))
