# -*- coding: utf-8 -*-
"""Per-device delivery state for v3 cache sync (Phase C).

``<home>/deliveries.json`` records, for this device:

* ``outbound``: one entry per (recipient deviceId, recipient key fingerprint,
  snapshot). Dedupe and redelivery are keyed by the recipient's *key*, not just
  its deviceId, so a re-confirmed key rotation gets a fresh delivery ID and
  fresh ciphertext while the original delivery stays untouched. Each entry
  keeps its delivery generations (``preparing`` -> ``published``) and whether
  the recipient has confirmed a verified cache (``peer_verified``).
* ``inbound``: outcomes for received deliveries that need memory beyond
  ``relay_state.processedDeliveries``: ``cached``, ``obsolete`` (signed by a
  retired sender key or sealed to this device's retired key), ``quarantined``
  (integrity failure), ``repair_pending`` (cache lost, delivery gone) and
  ``blocked_local`` (refused only because a local directory is a symlink:
  retried from the same delivery once fixed, never requested from peers).
* ``peerHave``: the last verified ``have`` sequence per peer and key, so a late
  (older) manifest cannot regress anything.
* ``self``: this device's own have sequence and last uploaded digest.

The file is separate from ``relay_state.json`` so that file does not grow with
snapshots x peers, and Phase A's strict loader for it stays untouched. Loading
is strict: a missing file is an empty state; a present but damaged file raises
:class:`DeliveryStateCorruptedError` and nothing is written.

Three delivery states are kept apart (see :func:`delivery_view`):

``published``      our ready/package are published for that recipient key
``peer_verified``  the recipient's signed, current-key have lists the snapshot
``claimed``        a handoff receipt was verified (``relay_state.sentRelays``)

A have never substitutes for a receipt, and a receipt never implies
``peer_verified`` for other snapshots.

Standard library only; never imports ``relay_engine``. Python 3.9 compatible.
"""
import copy
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import ledger as L
from . import validation as V

__all__ = [
    "FILE_NAME",
    "FORMAT",
    "DeliveryStateCorruptedError",
    "DeliveryBook",
    "load",
    "update",
    "norm_fp",
    "fp8",
    "outbound_key",
    "delivery_id_for",
    "legacy_delivery_ids",
    "match_snapshot_delivery",
    "cache_ok",
    "CacheIndex",
    "write_cache_memo",
    "retention_inputs",
    "delivery_view",
    "mark_pruned",
    "unmark_pruned",
    "handoff_superseded",
]

FILE_NAME = "deliveries.json"
FORMAT = 1
MAX_GENERATIONS = 4            # more than this for one (peer, key, snapshot) needs the user
NEEDS_LIMIT = 200              # snapshot IDs a have may request
MAINTENANCE_SEC = 600.0        # re-check published-but-unverified objects at most this often
MEMO_NAME = "verified.json"    # cache digest memo inside cache/snapshots/<vid>/
MEMO_MAX_AGE = timedelta(days=7)
REHASH_BUDGET_BYTES = 64 * 1024 * 1024

OUTBOUND_STATUSES = ("pending", "published", "peer_verified", "covered", "superseded")
GENERATION_STATES = ("preparing", "published", "abandoned")
INBOUND_STATES = ("cached", "obsolete", "quarantined", "repair_pending", "blocked_local")
NEEDS_STATES = ("obsolete", "quarantined", "repair_pending")


class DeliveryStateCorruptedError(V.LocalStateCorruptError):
    """``deliveries.json`` exists but cannot be used; nothing was written."""

    def __init__(self, message: str, path: str = "") -> None:
        V.LocalStateCorruptError.__init__(self, message, path=path, kind="deliveries",
                                          code="delivery_state_corrupted")


# ---------------------------------------------------------------- identifiers


def norm_fp(fp: Any) -> str:
    """Normalized key fingerprint: hex digits only, upper case (``ABCD-EF01`` -> ``ABCDEF01``)."""
    return re.sub(r"[^0-9A-Za-z]", "", str(fp or "")).upper()


def fp8(fp: Any) -> str:
    return norm_fp(fp)[:8].lower()


def outbound_key(recipient_did: str, recipient_fp: Any, snapshot_id: str) -> str:
    return "%s|%s|%s" % (recipient_did, norm_fp(recipient_fp), snapshot_id)


def delivery_id_for(snapshot_id: str, sender_did: str, recipient_fp: Any, gen: int, author_is_self: bool) -> str:
    """``(sync|fwd)_<snapshot>_<sender6>_k<fp8>[_g<n>]``.

    The prefixes keep older v3 engines treating these as cache deliveries (they
    skip ``sync_``/``fwd_`` in the inbox list). The key suffix binds the ID to the
    recipient's key, so a rotation can never reuse an ID published for the old key.
    """
    prefix = "sync" if author_is_self else "fwd"
    base = "%s_%s_%s_k%s" % (prefix, snapshot_id, str(sender_did)[-6:], fp8(recipient_fp))
    did = base if int(gen) <= 1 else "%s_g%d" % (base, int(gen))
    V.validate_delivery_id(did)
    return did


def legacy_delivery_ids(snapshot_id: str, sender_did: str) -> List[str]:
    """Pre-Phase-C IDs this device may have used for the snapshot (not bound to a key)."""
    s6 = str(sender_did)[-6:]
    return ["sync_%s_%s" % (snapshot_id, s6), "fwd_%s_%s" % (snapshot_id, s6)]


_SUFFIX_RE = re.compile(r"^(?P<s6>[A-Za-z0-9]{6})(?:_k(?P<fp8>[0-9a-f]{8}))?(?:_g(?P<gen>[0-9]{1,3}))?$")


def match_snapshot_delivery(delivery_id: str, snapshot_id: str) -> Optional[Dict[str, Any]]:
    """Parse a cache-delivery ID for a known snapshot; ``None`` when it is not one."""
    for prefix in ("sync", "fwd"):
        head = "%s_%s_" % (prefix, snapshot_id)
        if delivery_id.startswith(head):
            m = _SUFFIX_RE.match(delivery_id[len(head):])
            if m:
                return {"prefix": prefix, "sender6": m.group("s6"), "fp8": m.group("fp8") or "",
                        "gen": int(m.group("gen") or 1)}
    return None


# ---------------------------------------------------------------- file I/O


def path_for(home: Any) -> Path:
    return Path(home) / FILE_NAME


def empty_state() -> Dict[str, Any]:
    return {"format": FORMAT, "createdAt": L.now_iso(), "migration": {}, "outbound": {}, "inbound": {},
            "peerHave": {}, "self": {}, "prunedSnapshots": {}}


def problems(raw: Any) -> List[str]:
    if not isinstance(raw, dict):
        return ["顶层不是对象"]
    out: List[str] = []
    fmt = raw.get("format")
    if not isinstance(fmt, int) or isinstance(fmt, bool):
        out.append("format 不是整数")
    elif fmt > FORMAT:
        out.append("格式版本 %d 由更新的 relay-desk 写入，本版本只认识 %d" % (fmt, FORMAT))
    for key in ("outbound", "inbound", "peerHave", "self", "migration", "prunedSnapshots"):
        if raw.get(key) is not None and not isinstance(raw.get(key), dict):
            out.append("%s 不是对象" % key)
    for section in ("outbound", "inbound", "peerHave"):
        val = raw.get(section)
        if isinstance(val, dict):
            bad = [k for k, v in val.items() if not isinstance(v, dict)]
            if bad:
                out.append("%s 中有非对象条目（%s）" % (section, bad[0]))
    return out


def load(home: Any) -> Optional[Dict[str, Any]]:
    """Strict, read-only: ``None`` when never created; DeliveryStateCorruptedError when damaged."""
    path = path_for(home)
    try:
        raw = V.read_local_json_object(path, "deliveries", "投递状态 deliveries.json")
    except V.LocalStateCorruptError as e:
        raise DeliveryStateCorruptedError(e.message + " 可把该文件移到别处后重新运行 relay sync，"
                                          "会从本机发布检查点重建。", path=str(path))
    if raw is None:
        return None
    found = problems(raw)
    if found:
        raise DeliveryStateCorruptedError(
            "本机投递状态 deliveries.json 结构已损坏（%s）：%s。为避免覆盖已有记录，操作已停止；"
            "可把该文件移到别处后重新运行 relay sync，会从本机发布检查点重建。" % ("；".join(found[:5]), path),
            path=str(path))
    for key in ("outbound", "inbound", "peerHave", "self", "migration", "prunedSnapshots"):
        raw.setdefault(key, {})
    return raw


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + "-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        last: Optional[BaseException] = None
        for _ in range(20):
            try:
                os.replace(tmp, str(path))
                return
            except PermissionError as e:  # Windows: a reader holds the file briefly
                last = e
                time.sleep(0.05)
        if last is not None:
            raise last
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update(home: Any, fn: Callable[[Dict[str, Any]], Any], init: Optional[Callable[[Dict[str, Any]], None]] = None
           ) -> Dict[str, Any]:
    """Read-modify-write under the file's own lock. ``init`` runs once when the file is first created."""
    path = path_for(home)
    V.ensure_contained(home, path, field="deliveries")
    with L._Lock(path):
        st = load(home)
        if st is None:
            st = empty_state()
            if init is not None:
                init(st)
        before = json.dumps(st, sort_keys=True)
        fn(st)
        if json.dumps(st, sort_keys=True) != before or not path.exists():
            _atomic_write(path, st)
        return st


class DeliveryBook(object):
    """A device's delivery state for one operation: reads from memory, every write goes to disk first."""

    def __init__(self, home: Any, init: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        self.home = Path(home)
        self._init = init
        loaded = load(self.home)
        if loaded is None:
            loaded = update(self.home, lambda st: None, init=init)
        self.state = loaded

    def mutate(self, fn: Callable[[Dict[str, Any]], Any]) -> Any:
        box: Dict[str, Any] = {}

        def apply(st: Dict[str, Any]) -> None:
            box["r"] = fn(st)

        self.state = update(self.home, apply, init=self._init)
        return box.get("r")

    # ------------------------------------------------------------ outbound
    def outbound(self, did: str, fp: Any, snap: str) -> Optional[Dict[str, Any]]:
        return self.state["outbound"].get(outbound_key(did, fp, snap))

    def outbound_for_peer(self, did: str) -> List[Tuple[str, Dict[str, Any]]]:
        prefix = did + "|"
        return [(k, v) for k, v in self.state["outbound"].items() if k.startswith(prefix)]

    def verified_under_other_key(self, did: str, fp: Any, snap: str) -> bool:
        cur = norm_fp(fp)
        for _k, e in self.outbound_for_peer(did):
            if e.get("snapshotId") == snap and norm_fp(e.get("recipientKeyFingerprint")) != cur \
                    and isinstance(e.get("peerVerified"), dict):
                return True
        return False

    def supersede_other_keys(self, did: str, fp: Any) -> int:
        cur = norm_fp(fp)
        stale = [k for k, e in self.outbound_for_peer(did)
                 if norm_fp(e.get("recipientKeyFingerprint")) != cur and e.get("status") != "superseded"]
        if not stale:
            return 0

        def fn(st: Dict[str, Any]) -> None:
            now = L.now_iso()
            for k in stale:
                e = st["outbound"].get(k)
                if isinstance(e, dict) and e.get("status") != "superseded":
                    e["supersededFrom"] = e.get("status")
                    e["status"] = "superseded"
                    e["supersededAt"] = now

        self.mutate(fn)
        return len(stale)

    # ------------------------------------------------------------ inbound
    def inbound(self, delivery_id: str) -> Optional[Dict[str, Any]]:
        return self.state["inbound"].get(delivery_id)

    def set_inbound(self, delivery_id: str, **fields: Any) -> None:
        def fn(st: Dict[str, Any]) -> None:
            ent = st["inbound"].setdefault(delivery_id, {})
            ent.update({k: v for k, v in fields.items() if v is not None})
            ent["at"] = L.now_iso()

        self.mutate(fn)

    def needs(self, is_ok: Callable[[str], bool]) -> List[str]:
        """Snapshots a fresh delivery is requested for: an obsolete/quarantined/lost one and no good cache."""
        pruned = self.state.get("prunedSnapshots") or {}
        out: Set[str] = set()
        for ent in self.state["inbound"].values():
            sid = ent.get("snapshotId")
            if ent.get("state") in NEEDS_STATES and isinstance(sid, str) and sid and sid not in pruned:
                if sid not in out and not is_ok(sid):
                    out.add(sid)
        return sorted(out)[:NEEDS_LIMIT]

    # ------------------------------------------------------------ peer have
    def peer_have(self, did: str) -> Dict[str, Any]:
        return self.state["peerHave"].get(did) or {}

    # ------------------------------------------------------------ self
    def self_state(self) -> Dict[str, Any]:
        return self.state.get("self") or {}


# ---------------------------------------------------------------- outbound entry helpers (pure)


def new_outbound_entry(did: str, fp: Any, snap: str, project_id: str, author_did: str, sender_fp: Any) -> Dict[str, Any]:
    return {
        "recipientDeviceId": did,
        "recipientKeyFingerprint": norm_fp(fp),
        "snapshotId": snap,
        "projectId": project_id,
        "authorDeviceId": author_did,
        "senderKeyFingerprint": norm_fp(sender_fp),
        "status": "pending",
        "generations": [],
        "lastRegenHaveSeq": -1,
        "createdAt": L.now_iso(),
    }


def latest_generation(entry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    gens = (entry or {}).get("generations") or []
    gens = [g for g in gens if isinstance(g, dict)]
    return gens[-1] if gens else None


def next_generation_number(entry: Optional[Dict[str, Any]]) -> int:
    gens = [g for g in ((entry or {}).get("generations") or []) if isinstance(g, dict)]
    return (max([int(g.get("gen") or 0) for g in gens]) + 1) if gens else 1


# ---------------------------------------------------------------- local cache verification (memo)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(str(path), "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def parse_iso(text: Any) -> Optional[datetime]:
    """UTC datetime from ``L.now_iso()`` text (second precision); ``None`` when absent or unreadable."""
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


_parse_iso = parse_iso


def wall_now() -> datetime:
    """Wall clock for durable schedules (maintenance, memo age); tests patch this."""
    return datetime.now(timezone.utc)


def write_cache_memo(cdir: Path, sha256: str, size: int, mtime_ns: int) -> None:
    _atomic_write(Path(cdir) / MEMO_NAME, {"sha256": sha256, "size": int(size), "mtimeNs": int(mtime_ns),
                                           "verifiedAt": L.now_iso()})


class HashBudget(object):
    """Bytes a round may spend re-hashing cache entries only because their memo is old (bit-rot scan)."""

    def __init__(self, limit: int = REHASH_BUDGET_BYTES) -> None:
        self.left = int(limit)

    def take(self, n: int) -> bool:
        if self.left <= 0:
            return False
        self.left -= int(n)
        return True


def _hash_file(path: Path) -> Tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with open(str(path), "rb") as fh:
        while True:
            buf = fh.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
            total += len(buf)
    return h.hexdigest(), total


def cache_ok(cdir: Path, record_digest: Optional[Callable[[Dict[str, Any]], str]] = None,
             expected_record_sha: str = "", budget: Optional[HashBudget] = None) -> bool:
    """True when ``cdir`` holds a complete snapshot whose payload digest matches its signed record.

    Checks that ``payload.tar.gz`` and ``snapshot.json`` exist, that the record
    names this directory's versionId, optionally that the record's digest equals
    the one the version graph attested, and that the payload hashes to the
    record's ``contentSha256``. The hash is memoized in ``verified.json``
    (size, mtime_ns, sha256): it is recomputed when size or mtime differ, when
    no memo exists, and, within ``budget``, when the memo is older than 7 days.
    """
    return _cache_check(cdir, record_digest, expected_record_sha, budget)[0]


def _cache_check(cdir: Path, record_digest: Optional[Callable[[Dict[str, Any]], str]] = None,
                 expected_record_sha: str = "", budget: Optional[HashBudget] = None
                 ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[datetime]]:
    """:func:`cache_ok` plus, on success, the parsed signed record and the time of the memo that vouched.

    The memo time is ``None`` when the payload was hashed during this call (the
    memo was just rewritten) or when an expired memo was trusted only because no
    re-hash budget was left; :class:`CacheIndex` never reuses such a result.
    """
    cdir = Path(cdir)
    tar = cdir / "payload.tar.gz"
    snap_file = cdir / "snapshot.json"
    try:
        if cdir.is_symlink() or not tar.is_file() or not snap_file.is_file():
            return False, None, None
    except OSError:
        return False, None, None
    snap = _read_json(snap_file)
    if not snap or snap.get("versionId") != cdir.name:
        return False, None, None
    expected = str(snap.get("contentSha256") or "").lower()
    if len(expected) != 64:
        return False, None, None
    if expected_record_sha and record_digest is not None:
        try:
            if record_digest(snap) != expected_record_sha:
                return False, None, None
        except Exception:
            return False, None, None
    try:
        st = tar.stat()
    except OSError:
        return False, None, None
    memo = _read_json(cdir / MEMO_NAME) or {}
    same_file = memo.get("size") == st.st_size and memo.get("mtimeNs") == st.st_mtime_ns
    if same_file and str(memo.get("sha256") or "").lower() == expected:
        at = parse_iso(memo.get("verifiedAt"))
        fresh = at is not None and wall_now() - at < MEMO_MAX_AGE
        if fresh:
            return True, snap, at
        if budget is None or not budget.take(st.st_size):
            return True, snap, None
    try:
        digest, size = _hash_file(tar)
    except OSError:
        return False, None, None
    if digest.lower() != expected or size != st.st_size:
        return False, None, None
    try:
        write_cache_memo(cdir, digest, st.st_size, st.st_mtime_ns)
    except OSError:
        pass
    return True, snap, None


def _lstat_sig(path: Path) -> Optional[Tuple[int, int, int, int, int]]:
    try:
        st = os.lstat(str(path))
    except OSError:
        return None
    return (st.st_mode, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


class CacheIndex(object):
    """Cross-round memo of :func:`cache_ok` results for one long-running sync session (Phase F).

    A watch-mode sync used to re-check every cached snapshot from scratch each
    round: two JSON reads in :func:`cache_ok` plus the signed record and both
    metadata files again in the cache listing, for every entry, every few
    seconds. This index keeps the outcome of the full check and reuses it only
    while all of the following hold:

    * the ``lstat`` signatures (mode, device, inode, size, mtime_ns) of the
      snapshot directory, ``payload.tar.gz``, ``snapshot.json`` and the digest
      memo are exactly those taken *before* the full check (so a file changed
      during or after the check is checked again; a directory replaced by a
      symlink never matches);
    * the expected record digest (from the version graph) is unchanged;
    * the result was positive and the digest memo that vouched for it is still
      within ``MEMO_MAX_AGE`` -- an expired memo always goes back to
      :func:`cache_ok`, so the bounded bit-rot re-hash (``HashBudget``) runs
      exactly as before; negative results are never kept (checked again);
    * the directory is a real directory and its files are regular files (a
      symlinked file is followed by :func:`cache_ok`, so it is never reused).

    It never relaxes what :func:`cache_ok` checks; it only skips repeating a
    check whose inputs are byte-for-byte the same files. The parsed signed record
    of a positive entry is kept so the cache listing does not parse it again
    (callers get a shallow copy). ``limit`` bounds memory; when exceeded the
    index starts over (falling back to full checks, never to trusting).
    """

    def __init__(self, limit: int = 100000) -> None:
        self.limit = int(limit)
        self.entries: Dict[str, Tuple[Any, ...]] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def signature(cdir: Path) -> Optional[Tuple[Any, ...]]:
        """``lstat`` signatures, or ``None`` (never reused) unless the directory and its files are plain.

        The directory must be a real directory and each of the three files either a
        regular file or absent: ``cache_ok`` follows a symlinked file to its target,
        whose changes the link's own ``lstat`` would not show.
        """
        cdir = Path(cdir)
        head = _lstat_sig(cdir)
        if head is None or not stat.S_ISDIR(head[0]):
            return None  # missing, a symlink or not a directory: always the full check
        sigs = [head]
        for name in ("payload.tar.gz", "snapshot.json", MEMO_NAME):
            sig = _lstat_sig(cdir / name)
            if sig is not None and not stat.S_ISREG(sig[0]):
                return None
            sigs.append(sig)
        return tuple(sigs)

    def check(self, cdir: Path, record_digest: Optional[Callable[[Dict[str, Any]], str]] = None,
              expected_record_sha: str = "", budget: Optional[HashBudget] = None
              ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Same verdict as :func:`cache_ok`; returns ``(ok, record)`` where ``record`` is the signed snapshot."""
        cdir = Path(cdir)
        key = str(cdir)
        sig = self.signature(cdir)
        ent = self.entries.get(key)
        if sig is not None and ent is not None and ent[0] == sig and ent[1] == expected_record_sha:
            record, memo_at = ent[3], ent[4]
            if memo_at is not None and wall_now() - memo_at < MEMO_MAX_AGE:
                self.hits += 1
                return True, dict(record)
        self.misses += 1
        ok, record, memo_at = _cache_check(cdir, record_digest, expected_record_sha, budget)
        if sig is None or not ok or record is None:
            # Only positive verdicts are kept: a failure (possibly a transient read error) is checked
            # again next time, exactly as without the index.
            self.entries.pop(key, None)
            return ok, None
        if len(self.entries) >= self.limit and key not in self.entries:
            self.entries.clear()
        self.entries[key] = (sig, expected_record_sha, ok, record, memo_at)
        return True, dict(record)


# ---------------------------------------------------------------- views for status, doctor and prune (Phase D)


def mark_pruned(home: Any, snapshot_ids: Iterable[str]) -> Dict[str, Any]:
    """Record payloads a cache prune removed on purpose, so they are not requested again via ``needs``."""
    ids = [s for s in snapshot_ids if isinstance(s, str) and s]

    def fn(st: Dict[str, Any]) -> None:
        now = L.now_iso()
        for sid in ids:
            st.setdefault("prunedSnapshots", {})[sid] = now

    return update(home, fn)


def unmark_pruned(home: Any, snapshot_ids: Iterable[str]) -> Dict[str, Any]:
    """Forget the prune mark of snapshots that must be repairable again (current version, heads, re-cached)."""
    ids = [s for s in snapshot_ids if isinstance(s, str) and s]

    def fn(st: Dict[str, Any]) -> None:
        pruned = st.setdefault("prunedSnapshots", {})
        for sid in ids:
            pruned.pop(sid, None)

    return update(home, fn)


def handoff_superseded(info: Any) -> bool:
    """A sent handoff replaced by an explicit re-issue (``supersededBy``) or withdrawn because its original won."""
    return isinstance(info, dict) and bool(info.get("supersededBy") or info.get("withdrawn"))


def delivery_view(state: Optional[Dict[str, Any]], relay_state: Optional[Dict[str, Any]] = None
                  ) -> Dict[str, Any]:
    """Per-peer counts of the three delivery states plus pending/covered work (no I/O).

    ``published``: our delivery for the peer's current key is out, not yet confirmed.
    ``peerVerified``: the peer's signed current-key have lists the snapshot.
    ``claimed``: handoff receipts verified for that peer (from ``sentRelays``).
    """
    peers: Dict[str, Dict[str, int]] = {}
    for ent in ((state or {}).get("outbound") or {}).values():
        if not isinstance(ent, dict):
            continue
        did = str(ent.get("recipientDeviceId") or "")
        row = peers.setdefault(did, {"pending": 0, "published": 0, "peerVerified": 0, "covered": 0,
                                     "superseded": 0, "claimed": 0, "awaitingReceipt": 0})
        status = ent.get("status")
        key = {"peer_verified": "peerVerified"}.get(str(status), str(status))
        if key in row:
            row[key] += 1
    for info in ((relay_state or {}).get("sentRelays") or {}).values():
        if not isinstance(info, dict):
            continue
        did = str(info.get("toDeviceId") or "")
        row = peers.setdefault(did, {"pending": 0, "published": 0, "peerVerified": 0, "covered": 0,
                                     "superseded": 0, "claimed": 0, "awaitingReceipt": 0})
        if info.get("acked"):
            row["claimed"] += 1
        elif handoff_superseded(info):
            row["supersededHandoffs"] = row.get("supersededHandoffs", 0) + 1  # re-issued: never awaited again
        else:
            row["awaitingReceipt"] += 1
    return {"peers": peers}


def retention_inputs(state: Optional[Dict[str, Any]], relay_state: Optional[Dict[str, Any]],
                     paired_fps: Dict[str, str], fresh_needs: Optional[Dict[str, List[str]]] = None
                     ) -> Dict[str, Any]:
    """What a cache prune must keep, derived from durable state only (no I/O, no clock).

    ``paired_fps`` maps each currently paired peer to its pinned key fingerprint.
    ``fresh_needs`` maps peers to the ``needs`` list of their fresh, verified have.

    Returns ``snapshots`` (payload directories to keep, with reasons),
    ``checkpointReadyKeys`` (``sync_publications`` checkpoints to keep, keyed by
    readyKey) and ``deletableCheckpointReadyKeys``. Entry records themselves are
    never prunable: they are the anti-duplicate history.
    """
    st = state or {}
    rs = relay_state or {}
    keep: Dict[str, Set[str]] = {}

    def add(sid: Any, why: str) -> None:
        if isinstance(sid, str) and sid:
            keep.setdefault(sid, set()).add(why)

    for pid, vid in (rs.get("localCurrentVersion") or {}).items():
        add(vid, "localCurrentVersion")
    for pid, graph in (rs.get("versionGraph") or {}).items():
        for head in (graph or {}).get("heads") or []:
            add(head, "versionGraphHead")
    for pid, pend in (rs.get("pendingSends") or {}).items():
        if isinstance(pend, dict) and isinstance(pend.get("prepared"), dict):
            add(pend["prepared"].get("snapshotId"), "pendingSend")
    for rid, info in (rs.get("sentRelays") or {}).items():
        if isinstance(info, dict) and not info.get("acked") and not handoff_superseded(info):
            add(info.get("snapshotId"), "unackedHandoff")
    for rid, info in (rs.get("receivedRelays") or {}).items():
        if isinstance(info, dict) and not info.get("receiptUploaded"):
            add(((info.get("result") or {}).get("snapshotId")), "receiptNotUploaded")
    keep_ckpt: Set[str] = set()
    drop_ckpt: Set[str] = set()
    for ent in (st.get("outbound") or {}).values():
        if not isinstance(ent, dict):
            continue
        did = str(ent.get("recipientDeviceId") or "")
        current = norm_fp(paired_fps.get(did)) == norm_fp(ent.get("recipientKeyFingerprint")) if did in paired_fps else False
        status = ent.get("status")
        undelivered = current and status in ("pending", "published")
        if undelivered:
            add(ent.get("snapshotId"), "awaitingPeerVerification")
        for g in ent.get("generations") or []:
            if not isinstance(g, dict) or not g.get("readyKey"):
                continue
            if g.get("state") == "preparing" or (undelivered and g.get("state") == "published"):
                keep_ckpt.add(g["readyKey"])
            else:
                drop_ckpt.add(g["readyKey"])
    for ent in (st.get("inbound") or {}).values():
        if isinstance(ent, dict) and ent.get("state") in NEEDS_STATES:
            add(ent.get("snapshotId"), "ownNeeds")
    for did, needs in (fresh_needs or {}).items():
        for sid in needs or []:
            add(sid, "peerNeeds:%s" % did)
    return {
        "snapshots": {sid: sorted(why) for sid, why in sorted(keep.items())},
        "checkpointReadyKeys": sorted(keep_ckpt),
        "deletableCheckpointReadyKeys": sorted(drop_ckpt - keep_ckpt),
    }
