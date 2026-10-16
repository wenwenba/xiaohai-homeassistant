"""Data update coordinator."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import XiaohaiAuthError, XiaohaiClient, XiaohaiError
from .const import DOMAIN, SCAN_INTERVAL_SECONDS
from .dp import device_id_from_unique_id, expand_speed_range, profile, speed_enum_note

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
        self._speed_noted = False
        self._fan_noted = False
        self._device_ids: tuple[str, ...] = ()

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

        speed_notes: list[str] = []
        for product_id, schema in schemas.items():
            if not any(
                isinstance(key, str)
                and not key.startswith("@")
                and any(hint in key for hint in ("speed", "fan", "wind", "volume", "level"))
                for key in schema
            ):
                continue
            detail = await self.client.async_product_info(product_id)
            expanded = expand_speed_range(schema, detail)
            if not expanded:
                own = next(
                    (
                        device.get("schema")
                        for device in devices
                        if str(device.get("productId") or "") == product_id and device.get("schema")
                    ),
                    None,
                )
                expanded = expand_speed_range(schema, own)
            if expanded:
                speed_notes.append(f"{product_id}:{','.join(expanded)}")
        if not self._fan_noted and gids:
            for product_id, raw in await self.client.async_listed_schema(gids[0]):
                schema = schemas.get(product_id)
                if not schema:
                    continue
                expanded = expand_speed_range(schema, raw)
                if expanded:
                    speed_notes.append(f"{product_id}:{','.join(expanded)}")
        if not self._fan_noted:
            lines = list(speed_notes)
            if self.client.last_schema_note:
                lines.append(self.client.last_schema_note)
            for product_id in list(schemas)[:3]:
                detail = self.client._product_info.get(product_id)
                if isinstance(detail, dict):
                    lines.append(
                        f"{product_id} keys={','.join(sorted(detail)[:8])} {speed_enum_note(detail) or 'no-enum'}"
                    )
                else:
                    lines.append(f"{product_id} info={detail!s:.80}")
            _LOGGER.warning("风速排查：%s", " | ".join(lines)[:700])
            self._fan_noted = True

        indexed = {}
        for device in devices:
            device["_room"] = rooms.get(device["devId"])
            device["_schema"] = schemas.get(str(device.get("productId") or "")) or {}
            indexed[device["devId"]] = device
        if not self._speed_noted:
            missing = sorted(
                {
                    f"{device.get('productId')}:{'、'.join(code for code in sorted(schema) if not str(code).startswith('@'))}"
                    for device in indexed.values()
                    for schema in [device.get("_schema") or {}]
                    if schema
                    and (
                        (spec := profile(device)) is None
                        or (spec.get("kind") == "fan" and not spec.get("fan"))
                    )
                }
            )
            if missing:
                samples: list[str] = []
                for device in indexed.values():
                    product_id = str(device.get("productId") or "")
                    if not any(item.startswith(f"{product_id}:") for item in missing):
                        continue
                    dps = device.get("dps") or {}
                    if not isinstance(dps, dict):
                        continue
                    bits = []
                    for key, value in list(dps.items())[:10]:
                        if isinstance(value, bool):
                            bits.append(f"{key}={'true' if value else 'false'}")
                        elif isinstance(value, (int, str)) and not isinstance(value, bool) and len(str(value)) < 24:
                            bits.append(f"{key}={value}")
                    if bits:
                        samples.append(f"{product_id}[{','.join(bits)}]")
                    if len(samples) >= 2:
                        break
                _LOGGER.warning(
                    "云端有功能码但没有风速：%s %s",
                    " | ".join(missing[:6]),
                    " ".join(samples),
                )
            self._speed_noted = True
        ids = tuple(sorted(indexed))
        if ids != self._device_ids:
            _LOGGER.info("已从云端获取 %s 台设备", len(ids))
            self._device_ids = ids
        self.client.devices = indexed
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
