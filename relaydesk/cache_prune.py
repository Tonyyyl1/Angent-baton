# -*- coding: utf-8 -*-
"""Phase D: local cache statistics (``relay cache status``) and safe pruning (``relay cache prune``).

What is measured (all under the local home, never the cloud):

* payloads            ``cache/snapshots/<versionId>/`` (``payload.tar.gz`` plus its signed record)
* signed metadata     ``cache/lineage/<versionId>.json`` and the ``snapshot.json`` inside payload dirs
* ciphertext checkpoints ``sync_publications/<sha256(readyKey)>/`` (exact ready + package of a cache delivery)
* recovery data       ``transactions/<relayId>/`` (receive journals; never pruned)

The retention set is recomputed from durable state every time (Phase C model,
``delivery_state.retention_inputs``) plus unresolved version forks, unfinished
receive transactions and their rollback targets. Prune keeps:

* payloads of every project's current version, every graph head (fork heads
  included), both sides of an unresolved ``version_fork``, in-flight handoffs,
  pending sends, receipts not yet uploaded, unfinished receive transactions and
  their rollback version, deliveries a paired peer has not confirmed yet, and
  snapshots this device or a fresh peer have currently asks for;
* checkpoints of ``preparing`` generations and of published deliveries the
  recipient's current key has not confirmed; a checkpoint is deletable only after
  its publish state (``published``/``abandoned`` generation matching the
  checkpoint bytes) and receive state (peer-verified, recipient key superseded,
  or recipient no longer paired) were both checked;
* anything corrupt, unexpected (extra files such as credentials), symlinked or
  otherwise unclear: kept and reported;
* signed metadata always: a pruned payload's ``snapshot.json`` is first saved as
  ``cache/lineage/<versionId>.json`` so parent-chain verification still works.

Never touched: identity, cloud and relay state files, ``deliveries.json``
records (only ``prunedSnapshots`` is appended), ``processedDeliveries`` and
other anti-replay records, ``transactions/``, ``cache/lineage/``, the cloud and
any credential file. Prune never contacts the cloud; the next ``relay sync``
republishes the have manifest.

``--dry-run`` and ``status`` write nothing. ``--apply`` takes the local
workspace lock, recomputes the plan under it (a dry-run list is never replayed;
``expect_plan`` refuses when the plan changed), and deletes each entry by first
renaming it to a ``.pruning-*`` tombstone, so an interrupted prune leaves either
the intact entry or a tombstone that the next run finishes deleting.
"""
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import crypto
from . import delivery_state as DS
from . import ledger as L
from . import validation as V

__all__ = [
    "STATUS_SCHEMA",
    "PRUNE_SCHEMA",
    "UNCLEAR_STATES",
    "PrunePlanChangedError",
    "scan_inventory",
    "build_plan",
    "cache_status",
    "prune",
    "payload_present",
]

STATUS_SCHEMA = "relaydesk.cache.status/1"
PRUNE_SCHEMA = "relaydesk.cache.prune/1"
TOMBSTONE_PREFIX = ".pruning-"
PREPARING_PREFIX = ".preparing-"
PAYLOAD_FILES = frozenset(("payload.tar.gz", "snapshot.json", "metadata.json", "meta_plain.json", DS.MEMO_NAME))
_PAYLOAD_TEMP_RE = re.compile(r"^\.(payload-.*\.tmp|(snapshot|metadata|meta_plain|verified)\.json-.*\.tmp)$")
CHECKPOINT_FILES = frozenset(("ready.json", "package.enc"))
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
UNCLEAR_STATES = ("corrupt", "unexpected_files", "foreign")

REASON_TEXT = {
    # kept
    "localCurrentVersion": "项目当前版本",
    "versionGraphHead": "版本图头（含分叉头）",
    "pendingSend": "待完成的交棒",
    "unackedHandoff": "在途交接（尚未签收）",
    "receiptNotUploaded": "签收回执尚未上传",
    "awaitingPeerVerification": "对端尚未用当前公钥确认缓存（补发需要）",
    "ownNeeds": "本机正在请求补发",
    "peerNeeds": "对端正在请求补发",
    "unresolvedFork": "未解决的版本分叉",
    "recoveryTransaction": "未完成的接棒事务",
    "rollbackTarget": "回滚需要的上一版本",
    "recoveryUnclear": "有无法读取的恢复事务，暂不清理任何载荷",
    "stateUnclear": "本机状态无法读取，暂不清理",
    "corrupt": "损坏或不完整（保留并报告）",
    "unexpected_files": "目录中有不属于缓存的文件（保留并报告）",
    "foreign": "不是本机写入的缓存条目（保留并报告）",
    "lineageConflict": "已保存的签名元数据与该条目不一致（保留并报告）",
    "unfinishedDelivery": "未完成的投递（可续传）",
    "noDeliveryRecord": "没有对应的投递记录（保留并报告）",
    "checkpointMismatch": "检查点与投递记录不一致（保留并报告）",
    "projectFilter": "不属于所选项目",
    # deletable
    "notReferenced": "不在保留集合中；签名元数据已保存到 cache/lineage",
    "peerVerified": "接收方已用当前公钥确认缓存",
    "recipientKeySuperseded": "接收方已换钥，旧公钥的投递已作废",
    "peerNotPaired": "接收设备已不在配对列表中",
    "abandonedGeneration": "已被新一代投递取代，从未续传",
    "interruptedPrune": "上次清理中断后留下、已判定删除的目录",
    "preparingTemp": "投递准备中断留下的临时目录（从未发布）",
}

NEVER_TOUCHED = [
    "identity.json", "cloud.json", "relay_state.json（含 processedDeliveries 等防重放记录）",
    "deliveries.json 中的投递记录（只追加 prunedSnapshots）", "transactions/（恢复事务）", "cache/lineage/（签名元数据）",
    "archives/（会话归档）", "云端对象", "凭据文件",
]


class PrunePlanChangedError(Exception):
    """``--apply --expect-plan`` was given a digest that no longer matches the recomputed plan."""

    code = "prune_plan_changed"

    def __init__(self, message: str, plan: Dict[str, Any]) -> None:
        Exception.__init__(self, message)
        self.message = message
        self.plan = plan


def _engine():
    from . import relay_engine
    return relay_engine


# ---------------------------------------------------------------- filesystem helpers (never follow symlinks)


def _tree_size(path: Path) -> Tuple[int, int]:
    """(bytes, files) under ``path`` counting symlinks by their own size; never follows them."""
    total, files = 0, 0
    try:
        st = os.lstat(str(path))
    except OSError:
        return 0, 0
    if not os.path.isdir(str(path)) or os.path.islink(str(path)):
        return st.st_size, 1
    for root, dirs, names in os.walk(str(path), followlinks=False):
        for name in names + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
                files += 1
            except OSError:
                continue
    return total, files


def _list_dir(path: Path) -> List[os.DirEntry]:
    try:
        if not path.is_dir() or path.is_symlink():
            return []
        with os.scandir(str(path)) as it:
            return sorted(it, key=lambda e: e.name)
    except OSError:
        return []


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.is_symlink():
            return None
        with open(str(path), "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _hash_file(path: Path) -> Tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(str(path), "rb") as fh:
        while True:
            buf = fh.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
            n += len(buf)
    return h.hexdigest(), n


def payload_present(base: Any, vid: str) -> bool:
    """Cheap, read-only: the payload and its record exist for ``vid`` (no hashing)."""
    try:
        V.validate_version_id(vid)
    except V.ValidationError:
        return False
    cdir = Path(base) / "cache" / "snapshots" / vid
    return (cdir.is_dir() and not cdir.is_symlink() and (cdir / "payload.tar.gz").is_file()
            and (cdir / "snapshot.json").is_file())


# ---------------------------------------------------------------- inventory (read-only)


def _payload_entry(base: Path, entry: os.DirEntry, attested: Dict[str, str], hash_mode: str) -> Dict[str, Any]:
    """One ``cache/snapshots`` entry: state is verified / unverified / corrupt / unexpected_files / foreign."""
    path = Path(entry.path)
    size, files = _tree_size(path)
    out: Dict[str, Any] = {"snapshotId": entry.name, "path": str(path), "bytes": size, "files": files}
    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
        out.update(state="foreign", problem="不是目录或是符号链接")
        return out
    try:
        V.validate_version_id(entry.name)
    except V.ValidationError:
        out.update(state="foreign", problem="目录名不是合法的版本编号")
        return out
    names = [e.name for e in _list_dir(path)]
    extra = [n for n in names if n not in PAYLOAD_FILES and not _PAYLOAD_TEMP_RE.match(n)]
    links = [n for n in names if os.path.islink(str(path / n))]
    snap = _read_json(path / "snapshot.json")
    if snap:
        out["projectId"] = str(snap.get("projectId") or "")
        out["authorDeviceId"] = str(snap.get("authorDeviceId") or "")
        out["createdAt"] = str(snap.get("createdAt") or "")
    if extra or links:
        out.update(state="unexpected_files", problem="包含不属于缓存的文件：%s" % "、".join(sorted(set(extra + links))[:5]))
        return out
    tar = path / "payload.tar.gz"
    if not tar.is_file() or not (path / "snapshot.json").is_file():
        out.update(state="corrupt", problem="缺少 payload.tar.gz 或 snapshot.json")
        return out
    if not snap or snap.get("versionId") != entry.name:
        out.update(state="corrupt", problem="snapshot.json 无法解析或版本编号不符")
        return out
    expected = str(snap.get("contentSha256") or "").lower()
    if len(expected) != 64:
        out.update(state="corrupt", problem="签名记录缺少载荷摘要")
        return out
    if attested.get(entry.name):
        try:
            if crypto.snapshot_record_digest(snap) != attested[entry.name]:
                out.update(state="corrupt", problem="签名记录与版本图记录的摘要不一致")
                return out
        except Exception:
            out.update(state="corrupt", problem="签名记录无法计算摘要")
            return out
    try:
        st = tar.stat()
    except OSError:
        out.update(state="corrupt", problem="载荷不可读")
        return out
    memo = _read_json(path / DS.MEMO_NAME) or {}
    memo_ok = (memo.get("size") == st.st_size and memo.get("mtimeNs") == st.st_mtime_ns
               and str(memo.get("sha256") or "").lower() == expected)
    if hash_mode == "never" or (hash_mode == "memo" and memo_ok):
        out["state"] = "verified" if memo_ok else "unverified"
        return out
    try:
        digest, n = _hash_file(tar)
    except OSError:
        out.update(state="corrupt", problem="载荷不可读")
        return out
    if digest != expected or n != st.st_size:
        out.update(state="corrupt", problem="载荷摘要与签名记录不一致")
        return out
    out["state"] = "verified"
    return out


def _checkpoint_entry(entry: os.DirEntry) -> Dict[str, Any]:
    path = Path(entry.path)
    size, files = _tree_size(path)
    out: Dict[str, Any] = {"name": entry.name, "path": str(path), "bytes": size, "files": files}
    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False) or not _HEX64.match(entry.name):
        out.update(state="foreign", problem="不是本机写入的投递检查点")
        return out
    names = [e.name for e in _list_dir(path)]
    extra = [n for n in names if n not in CHECKPOINT_FILES or os.path.islink(str(path / n))]
    if extra:
        out.update(state="unexpected_files", problem="包含不属于检查点的文件：%s" % "、".join(sorted(extra)[:5]))
        return out
    ready = _read_json(path / "ready.json")
    if not ready:
        out.update(state="corrupt", problem="ready.json 缺失或无法解析")
        return out
    to_did = str(ready.get("toDeviceId") or "")
    deliv = str(ready.get("relayId") or "")
    ready_key = "v3/inbox/%s/%s/ready.json" % (to_did, deliv)
    if hashlib.sha256(ready_key.encode("utf-8")).hexdigest() != entry.name:
        out.update(state="corrupt", problem="ready.json 与检查点目录名不一致")
        return out
    snap = ready.get("snapshot") if isinstance(ready.get("snapshot"), dict) else {}
    out.update(state="ok", readyKey=ready_key, deliveryId=deliv, toDeviceId=to_did,
               snapshotId=str(snap.get("versionId") or ""), projectId=str(snap.get("projectId") or ""),
               packageSha256=str(ready.get("packageSha256") or ""), hasPackage="package.enc" in names)
    return out


def scan_inventory(base: Any, hash_payloads: Any = "memo", attested: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Read-only inventory of the local cache. ``hash_payloads``: ``never``, ``memo`` (trust a valid memo) or ``always``."""
    base = Path(base)
    mode = {True: "memo", False: "never"}.get(hash_payloads, hash_payloads) if isinstance(hash_payloads, bool) \
        else str(hash_payloads)
    attested = attested or {}
    payloads: List[Dict[str, Any]] = []
    leftovers: List[Dict[str, Any]] = []
    snap_root = base / "cache" / "snapshots"
    for entry in _list_dir(snap_root):
        if entry.name.startswith(TOMBSTONE_PREFIX):
            size, files = _tree_size(Path(entry.path))
            leftovers.append({"kind": "payload", "name": entry.name, "path": entry.path, "bytes": size,
                              "reason": "interruptedPrune", "symlink": entry.is_symlink()})
            continue
        payloads.append(_payload_entry(base, entry, attested, mode))
    lineage_count, lineage_bytes = 0, 0
    for entry in _list_dir(base / "cache" / "lineage"):
        if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
            lineage_count += 1
            try:
                lineage_bytes += entry.stat(follow_symlinks=False).st_size
            except OSError:
                pass
    in_payload = [(Path(p["path"]) / "snapshot.json") for p in payloads if p["state"] not in ("foreign",)]
    in_payload_bytes = 0
    for f in in_payload:
        try:
            in_payload_bytes += os.lstat(str(f)).st_size
        except OSError:
            continue
    checkpoints: List[Dict[str, Any]] = []
    for entry in _list_dir(base / "sync_publications"):
        if entry.name.startswith(TOMBSTONE_PREFIX) or entry.name.startswith(PREPARING_PREFIX):
            size, files = _tree_size(Path(entry.path))
            leftovers.append({"kind": "checkpoint", "name": entry.name, "path": entry.path, "bytes": size,
                              "reason": "interruptedPrune" if entry.name.startswith(TOMBSTONE_PREFIX) else "preparingTemp",
                              "symlink": entry.is_symlink()})
            continue
        checkpoints.append(_checkpoint_entry(entry))
    recovery: List[Dict[str, Any]] = []
    tx_root = base / "transactions"
    if tx_root.is_dir() and not tx_root.is_symlink():
        from . import workspace_tx as W
        for entry in _list_dir(tx_root):
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            size, files = _tree_size(Path(entry.path))
            item: Dict[str, Any] = {"relayId": entry.name, "bytes": size, "files": files}
            try:
                journal = W.load_and_verify_journal(base, entry.name)
                item["phase"] = str(journal.get("phase") or "")
                item["projectId"] = str(journal.get("projectId") or "")
            except Exception as e:
                item["phase"] = ""
                item["problem"] = str(getattr(e, "message", "") or e)[:200]
            recovery.append(item)
    return {
        "payloadEntries": payloads,
        "checkpointEntries": checkpoints,
        "recoveryEntries": recovery,
        "lineage": {"count": lineage_count, "bytes": lineage_bytes},
        "signedInPayloads": {"count": len(in_payload), "bytes": in_payload_bytes},
        "leftovers": {"count": len(leftovers), "bytes": sum(x["bytes"] for x in leftovers), "items": leftovers},
    }


# ---------------------------------------------------------------- retention plan (read-only)


def _blocked(code: str, **ctx: Any) -> Dict[str, Any]:
    from . import diagnostics as DX
    return DX.finding(code, **ctx)


def _load_inputs(base: Path, led_path: Optional[Path]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Strictly load everything the retention set depends on; any damage blocks pruning entirely."""
    E = _engine()
    blocked: List[Dict[str, Any]] = []
    inputs: Dict[str, Any] = {"relayState": {}, "book": None, "cloud": {}, "ledger": None}
    try:
        inputs["relayState"] = E.load_relay_state(base) or {}
    except V.LocalStateCorruptError as e:
        blocked.append(_blocked("relay_state_corrupted", path=getattr(e, "path", ""), detail=getattr(e, "message", str(e))))
    try:
        inputs["book"] = DS.load(base)
    except DS.DeliveryStateCorruptedError as e:
        blocked.append(_blocked("delivery_state_corrupted", path=getattr(e, "path", ""),
                                detail=getattr(e, "message", str(e))))
    try:
        inputs["cloud"] = E.load_cloud_config(base) or {}
    except V.LocalStateCorruptError as e:
        blocked.append(_blocked("cloud_config_corrupted", path=getattr(e, "path", ""), detail=getattr(e, "message", str(e))))
    if led_path is not None:
        try:
            inputs["ledger"] = L.load(led_path)
        except L.LedgerError as e:
            blocked.append(_blocked("ledger_unreadable", path=str(led_path), detail=str(e)))
    return inputs, blocked


def _transaction_refs(base: Path) -> Tuple[Dict[str, Set[str]], bool]:
    """Snapshots unfinished receive transactions reference; ``unclear`` when any record cannot be read."""
    from . import workspace_tx as W
    refs: Dict[str, Set[str]] = {}
    unclear = False
    try:
        tx_ids = W.list_transactions(base)
    except Exception:
        return refs, True
    for rid in tx_ids:
        try:
            journal = W.load_and_verify_journal(base, rid)
        except Exception:
            unclear = True
            continue
        if journal.get("phase") in (W.PHASE_COMPLETED, W.PHASE_ROLLED_BACK):
            continue
        try:
            meta = W.load_transaction_meta(base, rid)
        except Exception:
            unclear = True
            continue
        extra = meta.get("extra") or {}
        snap = extra.get("snapshot") if isinstance(extra.get("snapshot"), dict) else {}
        if snap.get("versionId"):
            refs.setdefault(str(snap["versionId"]), set()).add("recoveryTransaction")
        if extra.get("prevLocalVersion"):
            refs.setdefault(str(extra["prevLocalVersion"]), set()).add("rollbackTarget")
    return refs, unclear


def _retained(inputs: Dict[str, Any], base: Path) -> Tuple[Dict[str, Set[str]], Dict[str, Any], bool]:
    rs = inputs["relayState"]
    book = inputs["book"] or {}
    cloud = inputs["cloud"] or {}
    peers = cloud.get("peers") if isinstance(cloud.get("peers"), dict) else {}
    paired_fps = {did: str(p.get("keyFingerprint") or "") for did, p in peers.items()
                  if isinstance(p, dict) and p.get("paired") and p.get("status") == "paired"}
    fresh_needs: Dict[str, List[str]] = {}
    for did, have in (book.get("peerHave") or {}).items():
        if did in paired_fps and isinstance(have, dict) and \
                DS.norm_fp(have.get("keyFingerprint")) == DS.norm_fp(paired_fps[did]):
            fresh_needs[did] = [s for s in (have.get("needs") or []) if isinstance(s, str)]
    ret = DS.retention_inputs(book, rs, paired_fps, fresh_needs)
    keep: Dict[str, Set[str]] = {}
    for sid, why in ret["snapshots"].items():
        keep.setdefault(sid, set()).update("peerNeeds" if w.startswith("peerNeeds:") else w for w in why)
    for pid, pend in (rs.get("pendingSends") or {}).items():
        origin = pend.get("reissueOrigin") if isinstance(pend, dict) else None
        if isinstance(origin, dict) and origin.get("snapshotId"):
            keep.setdefault(str(origin["snapshotId"]), set()).add("pendingSend")
    for p in ((inputs.get("ledger") or {}).get("projects") or []):
        for c in (p.get("conflicts") or []) if isinstance(p, dict) else []:
            if isinstance(c, dict) and c.get("kind") == "version_fork" and not c.get("resolved"):
                for vid in (c.get("localVersion"), c.get("incomingVersion")):
                    if isinstance(vid, str) and vid:
                        keep.setdefault(vid, set()).add("unresolvedFork")
    tx_refs, tx_unclear = _transaction_refs(base)
    for sid, why in tx_refs.items():
        keep.setdefault(sid, set()).update(why)
    return keep, ret, tx_unclear


def _attested(rs: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for graph in (rs.get("versionGraph") or {}).values():
        for vid, node in ((graph or {}).get("nodes") or {}).items() if isinstance(graph, dict) else []:
            if isinstance(node, dict) and node.get("recordSha256"):
                out[str(vid)] = str(node["recordSha256"])
    return out


def _generation_map(book: Dict[str, Any]) -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
    out: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
    for ent in (book.get("outbound") or {}).values():
        if not isinstance(ent, dict):
            continue
        for g in ent.get("generations") or []:
            if isinstance(g, dict) and g.get("readyKey"):
                out[str(g["readyKey"])] = (ent, g)
    return out


def _checkpoint_delete_reason(ent: Dict[str, Any], gen: Dict[str, Any], paired_fps: Dict[str, str]) -> str:
    """Why a checkpoint's delivery needs no local copy any more (receive state), '' when it still might."""
    did = str(ent.get("recipientDeviceId") or "")
    if did not in paired_fps:
        return "peerNotPaired"
    if DS.norm_fp(paired_fps[did]) != DS.norm_fp(ent.get("recipientKeyFingerprint")) or ent.get("status") == "superseded":
        return "recipientKeySuperseded"
    if ent.get("status") == "peer_verified" and isinstance(ent.get("peerVerified"), dict):
        return "peerVerified"
    latest = DS.latest_generation(ent)
    if gen.get("state") == "abandoned" and latest is not None and latest is not gen:
        return "abandonedGeneration"
    return ""


def _item(kind: str, reason: str, **fields: Any) -> Dict[str, Any]:
    out = {"kind": kind, "reason": reason, "reasonText": REASON_TEXT.get(reason, reason)}
    out.update(fields)
    return out


def build_plan(base: Any, led_path: Optional[Any] = None, project: Optional[str] = None,
               hash_payloads: str = "always") -> Dict[str, Any]:
    """Read-only: what may be deleted, what is kept and why, estimated bytes freed, and a plan digest."""
    base = Path(base)
    inputs, blocked = _load_inputs(base, Path(led_path) if led_path is not None else None)
    rs = inputs["relayState"]
    book = inputs["book"]
    if book is None and not blocked:
        blocked.append(_blocked("delivery_state_missing"))
    keep: Dict[str, Set[str]] = {}
    paired_fps: Dict[str, str] = {}
    keep_ckpt: Set[str] = set()
    drop_ckpt: Set[str] = set()
    tx_unclear = False
    if not blocked:
        keep, ret, tx_unclear = _retained(inputs, base)
        keep_ckpt = set(ret["checkpointReadyKeys"])
        drop_ckpt = set(ret["deletableCheckpointReadyKeys"])
        cloud = inputs["cloud"] or {}
        peers = cloud.get("peers") if isinstance(cloud.get("peers"), dict) else {}
        paired_fps = {did: str(p.get("keyFingerprint") or "") for did, p in peers.items()
                      if isinstance(p, dict) and p.get("paired") and p.get("status") == "paired"}
    # Retained payloads: a valid memo is enough. Deletion candidates are always hashed, so corrupt ones are kept.
    attested = _attested(rs)
    inv = scan_inventory(base, hash_payloads="memo", attested=attested)
    plan_payload_delete: List[Dict[str, Any]] = []
    plan_payload_keep: List[Dict[str, Any]] = []
    unclear: List[Dict[str, Any]] = []
    for e in inv["payloadEntries"]:
        vid = e["snapshotId"]
        base_fields = {"snapshotId": vid, "projectId": e.get("projectId", ""), "bytes": e["bytes"], "state": e["state"]}
        if project and e.get("projectId") != project:
            continue
        if e["state"] in UNCLEAR_STATES:
            item = _item("payload", e["state"], problem=e.get("problem", ""), **base_fields)
            plan_payload_keep.append(item)
            unclear.append(item)
            continue
        if blocked:
            plan_payload_keep.append(_item("payload", "stateUnclear", **base_fields))
            continue
        if vid in keep:
            reasons = sorted(keep[vid])
            item = _item("payload", reasons[0], reasons=reasons,
                         reasonTexts=[REASON_TEXT.get(r, r) for r in reasons], **base_fields)
            plan_payload_keep.append(item)
            continue
        if tx_unclear:
            plan_payload_keep.append(_item("payload", "recoveryUnclear", **base_fields))
            continue
        if hash_payloads != "never":
            checked = _payload_entry(base, _FakeEntry(Path(e["path"])), attested, "always")
            if checked["state"] != "verified":
                item = _item("payload", "corrupt", problem=checked.get("problem", ""), **dict(base_fields, state="corrupt"))
                plan_payload_keep.append(item)
                unclear.append(item)
                continue
        lineage = base / "cache" / "lineage" / ("%s.json" % vid)
        snap = _read_json(Path(e["path"]) / "snapshot.json")
        if lineage.is_symlink() or (lineage.exists() and _read_json(lineage) != snap):
            item = _item("payload", "lineageConflict", **base_fields)
            plan_payload_keep.append(item)
            unclear.append(item)
            continue
        plan_payload_delete.append(_item("payload", "notReferenced", lineage="exists" if lineage.exists() else "copy",
                                         **base_fields))
    gen_map = _generation_map(book or {})
    ck_delete: List[Dict[str, Any]] = []
    ck_keep: List[Dict[str, Any]] = []
    for c in inv["checkpointEntries"]:
        rk = c.get("readyKey", "")
        ent, gen = gen_map.get(rk, ({}, {}))
        proj = str(ent.get("projectId") or c.get("projectId") or "")
        fields = {"name": c["name"], "bytes": c["bytes"], "state": c["state"], "readyKey": rk,
                  "deliveryId": c.get("deliveryId", ""), "toDeviceId": c.get("toDeviceId", ""),
                  "snapshotId": c.get("snapshotId", ""), "projectId": proj}
        if project and proj != project:
            continue
        if c["state"] in UNCLEAR_STATES:
            item = _item("checkpoint", c["state"], problem=c.get("problem", ""), **fields)
            ck_keep.append(item)
            unclear.append(item)
            continue
        if blocked:
            ck_keep.append(_item("checkpoint", "stateUnclear", **fields))
            continue
        if not gen:
            item = _item("checkpoint", "noDeliveryRecord", **fields)
            ck_keep.append(item)
            unclear.append(item)
            continue
        fields["generationState"] = str(gen.get("state") or "")
        fields["entryStatus"] = str(ent.get("status") or "")
        if rk in keep_ckpt or gen.get("state") == "preparing":
            ck_keep.append(_item("checkpoint", "unfinishedDelivery" if gen.get("state") == "preparing"
                                 else "awaitingPeerVerification", **fields))
            continue
        # Publish state: the generation finished (published, or abandoned and never resumed) and the
        # checkpoint is exactly that generation's ready. Receive state: confirmed, superseded or unpaired.
        pub_ok = gen.get("state") in ("published", "abandoned") and gen.get("deliveryId") == c.get("deliveryId") and (
            not gen.get("packageSha256") or gen.get("packageSha256") == c.get("packageSha256"))
        why = _checkpoint_delete_reason(ent, gen, paired_fps) if pub_ok else ""
        if not pub_ok:
            item = _item("checkpoint", "checkpointMismatch", **fields)
            ck_keep.append(item)
            unclear.append(item)
        elif why and rk in drop_ckpt:
            ck_delete.append(_item("checkpoint", why, **fields))
        else:
            ck_keep.append(_item("checkpoint", "awaitingPeerVerification", **fields))
    leftovers = [_item("leftover", x["reason"], name=x["name"], path=x["path"], bytes=x["bytes"], area=x["kind"])
                 for x in inv["leftovers"]["items"] if not x.get("symlink")]
    material = sorted([("payload", x["snapshotId"], x["bytes"]) for x in plan_payload_delete]
                      + [("checkpoint", x["name"], x["bytes"]) for x in ck_delete]
                      + [("leftover", x["area"] + "/" + x["name"], x["bytes"]) for x in leftovers])
    digest = hashlib.sha256(json.dumps(material).encode("utf-8")).hexdigest()
    estimated = sum(x["bytes"] for x in plan_payload_delete) + sum(x["bytes"] for x in ck_delete) \
        + sum(x["bytes"] for x in leftovers)
    return {
        "blocked": blocked,
        "planDigest": digest,
        "estimatedBytesFreed": estimated,
        "deletable": {"payloads": plan_payload_delete, "checkpoints": ck_delete, "leftovers": leftovers},
        "kept": {"payloads": plan_payload_keep, "checkpoints": ck_keep},
        "unclear": unclear,
        "retainedSnapshots": sorted(keep),
        "inventory": inv,
        "prunedSnapshots": sorted(((book or {}).get("prunedSnapshots") or {}).keys()),
    }


class _FakeEntry(object):
    """Minimal ``os.DirEntry`` stand-in for re-checking one known payload directory."""

    def __init__(self, path: Path) -> None:
        self.path = str(path)
        self.name = path.name

    def is_symlink(self) -> bool:
        return os.path.islink(self.path)

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        return os.path.isdir(self.path) and (follow_symlinks or not os.path.islink(self.path))


# ---------------------------------------------------------------- public entry points


def _context(project_key: Optional[str], ledger_path: Any, local: Any, local_home: Any, cwd: Optional[str]
             ) -> Tuple[Path, Optional[Path], Optional[str]]:
    E = _engine()
    base, led_path, _local = E._resolve_ctx(ledger_path, local, local_home)
    pid = None
    if project_key:
        led = L.load(led_path)
        pid = str(L.find_project(led, project_key, cwd or os.getcwd())["id"])  # LookupError if unknown
    return base, led_path, pid


def _summary_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    return {"count": len(items), "bytes": sum(int(x.get("bytes") or 0) for x in items)}


def cache_status(project_key: Optional[str] = None, ledger_path: Any = None, local: Any = None,
                 local_home: Any = None, cwd: Optional[str] = None) -> Dict[str, Any]:
    """``relay cache status [PROJECT] --json``: read-only counts and sizes, plus what a prune would free."""
    base, led_path, pid = _context(project_key, ledger_path, local, local_home, cwd)
    plan = build_plan(base, led_path, project=pid, hash_payloads="always")
    inv = plan["inventory"]
    payloads = [e for e in inv["payloadEntries"] if not pid or e.get("projectId") == pid]
    gen_map = _generation_map(DS.load(base) or {}) if not plan["blocked"] else {}
    checkpoints = [c for c in inv["checkpointEntries"]
                   if not pid or str((gen_map.get(c.get("readyKey", ""), ({}, {}))[0]).get("projectId")
                                     or c.get("projectId") or "") == pid]
    recovery = [r for r in inv["recoveryEntries"] if not pid or r.get("projectId") in ("", pid)]
    from . import workspace_tx as W
    finished = (W.PHASE_COMPLETED, W.PHASE_ROLLED_BACK)
    book = None
    if not plan["blocked"]:
        book = DS.load(base)
    by_state: Dict[str, int] = {}
    for e in payloads:
        by_state[e["state"]] = by_state.get(e["state"], 0) + 1
    return {
        "schema": STATUS_SCHEMA,
        "home": str(base),
        "project": pid or "",
        "payloads": dict(_summary_counts(payloads), states=by_state,
                         retained=len([e for e in plan["kept"]["payloads"] if e["reason"] not in UNCLEAR_STATES]),
                         deletable=len(plan["deletable"]["payloads"])),
        "signedMetadata": {"count": inv["lineage"]["count"] + inv["signedInPayloads"]["count"],
                           "bytes": inv["lineage"]["bytes"] + inv["signedInPayloads"]["bytes"],
                           "lineageFiles": inv["lineage"]["count"], "lineageBytes": inv["lineage"]["bytes"],
                           "inPayloadDirs": inv["signedInPayloads"]["count"]},
        "checkpoints": dict(_summary_counts(checkpoints), kept=len(plan["kept"]["checkpoints"]),
                            deletable=len(plan["deletable"]["checkpoints"])),
        "recovery": dict(_summary_counts(recovery),
                         incomplete=len([r for r in recovery if r.get("phase") and r["phase"] not in finished]),
                         completed=len([r for r in recovery if r.get("phase") == W.PHASE_COMPLETED]),
                         rolledBack=len([r for r in recovery if r.get("phase") == W.PHASE_ROLLED_BACK]),
                         unreadable=len([r for r in recovery if not r.get("phase")])),
        "leftovers": {"count": inv["leftovers"]["count"], "bytes": inv["leftovers"]["bytes"]},
        "deliveryState": {"exists": book is not None,
                          "outbound": len((book or {}).get("outbound") or {}),
                          "inbound": len((book or {}).get("inbound") or {}),
                          "prunedSnapshots": len((book or {}).get("prunedSnapshots") or {})},
        "prunable": {"payloads": len(plan["deletable"]["payloads"]), "checkpoints": len(plan["deletable"]["checkpoints"]),
                     "leftovers": len(plan["deletable"]["leftovers"]), "bytes": plan["estimatedBytesFreed"],
                     "planDigest": plan["planDigest"]},
        "unclear": plan["unclear"],
        "blocked": plan["blocked"],
    }


def _public_plan(plan: Dict[str, Any], mode: str, pid: Optional[str]) -> Dict[str, Any]:
    status = "blocked" if plan["blocked"] else "success"
    return {
        "schema": PRUNE_SCHEMA,
        "mode": mode,
        "ok": not plan["blocked"],
        "status": status,
        "project": pid or "",
        "planDigest": plan["planDigest"],
        "estimatedBytesFreed": plan["estimatedBytesFreed"],
        "deletable": plan["deletable"],
        "kept": plan["kept"],
        "unclear": plan["unclear"],
        "blocked": plan["blocked"],
        "counts": {
            "deletablePayloads": len(plan["deletable"]["payloads"]),
            "deletableCheckpoints": len(plan["deletable"]["checkpoints"]),
            "leftovers": len(plan["deletable"]["leftovers"]),
            "keptPayloads": len(plan["kept"]["payloads"]),
            "keptCheckpoints": len(plan["kept"]["checkpoints"]),
            "unclear": len(plan["unclear"]),
        },
        "neverTouched": list(NEVER_TOUCHED),
        "haveRefresh": "next_sync",
    }


def _rename_to_tombstone(path: Path) -> Path:
    tomb = path.parent / ("%s%s.%s" % (TOMBSTONE_PREFIX, path.name, secrets.token_hex(4)))
    os.rename(str(path), str(tomb))
    return tomb


def _remove_tree(path: Path, fault_point: str = "") -> None:
    """Delete a directory without following symlinks inside it; ``fault_point`` fires after the first file."""
    from . import workspace_tx as W
    if os.path.islink(str(path)):
        os.unlink(str(path))
        return
    fired = False
    for root, dirs, files in os.walk(str(path), topdown=False, followlinks=False):
        for name in sorted(files, key=lambda n: (n != "payload.tar.gz" and n != "package.enc", n)):
            os.unlink(os.path.join(root, name))
            if fault_point and not fired:
                fired = True
                W.maybe_inject_fault(fault_point)
        for name in dirs:
            p = os.path.join(root, name)
            if os.path.islink(p):
                os.unlink(p)
            else:
                os.rmdir(p)
    os.rmdir(str(path))


def _ensure_lineage(base: Path, vid: str, snap_path: Path) -> None:
    """Save the payload's signed record as ``cache/lineage/<vid>.json`` (re-read and compared) before deletion."""
    snap = _read_json(snap_path)
    if not snap or snap.get("versionId") != vid:
        raise ValueError("snapshot.json 无法读取")
    lineage = base / "cache" / "lineage" / ("%s.json" % vid)
    V.ensure_contained(base, lineage, field="cache")
    if lineage.exists():
        if _read_json(lineage) != snap:
            raise ValueError("cache/lineage/%s.json 与 snapshot.json 不一致" % vid)
        return
    _engine()._atomic_write_json(lineage, snap)
    if _read_json(lineage) != snap:
        raise ValueError("cache/lineage/%s.json 写入后校验失败" % vid)


def prune(project_key: Optional[str] = None, ledger_path: Any = None, local: Any = None, local_home: Any = None,
          cwd: Optional[str] = None, apply: bool = False, expect_plan: Optional[str] = None) -> Dict[str, Any]:
    """``relay cache prune``. Dry-run (default) writes nothing; ``apply`` deletes under the local lock."""
    from . import workspace_tx as W
    base, led_path, pid = _context(project_key, ledger_path, local, local_home, cwd)
    if not apply:
        return _public_plan(build_plan(base, led_path, project=pid), "dry-run", pid)
    E = _engine()
    with E._workspace_operation(base):
        plan = build_plan(base, led_path, project=pid)  # recomputed under the lock; never a replayed list
        result = _public_plan(plan, "apply", pid)
        if plan["blocked"]:
            result["deleted"] = {"payloads": [], "checkpoints": [], "leftovers": []}
            result["bytesFreed"] = 0
            return result
        if expect_plan and expect_plan != plan["planDigest"]:
            raise PrunePlanChangedError(
                "清理计划已变化（预览后有新的引用或状态变化），未删除任何内容；请重新运行 relay cache prune --dry-run 查看。",
                result)
        deleted: Dict[str, List[Dict[str, Any]]] = {"payloads": [], "checkpoints": [], "leftovers": []}
        failed: List[Dict[str, Any]] = []
        freed = 0
        for item in plan["deletable"]["payloads"]:
            vid = item["snapshotId"]
            cdir = base / "cache" / "snapshots" / vid
            try:
                V.ensure_contained(base, cdir, check_final=True, field="cache")
                if not cdir.is_dir() or cdir.is_symlink():
                    continue
                _ensure_lineage(base, vid, cdir / "snapshot.json")
                W.maybe_inject_fault("prune_after_lineage")
                # Marked first: a payload marked but still present is harmless (it stays usable); a payload gone
                # but unmarked would be requested again from peers.
                DS.mark_pruned(base, [vid])
                W.maybe_inject_fault("prune_after_mark")
                tomb = _rename_to_tombstone(cdir)
                W.maybe_inject_fault("prune_after_rename")
                _remove_tree(tomb, fault_point="prune_mid_delete")
            except (OSError, ValueError, V.ValidationError) as e:
                failed.append(dict(item, error=str(getattr(e, "message", "") or e)))
                continue
            deleted["payloads"].append(item)
            freed += int(item["bytes"])
        for item in plan["deletable"]["checkpoints"]:
            cdir = base / "sync_publications" / item["name"]
            try:
                V.ensure_contained(base, cdir, check_final=True, field="checkpoint")
                if not cdir.is_dir() or cdir.is_symlink():
                    continue
                tomb = _rename_to_tombstone(cdir)
                W.maybe_inject_fault("prune_ckpt_after_rename")
                _remove_tree(tomb)
            except (OSError, ValueError, V.ValidationError) as e:
                failed.append(dict(item, error=str(getattr(e, "message", "") or e)))
                continue
            deleted["checkpoints"].append(item)
            freed += int(item["bytes"])
        for item in plan["deletable"]["leftovers"]:
            p = Path(item["path"])
            try:
                V.ensure_contained(base, p.parent, check_final=True, field="cache")
                if os.path.lexists(str(p)):
                    _remove_tree(p)
            except (OSError, ValueError, V.ValidationError) as e:
                failed.append(dict(item, error=str(getattr(e, "message", "") or e)))
                continue
            deleted["leftovers"].append(item)
            freed += int(item["bytes"])
        # A prune mark on something retained (or re-cached since) must not stop its repair later.
        retained = set(plan["retainedSnapshots"])
        stale = [vid for vid in plan["prunedSnapshots"] if vid in retained and payload_present(base, vid)]
        if stale:
            DS.unmark_pruned(base, stale)
        W.maybe_inject_fault("prune_before_finish")
        result.update(deleted=deleted, failed=failed, bytesFreed=freed, unmarkedPruned=stale,
                      status="partial" if failed else "success", ok=not failed)
        return result
