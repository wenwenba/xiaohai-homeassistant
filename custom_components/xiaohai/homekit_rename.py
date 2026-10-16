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
                store.async_delay_save(lambda: {"names": dict(names)}, 1)

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


def _install_name_source() -> bool:
    try:
        from homeassistant.components import homekit as hk
    except ImportError:
        _LOGGER.warning("xiaohai: 没有 HomeKit 组件，无法区分重名")
        return False
    if not getattr(hk.HomeKit._async_create_bridge_accessory, "_xiaohai_names", False):
        original = hk.HomeKit._async_create_bridge_accessory

        async def _async_create_bridge_accessory(self, entity_states):
            try:
                _assign_names(self.hass, list(entity_states), self._config)
                _LOGGER.warning(
                    "xiaohai: 桥配件已按可写名称创建，%s 个重名已按区域区分",
                    self.hass.data.get(_CHANGED, 0),
                )
            except Exception:
                _LOGGER.warning("xiaohai: 区分 HomeKit 重名失败", exc_info=True)
            return await original(self, entity_states)

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


async def async_enable_homekit_rename(hass: HomeAssistant) -> None:
    """Patch the bridge before it builds accessories. Do not reload it."""
    await _load_names(hass)
    if hass.data.get(_INSTALLED):
        return
    hass.data[_INSTALLED] = True
    _install_patch()
    _install_name_source()
    _LOGGER.warning("xiaohai: 名称补丁已安装")
