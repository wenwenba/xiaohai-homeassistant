"""Fresh air."""

from __future__ import annotations

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XiaohaiConfigEntry
from .const import DOMAIN
from .coordinator import XiaohaiCoordinator
from .dp import dp_value, profile, speed_choices
from .entity import XiaohaiEntity, take_new_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaohaiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a fan entity for each fresh-air unit."""
    coordinator: XiaohaiCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    def _build(dev_id: str, device: dict) -> XiaohaiFan | None:
        spec = profile(device)
        if not spec or spec["kind"] != "fan":
            return None
        return XiaohaiFan(coordinator, dev_id)

    def _add_new() -> None:
        added = take_new_entities(coordinator, known, _build)
        if added:
            async_add_entities(added)

    entry.async_on_unload(coordinator.async_add_listener(_add_new))
    _add_new()


class XiaohaiFan(XiaohaiEntity, FanEntity):
    """One fresh-air unit. Speed is the cloud enum, for example level1."""

    _attr_name = None
    _attr_icon = "mdi:fan"

    def __init__(self, coordinator: XiaohaiCoordinator, dev_id: str) -> None:
        super().__init__(coordinator, dev_id)
        self._attr_unique_id = f"{dev_id}_fan"

    @property
    def _spec(self) -> dict:
        return profile(self.device) or {}

    @property
    def supported_features(self) -> FanEntityFeature:
        features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
        if self._spec.get("fan"):
            features |= FanEntityFeature.PRESET_MODE
        return features

    @property
    def is_on(self) -> bool | None:
        power = dp_value(self.device, self._spec.get("power"))
        return None if power is None else bool(power)

    @property
    def preset_mode(self) -> str | None:
        value = dp_value(self.device, self._spec.get("fan"))
        return None if value is None else str(value)

    @property
    def preset_modes(self) -> list[str] | None:
        if not self._spec.get("fan"):
            return None
        return speed_choices(dp_value(self.device, self._spec.get("fan")), self._spec.get("fan_values"))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        fan = self._spec.get("fan")
        if fan:
            await self.async_publish({fan: preset_mode})

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs) -> None:
        power = self._spec.get("power")
        if not power:
            return
        dps: dict = {power: True}
        if preset_mode and self._spec.get("fan"):
            dps[self._spec["fan"]] = preset_mode
        await self.async_publish(dps)

    async def async_turn_off(self, **kwargs) -> None:
        power = self._spec.get("power")
        if power:
            await self.async_publish({power: False})
