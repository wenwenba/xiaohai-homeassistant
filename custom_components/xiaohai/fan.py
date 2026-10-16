"""Fresh air. One fan: on/off plus a speed slider.

HomeKit turns each preset into its own switch, so the cloud speeds are
steps on a single slider instead.
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
from .dp import dp_value, profile, slider_speeds
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
        if (profile(coordinator.data.get(dev_id) or {}) or {}).get("kind") == "climate":
            self._attr_name = "风速"

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
        return features

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

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs) -> None:
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
