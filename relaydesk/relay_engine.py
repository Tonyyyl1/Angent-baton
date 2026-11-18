# -*- coding: utf-8 -*-
"""Unified encrypted asynchronous cloud relay engine for relay-desk.

Shared orchestration layer invoked by CLI (`relaydesk.cli`), local Web Workbench
HTTP API (`relaydesk.server`), and agent skills.
Python 3.9 compatible; UTF-8; Chinese user-facing messages.
"""
import copy
import json
import os
import re
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import crypto
from . import ledger as L
from . import scan

try:
    from . import storage
except ImportError:
    storage = None  # type: ignore

try:
    from . import conversations
except ImportError:
    conversations = None  # type: ignore

try:
    from . import packager
except ImportError:
    packager = None  # type: ignore

__all__ = [
    "RelayError",
    "NotConfiguredError",
    "NeedTargetPathError",
    "DuplicateReceiveError",
    "ReceiptUploadError",
    "HANDOFF_DONE_MESSAGE",
    "cloud_status",
    "pair_device",
    "sync_receipts",
    "send_project",
    "list_inbox",
    "receive_relay",
    "query_transcripts",
]

HANDOFF_DONE_MESSAGE = "已交棒，可以关机"
NEED_DEVICE_MSG = "无法识别本机是哪台设备。请先运行 relay init --device a（或 b）。"
RELAY_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


# ---------------------------------------------------------------- exceptions


class RelayError(Exception):
    """Base error for user-facing relay engine failures."""

    def __init__(self, message: str, code: str = "relay_error") -> None:
        Exception.__init__(self, message)
        self.message = message
        self.code = code


class NotConfiguredError(RelayError):
    """Raised when cloud connection or pairing confirmation is not ready."""

    def __init__(self, message: str) -> None:
        RelayError.__init__(self, message, code="not_configured")


class NeedTargetPathError(RelayError):
    """Raised on first-time receive on a device when no target path is known."""

    def __init__(
        self,
        message: str,
        project_id: str = "",
        project_name: str = "",
    ) -> None:
        RelayError.__init__(self, message, code="need_path")
        self.project_id = project_id
        self.project_name = project_name


class DuplicateReceiveError(RelayError):
    """Raised when attempting to receive an already claimed & acknowledged relay."""

    def __init__(self, message: str, relay_id: str = "") -> None:
        RelayError.__init__(self, message, code="duplicate_receive")
        self.relay_id = relay_id


class ReceiptUploadError(RelayError):
    """Raised when local workspace apply succeeded but uploading receipt.json failed."""

    def __init__(self, relay_id: str, message: Optional[str] = None) -> None:
        if not message:
            message = (
                "项目已在本地应用成功，但上传签收回执失败（交接编号：%s）。"
                "请稍后重新运行 relay receive %s 补传回执，不会覆盖本地改动。"
                % (relay_id, relay_id)
            )
        RelayError.__init__(self, message, code="receipt_upload_failed")
        self.relay_id = relay_id


# ---------------------------------------------------------------- lazy sibling access


def _storage_mod():
    global storage
    if storage is None:
        from . import storage as _s

        storage = _s
    return storage


def _conversations_mod():
    global conversations
    if conversations is None:
        from . import conversations as _c

        conversations = _c
    return conversations


def _packager_mod():
    global packager
    if packager is None:
        from . import packager as _p

        packager = _p
    return packager


# ---------------------------------------------------------------- atomic local state helpers


def _canon_local_path(p: str) -> str:
    if not p:
        return ""
    if hasattr(L, "canonical_local_path"):
        return L.canonical_local_path(p)
    return unicodedata.normalize("NFC", os.path.realpath(os.path.abspath(p)))


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


def _read_json_file(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return copy.deepcopy(default)
    try:
        with open(str(path), "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    return copy.deepcopy(default)


def _atomic_write_json(path: Path, data: Dict[str, Any], mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix="." + path.name + "-",
        suffix=".tmp",
    )
    try:
        if mode is not None and os.name != "nt":
            try:
                os.fchmod(fd, mode)
            except (AttributeError, OSError):
                os.chmod(tmp, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        if mode is not None and os.name != "nt":
            os.chmod(tmp, mode)
        _replace_with_retry(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


DEFAULT_CLOUD: Dict[str, Any] = {
    "provider": "rclone",
    "remote": "",
    "root": "",
    "configPath": "",
    "peer": None,
    "pairFingerprint": "",
    "paired": False,
    "pairedAt": "",
}

DEFAULT_RELAY_STATE: Dict[str, Any] = {
    "pendingSends": {},
    "sentRelays": {},
    "receivedRelays": {},
    "projectBaselines": {},
}


def _load_cloud(local_home: Path) -> Dict[str, Any]:
    raw = _read_json_file(local_home / "cloud.json", DEFAULT_CLOUD)
    out = copy.deepcopy(DEFAULT_CLOUD)
    out.update(raw)
    if out.get("provider") not in ("rclone", "local"):
        out["provider"] = "rclone"
    out["remote"] = str(out.get("remote") or "")
    out["root"] = str(out.get("root") or "")
    out["configPath"] = str(out.get("configPath") or "")
    out["pairFingerprint"] = str(out.get("pairFingerprint") or "")
    out["paired"] = bool(out.get("paired") and isinstance(out.get("peer"), dict))
    out["pairedAt"] = str(out.get("pairedAt") or "")
    if not isinstance(out.get("peer"), dict):
        out["peer"] = None
    return out


def _save_cloud(local_home: Path, cloud_cfg: Dict[str, Any]) -> None:
    _atomic_write_json(local_home / "cloud.json", cloud_cfg)


def _is_cloud_configured(cloud_cfg: Dict[str, Any]) -> bool:
    prov = cloud_cfg.get("provider") or "rclone"
    if prov == "local":
        return bool(str(cloud_cfg.get("root") or "").strip())
    return bool(str(cloud_cfg.get("remote") or "").strip())


def _load_relay_state(local_home: Path) -> Dict[str, Any]:
    raw = _read_json_file(local_home / "relay_state.json", DEFAULT_RELAY_STATE)
    out = copy.deepcopy(DEFAULT_RELAY_STATE)
    for k in ("pendingSends", "sentRelays", "receivedRelays", "projectBaselines"):
        if isinstance(raw.get(k), dict):
            out[k] = copy.deepcopy(raw[k])
    return out


def _save_relay_state(local_home: Path, state: Dict[str, Any]) -> None:
    _atomic_write_json(local_home / "relay_state.json", state)


def _update_relay_state(local_home: Path, fn) -> Dict[str, Any]:
    state_path = local_home / "relay_state.json"
    with L._Lock(state_path):
        st = _load_relay_state(local_home)
        fn(st)
        _save_relay_state(local_home, st)
        return st


def _resolve_ctx(
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Tuple[Path, Path, Dict[str, Any]]:
    base = Path(local_home) if local_home is not None else L.local_dir()
    if local is None:
        if local_home is not None:
            local = _read_json_file(base / "config.json", {})
        else:
            local = L.load_local()
    led_path = Path(ledger_path) if ledger_path is not None else L.ledger_path(local)
    return base, led_path, local


def _read_existing_identity_public(local_home: Path) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    ident_path = local_home / "identity.json"
    if not ident_path.exists():
        return "", "", None
    data = _read_json_file(ident_path, {})
    dev_id = str(data.get("deviceId") or "")
    fp = str(data.get("keyFingerprint") or "")
    if not fp and data.get("verifyPublicKey") and data.get("boxPublicKey"):
        try:
            fp = crypto.device_fingerprint(data["verifyPublicKey"], data["boxPublicKey"])
        except Exception:
            fp = ""
    try:
        desc = crypto.public_descriptor(data) if dev_id else None
    except Exception:
        desc = None
    return dev_id, fp, desc


def _same_peer_keys(p1: Optional[Dict[str, Any]], p2: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(p1, dict) or not isinstance(p2, dict):
        return False
    return (
        p1.get("deviceId") == p2.get("deviceId")
        and p1.get("verifyPublicKey") == p2.get("verifyPublicKey")
        and p1.get("boxPublicKey") == p2.get("boxPublicKey")
        and bool(p1.get("deviceId"))
    )


def _fetch_and_verify_peer_descriptor(provider, other_alias: str) -> Optional[Dict[str, Any]]:
    st_mod = _storage_mod()
    remote_key = "devices/%s.json" % other_alias
    if provider.stat(remote_key) is None:
        return None
    with tempfile.TemporaryDirectory() as tmpdir:
        dst = os.path.join(tmpdir, "peer.json")
        try:
            provider.download(remote_key, dst)
        except st_mod.ObjectNotFoundError:
            return None
        try:
            with open(dst, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError) as e:
            raise crypto.TamperError("对端设备公钥描述文件不是有效 JSON（%s）：%s" % (remote_key, e))
    if not isinstance(raw, dict):
        raise crypto.TamperError("对端设备公钥描述文件格式无效：%s" % remote_key)
    vpub = raw.get("verifyPublicKey")
    dev_id = raw.get("deviceId")
    if not isinstance(vpub, str) or not vpub.strip() or not isinstance(dev_id, str) or not dev_id.strip():
        raise crypto.TamperError("对端设备公钥描述缺少 deviceId 或 verifyPublicKey：%s" % remote_key)
    verified = crypto.verify_json(raw, vpub, expected_device_id=dev_id)
    expected_fp = crypto.device_fingerprint(verified["verifyPublicKey"], verified["boxPublicKey"])
    if verified.get("keyFingerprint") and verified["keyFingerprint"] != expected_fp:
        raise crypto.TamperError("对端设备公钥指纹校验不一致：%s" % remote_key)
    verified["keyFingerprint"] = expected_fp
    return crypto.public_descriptor(verified)


def _infer_device_os(led: Dict[str, Any], here_id: str, existing_ident_exists: bool) -> str:
    env_os = os.environ.get("RELAY_DESK_OS", "").strip().lower()
    if env_os in ("windows", "mac", "linux"):
        return env_os
    my_dev = L.dev(led["config"], here_id)
    if my_dev.get("osExplicit") and my_dev.get("os") in ("windows", "mac", "linux"):
        return str(my_dev["os"])
    default_devs = L.default_config()["devices"]
    default_os = default_devs[0]["os"] if here_id == "a" else default_devs[1]["os"]
    configured_os = my_dev.get("os")
    if configured_os in ("windows", "mac", "linux") and configured_os != default_os:
        return str(configured_os)
    if existing_ident_exists:
        return ""
    return L.local_os()


def _apply_local_facts_to_project(
    project: Dict[str, Any],
    here_id: str,
    facts: Optional[Dict[str, Any]],
    pres: Optional[Dict[str, Any]],
) -> None:
    if facts is not None:
        if facts.get("isGit"):
            project["branch"] = facts.get("branch", "")
            project["dirty"] = facts.get("dirty", 0)
            project["unpushed"] = facts.get("unpushed")
            remotes = facts.get("remotes") or []
            cur = str(project.get("remote") or "")
            first = cur.split()[0] if cur.split() else ""
            if not (first and first in remotes):
                project["remote"] = facts.get("remote", "")
            g = dict(project.get("git") or {})
            g.update(
                {
                    "head": facts.get("head", ""),
                    "lastCommit": facts.get("lastCommit", ""),
                    "worktrees": len(facts.get("worktrees") or []) or 1,
                    "scannedAt": L.now_iso(),
                    "scannedOn": here_id,
                }
            )
            project["git"] = g
        else:
            project["branch"] = ""
            project["dirty"] = 0
            project["unpushed"] = None
            project["remote"] = ""
    if pres:
        reg = project.setdefault("reg", {})
        for k, v in pres.items():
            entry = reg.get(k)
            if not isinstance(entry, dict):
                entry = {"on": {}, "note": ""}
                reg[k] = entry
            if not isinstance(entry.get("on"), dict):
                entry["on"] = {}
            entry["on"][here_id] = bool(v)


_SECRET_EXACT = frozenset(
    (
        ".env",
        ".credentials.json",
        "auth.json",
        "oauth_creds.json",
        ".claude.json",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
    )
)
_SECRET_ALLOW = frozenset((".env.example", ".env.sample", ".env.template"))
_SECRET_EXTS = (".pem", ".key", ".p12", ".pfx")


def _is_excluded_secret_or_sys(rel_path: str) -> bool:
    norm = unicodedata.normalize("NFC", rel_path.replace("\\", "/")).strip("/")
    if not norm:
        return True
    parts = norm.split("/")
    for seg in parts:
        if seg.lower() in (".git", ".ds_store"):
            return True
    base = parts[-1].lower()
    if base in _SECRET_ALLOW:
        return False
    if base in _SECRET_EXACT or base.startswith(".env.") or base.endswith(_SECRET_EXTS):
        return True
    return False


def _compute_git_baseline_fingerprint(repo_path: str) -> str:
    """Compute SHA-256 baseline of HEAD + staging index + unstaged diff + eligible untracked files."""
    import hashlib
    import subprocess

    if not repo_path or not os.path.isdir(repo_path):
        return ""

    def _git_b(*args: str) -> Tuple[int, bytes]:
        try:
            r = subprocess.run(
                ["git", "-c", "core.quotepath=false", "-C", repo_path] + list(args),
                capture_output=True,
            )
            return r.returncode, r.stdout or b""
        except OSError:
            return -1, b""

    rc_wt, out_wt = _git_b("rev-parse", "--is-inside-work-tree")
    if rc_wt != 0 or out_wt.strip() != b"true":
        return ""

    h = hashlib.sha256()
    rc_hd, out_hd = _git_b("rev-parse", "--verify", "HEAD")
    if rc_hd == 0:
        h.update(b"HEAD:" + out_hd.strip() + b"\n")
    else:
        _, out_sym = _git_b("symbolic-ref", "-q", "HEAD")
        h.update(b"UNBORN:" + out_sym.strip() + b"\n")

    _, out_idx = _git_b("ls-files", "--stage", "-z")
    h.update(b"INDEX:" + out_idx + b"\n")

    _, out_diff = _git_b("diff", "--binary", "--full-index", "--no-ext-diff")
    h.update(b"UNSTAGED:" + out_diff + b"\n")

    rc_un, out_un = _git_b("ls-files", "--others", "--exclude-standard", "-z")
    if rc_un == 0 and out_un:
        un_items: List[Tuple[str, bytes]] = []
        for raw_b in out_un.split(b"\x00"):
            if not raw_b:
                continue
            rel_str = raw_b.decode("utf-8", errors="surrogateescape")
            if _is_excluded_secret_or_sys(rel_str):
                continue
            norm_rel = unicodedata.normalize("NFC", rel_str.replace("\\", "/"))
            full_p = os.path.join(repo_path, rel_str)
            fh = hashlib.sha256()
            try:
                if os.path.islink(full_p):
                    fh.update(b"LINK:" + os.readlink(full_p).encode("utf-8", errors="surrogateescape"))
                elif os.path.isfile(full_p):
                    st = os.stat(full_p)
                    mode_tag = b"755:" if (st.st_mode & 0o111) else b"644:"
                    fh.update(mode_tag)
                    with open(full_p, "rb") as fp:
                        while True:
                            ck = fp.read(65536)
                            if not ck:
                                break
                            fh.update(ck)
                else:
                    continue
            except OSError:
                continue
            un_items.append((norm_rel, fh.digest()))
        un_items.sort(key=lambda x: x[0])
        for norm_rel, digest in un_items:
            h.update(b"UNTRACKED:" + norm_rel.encode("utf-8") + b":" + digest + b"\n")

    return h.hexdigest()


def _merge_sessions_meta(existing_list: List[Dict[str, Any]], incoming_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged = [copy.deepcopy(s) for s in (existing_list or []) if isinstance(s, dict)]
    by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for s in merged:
        sid = str(s.get("sessionId") or "")
        if sid:
            key = (str(s.get("tool") or ""), str(s.get("device") or ""), sid)
            by_key[key] = s

    for inc in incoming_list or []:
        if not isinstance(inc, dict):
            continue
        sid = str(inc.get("sessionId") or "")
        key = (str(inc.get("tool") or ""), str(inc.get("device") or ""), sid)
        if sid and key in by_key:
            cur = by_key[key]
            for fld in ("title", "lastUsed", "carry", "kind", "cwd", "note"):
                if inc.get(fld):
                    cur[fld] = inc[fld]
        else:
            item = copy.deepcopy(inc)
            if not item.get("id"):
                item["id"] = L.new_id("s_")
            merged.append(item)
            if sid:
                by_key[key] = item
    return merged


# ---------------------------------------------------------------- 1. cloud_status


def cloud_status(
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Dict[str, Any]:
    """Return current cloud relay configuration, local identity, pairing state, and remotes."""
    base, led_path, local_cfg = _resolve_ctx(ledger_path, local, local_home)
    led = L.load(led_path)
    alias = L.here(led, local_cfg) or ""
    cloud = _load_cloud(base)
    configured = _is_cloud_configured(cloud)

    has_crypto = crypto.has_crypto()
    st_mod = None
    has_rclone = False
    remotes: List[str] = []
    try:
        st_mod = _storage_mod()
        has_rclone = bool(st_mod.has_rclone())
        if has_rclone:
            remotes = list(
                st_mod.discover_rclone_remotes(
                    config_path=cloud.get("configPath") or None
                )
            )
    except Exception:
        remotes = []

    dev_id, key_fp, my_desc = _read_existing_identity_public(base)
    pinned_peer = cloud.get("peer") if isinstance(cloud.get("peer"), dict) else None
    paired = bool(cloud.get("paired") and pinned_peer)
    shown_peer = copy.deepcopy(pinned_peer) if pinned_peer else None
    pair_fp = cloud.get("pairFingerprint") if paired else ""

    if configured and not paired and has_crypto and my_desc and alias and st_mod is not None:
        other_alias = L.other(led["config"], alias)["id"]
        try:
            prov = st_mod.make_provider(cloud)
            cand = _fetch_and_verify_peer_descriptor(prov, other_alias)
            if cand is not None:
                shown_peer = cand
                pair_fp = crypto.pair_fingerprint(my_desc, cand)
        except Exception:
            pass

    return {
        "configured": configured,
        "cloud": {
            "provider": cloud.get("provider") or "rclone",
            "remote": cloud.get("remote") or "",
            "root": cloud.get("root") or "",
            "configPath": cloud.get("configPath") or "",
        },
        "deviceId": dev_id,
        "alias": alias,
        "keyFingerprint": key_fp,
        "paired": paired,
        "pairFingerprint": pair_fp,
        "peer": shown_peer,
        "remotes": remotes,
        "hasCrypto": has_crypto,
        "hasRclone": has_rclone,
    }


# ---------------------------------------------------------------- 2. pair_device


def pair_device(
    remote: Optional[str] = None,
    root: Optional[str] = None,
    provider_type: Optional[str] = "rclone",
    rclone_config: Optional[str] = None,
    accept: bool = False,
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Dict[str, Any]:
    """Publish local signed public key descriptor, discover peer, and optionally pin/accept pairing."""
    crypto.require_crypto()
    st_mod = _storage_mod()
    base, led_path, local_cfg = _resolve_ctx(ledger_path, local, local_home)
    led = L.load(led_path)
    here_id = L.here(led, local_cfg)
    if not here_id:
        raise RelayError(NEED_DEVICE_MSG, code="need_device")

    other_id = L.other(led["config"], here_id)["id"]
    my_dev_cfg = L.dev(led["config"], here_id)

    cloud = _load_cloud(base)
    prev_was_configured = _is_cloud_configured(cloud)
    prev_conn = (
        cloud.get("provider") or "rclone",
        cloud.get("remote") or "",
        cloud.get("root") or "",
        cloud.get("configPath") or "",
    )
    if provider_type == "local":
        cloud["provider"] = "local"
    elif remote is not None:
        cloud["provider"] = provider_type or "rclone"
    elif provider_type is not None and not _is_cloud_configured(cloud):
        cloud["provider"] = provider_type

    if remote is not None:
        cloud["remote"] = str(remote).strip()
    if root is not None:
        cloud["root"] = str(root).strip()
    if rclone_config is not None:
        cloud["configPath"] = str(rclone_config).strip()

    new_conn = (
        cloud.get("provider") or "rclone",
        cloud.get("remote") or "",
        cloud.get("root") or "",
        cloud.get("configPath") or "",
    )
    conn_changed = prev_was_configured and (new_conn != prev_conn)

    if not _is_cloud_configured(cloud):
        raise NotConfiguredError(
            "尚未配置云存储连接。请指定 --remote <连接名> --root <共享目录>（或本地目录模式 --provider local --root <目录>）。"
        )

    ident_exists = (base / "identity.json").exists()
    os_name = _infer_device_os(led, here_id, ident_exists)
    dev_name = str(my_dev_cfg.get("name") or "")
    identity = crypto.ensure_identity(
        base,
        alias=here_id,
        name=dev_name,
        os_name=os_name,
        host=L.hostname(),
    )
    my_desc = crypto.public_descriptor(identity)
    signed_my_desc = crypto.sign_json(
        my_desc,
        identity["signPrivateKey"],
        signer_device_id=identity["deviceId"],
    )

    provider = st_mod.make_provider(cloud)
    my_key = "devices/%s.json" % here_id
    with tempfile.TemporaryDirectory() as tmpdir:
        p_me = os.path.join(tmpdir, "me.json")
        _atomic_write_json(Path(p_me), signed_my_desc)
        provider.upload(p_me, my_key)
        if provider.stat(my_key) is None:
            raise st_mod.StorageError("发布本机公钥文件失败，远端未找到：%s" % my_key)

    remote_peer = _fetch_and_verify_peer_descriptor(provider, other_id)
    old_pinned = (
        cloud.get("peer")
        if (not conn_changed and cloud.get("paired") and isinstance(cloud.get("peer"), dict))
        else None
    )

    shown_peer: Optional[Dict[str, Any]] = None
    shown_pfp = ""

    if remote_peer is not None:
        if remote_peer.get("deviceId") == my_desc["deviceId"]:
            raise RelayError("云端对端设备（%s）与本机拥有相同的 deviceId，请检查本机身份配置。" % other_id)
        shown_pfp = crypto.pair_fingerprint(my_desc, remote_peer)
        shown_peer = remote_peer
        if accept:
            cloud["peer"] = remote_peer
            cloud["pairFingerprint"] = shown_pfp
            cloud["paired"] = True
            cloud["pairedAt"] = L.now_iso()
        elif old_pinned is not None and _same_peer_keys(old_pinned, remote_peer):
            cloud["peer"] = remote_peer
            cloud["pairFingerprint"] = shown_pfp
            cloud["paired"] = True
        else:
            cloud["peer"] = None
            cloud["pairFingerprint"] = ""
            cloud["paired"] = False
            cloud["pairedAt"] = ""
    else:
        if old_pinned is not None:
            shown_peer = old_pinned
            shown_pfp = crypto.pair_fingerprint(my_desc, old_pinned)
            cloud["peer"] = old_pinned
            cloud["pairFingerprint"] = shown_pfp
            cloud["paired"] = True
        elif accept:
            raise RelayError(
                "尚未在云端发现另一台设备（%s）的公钥文件（devices/%s.json）。"
                "请先在另一台设备上运行 relay pair 发布公钥，核对指纹一致后再运行 relay pair --accept。"
                % (other_id, other_id)
            )
        else:
            cloud["peer"] = None
            cloud["pairFingerprint"] = ""
            cloud["paired"] = False
            cloud["pairedAt"] = ""

    _save_cloud(base, cloud)

    def _upd_devices(l: Dict[str, Any]) -> None:
        d_me = L.dev(l["config"], here_id)
        if my_desc.get("os"):
            d_me["os"] = my_desc["os"]
        if my_desc.get("name"):
            d_me["name"] = my_desc["name"]
        d_me["deviceId"] = my_desc["deviceId"]
        if my_desc.get("host") and not d_me.get("host"):
            d_me["host"] = my_desc["host"]
        if shown_peer is not None:
            d_other = L.dev(l["config"], other_id)
            if shown_peer.get("os"):
                d_other["os"] = shown_peer["os"]
            if shown_peer.get("name"):
                d_other["name"] = shown_peer["name"]
            if shown_peer.get("deviceId"):
                d_other["deviceId"] = shown_peer["deviceId"]

    L.update(led_path, _upd_devices)

    has_rclone = False
    remotes: List[str] = []
    try:
        has_rclone = bool(st_mod.has_rclone())
        if has_rclone:
            remotes = list(
                st_mod.discover_rclone_remotes(
                    config_path=cloud.get("configPath") or None
                )
            )
    except Exception:
        remotes = []

    return {
        "configured": True,
        "cloud": {
            "provider": cloud.get("provider") or "rclone",
            "remote": cloud.get("remote") or "",
            "root": cloud.get("root") or "",
            "configPath": cloud.get("configPath") or "",
        },
        "deviceId": my_desc["deviceId"],
        "alias": here_id,
        "keyFingerprint": my_desc["keyFingerprint"],
        "paired": bool(cloud.get("paired") and cloud.get("peer")),
        "pairFingerprint": shown_pfp,
        "peer": shown_peer,
        "remotes": remotes,
        "hasCrypto": True,
        "hasRclone": has_rclone,
    }


def _require_paired_context(
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
):
    crypto.require_crypto()
    st_mod = _storage_mod()
    base, led_path, local_cfg = _resolve_ctx(ledger_path, local, local_home)
    cloud = _load_cloud(base)
    if not _is_cloud_configured(cloud):
        raise NotConfiguredError("尚未配置云存储连接。请先运行 relay pair 配置网盘与共享目录。")
    if not cloud.get("paired") or not isinstance(cloud.get("peer"), dict):
        raise NotConfiguredError("双机尚未完成配对确认。请核对双机配对指纹一致后运行 relay pair --accept。")
    led = L.load(led_path)
    here_id = L.here(led, local_cfg)
    if not here_id:
        raise RelayError(NEED_DEVICE_MSG, code="need_device")
    identity = crypto.ensure_identity(base, alias=here_id)
    peer_desc = cloud["peer"]
    provider = st_mod.make_provider(cloud)
    return base, led_path, local_cfg, led, here_id, cloud, identity, peer_desc, provider


# ---------------------------------------------------------------- 3. sync_receipts


def sync_receipts(
    provider,
    identity: Dict[str, Any],
    peer_desc: Dict[str, Any],
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Sync remote receipt.json acknowledgements for unacked outgoing relays."""
    st_mod = _storage_mod()
    base, led_path, local_cfg = _resolve_ctx(ledger_path, local, local_home)
    state = _load_relay_state(base)
    sent_relays = state.get("sentRelays") or {}
    unacked_ids = [rid for rid, info in sent_relays.items() if isinstance(info, dict) and not info.get("acked")]
    if not unacked_ids:
        return []

    synced: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for rid in unacked_ids:
            info = sent_relays[rid]
            rkey = "relays/%s/receipt.json" % rid
            if provider.stat(rkey) is None:
                continue
            rfile = os.path.join(tmpdir, "%s_receipt.json" % rid)
            try:
                provider.download(rkey, rfile)
                with open(rfile, "r", encoding="utf-8-sig") as fh:
                    raw_rec = json.load(fh)
                verified = crypto.verify_json(
                    raw_rec,
                    peer_desc["verifyPublicKey"],
                    expected_device_id=peer_desc.get("deviceId"),
                )
            except (st_mod.StorageError, crypto.CryptoError, OSError, ValueError, UnicodeDecodeError):
                continue

            if verified.get("relayId") != rid:
                continue

            proj_id = str(info.get("projectId") or verified.get("projectId") or "")
            to_alias = str(info.get("toAlias") or verified.get("toAlias") or "")
            from_alias = str(verified.get("fromAlias") or identity.get("alias") or "")
            acked_at = str(verified.get("receivedAt") or L.now_iso())

            def _apply_ack(l: Dict[str, Any]) -> None:
                for p in l.get("projects") or []:
                    if p.get("id") != proj_id:
                        continue
                    ho = p.get("handoff")
                    is_current_relay = (
                        p.get("lastRelayId") in (None, "", rid)
                        or (isinstance(ho, dict) and ho.get("relayId") == rid)
                    )
                    if is_current_relay and to_alias:
                        p["device"] = to_alias
                        p["handoff"] = None
                        L.touch(p, to_alias)
                    existing_tags = set(h.get("tag") or h.get("relayId") for h in (p.get("history") or []) if isinstance(h, dict))
                    if rid not in existing_tags:
                        p["history"] = (
                            list(p.get("history") or [])
                            + [
                                {
                                    "tag": rid,
                                    "relayId": rid,
                                    "from": from_alias,
                                    "to": to_alias,
                                    "transport": "cloud",
                                    "doneAt": acked_at,
                                }
                            ]
                        )[-10:]

            L.update(led_path, _apply_ack)

            def _mark_state(st_obj: Dict[str, Any]) -> None:
                entry = st_obj.setdefault("sentRelays", {}).get(rid)
                if isinstance(entry, dict):
                    entry["acked"] = True
                    entry["ackedAt"] = acked_at

            _update_relay_state(base, _mark_state)
            synced.append(
                {
                    "relayId": rid,
                    "projectId": proj_id,
                    "toAlias": to_alias,
                    "receivedAt": acked_at,
                }
            )

    return synced


# ---------------------------------------------------------------- 4. send_project


def send_project(
    project_key: Optional[str] = None,
    to_alias: Optional[str] = None,
    cwd: Optional[str] = None,
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Dict[str, Any]:
    """Package, encrypt, two-phase atomically upload, and verify a project handoff."""
    st_mod = _storage_mod()
    conv_mod = _conversations_mod()
    pkg_mod = _packager_mod()

    base, led_path, local_cfg, _led0, here_id, _cloud, identity, peer_desc, provider = _require_paired_context(
        ledger_path, local, local_home
    )

    sync_receipts(provider, identity, peer_desc, led_path, local_cfg, local_home=base)
    led = L.load(led_path)
    proj = L.find_project(led, project_key, cwd or os.getcwd())
    proj_id = proj["id"]

    default_other = L.other(led["config"], here_id)["id"]
    dest_alias = to_alias or peer_desc.get("alias") or default_other
    if dest_alias == here_id:
        raise RelayError("--to 不能是出发的本机设备自己（%s）。" % dest_alias)
    if dest_alias not in ("a", "b"):
        raise RelayError("无效的目标设备别名：%s（只能是 a 或 b）。" % dest_alias)

    if proj.get("device") != here_id:
        owner_cfg = L.dev(led["config"], proj.get("device"))
        raise RelayError(
            "项目「%s」的最新现场登记在 %s（%s），不在本机（%s）。请从那台设备发起交棒；若现场确实在本机，请先运行 relay set %s --device %s。"
            % (proj["name"], owner_cfg.get("name", proj.get("device")), proj.get("device"), here_id, proj_id, here_id)
        )

    raw_proj_path = (proj.get("paths") or {}).get(here_id) or ""
    if not raw_proj_path:
        raise RelayError(
            "项目「%s」在本机（%s）尚未登记路径，请先运行 relay set %s --path-here <目录>。"
            % (proj["name"], here_id, proj_id)
        )
    proj_path = _canon_local_path(raw_proj_path)
    if not os.path.isdir(proj_path):
        raise RelayError("项目「%s」的本机目录不存在：%s" % (proj["name"], proj_path))

    user_h = os.environ.get("RELAY_DESK_USER_HOME") or None
    facts = scan.git_facts(proj_path)
    pres = scan.presence(proj_path, home=user_h)

    # Refresh project dict copy with latest local facts before collecting & packaging
    proj_work = copy.deepcopy(proj)
    proj_work.setdefault("paths", {})[here_id] = proj_path
    _apply_local_facts_to_project(proj_work, here_id, facts, pres)

    # 1. Collect full conversations & check for gaps BEFORE touching cloud
    conv_bundle = conv_mod.collect_project_conversations(
        proj_work,
        here_id,
        base,
        user_home=user_h,
    )
    # Persist local archive on sender too so sender has unified history index
    conv_mod.merge_and_save_archive(base, proj_id, conv_bundle)

    # 2. Run secret gate BEFORE allocating or uploading
    pkg_mod.scan_secrets_before_pack(proj_path, is_git=facts.get("isGit"))

    # 3. Reuse pending relayId if an earlier upload to the same target was interrupted
    alloc_box: Dict[str, str] = {}

    def _alloc_or_reuse(st_obj: Dict[str, Any]) -> None:
        pending = st_obj.setdefault("pendingSends", {})
        cur = pending.get(proj_id)
        if isinstance(cur, dict) and cur.get("toAlias") == dest_alias and cur.get("relayId"):
            alloc_box["relayId"] = str(cur["relayId"])
            alloc_box["parentRelayId"] = str(cur.get("parentRelayId") or "")
        else:
            day_str = datetime.now(timezone.utc).strftime("%Y%m%d")
            rid = L.new_id("r_%s_" % day_str)
            parent_id = str(proj.get("lastRelayId") or "")
            pending[proj_id] = {
                "relayId": rid,
                "toAlias": dest_alias,
                "toDeviceId": peer_desc["deviceId"],
                "parentRelayId": parent_id,
                "createdAt": L.now_iso(),
            }
            alloc_box["relayId"] = rid
            alloc_box["parentRelayId"] = parent_id

    _update_relay_state(base, _alloc_or_reuse)
    relay_id = alloc_box["relayId"]
    parent_id = alloc_box["parentRelayId"]

    handoff_md_text = ""
    handoff_md_file = Path(proj_path) / "HANDOFF.md"
    if handoff_md_file.is_file():
        try:
            handoff_md_text = handoff_md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            handoff_md_text = ""

    metadata = {
        "id": proj_id,
        "name": proj_work["name"],
        "tool": proj_work.get("tool") or "claude",
        "lastTask": proj_work.get("lastTask") or "",
        "nextAction": proj_work.get("nextAction") or "",
        "note": proj_work.get("note") or "",
        "verify": copy.deepcopy(proj_work.get("verify") or {"state": "none", "cmd": "", "note": ""}),
        "sessions": copy.deepcopy(proj_work.get("sessions") or []),
        "handoffMd": handoff_md_text,
        "worktree": proj_work.get("worktree") or "",
        "remote": proj_work.get("remote") or "",
        "branch": proj_work.get("branch") or "",
        "dirty": proj_work.get("dirty", 0),
        "paths": copy.deepcopy(proj_work.get("paths") or {}),
        "fromAlias": here_id,
        "fromPath": proj_path,
        "toAlias": dest_alias,
        "relayId": relay_id,
        "parentRelayId": parent_id,
    }

    dest_dev_cfg = L.dev(led["config"], dest_alias)
    if dest_dev_cfg.get("osExplicit") and dest_dev_cfg.get("os"):
        target_os = str(dest_dev_cfg["os"])
    else:
        target_os = str(peer_desc.get("os") or dest_dev_cfg.get("os") or "")

    pkg_key = "relays/%s/package.enc" % relay_id
    ready_key = "relays/%s/ready.json" % relay_id
    created_at = L.now_iso()
    session_count = len(conv_bundle.get("entries") or [])

    with tempfile.TemporaryDirectory() as tmpdir:
        tar_path = os.path.join(tmpdir, "payload.tar.gz")
        enc_path = os.path.join(tmpdir, "package.enc")
        ready_path = os.path.join(tmpdir, "ready.json")

        pack_info = pkg_mod.build_payload(
            proj_path,
            tar_path,
            metadata,
            conv_bundle,
            target_os=target_os or None,
        )
        envelope = crypto.encrypt_package(tar_path, enc_path, identity, peer_desc)

        # Two-phase atomic publish: Phase 1 -> package.enc upload + remote size verification
        provider.upload(enc_path, pkg_key)
        st_pkg = provider.stat(pkg_key)
        if st_pkg is None or st_pkg.size != envelope["packageSize"]:
            raise st_mod.StorageError(
                "云端加密交接包校验失败（%s）：远端大小 %s 与本地 %d 不一致。"
                % (pkg_key, getattr(st_pkg, "size", None), envelope["packageSize"])
            )

        # Two-phase atomic publish: Phase 2 -> signed ready.json upload + remote verification
        meta_plain = {
            "projectId": proj_id,
            "projectName": proj_work["name"],
            "lastTask": proj_work.get("lastTask") or "",
            "nextAction": proj_work.get("nextAction") or "",
            "branch": pack_info.get("branch") or proj_work.get("branch") or "",
            "dirty": proj_work.get("dirty", 0),
            "sessionCount": session_count,
        }
        meta_enc = crypto.encrypt_meta(meta_plain, identity, peer_desc)
        ready_unsigned = dict(envelope)
        ready_unsigned.update(
            {
                "formatVersion": 1,
                "relayId": relay_id,
                "fromAlias": here_id,
                "fromDeviceId": identity["deviceId"],
                "toAlias": dest_alias,
                "toDeviceId": peer_desc["deviceId"],
                "parentRelayId": parent_id,
                "createdAt": created_at,
                "packageKey": pkg_key,
                "meta": meta_enc,
            }
        )
        signed_ready = crypto.sign_json(
            ready_unsigned,
            identity["signPrivateKey"],
            signer_device_id=identity["deviceId"],
        )
        _atomic_write_json(Path(ready_path), signed_ready)
        provider.upload(ready_path, ready_key)
        st_ready = provider.stat(ready_key)
        if st_ready is None:
            raise st_mod.StorageError("云端就绪标记（%s）上传后校验未找到该文件。" % ready_key)

    def _upd_sender_ledger(l: Dict[str, Any]) -> None:
        for p in l.get("projects") or []:
            if p.get("id") != proj_id:
                continue
            p.setdefault("paths", {})[here_id] = proj_path
            _apply_local_facts_to_project(p, here_id, facts, pres)
            p["lastRelayId"] = relay_id
            p["handoff"] = {
                "tag": relay_id,
                "relayId": relay_id,
                "from": here_id,
                "to": dest_alias,
                "transport": "cloud",
                "state": "transit",
                "parentRelayId": parent_id,
                "hadDirty": bool(p.get("dirty")),
                "branch": p.get("branch") or "",
                "baseCommit": pack_info.get("headCommit") or "",
                "createdAt": created_at,
            }
            L.touch(p, here_id)

    L.update(led_path, _upd_sender_ledger)

    git_snap_sent = _compute_git_baseline_fingerprint(proj_path) if pack_info.get("mode") == "git" else ""

    def _finalize_send_state(st_obj: Dict[str, Any]) -> None:
        st_obj.setdefault("pendingSends", {}).pop(proj_id, None)
        st_obj.setdefault("sentRelays", {})[relay_id] = {
            "projectId": proj_id,
            "toAlias": dest_alias,
            "toDeviceId": peer_desc["deviceId"],
            "sentAt": created_at,
            "acked": False,
            "ackedAt": "",
        }
        st_obj.setdefault("projectBaselines", {})[proj_id] = {
            "headCommit": pack_info.get("headCommit") or "",
            "folderHashes": pack_info.get("folderHashes") or {},
            "gitSnapshotHash": git_snap_sent,
        }

    _update_relay_state(base, _finalize_send_state)

    return {
        "ok": True,
        "relayId": relay_id,
        "parentRelayId": parent_id,
        "projectId": proj_id,
        "projectName": proj_work["name"],
        "from": here_id,
        "to": dest_alias,
        "packageSize": envelope["packageSize"],
        "packageSha256": envelope["packageSha256"],
        "sessionCount": session_count,
        "excludedFiles": pack_info.get("excludedFiles") or [],
        "message": HANDOFF_DONE_MESSAGE,
    }


# ---------------------------------------------------------------- 5. list_inbox


def list_inbox(
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Dict[str, Any]:
    """Sync receipts and list incoming claimable cloud relay packages for this device."""
    st_mod = _storage_mod()
    base, led_path, local_cfg, _led, here_id, _cloud, identity, peer_desc, provider = _require_paired_context(
        ledger_path, local, local_home
    )

    synced = sync_receipts(provider, identity, peer_desc, led_path, local_cfg, local_home=base)
    state = _load_relay_state(base)
    received_relays = state.get("receivedRelays") or {}

    objs = provider.list_objects("relays/")
    by_relay: Dict[str, Dict[str, Any]] = {}
    for obj in objs:
        key = str(obj.key or "").lstrip("/")
        parts = key.split("/")
        if len(parts) == 3 and parts[0] == "relays" and parts[1]:
            by_relay.setdefault(parts[1], {})[parts[2]] = obj

    incoming: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for rid in sorted(by_relay.keys()):
            files = by_relay[rid]
            # Partial uploads without ready.json or package.enc are NEVER claimable
            if "ready.json" not in files or "package.enc" not in files:
                continue

            rec_local = received_relays.get(rid)
            if isinstance(rec_local, dict) and rec_local.get("receiptUploaded"):
                continue
            pending_ack = bool(isinstance(rec_local, dict) and not rec_local.get("receiptUploaded"))

            if not pending_ack and "receipt.json" in files:
                rec_tmp = os.path.join(tmpdir, "%s_rec.json" % rid)
                try:
                    provider.download("relays/%s/receipt.json" % rid, rec_tmp)
                    with open(rec_tmp, "r", encoding="utf-8-sig") as fh:
                        raw_rec = json.load(fh)
                    crypto.verify_json(
                        raw_rec,
                        identity["verifyPublicKey"],
                        expected_device_id=identity["deviceId"],
                    )
                    # Already signed & uploaded by this device
                    continue
                except Exception:
                    pass

            ready_tmp = os.path.join(tmpdir, "%s_ready.json" % rid)
            try:
                provider.download("relays/%s/ready.json" % rid, ready_tmp)
                with open(ready_tmp, "r", encoding="utf-8-sig") as fh:
                    raw_ready = json.load(fh)
            except (st_mod.StorageError, OSError, ValueError, UnicodeDecodeError):
                continue

            if not isinstance(raw_ready, dict):
                continue

            # Ignore outgoing packages sent from this device
            if raw_ready.get("fromDeviceId") == identity["deviceId"]:
                continue
            to_dev_id = raw_ready.get("toDeviceId")
            to_al = raw_ready.get("toAlias")
            if to_dev_id and to_dev_id != identity["deviceId"] and to_al != here_id:
                continue
            if not to_dev_id and to_al != here_id:
                continue

            try:
                verified_ready = crypto.verify_json(
                    raw_ready,
                    peer_desc["verifyPublicKey"],
                    expected_device_id=peer_desc.get("deviceId"),
                )
                if verified_ready.get("relayId") != rid:
                    raise crypto.TamperError("ready.json 中的 relayId 与目录不一致。")
                pkg_obj_size = files["package.enc"].size
                if pkg_obj_size != verified_ready.get("packageSize"):
                    raise crypto.TamperError("云端 package.enc 大小与签名记录不一致。")
                meta = crypto.decrypt_meta(verified_ready.get("meta") or {}, identity, peer_desc)
                incoming.append(
                    {
                        "relayId": rid,
                        "parentRelayId": str(verified_ready.get("parentRelayId") or ""),
                        "fromAlias": str(verified_ready.get("fromAlias") or ""),
                        "fromDeviceId": str(verified_ready.get("fromDeviceId") or ""),
                        "toAlias": str(verified_ready.get("toAlias") or here_id),
                        "toDeviceId": str(verified_ready.get("toDeviceId") or identity["deviceId"]),
                        "createdAt": str(verified_ready.get("createdAt") or ""),
                        "packageSize": int(verified_ready.get("packageSize") or pkg_obj_size),
                        "packageSha256": str(verified_ready.get("packageSha256") or ""),
                        "projectId": str(meta.get("projectId") or ""),
                        "projectName": str(meta.get("projectName") or ""),
                        "lastTask": str(meta.get("lastTask") or ""),
                        "nextAction": str(meta.get("nextAction") or ""),
                        "branch": str(meta.get("branch") or ""),
                        "dirty": meta.get("dirty", 0),
                        "sessionCount": meta.get("sessionCount", 0),
                        "pendingAck": pending_ack,
                        "tampered": False,
                    }
                )
            except crypto.CryptoError as e:
                incoming.append(
                    {
                        "relayId": rid,
                        "parentRelayId": str(raw_ready.get("parentRelayId") or ""),
                        "fromAlias": str(raw_ready.get("fromAlias") or ""),
                        "toAlias": str(raw_ready.get("toAlias") or here_id),
                        "createdAt": str(raw_ready.get("createdAt") or ""),
                        "packageSize": files["package.enc"].size,
                        "projectId": "",
                        "projectName": "（签名或加密校验异常）",
                        "lastTask": "",
                        "nextAction": "",
                        "branch": "",
                        "dirty": 0,
                        "sessionCount": 0,
                        "pendingAck": pending_ack,
                        "tampered": True,
                        "error": str(e),
                    }
                )

    incoming.sort(key=lambda x: (x.get("createdAt") or "", x.get("relayId") or ""))
    return {"incoming": incoming, "syncedReceipts": synced}


# ---------------------------------------------------------------- 6. receive_relay


def _upload_and_verify_receipt(
    provider,
    relay_id: str,
    signed_receipt: Dict[str, Any],
    proj_name: str,
    dest_dir: str,
) -> None:
    st_mod = _storage_mod()
    receipt_key = "relays/%s/receipt.json" % relay_id
    try:
        with tempfile.TemporaryDirectory() as rtmp:
            rpath = os.path.join(rtmp, "receipt.json")
            _atomic_write_json(Path(rpath), signed_receipt)
            provider.upload(rpath, receipt_key)
            if provider.stat(receipt_key) is None:
                raise st_mod.StorageError("回执上传后远端校验未找到文件：%s" % receipt_key)
    except Exception as e:
        raise ReceiptUploadError(
            relay_id,
            "项目「%s」已在本地应用成功（%s），但上传签收回执失败：%s。"
            "请稍后重新运行 relay receive %s 完成回执补传，不会覆盖本地目录改动。"
            % (proj_name or relay_id, dest_dir, e, relay_id),
        )


def receive_relay(
    relay_id: str,
    target_path: Optional[str] = None,
    into_path: Optional[str] = None,
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Dict[str, Any]:
    """Safely verify, decrypt, unpack, apply, archive conversations, and acknowledge a relay."""
    relay_id = str(relay_id or "").strip()
    if not RELAY_ID_RE.match(relay_id) or relay_id in (".", ".."):
        raise RelayError("无效的交接包编号：%r" % relay_id)

    st_mod = _storage_mod()
    conv_mod = _conversations_mod()
    pkg_mod = _packager_mod()

    base, led_path, local_cfg, _led0, here_id, _cloud, identity, peer_desc, provider = _require_paired_context(
        ledger_path, local, local_home
    )

    state = _load_relay_state(base)
    rec_entry = (state.get("receivedRelays") or {}).get(relay_id)

    # 1. CRITICAL Recovery: local apply already succeeded earlier, only receipt upload failed!
    if isinstance(rec_entry, dict) and not rec_entry.get("receiptUploaded"):
        signed_receipt = rec_entry.get("signedReceipt")
        saved_result = copy.deepcopy(rec_entry.get("result") or {})
        saved_proj_name = str(saved_result.get("projectName") or rec_entry.get("projectId") or relay_id)
        saved_dest = str(rec_entry.get("targetPath") or saved_result.get("targetPath") or "")
        if not isinstance(signed_receipt, dict):
            raise RelayError("本地回执恢复记录损坏（缺少 signedReceipt）：%s" % relay_id)
        _upload_and_verify_receipt(provider, relay_id, signed_receipt, saved_proj_name, saved_dest)

        def _mark_recovered(st_obj: Dict[str, Any]) -> None:
            ent = st_obj.setdefault("receivedRelays", {}).get(relay_id)
            if isinstance(ent, dict):
                ent["receiptUploaded"] = True

        _update_relay_state(base, _mark_recovered)
        return saved_result

    # 2. Reject duplicate receive (local state or existing remote receipt signed by this device)
    if isinstance(rec_entry, dict) and rec_entry.get("receiptUploaded"):
        raise DuplicateReceiveError("交接包 %s 已经领取并签收，不能重复领取。" % relay_id, relay_id=relay_id)

    receipt_key = "relays/%s/receipt.json" % relay_id
    if provider.stat(receipt_key) is not None:
        with tempfile.TemporaryDirectory() as ctmp:
            cpath = os.path.join(ctmp, "receipt.json")
            try:
                provider.download(receipt_key, cpath)
                with open(cpath, "r", encoding="utf-8-sig") as fh:
                    rem_rec = json.load(fh)
                crypto.verify_json(
                    rem_rec,
                    identity["verifyPublicKey"],
                    expected_device_id=identity["deviceId"],
                )
                is_dup = True
            except Exception:
                is_dup = False
        if is_dup:
            raise DuplicateReceiveError("交接包 %s 已经领取并签收，不能重复领取。" % relay_id, relay_id=relay_id)

    # 3. Download, verify Ed25519 signature, decrypt secretstream, and verify payload inside temp dir!
    ready_key = "relays/%s/ready.json" % relay_id
    expected_pkg_key = "relays/%s/package.enc" % relay_id

    with tempfile.TemporaryDirectory() as tmpdir:
        ready_local = os.path.join(tmpdir, "ready.json")
        enc_local = os.path.join(tmpdir, "package.enc")
        tar_local = os.path.join(tmpdir, "payload.tar.gz")
        staging_dir = os.path.join(tmpdir, "staging")

        try:
            provider.download(ready_key, ready_local)
        except st_mod.ObjectNotFoundError:
            raise RelayError("找不到可领取的交接包 %s（云端不存在 ready.json，可能尚未上传完成）。" % relay_id)

        try:
            with open(ready_local, "r", encoding="utf-8-sig") as fh:
                raw_ready = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError) as e:
            raise crypto.TamperError("交接包 %s 的 ready.json 已损坏，不是有效 JSON：%s" % (relay_id, e))

        verified_ready = crypto.verify_json(
            raw_ready,
            peer_desc["verifyPublicKey"],
            expected_device_id=peer_desc.get("deviceId"),
        )
        if verified_ready.get("relayId") != relay_id:
            raise crypto.TamperError(
                "交接包编号不匹配：期望 %s，实际 %s。" % (relay_id, verified_ready.get("relayId"))
            )

        if verified_ready.get("toDeviceId") and verified_ready.get("toDeviceId") != identity["deviceId"]:
            raise RelayError(
                "交接包 %s 的目标设备不是本机（目标：%s，本机：%s）。"
                % (relay_id, verified_ready.get("toAlias") or verified_ready.get("toDeviceId"), here_id)
            )
        if verified_ready.get("toAlias") and verified_ready.get("toAlias") != here_id:
            raise RelayError(
                "交接包 %s 的目标设备是 %s，不是本机（%s）。"
                % (relay_id, verified_ready.get("toAlias"), here_id)
            )

        pkg_key = verified_ready.get("packageKey") or expected_pkg_key
        if pkg_key != expected_pkg_key:
            raise crypto.TamperError("交接包 %s 的加密包路径异常：%s" % (relay_id, pkg_key))

        try:
            provider.download(pkg_key, enc_local)
        except st_mod.ObjectNotFoundError:
            raise RelayError("交接包 %s 的加密包文件缺失：%s" % (relay_id, pkg_key))

        crypto.decrypt_package(enc_local, tar_local, verified_ready, identity, peer_desc)
        extracted = pkg_mod.extract_and_verify_payload(tar_local, staging_dir)

        manifest = extracted["manifest"]
        metadata = extracted["metadata"]
        conv_bundle = extracted["conversations"]

        # 4. Resolve destination directory & verify target cleanliness BEFORE modifying workspace
        led = L.load(led_path)
        proj_id = str(metadata.get("id") or conv_bundle.get("projectId") or "")
        if not proj_id:
            raise pkg_mod.PackageError("交接包缺少有效的项目 ID。")
        proj_name = str(metadata.get("name") or proj_id)

        existing_proj = next((p for p in (led.get("projects") or []) if p.get("id") == proj_id), None)
        existing_here_raw = (existing_proj.get("paths") or {}).get(here_id) if existing_proj else ""
        existing_here_canon = _canon_local_path(existing_here_raw) if existing_here_raw else ""

        if into_path:
            raw_dest = into_path
        elif target_path:
            raw_dest = target_path
        elif existing_here_raw:
            raw_dest = existing_here_raw
        else:
            raw_dest = ""

        if not raw_dest:
            raise NeedTargetPathError(
                "首次在本机接收项目「%s」，请用 --path 指定项目目录。" % proj_name,
                project_id=proj_id,
                project_name=proj_name,
            )

        dest_dir = _canon_local_path(raw_dest)
        baseline = (state.get("projectBaselines") or {}).get(proj_id) or {}
        use_baseline = baseline if (not existing_here_canon or dest_dir == existing_here_canon) else {}

        try:
            pkg_mod.check_target_clean_for_receive(
                dest_dir,
                manifest,
                expected_base_head=use_baseline.get("headCommit") or None,
                expected_folder_hashes=(
                    use_baseline["folderHashes"] if "folderHashes" in use_baseline else None
                ),
            )
        except pkg_mod.TargetDirtyError as e:
            # If dest_dir is 100% identical to the snapshot recorded when this machine last
            # handed off / received this Git project, and all reasons are merely those
            # previously handed-off uncommitted changes (no diverged local commits), allow clean apply!
            unchanged_handoff_baseline = False
            saved_git_snap = use_baseline.get("gitSnapshotHash") or ""
            if (
                manifest.get("mode") == "git"
                and saved_git_snap
                and os.path.isdir(dest_dir)
                and _compute_git_baseline_fingerprint(dest_dir) == saved_git_snap
            ):
                reasons_list = getattr(e, "reasons", []) or []
                if reasons_list and all(
                    isinstance(r, str) and r.startswith(("存在本地未跟踪文件：", "存在本地未提交改动"))
                    for r in reasons_list
                ):
                    unchanged_handoff_baseline = True

            if not unchanged_handoff_baseline:
                msg = str(e)
                if not into_path and "--into" not in msg:
                    msg = "%s 原目录已完整保留；如需保留本地改动并接棒到新目录，请加 --into <新目录>。" % msg
                dirty_err = pkg_mod.TargetDirtyError(
                    msg,
                    target_path=getattr(e, "target_path", dest_dir),
                    reasons=getattr(e, "reasons", []),
                )
                dirty_err.project_id = proj_id
                dirty_err.project_name = proj_name
                raise dirty_err

        # 5. Apply verified payload to destination directory
        apply_res = pkg_mod.apply_verified_payload(staging_dir, dest_dir)

    # 6. Merge full conversation archive locally
    conv_res = conv_mod.merge_and_save_archive(base, proj_id, conv_bundle)

    # 7. Scan applied destination directory & update local ledger.json
    user_h = os.environ.get("RELAY_DESK_USER_HOME") or None
    facts = scan.git_facts(dest_dir)
    pres = scan.presence(dest_dir, home=user_h)
    from_alias = str(verified_ready.get("fromAlias") or L.other(led["config"], here_id)["id"])
    received_at = L.now_iso()

    def _upd_receiver_ledger(l: Dict[str, Any]) -> None:
        target_p = None
        for p in l.get("projects") or []:
            if p.get("id") == proj_id:
                target_p = p
                break
        if target_p is None:
            target_p = L.norm_project(
                {
                    "id": proj_id,
                    "name": proj_name,
                    "tool": metadata.get("tool") or "claude",
                    "device": here_id,
                },
                l["config"],
            )
            l.setdefault("projects", []).append(target_p)

        if metadata.get("name"):
            target_p["name"] = metadata["name"]
        if metadata.get("tool"):
            target_p["tool"] = metadata["tool"]
        target_p["device"] = here_id
        paths_map = target_p.setdefault("paths", {})
        paths_map[here_id] = dest_dir
        sender_path = metadata.get("fromPath") or (metadata.get("paths") or {}).get(from_alias) or ""
        if sender_path:
            paths_map[from_alias] = sender_path
        elif from_alias not in paths_map:
            paths_map[from_alias] = ""

        for fld in ("lastTask", "nextAction", "note", "worktree"):
            if fld in metadata:
                target_p[fld] = metadata.get(fld) or ""
        if isinstance(metadata.get("verify"), dict):
            target_p.setdefault("verify", {"state": "none", "cmd": "", "note": ""}).update(metadata["verify"])
        if metadata.get("remote") and not target_p.get("remote"):
            target_p["remote"] = metadata["remote"]

        target_p["sessions"] = _merge_sessions_meta(
            target_p.get("sessions") or [],
            metadata.get("sessions") or [],
        )
        target_p["lastRelayId"] = relay_id
        target_p["handoff"] = None
        target_p["history"] = (
            list(target_p.get("history") or [])
            + [
                {
                    "tag": relay_id,
                    "relayId": relay_id,
                    "parentRelayId": str(verified_ready.get("parentRelayId") or ""),
                    "from": from_alias,
                    "to": here_id,
                    "transport": "cloud",
                    "doneAt": received_at,
                }
            ]
        )[-10:]
        _apply_local_facts_to_project(target_p, here_id, facts, pres)
        L.touch(target_p, here_id)

    L.update(led_path, _upd_receiver_ledger)

    receipt_unsigned = {
        "formatVersion": 1,
        "relayId": relay_id,
        "projectId": proj_id,
        "fromAlias": from_alias,
        "toAlias": here_id,
        "receiverDeviceId": identity["deviceId"],
        "packageSha256": verified_ready["packageSha256"],
        "receivedAt": received_at,
    }
    signed_receipt = crypto.sign_json(
        receipt_unsigned,
        identity["signPrivateKey"],
        signer_device_id=identity["deviceId"],
    )

    result = {
        "ok": True,
        "relayId": relay_id,
        "parentRelayId": str(verified_ready.get("parentRelayId") or ""),
        "projectId": proj_id,
        "projectName": proj_name,
        "from": from_alias,
        "to": here_id,
        "targetPath": dest_dir,
        "mode": apply_res.get("mode", ""),
        "branch": apply_res.get("branch") or facts.get("branch") or "",
        "headCommit": apply_res.get("headCommit") or "",
        "lastTask": str(metadata.get("lastTask") or ""),
        "nextAction": str(metadata.get("nextAction") or ""),
        "note": str(metadata.get("note") or ""),
        "verify": copy.deepcopy(metadata.get("verify") or {"state": "none", "cmd": "", "note": ""}),
        "conversations": {
            "total": conv_res.get("total", 0),
            "added": conv_res.get("added", 0),
            "updated": conv_res.get("updated", 0),
            "skipped": conv_res.get("skipped", 0),
            "archiveDir": conv_res.get("archiveDir", ""),
        },
        "message": "接棒完成：%s 已就绪（%s）" % (proj_name, dest_dir),
    }

    git_snap_recv = _compute_git_baseline_fingerprint(dest_dir) if apply_res.get("mode") == "git" else ""

    # 8. CRITICAL: Persist receivedRelays with receiptUploaded=False BEFORE uploading receipt.json!
    def _record_local_applied(st_obj: Dict[str, Any]) -> None:
        st_obj.setdefault("projectBaselines", {})[proj_id] = {
            "headCommit": apply_res.get("headCommit") or "",
            "folderHashes": apply_res.get("folderHashes") or {},
            "gitSnapshotHash": git_snap_recv,
        }
        st_obj.setdefault("receivedRelays", {})[relay_id] = {
            "projectId": proj_id,
            "targetPath": dest_dir,
            "receivedAt": received_at,
            "receiptUploaded": False,
            "signedReceipt": signed_receipt,
            "result": result,
        }

    _update_relay_state(base, _record_local_applied)

    # 9. Upload signed receipt.json & verify remote stat, then mark receiptUploaded=True
    _upload_and_verify_receipt(provider, relay_id, signed_receipt, proj_name, dest_dir)

    def _mark_receipt_uploaded(st_obj: Dict[str, Any]) -> None:
        ent = st_obj.setdefault("receivedRelays", {}).get(relay_id)
        if isinstance(ent, dict):
            ent["receiptUploaded"] = True

    _update_relay_state(base, _mark_receipt_uploaded)
    return result


# ---------------------------------------------------------------- 7. query_transcripts


def query_transcripts(
    project_key: Optional[str] = None,
    session_id: Optional[str] = None,
    tool: Optional[str] = None,
    grep: Optional[str] = None,
    cwd: Optional[str] = None,
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Dict[str, Any]:
    """Query archived cross-device conversation transcripts for a project."""
    conv_mod = _conversations_mod()
    base, led_path, _local_cfg = _resolve_ctx(ledger_path, local, local_home)
    led = L.load(led_path)
    try:
        proj = L.find_project(led, project_key, cwd or os.getcwd())
        proj_id = proj["id"]
    except LookupError:
        if project_key and RELAY_ID_RE.match(str(project_key)):
            idx = conv_mod.archive_dir(base, str(project_key)) / "index.json"
            if idx.exists():
                proj_id = str(project_key)
            else:
                raise
        else:
            raise
    return conv_mod.query_archive(
        base,
        proj_id,
        session_id=session_id or None,
        tool=tool or None,
        grep=grep or None,
    )
