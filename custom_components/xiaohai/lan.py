"""LAN control for a Xiaohai gateway, after the cloud has listed the devices.

The app encrypts a JSON body with the gateway local key and sends it to the
gateway on TCP 6668. Zigbee children are not on the network themselves: the
body names them with ``cid`` and ``ctype`` 0, and the frame command is
CONTROL_NEW (13). A protocol the gateway does not speak is left to the cloud.
"""

from __future__ import annotations

import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
import select
import socket
import struct
import time
import urllib.request
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
    payload = b"3.3" + (b"\x00" * 12) + _aes(key.encode("latin-1"), _pad(raw))
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


def _pieces(blob: bytes) -> list[bytes]:
    found = [blob]
    def opened(raw: bytes) -> None:
        if raw and len(raw) % 16 == 0:
            try:
                found.append(_aes(UDP_KEY, raw, decrypt=True))
            except Exception:
                return
    opened(blob)
    if blob.startswith(PREFIX) and len(blob) > 24:
        inner = blob[16:-8]
        found.append(inner)
        opened(inner)
        if inner.startswith(b"3.3"):
            opened(inner[15:])
    return found


def _notice(blob: bytes, sender: str) -> tuple[str, dict[str, str]] | None:
    for piece in _pieces(blob):
        for item in _json_objects(piece):
            noted = _note(item, sender)
            gateway = item.get("gwId") or item.get("devId")
            if noted and gateway:
                return str(gateway), noted
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


def _bind(port: int) -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("0.0.0.0", port))
        return sock
    except OSError:
        sock.close()
        return None


def _hosts(base: str) -> list[str]:
    return [f"{base}.{index}" for index in range(1, 255)]


def _add_base(bases: list[str], ip: str) -> None:
    if not _private_ip(ip):
        return
    base = ".".join(ip.split(".")[:3])
    if base not in bases:
        bases.append(base)


def _supervisor_ips() -> list[str]:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return []
    request = urllib.request.Request(
        "http://supervisor/network/info",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8", "ignore"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    ips: list[str] = []
    for iface in data.get("interfaces") or []:
        if not isinstance(iface, dict):
            continue
        addresses = (iface.get("ipv4") or {}).get("address") or []
        for address in addresses:
            ips.append(str(address).split("/")[0])
    return ips


def _route_ips() -> list[str]:
    try:
        rows = open("/proc/net/route", encoding="utf-8").read().splitlines()[1:]
    except OSError:
        return []
    ips: list[str] = []
    for row in rows:
        parts = row.split()
        if len(parts) < 8:
            continue
        try:
            dest, mask = int(parts[1], 16), int(parts[7], 16)
        except ValueError:
            continue
        if bin(mask).count("1") < 24:
            continue
        ips.append(socket.inet_ntoa(struct.pack("<I", dest)))
    return ips


def _networks() -> tuple[str, list[str]]:
    local = _local_ip()
    bases: list[str] = []
    for ip in _supervisor_ips():
        _add_base(bases, ip)
    _add_base(bases, local)
    for ip in _route_ips():
        _add_base(bases, ip)
    bases.sort(key=lambda base: base.startswith("172."))
    return local, bases[:3]


def _port_open(ip: str) -> str | None:
    sock = socket.socket()
    sock.settimeout(0.35)
    try:
        sock.connect((ip, TCP_PORT))
    except OSError:
        return None
    else:
        return ip
    finally:
        sock.close()


def _gateway_keys(devices: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for device in devices.values():
        kind = device.get("category") or device.get("categoryCode")
        key = device.get("localKey")
        dev_id = str(device.get("devId") or "")
        if kind in {"jzq", "wg2"} and dev_id and isinstance(key, str) and len(key) >= 16:
            pairs.append((dev_id, key[:16]))
    return pairs


def _accepts(data: bytes, key: str) -> bool:
    if len(data) < 16 or not data.startswith(PREFIX):
        return False
    length = struct.unpack(">I", data[12:16])[0]
    frame = data[: 16 + length]
    if len(frame) < 24 or not frame.endswith(SUFFIX):
        return False
    payload = frame[16:-8]
    if payload.startswith(b"3.3"):
        payload = payload[15:]
    if not payload or len(payload) % 16:
        return False
    try:
        plain = _unpad(_aes(key.encode("utf-8"), payload, decrypt=True))
    except Exception:
        return False
    if not plain:
        return False
    try:
        json.loads(plain)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return True


def _probe(ip: str, key: str) -> bool:
    frame = pack_33(2, 9, {"t": int(time.time())}, key)
    try:
        sock = socket.create_connection((ip, TCP_PORT), timeout=0.8)
    except OSError:
        return False
    try:
        sock.sendall(frame)
        sock.settimeout(0.8)
        try:
            data = sock.recv(1024)
        except OSError:
            return False
    finally:
        sock.close()
    return _accepts(data, key)


def pack_signed(seq: int, command: int, payload: bytes, key: bytes) -> bytes:
    """3.4 frame: HMAC-SHA256 instead of the CRC used by 3.3."""
    head = PREFIX + struct.pack(">III", seq, command, len(payload) + 36) + payload
    digest = hmac.new(key, head, hashlib.sha256).digest()
    return head + digest + SUFFIX


def control_34(cid: str, dps: dict[str, Any], now: int) -> dict[str, Any]:
    data: dict[str, Any] = {"dps": dps}
    if cid:
        data["cid"] = cid
    return {"protocol": 5, "t": now, "data": data}


def _read_frame(sock: socket.socket) -> bytes:
    sock.settimeout(1.2)
    data = b""
    while len(data) < 16:
        chunk = sock.recv(1024)
        if not chunk:
            return data
        data += chunk
    if not data.startswith(PREFIX):
        return data
    total = 16 + struct.unpack(">I", data[12:16])[0]
    while len(data) < total:
        chunk = sock.recv(1024)
        if not chunk:
            break
        data += chunk
    return data


def _remote_nonce(data: bytes, raw_key: bytes, nonce: bytes) -> bytes:
    if len(data) < 24 or not data.startswith(PREFIX):
        return b""
    length = struct.unpack(">I", data[12:16])[0]
    frame = data[: 16 + length]
    if len(frame) < 24 or not frame.endswith(SUFFIX):
        return b""
    if struct.unpack(">I", frame[8:12])[0] not in {3, 4}:
        return b""
    blobs = [frame[16:-8]]
    if len(frame) >= 52:
        blobs.insert(0, frame[16:-36])
    check = hmac.new(raw_key, nonce, hashlib.sha256).digest()
    for blob in blobs:
        plains = [blob] if len(blob) >= 48 else []
        chunks = (blob[4:], blob) if len(blob) > 4 else (blob,)
        for chunk in chunks:
            if not chunk or len(chunk) % 16:
                continue
            try:
                plains.append(_unpad(_aes(raw_key, chunk, decrypt=True)))
            except Exception:
                continue
        for plain in plains:
            if len(plain) < 48:
                continue
            signed = hmac.new(raw_key, plain[:16], hashlib.sha256).digest()
            if plain[16:48] in {check, signed}:
                return plain[:16]
            if plain[:32] == check:
                return plain[32:48]
    return b""


def _frame_body(data: bytes) -> tuple[int, bytes] | None:
    if len(data) < 52 or not data.startswith(PREFIX) or not data.endswith(SUFFIX):
        return None
    length = struct.unpack(">I", data[12:16])[0]
    frame = data[: 16 + length]
    if len(frame) < 52 or not frame.endswith(SUFFIX):
        return None
    command = struct.unpack(">I", frame[8:12])[0]
    return command, frame[16:-36]


def _open_session(sock: socket.socket, raw_key: bytes, *, encrypt: bool = True) -> bytes | None:
    nonce = os.urandom(16)
    payload = _aes(raw_key, nonce) if encrypt else nonce
    try:
        sock.sendall(pack_signed(1, 3, payload, raw_key))
        data = _read_frame(sock)
    except OSError:
        return None
    remote = _remote_nonce(data, raw_key, nonce)
    if not remote:
        return None
    finish = hmac.new(raw_key, remote, hashlib.sha256).digest()
    try:
        sock.sendall(pack_signed(2, 5, _aes(raw_key, _pad(finish)), raw_key))
        _read_frame(sock)
    except OSError:
        pass
    mixed = bytes(left ^ right for left, right in zip(nonce, remote))
    return _aes(raw_key, mixed)


def _session_kind(ip: str, raw_key: bytes) -> str | None:
    for encrypt in (True, False):
        try:
            sock = socket.create_connection((ip, TCP_PORT), timeout=0.8)
        except OSError:
            return "down"
        try:
            if _open_session(sock, raw_key, encrypt=encrypt):
                return "3.4" if encrypt else "3.4p"
        finally:
            sock.close()
    return None


def _key_materials(value: Any, label: str) -> list[tuple[str, bytes]]:
    if not isinstance(value, str) or len(value) < 16:
        return []
    raw = value.encode("utf-8")[:16]
    found = [(label, raw)]
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        decoded = b""
    if len(decoded) >= 16 and decoded[:16] != raw:
        found.append((f"{label}.b64", decoded[:16]))
    return found


def _materials(devices: dict[str, dict[str, Any]]) -> list[tuple[str, str, bytes]]:
    """Gateway keys first, then every other device. devKey is separate from localKey."""
    ranked: list[tuple[int, str, str, bytes]] = []
    for device in devices.values():
        kind = device.get("category") or device.get("categoryCode")
        rank = 0 if kind in {"jzq", "wg2"} else 1
        dev_id = str(device.get("devId") or "")
        if not dev_id:
            continue
        fields = ("localKey", "devKey") if rank == 0 else ("localKey",)
        for field in fields:
            for label, raw in _key_materials(device.get(field), field):
                ranked.append((rank, dev_id, label, raw))
    ranked.sort(key=lambda item: item[0])
    return [(dev_id, label, raw) for _rank, dev_id, label, raw in ranked]


def _clue(ip: str, raw_key: bytes) -> str:
    nonce = os.urandom(16)
    try:
        sock = socket.create_connection((ip, TCP_PORT), timeout=0.8)
    except OSError:
        return "连不上"
    try:
        sock.sendall(pack_signed(1, 3, _aes(raw_key, nonce), raw_key))
        data = _read_frame(sock)
    except OSError:
        data = b""
    finally:
        sock.close()
    if len(data) < 16 or not data.startswith(PREFIX):
        return f"会话回{len(data)}字节 {data[:8].hex()}"
    command, length = struct.unpack(">II", data[8:16])
    frame = data[: 16 + length]
    kind = "不能解密"
    body = frame[16:-8] if frame.endswith(SUFFIX) else b""
    if body and len(body) % 16 == 0:
        try:
            plain = _unpad(_aes(raw_key, body, decrypt=True))
        except Exception:
            plain = b""
        if plain.startswith((b"{", b"[")):
            kind = "解密为json"
        elif plain and all(32 <= byte < 127 for byte in plain[:8]):
            kind = "解密为文本"
        elif plain:
            kind = "解密为二进制"
    return f"会话回{len(data)} 命令{command} 长度{length} 尾{data[-4:].hex()} {kind}"


def _publish_34(ip: str, raw_key: bytes, cid: str, dps: dict[str, Any], *, plain: bool = False) -> str:
    try:
        sock = socket.create_connection((ip, TCP_PORT), timeout=3)
    except OSError:
        return "连接失败"
    try:
        session = _open_session(sock, raw_key, encrypt=not plain)
        if not session:
            return "会话失败"
        raw = b"3.4" + (b"\x00" * 12) + json.dumps(
            control_34(cid, dps, int(time.time())),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        sock.sendall(pack_signed(3, CONTROL_NEW, _aes(session, _pad(raw)), session))
    except OSError:
        return "连接失败"
    finally:
        sock.close()
    return "sent"


def _peek(ip: str, key: str) -> str:
    frame = pack_33(2, 9, {"t": int(time.time())}, key)
    try:
        sock = socket.create_connection((ip, TCP_PORT), timeout=0.8)
    except OSError:
        return "连不上"
    try:
        sock.sendall(frame)
        sock.settimeout(0.8)
        try:
            data = sock.recv(256)
        except OSError:
            return "无回应"
    finally:
        sock.close()
    if len(data) < 16 or not data.startswith(PREFIX):
        return f"非涂鸦{len(data)}字节"
    command = struct.unpack(">I", data[8:12])[0]
    return f"命令{command} {data[16:19].hex()}"


def _scan(devices: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, str]], str]:
    """Find gateways by who answers on TCP 6668 with a key from the cloud list.

    shortcut: only the /24s this host can see. Protocol 3.3 first, then a 3.4 session.
    """
    local, bases = _networks()
    if not bases:
        return {}, f"本机 {local}，没有可扫描的局域网段"
    materials = _materials(devices)
    open_ips: list[str] = []
    own = {local}
    for base in bases:
        with ThreadPoolExecutor(max_workers=64) as pool:
            for ip in pool.map(_port_open, _hosts(base)):
                if ip and ip not in own and ip not in open_ips:
                    open_ips.append(ip)
    found: dict[str, dict[str, str]] = {}
    for ip in open_ips[:8]:
        for dev_id, label, raw in materials:
            kind = _session_kind(ip, raw)
            if kind == "down":
                break
            if kind:
                found[dev_id] = {"ip": ip, "version": kind, "dev": dev_id, "material": label}
                break
        if found:
            break
    where = "、".join(open_ips[:4])
    forms = ",".join(dict.fromkeys(label for _dev, label, _raw in materials))
    peek = _clue(open_ips[0], materials[0][2]) if open_ips and materials and not found else ""
    note = (
        f"本机 {local}，扫描 {','.join(bases)}，6668 开放 {len(open_ips)}"
        + (f"（{where}）" if where else "")
        + (f"，密钥 {forms}" if forms else "")
        + (f"，{peek}" if peek else "")
    )
    return found, note


def discover_gateways(
    devices: dict[str, dict[str, Any]] | None = None,
    timeout: float = 1.5,
) -> tuple[dict[str, dict[str, str]], str]:
    """Listen briefly, then scan the local subnet for the gateway."""
    found: dict[str, dict[str, str]] = {}
    sockets = [sock for port in (0, 6666, 6667) if (sock := _bind(port))]
    if sockets:
        try:
            message = json.dumps({"from": "app", "ip": _local_ip()}).encode()
            encrypted = _aes(UDP_KEY, _pad(message))
            frames = (encrypted, pack(0, 0, encrypted))
            for port in (6666, 6667):
                for frame in frames:
                    try:
                        sockets[0].sendto(frame, ("255.255.255.255", port))
                    except OSError:
                        continue
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not found:
                ready, _, _ = select.select(sockets, [], [], 0.4)
                for sock in ready:
                    try:
                        blob, sender = sock.recvfrom(2048)
                    except OSError:
                        continue
                    noticed = _notice(blob, sender[0])
                    if noticed:
                        gateway, noted = noticed
                        found[gateway] = noted
        finally:
            for sock in sockets:
                sock.close()
    if found or not devices:
        return found, ""
    scanned, note = _scan(devices)
    found.update(scanned)
    return found, note


def private_gateway_ips(devices: dict[str, dict[str, Any]]) -> list[str]:
    """LAN addresses the cloud already attached to a gateway. Public addresses are ignored."""
    ips: list[str] = []
    for device in devices.values():
        kind = device.get("category") or device.get("categoryCode")
        if kind not in {"jzq", "wg2"}:
            continue
        ip = _private_ip(device.get("ip"))
        if ip and ip not in ips:
            ips.append(ip)
    return ips


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
    found = lan.get(gateway_id) or {}
    if not found and len(lan) == 1:
        found = next(iter(lan.values()))
    owner = devices.get(str(found.get("dev") or "")) or devices.get(gateway_id) or device
    label = str(found.get("material") or "localKey")
    picked = _key_materials(owner.get(label.split(".")[0]), label.split(".")[0])
    raw_key = next((item for name, item in picked if name == label), b"")
    if not raw_key:
        value = owner.get("localKey")
        raw_key = value.encode("utf-8")[:16] if isinstance(value, str) and len(value) >= 16 else b""
    if not raw_key:
        return "没有本地密钥"
    version = str(found.get("version") or "")
    ip = found.get("ip") or _private_ip(owner.get("ip"))
    if version.startswith("3.4") and ip:
        return _publish_34(ip, raw_key, cid, dps, plain=version == "3.4p")
    if version.startswith("3.5"):
        return f"网关协议 {version}"
    if not ip:
        return "没有发现网关"
    frame = pack_33(1, CONTROL_NEW, control_body(cid, dps, int(time.time())), raw_key.decode("latin-1"))
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
    hosts = _hosts("192.168.50")
    assert len(hosts) == 254 and "192.168.50.0" not in hosts and "192.168.50.255" not in hosts
    assert _accepts(pack_33(2, 9, {"t": 1}, "0123456789abcdef"), "0123456789abcdef")
    assert not _accepts(pack_33(2, 9, {"t": 1}, "0123456789abcdef"), "abcdef0123456789")
    signed = pack_signed(1, 3, b"\x11" * 16, b"0123456789abcdef")
    assert signed.endswith(SUFFIX) and struct.unpack(">I", signed[12:16])[0] == 52
    assert control_34("abcd", {"1": True}, 1)["data"]["cid"] == "abcd"
    coded = base64.b64encode(b"0123456789abcdef").decode()
    forms = _key_materials(coded, "localKey")
    assert forms[0][0] == "localKey" and ("localKey.b64", b"0123456789abcdef") in forms
    assert _materials({"g": {"devId": "g", "category": "jzq", "localKey": "0123456789abcdef", "devKey": "abcdef0123456789"}})[1][1] == "devKey"
    print("lan self-check ok")


if __name__ == "__main__":
    _self_check()
