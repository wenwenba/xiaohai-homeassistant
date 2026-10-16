"""Fresh air and air-conditioner speed.

HomeKit turns two or more presets into switches. One preset named auto
becomes the fan's Auto control, so air conditioners keep that and nothing else.
"""

from __future__ import annotations

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from . import XiaohaiConfigEntry
from .const import DOMAIN
from .coordinator import XiaohaiCoordinator
from .dp import dp_value, offers_speed_auto, profile, slider_speeds
from .entity import XiaohaiEntity, take_new_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaohaiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a fan for each fresh-air unit and each air conditioner."""
    coordinator: XiaohaiCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    def _build(dev_id: str, device: dict) -> XiaohaiFan | None:
        spec = profile(device)
        if not spec or spec["kind"] not in {"fan", "climate"} or not spec.get("fan"):
            return None
        return XiaohaiFan(coordinator, dev_id)

    def _add_new() -> None:
        added = take_new_entities(coordinator, known, _build)
        if added:
            async_add_entities(added)

    entry.async_on_unload(coordinator.async_add_listener(_add_new))
    _add_new()


class XiaohaiFan(XiaohaiEntity, FanEntity):
    """One fresh-air unit."""

    _attr_name = None
    _attr_icon = "mdi:fan"

    def __init__(self, coordinator: XiaohaiCoordinator, dev_id: str) -> None:
        super().__init__(coordinator, dev_id)
        self._attr_unique_id = f"{dev_id}_fan"
        device = coordinator.data.get(dev_id) or {}
        if (profile(device) or {}).get("kind") == "climate":
            self._attr_has_entity_name = False
            self._attr_name = f"{device.get('name') or '空调'} 风速"

    @property
    def _spec(self) -> dict:
        return profile(self.device) or {}

    def _ordered(self) -> list[str]:
        return slider_speeds(self.device)

    @property
    def supported_features(self) -> FanEntityFeature:
        features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
        if self._ordered():
            features |= FanEntityFeature.SET_SPEED
        if self.preset_modes:
            features |= FanEntityFeature.PRESET_MODE
        return features

    @property
    def preset_modes(self) -> list[str] | None:
        return ["auto"] if offers_speed_auto(self.device) else None

    @property
    def preset_mode(self) -> str | None:
        if self.preset_modes and str(dp_value(self.device, self._spec.get("fan")) or "") == "auto":
            return "auto"
        return None

    @property
    def speed_count(self) -> int:
        return len(self._ordered()) or 1

    @property
    def percentage_step(self) -> float:
        return 100 / self.speed_count

    @property
    def is_on(self) -> bool | None:
        power = dp_value(self.device, self._spec.get("power"))
        return None if power is None else bool(power)

    @property
    def percentage(self) -> int | None:
        if self.is_on is False:
            return 0
        ordered = self._ordered()
        current = dp_value(self.device, self._spec.get("fan"))
        if not ordered or current is None or str(current) not in ordered:
            if self._spec.get("kind") == "climate":
                return None
            return None if self.is_on is None else 100
        return ordered_list_item_to_percentage(ordered, str(current))

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            await self.async_turn_off()
            return
        ordered = self._ordered()
        power = self._spec.get("power")
        fan = self._spec.get("fan")
        dps: dict = {}
        if power:
            dps[power] = True
        if ordered and fan:
            dps[fan] = percentage_to_ordered_list_item(ordered, percentage)
        if dps:
            await self.async_publish(dps)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        fan = self._spec.get("fan")
        power = self._spec.get("power")
        if preset_mode != "auto" or not fan:
            return
        dps: dict = {fan: "auto"}
        if power:
            dps[power] = True
        await self.async_publish(dps)

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs) -> None:
        if preset_mode:
            await self.async_set_preset_mode(preset_mode)
            return
        if percentage:
            await self.async_set_percentage(percentage)
            return
        power = self._spec.get("power")
        if power:
            await self.async_publish({power: True})

    async def async_turn_off(self, **kwargs) -> None:
        power = self._spec.get("power")
        if power:
            await self.async_publish({power: False})
