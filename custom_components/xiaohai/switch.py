"""Supply-air mode. HomeKit's thermostat has no fan-only setting."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XiaohaiConfigEntry
from .const import DOMAIN
from .coordinator import XiaohaiCoordinator
from .dp import MODE_TO_HVAC, cloud_mode_token, dp_value, profile
from .entity import XiaohaiEntity, take_new_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaohaiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a supply-air switch for each air conditioner that has that mode."""
    coordinator: XiaohaiCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    def _build(dev_id: str, device: dict) -> XiaohaiSupplySwitch | None:
        spec = profile(device)
        if not spec or spec["kind"] != "climate":
            return None
        if not cloud_mode_token(spec.get("mode_values"), "fan_only"):
            return None
        return XiaohaiSupplySwitch(coordinator, dev_id)

    def _add_new() -> None:
        added = take_new_entities(coordinator, known, _build)
        if added:
            async_add_entities(added)

    entry.async_on_unload(coordinator.async_add_listener(_add_new))
    _add_new()


class XiaohaiSupplySwitch(XiaohaiEntity, SwitchEntity):
    """Turn supply air on without replacing the thermostat."""

    _attr_icon = "mdi:weather-windy"

    def __init__(self, coordinator: XiaohaiCoordinator, dev_id: str) -> None:
        super().__init__(coordinator, dev_id)
        self._attr_unique_id = f"{dev_id}_fan_only"
        device = coordinator.data.get(dev_id) or {}
        self._attr_has_entity_name = False
        self._attr_name = f"{device.get('name') or '空调'} 送风"

    @property
    def _spec(self) -> dict:
        return profile(self.device) or {}

    @property
    def is_on(self) -> bool | None:
        power = dp_value(self.device, self._spec.get("power"))
        if power is None:
            return None
        if power is False:
            return False
        token = str(dp_value(self.device, self._spec.get("mode")) or "")
        return MODE_TO_HVAC.get(token) == "fan_only"

    async def async_turn_on(self, **kwargs) -> None:
        power = self._spec.get("power")
        mode = self._spec.get("mode")
        token = cloud_mode_token(self._spec.get("mode_values"), "fan_only")
        if not power or not mode or not token:
            return
        await self.async_publish({power: True, mode: token})

    async def async_turn_off(self, **kwargs) -> None:
        if not self.is_on:
            return
        mode = self._spec.get("mode")
        cool = cloud_mode_token(self._spec.get("mode_values"), "cool")
        if mode and cool:
            await self.async_publish({mode: cool})
            return
        power = self._spec.get("power")
        if power:
            await self.async_publish({power: False})
