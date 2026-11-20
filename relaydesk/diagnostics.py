# -*- coding: utf-8 -*-
"""Phase D: strictly read-only local diagnosis (``relay doctor``) and the shared advice table.

Every finding carries a severity (``info`` / ``warning`` / ``error``), a stable
``code``, a ``reason`` and exactly one concrete recovery ``command``. Reason and
command always come from :func:`advice`, so the CLI text, ``relay doctor --json``,
the local HTTP API and the web page show the same words for the same situation.

Nothing here writes: no lock files, no directories, no memo files, no network.
Local state is read with the strict loaders of Phase A/C (a damaged file is
reported, never replaced by defaults). The device identity is read only to
check signatures of this device's own records; it is never copied.

Python 3.9 compatible; standard library plus relay-desk modules. ``relay_engine``
is imported lazily so that the engine itself can import :func:`advice`.
"""
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import delivery_state as DS
from . import ledger as L
from . import sync_runtime as SR
from . import validation as V

__all__ = [
    "SCHEMA",
    "ADVICE",
    "advice",
    "finding",
    "worst_severity",
    "exit_code_for",
    "run_doctor",
    "rotation_blockers",
    "network_view",
]

SCHEMA = "relaydesk.doctor/1"
SEVERITIES = ("info", "warning", "error")
_RANK = {"info": 0, "warning": 1, "error": 2}

# Placeholders for context values a finding does not know (shown literally).
_PLACEHOLDER = {
    "pid": "<项目>", "to": "<目标设备>", "rid": "<交接编号>", "did": "<设备>", "peer": "<设备>",
    "path": "<文件>", "new": "<新交接编号>", "orig": "<原交接编号>", "from": "<发送方设备>",
    "me": "<本机别名>", "phase": "", "detail": "", "count": "0", "at": "", "local": "", "incoming": "",
    "target": "<链接目标>", "missing": "（未知）", "name": "<项目名称>", "dir": "<项目目录>",
}

# code -> (severity, reason template, command template). The single source of reasons and commands.
ADVICE: Dict[str, Tuple[str, str, str]] = {
    # ---- local identity and state files
    "identity_missing": ("error", "本机还没有设备身份（identity.json），不能加密交棒、验证回执或同步。",
                         "relay pair --remote <rclone 连接名> --root <云端目录>"),
    "identity_corrupted": ("error", "本机身份文件已损坏或无法读取（{detail}）。对端固定了本机公钥，不能删除或重新生成，"
                                    "只能从备份恢复。", 'cp "<identity.json 的备份>" "{path}"'),
    "config_corrupted": ("error", "设备配置 config.json 已损坏（{detail}），无法确定台账位置，已停止写入。",
                         'cp "<config.json 的备份>" "{path}"'),
    "cloud_config_corrupted": ("error", "云端连接配置 cloud.json 已损坏（{detail}）；为避免丢失已配对设备，"
                                        "云端操作已停止。", 'cp "<cloud.json 的备份>" "{path}"'),
    "relay_state_corrupted": ("error", "接力状态 relay_state.json 已损坏（{detail}）；收发、同步和缓存清理都已停止，"
                                       "以免覆盖在途记录。", 'cp "<relay_state.json 的备份>" "{path}"'),
    "delivery_state_corrupted": ("error", "投递状态 deliveries.json 已损坏（{detail}）；同步与缓存清理已停止。"
                                          "该文件可以从本机发布检查点重建。", 'mv "{path}" "{path}.corrupt" && relay sync'),
    "ledger_unreadable": ("error", "台账文件无法读取（{detail}）。", 'cp "<台账备份>" "{path}"'),
    "need_device": ("error", "无法识别本机是台账里的哪台设备。", "relay init --device <别名>"),
    "crypto_unavailable": ("error", "未安装 PyNaCl，无法签名、验签和加解密。", "python3 -m pip install pynacl"),
    # ---- pairing
    "cloud_not_configured": ("warning", "尚未配置云端连接，目前只能使用手动接力。",
                             "relay pair --remote <rclone 连接名> --root <云端目录>"),
    "no_paired_peers": ("warning", "云端已配置，但还没有核对指纹并确认配对的设备，无法加密交棒。", "relay pair --status"),
    "peer_pending_pairing": ("info", "发现设备 {peer}（{did}），尚未核对指纹确认配对。", "relay pair --with {did} --accept"),
    "peer_key_changed": ("error", "设备 {peer}（{did}）已更换密钥；在核对新指纹前，本机不接收也不发送它的数据。",
                         "relay pair --with {did} --accept --fingerprint <核对后的新配对指纹>"),
    # ---- outgoing handoffs
    "handoff_in_flight": ("info", "交接 {rid} 已发给 {to}，等待对方领取并回传签收回执。", "relay sync {pid}"),
    "handoff_recipient_key_changed": (
        "error", "交接 {rid} 是发给 {to} 换钥前公钥的，对方已无法领取；原交接包保持不变，"
                 "需要为它当前已重新确认的公钥重新签发（新交接编号、新密文）。", "relay send {pid} --to {to} --reissue"),
    "handoff_sender_key_rotated": (
        "error", "交接 {rid} 由本机换钥前的旧密钥签名，{to} 重新确认本机新公钥后将无法验证；原交接包保持不变，"
                 "需要用新密钥重新签发（新交接编号、新密文）。", "relay send {pid} --to {to} --reissue"),
    "handoff_target_key_changed": (
        "error", "交接 {rid} 的目标设备 {to} 已更换密钥但尚未重新确认；先核对新指纹，之后才能为新公钥重新签发。",
        "relay pair --with {did} --accept --fingerprint <核对后的新配对指纹>"),
    "handoff_superseded": ("info", "交接 {rid} 已由重新签发的 {new} 取代；原交接包保留不变，对方不能同时领取两者。",
                           "relay sync {pid}"),
    "handoff_withdrawn": ("info", "重新签发的交接 {rid} 已作废：对方先领取了原交接 {orig}。", "relay sync {pid}"),
    "send_interrupted": ("warning", "项目 {pid} 发往 {to} 的交棒 {rid} 在发布完成前中断；重新运行会沿用同一交接编号，"
                                    "不会改写已发布的对象。", "relay send {pid} --to {to}"),
    "reissue_interrupted": ("warning", "重新签发的交接 {rid}（取代 {orig}）尚未发布完成。",
                            "relay send {pid} --to {to} --reissue"),
    "send_needs_verification": ("error", "旧版迁移的发送记录 {rid} 无法确认是否已发布。", "relay recover {rid} --rollback"),
    # ---- incoming handoffs
    "receipt_upload_pending": ("warning", "交接 {rid} 已在本机落地，但签收回执尚未上传，发送方仍显示在途。",
                               "relay receive {rid}"),
    "applied_receive_pending": ("warning", "旧版迁移的接棒 {rid} 尚未完成。", "relay receive {rid}"),
    "incoming_sealed_to_retired_key": (
        "error", "待领取的交接 {rid} 是发给本机换钥前公钥的，本机无法领取；需要发送方 {from} 在它的设备上运行下面的命令"
                 "为本机新公钥重新签发（原交接包保持不变）。", "relay send {pid} --to {me} --reissue"),
    "incoming_signed_by_retired_key": (
        "error", "待领取的交接 {rid} 由发送方 {from} 换钥前的旧密钥签名，无法验证；需要发送方在它的设备上运行下面的命令"
                 "用新密钥重新签发（原交接包保持不变）。", "relay send {pid} --to {me} --reissue"),
    # ---- recovery transactions
    "recovery_corrupted": ("error", "交接事务 {rid} 的恢复记录已损坏（{detail}），不能自动继续、签收或回滚。"
                                    "先备份整个事务目录，再从备份恢复 meta.json 或 journal.json；确需放弃时人工处理，不要直接删除。",
                           'cp -R "{path}" "{path}.backup"'),
    "rollback_in_progress": ("error", "交接 {rid} 的回滚尚未完成，完成前不能签收。", "relay recover {rid} --rollback"),
    "receive_interrupted_after_commit": ("warning", "交接 {rid} 的工作区已落地但接收尚未完成（阶段 {phase}），"
                                                    "需要继续完成并补传签收回执。", "relay receive {rid}"),
    "receive_interrupted": ("warning", "交接 {rid} 在应用工作区时中断（阶段 {phase}）；可以继续完成，也可以回滚。",
                            "relay receive {rid}"),
    "receive_conflict": ("error", "交接 {rid} 中断后目标目录又被修改（{detail}），不能自动继续或回滚。",
                         "relay recover {rid} --status"),
    # ---- sync
    "sync_never_run": ("info", "本机还没有同步记录。", "relay sync"),
    "sync_stale": ("warning", "上一次持续同步没有正常结束（最后心跳 {at}）。", "relay sync"),
    "sync_partial": ("warning", "最近一次同步有暂时性失败，会自动重试（{detail}）。", "relay sync"),
    "sync_problem": ("error", "{detail}", "relay sync"),
    "awaiting_redelivery": ("info", "本机缺少 {count} 个快照的可用缓存，已在缓存清单中请求对端重新投递。", "relay sync"),
    "version_fork": ("warning", "项目 {pid} 有未解决的版本分叉（本机 {local}，传入 {incoming}）；两侧的快照缓存都会保留。",
                     "relay status {pid}"),
    "lineage_unproven": ("warning", "项目 {pid} 无法证明本机版本 {local} 与传入版本 {incoming} 的先后关系：缺少可验证的父版本 "
                                    "{missing}。这是证据不足，不是版本分叉；两侧的快照缓存都会保留，补齐签名记录后同步会自动复核。",
                         "relay sync {pid}"),
    # ---- local directories
    "local_dir_symlink": ("error", "本机目录 {path} 是符号链接（指向 {target}）。relay-desk 不经过符号链接写入缓存或归档，"
                                   "相关投递已暂停；这是本机目录问题，不是数据完整性失败。改回普通目录后，下次同步会直接用"
                                   "云端原投递重新处理，不需要对端重新投递。", 'rm "{path}" && mv "{target}" "{path}"'),
    "local_retry_pending": ("info", "{count} 个投递曾因本机目录是符号链接而暂停；目录已是普通目录，下次同步会直接用云端原投递"
                                    "重新处理，不需要对端重新投递。", "relay sync"),
    # ---- ledger
    "project_id_invalid": ("warning", "项目 ID {pid} 不符合当前规则（{detail}）。台账照常读取，但这个项目不能发布、交棒或接收；"
                                      "请用合法的 ID 重新登记（之后可在网页中删除旧条目）。",
                           'relay add "{name}" --path "{dir}" --id <新项目ID>'),
    # ---- cache
    "delivery_state_missing": ("warning", "本机还没有投递状态记录（deliveries.json），无法判断哪些缓存仍需补发；"
                                          "先同步一次再清理。", "relay sync"),
    "prune_plan_changed": ("warning", "清理计划在预览后发生了变化，未删除任何内容。", "relay cache prune --dry-run"),
    "prune_interrupted": ("warning", "上一次缓存清理被中断，留下 {count} 个已判定删除、尚未删完的目录；"
                                     "它们不影响接棒、恢复和补发。", "relay cache prune --apply"),
    "cache_entry_unclear": ("warning", "{count} 个本机缓存条目损坏或状态无法判断；清理时会保留并报告，"
                                       "可用缓存会在同步时从收件箱重新取回。", "relay cache status --json"),
}


class _Fill(dict):
    def __missing__(self, key: str) -> str:
        return _PLACEHOLDER.get(key, "<%s>" % key)


def advice(code: str, **ctx: Any) -> Dict[str, str]:
    """``{code, severity, reason, command}`` for a finding code; unknown codes fall back to ``sync_problem``."""
    sev, reason_t, cmd_t = ADVICE.get(code) or ADVICE["sync_problem"]
    fill = _Fill({k: ("" if v is None else str(v)) for k, v in ctx.items() if v not in (None, "")})
    if code == "identity_missing" and ctx.get("configured"):
        cmd_t = "relay pair"
    if code == "local_dir_symlink" and not ctx.get("target"):
        # A dangling link: removing it is the whole fix (the next sync recreates the directory).
        reason_t = reason_t.replace("（指向 {target}）", "（链接目标已不存在）")
        cmd_t = 'rm "{path}"'
    reason = reason_t.format_map(fill).replace("（）", "")
    command = cmd_t.format_map(fill)
    if ctx.get("severity") in _RANK:
        sev = str(ctx["severity"])
    return {"code": code, "severity": sev, "reason": reason, "command": command}


_CTX_FIELDS = (("projectId", "pid"), ("relayId", "rid"), ("deviceId", "did"), ("path", "path"),
               ("newRelayId", "new"), ("originalRelayId", "orig"), ("snapshotId", "snap"), ("phase", "phase"))


def finding(code: str, **ctx: Any) -> Dict[str, Any]:
    """A finding dict: advice plus the context fields scripts can key on."""
    out: Dict[str, Any] = advice(code, **ctx)
    for field, key in _CTX_FIELDS:
        if ctx.get(key):
            out[field] = str(ctx[key])
    if ctx.get("source"):
        out["source"] = str(ctx["source"])
    return out


def worst_severity(findings: List[Dict[str, Any]]) -> str:
    worst = ""
    for f in findings:
        if not worst or _RANK.get(f.get("severity"), 0) > _RANK.get(worst, -1):
            worst = str(f.get("severity") or "info")
    return worst


def exit_code_for(findings: List[Dict[str, Any]]) -> int:
    """0 nothing above info, 3 warnings only, 4 at least one error (needs the user)."""
    worst = worst_severity(findings)
    return 4 if worst == "error" else 3 if worst == "warning" else 0


# ---------------------------------------------------------------- read-only state access


def _engine():
    from . import relay_engine
    return relay_engine


def _crypto():
    from . import crypto
    return crypto


def _detail(exc: BaseException) -> str:
    msg = getattr(exc, "message", None) or str(exc)
    return str(msg).strip().splitlines()[0][:200] if msg else exc.__class__.__name__


class _State(object):
    """Everything doctor reads, loaded once and strictly; problems become findings."""

    def __init__(self, ledger_path: Any = None, local: Optional[Dict[str, Any]] = None, local_home: Any = None) -> None:
        self.findings: List[Dict[str, Any]] = []
        E = _engine()
        self.base: Optional[Path] = None
        self.led_path: Optional[Path] = None
        self.local: Dict[str, Any] = {}
        try:
            self.base, self.led_path, self.local = E._resolve_ctx(ledger_path, local, local_home)
        except V.LocalStateCorruptError as e:
            self.base = Path(local_home) if local_home is not None else L.local_dir()
            self.add("config_corrupted", path=getattr(e, "path", "") or str(self.base / "config.json"), detail=_detail(e))
        base = self.base
        self.identity: Optional[Dict[str, Any]] = None
        self.identity_ok = False
        try:
            self.identity = _crypto().load_identity(base)
            self.identity_ok = self.identity is not None
        except Exception as e:  # IdentityCorruptError
            self.add("identity_corrupted", path=getattr(e, "path", "") or str(base / "identity.json"), detail=_detail(e))
            self.identity = None
        self.cloud: Optional[Dict[str, Any]] = None
        self.cloud_ok = True
        try:
            self.cloud = E.load_cloud_config(base)
        except V.LocalStateCorruptError as e:
            self.cloud_ok = False
            self.add("cloud_config_corrupted", path=getattr(e, "path", "") or str(base / "cloud.json"), detail=_detail(e))
        self.state: Dict[str, Any] = {}
        self.state_ok = True
        try:
            self.state = E.load_relay_state(base) or {}
        except V.LocalStateCorruptError as e:
            self.state_ok = False
            self.add("relay_state_corrupted", path=getattr(e, "path", "") or str(base / "relay_state.json"),
                     detail=_detail(e))
        self.book: Optional[Dict[str, Any]] = None
        self.book_ok = True
        try:
            self.book = DS.load(base)
        except DS.DeliveryStateCorruptedError as e:
            self.book_ok = False
            self.add("delivery_state_corrupted", path=getattr(e, "path", "") or str(DS.path_for(base)), detail=_detail(e))
        self.ledger: Optional[Dict[str, Any]] = None
        if self.led_path is not None:
            try:
                self.ledger = L.load(self.led_path)
            except L.LedgerError as e:
                self.add("ledger_unreadable", path=str(self.led_path), detail=_detail(e))
        self.here = ""
        if self.ledger is not None:
            try:
                self.here = L.here(self.ledger, self.local) or ""
            except Exception:
                self.here = ""

    def add(self, code: str, **ctx: Any) -> Dict[str, Any]:
        f = finding(code, **ctx)
        self.findings.append(f)
        return f

    # ---- helpers
    @property
    def configured(self) -> bool:
        return bool(self.cloud) and _engine()._is_cloud_configured(self.cloud)

    def peers(self) -> Dict[str, Dict[str, Any]]:
        peers = (self.cloud or {}).get("peers")
        return peers if isinstance(peers, dict) else {}

    def peer_label(self, did: str) -> str:
        p = self.peers().get(did) or {}
        return str(p.get("alias") or p.get("name") or did)

    def projects(self) -> List[Dict[str, Any]]:
        return [p for p in ((self.ledger or {}).get("projects") or []) if isinstance(p, dict)]

    def device_name(self, alias: str) -> str:
        for d in (((self.ledger or {}).get("config") or {}).get("devices") or []):
            if isinstance(d, dict) and d.get("id") == alias:
                return str(d.get("name") or alias)
        return alias


def _project_filter(st: _State, project_key: Optional[str], cwd: Optional[str]) -> Optional[str]:
    if not project_key or st.ledger is None:
        return None
    proj = L.find_project(st.ledger, project_key, cwd or os.getcwd())  # LookupError -> caller
    return str(proj["id"])


# ---------------------------------------------------------------- checks


def _check_pairing(st: _State) -> None:
    if not st.cloud_ok:
        return
    if not st.configured:
        st.add("cloud_not_configured")  # manual relay needs no device identity
        return
    if st.identity is None and not any(f["code"] == "identity_corrupted" for f in st.findings):
        st.add("identity_missing", configured=True)
    try:
        if not _crypto().has_crypto():
            st.add("crypto_unavailable")
    except Exception:
        pass
    paired = 0
    for did, p in sorted(st.peers().items()):
        if not isinstance(p, dict):
            continue
        status = p.get("status")
        if status == "paired" and p.get("paired"):
            paired += 1
        elif status == "key_changed":
            st.add("peer_key_changed", did=did, peer=st.peer_label(did))
        elif status in ("discovered", "pending"):
            st.add("peer_pending_pairing", did=did, peer=st.peer_label(did))
    if paired == 0:
        st.add("no_paired_peers")


def _verify_own_signature(st: _State, raw: Any) -> str:
    """``current`` / ``previous`` / ``none`` / ``unknown`` (no crypto or no identity)."""
    if not isinstance(raw, dict) or not st.identity:
        return "unknown"
    try:
        c = _crypto()
        if not c.has_crypto():
            return "unknown"
        did = st.identity["deviceId"]
        try:
            c.verify_json(raw, st.identity["verifyPublicKey"], expected_device_id=did)
            return "current"
        except c.CryptoError:
            pass
        if _engine()._verify_with_keys(raw, _engine()._previous_keys(st.identity), did) is not None:
            return "previous"
        return "none"
    except Exception:
        return "unknown"


def _check_outgoing(st: _State, only: Optional[str]) -> None:
    rs = st.state
    peers = st.peers()
    for pid, pend in sorted((rs.get("pendingSends") or {}).items()):
        if only and pid != only or not isinstance(pend, dict) or not pend.get("relayId"):
            continue
        rid = str(pend["relayId"])
        to = str(pend.get("toAlias") or pend.get("toDeviceId") or "")
        if pend.get("verificationRequired") or pend.get("status") == "needs_verification":
            st.add("send_needs_verification", rid=rid, pid=pid)
        elif pend.get("reissueOf"):
            st.add("reissue_interrupted", rid=rid, pid=pid, to=to, orig=pend.get("reissueOf"))
        else:
            st.add("send_interrupted", rid=rid, pid=pid, to=to)
    for rid, info in sorted((rs.get("sentRelays") or {}).items()):
        if not isinstance(info, dict) or info.get("acked"):
            continue
        pid = str(info.get("projectId") or "")
        if only and pid != only:
            continue
        did = str(info.get("toDeviceId") or "")
        to = str(info.get("toAlias") or st.peer_label(did) or did)
        if info.get("withdrawn"):
            w = info.get("withdrawn") if isinstance(info.get("withdrawn"), dict) else {}
            st.add("handoff_withdrawn", rid=rid, pid=pid, orig=w.get("by"))
            continue
        if info.get("supersededBy"):
            st.add("handoff_superseded", rid=rid, pid=pid, new=info.get("supersededBy"))
            continue
        peer = peers.get(did) if isinstance(peers.get(did), dict) else {}
        proto = int(info.get("protocolVersion") or 2)
        if peer.get("status") == "key_changed":
            st.add("handoff_target_key_changed", rid=rid, pid=pid, to=to, did=did)
            continue
        if proto >= 3:
            bound = DS.norm_fp(info.get("recipientKeyFingerprint"))
            current = DS.norm_fp(peer.get("keyFingerprint"))
            if bound and current and bound != current:
                st.add("handoff_recipient_key_changed", rid=rid, pid=pid, to=to)
                continue
            if _verify_own_signature(st, info.get("signedReady")) == "previous":
                st.add("handoff_sender_key_rotated", rid=rid, pid=pid, to=to)
                continue
        st.add("handoff_in_flight", rid=rid, pid=pid, to=to)


def _check_incoming(st: _State, only: Optional[str]) -> None:
    rs = st.state
    for rid, info in sorted((rs.get("receivedRelays") or {}).items()):
        if not isinstance(info, dict) or info.get("receiptUploaded"):
            continue
        pid = str(info.get("projectId") or "")
        if only and pid != only:
            continue
        st.add("receipt_upload_pending", rid=rid, pid=pid)
    for rid, info in sorted((rs.get("appliedReceives") or {}).items()):
        if not isinstance(info, dict):
            continue
        pid = str(info.get("projectId") or "")
        if only and pid != only:
            continue
        st.add("applied_receive_pending", rid=rid, pid=pid)
    # Unclaimed handoffs nobody can open any more (recorded by sync, Phase C); a re-issue resolves them.
    received = rs.get("receivedRelays") or {}
    superseded = {str(v.get("supersedesRelayId")) for v in received.values()
                  if isinstance(v, dict) and v.get("supersedesRelayId")}
    for did, ent in sorted(((st.book or {}).get("inbound") or {}).items()):
        if not isinstance(ent, dict) or ent.get("purpose") != "handoff" or ent.get("state") != "obsolete":
            continue
        if ent.get("reason") == "superseded" or did in superseded or did in received:
            continue
        pid = str(ent.get("projectId") or "")
        if only and pid != only:
            continue
        sender = str(ent.get("senderDeviceId") or "")
        code = ("incoming_signed_by_retired_key" if ent.get("reason") == "sender_key_retired"
                else "incoming_sealed_to_retired_key")
        st.add(code, rid=did, pid=pid, me=st.here or (st.identity or {}).get("deviceId"),
               **{"from": st.peer_label(sender) if sender else ""})


def _check_transactions(st: _State, only: Optional[str]) -> None:
    from . import workspace_tx as W
    if st.base is None:
        return
    try:
        tx_ids = W.list_transactions(st.base)
    except Exception:
        return
    for rid in tx_ids:
        tx_path = str(Path(st.base) / "transactions" / rid)
        try:
            info = W.inspect_transaction(st.base, rid)
        except Exception as e:
            st.add("recovery_corrupted", rid=rid, path=tx_path, detail=_detail(e))
            continue
        state = info.get("state")
        if state in ("completed", "rolled_back", "none"):
            continue
        pid = str(info.get("projectId") or "")
        if only and pid and pid != only:
            continue
        if state == "corrupted":
            st.add("recovery_corrupted", rid=rid, pid=pid, path=tx_path,
                   detail="；".join(str(r) for r in (info.get("reasons") or [])[:2]))
            continue
        try:
            W.load_transaction_meta(st.base, rid)
        except Exception as e:
            st.add("recovery_corrupted", rid=rid, pid=pid, path=tx_path, detail=_detail(e))
            continue
        phase = str(info.get("phase") or "")
        if state == "rolling_back" or phase == W.PHASE_ROLLING_BACK:
            st.add("rollback_in_progress", rid=rid, pid=pid, phase=phase)
        elif state == "conflict":
            st.add("receive_conflict", rid=rid, pid=pid, phase=phase,
                   detail="；".join(str(r) for r in (info.get("reasons") or [])[:2]))
        elif state == "workspace_committed":
            st.add("receive_interrupted_after_commit", rid=rid, pid=pid, phase=phase)
        else:
            st.add("receive_interrupted", rid=rid, pid=pid, phase=phase)


_COMMAND_PREFIXES = ("relay ", "rclone ", "python3 ", "mv ", "cp ")
# Sync problems already reported by a fresher local check (same situation, same advice).
_SYNC_CODES_COVERED = frozenset((
    "peer_key_changed", "key_changed", "delivery_state_corrupted", "local_state_corrupted", "recovery_corrupted",
    "sealed_to_retired_key", "handoff_signed_by_retired_key", "handoff_recipient_key_changed",
    "handoff_sender_key_rotated", "not_configured", "crypto_unavailable", "identity_missing", "need_device",
    "local_dir_symlink",
))


def _command_in(action: str) -> str:
    """The runnable command inside a sync action hint (``说明：命令``), '' when it holds none."""
    for part in reversed([action] + action.split("：")):
        part = part.strip()
        if part.startswith(_COMMAND_PREFIXES):
            return part
    return ""


def _check_sync(st: _State) -> Dict[str, Any]:
    if st.base is None:
        return {}
    data = SR.read_status_file(Path(st.base) / "sync_status.json", brief=True)
    if not data:
        if st.configured:
            st.add("sync_never_run")
        return {}
    if data.get("running") and data.get("stale"):
        st.add("sync_stale", at=data.get("heartbeatAt"))
    status = data.get("status")
    for prob in data.get("unresolved") or []:
        if not isinstance(prob, dict):
            continue
        cls = prob.get("errorClass")
        code = str(prob.get("code") or "")
        if cls in (SR.NEEDS_ACTION, SR.INTEGRITY):
            if code in _SYNC_CODES_COVERED:
                continue  # covered by a fresher local check (same situation, same advice)
            f = finding("sync_problem", detail=prob.get("message") or code, source="sync")
            f["syncCode"] = code
            cmd = _command_in(str(prob.get("action") or ""))
            if cmd:
                f["command"] = cmd
            st.findings.append(f)
    if status == SR.PARTIAL:
        transient = [str(p.get("code") or "") for p in data.get("unresolved") or []
                     if isinstance(p, dict) and p.get("errorClass") in (SR.TRANSIENT, SR.LOCAL_BUSY)]
        st.add("sync_partial", detail="、".join(sorted(set(transient))[:4]) or "暂时性失败")
    return data


def _check_cache(st: _State, only: Optional[str]) -> None:
    if st.base is None:
        return
    from . import cache_prune as CP
    try:
        inv = CP.scan_inventory(st.base, hash_payloads=False)
    except Exception:
        return
    leftovers = inv["leftovers"]["count"]
    if leftovers:
        st.add("prune_interrupted", count=leftovers)
    unclear = [e for e in inv["payloadEntries"] if e["state"] in CP.UNCLEAR_STATES
               and (not only or e.get("projectId") in (None, "", only))]
    if unclear:
        st.add("cache_entry_unclear", count=len(unclear))
    if st.book is not None:
        pruned = st.book.get("prunedSnapshots") or {}
        pending = {str(ent.get("snapshotId")) for ent in (st.book.get("inbound") or {}).values()
                   if isinstance(ent, dict) and ent.get("state") in DS.NEEDS_STATES and ent.get("snapshotId")
                   and ent.get("snapshotId") not in pruned and ent.get("purpose") != "handoff"
                   and (not only or ent.get("projectId") in (None, "", only))
                   and not CP.payload_present(st.base, str(ent.get("snapshotId")))}
        if pending:
            st.add("awaiting_redelivery", count=len(pending))


def _check_forks(st: _State, only: Optional[str]) -> None:
    for p in st.projects():
        pid = str(p.get("id") or "")
        if only and pid != only:
            continue
        for c in p.get("conflicts") or []:
            if isinstance(c, dict) and c.get("kind") == "version_fork" and not c.get("resolved"):
                st.add("version_fork", pid=pid, local=c.get("localVersion"), incoming=c.get("incomingVersion"))


def _check_lineage(st: _State, only: Optional[str]) -> None:
    """Fix G: pairs sync could neither prove linear nor prove a fork (insufficient evidence, plan section 2)."""
    pending = st.state.get("lineagePending")
    if not isinstance(pending, dict):
        return
    E = _engine()
    for pid, pairs in sorted(pending.items()):
        if only and pid != only or not isinstance(pairs, dict):
            continue
        graph = (st.state.get("versionGraph") or {}).get(pid)
        graph = graph if isinstance(graph, dict) else {}
        for _key, ent in sorted(pairs.items()):
            if not isinstance(ent, dict):
                continue
            a, b = str(ent.get("localVersion") or ""), str(ent.get("incomingVersion") or "")
            if a and b and (E._is_version_ancestor(graph, a, b) or E._is_version_ancestor(graph, b, a)):
                continue  # proven since (e.g. by receive); the next sync clears the record
            missing = ent.get("missing") if isinstance(ent.get("missing"), dict) else {}
            st.add("lineage_unproven", pid=pid, local=a, incoming=b, missing="、".join(sorted(missing)))


def _check_local_dirs(st: _State) -> List[Dict[str, Any]]:
    """Fix G: relay-desk's own cache/archive directories must be real directories (fresh, read-only check)."""
    if st.base is None:
        return []
    base = Path(st.base)
    links: List[Path] = []
    for rel in (("cache",), ("cache", "snapshots"), ("cache", "lineage"), ("archives",)):
        p = base.joinpath(*rel)
        if os.path.islink(str(p)):
            links.append(p)
    for parent, subs in ((base / "cache" / "snapshots", ()), (base / "archives", ("markdown", "latest"))):
        if os.path.islink(str(parent.parent)) or os.path.islink(str(parent)) or not parent.is_dir():
            continue
        try:
            names = sorted(os.listdir(str(parent)))
        except OSError:
            continue
        for name in names:
            child = parent / name
            if os.path.islink(str(child)):
                links.append(child)
                continue
            for sub in subs:
                if os.path.islink(str(child / sub)):
                    links.append(child / sub)
    found = []
    for link in links[:20]:
        target = os.path.realpath(str(link)) if os.path.exists(str(link)) else ""
        found.append(st.add("local_dir_symlink", path=str(link), target=target))
    if not found and st.book is not None:
        blocked = [ent for ent in (st.book.get("inbound") or {}).values()
                   if isinstance(ent, dict) and ent.get("state") == "blocked_local"]
        if blocked:
            st.add("local_retry_pending", count=len(blocked))
    return found


def _check_project_ids(st: _State, only: Optional[str]) -> None:
    """Fix G: IDs registered before projectId validation applied at registration keep loading; report them."""
    ident = st.identity or {}
    for p in st.projects():
        pid = p.get("id")
        if only and pid != only:
            continue
        try:
            V.validate_project_id(pid)
            continue
        except V.ValidationError as e:
            detail = getattr(e, "message", "") or str(e)
        paths = p.get("paths") if isinstance(p.get("paths"), dict) else {}
        by_did = p.get("pathsByDeviceId") if isinstance(p.get("pathsByDeviceId"), dict) else {}
        here_path = str(paths.get(st.here) or by_did.get(str(ident.get("deviceId") or "")) or "")
        st.add("project_id_invalid", pid=str(pid), name=str(p.get("name") or pid), dir=here_path or None,
               detail=detail.rstrip("。"))


# ---------------------------------------------------------------- per-project network view


def _int(v: Any, default: int = -1) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else default


_STATUS_LABEL = {
    "confirmed": "已确认缓存",
    "awaiting": "已投递，等待确认",
    "resend": "待补发",
    "unknown": "未知",
    "needs_action": "需要操作",
}


def _peer_version_status(st: _State, did: str, peer: Dict[str, Any], vid: str) -> Tuple[str, str]:
    """(status, detail) of one peer for one version, from durable local evidence only."""
    if peer.get("status") == "key_changed":
        return "needs_action", "密钥已更换，待重新核对指纹"
    rs = st.state
    graph_author = ""
    for g in (rs.get("versionGraph") or {}).values():
        node = ((g or {}).get("nodes") or {}).get(vid) if isinstance(g, dict) else None
        if isinstance(node, dict):
            graph_author = str(node.get("authorDeviceId") or "")
            break
    if graph_author == did:
        return "confirmed", "作者"
    for info in (rs.get("sentRelays") or {}).values():
        if isinstance(info, dict) and info.get("toDeviceId") == did and info.get("snapshotId") == vid:
            if info.get("acked"):
                return "confirmed", "已接棒"
    fp = DS.norm_fp(peer.get("keyFingerprint"))
    book = st.book or {}
    ent = (book.get("outbound") or {}).get(DS.outbound_key(did, fp, vid))
    have = (book.get("peerHave") or {}).get(did) or {}
    fresh = isinstance(have, dict) and DS.norm_fp(have.get("keyFingerprint")) == fp
    if isinstance(ent, dict):
        status = ent.get("status")
        if status == "peer_verified":
            return "confirmed", "对方已验证缓存"
        if fresh and vid in (have.get("needs") or []) and _int(have.get("seq")) > _int(ent.get("lastRegenHaveSeq")):
            return "resend", "对方请求补发"
        gen = DS.latest_generation(ent)
        if status == "pending" or (gen is not None and gen.get("state") == "preparing"):
            return "resend", "投递尚未完成"
        if status in ("published", "covered"):
            return "awaiting", "已投递，等待对方缓存清单确认" if status == "published" else "由其他设备投递，等待确认"
    if fresh and vid in (have.get("needs") or []):
        return "resend", "对方请求补发"
    for info in (rs.get("sentRelays") or {}).values():
        if (isinstance(info, dict) and info.get("toDeviceId") == did and info.get("snapshotId") == vid
                and not info.get("acked") and not DS.handoff_superseded(info)):
            return "awaiting", "交接在途"
    return "unknown", "尚无该设备最近的缓存清单记录" if not fresh else "缓存清单中没有该版本的确认记录"


def network_view(st: _State, only: Optional[str] = None) -> List[Dict[str, Any]]:
    """Per project: current editor, confirmed-cache devices, pending re-sends, unknown devices, actions."""
    out: List[Dict[str, Any]] = []
    rs = st.state
    peers = {did: p for did, p in st.peers().items()
             if isinstance(p, dict) and p.get("status") in ("paired", "key_changed")}
    for p in st.projects():
        pid = str(p.get("id") or "")
        if only and pid != only:
            continue
        editor = str(p.get("device") or "")
        ho = p.get("handoff") if isinstance(p.get("handoff"), dict) else None
        graph = (rs.get("versionGraph") or {}).get(pid) or {}
        current = str((rs.get("localCurrentVersion") or {}).get(pid) or "")
        heads = [h for h in (graph.get("heads") or []) if isinstance(h, str)] if isinstance(graph, dict) else []
        versions = [current] if current else sorted(heads)
        devices: List[Dict[str, Any]] = []
        rank = {"needs_action": 4, "resend": 3, "awaiting": 2, "unknown": 1, "confirmed": 0}
        for did, peer in sorted(peers.items(), key=lambda kv: str(kv[1].get("alias") or kv[0])):
            if not versions:
                status, detail = ("needs_action", "密钥已更换，待重新核对指纹") if peer.get("status") == "key_changed" \
                    else ("unknown", "该项目还没有已知版本")
            else:
                worst = None
                for vid in versions:
                    s, d = _peer_version_status(st, did, peer, vid)
                    if worst is None or rank[s] > rank[worst[0]]:
                        worst = (s, d)
                status, detail = worst
            devices.append({"deviceId": did, "alias": str(peer.get("alias") or ""),
                            "name": str(peer.get("name") or peer.get("alias") or did),
                            "status": status, "label": _STATUS_LABEL[status], "detail": detail})
        relevant = [f for f in st.findings if f.get("projectId") == pid and f["severity"] in ("warning", "error")]
        out.append({
            "projectId": pid,
            "projectName": str(p.get("name") or pid),
            "editor": {"alias": editor, "name": st.device_name(editor), "isHere": bool(editor and editor == st.here)},
            "handoff": ({"relayId": str(ho.get("relayId") or ho.get("tag") or ""), "from": str(ho.get("from") or ""),
                         "to": str(ho.get("to") or ""), "toDeviceId": str(ho.get("toDeviceId") or ""),
                         "transport": str(ho.get("transport") or "")} if ho else None),
            "versions": versions,
            "devices": devices,
            "confirmedCache": [d["alias"] or d["deviceId"] for d in devices if d["status"] == "confirmed"],
            "pendingResend": [d["alias"] or d["deviceId"] for d in devices if d["status"] == "resend"],
            "awaiting": [d["alias"] or d["deviceId"] for d in devices if d["status"] == "awaiting"],
            "unknown": [d["alias"] or d["deviceId"] for d in devices if d["status"] == "unknown"],
            # The project's own warnings and errors (a peer needing re-confirmation is a device-level finding).
            "needsAction": [{"code": f["code"], "severity": f["severity"], "reason": f["reason"],
                             "command": f["command"]} for f in relevant],
        })
    return out


# ---------------------------------------------------------------- entry points


def run_doctor(project_key: Optional[str] = None, ledger_path: Any = None, local: Optional[Dict[str, Any]] = None,
               local_home: Any = None, cwd: Optional[str] = None) -> Dict[str, Any]:
    """``relay doctor [PROJECT] --json``: strictly read-only. Raises LookupError for an unknown project only."""
    st = _State(ledger_path, local, local_home)
    only = _project_filter(st, project_key, cwd)
    if st.ledger is not None and not st.here and st.configured:
        st.add("need_device")
    _check_pairing(st)
    if st.state_ok:
        _check_outgoing(st, only)
        _check_incoming(st, only)
    _check_transactions(st, only)
    sync = _check_sync(st)
    _check_local_dirs(st)
    if st.state_ok:
        _check_cache(st, only)
    _check_forks(st, only)
    if st.state_ok:
        _check_lineage(st, only)
    _check_project_ids(st, only)
    projects = network_view(st, only) if st.state_ok else []
    ident = st.identity or {}
    counts = {sev: sum(1 for f in st.findings if f["severity"] == sev) for sev in SEVERITIES}
    worst = worst_severity(st.findings)
    return {
        "schema": SCHEMA,
        "ok": worst != "error",
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "home": str(st.base) if st.base is not None else "",
        "project": only or "",
        "device": {"alias": st.here, "deviceId": str(ident.get("deviceId") or ""),
                   "keyFingerprint": str(ident.get("keyFingerprint") or "")},
        "cloudConfigured": st.configured,
        "status": "needs_action" if worst == "error" else "warning" if worst == "warning" else "ok",
        "counts": counts,
        "findings": sorted(st.findings, key=lambda f: (-_RANK.get(f["severity"], 0), f["code"],
                                                      f.get("projectId", ""), f.get("relayId", ""))),
        "projects": projects,
        "lastSync": sync or None,
    }


def rotation_blockers(local_home: Any) -> List[Dict[str, Any]]:
    """What a key rotation would strand: in-flight sends, pending receipts, unfinished receive transactions.

    Read-only. Corrupt state is a blocker too (nothing is known to be safe).
    """
    st = _State(local_home=local_home)
    out = [f for f in st.findings if f["code"] in ("relay_state_corrupted", "delivery_state_corrupted")]
    if st.state_ok:
        _check_outgoing(st, None)
        _check_incoming(st, None)
    _check_transactions(st, None)
    blocking = ("send_interrupted", "reissue_interrupted", "send_needs_verification", "handoff_in_flight",
                "handoff_recipient_key_changed", "handoff_sender_key_rotated", "handoff_target_key_changed",
                "receipt_upload_pending", "applied_receive_pending", "recovery_corrupted", "rollback_in_progress",
                "receive_interrupted_after_commit", "receive_interrupted", "receive_conflict")
    out.extend(f for f in st.findings if f["code"] in blocking)
    return out
