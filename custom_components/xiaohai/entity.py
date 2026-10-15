"""Shared device info."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import XiaohaiCoordinator
from .dp import CATEGORY_LABEL


class XiaohaiEntity(CoordinatorEntity[XiaohaiCoordinator]):
    """One Xiaohai device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: XiaohaiCoordinator, dev_id: str) -> None:
        super().__init__(coordinator)
        self._dev_id = dev_id

    @property
    def device(self) -> dict:
        return self.coordinator.data.get(self._dev_id) or {}

    @property
    def available(self) -> bool:
        return self._dev_id in self.coordinator.data

    @property
    def device_info(self) -> dict[str, Any]:
        device = self.device
        category = str(device.get("category") or "")
        firmware = None
        modules = device.get("moduleMap") or {}
        if isinstance(modules, dict):
            for module in modules.values():
                if isinstance(module, dict) and module.get("verSw"):
                    firmware = module["verSw"]
                    break
        info: dict[str, Any] = {
            "identifiers": {(DOMAIN, self._dev_id)},
            "name": device.get("name") or self._dev_id,
            "manufacturer": "小海智家",
            "model": CATEGORY_LABEL.get(category) or device.get("productId") or category,
            "sw_version": firmware or device.get("verSw") or device.get("sv"),
        }
        if device.get("_room"):
            info["suggested_area"] = device["_room"]
        return info

    async def async_publish(self, dps: dict[str, Any]) -> None:
        device = dict(self.device)
        await self.coordinator.client.async_publish(device, dps)
        merged = dict(device.get("dps") or {})
        merged.update({str(key): value for key, value in dps.items()})
        device["dps"] = merged
        self.coordinator.data[self._dev_id] = device
        self.async_write_ha_state()
