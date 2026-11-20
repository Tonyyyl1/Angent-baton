# -*- coding: utf-8 -*-
"""Unified encrypted asynchronous cloud relay engine for relay-desk (v3 + v2 adapter).

Shared orchestration layer invoked by CLI (`relaydesk.cli`), local Web Workbench
HTTP API (`relaydesk.server`), and agent skills.
Python 3.9 compatible; UTF-8; Chinese user-facing messages.
"""
import copy
import hashlib
import json
import os
import re
import tarfile
import tempfile
import time
import unicodedata
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import crypto
from . import devices
from . import ledger as L
from . import scan
from . import validation as V
from . import workspace_tx

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
    "AmbiguousPeerError",
    "PeerConflictError",
    "KeyChangedError",
    "UnauthorizedReceiverError",
    "ActiveHandoffConflictError",
    "VersionConflictError",
    "PendingVerificationError",
    "LocalStateCorruptError",
    "PathEscapeError",
    "load_cloud_config",
    "load_relay_state",
    "HANDOFF_DONE_MESSAGE",
    "cloud_status",
    "list_devices",
    "pair_device",
    "sync_receipts",
    "publish_project",
    "sync_network",
    "send_project",
    "list_inbox",
    "receive_relay",
    "recover_relay",
    "query_transcripts",
]

HANDOFF_DONE_MESSAGE = "已交棒，可以关机"
NEED_DEVICE_MSG = "无法识别本机是哪台设备。请先运行 relay init --device a（或 b，或自定义别名）。"
RELAY_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_GENERIC_DEFAULT_NAMES = frozenset(("设备 a", "设备 b", "device a", "device b"))
_LOCAL_CAPABILITIES = ["v2", "v3"]
MAX_KEY_HISTORY = 10  # previously confirmed public keys kept per peer (cloud.json peers[*].keyHistory)


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


class AmbiguousPeerError(RelayError):
    """Raised when target peer selection matches multiple devices."""

    def __init__(
        self,
        message: str,
        candidates: Optional[List[Dict[str, Any]]] = None,
        code: str = "ambiguous_peer",
    ) -> None:
        RelayError.__init__(self, message, code=code)
        self.candidates = list(candidates or [])


class PeerConflictError(RelayError):
    """Raised when an untrusted device claims an existing alias or name."""

    def __init__(self, message: str, code: str = "alias_conflict") -> None:
        RelayError.__init__(self, message, code=code)


class KeyChangedError(RelayError):
    """Raised when a paired peer's public keys rotated and require re-verification."""

    def __init__(
        self,
        message: str,
        peer_id: str = "",
        old_fingerprint: str = "",
        new_fingerprint: str = "",
        code: str = "peer_key_changed",
    ) -> None:
        RelayError.__init__(self, message, code=code)
        self.peer_id = peer_id
        self.old_fingerprint = old_fingerprint
        self.new_fingerprint = new_fingerprint


class UnauthorizedReceiverError(RelayError):
    """Raised when a device tries to claim a handoff or sync snapshot not authorized for it."""

    def __init__(self, message: str, code: str = "unauthorized_receiver") -> None:
        RelayError.__init__(self, message, code=code)


class ActiveHandoffConflictError(RelayError):
    """Raised when trying to send to a different device while an unacked handoff is in flight."""

    def __init__(
        self,
        message: str,
        relay_id: str = "",
        target_alias: str = "",
        code: str = "active_handoff_exists",
    ) -> None:
        RelayError.__init__(self, message, code=code)
        self.relay_id = relay_id
        self.target_alias = target_alias


class VersionConflictError(RelayError):
    """Raised on concurrent version fork or version regression."""

    def __init__(
        self,
        message: str,
        code: str = "version_fork",
        local_version: str = "",
        incoming_version: str = "",
    ) -> None:
        RelayError.__init__(self, message, code=code)
        self.local_version = local_version
        self.incoming_version = incoming_version


class PendingVerificationError(RelayError):
    """Raised when a migrated v2 pendingSend lacks cloud publication proof."""

    def __init__(
        self,
        message: str,
        relay_id: str = "",
        code: str = "pending_send_needs_verification",
    ) -> None:
        RelayError.__init__(self, message, code=code)
        self.relay_id = relay_id


# Re-exported so CLI/API callers can classify Phase A failures without importing validation.
LocalStateCorruptError = V.LocalStateCorruptError
PathEscapeError = V.PathEscapeError


@contextmanager
def _remote_fields(context: str = ""):
    """Report malformed or inconsistent fields of verified remote records as integrity failures."""
    try:
        yield
    except V.ValidationError as e:
        raise crypto.InvalidSignedFieldError.from_validation(e, context) from e


def _check_remote(fn, *args, **kwargs):
    with _remote_fields():
        return fn(*args, **kwargs)


def _require_consistent_ready(verified_ready: Dict[str, Any], delivery_id: str, sender_did: str,
                              my_did: str, expected_pkg_key: str, purpose: str) -> None:
    """Cross-check a verified ready.json against its key path, signer, and this device."""
    with _remote_fields("交接记录 %s" % delivery_id):
        # relayId is the field every later step consumes; the informational
        # deliveryId copy is not read anywhere, so it is not compared.
        V.require_equal(verified_ready.get("relayId"), delivery_id, "relayId", "记录与云端路径")
        for key in ("senderDeviceId", "fromDeviceId", "signerDeviceId"):
            if verified_ready.get(key) not in (None, ""):
                V.require_equal(verified_ready.get(key), sender_did, key, "签名设备")
        for key in ("toDeviceId", "recipientDeviceId"):
            if verified_ready.get(key) not in (None, ""):
                V.require_equal(verified_ready.get(key), my_did, key, "目标设备")
        if purpose == "handoff" and verified_ready.get("authorizedDeviceId") not in (None, ""):
            V.require_equal(verified_ready.get("authorizedDeviceId"), my_did, "authorizedDeviceId", "授权设备")
        if verified_ready.get("packageKey") not in (None, ""):
            V.require_equal(verified_ready.get("packageKey"), expected_pkg_key, "packageKey", "加密包路径")


def _require_same_project(snapshot_project: str, metadata: Dict[str, Any], conv_bundle: Dict[str, Any],
                          meta_plain: Optional[Dict[str, Any]], context: str) -> None:
    """The signed snapshot, the payload metadata/conversations and the preview must name one project."""
    with _remote_fields(context):
        V.validate_project_id(snapshot_project, "snapshot.projectId")
        if metadata.get("id") not in (None, ""):
            V.require_equal(metadata.get("id"), snapshot_project, "metadata.id", "载荷与签名快照")
        if isinstance(conv_bundle, dict) and conv_bundle.get("projectId") not in (None, ""):
            V.require_equal(conv_bundle.get("projectId"), snapshot_project, "conversations.projectId", "载荷与签名快照")
        if isinstance(meta_plain, dict) and meta_plain.get("projectId") not in (None, ""):
            V.require_equal(meta_plain.get("projectId"), snapshot_project, "meta.projectId", "预览与签名快照")


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


def _sha256_file_info(path: str) -> Tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


def _norm_fp(fp: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(fp or "")).upper()


DEFAULT_CLOUD: Dict[str, Any] = {
    "version": devices.CLOUD_CONFIG_VERSION,
    "provider": "rclone",
    "remote": "",
    "root": "",
    "configPath": "",
    "shownFingerprints": {},
    "peers": {},
}

DEFAULT_RELAY_STATE: Dict[str, Any] = {
    "version": devices.RELAY_STATE_VERSION,
    "pendingSends": {},
    "sentRelays": {},
    "appliedReceives": {},
    "receivedRelays": {},
    "projectBaselines": {},
    "activeHandoffs": {},
    "versionGraph": {},
    "localCurrentVersion": {},
    "processedDeliveries": {},
    "archivedRelayIds": {},
    "forwardedSnapshots": {},
}


def _read_local_state(path: Path, kind: str, label: str, problems_fn=None) -> Optional[Dict[str, Any]]:
    """``None`` when never created; LocalStateCorruptError when present but unusable."""
    raw = V.read_local_json_object(path, kind, label)
    if raw is not None and problems_fn is not None:
        problems = problems_fn(raw)
        if problems:
            raise V.LocalStateCorruptError(
                "本机%s结构已损坏（%s）：%s。为避免覆盖已有数据，操作已停止；请先备份并修复或恢复该文件，不要直接删除。"
                % (label, "；".join(problems[:5]), path),
                path=str(path),
                kind=kind,
            )
    return raw


def _load_cloud(local_home: Path) -> Dict[str, Any]:
    raw = _read_local_state(local_home / "cloud.json", "cloud", "云端连接配置 cloud.json",
                            devices.cloud_config_problems)
    if raw is None:
        return copy.deepcopy(DEFAULT_CLOUD)
    return devices.migrate_cloud_config(raw, local_home=local_home)


def _save_cloud(local_home: Path, cloud_cfg: Dict[str, Any]) -> None:
    normed = devices.migrate_cloud_config(cloud_cfg, local_home=local_home)
    synced = devices.sync_legacy_cloud_fields(normed)
    _atomic_write_json(local_home / "cloud.json", synced)


def _update_cloud(local_home: Path, fn) -> Dict[str, Any]:
    cloud_path = local_home / "cloud.json"
    with L._Lock(cloud_path):
        cl = _load_cloud(local_home)
        fn(cl)
        _save_cloud(local_home, cl)
        return _load_cloud(local_home)


def _is_cloud_configured(cloud_cfg: Dict[str, Any]) -> bool:
    prov = cloud_cfg.get("provider") or "rclone"
    if prov == "local":
        return bool(str(cloud_cfg.get("root") or "").strip())
    return bool(str(cloud_cfg.get("remote") or "").strip())


def _load_relay_state(local_home: Path) -> Dict[str, Any]:
    raw = _read_local_state(local_home / "relay_state.json", "relay_state", "接力状态 relay_state.json",
                            devices.relay_state_problems)
    if raw is None:
        return copy.deepcopy(DEFAULT_RELAY_STATE)
    return devices.migrate_relay_state(raw, local_home=local_home)


def load_cloud_config(local_home: Any) -> Optional[Dict[str, Any]]:
    """Read-only strict loader: normalized cloud.json, ``None`` if never created.

    Raises LocalStateCorruptError (code ``local_state_corrupted``) for a damaged
    file. Never writes, not even the one-time v2 migration backup.
    """
    home = Path(local_home)
    raw = _read_local_state(home / "cloud.json", "cloud", "云端连接配置 cloud.json", devices.cloud_config_problems)
    return None if raw is None else devices.migrate_cloud_config(raw, local_home=None)


def load_relay_state(local_home: Any) -> Optional[Dict[str, Any]]:
    """Read-only strict loader for relay_state.json; ``None`` if never created. Never writes."""
    home = Path(local_home)
    raw = _read_local_state(home / "relay_state.json", "relay_state", "接力状态 relay_state.json",
                            devices.relay_state_problems)
    return None if raw is None else devices.migrate_relay_state(raw, local_home=None)


def _save_relay_state(local_home: Path, state: Dict[str, Any]) -> None:
    _atomic_write_json(local_home / "relay_state.json", state)


def _update_relay_state(local_home: Path, fn) -> Dict[str, Any]:
    state_path = local_home / "relay_state.json"
    with L._Lock(state_path):
        st = _load_relay_state(local_home)
        fn(st)
        _save_relay_state(local_home, st)
        return st


@contextmanager
def _workspace_operation(local_home: Path):
    """Hold an OS lock across a send, publish, sync, or receive, including cloud transfers."""
    local_home.mkdir(parents=True, exist_ok=True)
    with (local_home / "workspace-operation.lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RelayError("本机有其他交棒、同步或接棒操作正在进行，请稍后重试。", code="busy") from e
        yield


# ---- Fix H: receive waits a bounded time for a sync round that holds the workspace lock

LOCK_WAIT_SEC = 20.0          # longest wait for a sync round; then the explicit "busy" error
_LOCK_HOLDER_FILE = "workspace-operation.holder.json"
_LOCK_WAIT_CLOCK = None       # tests: a fake clock (monotonic / sleep); None = wall clock


@contextmanager
def _lock_holder(local_home: Path, purpose: str):
    """Advertise what holds the workspace lock (best effort; only read to decide whether waiting helps)."""
    path = Path(local_home) / _LOCK_HOLDER_FILE
    try:
        with path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"purpose": purpose, "pid": os.getpid()}))
    except OSError:
        pass
    try:
        yield
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _held_by_sync_round(local_home: Path) -> bool:
    """The lock holder says it is a sync round of a live process (anything unknown: not waited for)."""
    try:
        with (Path(local_home) / _LOCK_HOLDER_FILE).open(encoding="utf-8") as fh:
            info = json.loads(fh.read())
        pid = int(info.get("pid"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    if info.get("purpose") != "sync" or pid <= 0:
        return False
    if os.name != "nt" and pid != os.getpid():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False  # stale marker of a killed sync process
        except OSError:
            pass  # exists, owned by someone else
    return True


@contextmanager
def _workspace_operation_after_sync_round(local_home: Path, max_wait: Optional[float] = None):
    """``_workspace_operation`` that waits up to ``LOCK_WAIT_SEC`` while a sync round holds the lock.

    Sync rounds are short and release the lock between rounds, so a receive
    started during one would otherwise fail with "busy" for no lasting reason.
    Any other holder (send, publish, another receive, an unknown process), or a
    wait past the bound, still raises the explicit ``busy`` error at once.
    """
    clock = _LOCK_WAIT_CLOCK if _LOCK_WAIT_CLOCK is not None else SR.RealClock()
    limit = LOCK_WAIT_SEC if max_wait is None else float(max_wait)
    started = clock.monotonic()
    polls = 0
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(_workspace_operation(local_home))
                break
            except RelayError as e:
                if e.code != "busy" or not _held_by_sync_round(local_home):
                    raise
                left = limit - (clock.monotonic() - started)
                if left <= 0:
                    raise
                polls += 1
                clock.sleep(min(left, 0.2 + 0.1 * min(polls, 8)))
        yield


def _resolve_ctx(
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Tuple[Path, Path, Dict[str, Any]]:
    base = Path(local_home) if local_home is not None else L.local_dir()
    if local is None:
        if local_home is not None:
            local = _read_local_state(base / "config.json", "config", "设备配置 config.json") or {}
        else:
            local = L.load_local()
    led_path = Path(ledger_path) if ledger_path is not None else L.ledger_path(local)
    return base, led_path, local


def _read_existing_identity_public(local_home: Path) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    data = crypto.load_identity(local_home)
    if data is None:
        return "", "", None
    dev_id = str(data.get("deviceId") or "")
    fp = str(data.get("keyFingerprint") or "")
    if not fp and data.get("verifyPublicKey") and data.get("boxPublicKey"):
        try:
            fp = crypto.device_fingerprint(data["verifyPublicKey"], data["boxPublicKey"])
        except Exception:
            fp = ""
    try:
        desc = crypto.public_descriptor_v3(data, capabilities=_LOCAL_CAPABILITIES) if dev_id else None
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


def _paired_active_peers(cloud: Dict[str, Any]) -> List[Dict[str, Any]]:
    peers_map = cloud.get("peers") if isinstance(cloud.get("peers"), dict) else {}
    return [
        p
        for p in peers_map.values()
        if isinstance(p, dict) and bool(p.get("paired")) and p.get("status") == "paired"
    ]


def _key_changed_peers(cloud: Dict[str, Any]) -> List[Dict[str, Any]]:
    peers_map = cloud.get("peers") if isinstance(cloud.get("peers"), dict) else {}
    return [
        p
        for p in peers_map.values()
        if isinstance(p, dict) and p.get("status") == "key_changed"
    ]


def _peer_is_v3(peer: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(peer, dict):
        return False
    caps = peer.get("capabilities") or []
    return "v3" in caps


def _infer_device_os(led: Dict[str, Any], here_id: str, existing_ident_exists: bool) -> str:
    env_os = os.environ.get("RELAY_DESK_OS", "").strip().lower()
    if env_os in ("windows", "mac", "linux"):
        return env_os
    my_dev = L.dev(led["config"], here_id)
    if my_dev.get("osExplicit") and my_dev.get("os") in ("windows", "mac", "linux"):
        return str(my_dev["os"])
    default_devs = L.default_config()["devices"]
    if here_id == "a":
        default_os = default_devs[0]["os"]
    elif here_id == "b":
        default_os = default_devs[1]["os"]
    else:
        default_os = "linux"
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


def _project_path_for_device(proj: Dict[str, Any], alias: str, device_id: str) -> str:
    if device_id:
        p_by_did = proj.get("pathsByDeviceId")
        if isinstance(p_by_did, dict) and p_by_did.get(device_id):
            return str(p_by_did[device_id])
    return str((proj.get("paths") or {}).get(alias) or "")


# ---------------------------------------------------------------- descriptor discovery & conflict checks


def _verify_descriptor_obj(raw: Any, remote_key: str, default_v3: bool) -> Dict[str, Any]:
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

    raw_caps = verified.get("capabilities")
    if default_v3:
        caps = [str(c) for c in raw_caps] if isinstance(raw_caps, list) and raw_caps else ["v2", "v3"]
        if "v2" not in caps:
            caps.insert(0, "v2")
        if "v3" not in caps:
            caps.append("v3")
        proto_ver = int(verified.get("protocolVersion") or 3)
        return crypto.public_descriptor(verified, protocol_version=proto_ver, capabilities=caps)

    if isinstance(raw_caps, list) and "v3" in raw_caps:
        caps = [str(c) for c in raw_caps]
        if "v2" not in caps:
            caps.insert(0, "v2")
        proto_ver = int(verified.get("protocolVersion") or 3)
        return crypto.public_descriptor(verified, protocol_version=proto_ver, capabilities=caps)

    desc = crypto.public_descriptor(verified)
    desc["protocolVersion"] = 2
    desc["capabilities"] = ["v2"]
    return desc


def _download_descriptor(provider, remote_key: str, default_v3: bool) -> Optional[Dict[str, Any]]:
    st_mod = _storage_mod()
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
    return _verify_descriptor_obj(raw, remote_key, default_v3=default_v3)


def _discover_remote_descriptors(
    provider,
    my_device_id: str,
    here_id: str,
) -> Dict[str, Dict[str, Any]]:
    discovered: Dict[str, Dict[str, Any]] = {}

    for obj in provider.list_objects("v3/devices/"):
        key = str(obj.key or "").lstrip("/")
        if not key.startswith("v3/devices/") or not key.endswith(".json"):
            continue
        key_did = key[len("v3/devices/"):-len(".json")]
        try:
            V.validate_device_id(key_did)
        except V.ValidationError:
            continue
        desc = _download_descriptor(provider, key, default_v3=True)
        if desc is None:
            continue
        # A descriptor is only accepted under its own deviceId: the key path
        # and the signed deviceId must agree.
        if desc["deviceId"] != key_did:
            continue
        if desc["deviceId"] == my_device_id:
            continue
        discovered[desc["deviceId"]] = desc

    for legacy_alias in ("a", "b"):
        if legacy_alias == here_id:
            continue
        legacy_key = "devices/%s.json" % legacy_alias
        if provider.stat(legacy_key) is None:
            continue
        desc = _download_descriptor(provider, legacy_key, default_v3=False)
        if desc is None:
            continue
        try:
            V.validate_device_id(desc["deviceId"])
        except V.ValidationError:
            continue
        if desc["deviceId"] == my_device_id:
            raise RelayError(
                "云端对端设备（%s）与本机拥有相同的 deviceId，请检查本机身份配置。" % legacy_alias
            )
        if desc["deviceId"] not in discovered:
            discovered[desc["deviceId"]] = desc

    return discovered


def _find_alias_or_name_collision(
    cand_alias: str,
    cand_name: str,
    cand_did: str,
    here_id: str,
    local_name: str,
    peers_map: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[str], List[str]]:
    """Check whether a new deviceId collides with local alias/name or another known peer.

    Returns `(conflict_reason_or_None, colliding_peer_device_ids)`.
    """
    colliding_dids: List[str] = []
    al_l = cand_alias.strip().lower()
    nm_l = cand_name.strip().lower()
    is_generic_nm = (not nm_l) or (nm_l in _GENERIC_DEFAULT_NAMES)

    if al_l and al_l == here_id.strip().lower():
        return (
            "远端新设备（%s）声明了与本机相同的别名「%s」，但 deviceId 不同，拒绝自动信任。"
            % (cand_did, cand_alias),
            colliding_dids,
        )
    local_nm_l = local_name.strip().lower()
    if (not is_generic_nm) and local_nm_l and (local_nm_l not in _GENERIC_DEFAULT_NAMES) and nm_l == local_nm_l:
        return (
            "远端新设备（%s）声明了与本机相同的设备名称「%s」，拒绝自动信任。"
            % (cand_did, cand_name),
            colliding_dids,
        )

    for did, p in peers_map.items():
        if not isinstance(p, dict) or did == cand_did:
            continue
        p_al = str(p.get("alias") or "").strip()
        p_nm = str(p.get("name") or "").strip()
        if al_l and p_al and al_l == p_al.lower():
            colliding_dids.append(did)
        elif (not is_generic_nm) and p_nm and (p_nm.lower() not in _GENERIC_DEFAULT_NAMES) and nm_l == p_nm.lower():
            colliding_dids.append(did)

    if colliding_dids:
        old_did = colliding_dids[0]
        old_al = str(peers_map[old_did].get("alias") or old_did)
        return (
            "检测到设备别名或名称冲突：新设备 %s 声明别名「%s」，但该别名已绑定到另一个 deviceId（%s）。"
            "基于安全原则绝不按别名继承信任；如需替换旧设备请在核对指纹后使用 --replace-device --accept --fingerprint <指纹>，"
            "或使用 --with <deviceId> --alias <新别名> 为其分配独立别名。"
            % (cand_did, cand_alias or cand_name, old_al),
            colliding_dids,
        )
    return None, []


def _format_peer_view(peer: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "deviceId": str(peer.get("deviceId") or ""),
        "alias": str(peer.get("alias") or ""),
        "name": str(peer.get("name") or ""),
        "os": str(peer.get("os") or ""),
        "host": str(peer.get("host") or ""),
        "verifyPublicKey": str(peer.get("verifyPublicKey") or ""),
        "boxPublicKey": str(peer.get("boxPublicKey") or ""),
        "keyFingerprint": str(peer.get("keyFingerprint") or ""),
        "pairFingerprint": str(peer.get("pairFingerprint") or ""),
        "paired": bool(peer.get("paired")),
        "status": str(peer.get("status") or ("paired" if peer.get("paired") else "discovered")),
        "pairedAt": str(peer.get("pairedAt") or ""),
        "protocolVersion": int(peer.get("protocolVersion") or 2),
        "capabilities": list(peer.get("capabilities") or ["v2"]),
        "pendingDescriptor": copy.deepcopy(peer.get("pendingDescriptor")),
    }


def _select_summary_peer(
    peers_list: List[Dict[str, Any]],
    selected_did: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], bool, str]:
    if selected_did:
        for p in peers_list:
            if p["deviceId"] == selected_did:
                is_paired = bool(p.get("paired") and p.get("status") == "paired")
                return copy.deepcopy(p), is_paired, str(p.get("pairFingerprint") or "")
    if len(peers_list) == 1:
        p = peers_list[0]
        is_paired = bool(p.get("paired") and p.get("status") == "paired")
        return copy.deepcopy(p), is_paired, str(p.get("pairFingerprint") or "")
    paired = [p for p in peers_list if p.get("paired") and p.get("status") == "paired"]
    if paired:
        return copy.deepcopy(paired[0]), True, str(paired[0].get("pairFingerprint") or "")
    if peers_list:
        return copy.deepcopy(peers_list[0]), False, str(peers_list[0].get("pairFingerprint") or "")
    return None, False, ""


def _reconcile_discovered_peers(
    cloud: Dict[str, Any],
    my_desc: Dict[str, Any],
    here_id: str,
    local_name: str,
    discovered: Dict[str, Dict[str, Any]],
    override_target_did: Optional[str] = None,
    override_alias: Optional[str] = None,
    replace_device: bool = False,
    allow_replace: bool = False,
    strict_conflict: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Reconcile remote descriptors with pinned `cloud['peers']`, detecting key rotation and alias collisions."""
    peers_map = cloud.setdefault("peers", {})
    shown_fps = cloud.setdefault("shownFingerprints", {})
    candidates_map: Dict[str, Dict[str, Any]] = {}

    # Update existing known peers first
    for did, existing in list(peers_map.items()):
        if not isinstance(existing, dict):
            continue
        rem = discovered.get(did)
        if rem is not None:
            pair_fp = crypto.pair_fingerprint(my_desc, rem)
            shown_fps[did] = pair_fp
            if _same_peer_keys(existing, rem):
                existing["name"] = rem.get("name") or existing.get("name") or ""
                existing["os"] = rem.get("os") or existing.get("os") or ""
                existing["host"] = rem.get("host") or existing.get("host") or ""
                existing["keyFingerprint"] = rem.get("keyFingerprint") or existing.get("keyFingerprint") or ""
                existing["protocolVersion"] = rem.get("protocolVersion", existing.get("protocolVersion", 2))
                existing["capabilities"] = list(rem.get("capabilities") or existing.get("capabilities") or ["v2"])
                existing["pairFingerprint"] = pair_fp
                existing["pendingDescriptor"] = None
                if existing.get("paired"):
                    existing["status"] = "paired"
                else:
                    existing["status"] = "discovered"
                cand_entry = copy.deepcopy(rem)
                if existing.get("alias"):
                    cand_entry["alias"] = existing["alias"]
                candidates_map[did] = cand_entry
            else:
                if existing.get("paired") or existing.get("status") in ("paired", "key_changed"):
                    existing["paired"] = False
                    existing["status"] = "key_changed"
                    existing["pendingDescriptor"] = copy.deepcopy(rem)
                    existing["pairFingerprint"] = pair_fp
                    cand_entry = copy.deepcopy(rem)
                    if existing.get("alias"):
                        cand_entry["alias"] = existing["alias"]
                    candidates_map[did] = cand_entry
                else:
                    existing.update(
                        {
                            "name": rem.get("name") or "",
                            "os": rem.get("os") or "",
                            "host": rem.get("host") or "",
                            "verifyPublicKey": rem["verifyPublicKey"],
                            "boxPublicKey": rem["boxPublicKey"],
                            "keyFingerprint": rem["keyFingerprint"],
                            "pairFingerprint": pair_fp,
                            "paired": False,
                            "status": "discovered",
                            "protocolVersion": rem.get("protocolVersion", 2),
                            "capabilities": list(rem.get("capabilities") or ["v2"]),
                            "pendingDescriptor": None,
                        }
                    )
                    cand_entry = copy.deepcopy(rem)
                    if existing.get("alias"):
                        cand_entry["alias"] = existing["alias"]
                    candidates_map[did] = cand_entry
        else:
            if my_desc:
                try:
                    fp = crypto.pair_fingerprint(my_desc, existing)
                    existing["pairFingerprint"] = fp
                    shown_fps[did] = fp
                except Exception:
                    pass
            candidates_map[did] = copy.deepcopy(existing.get("pendingDescriptor") or existing)

    # Process newly discovered peers
    conflict_views: List[Dict[str, Any]] = []
    for did, rem in sorted(discovered.items()):
        if did in peers_map:
            continue
        eff_alias = str(rem.get("alias") or "").strip()
        if override_alias and (override_target_did == did or (override_target_did is None and len(discovered) == 1)):
            eff_alias = override_alias

        conflict_msg, colliding_dids = _find_alias_or_name_collision(
            eff_alias,
            str(rem.get("name") or ""),
            did,
            here_id,
            local_name,
            peers_map,
        )
        pair_fp = crypto.pair_fingerprint(my_desc, rem)
        shown_fps[did] = pair_fp
        if conflict_msg:
            if replace_device and allow_replace and colliding_dids:
                cand_entry = copy.deepcopy(rem)
                if eff_alias:
                    cand_entry["alias"] = eff_alias
                candidates_map[did] = cand_entry
                continue
            # Colliding with an existing peer in cloud['peers'] is always a hard conflict unless resolved;
            # colliding only with local here_id (e.g. an abandoned previous identity of this same machine)
            # raises when targeted or when accepting, while allowing local discovery of other peers on accept=False.
            is_peer_collision = bool(colliding_dids)
            is_targeted = (override_target_did == did)
            if strict_conflict and (is_peer_collision or is_targeted) and (override_target_did is None or is_targeted):
                raise PeerConflictError(conflict_msg, code="alias_conflict")
            view = _format_peer_view(
                {
                    **rem,
                    "alias": eff_alias,
                    "pairFingerprint": pair_fp,
                    "paired": False,
                    "status": "conflict",
                    "pendingDescriptor": None,
                }
            )
            view["conflict"] = "alias_conflict"
            view["error"] = conflict_msg
            conflict_views.append(view)
            # Keep in candidates_map only if not a peer collision? No, keep conflict info so accept=True rejects it!
            if not is_peer_collision:
                cand_entry = copy.deepcopy(rem)
                cand_entry["_conflictError"] = conflict_msg
                if eff_alias:
                    cand_entry["alias"] = eff_alias
                candidates_map[did] = cand_entry
            continue

        peers_map[did] = {
            "deviceId": did,
            "alias": eff_alias,
            "name": str(rem.get("name") or ""),
            "os": str(rem.get("os") or ""),
            "host": str(rem.get("host") or ""),
            "verifyPublicKey": rem["verifyPublicKey"],
            "boxPublicKey": rem["boxPublicKey"],
            "keyFingerprint": rem["keyFingerprint"],
            "pairFingerprint": pair_fp,
            "paired": False,
            "status": "discovered",
            "pairedAt": "",
            "protocolVersion": int(rem.get("protocolVersion") or 2),
            "capabilities": list(rem.get("capabilities") or ["v2"]),
            "pendingDescriptor": None,
        }
        cand_entry = copy.deepcopy(rem)
        if eff_alias:
            cand_entry["alias"] = eff_alias
        candidates_map[did] = cand_entry

    peers_view = [_format_peer_view(p) for p in peers_map.values() if isinstance(p, dict)]
    peers_view.extend(conflict_views)
    peers_view.sort(key=lambda x: (x.get("alias") or "", x.get("deviceId") or ""))
    return peers_view, candidates_map


# ---------------------------------------------------------------- 1. cloud_status & list_devices


def cloud_status(
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Dict[str, Any]:
    """Return current cloud relay configuration, local identity, pairing state, and peers."""
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
    local_name = str(L.dev(led["config"], alias).get("name") or "") if alias else ""
    peers_view = [_format_peer_view(p) for p in (cloud.get("peers") or {}).values() if isinstance(p, dict)]
    peers_view.sort(key=lambda x: (x.get("alias") or "", x.get("deviceId") or ""))

    if configured and has_crypto and my_desc and alias and st_mod is not None:
        try:
            prov = st_mod.with_read_retry(st_mod.make_provider(cloud))
            discovered = _discover_remote_descriptors(prov, my_desc["deviceId"], alias)
            before_cloud = copy.deepcopy(cloud)
            peers_view, _ = _reconcile_discovered_peers(
                cloud,
                my_desc,
                alias,
                local_name,
                discovered,
                strict_conflict=False,
            )
            if cloud != before_cloud:
                _save_cloud(base, cloud)
        except Exception:
            pass

    shown_peer, paired, pair_fp = _select_summary_peer(peers_view)

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
        "protocolVersion": 3,
        "capabilities": list(_LOCAL_CAPABILITIES),
        "paired": paired,
        "pairFingerprint": pair_fp,
        "peer": shown_peer,
        "peers": peers_view,
        "remotes": remotes,
        "hasCrypto": has_crypto,
        "hasRclone": has_rclone,
    }


def list_devices(
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Dict[str, Any]:
    """List local device, configured ledger devices, and paired/discovered cloud peers."""
    base, led_path, local_cfg = _resolve_ctx(ledger_path, local, local_home)
    led = L.load(led_path)
    here_id = L.here(led, local_cfg) or ""
    cloud = _load_cloud(base)
    dev_id, key_fp, _ = _read_existing_identity_public(base)
    peers_map = cloud.get("peers") if isinstance(cloud.get("peers"), dict) else {}

    out_devices: List[Dict[str, Any]] = []
    seen_dids: Set[str] = set()
    for d in led["config"]["devices"]:
        if not isinstance(d, dict):
            continue
        d_id = str(d.get("id") or "")
        d_did = str(d.get("deviceId") or "")
        is_here = bool(here_id and d_id == here_id)
        if is_here and not d_did and dev_id:
            d_did = dev_id
        peer = peers_map.get(d_did) if d_did else None
        if peer is None and not is_here and not d_did:
            for p in peers_map.values():
                if isinstance(p, dict) and str(p.get("alias") or "") == d_id:
                    peer = p
                    break
        if d_did:
            seen_dids.add(d_did)
        if is_here:
            status = "self"
            paired = True
            caps = list(_LOCAL_CAPABILITIES)
            proto_ver = 3
            kfp = key_fp
            pfp = ""
            paired_at = ""
        elif isinstance(peer, dict):
            status = str(peer.get("status") or ("paired" if peer.get("paired") else "discovered"))
            paired = bool(peer.get("paired") and status == "paired")
            caps = list(peer.get("capabilities") or ["v2"])
            proto_ver = int(peer.get("protocolVersion") or 2)
            kfp = str(peer.get("keyFingerprint") or "")
            pfp = str(peer.get("pairFingerprint") or "")
            paired_at = str(peer.get("pairedAt") or "")
        else:
            status = "unpaired"
            paired = False
            caps = []
            proto_ver = 0
            kfp = ""
            pfp = ""
            paired_at = ""

        out_devices.append(
            {
                "id": d_id,
                "alias": d_id,
                "name": str(d.get("name") or (peer.get("name") if isinstance(peer, dict) else "") or ""),
                "os": str(d.get("os") or (peer.get("os") if isinstance(peer, dict) else "") or ""),
                "host": str(d.get("host") or (peer.get("host") if isinstance(peer, dict) else "") or ""),
                "deviceId": d_did or (str(peer.get("deviceId") or "") if isinstance(peer, dict) else ""),
                "isHere": is_here,
                "paired": paired,
                "status": status,
                "keyFingerprint": kfp,
                "pairFingerprint": pfp,
                "protocolVersion": proto_ver,
                "capabilities": caps,
                "pairedAt": paired_at,
                "needsPairing": bool(d.get("needsPairing")),
            }
        )

    for did, p in sorted(peers_map.items()):
        if not isinstance(p, dict) or did in seen_dids:
            continue
        out_devices.append(
            {
                "id": str(p.get("alias") or ""),
                "alias": str(p.get("alias") or ""),
                "name": str(p.get("name") or ""),
                "os": str(p.get("os") or ""),
                "host": str(p.get("host") or ""),
                "deviceId": did,
                "isHere": False,
                "paired": bool(p.get("paired") and p.get("status") == "paired"),
                "status": str(p.get("status") or "discovered"),
                "keyFingerprint": str(p.get("keyFingerprint") or ""),
                "pairFingerprint": str(p.get("pairFingerprint") or ""),
                "protocolVersion": int(p.get("protocolVersion") or 2),
                "capabilities": list(p.get("capabilities") or ["v2"]),
                "pairedAt": str(p.get("pairedAt") or ""),
                "needsPairing": False,
            }
        )

    return {
        "here": here_id,
        "deviceId": dev_id,
        "keyFingerprint": key_fp,
        "capabilities": list(_LOCAL_CAPABILITIES),
        "devices": out_devices,
    }


# ---------------------------------------------------------------- 2. pair_device


def _match_peer_candidates(
    candidates: List[Dict[str, Any]],
    query: str,
) -> List[Dict[str, Any]]:
    q = query.strip()
    ql = q.lower()
    if not q:
        return []
    by_did = [c for c in candidates if str(c.get("deviceId") or "") == q]
    if by_did:
        return by_did
    by_al_exact = [c for c in candidates if str(c.get("alias") or "") == q]
    if by_al_exact:
        return by_al_exact
    by_al_ci = [c for c in candidates if str(c.get("alias") or "").lower() == ql]
    if by_al_ci:
        return by_al_ci
    return [c for c in candidates if str(c.get("name") or "").strip().lower() == ql]


def pair_device(
    remote: Optional[str] = None,
    root: Optional[str] = None,
    provider_type: Optional[str] = "rclone",
    rclone_config: Optional[str] = None,
    accept: bool = False,
    with_device: Optional[str] = None,
    fingerprint: Optional[str] = None,
    alias: Optional[str] = None,
    replace_device: bool = False,
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Dict[str, Any]:
    """Publish local signed public key descriptor, discover peers, and optionally verify/pin pairing."""
    crypto.require_crypto()
    st_mod = _storage_mod()
    base, led_path, local_cfg = _resolve_ctx(ledger_path, local, local_home)
    led = L.load(led_path)
    here_id = L.here(led, local_cfg)
    if not here_id:
        raise RelayError(NEED_DEVICE_MSG, code="need_device")

    cleaned_override_alias: Optional[str] = None
    if alias is not None and str(alias).strip():
        cleaned_override_alias = devices.validate_alias(str(alias))

    my_dev_cfg = L.dev(led["config"], here_id)
    local_name = str(my_dev_cfg.get("name") or "")

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
    if conn_changed:
        cloud["peers"] = {}
        cloud["shownFingerprints"] = {}

    if not _is_cloud_configured(cloud):
        raise NotConfiguredError(
            "尚未配置云存储连接。请指定 --remote <连接名> --root <共享目录>（或本地目录模式 --provider local --root <目录>）。"
        )
    if conn_changed:
        _update_relay_state(
            base,
            lambda st: (
                st.get("pendingSends", {}).clear(),
                st.get("activeHandoffs", {}).clear(),
            ),
        )

    ident_exists = (base / "identity.json").exists()
    os_name = _infer_device_os(led, here_id, ident_exists)
    identity = crypto.ensure_identity(
        base,
        alias=here_id,
        name=local_name,
        os_name=os_name,
        host=L.hostname(),
    )
    my_desc_v3 = crypto.public_descriptor_v3(identity, capabilities=_LOCAL_CAPABILITIES)
    signed_my_desc_v3 = crypto.sign_json(
        my_desc_v3,
        identity["signPrivateKey"],
        signer_device_id=identity["deviceId"],
    )

    provider = st_mod.with_read_retry(st_mod.make_provider(cloud))
    v3_dev_key = "v3/devices/%s.json" % identity["deviceId"]
    with tempfile.TemporaryDirectory() as tmpdir:
        p_me_v3 = os.path.join(tmpdir, "me_v3.json")
        _atomic_write_json(Path(p_me_v3), signed_my_desc_v3)
        provider.upload(p_me_v3, v3_dev_key)
        if provider.stat(v3_dev_key) is None:
            raise st_mod.StorageError("发布本机 v3 公钥文件失败，远端未找到：%s" % v3_dev_key)

        if here_id in ("a", "b"):
            my_desc_v2 = crypto.public_descriptor(identity)
            signed_my_desc_v2 = crypto.sign_json(
                my_desc_v2,
                identity["signPrivateKey"],
                signer_device_id=identity["deviceId"],
            )
            legacy_key = "devices/%s.json" % here_id
            p_me_v2 = os.path.join(tmpdir, "me_v2.json")
            _atomic_write_json(Path(p_me_v2), signed_my_desc_v2)
            provider.upload(p_me_v2, legacy_key)
            if provider.stat(legacy_key) is None:
                raise st_mod.StorageError("发布本机公钥文件失败，远端未找到：%s" % legacy_key)

    discovered = _discover_remote_descriptors(provider, identity["deviceId"], here_id)

    override_target_did: Optional[str] = None
    if with_device is not None and str(with_device).strip():
        q = str(with_device).strip()
        all_raw_cands: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        for did, desc in discovered.items():
            all_raw_cands.append(desc)
            seen_ids.add(did)
        for did, p in (cloud.get("peers") or {}).items():
            if isinstance(p, dict) and did not in seen_ids:
                all_raw_cands.append(p)
        matched = _match_peer_candidates(all_raw_cands, q)
        if len(matched) == 1:
            override_target_did = str(matched[0]["deviceId"])

    prev_shown_fps = copy.deepcopy(cloud.get("shownFingerprints") or {})
    peers_view, candidates_map = _reconcile_discovered_peers(
        cloud,
        my_desc_v3,
        here_id,
        local_name,
        discovered,
        override_target_did=override_target_did,
        override_alias=cleaned_override_alias,
        replace_device=replace_device,
        allow_replace=bool(replace_device and accept),
        strict_conflict=True,
    )

    selected_did: Optional[str] = None
    rotated_from = ""
    if with_device is not None and str(with_device).strip():
        q = str(with_device).strip()
        cand_list = list(candidates_map.values())
        matches = _match_peer_candidates(cand_list, q)
        if len(matches) == 0:
            raise RelayError("尚未在云端发现设备「%s」的公钥描述。请先在该设备上运行 relay pair。" % q, code="peer_not_found")
        if len(matches) > 1:
            cands_info = [
                {
                    "alias": str(m.get("alias") or ""),
                    "deviceId": str(m.get("deviceId") or ""),
                    "name": str(m.get("name") or ""),
                    "os": str(m.get("os") or ""),
                }
                for m in matches
            ]
            raise AmbiguousPeerError(
                "「%s」匹配到多台远端设备，请改用准确的 deviceId。" % q,
                candidates=cands_info,
            )
        selected_did = str(matches[0]["deviceId"])

    if accept:
        if selected_did is None:
            if len(candidates_map) == 0:
                raise RelayError(
                    "尚未在云端发现任何其他设备的公钥文件。"
                    "请先在另一台设备上运行 relay pair 发布公钥，核对指纹一致后再运行 relay pair --accept。",
                    code="peer_not_found",
                )
            if len(candidates_map) > 1 and replace_device:
                new_disc_dids = [
                    did
                    for did in discovered
                    if did in candidates_map and did not in (cloud.get("peers") or {})
                ]
                if fingerprint and str(fingerprint).strip():
                    fp_norm = _norm_fp(fingerprint)
                    fp_matches = [
                        did
                        for did in new_disc_dids
                        if _norm_fp(crypto.pair_fingerprint(my_desc_v3, candidates_map[did])) == fp_norm
                    ]
                    if len(fp_matches) == 1:
                        selected_did = fp_matches[0]
                elif len(new_disc_dids) == 1:
                    selected_did = new_disc_dids[0]
            if selected_did is None and len(candidates_map) > 1:
                cands_info = [
                    {
                        "alias": str((cloud.get("peers") or {}).get(did, {}).get("alias") or d.get("alias") or ""),
                        "deviceId": did,
                        "name": str(d.get("name") or ""),
                        "os": str(d.get("os") or ""),
                    }
                    for did, d in sorted(candidates_map.items())
                ]
                labels = ", ".join(c["alias"] or c["deviceId"] for c in cands_info)
                raise AmbiguousPeerError(
                    "云端存在多台远端设备（%s），请使用 --with <设备别名或deviceId> 指定要确认配对的设备。" % labels,
                    candidates=cands_info,
                )
            if selected_did is None:
                selected_did = next(iter(candidates_map.keys()))

        target_desc = candidates_map[selected_did]
        if target_desc.get("_conflictError"):
            raise PeerConflictError(str(target_desc["_conflictError"]), code="alias_conflict")
        live_fp = crypto.pair_fingerprint(my_desc_v3, target_desc)

        if fingerprint is not None and str(fingerprint).strip():
            if _norm_fp(fingerprint) != _norm_fp(live_fp):
                raise RelayError(
                    "配对指纹不匹配：期望 %s，输入 %s。请重新核对两台设备的配对指纹。" % (live_fp, fingerprint.strip()),
                    code="fingerprint_mismatch",
                )
        else:
            if replace_device:
                raise RelayError(
                    "替换同别名冲突设备时必须使用 --fingerprint 显式传入核对后的配对指纹。",
                    code="fingerprint_required",
                )
            shown_fp = prev_shown_fps.get(selected_did, "")
            if shown_fp != live_fp:
                raise RelayError(
                    "请先查看并核对配对指纹后再运行 --accept，或使用 --fingerprint 指定指纹。",
                    code="fingerprint_not_shown",
                )

        existing_rec = (cloud.get("peers") or {}).get(selected_did) or {}
        eff_alias = cleaned_override_alias or str(existing_rec.get("alias") or target_desc.get("alias") or "").strip()
        if not eff_alias:
            eff_alias = "dev_" + selected_did[-6:]
        eff_alias = devices.validate_alias(eff_alias)

        if replace_device:
            _, colliding_dids = _find_alias_or_name_collision(
                eff_alias,
                str(target_desc.get("name") or ""),
                selected_did,
                here_id,
                local_name,
                cloud.get("peers") or {},
            )
            for old_did in colliding_dids:
                cloud["peers"].pop(old_did, None)
                (cloud.get("shownFingerprints") or {}).pop(old_did, None)
        else:
            conflict_msg, _ = _find_alias_or_name_collision(
                eff_alias,
                str(target_desc.get("name") or ""),
                selected_did,
                here_id,
                local_name,
                cloud.get("peers") or {},
            )
            if conflict_msg:
                raise PeerConflictError(conflict_msg, code="alias_conflict")

        target_caps = list(target_desc.get("capabilities") or ["v2"])
        target_proto = int(target_desc.get("protocolVersion") or (3 if "v3" in target_caps else 2))
        # Phase C: keep the previously confirmed public keys of this same device.
        # They let this device recognise evidence signed before the rotation
        # (older snapshots, receipts bound to that key); they never authorise
        # anything new. Keys only ever seen as pendingDescriptor are not kept.
        key_history = _valid_key_entries(existing_rec.get("keyHistory"))
        old_confirmed = bool(existing_rec.get("pairedAt")) or existing_rec.get("status") in ("paired", "key_changed")
        rotated_from = ""
        if (old_confirmed and existing_rec.get("verifyPublicKey") and existing_rec.get("boxPublicKey")
                and (existing_rec.get("verifyPublicKey"), existing_rec.get("boxPublicKey"))
                != (target_desc.get("verifyPublicKey"), target_desc.get("boxPublicKey"))):
            try:
                old_fp = str(existing_rec.get("keyFingerprint") or "") or crypto.device_fingerprint(
                    existing_rec["verifyPublicKey"], existing_rec["boxPublicKey"])
            except Exception:
                old_fp = ""
            if old_fp:
                rotated_from = old_fp
                key_history.append({
                    "keyFingerprint": old_fp,
                    "verifyPublicKey": existing_rec["verifyPublicKey"],
                    "boxPublicKey": existing_rec["boxPublicKey"],
                    "pairedAt": str(existing_rec.get("pairedAt") or ""),
                    "retiredAt": L.now_iso(),
                    "reason": "rotated",
                })
        cloud.setdefault("peers", {})[selected_did] = {
            "deviceId": selected_did,
            "alias": eff_alias,
            "name": str(target_desc.get("name") or ""),
            "os": str(target_desc.get("os") or ""),
            "host": str(target_desc.get("host") or ""),
            "verifyPublicKey": target_desc["verifyPublicKey"],
            "boxPublicKey": target_desc["boxPublicKey"],
            "keyFingerprint": target_desc.get("keyFingerprint") or crypto.device_fingerprint(
                target_desc["verifyPublicKey"], target_desc["boxPublicKey"]
            ),
            "pairFingerprint": live_fp,
            "paired": True,
            "status": "paired",
            "pairedAt": L.now_iso(),
            "protocolVersion": target_proto,
            "capabilities": target_caps,
            "pendingDescriptor": None,
        }
        if key_history:
            cloud["peers"][selected_did]["keyHistory"] = key_history[-MAX_KEY_HISTORY:]
        cloud.setdefault("shownFingerprints", {})[selected_did] = live_fp

    _save_cloud(base, cloud)

    peers_view = [_format_peer_view(p) for p in (cloud.get("peers") or {}).values() if isinstance(p, dict)]
    peers_view.sort(key=lambda x: (x.get("alias") or "", x.get("deviceId") or ""))

    def _upd_devices(l: Dict[str, Any]) -> None:
        d_me = L.dev(l["config"], here_id)
        if my_desc_v3.get("os"):
            d_me["os"] = my_desc_v3["os"]
        if my_desc_v3.get("name"):
            d_me["name"] = my_desc_v3["name"]
        d_me["deviceId"] = my_desc_v3["deviceId"]
        if my_desc_v3.get("host") and not d_me.get("host"):
            d_me["host"] = my_desc_v3["host"]

        devs_list = l["config"]["devices"]
        for p in (cloud.get("peers") or {}).values():
            if not isinstance(p, dict):
                continue
            p_did = str(p.get("deviceId") or "")
            p_al = str(p.get("alias") or "")
            if not p_did or not p_al:
                continue
            if not p.get("paired") and p_did != selected_did:
                # Only auto-register unpaired peer into ledger if alias slot already exists without deviceId
                slot = next((d for d in devs_list if d.get("id") == p_al and not d.get("deviceId")), None)
                if slot is not None:
                    if p.get("os"):
                        slot["os"] = p["os"]
                    if p.get("name"):
                        slot["name"] = p["name"]
                    slot["deviceId"] = p_did
                continue

            match_by_did = next((d for d in devs_list if d.get("deviceId") == p_did), None)
            if match_by_did is not None:
                if p.get("os"):
                    match_by_did["os"] = p["os"]
                if p.get("name"):
                    match_by_did["name"] = p["name"]
                if p.get("host") and not match_by_did.get("host"):
                    match_by_did["host"] = p["host"]
                continue

            match_by_alias = next((d for d in devs_list if str(d.get("id") or "").lower() == p_al.lower()), None)
            if match_by_alias is not None:
                if replace_device or not match_by_alias.get("deviceId"):
                    match_by_alias["deviceId"] = p_did
                    if p.get("os"):
                        match_by_alias["os"] = p["os"]
                    if p.get("name"):
                        match_by_alias["name"] = p["name"]
                    if p.get("host"):
                        match_by_alias["host"] = p["host"]
            else:
                devs_list.append(
                    {
                        "id": p_al,
                        "name": p.get("name") or ("设备 %s" % p_al),
                        "os": p.get("os") or "linux",
                        "drop": "",
                        "host": p.get("host") or "",
                        "deviceId": p_did,
                    }
                )

        migrated = devices.migrate_ledger_identities(l, local_ident=identity, cloud_cfg=cloud)
        l.clear()
        l.update(migrated)

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

    shown_peer, is_paired, shown_pfp = _select_summary_peer(peers_view, selected_did=selected_did)

    return {
        "configured": True,
        "cloud": {
            "provider": cloud.get("provider") or "rclone",
            "remote": cloud.get("remote") or "",
            "root": cloud.get("root") or "",
            "configPath": cloud.get("configPath") or "",
        },
        "deviceId": my_desc_v3["deviceId"],
        "alias": here_id,
        "keyFingerprint": my_desc_v3["keyFingerprint"],
        "protocolVersion": 3,
        "capabilities": list(_LOCAL_CAPABILITIES),
        "paired": is_paired,
        "pairFingerprint": shown_pfp,
        "peer": shown_peer,
        "peers": peers_view,
        "remotes": remotes,
        "hasCrypto": True,
        "hasRclone": has_rclone,
        "keyRotationConfirmed": (
            {"deviceId": selected_did, "previousKeyFingerprint": rotated_from,
             "keyFingerprint": str(((cloud.get("peers") or {}).get(selected_did) or {}).get("keyFingerprint") or "")}
            if accept and rotated_from else None
        ),
    }


def _require_paired_context(
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
    require_any_peer: bool = True,
):
    crypto.require_crypto()
    st_mod = _storage_mod()
    base, led_path, local_cfg = _resolve_ctx(ledger_path, local, local_home)
    cloud = _load_cloud(base)
    if not _is_cloud_configured(cloud):
        raise NotConfiguredError("尚未配置云存储连接。请先运行 relay pair 配置网盘与共享目录。")
    active_peers = _paired_active_peers(cloud)
    if require_any_peer and not active_peers:
        kc = _key_changed_peers(cloud)
        if kc:
            p = kc[0]
            raise KeyChangedError(
                "对端设备「%s」（%s）密钥已变更，请重新核对指纹后运行 relay pair --with %s --accept。"
                % (p.get("alias") or p.get("deviceId"), p.get("deviceId"), p.get("deviceId")),
                peer_id=str(p.get("deviceId") or ""),
                old_fingerprint=str(p.get("keyFingerprint") or ""),
                new_fingerprint=str((p.get("pendingDescriptor") or {}).get("keyFingerprint") or ""),
            )
        raise NotConfiguredError("尚未完成设备配对确认。请核对配对指纹一致后运行 relay pair --accept。")
    led = L.load(led_path)
    here_id = L.here(led, local_cfg)
    if not here_id:
        raise RelayError(NEED_DEVICE_MSG, code="need_device")
    identity = crypto.ensure_identity(base, alias=here_id)
    # Fix H: commands retry idempotent reads (bounded); SyncSession unwraps this (round backoff instead).
    provider = st_mod.with_read_retry(st_mod.make_provider(cloud))
    return base, led_path, local_cfg, led, here_id, cloud, identity, provider


def _download_json(provider, key: str) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "object.json"
        provider.download(key, str(path))
        with path.open(encoding="utf-8-sig") as stream:
            return json.load(stream)


# ---------------------------------------------------------------- version lineage DAG


def _recompute_graph_heads(graph: Dict[str, Any]) -> List[str]:
    nodes = graph.get("nodes") or {}
    parent_vids: Set[str] = set()
    relay_to_vid: Dict[str, str] = {}
    for vid, node in nodes.items():
        if isinstance(node, dict) and node.get("relayId"):
            relay_to_vid[str(node["relayId"])] = vid

    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        for pv in node.get("parentVersionIds") or []:
            if pv:
                parent_vids.add(str(pv))
        pr = str(node.get("parentRelayId") or "")
        if pr and pr in relay_to_vid:
            parent_vids.add(relay_to_vid[pr])

    heads = [vid for vid in nodes.keys() if vid not in parent_vids]
    heads.sort(key=lambda vid: (str((nodes.get(vid) or {}).get("createdAt") or ""), vid))
    graph["heads"] = heads
    return heads


def _record_version(st_obj: Dict[str, Any], proj_id: str, ver_node: Dict[str, Any]) -> None:
    if not proj_id or not isinstance(ver_node, dict) or not ver_node.get("versionId"):
        return
    vg = st_obj.setdefault("versionGraph", {})
    graph = vg.setdefault(proj_id, {"nodes": {}, "heads": []})
    nodes = graph.setdefault("nodes", {})
    vid = str(ver_node["versionId"])
    existing = nodes.get(vid)
    if isinstance(existing, dict):
        merged = copy.deepcopy(existing)
        for k, v in ver_node.items():
            if v and not merged.get(k):
                merged[k] = copy.deepcopy(v)
        nodes[vid] = merged
    else:
        nodes[vid] = copy.deepcopy(ver_node)
    _recompute_graph_heads(graph)


def _is_version_ancestor(graph: Dict[str, Any], anc_id: str, desc_id: str) -> bool:
    if not anc_id or not desc_id:
        return False
    if anc_id == desc_id:
        return True
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if not isinstance(nodes, dict):
        return False

    relay_to_vid: Dict[str, str] = {}
    for vid, node in nodes.items():
        if isinstance(node, dict) and node.get("relayId"):
            relay_to_vid[str(node["relayId"])] = vid

    stack: List[str] = [desc_id]
    visited: Set[str] = set()
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        node = nodes.get(cur)
        if not isinstance(node, dict):
            continue
        parents = [str(p) for p in (node.get("parentVersionIds") or []) if p]
        p_rel = str(node.get("parentRelayId") or "")
        if p_rel and p_rel in relay_to_vid:
            parents.append(relay_to_vid[p_rel])
        for p in parents:
            if p == anc_id:
                return True
            if p not in visited:
                stack.append(p)
    return False


def _record_conflict_in_ledger(
    led_path: Path,
    proj_id: str,
    here_id: str,
    local_ver: str,
    incoming_ver: str,
    author_device_id: str = "",
) -> None:
    pair_key = "|".join(sorted([str(local_ver), str(incoming_ver)]))

    def _upd(l: Dict[str, Any]) -> None:
        for p in l.get("projects") or []:
            if p.get("id") != proj_id:
                continue
            conflicts = list(p.get("conflicts") or [])
            if any(
                isinstance(c, dict) and c.get("pairKey") == pair_key
                for c in conflicts
            ):
                return
            conflicts.append(
                {
                    "id": L.new_id("cf_"),
                    "kind": "version_fork",
                    "pairKey": pair_key,
                    "localVersion": local_ver,
                    "incomingVersion": incoming_ver,
                    "device": here_id,
                    "authorDeviceId": author_device_id,
                    "detectedAt": L.now_iso(),
                    "resolved": False,
                }
            )
            p["conflicts"] = conflicts[-20:]

    L.update(led_path, _upd)


# ---------------------------------------------------------------- snapshot cache & have-manifest

from . import sync_runtime as SR  # noqa: E402  (Phase B; stdlib-only, never imports this module)


def _snapshot_record_key(version_id: str) -> str:
    _check_remote(V.validate_version_id, version_id)
    return "v3/snapshots/%s.json" % version_id


def _put_immutable_json(provider, key: str, document: Dict[str, Any]) -> None:
    """Write-once JSON: absent -> upload; present -> must equal ``document`` (else TamperError, never rewritten).

    The upload itself is never repeated within one call (Fix H keeps writes
    single-shot; only the reads are retried by a ``RetryingProvider``). A run
    whose upload answer was lost fails; the next run reads the key first, so it
    confirms an identical record instead of writing again, and a conflicting one
    stops as integrity.
    """
    try:
        existing = _download_json(provider, key)
    except _storage_mod().ObjectNotFoundError:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "record.json"
            _atomic_write_json(local, document)
            provider.upload(str(local), key)
        existing = _download_json(provider, key)
    if existing != document:
        raise crypto.TamperError("云端已发布记录存在冲突，拒绝改写：%s" % key)


# ---- Fix H: a snapshot's signed record is published before the snapshot becomes a local head

REPAIR_RECORDS_LIMIT = 1000  # own unconfirmed ancestors walked per operation (local walk + one listing)


def _own_unconfirmed_ancestry(state: Dict[str, Any], project_id: str, my_did: str, start: List[str]) -> List[str]:
    """Own-authored versions reachable from ``start`` whose cloud record was never confirmed (``recordPublished``)."""
    nodes = ((state.get("versionGraph") or {}).get(project_id) or {}).get("nodes") or {}
    out: List[str] = []
    seen: Set[str] = set()
    stack = [str(v) for v in start if v]
    while stack and len(out) < REPAIR_RECORDS_LIMIT:
        vid = stack.pop()
        if vid in seen:
            continue
        seen.add(vid)
        node = nodes.get(vid)
        if not isinstance(node, dict) or node.get("recordPublished") or node.get("authorDeviceId") != my_did:
            continue
        out.append(vid)
        parents = node.get("parentVersionIds")
        if isinstance(parents, list):
            stack.extend(str(p) for p in parents if isinstance(p, str) and p)
    return out


def _own_cached_record(base: Path, identity: Dict[str, Any], project_id: str, vid: str,
                       node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """This device's signed record of ``vid`` from the local caches, if it is exactly the one the graph attests."""
    for where in ("snapshots", "lineage"):
        try:
            V.validate_version_id(vid)
            if where == "snapshots":
                path = base / "cache" / "snapshots" / vid / "snapshot.json"
            else:
                path = base / "cache" / "lineage" / (vid + ".json")
            V.ensure_contained(base, path, check_final=True, field="cache", local=True)
            if not path.is_file():
                continue
            with path.open(encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, ValueError, V.ValidationError):
            continue
        if not isinstance(raw, dict) or raw.get("versionId") != vid or raw.get("projectId") != project_id \
                or raw.get("authorDeviceId") != identity["deviceId"]:
            continue
        if node.get("recordSha256"):
            if crypto.snapshot_record_digest(raw) == node["recordSha256"]:
                return raw
        elif _verify_with_keys(raw, _own_keys(identity), identity["deviceId"]) is not None:
            return raw
    return None


def _republish_own_records(provider, base: Path, identity: Dict[str, Any], project_id: str,
                           start: List[str]) -> List[str]:
    """Make sure the cloud holds the signed record of every own ancestor of ``start``; returns those re-published.

    A publish can fail after its snapshot became the local head (the record
    upload failed, or its answer was lost). A later snapshot naming that parent
    left receivers with "insufficient evidence" for good, because only the
    author has the signed record (in its cache). It is written again here
    (write-once, never rewritten) before anything chains onto it. Versions found
    or written are marked ``recordPublished``; confirmed versions cost nothing,
    unconfirmed ones (a failed publish, or state of an older engine) one listing.
    """
    state = _load_relay_state(base)
    todo = _own_unconfirmed_ancestry(state, project_id, identity["deviceId"], start)
    if not todo:
        return []
    listed = {str(obj.key) for obj in provider.list_objects("v3/snapshots/")}
    nodes = ((state.get("versionGraph") or {}).get(project_id) or {}).get("nodes") or {}
    confirmed: List[str] = []
    repaired: List[str] = []
    for vid in todo:
        key = _snapshot_record_key(vid)
        if key in listed:
            confirmed.append(vid)
            continue
        raw = _own_cached_record(base, identity, project_id, vid, nodes.get(vid) or {})
        if raw is None:
            continue  # no verifiable local copy: receivers keep reporting the missing parent
        _put_immutable_json(provider, key, raw)
        confirmed.append(vid)
        repaired.append(vid)

    _mark_records_published(base, project_id, confirmed)
    return repaired


def _mark_records_published(base: Path, project_id: str, vids: List[str]) -> None:
    """Record that the cloud holds these versions' signed records (written or found there)."""
    if not vids:
        return

    def mark(st: Dict[str, Any]) -> None:
        graph_nodes = ((st.get("versionGraph") or {}).get(project_id) or {}).get("nodes") or {}
        for vid in vids:
            if isinstance(graph_nodes.get(vid), dict):
                graph_nodes[vid]["recordPublished"] = True

    _update_relay_state(base, mark)


def _read_lineage_raw(provider, base: Path, vid: str, ready_box: Dict[str, Any]) -> Any:
    """Signed metadata of one ancestor: ``cache/lineage``, the snapshot cache, the cloud index or a ready marker.

    Older v3 publications did not have the metadata index; for those the
    author's signed snapshot is recovered from existing ready markers (listed
    once per ``ready_box``). No copy anywhere raises ``lineage_incomplete``.
    """
    cached = base / "cache" / "lineage" / (vid + ".json")
    snapshot_file = _snapshot_cache_dir(base, vid) / "snapshot.json"
    if cached.is_file() or snapshot_file.is_file():
        with (cached if cached.is_file() else snapshot_file).open(encoding="utf-8") as stream:
            return json.load(stream)
    try:
        return _download_json(provider, _snapshot_record_key(vid))
    except _storage_mod().ObjectNotFoundError:
        pass
    if ready_box.get("keys") is None:
        ready_box["keys"] = [obj.key for obj in provider.list_objects("v3/inbox/") if obj.key.endswith("/ready.json")]
    # Sync delivery names include their snapshot ID. Search
    # those before legacy handoff IDs that contain no version.
    ordered = sorted(ready_box["keys"], key=lambda key: vid not in key)
    for key in ordered:
        try:
            candidate = _download_json(provider, key).get("snapshot")
        except _storage_mod().ObjectNotFoundError:
            continue
        if isinstance(candidate, dict) and candidate.get("versionId") == vid:
            return candidate
    raise RelayError("缺少可验证的父版本 %s，请恢复云端记录后重试；尚不能判定为分叉。" % vid,
                     code="lineage_incomplete")


def _verified_lineage_node(raw: Any, identity: Dict[str, Any], cloud: Dict[str, Any], version_graph: Any,
                           project_id: str, vid: str) -> Dict[str, Any]:
    """Graph node for one ancestor's signed metadata, verified with its author's keys (``use="lineage"``)."""
    # Phase C: ancestors signed before a (re-confirmed) key rotation verify
    # with the retired key as historical evidence; untrusted authors still stop.
    verified, proof = _verify_author_snapshot(raw, identity, cloud, version_graph, use="lineage")
    if verified["versionId"] != vid or verified["projectId"] != project_id:
        raise crypto.TamperError("父版本记录的项目或版本编号不匹配。")
    node = {key: copy.deepcopy(verified.get(key)) for key in (
        "versionId", "projectId", "authorDeviceId", "parentVersionIds",
        "parentRelayId", "relayId", "contentSha256", "createdAt")}
    node.update(_proof_fields(proof))
    return node


def _store_lineage_raw(base: Path, vid: str, raw: Any) -> None:
    cached = base / "cache" / "lineage" / (vid + ".json")
    V.ensure_contained(base, cached, field="cache", local=True)
    _atomic_write_json(cached, raw)


def _complete_version_ancestry(provider, base, identity, cloud, project_id, temp_state, version_id, local_version):
    """Read signed ancestor metadata, without downloading/applying workspaces.

    Older v3 publications did not have the metadata index. For those records,
    recover the author's signed snapshot from existing ready markers. Missing
    evidence is an incomplete lineage, not proof of a concurrent fork.
    """
    graph = temp_state["versionGraph"][project_id]
    nodes = graph["nodes"]
    loaded = {}
    ready_box: Dict[str, Any] = {}
    visiting, visited = set(), set()
    stack = [(version_id, False), (local_version, False)]
    while stack:
        vid, leaving = stack.pop()
        if not vid:
            continue
        if leaving:
            visiting.discard(vid)
            visited.add(vid)
            continue
        if vid in visited:
            continue
        if vid in visiting:
            raise crypto.TamperError("快照父版本链存在循环。")
        _snapshot_record_key(vid)
        node = nodes.get(vid)
        if not isinstance(node, dict):
            raw = _read_lineage_raw(provider, base, vid, ready_box)
            node = _verified_lineage_node(raw, identity, cloud, temp_state.get("versionGraph"), project_id, vid)
            nodes[vid] = node
            loaded[vid] = node
            _store_lineage_raw(base, vid, raw)
        parents = node.get("parentVersionIds") or []
        if not isinstance(parents, list) or any(not isinstance(parent, str) for parent in parents):
            raise crypto.TamperError("快照父版本字段格式无效。")
        visiting.add(vid)
        stack.append((vid, True))
        stack.extend((parent, False) for parent in parents)
    _recompute_graph_heads(graph)
    if loaded:
        def save_ancestors(state):
            target = state.setdefault("versionGraph", {}).setdefault(project_id, {"nodes": {}, "heads": []})
            target.setdefault("nodes", {}).update(copy.deepcopy(loaded))
            _recompute_graph_heads(target)
        _update_relay_state(base, save_ancestors)


# ---- Fix G: sync keeps "insufficient evidence" apart from an actual fork (plan section 2)

LINEAGE_RETRY_SEC = 600.0   # re-read missing ancestor evidence at most this often per pending pair


def _lineage_parents(nodes: Dict[str, Any], relay_to_vid: Dict[str, str], vid: str) -> Optional[List[str]]:
    node = nodes.get(vid)
    if not isinstance(node, dict):
        return None
    raw_parents = node.get("parentVersionIds")
    parents = [str(p) for p in raw_parents if p] if isinstance(raw_parents, list) else []
    p_rel = str(node.get("parentRelayId") or "")
    if p_rel and p_rel in relay_to_vid:
        parents.append(relay_to_vid[p_rel])
    return parents


def _lineage_reach(nodes: Dict[str, Any], relay_to_vid: Dict[str, str], start: str) -> Tuple[Set[str], Set[str]]:
    """Every version ID reachable from ``start`` through known nodes, and the reached IDs with no node."""
    seen: Set[str] = set()
    missing: Set[str] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        parents = _lineage_parents(nodes, relay_to_vid, cur)
        if parents is None:
            missing.add(cur)
            continue
        stack.extend(p for p in parents if p not in seen)
    return seen, missing


def _fork_candidates(graph: Dict[str, Any], v_local: str, prev_heads: List[str], snap_id: str) -> List[Tuple[str, str]]:
    """Pairs (local current version or previous head, incoming) the local graph cannot put on one chain."""
    pairs: List[Tuple[str, str]] = []
    if v_local and v_local != snap_id:
        if not _is_version_ancestor(graph, v_local, snap_id) and not _is_version_ancestor(graph, snap_id, v_local):
            pairs.append((v_local, snap_id))
    for h in prev_heads:
        if h and h != snap_id and h != v_local:
            if not _is_version_ancestor(graph, h, snap_id) and not _is_version_ancestor(graph, snap_id, h):
                pairs.append((h, snap_id))
    return pairs


class _LineageNeeded(Exception):
    """Raised inside a relay-state update to judge pairs outside the lock (nothing was written)."""

    def __init__(self, graph: Dict[str, Any], pairs: List[Tuple[str, str]]) -> None:
        Exception.__init__(self, "lineage evidence needed")
        self.graph = graph
        self.pairs = pairs


def _lineage_pair_key(a: str, b: str) -> str:
    return "|".join(sorted([str(a), str(b)]))


def _lineage_verdict(provider, base: Path, identity: Dict[str, Any], cloud: Dict[str, Any], project_id: str,
                     graph: Dict[str, Any], a: str, b: str, ready_box: Optional[Dict[str, Any]] = None,
                     failed: Optional[Dict[str, Dict[str, str]]] = None
                     ) -> Tuple[str, Dict[str, Dict[str, str]], Dict[str, Dict[str, Any]]]:
    """Are versions ``a`` and ``b`` on one signed parent chain? ``(verdict, missing, loaded)``.

    ``linear``: one reaches the other through signed parents. ``fork``: both
    ancestries are known as far as they matter and neither reaches the other.
    ``unproven``: the decision hangs on ancestors with no verifiable copy
    (``missing`` maps each to ``{reason, detail}``) -- insufficient evidence,
    never reported as a fork.

    Missing ancestors are fetched only where they matter: an unknown ancestor
    that the other side also reaches is a common ancestor and cannot hide
    either version. Copies come from the same places ``receive`` reads
    (:func:`_read_lineage_raw`) and are verified with the author's keys,
    including re-confirmed retired keys (``keyHistory`` / ``previousKeys``) as
    historical evidence, which grants those keys nothing else. Verified nodes
    are added to ``graph`` (and returned in ``loaded``); ``cache/lineage`` keeps
    their records, exactly as ``receive`` does.
    """
    nodes = graph.setdefault("nodes", {})
    ready_box = {} if ready_box is None else ready_box
    failed = {} if failed is None else failed
    loaded: Dict[str, Dict[str, Any]] = {}
    st_mod = _storage_mod()
    while True:
        relay_to_vid = {str(n["relayId"]): vid for vid, n in nodes.items() if isinstance(n, dict) and n.get("relayId")}
        reach_a, miss_a = _lineage_reach(nodes, relay_to_vid, a)
        reach_b, miss_b = _lineage_reach(nodes, relay_to_vid, b)
        if b in reach_a or a in reach_b:
            return "linear", {}, loaded
        relevant = sorted({m for m in miss_a if m not in reach_b} | {m for m in miss_b if m not in reach_a})
        if not relevant:
            return "fork", {}, loaded
        todo = [m for m in relevant if m not in failed]
        if not todo:
            return "unproven", {m: dict(failed[m]) for m in relevant}, loaded
        for m in todo:
            try:
                V.validate_version_id(m)
                raw = _read_lineage_raw(provider, base, m, ready_box)
                node = _verified_lineage_node(raw, identity, cloud, {project_id: graph}, project_id, m)
            except RelayError as e:
                failed[m] = {"reason": "missing" if e.code == "lineage_incomplete" else (e.code or "untrusted"),
                             "detail": str(e.message)[:200]}
                continue
            except Exception as e:  # never SyncCancelled / KeyboardInterrupt (BaseException)
                transport = isinstance(e, (st_mod.StorageError, OSError)) and not isinstance(e, V.ValidationError)
                failed[m] = {"reason": "unavailable" if transport else "unverifiable",
                             "detail": SR.error_message(e)[:200]}
                continue
            nodes[m] = node
            loaded[m] = node
            try:
                _store_lineage_raw(base, m, raw)
            except (OSError, V.ValidationError):
                pass  # the verified node is kept in the graph; only the local copy is skipped


def _snapshot_cache_dir(local_home: Path, version_id: str) -> Path:
    """Cache directory of one snapshot; the ID and on-disk containment are checked first."""
    V.validate_version_id(version_id)
    cdir = Path(local_home) / "cache" / "snapshots" / version_id
    V.ensure_contained(local_home, cdir, check_final=True, field="cache", local=True)
    return cdir


def _local_dir_classified(exc: "V.LocalPathError") -> "SR.ClassifiedError":
    """A local directory refusal as a sync problem: needs the user, with the doctor's exact fix command."""
    from . import diagnostics as DX
    adv = DX.advice("local_dir_symlink", path=exc.link, target=exc.target)
    return SR.ClassifiedError(SR.NEEDS_ACTION, "local_dir_symlink", adv["reason"], adv["command"])


def _archive_dir_checked(local_home: Path, project_id: str) -> Path:
    """Conversation archive directory of a project, refusing unsafe IDs and symlinked components."""
    V.validate_project_id(project_id)
    adir = Path(local_home) / "archives" / project_id
    for sub in ("markdown", "latest"):
        V.ensure_contained(local_home, adir / sub, check_final=True, field="archive", local=True)
    return adir


def _merge_archive_checked(conv_mod, local_home: Path, project_id: str, bundle: Dict[str, Any]) -> Dict[str, Any]:
    _archive_dir_checked(local_home, project_id)
    return conv_mod.merge_and_save_archive(local_home, project_id, bundle)


def _save_snapshot_to_cache(
    local_home: Path,
    version_id: str,
    signed_snapshot: Dict[str, Any],
    tar_path: str,
    metadata: Dict[str, Any],
    meta_plain: Dict[str, Any],
) -> Path:
    cdir = _snapshot_cache_dir(local_home, version_id)
    cdir.mkdir(parents=True, exist_ok=True)
    dst_tar = cdir / "payload.tar.gz"
    memo = cdir / DS.MEMO_NAME
    try:
        os.unlink(str(memo))  # a stale memo must never vouch for the new payload
    except OSError:
        pass
    fd, tmp_tar = tempfile.mkstemp(dir=str(cdir), prefix=".payload-", suffix=".tmp")
    hasher = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as fout, open(tar_path, "rb") as fin:
            while True:
                buf = fin.read(65536)
                if not buf:
                    break
                hasher.update(buf)
                fout.write(buf)
            fout.flush()
            try:
                os.fsync(fout.fileno())
            except OSError:
                pass
        _replace_with_retry(tmp_tar, str(dst_tar))
    except BaseException:
        try:
            os.unlink(tmp_tar)
        except OSError:
            pass
        raise
    _atomic_write_json(cdir / "snapshot.json", signed_snapshot)
    _atomic_write_json(cdir / "metadata.json", metadata)
    _atomic_write_json(cdir / "meta_plain.json", meta_plain)
    # Digest memo (Phase C): later checks re-hash only when size or mtime changed.
    try:
        st_tar = dst_tar.stat()
        DS.write_cache_memo(cdir, hasher.hexdigest(), st_tar.st_size, st_tar.st_mtime_ns)
    except OSError:
        pass
    return cdir


def _list_cached_snapshots(local_home: Path, checker: Optional["_CacheChecker"] = None) -> List[Dict[str, Any]]:
    """Cached snapshots; with ``checker``, only those whose payload digest verifies (have and forwarding).

    Items carry ``versionId``, ``dir``, ``snapshot`` (the signed record) and
    ``tarPath``. ``metaPlain``/``metadata`` are read lazily by
    :func:`_item_meta_plain`, only for snapshots actually delivered (Phase F:
    reading them for every cached snapshot every round was pure overhead).
    With an indexed checker (sync sessions), the record parsed by the cache
    check is reused instead of being read a second time.
    """
    root = local_home / "cache" / "snapshots"
    if not root.is_dir():
        return []
    try:
        V.ensure_contained(local_home, root, check_final=True, field="cache")
    except V.PathEscapeError:
        return []
    indexed = checker is not None and getattr(checker, "index", None) is not None
    out: List[Dict[str, Any]] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.is_symlink() or not entry.is_dir():
            continue
        snap_obj: Optional[Dict[str, Any]] = None
        if indexed:
            # checker.ok() already requires: a real directory, payload and record present, the record
            # naming this directory, the attested record digest and the payload digest.
            try:
                V.validate_version_id(entry.name)
            except V.ValidationError:
                continue
            if not checker.ok(entry.name):
                continue
            snap_obj = checker.record(entry.name)
        tar_file = entry / "payload.tar.gz"
        if snap_obj is None:
            snap_file = entry / "snapshot.json"
            if not snap_file.is_file() or not tar_file.is_file():
                continue
            snap_obj = _read_json_file(snap_file, {})
        # The directory name and the signed record must agree; anything else
        # was not written by this engine and is not offered to peers.
        if not snap_obj.get("versionId") or snap_obj.get("versionId") != entry.name:
            continue
        if not indexed:
            try:
                V.validate_version_id(entry.name)
            except V.ValidationError:
                continue
            if checker is not None and not checker.ok(entry.name):
                continue
        out.append(
            {
                "versionId": str(snap_obj["versionId"]),
                "dir": entry,
                "snapshot": snap_obj,
                "tarPath": str(tar_file),
            }
        )
    return out


def _extract_have_snapshot_ids(have_obj: Any) -> Set[str]:
    ids: Set[str] = set()
    if not isinstance(have_obj, dict):
        return ids
    for item in have_obj.get("snapshots") or []:
        if isinstance(item, str) and item:
            ids.add(item)
        elif isinstance(item, dict):
            vid = item.get("versionId") or item.get("snapshotId")
            if isinstance(vid, str) and vid:
                ids.add(vid)
    return ids


def _update_and_upload_have_manifest(
    provider,
    local_home: Path,
    identity: Dict[str, Any],
) -> Dict[str, Any]:
    """Upload this device's signed have now (have v2: digest-verified cache, ``needs``, ``seq``)."""
    return _refresh_have(provider, Path(local_home), identity, force=True) or {}


def _fetch_peer_have_ids(provider, peer: Dict[str, Any]) -> Set[str]:
    """Snapshot IDs listed in a peer's signed have manifest; empty when the peer has published none.

    Transport failures, malformed JSON and signature failures propagate: "could
    not read the manifest" must not look like "the peer has nothing" (Phase B, S3).
    """
    have_key = "v3/have/%s.json" % peer["deviceId"]
    try:
        raw = _download_json(provider, have_key)
    except _storage_mod().ObjectNotFoundError:
        return set()
    try:
        verified = crypto.verify_json(
            raw,
            peer["verifyPublicKey"],
            expected_device_id=peer["deviceId"],
        )
    except crypto.CryptoError as e:
        # Unverified hint for telling a retired signing key apart from tampering; never trusted.
        e.signed_at = raw.get("updatedAt") if isinstance(raw, dict) else None
        raise
    return _extract_have_snapshot_ids(verified)


def _upload_v3_sync_delivery(
    provider,
    identity: Dict[str, Any],
    here_id: str,
    target_peer: Dict[str, Any],
    delivery_id: str,
    tar_path: str,
    signed_snapshot: Dict[str, Any],
    meta_plain: Dict[str, Any],
    is_forwarded: bool = False,
    local_home: Optional[Path] = None,
    record_verified: bool = False,
) -> Dict[str, Any]:
    """Publish one sync delivery: snapshot record, then ciphertext, then the signed ready marker.

    ``record_verified``: the caller wrote and read back the snapshot record in
    this operation (publish, send, an earlier delivery of the same round), so it
    is not read again. Fix H: the delivery is checked and prepared once; the
    record and the remote ready marker are no longer read a second time after
    the checkpoint is written, and the ready marker is verified by one read.
    """
    st_mod = _storage_mod()
    target_did = target_peer["deviceId"]
    pkg_key = "v3/inbox/%s/%s/package.enc" % (target_did, delivery_id)
    ready_key = "v3/inbox/%s/%s/ready.json" % (target_did, delivery_id)

    if local_home is None:
        raise RelayError("缓存投递需要本机持久化目录。", code="missing_checkpoint_home")
    if not record_verified:
        _put_immutable_json(provider, _snapshot_record_key(signed_snapshot["versionId"]), signed_snapshot)
    checkpoint_dir = Path(local_home) / "sync_publications" / hashlib.sha256(ready_key.encode("utf-8")).hexdigest()
    checkpoint_file = checkpoint_dir / "ready.json"
    V.ensure_contained(local_home, checkpoint_file, field="checkpoint")

    def validate_ready(raw):
        verified = crypto.verify_json(raw, identity["verifyPublicKey"], expected_device_id=identity["deviceId"])
        expected = {"relayId": delivery_id, "toDeviceId": target_did, "fromDeviceId": identity["deviceId"],
                    "packageKey": pkg_key, "purpose": "sync", "snapshot": signed_snapshot}
        if any(verified.get(key) != value for key, value in expected.items()):
            raise crypto.TamperError("缓存投递的恢复记录与当前目标或快照不一致。")
        if verified.get("recipientKeyFingerprint") not in (None, target_peer.get("keyFingerprint")):
            raise RelayError("缓存投递绑定的目标公钥已改变，拒绝重写原投递。", code="key_changed")
        return verified

    try:
        remote_ready = _download_json(provider, ready_key)
    except st_mod.ObjectNotFoundError:
        remote_ready = None
    if remote_ready is not None:
        validate_ready(remote_ready)
        if os.path.lexists(str(checkpoint_file)):
            try:
                with checkpoint_file.open(encoding="utf-8") as stream:
                    local_ready = json.load(stream)
            except (OSError, ValueError) as exc:
                raise RelayError("缓存投递恢复记录损坏，拒绝重新生成已用编号。", code="checkpoint_corrupted") from exc
            if local_ready != remote_ready:
                raise crypto.TamperError("云端同步记录与本机已准备的记录不符。")
        _verify_published_package(provider, remote_ready)
        return remote_ready

    if not checkpoint_dir.exists():
        checkpoint_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(checkpoint_dir.parent), prefix=".preparing-") as tmpdir:
            enc_path = os.path.join(tmpdir, "package.enc")
            envelope = crypto.encrypt_package(tar_path, enc_path, identity, target_peer)
            with open(enc_path, "rb+") as encrypted_stream:
                os.fsync(encrypted_stream.fileno())
            meta_enc = crypto.encrypt_meta(meta_plain, identity, target_peer)

            ready_unsigned = dict(envelope)
            ready_unsigned.update(
                {
                    "formatVersion": 3,
                    "purpose": "sync",
                    "deliveryId": delivery_id,
                    "relayId": delivery_id,
                    "senderDeviceId": identity["deviceId"],
                    "senderAlias": here_id,
                    "fromAlias": here_id,
                    "fromDeviceId": identity["deviceId"],
                    "toAlias": str(target_peer.get("alias") or ""),
                    "toDeviceId": target_did,
                    "authorizedDeviceId": "",
                    "isForwarded": bool(is_forwarded),
                    "createdAt": L.now_iso(),
                    "packageKey": pkg_key,
                    "snapshot": signed_snapshot,
                    "recipientKeyFingerprint": target_peer.get("keyFingerprint"),
                    "meta": meta_enc,
                }
            )
            signed_ready = crypto.sign_json(
                ready_unsigned,
                identity["signPrivateKey"],
                signer_device_id=identity["deviceId"],
            )
            ready_path = Path(tmpdir) / "ready.json"
            _atomic_write_json(ready_path, signed_ready)
            # Persist exact ciphertext and marker together before any publication.
            os.replace(tmpdir, str(checkpoint_dir))
        workspace_tx.maybe_inject_fault("sync_fwd_after_checkpoint")

    try:
        with checkpoint_file.open(encoding="utf-8") as stream:
            prepared = json.load(stream)
        validate_ready(prepared)
    except (OSError, ValueError) as exc:
        raise RelayError("缓存投递恢复记录损坏，拒绝重新生成已用编号。", code="checkpoint_corrupted") from exc
    encrypted_file = checkpoint_dir / "package.enc"
    digest, size = _sha256_file_info(str(encrypted_file))
    if digest != prepared["packageSha256"] or size != prepared["packageSize"]:
        raise crypto.TamperError("缓存投递的本机密文已损坏。")
    provider.upload(str(encrypted_file), pkg_key)
    _verify_published_package(provider, prepared)
    workspace_tx.maybe_inject_fault("sync_fwd_after_package_upload")
    provider.upload(str(checkpoint_file), ready_key)
    try:
        published = _download_json(provider, ready_key)
    except st_mod.ObjectNotFoundError:
        raise st_mod.StorageError("云端同步标记上传后未找到：%s" % ready_key)
    if published != prepared:
        raise crypto.TamperError("同步标记上传后内容不一致。")
    return prepared


# ---------------------------------------------------------------- Phase C: key versions, delivery state, cache recovery

from . import delivery_state as DS  # noqa: E402  (stdlib-only, never imports this module)


def _valid_key_entries(entries: Any) -> List[Dict[str, Any]]:
    """Well-formed retired-key records (``previousKeys`` / ``keyHistory``); malformed ones are ignored."""
    out: List[Dict[str, Any]] = []
    if not isinstance(entries, list):
        return out
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        vpk, bpk = ent.get("verifyPublicKey"), ent.get("boxPublicKey")
        if not isinstance(vpk, str) or not vpk or not isinstance(bpk, str) or not bpk:
            continue
        fp = str(ent.get("keyFingerprint") or "")
        if not fp:
            try:
                fp = crypto.device_fingerprint(vpk, bpk)
            except Exception:
                continue
        item = dict(ent)
        item["keyFingerprint"] = fp
        out.append(item)
    return out


def _previous_keys(identity: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _valid_key_entries((identity or {}).get("previousKeys"))


def _key_history(peer: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return _valid_key_entries((peer or {}).get("keyHistory"))


def _verify_with_keys(raw: Any, keys: List[Dict[str, Any]], expected_did: str) -> Optional[Dict[str, Any]]:
    """The first key record whose verify key checks the signature on ``raw``; ``None`` if none does."""
    for ent in keys:
        try:
            crypto.verify_json(raw, ent["verifyPublicKey"], expected_device_id=expected_did)
        except crypto.CryptoError:
            continue
        return ent
    return None


def _own_keys(identity: Dict[str, Any]) -> List[Dict[str, Any]]:
    current = {"verifyPublicKey": identity["verifyPublicKey"], "boxPublicKey": identity.get("boxPublicKey", ""),
               "keyFingerprint": _identity_fingerprint(identity), "current": True}
    return [current] + _previous_keys(identity)


def _graph_node(version_graph: Any, project_id: Any, version_id: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(version_graph, dict):
        return None
    graph = version_graph.get(str(project_id or ""))
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    node = nodes.get(str(version_id or "")) if isinstance(nodes, dict) else None
    return node if isinstance(node, dict) else None


def _graph_attests(node: Optional[Dict[str, Any]], raw: Dict[str, Any], record_sha: str) -> bool:
    """Whether a version-graph node already vouches for exactly this signed snapshot record."""
    if not isinstance(node, dict):
        return False
    if node.get("recordSha256"):
        return node.get("recordSha256") == record_sha
    # Nodes recorded before Phase C carry no record digest: require the same author and payload digest.
    return (str(node.get("contentSha256") or "").lower() == str(raw.get("contentSha256") or "").lower() != ""
            and node.get("authorDeviceId") == raw.get("authorDeviceId"))


def _verify_author_snapshot(raw: Any, identity: Dict[str, Any], cloud: Dict[str, Any],
                            version_graph: Any = None, use: str = "cache",
                            sender_did: str = "") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Verify a signed snapshot against its author's keys; return ``(verified, proof)``.

    ``use`` is ``cache``, ``forward``, ``lineage`` or ``handoff_incoming``. Keys tried:

    1. author is this device: the current identity key, then ``previousKeys``;
    2. author is a paired peer: the pinned key, then its re-confirmed ``keyHistory``;
    3. author is ``key_changed`` (not re-confirmed): only records the local version
       graph already attests (same record digest), with the last confirmed keys;
    4. anything else: ``untrusted_author`` (needs the user).

    A retired key yields ``trust: "historical"``: it proves the record existed
    under a key this device's user confirmed, and grants nothing new. A handoff
    snapshot may be historical only when its sender is its author (self-vouched by
    the sender's current-key ready signature). ``proof`` carries
    ``authorKeyFingerprint``, ``trust`` and ``recordSha256`` for the graph node.
    """
    if not isinstance(raw, dict):
        raise crypto.TamperError("快照记录格式无效：顶层不是对象。")
    author = str(raw.get("authorDeviceId") or "")
    vid = str(raw.get("versionId") or "")
    record_sha = crypto.snapshot_record_digest(raw)
    candidates: List[Tuple[Dict[str, Any], str]] = []
    if author and author == identity.get("deviceId"):
        for ent in _own_keys(identity):
            candidates.append((ent, "current" if ent.get("current") else "historical"))
    else:
        peers = cloud.get("peers") if isinstance(cloud.get("peers"), dict) else {}
        peer = peers.get(author)
        pinned = None
        if isinstance(peer, dict) and peer.get("verifyPublicKey"):
            pinned = {"verifyPublicKey": peer["verifyPublicKey"], "boxPublicKey": peer.get("boxPublicKey", ""),
                      "keyFingerprint": str(peer.get("keyFingerprint") or "")}
        if pinned is not None and peer.get("paired") and peer.get("status") == "paired":
            candidates.append((pinned, "current"))
            candidates.extend((ent, "historical") for ent in _key_history(peer))
        elif pinned is not None and peer.get("status") == "key_changed":
            if not _graph_attests(_graph_node(version_graph, raw.get("projectId"), vid), raw, record_sha):
                raise RelayError(
                    "快照 %s 的作者设备 %s 已更换密钥且尚未重新确认；在重新核对指纹前只接受本机版本图已记录的旧快照。"
                    % (vid or "（未知）", author), code="untrusted_author")
            candidates.append((pinned, "attested"))
            candidates.extend((ent, "attested") for ent in _key_history(peer))
        else:
            raise RelayError("未知或未配对的快照作者设备：%s" % (author or "（缺失）"), code="untrusted_author")
    for ent, trust in candidates:
        try:
            body = crypto.verify_json(raw, ent["verifyPublicKey"], expected_device_id=author)
        except crypto.CryptoError:
            continue
        verified = crypto.check_snapshot_body(body, expected_author_id=author)  # one signature check per key
        if trust == "historical" and use == "handoff_incoming" and sender_did != author:
            raise RelayError(
                "交接中的快照 %s 由作者设备 %s 换钥前的旧密钥签名，且不是作者本人发送；拒绝作为当前交接领取。"
                % (vid, author), code="historical_snapshot_not_current")
        fp = DS.norm_fp(ent.get("keyFingerprint"))
        if not fp:
            try:
                fp = DS.norm_fp(crypto.device_fingerprint(ent["verifyPublicKey"], ent["boxPublicKey"]))
            except Exception:
                fp = ""
        return verified, {"authorKeyFingerprint": fp, "trust": trust, "recordSha256": record_sha}
    # No known key verifies: report the precise failure against the current key.
    crypto.verify_snapshot(raw, candidates[0][0]["verifyPublicKey"], expected_author_id=author)
    raise crypto.TamperError("快照 %s 的作者签名无法用任何已确认的公钥验证。" % vid)


def _protected_snapshots(state: Dict[str, Any]) -> Set[str]:
    """Every project's current version and every version-graph head (never pruned, always repairable)."""
    out: Set[str] = set()
    for vid in ((state or {}).get("localCurrentVersion") or {}).values():
        if isinstance(vid, str) and vid:
            out.add(vid)
    for graph in ((state or {}).get("versionGraph") or {}).values():
        for head in ((graph or {}).get("heads") or []) if isinstance(graph, dict) else []:
            if isinstance(head, str) and head:
                out.add(head)
    return out


def _proof_fields(proof: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(proof, dict):
        return {}
    return {k: proof[k] for k in ("authorKeyFingerprint", "trust", "recordSha256") if proof.get(k)}


class _CacheChecker(object):
    """Per-operation memo of :func:`delivery_state.cache_ok` results for cached snapshots.

    With ``index`` (a :class:`delivery_state.CacheIndex` kept by a sync session
    across rounds), unchanged cache entries are not re-read every round, and the
    signed record parsed by the check is kept for :func:`_list_cached_snapshots`.
    Containment is then checked once per checker for ``cache/snapshots`` (as
    :func:`_snapshot_cache_dir` would for every entry); each entry's own
    directory must still be a real directory (not a symlink), which both the
    index signature and :func:`delivery_state.cache_ok` require.
    """

    def __init__(self, base: Path, version_graph: Any = None, budget: Optional["DS.HashBudget"] = None,
                 index: Optional["DS.CacheIndex"] = None) -> None:
        self.base = Path(base)
        self.budget = budget
        self.index = index
        self.memo: Dict[str, bool] = {}
        self.records: Dict[str, Dict[str, Any]] = {}
        self.attested: Dict[str, str] = {}
        self._root_ok: Optional[bool] = None
        for graph in (version_graph or {}).values() if isinstance(version_graph, dict) else []:
            for vid, node in ((graph or {}).get("nodes") or {}).items() if isinstance(graph, dict) else []:
                if isinstance(node, dict) and node.get("recordSha256"):
                    self.attested[str(vid)] = str(node["recordSha256"])

    def _indexed_dir(self, vid: str) -> Path:
        V.validate_version_id(vid)
        root = self.base / "cache" / "snapshots"
        if self._root_ok is None:
            try:
                V.ensure_contained(self.base, root, check_final=True, field="cache", local=True)
                self._root_ok = True
            except V.ValidationError:
                self._root_ok = False
        if not self._root_ok:
            raise V.PathEscapeError("缓存目录经过符号链接或位于本机目录之外：%s" % root, field="cache")
        return root / vid

    def ok(self, vid: str) -> bool:
        if vid in self.memo:
            return self.memo[vid]
        try:
            cdir = self._indexed_dir(vid) if self.index is not None else _snapshot_cache_dir(self.base, vid)
        except (V.ValidationError, OSError):
            self.memo[vid] = False
            return False
        if self.index is not None:
            result, record = self.index.check(cdir, crypto.snapshot_record_digest, self.attested.get(vid, ""),
                                              self.budget)
            if result and record is not None:
                self.records[vid] = record
        else:
            result = DS.cache_ok(cdir, crypto.snapshot_record_digest, self.attested.get(vid, ""), self.budget)
        self.memo[vid] = result
        return result

    def note(self, vid: str, ok: bool, record_sha: str = "") -> None:
        self.memo[vid] = bool(ok)
        self.records.pop(vid, None)  # just (re)written: the listing reads the record from disk
        if record_sha:
            self.attested[vid] = record_sha

    def record(self, vid: str) -> Optional[Dict[str, Any]]:
        """The signed record parsed while checking ``vid`` through the index (this round's copy), if any."""
        return self.records.get(vid)


def _delivery_book(base: Path, identity: Dict[str, Any]) -> "DS.DeliveryBook":
    """Open ``deliveries.json``; on first use, adopt this device's existing publication checkpoints.

    Migration (format 1): every ``sync_publications/<sha256(readyKey)>/ready.json``
    signed by this device (current or previous key) becomes an outbound entry
    ``published`` under the recipient key fingerprint it was sealed to, with
    reason ``legacy``. ``forwardedSnapshots`` and ``processedDeliveries`` stay
    where they are and are read directly; only their sizes are recorded.
    """
    base = Path(base)

    def init(st: Dict[str, Any]) -> None:
        my_did = identity["deviceId"]
        keys = _own_keys(identity)
        counts = {"checkpoints": 0, "skippedCheckpoints": 0}
        root = base / "sync_publications"
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name) if root.is_dir() and not root.is_symlink() else []
        except OSError:
            entries = []
        for d in entries:
            ready_file = d / "ready.json"
            try:
                if d.is_symlink() or not ready_file.is_file():
                    continue
                with ready_file.open(encoding="utf-8") as stream:
                    ready = json.load(stream)
                signer = _verify_with_keys(ready, keys, my_did)
                to_did = str(ready.get("toDeviceId") or "")
                deliv = str(ready.get("relayId") or "")
                snap = ready.get("snapshot") if isinstance(ready.get("snapshot"), dict) else {}
                snap_id = str(snap.get("versionId") or "")
                ready_key = "v3/inbox/%s/%s/ready.json" % (to_did, deliv)
                if (signer is None or not to_did or not deliv or not snap_id or ready.get("purpose") != "sync"
                        or hashlib.sha256(ready_key.encode("utf-8")).hexdigest() != d.name):
                    counts["skippedCheckpoints"] += 1
                    continue
                V.validate_device_id(to_did)
                V.validate_delivery_id(deliv)
                V.validate_version_id(snap_id)
            except (OSError, ValueError, UnicodeDecodeError):
                counts["skippedCheckpoints"] += 1
                continue
            rfp = DS.norm_fp(ready.get("recipientKeyFingerprint"))
            key = DS.outbound_key(to_did, rfp, snap_id)
            ent = st["outbound"].get(key) or DS.new_outbound_entry(
                to_did, rfp, snap_id, str(snap.get("projectId") or ""), str(snap.get("authorDeviceId") or ""),
                signer.get("keyFingerprint"))
            gens = ent.setdefault("generations", [])
            if any(g.get("deliveryId") == deliv for g in gens if isinstance(g, dict)):
                continue
            gens.append({"gen": len(gens) + 1, "deliveryId": deliv, "readyKey": ready_key, "state": "published",
                         "reason": "legacy", "packageSha256": str(ready.get("packageSha256") or ""),
                         "packageSize": ready.get("packageSize"), "createdAt": str(ready.get("createdAt") or ""),
                         "publishedAt": ""})
            ent["status"] = "published"
            st["outbound"][key] = ent
            counts["checkpoints"] += 1
        try:
            rs = load_relay_state(base) or {}
        except Exception:
            rs = {}
        counts["forwardedSnapshots"] = sum(len(v) for v in (rs.get("forwardedSnapshots") or {}).values()
                                           if isinstance(v, list))
        counts["processedDeliveries"] = len(rs.get("processedDeliveries") or {})
        counts["at"] = L.now_iso()
        st["migration"] = counts

    return DS.DeliveryBook(base, init=init)


# ---- local cache listing with digest verification


def _cache_checker(base: Path, state: Optional[Dict[str, Any]] = None, budget: Any = None,
                   index: Optional["DS.CacheIndex"] = None) -> _CacheChecker:
    if state is None:
        state = _load_relay_state(base)
    return _CacheChecker(base, state.get("versionGraph") or {}, budget, index=index)


# ---- signed have v2


class HaveView(object):
    """A peer's have manifest as this device understands it.

    ``status``: ``ok`` (current pinned key, fresh), ``absent`` (no manifest:
    unknown, never "missing"), ``stale`` (older sequence, or signed by a key the
    peer has since retired: ignored, never an alarm). Transport errors, damaged
    JSON and unverifiable signatures raise instead.
    """

    __slots__ = ("status", "seq", "snapshots", "needs", "key_fingerprint", "digest", "changed", "reason")

    def __init__(self, status: str, seq: int = -1, snapshots: Optional[Set[str]] = None,
                 needs: Optional[Set[str]] = None, key_fingerprint: str = "", digest: str = "",
                 changed: bool = False, reason: str = "") -> None:
        self.status = status
        self.seq = int(seq)
        self.snapshots = set(snapshots or ())
        self.needs = set(needs or ())
        self.key_fingerprint = key_fingerprint
        self.digest = digest
        self.changed = changed
        self.reason = reason

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _have_digest(device_id: str, fp: str, snapshots: List[str], needs: List[str]) -> str:
    return hashlib.sha256(json.dumps([device_id, DS.norm_fp(fp), sorted(snapshots), sorted(needs)])
                          .encode("utf-8")).hexdigest()


def _fetch_peer_have(provider, peer: Dict[str, Any], book: Optional["DS.DeliveryBook"] = None) -> HaveView:
    """Read and classify a peer's signed have manifest (see :class:`HaveView`)."""
    did = str(peer["deviceId"])
    try:
        raw = _download_json(provider, "v3/have/%s.json" % did)
    except _storage_mod().ObjectNotFoundError:
        return HaveView("absent")
    if not isinstance(raw, dict):
        raise crypto.TamperError("对端设备 %s 的缓存清单不是 JSON 对象。" % did)
    try:
        verified = crypto.verify_json(raw, peer["verifyPublicKey"], expected_device_id=did)
    except crypto.CryptoError as e:
        if _verify_with_keys(raw, _key_history(peer), did) is not None:
            return HaveView("stale", reason="retired_key")  # written before the rotation we re-confirmed
        e.signed_at = raw.get("updatedAt")  # unverified hint for the legacy triage; never trusted
        raise
    pinned = DS.norm_fp(peer.get("keyFingerprint"))
    claimed = verified.get("keyFingerprint")
    if claimed and DS.norm_fp(claimed) != pinned:
        return HaveView("stale", reason="key_mismatch")
    seq_raw = verified.get("seq")
    seq = seq_raw if isinstance(seq_raw, int) and not isinstance(seq_raw, bool) and seq_raw >= 0 else -1
    snaps = _extract_have_snapshot_ids(verified)
    needs: Set[str] = set()
    for item in (verified.get("needs") or [])[:DS.NEEDS_LIMIT] if isinstance(verified.get("needs"), list) else []:
        if isinstance(item, str):
            try:
                needs.add(V.validate_version_id(item))
            except V.ValidationError:
                continue
    prev = book.peer_have(did) if book is not None else {}
    same_key = DS.norm_fp(prev.get("keyFingerprint")) == pinned
    if same_key and isinstance(prev.get("seq"), int) and seq < prev["seq"]:
        return HaveView("stale", seq=seq, reason="late")  # an older manifest arrived after a newer one
    digest = _have_digest(did, pinned, sorted(snaps), sorted(needs))
    changed = not same_key or prev.get("digest") != digest or prev.get("seq") != seq
    return HaveView("ok", seq=seq, snapshots=snaps, needs=needs, key_fingerprint=pinned, digest=digest,
                    changed=changed)


def _upload_have(provider, base: Path, identity: Dict[str, Any], book: "DS.DeliveryBook",
                 snapshots: List[str], needs: List[str]) -> Dict[str, Any]:
    """Sign and upload this device's have v2 with the next sequence number.

    ``seq`` continues from ``deliveries.json``; when that has no record (lost or
    new), from the device's own published manifest, so a reset cannot make peers
    treat new manifests as late ones.
    """
    my_did = identity["deviceId"]
    have_key = "v3/have/%s.json" % my_did
    last = book.self_state().get("haveSeq")
    if not isinstance(last, int) or isinstance(last, bool):
        try:
            remote = _download_json(provider, have_key)
            rseq = remote.get("seq") if isinstance(remote, dict) else None
            last = rseq if isinstance(rseq, int) and not isinstance(rseq, bool) else -1
        except _storage_mod().ObjectNotFoundError:
            last = -1
        except ValueError:
            last = -1
    seq = int(last) + 1
    book.mutate(lambda st: st.setdefault("self", {}).update({"haveSeq": seq}))
    workspace_tx.maybe_inject_fault("sync_have_before_upload")
    body = {
        "formatVersion": 3,
        "haveFormat": 2,
        "deviceId": my_did,
        "keyFingerprint": _identity_fingerprint(identity),
        "seq": seq,
        "snapshots": sorted(snapshots),
        "needs": sorted(needs)[:DS.NEEDS_LIMIT],
        "updatedAt": L.now_iso(),
    }
    signed = crypto.sign_json(body, identity["signPrivateKey"], signer_device_id=my_did)
    with tempfile.TemporaryDirectory() as tmpdir:
        hp = os.path.join(tmpdir, "have.json")
        _atomic_write_json(Path(hp), signed)
        provider.upload(hp, have_key)
    digest = _have_digest(my_did, _identity_fingerprint(identity), snapshots, needs)
    book.mutate(lambda st: st.setdefault("self", {}).update(
        {"lastHaveDigest": digest, "lastHaveAt": L.now_iso(), "lastHaveSeq": seq}))
    return signed


def _have_lists(base: Path, book: "DS.DeliveryBook", checker: _CacheChecker) -> Tuple[List[str], List[str]]:
    snaps = sorted({item["versionId"] for item in _list_cached_snapshots(base, checker=checker)})
    return snaps, book.needs(checker.ok)


def _refresh_have(provider, base: Path, identity: Dict[str, Any], force: bool = False) -> Optional[Dict[str, Any]]:
    """Upload the have after publish/send/receive when its content changed (or the refresh is due)."""
    book = _delivery_book(base, identity)
    snaps, needs = _have_lists(base, book, _cache_checker(base))
    me = book.self_state()
    digest = _have_digest(identity["deviceId"], _identity_fingerprint(identity), snaps, needs)
    if not force and me.get("lastHaveDigest") == digest:
        last = DS._parse_iso(me.get("lastHaveAt"))
        if last is not None and (datetime.now(timezone.utc) - last).total_seconds() < HAVE_REFRESH_SEC:
            return None
    return _upload_have(provider, base, identity, book, snaps, needs)


# ---- outbound planner (sender side; publish, send bystanders and forwarding all use it)


def _item_meta_plain(item: Dict[str, Any], project_id: str) -> Dict[str, Any]:
    if "metaPlain" not in item and item.get("dir"):
        # Cache listing items load their metadata only when a delivery needs it (Phase F).
        item["metaPlain"] = _read_json_file(Path(item["dir"]) / "meta_plain.json", {})
        item["metadata"] = _read_json_file(Path(item["dir"]) / "metadata.json", {})
    meta = item.get("metaPlain")
    if isinstance(meta, dict) and meta:
        return meta
    md = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return {
        "projectId": project_id,
        "projectName": str(md.get("name") or project_id),
        "lastTask": str(md.get("lastTask") or ""),
        "nextAction": str(md.get("nextAction") or ""),
        "branch": str(md.get("branch") or ""),
        "dirty": md.get("dirty", 0),
        "sessionCount": len(md.get("sessions") or []),
    }


class _Outbound(object):
    """Decide and perform cache deliveries keyed by (recipient deviceId, recipient key, snapshot).

    Rules (Phase C, design C2.5):

    * the recipient's fresh, current-key have lists the snapshot -> ``peer_verified``;
    * no entry yet: adopt a pre-Phase-C delivery of ours bound to this key, or
      skip a delivery another paired sender (or our own handoff) already made for
      this key (``covered``), or wait when the peer verified it under its previous
      key and its manifest is not readable yet; otherwise generation 1;
    * a ``preparing`` generation resumes under the same delivery ID (nothing new
      is encrypted when its checkpoint exists);
    * a new generation (new ID, new ciphertext) only when the recipient asks for
      the snapshot in a newer have (``needs``), when our own key rotated since the
      last generation, or when maintenance finds our published objects gone and
      no checkpoint to restore them from. Published objects are never rewritten.
    """

    def __init__(self, provider, identity: Dict[str, Any], here_id: str, base: Path, book: "DS.DeliveryBook",
                 cloud: Dict[str, Any], relay_state: Optional[Dict[str, Any]] = None, clock=None,
                 maint_at: Optional[Dict[str, float]] = None) -> None:
        self.provider = provider
        self.identity = identity
        self.here_id = here_id
        self.base = Path(base)
        self.book = book
        self.cloud = cloud
        self.relay_state = relay_state if relay_state is not None else _load_relay_state(self.base)
        self.clock = clock
        self.maint_at = maint_at if maint_at is not None else {}
        self.my_did = identity["deviceId"]
        self.my_fp = DS.norm_fp(_identity_fingerprint(identity))
        self._inbox: Dict[str, Dict[str, Set[str]]] = {}
        # Fix H: snapshot records written and read back during this operation (publish, send, one
        # sync round); further deliveries of the same snapshot do not read the record again.
        self.verified_records: Set[str] = set()

    # ------------------------------------------------------------ entry point
    def deliver(self, peer: Dict[str, Any], item: Dict[str, Any], have: Optional[HaveView] = None,
                allow_cover: bool = True, adopt_legacy: bool = True) -> Dict[str, Any]:
        """One planning step for ``item`` towards ``peer``; ``adopt_legacy=False`` for snapshots created just now."""
        did = str(peer["deviceId"])
        fp = DS.norm_fp(peer.get("keyFingerprint"))
        snap = str(item["versionId"])
        have_ok = have is not None and have.ok
        e = self.book.outbound(did, fp, snap)
        if have_ok and snap in have.snapshots:
            if e is None or e.get("status") != "peer_verified":
                self._mark_verified(did, fp, snap, item, have.seq)
                return {"action": "verified", "haveSeq": have.seq}
            return {"action": "noop"}
        needs = bool(have_ok and snap in have.needs and have.seq > int((e or {}).get("lastRegenHaveSeq", -1)))
        if e is None and adopt_legacy:
            e = self._adopt_legacy(peer, fp, item)
        if e is None:
            if not have_ok and self.book.verified_under_other_key(did, fp, snap):
                return {"action": "waiting", "why": "verified_under_previous_key"}
            if allow_cover and not needs:
                cover = self._covering_delivery(peer, fp, snap)
                if cover:
                    self._mark_covered(did, fp, item, cover)
                    return {"action": "covered", "deliveryId": cover}
            return self._new_generation(peer, fp, item, None, "peer_needs" if needs else "new", have)
        gen = DS.latest_generation(e)
        sender_rotated = DS.norm_fp(e.get("senderKeyFingerprint")) != self.my_fp
        if gen is not None and gen.get("state") == "preparing":
            if not sender_rotated:
                return self._run_generation(peer, fp, item, gen, resumed=True)
            return self._new_generation(peer, fp, item, e, "sender_key_rotated", have)
        if needs:
            return self._new_generation(peer, fp, item, e, "peer_needs", have)
        status = e.get("status")
        if status == "superseded":
            status = e.get("supersededFrom") or "published"  # this key is current again (re-confirmed back)
        if status == "covered":
            if self._maintenance_due(did, fp, snap, e, gen):
                cover = str(e.get("coveredBy") or "")
                if not self._delivery_listed(did, cover) and cover not in (self.relay_state.get("sentRelays") or {}):
                    return self._new_generation(peer, fp, item, e, "objects_missing", have)
            return {"action": "waiting"}
        if status == "peer_verified":
            return {"action": "noop"}  # held by the recipient; only a fresh ``needs`` asks for it again
        if gen is None:
            return self._new_generation(peer, fp, item, e, "new", have)
        if sender_rotated:
            return self._new_generation(peer, fp, item, e, "sender_key_rotated", have)
        if status == "published" and self._maintenance_due(did, fp, snap, e, gen):
            return self._maintain(peer, fp, item, e, gen, have)
        return {"action": "waiting"}

    # ------------------------------------------------------------ transitions
    def _entry_base(self, st: Dict[str, Any], did: str, fp: str, item: Dict[str, Any]) -> Dict[str, Any]:
        snap_obj = item.get("snapshot") or {}
        key = DS.outbound_key(did, fp, item["versionId"])
        ent = st["outbound"].get(key)
        if not isinstance(ent, dict):
            ent = DS.new_outbound_entry(did, fp, str(item["versionId"]), str(snap_obj.get("projectId") or ""),
                                        str(snap_obj.get("authorDeviceId") or ""), self.my_fp)
            st["outbound"][key] = ent
        return ent

    def _mark_verified(self, did: str, fp: str, snap: str, item: Dict[str, Any], seq: int) -> None:
        def fn(st: Dict[str, Any]) -> None:
            ent = self._entry_base(st, did, fp, item)
            _set_verified(ent, seq)

        self.book.mutate(fn)

    def _mark_covered(self, did: str, fp: str, item: Dict[str, Any], cover: str) -> None:
        def fn(st: Dict[str, Any]) -> None:
            ent = self._entry_base(st, did, fp, item)
            ent["status"] = "covered"
            ent["coveredBy"] = cover
            ent["coveredAt"] = L.now_iso()

        self.book.mutate(fn)

    def _new_generation(self, peer: Dict[str, Any], fp: str, item: Dict[str, Any], e: Optional[Dict[str, Any]],
                        reason: str, have: Optional[HaveView]) -> Dict[str, Any]:
        did = str(peer["deviceId"])
        snap_obj = item.get("snapshot") or {}
        gen_no = DS.next_generation_number(e)
        if gen_no > DS.MAX_GENERATIONS:
            raise RelayError(
                "快照 %s 已向设备 %s 的当前公钥重新投递 %d 次仍未被确认，已停止自动重投。"
                % (item["versionId"], did, DS.MAX_GENERATIONS), code="repeated_redelivery")
        author_is_self = str(snap_obj.get("authorDeviceId") or "") == self.my_did
        delivery_id = DS.delivery_id_for(str(item["versionId"]), self.my_did, fp, gen_no, author_is_self)
        ready_key = "v3/inbox/%s/%s/ready.json" % (did, delivery_id)
        now = L.now_iso()
        gen = {"gen": gen_no, "deliveryId": delivery_id, "readyKey": ready_key, "state": "preparing",
               "reason": reason, "createdAt": now}

        def fn(st: Dict[str, Any]) -> None:
            ent = self._entry_base(st, did, fp, item)
            for old in ent.setdefault("generations", []):
                if isinstance(old, dict) and old.get("state") == "preparing":
                    old["state"] = "abandoned"
            ent["generations"].append(dict(gen))
            ent["senderKeyFingerprint"] = self.my_fp
            ent["status"] = "pending"
            ent.pop("coveredBy", None)
            if reason == "peer_needs" and have is not None:
                ent["lastRegenHaveSeq"] = have.seq

        # Crash ordering: the generation (and its delivery ID) is durable before any encryption or upload.
        self.book.mutate(fn)
        workspace_tx.maybe_inject_fault("sync_fwd_after_entry_preparing")
        return self._run_generation(peer, fp, item, gen, resumed=False)

    def _run_generation(self, peer: Dict[str, Any], fp: str, item: Dict[str, Any], gen: Dict[str, Any],
                        resumed: bool) -> Dict[str, Any]:
        did = str(peer["deviceId"])
        snap_obj = item.get("snapshot") or {}
        project_id = str(snap_obj.get("projectId") or "")
        delivery_id = str(gen["deliveryId"])
        author_is_self = str(snap_obj.get("authorDeviceId") or "") == self.my_did
        snap_id = str(item["versionId"])
        ready = _upload_v3_sync_delivery(
            self.provider, self.identity, self.here_id, peer, delivery_id, str(item.get("tarPath") or ""),
            snap_obj, _item_meta_plain(item, project_id), is_forwarded=not author_is_self, local_home=self.base,
            record_verified=snap_id in self.verified_records)
        self.verified_records.add(snap_id)
        workspace_tx.maybe_inject_fault("sync_fwd_after_ready_upload")

        def fn(st: Dict[str, Any]) -> None:
            ent = self._entry_base(st, did, fp, item)
            for g in ent.get("generations") or []:
                if isinstance(g, dict) and g.get("deliveryId") == delivery_id:
                    g["state"] = "published"
                    g["packageSha256"] = str(ready.get("packageSha256") or "")
                    g["packageSize"] = ready.get("packageSize")
                    g["publishedAt"] = L.now_iso()
            if ent.get("status") != "peer_verified":
                ent["status"] = "published"

        self.book.mutate(fn)

        def mirror(st_obj: Dict[str, Any]) -> None:
            # Non-authoritative mirror kept for a downgrade to a pre-Phase-C v3 engine.
            lst = list(st_obj.setdefault("forwardedSnapshots", {}).get(did) or [])
            if item["versionId"] not in lst:
                lst.append(item["versionId"])
                st_obj["forwardedSnapshots"][did] = lst

        _update_relay_state(self.base, mirror)
        return {"action": "resumed" if resumed else "published", "deliveryId": delivery_id,
                "generation": int(gen.get("gen") or 1), "reason": str(gen.get("reason") or "")}

    # ------------------------------------------------------------ legacy adoption and covering deliveries
    def _adopt_legacy(self, peer: Dict[str, Any], fp: str, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Adopt a pre-Phase-C delivery of ours (``sync_|fwd_<snap>_<me6>``) bound to this key, if any."""
        did = str(peer["deviceId"])
        snap = str(item["versionId"])
        keys = _own_keys(self.identity)
        st_mod = _storage_mod()
        for legacy_id in DS.legacy_delivery_ids(snap, self.my_did):
            ready_key = "v3/inbox/%s/%s/ready.json" % (did, legacy_id)
            ckpt = self.base / "sync_publications" / hashlib.sha256(ready_key.encode("utf-8")).hexdigest() / "ready.json"
            ready: Optional[Dict[str, Any]] = None
            published = False
            try:
                if ckpt.is_file() and not ckpt.is_symlink():
                    with ckpt.open(encoding="utf-8") as stream:
                        ready = json.load(stream)
                    published = self.provider.stat(ready_key) is not None
                elif self.provider.stat(ready_key) is not None:
                    ready = _download_json(self.provider, ready_key)
                    published = True
            except (OSError, ValueError, st_mod.ObjectNotFoundError):
                ready = None
            if not isinstance(ready, dict):
                continue
            signer = _verify_with_keys(ready, keys, self.my_did)
            snap_in = ready.get("snapshot") if isinstance(ready.get("snapshot"), dict) else {}
            if signer is None or ready.get("toDeviceId") != did or snap_in.get("versionId") != snap:
                continue
            rfp = DS.norm_fp(ready.get("recipientKeyFingerprint"))
            gen = {"gen": 1, "deliveryId": legacy_id, "readyKey": ready_key,
                   "state": "published" if published else "preparing", "reason": "legacy",
                   "packageSha256": str(ready.get("packageSha256") or ""), "packageSize": ready.get("packageSize"),
                   "createdAt": str(ready.get("createdAt") or ""), "publishedAt": ""}
            sender_fp = DS.norm_fp(signer.get("keyFingerprint"))

            def fn(st: Dict[str, Any], _rfp: str = rfp, _gen: Dict[str, Any] = gen) -> None:
                key = DS.outbound_key(did, _rfp, snap)
                ent = st["outbound"].get(key)
                if not isinstance(ent, dict):
                    snap_obj = item.get("snapshot") or {}
                    ent = DS.new_outbound_entry(did, _rfp, snap, str(snap_obj.get("projectId") or ""),
                                                str(snap_obj.get("authorDeviceId") or ""), sender_fp)
                    st["outbound"][key] = ent
                gens = ent.setdefault("generations", [])
                if not any(isinstance(g, dict) and g.get("deliveryId") == _gen["deliveryId"] for g in gens):
                    _gen["gen"] = len(gens) + 1
                    gens.append(dict(_gen))
                if _rfp != fp:
                    ent["status"] = "superseded"
                elif ent.get("status") not in ("peer_verified",):
                    ent["status"] = "published" if _gen["state"] == "published" else "pending"

            self.book.mutate(fn)
            if rfp == fp:
                return self.book.outbound(did, fp, snap)
        return None

    def _peer_inbox(self, did: str) -> Dict[str, Set[str]]:
        if did not in self._inbox:
            listing: Dict[str, Set[str]] = {}
            prefix = "v3/inbox/%s/" % did
            for obj in self.provider.list_objects(prefix):
                parts = str(obj.key or "").lstrip("/").split("/")
                if len(parts) == 5 and parts[2] == did and parts[3]:
                    listing.setdefault(parts[3], set()).add(parts[4])
            self._inbox[did] = listing
        return self._inbox[did]

    def _delivery_listed(self, did: str, delivery_id: str) -> bool:
        if not delivery_id:
            return False
        self._inbox.pop(did, None)
        files = self._peer_inbox(did).get(delivery_id) or set()
        return "ready.json" in files and "package.enc" in files

    def _covering_delivery(self, peer: Dict[str, Any], fp: str, snap: str) -> str:
        """A delivery that already brings ``snap`` to this recipient key: our handoff, or another paired sender's."""
        did = str(peer["deviceId"])
        for rid, info in (self.relay_state.get("sentRelays") or {}).items():
            if (isinstance(info, dict) and info.get("toDeviceId") == did and info.get("snapshotId") == snap
                    and DS.norm_fp(info.get("recipientKeyFingerprint") or peer.get("keyFingerprint")) == fp
                    and int(info.get("protocolVersion") or 2) >= 3):
                return str(rid)
        peers_map = self.cloud.get("peers") if isinstance(self.cloud.get("peers"), dict) else {}
        for deliv, files in sorted(self._peer_inbox(did).items()):
            m = DS.match_snapshot_delivery(deliv, snap)
            if not m or "ready.json" not in files or "package.enc" not in files:
                continue
            if m["sender6"] == self.my_did[-6:] or (m["fp8"] and m["fp8"] != DS.fp8(fp)):
                continue
            try:
                ready = _download_json(self.provider, "v3/inbox/%s/%s/ready.json" % (did, deliv))
            except Exception:
                continue
            sender = str((ready or {}).get("senderDeviceId") or (ready or {}).get("fromDeviceId") or "") \
                if isinstance(ready, dict) else ""
            sp = peers_map.get(sender)
            if (not isinstance(sp, dict) or not sp.get("paired") or sp.get("status") != "paired"
                    or sender[-6:] != m["sender6"]):
                continue
            try:
                verified = crypto.verify_json(ready, sp["verifyPublicKey"], expected_device_id=sender)
            except crypto.CryptoError:
                continue
            snap_in = verified.get("snapshot") if isinstance(verified.get("snapshot"), dict) else {}
            if (verified.get("purpose") == "sync" and verified.get("toDeviceId") == did
                    and DS.norm_fp(verified.get("recipientKeyFingerprint")) == fp and snap_in.get("versionId") == snap):
                return deliv
        return ""

    # ------------------------------------------------------------ maintenance of published objects
    def _maintenance_due(self, did: str, fp: str, snap: str, e: Dict[str, Any],
                         gen: Optional[Dict[str, Any]]) -> bool:
        """Unconfirmed for at least MAINTENANCE_SEC (wall clock, persisted) since publication or the last check."""
        last = DS.parse_iso(e.get("lastCheckedAt") or (gen or {}).get("publishedAt") or (gen or {}).get("createdAt")
                            or e.get("coveredAt") or e.get("createdAt"))
        now = DS.wall_now()
        if last is not None and (now - last).total_seconds() < DS.MAINTENANCE_SEC:
            return False

        def fn(st: Dict[str, Any]) -> None:
            ent = st["outbound"].get(DS.outbound_key(did, fp, snap))
            if isinstance(ent, dict):
                ent["lastCheckedAt"] = L.now_iso()

        self.book.mutate(fn)
        return True

    def _maintain(self, peer: Dict[str, Any], fp: str, item: Dict[str, Any], e: Dict[str, Any],
                  gen: Dict[str, Any], have: Optional[HaveView]) -> Dict[str, Any]:
        """Published but unconfirmed for a while: restore missing objects from the checkpoint, byte for byte."""
        ready_key = str(gen.get("readyKey") or "")
        if not ready_key.endswith("/ready.json"):
            return {"action": "waiting"}
        pkg_key = ready_key[: -len("ready.json")] + "package.enc"
        has_ready = self.provider.stat(ready_key) is not None
        has_pkg = self.provider.stat(pkg_key) is not None
        if has_ready and has_pkg:
            return {"action": "waiting"}
        ckpt = self.base / "sync_publications" / hashlib.sha256(ready_key.encode("utf-8")).hexdigest()
        ck_ready, ck_pkg = ckpt / "ready.json", ckpt / "package.enc"
        if ck_ready.is_file() and ck_pkg.is_file():
            with ck_ready.open(encoding="utf-8") as stream:
                prepared = json.load(stream)
            digest, size = _sha256_file_info(str(ck_pkg))
            if digest != prepared.get("packageSha256") or size != prepared.get("packageSize"):
                raise crypto.TamperError("投递 %s 的本机密文检查点已损坏。" % gen.get("deliveryId"))
            if not has_pkg:
                self.provider.upload(str(ck_pkg), pkg_key)
                _verify_published_package(self.provider, prepared)
            if not has_ready:
                self.provider.upload(str(ck_ready), ready_key)
            return {"action": "restored", "deliveryId": str(gen.get("deliveryId") or ""),
                    "generation": int(gen.get("gen") or 1), "reason": "objects_restored"}
        return self._new_generation(peer, fp, item, e, "objects_missing", have)


def _set_verified(ent: Dict[str, Any], seq: int) -> None:
    ent["status"] = "peer_verified"
    ent["peerVerified"] = {"haveSeq": int(seq), "at": L.now_iso()}
    for g in ent.get("generations") or []:
        if isinstance(g, dict) and g.get("state") == "preparing":
            g["state"] = "abandoned"  # the recipient already holds a verified copy


def _record_peer_have(book: "DS.DeliveryBook", peer: Dict[str, Any], have: HaveView) -> List[Dict[str, Any]]:
    """Store a fresh, verified have and mark our entries for that key ``peer_verified``; return transitions."""
    did = str(peer["deviceId"])
    fp = have.key_fingerprint
    changed: List[Dict[str, Any]] = []
    if not have.changed:
        return changed

    def fn(st: Dict[str, Any]) -> None:
        st["peerHave"][did] = {"keyFingerprint": fp, "seq": have.seq, "digest": have.digest,
                               "verifiedAt": L.now_iso(), "count": len(have.snapshots),
                               "needs": sorted(have.needs)[:DS.NEEDS_LIMIT]}
        prefix = "%s|%s|" % (did, fp)
        for key, ent in st["outbound"].items():
            if not key.startswith(prefix) or not isinstance(ent, dict):
                continue
            if ent.get("snapshotId") in have.snapshots and ent.get("status") in ("pending", "published", "covered"):
                _set_verified(ent, have.seq)
                changed.append({"snapshotId": ent.get("snapshotId"), "projectId": ent.get("projectId"),
                                "haveSeq": have.seq})

    book.mutate(fn)
    return changed


# ---- receipts signed by a retired key (S4)


def _verify_receipt_with_retired_key(raw_rec: Any, peer: Dict[str, Any], info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Accept a receipt signed by one of the receiver's re-confirmed *retired* keys, only for a relay bound to it.

    Whoever held that key could already decrypt and claim this relay, so this
    grants nothing new. The relay is bound to the key by the fingerprint recorded
    at send time; for older records without it, by ``sentAt`` lying inside the
    key's confirmed period on this device's own clock.
    """
    did = str(peer.get("deviceId") or "")
    bound = DS.norm_fp(info.get("recipientKeyFingerprint"))
    sent_at = str(info.get("sentAt") or "")
    for ent in _key_history(peer):
        try:
            body = crypto.verify_json(raw_rec, ent["verifyPublicKey"], expected_device_id=did)
        except crypto.CryptoError:
            continue
        if bound:
            if DS.norm_fp(ent.get("keyFingerprint")) == bound:
                return body
            continue
        lo, hi = str(ent.get("pairedAt") or ""), str(ent.get("retiredAt") or "")
        if sent_at and lo and hi and lo <= sent_at < hi:
            return body
    return None


# ---------------------------------------------------------------- 3. sync_receipts


def _verify_outgoing_ready(
    raw_ready: dict,
    relay_id: str,
    project_id: str,
    identity: dict,
    peer_desc: dict,
    proto_ver: int = 2,
    recorded_fp: str = "",
) -> Tuple[dict, dict]:
    try:
        ready = crypto.verify_json(raw_ready, identity["verifyPublicKey"], expected_device_id=identity["deviceId"])
    except crypto.CryptoError:
        # S5: signed by this device before its own key rotation -> not tampering.
        if _verify_with_keys(raw_ready, _previous_keys(identity), identity["deviceId"]) is not None:
            cmd = _reissue_command("handoff_sender_key_rotated", project_id, peer_desc)
            err = KeyChangedError(
                "交接包 %s 由本机换钥前的旧密钥签名，对方重新核对本机新公钥后将无法验证或领取；原交接包保持不变。"
                "确认后运行 %s 用新密钥重新签发（新交接编号）。" % (relay_id, cmd),
                peer_id=str(peer_desc.get("deviceId") or ""), code="handoff_sender_key_rotated")
            err.action = cmd
            raise err
        raise
    if proto_ver >= 3:
        expected_pkg_key = "v3/inbox/%s/%s/package.enc" % (peer_desc["deviceId"], relay_id)
        expected = {
            "relayId": relay_id,
            "fromDeviceId": identity["deviceId"],
            "toDeviceId": peer_desc["deviceId"],
            "packageKey": expected_pkg_key,
        }
    else:
        expected = {
            "relayId": relay_id,
            "fromAlias": identity["alias"],
            "fromDeviceId": identity["deviceId"],
            "toAlias": peer_desc["alias"],
            "toDeviceId": peer_desc["deviceId"],
            "packageKey": "relays/%s/package.enc" % relay_id,
        }
    if any(ready.get(key) != value for key, value in expected.items()):
        raise crypto.TamperError("交接包 %s 的已发布记录与本机发送记录不符。" % relay_id)
    if not re.fullmatch(r"[0-9a-f]{64}", str(ready.get("packageSha256") or "")):
        raise crypto.TamperError("交接包 %s 的已发布记录缺少有效的包摘要。" % relay_id)
    bound_fp = DS.norm_fp(ready.get("recipientKeyFingerprint") or recorded_fp)
    current_fp = DS.norm_fp(peer_desc.get("keyFingerprint"))
    reissue_cmd = _reissue_command("handoff_recipient_key_changed", project_id, peer_desc)
    recipient_changed = KeyChangedError(
        "目标设备「%s」已更换密钥，在途交接 %s 是发给它旧公钥的，对方无法再领取；原交接包保持不变。"
        "核对并重新确认对方新指纹后，运行 %s 为新公钥重新签发（新交接编号）。"
        % (peer_desc.get("alias") or peer_desc.get("deviceId") or "", relay_id, reissue_cmd),
        peer_id=str(peer_desc.get("deviceId") or ""), old_fingerprint=bound_fp, new_fingerprint=current_fp,
        code="handoff_recipient_key_changed")
    recipient_changed.action = reissue_cmd
    if bound_fp and current_fp and bound_fp != current_fp:
        raise recipient_changed  # S5: checked before the meta decryption that would report tampering
    try:
        meta = crypto.decrypt_meta(ready.get("meta") or {}, identity, peer_desc)
    except crypto.TamperError:
        if not bound_fp and _key_history(peer_desc):
            raise recipient_changed  # older record without the bound fingerprint, peer re-confirmed since
        raise
    if meta.get("projectId") != project_id:
        raise crypto.TamperError("交接包 %s 的项目编号与本机发送记录不符。" % relay_id)
    return ready, meta


def _reissue_command(code: str, project_id: Any, peer_desc: Optional[Dict[str, Any]]) -> str:
    """The explicit re-issue command, from the advice table shared with ``relay doctor`` and the web page."""
    from . import diagnostics as DX
    to = str((peer_desc or {}).get("alias") or (peer_desc or {}).get("deviceId") or "")
    return DX.advice(code, pid=str(project_id or ""), to=to)["command"]


def _resolve_peer_for_sent_relay(
    info: Dict[str, Any],
    cloud: Dict[str, Any],
    fallback_peer_desc: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    to_did = str(info.get("toDeviceId") or "")
    peers_map = cloud.get("peers") if isinstance(cloud.get("peers"), dict) else {}
    if to_did and isinstance(peers_map.get(to_did), dict):
        return peers_map[to_did]
    if isinstance(fallback_peer_desc, dict):
        if not to_did or fallback_peer_desc.get("deviceId") == to_did:
            return fallback_peer_desc
    return None


def sync_receipts(
    provider,
    identity: Dict[str, Any],
    peer_desc: Optional[Dict[str, Any]] = None,
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
    on_error=None,
    skip=None,
) -> List[Dict[str, Any]]:
    """Sync remote receipt.json acknowledgements for unacked outgoing relays across v3 and v2.

    ``on_error(relay_id, to_device_id, peer, exc)``, when given, receives each
    per-relay failure (storage, signature, mismatching receipt) instead of the
    failure being skipped silently; ``skip(relay_id, to_device_id)`` returning
    true leaves that relay for a later round. Without them the behaviour is
    unchanged (publish, send and inbox stay silent). A receipt is applied only
    after it verifies and matches the send record, as before.
    """
    st_mod = _storage_mod()
    base, led_path, _local_cfg = _resolve_ctx(ledger_path, local, local_home)
    cloud = _load_cloud(base)
    state = _load_relay_state(base)
    sent_relays = state.get("sentRelays") or {}
    unacked_ids = [rid for rid, info in sent_relays.items() if isinstance(info, dict) and not info.get("acked")]
    if not unacked_ids:
        return []

    synced: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for rid in unacked_ids:
            info = sent_relays[rid]
            peer = _resolve_peer_for_sent_relay(info, cloud, peer_desc)
            if not isinstance(peer, dict) or not peer.get("verifyPublicKey"):
                continue

            proto_ver = int(info.get("protocolVersion") or (3 if _peer_is_v3(peer) and str(info.get("readyKey") or "").startswith("v3/") else 2))
            to_did = str(info.get("toDeviceId") or peer.get("deviceId") or "")
            if proto_ver >= 3:
                rkey = "v3/inbox/%s/%s/receipt.json" % (to_did, rid)
                ready_key = str(info.get("readyKey") or ("v3/inbox/%s/%s/ready.json" % (to_did, rid)))
            else:
                rkey = "relays/%s/receipt.json" % rid
                ready_key = str(info.get("readyKey") or ("relays/%s/ready.json" % rid))

            if skip is not None and skip(rid, to_did):
                continue
            if on_error is None:
                if provider.stat(rkey) is None:
                    continue
            else:
                try:
                    if provider.stat(rkey) is None:
                        continue
                except Exception as e:
                    on_error(rid, to_did, peer, e)
                    continue
            rfile = os.path.join(tmpdir, "%s_receipt.json" % rid)
            try:
                provider.download(rkey, rfile)
                with open(rfile, "r", encoding="utf-8-sig") as fh:
                    raw_rec = json.load(fh)
                try:
                    verified = crypto.verify_json(
                        raw_rec,
                        peer["verifyPublicKey"],
                        expected_device_id=peer.get("deviceId"),
                    )
                except crypto.CryptoError:
                    # S4: signed by the receiver's re-confirmed previous key for a relay bound to that key.
                    retired = _verify_receipt_with_retired_key(raw_rec, peer, info)
                    if retired is None:
                        raise
                    verified = retired
                expected_digest = info.get("packageSha256")
                if not expected_digest:
                    raw_ready = _download_json(provider, ready_key)
                    ready, _ = _verify_outgoing_ready(
                        raw_ready, rid, info.get("projectId"), identity, peer, proto_ver=proto_ver,
                        recorded_fp=str(info.get("recipientKeyFingerprint") or ""),
                    )
                    expected_digest = ready["packageSha256"]
            except (st_mod.StorageError, crypto.CryptoError, RelayError, OSError, ValueError, UnicodeDecodeError) as e:
                if on_error is not None:
                    on_error(rid, to_did, peer, e)
                continue

            expected_receipt: Dict[str, Any] = {
                "relayId": rid,
                "projectId": info.get("projectId"),
                "receiverDeviceId": to_did,
                "packageSha256": expected_digest,
            }
            if proto_ver >= 3:
                expected_receipt["purpose"] = "handoff"
                if info.get("snapshotId"):
                    expected_receipt["snapshotId"] = info["snapshotId"]
            else:
                expected_receipt["fromAlias"] = identity["alias"]
                expected_receipt["toAlias"] = info.get("toAlias")

            if any(verified.get(key) != value for key, value in expected_receipt.items()):
                if on_error is not None:
                    mismatch = crypto.TamperError("签收回执 %s 与本机发送记录不符（项目、接收设备、快照或包摘要），未签收。" % rid)
                    mismatch.code = "receipt_mismatch"
                    on_error(rid, to_did, peer, mismatch)
                continue

            proj_id = str(info.get("projectId") or verified.get("projectId") or "")
            to_alias = str(info.get("toAlias") or verified.get("toAlias") or peer.get("alias") or "")
            from_alias = str(verified.get("fromAlias") or identity.get("alias") or "")
            acked_at = str(verified.get("receivedAt") or L.now_iso())

            st_now = _load_relay_state(base)
            local_ver = str((st_now.get("localCurrentVersion") or {}).get(proj_id) or "")
            graph = (st_now.get("versionGraph") or {}).get(proj_id) or {}
            sent_snap_id = str(info.get("snapshotId") or "")
            active_rid = str((st_now.get("activeHandoffs") or {}).get(proj_id) or "")

            successor = str(info.get("supersededBy") or "")
            # Phase D: the target claimed the original before it saw the re-issue (it then refuses the
            # re-issue): the original's verified receipt is the claim, and the re-issue is withdrawn.
            original_won = bool(successor and active_rid == successor
                                and not ((st_now.get("sentRelays") or {}).get(successor) or {}).get("acked"))
            is_late = False
            if active_rid and active_rid != rid:
                is_late = not original_won
            elif sent_snap_id and local_ver and local_ver != sent_snap_id and _is_version_ancestor(graph, sent_snap_id, local_ver):
                is_late = True

            def _apply_ack(l: Dict[str, Any]) -> None:
                for p in l.get("projects") or []:
                    if p.get("id") != proj_id:
                        continue
                    ho = p.get("handoff")
                    is_current_relay = (not is_late) and (
                        p.get("lastRelayId") in (None, "", rid)
                        or (isinstance(ho, dict) and ho.get("relayId") == rid)
                        or (original_won and (p.get("lastRelayId") == successor
                                              or (isinstance(ho, dict) and ho.get("relayId") == successor)))
                    )
                    if is_current_relay and to_alias:
                        p["device"] = to_alias
                        p["handoff"] = None
                        if original_won:
                            p["lastRelayId"] = rid
                        L.touch(p, to_alias)
                    existing_tags = set(
                        h.get("tag") or h.get("relayId")
                        for h in (p.get("history") or [])
                        if isinstance(h, dict)
                    )
                    if rid not in existing_tags:
                        p["history"] = (
                            list(p.get("history") or [])
                            + [
                                {
                                    "tag": rid,
                                    "relayId": rid,
                                    "from": from_alias,
                                    "fromDeviceId": identity["deviceId"],
                                    "to": to_alias,
                                    "toDeviceId": to_did,
                                    "transport": "cloud",
                                    "doneAt": acked_at,
                                }
                            ]
                        )[-10:]
                migrated = devices.migrate_ledger_identities(l, local_ident=identity, cloud_cfg=cloud)
                l.clear()
                l.update(migrated)

            L.update(led_path, _apply_ack)

            def _mark_state(st_obj: Dict[str, Any]) -> None:
                entry = st_obj.setdefault("sentRelays", {}).get(rid)
                if isinstance(entry, dict):
                    entry["acked"] = True
                    entry["ackedAt"] = acked_at
                    entry["packageSha256"] = expected_digest
                if st_obj.get("activeHandoffs", {}).get(proj_id) == rid:
                    st_obj["activeHandoffs"].pop(proj_id, None)
                elif original_won and st_obj.get("activeHandoffs", {}).get(proj_id) == successor:
                    st_obj["activeHandoffs"].pop(proj_id, None)
                    succ = st_obj.setdefault("sentRelays", {}).get(successor)
                    if isinstance(succ, dict) and not succ.get("acked"):
                        succ["withdrawn"] = {"reason": "original_claimed", "by": rid, "at": L.now_iso()}

            _update_relay_state(base, _mark_state)
            synced.append(
                {
                    "relayId": rid,
                    "projectId": proj_id,
                    "toAlias": to_alias,
                    "toDeviceId": to_did,
                    "receivedAt": acked_at,
                    "late": is_late,
                }
            )

    return synced


# ---------------------------------------------------------------- 4. publish_project & sync_network


def _build_project_metadata(
    proj_work: Dict[str, Any],
    proj_path: str,
    here_id: str,
    dest_alias: str = "",
    relay_id: str = "",
    parent_id: str = "",
    from_device_id: str = "",
    to_device_id: str = "",
) -> Dict[str, Any]:
    handoff_md_text = ""
    handoff_md_file = Path(proj_path) / "HANDOFF.md"
    if handoff_md_file.is_file():
        try:
            handoff_md_text = handoff_md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            handoff_md_text = ""

    return {
        "id": proj_work["id"],
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
        "pathsByDeviceId": copy.deepcopy(proj_work.get("pathsByDeviceId") or {}),
        "fromAlias": here_id,
        "fromDeviceId": from_device_id,
        "fromPath": proj_path,
        "toAlias": dest_alias,
        "toDeviceId": to_device_id,
        "relayId": relay_id,
        "parentRelayId": parent_id,
    }


def publish_project(
    project_key: Optional[str] = None,
    cwd: Optional[str] = None,
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build a receiver-independent snapshot, update lineage DAG, cache locally, and sync to v3 peers."""
    ctx = _require_paired_context(ledger_path, local, local_home, require_any_peer=False)
    base, led_path, _local_cfg, _led0, here_id, cloud, identity, provider = ctx

    with _workspace_operation(base):
        conv_mod = _conversations_mod()
        pkg_mod = _packager_mod()

        sync_receipts(provider, identity, ledger_path=led_path, local=_local_cfg, local_home=base)
        led = L.load(led_path)
        proj = L.find_project(led, project_key, cwd or os.getcwd())
        proj_id = proj["id"]

        state = _load_relay_state(base)
        pending = (state.get("pendingSends") or {}).get(proj_id) or {}
        active_id = (state.get("activeHandoffs") or {}).get(proj_id)
        outstanding = next((rid for rid, info in (state.get("sentRelays") or {}).items()
                            if info.get("projectId") == proj_id and not info.get("acked")
                            and not DS.handoff_superseded(info)), "")
        blocked_id = pending.get("relayId") or active_id or outstanding
        if blocked_id or proj.get("handoff"):
            raise ActiveHandoffConflictError(
                "项目已有待完成的交接（%s），暂不能发布新快照；本机新增编辑已保留。" % (blocked_id or "手动接力"),
                relay_id=str(blocked_id or ""),
            )

        if proj.get("device") != here_id:
            owner_cfg = L.dev(led["config"], proj.get("device"))
            raise RelayError(
                "项目「%s」的最新现场登记在 %s（%s），不在本机（%s）。请从那台设备发布快照。"
                % (proj["name"], owner_cfg.get("name", proj.get("device")), proj.get("device"), here_id),
                code="wrong_device",
            )

        raw_proj_path = _project_path_for_device(proj, here_id, identity["deviceId"])
        if not raw_proj_path:
            raise RelayError(
                "项目「%s」在本机（%s）尚未登记路径，请先运行 relay set %s --path-here <目录>。"
                % (proj["name"], here_id, proj_id),
                code="missing_path",
            )
        proj_path = _canon_local_path(raw_proj_path)
        if not os.path.isdir(proj_path):
            raise RelayError("项目「%s」的本机目录不存在：%s" % (proj["name"], proj_path), code="missing_path")

        user_h = os.environ.get("RELAY_DESK_USER_HOME") or None
        facts = scan.git_facts(proj_path)
        pres = scan.presence(proj_path, home=user_h)

        proj_work = copy.deepcopy(proj)
        proj_work.setdefault("paths", {})[here_id] = proj_path
        proj_work.setdefault("pathsByDeviceId", {})[identity["deviceId"]] = proj_path
        _apply_local_facts_to_project(proj_work, here_id, facts, pres)

        conv_bundle = conv_mod.collect_project_conversations(proj_work, here_id, base, user_home=user_h)
        _merge_archive_checked(conv_mod, base, proj_id, conv_bundle)
        pkg_mod.scan_secrets_before_pack(proj_path, is_git=facts.get("isGit"))

        state = _load_relay_state(base)
        cur_ver = str((state.get("localCurrentVersion") or {}).get(proj_id) or "")
        parent_relay_id = str(proj.get("lastRelayId") or "")
        day_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        version_id = L.new_id("snap_%s_" % day_str)
        created_at = L.now_iso()
        session_count = len(conv_bundle.get("entries") or [])

        metadata = _build_project_metadata(
            proj_work,
            proj_path,
            here_id,
            from_device_id=identity["deviceId"],
            parent_id=parent_relay_id,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tar_path = os.path.join(tmpdir, "payload.tar.gz")
            pack_info = pkg_mod.build_payload(
                proj_path,
                tar_path,
                metadata,
                conv_bundle,
                target_os=None,
            )
            payload_sha, payload_size = _sha256_file_info(tar_path)
            snapshot_unsigned = {
                "formatVersion": 3,
                "projectId": proj_id,
                "projectName": proj_work["name"],
                "versionId": version_id,
                "authorDeviceId": identity["deviceId"],
                "authorAlias": here_id,
                "parentVersionIds": [cur_ver] if cur_ver else [],
                "parentRelayId": parent_relay_id,
                "relayId": "",
                "contentSha256": payload_sha,
                "contentSize": payload_size,
                "createdAt": created_at,
            }
            signed_snapshot = crypto.sign_snapshot(
                snapshot_unsigned,
                identity["signPrivateKey"],
                author_device_id=identity["deviceId"],
            )
            meta_plain = {
                "projectId": proj_id,
                "projectName": proj_work["name"],
                "lastTask": proj_work.get("lastTask") or "",
                "nextAction": proj_work.get("nextAction") or "",
                "branch": pack_info.get("branch") or proj_work.get("branch") or "",
                "dirty": proj_work.get("dirty", 0),
                "sessionCount": session_count,
            }
            # Fix H: before chaining onto the local head, make sure the cloud holds the signed record
            # of every own ancestor not yet confirmed there (a publish whose record upload failed
            # leaves its snapshot as the local head with ``recordPublished`` unset). Pre-fix, the next
            # snapshot named such a parent and receivers reported lineage_incomplete for good.
            if cur_ver:
                _republish_own_records(provider, base, identity, proj_id, [cur_ver])
            _save_snapshot_to_cache(base, version_id, signed_snapshot, tar_path, metadata, meta_plain)

            baseline = {
                "headCommit": pack_info.get("headCommit") or "",
                "folderHashes": pack_info.get("folderHashes") or {},
                "gitSnapshotHash": _compute_git_baseline_fingerprint(proj_path) if pack_info["mode"] == "git" else "",
            }

            def _upd_state(st_obj: Dict[str, Any]) -> None:
                _record_version(
                    st_obj,
                    proj_id,
                    {
                        "versionId": version_id,
                        "projectId": proj_id,
                        "authorDeviceId": identity["deviceId"],
                        "parentVersionIds": [cur_ver] if cur_ver else [],
                        "parentRelayId": parent_relay_id,
                        "relayId": "",
                        "contentSha256": payload_sha,
                        "createdAt": created_at,
                        "recordSha256": crypto.snapshot_record_digest(signed_snapshot),
                        "trust": "current",
                        "authorKeyFingerprint": DS.norm_fp(_identity_fingerprint(identity)),
                    },
                )
                st_obj.setdefault("localCurrentVersion", {})[proj_id] = version_id
                st_obj.setdefault("projectBaselines", {})[proj_id] = baseline

            _update_relay_state(base, _upd_state)

            def _upd_led(l: Dict[str, Any]) -> None:
                for p in l.get("projects") or []:
                    if p.get("id") != proj_id:
                        continue
                    p.setdefault("paths", {})[here_id] = proj_path
                    p.setdefault("pathsByDeviceId", {})[identity["deviceId"]] = proj_path
                    _apply_local_facts_to_project(p, here_id, facts, pres)
                    # Keep device ownership unchanged on publish!
                    p["device"] = here_id
                    L.touch(p, here_id)
                migrated = devices.migrate_ledger_identities(l, local_ident=identity, cloud_cfg=cloud)
                l.clear()
                l.update(migrated)

            L.update(led_path, _upd_led)
            _put_immutable_json(provider, _snapshot_record_key(version_id), signed_snapshot)
            _mark_records_published(base, proj_id, [version_id])
            _refresh_have(provider, base, identity)

            delivered_to: List[Dict[str, str]] = []
            delivery_errors: List[Dict[str, str]] = []
            # Phase C: one delivery per (peer, peer key, snapshot), recorded in deliveries.json,
            # so later sync rounds do not send this snapshot to the same key again.
            book = _delivery_book(base, identity)
            outbound = _Outbound(provider, identity, here_id, base, book, cloud)
            outbound.verified_records.add(version_id)  # written and read back above
            item = {"versionId": version_id, "snapshot": signed_snapshot, "tarPath": tar_path,
                    "metaPlain": meta_plain, "metadata": metadata}
            for peer in _paired_active_peers(cloud):
                if not _peer_is_v3(peer):
                    continue
                delivery_id = DS.delivery_id_for(version_id, identity["deviceId"], peer.get("keyFingerprint"), 1, True)
                try:
                    res = outbound.deliver(peer, item, have=None, allow_cover=False, adopt_legacy=False)
                    delivery_id = str(res.get("deliveryId") or delivery_id)
                except Exception as e:
                    # S2: the snapshot is already cached, indexed and listed in the have
                    # manifest; one peer's failure must not skip the remaining peers.
                    # A later `relay sync` forwards the snapshot to this peer.
                    classified = SR.classify_error(e, "publish")
                    delivery_errors.append(
                        {
                            "peerDeviceId": peer["deviceId"],
                            "alias": str(peer.get("alias") or ""),
                            "deliveryId": delivery_id,
                            "error": classified.message,
                            "errorClass": classified.error_class,
                            "code": classified.code,
                        }
                    )
                    continue
                delivered_to.append(
                    {
                        "deviceId": peer["deviceId"],
                        "alias": str(peer.get("alias") or ""),
                        "deliveryId": delivery_id,
                    }
                )

    return {
        "ok": True,
        "snapshotId": version_id,
        "versionId": version_id,
        "parentVersionIds": signed_snapshot["parentVersionIds"],
        "parentRelayId": parent_relay_id,
        "projectId": proj_id,
        "projectName": proj_work["name"],
        "device": here_id,
        "deviceId": identity["deviceId"],
        "contentSha256": payload_sha,
        "contentSize": payload_size,
        "sessionCount": session_count,
        "excludedFiles": pack_info.get("excludedFiles") or [],
        "deliveredTo": delivered_to,
        "deliveryErrors": delivery_errors,
        "status": SR.completion_status(delivery_errors),
        "message": (
            "项目快照已发布并同步：%s（版本 %s）" % (proj_work["name"], version_id)
            if not delivery_errors
            else "项目快照已发布：%s（版本 %s），但推送到 %d 台设备失败；稍后运行 relay sync 会继续投递。"
            % (proj_work["name"], version_id, len(delivery_errors))
        ),
    }


# ---------------------------------------------------------------- continuous sync session (Phase B)

import collections as _collections  # noqa: E402
import random as _random  # noqa: E402
import threading as _threading  # noqa: E402

HAVE_REFRESH_SEC = 900.0          # re-upload an unchanged have manifest at most this often
PROBE_MIN_INTERVAL_SEC = 600.0    # descriptor probe per peer after a signature failure
STATUS_WRITE_MIN_SEC = 10.0       # sync_status.json at most this often while watching
STATUS_HEARTBEAT_SEC = 60.0       # ... and at least this often (heartbeat)
_LEGACY_LISTS = ("syncedReceipts", "syncedSnapshots", "forwarded", "conflictsDetected", "errors", "lineageUnproven")


class SyncCancelled(BaseException):
    """Cancellation requested; raised between phases so the per-round lock unwinds normally."""


class RoundOutcome(object):
    """What one :meth:`SyncSession.step` did, for scheduling the next round."""

    __slots__ = ("busy", "global_error", "changed", "fatal", "aborted")

    def __init__(self) -> None:
        self.busy = False
        self.global_error = ""
        self.changed = False
        self.fatal = False
        self.aborted = False

    def as_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}


def _object_tag(info: Any) -> Tuple[Any, ...]:
    if info is None:
        return (None,)
    return (getattr(info, "size", None), getattr(info, "etag", None), getattr(info, "sha256", None),
            getattr(info, "mtime", None))


def _file_tag(path: str) -> Tuple[Any, ...]:
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return (None,)


def _peer_trust_tuple(peer: Dict[str, Any]) -> Tuple[Any, ...]:
    return (str(peer.get("status") or ""), bool(peer.get("paired")), str(peer.get("keyFingerprint") or ""),
            str(peer.get("verifyPublicKey") or ""), str(peer.get("boxPublicKey") or ""),
            tuple(sorted(str(c) for c in (peer.get("capabilities") or []))))


def _connection_tuple(cloud: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(str(cloud.get(k) or "") for k in ("provider", "remote", "root", "configPath", "rcloneBin", "timeout"))


def _identity_fingerprint(identity: Dict[str, Any]) -> str:
    fp = str(identity.get("keyFingerprint") or "")
    if not fp:
        fp = crypto.device_fingerprint(identity["verifyPublicKey"], identity["boxPublicKey"])
    return fp


def _peer_label(peer: Optional[Dict[str, Any]], did: str) -> str:
    alias = str((peer or {}).get("alias") or "")
    return "「%s」（%s）" % (alias, did) if alias else did


class SyncSession(object):
    """One sync run (one-shot, CLI watch or server worker), driven round by round.

    Each :meth:`step` re-reads identity, cloud config and relay state through the
    read-only loaders, takes the workspace lock for that round only, then runs
    receipts, inbound, have publication and forwarding with per-peer isolation.
    Results are reported as events (``on_event`` is called synchronously) and
    folded into bounded recent lists and exact cumulative counters.
    """

    def __init__(self, project_key: Optional[str] = None, cwd: Optional[str] = None,
                 ledger_path: Optional[Any] = None, local: Optional[Dict[str, Any]] = None,
                 local_home: Optional[Any] = None, *, interval: float = 5.0, max_backoff: float = 300.0,
                 on_event=None, clock=None, rng=None, cancel=None, event_limit: int = SR.DEFAULT_EVENT_LIMIT,
                 mode: str = "watch", status_file: bool = False, stats_every: Optional[float] = None,
                 have_refresh: float = HAVE_REFRESH_SEC, _ctx=None) -> None:
        if _ctx is None:
            ctx = _require_paired_context(ledger_path, local, local_home, require_any_peer=False)
            base, led_path, local_cfg, led, here_id, cloud, identity, provider = ctx
            # Damaged relay state stops the run before the first round, as before.
            load_relay_state(base)
            target: Optional[str] = None
            if project_key:
                try:
                    target = L.find_project(led, project_key, cwd or os.getcwd())["id"]
                except LookupError:
                    target = str(project_key).strip()
        else:
            target, base, led_path, local_cfg, here_id, cloud, identity, provider = _ctx
        self.base = Path(base)
        self.led_path = led_path
        self.local_cfg = local_cfg
        self.here_id = here_id
        self.target_proj_id = target
        self.identity = dict(identity)
        self.identity["keyFingerprint"] = _identity_fingerprint(identity)
        self.cloud = cloud
        # Fix H: rounds do not retry reads in the round (commands do): a failed round backs off
        # with jitter instead, and the workspace lock is released as early as possible.
        self.provider = _storage_mod().without_retry(provider)
        self.interval = max(0.01, float(interval))
        self.max_backoff = max(self.interval, float(max_backoff))
        self.clock = clock if clock is not None else SR.RealClock()
        self.rng = rng if rng is not None else _random.Random()
        self.cancel = cancel
        self.mode = mode
        self.backoff = SR.Backoff(self.interval, self.max_backoff, self.rng)
        self.health = SR.HealthTable(self.backoff, self.clock)
        self.quarantine = SR.Quarantine(1000)
        self.log = SR.EventLog(limit=event_limit, on_event=on_event, clock=self.clock,
                               on_sink_closed=self._sink_closed)
        self.cycles = 0
        self.global_attempt = 0
        self.cancelled = False
        self.fatal = ""
        self.started_at = self.clock.now_iso()
        self.finished_at = ""
        self.have_refresh = float(have_refresh)
        self._have_digest: Optional[str] = None
        self._have_at = 0.0
        self._probe_at: Dict[str, Tuple[float, str, str]] = {}
        self._conn = _connection_tuple(cloud)
        self._peer_view = {did: _peer_trust_tuple(p) for did, p in (cloud.get("peers") or {}).items()
                           if isinstance(p, dict)}
        self._busy_streak = 0
        self._legacy = {name: _collections.deque(maxlen=SR.LEGACY_LIST_LIMIT) for name in _LEGACY_LISTS}
        self._legacy_total = {name: 0 for name in _LEGACY_LISTS}
        self._round_ok: Set[str] = set()
        self._round_failed: Set[str] = set()
        self._verified_cache: Dict[str, Any] = {}
        self.book: Optional[DS.DeliveryBook] = None   # deliveries.json, opened inside each round's lock
        self._checker: Optional[_CacheChecker] = None
        # Phase F: verified-cache index kept across rounds; unchanged entries are not re-read each round.
        self._cache_index = DS.CacheIndex()
        self._maint_at: Dict[str, float] = {}
        # Fix G: a symlinked cache root found this round (nothing cached can be judged or written),
        # and when each "insufficient evidence" pair was last re-checked (monotonic time, graph size).
        self._root_block: Optional[V.LocalPathError] = None
        self._lineage_tries: Dict[str, Tuple[float, int]] = {}
        self.status_path = (self.base / "sync_status.json") if status_file else None
        self._status_digest: Optional[str] = None
        self._status_at: Optional[float] = None
        self.stats_every = float(stats_every) if stats_every else None
        self._stats_at: Optional[float] = None
        self.log.emit("run_started", mode=mode, interval=self.interval, maxBackoff=self.max_backoff,
                      project=target or "", deviceId=self.identity["deviceId"], pid=os.getpid())

    # ------------------------------------------------------------ driving
    def run(self, watch: bool = False, max_cycles: Optional[int] = None) -> Dict[str, Any]:
        """Run rounds until one-shot completion, ``max_cycles``, a fatal problem or cancellation."""
        try:
            while True:
                out = self.step()
                if out.fatal or not watch:
                    break
                if max_cycles is not None and self.cycles >= int(max_cycles):
                    break
                delay = self.next_delay(out)
                if self.cancel is not None:
                    interrupted = self.clock.sleep(delay, self.cancel)
                else:
                    interrupted = self.clock.sleep(delay)
                if interrupted:
                    self.cancelled = True
                    break
        except (KeyboardInterrupt, SyncCancelled):
            self.cancelled = True
        return self.finish()

    def step(self) -> RoundOutcome:
        out = RoundOutcome()
        self.cycles += 1
        self.log.begin_round(self.cycles)
        self._round_ok = set()
        self._round_failed = set()
        self._verified_cache = {}
        started = self.clock.monotonic()
        retained_before = self._retained_count()
        try:
            self._check_cancel()
            if not self._recheck_trust(out):
                out.aborted = True
            else:
                busy_exc = None
                entered = False
                try:
                    with _workspace_operation(self.base):
                        entered = True
                        with _lock_holder(self.base, "sync"):
                            self._round_body(out)
                except RelayError as e:
                    if entered or e.code != "busy":
                        raise
                    busy_exc = e
                if busy_exc is not None:
                    self._on_busy(busy_exc, out)
                else:
                    self._busy_streak = 0
        except Exception as e:  # anything outside the per-item handlers aborts this round only
            p, _ = self._report("round||", e, "round")
            out.global_error = p["errorClass"]
            out.aborted = True
        self._finish_round(out, started, retained_before)
        return out

    def next_delay(self, out: RoundOutcome) -> float:
        """Seconds until the next round: interval on success, capped jittered backoff on global failure."""
        if out.busy or out.global_error == SR.LOCAL_BUSY:
            return self.interval * (1.0 + 0.25 * self.rng.random())
        if out.global_error == SR.TRANSIENT:
            self.global_attempt += 1
            delay = self.backoff.delay(self.global_attempt)
            self.log.bump("retriesScheduled")
            self.log.emit("backoff", retain=(self.global_attempt == 1), scope="global",
                          attempt=self.global_attempt, retryInSec=round(delay, 3))
            return delay
        self.global_attempt = 0
        if out.global_error:
            # needs_user_action / integrity: re-check slowly, never at transport cadence
            return self.max_backoff
        return self.interval

    def finish(self) -> Dict[str, Any]:
        self.finished_at = self.clock.now_iso()
        summary = self.summary()
        brief = {k: v for k, v in summary.items() if k != "recentEvents"}
        self.log.emit("summary", retain=False, summary=brief)
        self._write_status(summary, running=False)
        return summary

    # ------------------------------------------------------------ reporting helpers
    def _retained_count(self) -> int:
        c = self.log.counters
        return int(c["eventsEmitted"]) - int(c["eventsNotRetained"])

    def _sink_closed(self) -> None:
        if self.cancel is None:
            self.cancel = _threading.Event()
        self.cancel.set()

    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise SyncCancelled()

    def _legacy_add(self, name: str, item: Dict[str, Any]) -> None:
        with self.log.lock:
            self._legacy[name].append(item)
            self._legacy_total[name] += 1

    def _report(self, key: str, exc_or_classified: Any, stage: str, peer: str = "", **ctx: Any):
        if isinstance(exc_or_classified, SR.ClassifiedError):
            classified = exc_or_classified
        else:
            classified = SR.classify_error(exc_or_classified, stage)
        for field, label in (("deliveryId", "投递"), ("relayId", "交接"), ("snapshotId", "快照")):
            ident = ctx.get(field)
            if ident and str(ident) not in classified.message:
                # Name the object: messages such as a JSON parse error say nothing about which one failed.
                classified = SR.ClassifiedError(classified.error_class, classified.code,
                                                "%s %s：%s" % (label, ident, classified.message), classified.action)
                break
        problem, opened = self.log.report_problem(key, classified, peer=peer, stage=stage, **ctx)
        if opened:
            item = {"error": problem["message"], "errorClass": problem["errorClass"], "code": problem["code"],
                    "stage": stage}
            if peer:
                item["peerDeviceId"] = peer
            for k in ("deliveryId", "snapshotId", "relayId"):
                if ctx.get(k):
                    item[k] = ctx[k]
            self._legacy_add("errors", item)
        return problem, opened

    def _peer_state_event(self, did: str, before: str, after: str, **extra: Any) -> None:
        if before != after:
            self.log.emit("peer_state", peerDeviceId=did, **{"from": before, "to": after}, **extra)

    def _item_failure(self, key: str, exc_or_classified: Any, stage: str, peer: str = "",
                      qkey: Optional[str] = None, tag: Any = None, **ctx: Any) -> Dict[str, Any]:
        """Report one failed operation and apply its consequence: backoff, park or quarantine."""
        classified = (exc_or_classified if isinstance(exc_or_classified, SR.ClassifiedError)
                      else SR.classify_error(exc_or_classified, stage))
        trust_peer = str(ctx.pop("trustPeer", "") or peer)
        if peer and classified.peer_level:
            key = "peer|%s|trust" % peer
        problem, _ = self._report(key, classified, stage, peer=peer, **ctx)
        cls = problem["errorClass"]
        if cls == SR.TRANSIENT and peer:
            self._peer_backoff(peer, problem)
        elif cls == SR.NEEDS_ACTION and peer and classified.peer_level:
            self._park(peer, problem)
            if qkey:
                self.quarantine.put(qkey, tag, problem["key"], peer=peer)
        elif cls in (SR.INTEGRITY, SR.NEEDS_ACTION):
            if peer:
                self._round_failed.add(peer)
            if qkey:
                self.quarantine.put(qkey, tag, problem["key"], peer=trust_peer)
        return problem

    def _peer_backoff(self, peer: str, problem: Dict[str, Any]) -> None:
        before, after, attempt, delay = self.health.fail(peer, problem["errorClass"], problem["code"])
        self._round_failed.add(peer)
        if after == SR.BACKOFF:
            self.log.bump("retriesScheduled")
            self._peer_state_event(peer, before, after, attempt=attempt, retryInSec=round(delay, 3))

    def _park(self, did: str, problem: Dict[str, Any]) -> None:
        before, after = self.health.park(did, problem["errorClass"], problem["code"])
        self._round_failed.add(did)
        self._peer_state_event(did, before, after, code=problem["code"])

    def _global_fail(self, out: RoundOutcome, key: str, exc_or_classified: Any, stage: str = "trust_check") -> bool:
        problem, _ = self._report(key, exc_or_classified, stage)
        out.global_error = problem["errorClass"]
        return False

    def _on_busy(self, exc: BaseException, out: RoundOutcome) -> None:
        out.busy = True
        self._busy_streak += 1
        self._report("lock||", exc, "lock")
        self.log.emit("round_skipped", retain=(self._busy_streak == 1), reason="local_busy", streak=self._busy_streak)

    def _finish_round(self, out: RoundOutcome, started: float, retained_before: int) -> None:
        if out.aborted or out.busy:
            self.log.carry_all()
        if out.aborted:
            self.log.bump("roundsAborted")
        for did in sorted(self._round_ok - self._round_failed):
            before, after = self.health.succeed(did)
            self._peer_state_event(did, before, after)
        self.log.end_round()
        changed = out.changed or self._retained_count() > retained_before
        out.changed = changed
        duration_ms = int(round((self.clock.monotonic() - started) * 1000))
        self.log.emit("round_finished", retain=changed, changed=changed, durationMs=duration_ms,
                      busy=out.busy or None, aborted=out.aborted or None)
        self._maybe_stats()
        self._maybe_write_status()

    # ------------------------------------------------------------ trust recheck (B2.7)
    def _recheck_trust(self, out: RoundOutcome) -> bool:
        """Re-read identity, cloud config and relay state read-only; False aborts the round with no writes."""
        try:
            ident = crypto.load_identity(self.base)
        except Exception as e:
            return self._global_fail(out, "trust|identity|", e)
        if ident is None:
            return self._global_fail(out, "trust|identity|", SR.ClassifiedError(
                SR.NEEDS_ACTION, "identity_missing", "本机身份文件 identity.json 已不存在，同步已暂停。",
                SR.ACTIONS["identity_missing"]))
        if ident.get("deviceId") != self.identity["deviceId"]:
            out.fatal = True
            self.fatal = "identity_replaced"
            return self._global_fail(out, "trust|identity|", SR.ClassifiedError(
                SR.NEEDS_ACTION, "identity_replaced",
                "本机设备身份已被替换（%s → %s），持续同步已停止。" % (self.identity["deviceId"], ident.get("deviceId")),
                SR.ACTIONS["identity_replaced"]))
        new_fp = _identity_fingerprint(ident)
        if new_fp != self.identity["keyFingerprint"]:
            old_fp = self.identity["keyFingerprint"]
            fresh = dict(ident)
            fresh["alias"] = str(ident.get("alias") or self.identity.get("alias") or self.here_id)
            fresh["keyFingerprint"] = new_fp
            self.identity = fresh
            self.log.emit("trust_changed", what="identity", detail={"oldFingerprint": old_fp, "newFingerprint": new_fp})
            self._reset_all_peers()
        try:
            cloud = load_cloud_config(self.base)
        except Exception as e:
            return self._global_fail(out, "trust|cloud|", e)
        if cloud is None or not _is_cloud_configured(cloud):
            return self._global_fail(out, "trust|cloud|", SR.ClassifiedError(
                SR.NEEDS_ACTION, "not_configured", "云存储连接已不存在或未配置，同步已暂停。", "relay pair"))
        conn = _connection_tuple(cloud)
        if conn != self._conn:
            try:
                provider = _storage_mod().make_provider(cloud)
            except Exception as e:
                return self._global_fail(out, "trust|connection|", e)
            self.provider = provider
            self._conn = conn
            self.log.emit("trust_changed", what="connection",
                          detail={"provider": conn[0], "remote": conn[1], "root": conn[2]})
            self._reset_all_peers()
        try:
            load_relay_state(self.base)
        except Exception as e:
            return self._global_fail(out, "trust|relay_state|", e)
        try:
            DS.load(self.base)  # Phase C: a damaged deliveries.json stops the round before any write
        except Exception as e:
            return self._global_fail(out, "trust|deliveries|", e)
        peers = cloud.get("peers") if isinstance(cloud.get("peers"), dict) else {}
        view = {did: _peer_trust_tuple(p) for did, p in peers.items() if isinstance(p, dict)}
        for did in sorted(set(view) | set(self._peer_view)):
            old, new = self._peer_view.get(did), view.get(did)
            if old == new:
                continue
            self.log.emit("trust_changed", what="peer", peerDeviceId=did, detail={
                "fromStatus": old[0] if old else "", "toStatus": new[0] if new else "",
                "keyFingerprint": new[2] if new else ""})
            before, after = self.health.reset(did)
            self.quarantine.clear_peer(did)
            self._probe_at.pop(did, None)
            self._peer_state_event(did, before, after)
        self._peer_view = view
        self.cloud = cloud
        for did, peer in sorted(peers.items()):
            if isinstance(peer, dict) and peer.get("status") == "key_changed":
                problem, _ = self._report("peer|%s|trust" % did, SR.ClassifiedError(
                    SR.NEEDS_ACTION, "peer_key_changed",
                    "对端设备%s密钥已变更，已暂停与它的同步收发。" % _peer_label(peer, did),
                    "relay pair --with %s --accept --fingerprint <核对后的新配对指纹>" % did), "trust_check", peer=did)
                self._park(did, problem)
        return True

    def _reset_all_peers(self) -> None:
        for did, before in self.health.reset_all():
            self._peer_state_event(did, before, SR.HEALTHY)
        self.quarantine.clear()
        self._probe_at.clear()
        self._have_digest = None

    def _triage_signature(self, peer: Optional[Dict[str, Any]], classified: SR.ClassifiedError,
                          created_at: Any = None, stage: str = "") -> SR.ClassifiedError:
        """A peer-signed object failed verification: tell a key rotation apart from tampering.

        Probes the peer's self-signed descriptor (rate-limited). New keys there
        mark the peer ``key_changed`` exactly like ``relay pair`` discovery would
        (``shownFingerprints`` untouched, so ``--accept`` still needs explicit
        confirmation). This only ever reduces trust.
        """
        if not isinstance(peer, dict) or not peer.get("deviceId"):
            return classified
        did = str(peer["deviceId"])
        now = self.clock.monotonic()
        last = self._probe_at.get(did)
        if last is not None and now - last[0] < PROBE_MIN_INTERVAL_SEC and last[1] == classified.code:
            result = last[2]
        else:
            result = ""
            try:
                desc = _download_descriptor(self.provider, "v3/devices/%s.json" % did, default_v3=True)
            except Exception:
                desc = None
            if isinstance(desc, dict) and desc.get("deviceId") == did and not _same_peer_keys(peer, desc):
                result = "rotated"
                self._mark_peer_key_changed(did, peer, desc)
            self._probe_at[did] = (now, classified.code, result)
        if result == "rotated":
            return SR.ClassifiedError(
                SR.NEEDS_ACTION, "peer_key_changed",
                "对端设备%s已发布新的公钥，签名无法用已固定的公钥验证；已暂停与它的同步收发。" % _peer_label(peer, did),
                "relay pair --with %s --accept --fingerprint <核对后的新配对指纹>" % did)
        if _key_history(peer):
            # Phase C: retired keys are recorded for this peer and were already tried
            # (have, ready and receipt checks); nothing known verifies this object.
            return classified
        # Legacy pairings re-confirmed before Phase C have no key history: keep the
        # Phase B label heuristic (unverified time, affects only the label, never acceptance).
        paired_at = str(peer.get("pairedAt") or "")
        if created_at and paired_at and str(created_at) < paired_at:
            if stage == "have_fetch":
                return SR.ClassifiedError(
                    SR.NEEDS_ACTION, "signed_by_retired_key",
                    "对端设备%s的缓存清单仍由换钥前的旧密钥签名，暂不向它转发。" % _peer_label(peer, did),
                    "在对端设备运行一次 relay sync，用新密钥重新签名缓存清单")
            return SR.ClassifiedError(
                SR.NEEDS_ACTION, "signed_by_retired_key",
                "签名无法用对端设备%s当前固定的公钥验证；该记录创建于最近一次确认此公钥之前，"
                "应是对端换钥前用旧密钥签名的投递，未处理。" % _peer_label(peer, did),
                SR.ACTIONS["signed_by_retired_key"])
        return classified

    def _mark_peer_key_changed(self, did: str, peer: Dict[str, Any], desc: Dict[str, Any]) -> None:
        pinned = (peer.get("verifyPublicKey"), peer.get("boxPublicKey"))
        my_desc = crypto.public_descriptor_v3(self.identity, capabilities=_LOCAL_CAPABILITIES)

        def _mark(cl: Dict[str, Any]) -> None:
            existing = (cl.get("peers") or {}).get(did)
            if not isinstance(existing, dict):
                return
            if (existing.get("verifyPublicKey"), existing.get("boxPublicKey")) != pinned or _same_peer_keys(existing, desc):
                return
            existing["paired"] = False
            existing["status"] = "key_changed"
            existing["pendingDescriptor"] = copy.deepcopy(desc)
            existing["pairFingerprint"] = crypto.pair_fingerprint(my_desc, desc)

        try:
            cloud = _update_cloud(self.base, _mark)
        except Exception as e:
            self._report("peer|%s|probe" % did, e, "trust_check", peer=did)
            return
        self.cloud = cloud
        new_peer = (cloud.get("peers") or {}).get(did)
        if isinstance(new_peer, dict):
            self._peer_view[did] = _peer_trust_tuple(new_peer)
        self.log.emit("trust_changed", what="peer", peerDeviceId=did, detail={
            "fromStatus": "paired", "toStatus": "key_changed", "reason": "descriptor_probe",
            "keyFingerprint": str(desc.get("keyFingerprint") or "")})

    # ------------------------------------------------------------ the round
    def _round_body(self, out: RoundOutcome) -> None:
        # Phase C: delivery state (built from local checkpoints on first use) and a
        # per-round cache verifier with a bounded bit-rot re-hash budget.
        self.book = _delivery_book(self.base, self.identity)
        self._checker = _cache_checker(self.base, budget=DS.HashBudget(), index=self._cache_index)
        # Fix G: a symlinked cache/snapshots is the local environment, not the data. Nothing cached
        # can be judged through it, so no cache is "lost", no have is built from it and nothing new
        # is written; the user is asked to fix the directory (needs_user_action), every round.
        self._root_block = self._cache_root_block()
        if self._root_block is not None:
            self._report("local|dir|cache/snapshots", _local_dir_classified(self._root_block), "inbound")
        self._phase_receipts(out)
        self._check_cancel()
        inbox_prefix = "v3/inbox/%s/" % self.identity["deviceId"]
        try:
            objs = self.provider.list_objects(inbox_prefix)
        except Exception as e:
            problem, _ = self._report("inbox_list||", e, "inbox_list")
            out.global_error = problem["errorClass"]
            out.aborted = True
            return
        self._phase_inbound(objs, out)
        self._check_cancel()
        cached_items = _list_cached_snapshots(self.base, checker=self._checker)
        if self._root_block is None:
            self._phase_have(cached_items, out)
        self._check_cancel()
        self._phase_forward(cached_items, out)

    # ------------------------------------------------------------ Fix G: local directory refusals
    def _cache_root_block(self) -> Optional[V.LocalPathError]:
        try:
            V.ensure_contained(self.base, self.base / "cache" / "snapshots", check_final=True, field="cache",
                               local=True)
        except V.LocalPathError as e:
            return e
        except V.ValidationError:
            return None
        return None

    def _local_block(self, snap_id: str, proj_id: str) -> Optional[V.LocalPathError]:
        """The local directory problem that would refuse writing this snapshot, if any (checked before download)."""
        try:
            if snap_id:
                _snapshot_cache_dir(self.base, snap_id)
            if proj_id:
                _archive_dir_checked(self.base, proj_id)
        except V.LocalPathError as e:
            return e
        except V.ValidationError:
            return None  # an invalid identifier belongs to the delivery; the normal path reports it
        return None

    def _block_local(self, book: "DS.DeliveryBook", deliv_id: str, exc: V.LocalPathError, **fields: Any) -> None:
        """Park a delivery refused only by a local directory: needs the user, retried from the same delivery.

        Not an integrity failure and not quarantined: nothing is requested from
        peers (``blocked_local`` is not a needs state). Once the directory is
        fixed the next round processes the same immutable cloud delivery; if the
        delivery has left the inbox by then, it becomes ``repair_pending``.
        """
        classified = _local_dir_classified(exc)
        problem, _ = self._report("inbound||%s" % deliv_id, classified, "inbound", deliveryId=deliv_id,
                                  snapshotId=fields.get("snapshotId") or None)
        record = {"key": problem["key"], "errorClass": classified.error_class, "code": classified.code,
                  "message": classified.message, "action": classified.action}
        prev = book.inbound(deliv_id) or {}
        if (prev.get("state") == "blocked_local" and prev.get("objectTag") == fields.get("objectTag")
                and prev.get("problem") == record):
            return  # unchanged: no rewrite of deliveries.json every round
        book.set_inbound(deliv_id, state="blocked_local", reason="local_dir_symlink", problem=record, **fields)

    def _mark_cached(self, book: "DS.DeliveryBook", deliv_id: str, **fields: Any) -> None:
        """A delivery judged before is now cached: its old problem and reason no longer apply."""
        def fn(st: Dict[str, Any]) -> None:
            ent = st["inbound"].setdefault(deliv_id, {})
            ent.pop("problem", None)
            ent.pop("reason", None)
            ent.update({k: v for k, v in fields.items() if v is not None})
            ent["state"] = "cached"
            ent["at"] = L.now_iso()

        book.mutate(fn)

    def _phase_receipts(self, out: RoundOutcome) -> None:
        def skip(rid: str, did: str) -> bool:
            if did and not self.health.is_due(did):
                self.log.carry_peer(did)
                return True
            held = self.quarantine.match("receipts|%s|%s" % (did, rid), None)
            if held and self.log.carry(held):
                return True
            return False

        def on_error(rid: str, did: str, peer: Any, exc: BaseException) -> None:
            classified = SR.classify_error(exc, "receipts")
            if classified.code == "signature_invalid":
                classified = self._triage_signature(peer, classified)
            self._item_failure("receipts|%s|%s" % (did, rid), classified, "receipts", did,
                               qkey="receipts|%s|%s" % (did, rid), tag=None, relayId=rid)

        synced = sync_receipts(self.provider, self.identity, peer_desc=None, ledger_path=self.led_path,
                               local=self.local_cfg, local_home=self.base, on_error=on_error, skip=skip)
        for rec in synced:
            out.changed = True
            self.log.emit("receipt_synced", relayId=rec.get("relayId"), projectId=rec.get("projectId"),
                          toDeviceId=rec.get("toDeviceId"), late=bool(rec.get("late")))
            self._legacy_add("syncedReceipts", rec)
            if rec.get("toDeviceId"):
                self._round_ok.add(str(rec["toDeviceId"]))

    def _phase_inbound(self, objs: List[Any], out: RoundOutcome) -> None:
        """Process the inbox. Phase C: skip only what is verified-cached, repair the rest, never trust blindly.

        A delivery recorded in ``processedDeliveries`` (or a claimed handoff) is
        skipped only while its snapshot's cache verifies; otherwise it is processed
        again from the inbox (``snapshot_cached.repaired``). Deliveries signed by a
        sender's retired key, or sealed to this device's retired key, are
        ``obsolete``: recorded, never an alarm, and the snapshot is requested again
        through the have's ``needs`` when no good copy is cached. An integrity
        failure after the ready verified quarantines the delivery and also asks for
        the snapshot. When the cache already holds a verified copy, only the missing
        records (graph node, processed marker) are written; nothing is downloaded.
        """
        conv_mod = _conversations_mod()
        pkg_mod = _packager_mod()
        base, identity, provider = self.base, self.identity, self.provider
        book = self.book if self.book is not None else _delivery_book(base, identity)
        checker = self._checker if self._checker is not None else _cache_checker(base)
        peers_map = self.cloud.get("peers") if isinstance(self.cloud.get("peers"), dict) else {}
        state = _load_relay_state(base)
        processed = state.get("processedDeliveries") or {}
        received_relays = state.get("receivedRelays") or {}
        version_graph = state.get("versionGraph") or {}
        # Phase D: payloads a cache prune removed on purpose are not downloaded again,
        # except a project's current version or a graph head (those are never pruned).
        pruned = book.state.get("prunedSnapshots") or {}
        protected = _protected_snapshots(state)
        inbox_prefix = "v3/inbox/%s/" % identity["deviceId"]
        by_delivery: Dict[str, Dict[str, Any]] = {}
        for obj in objs:
            key = str(obj.key or "").lstrip("/")
            parts = key.split("/")
            if len(parts) == 5 and parts[0] == "v3" and parts[1] == "inbox" and parts[2] == identity["deviceId"] and parts[3]:
                by_delivery.setdefault(parts[3], {})[parts[4]] = obj
        my_fp = _norm_fp(identity["keyFingerprint"])

        with tempfile.TemporaryDirectory() as tmpdir:
            prefetched, superseded = self._handoff_supersessions(by_delivery, processed, received_relays,
                                                                  peers_map, inbox_prefix, tmpdir)
            for deliv_id in sorted(by_delivery.keys()):
                self._check_cancel()
                files = by_delivery[deliv_id]
                if "ready.json" not in files or "package.enc" not in files:
                    continue
                tag = (_object_tag(files.get("ready.json")), _object_tag(files.get("package.enc")))
                tag_s = json.dumps(tag, default=str)
                proc = processed.get(deliv_id) if isinstance(processed.get(deliv_id), dict) else None
                rr = received_relays.get(deliv_id) if isinstance(received_relays.get(deliv_id), dict) else None
                claimed = bool(rr and rr.get("receiptUploaded"))
                inb = book.inbound(deliv_id)
                hint = str((proc or {}).get("snapshotId") or ((rr or {}).get("result") or {}).get("snapshotId")
                           or (inb or {}).get("snapshotId") or "")
                done_before = bool(proc) or claimed or bool(inb and inb.get("state") == "cached")
                if inb and inb.get("state") == "blocked_local":
                    # Fix G: refused earlier only because a local directory was a symlink.
                    if hint and checker.ok(hint):
                        self._mark_cached(book, deliv_id)  # cached meanwhile (e.g. another delivery)
                        continue
                    if inb.get("objectTag") == tag_s:
                        still = self._local_block(hint, str(inb.get("projectId") or ""))
                        if still is not None:
                            self._block_local(book, deliv_id, still, purpose=inb.get("purpose"), snapshotId=hint,
                                              projectId=inb.get("projectId"),
                                              senderDeviceId=inb.get("senderDeviceId"), objectTag=tag_s)
                            continue
                    # the directory is fixed: process the same delivery from the cloud again
                if self._root_block is not None and done_before and hint:
                    continue  # its cache cannot be judged through the symlinked root (reported once per round)
                if (deliv_id in superseded and inb and inb.get("purpose") == "handoff"
                        and inb.get("state") == "obsolete" and inb.get("reason") != "superseded"):
                    # Phase D: replaced by an explicit re-issue: information, no longer a problem.
                    self._mark_superseded(book, deliv_id, superseded[deliv_id], inb, tag_s)
                    continue
                if hint and (done_before or (inb or {}).get("state") in DS.NEEDS_STATES) and checker.ok(hint):
                    continue  # a verified copy is cached: nothing to do (and nothing left to ask for)
                if hint and done_before and hint in pruned and hint not in protected:
                    continue  # removed on purpose by `relay cache prune` (Phase D): not downloaded again
                if done_before and not hint:
                    continue  # pre-Phase-C record without a snapshot ID: nothing to check it against
                pkey = "inbound||%s" % deliv_id
                qkey = "inbound|%s" % deliv_id
                if inb and inb.get("state") in ("obsolete", "quarantined") and inb.get("objectTag") == tag_s:
                    problem = inb.get("problem")
                    if isinstance(problem, dict) and problem.get("key"):
                        if not self.log.carry(str(problem["key"])):
                            ctx = {"deliveryId": deliv_id}
                            if inb.get("snapshotId"):
                                ctx["snapshotId"] = str(inb["snapshotId"])
                            self._report(str(problem["key"]), SR.ClassifiedError(
                                str(problem.get("errorClass") or SR.INTEGRITY), str(problem.get("code") or "tamper"),
                                str(problem.get("message") or ""), str(problem.get("action") or "")),
                                "inbound", peer=str(inb.get("senderDeviceId") or ""), **ctx)
                    continue  # unchanged object already judged: never downloaded or re-reported again
                repairing = done_before
                held = self.quarantine.match(qkey, tag)
                if held and self.log.carry(held):
                    continue
                try:
                    V.validate_delivery_id(deliv_id)
                except V.ValidationError as e:
                    self._item_failure(pkey, e, "inbound", "", qkey, tag, deliveryId=deliv_id)
                    continue

                ready_key = "%s%s/ready.json" % (inbox_prefix, deliv_id)
                pkg_key = "%s%s/package.enc" % (inbox_prefix, deliv_id)
                ready_tmp = os.path.join(tmpdir, "%s_ready.json" % deliv_id)
                try:
                    if deliv_id in prefetched:
                        raw_ready = prefetched[deliv_id]
                    else:
                        provider.download(ready_key, ready_tmp)
                        with open(ready_tmp, "r", encoding="utf-8-sig") as fh:
                            raw_ready = json.load(fh)
                except Exception as e:
                    self._item_failure(pkey, e, "inbound", "", qkey, tag, deliveryId=deliv_id)
                    continue
                if not isinstance(raw_ready, dict):
                    self._item_failure(pkey, SR.ClassifiedError(
                        SR.INTEGRITY, "remote_json_invalid", "交接记录 %s 不是 JSON 对象。" % deliv_id),
                        "inbound", "", qkey, tag, deliveryId=deliv_id)
                    continue

                sender_did = str(raw_ready.get("senderDeviceId") or raw_ready.get("fromDeviceId") or "")
                sender_peer = peers_map.get(sender_did)
                if not isinstance(sender_peer, dict) or not sender_peer.get("paired") or sender_peer.get("status") != "paired":
                    if isinstance(sender_peer, dict) and sender_peer.get("status") == "key_changed":
                        self.log.carry_peer(sender_did)  # parked by the trust check, reported once there
                        continue
                    self._item_failure(pkey, SR.ClassifiedError(
                        SR.NEEDS_ACTION, "untrusted_sender",
                        "收到尚未确认配对的设备 %s 的投递，未处理。" % (sender_did or "（未知）"),
                        ("relay pair --with %s" % sender_did) if sender_did else "relay pair --status"),
                        "inbound", sender_did, qkey, tag, deliveryId=deliv_id)
                    continue
                if not self.health.is_due(sender_did):
                    self.log.carry_peer(sender_did)
                    continue

                # The sender's current key, or one it retired since (the delivery is then obsolete).
                retired_sender: Optional[Dict[str, Any]] = None
                try:
                    try:
                        verified_ready = crypto.verify_json(raw_ready, sender_peer["verifyPublicKey"],
                                                            expected_device_id=sender_did)
                    except crypto.CryptoError:
                        retired_sender = _verify_with_keys(raw_ready, _key_history(sender_peer), sender_did)
                        if retired_sender is None:
                            raise
                        verified_ready = crypto.verify_json(raw_ready, retired_sender["verifyPublicKey"],
                                                            expected_device_id=sender_did)
                    purpose = str(verified_ready.get("purpose") or "sync")
                    if purpose in ("sync", "handoff"):
                        _require_consistent_ready(verified_ready, deliv_id, sender_did, identity["deviceId"],
                                                  pkg_key, purpose)
                except crypto.CryptoError as e:
                    classified = SR.classify_error(e, "inbound")
                    if classified.code == "signature_invalid":
                        classified = self._triage_signature(sender_peer, classified, raw_ready.get("createdAt"))
                    self._item_failure(pkey, classified, "inbound", sender_did, qkey, tag, deliveryId=deliv_id)
                    continue

                if purpose not in ("sync", "handoff"):
                    continue  # not ours to process; not a problem
                snap_raw = verified_ready.get("snapshot")
                if not isinstance(snap_raw, dict):
                    self._item_failure(pkey, SR.ClassifiedError(
                        SR.INTEGRITY, "remote_json_invalid", "投递 %s 缺少签名快照。" % deliv_id),
                        "inbound", sender_did, qkey, tag, deliveryId=deliv_id)
                    continue

                author_did = str(snap_raw.get("authorDeviceId") or "")
                try:
                    verified_snap, proof = _verify_author_snapshot(snap_raw, identity, self.cloud, version_graph,
                                                                   use="cache")
                except RelayError as e:
                    self._item_failure(pkey, SR.ClassifiedError(
                        SR.NEEDS_ACTION, e.code or "untrusted_author", e.message,
                        "relay pair --with %s" % author_did if author_did else ""),
                        "inbound", sender_did, qkey, tag, deliveryId=deliv_id, authorDeviceId=author_did,
                        trustPeer=author_did)
                    continue
                except crypto.CryptoError as e:
                    classified = SR.classify_error(e, "inbound")
                    author_peer = peers_map.get(author_did) if author_did != identity["deviceId"] else None
                    if classified.code == "signature_invalid" and isinstance(author_peer, dict):
                        classified = self._triage_signature(author_peer, classified, snap_raw.get("createdAt"))
                    self._item_failure(pkey, classified, "inbound", sender_did, qkey, tag, deliveryId=deliv_id,
                                       authorDeviceId=author_did)
                    continue

                snap_id = str(verified_snap["versionId"])
                snap_proj_id = str(verified_snap["projectId"])
                if self.target_proj_id and snap_proj_id != self.target_proj_id:
                    continue

                recipient_fp = _norm_fp(str(verified_ready.get("recipientKeyFingerprint") or ""))
                obsolete_reason = ""
                if retired_sender is not None:
                    obsolete_reason = "sender_key_retired"
                elif recipient_fp and recipient_fp != my_fp:
                    # Signed by a trusted sender for a key this device no longer holds (C03).
                    obsolete_reason = "recipient_key_retired"
                if obsolete_reason:
                    if purpose == "handoff" and rr is None and deliv_id in superseded:
                        self._mark_superseded(book, deliv_id, superseded[deliv_id], {
                            "snapshotId": snap_id, "projectId": snap_proj_id, "senderDeviceId": sender_did}, tag_s)
                        self._round_ok.add(sender_did)
                        continue
                    if purpose == "handoff" and rr is None:
                        # An unclaimed handoff nobody can open any more: only its sender can re-issue it.
                        code = "handoff_signed_by_retired_key" if retired_sender is not None else "sealed_to_retired_key"
                        msg = ("交接 %s 由发送方换钥前的旧密钥签名，无法领取；原交接包保持不变。" % deliv_id
                               if retired_sender is not None else
                               "交接 %s 是发给本机换钥前公钥的，无法领取；原交接包保持不变。" % deliv_id)
                        classified = SR.ClassifiedError(SR.NEEDS_ACTION, code, msg, SR.ACTIONS.get(code, ""))
                        problem = self._item_failure(pkey, classified, "inbound", sender_did, qkey, tag,
                                                     deliveryId=deliv_id, snapshotId=snap_id)
                        book.set_inbound(deliv_id, state="obsolete", reason=obsolete_reason, purpose=purpose,
                                         snapshotId=snap_id, projectId=snap_proj_id, senderDeviceId=sender_did,
                                         objectTag=tag_s, recipientKeyFingerprint=recipient_fp,
                                         problem={"key": problem["key"], "errorClass": classified.error_class,
                                                  "code": classified.code, "message": classified.message,
                                                  "action": classified.action})
                        continue
                    book.set_inbound(deliv_id, state="obsolete", reason=obsolete_reason, purpose=purpose,
                                     snapshotId=snap_id, projectId=snap_proj_id, senderDeviceId=sender_did,
                                     objectTag=tag_s, recipientKeyFingerprint=recipient_fp)
                    self.log.emit("delivery_obsolete", deliveryId=deliv_id, snapshotId=snap_id, projectId=snap_proj_id,
                                  senderDeviceId=sender_did, reason=obsolete_reason, needed=not checker.ok(snap_id))
                    self._round_ok.add(sender_did)
                    continue

                if purpose == "handoff" and not claimed and checker.ok(snap_id):
                    # Already cached earlier; leave the handoff claimable without touching receipt/processedDeliveries
                    self._round_ok.add(sender_did)
                    continue

                if checker.ok(snap_id):
                    # The verified payload is already cached (another delivery, or a crash after the cache
                    # write): record only what is missing; nothing is downloaded.
                    self._record_inbound(deliv_id, snap_id, snap_proj_id, verified_snap, proof, purpose,
                                         author_did, sender_did, tag_s)
                    self._round_ok.add(sender_did)
                    continue

                local_block = self._local_block(snap_id, snap_proj_id)
                if local_block is not None:
                    # Fix G: checked before anything is downloaded; the delivery itself is fine.
                    self._block_local(book, deliv_id, local_block, purpose=purpose, snapshotId=snap_id,
                                      projectId=snap_proj_id, senderDeviceId=sender_did, objectTag=tag_s)
                    self._round_ok.add(sender_did)
                    continue

                enc_tmp = os.path.join(tmpdir, "%s_pkg.enc" % deliv_id)
                tar_tmp = os.path.join(tmpdir, "%s_payload.tar.gz" % deliv_id)
                staging_tmp = os.path.join(tmpdir, "%s_staging" % deliv_id)
                try:
                    provider.download(pkg_key, enc_tmp)
                    crypto.decrypt_package(enc_tmp, tar_tmp, verified_ready, identity, sender_peer)
                    actual_sha, _ = _sha256_file_info(tar_tmp)
                    if actual_sha.lower() != str(verified_snap["contentSha256"]).lower():
                        raise crypto.TamperError("快照 %s 载荷 SHA-256 与作者签名摘要不一致。" % snap_id)
                    extracted = pkg_mod.extract_and_verify_payload(tar_tmp, staging_tmp)
                    meta_plain = crypto.decrypt_meta(verified_ready.get("meta") or {}, identity, sender_peer)
                    _require_same_project(snap_proj_id, extracted["metadata"], extracted["conversations"],
                                          meta_plain, "快照 %s" % snap_id)
                    # Check every destination before the first write.
                    _snapshot_cache_dir(base, snap_id)
                    _archive_dir_checked(base, snap_proj_id)
                except Exception as e:
                    if isinstance(e, V.LocalPathError):  # a local directory turned into a symlink meanwhile
                        self._block_local(book, deliv_id, e, purpose=purpose, snapshotId=snap_id,
                                          projectId=snap_proj_id, senderDeviceId=sender_did, objectTag=tag_s)
                        continue
                    classified = SR.classify_error(e, "inbound")
                    problem = self._item_failure(pkey, classified, "inbound", sender_did, qkey, tag,
                                                 deliveryId=deliv_id, snapshotId=snap_id)
                    if problem["errorClass"] == SR.INTEGRITY:
                        # The verified ready names the snapshot: ask the network for a good copy.
                        book.set_inbound(deliv_id, state="quarantined", purpose=purpose, snapshotId=snap_id,
                                         projectId=snap_proj_id, senderDeviceId=sender_did, objectTag=tag_s,
                                         problem={"key": problem["key"], "errorClass": problem["errorClass"],
                                                  "code": problem["code"], "message": problem["message"],
                                                  "action": problem.get("action") or ""})
                    continue
                finally:
                    for leftover in (enc_tmp,):
                        try:
                            os.unlink(leftover)
                        except OSError:
                            pass

                try:
                    _save_snapshot_to_cache(base, snap_id, snap_raw, tar_tmp, extracted["metadata"], meta_plain)
                    checker.note(snap_id, True, proof.get("recordSha256", ""))
                    _merge_archive_checked(conv_mod, base, snap_proj_id, extracted["conversations"])
                except Exception as e:
                    if isinstance(e, V.LocalPathError):
                        self._block_local(book, deliv_id, e, purpose=purpose, snapshotId=snap_id,
                                          projectId=snap_proj_id, senderDeviceId=sender_did, objectTag=tag_s)
                        continue
                    self._item_failure(pkey, e, "inbound", "", qkey if not isinstance(e, OSError) else None, tag,
                                       deliveryId=deliv_id, snapshotId=snap_id)
                    continue
                finally:
                    _rmtree_quiet(staging_tmp)
                    try:
                        os.unlink(tar_tmp)
                    except OSError:
                        pass
                workspace_tx.maybe_inject_fault("sync_rx_after_cache_save")

                self._record_inbound(deliv_id, snap_id, snap_proj_id, verified_snap, proof, purpose,
                                     author_did, sender_did, tag_s)
                workspace_tx.maybe_inject_fault("sync_rx_after_state_update")
                record = {
                    "deliveryId": deliv_id,
                    "snapshotId": snap_id,
                    "projectId": snap_proj_id,
                    "purpose": purpose,
                    "authorDeviceId": author_did,
                    "senderDeviceId": sender_did,
                    "isForwarded": bool(verified_ready.get("isForwarded")),
                }
                self._legacy_add("syncedSnapshots", record)
                self.log.emit("snapshot_cached", repaired=repairing or None, trust=proof.get("trust"), **record)
                if repairing:
                    self.log.bump("snapshotsRepaired")
                self._round_ok.add(sender_did)
                out.changed = True

        try:
            self._recheck_lineage_pending(state)
        except (SyncCancelled, KeyboardInterrupt):
            raise
        except Exception as e:
            self._report("lineage||", e, "lineage")
        self._note_lost_caches(state, set(by_delivery), book, checker)

    def _handoff_supersessions(self, by_delivery: Dict[str, Dict[str, Any]], processed: Dict[str, Any],
                               received_relays: Dict[str, Any], peers_map: Dict[str, Any], inbox_prefix: str,
                               tmpdir: str) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """Phase D: handoffs replaced by a re-issue (``supersedesRelayIds``) -> the re-issue's ID.

        Learned from handoffs this device claimed and from re-issued handoff readies in
        the inbox that verify with their sender's current key. Downloaded readies are
        returned for reuse so nothing is fetched twice. Failures here are silent: the
        main pass downloads again and reports them.
        """
        superseded: Dict[str, str] = {}
        prefetched: Dict[str, Any] = {}
        for got_rid, ent in sorted(received_relays.items()):
            for old_rid in (ent.get("supersedesRelayIds") or []) if isinstance(ent, dict) else []:
                superseded.setdefault(str(old_rid), str(got_rid))
        my_did = self.identity["deviceId"]
        for deliv_id, files in sorted(by_delivery.items()):
            if deliv_id.startswith(("sync_", "fwd_")) or "ready.json" not in files or "package.enc" not in files:
                continue
            if deliv_id in processed or ((received_relays.get(deliv_id) or {}).get("receiptUploaded")):
                continue
            try:
                V.validate_delivery_id(deliv_id)
                path = os.path.join(tmpdir, "%s_ready_pre.json" % deliv_id)
                self.provider.download("%s%s/ready.json" % (inbox_prefix, deliv_id), path)
                with open(path, "r", encoding="utf-8-sig") as fh:
                    raw = json.load(fh)
            except Exception:
                continue
            prefetched[deliv_id] = raw
            if not isinstance(raw, dict) or raw.get("purpose") != "handoff":
                continue
            sender = str(raw.get("senderDeviceId") or raw.get("fromDeviceId") or "")
            peer = peers_map.get(sender)
            if not isinstance(peer, dict) or not peer.get("paired") or peer.get("status") != "paired":
                continue
            try:
                verified = crypto.verify_json(raw, peer["verifyPublicKey"], expected_device_id=sender)
                if verified.get("relayId") != deliv_id or verified.get("toDeviceId") != my_did:
                    continue
                for old_rid in _supersedes_chain(verified):
                    superseded[old_rid] = deliv_id
            except Exception:
                continue
        return prefetched, superseded

    def _mark_superseded(self, book: "DS.DeliveryBook", deliv_id: str, by: str, ctx: Dict[str, Any],
                         tag_s: str) -> None:
        """An unclaimable handoff replaced by an explicit re-issue: information only, its problem is resolved."""
        def fn(st: Dict[str, Any]) -> None:
            ent = st["inbound"].setdefault(deliv_id, {})
            ent.pop("problem", None)
            ent.update({"state": "obsolete", "reason": "superseded", "supersededBy": by, "purpose": "handoff",
                        "objectTag": tag_s, "at": L.now_iso()})
            for k in ("snapshotId", "projectId", "senderDeviceId"):
                if ctx.get(k) and not ent.get(k):
                    ent[k] = ctx[k]

        book.mutate(fn)
        self.log.emit("delivery_obsolete", deliveryId=deliv_id, snapshotId=str(ctx.get("snapshotId") or ""),
                      projectId=str(ctx.get("projectId") or ""), senderDeviceId=str(ctx.get("senderDeviceId") or ""),
                      reason="superseded", supersededBy=by, needed=False)

    def _note_lost_caches(self, state: Dict[str, Any], listed: Set[str], book: "DS.DeliveryBook",
                          checker: "_CacheChecker") -> None:
        """Ask the network again (``needs``) for snapshots whose cache is gone and that the inbox cannot refill.

        Covers processed deliveries that have left the inbox, each project's
        current version, and graph heads this device authored -- all of which were
        cached here -- when recorded with a signed record digest (Phase C). Heads
        known only from lineage metadata were never cached and are not requested.
        Snapshots a cache prune removed on purpose (``prunedSnapshots``) never are.
        """
        if self._root_block is not None:
            return  # Fix G: nothing is lost behind a symlinked cache root; it cannot be judged until fixed
        processed = state.get("processedDeliveries") or {}
        pruned = book.state.get("prunedSnapshots") or {}
        my_did = self.identity["deviceId"]
        for deliv_id, ent in sorted((book.state.get("inbound") or {}).items()):
            # Fix G: refused by a local directory, and the delivery has left the inbox since:
            # the payload is gone, so ask the network for it (``needs``).
            if deliv_id in listed or not isinstance(ent, dict) or ent.get("state") != "blocked_local":
                continue
            sid = str(ent.get("snapshotId") or "")
            if self.target_proj_id and ent.get("projectId") not in (None, "", self.target_proj_id):
                continue
            if sid and checker.ok(sid):
                self._mark_cached(book, deliv_id)
                continue

            def gone(st: Dict[str, Any], _d: str = deliv_id) -> None:
                cur = st["inbound"].get(_d)
                if isinstance(cur, dict):
                    cur.pop("problem", None)
                    cur.update({"state": "repair_pending", "reason": "delivery_gone", "at": L.now_iso()})

            book.mutate(gone)
        for pid, graph in sorted((state.get("versionGraph") or {}).items()):
            if self.target_proj_id and pid != self.target_proj_id or not isinstance(graph, dict):
                continue
            nodes = graph.get("nodes") if isinstance(graph.get("nodes"), dict) else {}
            wanted = {h for h in (graph.get("heads") or [])
                      if isinstance(nodes.get(h), dict) and nodes[h].get("authorDeviceId") == my_did}
            current = (state.get("localCurrentVersion") or {}).get(pid)
            if current:
                wanted.add(current)
            for vid in sorted(str(v) for v in wanted if v):
                node = nodes.get(vid)
                if not isinstance(node, dict) or not node.get("recordSha256") or checker.ok(vid):
                    continue
                if vid in pruned:
                    # A current version or head is never pruned on purpose (Phase D): a stale prune mark
                    # must not stop it from being requested back.
                    book.mutate(lambda st, _v=vid: (st.get("prunedSnapshots") or {}).pop(_v, None))
                key = "head|%s" % vid
                if (book.inbound(key) or {}).get("state") == "repair_pending":
                    continue
                book.set_inbound(key, state="repair_pending", reason="head_cache_missing", snapshotId=vid,
                                 projectId=pid, purpose="cache")
        for deliv_id, ent in sorted(processed.items()):
            if deliv_id in listed or not isinstance(ent, dict):
                continue
            sid = str(ent.get("snapshotId") or "")
            if not sid or sid in pruned:
                continue
            if self.target_proj_id and ent.get("projectId") not in (None, "", self.target_proj_id):
                continue
            inb = book.inbound(deliv_id) or {}
            if inb.get("state") == "repair_pending" or checker.ok(sid):
                continue
            book.set_inbound(deliv_id, state="repair_pending", reason="cache_missing", snapshotId=sid,
                             projectId=str(ent.get("projectId") or ""), purpose="sync")

    def _record_inbound(self, deliv_id: str, snap_id: str, snap_proj_id: str, verified_snap: Dict[str, Any],
                        proof: Dict[str, Any], purpose: str, author_did: str, sender_did: str, tag_s: str) -> None:
        node = {
            "versionId": snap_id,
            "projectId": snap_proj_id,
            "authorDeviceId": author_did,
            "parentVersionIds": list(verified_snap.get("parentVersionIds") or []),
            "parentRelayId": str(verified_snap.get("parentRelayId") or ""),
            "relayId": str(verified_snap.get("relayId") or ""),
            "contentSha256": verified_snap["contentSha256"],
            "createdAt": str(verified_snap.get("createdAt") or ""),
        }
        node.update(_proof_fields(proof))
        # Fix G: before calling anything a fork, complete the signed ancestry the way receive does.
        # Pairs the local graph cannot put on one chain abort the state update (nothing written),
        # are judged outside the state lock (signed ancestors may be read from the cloud), and the
        # update runs again with the verified ancestors. Without such pairs: one pass, no extra I/O.
        verdicts: Dict[Tuple[str, str], Tuple[str, Dict[str, Any]]] = {}
        loaded: Dict[str, Dict[str, Any]] = {}
        ready_box: Dict[str, Any] = {}
        failed: Dict[str, Dict[str, str]] = {}
        fork_pairs: List[Tuple[str, str]] = []
        unproven: List[Dict[str, Any]] = []

        def _upd_sync_state(st_obj: Dict[str, Any], may_fetch: bool) -> None:
            del fork_pairs[:], unproven[:]
            graph_before = copy.deepcopy((st_obj.get("versionGraph") or {}).get(snap_proj_id) or {"nodes": {}, "heads": []})
            prev_heads = list(graph_before.get("heads") or [])
            if loaded:
                target = st_obj.setdefault("versionGraph", {}).setdefault(snap_proj_id, {"nodes": {}, "heads": []})
                target_nodes = target.setdefault("nodes", {})
                for vid, anc in loaded.items():
                    if not isinstance(target_nodes.get(vid), dict):
                        target_nodes[vid] = copy.deepcopy(anc)
            _record_version(st_obj, snap_proj_id, node)
            graph_after = st_obj["versionGraph"][snap_proj_id]
            v_local = str((st_obj.get("localCurrentVersion") or {}).get(snap_proj_id) or "")
            candidates = _fork_candidates(graph_after, v_local, prev_heads, snap_id)
            todo = [pair for pair in candidates if pair not in verdicts]
            if todo and may_fetch:
                raise _LineageNeeded(copy.deepcopy(graph_after), todo)
            for v_a, v_b in candidates:
                verdict, missing = verdicts.get((v_a, v_b), ("unproven", {}))
                if verdict == "fork":
                    fork_pairs.append((v_a, v_b))
                elif verdict != "linear":
                    key = _lineage_pair_key(v_a, v_b)
                    proj_pending = st_obj.setdefault("lineagePending", {}).setdefault(snap_proj_id, {})
                    fresh = key not in proj_pending
                    proj_pending[key] = {"localVersion": v_a, "incomingVersion": v_b, "missing": missing,
                                         "authorDeviceId": author_did, "deliveryId": deliv_id,
                                         "detectedAt": (proj_pending.get(key) or {}).get("detectedAt") or L.now_iso(),
                                         "checkedAt": L.now_iso()}
                    unproven.append({"projectId": snap_proj_id, "localVersion": v_a, "incomingVersion": v_b,
                                     "missingVersions": sorted(missing), "authorDeviceId": author_did,
                                     "fresh": fresh, "nodes": len(graph_after.get("nodes") or {})})
            if purpose == "sync":
                st_obj.setdefault("processedDeliveries", {})[deliv_id] = {
                    "snapshotId": snap_id,
                    "projectId": snap_proj_id,
                    "purpose": "sync",
                    "processedAt": L.now_iso(),
                }

        for attempt in range(3):
            try:
                _update_relay_state(self.base, lambda st_obj: _upd_sync_state(st_obj, attempt < 2))
                break
            except _LineageNeeded as need:
                for v_a, v_b in need.pairs:
                    self._check_cancel()
                    verdict, missing, got = _lineage_verdict(self.provider, self.base, self.identity, self.cloud,
                                                             snap_proj_id, need.graph, v_a, v_b, ready_box, failed)
                    loaded.update(got)
                    verdicts[(v_a, v_b)] = (verdict, missing)
        if self.book is not None and self.book.inbound(deliv_id) is not None:
            # Only a delivery judged before (obsolete, quarantined, lost, blocked) needs its state moved on.
            self._mark_cached(self.book, deliv_id, purpose=purpose, snapshotId=snap_id, projectId=snap_proj_id,
                              senderDeviceId=sender_did, objectTag=tag_s)

        for v_a, v_b in fork_pairs:
            _record_conflict_in_ledger(self.led_path, snap_proj_id, self.here_id, v_a, v_b,
                                       author_device_id=author_did)
            conflict = {"projectId": snap_proj_id, "localVersion": v_a, "incomingVersion": v_b,
                        "authorDeviceId": author_did}
            self._legacy_add("conflictsDetected", conflict)
            self.log.emit("fork_detected", **conflict)
        now = self.clock.monotonic()
        for item in unproven:
            self._lineage_tries["%s|%s" % (snap_proj_id, _lineage_pair_key(item["localVersion"], item["incomingVersion"]))] \
                = (now, int(item.pop("nodes")))
            if not item.pop("fresh"):
                continue
            self._legacy_add("lineageUnproven", item)
            self.log.emit("lineage_unproven", **item)

    def _recheck_lineage_pending(self, state_hint: Dict[str, Any]) -> None:
        """Re-judge recorded "insufficient evidence" pairs: resolved when proven linear, a fork when proven one.

        Nothing to do (and no extra I/O) unless the round's relay state holds
        such pairs. The local graph is checked every round; missing ancestors
        are read again at most every ``LINEAGE_RETRY_SEC`` per pair, or as soon
        as the project's graph has grown (a new snapshot or ancestor was recorded).
        """
        if not state_hint.get("lineagePending") or self._root_block is not None:
            return
        state = _load_relay_state(self.base)
        pending = state.get("lineagePending")
        if not isinstance(pending, dict) or not pending:
            return
        vg = state.get("versionGraph") or {}
        now = self.clock.monotonic()
        results: List[Tuple[str, str, str, Dict[str, Any], Dict[str, Any]]] = []
        loaded_by_pid: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for pid in sorted(pending):
            pairs = pending.get(pid)
            if not isinstance(pairs, dict) or (self.target_proj_id and pid != self.target_proj_id):
                continue
            graph = copy.deepcopy(vg.get(pid) if isinstance(vg.get(pid), dict) else {"nodes": {}, "heads": []})
            size = len(graph.get("nodes") or {})
            ready_box: Dict[str, Any] = {}
            failed: Dict[str, Dict[str, str]] = {}
            for key in sorted(pairs):
                ent = pairs[key]
                a = str((ent or {}).get("localVersion") or "") if isinstance(ent, dict) else ""
                b = str((ent or {}).get("incomingVersion") or "") if isinstance(ent, dict) else ""
                if not a or not b:
                    results.append((pid, key, "drop", {}, {}))
                    continue
                if _is_version_ancestor(graph, a, b) or _is_version_ancestor(graph, b, a):
                    results.append((pid, key, "linear", {}, ent))
                    continue
                tkey = "%s|%s" % (pid, key)
                last = self._lineage_tries.get(tkey)
                if last is not None and last[1] == size and now - last[0] < LINEAGE_RETRY_SEC:
                    continue
                self._check_cancel()
                verdict, missing, got = _lineage_verdict(self.provider, self.base, self.identity, self.cloud, pid,
                                                         graph, a, b, ready_box, failed)
                self._lineage_tries[tkey] = (now, len(graph.get("nodes") or {}))
                if got:
                    loaded_by_pid.setdefault(pid, {}).update(got)
                if verdict != "unproven" or missing != ent.get("missing") or got:
                    results.append((pid, key, verdict, missing, ent))
        if not results and not loaded_by_pid:
            return

        def upd(st: Dict[str, Any]) -> None:
            for pid, got in loaded_by_pid.items():
                target = st.setdefault("versionGraph", {}).setdefault(pid, {"nodes": {}, "heads": []})
                target_nodes = target.setdefault("nodes", {})
                for vid, anc in got.items():
                    if not isinstance(target_nodes.get(vid), dict):
                        target_nodes[vid] = copy.deepcopy(anc)
                _recompute_graph_heads(target)
            pend = st.get("lineagePending") if isinstance(st.get("lineagePending"), dict) else {}
            for pid, key, verdict, missing, _ent in results:
                pp = pend.get(pid) if isinstance(pend.get(pid), dict) else {}
                if verdict == "unproven":
                    cur = pp.get(key)
                    if isinstance(cur, dict):
                        cur["missing"] = missing
                        cur["checkedAt"] = L.now_iso()
                else:
                    pp.pop(key, None)
                if not pp:
                    pend.pop(pid, None)
            if not pend:
                st.pop("lineagePending", None)

        _update_relay_state(self.base, upd)
        for pid, key, verdict, _missing, ent in results:
            a, b = str(ent.get("localVersion") or ""), str(ent.get("incomingVersion") or "")
            if verdict == "linear":
                self.log.emit("lineage_resolved", projectId=pid, localVersion=a, incomingVersion=b, relation="linear")
            elif verdict == "fork":
                author = str(ent.get("authorDeviceId") or "")
                _record_conflict_in_ledger(self.led_path, pid, self.here_id, a, b, author_device_id=author)
                conflict = {"projectId": pid, "localVersion": a, "incomingVersion": b, "authorDeviceId": author}
                self._legacy_add("conflictsDetected", conflict)
                self.log.emit("fork_detected", **conflict)
                self.log.emit("lineage_resolved", projectId=pid, localVersion=a, incomingVersion=b, relation="fork")

    def _phase_have(self, cached_items: List[Dict[str, Any]], out: RoundOutcome) -> None:
        """Publish the have v2 (digest-verified cache + ``needs``) when it changed or the refresh is due."""
        ids = sorted({item["versionId"] for item in cached_items})
        book = self.book if self.book is not None else _delivery_book(self.base, self.identity)
        checker = self._checker if self._checker is not None else _cache_checker(self.base)
        needs = book.needs(checker.ok)
        digest = _have_digest(self.identity["deviceId"], self.identity["keyFingerprint"], ids, needs)
        now = self.clock.monotonic()
        if digest == self._have_digest and now - self._have_at < self.have_refresh:
            return
        try:
            signed = _upload_have(self.provider, self.base, self.identity, book, ids, needs)
        except Exception as e:
            # Recorded, but forwarding still runs: the have manifest only saves peers work.
            self._report("have_publish||", e, "have_publish")
            return
        refresh_only = digest == self._have_digest
        self._have_digest = digest
        self._have_at = now
        self.log.emit("have_published", retain=not refresh_only, count=len(ids), needs=len(needs) or None,
                      haveSeq=signed.get("seq"), refresh=refresh_only or None)

    def _phase_forward(self, cached_items: List[Dict[str, Any]], out: RoundOutcome) -> None:
        """Deliver cached head snapshots (and snapshots peers ask for) per recipient key; each peer is isolated."""
        identity = self.identity
        state = _load_relay_state(self.base)
        vg = state.get("versionGraph") or {}
        book = self.book if self.book is not None else _delivery_book(self.base, identity)
        by_id = {item["versionId"]: item for item in cached_items}
        head_items: List[Dict[str, Any]] = []
        for item in cached_items:
            snap_obj = item["snapshot"]
            pid = str(snap_obj.get("projectId") or "")
            if self.target_proj_id and pid != self.target_proj_id:
                continue
            proj_heads = set(((vg.get(pid) or {}).get("heads") or []))
            proj_nodes = ((vg.get(pid) or {}).get("nodes") or {})
            if (not proj_nodes) or (item["versionId"] in proj_heads) or (item["versionId"] not in proj_nodes):
                head_items.append(item)
        outbound = _Outbound(self.provider, identity, self.here_id, self.base, book, self.cloud,
                             relay_state=state, clock=self.clock, maint_at=self._maint_at)

        for peer in [p for p in _paired_active_peers(self.cloud) if _peer_is_v3(p)]:
            p_did = peer["deviceId"]
            p_fp = DS.norm_fp(peer.get("keyFingerprint"))
            self._check_cancel()
            if not self.health.is_due(p_did):
                self.log.carry_peer(p_did)
                continue
            try:
                have = _fetch_peer_have(self.provider, peer, book)
            except Exception as e:
                classified = SR.classify_error(e, "have_fetch")
                if classified.code == "signature_invalid":
                    classified = self._triage_signature(peer, classified, getattr(e, "signed_at", None), "have_fetch")
                if classified.error_class == SR.INTEGRITY or (classified.error_class == SR.NEEDS_ACTION
                                                              and not classified.peer_level):
                    # An unverifiable manifest is re-read on the backoff schedule, not every round.
                    problem, _ = self._report("have_fetch|%s|" % p_did, classified, "have_fetch", peer=p_did)
                    self._peer_backoff(p_did, problem)
                else:
                    self._item_failure("have_fetch|%s|" % p_did, classified, "have_fetch", p_did)
                continue
            self._round_ok.add(p_did)
            book.supersede_other_keys(p_did, p_fp)
            if have.ok:
                for change in _record_peer_have(book, peer, have):
                    self.log.emit("peer_verified", peerDeviceId=p_did, **change)
            candidates = list(head_items)
            if have.ok and have.needs:
                head_ids = {item["versionId"] for item in head_items}
                candidates.extend(by_id[sid] for sid in sorted(have.needs) if sid in by_id and sid not in head_ids)

            for item in candidates:
                snap_obj = item["snapshot"]
                snap_id = item["versionId"]
                author_did = str(snap_obj.get("authorDeviceId") or "")
                if not author_did or (author_did == p_did and not (have.ok and snap_id in have.needs)):
                    continue
                fkey = "forward|%s|%s" % (p_did, snap_id)
                held = self.quarantine.match(fkey, None)
                if held and self.log.carry(held):
                    continue
                ckey = "forward||%s" % snap_id
                ctag = _file_tag(item["tarPath"])
                held = self.quarantine.match(ckey, ctag)
                if held and self.log.carry(held):
                    continue
                if snap_id not in self._verified_cache:
                    try:
                        # The payload digest was verified by the cache check; this checks the author.
                        self._verified_cache[snap_id] = _verify_author_snapshot(
                            snap_obj, identity, self.cloud, vg, use="forward")[0]
                    except RelayError as e:
                        self._item_failure(ckey, SR.ClassifiedError(
                            SR.NEEDS_ACTION, e.code or "untrusted_author", e.message,
                            "relay pair --with %s" % author_did), "forward", "", ckey, ctag, snapshotId=snap_id)
                        continue
                    except Exception as e:
                        self._item_failure(ckey, e, "forward", "", ckey, ctag, snapshotId=snap_id)
                        continue
                verified_snap = self._verified_cache[snap_id]
                try:
                    res = outbound.deliver(peer, item, have=have)
                except Exception as e:
                    self._item_failure(fkey, e, "forward", p_did, fkey, None, snapshotId=snap_id)
                    if not self.health.is_due(p_did):
                        break  # the peer went into backoff or was parked: leave the rest for later
                    continue
                action = res.get("action")
                if action == "verified":
                    self.log.emit("peer_verified", peerDeviceId=p_did, snapshotId=snap_id,
                                  projectId=str(verified_snap["projectId"]), haveSeq=res.get("haveSeq"))
                    continue
                if action not in ("published", "resumed", "restored"):
                    continue
                record = {
                    "snapshotId": snap_id,
                    "projectId": str(verified_snap["projectId"]),
                    "toDeviceId": p_did,
                    "toAlias": str(peer.get("alias") or ""),
                    "deliveryId": res.get("deliveryId"),
                }
                self._legacy_add("forwarded", record)
                self.log.emit("snapshot_forwarded", peerDeviceId=p_did, generation=res.get("generation"),
                              reason=res.get("reason"), **record)
                out.changed = True

    # ------------------------------------------------------------ results
    def peers_view(self) -> Dict[str, Dict[str, Any]]:
        health = self.health.snapshot()
        # Phase C: published / peer-verified / claimed counts per peer (from deliveries.json and sentRelays).
        try:
            deliveries = DS.delivery_view(self.book.state if self.book is not None else DS.load(self.base),
                                          load_relay_state(self.base))["peers"]
        except Exception:
            deliveries = {}
        peers = self.cloud.get("peers") if isinstance(self.cloud.get("peers"), dict) else {}
        out: Dict[str, Dict[str, Any]] = {}
        for did, peer in sorted(peers.items()):
            if not isinstance(peer, dict):
                continue
            if not (peer.get("paired") or peer.get("status") == "key_changed" or did in health):
                continue
            view = {"alias": str(peer.get("alias") or ""), "status": str(peer.get("status") or ""),
                    "keyFingerprint": str(peer.get("keyFingerprint") or ""), "state": SR.HEALTHY,
                    "consecutiveFailures": 0}
            view.update(health.get(did) or {})
            if did in deliveries:
                view["deliveries"] = deliveries[did]
            out[did] = view
        for did, h in health.items():
            if did not in out:
                view = {"alias": "", "status": "", "keyFingerprint": ""}
                view.update(h)
                out[did] = view
        return out

    def summary(self) -> Dict[str, Any]:
        snap = self.log.snapshot()
        status = SR.completion_status(snap["unresolved"], cancelled=self.cancelled)
        out: Dict[str, Any] = {
            "schema": SR.SUMMARY_SCHEMA,
            "ok": status == SR.SUCCESS,
            "status": status,
            "runId": self.log.run_id,
            "mode": self.mode,
            "cycles": self.cycles,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "deviceId": self.identity["deviceId"],
            "project": self.target_proj_id or "",
            "interval": self.interval,
            "maxBackoff": self.max_backoff,
            "counters": snap["counters"],
            "recentEvents": snap["recentEvents"],
            "recentEventsTruncated": snap["recentEventsTruncated"],
            "recentEventsDropped": snap["recentEventsDropped"],
            "firstRecentSeq": snap["firstRecentSeq"],
            "lastSeq": snap["lastSeq"],
            "unresolved": snap["unresolved"],
            "unresolvedTruncated": snap["unresolvedTruncated"],
            "peers": self.peers_view(),
        }
        with self.log.lock:
            for name in _LEGACY_LISTS:
                out[name] = [dict(x) for x in self._legacy[name]]
            out["legacyListsTruncated"] = {name: self._legacy_total[name] > len(self._legacy[name])
                                           for name in _LEGACY_LISTS}
        if self.fatal:
            out["stoppedReason"] = self.fatal
        return out

    def _maybe_stats(self) -> None:
        if not self.stats_every:
            return
        now = self.clock.monotonic()
        if self._stats_at is not None and now - self._stats_at < self.stats_every:
            return
        self._stats_at = now
        fields: Dict[str, Any] = {"ringSize": len(self.log.recent), "unresolved": len(self.log.unresolved),
                                  "quarantined": len(self.quarantine)}
        try:
            import sys

            fields["allocatedBlocks"] = sys.getallocatedblocks()  # Python heap size, no tracing overhead
        except Exception:
            pass
        try:
            import resource

            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            fields["maxRssKb"] = int(peak // 1024) if _sys_platform() == "darwin" else int(peak)
        except Exception:
            pass
        try:
            import tracemalloc

            if tracemalloc.is_tracing():
                fields["traced"] = tracemalloc.get_traced_memory()[0]
        except Exception:
            pass
        self.log.emit("stats", retain=False, **fields)

    def _maybe_write_status(self) -> None:
        if self.status_path is None:
            return
        now = self.clock.monotonic()
        if self._status_at is not None and now - self._status_at < STATUS_WRITE_MIN_SEC:
            return
        summary = self.summary()
        digest = SR.summary_digest(summary)
        if digest == self._status_digest and self._status_at is not None and now - self._status_at < STATUS_HEARTBEAT_SEC:
            return
        self._write_status(summary, running=True)

    def _write_status(self, summary: Dict[str, Any], running: bool) -> None:
        if self.status_path is None:
            return
        try:
            V.ensure_contained(self.base, self.status_path, field="status")
            SR.write_status_file(self.status_path, summary, extra={
                "pid": os.getpid(), "running": bool(running), "heartbeatAt": L.now_iso(),
            })
        except Exception:
            return  # the status file is advisory; a failure here never fails the sync
        self._status_digest = SR.summary_digest(summary)
        self._status_at = self.clock.monotonic()


def _sys_platform() -> str:
    import sys

    return sys.platform


def _rmtree_quiet(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _sync_network_once(
    target_proj_id: Optional[str],
    base: Path,
    led_path: Path,
    local_cfg: Dict[str, Any],
    here_id: str,
    cloud: Dict[str, Any],
    identity: Dict[str, Any],
    provider,
) -> Dict[str, Any]:
    """One legacy round inside a lock the caller already holds (kept for compatibility; unused)."""
    session = SyncSession(mode="once", _ctx=(target_proj_id, base, led_path, local_cfg, here_id, cloud,
                                              identity, provider))
    session.log.begin_round(1)
    out = RoundOutcome()
    session._round_body(out)
    session.log.end_round()
    summary = session.summary()
    return {
        "ok": True,
        "syncedReceipts": summary["syncedReceipts"],
        "syncedSnapshots": summary["syncedSnapshots"],
        "forwarded": summary["forwarded"],
        "conflictsDetected": summary["conflictsDetected"],
        "errors": summary["errors"],
    }


def sync_network(
    project_key: Optional[str] = None,
    watch: bool = False,
    interval: float = 5.0,
    max_cycles: Optional[int] = None,
    cwd: Optional[str] = None,
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
    *,
    max_backoff: float = 300.0,
    on_event=None,
    clock=None,
    rng=None,
    cancel=None,
    event_limit: int = SR.DEFAULT_EVENT_LIMIT,
    mode: Optional[str] = None,
    status_file: bool = True,
    stats_every: Optional[float] = None,
) -> Dict[str, Any]:
    """Synchronize receipts, cache snapshots, detect forks and forward head snapshots, round by round.

    Returns the run summary (``relaydesk.sync.summary/1``): the legacy lists
    (``syncedReceipts``, ``syncedSnapshots``, ``forwarded``, ``conflictsDetected``,
    ``errors``; each capped at 200 with ``legacyListsTruncated``), exact
    ``counters``, the bounded ``recentEvents``, the ``unresolved`` problems and
    ``status`` (success / partial / needs_action / cancelled; ``ok`` is
    ``status == "success"``). Context errors before the first round (not
    configured, damaged local state, unknown device) still raise.
    """
    session = SyncSession(
        project_key=project_key, cwd=cwd, ledger_path=ledger_path, local=local, local_home=local_home,
        interval=interval, max_backoff=max_backoff, on_event=on_event, clock=clock, rng=rng, cancel=cancel,
        event_limit=event_limit, mode=mode or ("watch" if watch else "once"), status_file=status_file,
        stats_every=stats_every,
    )
    return session.run(watch=bool(watch), max_cycles=max_cycles)


# ---------------------------------------------------------------- 5. send_project


def _resolve_cloud_target_peer(
    led: Dict[str, Any],
    here_id: str,
    to_alias: Optional[str],
    cloud: Dict[str, Any],
) -> Dict[str, Any]:
    try:
        return devices.resolve_target_device(
            led["config"],
            here_id,
            to_query=to_alias,
            cloud_cfg=cloud,
            require_paired_cloud=True,
        )
    except devices.AmbiguousDeviceError as e:
        raise AmbiguousPeerError(str(e), candidates=e.candidates)
    except LookupError as e:
        paired = _paired_active_peers(cloud)
        if to_alias:
            q = str(to_alias).strip()
            for p in _key_changed_peers(cloud):
                if (
                    p.get("deviceId") == q
                    or str(p.get("alias") or "").lower() == q.lower()
                    or str(p.get("name") or "").lower() == q.lower()
                ):
                    raise KeyChangedError(
                        "目标设备「%s」密钥已变更，请先核对指纹后运行 relay pair --with %s --accept。"
                        % (p.get("alias") or q, p.get("deviceId")),
                        peer_id=str(p.get("deviceId") or ""),
                        old_fingerprint=str(p.get("keyFingerprint") or ""),
                        new_fingerprint=str((p.get("pendingDescriptor") or {}).get("keyFingerprint") or ""),
                    )
        if not paired:
            raise NotConfiguredError("尚未完成设备配对确认。请核对配对指纹一致后运行 relay pair --accept。")
        raise RelayError(str(e), code="peer_not_found")
    except ValueError as e:
        raise RelayError(str(e), code="invalid_target")


def send_project(
    project_key: Optional[str] = None,
    to_alias: Optional[str] = None,
    cwd: Optional[str] = None,
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
    reissue: bool = False,
) -> Dict[str, Any]:
    """Package, encrypt, two-phase atomically upload, and verify a project handoff.

    ``reissue=True`` (``relay send --reissue``, Phase D) never packs the workspace again:
    it re-encrypts the cached payload of an in-flight handoff's *same* snapshot for the
    target's current, re-confirmed key under a new relay ID, and marks the original
    handoff superseded. Only an explicit user command does this.
    """
    base, led_path, local_cfg, led0, here_id, cloud, identity, provider = _require_paired_context(
        ledger_path, local, local_home, require_any_peer=True
    )
    target_peer = _resolve_cloud_target_peer(led0, here_id, to_alias, cloud)
    ctx = (base, led_path, local_cfg, led0, here_id, cloud, identity, target_peer, provider)
    with _workspace_operation(base):
        if reissue:
            return _reissue_handoff(project_key, cwd, ctx)
        return _send_project(project_key, cwd, ctx)


def _reissue_origin(state: Dict[str, Any], proj: Dict[str, Any], proj_id: str) -> Dict[str, Any]:
    """The in-flight handoff of ``proj_id`` a re-issue would replace (sent, or prepared but not finished)."""
    sent = state.get("sentRelays") or {}
    rid = str((state.get("activeHandoffs") or {}).get(proj_id) or "")
    if not (rid and isinstance(sent.get(rid), dict)):
        ho = proj.get("handoff")
        rid = str(ho.get("relayId") or "") if isinstance(ho, dict) else ""
    if rid and isinstance(sent.get(rid), dict):
        info = sent[rid]
        if info.get("acked"):
            raise RelayError("交接 %s 已被对方领取并签收，不能重新签发。" % rid, code="handoff_already_claimed")
        if DS.handoff_superseded(info):
            raise RelayError("交接 %s 已由 %s 取代，无需再次重新签发。" % (rid, info.get("supersededBy") or "新交接"),
                             code="handoff_already_superseded")
        ready = info.get("signedReady")
        return {"relayId": rid, "signedReady": ready, "readyKey": str(info.get("readyKey") or ""),
                "packageKey": str(info.get("packageKey") or ""), "toDeviceId": str(info.get("toDeviceId") or ""),
                "toAlias": str(info.get("toAlias") or ""), "snapshotId": str(info.get("snapshotId") or ""),
                "packageSha256": str(info.get("packageSha256") or ""), "sentAt": str(info.get("sentAt") or ""),
                "protocolVersion": int(info.get("protocolVersion") or 2),
                "recipientKeyFingerprint": str(info.get("recipientKeyFingerprint") or ""), "fromPending": False,
                "chain": [str(x) for x in (info.get("supersedesRelayIds") or []) if isinstance(x, str) and x]}
    pend = (state.get("pendingSends") or {}).get(proj_id) or {}
    prep = pend.get("prepared") if isinstance(pend, dict) else None
    if isinstance(prep, dict) and isinstance(prep.get("ready"), dict) and not pend.get("reissueOf"):
        ready = prep["ready"]
        return {"relayId": str(pend.get("relayId") or ""), "signedReady": ready,
                "readyKey": str(prep.get("readyKey") or ""), "packageKey": str(ready.get("packageKey") or ""),
                "toDeviceId": str(pend.get("toDeviceId") or ""), "toAlias": str(pend.get("toAlias") or ""),
                "snapshotId": str(prep.get("snapshotId") or ""), "packageSha256": str(ready.get("packageSha256") or ""),
                "sentAt": str(ready.get("createdAt") or ""), "protocolVersion": int(prep.get("protocolVersion") or 2),
                "recipientKeyFingerprint": str(prep.get("recipientKeyFingerprint")
                                               or pend.get("recipientKeyFingerprint") or ""),
                "fromPending": True, "baseline": prep.get("baseline") or {}, "sourcePath": prep.get("sourcePath") or ""}
    raise RelayError("项目「%s」没有可以重新签发的在途交接。" % (proj.get("name") or proj_id), code="nothing_to_reissue")


def _reissue_handoff(project_key, cwd, ctx) -> Dict[str, Any]:
    """``relay send PROJECT --to DEVICE --reissue``: a new relay ID and ciphertext for the same snapshot.

    Safety (plan section 2): the original package, ready and any receipt are never
    touched; the new ready names the original in ``supersedesRelayId`` (signed),
    receivers refuse a re-issue once they claimed the original and refuse the
    original once they claimed a re-issue, so the target can never claim both.
    Only handoffs the current keys can no longer verify or open are re-issued.
    """
    base, led_path, local_cfg, _led0, here_id, cloud, identity, target_peer, provider = ctx
    target_did = str(target_peer["deviceId"])
    dest_alias = str(target_peer.get("alias") or "")
    sync_receipts(provider, identity, peer_desc=None, ledger_path=led_path, local=local_cfg, local_home=base)
    led = L.load(led_path)
    proj = L.find_project(led, project_key, cwd or os.getcwd())
    proj_id = proj["id"]
    state = _load_relay_state(base)
    pending = (state.get("pendingSends") or {}).get(proj_id) or {}
    if pending.get("reissueOf"):
        # An interrupted re-issue continues under its own relay ID (prepared bytes are never re-encrypted).
        if str(pending.get("toDeviceId") or "") != target_did:
            raise ActiveHandoffConflictError(
                "项目「%s」正在为 %s 重新签发交接 %s，不能改发到另一台设备。"
                % (proj["name"], pending.get("toAlias") or pending.get("toDeviceId"), pending.get("relayId")),
                relay_id=str(pending.get("relayId") or ""), target_alias=str(pending.get("toAlias") or ""))
        if pending.get("prepared"):
            return _publish_prepared_send(ctx, proj_id, str(pending["relayId"]), pending["prepared"], retry=True)
        origin = pending.get("reissueOrigin") or {}
        new_rid = str(pending["relayId"])
    else:
        origin = _reissue_origin(state, proj, proj_id)
        new_rid = ""
    orig_rid = str(origin.get("relayId") or "")
    ready = origin.get("signedReady")
    if origin.get("toDeviceId") and origin["toDeviceId"] != target_did:
        raise ActiveHandoffConflictError(
            "项目「%s」的在途交接 %s 是发给 %s 的，只能为同一台设备重新签发。"
            % (proj["name"], orig_rid, origin.get("toAlias") or origin.get("toDeviceId")),
            relay_id=orig_rid, target_alias=str(origin.get("toAlias") or ""))
    if int(origin.get("protocolVersion") or 2) < 3 or not isinstance(ready, dict) \
            or not isinstance(ready.get("snapshot"), dict):
        raise RelayError("交接 %s 使用旧版协议，没有可重新签发的签名快照。" % orig_rid, code="reissue_not_supported")
    snap = ready["snapshot"]
    snap_id = str(snap.get("versionId") or "")
    if snap.get("authorDeviceId") != identity["deviceId"]:
        raise RelayError("交接 %s 中的快照不是本机创建的，不能由本机重新签发。" % orig_rid, code="reissue_not_supported")
    if not new_rid:
        # Only a handoff the current keys can no longer verify or open needs a re-issue.
        try:
            _verify_outgoing_ready(ready, orig_rid, proj_id, identity, target_peer, proto_ver=3,
                                   recorded_fp=str(origin.get("recipientKeyFingerprint") or ""))
        except KeyChangedError as e:
            if e.code not in ("handoff_recipient_key_changed", "handoff_sender_key_rotated"):
                raise
            origin["reason"] = e.code
        else:
            raise RelayError(
                "交接 %s 仍可由目标设备用当前密钥领取，无需重新签发；直接运行 relay send %s --to %s 会沿用原交接包。"
                % (orig_rid, proj_id, dest_alias or target_did), code="reissue_not_needed")
        if provider.stat("v3/inbox/%s/%s/receipt.json" % (target_did, orig_rid)) is not None:
            raise RelayError(
                "云端已有交接 %s 的签收回执但本机尚未确认，对方可能已经领取；请先运行 relay sync %s，不能重新签发。"
                % (orig_rid, proj_id), code="reissue_receipt_exists")
    cdir = _snapshot_cache_dir(base, snap_id)
    if not DS.cache_ok(cdir, crypto.snapshot_record_digest, crypto.snapshot_record_digest(snap)):
        raise RelayError(
            "本机缓存中没有快照 %s 完整且可验证的载荷，无法重新签发；原交接包 %s 保持不变。" % (snap_id, orig_rid),
            code="reissue_payload_missing")
    meta_plain = _read_json_file(cdir / "meta_plain.json", {})
    if not meta_plain.get("projectId"):
        meta_plain = {"projectId": proj_id, "projectName": proj.get("name") or proj_id,
                      "lastTask": proj.get("lastTask") or "", "nextAction": proj.get("nextAction") or "",
                      "branch": proj.get("branch") or "", "dirty": proj.get("dirty", 0), "sessionCount": 0}
    if meta_plain.get("projectId") != proj_id:
        raise RelayError("快照 %s 的缓存元数据与项目不一致，拒绝重新签发。" % snap_id, code="reissue_not_supported")
    recipient_fp = str(target_peer.get("keyFingerprint") or "")
    origin_min = {k: origin.get(k) for k in ("relayId", "signedReady", "readyKey", "packageKey", "toDeviceId", "toAlias",
                                             "snapshotId", "packageSha256", "sentAt", "recipientKeyFingerprint",
                                             "protocolVersion", "fromPending", "reason", "chain", "baseline",
                                             "sourcePath")}
    chain = [x for x in (origin.get("chain") or []) if x != orig_rid] + [orig_rid]  # every handoff this one replaces
    if not new_rid:
        day_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        new_rid = L.new_id("r_%s_" % day_str)
    parent_id = str(ready.get("parentRelayId") or "")
    pending_rec = {
        "relayId": new_rid, "toAlias": dest_alias, "toDeviceId": target_did, "parentRelayId": parent_id,
        "protocolVersion": 3, "checkpointBeforeReady": True, "recipientKeyFingerprint": recipient_fp,
        "reissueOf": orig_rid, "reissueOrigin": origin_min,
    }

    def _save_pending(st_obj: Dict[str, Any]) -> None:
        st_obj.setdefault("pendingSends", {})[proj_id] = pending_rec

    _update_relay_state(base, _save_pending)
    pkg_key = "v3/inbox/%s/%s/package.enc" % (target_did, new_rid)
    ready_key = "v3/inbox/%s/%s/ready.json" % (target_did, new_rid)
    created_at = L.now_iso()
    with tempfile.TemporaryDirectory() as tmpdir:
        enc_path = os.path.join(tmpdir, "package.enc")
        envelope = crypto.encrypt_package(str(cdir / "payload.tar.gz"), enc_path, identity, target_peer)
        meta_enc = crypto.encrypt_meta(meta_plain, identity, target_peer)
        ready_unsigned = dict(envelope)
        ready_unsigned.update({
            "formatVersion": 3, "purpose": "handoff", "deliveryId": new_rid, "relayId": new_rid,
            "senderDeviceId": identity["deviceId"], "senderAlias": here_id, "fromAlias": here_id,
            "fromDeviceId": identity["deviceId"], "toAlias": dest_alias, "toDeviceId": target_did,
            "authorizedDeviceId": target_did, "parentRelayId": parent_id, "isForwarded": False,
            "createdAt": created_at, "packageKey": pkg_key, "snapshot": snap,
            "recipientKeyFingerprint": recipient_fp,
            # Signed: receivers treat the original (and any earlier re-issue) as replaced and never let
            # two of them be claimed.
            "supersedesRelayId": orig_rid,
            "supersedesRelayIds": chain,
            "meta": meta_enc,
        })
        prepared = {
            "protocolVersion": 3, "recipientKeyFingerprint": recipient_fp, "readyKey": ready_key,
            "snapshotId": snap_id, "snapshot": snap, "metaPlain": meta_plain,
            "ready": crypto.sign_json(ready_unsigned, identity["signPrivateKey"], signer_device_id=identity["deviceId"]),
            "sourcePath": str(origin.get("sourcePath") or ""), "facts": None, "presence": None, "excludedFiles": [],
            "baseline": copy.deepcopy((state.get("projectBaselines") or {}).get(proj_id) or origin.get("baseline") or {}),
            "reissueOf": orig_rid, "reissueOrigin": origin_min,
        }
        provider.upload(enc_path, pkg_key)
        _verify_published_package(provider, ready_unsigned)
        workspace_tx.maybe_inject_fault("reissue_after_package_upload")

        def _checkpoint(st_obj: Dict[str, Any]) -> None:
            st_obj["pendingSends"][proj_id]["prepared"] = prepared

        _update_relay_state(base, _checkpoint)
    return _publish_prepared_send(ctx, proj_id, new_rid, prepared)


def _verify_published_package(provider, ready: dict) -> None:
    pkg_key = ready["packageKey"]
    remote_package = provider.stat(pkg_key)
    if remote_package is None or remote_package.size != ready.get("packageSize"):
        raise _storage_mod().StorageError("云端加密交接包缺失或大小不一致：%s" % pkg_key)
    if remote_package.sha256 and remote_package.sha256.lower() != ready["packageSha256"]:
        raise _storage_mod().StorageError("云端加密交接包的 SHA-256 不一致：%s" % pkg_key)


def _send_project(project_key, cwd, ctx) -> Dict[str, Any]:
    conv_mod = _conversations_mod()
    pkg_mod = _packager_mod()
    base, led_path, local_cfg, _led0, here_id, cloud, identity, target_peer, provider = ctx

    sync_receipts(provider, identity, peer_desc=None, ledger_path=led_path, local=local_cfg, local_home=base)
    led = L.load(led_path)
    proj = L.find_project(led, project_key, cwd or os.getcwd())
    proj_id = proj["id"]

    dest_alias = str(target_peer.get("alias") or "")
    target_did = str(target_peer["deviceId"])
    state = _load_relay_state(base)

    # 1. Check existing pendingSends[proj_id]
    pending = (state.get("pendingSends") or {}).get(proj_id) or {}
    if pending.get("relayId"):
        old_id = str(pending["relayId"])
        pend_to_did = str(pending.get("toDeviceId") or "")
        if pend_to_did and pend_to_did != target_did:
            raise ActiveHandoffConflictError(
                "项目「%s」已有尚未完成的交接记录（%s -> %s），不能同时向另一台设备（%s）发起交棒。"
                % (proj["name"], old_id, pending.get("toAlias") or pend_to_did, dest_alias or target_did),
                relay_id=old_id,
                target_alias=str(pending.get("toAlias") or ""),
            )
        if pending.get("prepared"):
            return _publish_prepared_send(ctx, proj_id, old_id, pending["prepared"], retry=True)

        if pending.get("verificationRequired") or not pending.get("checkpointBeforeReady"):
            use_v3_pend = int(pending.get("protocolVersion") or (3 if _peer_is_v3(target_peer) else 2)) >= 3
            cand_ready_keys = (
                ["v3/inbox/%s/%s/ready.json" % (target_did, old_id), "relays/%s/ready.json" % old_id]
                if use_v3_pend
                else ["relays/%s/ready.json" % old_id]
            )
            adopted_ready = None
            for rk in cand_ready_keys:
                try:
                    adopted_ready = _download_json(provider, rk)
                    break
                except _storage_mod().ObjectNotFoundError:
                    continue
            if adopted_ready is not None:
                return _publish_prepared_send(ctx, proj_id, old_id, {"ready": adopted_ready}, retry=True)
            raise PendingVerificationError(
                "交接 %s 为旧版迁移的待核实发送记录且云端未找到已发布记录。"
                "请确认后运行 relay recover %s --rollback 清理。" % (old_id, old_id),
                relay_id=old_id,
            )

    # 2. Check existing active unacked handoff for this project
    active_rid = str((state.get("activeHandoffs") or {}).get(proj_id) or "")
    sent_relays = state.get("sentRelays") or {}
    if not active_rid:
        ho = proj.get("handoff")
        if isinstance(ho, dict) and ho.get("relayId"):
            cand_rid = str(ho["relayId"])
            sr_cand = sent_relays.get(cand_rid)
            if isinstance(sr_cand, dict) and not sr_cand.get("acked"):
                active_rid = cand_rid

    if active_rid and isinstance(sent_relays.get(active_rid), dict) and not sent_relays[active_rid].get("acked"):
        r_act = sent_relays[active_rid]
        act_to_did = str(r_act.get("toDeviceId") or "")
        act_to_alias = str(r_act.get("toAlias") or "")
        if act_to_did != target_did:
            raise ActiveHandoffConflictError(
                "项目「%s」已有尚未签收的在途交接（%s -> %s），不能同时向另一台设备（%s）发起交棒。"
                % (proj["name"], active_rid, act_to_alias or act_to_did, dest_alias or target_did),
                relay_id=active_rid,
                target_alias=act_to_alias,
            )
        return _idempotent_resend_active(ctx, proj_id, active_rid, r_act)

    # 3. Verify ownership & path
    if proj.get("device") != here_id:
        owner_cfg = L.dev(led["config"], proj.get("device"))
        raise RelayError(
            "项目「%s」的最新现场登记在 %s（%s），不在本机（%s）。请从那台设备发起交棒；若现场确实在本机，请先运行 relay set %s --device %s。"
            % (proj["name"], owner_cfg.get("name", proj.get("device")), proj.get("device"), here_id, proj_id, here_id),
            code="wrong_device",
        )

    raw_proj_path = _project_path_for_device(proj, here_id, identity["deviceId"])
    if not raw_proj_path:
        raise RelayError(
            "项目「%s」在本机（%s）尚未登记路径，请先运行 relay set %s --path-here <目录>。"
            % (proj["name"], here_id, proj_id),
            code="missing_path",
        )
    proj_path = _canon_local_path(raw_proj_path)
    if not os.path.isdir(proj_path):
        raise RelayError("项目「%s」的本机目录不存在：%s" % (proj["name"], proj_path), code="missing_path")

    # 4. Strict protocol selection: v3 if target peer supports v3, NEVER fallback to v2 on error; else v2 adapter
    use_v3 = _peer_is_v3(target_peer)
    if not use_v3:
        if here_id not in ("a", "b") or dest_alias not in ("a", "b"):
            raise RelayError(
                "对端设备「%s」仅支持旧版 v2 协议，旧版 v2 适配器仅支持固定别名 a 与 b；请升级对端设备至 v3 后再向动态别名设备交棒。"
                % (dest_alias or target_did),
                code="v2_incompatible_alias",
            )

    user_h = os.environ.get("RELAY_DESK_USER_HOME") or None
    facts = scan.git_facts(proj_path)
    pres = scan.presence(proj_path, home=user_h)

    proj_work = copy.deepcopy(proj)
    proj_work.setdefault("paths", {})[here_id] = proj_path
    proj_work.setdefault("pathsByDeviceId", {})[identity["deviceId"]] = proj_path
    _apply_local_facts_to_project(proj_work, here_id, facts, pres)

    conv_bundle = conv_mod.collect_project_conversations(proj_work, here_id, base, user_home=user_h)
    _merge_archive_checked(conv_mod, base, proj_id, conv_bundle)
    pkg_mod.scan_secrets_before_pack(proj_path, is_git=facts.get("isGit"))

    same_recipient_cp = bool(
        pending.get("relayId")
        and pending.get("toDeviceId") == target_did
        and pending.get("checkpointBeforeReady")
    )
    if not same_recipient_cp:
        day_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        pending = {
            "relayId": L.new_id("r_%s_" % day_str),
            "toAlias": dest_alias,
            "toDeviceId": target_did,
            "parentRelayId": str(proj.get("lastRelayId") or ""),
            "protocolVersion": 3 if use_v3 else 2,
            "checkpointBeforeReady": True,
            # Phase C: the recipient key this handoff is sealed to (receipts, retries).
            "recipientKeyFingerprint": str(target_peer.get("keyFingerprint") or ""),
        }

    relay_id = str(pending["relayId"])
    parent_id = str(pending["parentRelayId"])
    created_at = L.now_iso()
    session_count = len(conv_bundle.get("entries") or [])

    metadata = _build_project_metadata(
        proj_work,
        proj_path,
        here_id,
        dest_alias=dest_alias,
        relay_id=relay_id,
        parent_id=parent_id,
        from_device_id=identity["deviceId"],
        to_device_id=target_did,
    )

    dest_dev_cfg = (
        next(
            (d for d in ((led.get("config") or {}).get("devices") or []) if d.get("id") == dest_alias),
            {},
        )
        if dest_alias
        else {}
    )
    if dest_dev_cfg.get("osExplicit") and dest_dev_cfg.get("os"):
        dest_target_os = str(dest_dev_cfg["os"])
    else:
        dest_target_os = str(target_peer.get("os") or dest_dev_cfg.get("os") or "")
    dest_target_os = dest_target_os or None

    if use_v3:
        target_os = None
        pkg_key = "v3/inbox/%s/%s/package.enc" % (target_did, relay_id)
        ready_key = "v3/inbox/%s/%s/ready.json" % (target_did, relay_id)
    else:
        target_os = dest_target_os
        pkg_key = "relays/%s/package.enc" % relay_id
        ready_key = "relays/%s/ready.json" % relay_id

    cur_ver = str((_load_relay_state(base).get("localCurrentVersion") or {}).get(proj_id) or "")
    day_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    version_id = L.new_id("snap_%s_" % day_str) if use_v3 else relay_id

    with tempfile.TemporaryDirectory() as tmpdir:
        tar_path = os.path.join(tmpdir, "payload.tar.gz")
        enc_path = os.path.join(tmpdir, "package.enc")

        pack_info = pkg_mod.build_payload(
            proj_path,
            tar_path,
            metadata,
            conv_bundle,
            target_os=target_os,
        )
        if use_v3 and dest_target_os:
            dest_os_norm = dest_target_os.strip().lower()
            if dest_os_norm in ("darwin", "macos"):
                dest_os_norm = "mac"
            elif dest_os_norm in ("win32", "nt"):
                dest_os_norm = "windows"
            with tarfile.open(tar_path, "r:gz") as tf:
                mf = tf.extractfile("payload_manifest.json")
                manifest_data = json.loads(mf.read().decode("utf-8")) if mf is not None else {}
            pkg_mod._validate_cross_os_paths(
                manifest_data.get("allPaths") or [],
                dest_os_norm,
                check_case=(dest_os_norm in ("windows", "mac")),
                check_windows=(dest_os_norm == "windows"),
            )
        payload_sha, payload_size = _sha256_file_info(tar_path)
        envelope = crypto.encrypt_package(tar_path, enc_path, identity, target_peer)

        meta_plain = {
            "projectId": proj_id,
            "projectName": proj_work["name"],
            "lastTask": proj_work.get("lastTask") or "",
            "nextAction": proj_work.get("nextAction") or "",
            "branch": pack_info.get("branch") or proj_work.get("branch") or "",
            "dirty": proj_work.get("dirty", 0),
            "sessionCount": session_count,
        }
        meta_enc = crypto.encrypt_meta(meta_plain, identity, target_peer)

        signed_snapshot: Optional[Dict[str, Any]] = None
        if use_v3:
            snap_unsigned = {
                "formatVersion": 3,
                "projectId": proj_id,
                "projectName": proj_work["name"],
                "versionId": version_id,
                "authorDeviceId": identity["deviceId"],
                "authorAlias": here_id,
                "parentVersionIds": [cur_ver] if cur_ver else [],
                "parentRelayId": parent_id,
                "relayId": relay_id,
                "contentSha256": payload_sha,
                "contentSize": payload_size,
                "createdAt": created_at,
            }
            signed_snapshot = crypto.sign_snapshot(
                snap_unsigned,
                identity["signPrivateKey"],
                author_device_id=identity["deviceId"],
            )
            _save_snapshot_to_cache(base, version_id, signed_snapshot, tar_path, metadata, meta_plain)
            ready_unsigned = dict(envelope)
            ready_unsigned.update(
                {
                    "formatVersion": 3,
                    "purpose": "handoff",
                    "deliveryId": relay_id,
                    "relayId": relay_id,
                    "senderDeviceId": identity["deviceId"],
                    "senderAlias": here_id,
                    "fromAlias": here_id,
                    "fromDeviceId": identity["deviceId"],
                    "toAlias": dest_alias,
                    "toDeviceId": target_did,
                    "authorizedDeviceId": target_did,
                    "parentRelayId": parent_id,
                    "isForwarded": False,
                    "createdAt": created_at,
                    "packageKey": pkg_key,
                    "snapshot": signed_snapshot,
                    # Additive and signed (Phase C): lets receivers and retries tell a
                    # recipient key rotation apart from tampering.
                    "recipientKeyFingerprint": str(target_peer.get("keyFingerprint") or ""),
                    "meta": meta_enc,
                }
            )
        else:
            ready_unsigned = dict(envelope)
            ready_unsigned.update(
                {
                    "formatVersion": 1,
                    "relayId": relay_id,
                    "fromAlias": here_id,
                    "fromDeviceId": identity["deviceId"],
                    "toAlias": dest_alias,
                    "toDeviceId": target_did,
                    "parentRelayId": parent_id,
                    "createdAt": created_at,
                    "packageKey": pkg_key,
                    "meta": meta_enc,
                }
            )

        prepared = {
            "protocolVersion": 3 if use_v3 else 2,
            "recipientKeyFingerprint": str(target_peer.get("keyFingerprint") or ""),
            "readyKey": ready_key,
            "snapshotId": version_id,
            "snapshot": signed_snapshot,
            "metaPlain": meta_plain,
            "ready": crypto.sign_json(
                ready_unsigned,
                identity["signPrivateKey"],
                signer_device_id=identity["deviceId"],
            ),
            "sourcePath": proj_path,
            "facts": facts,
            "presence": pres,
            "excludedFiles": pack_info.get("excludedFiles") or [],
            "baseline": {
                "headCommit": pack_info.get("headCommit") or "",
                "folderHashes": pack_info.get("folderHashes") or {},
                "gitSnapshotHash": _compute_git_baseline_fingerprint(proj_path) if pack_info["mode"] == "git" else "",
            },
        }
        if not same_recipient_cp:
            def _save_pending(st_obj: Dict[str, Any]) -> None:
                st_obj["pendingSends"][proj_id] = pending

            _update_relay_state(base, _save_pending)

        provider.upload(enc_path, pkg_key)
        _verify_published_package(provider, ready_unsigned)

        def _checkpoint(st_obj: Dict[str, Any]) -> None:
            st_obj["pendingSends"][proj_id]["prepared"] = prepared

        _update_relay_state(base, _checkpoint)

    return _publish_prepared_send(ctx, proj_id, relay_id, prepared)


def _idempotent_resend_active(ctx, proj_id: str, relay_id: str, r_act: Dict[str, Any]) -> Dict[str, Any]:
    """Verify remote objects of an existing unacked handoff and return original result without repackaging."""
    base, _led_path, _local_cfg, led, here_id, _cloud, identity, target_peer, provider = ctx
    proto_ver = int(r_act.get("protocolVersion") or (3 if _peer_is_v3(target_peer) and str(r_act.get("readyKey") or "").startswith("v3/") else 2))
    ready_key = str(
        r_act.get("readyKey")
        or ("v3/inbox/%s/%s/ready.json" % (target_peer["deviceId"], relay_id) if proto_ver >= 3 else "relays/%s/ready.json" % relay_id)
    )
    signed_ready = r_act.get("signedReady")
    recorded_fp = str(r_act.get("recipientKeyFingerprint") or "")
    if proto_ver >= 3 and r_act.get("snapshotId"):
        # Fix H: re-running send for a stuck handoff also writes any of its own ancestors'
        # records that an older engine left unpublished (receivers reported a missing parent).
        _republish_own_records(provider, base, identity, proj_id, [str(r_act["snapshotId"])])
    if isinstance(signed_ready, dict):
        ready, meta = _verify_outgoing_ready(
            signed_ready, relay_id, proj_id, identity, target_peer, proto_ver=proto_ver, recorded_fp=recorded_fp
        )
        _verify_published_package(provider, ready)
        try:
            remote_ready = _download_json(provider, ready_key)
        except _storage_mod().ObjectNotFoundError:
            with tempfile.TemporaryDirectory() as tmpdir:
                rp = Path(tmpdir) / "ready.json"
                _atomic_write_json(rp, signed_ready)
                provider.upload(str(rp), ready_key)
                if provider.stat(ready_key) is None:
                    raise _storage_mod().StorageError("云端就绪标记（%s）上传后校验未找到该文件。" % ready_key)
        else:
            if remote_ready != signed_ready:
                raise crypto.TamperError("交接包 %s 已有不同的云端就绪记录。" % relay_id)
        saved_res = copy.deepcopy(r_act.get("result") or {})
        if saved_res:
            saved_res["message"] = HANDOFF_DONE_MESSAGE + "。本次重试沿用原交接包。"
            return saved_res
    else:
        remote_ready = _download_json(provider, ready_key)
        ready, meta = _verify_outgoing_ready(
            remote_ready, relay_id, proj_id, identity, target_peer, proto_ver=proto_ver, recorded_fp=recorded_fp
        )
        _verify_published_package(provider, ready)

    dest_alias = str(r_act.get("toAlias") or target_peer.get("alias") or "")
    proj = L.find_project(led, proj_id)
    return {
        "ok": True,
        "relayId": relay_id,
        "parentRelayId": str(proj.get("lastRelayId") or ""),
        "projectId": proj_id,
        "projectName": proj.get("name") or proj_id,
        "from": here_id,
        "to": dest_alias,
        "toDeviceId": target_peer["deviceId"],
        "packageSize": ready["packageSize"],
        "packageSha256": ready["packageSha256"],
        "snapshotId": str(r_act.get("snapshotId") or ""),
        "sessionCount": meta.get("sessionCount", 0),
        "excludedFiles": [],
        "message": HANDOFF_DONE_MESSAGE + "。本次重试沿用原交接包。",
    }


def _publish_prepared_send(ctx, proj_id, relay_id, prepared, retry=False) -> Dict[str, Any]:
    base, led_path, _, _, here_id, cloud, identity, target_peer, provider = ctx
    signed_ready = prepared["ready"]
    proto_ver = int(
        prepared.get("protocolVersion")
        or signed_ready.get("formatVersion")
        or (3 if "snapshot" in signed_ready else 2)
    )
    if proto_ver == 1:
        proto_ver = 2
    recorded_fp = str(signed_ready.get("recipientKeyFingerprint") or prepared.get("recipientKeyFingerprint") or "")
    ready, meta = _verify_outgoing_ready(
        signed_ready, relay_id, proj_id, identity, target_peer, proto_ver=proto_ver, recorded_fp=recorded_fp
    )
    ready_key = str(
        prepared.get("readyKey")
        or ("v3/inbox/%s/%s/ready.json" % (target_peer["deviceId"], relay_id) if proto_ver >= 3 else "relays/%s/ready.json" % relay_id)
    )

    ready_visible = False
    if isinstance(ready.get("snapshot"), dict):
        # Fix H: an own ancestor an older engine left unpublished is written before this record.
        parents = ready["snapshot"].get("parentVersionIds")
        _republish_own_records(provider, base, identity, proj_id,
                               [str(x) for x in parents if isinstance(x, str)] if isinstance(parents, list) else [])
        _put_immutable_json(provider, _snapshot_record_key(ready["snapshot"]["versionId"]), ready["snapshot"])
    if retry:
        _verify_published_package(provider, ready)
        try:
            remote_ready = _download_json(provider, ready_key)
        except _storage_mod().ObjectNotFoundError:
            pass
        else:
            if remote_ready != signed_ready:
                raise crypto.TamperError("交接包 %s 已有不同的云端就绪记录。" % relay_id)
            ready_visible = True

    if not ready_visible:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ready.json"
            _atomic_write_json(path, signed_ready)
            provider.upload(str(path), ready_key)
            if provider.stat(ready_key) is None:
                raise _storage_mod().StorageError("云端就绪标记（%s）上传后校验未找到该文件。" % ready_key)

    dest_alias = str(ready.get("toAlias") or target_peer.get("alias") or "")
    target_did = str(target_peer["deviceId"])
    parent_id = str(ready.get("parentRelayId") or "")
    created_at = str(ready.get("createdAt") or L.now_iso())
    baseline = prepared.get("baseline") or {}
    snap_obj = prepared.get("snapshot") or ready.get("snapshot")
    snap_id = str(prepared.get("snapshotId") or (snap_obj.get("versionId") if isinstance(snap_obj, dict) else "") or relay_id)

    def _upd_sender_ledger(l: Dict[str, Any]) -> None:
        for p in l.get("projects") or []:
            if p.get("id") != proj_id:
                continue
            if prepared.get("sourcePath"):
                p.setdefault("paths", {})[here_id] = prepared["sourcePath"]
                p.setdefault("pathsByDeviceId", {})[identity["deviceId"]] = prepared["sourcePath"]
            _apply_local_facts_to_project(p, here_id, prepared.get("facts"), prepared.get("presence"))
            p["lastRelayId"] = relay_id
            p["handoff"] = {
                "tag": relay_id,
                "relayId": relay_id,
                "from": here_id,
                "fromDeviceId": identity["deviceId"],
                "to": dest_alias,
                "toDeviceId": target_did,
                "transport": "cloud",
                "state": "transit",
                "parentRelayId": parent_id,
                "hadDirty": bool(meta.get("dirty")),
                "branch": meta.get("branch") or "",
                "baseCommit": baseline.get("headCommit") or "",
                "createdAt": created_at,
            }
            L.touch(p, here_id)
        migrated = devices.migrate_ledger_identities(l, local_ident=identity, cloud_cfg=cloud)
        l.clear()
        l.update(migrated)

    L.update(led_path, _upd_sender_ledger)

    result_obj: Dict[str, Any] = {
        "ok": True,
        "relayId": relay_id,
        "parentRelayId": parent_id,
        "projectId": proj_id,
        "projectName": meta["projectName"],
        "from": here_id,
        "fromDeviceId": identity["deviceId"],
        "to": dest_alias,
        "toDeviceId": target_did,
        "protocolVersion": proto_ver,
        "snapshotId": snap_id if proto_ver >= 3 else "",
        "packageSize": ready["packageSize"],
        "packageSha256": ready["packageSha256"],
        "sessionCount": meta.get("sessionCount", 0),
        "excludedFiles": prepared.get("excludedFiles") or [],
        "message": HANDOFF_DONE_MESSAGE + ("。本次重试沿用原交接包。" if retry else ""),
    }
    reissue_of = str(prepared.get("reissueOf") or "")
    if reissue_of:
        result_obj["reissued"] = True
        result_obj["supersedesRelayId"] = reissue_of
        result_obj["message"] = ("已为目标设备当前公钥重新签发交接（取代 %s，原交接包保持不变）。" % reissue_of
                                 + HANDOFF_DONE_MESSAGE)

    def _finalize_send_state(st_obj: Dict[str, Any]) -> None:
        st_obj.setdefault("pendingSends", {}).pop(proj_id, None)
        if reissue_of:
            sent_map = st_obj.setdefault("sentRelays", {})
            orig = sent_map.get(reissue_of)
            if not isinstance(orig, dict):
                # The original was prepared but never confirmed published: keep it on record (unacked) so a
                # late receipt is still seen, and it can never be mistaken for the active handoff.
                o = prepared.get("reissueOrigin") or {}
                orig = {"projectId": proj_id, "toAlias": str(o.get("toAlias") or dest_alias),
                        "toDeviceId": str(o.get("toDeviceId") or target_did), "protocolVersion": 3,
                        "readyKey": str(o.get("readyKey") or ""), "packageKey": str(o.get("packageKey") or ""),
                        "snapshotId": str(o.get("snapshotId") or ""), "packageSha256": str(o.get("packageSha256") or ""),
                        "signedReady": o.get("signedReady"), "sentAt": str(o.get("sentAt") or ""), "acked": False,
                        "ackedAt": "", "recipientKeyFingerprint": str(o.get("recipientKeyFingerprint") or ""),
                        "publishUnconfirmed": True}
                sent_map[reissue_of] = orig
            if not orig.get("acked"):
                orig["supersededBy"] = relay_id
                orig["supersededAt"] = L.now_iso()
                orig["supersededReason"] = str((prepared.get("reissueOrigin") or {}).get("reason") or "")
        st_obj.setdefault("sentRelays", {})[relay_id] = {
            "projectId": proj_id,
            "toAlias": dest_alias,
            "toDeviceId": target_did,
            "protocolVersion": proto_ver,
            "readyKey": ready_key,
            "packageKey": str(ready.get("packageKey") or ""),
            "snapshotId": snap_id,
            "packageSha256": ready["packageSha256"],
            "signedReady": signed_ready,
            "result": result_obj,
            "sentAt": created_at,
            "acked": False,
            "ackedAt": "",
            "recipientKeyFingerprint": recorded_fp or str(target_peer.get("keyFingerprint") or ""),
        }
        if reissue_of:
            st_obj["sentRelays"][relay_id]["supersedesRelayId"] = reissue_of
            st_obj["sentRelays"][relay_id]["supersedesRelayIds"] = list(signed_ready.get("supersedesRelayIds")
                                                                          or [reissue_of])
        st_obj.setdefault("activeHandoffs", {})[proj_id] = relay_id
        st_obj["projectBaselines"][proj_id] = baseline
        if isinstance(snap_obj, dict) and snap_obj.get("versionId"):
            _record_version(
                st_obj,
                proj_id,
                {
                    "versionId": str(snap_obj["versionId"]),
                    "projectId": proj_id,
                    "authorDeviceId": str(snap_obj.get("authorDeviceId") or identity["deviceId"]),
                    "parentVersionIds": list(snap_obj.get("parentVersionIds") or []),
                    "parentRelayId": parent_id,
                    "relayId": relay_id,
                    "contentSha256": str(snap_obj.get("contentSha256") or ""),
                    "createdAt": created_at,
                    "recordSha256": crypto.snapshot_record_digest(snap_obj),
                    "trust": "current",
                    "authorKeyFingerprint": DS.norm_fp(_identity_fingerprint(identity)),
                    "recordPublished": isinstance(ready.get("snapshot"), dict),
                },
            )
            st_obj.setdefault("localCurrentVersion", {})[proj_id] = str(snap_obj["versionId"])

    _update_relay_state(base, _finalize_send_state)

    if proto_ver >= 3 and isinstance(snap_obj, dict):
        try:
            _refresh_have(provider, base, identity)
        except Exception:
            pass
        cdir = _snapshot_cache_dir(base, snap_id)
        cached_tar = cdir / "payload.tar.gz"
        if cached_tar.is_file():
            meta_plain = prepared.get("metaPlain") or {
                "projectId": proj_id,
                "projectName": meta["projectName"],
                "lastTask": meta.get("lastTask") or "",
                "nextAction": meta.get("nextAction") or "",
                "branch": meta.get("branch") or "",
                "dirty": meta.get("dirty", 0),
                "sessionCount": meta.get("sessionCount", 0),
            }
            item = {"versionId": snap_id, "snapshot": snap_obj, "tarPath": str(cached_tar), "metaPlain": meta_plain,
                    "metadata": {}}
            try:
                outbound = _Outbound(provider, identity, here_id, base, _delivery_book(base, identity), cloud)
                if isinstance(ready.get("snapshot"), dict):
                    outbound.verified_records.add(snap_id)  # written and read back above
            except Exception:
                outbound = None
            for bystander in _paired_active_peers(cloud):
                if outbound is None or bystander["deviceId"] == target_did or not _peer_is_v3(bystander):
                    continue
                try:
                    outbound.deliver(bystander, item, have=None, allow_cover=False, adopt_legacy=False)
                except Exception:
                    pass  # best effort: relay sync delivers to this bystander later

    return result_obj


# ---------------------------------------------------------------- 6. list_inbox


def _has_v2_peers_or_legacy_state(base: Path, cloud: Dict[str, Any], state: Dict[str, Any]) -> bool:
    for p in _paired_active_peers(cloud):
        if not _peer_is_v3(p):
            return True
    if (base / "cloud.v2.backup.json").exists() or (base / "relay_state.v2.backup.json").exists():
        return True
    for entry in (state.get("sentRelays") or {}).values():
        if isinstance(entry, dict) and entry.get("protocolVersion", 2) == 2:
            return True
    for entry in (state.get("receivedRelays") or {}).values():
        if isinstance(entry, dict) and entry.get("protocolVersion", 2) == 2:
            return True
    if state.get("appliedReceives"):
        return True
    return False


def list_inbox(
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Dict[str, Any]:
    """Sync receipts and list incoming claimable cloud relay handoffs for this device."""
    st_mod = _storage_mod()
    base, led_path, local_cfg, _led, here_id, cloud, identity, provider = _require_paired_context(
        ledger_path, local, local_home, require_any_peer=True
    )

    synced = sync_receipts(
        provider,
        identity,
        peer_desc=None,
        ledger_path=led_path,
        local=local_cfg,
        local_home=base,
    )
    state = _load_relay_state(base)
    received_relays = state.get("receivedRelays") or {}
    sent_relays = state.get("sentRelays") or {}
    processed_deliveries = state.get("processedDeliveries") or {}
    archived_ids = dict(state.get("archivedRelayIds") or {})
    peers_map = cloud.get("peers") if isinstance(cloud.get("peers"), dict) else {}

    incoming: List[Dict[str, Any]] = []
    seen_rids: Set[str] = set()
    newly_archived: Dict[str, bool] = {}

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Scan v3 personal inbox
        v3_prefix = "v3/inbox/%s/" % identity["deviceId"]
        v3_objs = provider.list_objects(v3_prefix)
        v3_by_rid: Dict[str, Dict[str, Any]] = {}
        for obj in v3_objs:
            key = str(obj.key or "").lstrip("/")
            parts = key.split("/")
            if len(parts) == 5 and parts[0] == "v3" and parts[1] == "inbox" and parts[2] == identity["deviceId"] and parts[3]:
                v3_by_rid.setdefault(parts[3], {})[parts[4]] = obj

        for rid in sorted(v3_by_rid.keys()):
            files = v3_by_rid[rid]
            if "ready.json" not in files or "package.enc" not in files:
                continue
            if rid.startswith(("sync_", "fwd_")) or rid in processed_deliveries:
                continue
            try:
                V.validate_delivery_id(rid, "relayId")
            except V.ValidationError:
                continue
            rec_local = received_relays.get(rid)
            if isinstance(rec_local, dict) and rec_local.get("receiptUploaded"):
                continue
            pending_ack = bool(isinstance(rec_local, dict) and not rec_local.get("receiptUploaded"))

            if not pending_ack and "receipt.json" in files:
                rec_tmp = os.path.join(tmpdir, "v3_%s_rec.json" % rid)
                try:
                    provider.download("%s%s/receipt.json" % (v3_prefix, rid), rec_tmp)
                    with open(rec_tmp, "r", encoding="utf-8-sig") as fh:
                        raw_rec = json.load(fh)
                    crypto.verify_json(
                        raw_rec,
                        identity["verifyPublicKey"],
                        expected_device_id=identity["deviceId"],
                    )
                    continue
                except Exception:
                    pass

            ready_tmp = os.path.join(tmpdir, "v3_%s_ready.json" % rid)
            try:
                provider.download("%s%s/ready.json" % (v3_prefix, rid), ready_tmp)
                with open(ready_tmp, "r", encoding="utf-8-sig") as fh:
                    raw_ready = json.load(fh)
            except (st_mod.StorageError, OSError, ValueError, UnicodeDecodeError):
                continue

            if not isinstance(raw_ready, dict):
                continue
            if raw_ready.get("purpose") and raw_ready.get("purpose") != "handoff":
                continue
            if raw_ready.get("fromDeviceId") == identity["deviceId"]:
                continue
            auth_did = raw_ready.get("authorizedDeviceId") or raw_ready.get("toDeviceId")
            if auth_did and auth_did != identity["deviceId"]:
                continue

            sender_did = str(raw_ready.get("senderDeviceId") or raw_ready.get("fromDeviceId") or "")
            sender_peer = peers_map.get(sender_did)
            if not isinstance(sender_peer, dict) or not sender_peer.get("paired") or sender_peer.get("status") != "paired":
                incoming.append(
                    {
                        "relayId": rid,
                        "parentRelayId": str(raw_ready.get("parentRelayId") or ""),
                        "fromAlias": str(raw_ready.get("fromAlias") or ""),
                        "fromDeviceId": sender_did,
                        "toAlias": str(raw_ready.get("toAlias") or here_id),
                        "createdAt": str(raw_ready.get("createdAt") or ""),
                        "packageSize": files["package.enc"].size,
                        "projectId": "",
                        "projectName": "（发送方未配对或密钥已变更）",
                        "lastTask": "",
                        "nextAction": "",
                        "branch": "",
                        "dirty": 0,
                        "sessionCount": 0,
                        "pendingAck": pending_ack,
                        "tampered": True,
                        "error": "发送方设备 %s 未配对或密钥变更待重新确认。" % sender_did,
                    }
                )
                seen_rids.add(rid)
                continue

            try:
                verified_ready = crypto.verify_json(
                    raw_ready,
                    sender_peer["verifyPublicKey"],
                    expected_device_id=sender_did,
                )
                if verified_ready.get("relayId") != rid:
                    raise crypto.TamperError("ready.json 中的 relayId 与目录不一致。")
                pkg_obj_size = files["package.enc"].size
                if pkg_obj_size != verified_ready.get("packageSize"):
                    raise crypto.TamperError("云端 package.enc 大小与签名记录不一致。")
                bound_fp = DS.norm_fp(verified_ready.get("recipientKeyFingerprint"))
                if bound_fp and bound_fp != DS.norm_fp(_identity_fingerprint(identity)):
                    raise KeyChangedError(
                        "交接包是发给本机换钥前公钥的，无法领取；请发送方为本机新公钥重新签发交接（原交接包保持不变）。",
                        peer_id=sender_did, code="sealed_to_retired_key")
                meta = crypto.decrypt_meta(verified_ready.get("meta") or {}, identity, sender_peer)
                snap_raw = verified_ready.get("snapshot")
                snap_id = ""
                if isinstance(snap_raw, dict):
                    # Phase C (S7): same author rules as receive, including the author's trust status.
                    v_snap, _proof = _verify_author_snapshot(snap_raw, identity, cloud, state.get("versionGraph"),
                                                             use="handoff_incoming", sender_did=sender_did)
                    snap_id = str(v_snap["versionId"])
                chain = _supersedes_chain(verified_ready)
                incoming.append(
                    {
                        "relayId": rid,
                        "parentRelayId": str(verified_ready.get("parentRelayId") or ""),
                        "snapshotId": snap_id,
                        "supersedesRelayIds": chain,
                        "protocolVersion": 3,
                        "fromAlias": str(verified_ready.get("fromAlias") or sender_peer.get("alias") or ""),
                        "fromDeviceId": sender_did,
                        "toAlias": str(verified_ready.get("toAlias") or here_id),
                        "toDeviceId": identity["deviceId"],
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
                seen_rids.add(rid)
            except (crypto.CryptoError, RelayError) as e:
                incoming.append(
                    {
                        "relayId": rid,
                        "parentRelayId": str(raw_ready.get("parentRelayId") or ""),
                        "fromAlias": str(raw_ready.get("fromAlias") or ""),
                        "toAlias": str(raw_ready.get("toAlias") or here_id),
                        "createdAt": str(raw_ready.get("createdAt") or ""),
                        "packageSize": files["package.enc"].size,
                        "projectId": "",
                        "projectName": ("（发给本机换钥前公钥，需重新签发）"
                                        if getattr(e, "code", "") == "sealed_to_retired_key"
                                        else "（签名或加密校验异常）"),
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
                seen_rids.add(rid)

        # 2. Scan legacy `relays/` ONLY if any paired peer is v2-only or legacy state exists
        if _has_v2_peers_or_legacy_state(base, cloud, state):
            objs = provider.list_objects("relays/")
            by_relay: Dict[str, Dict[str, Any]] = {}
            for obj in objs:
                key = str(obj.key or "").lstrip("/")
                parts = key.split("/")
                if len(parts) == 3 and parts[0] == "relays" and parts[1]:
                    by_relay.setdefault(parts[1], {})[parts[2]] = obj

            for rid in sorted(by_relay.keys()):
                if rid in seen_rids:
                    continue
                files = by_relay[rid]
                if "ready.json" not in files or "package.enc" not in files:
                    continue
                if rid in sent_relays:
                    if rid not in archived_ids:
                        newly_archived[rid] = True
                    continue
                rec_local = received_relays.get(rid)
                if isinstance(rec_local, dict) and rec_local.get("receiptUploaded"):
                    continue
                if rid in archived_ids:
                    continue

                pending_ack = bool(isinstance(rec_local, dict) and not rec_local.get("receiptUploaded"))
                if not pending_ack and "receipt.json" in files:
                    rec_tmp = os.path.join(tmpdir, "v2_%s_rec.json" % rid)
                    try:
                        provider.download("relays/%s/receipt.json" % rid, rec_tmp)
                        with open(rec_tmp, "r", encoding="utf-8-sig") as fh:
                            raw_rec = json.load(fh)
                        rec_did = str(raw_rec.get("receiverDeviceId") or raw_rec.get("signerDeviceId") or "")
                        if rec_did == identity["deviceId"]:
                            crypto.verify_json(
                                raw_rec,
                                identity["verifyPublicKey"],
                                expected_device_id=identity["deviceId"],
                            )
                            newly_archived[rid] = True
                            continue
                        rec_peer = peers_map.get(rec_did)
                        if isinstance(rec_peer, dict) and rec_peer.get("verifyPublicKey"):
                            crypto.verify_json(
                                raw_rec,
                                rec_peer["verifyPublicKey"],
                                expected_device_id=rec_did,
                            )
                            newly_archived[rid] = True
                            continue
                    except Exception:
                        pass

                ready_tmp = os.path.join(tmpdir, "v2_%s_ready.json" % rid)
                try:
                    provider.download("relays/%s/ready.json" % rid, ready_tmp)
                    with open(ready_tmp, "r", encoding="utf-8-sig") as fh:
                        raw_ready = json.load(fh)
                except (st_mod.StorageError, OSError, ValueError, UnicodeDecodeError):
                    continue

                if not isinstance(raw_ready, dict):
                    continue
                if raw_ready.get("fromDeviceId") == identity["deviceId"]:
                    newly_archived[rid] = True
                    continue
                to_dev_id = raw_ready.get("toDeviceId")
                to_al = raw_ready.get("toAlias")
                if to_dev_id and to_dev_id != identity["deviceId"] and to_al != here_id:
                    newly_archived[rid] = True
                    continue
                if not to_dev_id and to_al != here_id:
                    newly_archived[rid] = True
                    continue

                sender_did = str(raw_ready.get("fromDeviceId") or "")
                sender_peer = peers_map.get(sender_did)
                if not isinstance(sender_peer, dict) or not sender_peer.get("paired") or sender_peer.get("status") != "paired":
                    continue

                try:
                    verified_ready = crypto.verify_json(
                        raw_ready,
                        sender_peer["verifyPublicKey"],
                        expected_device_id=sender_did,
                    )
                    if verified_ready.get("relayId") != rid:
                        raise crypto.TamperError("ready.json 中的 relayId 与目录不一致。")
                    pkg_obj_size = files["package.enc"].size
                    if pkg_obj_size != verified_ready.get("packageSize"):
                        raise crypto.TamperError("云端 package.enc 大小与签名记录不一致。")
                    meta = crypto.decrypt_meta(verified_ready.get("meta") or {}, identity, sender_peer)
                    incoming.append(
                        {
                            "relayId": rid,
                            "parentRelayId": str(verified_ready.get("parentRelayId") or ""),
                            "protocolVersion": 2,
                            "fromAlias": str(verified_ready.get("fromAlias") or ""),
                            "fromDeviceId": sender_did,
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

    if newly_archived:
        def _save_arch(st_obj: Dict[str, Any]) -> None:
            st_obj.setdefault("archivedRelayIds", {}).update(newly_archived)

        _update_relay_state(base, _save_arch)

    # Phase D: a handoff replaced by a verified re-issue (or by one this device claimed) is not offered.
    superseded_by: Dict[str, str] = {}
    for item in incoming:
        if not item.get("tampered"):
            for old_rid in item.get("supersedesRelayIds") or []:
                superseded_by[str(old_rid)] = str(item["relayId"])
    for got_rid, ent in received_relays.items():
        for old_rid in (ent.get("supersedesRelayIds") or []) if isinstance(ent, dict) else []:
            superseded_by.setdefault(str(old_rid), str(got_rid))
    superseded = [{"relayId": it["relayId"], "supersededBy": superseded_by[it["relayId"]]}
                  for it in incoming if it.get("relayId") in superseded_by]
    incoming = [it for it in incoming if it.get("relayId") not in superseded_by]
    incoming.sort(key=lambda x: (x.get("createdAt") or "", x.get("relayId") or ""))
    return {"incoming": incoming, "syncedReceipts": synced, "superseded": superseded}


# ---------------------------------------------------------------- 7. receive_relay & recover_relay


def _upload_and_verify_receipt(
    provider,
    relay_id: str,
    signed_receipt: Dict[str, Any],
    proj_name: str,
    dest_dir: str,
    receipt_key: Optional[str] = None,
) -> None:
    st_mod = _storage_mod()
    rkey = receipt_key or ("relays/%s/receipt.json" % relay_id)
    try:
        with tempfile.TemporaryDirectory() as rtmp:
            rpath = os.path.join(rtmp, "receipt.json")
            _atomic_write_json(Path(rpath), signed_receipt)
            provider.upload(rpath, rkey)
            if provider.stat(rkey) is None:
                raise st_mod.StorageError("回执上传后远端校验未找到文件：%s" % rkey)
    except Exception as e:
        raise ReceiptUploadError(
            relay_id,
            "项目「%s」已在本地应用成功（%s），但上传签收回执失败：%s。"
            "请稍后重新运行 relay receive %s 完成回执补传，不会覆盖本地目录改动。"
            % (proj_name or relay_id, dest_dir, e, relay_id),
        )


def _directory_identity(path: str) -> List[int]:
    info = os.stat(path)
    return [info.st_dev, info.st_ino]


def _check_receive_target(pkg_mod, manifest, state, proj_id, dest_dir, existing_here_canon, proj_name) -> None:
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
            # workspace_tx keeps Git-ignored files as preservedEntries and refuses collisions.
            preserve_ignored=True,
        )
    except pkg_mod.TargetDirtyError as e:
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
            if "--into" not in msg:
                msg = "%s 原目录已完整保留；如需保留本地改动并接棒到新目录，请加 --into <新目录>。" % msg
            dirty_err = pkg_mod.TargetDirtyError(
                msg,
                target_path=getattr(e, "target_path", dest_dir),
                reasons=getattr(e, "reasons", []),
            )
            dirty_err.project_id = proj_id
            dirty_err.project_name = proj_name
            raise dirty_err


def _locate_delivery_for_receive(provider, identity: Dict[str, Any], here_id: str, relay_id: str) -> Tuple[int, str, str]:
    """Return `(proto_ver, ready_key, expected_pkg_key)` or raise UnauthorizedReceiverError / RelayError."""
    my_did = identity["deviceId"]
    v3_ready = "v3/inbox/%s/%s/ready.json" % (my_did, relay_id)
    if provider.stat(v3_ready) is not None:
        return 3, v3_ready, "v3/inbox/%s/%s/package.enc" % (my_did, relay_id)

    v2_ready = "relays/%s/ready.json" % relay_id
    if provider.stat(v2_ready) is not None:
        return 2, v2_ready, "relays/%s/package.enc" % relay_id

    for obj in provider.list_objects("v3/inbox/"):
        key = str(obj.key or "").lstrip("/")
        parts = key.split("/")
        if len(parts) == 5 and parts[0] == "v3" and parts[1] == "inbox" and parts[3] == relay_id and parts[4] == "ready.json":
            raise UnauthorizedReceiverError(
                "交接包 %s 未授权给当前设备（%s / %s）领取。" % (relay_id, here_id, my_did)
            )

    raise RelayError(
        "找不到可领取的交接包 %s（云端不存在 ready.json，可能尚未上传完成）。" % relay_id,
        code="relay_not_found",
    )


def _supersedes_chain(verified_ready: Dict[str, Any]) -> List[str]:
    """Relay IDs a verified (re-issued) handoff ready replaces, oldest first; [] for an ordinary handoff."""
    raw = verified_ready.get("supersedesRelayIds")
    chain = [str(x) for x in raw if isinstance(x, str) and x] if isinstance(raw, list) else []
    direct = verified_ready.get("supersedesRelayId")
    if isinstance(direct, str) and direct and direct not in chain:
        chain.append(direct)
    for rid in chain:
        _check_remote(V.validate_token, rid, "supersedesRelayIds", 80)
    return chain


def _claimed_here(base: Path, state: Dict[str, Any], relay_id: str) -> bool:
    """This device claimed (or started claiming) ``relay_id``: local receive record or an open transaction."""
    if isinstance((state.get("receivedRelays") or {}).get(relay_id), dict):
        return True
    if isinstance((state.get("appliedReceives") or {}).get(relay_id), dict):
        return True
    try:
        if workspace_tx.has_transaction(base, relay_id):
            return workspace_tx.inspect_transaction(base, relay_id).get("state") != "rolled_back"
    except workspace_tx.WorkspaceTxError:
        return True
    return False


def _refuse_if_superseded_claim(base: Path, provider, identity: Dict[str, Any], state: Dict[str, Any],
                                relay_id: str, chain: List[str]) -> None:
    """A re-issue is refused when the original, or another re-issue of it, was already claimed here."""
    for rid in chain:
        claimed = _claimed_here(base, state, rid)
        if not claimed and provider.stat("v3/inbox/%s/%s/receipt.json" % (identity["deviceId"], rid)) is not None:
            claimed = True  # our own receipt for the original is in the cloud
        if claimed:
            raise RelayError(
                "交接 %s 是 %s 的重新签发版本，而本机已经领取过 %s；不能再次领取。" % (relay_id, rid, rid),
                code="superseded_original_claimed")
    for other_id, other in (state.get("receivedRelays") or {}).items():
        if other_id != relay_id and isinstance(other, dict) and \
                set(str(x) for x in (other.get("supersedesRelayIds") or [])) & set(chain):
            raise RelayError(
                "交接 %s 与本机已领取的 %s 取代的是同一个原交接，不能再次领取。" % (relay_id, other_id),
                code="superseded_original_claimed")


def receive_relay(
    relay_id: str,
    target_path: Optional[str] = None,
    into_path: Optional[str] = None,
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Dict[str, Any]:
    """Safely verify, decrypt, transactionally apply, archive conversations, and acknowledge a relay."""
    relay_id = str(relay_id or "").strip()
    if not RELAY_ID_RE.match(relay_id) or relay_id in (".", ".."):
        raise RelayError("无效的交接包编号：%r" % relay_id)
    try:
        V.validate_token(relay_id, "relayId", max_len=80)
    except V.ValidationError as e:
        raise RelayError("无效的交接包编号：%r（%s）" % (relay_id, e.message), code="invalid_relay_id")

    ctx = _require_paired_context(ledger_path, local, local_home, require_any_peer=True)
    with _workspace_operation_after_sync_round(ctx[0]):
        return _receive_relay(relay_id, target_path, into_path, ctx)


def _receive_relay(relay_id: str, target_path: Optional[str], into_path: Optional[str], ctx) -> Dict[str, Any]:
    st_mod = _storage_mod()
    conv_mod = _conversations_mod()
    pkg_mod = _packager_mod()
    base, led_path, _local_cfg, _led0, here_id, cloud, identity, provider = ctx
    peers_map = cloud.get("peers") if isinstance(cloud.get("peers"), dict) else {}

    state = _load_relay_state(base)
    rec_entry = (state.get("receivedRelays") or {}).get(relay_id)
    applied_v2 = (state.get("appliedReceives") or {}).get(relay_id)

    if workspace_tx.has_transaction(base, relay_id):
        phase = workspace_tx.load_and_verify_journal(base, relay_id)["phase"]
        if phase == workspace_tx.PHASE_ROLLING_BACK or (phase == workspace_tx.PHASE_ROLLED_BACK and rec_entry):
            raise workspace_tx.WorkspaceTxError(
                "交接回滚尚未完成，请运行 relay recover %s --rollback，不能补传签收回执。" % relay_id,
                code="rollback_in_progress",
            )

    # 1. Pending receipt upload recovery (local apply & ledger already done)
    if isinstance(rec_entry, dict) and not rec_entry.get("receiptUploaded"):
        signed_receipt = rec_entry.get("signedReceipt")
        saved_result = copy.deepcopy(rec_entry.get("result") or {})
        saved_proj_name = str(saved_result.get("projectName") or rec_entry.get("projectId") or relay_id)
        saved_dest = str(rec_entry.get("targetPath") or saved_result.get("targetPath") or "")
        if not isinstance(signed_receipt, dict):
            raise RelayError("本地回执恢复记录损坏（缺少 signedReceipt）：%s" % relay_id)
        proto_ver_rec = int(rec_entry.get("protocolVersion") or (3 if str(rec_entry.get("receiptKey") or "").startswith("v3/") else 2))
        rkey_rec = str(
            rec_entry.get("receiptKey")
            or ("v3/inbox/%s/%s/receipt.json" % (identity["deviceId"], relay_id) if proto_ver_rec >= 3 else "relays/%s/receipt.json" % relay_id)
        )
        workspace_tx.maybe_inject_fault("rx_before_receipt")
        _upload_and_verify_receipt(provider, relay_id, signed_receipt, saved_proj_name, saved_dest, receipt_key=rkey_rec)

        def _mark_recovered(st_obj: Dict[str, Any]) -> None:
            ent = st_obj.setdefault("receivedRelays", {}).get(relay_id)
            if isinstance(ent, dict):
                ent["receiptUploaded"] = True

        _update_relay_state(base, _mark_recovered)
        if workspace_tx.has_transaction(base, relay_id):
            workspace_tx.archive_completed_transaction(base, relay_id)
        return saved_result

    if not workspace_tx.has_transaction(base, relay_id):
        try:
            sync_receipts(
                provider,
                identity,
                peer_desc=None,
                ledger_path=led_path,
                local=_local_cfg,
                local_home=base,
            )
        except Exception:
            pass
        state = _load_relay_state(base)
        rec_entry = (state.get("receivedRelays") or {}).get(relay_id)
        applied_v2 = (state.get("appliedReceives") or {}).get(relay_id)

    # 2. Reject duplicate receive (local state or remote receipt signed by self)
    if isinstance(rec_entry, dict) and rec_entry.get("receiptUploaded"):
        raise DuplicateReceiveError("交接包 %s 已经领取并签收，不能重复领取。" % relay_id, relay_id=relay_id)

    for dup_rkey in (
        "v3/inbox/%s/%s/receipt.json" % (identity["deviceId"], relay_id),
        "relays/%s/receipt.json" % relay_id,
    ):
        if provider.stat(dup_rkey) is not None:
            with tempfile.TemporaryDirectory() as ctmp:
                cpath = os.path.join(ctmp, "receipt.json")
                try:
                    provider.download(dup_rkey, cpath)
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

    # 2b. Phase D: a handoff this device already claimed in its re-issued form is never claimed again.
    for other_id, other in sorted((state.get("receivedRelays") or {}).items()):
        if isinstance(other, dict) and relay_id in [str(x) for x in (other.get("supersedesRelayIds") or [])]:
            raise RelayError(
                "交接 %s 已由重新签发的交接 %s 取代，本机已领取后者，不能再领取原交接。" % (relay_id, other_id),
                code="handoff_superseded")

    # 3. Check cached snapshot unauthorized receive attempt if relay_id is a snap_... id not in inbox
    if _snapshot_cache_dir(base, relay_id).is_dir():
        v3_check = "v3/inbox/%s/%s/ready.json" % (identity["deviceId"], relay_id)
        v2_check = "relays/%s/ready.json" % relay_id
        if provider.stat(v3_check) is None and provider.stat(v2_check) is None:
            raise UnauthorizedReceiverError("快照 %s 仅为缓存同步版本，未授权作为交接包领取。" % relay_id)

    # 4. Check existing workspace_tx transaction BEFORE downloading from cloud when possible
    tx_id_to_use = relay_id
    if workspace_tx.has_transaction(base, relay_id):
        tx_info = workspace_tx.inspect_transaction(base, relay_id)
        if into_path:
            canon_into = _canon_local_path(into_path)
            canon_tx = _canon_local_path(tx_info.get("targetPath") or "")
            if canon_into != canon_tx and tx_info["state"] != "rolled_back":
                # Keep <local_home>/transactions/<relay_id> intact for orig_path!
                tx_id_to_use = "%s__into_%s" % (
                    relay_id,
                    hashlib.sha256(canon_into.encode("utf-8")).hexdigest()[:8],
                )
        if tx_id_to_use == relay_id and tx_info["state"] != "rolled_back":
            # Read the recovery record before resuming, so a damaged record stops
            # the receive before any further workspace write.
            meta_disk = workspace_tx.load_transaction_meta(base, relay_id)
            extra = meta_disk.get("extra") or {}
            apply_res = workspace_tx.resume_transaction(base, relay_id)
            journal = workspace_tx.load_and_verify_journal(base, relay_id)
            dest_dir = str(journal["targetPath"])
            proj_id = str(journal["projectId"])
            verified_ready = extra.get("verifiedReady") or {}
            metadata = extra.get("metadata") or {}
            conv_bundle = extra.get("conversations") or {"projectId": proj_id, "entries": []}
            proto_ver = int(extra.get("protocolVersion") or 2)
            verified_snap = extra.get("snapshot")
            receipt_key = str(
                extra.get("receiptKey")
                or ("v3/inbox/%s/%s/receipt.json" % (identity["deviceId"], relay_id) if proto_ver >= 3 else "relays/%s/receipt.json" % relay_id)
            )
            return _complete_post_apply_receive(
                ctx,
                relay_id=relay_id,
                tx_id=relay_id,
                proto_ver=proto_ver,
                verified_ready=verified_ready,
                verified_snap=verified_snap,
                metadata=metadata,
                conv_bundle=conv_bundle,
                apply_res=apply_res,
                dest_dir=dest_dir,
                receipt_key=receipt_key,
                start_phase=str(journal["phase"]),
                is_into=bool(into_path),
            )

    if tx_id_to_use != relay_id and workspace_tx.has_transaction(base, tx_id_to_use):
        tx_info_into = workspace_tx.inspect_transaction(base, tx_id_to_use)
        if tx_info_into["state"] != "rolled_back":
            meta_disk = workspace_tx.load_transaction_meta(base, tx_id_to_use)
            extra = meta_disk.get("extra") or {}
            apply_res = workspace_tx.resume_transaction(base, tx_id_to_use)
            journal = workspace_tx.load_and_verify_journal(base, tx_id_to_use)
            return _complete_post_apply_receive(
                ctx,
                relay_id=relay_id,
                tx_id=tx_id_to_use,
                proto_ver=int(extra.get("protocolVersion") or 2),
                verified_ready=extra.get("verifiedReady") or {},
                verified_snap=extra.get("snapshot"),
                metadata=extra.get("metadata") or {},
                conv_bundle=extra.get("conversations") or {"projectId": str(journal["projectId"]), "entries": []},
                apply_res=apply_res,
                dest_dir=str(journal["targetPath"]),
                receipt_key=str(extra.get("receiptKey") or ("relays/%s/receipt.json" % relay_id)),
                start_phase=str(journal["phase"]),
                is_into=bool(into_path),
            )

    # 5. Locate & download delivery from cloud
    proto_ver, ready_key, expected_pkg_key = _locate_delivery_for_receive(provider, identity, here_id, relay_id)
    receipt_key = (
        "v3/inbox/%s/%s/receipt.json" % (identity["deviceId"], relay_id)
        if proto_ver >= 3
        else "relays/%s/receipt.json" % relay_id
    )

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

        if not isinstance(raw_ready, dict):
            raise crypto.TamperError("交接包 %s 的 ready.json 格式无效。" % relay_id)

        if proto_ver >= 3:
            if raw_ready.get("purpose") != "handoff":
                raise UnauthorizedReceiverError(
                    "交接包 %s 用途为 %r，不是授权给本机领取的交接。" % (relay_id, raw_ready.get("purpose"))
                )
            auth_did = raw_ready.get("authorizedDeviceId") or raw_ready.get("toDeviceId")
            if auth_did != identity["deviceId"]:
                raise UnauthorizedReceiverError(
                    "交接包 %s 未授权给当前设备（%s）领取。" % (relay_id, identity["deviceId"])
                )

        sender_did = str(raw_ready.get("senderDeviceId") or raw_ready.get("fromDeviceId") or "")
        sender_peer = peers_map.get(sender_did)
        if isinstance(sender_peer, dict) and sender_peer.get("status") == "key_changed":
            raise KeyChangedError(
                "发送方设备「%s」（%s）密钥已变更，请重新核对指纹后运行 relay pair --with %s --accept。"
                % (sender_peer.get("alias") or sender_did, sender_did, sender_did),
                peer_id=sender_did,
                old_fingerprint=str(sender_peer.get("keyFingerprint") or ""),
                new_fingerprint=str((sender_peer.get("pendingDescriptor") or {}).get("keyFingerprint") or ""),
            )
        if not isinstance(sender_peer, dict) or not sender_peer.get("paired") or sender_peer.get("status") != "paired":
            raise RelayError("发送方设备（%s）尚未完成配对或已停用，拒绝接收。" % (sender_did or "未知"), code="untrusted_sender")

        verified_ready = crypto.verify_json(
            raw_ready,
            sender_peer["verifyPublicKey"],
            expected_device_id=sender_did,
        )
        if verified_ready.get("relayId") != relay_id:
            raise crypto.TamperError(
                "交接包编号不匹配：期望 %s，实际 %s。" % (relay_id, verified_ready.get("relayId"))
            )

        if verified_ready.get("toDeviceId") and verified_ready.get("toDeviceId") != identity["deviceId"]:
            raise UnauthorizedReceiverError(
                "交接包 %s 的目标设备不是本机（目标：%s，本机：%s）。"
                % (relay_id, verified_ready.get("toAlias") or verified_ready.get("toDeviceId"), here_id)
            )
        if proto_ver < 3 and verified_ready.get("toAlias") and verified_ready.get("toAlias") != here_id:
            raise UnauthorizedReceiverError(
                "交接包 %s 的目标设备是 %s，不是本机（%s）。"
                % (relay_id, verified_ready.get("toAlias"), here_id)
            )

        pkg_key = verified_ready.get("packageKey") or expected_pkg_key
        if pkg_key != expected_pkg_key:
            raise crypto.TamperError("交接包 %s 的加密包路径异常：%s" % (relay_id, pkg_key))
        if proto_ver >= 3:
            _require_consistent_ready(verified_ready, relay_id, sender_did, identity["deviceId"],
                                      expected_pkg_key, "handoff")

        verified_snap: Optional[Dict[str, Any]] = None
        if proto_ver >= 3:
            snap_raw = verified_ready.get("snapshot")
            if not isinstance(snap_raw, dict):
                raise crypto.TamperError("v3 交接包 %s 缺少有效的 snapshot 元数据。" % relay_id)
            # Phase C (S7): the author's trust status is checked like everywhere else; a
            # retired author key is accepted only when the author itself sent this handoff.
            verified_snap, snap_proof = _verify_author_snapshot(
                snap_raw, identity, cloud, state.get("versionGraph"), use="handoff_incoming", sender_did=sender_did)
            bound_fp = DS.norm_fp(verified_ready.get("recipientKeyFingerprint"))
            if bound_fp and bound_fp != DS.norm_fp(_identity_fingerprint(identity)):
                raise KeyChangedError(
                    "交接包 %s 是发给本机换钥前公钥的，本机无法解封领取；原交接包保持不变，请发送方为本机新公钥重新签发交接。"
                    % relay_id, peer_id=sender_did, old_fingerprint=bound_fp,
                    new_fingerprint=DS.norm_fp(_identity_fingerprint(identity)), code="sealed_to_retired_key")
        # Phase D: a re-issue is refused before anything is downloaded when its original was claimed here.
        supersedes_chain = _supersedes_chain(verified_ready) if proto_ver >= 3 else []
        if supersedes_chain:
            _refuse_if_superseded_claim(base, provider, identity, state, relay_id, supersedes_chain)

        try:
            provider.download(pkg_key, enc_local)
        except st_mod.ObjectNotFoundError:
            raise RelayError("交接包 %s 的加密包文件缺失：%s" % (relay_id, pkg_key))

        crypto.decrypt_package(enc_local, tar_local, verified_ready, identity, sender_peer)
        if verified_snap is not None:
            actual_sha, _ = _sha256_file_info(tar_local)
            if actual_sha.lower() != str(verified_snap["contentSha256"]).lower():
                raise crypto.TamperError("交接包 %s 载荷 SHA-256 与快照作者签名摘要不一致。" % relay_id)

        extracted = pkg_mod.extract_and_verify_payload(tar_local, staging_dir)
        manifest = extracted["manifest"]
        metadata = extracted["metadata"]
        conv_bundle = extracted["conversations"]

        proj_id = str(metadata.get("id") or conv_bundle.get("projectId") or "")
        if not proj_id:
            raise pkg_mod.PackageError("交接包缺少有效的项目 ID。")
        _check_remote(V.validate_project_id, proj_id, "metadata.id")
        meta_plain_cache: Dict[str, Any] = {}
        if proto_ver >= 3 and isinstance(verified_snap, dict):
            try:
                meta_plain_cache = crypto.decrypt_meta(verified_ready.get("meta") or {}, identity, sender_peer)
            except Exception:
                meta_plain_cache = {}
            _require_same_project(str(verified_snap["projectId"]), metadata, conv_bundle, meta_plain_cache,
                                  "交接包 %s" % relay_id)
        elif conv_bundle.get("projectId") not in (None, ""):
            with _remote_fields("交接包 %s" % relay_id):
                V.require_equal(conv_bundle.get("projectId"), proj_id, "conversations.projectId", "载荷")
        if verified_snap is not None:
            v_local_now = str((state.get("localCurrentVersion") or {}).get(proj_id) or "")
            if v_local_now and str(verified_snap["versionId"]) == v_local_now and not into_path:
                raise VersionConflictError(
                    "拒绝重复接收本机当前已落地的相同快照版本（%s）。" % v_local_now,
                    code="version_regression",
                    local_version=v_local_now,
                    incoming_version=v_local_now,
                )
        # A handoff snapshot is signed for exactly one relay; re-wrapping it in
        # another handoff is a replay. (The payload's informational
        # metadata.relayId is not read anywhere and is not compared.)
        if verified_snap is not None and verified_snap.get("relayId"):
            reissued_by_author = bool(supersedes_chain and verified_snap.get("relayId") in supersedes_chain
                                      and verified_snap.get("authorDeviceId") == sender_did)
            if not reissued_by_author:  # Phase D: only the author may re-issue its own snapshot, explicitly
                with _remote_fields("交接包 %s" % relay_id):
                    V.require_equal(verified_snap.get("relayId"), relay_id, "snapshot.relayId", "快照与交接编号")
        # Every local destination is checked before the first write.
        _archive_dir_checked(base, proj_id)
        if proto_ver >= 3 and isinstance(verified_snap, dict):
            _save_snapshot_to_cache(
                base,
                str(verified_snap["versionId"]),
                verified_ready["snapshot"],
                tar_local,
                metadata,
                meta_plain_cache,
            )

        led = L.load(led_path)
        proj_name = str(metadata.get("name") or proj_id)

        # 6. Migrated v2 appliedReceives with workspaceApplied == True: NEVER re-apply workspace!
        if isinstance(applied_v2, dict) and applied_v2.get("workspaceApplied"):
            if applied_v2.get("packageSha256") != verified_ready.get("packageSha256") or applied_v2.get("projectId") != proj_id:
                raise crypto.TamperError("交接包 %s 与本机已恢复的交接内容不一致。" % relay_id)
            dest_dir = str(applied_v2.get("targetPath") or "")
            if not dest_dir:
                raise RelayError("旧版恢复记录缺少 targetPath：%s" % relay_id)
            apply_res = {
                "mode": applied_v2.get("mode") or manifest.get("mode") or "git",
                "targetPath": dest_dir,
                "headCommit": (applied_v2.get("baseline") or {}).get("headCommit") or "",
                "branch": applied_v2.get("branch") or "",
                "folderHashes": (applied_v2.get("baseline") or {}).get("folderHashes") or {},
            }
            return _complete_post_apply_receive(
                ctx,
                relay_id=relay_id,
                tx_id="",
                proto_ver=proto_ver,
                verified_ready=verified_ready,
                verified_snap=verified_snap,
                metadata=metadata,
                conv_bundle=conv_bundle,
                apply_res=apply_res,
                dest_dir=dest_dir,
                receipt_key=receipt_key,
                start_phase=workspace_tx.PHASE_WORKSPACE_COMMITTED,
                is_into=bool(into_path),
            )

        existing_proj = next((p for p in (led.get("projects") or []) if p.get("id") == proj_id), None)
        existing_here_raw = _project_path_for_device(existing_proj, here_id, identity["deviceId"]) if existing_proj else ""
        existing_here_canon = _canon_local_path(existing_here_raw) if existing_here_raw else ""

        requested_dest = into_path or target_path
        raw_dest = requested_dest or (applied_v2 or {}).get("targetPath") or existing_here_raw
        if not raw_dest:
            raise NeedTargetPathError(
                "首次在本机接收项目「%s」，请用 --path 指定项目目录。" % proj_name,
                project_id=proj_id,
                project_name=proj_name,
            )

        dest_dir = _canon_local_path(raw_dest)

        # 7. Version lineage DAG check before modifying workspace
        if verified_snap is not None:
            v_in = str(verified_snap["versionId"])
            v_local = str((state.get("localCurrentVersion") or {}).get(proj_id) or "")
            if v_local and v_in == v_local and not into_path:
                raise VersionConflictError(
                    "拒绝重复接收本机当前已落地的相同快照版本（%s）。" % v_in,
                    code="version_regression",
                    local_version=v_local,
                    incoming_version=v_in,
                )
            if v_local and v_in != v_local:
                temp_state = copy.deepcopy(state)
                _record_version(
                    temp_state,
                    proj_id,
                    {
                        "versionId": v_in,
                        "projectId": proj_id,
                        "authorDeviceId": str(verified_snap.get("authorDeviceId") or ""),
                        "parentVersionIds": list(verified_snap.get("parentVersionIds") or []),
                        "parentRelayId": str(verified_snap.get("parentRelayId") or ""),
                        "relayId": relay_id,
                        "contentSha256": str(verified_snap.get("contentSha256") or ""),
                        "createdAt": str(verified_snap.get("createdAt") or ""),
                    },
                )
                _complete_version_ancestry(provider, base, identity, cloud, proj_id,
                                           temp_state, v_in, v_local)
                proj_graph = (temp_state.get("versionGraph") or {}).get(proj_id) or {}
                if _is_version_ancestor(proj_graph, v_in, v_local):
                    raise VersionConflictError(
                        "拒绝接收旧版本交接包（版本倒退：传入版本 %s 是本机当前版本 %s 的祖先）。" % (v_in, v_local),
                        code="version_regression",
                        local_version=v_local,
                        incoming_version=v_in,
                    )
                if not _is_version_ancestor(proj_graph, v_local, v_in):
                    if not into_path:
                        def _save_fork_graph(st_obj: Dict[str, Any]) -> None:
                            _record_version(
                                st_obj,
                                proj_id,
                                {
                                    "versionId": v_in,
                                    "projectId": proj_id,
                                    "authorDeviceId": str(verified_snap.get("authorDeviceId") or ""),
                                    "parentVersionIds": list(verified_snap.get("parentVersionIds") or []),
                                    "parentRelayId": str(verified_snap.get("parentRelayId") or ""),
                                    "relayId": relay_id,
                                    "contentSha256": str(verified_snap.get("contentSha256") or ""),
                                    "createdAt": str(verified_snap.get("createdAt") or ""),
                                },
                            )

                        _update_relay_state(base, _save_fork_graph)
                        _record_conflict_in_ledger(
                            led_path,
                            proj_id,
                            here_id,
                            v_local,
                            v_in,
                            author_device_id=str(verified_snap.get("authorDeviceId") or ""),
                        )
                        raise VersionConflictError(
                            "检测到离线分叉冲突（本机版本 %s 与传入版本 %s 并发分叉），已保留原目录不动；"
                            "如需将传入版本落地到新目录对比，请使用 --into <新目录>。" % (v_local, v_in),
                            code="version_fork",
                            local_version=v_local,
                            incoming_version=v_in,
                        )

        # 8. Block if another incomplete transaction or appliedReceive exists for the same project or targetPath
        for pending_id, entry in (state.get("appliedReceives") or {}).items():
            if pending_id != relay_id and (entry.get("projectId") == proj_id or entry.get("targetPath") == dest_dir):
                raise RelayError("此项目或目录有待完成的接棒，请先运行 relay receive %s。" % pending_id)

        for other_tx_id in workspace_tx.list_transactions(base):
            if other_tx_id in (relay_id, tx_id_to_use):
                continue
            other_info = workspace_tx.inspect_transaction(base, other_tx_id)
            if other_info.get("state") in ("completed", "rolled_back", "none"):
                continue
            if not into_path and (
                other_info.get("projectId") == proj_id
                or _canon_local_path(other_info.get("targetPath") or "") == dest_dir
            ):
                raise RelayError(
                    "此项目或目录有待恢复的交接事务（%s），请先运行 relay receive %s 或 relay recover %s。"
                    % (other_tx_id, other_tx_id, other_tx_id),
                    code="pending_transaction",
                )

        # 9. Clean target & cross-OS path checks
        _check_receive_target(pkg_mod, manifest, state, proj_id, dest_dir, existing_here_canon, proj_name)

        # 10. Transactional Stage A + Stage B workspace apply
        extra_meta = {
            "protocolVersion": proto_ver,
            "verifiedReady": verified_ready,
            "snapshot": verified_snap,
            "metadata": metadata,
            "conversations": conv_bundle,
            "receiptKey": receipt_key,
            "fromAlias": str(verified_ready.get("fromAlias") or sender_peer.get("alias") or ""),
            "fromDeviceId": sender_did,
            "destDir": dest_dir,
            "prevLocalVersion": str((state.get("localCurrentVersion") or {}).get(proj_id) or ""),
        }
        try:
            apply_res = workspace_tx.prepare_and_apply_workspace(
                base,
                tx_id_to_use,
                proj_id,
                str(verified_ready["packageSha256"]),
                staging_dir,
                dest_dir,
                extra_meta=extra_meta,
            )
        except pkg_mod.TargetDirtyError as e:
            # Raised before the journal exists (a path collides with a preserved local file).
            e.project_id = proj_id
            e.project_name = proj_name
            raise

    return _complete_post_apply_receive(
        ctx,
        relay_id=relay_id,
        tx_id=tx_id_to_use,
        proto_ver=proto_ver,
        verified_ready=verified_ready,
        verified_snap=verified_snap,
        metadata=metadata,
        conv_bundle=conv_bundle,
        apply_res=apply_res,
        dest_dir=dest_dir,
        receipt_key=receipt_key,
        start_phase=workspace_tx.PHASE_WORKSPACE_COMMITTED,
        is_into=bool(into_path),
    )


def _complete_post_apply_receive(
    ctx,
    relay_id: str,
    tx_id: str,
    proto_ver: int,
    verified_ready: Dict[str, Any],
    verified_snap: Optional[Dict[str, Any]],
    metadata: Dict[str, Any],
    conv_bundle: Dict[str, Any],
    apply_res: Dict[str, Any],
    dest_dir: str,
    receipt_key: str,
    start_phase: str,
    is_into: bool = False,
) -> Dict[str, Any]:
    conv_mod = _conversations_mod()
    base, led_path, _local_cfg, led, here_id, cloud, identity, provider = ctx
    proj_id = str(metadata.get("id") or conv_bundle.get("projectId") or "")
    proj_name = str(metadata.get("name") or proj_id)
    from_alias = str(verified_ready.get("fromAlias") or L.other(led["config"], here_id)["id"])
    from_did = str(verified_ready.get("senderDeviceId") or verified_ready.get("fromDeviceId") or "")
    received_at = L.now_iso()

    # Phase: Archive
    if start_phase == workspace_tx.PHASE_WORKSPACE_COMMITTED:
        workspace_tx.maybe_inject_fault("rx_before_archive")
        conv_res = _merge_archive_checked(conv_mod, base, proj_id, conv_bundle)
        if tx_id and workspace_tx.has_transaction(base, tx_id):
            workspace_tx.mark_transaction_phase(base, tx_id, workspace_tx.PHASE_ARCHIVED)
    else:
        conv_res = _merge_archive_checked(conv_mod, base, proj_id, conv_bundle)

    conv_summary = {key: conv_res[key] for key in ("total", "added", "updated", "skipped", "archiveDir")}

    # Phase: Ledger
    user_h = os.environ.get("RELAY_DESK_USER_HOME") or None
    facts = scan.git_facts(dest_dir)
    pres = scan.presence(dest_dir, home=user_h)

    if start_phase in (workspace_tx.PHASE_WORKSPACE_COMMITTED, workspace_tx.PHASE_ARCHIVED, workspace_tx.PHASE_UPDATING_LEDGER):
        workspace_tx.maybe_inject_fault("rx_before_ledger")
        if tx_id and workspace_tx.has_transaction(base, tx_id):
            workspace_tx.mark_transaction_phase(base, tx_id, workspace_tx.PHASE_UPDATING_LEDGER)

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
            p_by_did = target_p.setdefault("pathsByDeviceId", {})
            p_by_did[identity["deviceId"]] = dest_dir

            sender_path = metadata.get("fromPath") or (metadata.get("paths") or {}).get(from_alias) or ""
            if sender_path:
                paths_map[from_alias] = sender_path
                if from_did:
                    p_by_did.setdefault(from_did, sender_path)
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
            history = list(target_p.get("history") or [])
            if not any(entry.get("relayId") == relay_id for entry in history):
                target_p["history"] = (
                    history
                    + [
                        {
                            "tag": relay_id,
                            "relayId": relay_id,
                            "parentRelayId": str(verified_ready.get("parentRelayId") or ""),
                            "from": from_alias,
                            "fromDeviceId": from_did,
                            "to": here_id,
                            "toDeviceId": identity["deviceId"],
                            "transport": "cloud",
                            "doneAt": received_at,
                        }
                    ]
                )[-10:]
            _apply_local_facts_to_project(target_p, here_id, facts, pres)
            L.touch(target_p, here_id)

            migrated = devices.migrate_ledger_identities(l, local_ident=identity, cloud_cfg=cloud)
            l.clear()
            l.update(migrated)

        L.update(led_path, _upd_receiver_ledger)
        if tx_id and workspace_tx.has_transaction(base, tx_id):
            workspace_tx.mark_transaction_phase(base, tx_id, workspace_tx.PHASE_LEDGER_UPDATED)

    # Build baseline & signed receipt
    git_snapshot = _compute_git_baseline_fingerprint(dest_dir) if apply_res.get("mode") == "git" else ""
    baseline = {
        "headCommit": apply_res.get("headCommit") or "",
        "folderHashes": apply_res.get("folderHashes") or {},
        "gitSnapshotHash": git_snapshot,
    }
    snap_id = str(verified_snap["versionId"]) if isinstance(verified_snap, dict) else ""

    if proto_ver >= 3:
        receipt_unsigned = {
            "formatVersion": 3,
            "purpose": "handoff",
            "relayId": relay_id,
            "projectId": proj_id,
            "snapshotId": snap_id,
            "fromAlias": from_alias,
            "fromDeviceId": from_did,
            "toAlias": here_id,
            "receiverDeviceId": identity["deviceId"],
            "packageSha256": verified_ready["packageSha256"],
            "receivedAt": received_at,
        }
    else:
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
        "snapshotId": snap_id,
        "protocolVersion": proto_ver,
        "projectId": proj_id,
        "projectName": proj_name,
        "from": from_alias,
        "fromDeviceId": from_did,
        "to": here_id,
        "toDeviceId": identity["deviceId"],
        "targetPath": dest_dir,
        "mode": apply_res.get("mode") or "git",
        "branch": apply_res.get("branch") or facts.get("branch") or "",
        "headCommit": baseline["headCommit"],
        "lastTask": str(metadata.get("lastTask") or ""),
        "nextAction": str(metadata.get("nextAction") or ""),
        "note": str(metadata.get("note") or ""),
        "verify": copy.deepcopy(metadata.get("verify") or {"state": "none", "cmd": "", "note": ""}),
        "conversations": conv_summary,
        "message": "接棒完成：%s 已就绪（%s）" % (proj_name, dest_dir),
    }

    def _record_local_applied(st_obj: Dict[str, Any]) -> None:
        st_obj["appliedReceives"].pop(relay_id, None)
        st_obj["projectBaselines"][proj_id] = baseline
        st_obj.setdefault("receivedRelays", {})[relay_id] = {
            "projectId": proj_id,
            "targetPath": dest_dir,
            "receivedAt": received_at,
            "protocolVersion": proto_ver,
            "receiptKey": receipt_key,
            "receiptUploaded": False,
            "signedReceipt": signed_receipt,
            "result": result,
        }
        chain = _supersedes_chain(verified_ready) if proto_ver >= 3 else []
        if chain:  # Phase D: remembered so the replaced handoff(s) can never be claimed here as well
            st_obj["receivedRelays"][relay_id]["supersedesRelayIds"] = chain
        ver_id_to_record = snap_id or relay_id
        _record_version(
            st_obj,
            proj_id,
            {
                "versionId": ver_id_to_record,
                "projectId": proj_id,
                "authorDeviceId": str((verified_snap or {}).get("authorDeviceId") or from_did),
                "parentVersionIds": list((verified_snap or {}).get("parentVersionIds") or []),
                "parentRelayId": str(verified_ready.get("parentRelayId") or ""),
                "relayId": relay_id,
                "contentSha256": str((verified_snap or {}).get("contentSha256") or verified_ready.get("packageSha256") or ""),
                "createdAt": str((verified_snap or {}).get("createdAt") or verified_ready.get("createdAt") or received_at),
                "recordSha256": (crypto.snapshot_record_digest(verified_ready["snapshot"])
                                 if isinstance(verified_ready.get("snapshot"), dict) else ""),
            },
        )
        st_obj.setdefault("localCurrentVersion", {})[proj_id] = ver_id_to_record
        if not is_into:
            st_obj.get("activeHandoffs", {}).pop(proj_id, None)

    _update_relay_state(base, _record_local_applied)

    workspace_tx.maybe_inject_fault("rx_before_receipt")
    _upload_and_verify_receipt(provider, relay_id, signed_receipt, proj_name, dest_dir, receipt_key=receipt_key)

    def _mark_receipt_uploaded(st_obj: Dict[str, Any]) -> None:
        ent = st_obj.setdefault("receivedRelays", {}).get(relay_id)
        if isinstance(ent, dict):
            ent["receiptUploaded"] = True

    _update_relay_state(base, _mark_receipt_uploaded)

    if proto_ver >= 3:
        try:
            _refresh_have(provider, base, identity)
        except Exception:
            pass

    if tx_id and workspace_tx.has_transaction(base, tx_id):
        workspace_tx.archive_completed_transaction(base, tx_id)

    return result


# ---------------------------------------------------------------- 8. recover_relay


def recover_relay(
    relay_id: str,
    action: str = "status",
    ledger_path: Optional[Any] = None,
    local: Optional[Dict[str, Any]] = None,
    local_home: Optional[Any] = None,
) -> Dict[str, Any]:
    """Inspect or roll back an interrupted receive transaction or pending send."""
    rid = str(relay_id or "").strip()
    if not RELAY_ID_RE.match(rid) or rid in (".", ".."):
        raise RelayError("无效的交接包编号：%r" % relay_id)
    try:
        V.validate_token(rid, "relayId", max_len=80)
    except V.ValidationError as e:
        raise RelayError("无效的交接包编号：%r（%s）" % (relay_id, e.message), code="invalid_relay_id")
    act = str(action or "status").strip().lower()
    if act not in ("status", "rollback"):
        raise RelayError("不支持的恢复动作：%r（仅支持 status 或 rollback）。" % action, code="invalid_action")

    base, _led_path, _local_cfg = _resolve_ctx(ledger_path, local, local_home)

    with _workspace_operation(base):
        state = _load_relay_state(base)

        if workspace_tx.has_transaction(base, rid):
            if act == "status":
                info = workspace_tx.inspect_transaction(base, rid)
                info["ok"] = True
                rec_ent = (state.get("receivedRelays") or {}).get(rid)
                if isinstance(rec_ent, dict) and not rec_ent.get("receiptUploaded"):
                    info["pendingReceiptUpload"] = True
                return info

            meta_disk = workspace_tx.load_transaction_meta(base, rid)
            rb_res = workspace_tx.rollback_transaction(base, rid)

            def _cleanup_rolled_back(st_obj: Dict[str, Any]) -> None:
                rec = (st_obj.get("receivedRelays") or {}).get(rid)
                if isinstance(rec, dict) and not rec.get("receiptUploaded"):
                    st_obj["receivedRelays"].pop(rid, None)
                st_obj.get("appliedReceives", {}).pop(rid, None)
                extra = meta_disk.get("extra") or {}
                pid_rb = str(rb_res.get("projectId") or meta_disk.get("projectId") or "")
                if pid_rb and "prevLocalVersion" in extra:
                    prev_v = str(extra.get("prevLocalVersion") or "")
                    if prev_v:
                        st_obj.setdefault("localCurrentVersion", {})[pid_rb] = prev_v
                    else:
                        st_obj.get("localCurrentVersion", {}).pop(pid_rb, None)

            _update_relay_state(base, _cleanup_rolled_back)
            return rb_res

        # Check pendingSends (including migrated v2 needs_verification records)
        for pid, pend in (state.get("pendingSends") or {}).items():
            if not isinstance(pend, dict) or pend.get("relayId") != rid:
                continue
            needs_ver = bool(pend.get("verificationRequired") or pend.get("status") == "needs_verification")
            if act == "status":
                return {
                    "ok": True,
                    "exists": True,
                    "kind": "pending_send",
                    "relayId": rid,
                    "projectId": pid,
                    "targetPath": "",
                    "phase": "NEEDS_VERIFICATION" if needs_ver else "PENDING_SEND",
                    "state": "needs_verification" if needs_ver else "pending_send",
                    "canResume": not needs_ver,
                    "canRollback": True,
                    "reasons": ["旧版迁移的待核实发送记录"] if needs_ver else [],
                }

            cloud = _load_cloud(base)
            if _is_cloud_configured(cloud):
                try:
                    prov = _storage_mod().with_read_retry(_storage_mod().make_provider(cloud))
                    to_did = str(pend.get("toDeviceId") or "")
                    keys_to_check = ["relays/%s/ready.json" % rid]
                    if to_did:
                        keys_to_check.insert(0, "v3/inbox/%s/%s/ready.json" % (to_did, rid))
                    for rk in keys_to_check:
                        if prov.stat(rk) is not None:
                            raise RelayError(
                                "交接包 %s 已发布到云端（%s），不能回滚清除发送记录。" % (rid, rk),
                                code="already_published",
                            )
                except RelayError:
                    raise
                except Exception as e:
                    # Fail closed: without a confirmed absence the handoff may already be
                    # claimable, and dropping the record would allow two owners.
                    raise RelayError(
                        "无法确认交接包 %s 是否已发布到云端（%s）。为避免双重所有权，已保留发送记录；"
                        "请在网络恢复后重新运行 relay recover %s --rollback，或重新运行 send 完成发送。"
                        % (rid, e, rid),
                        code="rollback_cloud_check_failed",
                    )

            def _drop_pending(st_obj: Dict[str, Any], _pid: str = pid) -> None:
                st_obj.get("pendingSends", {}).pop(_pid, None)

            _update_relay_state(base, _drop_pending)
            return {
                "ok": True,
                "relayId": rid,
                "projectId": pid,
                "kind": "pending_send",
                "phase": workspace_tx.PHASE_ROLLED_BACK,
                "alreadyRolledBack": False,
            }

        rec_ent = (state.get("receivedRelays") or {}).get(rid)
        if isinstance(rec_ent, dict):
            if act == "status":
                done = bool(rec_ent.get("receiptUploaded"))
                return {
                    "ok": True,
                    "exists": True,
                    "kind": "received_relay",
                    "relayId": rid,
                    "projectId": str(rec_ent.get("projectId") or ""),
                    "targetPath": str(rec_ent.get("targetPath") or ""),
                    "phase": workspace_tx.PHASE_COMPLETED if done else workspace_tx.PHASE_LEDGER_UPDATED,
                    "state": "completed" if done else "pending_receipt",
                    "canResume": not done,
                    "canRollback": False,
                    "reasons": [],
                }
            if rec_ent.get("receiptUploaded"):
                raise workspace_tx.WorkspaceTxError("该交接已完成签收，不能回滚。", code="already_completed")
            raise workspace_tx.WorkspaceTxError("该交接缺少事务快照，无法自动回滚工作区。", code="no_transaction_snapshot")

        app_ent = (state.get("appliedReceives") or {}).get(rid)
        if isinstance(app_ent, dict):
            if act == "status":
                return {
                    "ok": True,
                    "exists": True,
                    "kind": "applied_receive",
                    "relayId": rid,
                    "projectId": str(app_ent.get("projectId") or ""),
                    "targetPath": str(app_ent.get("targetPath") or ""),
                    "phase": workspace_tx.PHASE_WORKSPACE_COMMITTED,
                    "state": "workspace_committed",
                    "canResume": True,
                    "canRollback": False,
                    "reasons": [],
                }
            raise workspace_tx.WorkspaceTxError("旧版 v2 迁移记录不包含事务前快照，无法回滚。", code="no_transaction_snapshot")

        if act == "status":
            return {
                "ok": True,
                "exists": False,
                "relayId": rid,
                "phase": "",
                "targetPath": "",
                "projectId": "",
                "state": "none",
                "canResume": False,
                "canRollback": False,
                "reasons": [],
            }
        raise workspace_tx.WorkspaceTxError("找不到交接事务记录：%s" % rid, code="transaction_not_found")


# ---------------------------------------------------------------- 9. query_transcripts


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
