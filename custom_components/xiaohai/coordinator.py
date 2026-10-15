"""Data update coordinator."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import XiaohaiAuthError, XiaohaiClient, XiaohaiError
from .const import DOMAIN, SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)


def boolean_dps(device: dict) -> dict[str, bool]:
    """Return datapoints that can be shown as switches."""
    dps = device.get("dps") or {}
    if not isinstance(dps, dict):
        return {}
    return {str(key): value for key, value in dps.items() if isinstance(value, bool)}


class XiaohaiCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Poll homes and devices from the Xiaohai cloud."""

    def __init__(self, hass: HomeAssistant, client: XiaohaiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, dict]:
        try:
            devices = await self.client.async_list_devices()
        except XiaohaiAuthError as err:
            raise UpdateFailed(f"登录失败：{err}") from err
        except XiaohaiError as err:
            raise UpdateFailed(str(err)) from err
        indexed = {device["devId"]: device for device in devices}
        await self._async_debug_snapshot(indexed)
        return indexed

    async def _async_debug_snapshot(self, devices: dict[str, dict]) -> None:
        """Temporary: one sample per product, plus room-list attempts."""
        import json

        samples = []
        seen_products: set[str] = set()
        for device in devices.values():
            product = str(device.get("productId") or "")
            if product in seen_products:
                continue
            seen_products.add(product)
            samples.append(
                {
                    "name": device.get("name"),
                    "productId": product,
                    "keys": sorted(device.keys()),
                    "online": {
                        key: device.get(key)
                        for key in device
                        if "online" in key.lower() or "connect" in key.lower() or key in {"isOnline", "devOnline"}
                    },
                    "room": {
                        key: device.get(key)
                        for key in device
                        if any(part in key.lower() for part in ("room", "area", "group", "owner", "category", "schema"))
                    },
                    "dps": device.get("dps"),
                }
            )
        rooms: dict[str, str] = {}
        group_ids = {str(device.get("_group_id")) for device in devices.values() if device.get("_group_id")}
        for action in (
            "tuya.m.room.list",
            "tuya.m.my.group.room.list",
            "tuya.m.location.room.list",
        ):
            for gid in group_ids:
                try:
                    result = await self.client._request(action, {"gid": gid}, extra_params={"gid": gid})
                except Exception as err:
                    rooms[f"{action}:{gid}"] = type(err).__name__ + " " + str(err)[:180]
                else:
                    rooms[f"{action}:{gid}"] = json.dumps(result, ensure_ascii=False)[:2500]
        message = json.dumps({"samples": samples, "rooms": rooms}, ensure_ascii=False)[:12000]
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {"title": "xiaohai-debug", "message": message, "notification_id": "xiaohai_debug"},
            blocking=True,
        )
