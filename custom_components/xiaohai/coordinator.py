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
        return {device["devId"]: device for device in devices}
