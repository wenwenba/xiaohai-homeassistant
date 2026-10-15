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
PREFIX35 = b"\x00\x00\x66\x99"
SUFFIX35 = b"\x00\x00\x99\x66"
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


def _cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(key[:16]), modes.CBC(iv[:16]))
    ctx = cipher.decryptor()
    return ctx.update(data) + ctx.finalize()


def _key_alts(raw_key: bytes) -> list[bytes]:
    """The library both uses the local key directly and runs it through MD5."""
    digest = hashlib.md5(raw_key).digest()
    if digest == raw_key:
        return [raw_key]
    return [raw_key, digest]


def _padded(data: bytes) -> bool:
    if not data:
        return False
    extra = data[-1]
    return 1 <= extra <= 16 and data.endswith(bytes([extra]) * extra)


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


def _gcm(key: bytes, iv: bytes, data: bytes, aad: bytes, *, decrypt: bool = False) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    box = AESGCM(key[:16])
    if decrypt:
        return box.decrypt(iv, data, aad)
    return box.encrypt(iv, data, aad)


def pack_35(seq: int, command: int, payload: bytes, key: bytes, iv: bytes | None = None) -> bytes:
    """Protocol 3.5: prefix 6699, 14-byte header as GCM additional data, 12-byte nonce."""
    iv = iv if iv is not None else os.urandom(12)
    length = len(payload) + 28
    aad = b"\x00\x00" + struct.pack(">III", seq, command, length)
    return PREFIX35 + aad + iv + _gcm(key, iv, payload, aad) + SUFFIX35


def unpack_35(data: bytes, key: bytes) -> tuple[int, bytes] | None:
    if len(data) < 50 or not data.startswith(PREFIX35):
        return None
    _seq, command, length = struct.unpack(">III", data[6:18])
    total = 18 + length + 4
    if length < 28 or len(data) < total or data[total - 4 : total] != SUFFIX35:
        return None
    iv = data[18:30]
    sealed = data[30 : 18 + length]
    try:
        plain = _gcm(key, iv, sealed, data[4:18], decrypt=True)
    except Exception:
        return None
    return command, plain


def _read_any(sock: socket.socket) -> bytes:
    sock.settimeout(1.2)
    data = b""
    while len(data) < 18:
        chunk = sock.recv(1024)
        if not chunk:
            return data
        data += chunk
    if data.startswith(PREFIX35) and len(data) >= 18:
        total = 18 + struct.unpack(">I", data[14:18])[0] + 4
    elif data.startswith(PREFIX) and len(data) >= 16:
        total = 16 + struct.unpack(">I", data[12:16])[0]
    else:
        return data
    while len(data) < total:
        chunk = sock.recv(1024)
        if not chunk:
            break
        data += chunk
    return data


def _read_frame(sock: socket.socket, timeout: float = 1.2) -> bytes:
    sock.settimeout(timeout)
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


def _signed_nonce(data: bytes, raw_key: bytes) -> bytes:
    """55AA reply: return code, ciphertext, then HMAC over everything before it."""
    if len(data) < 24 or not data.startswith(PREFIX):
        return b""
    length = struct.unpack(">I", data[12:16])[0]
    frame = data[: 16 + length]
    payload_len = length - 40
    if payload_len < 16 or payload_len % 16 or len(frame) < 20 + payload_len + 36:
        return b""
    if not frame.endswith(SUFFIX):
        return b""
    covered = frame[: 20 + payload_len]
    mac = frame[20 + payload_len : 52 + payload_len]
    if hmac.new(raw_key, covered, hashlib.sha256).digest() != mac:
        return b""
    try:
        plain = _unpad(_aes(raw_key, frame[20 : 20 + payload_len], decrypt=True))
    except Exception:
        return b""
    return plain[:16] if len(plain) >= 16 else b""


def _remote_nonce(data: bytes, raw_key: bytes, nonce: bytes) -> tuple[bytes, bytes] | None:
    signed = _signed_nonce(data, raw_key)
    if signed:
        return signed, raw_key
    if len(data) < 24 or not data.startswith(PREFIX):
        return None
    length = struct.unpack(">I", data[12:16])[0]
    frame = data[: 16 + length]
    if len(frame) < 24 or not frame.endswith(SUFFIX):
        return None
    if struct.unpack(">I", frame[8:12])[0] not in {3, 4}:
        return None
    blobs = [frame[16:-8]]
    if len(frame) >= 52:
        blobs.insert(0, frame[16:-36])
    for aes_key in _key_alts(raw_key):
        checks = [hmac.new(mac_key, nonce, hashlib.sha256).digest() for mac_key in _key_alts(raw_key)]
        for blob in blobs:
            plains = [blob] if len(blob) >= 48 else []
            chunks = (blob[4:], blob) if len(blob) > 4 else (blob,)
            for chunk in chunks:
                if not chunk or len(chunk) % 16:
                    continue
                try:
                    plains.append(_unpad(_aes(aes_key, chunk, decrypt=True)))
                except Exception:
                    pass
                if len(chunk) >= 32 and (len(chunk) - 16) % 16 == 0:
                    try:
                        plains.append(_unpad(_cbc(aes_key, chunk[:16], chunk[16:])))
                    except Exception:
                        pass
            for plain in plains:
                if len(plain) < 48:
                    continue
                signed = [hmac.new(mac_key, plain[:16], hashlib.sha256).digest() for mac_key in _key_alts(raw_key)]
                if plain[16:48] in checks or plain[16:48] in signed:
                    return plain[:16], aes_key
                if plain[:32] in checks:
                    return plain[32:48], aes_key
    return None


def _frame_body(data: bytes) -> tuple[int, bytes] | None:
    if len(data) < 52 or not data.startswith(PREFIX) or not data.endswith(SUFFIX):
        return None
    length = struct.unpack(">I", data[12:16])[0]
    frame = data[: 16 + length]
    if len(frame) < 52 or not frame.endswith(SUFFIX):
        return None
    command = struct.unpack(">I", frame[8:12])[0]
    return command, frame[16:-36]


def _open_session(sock: socket.socket, raw_key: bytes, *, encrypt: bool = True) -> tuple[bytes, bytes] | None:
    """Handshake only. Command 5 is not sent here: this gateway resets when it is written alone."""
    nonce = os.urandom(16)
    payload = _aes(raw_key, nonce) if encrypt else nonce
    try:
        sock.sendall(pack_signed(1, 3, payload, raw_key))
        data = _read_frame(sock)
    except OSError:
        return None
    opened = _remote_nonce(data, raw_key, nonce)
    if not opened:
        return None
    remote, aes_key = opened
    mixed = bytes(left ^ right for left, right in zip(nonce, remote))
    return _aes(aes_key, mixed), remote


def _local_command(raw_key: bytes, plain: bytes) -> bytes:
    """Control uses the cloud local key directly. Command 5 resets this gateway."""
    return pack_signed(1, CONTROL_NEW, _aes(raw_key, _pad(plain)), raw_key)


def _accepted(data: bytes, key: bytes) -> bool:
    """True when the gateway signed the reply and did not return an error code."""
    if len(data) < 24 or not data.startswith(PREFIX):
        return False
    length = struct.unpack(">I", data[12:16])[0]
    frame = data[: 16 + length]
    if len(frame) < 24 or not frame.endswith(SUFFIX):
        return False
    if length < 36 or hmac.new(key, frame[:-36], hashlib.sha256).digest() != frame[-36:-4]:
        return False
    # A small value here is the gateway's return code. Ciphertext is larger.
    retcode = struct.unpack(">I", frame[16:20])[0]
    return retcode == 0 or retcode > 16


def _open_session_35(sock: socket.socket, raw_key: bytes) -> bytes | None:
    nonce = os.urandom(16)
    try:
        sock.sendall(pack_35(1, 3, nonce, raw_key))
        opened = unpack_35(_read_any(sock), raw_key)
    except OSError:
        return None
    if not opened or opened[0] not in {3, 4} or len(opened[1]) < 48:
        return None
    remote = opened[1][:16]
    check = hmac.new(raw_key, nonce, hashlib.sha256).digest()
    if opened[1][16:48] != check:
        return None
    finish = hmac.new(raw_key, remote, hashlib.sha256).digest()
    try:
        sock.sendall(pack_35(2, 5, finish, raw_key))
        _read_any(sock)
    except OSError:
        pass
    mixed = bytes(left ^ right for left, right in zip(nonce, remote))
    return _gcm(raw_key, nonce[:12], mixed, b"")[:16]


def _session_kind(ip: str, raw_key: bytes) -> str | None:
    try:
        sock = socket.create_connection((ip, TCP_PORT), timeout=0.8)
    except OSError:
        return "down"
    try:
        if _open_session_35(sock, raw_key):
            return "3.5"
    finally:
        sock.close()
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


def _census(body: bytes, keys: list[bytes]) -> str:
    ecb = md5_hits = cbc_hits = zero_hits = 0
    unique: list[bytes] = []
    for key in keys:
        if key not in unique:
            unique.append(key)
    zero = b"\x00" * 16
    for key in unique:
        digest = hashlib.md5(key).digest()
        if body and len(body) % 16 == 0:
            for secret, slot in ((key, "ecb"), (digest, "md5")):
                try:
                    if _padded(_aes(secret, body, decrypt=True)):
                        if slot == "ecb":
                            ecb += 1
                        else:
                            md5_hits += 1
                except Exception:
                    pass
            try:
                if _padded(_cbc(key, zero, body)):
                    zero_hits += 1
            except Exception:
                pass
        if len(body) >= 32 and (len(body) - 16) % 16 == 0:
            try:
                if _padded(_cbc(key, body[:16], body[16:])):
                    cbc_hits += 1
            except Exception:
                pass
    return f"ECB{ecb} MD5{md5_hits} CBC{cbc_hits} 零IV{zero_hits} 共{len(unique)}"


def _frame_name(data: bytes) -> str:
    if data.startswith(PREFIX35):
        return "6699"
    if data.startswith(PREFIX):
        return "55aa"
    return f"{len(data)}字节"


def _clue(ip: str, keys: list[bytes]) -> str:
    raw_key = keys[0]
    nonce = os.urandom(16)
    try:
        sock = socket.create_connection((ip, TCP_PORT), timeout=0.8)
    except OSError:
        return "连不上"
    try:
        sock.sendall(pack_35(1, 3, nonce, raw_key))
        newer = _read_any(sock)
    except OSError:
        newer = b""
    finally:
        sock.close()
    if unpack_35(newer, raw_key):
        newer_note = "3.5解开"
    elif newer.startswith(PREFIX35):
        newer_note = "3.5标签失败"
    else:
        newer_note = f"3.5回{_frame_name(newer)}"
    nonce = os.urandom(16)
    try:
        sock = socket.create_connection((ip, TCP_PORT), timeout=0.8)
    except OSError:
        return f"{newer_note} 连不上"
    try:
        sock.sendall(pack_signed(1, 3, _aes(raw_key, nonce), raw_key))
        data = _read_frame(sock)
    except OSError:
        data = b""
    finally:
        sock.close()
    if len(data) < 16 or not data.startswith(PREFIX):
        return f"{newer_note} 会话回{len(data)}字节 {data[:8].hex()}"
    command, length = struct.unpack(">II", data[8:16])
    frame = data[: 16 + length]
    body = frame[16:-8] if len(frame) >= 24 and frame.endswith(SUFFIX) else b""
    signed = sum(1 for key in dict.fromkeys(keys) if _signed_nonce(data, key))
    return f"{newer_note} 会话回{len(data)} 命令{command} 长度{length} 帧内校验{signed} {_census(body, keys)}"


def _publish_35(ip: str, raw_key: bytes, cid: str, dps: dict[str, Any]) -> str:
    try:
        sock = socket.create_connection((ip, TCP_PORT), timeout=3)
    except OSError:
        return "连接失败"
    try:
        session = _open_session_35(sock, raw_key)
        if not session:
            return "会话失败"
        raw = b"3.5" + (b"\x00" * 12) + json.dumps(
            control_34(cid, dps, int(time.time())),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        sock.sendall(pack_35(3, CONTROL_NEW, raw, session))
    except OSError:
        return "连接失败"
    finally:
        sock.close()
    return "sent"


def _shape(data: bytes, key: bytes) -> str:
    """Reply outline only: length, command, and whether the signature matches."""
    if not data:
        return "回0"
    if len(data) < 16 or not data.startswith(PREFIX):
        return f"回{len(data)}非帧"
    command, length = struct.unpack(">II", data[8:16])
    frame = data[: 16 + length] if len(data) >= 16 + length else data
    marks: list[str] = []
    if (
        len(frame) >= 52
        and frame.endswith(SUFFIX)
        and hmac.new(key, frame[:-36], hashlib.sha256).digest() == frame[-36:-4]
    ):
        marks.append("签名")
    if (
        len(frame) >= 24
        and frame.endswith(SUFFIX)
        and (binascii.crc32(frame[:-8]) & 0xFFFFFFFF) == struct.unpack(">I", frame[-8:-4])[0]
    ):
        marks.append("CRC")
    code = struct.unpack(">I", frame[16:20])[0] if len(frame) >= 20 else -1
    return f"回{len(data)}命令{command}{'/'.join(marks) or '无校验'}码{code}"


def _crc_ok(data: bytes, key: bytes) -> bool:
    if len(data) < 24 or not data.startswith(PREFIX):
        return False
    length = struct.unpack(">I", data[12:16])[0]
    frame = data[: 16 + length]
    if len(frame) < 24 or not frame.endswith(SUFFIX):
        return False
    if (binascii.crc32(frame[:-8]) & 0xFFFFFFFF) != struct.unpack(">I", frame[-8:-4])[0]:
        return False
    payload = frame[16:-8]
    if payload.startswith(b"3.3") or payload.startswith(b"3.4"):
        payload = payload[15:]
    if not payload or len(payload) % 16:
        return False
    try:
        plain = _unpad(_aes(key[:16], payload, decrypt=True))
    except Exception:
        return False
    return plain.startswith(b"{") and b"unvalid" not in plain and b"error" not in plain.lower()


def _swap_frames(raw_key: bytes, nonce: bytes, remote: bytes, plain: bytes) -> tuple[bytes, bytes]:
    """Command 5 is the 32-byte HMAC of the gateway nonce, then control uses the swapped key."""
    finish = hmac.new(raw_key, remote, hashlib.sha256).digest()
    mixed = bytes(left ^ right for left, right in zip(nonce, remote))
    session = _aes(raw_key, mixed)
    blob = pack_signed(2, 5, _aes(raw_key, finish), raw_key)
    blob += pack_signed(3, CONTROL_NEW, _aes(session, _pad(plain)), session)
    return blob, session


def _frame_command(data: bytes) -> int:
    if len(data) < 12 or not data.startswith(PREFIX):
        return -1
    return struct.unpack(">I", data[8:12])[0]


def _publish_34(ip: str, raw_key: bytes, cid: str, dps: dict[str, Any], *, plain: bool = False) -> str:
    """Finish the key swap, then send control on that same connection."""
    now = int(time.time())
    body = control_body(cid, dps, now) if plain else control_34(cid, dps, now)
    wrapped = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    if not plain:
        wrapped = b"3.4" + (b"\x00" * 12) + wrapped
    nonce = os.urandom(16)
    try:
        sock = socket.create_connection((ip, TCP_PORT), timeout=3)
    except OSError:
        return "连接失败"
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.sendall(pack_signed(1, 3, _aes(raw_key, nonce), raw_key))
        remote = _signed_nonce(_read_frame(sock), raw_key)
        if len(remote) != 16:
            return "会话失败"
        blob, session = _swap_frames(raw_key, nonce, remote, wrapped)
        sock.sendall(blob)
        first = _read_frame(sock, 2.5)
        try:
            second = _read_frame(sock, 1.2)
        except OSError:
            second = b""
    except OSError:
        return "连接失败"
    finally:
        sock.close()

    def accepted(data: bytes) -> bool:
        if _frame_command(data) in {3, 5}:
            return False
        return _accepted(data, raw_key) or _accepted(data, session) or _crc_ok(data, raw_key)

    if accepted(first) or accepted(second):
        return "sent"
    return "网关未确认 " + _shape(first, raw_key) + " " + _shape(second, session)


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


def _mac_note(devices: dict[str, dict[str, Any]], ip: str) -> str:
    """Say whether this host is one of the cloud devices, without logging the MAC."""
    try:
        rows = open("/proc/net/arp", encoding="utf-8").read().splitlines()[1:]
    except OSError:
        return "没有 ARP"
    mac = ""
    for row in rows:
        parts = row.split()
        if len(parts) >= 4 and parts[0] == ip:
            mac = parts[3].lower().replace("-", ":")
            break
    if not mac or mac == "00:00:00:00:00:00":
        return "没有读到 MAC"
    for device in devices.values():
        own = str(device.get("mac") or "").lower().replace("-", ":")
        if own and own == mac:
            return "MAC 与设备名单一致"
    return "MAC 不在设备名单里"


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
    local_keys = [raw for _dev, label, raw in materials if label == "localKey"]
    peek = _clue(open_ips[0], local_keys) if open_ips and local_keys and not found else ""
    note = (
        f"本机 {local}，扫描 {','.join(bases)}，6668 开放 {len(open_ips)}"
        + (f"（{where}）" if where else "")
        + (f"，密钥 {forms}" if forms else "")
        + (f"，{peek}" if peek else "")
        + (f"，{_mac_note(devices, open_ips[0])}" if open_ips else "")
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
    if version.startswith("3.5") and ip:
        return _publish_35(ip, raw_key, cid, dps)
    if version.startswith("3.4") and ip:
        return _publish_34(ip, raw_key, cid, dps, plain=version == "3.4p")
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
    assert _mac_note({}, "127.0.0.1") in {"没有 ARP", "没有读到 MAC", "MAC 不在设备名单里"}
    assert len(_key_alts(b"0123456789abcdef")) == 2
    assert _padded(b"\x04\x04\x04\x04") and not _padded(b"\x01\x02")
    sample = b"0123456789abcdef"
    sealed = pack_35(7, 3, sample, sample, iv=b"0123456789ab")
    assert sealed.startswith(PREFIX35) and sealed.endswith(SUFFIX35)
    assert unpack_35(sealed, sample) == (3, sample)
    assert unpack_35(sealed, b"abcdef0123456789") is None
    assert len(_gcm(sample, b"0123456789ab", sample, b"")) == 32
    cipher = _aes(sample, _pad(sample))
    covered = PREFIX + struct.pack(">III", 1, 3, 40 + len(cipher)) + b"\x00\x00\x00\x00" + cipher
    reply = covered + hmac.new(sample, covered, hashlib.sha256).digest() + SUFFIX
    assert _signed_nonce(reply, sample) == sample
    assert _signed_nonce(reply, b"abcdef0123456789") == b""
    command = _local_command(sample, b"hello")
    assert struct.unpack(">I", command[8:12])[0] == CONTROL_NEW
    swapped, session = _swap_frames(sample, sample, sample, b"3.4" + (b"\x00" * 12) + b"{}")
    assert struct.unpack(">I", swapped[8:12])[0] == 5
    assert len(_aes(sample, hmac.new(sample, sample, hashlib.sha256).digest())) == 32
    assert len(session) == 16
    second = swapped.find(PREFIX, 4)
    assert struct.unpack(">I", swapped[second + 8 : second + 12])[0] == CONTROL_NEW
    ok = PREFIX + struct.pack(">IIII", 1, CONTROL_NEW, 4 + 16 + 36, 0) + (b"\x11" * 16)
    ok += hmac.new(sample, ok, hashlib.sha256).digest() + SUFFIX
    bad = PREFIX + struct.pack(">IIII", 1, CONTROL_NEW, 4 + 16 + 36, 1) + (b"\x11" * 16)
    bad += hmac.new(sample, bad, hashlib.sha256).digest() + SUFFIX
    assert _accepted(ok, sample)
    assert not _accepted(bad, sample)
    assert not _accepted(ok, b"abcdef0123456789")
    assert not _accepted(b"", sample)
    assert _shape(b"", sample) == "回0"
    assert "签名" in _shape(ok, sample)
    print("lan self-check ok")


if __name__ == "__main__":
    _self_check()
