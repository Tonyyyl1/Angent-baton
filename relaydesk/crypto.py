"""Cryptographic identity, Ed25519 signatures, and streaming encryption for cloud relay.

PyNaCl (libsodium) is imported lazily so stdlib-only commands keep working when
PyNaCl is not installed.
"""
import base64
import binascii
import copy
import hashlib
import json
import os
import secrets
import struct
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "CryptoError",
    "CryptoUnavailableError",
    "TamperError",
    "has_crypto",
    "require_crypto",
    "ensure_identity",
    "public_descriptor",
    "device_fingerprint",
    "pair_fingerprint",
    "sign_json",
    "verify_json",
    "encrypt_package",
    "decrypt_package",
    "encrypt_meta",
    "decrypt_meta",
]

CRYPTO_UNAVAILABLE_MSG = (
    "云接力加密需要 PyNaCl（libsodium）。请先运行：python3 -m pip install pynacl"
)

PUBLIC_DESCRIPTOR_FIELDS = (
    "deviceId",
    "alias",
    "name",
    "os",
    "host",
    "verifyPublicKey",
    "boxPublicKey",
    "keyFingerprint",
    "createdAt",
)

DEFAULT_CHUNK_SIZE = 65536
MAX_FRAME_CIPHERTEXT_BYTES = 64 * 1024 * 1024  # hard safety cap against bogus frame headers


class CryptoError(Exception):
    """Base error for cryptographic operations in relay-desk."""


class CryptoUnavailableError(CryptoError):
    """Raised when PyNaCl (libsodium) is required but not installed."""


class TamperError(CryptoError):
    """Raised when signature verification, MAC check, hash, or stream framing fails."""


# ---------------------------------------------------------------- availability


def _load_nacl():
    import nacl.bindings as bindings  # type: ignore
    import nacl.public as public  # type: ignore
    import nacl.signing as signing  # type: ignore
    import nacl.utils as utils  # type: ignore

    if not hasattr(bindings, "crypto_secretstream_xchacha20poly1305_init_push"):
        raise ImportError("libsodium secretstream bindings unavailable")
    return bindings, signing, public, utils


def has_crypto() -> bool:
    """Return True if PyNaCl and libsodium secretstream bindings are importable."""
    try:
        _load_nacl()
        return True
    except Exception:
        return False


def require_crypto() -> None:
    """Ensure PyNaCl is available or raise CryptoUnavailableError."""
    if not has_crypto():
        raise CryptoUnavailableError(CRYPTO_UNAVAILABLE_MSG)


def _require_nacl():
    try:
        return _load_nacl()
    except Exception:
        raise CryptoUnavailableError(CRYPTO_UNAVAILABLE_MSG)


# ---------------------------------------------------------------- internal helpers


def _now_iso() -> str:
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(b64_str: Any, label: str = "密钥数据", tamper: bool = True) -> bytes:
    err_cls = TamperError if tamper else CryptoError
    if not isinstance(b64_str, str) or not b64_str.strip():
        raise err_cls("%s缺失或格式无效。" % label)
    try:
        return base64.b64decode(b64_str.strip().encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise err_cls("%s Base64 解码失败。" % label)


def _canonical_json_bytes(obj: Dict[str, Any]) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _format_hex_groups(hex_upper: str, group_size: int = 4) -> str:
    return "-".join(
        hex_upper[i : i + group_size] for i in range(0, len(hex_upper), group_size)
    )


def _replace_with_retry(tmp_path: str, dest_path: str) -> None:
    last_err = None
    for _ in range(20):
        try:
            os.replace(tmp_path, dest_path)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(0.05)
    if last_err is not None:
        raise last_err


def _write_private_json(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent),
            prefix="." + path.name + "-",
            suffix=".tmp",
        )
    except OSError as e:
        raise CryptoError("无法创建本机身份文件 %s：%s" % (path, e))

    try:
        if os.name != "nt":
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        _replace_with_retry(tmp, str(path))
        if os.name != "nt":
            os.chmod(str(path), 0o600)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise CryptoError("无法写入本机身份文件 %s：%s" % (path, e))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sha256_file(path: Path) -> Tuple[int, str]:
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(1024 * 1024)
            if not buf:
                break
            total += len(buf)
            h.update(buf)
    return total, h.hexdigest()


# ---------------------------------------------------------------- identity & fingerprints


def device_fingerprint(verify_pub_b64: str, box_pub_b64: str) -> str:
    """Compute human-verifiable device key fingerprint (XXXX-XXXX-XXXX-XXXX)."""
    verify_raw = _b64d(verify_pub_b64, "签名公钥", tamper=False)
    box_raw = _b64d(box_pub_b64, "加密公钥", tamper=False)
    digest = (
        hashlib.sha256(b"relaydesk-dev-v1:" + verify_raw + b":" + box_raw)
        .hexdigest()
        .upper()[:16]
    )
    return _format_hex_groups(digest, 4)


def pair_fingerprint(desc_1: dict, desc_2: dict) -> str:
    """Compute symmetric order-independent SAS pairing fingerprint (24 hex chars)."""
    if not isinstance(desc_1, dict) or not isinstance(desc_2, dict):
        raise CryptoError("设备公钥描述必须是字典对象。")

    def _canon_tuple(desc: dict) -> Tuple[str, str, str]:
        dev_id = desc.get("deviceId")
        v_pub = desc.get("verifyPublicKey")
        b_pub = desc.get("boxPublicKey")
        if not isinstance(dev_id, str) or not dev_id.strip():
            raise CryptoError("设备描述缺少有效的 deviceId。")
        v_norm = _b64e(_b64d(v_pub, "签名公钥", tamper=False))
        b_norm = _b64e(_b64d(b_pub, "加密公钥", tamper=False))
        return (dev_id.strip(), v_norm, b_norm)

    ordered = sorted([_canon_tuple(desc_1), _canon_tuple(desc_2)])
    canonical_bytes = (
        b"relaydesk-pair-v1:"
        + json.dumps(ordered, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    )
    digest = hashlib.sha256(canonical_bytes).hexdigest().upper()[:24]
    return _format_hex_groups(digest, 4)


def ensure_identity(
    local_home,
    alias: str = "",
    name: str = "",
    os_name: str = "",
    host: str = "",
) -> dict:
    """Load or generate `<local_home>/identity.json` with 0600 permissions."""
    home_path = Path(local_home)
    ident_path = home_path / "identity.json"
    required_keys = (
        "deviceId",
        "signPrivateKey",
        "verifyPublicKey",
        "boxPrivateKey",
        "boxPublicKey",
    )

    if ident_path.exists():
        try:
            with open(ident_path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError) as e:
            raise CryptoError("读取本机身份文件失败 %s：%s" % (ident_path, e))
        if not isinstance(data, dict):
            raise CryptoError("本机身份文件格式无效（顶层不是对象）：%s" % ident_path)
        for k in required_keys:
            if not isinstance(data.get(k), str) or not data[k].strip():
                raise CryptoError("本机身份文件缺少必要密钥字段 %s：%s" % (k, ident_path))
        # Validate stored key lengths
        for k in ("signPrivateKey", "verifyPublicKey", "boxPrivateKey", "boxPublicKey"):
            raw = _b64d(data[k], k, tamper=False)
            if len(raw) != 32:
                raise CryptoError("本机身份文件密钥长度异常 %s：%s" % (k, ident_path))
    else:
        _, signing, public, _ = _require_nacl()
        sk = signing.SigningKey.generate()
        bk = public.PrivateKey.generate()
        data = {
            "deviceId": "dev_" + secrets.token_hex(8),
            "alias": "",
            "name": "",
            "os": "",
            "host": "",
            "signPrivateKey": _b64e(sk.encode()),
            "verifyPublicKey": _b64e(sk.verify_key.encode()),
            "boxPrivateKey": _b64e(bk.encode()),
            "boxPublicKey": _b64e(bk.public_key.encode()),
            "keyFingerprint": "",
            "createdAt": _now_iso(),
        }

    data.setdefault("alias", "")
    data.setdefault("name", "")
    data.setdefault("os", "")
    data.setdefault("host", "")
    data.setdefault("createdAt", _now_iso())

    if alias:
        data["alias"] = str(alias)
    if name:
        data["name"] = str(name)
    if os_name:
        data["os"] = str(os_name)
    if host:
        data["host"] = str(host)

    data["keyFingerprint"] = device_fingerprint(
        data["verifyPublicKey"], data["boxPublicKey"]
    )
    _write_private_json(ident_path, data)
    return copy.deepcopy(data)


def public_descriptor(identity: dict) -> dict:
    """Extract strictly public identity fields (never exposing private keys)."""
    if not isinstance(identity, dict):
        raise CryptoError("身份对象必须是字典。")
    for req in ("deviceId", "verifyPublicKey", "boxPublicKey"):
        if not isinstance(identity.get(req), str) or not identity[req].strip():
            raise CryptoError("身份对象缺少必要字段：%s" % req)

    fp = identity.get("keyFingerprint")
    if not isinstance(fp, str) or not fp.strip():
        fp = device_fingerprint(identity["verifyPublicKey"], identity["boxPublicKey"])

    out = {
        "deviceId": str(identity["deviceId"]),
        "alias": str(identity.get("alias") or ""),
        "name": str(identity.get("name") or ""),
        "os": str(identity.get("os") or ""),
        "host": str(identity.get("host") or ""),
        "verifyPublicKey": str(identity["verifyPublicKey"]),
        "boxPublicKey": str(identity["boxPublicKey"]),
        "keyFingerprint": fp,
        "createdAt": str(identity.get("createdAt") or ""),
    }
    return {k: out[k] for k in PUBLIC_DESCRIPTOR_FIELDS}


# ---------------------------------------------------------------- Ed25519 JSON signing


def sign_json(
    payload: dict,
    sign_priv_b64: str,
    signer_device_id: Optional[str] = None,
) -> dict:
    """Sign a JSON-serializable dict with Ed25519 and return a copy with `signature`."""
    if not isinstance(payload, dict):
        raise CryptoError("待签名数据必须是字典对象。")
    _, signing, _, _ = _require_nacl()

    seed = _b64d(sign_priv_b64, "Ed25519 签名私钥", tamper=False)
    if len(seed) != 32:
        raise CryptoError("Ed25519 签名私钥长度无效（应为 32 字节）。")

    body = copy.deepcopy(payload)
    body.pop("signature", None)
    if signer_device_id is not None:
        body["signerDeviceId"] = signer_device_id

    canonical = _canonical_json_bytes(body)
    sk = signing.SigningKey(seed)
    sig_bytes = sk.sign(canonical).signature

    signed = copy.deepcopy(body)
    signed["signature"] = _b64e(sig_bytes)
    return signed


def verify_json(
    signed_payload: dict,
    verify_pub_b64: str,
    expected_device_id: Optional[str] = None,
) -> dict:
    """Verify Ed25519 signature over canonical JSON without `signature`."""
    _, signing, _, _ = _require_nacl()

    if not isinstance(signed_payload, dict):
        raise TamperError("签名负载格式无效：顶层不是对象。")
    sig_b64 = signed_payload.get("signature")
    if not isinstance(sig_b64, str) or not sig_b64.strip():
        raise TamperError("签名负载缺少有效的 signature 字段。")

    verify_raw = _b64d(verify_pub_b64, "Ed25519 验证公钥", tamper=True)
    if len(verify_raw) != 32:
        raise TamperError("Ed25519 验证公钥长度无效。")
    sig_raw = _b64d(sig_b64, "Ed25519 签名", tamper=True)
    if len(sig_raw) != 64:
        raise TamperError("Ed25519 签名长度无效。")

    body = copy.deepcopy(signed_payload)
    body.pop("signature", None)

    if expected_device_id is not None:
        claimed_id = None
        for key in (
            "signerDeviceId",
            "deviceId",
            "fromDeviceId",
            "senderDeviceId",
            "receiverDeviceId",
        ):
            val = body.get(key)
            if isinstance(val, str) and val:
                claimed_id = val
                break
        if claimed_id != expected_device_id:
            raise TamperError(
                "签名设备身份不匹配：期望 %s，实际 %s。"
                % (expected_device_id, claimed_id or "缺失")
            )

    canonical = _canonical_json_bytes(body)
    try:
        vk = signing.VerifyKey(verify_raw)
        vk.verify(canonical, sig_raw)
    except Exception:
        raise TamperError("Ed25519 签名验证失败，数据可能已被篡改。")

    return body


# ---------------------------------------------------------------- secretstream package crypto


def _parse_x25519_private(public_mod, b64_key: Any, label: str, tamper: bool = False):
    raw = _b64d(b64_key, label, tamper=tamper)
    err_cls = TamperError if tamper else CryptoError
    if len(raw) != 32:
        raise err_cls("%s长度无效（应为 32 字节）。" % label)
    try:
        return public_mod.PrivateKey(raw)
    except Exception:
        raise err_cls("%s无效。" % label)


def _parse_x25519_public(public_mod, b64_key: Any, label: str, tamper: bool = False):
    raw = _b64d(b64_key, label, tamper=tamper)
    err_cls = TamperError if tamper else CryptoError
    if len(raw) != 32:
        raise err_cls("%s长度无效（应为 32 字节）。" % label)
    try:
        return public_mod.PublicKey(raw)
    except Exception:
        raise err_cls("%s无效。" % label)


def encrypt_package(
    plain_path: str,
    enc_path: str,
    sender_identity: dict,
    recipient_pub_desc: dict,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict:
    """Encrypt `plain_path` to `enc_path` using libsodium secretstream + X25519 Box."""
    bindings, _, public, _ = _require_nacl()

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise CryptoError("chunk_size 必须是正整数。")
    if not isinstance(sender_identity, dict) or not isinstance(recipient_pub_desc, dict):
        raise CryptoError("发送方身份与接收方公钥描述必须是字典对象。")

    src = Path(plain_path)
    dst = Path(enc_path)
    if not src.is_file():
        raise CryptoError("待加密文件不存在：%s" % src)

    sender_id = sender_identity.get("deviceId")
    recipient_id = recipient_pub_desc.get("deviceId")
    if not isinstance(sender_id, str) or not sender_id.strip():
        raise CryptoError("发送方身份缺少有效的 deviceId。")
    if not isinstance(recipient_id, str) or not recipient_id.strip():
        raise CryptoError("接收方描述缺少有效的 deviceId。")

    sender_priv = _parse_x25519_private(
        public, sender_identity.get("boxPrivateKey"), "发送方加密私钥", tamper=False
    )
    sender_pub = _parse_x25519_public(
        public, sender_identity.get("boxPublicKey"), "发送方加密公钥", tamper=False
    )
    recipient_pub = _parse_x25519_public(
        public, recipient_pub_desc.get("boxPublicKey"), "接收方加密公钥", tamper=False
    )

    sym_key = bindings.crypto_secretstream_xchacha20poly1305_keygen()
    state = bindings.crypto_secretstream_xchacha20poly1305_state()
    header = bindings.crypto_secretstream_xchacha20poly1305_init_push(state, sym_key)

    tag_msg = bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE
    tag_final = bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(dst.parent),
            prefix="." + dst.name + "-",
            suffix=".tmp",
        )
    except OSError as e:
        raise CryptoError("无法创建临时加密文件 %s：%s" % (dst, e))

    hasher = hashlib.sha256()
    total_size = 0
    try:
        with os.fdopen(fd, "wb") as fout, open(src, "rb") as fin:
            first = fin.read(chunk_size)
            if not first:
                ct = bindings.crypto_secretstream_xchacha20poly1305_push(
                    state, b"", tag=tag_final
                )
                frame = struct.pack(">I", len(ct)) + ct
                fout.write(frame)
                hasher.update(frame)
                total_size += len(frame)
            else:
                cur = first
                while True:
                    nxt = fin.read(chunk_size)
                    is_last = len(nxt) == 0
                    tag = tag_final if is_last else tag_msg
                    ct = bindings.crypto_secretstream_xchacha20poly1305_push(
                        state, cur, tag=tag
                    )
                    frame = struct.pack(">I", len(ct)) + ct
                    fout.write(frame)
                    hasher.update(frame)
                    total_size += len(frame)
                    if is_last:
                        break
                    cur = nxt
            fout.flush()
            try:
                os.fsync(fout.fileno())
            except OSError:
                pass
        _replace_with_retry(tmp, str(dst))
    except CryptoError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise CryptoError("写入加密包失败 %s：%s" % (dst, e))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    wrapped_recipient = _b64e(bytes(public.Box(sender_priv, recipient_pub).encrypt(sym_key)))
    wrapped_sender = _b64e(bytes(public.Box(sender_priv, sender_pub).encrypt(sym_key)))

    return {
        "packageSize": total_size,
        "packageSha256": hasher.hexdigest(),
        "streamHeader": _b64e(header),
        "wrappedKeyRecipient": wrapped_recipient,
        "wrappedKeySender": wrapped_sender,
        "senderDeviceId": sender_id,
        "recipientDeviceId": recipient_id,
        "chunkSize": chunk_size,
    }


def decrypt_package(
    enc_path: str,
    plain_path: str,
    envelope: dict,
    receiver_identity: dict,
    sender_pub_desc: dict,
) -> None:
    """Verify and stream-decrypt `enc_path` into `plain_path` atomically."""
    bindings, _, public, _ = _require_nacl()

    if (
        not isinstance(envelope, dict)
        or not isinstance(receiver_identity, dict)
        or not isinstance(sender_pub_desc, dict)
    ):
        raise TamperError("解密参数格式无效。")

    src = Path(enc_path)
    dst = Path(plain_path)
    if not src.is_file():
        raise TamperError("加密交接包不存在：%s" % src)

    expected_size = envelope.get("packageSize")
    expected_sha = envelope.get("packageSha256")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
        raise TamperError("加密包元数据缺少有效的 packageSize。")
    if not isinstance(expected_sha, str) or len(expected_sha.strip()) != 64:
        raise TamperError("加密包元数据缺少有效的 packageSha256。")

    try:
        stat_size = src.stat().st_size
    except OSError as e:
        raise TamperError("无法读取加密交接包状态 %s：%s" % (src, e))
    if stat_size != expected_size:
        raise TamperError(
            "加密交接包大小不匹配（期望 %d 字节，实际 %d 字节），文件可能被截断或篡改。"
            % (expected_size, stat_size)
        )

    actual_size, actual_sha = _sha256_file(src)
    if actual_size != expected_size or actual_sha.lower() != expected_sha.strip().lower():
        raise TamperError("加密交接包 SHA-256 校验失败，文件可能已被篡改或损坏。")

    env_sender_id = envelope.get("senderDeviceId")
    desc_sender_id = sender_pub_desc.get("deviceId")
    if (
        isinstance(env_sender_id, str)
        and env_sender_id
        and isinstance(desc_sender_id, str)
        and desc_sender_id
        and env_sender_id != desc_sender_id
    ):
        raise TamperError("发送方设备身份与公钥描述不一致。")

    viewer_id = receiver_identity.get("deviceId")
    if not isinstance(viewer_id, str) or not viewer_id:
        raise TamperError("接收方身份缺少 deviceId。")

    if viewer_id == envelope.get("recipientDeviceId"):
        wrapped_b64 = envelope.get("wrappedKeyRecipient")
    elif viewer_id == envelope.get("senderDeviceId"):
        wrapped_b64 = envelope.get("wrappedKeySender")
    else:
        raise TamperError("当前设备不是该交接包的目标接收方或发送方，无法解密。")

    wrapped_raw = _b64d(wrapped_b64, "包裹对称密钥", tamper=True)
    recv_priv = _parse_x25519_private(
        public, receiver_identity.get("boxPrivateKey"), "本机加密私钥", tamper=True
    )
    sender_pub = _parse_x25519_public(
        public, sender_pub_desc.get("boxPublicKey"), "发送方加密公钥", tamper=True
    )

    try:
        sym_key = public.Box(recv_priv, sender_pub).decrypt(wrapped_raw)
    except Exception:
        raise TamperError("包裹密钥解封失败：密钥不匹配或包裹已被篡改。")

    if len(sym_key) != bindings.crypto_secretstream_xchacha20poly1305_KEYBYTES:
        raise TamperError("解封后的对称密钥长度无效。")

    header = _b64d(envelope.get("streamHeader"), "加密流头部", tamper=True)
    if len(header) != bindings.crypto_secretstream_xchacha20poly1305_HEADERBYTES:
        raise TamperError("加密流头部长度无效。")

    state = bindings.crypto_secretstream_xchacha20poly1305_state()
    try:
        bindings.crypto_secretstream_xchacha20poly1305_init_pull(state, header, sym_key)
    except Exception:
        raise TamperError("初始化解密流失败：流头部已损坏。")

    abytes = bindings.crypto_secretstream_xchacha20poly1305_ABYTES
    tag_final = bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(dst.parent),
            prefix="." + dst.name + "-",
            suffix=".tmp",
        )
    except OSError as e:
        raise CryptoError("无法创建解密临时文件 %s：%s" % (dst, e))

    try:
        seen_final = False
        with os.fdopen(fd, "wb") as fout, open(src, "rb") as fin:
            while True:
                len_buf = fin.read(4)
                if not len_buf:
                    break
                if len(len_buf) < 4:
                    raise TamperError("加密包帧头不完整，文件已被截断或附加垃圾数据。")
                (frame_len,) = struct.unpack(">I", len_buf)
                if frame_len < abytes or frame_len > MAX_FRAME_CIPHERTEXT_BYTES:
                    raise TamperError("加密包帧长度非法（%d 字节），数据已损坏。" % frame_len)
                ct = fin.read(frame_len)
                if len(ct) != frame_len:
                    raise TamperError("加密包帧内容被截断。")
                try:
                    pt, tag = bindings.crypto_secretstream_xchacha20poly1305_pull(state, ct)
                except Exception:
                    raise TamperError("加密流数据块认证失败，交接包已被篡改。")
                fout.write(pt)
                if tag == tag_final:
                    trailing = fin.read(1)
                    if trailing:
                        raise TamperError("加密流结束标记后存在多余字节，疑似被注入或篡改。")
                    seen_final = True
                    break

            if not seen_final:
                raise TamperError("加密流缺少结束标记 TAG_FINAL，交接包已被截断。")

            fout.flush()
            try:
                os.fsync(fout.fileno())
            except OSError:
                pass
        _replace_with_retry(tmp, str(dst))
    except TamperError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise CryptoError("写入解密文件失败 %s：%s" % (dst, e))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- encrypted preview metadata


def encrypt_meta(
    meta_obj: dict,
    sender_identity: dict,
    recipient_pub_desc: dict,
) -> dict:
    """Encrypt small preview metadata dict for both recipient and sender via X25519 Box."""
    _, _, public, _ = _require_nacl()

    if not isinstance(meta_obj, dict):
        raise CryptoError("预览元数据必须是字典对象。")
    if not isinstance(sender_identity, dict) or not isinstance(recipient_pub_desc, dict):
        raise CryptoError("发送方身份与接收方公钥描述必须是字典对象。")

    sender_id = sender_identity.get("deviceId")
    recipient_id = recipient_pub_desc.get("deviceId")
    if not isinstance(sender_id, str) or not sender_id.strip():
        raise CryptoError("发送方身份缺少有效的 deviceId。")
    if not isinstance(recipient_id, str) or not recipient_id.strip():
        raise CryptoError("接收方描述缺少有效的 deviceId。")

    sender_priv = _parse_x25519_private(
        public, sender_identity.get("boxPrivateKey"), "发送方加密私钥", tamper=False
    )
    sender_pub = _parse_x25519_public(
        public, sender_identity.get("boxPublicKey"), "发送方加密公钥", tamper=False
    )
    recipient_pub = _parse_x25519_public(
        public, recipient_pub_desc.get("boxPublicKey"), "接收方加密公钥", tamper=False
    )

    raw_bytes = _canonical_json_bytes(meta_obj)
    ct_recipient = _b64e(bytes(public.Box(sender_priv, recipient_pub).encrypt(raw_bytes)))
    ct_sender = _b64e(bytes(public.Box(sender_priv, sender_pub).encrypt(raw_bytes)))

    return {
        "formatVersion": 1,
        "senderDeviceId": sender_id,
        "recipientDeviceId": recipient_id,
        "ciphertextRecipient": ct_recipient,
        "ciphertextSender": ct_sender,
    }


def decrypt_meta(
    meta_env: dict,
    viewer_identity: dict,
    peer_pub_desc: dict,
) -> dict:
    """Decrypt preview metadata envelope for either recipient or sender."""
    _, _, public, _ = _require_nacl()

    if (
        not isinstance(meta_env, dict)
        or not isinstance(viewer_identity, dict)
        or not isinstance(peer_pub_desc, dict)
    ):
        raise TamperError("加密预览元数据参数无效。")

    viewer_id = viewer_identity.get("deviceId")
    peer_id = peer_pub_desc.get("deviceId")
    sender_id = meta_env.get("senderDeviceId")
    recipient_id = meta_env.get("recipientDeviceId")
    if (
        not isinstance(viewer_id, str)
        or not viewer_id
        or not isinstance(peer_id, str)
        or not peer_id
        or not isinstance(sender_id, str)
        or not sender_id
        or not isinstance(recipient_id, str)
        or not recipient_id
    ):
        raise TamperError("加密预览元数据缺少设备身份字段。")

    viewer_priv = _parse_x25519_private(
        public, viewer_identity.get("boxPrivateKey"), "本机加密私钥", tamper=True
    )
    peer_pub = _parse_x25519_public(
        public, peer_pub_desc.get("boxPublicKey"), "对端加密公钥", tamper=True
    )

    if viewer_id == recipient_id:
        if peer_id != sender_id:
            raise TamperError("预览元数据发送方身份与对端公钥描述不一致。")
        ct_raw = _b64d(meta_env.get("ciphertextRecipient"), "接收方元数据密文", tamper=True)
        try:
            plain_bytes = public.Box(viewer_priv, peer_pub).decrypt(ct_raw)
        except Exception:
            raise TamperError("预览元数据解密失败：密钥不匹配或密文已被篡改。")
    elif viewer_id == sender_id:
        ct_sender_raw = _b64d(meta_env.get("ciphertextSender"), "发送方元数据密文", tamper=True)
        if peer_id == recipient_id:
            ct_recip_raw = _b64d(
                meta_env.get("ciphertextRecipient"), "接收方元数据密文", tamper=True
            )
            try:
                plain_sender = public.Box(viewer_priv, viewer_priv.public_key).decrypt(
                    ct_sender_raw
                )
                plain_recip = public.Box(viewer_priv, peer_pub).decrypt(ct_recip_raw)
            except Exception:
                raise TamperError("预览元数据解密失败：密钥不匹配或密文已被篡改。")
            if plain_sender != plain_recip:
                raise TamperError("预览元数据双端密文不一致，疑似被篡改。")
            plain_bytes = plain_sender
        elif peer_id == sender_id:
            try:
                plain_bytes = public.Box(viewer_priv, peer_pub).decrypt(ct_sender_raw)
            except Exception:
                raise TamperError("预览元数据解密失败：密钥不匹配或密文已被篡改。")
        else:
            raise TamperError("预览元数据对端设备身份不匹配。")
    else:
        raise TamperError("当前设备无权解密该预览元数据。")

    try:
        parsed = json.loads(plain_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise TamperError("预览元数据 JSON 解析失败，内容已损坏。")
    if not isinstance(parsed, dict):
        raise TamperError("预览元数据内容不是有效的 JSON 对象。")
    return parsed
