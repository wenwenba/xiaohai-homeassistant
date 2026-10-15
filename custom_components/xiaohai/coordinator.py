"""Data update coordinator."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import XiaohaiAuthError, XiaohaiClient, XiaohaiError
from .const import DOMAIN, SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)


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
        self._room_warned = False

    async def _async_update_data(self) -> dict[str, dict]:
        try:
            devices = await self.client.async_list_devices()
        except XiaohaiAuthError as err:
            raise UpdateFailed(f"登录失败：{err}") from err
        except XiaohaiError as err:
            raise UpdateFailed(str(err)) from err

        rooms: dict[str, str] = {}
        schemas: dict[str, dict] = {}
        gids: list = []
        for device in devices:
            gid = device.get("_group_id")
            if gid is not None and gid not in gids:
                gids.append(gid)
        for gid in gids:
            try:
                rooms.update(await self.client.async_device_rooms(gid))
                self._room_warned = False
            except XiaohaiError as err:
                if not self._room_warned:
                    _LOGGER.warning("房间关系没有取到：%s", err)
                    self._room_warned = True
            schemas.update(await self.client.async_product_schema(gid))

        indexed = {}
        for device in devices:
            device["_room"] = rooms.get(device["devId"])
            device["_schema"] = schemas.get(str(device.get("productId") or "")) or {}
            indexed[device["devId"]] = device
        return indexed


def async_assign_areas(hass: HomeAssistant, devices: dict[str, dict]) -> None:
    """Put a device in the Home Assistant area that matches its cloud room.

    shortcut: only fills an empty area, so a later manual move is kept.
    """
    area_reg = ar.async_get(hass)
    device_reg = dr.async_get(hass)
    by_name = {area.name: area.id for area in area_reg.async_list_areas()}
    for dev_id, device in devices.items():
        room = device.get("_room")
        if not room:
            continue
        area_id = by_name.get(room)
        if area_id is None:
            area_id = area_reg.async_create(room).id
            by_name[room] = area_id
        current = device_reg.async_get_device(identifiers={(DOMAIN, dev_id)})
        if current is not None and current.area_id is None:
            device_reg.async_update_device(current.id, area_id=area_id)
