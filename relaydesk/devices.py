"""Device identity, alias resolution, target selection, and v1/v2 -> v3 migrations."""
import copy
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import validation

ALIAS_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
CLOUD_CONFIG_VERSION = 3
RELAY_STATE_VERSION = 3


class DeviceConflictError(ValueError):
    """Raised when device aliases, names, or deviceIds conflict."""

    def __init__(self, message: str, code: str = "alias_conflict") -> None:
        super().__init__(message)
        self.code = code


class AmbiguousDeviceError(LookupError):
    """Raised when a device query or target selection matches multiple devices."""

    def __init__(
        self,
        message: str,
        candidates: Optional[List[Dict[str, Any]]] = None,
        code: str = "ambiguous_peer",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.candidates: List[Dict[str, Any]] = list(candidates or [])


def validate_alias(alias: str) -> str:
    """Validate and return a normalized device alias matching ALIAS_RE."""
    if not isinstance(alias, str):
        raise ValueError("设备别名必须是字符串。")
    cleaned = alias.strip()
    if not ALIAS_RE.match(cleaned):
        raise ValueError(
            "设备别名「%s」不合法：只允许 1~40 位字母、数字、下划线、点或连字符。" % alias
        )
    return cleaned


def _candidate_info(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "alias": str(entry.get("alias") or entry.get("id") or ""),
        "deviceId": str(entry.get("deviceId") or ""),
        "name": str(entry.get("name") or ""),
        "os": str(entry.get("os") or ""),
    }


def _match_entries(entries: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """Return matching device/peer entries ordered by exact deviceId, exact id/alias, CI id/alias, CI name."""
    q = query.strip()
    ql = q.lower()
    if not q:
        return []

    by_did = [d for d in entries if str(d.get("deviceId") or "").strip() == q]
    if by_did:
        return by_did

    by_id_exact = [
        d for d in entries if str(d.get("alias") or d.get("id") or "") == q
    ]
    if by_id_exact:
        return by_id_exact

    by_id_ci = [
        d for d in entries if str(d.get("alias") or d.get("id") or "").lower() == ql
    ]
    if by_id_ci:
        return by_id_ci

    by_name_ci = [
        d for d in entries if str(d.get("name") or "").strip().lower() == ql
    ]
    return by_name_ci


def resolve_device_entry(config: Dict[str, Any], query: str) -> Dict[str, Any]:
    """Resolve a single device entry from ledger config by deviceId, id/alias, or unique name."""
    if not isinstance(query, str) or not query.strip():
        raise LookupError("请提供要查找的设备别名、名称或 deviceId。")
    devices = [d for d in (config.get("devices") or []) if isinstance(d, dict)]
    matches = _match_entries(devices, query)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        cands = [_candidate_info(d) for d in matches]
        labels = ", ".join(c["alias"] or c["deviceId"] or c["name"] for c in cands)
        raise AmbiguousDeviceError(
            "「%s」匹配到多台设备：%s。请改用准确的别名或 deviceId。" % (query.strip(), labels),
            candidates=cands,
        )
    known = ", ".join(str(d.get("id") or d.get("deviceId") or "") for d in devices)
    raise LookupError("找不到设备「%s」。已有设备：%s。" % (query.strip(), known))


def list_other_devices(config: Dict[str, Any], here_id: Optional[str]) -> List[Dict[str, Any]]:
    """Return all configured devices whose id differs from here_id."""
    devices = [d for d in (config.get("devices") or []) if isinstance(d, dict)]
    if not here_id:
        return list(devices)
    return [d for d in devices if d.get("id") != here_id]


def _is_self_query(config: Dict[str, Any], here_id: Optional[str], query: str) -> bool:
    if not here_id or not query:
        return False
    q = query.strip()
    ql = q.lower()
    if ql == str(here_id).strip().lower():
        return True
    for d in (config.get("devices") or []):
        if isinstance(d, dict) and d.get("id") == here_id:
            did = str(d.get("deviceId") or "").strip()
            name = str(d.get("name") or "").strip().lower()
            if did and q == did:
                return True
            if name and ql == name:
                return True
    return False


def resolve_target_device(
    config: Dict[str, Any],
    here_id: Optional[str],
    to_query: Optional[str] = None,
    cloud_cfg: Optional[Dict[str, Any]] = None,
    require_paired_cloud: bool = False,
) -> Dict[str, Any]:
    """Resolve the target peer device for handoff/send/plan.

    When require_paired_cloud=True, selects from paired v3/migrated cloud peers
    (paired == True and status == 'paired').
    When require_paired_cloud=False, selects from ledger config['devices'] excluding here_id.
    """
    q = to_query.strip() if isinstance(to_query, str) else ""

    if require_paired_cloud:
        cfg_norm = cloud_cfg if isinstance(cloud_cfg, dict) else {}
        if "peers" not in cfg_norm:
            cfg_norm = migrate_cloud_config(cfg_norm, local_home=None)
        peers_map = cfg_norm.get("peers") if isinstance(cfg_norm.get("peers"), dict) else {}
        all_peers = [p for p in peers_map.values() if isinstance(p, dict)]
        paired_peers = [
            p
            for p in all_peers
            if bool(p.get("paired")) and p.get("status") == "paired"
        ]

        if not q:
            if len(paired_peers) == 1:
                return paired_peers[0]
            if len(paired_peers) == 0:
                raise LookupError("尚未配对任何远端设备。")
            cands = [_candidate_info(p) for p in paired_peers]
            labels = ", ".join(c["alias"] or c["deviceId"] for c in cands)
            raise AmbiguousDeviceError(
                "已配对多台设备（%s），请使用 --to 指定目标设备。" % labels,
                candidates=cands,
            )

        matches = _match_entries(paired_peers, q)
        if not matches and isinstance(config, dict):
            # Fallback: allow resolving an alias in ledger config that carries a paired deviceId.
            ledger_others = list_other_devices(config, here_id)
            ledger_matches = _match_entries(ledger_others, q)
            if len(ledger_matches) == 1 and ledger_matches[0].get("deviceId"):
                target_did = ledger_matches[0]["deviceId"]
                matches = [p for p in paired_peers if p.get("deviceId") == target_did]

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            cands = [_candidate_info(p) for p in matches]
            labels = ", ".join(c["alias"] or c["deviceId"] for c in cands)
            raise AmbiguousDeviceError(
                "目标设备「%s」匹配到多个已配对节点：%s。" % (q, labels),
                candidates=cands,
            )
        if _is_self_query(config or {}, here_id, q):
            raise ValueError("目标设备不能是当前本机（%s）。" % q)
        unpaired_matches = _match_entries(all_peers, q)
        if unpaired_matches:
            raise LookupError("设备「%s」尚未完成配对或密钥变更待重新确认。" % q)
        raise LookupError("找不到已配对的目标设备「%s」。" % q)

    # Manual ledger mode
    others = list_other_devices(config or {}, here_id)
    if not q:
        if len(others) == 1:
            return others[0]
        if len(others) == 0:
            raise LookupError("台账里没有其他目标设备。")
        cands = [_candidate_info(d) for d in others]
        labels = ", ".join(c["alias"] or c["deviceId"] for c in cands)
        raise AmbiguousDeviceError(
            "台账中有多台目标设备（%s），请使用 --to 指定目标设备。" % labels,
            candidates=cands,
        )

    matches = _match_entries(others, q)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        cands = [_candidate_info(d) for d in matches]
        labels = ", ".join(c["alias"] or c["deviceId"] for c in cands)
        raise AmbiguousDeviceError(
            "目标设备「%s」匹配到多台设备：%s。" % (q, labels),
            candidates=cands,
        )
    if _is_self_query(config or {}, here_id, q):
        raise ValueError("目标设备不能是当前本机（%s）。" % q)
    raise LookupError("找不到目标设备「%s」。" % q)


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


def ensure_backup_once(path: Path, backup_path: Path) -> bool:
    """Atomically copy path to backup_path only if path exists and backup_path does not."""
    src = Path(path)
    dst = Path(backup_path)
    if not src.exists() or dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(str(src), "rb") as rfh:
        raw_bytes = rfh.read()
    fd, tmp = tempfile.mkstemp(
        dir=str(dst.parent),
        prefix="." + dst.name + "-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as wfh:
            wfh.write(raw_bytes)
            wfh.flush()
            try:
                os.fsync(wfh.fileno())
            except OSError:
                pass
        if dst.exists():
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False
        _replace_with_retry(tmp, str(dst))
        return True
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _usable_peer_id(device_id: str, peer: Dict[str, Any]) -> bool:
    """Discovered-but-unconfirmed peers with unusable ids are dropped; they can be rediscovered."""
    if peer.get("paired") or peer.get("status") in ("paired", "key_changed"):
        return True
    try:
        validation.validate_device_id(device_id)
        return True
    except validation.ValidationError:
        return False


def _int_version(val: Any, default: int = 2) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        return default
    return val


def _norm_peer_record(raw_peer: Dict[str, Any], default_paired: bool = False) -> Dict[str, Any]:
    rec = copy.deepcopy(raw_peer)
    did = str(rec.get("deviceId") or "").strip()
    is_paired = bool(rec.get("paired", default_paired))
    proto_raw = rec.get("protocolVersion")
    if isinstance(proto_raw, int) and not isinstance(proto_raw, bool) and proto_raw >= 3:
        proto_ver = proto_raw
        default_caps = ["v2", "v3"]
    else:
        proto_ver = 2
        default_caps = ["v2"]

    caps_raw = rec.get("capabilities")
    if isinstance(caps_raw, list) and caps_raw:
        caps = [str(c) for c in caps_raw if isinstance(c, str) and c]
    else:
        caps = default_caps
    if proto_ver >= 3 and "v3" not in caps:
        caps.append("v3")
    if "v2" not in caps:
        caps.insert(0, "v2")

    raw_status = rec.get("status")
    if raw_status in ("paired", "discovered", "key_changed"):
        status = raw_status
    else:
        status = "paired" if is_paired else "discovered"

    pending_desc = rec.get("pendingDescriptor")
    rec["deviceId"] = did
    rec["alias"] = str(rec.get("alias") or rec.get("id") or "")
    rec["name"] = str(rec.get("name") or "")
    rec["os"] = str(rec.get("os") or "")
    rec["host"] = str(rec.get("host") or "")
    rec["verifyPublicKey"] = str(rec.get("verifyPublicKey") or "")
    rec["boxPublicKey"] = str(rec.get("boxPublicKey") or "")
    rec["keyFingerprint"] = str(rec.get("keyFingerprint") or "")
    rec["pairFingerprint"] = str(rec.get("pairFingerprint") or "")
    rec["paired"] = is_paired
    rec["status"] = status
    rec["pairedAt"] = str(rec.get("pairedAt") or "")
    rec["protocolVersion"] = proto_ver
    rec["capabilities"] = caps
    rec["pendingDescriptor"] = copy.deepcopy(pending_desc) if isinstance(pending_desc, dict) else None
    return rec


def sync_legacy_cloud_fields(cloud_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy with optional legacy single-peer convenience fields synchronized."""
    out = copy.deepcopy(cloud_cfg) if isinstance(cloud_cfg, dict) else {}
    peers = out.get("peers") if isinstance(out.get("peers"), dict) else {}
    paired = [p for p in peers.values() if isinstance(p, dict) and p.get("paired") and p.get("status") == "paired"]
    if len(paired) == 1:
        p = paired[0]
        out["peer"] = copy.deepcopy(p)
        out["paired"] = True
        out["pairFingerprint"] = str(p.get("pairFingerprint") or "")
        out["pairedAt"] = str(p.get("pairedAt") or "")
    else:
        out["peer"] = None
        out["paired"] = False
        out["pairFingerprint"] = ""
        out["pairedAt"] = ""
    return out


def cloud_config_problems(raw: Any) -> List[str]:
    """List structural corruption in a parsed cloud.json (empty list when usable).

    Missing keys are fine (older or never-written fields). Present keys with the
    wrong type, or a paired peer whose deviceId is unusable, mean the file was
    damaged and must not be normalized into defaults.
    """
    if not isinstance(raw, dict):
        return ["顶层不是对象"]
    problems: List[str] = []
    for key in ("provider", "remote", "root", "configPath"):
        if raw.get(key) is not None and not isinstance(raw.get(key), str):
            problems.append("%s 不是字符串" % key)
    for key in ("peers", "shownFingerprints"):
        if raw.get(key) is not None and not isinstance(raw.get(key), dict):
            problems.append("%s 不是对象" % key)
    if raw.get("peer") is not None and not isinstance(raw.get("peer"), dict):
        problems.append("peer 不是对象")
    peers = raw.get("peers") if isinstance(raw.get("peers"), dict) else {}
    for key, peer in peers.items():
        if not isinstance(peer, dict):
            problems.append("peers[%r] 不是对象" % key)
            continue
        if peer.get("paired") or peer.get("status") in ("paired", "key_changed"):
            try:
                validation.validate_device_id(str(peer.get("deviceId") or key), "peers.deviceId")
            except validation.ValidationError as e:
                problems.append("已配对设备的 deviceId 无效：%s" % e.message)
    return problems


def relay_state_problems(raw: Any) -> List[str]:
    """List structural corruption in a parsed relay_state.json (empty list when usable)."""
    if not isinstance(raw, dict):
        return ["顶层不是对象"]
    return [
        "%s 不是对象" % key
        for key in _RELAY_STATE_DICT_KEYS
        if raw.get(key) is not None and not isinstance(raw.get(key), dict)
    ]


def migrate_cloud_config(raw: Dict[str, Any], local_home: Optional[Path] = None) -> Dict[str, Any]:
    """Idempotently migrate v2 cloud.json (single `peer`) into v3 (`peers` map)."""
    raw_in = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    raw_ver = _int_version(raw_in.get("version"), 2)

    if local_home is not None and raw_ver < CLOUD_CONFIG_VERSION:
        cloud_path = Path(local_home) / "cloud.json"
        if cloud_path.exists():
            ensure_backup_once(cloud_path, Path(local_home) / "cloud.v2.backup.json")

    out: Dict[str, Any] = {}
    for k, v in raw_in.items():
        if k not in ("peer", "paired", "pairFingerprint", "pairedAt"):
            out[k] = v

    prov = out.get("provider")
    out["version"] = CLOUD_CONFIG_VERSION
    out["provider"] = prov if prov in ("rclone", "local") else "rclone"
    out["remote"] = str(out.get("remote") or "")
    out["root"] = str(out.get("root") or "")
    out["configPath"] = str(out.get("configPath") or "")

    shown = raw_in.get("shownFingerprints")
    out["shownFingerprints"] = copy.deepcopy(shown) if isinstance(shown, dict) else {}

    peers_out: Dict[str, Dict[str, Any]] = {}
    raw_peers = raw_in.get("peers")
    if isinstance(raw_peers, dict):
        for did, peer_val in raw_peers.items():
            if isinstance(peer_val, dict):
                normed = _norm_peer_record(peer_val)
                key = normed["deviceId"] or str(did)
                if key and _usable_peer_id(key, normed):
                    normed["deviceId"] = key
                    peers_out[key] = normed

    legacy_peer = raw_in.get("peer")
    if isinstance(legacy_peer, dict):
        did = str(legacy_peer.get("deviceId") or "").strip()
        if did and did not in peers_out:
            merged_peer = copy.deepcopy(legacy_peer)
            is_paired = bool(raw_in.get("paired"))
            merged_peer.setdefault("paired", is_paired)
            if raw_in.get("pairFingerprint") and not merged_peer.get("pairFingerprint"):
                merged_peer["pairFingerprint"] = str(raw_in["pairFingerprint"])
            if raw_in.get("pairedAt") and not merged_peer.get("pairedAt"):
                merged_peer["pairedAt"] = str(raw_in["pairedAt"])
            if "status" not in merged_peer:
                merged_peer["status"] = "paired" if is_paired else "discovered"
            peers_out[did] = _norm_peer_record(merged_peer, default_paired=is_paired)

    out["peers"] = peers_out
    return out


_RELAY_STATE_DICT_KEYS = (
    "pendingSends",
    "sentRelays",
    "appliedReceives",
    "receivedRelays",
    "projectBaselines",
    "activeHandoffs",
    "versionGraph",
    "localCurrentVersion",
    "processedDeliveries",
    "archivedRelayIds",
    "forwardedSnapshots",
)


def migrate_relay_state(raw: Dict[str, Any], local_home: Optional[Path] = None) -> Dict[str, Any]:
    """Idempotently migrate v2 relay_state.json into v3 with recovery/versioning fields."""
    raw_in = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    raw_ver = _int_version(raw_in.get("version"), 2)
    is_v2 = raw_ver < RELAY_STATE_VERSION

    if local_home is not None and is_v2:
        state_path = Path(local_home) / "relay_state.json"
        if state_path.exists():
            ensure_backup_once(state_path, Path(local_home) / "relay_state.v2.backup.json")

    out = dict(raw_in)
    out["version"] = RELAY_STATE_VERSION
    for key in _RELAY_STATE_DICT_KEYS:
        val = out.get(key)
        out[key] = val if isinstance(val, dict) else {}

    if is_v2:
        for entry in out["pendingSends"].values():
            if not isinstance(entry, dict):
                continue
            if "prepared" in entry:
                entry.setdefault("protocolVersion", 2)
            elif not entry.get("checkpointBeforeReady"):
                entry["status"] = "needs_verification"
                entry["verificationRequired"] = True

        for entry in out["appliedReceives"].values():
            if not isinstance(entry, dict):
                continue
            entry["workspaceApplied"] = True
            entry["migratedFromV2"] = True

        active_handoffs = out["activeHandoffs"]
        for rid, entry in out["sentRelays"].items():
            if not isinstance(entry, dict):
                continue
            if not entry.get("acked"):
                proj_id = entry.get("projectId")
                if isinstance(proj_id, str) and proj_id:
                    active_handoffs.setdefault(proj_id, rid)

    return out


def _collect_known_identities(
    local_ident: Optional[Dict[str, Any]],
    cloud_cfg: Optional[Dict[str, Any]],
) -> List[Dict[str, str]]:
    known: List[Dict[str, str]] = []
    if isinstance(local_ident, dict):
        did = str(local_ident.get("deviceId") or "").strip()
        if did:
            known.append(
                {
                    "deviceId": did,
                    "alias": str(local_ident.get("alias") or local_ident.get("id") or "").strip(),
                    "host": str(local_ident.get("host") or "").strip(),
                }
            )
    if isinstance(cloud_cfg, dict):
        peers = cloud_cfg.get("peers")
        if isinstance(peers, dict):
            for peer in peers.values():
                if isinstance(peer, dict):
                    did = str(peer.get("deviceId") or "").strip()
                    if did:
                        known.append(
                            {
                                "deviceId": did,
                                "alias": str(peer.get("alias") or peer.get("id") or "").strip(),
                                "host": str(peer.get("host") or "").strip(),
                            }
                        )
        legacy_peer = cloud_cfg.get("peer")
        if isinstance(legacy_peer, dict):
            did = str(legacy_peer.get("deviceId") or "").strip()
            if did:
                known.append(
                    {
                        "deviceId": did,
                        "alias": str(legacy_peer.get("alias") or legacy_peer.get("id") or "").strip(),
                        "host": str(legacy_peer.get("host") or "").strip(),
                    }
                )
    return known


def migrate_ledger_identities(
    ledger: Dict[str, Any],
    local_ident: Optional[Dict[str, Any]] = None,
    cloud_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Idempotently attach known deviceIds onto ledger devices/projects without guessing."""
    out = copy.deepcopy(ledger) if isinstance(ledger, dict) else {}
    config = out.get("config")
    if not isinstance(config, dict):
        return out
    devices = config.get("devices")
    if not isinstance(devices, list):
        return out

    known = _collect_known_identities(local_ident, cloud_cfg)
    used_dids = {
        str(d.get("deviceId") or "").strip()
        for d in devices
        if isinstance(d, dict) and str(d.get("deviceId") or "").strip()
    }

    # Map known deviceIds strictly by exact alias or unique case-insensitive alias.
    for info in known:
        did = info["deviceId"]
        alias = info["alias"]
        if not did or did in used_dids or not alias:
            continue
        exact = [d for d in devices if isinstance(d, dict) and d.get("id") == alias]
        candidates = exact
        if not candidates:
            ci = [
                d
                for d in devices
                if isinstance(d, dict) and str(d.get("id") or "").lower() == alias.lower()
            ]
            if len(ci) == 1:
                candidates = ci
        if len(candidates) == 1 and not str(candidates[0].get("deviceId") or "").strip():
            candidates[0]["deviceId"] = did
            used_dids.add(did)

    alias_to_did: Dict[str, str] = {}
    for d in devices:
        if isinstance(d, dict):
            d_id = str(d.get("id") or "").strip()
            d_did = str(d.get("deviceId") or "").strip()
            if d_id and d_did:
                alias_to_did[d_id] = d_did

    referenced_aliases = set()
    projects = out.get("projects")
    if isinstance(projects, list):
        for proj in projects:
            if not isinstance(proj, dict):
                continue
            p_dev = proj.get("device")
            if isinstance(p_dev, str) and p_dev.strip():
                referenced_aliases.add(p_dev.strip())

            paths = proj.get("paths")
            if isinstance(paths, dict):
                for dev_alias, p_val in paths.items():
                    if isinstance(p_val, str) and p_val.strip():
                        referenced_aliases.add(dev_alias)
                        did = alias_to_did.get(dev_alias)
                        if did:
                            p_by_did = proj.get("pathsByDeviceId")
                            if not isinstance(p_by_did, dict):
                                p_by_did = {}
                                proj["pathsByDeviceId"] = p_by_did
                            if not p_by_did.get(did):
                                p_by_did[did] = p_val

            sessions = proj.get("sessions")
            if isinstance(sessions, list):
                for sess in sessions:
                    if not isinstance(sess, dict):
                        continue
                    s_dev = sess.get("device")
                    if isinstance(s_dev, str) and s_dev.strip():
                        referenced_aliases.add(s_dev.strip())
                        did = alias_to_did.get(s_dev.strip())
                        if did and not sess.get("deviceId"):
                            sess["deviceId"] = did

            history = proj.get("history")
            if isinstance(history, list):
                for item in history:
                    if not isinstance(item, dict):
                        continue
                    for side_key, did_key in (("from", "fromDeviceId"), ("to", "toDeviceId")):
                        side_val = item.get(side_key)
                        if isinstance(side_val, str) and side_val.strip():
                            referenced_aliases.add(side_val.strip())
                            did = alias_to_did.get(side_val.strip())
                            if did and not item.get(did_key):
                                item[did_key] = did

            handoff = proj.get("handoff")
            if isinstance(handoff, dict):
                for side_key, did_key in (("from", "fromDeviceId"), ("to", "toDeviceId")):
                    side_val = handoff.get(side_key)
                    if isinstance(side_val, str) and side_val.strip():
                        referenced_aliases.add(side_val.strip())
                        did = alias_to_did.get(side_val.strip())
                        if did and not handoff.get(did_key):
                            handoff[did_key] = did

            conflicts = proj.get("conflicts")
            if isinstance(conflicts, list):
                for item in conflicts:
                    if isinstance(item, dict):
                        c_dev = item.get("device")
                        if isinstance(c_dev, str) and c_dev.strip():
                            referenced_aliases.add(c_dev.strip())

    for d in devices:
        if not isinstance(d, dict):
            continue
        if str(d.get("deviceId") or "").strip():
            d.pop("needsPairing", None)
        elif d.get("id") in referenced_aliases:
            d["needsPairing"] = True

    return out
