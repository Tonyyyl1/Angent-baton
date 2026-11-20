# -*- coding: utf-8 -*-
"""Continuous-sync runtime for relay-desk (Phase B).

Pure helpers shared by the sync engine (``relay_engine.SyncSession``), the CLI
and the local HTTP API:

* error classification into four classes (:func:`classify_error`);
* a bounded, thread-safe event log with cumulative counters and an
  unresolved-problem set that reports each problem once (:class:`EventLog`);
* capped exponential backoff with jitter and per-peer health
  (:class:`Backoff`, :class:`HealthTable`), driven by an injectable clock and RNG;
* completion status, exit codes, and the persisted last-run status file.

Standard library only. This module never imports ``relay_engine``; it
recognises errors by class name, ``code``/``category``/``kind`` attributes and,
for storage failures whose class carries no ``kind`` yet, by message markers.
Python 3.9 compatible.
"""
import collections
import errno
import hashlib
import json
import os
import random
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "EVENT_SCHEMA",
    "SUMMARY_SCHEMA",
    "STATUS_SCHEMA",
    "TRANSIENT",
    "NEEDS_ACTION",
    "INTEGRITY",
    "LOCAL_BUSY",
    "ERROR_CLASSES",
    "SUCCESS",
    "PARTIAL",
    "NEEDS_ACTION_STATUS",
    "CANCELLED",
    "COMPLETION_STATUSES",
    "EXIT_CODES",
    "DEFAULT_EVENT_LIMIT",
    "LEGACY_LIST_LIMIT",
    "ClassifiedError",
    "classify_error",
    "ACTIONS",
    "EventLog",
    "Backoff",
    "PeerHealth",
    "HealthTable",
    "Quarantine",
    "RealClock",
    "FakeClock",
    "completion_status",
    "exit_code_for",
    "status_label",
    "write_status_file",
    "read_status_file",
]

EVENT_SCHEMA = "relaydesk.sync/1"
SUMMARY_SCHEMA = "relaydesk.sync.summary/1"
STATUS_SCHEMA = "relaydesk.sync.status/1"

# ---------------------------------------------------------------- error classes

TRANSIENT = "transient_transport"   # retried with capped backoff
NEEDS_ACTION = "needs_user_action"  # configuration or trust: the user must act
INTEGRITY = "integrity"             # data failed verification: never fixed by retrying
LOCAL_BUSY = "local_busy"           # another local operation holds a lock
ERROR_CLASSES = (TRANSIENT, NEEDS_ACTION, INTEGRITY, LOCAL_BUSY)

SUCCESS = "success"
PARTIAL = "partial"
NEEDS_ACTION_STATUS = "needs_action"
CANCELLED = "cancelled"
COMPLETION_STATUSES = (SUCCESS, PARTIAL, NEEDS_ACTION_STATUS, CANCELLED)
EXIT_CODES = {SUCCESS: 0, PARTIAL: 3, NEEDS_ACTION_STATUS: 4, CANCELLED: 130}
STATUS_LABELS = {SUCCESS: "成功", PARTIAL: "部分失败（自动重试中）", NEEDS_ACTION_STATUS: "需要处理", CANCELLED: "已取消"}
CLASS_LABELS = {TRANSIENT: "暂时性传输失败", NEEDS_ACTION: "需要操作", INTEGRITY: "完整性失败", LOCAL_BUSY: "本机忙"}

DEFAULT_EVENT_LIMIT = 200
LEGACY_LIST_LIMIT = 200
UNEXPECTED_ESCALATE_AFTER = 3   # the same unexpected failure this many times in a row -> needs_user_action
STUCK_AFTER = 20                # the same transient failure this many times in a row -> "stuck" flag

# RelayError / Phase A codes that need the user (configuration, trust, damaged local state).
_NEEDS_ACTION_CODES = frozenset((
    "need_device", "not_configured", "peer_key_changed", "key_changed", "untrusted_author",
    "untrusted_sender", "alias_conflict", "device_id_conflict", "name_conflict", "identity_replaced",
    "identity_missing", "local_state_corrupted", "recovery_corrupted", "delivery_state_corrupted",
    "missing_checkpoint_home", "lineage_incomplete", "active_handoff_exists", "wrong_device",
    "missing_path", "unauthorized_receiver", "signed_by_retired_key", "sealed_to_retired_key",
    "storage_auth", "storage_config", "crypto_unavailable", "disk_full", "permission_denied",
    "internal_error", "ledger_error", "pending_send_needs_verification",
    # Phase C: key versions and redelivery
    "repeated_redelivery", "handoff_recipient_key_changed", "handoff_sender_key_rotated",
    "historical_snapshot_not_current", "handoff_signed_by_retired_key",
    # Phase D: explicit re-issue, superseded handoffs, cache prune
    "nothing_to_reissue", "reissue_not_needed", "reissue_not_supported", "reissue_payload_missing",
    "reissue_receipt_exists", "handoff_already_claimed", "handoff_already_superseded", "handoff_superseded",
    "superseded_original_claimed", "prune_plan_changed", "delivery_state_missing",
    # Fix G: a local cache/archive directory is a symlink (the environment, not the data)
    "local_dir_symlink",
))
_INTEGRITY_CODES = frozenset((
    "checkpoint_corrupted", "invalid_identifier", "field_mismatch", "path_escape", "symlink_in_path",
    "invalid_path", "invalid_input", "tamper", "signature_invalid", "digest_mismatch", "package_invalid",
    "remote_json_invalid", "receipt_mismatch", "invalid_key", "unseal_failed", "crypto_error",
))
_PEER_LEVEL_CODES = frozenset(("peer_key_changed", "untrusted_sender"))

_STORAGE_CONFIG_MARKERS = (
    "未找到 rclone", "didn't find section in config file", "failed to create file system",
    "本地存储根目录不能为空", "云端存储配置必须是对象", "不支持的云存储提供方", "远端名称",
    "config file not found",
)
_STORAGE_AUTH_MARKERS = (
    "unauthorized", "401 ", "403 ", "forbidden", "invalidaccesskeyid", "signaturedoesnotmatch",
    "access denied", "accessdenied", "invalid_grant", "token expired", "authentication",
)
_STORAGE_KEY_MARKERS = ("远端对象键", "越界符号链接", "非法符号链接对象")
# Checked first: rclone wraps network failures in config-sounding text, e.g.
# 'Failed to create file system for "x": read metadata failed: Propfind ...: connection reset by peer'.
_STORAGE_TRANSPORT_MARKERS = (
    "connection reset", "connection refused", "broken pipe", "i/o timeout", "timeout", "timed out",
    "no such host", "dial tcp", "network is unreachable", "tls handshake", "eof", "corrupted on transfer",
    "context deadline exceeded", "temporary failure", "too many requests", "503 ", "502 ", "504 ", "超时",
)

ACTIONS = _ACTIONS_TABLE = {
    "not_configured": "relay pair",
    "need_device": "relay init --device <别名>",
    "storage_auth": "检查云盘授权：rclone config reconnect <远端>:",
    "storage_config": "检查云存储配置：relay pair --status",
    "crypto_unavailable": "python3 -m pip install pynacl",
    "local_state_corrupted": "备份并修复报告中的本机状态文件后重新运行（不要直接删除）",
    "recovery_corrupted": "relay recover <交接编号> --status",
    "identity_replaced": "本机身份已被替换，请确认后重新启动同步",
    "identity_missing": "本机身份文件缺失，请恢复 identity.json 备份",
    "disk_full": "释放本机磁盘空间后重试",
    "permission_denied": "检查报告中路径的读写权限",
    "internal_error": "重启同步；若重复出现请保留日志并报告",
    "signed_by_retired_key": "同步投递会自动重新投递；交接需要发送方在它的设备上运行：relay send <项目> --to <本机别名> --reissue",
    "sealed_to_retired_key": "同步投递会自动重新投递；交接需要发送方在它的设备上运行：relay send <项目> --to <本机别名> --reissue",
    "delivery_state_corrupted": "把报告中的 deliveries.json 移到别处后重新运行 relay sync（会从本机发布检查点重建）",
    "repeated_redelivery": "检查对端缓存与云端对象后运行 relay sync；若仍失败请保留日志并报告",
    "handoff_recipient_key_changed": "原交接包保持不变；与目标设备核对新指纹后运行：relay send <项目> --to <目标设备> --reissue",
    "handoff_sender_key_rotated": "原交接包保持不变；对方重新确认本机新指纹后运行：relay send <项目> --to <目标设备> --reissue",
    "historical_snapshot_not_current": "请发送方用当前密钥重新签发该交接：relay send <项目> --to <本机别名> --reissue",
    "handoff_signed_by_retired_key": "原交接包保持不变；需要发送方在它的设备上运行：relay send <项目> --to <本机别名> --reissue",
    "local_dir_symlink": "把报告中的符号链接换回普通目录（或把整个本机目录移到目标位置并设置 RELAY_DESK_HOME）后运行 relay sync；"
                         "云端原投递会被重新处理，不需要对端重新投递",
}


# Retryable codes that stop being treated as transient after repeated consecutive failures.
_ESCALATIONS = {
    "unexpected": (NEEDS_ACTION, "internal_error", ACTIONS["internal_error"]),
    "torn_read": (INTEGRITY, "remote_json_invalid", ""),
}


class ClassifiedError(object):
    """One classified failure: class, stable code, user message and an optional action hint."""

    __slots__ = ("error_class", "code", "message", "action")

    def __init__(self, error_class: str, code: str, message: str, action: str = "") -> None:
        if error_class not in ERROR_CLASSES:
            raise ValueError("unknown error class %r" % (error_class,))
        self.error_class = error_class
        self.code = str(code or "unexpected")
        self.message = str(message or "")
        self.action = str(action or "")

    @property
    def retryable(self) -> bool:
        return self.error_class in (TRANSIENT, LOCAL_BUSY)

    @property
    def peer_level(self) -> bool:
        return self.code in _PEER_LEVEL_CODES

    def to_dict(self) -> Dict[str, Any]:
        out = {"errorClass": self.error_class, "code": self.code, "message": self.message}
        if self.action:
            out["action"] = self.action
        return out

    def __repr__(self) -> str:
        return "ClassifiedError(%s, %s, %r)" % (self.error_class, self.code, self.message[:60])


def _mro_names(exc: BaseException) -> set:
    return {c.__name__ for c in type(exc).__mro__}


def error_message(exc: BaseException) -> str:
    msg = getattr(exc, "message", None)
    if isinstance(msg, str) and msg:
        return msg
    text = str(exc)
    return text if text else type(exc).__name__


def _storage_kind(exc: BaseException, msg: str) -> str:
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str) and kind in ("transport", "auth", "config", "local", "key"):
        return kind
    low = msg.lower()
    if any(m in low for m in _STORAGE_TRANSPORT_MARKERS):
        return "transport"
    if any(m in low for m in _STORAGE_AUTH_MARKERS):
        return "auth"
    if any(m.lower() in low for m in _STORAGE_CONFIG_MARKERS):
        return "config"
    if any(m in msg for m in _STORAGE_KEY_MARKERS):
        return "key"
    if isinstance(exc.__cause__, OSError) or "permission denied" in low or "拒绝访问" in msg:
        cause = exc.__cause__
        if isinstance(cause, OSError) and cause.errno in (errno.EACCES, errno.EPERM):
            return "local"
        if "permission denied" in low or "拒绝访问" in msg:
            return "local"
    return "transport"


def _tamper_code(msg: str) -> str:
    if "签名" in msg and ("验证失败" in msg or "长度无效" in msg):
        return "signature_invalid"
    if "包裹密钥解封失败" in msg:
        return "unseal_failed"
    if "SHA-256" in msg or "摘要" in msg:
        return "digest_mismatch"
    return "tamper"


def classify_error(exc: BaseException, stage: str = "") -> ClassifiedError:
    """Map an exception raised during sync (or a sync-adjacent API call) to a class and code.

    ``stage`` matters only where the same exception means different things:
    a non-tamper ``CryptoError`` while re-reading the local identity
    (``trust_check``) is damaged local state, elsewhere it is an integrity
    failure; ``ValueError`` while parsing a remote object is malformed remote JSON.
    """
    if isinstance(exc, ClassifiedError):  # pragma: no cover - defensive
        return exc
    names = _mro_names(exc)
    msg = error_message(exc)
    raw_code = getattr(exc, "code", None)
    code = raw_code if isinstance(raw_code, str) and raw_code else ""

    if code == "busy":
        return ClassifiedError(LOCAL_BUSY, "busy", msg)
    if "LedgerError" in names and "LocalStateCorruptError" not in names:
        if code == "lock_timeout" or "正被另一个进程占用" in msg:
            return ClassifiedError(LOCAL_BUSY, "lock_timeout", msg)
        return ClassifiedError(NEEDS_ACTION, code or "ledger_error", msg, "检查台账文件后重试")
    if "LocalStateCorruptError" in names or code == "local_state_corrupted":
        path = getattr(exc, "path", "") or ""
        specific = code if code == "delivery_state_corrupted" else "local_state_corrupted"
        action = ACTIONS[specific] + ("：%s" % path if path else "")
        return ClassifiedError(NEEDS_ACTION, specific, msg, action)
    if "CorruptedRecoveryError" in names or code == "recovery_corrupted":
        return ClassifiedError(NEEDS_ACTION, "recovery_corrupted", msg, ACTIONS["recovery_corrupted"])
    if "CryptoUnavailableError" in names:
        return ClassifiedError(NEEDS_ACTION, "crypto_unavailable", msg, ACTIONS["crypto_unavailable"])
    if "NotConfiguredError" in names:
        return ClassifiedError(NEEDS_ACTION, "not_configured", msg, ACTIONS["not_configured"])

    if "ObjectNotFoundError" in names:
        # Listed a moment ago, gone now: eventual consistency or a concurrent delete.
        return ClassifiedError(TRANSIENT, "listing_lag", msg)
    if "StorageError" in names:
        kind = _storage_kind(exc, msg)
        if kind == "auth":
            return ClassifiedError(NEEDS_ACTION, "storage_auth", msg, ACTIONS["storage_auth"])
        if kind == "config":
            return ClassifiedError(NEEDS_ACTION, "storage_config", msg, ACTIONS["storage_config"])
        if kind == "key":
            return ClassifiedError(INTEGRITY, "invalid_key", msg)
        if kind == "local":
            return ClassifiedError(NEEDS_ACTION, "permission_denied", msg, ACTIONS["permission_denied"])
        return ClassifiedError(TRANSIENT, "storage_transport", msg)

    if "TamperError" in names:
        return ClassifiedError(INTEGRITY, code or _tamper_code(msg), msg)
    if "ValidationError" in names or "PackageError" in names:
        category = getattr(exc, "category", "")
        if category == NEEDS_ACTION:
            return ClassifiedError(NEEDS_ACTION, code or "needs_action", msg, ACTIONS.get(code, ""))
        return ClassifiedError(INTEGRITY, code or "package_invalid", msg)
    if "CryptoError" in names:
        if stage == "trust_check":
            return ClassifiedError(NEEDS_ACTION, "local_state_corrupted", msg, ACTIONS["local_state_corrupted"])
        return ClassifiedError(INTEGRITY, "crypto_error", msg)

    if code:
        if code in _NEEDS_ACTION_CODES:
            return ClassifiedError(NEEDS_ACTION, code, msg, ACTIONS.get(code, ""))
        if code in _INTEGRITY_CODES:
            return ClassifiedError(INTEGRITY, code, msg)
    category = getattr(exc, "category", None)
    if category == NEEDS_ACTION:
        return ClassifiedError(NEEDS_ACTION, code or "needs_action", msg)
    if category == INTEGRITY:
        return ClassifiedError(INTEGRITY, code or "integrity", msg)

    if isinstance(exc, OSError):
        if exc.errno == errno.ENOSPC:
            return ClassifiedError(NEEDS_ACTION, "disk_full", msg, ACTIONS["disk_full"])
        if exc.errno in (errno.EACCES, errno.EPERM) or isinstance(exc, PermissionError):
            if os.name == "nt" and stage not in ("trust_check",):
                # Windows reports a briefly held file (antivirus, indexer) as PermissionError.
                return ClassifiedError(TRANSIENT, "local_io_retry", msg)
            return ClassifiedError(NEEDS_ACTION, "permission_denied", msg, ACTIONS["permission_denied"])
        if exc.errno in (errno.EAGAIN, errno.EBUSY, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)):
            return ClassifiedError(TRANSIENT, "local_io_retry", msg)
        return ClassifiedError(TRANSIENT, "unexpected", msg)
    if isinstance(exc, ValueError) and stage == "have_fetch" and (
            "JSONDecodeError" in names or isinstance(exc, UnicodeDecodeError)):
        # The have manifest is the one object peers overwrite in place; on WebDAV a read can
        # overlap the write. Retried; three unreadable reads in a row become an integrity problem.
        return ClassifiedError(TRANSIENT, "torn_read", "对端缓存清单读取不完整（可能正被改写），稍后重试（%s）" % msg)
    if isinstance(exc, ValueError) and stage in ("inbound", "have_fetch", "receipts", "forward"):
        if "JSONDecodeError" in names or isinstance(exc, UnicodeDecodeError):
            return ClassifiedError(INTEGRITY, "remote_json_invalid", "云端记录不是有效的 JSON（%s）" % msg)
    return ClassifiedError(TRANSIENT, "unexpected", msg)


# ---------------------------------------------------------------- clocks


def _iso_ms(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


class RealClock(object):
    """Wall clock for production: monotonic scheduling, UTC timestamps, cancellable sleep."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now_iso(self) -> str:
        return _iso_ms(datetime.now(timezone.utc))

    def sleep(self, seconds: float, cancel: Optional[threading.Event] = None) -> bool:
        """Sleep; True when ``cancel`` was set (before or during the sleep)."""
        seconds = max(0.0, float(seconds))
        if cancel is not None:
            return bool(cancel.wait(seconds))
        time.sleep(seconds)
        return False


class FakeClock(object):
    """Deterministic clock: ``sleep`` advances time instantly and records the requested delays."""

    def __init__(self, start: float = 1000.0, epoch: Optional[datetime] = None) -> None:
        self.t = float(start)
        self._start = float(start)
        self.epoch = epoch or datetime(2026, 11, 18, 0, 0, 0, tzinfo=timezone.utc)
        self.slept = 0.0
        self.sleeps = collections.deque(maxlen=1000)
        self.sleep_count = 0
        self.on_sleep = None  # optional hook(seconds) for tests (e.g. inject KeyboardInterrupt)

    def monotonic(self) -> float:
        return self.t

    def now_iso(self) -> str:
        return _iso_ms(self.epoch + timedelta(seconds=self.t - self._start))

    def advance(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))

    def sleep(self, seconds: float, cancel: Optional[threading.Event] = None) -> bool:
        if cancel is not None and cancel.is_set():
            return True
        if self.on_sleep is not None:
            self.on_sleep(seconds)
        seconds = max(0.0, float(seconds))
        self.t += seconds
        self.slept += seconds
        self.sleep_count += 1
        self.sleeps.append(seconds)
        return bool(cancel is not None and cancel.is_set())


# ---------------------------------------------------------------- backoff & peer health


class Backoff(object):
    """Capped exponential backoff with equal jitter: ``delay(n)`` lies in ``[raw/2, raw]``."""

    def __init__(self, base: float, cap: float, rng: Optional[random.Random] = None, factor: float = 2.0) -> None:
        self.base = max(0.0, float(base))
        self.cap = max(self.base, float(cap))
        self.factor = float(factor)
        self.rng = rng if rng is not None else random.Random()

    def raw(self, attempt: int) -> float:
        n = max(1, int(attempt))
        return min(self.cap, self.base * self.factor ** min(n - 1, 32))

    def delay(self, attempt: int) -> float:
        raw = self.raw(attempt)
        return raw / 2.0 + self.rng.random() * raw / 2.0

    def envelope(self, attempt: int) -> Tuple[float, float]:
        raw = self.raw(attempt)
        return raw / 2.0, raw


HEALTHY = "healthy"
BACKOFF = "backoff"
PARKED = "parked"


class PeerHealth(object):
    __slots__ = ("device_id", "state", "attempt", "next_at", "consecutive_failures", "last_error_class",
                 "last_error_code", "park_code", "parked_at", "last_ok_at")

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.state = HEALTHY
        self.attempt = 0
        self.next_at = 0.0
        self.consecutive_failures = 0
        self.last_error_class = ""
        self.last_error_code = ""
        self.park_code = ""
        self.parked_at = 0.0
        self.last_ok_at = 0.0

    def view(self, now: float) -> Dict[str, Any]:
        out = {
            "state": self.state,
            "attempt": self.attempt,
            "consecutiveFailures": self.consecutive_failures,
            "lastErrorClass": self.last_error_class,
            "lastErrorCode": self.last_error_code,
        }
        if self.state == BACKOFF:
            out["nextAttemptInSec"] = round(max(0.0, self.next_at - now), 3)
        if self.state == PARKED:
            out["parkedCode"] = self.park_code
        return out


class HealthTable(object):
    """Per-peer state machine: healthy -> backoff (transient) -> healthy; parked until trust changes."""

    def __init__(self, backoff: Backoff, clock: Any) -> None:
        self.backoff = backoff
        self.clock = clock
        self._peers: Dict[str, PeerHealth] = {}
        self._lock = threading.Lock()

    def get(self, did: str) -> PeerHealth:
        with self._lock:
            h = self._peers.get(did)
            if h is None:
                h = PeerHealth(did)
                self._peers[did] = h
            return h

    def state(self, did: str) -> str:
        h = self._peers.get(did)
        return h.state if h is not None else HEALTHY

    def is_due(self, did: str) -> bool:
        h = self._peers.get(did)
        if h is None or h.state == HEALTHY:
            return True
        if h.state == PARKED:
            return False
        return self.clock.monotonic() >= h.next_at

    def fail(self, did: str, error_class: str, code: str) -> Tuple[str, str, int, float]:
        """Record a retryable failure; returns (from, to, attempt, delay)."""
        h = self.get(did)
        with self._lock:
            before = h.state
            h.consecutive_failures += 1
            h.last_error_class, h.last_error_code = error_class, code
            if h.state == PARKED:
                return before, h.state, h.attempt, 0.0
            h.attempt += 1
            delay = self.backoff.delay(h.attempt)
            h.next_at = self.clock.monotonic() + delay
            h.state = BACKOFF
            return before, h.state, h.attempt, delay

    def park(self, did: str, error_class: str, code: str) -> Tuple[str, str]:
        h = self.get(did)
        with self._lock:
            before = h.state
            h.consecutive_failures += 1
            h.last_error_class, h.last_error_code = error_class, code
            h.state = PARKED
            h.park_code = code
            h.parked_at = self.clock.monotonic()
            return before, h.state

    def succeed(self, did: str) -> Tuple[str, str]:
        h = self.get(did)
        with self._lock:
            before = h.state
            if h.state == PARKED:
                return before, before
            h.state = HEALTHY
            h.attempt = 0
            h.next_at = 0.0
            h.consecutive_failures = 0
            h.last_ok_at = self.clock.monotonic()
            return before, h.state

    def reset(self, did: str) -> Tuple[str, str]:
        """Trust epoch changed for this peer: forget backoff and parking."""
        with self._lock:
            h = self._peers.pop(did, None)
        return (h.state if h is not None else HEALTHY), HEALTHY

    def reset_all(self) -> List[Tuple[str, str]]:
        with self._lock:
            old = [(did, h.state) for did, h in self._peers.items()]
            self._peers.clear()
        return old

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        now = self.clock.monotonic()
        with self._lock:
            return {did: h.view(now) for did, h in sorted(self._peers.items())}


class Quarantine(object):
    """LRU of objects that failed verification: not retried until their listing tag changes."""

    def __init__(self, limit: int = 1000) -> None:
        self.limit = max(1, int(limit))
        self._items: "collections.OrderedDict[str, Tuple[Any, str, str]]" = collections.OrderedDict()

    def put(self, key: str, tag: Any, problem_key: str, peer: str = "") -> None:
        self._items[key] = (tag, problem_key, peer)
        self._items.move_to_end(key)
        while len(self._items) > self.limit:
            self._items.popitem(last=False)

    def match(self, key: str, tag: Any) -> Optional[str]:
        """The problem key when ``key`` is quarantined with the same tag; drops stale entries."""
        hit = self._items.get(key)
        if hit is None:
            return None
        if hit[0] != tag:
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return hit[1]

    def clear_peer(self, peer: str) -> None:
        for k in [k for k, v in self._items.items() if v[2] == peer]:
            del self._items[k]

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


# ---------------------------------------------------------------- event log


def new_counters() -> Dict[str, Any]:
    return {
        "rounds": 0,
        "roundsSkippedBusy": 0,
        "roundsAborted": 0,
        "receiptsSynced": 0,
        "snapshotsCached": 0,
        "snapshotsRepaired": 0,
        "forwarded": 0,
        "peerVerified": 0,
        "forksDetected": 0,
        "lineageUnproven": 0,
        "havePublished": 0,
        "retriesScheduled": 0,
        "failedOperations": {c: 0 for c in ERROR_CLASSES},
        "problemsOpened": {c: 0 for c in ERROR_CLASSES},
        "problemsResolved": 0,
        "eventsEmitted": 0,
        "eventsNotRetained": 0,
    }


_KIND_COUNTERS = {
    "receipt_synced": "receiptsSynced",
    "snapshot_cached": "snapshotsCached",
    "snapshot_forwarded": "forwarded",
    "peer_verified": "peerVerified",
    "fork_detected": "forksDetected",
    "lineage_unproven": "lineageUnproven",
    "have_published": "havePublished",
    "round_skipped": "roundsSkippedBusy",
}


class EventLog(object):
    """Bounded recent-event ring, cumulative counters and the unresolved-problem set.

    * ``emit`` assigns a sequence number, updates counters, keeps the event in a
      ring of ``limit`` entries when ``retain`` is true, then calls ``on_event``.
    * A problem is emitted only when its key is first seen or its class/code
      changes. Keys not reported or carried during a round are resolved at
      :meth:`end_round`.
    * All state is guarded by one lock so HTTP readers can poll a running session.
    """

    def __init__(self, limit: int = DEFAULT_EVENT_LIMIT, on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                 clock: Any = None, run_id: str = "", unresolved_limit: int = DEFAULT_EVENT_LIMIT,
                 on_sink_closed: Optional[Callable[[], None]] = None) -> None:
        self.limit = max(1, int(limit))
        self.recent: "collections.deque[Dict[str, Any]]" = collections.deque(maxlen=self.limit)
        self.dropped = 0
        self.last_seq = 0
        self.counters = new_counters()
        self.unresolved: "collections.OrderedDict[str, Dict[str, Any]]" = collections.OrderedDict()
        self.unresolved_limit = max(1, int(unresolved_limit))
        self.unresolved_dropped = 0
        self.unresolved_truncated = False
        self.clock = clock or RealClock()
        self.run_id = run_id or new_run_id(self.clock)
        self.on_event = on_event
        self.on_sink_closed = on_sink_closed
        self.sink_error: Optional[BaseException] = None
        self.lock = threading.RLock()
        self.round = 0
        self._seen: set = set()
        self._round_dropped = 0
        self._dropped_max_seq = 0

    # ---- events
    def emit(self, kind: str, retain: bool = True, **fields: Any) -> Dict[str, Any]:
        with self.lock:
            self.last_seq += 1
            ev = {"schema": EVENT_SCHEMA, "runId": self.run_id, "seq": self.last_seq,
                  "at": self.clock.now_iso(), "round": self.round, "kind": kind}
            for k, v in fields.items():
                if v is not None:
                    ev[k] = v
            self.counters["eventsEmitted"] += 1
            name = _KIND_COUNTERS.get(kind)
            if name:
                self.counters[name] += 1
            if retain:
                if len(self.recent) == self.limit:
                    self.dropped += 1
                    self._dropped_max_seq = self.recent[0]["seq"]
                self.recent.append(ev)
            else:
                self.counters["eventsNotRetained"] += 1
        self._deliver(ev)
        return ev

    def _deliver(self, ev: Dict[str, Any]) -> None:
        sink = self.on_event
        if sink is None:
            return
        try:
            sink(ev)
        except BrokenPipeError as e:
            # `relay sync --jsonl | head`: the reader went away; stop streaming and cancel.
            self.on_event = None
            self.sink_error = e
            if self.on_sink_closed is not None:
                self.on_sink_closed()
        except OSError as e:
            if e.errno == errno.EPIPE:
                self.on_event = None
                self.sink_error = e
                if self.on_sink_closed is not None:
                    self.on_sink_closed()
            else:
                raise

    def bump(self, name: str, n: int = 1) -> None:
        with self.lock:
            self.counters[name] += n

    def events_since(self, seq: int) -> Tuple[List[Dict[str, Any]], int]:
        """Retained events newer than ``seq``, and ``gap`` > 0 when retained events after ``seq`` were dropped.

        ``gap`` is an upper bound of the events the caller missed (it also counts
        events that were streamed but never retained); 0 means nothing was lost.
        """
        with self.lock:
            seq = int(seq)
            items = [dict(e) for e in self.recent if e["seq"] > seq]
            if seq < self._dropped_max_seq:
                first = self.recent[0]["seq"] if self.recent else self.last_seq + 1
                gap = max(1, first - seq - 1)
            else:
                gap = 0
            return items, gap

    @property
    def first_seq(self) -> int:
        with self.lock:
            return self.recent[0]["seq"] if self.recent else 0

    # ---- rounds and problems
    def begin_round(self, round_no: int) -> None:
        with self.lock:
            self.round = int(round_no)
            self.counters["rounds"] += 1
            self._seen = set()
            self._round_dropped = 0

    def report_problem(self, key: str, classified: ClassifiedError, peer: str = "",
                       **ctx: Any) -> Tuple[Dict[str, Any], bool]:
        """Record one failed operation; emits ``problem`` only for a new key or a changed class/code.

        Returns ``(problem, opened)``; ``opened`` is true when the event was emitted.
        """
        with self.lock:
            prev = self.unresolved.get(key)
            occurrences = (int(prev.get("occurrences") or 0) + 1) if prev else 1
            cls, code, action = classified.error_class, classified.code, classified.action
            escalated = _ESCALATIONS.get(code)
            if escalated is not None and (occurrences >= UNEXPECTED_ESCALATE_AFTER or (
                    prev is not None and prev.get("code") == escalated[1])):
                cls, code, action = escalated[0], escalated[1], escalated[2]
            self.counters["failedOperations"][classified.error_class] += 1
            problem = {"key": key, "errorClass": cls, "code": code, "message": classified.message[:500]}
            if action:
                problem["action"] = action
            if peer:
                problem["peerDeviceId"] = peer
            for k, v in ctx.items():
                if v not in (None, ""):
                    problem[k] = v
            problem["firstSeenRound"] = prev.get("firstSeenRound", self.round) if prev else self.round
            problem["firstSeenAt"] = prev.get("firstSeenAt") if prev else self.clock.now_iso()
            problem["lastSeenRound"] = self.round
            problem["occurrences"] = occurrences
            if cls == TRANSIENT and occurrences >= STUCK_AFTER:
                problem["stuck"] = True
            changed = prev is None or prev.get("errorClass") != cls or prev.get("code") != code
            self.unresolved[key] = problem
            self.unresolved.move_to_end(key)
            self._seen.add(key)
            while len(self.unresolved) > self.unresolved_limit:
                old_key, _ = self.unresolved.popitem(last=False)
                self._seen.discard(old_key)
                self.unresolved_dropped += 1
                self._round_dropped += 1
            if changed:
                self.counters["problemsOpened"][cls] += 1
            out = dict(problem)
        if changed:
            fields = {k: v for k, v in out.items() if k not in ("firstSeenAt",)}
            self.emit("problem", **fields)
        return out, changed

    def carry(self, key: str) -> bool:
        with self.lock:
            if key in self.unresolved:
                self._seen.add(key)
                return True
            return False

    def carry_peer(self, peer: str) -> None:
        with self.lock:
            for key, p in self.unresolved.items():
                if p.get("peerDeviceId") == peer:
                    self._seen.add(key)

    def carry_prefix(self, prefix: str) -> None:
        with self.lock:
            for key in self.unresolved:
                if key.startswith(prefix):
                    self._seen.add(key)

    def carry_all(self) -> None:
        with self.lock:
            self._seen.update(self.unresolved.keys())

    def end_round(self) -> List[str]:
        """Resolve every unresolved key that was neither reported nor carried this round."""
        with self.lock:
            gone = [k for k in self.unresolved if k not in self._seen]
            resolved = [self.unresolved.pop(k) for k in gone]
            self.counters["problemsResolved"] += len(resolved)
            self.unresolved_truncated = self._round_dropped > 0
        for p in resolved:
            self.emit("resolved", key=p["key"], errorClass=p["errorClass"], code=p["code"],
                      peerDeviceId=p.get("peerDeviceId"))
        return gone

    def unresolved_list(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(p) for p in self.unresolved.values()]

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            counters = json.loads(json.dumps(self.counters))
            recent = [dict(e) for e in self.recent]
            return {
                "counters": counters,
                "recentEvents": recent,
                "recentEventsTruncated": self.dropped > 0,
                "recentEventsDropped": self.dropped,
                "firstRecentSeq": recent[0]["seq"] if recent else 0,
                "lastSeq": self.last_seq,
                "unresolved": [dict(p) for p in self.unresolved.values()],
                "unresolvedTruncated": bool(self.unresolved_truncated),
                "unresolvedDropped": self.unresolved_dropped,
            }


def new_run_id(clock: Any = None) -> str:
    stamp = (clock or RealClock()).now_iso()[:10].replace("-", "")
    return "sr_%s_%s" % (stamp, "%08x" % random.SystemRandom().getrandbits(32))


# ---------------------------------------------------------------- status


def completion_status(unresolved: Iterable[Dict[str, Any]], cancelled: bool = False) -> str:
    if cancelled:
        return CANCELLED
    classes = {str(p.get("errorClass") or "") for p in unresolved}
    if NEEDS_ACTION in classes or INTEGRITY in classes:
        return NEEDS_ACTION_STATUS
    if TRANSIENT in classes or LOCAL_BUSY in classes:
        return PARTIAL
    return SUCCESS


def exit_code_for(status: str) -> int:
    return EXIT_CODES.get(status, 1)


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def class_label(error_class: str) -> str:
    return CLASS_LABELS.get(error_class, error_class)


def summary_digest(summary: Dict[str, Any]) -> str:
    """Stable digest of the parts of a summary worth persisting when they change."""
    counters = dict(summary.get("counters") or {})
    counters.pop("eventsEmitted", None)
    counters.pop("eventsNotRetained", None)
    counters.pop("rounds", None)
    material = {
        "status": summary.get("status"),
        "counters": counters,
        "unresolved": sorted((p.get("key"), p.get("errorClass"), p.get("code")) for p in summary.get("unresolved") or []),
        "peers": {k: (v.get("state"), v.get("lastErrorCode")) for k, v in (summary.get("peers") or {}).items()},
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def write_status_file(path: Any, summary: Dict[str, Any], extra: Optional[Dict[str, Any]] = None,
                      tail: int = 20) -> None:
    """Atomically persist the last-run summary (without the full event ring) plus run metadata."""
    data = {k: v for k, v in summary.items() if k != "recentEvents"}
    data["schema"] = STATUS_SCHEMA
    data["summarySchema"] = summary.get("schema", SUMMARY_SCHEMA)
    data["recentEventsTail"] = list(summary.get("recentEvents") or [])[-tail:]
    if extra:
        data.update(extra)
    target = os.fspath(path)
    parent = os.path.dirname(target) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".sync_status-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_status_file(path: Any, brief: bool = False, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The persisted status, ``None`` if absent or unreadable; ``stale`` when a 'running' run stopped beating."""
    try:
        with open(os.fspath(path), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    stale = False
    if data.get("running"):
        try:
            beat = datetime.strptime(str(data.get("heartbeatAt"))[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            window = 3.0 * max(float(data.get("interval") or 5.0), float(data.get("maxBackoff") or 300.0))
            stale = ((now or datetime.now(timezone.utc)) - beat).total_seconds() > window
        except (TypeError, ValueError):
            stale = True
    data["stale"] = stale
    if not brief:
        return data
    keys = ("status", "ok", "runId", "mode", "cycles", "startedAt", "finishedAt", "running", "heartbeatAt",
            "pid", "stale", "counters")
    out = {k: data.get(k) for k in keys if k in data}
    out["unresolved"] = list(data.get("unresolved") or [])[:20]
    return out
