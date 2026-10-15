"""Datapoint and room helpers. Kept free of Home Assistant imports."""

from __future__ import annotations

import json
from typing import Any

# Category fallbacks match the live products and the Tuya standard instruction
# set for kt (air conditioner) and wk (thermostat): set/current are separate dps.
FALLBACK = {
    "kt": {"kind": "climate", "power": "1", "target": "2", "current": "3", "mode": "4", "fan": "5"},
    "wk": {"kind": "climate", "power": "1", "target": "16", "current": "24"},
    "xfj": {"kind": "fan", "power": "1", "fan": "12"},
}

MODE_TO_HVAC = {
    "cold": "cool",
    "hot": "heat",
    "heat": "heat",
    "wind": "fan_only",
    "fan": "fan_only",
    "auto": "auto",
    "wet": "dry",
    "wet_model": "dry",
    "dry": "dry",
}
HVAC_TO_MODE = {
    "cool": "cold",
    "heat": "hot",
    "fan_only": "wind",
    "auto": "auto",
    "dry": "wet",
}

CATEGORY_LABEL = {
    "kt": "空调",
    "wk": "地暖",
    "xfj": "新风",
    "jzq": "网关",
    "wg2": "网关",
}


def device_online(device: dict) -> bool | None:
    """Online if any reported module is online. Missing data is unknown, not offline."""
    modules = device.get("moduleMap")
    if isinstance(modules, dict) and modules:
        flags = [
            module.get("isOnline")
            for module in modules.values()
            if isinstance(module, dict) and "isOnline" in module
        ]
        if flags:
            return any(bool(flag) for flag in flags)
    if "isOnline" in device:
        return bool(device["isOnline"])
    return None


def celsius(value: Any) -> float | None:
    """Cloud temperatures are plain °C. Values above 60 are decidegrees (260 = 26.0)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value == 0:
        return None
    if value > 60:
        return float(value) / 10
    return float(value)


def dp_value(device: dict, dp: str | None) -> Any:
    if not dp:
        return None
    dps = device.get("dps") or {}
    if not isinstance(dps, dict):
        return None
    if dp in dps:
        return dps[dp]
    return dps.get(str(dp))


def _code_dp(schema: dict[str, str], *codes: str, fallback: str | None = None) -> str | None:
    for code in codes:
        if schema.get(code):
            return str(schema[code])
    return fallback


def profile(device: dict) -> dict | None:
    """How to control this device. None for gateways and unknown products."""
    category = str(device.get("category") or "")
    if category in {"jzq", "wg2"}:
        return None
    schema = device.get("_schema") or {}
    base = FALLBACK.get(category) or {}
    kind = base.get("kind")
    target = _code_dp(schema, "temp_set", "temp_set_c", fallback=base.get("target"))
    current = _code_dp(schema, "temp_current", "temp_current_c", fallback=base.get("current"))
    if kind is None and (target or current):
        kind = "climate"
    if kind is None:
        return None
    return {
        "kind": kind,
        "power": _code_dp(schema, "switch", "switch_led", fallback=base.get("power")),
        "target": target,
        "current": current,
        "mode": _code_dp(schema, "mode", fallback=base.get("mode")),
        "fan": _code_dp(schema, "fan_speed_enum", "fan_speed", "speed", fallback=base.get("fan")),
    }


def speed_choices(current: Any) -> list[str]:
    text = "" if current is None else str(current)
    choices = ["auto", "level_1", "level_2", "level_3"] if "level_" in text else ["auto", "level1", "level2", "level3"]
    if text and text not in choices:
        choices.append(text)
    return choices


def _walk(node: Any, visit) -> None:
    if isinstance(node, str) and node[:1] in "[{":
        try:
            node = json.loads(node)
        except json.JSONDecodeError:
            return
    if isinstance(node, dict):
        visit(node)
        for value in node.values():
            _walk(value, visit)
    elif isinstance(node, list):
        for value in node:
            _walk(value, visit)


def rooms_from_payload(payload: Any, names: dict[str, str]) -> dict[str, str]:
    """Pull devId -> room name out of sort, relation, or location payloads."""
    found: dict[str, str] = {}

    def visit(node: dict) -> None:
        dev = node.get("bizId") or node.get("devId")
        room_name = node.get("roomName")
        if isinstance(room_name, str) and room_name:
            ident = dev or node.get("id")
            if isinstance(ident, str) and len(ident) > 8:
                found[ident] = room_name
        room_id = node.get("roomId")
        if dev and room_id not in (None, "", 0, "0", -1, "-1"):
            named = names.get(str(room_id))
            if named:
                found[str(dev)] = named
        if not isinstance(node.get("name"), str):
            return
        for key in ("deviceIds", "devIds", "deviceList", "devices", "ids"):
            ids = node.get(key)
            if isinstance(ids, str) and ids[:1] == "[":
                try:
                    ids = json.loads(ids)
                except json.JSONDecodeError:
                    continue
            if not isinstance(ids, list):
                continue
            for item in ids:
                if isinstance(item, dict):
                    # 6 is a device. Groups and rooms share this list.
                    if item.get("type") not in (None, 6, "6"):
                        continue
                    dev_id = item.get("devId") or item.get("bizId") or item.get("id")
                else:
                    dev_id = item
                if isinstance(dev_id, str) and len(dev_id) > 8:
                    found[dev_id] = node["name"]

    _walk(payload, visit)
    return found


def device_id_from_unique_id(unique_id: str) -> str | None:
    """Device id stored in an entity unique id, before the entity suffix."""
    for suffix in ("_climate", "_temperature", "_online", "_fan"):
        if unique_id.endswith(suffix):
            return unique_id[: -len(suffix)]
    return None


def device_ids(payload: Any) -> list[str]:
    """Device ids inside a room-membership response."""
    found: list[str] = []

    def visit(node: dict) -> None:
        for key in ("bizId", "devId"):
            value = node.get(key)
            if isinstance(value, str) and len(value) > 8 and value not in found:
                found.append(value)

    _walk(payload, visit)
    return found


def payload_shape(payload: Any) -> str:
    """Key names only, so a failed room lookup can be diagnosed without secrets."""
    if isinstance(payload, list):
        head = payload_shape(payload[0]) if payload else "-"
        rooms = ",".join(
            f"{item.get('bizType')}:{item.get('roomId')}"
            for item in payload[:6]
            if isinstance(item, dict) and "roomId" in item
        )
        return f"list {len(payload)} {head} roomId[{rooms}]"
    if isinstance(payload, dict):
        return ",".join(sorted(str(key) for key in payload)[:12]) or "{}"
    if isinstance(payload, str):
        return f"str {len(payload)}"
    return repr(payload)[:40]


def schema_from_payload(payload: Any) -> dict[str, dict[str, str]]:
    """productId -> {datapoint code: dp id}."""
    found: dict[str, dict[str, str]] = {}

    def visit(node: dict) -> None:
        product_id = node.get("productId")
        if not product_id:
            return
        codes: dict[str, str] = {}
        for key in (
            "functionSchemaList",
            "statusSchemaList",
            "functionList",
            "statusList",
            "schema",
            "functions",
            "status",
        ):
            items = node.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                code = item.get("code") or item.get("dpCode") or item.get("functionCode")
                dp = item.get("dpId")
                if dp is None:
                    dp = item.get("dp_id")
                if dp is None and isinstance(item.get("id"), int) and item["id"] < 1000:
                    dp = item["id"]
                if code and dp is not None:
                    codes[str(code)] = str(dp)
        if codes:
            found[str(product_id)] = codes

    _walk(payload, visit)
    return found


def _self_check() -> None:
    assert device_online({"moduleMap": {"zigbee": {"isOnline": True}}}) is True
    assert device_online({"moduleMap": {"zigbee": {"isOnline": False}}}) is False
    assert device_online({}) is None
    assert celsius(26) == 26
    assert celsius(260) == 26.0
    assert celsius(0) is None
    assert celsius(False) is None
    ac = profile({"category": "kt", "productId": "bb8ial3n"})
    assert ac["target"] == "2" and ac["current"] == "3" and ac["kind"] == "climate"
    heat = profile({"category": "wk", "_schema": {"temp_set": "24", "temp_current": "16"}})
    assert heat["target"] == "24" and heat["current"] == "16"
    rooms = rooms_from_payload(
        [{"bizId": "dev1", "bizType": 6, "roomId": "45684092"}],
        {"45684092": "客厅"},
    )
    assert rooms == {"dev1": "客厅"}
    nested = rooms_from_payload(
        {"rooms": [{"name": "主卧", "deviceList": [{"devId": "dev12345678"}]}]},
        {},
    )
    assert nested == {"dev12345678": "主卧"}
    detail = rooms_from_payload(
        {"rooms": [{"name": "客厅", "ids": [
            {"id": "6c8c9339b6331381ec7a79", "type": 6},
            {"id": "group12345678", "type": 5},
        ]}]},
        {},
    )
    assert detail == {"6c8c9339b6331381ec7a79": "客厅"}
    assert device_ids([{"bizId": "abcdef123456"}]) == ["abcdef123456"]
    assert device_id_from_unique_id("6c8c9339b6331381ec7a79_climate") == "6c8c9339b6331381ec7a79"
    assert device_id_from_unique_id("6c8c9339b6331381ec7a79_online") == "6c8c9339b6331381ec7a79"
    schema = schema_from_payload(
        [{"productId": "bb8ial3n", "functionList": [{"code": "temp_set", "id": 2}]}]
    )
    assert schema["bb8ial3n"]["temp_set"] == "2"


if __name__ == "__main__":
    _self_check()
