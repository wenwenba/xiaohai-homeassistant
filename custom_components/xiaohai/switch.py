"""Switches for boolean datapoints."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XiaohaiConfigEntry
from .const import DOMAIN
from .coordinator import XiaohaiCoordinator, boolean_dps


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaohaiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a switch for each boolean datapoint."""
    coordinator: XiaohaiCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    def _add_new() -> None:
        added: list[XiaohaiSwitch] = []
        for dev_id, device in coordinator.data.items():
            for dp in boolean_dps(device):
                unique_id = f"{dev_id}_{dp}"
                if unique_id in known:
                    continue
                known.add(unique_id)
                added.append(XiaohaiSwitch(coordinator, dev_id, dp))
        if added:
            async_add_entities(added)

    coordinator.async_add_listener(_add_new)
    _add_new()


class XiaohaiSwitch(SwitchEntity):
    """One boolean datapoint."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: XiaohaiCoordinator, dev_id: str, dp: str) -> None:
        self.coordinator = coordinator
        self._dev_id = dev_id
        self._dp = dp
        self._attr_unique_id = f"{dev_id}_{dp}"

    @property
    def device(self) -> dict:
        return self.coordinator.data.get(self._dev_id) or {}

    @property
    def available(self) -> bool:
        return bool(self.device) and bool(self.device.get("isOnline", True))

    @property
    def name(self) -> str:
        dps = boolean_dps(self.device)
        if len(dps) == 1:
            return "开关"
        return f"开关 {self._dp}"

    @property
    def is_on(self) -> bool | None:
        value = boolean_dps(self.device).get(self._dp)
        return None if value is None else bool(value)

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

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, value: bool) -> None:
        device = dict(self.device)
        await self.coordinator.client.async_publish(device, {self._dp: value})
        dps = dict(device.get("dps") or {})
        dps[self._dp] = value
        device["dps"] = dps
        self.coordinator.data[self._dev_id] = device
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))
