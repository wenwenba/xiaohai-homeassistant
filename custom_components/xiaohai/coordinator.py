"""Data update coordinator."""

from __future__ import annotations

from datetime import timedelta
import logging
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import XiaohaiAuthError, XiaohaiClient, XiaohaiError
from .const import DOMAIN, SCAN_INTERVAL_SECONDS
from .dp import device_id_from_unique_id
from .lan import discover_gateways, private_gateway_ips

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
        self._device_ids: tuple[str, ...] = ()
        self._ip_logged = False
        self._lan_at = 0.0

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
            except XiaohaiError as err:
                if not self._room_warned:
                    _LOGGER.warning("房间关系没有取到：%s", err)
                    self._room_warned = True
            if rooms:
                self._room_warned = False
            elif self.client.last_room_note and not self._room_warned:
                _LOGGER.warning("房间关系没有解析出来：%s", self.client.last_room_note)
                self._room_warned = True
            schemas.update(await self.client.async_product_schema(gid))

        indexed = {}
        for device in devices:
            device["_room"] = rooms.get(device["devId"])
            device["_schema"] = schemas.get(str(device.get("productId") or "")) or {}
            indexed[device["devId"]] = device
        ids = tuple(sorted(indexed))
        if ids != self._device_ids:
            _LOGGER.info("已从云端获取 %s 台设备", len(ids))
            self._device_ids = ids
        self.client.devices = indexed
        if not self._ip_logged:
            ips = private_gateway_ips(indexed)
            _LOGGER.warning("云端给出的网关局域网地址：%s", "、".join(ips) or "无")
            self._ip_logged = True
        await self._async_discover()
        return indexed

    async def _async_discover(self) -> None:
        """Look for the gateway on the LAN. Repeat until one answers, then hourly."""
        now = time.monotonic()
        wait = 3600 if self.client.lan else 600
        if self._lan_at and now - self._lan_at < wait:
            return
        try:
            found, note = await self.hass.async_add_executor_job(
                discover_gateways, self.client.devices
            )
        except OSError as err:
            _LOGGER.warning("局域网发现失败：%s", err)
            return
        changed = found != self.client.lan or self._lan_at == 0.0
        self._lan_at = now
        self.client.lan = found
        if changed:
            summary = ", ".join(
                f"{item['ip']} {item.get('version') or ''}".strip() for item in found.values()
            )
            extra = f"：{summary}" if summary else ""
            _LOGGER.warning("局域网发现 %s 台网关%s%s", len(found), extra, f" {note}" if note else "")


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


def async_sync_devices(hass: HomeAssistant, entry_id: str, devices: dict[str, dict]) -> None:
    """Make the Home Assistant device list match the cloud list from the last fetch."""
    live = set(devices)
    ent_reg = er.async_get(hass)
    for entity in list(ent_reg.entities.values()):
        if entity.config_entry_id != entry_id or not entity.unique_id:
            continue
        dev_id = device_id_from_unique_id(entity.unique_id)
        if dev_id is not None and dev_id not in live:
            ent_reg.async_remove(entity.entity_id)

    dev_reg = dr.async_get(hass)
    for device in list(dr.async_entries_for_config_entry(dev_reg, entry_id)):
        idents = [item[1] for item in device.identifiers if item[0] == DOMAIN]
        if not idents or any(item not in live for item in idents):
            dev_reg.async_remove_device(device.id)
            continue
        cloud = devices.get(idents[0])
        if cloud is None or device.name_by_user:
            continue
        name = cloud.get("name") or idents[0]
        if device.name != name:
            dev_reg.async_update_device(device.id, name=name)
