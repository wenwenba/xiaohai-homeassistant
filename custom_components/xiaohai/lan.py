"""LAN control for a Xiaohai gateway, after the cloud has listed the devices.

The app encrypts a JSON body with the gateway local key and sends it to the
gateway on TCP 6668. Zigbee children are not on the network themselves: the
body names them with ``cid`` and ``ctype`` 0, and the frame command is
CONTROL_NEW (13). A protocol the gateway does not speak is left to the cloud.
"""

from __future__ import annotations

import binascii
import json
import socket
import struct
import time
from typing import Any

PREFIX = b"\x00\x00\x55\xaa"
SUFFIX = b"\x00\x00\xaa\x55"
UDP_KEY = b"yGAdlopoPVldABfn"
CONTROL_NEW = 13
TCP_PORT = 6668

def route(device: dict[str, Any]) -> tuple[str, str]:
    """Return the gateway id and the child id the gateway expects.

    When the communication node is the device itself, it is the gateway and
    there is no child id.
    """
    comm = device.get("communication")
    node = comm.get("communicationNode") if isinstance(comm, dict) else None
    gateway = str(node or device.get("parentId") or device.get("meshId") or device.get("devId") or "")
    cid = str(device.get("nodeId") or "")
    topo = device.get("deviceTopo")
    if not cid and isinstance(topo, dict):
        cid = str(topo.get("nodeId") or "")
    if not cid or cid == gateway or gateway == str(device.get("devId") or ""):
        cid = ""
    return gateway, cid


def control_body(cid: str, dps: dict[str, Any], now: int) -> dict[str, Any]:
    body: dict[str, Any] = {"dps": dps, "t": now}
    if cid:
        body["cid"] = cid
        body["ctype"] = 0
    return body


def _private_ip(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) != 4:
        return None
    try:
        first, second = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if first == 10 or (first == 192 and second == 168) or (first == 172 and 16 <= second <= 31):
        return value
    return None


def _pad(data: bytes) -> bytes:
    extra = 16 - (len(data) % 16)
    return data + bytes([extra]) * extra


def _unpad(data: bytes) -> bytes:
    extra = data[-1]
    if 1 <= extra <= 16 and data.endswith(bytes([extra]) * extra):
        return data[:-extra]
    return data


def _aes(key: bytes, data: bytes, *, decrypt: bool = False) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(key[:16]), modes.ECB())
    ctx = cipher.decryptor() if decrypt else cipher.encryptor()
    return ctx.update(data) + ctx.finalize()


def pack(seq: int, command: int, payload: bytes) -> bytes:
    head = PREFIX + struct.pack(">III", seq, command, len(payload) + 8) + payload
    crc = binascii.crc32(head) & 0xFFFFFFFF
    return head + struct.pack(">I", crc) + SUFFIX


def pack_33(seq: int, command: int, body: dict[str, Any], key: str) -> bytes:
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    payload = b"3.3" + (b"\x00" * 12) + _aes(key.encode("utf-8"), _pad(raw))
    return pack(seq, command, payload)


def unpack_33(frame: bytes, key: str) -> dict[str, Any]:
    payload = frame[16:-8]
    if payload.startswith(b"3.3"):
        payload = payload[15:]
    return json.loads(_unpad(_aes(key.encode("utf-8"), payload, decrypt=True)))


def _json_objects(blob: bytes) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    text = blob.decode("utf-8", "ignore")
    start = 0
    while True:
        left = text.find("{", start)
        if left < 0:
            return found
        right = text.find("}", left)
        if right < 0:
            return found
        try:
            item = json.loads(text[left : right + 1])
        except json.JSONDecodeError:
            start = left + 1
            continue
        if isinstance(item, dict):
            found.append(item)
        start = right + 1


def _note(item: dict[str, Any], sender: str) -> dict[str, str] | None:
    gateway = item.get("gwId") or item.get("devId")
    if not gateway:
        return None
    ip = _private_ip(item.get("ip")) or _private_ip(sender)
    if not ip:
        return None
    return {"ip": ip, "version": str(item.get("version") or "")}


def _read_datagram(blob: bytes, sender: str) -> dict[str, str] | None:
    for item in _json_objects(blob):
        noted = _note(item, sender)
        if noted:
            return noted
    try:
        opened = _aes(UDP_KEY, blob, decrypt=True)
    except Exception:
        opened = b""
    for item in _json_objects(opened):
        noted = _note(item, sender)
        if noted:
            return noted
    if blob.startswith(PREFIX) and len(blob) > 24:
        return _read_datagram(blob[16:-8], sender)
    return None


def _local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("255.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        sock.close()


def discover_gateways(timeout: float = 2.0) -> dict[str, dict[str, str]]:
    """Broadcast for gateways and return gwId -> {ip, version}.

    shortcut: one short broadcast. A gateway that does not answer is left for
    the cloud path.
    """
    found: dict[str, dict[str, str]] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.4)
        sock.bind(("0.0.0.0", 0))
        message = json.dumps({"from": "app", "ip": _local_ip()}).encode()
        encrypted = _aes(UDP_KEY, _pad(message))
        frames = (encrypted, pack(0, 0, encrypted))
        for port in (6666, 6667):
            for frame in frames:
                try:
                    sock.sendto(frame, ("255.255.255.255", port))
                except OSError:
                    continue
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                blob, sender = sock.recvfrom(2048)
            except socket.timeout:
                continue
            gateway = _gateway_id(blob, sender[0])
            noted = _read_datagram(blob, sender[0])
            if gateway and noted:
                found[gateway] = noted
    finally:
        sock.close()
    return found


def _gateway_id(blob: bytes, sender: str) -> str | None:
    blobs = [blob]
    try:
        blobs.append(_aes(UDP_KEY, blob, decrypt=True))
    except Exception:
        pass
    if blob.startswith(PREFIX) and len(blob) > 24:
        blobs.append(blob[16:-8])
    for item_blob in blobs:
        for item in _json_objects(item_blob):
            gateway = item.get("gwId") or item.get("devId")
            if gateway and (_private_ip(item.get("ip")) or _private_ip(sender)):
                return str(gateway)
    return None


def publish_local(
    lan: dict[str, dict[str, str]],
    devices: dict[str, dict[str, Any]],
    device: dict[str, Any],
    dps: dict[str, Any],
) -> str:
    """Send one command to the gateway. Returns ``sent`` or why it did not."""
    gateway_id, cid = route(device)
    if gateway_id != str(device.get("devId") or "") and not cid:
        return "没有子设备编号"
    gateway = devices.get(gateway_id) or device
    key = gateway.get("localKey")
    if not isinstance(key, str) or len(key) < 16:
        return "没有本地密钥"
    found = lan.get(gateway_id) or {}
    version = str(found.get("version") or "")
    if version.startswith("3.4") or version.startswith("3.5"):
        return f"网关协议 {version}"
    ip = found.get("ip") or _private_ip(gateway.get("ip"))
    if not ip:
        return "没有发现网关"
    frame = pack_33(1, CONTROL_NEW, control_body(cid, dps, int(time.time())), key[:16])
    try:
        sock = socket.create_connection((ip, TCP_PORT), timeout=3)
    except OSError:
        return "连接失败"
    try:
        sock.sendall(frame)
        sock.settimeout(1.5)
        try:
            sock.recv(1024)
        except socket.timeout:
            pass
    except OSError:
        return "连接失败"
    finally:
        sock.close()
    return "sent"


def _self_check() -> None:
    device = {
        "devId": "child",
        "nodeId": "abcd",
        "communication": {"communicationNode": "gw"},
    }
    assert route(device) == ("gw", "abcd")
    assert route({"devId": "gw", "communication": {"communicationNode": "gw"}}) == ("gw", "")
    body = control_body("abcd", {"1": True}, 1)
    assert body["cid"] == "abcd" and body["ctype"] == 0
    assert "cid" not in control_body("", {"1": True}, 1)
    key = "0123456789abcdef"
    frame = pack_33(1, CONTROL_NEW, body, key)
    assert frame.startswith(PREFIX) and frame.endswith(SUFFIX)
    assert struct.unpack(">I", frame[8:12])[0] == CONTROL_NEW
    assert unpack_33(frame, key) == body
    assert _private_ip("171.218.238.58") is None
    assert _private_ip("192.168.50.8") == "192.168.50.8"
    print("lan self-check ok")


if __name__ == "__main__":
    _self_check()
