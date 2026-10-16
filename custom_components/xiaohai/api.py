"""Client for the Xiaohai private cloud (coli688 mobile API)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import aiohttp

from .const import API_URL, API_VERSION, CLIENT_ID, ET_VERSION, SIGN_KEY, USER_AGENT
from .dp import payload_shape, rooms_from_payload, schema_from_payload, thing_schema


class XiaohaiError(Exception):
    """API returned an error."""


class XiaohaiAuthError(XiaohaiError):
    """Username or password was rejected, or the session expired."""


def _mobile_hash(data: str) -> str:
    digest = hashlib.md5(data.encode("utf-8")).hexdigest()
    return digest[8:16] + digest[0:8] + digest[24:32] + digest[16:24]


def password_digest(password: str) -> str:
    """MD5 of the password, which is what the cloud compares at login."""
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def sign_request(data: dict[str, str], secret: str = SIGN_KEY) -> str:
    """Sign a mobile API request the way libjnimain does.

    Sorted fields are joined with ``||``. postData is the swapped MD5 from
    TuyaApiSignManager. The native library then HMACs that string with
    ``A_<bmp key>_<app secret>`` and returns lowercase hex.
    """
    parts: list[str] = []
    for key in sorted(data):
        if key in {"gid", "sign"} or data[key] == "":
            continue
        value = _mobile_hash(data[key]) if key == "postData" else data[key]
        parts.append(f"{key}={value}")
    message = "||".join(parts)
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


class XiaohaiClient:
    """Async client. The password stays in memory and is never logged."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        country_code: str,
    ) -> None:
        self._session = session
        self._username = username.strip()
        self._password = password
        self._country_code = country_code.strip() or "86"
        self._sid: str | None = None
        self.last_room_note = ""
        self.last_thing_note = ""
        self._thing_cache: dict[str, dict[str, str]] = {}
        self.devices: dict[str, dict[str, Any]] = {}

    @property
    def uses_email(self) -> bool:
        return "@" in self._username

    async def async_login(self) -> dict[str, Any]:
        account_key = "email" if self.uses_email else "mobile"
        token_action = (
            "tuya.m.user.email.token.create"
            if self.uses_email
            else "tuya.m.user.mobile.token.get"
        )
        login_action = (
            "tuya.m.user.email.password.login"
            if self.uses_email
            else "tuya.m.user.mobile.passwd.login"
        )
        token_info = await self._request(
            token_action,
            {account_key: self._username, "countryCode": self._country_code},
            require_sid=False,
        )
        login_info = await self._request(
            login_action,
            {
                "countryCode": self._country_code,
                account_key: self._username,
                "passwd": password_digest(self._password),
                "options": '{"group": 1}',
                "token": token_info["token"],
                "ifencrypt": 0,
            },
            require_sid=False,
        )
        sid = login_info.get("sid")
        if not sid:
            raise XiaohaiAuthError("登录成功响应里没有 sid")
        self._sid = sid
        return login_info

    async def async_list_devices(self) -> list[dict[str, Any]]:
        homes = await self._request("tuya.m.location.list", None)
        if isinstance(homes, dict):
            homes = homes.get("list") or homes.get("groups") or []
        devices: list[dict[str, Any]] = []
        seen: set[str] = set()
        for home in homes or []:
            group_id = home.get("groupId") or home.get("gid") or home.get("ownerId")
            if group_id is None:
                continue
            listed = await self._request(
                "tuya.m.my.group.device.list",
                None,
                extra_params={"gid": str(group_id)},
            )
            if isinstance(listed, dict):
                listed = listed.get("devices") or listed.get("list") or []
            for device in listed or []:
                dev_id = device.get("devId")
                if not dev_id or dev_id in seen:
                    continue
                seen.add(dev_id)
                device["_group_id"] = group_id
                devices.append(device)
        return devices

    async def async_device_rooms(self, gid: Any) -> dict[str, str]:
        """Map devId to the cloud room name.

        shortcut: only the room list and the device sort list. A roomId of -1
        means the app has not placed that device in a room.
        """
        gid_s = str(gid)
        listed = await self._request(
            "tuya.m.room.list",
            {"gid": int(gid)},
            extra_params={"gid": gid_s},
        )
        names = {
            str(room.get("id")): room.get("name")
            for room in listed or []
            if isinstance(room, dict) and room.get("id") is not None and room.get("name")
        }
        found = rooms_from_payload(listed, names)
        if found:
            self.last_room_note = ""
            return found
        try:
            listed_devices = await self._request(
                "tuya.m.my.group.device.sort.list",
                {"gid": gid_s},
                extra_params={"gid": gid_s},
                version="1.0",
            )
        except XiaohaiError as err:
            self.last_room_note = str(err)
            return {}
        found = rooms_from_payload(listed_devices, names)
        if found:
            self.last_room_note = ""
            return found
        name_ids = ",".join(f"{key}:{value}" for key, value in list(names.items())[:8])
        self.last_room_note = f"sort.list {payload_shape(listed_devices)} names={name_ids}"
        return {}

    async def async_product_schema(self, gid: Any) -> dict[str, dict[str, str]]:
        """Map productId to datapoint code -> dp id, when the cloud has a schema."""
        try:
            result = await self._request(
                "tuya.m.product.standard.config.list",
                None,
                extra_params={"gid": str(gid)},
            )
        except XiaohaiError:
            return {}
        return schema_from_payload(result)

    async def async_ref_specs(self, gid: Any) -> dict[str, dict[str, str]]:
        """Per-product datapoints from the device reference list."""
        key = str(gid)
        if key in self._thing_cache:
            return self._thing_cache[key]
        try:
            result = await self._request(
                "tuya.m.device.ref.info.my.list",
                None,
                extra_params={"gid": key},
            )
        except XiaohaiError as err:
            self.last_thing_note = str(err)
            self._thing_cache[key] = {}
            return {}
        parsed = schema_from_payload(result)
        loose = thing_schema(result)
        if not parsed and loose:
            self.last_thing_note = ",".join(sorted(key for key in loose if not key.startswith("@"))[:20])
            self._thing_cache[key] = {}
            return {}
        self.last_thing_note = "" if parsed else payload_shape(result)
        self._thing_cache[key] = parsed
        return parsed

    async def async_publish(self, device: dict[str, Any], dps: dict[str, Any]) -> Any:
        """Publish datapoints through the Xiaohai cloud."""
        dev_id = device["devId"]
        gateway_id = device.get("meshId") or device.get("parentId") or dev_id
        return await self._request(
            "tuya.m.device.dp.publish",
            {
                "devId": dev_id,
                "gwId": gateway_id,
                "dps": json.dumps(dps, separators=(",", ":")),
            },
        )

    async def _request(
        self,
        action: str,
        payload: dict[str, Any] | None,
        *,
        extra_params: dict[str, str] | None = None,
        require_sid: bool = True,
        retry: bool = True,
        version: str | None = None,
    ) -> Any:
        if require_sid and not self._sid:
            await self.async_login()

        form: dict[str, str] = {
            "a": action,
            "clientId": CLIENT_ID,
            "v": version or API_VERSION,
            "time": str(int(time.time())),
            "et": ET_VERSION,
            "requestId": str(uuid.uuid4()),
        }
        if extra_params:
            form.update(extra_params)
        if require_sid and self._sid:
            form["sid"] = self._sid
        if payload is not None:
            form["postData"] = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

        form["sign"] = sign_request(form)
        try:
            async with self._session.post(
                API_URL,
                data=form,
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise XiaohaiError(f"无法连接小海云：{err}") from err

        if not isinstance(body, dict):
            raise XiaohaiError("小海云返回了无法识别的内容")
        if body.get("success"):
            return body.get("result")

        code = str(body.get("errorCode") or "")
        message = str(body.get("errorMsg") or code or "未知错误")
        if code in {"USER_SESSION_INVALID", "USER_SESSION_LOSS", "SING_VALIDATE_FALED_4", "SING_VALIDATE_FALED"} and retry and require_sid:
            self._sid = None
            await self.async_login()
            return await self._request(
                action,
                payload,
                extra_params=extra_params,
                require_sid=True,
                retry=False,
                version=version,
            )
        if code in {
            "USER_PASSWD_WRONG",
            "USER_NOT_EXISTS",
            "USER_NAME_NOT_EXIST",
            "NOT_EXISTS",
        } or "passwd" in message.lower() or "未注册" in message or "密码" in message:
            raise XiaohaiAuthError(message)
        if code == "1004" or "sign" in message.lower():
            raise XiaohaiError(f"签名被拒绝（{message}）。云端签名与 App 内置算法不一致。")
        raise XiaohaiError(f"{message}（{code}）" if code else message)
