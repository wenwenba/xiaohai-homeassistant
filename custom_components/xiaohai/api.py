"""Client for the Xiaohai private cloud (coli688 mobile API)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import aiohttp

from .const import API_URL, API_VERSION, APP_SECRET, CLIENT_ID, USER_AGENT


class XiaohaiError(Exception):
    """API returned an error."""


class XiaohaiAuthError(XiaohaiError):
    """Username or password was rejected, or the session expired."""


def _mobile_hash(data: str) -> str:
    digest = hashlib.md5(data.encode("utf-8")).hexdigest()
    return digest[8:16] + digest[0:8] + digest[24:32] + digest[16:24]


def _plain_rsa_encrypt(modulus: int, exponent: int, message: bytes) -> bytes:
    encrypted = pow(int.from_bytes(message, "big"), exponent, modulus)
    return encrypted.to_bytes(256, "big")


def encrypt_password(public_key: str, exponent: str, password: str) -> str:
    """Encrypt MD5(password) the way the app's email/mobile login does."""
    password_hash = hashlib.md5(password.encode("utf-8")).hexdigest().encode("utf-8")
    return _plain_rsa_encrypt(int(public_key), int(exponent), password_hash).hex()


def sign_request(data: dict[str, str], secret: str = APP_SECRET) -> str:
    """Sign a mobile API request.

    postData is replaced by the swapped MD5 used in TuyaApiSignManager.
    The remaining string is HMAC-SHA256 keyed with the app secret, which is
    the signer used by the public OEM mobile client against the same action
    names. The app itself finishes the hash inside a native library.
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
                account_key: self._username,
                "countryCode": self._country_code,
                "ifencrypt": 1,
                "options": '{"group": 1}',
                "passwd": encrypt_password(
                    str(token_info["publicKey"]),
                    str(token_info["exponent"]),
                    self._password,
                ),
                "token": token_info["token"],
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

    async def async_publish(self, device: dict[str, Any], dps: dict[str, Any]) -> Any:
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
    ) -> Any:
        if require_sid and not self._sid:
            await self.async_login()

        params: dict[str, str] = {
            "a": action,
            "clientId": CLIENT_ID,
            "v": API_VERSION,
            "time": str(int(time.time())),
        }
        if extra_params:
            params.update(extra_params)
        if require_sid and self._sid:
            params["sid"] = self._sid

        data: dict[str, str] = {}
        if payload is not None:
            data["postData"] = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

        params["sign"] = sign_request({**params, **data})
        try:
            async with self._session.post(
                API_URL,
                params=params,
                data=data,
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
        if code in {"USER_SESSION_INVALID", "SING_VALIDATE_FALED_4", "SING_VALIDATE_FALED"} and retry and require_sid:
            self._sid = None
            await self.async_login()
            return await self._request(
                action, payload, extra_params=extra_params, require_sid=True, retry=False
            )
        if code in {"USER_PASSWD_WRONG", "USER_NOT_EXISTS", "NOT_EXISTS"} or "passwd" in message.lower():
            raise XiaohaiAuthError(message)
        if code == "1004" or "sign" in message.lower():
            raise XiaohaiError(f"签名被拒绝（{message}）。云端签名与 App 内置算法不一致。")
        raise XiaohaiError(f"{message}（{code}）" if code else message)
