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

        seen_products: set[str] = set()
        for device in devices.values():
            product = str(device.get("productId") or "")
            if product in seen_products:
                continue
            seen_products.add(product)
            _LOGGER.warning(
                "xiaohai sample %s %s dpName=%s moduleMap=%s devAttribute=%s virtual=%s topo=%s node=%s",
                device.get("name"),
                product,
                json.dumps(device.get("dpName"), ensure_ascii=False)[:2000],
                json.dumps(device.get("moduleMap"), ensure_ascii=False)[:800],
                json.dumps(device.get("devAttribute"), ensure_ascii=False)[:400],
                device.get("virtual"),
                device.get("topoType"),
                device.get("nodeId"),
            )
        gid = next(str(device.get("_group_id")) for device in devices.values() if device.get("_group_id"))
        probes = [
            ("tuya.m.room.device.list", {"roomId": 45684092, "gid": gid}),
            ("tuya.m.my.room.device.list", {"roomId": 45684092}),
            ("tuya.m.device.ref.info.my.list", {"gid": gid}),
            ("tuya.m.device.relation.list", {"gid": gid}),
            ("tuya.m.location.list", None),
        ]
        for action, payload in probes:
            try:
                result = await self.client._request(action, payload, extra_params={"gid": gid})
            except Exception as err:
                _LOGGER.warning("xiaohai probe %s %s", action, err)
            else:
                _LOGGER.warning("xiaohai probe %s %s", action, json.dumps(result, ensure_ascii=False)[:2500])
