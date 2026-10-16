"""Let the Home app rename and move accessories that this bridge already has.

iOS refuses the settings sheet when Name is read-only, and the home hub
rejects the save when two accessories share a name. Configured Name is added,
Name is made writable, and a colliding Xiaohai accessory is shown as its
current area plus its current name. The same bridge and accessory ids stay.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

_INSTALLED = "xiaohai_homekit_rename"
_NAMES = "xiaohai_homekit_names"
_ASSIGNED = "xiaohai_homekit_assigned"
_CHANGED = "xiaohai_homekit_changed"
_STORE = "xiaohai_homekit_name_store"
_AIDS_ROTATED = "xiaohai_homekit_aids_rotated"
_SPEED_ROTATED = "xiaohai_homekit_speed_aids_rotated"
# These two kept the shared Home name and iOS locked their settings.
# The third id is the child-room fan before its entity id changed.
_SPEED_ENTITY_IDS = (
    "fan.ci_wo_kong_diao_feng_su",
    "fan.wen_wen_kong_diao_kong_diao_1_feng_su",
    "fan.wen_wen_kong_diao_feng_su",
)
_LOCKED_SPEED_NAME = "空调风速"
_SKIP_SERVICES = {"HAPProtocolInformation"}
_NAME_LIMIT = 64
_PERMISSIONS = ["pr", "pw", "ev"]


def pick_names(
    rows: list[tuple[str, str, bool, str | None, str | None]],
) -> tuple[dict[str, str], int]:
    """Return HomeKit names and how many collisions were split.

    Each row is entity id, preferred name, whether it is Xiaohai, area name,
    and a name already set on the bridge. A bridge name is left as it is.
    """
    chosen = {entity_id: (locked or preferred)[:_NAME_LIMIT] for entity_id, preferred, _xiaohai, _area, locked in rows}
    original = dict(chosen)
    counts: dict[str, int] = {}
    for name in original.values():
        counts[name] = counts.get(name, 0) + 1
    changed = 0
    for entity_id, preferred, is_xiaohai, area, locked in sorted(rows, key=lambda row: row[0]):
        if locked or not is_xiaohai or counts.get(original[entity_id], 0) < 2:
            continue
        candidate = f"{area} {preferred}"[:_NAME_LIMIT] if area else preferred[:_NAME_LIMIT]
        number = 2
        while _conflicts(chosen, entity_id, candidate):
            suffix = f" {number}"
            stem = f"{area} {preferred}" if area else preferred
            candidate = f"{stem[: _NAME_LIMIT - len(suffix)]}{suffix}"
            number += 1
        if candidate != chosen[entity_id]:
            chosen[entity_id] = candidate
            changed += 1
    return chosen, changed


def _stored(hass: HomeAssistant) -> dict:
    return {
        "names": dict(hass.data.get(_NAMES) or {}),
        "aids_rotated": bool(hass.data.get(_AIDS_ROTATED)),
        "speed_aids_rotated": bool(hass.data.get(_SPEED_ROTATED)),
    }


def _conflicts(chosen: dict[str, str], entity_id: str, name: str | None = None) -> bool:
    current = chosen[entity_id] if name is None else name
    return any(other != entity_id and value == current for other, value in chosen.items())


def _remember_name(accessory: object, value: object) -> None:
    """Keep a Home rename without reloading the bridge or renaming the HA device.

    A failure here must not fail the HomeKit write, or the room change fails too.
    shortcut: names live in .storage, not the HomeKit config entry, so a rename does not reload the bridge.
    """
    try:
        hass = getattr(accessory, "hass", None)
        entity_id = getattr(accessory, "entity_id", None)
        if hass is None or not entity_id or not isinstance(value, str):
            return
        name = value.strip()[:_NAME_LIMIT]
        if not name:
            return

        async def _store() -> None:
            names = hass.data.setdefault(_NAMES, {})
            if names.get(entity_id) == name:
                return
            names[entity_id] = name
            store: Store | None = hass.data.get(_STORE)
            if store is not None:
                store.async_delay_save(lambda: _stored(hass), 1)

        hass.add_job(_store)
    except Exception:
        _LOGGER.debug("未能记下家庭 App 里的新名称", exc_info=True)


def _make_name_writable(accessory: object, service: object) -> None:
    try:
        char = service.get_characteristic("Name")
    except ValueError:
        return
    perms = list((getattr(char, "properties", None) or {}).get("Permissions") or [])
    if "pw" not in perms:
        char.override_properties({"Permissions": list(_PERMISSIONS)})
    if getattr(char.setter_callback, "_xiaohai_name", False):
        return
    previous = char.setter_callback

    def _on_name(value: object, acc: object = accessory, prev=previous) -> None:
        if prev is not None:
            prev(value)
        _remember_name(acc, value)

    _on_name._xiaohai_name = True
    char.setter_callback = _on_name


def _attach_configured_name(accessory: object, service: object) -> None:
    if getattr(service, "display_name", None) in _SKIP_SERVICES:
        return
    _make_name_writable(accessory, service)
    try:
        service.get_characteristic("ConfiguredName")
        return
    except ValueError:
        pass
    char = accessory.driver.loader.get_char("ConfiguredName")
    char.setter_callback = lambda value, acc=accessory: _remember_name(acc, value)
    service.add_characteristic(char)
    current = getattr(accessory, "display_name", None)
    if isinstance(current, str) and current:
        char.set_value(current[:_NAME_LIMIT], should_notify=False)


def _install_patch() -> bool:
    """Add a writable name before HomeKit assigns characteristic ids."""
    try:
        from pyhap.accessory import Accessory
    except ImportError:
        _LOGGER.warning("xiaohai: 没有 pyhap，家庭 App 仍不能改名称")
        return False
    if getattr(Accessory.add_service, "_xiaohai_configured_name", False):
        return True
    original = Accessory.add_service

    def add_service(self: Accessory, *services: object) -> None:
        for service in services:
            try:
                _attach_configured_name(self, service)
            except Exception:
                _LOGGER.warning("xiaohai: 名称补丁跳过一个服务", exc_info=True)
        original(self, *services)

    add_service._xiaohai_configured_name = True
    Accessory.add_service = add_service
    return True


def _xiaohai_area(hass: HomeAssistant, entity_id: str) -> tuple[bool, str | None]:
    entity = er.async_get(hass).async_get(entity_id)
    if entity is None or entity.platform != "xiaohai":
        return False, None
    area_id = entity.area_id
    if not area_id and entity.device_id:
        device = dr.async_get(hass).async_get(entity.device_id)
        area_id = device.area_id if device is not None else None
    if not area_id:
        return True, None
    area = ar.async_get(hass).async_get_area(area_id)
    return True, area.name if area is not None else None


def _assign_names(hass: HomeAssistant, states: list[State], config: dict) -> None:
    stored = hass.data.get(_NAMES) or {}
    rows = []
    for state in states:
        row = config.get(state.entity_id) if isinstance(config, dict) else None
        locked = row.get("name") if isinstance(row, dict) else None
        locked = str(locked) if locked else None
        preferred = str(stored.get(state.entity_id) or state.name or state.entity_id)
        is_xiaohai, area = _xiaohai_area(hass, state.entity_id)
        rows.append((state.entity_id, preferred, is_xiaohai, area, locked))
    chosen, changed = pick_names(rows)
    hass.data[_ASSIGNED] = chosen
    hass.data[_CHANGED] = changed


async def _rotate_xiaohai_aids(
    homekit: object, states: list[State]
) -> tuple[dict[str, int], dict[str, int | None]]:
    """Give bridged Xiaohai accessories a new aid, once.

    The aid is a hash of the entity unique id, so deleting it would just
    hand the same number back. The old number stays reserved and is never
    given to another accessory. Pairing data and every other aid are left
    alone. shortcut: tombstones are permanent, compact the aid store only if
    it has to be rebuilt.
    """
    empty: tuple[dict[str, int], dict[str, int | None]] = ({}, {})
    hass = homekit.hass
    if hass.data.get(_AIDS_ROTATED):
        return empty
    storage = homekit.aid_storage
    if storage is None:
        return empty
    registry = er.async_get(hass)
    targets: list[str] = []
    others: dict[str, int | None] = {}
    for state in states:
        entity = registry.async_get(state.entity_id)
        if entity is not None and entity.platform == "xiaohai":
            targets.append(state.entity_id)
        else:
            others[state.entity_id] = storage.get_allocated_aid_for_entity_id(state.entity_id)
    if not targets:
        return empty
    retired: dict[str, int] = {}
    for entity_id in targets:
        old = storage.get_allocated_aid_for_entity_id(entity_id)
        if old is None:
            continue
        storage.async_delete_aid_for_entity_id(entity_id)
        storage.allocations[f"xiaohai.retired.{old}"] = old
        storage.allocated_aids.add(old)
        retired[entity_id] = old
    if not retired:
        return empty
    hass.data[_AIDS_ROTATED] = True
    store: Store | None = hass.data.get(_STORE)
    if store is not None:
        await store.async_save(_stored(hass))
    await storage.async_save()
    return retired, others


def _drop_locked_speed_names(hass: HomeAssistant) -> None:
    names = hass.data.get(_NAMES)
    if not isinstance(names, dict):
        return
    for entity_id in _SPEED_ENTITY_IDS:
        if names.get(entity_id) == _LOCKED_SPEED_NAME:
            names.pop(entity_id, None)


async def _rotate_speed_aids(homekit: object) -> dict[str, int]:
    """Give the locked speed fans a new aid, once.

    Same rule as the first rotation: the old number stays reserved.
    """
    hass = homekit.hass
    if hass.data.get(_SPEED_ROTATED):
        return {}
    storage = homekit.aid_storage
    if storage is None:
        return {}
    retired: dict[str, int] = {}
    for entity_id in _SPEED_ENTITY_IDS:
        old = storage.get_allocated_aid_for_entity_id(entity_id)
        if old is None:
            continue
        storage.async_delete_aid_for_entity_id(entity_id)
        storage.allocations[f"xiaohai.retired.{old}"] = old
        storage.allocated_aids.add(old)
        retired[entity_id] = old
    hass.data[_SPEED_ROTATED] = True
    store: Store | None = hass.data.get(_STORE)
    if store is not None:
        await store.async_save(_stored(hass))
    if retired:
        await storage.async_save()
    return retired


def _install_name_source() -> bool:
    try:
        from homeassistant.components import homekit as hk
    except ImportError:
        _LOGGER.warning("xiaohai: 没有 HomeKit 组件，无法区分重名")
        return False
    if not getattr(hk.HomeKit._async_create_bridge_accessory, "_xiaohai_names", False):
        original = hk.HomeKit._async_create_bridge_accessory

        async def _async_create_bridge_accessory(self, entity_states):
            states = list(entity_states)
            try:
                _drop_locked_speed_names(self.hass)
                _assign_names(self.hass, states, self._config)
                _LOGGER.warning(
                    "xiaohai: 桥配件已按可写名称创建，%s 个重名已按区域区分",
                    self.hass.data.get(_CHANGED, 0),
                )
            except Exception:
                _LOGGER.warning("xiaohai: 区分 HomeKit 重名失败", exc_info=True)
            retired: dict[str, int] = {}
            others: dict[str, int | None] = {}
            try:
                retired, others = await _rotate_xiaohai_aids(self, states)
            except Exception:
                _LOGGER.warning("xiaohai: 更换配件编号失败", exc_info=True)
            try:
                speed_retired = await _rotate_speed_aids(self)
            except Exception:
                speed_retired = {}
                _LOGGER.warning("xiaohai: 更换风速配件编号失败", exc_info=True)
            else:
                if speed_retired:
                    _LOGGER.warning("xiaohai: 已更换 %s 个风速配件编号", len(speed_retired))
            bridge = await original(self, states)
            if retired and self.aid_storage is not None:
                changed = 0
                for entity_id, old in retired.items():
                    new = self.aid_storage.get_allocated_aid_for_entity_id(entity_id)
                    if new is not None and new != old:
                        changed += 1
                stable = 0
                for entity_id, old in others.items():
                    if self.aid_storage.get_allocated_aid_for_entity_id(entity_id) == old:
                        stable += 1
                _LOGGER.warning(
                    "xiaohai: 已更换 %s 个小海配件编号，其余 %s 个编号未变",
                    changed,
                    stable,
                )
                if changed == len(retired):
                    await self.aid_storage.async_save()
            return bridge

        _async_create_bridge_accessory._xiaohai_names = True
        hk.HomeKit._async_create_bridge_accessory = _async_create_bridge_accessory

    if not getattr(hk.get_accessory, "_xiaohai_names", False):
        original_get = hk.get_accessory

        def get_accessory(hass, driver, state, aid, config):
            assigned = hass.data.get(_ASSIGNED, {}).get(state.entity_id)
            if assigned and not config.get("name"):
                config = dict(config)
                config["name"] = assigned
            return original_get(hass, driver, state, aid, config)

        get_accessory._xiaohai_names = True
        hk.get_accessory = get_accessory
    return True


async def _load_names(hass: HomeAssistant) -> None:
    store = Store(hass, 1, "xiaohai.homekit_names")
    hass.data[_STORE] = store
    data = await store.async_load()
    names = data.get("names") if isinstance(data, dict) else None
    hass.data[_NAMES] = dict(names) if isinstance(names, dict) else {}
    hass.data[_AIDS_ROTATED] = bool(data.get("aids_rotated")) if isinstance(data, dict) else False
    hass.data[_SPEED_ROTATED] = bool(data.get("speed_aids_rotated")) if isinstance(data, dict) else False


async def async_enable_homekit_rename(hass: HomeAssistant) -> None:
    """Patch the bridge before it builds accessories. Do not reload it."""
    await _load_names(hass)
    if hass.data.get(_INSTALLED):
        return
    hass.data[_INSTALLED] = True
    _install_patch()
    _install_name_source()
    _LOGGER.warning("xiaohai: 名称补丁已安装")
