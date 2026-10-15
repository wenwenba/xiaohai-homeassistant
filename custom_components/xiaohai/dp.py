"""Datapoint and room helpers. Kept free of Home Assistant imports."""

from __future__ import annotations

import json
from typing import Any

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


def celsius(value: Any, scale: int | None = None) -> float | None:
    """Cloud temperature. Exact 0 is no reading. scale 1 means decidegrees."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value == 0:
        return None
    if scale is not None:
        return float(value) / (10 ** int(scale)) if int(scale) else float(value)
    if value > 60:
        return float(value) / 10
    return float(value)


def scaled(value: Any, scale: int | None = None) -> float | None:
    """Schema min/max/step, including a legitimate 0."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if scale:
        return float(value) / (10 ** int(scale))
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


def _code_dp(schema: dict[str, str], *codes: str) -> str | None:
    for code in codes:
        if schema.get(code):
            return str(schema[code])
    return None


def _code_range(schema: dict[str, str], *codes: str) -> list[str] | None:
    for code in codes:
        raw = schema.get(f"@range:{code}")
        if not raw:
            continue
        try:
            values = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(values, list) and values:
            return [str(value) for value in values]
    return None


def _code_limit(schema: dict[str, str], *codes: str) -> dict[str, Any] | None:
    for code in codes:
        raw = schema.get(f"@limit:{code}")
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def cloud_mode_token(mode_values: list[str] | None, hvac: str) -> str | None:
    """Cloud enum word for a Home Assistant mode, taken from this device's range."""
    for token in mode_values or []:
        if MODE_TO_HVAC.get(str(token)) == hvac:
            return str(token)
    return None


def hvac_names(mode_values: list[str] | None) -> list[str]:
    names: list[str] = []
    for token in mode_values or []:
        name = MODE_TO_HVAC.get(str(token))
        if name and name not in names:
            names.append(name)
    return names


def profile(device: dict) -> dict | None:
    """How to control this device, using only the cloud schema."""
    schema = device.get("_schema") or {}
    target = _code_dp(schema, "temp_set", "temp_set_c")
    current = _code_dp(schema, "temp_current", "temp_current_c")
    mode = _code_dp(schema, "mode")
    fan = _code_dp(schema, "fan_speed_enum", "fan_speed", "speed")
    if target or current or mode:
        kind = "climate"
    elif fan:
        kind = "fan"
    else:
        return None
    limit = _code_limit(schema, "temp_set", "temp_set_c") or {}
    current_limit = _code_limit(schema, "temp_current", "temp_current_c") or {}
    return {
        "kind": kind,
        "power": _code_dp(schema, "switch", "switch_led"),
        "target": target,
        "current": current,
        "mode": mode,
        "mode_values": _code_range(schema, "mode"),
        "fan": fan,
        "fan_values": _code_range(schema, "fan_speed_enum", "fan_speed", "speed"),
        "target_min": limit.get("min"),
        "target_max": limit.get("max"),
        "target_step": limit.get("step"),
        "target_scale": limit.get("scale"),
        "current_scale": current_limit.get("scale"),
    }


def _parse_spec(raw: Any) -> Any:
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text[:1] not in "{[":
            return None
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return None
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if any(key in raw for key in ("range", "min", "max")):
            return raw
        for key in ("typeSpec", "property", "valueRange"):
            if key in raw:
                found = _parse_spec(raw[key])
                if found is not None:
                    return found
    return None


def _enum_range(item: dict) -> list[str] | None:
    for key in ("valueRange", "values", "property", "typeSpec"):
        parsed = _parse_spec(item.get(key))
        raw = parsed.get("range") if isinstance(parsed, dict) else parsed
        if isinstance(raw, list) and raw and all(isinstance(value, str) for value in raw):
            return [str(value) for value in raw]
    return None


def _number_limit(item: dict) -> dict[str, Any] | None:
    for key in ("valueRange", "values", "property", "typeSpec"):
        parsed = _parse_spec(item.get(key))
        if isinstance(parsed, list) and len(parsed) == 2 and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in parsed
        ):
            return {"min": parsed[0], "max": parsed[1], "step": None, "scale": None}
        if isinstance(parsed, dict) and ("min" in parsed or "max" in parsed):
            return {
                "min": parsed.get("min"),
                "max": parsed.get("max"),
                "step": parsed.get("step"),
                "scale": parsed.get("scale"),
            }
    return None


def _unique(values: list[str]) -> list[str]:
    found: list[str] = []
    for value in values:
        if value not in found:
            found.append(value)
    return found


def _device_tokens(standard: list[str] | None, raw: Any) -> list[str] | None:
    """Device enum hidden behind a standard-name mapping, when the cloud sent one."""
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text[:1] != "{":
            return None
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict) or not raw:
        return None
    if not all(isinstance(value, str) for value in raw.values()):
        return None
    keys = [str(key) for key in raw]
    values = [str(value) for value in raw.values()]
    standard_set = set(standard or [])
    if not standard_set:
        return None
    if set(keys) <= standard_set and any(value not in standard_set for value in values):
        return _unique(values)
    if set(values) <= standard_set and any(key not in standard_set for key in keys):
        return _unique(keys)
    return None


def _item_codes(item: dict) -> list[str]:
    found: list[str] = []
    for key in ("standardCode", "dpCode", "code", "functionCode"):
        value = item.get(key)
        if value and str(value) not in found:
            found.append(str(value))
    return found


def _is_dp_id(value: Any) -> bool:
    if isinstance(value, bool) or isinstance(value, float):
        return False
    if isinstance(value, int):
        return 0 <= value < 1000
    return isinstance(value, str) and value.isdigit() and int(value) < 1000


def _as_dp_id(value: Any) -> str:
    return str(int(value)) if isinstance(value, int) else str(value)


def _relation_map(item: dict) -> dict | None:
    maps = item.get("relationDpIdMaps")
    if isinstance(maps, str):
        try:
            maps = json.loads(maps)
        except json.JSONDecodeError:
            return None
    return maps if isinstance(maps, dict) and maps else None


def _item_dp(item: dict, code: str | None) -> str | None:
    for key in ("dpId", "dp_id"):
        if _is_dp_id(item.get(key)):
            return _as_dp_id(item[key])
    if _is_dp_id(item.get("id")):
        return _as_dp_id(item["id"])
    maps = _relation_map(item)
    if not maps:
        return None
    for key, value in maps.items():
        if code and str(key) == code and _is_dp_id(value):
            return _as_dp_id(value)
        if code and str(value) == code and _is_dp_id(key):
            return _as_dp_id(key)
    numeric: list[str] = []
    for key, value in maps.items():
        for side in (key, value):
            if _is_dp_id(side):
                text = _as_dp_id(side)
                if text not in numeric:
                    numeric.append(text)
    if len(numeric) == 1:
        return numeric[0]
    return None


def speed_choices(current: Any, values: list[str] | None = None) -> list[str] | None:
    """Fan speeds from the cloud enum. The current value is kept visible."""
    text = "" if current is None else str(current)
    if not values:
        return [text] if text else None
    choices = list(values)
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


def schema_field_names(payload: Any) -> str:
    """Fan and mode mapping rules. Tokens only, no account data."""
    found: list[str] = []

    def visit(node: Any) -> None:
        if len(found) >= 8 or not isinstance(node, dict):
            return
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
                if not isinstance(item, dict) or len(found) >= 8:
                    continue
                code = str(item.get("standardCode") or item.get("dpCode") or item.get("code") or "")
                if code not in {"fan_speed_enum", "fan_speed", "speed", "mode"}:
                    continue
                rule = item.get("strategyValue")
                if isinstance(rule, str) and len(rule) > 400:
                    rule = rule[:400] + "..."
                found.append(
                    f"{code} map[{item.get('relationDpIdMaps')}] range[{item.get('valueRange')}] "
                    f"strategy[{item.get('strategyCode')}] rule[{rule}]"
                )

    _walk(payload, visit)
    return " | ".join(found) or payload_shape(payload)


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
                names = _item_codes(item)
                dp = _item_dp(item, names[0] if names else None)
                if not names or dp is None:
                    continue
                enum_range = _device_tokens(_enum_range(item), item.get("strategyValue")) or _enum_range(item)
                limit = _number_limit(item)
                for code in names:
                    codes[code] = dp
                    if enum_range:
                        codes[f"@range:{code}"] = json.dumps(enum_range, ensure_ascii=False)
                    if limit and any(value is not None for value in limit.values()):
                        codes[f"@limit:{code}"] = json.dumps(limit, ensure_ascii=False)
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
    assert celsius(260, 0) == 260
    assert celsius(260, 1) == 26.0
    assert celsius(0) is None
    assert celsius(False) is None
    assert profile({"category": "kt", "productId": "bb8ial3n"}) is None
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
    assert speed_choices("level_1") == ["level_1"]
    assert speed_choices("level1") == ["level1"]
    assert speed_choices(None) is None
    ranged = schema_from_payload(
        [{
            "productId": "bb8ial3n",
            "functionList": [{
                "code": "fan_speed_enum",
                "id": 5,
                "values": "{\"range\":[\"auto\",\"level_1\",\"level_2\",\"level_3\",\"level_4\",\"level_5\"]}",
            }],
        }]
    )
    assert json.loads(ranged["bb8ial3n"]["@range:fan_speed_enum"])[1] == "level_1"
    spec = profile({"category": "kt", "_schema": ranged["bb8ial3n"]})
    assert spec["fan_values"][-1] == "level_5"
    assert speed_choices("auto", spec["fan_values"]) == spec["fan_values"]
    assert speed_choices("level_9", spec["fan_values"])[-1] == "level_9"
    standard = schema_from_payload(
        [{
            "productId": "bb8ial3n",
            "functionSchemaList": [
                {
                    "standardCode": "fan_speed_enum",
                    "valueRange": "[\"auto\",\"level_1\",\"level_2\",\"level_3\",\"level_4\",\"level_5\"]",
                    "relationDpIdMaps": {"5": "fan_speed_enum"},
                },
                {
                    "standardCode": "temp_set",
                    "valueRange": "{\"min\":16,\"max\":32,\"scale\":0,\"step\":1}",
                    "relationDpIdMaps": {"2": "temp_set"},
                },
                {
                    "standardCode": "mode",
                    "valueRange": "{\"range\":[\"cold\",\"hot\",\"wind\",\"auto\",\"wet\"]}",
                    "relationDpIdMaps": {"4": "mode"},
                },
            ],
            "statusSchemaList": [{
                "dpCode": "temp_current",
                "valueRange": "{\"min\":-20,\"max\":100,\"scale\":0,\"step\":1}",
                "relationDpIdMaps": {"3": "temp_current"},
            }],
        }]
    )
    live = profile({"_schema": standard["bb8ial3n"]})
    assert live["fan"] == "5" and live["target"] == "2" and live["current"] == "3" and live["mode"] == "4"
    flipped = schema_from_payload(
        [{
            "productId": "bb8ial3n",
            "functionSchemaList": [{
                "standardCode": "fan_speed_enum",
                "valueRange": "{\"range\":[\"auto\",\"low\",\"middle\",\"high\"]}",
                "relationDpIdMaps": {"fan_speed_enum": 5},
            }],
        }]
    )
    flipped_spec = profile({"_schema": flipped["bb8ial3n"]})
    assert flipped_spec["fan"] == "5"
    assert flipped_spec["fan_values"] == ["auto", "low", "middle", "high"]
    mapped = schema_from_payload(
        [{
            "productId": "bb8ial3n",
            "functionSchemaList": [{
                "standardCode": "fan_speed_enum",
                "valueRange": "{\"range\":[\"auto\",\"low\",\"middle\",\"high\"]}",
                "strategyValue": "{\"level_1\":\"low\",\"level_2\":\"middle\",\"level_3\":\"middle\",\"level_4\":\"high\",\"level_5\":\"high\",\"auto\":\"auto\"}",
                "relationDpIdMaps": {"fan_speed_enum": 5},
            }],
        }]
    )
    assert profile({"_schema": mapped["bb8ial3n"]})["fan_values"] == [
        "level_1",
        "level_2",
        "level_3",
        "level_4",
        "level_5",
        "auto",
    ]
    assert live["fan_values"] == ["auto", "level_1", "level_2", "level_3", "level_4", "level_5"]
    assert live["target_min"] == 16 and live["target_max"] == 32 and live["target_step"] == 1
    assert live["target_scale"] == 0 and live["current_scale"] == 0
    assert cloud_mode_token(live["mode_values"], "cool") == "cold"
    assert hvac_names(live["mode_values"]) == ["cool", "heat", "fan_only", "auto", "dry"]
    assert cloud_mode_token(["level1"], "cool") is None


if __name__ == "__main__":
    _self_check()
