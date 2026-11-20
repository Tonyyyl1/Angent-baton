"""Transactional workspace preparation, journaled apply, drift inspection, resume, and rollback.

Provides crash-safe workspace application for RelayDesk:
- Stage A (Isolated Preparation): builds the full desired post-state in an
  isolated directory, captures pre/post file blobs, preserved secrets and
  gitignored paths, Git refs, HEAD, and raw `.git/index`, and atomically commits
  a SHA-256 checksummed `journal.json` before mutating the target directory.
- Stage B (Journal-Protected Commit): applies file deletions/creations/updates,
  unpacks Git bundle/staged objects, updates affected refs and HEAD, swaps
  `.git/index` atomically, and transitions the journal to `WORKSPACE_COMMITTED`.
- Recovery: verifies journal integrity (`CorruptedRecoveryError` on corruption),
  inspects target workspace for post-interruption user drift
  (`RecoveryConflictError`), and supports deterministic forward resume or safe
  rollback.
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import packager
from . import validation


JOURNAL_FORMAT_VERSION = 2

PHASE_PREPARED = "PREPARED"
PHASE_APPLYING_FILES = "APPLYING_FILES"
PHASE_APPLYING_GIT = "APPLYING_GIT"
PHASE_WORKSPACE_COMMITTED = "WORKSPACE_COMMITTED"
PHASE_ARCHIVED = "ARCHIVED"
PHASE_UPDATING_LEDGER = "UPDATING_LEDGER"
PHASE_LEDGER_UPDATED = "LEDGER_UPDATED"
PHASE_COMPLETED = "COMPLETED"
PHASE_ROLLING_BACK = "ROLLING_BACK"
PHASE_ROLLED_BACK = "ROLLED_BACK"

_POST_COMMIT_PHASES = frozenset((
    PHASE_WORKSPACE_COMMITTED,
    PHASE_ARCHIVED,
    PHASE_UPDATING_LEDGER,
    PHASE_LEDGER_UPDATED,
))

_TEMP_REF_PREFIX = "refs/relay-tx-objects/"


# ---------------------------------------------------------------- exceptions


class WorkspaceTxError(Exception):
    """Base error for transactional workspace apply and recovery."""

    def __init__(self, message: str, code: str = "workspace_tx_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class CorruptedRecoveryError(WorkspaceTxError):
    """Raised when a transaction directory exists with missing or corrupted journal/blobs."""

    category = validation.CATEGORY_NEEDS_ACTION

    def __init__(
        self,
        message: str = "工作区恢复日志已损坏或校验失败。",
        code: str = "recovery_corrupted",
    ) -> None:
        super().__init__(message, code=code)


class RecoveryConflictError(WorkspaceTxError):
    """Raised when post-interruption local edits prevent safe resume or rollback."""

    def __init__(
        self,
        message: str,
        target_path: str = "",
        reasons: Optional[List[str]] = None,
        relay_id: str = "",
        code: str = "recovery_conflict",
    ) -> None:
        super().__init__(message, code=code)
        self.target_path = target_path
        self.reasons = list(reasons or [])
        self.relay_id = relay_id


# ---------------------------------------------------------------- fault hook


def maybe_inject_fault(point_name: str) -> None:
    """Trigger a deterministic test fault when `RELAY_DESK_FAULT_POINT` matches."""
    if os.environ.get("RELAY_DESK_FAULT_POINT") == point_name:
        if os.environ.get("RELAY_DESK_FAULT_MODE", "").strip().lower() == "sigkill":
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except Exception:
                    pass
            if os.name == "nt":
                os._exit(137)
            else:
                os.kill(os.getpid(), signal.SIGKILL)
        raise RuntimeError("Injected fault: " + point_name)


# ---------------------------------------------------------------- utilities


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_relay_id(relay_id: str) -> str:
    if not isinstance(relay_id, str) or not relay_id.strip():
        raise WorkspaceTxError("无效的交接事务 ID。", code="invalid_relay_id")
    rid = relay_id.strip()
    if "/" in rid or "\\" in rid or ".." in rid or "\x00" in rid or rid in (".", ".."):
        raise WorkspaceTxError("交接事务 ID 包含非法路径字符：%s" % rid, code="invalid_relay_id")
    return rid


def _transactions_root(local_home: Path) -> Path:
    return Path(local_home) / "transactions"


def _tx_dir(local_home: Path, relay_id: str) -> Path:
    rid = _validate_relay_id(relay_id)
    tx_dir = _transactions_root(local_home) / rid
    # Recovery records must live inside the local home, never behind a planted symlink.
    validation.ensure_contained(local_home, tx_dir, check_final=True, field="transactions")
    return tx_dir


def _canonical_journal_sha256(journal: Dict[str, Any]) -> str:
    payload = {k: v for k, v in journal.items() if k != "journalSha256"}
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _atomic_write_bytes(dest: Path, data: bytes, mode: Optional[int] = 0o600) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        if mode is not None:
            try:
                os.chmod(tmp_name, mode)
            except OSError:
                pass
        os.replace(tmp_name, str(dest))
    except BaseException:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_write_json(dest: Path, obj: Dict[str, Any], mode: Optional[int] = 0o600) -> None:
    data = (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write_bytes(dest, data, mode=mode)


def _write_journal_atomic(tx_dir: Path, journal: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(journal)
    out["formatVersion"] = JOURNAL_FORMAT_VERSION
    out["journalSha256"] = _canonical_journal_sha256(out)
    _atomic_write_json(tx_dir / "journal.json", out, mode=0o600)
    return out


def _rmtree_robust(path: Path) -> None:
    if not os.path.lexists(str(path)):
        return

    def _on_error(func: Any, p: str, _exc_info: Any) -> None:
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            func(p)
        except OSError:
            pass

    if os.path.islink(str(path)) or not os.path.isdir(str(path)):
        os.unlink(str(path))
    else:
        shutil.rmtree(str(path), onerror=_on_error)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blob_rel_for_sha(bucket: str, sha: str) -> str:
    return "%s/%s.bin" % (bucket, sha)


def _store_entry_blob(
    tx_root: Path,
    bucket: str,
    full_path: str,
    kind: str,
    sha: str,
) -> str:
    rel_blob = _blob_rel_for_sha(bucket, sha)
    dest = tx_root / rel_blob
    if dest.is_file():
        return rel_blob
    dest.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        data = os.readlink(full_path).encode("utf-8")
        _atomic_write_bytes(dest, data, mode=0o600)
    else:
        with open(full_path, "rb") as fh:
            data = fh.read()
        _atomic_write_bytes(dest, data, mode=0o600)
    return rel_blob


def _verify_and_read_blob_path(tx_dir: Path, rel_blob: str, expected_sha: str) -> Path:
    blob_path = tx_dir / rel_blob
    if not blob_path.is_file():
        raise CorruptedRecoveryError("恢复事务缺少必需的数据块：%s" % rel_blob)
    actual_sha, _ = packager._sha256_file(str(blob_path))
    if not hmac.compare_digest(actual_sha, expected_sha):
        raise CorruptedRecoveryError("恢复数据块 SHA-256 校验失败：%s" % rel_blob)
    return blob_path


def _format_conflict_msg(target_path: str, reasons: List[str]) -> str:
    summary = "；".join(reasons[:6])
    if len(reasons) > 6:
        summary += "（等共 %d 处改动）" % len(reasons)
    return (
        "工作区「%s」在中断后检测到新的本地改动（%s），无法安全自动恢复或回滚；"
        "已完整保留当前目录，请先手动处理或使用 --into 接收至新目录。"
        % (target_path, summary)
    )


# ---------------------------------------------------------------- filesystem & git inspection helpers


def _stat_entry(full_path: str) -> Optional[Dict[str, Any]]:
    if not os.path.lexists(full_path):
        return None
    if os.path.islink(full_path):
        target_bytes = os.readlink(full_path).encode("utf-8")
        return {
            "exists": True,
            "kind": "symlink",
            "mode": "120000",
            "sha256": _sha256_bytes(target_bytes),
            "size": len(target_bytes),
        }
    if os.path.isfile(full_path):
        sha, size = packager._sha256_file(full_path)
        return {
            "exists": True,
            "kind": "file",
            "mode": packager._file_git_mode(full_path),
            "sha256": sha,
            "size": size,
        }
    return None


def _entry_sig(entry: Optional[Dict[str, Any]]) -> Tuple[bool, str, str, str]:
    if not entry or not entry.get("exists"):
        return (False, "", "", "")
    return (
        True,
        str(entry.get("kind") or ""),
        str(entry.get("mode") or ""),
        str(entry.get("sha256") or ""),
    )


def _scan_worktree_entries(
    root_path: str,
    ignored_rel_paths: Optional[Set[str]] = None,
    transaction_scratch: str = "",
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Walk root_path (excluding .git and system junk) and split regular vs preserved entries.

    Returns `(regular_files, preserved_files)`.
    """
    regular: Dict[str, Dict[str, Any]] = {}
    preserved: Dict[str, Dict[str, Any]] = {}
    ignored_set = set(ignored_rel_paths or ())

    if not os.path.isdir(root_path) or os.path.islink(root_path):
        return regular, preserved

    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        kept_dirs: List[str] = []
        for dname in sorted(dirnames):
            if dirpath == root_path and dname == transaction_scratch:
                continue
            if dname.lower() == ".git" or dname.lower() in packager.IGNORED_SYSTEM_NAMES:
                continue
            full_d = os.path.join(dirpath, dname)
            rel_d = packager._nfc(os.path.relpath(full_d, root_path).replace("\\", "/"))
            if os.path.islink(full_d):
                info = _stat_entry(full_d)
                if info is not None:
                    if packager._is_secret_filename(rel_d) or rel_d in ignored_set:
                        preserved[rel_d] = info
                    else:
                        regular[rel_d] = info
                continue
            kept_dirs.append(dname)
        dirnames[:] = kept_dirs

        for fname in sorted(filenames):
            full_f = os.path.join(dirpath, fname)
            rel_f = packager._nfc(os.path.relpath(full_f, root_path).replace("\\", "/"))
            if packager._is_ignored_system_path(rel_f):
                continue
            info = _stat_entry(full_f)
            if info is None:
                continue
            if packager._is_secret_filename(rel_f) or rel_f in ignored_set:
                preserved[rel_f] = info
            else:
                regular[rel_f] = info

    return regular, preserved


def _collect_gitignored_paths(repo_path: str) -> Set[str]:
    if not packager._is_git_repo_root(repo_path):
        return set()
    rc, raw, _ = packager._run_git_bytes(
        repo_path,
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
    )
    if rc != 0 or not raw:
        return set()
    out: Set[str] = set()
    for p in packager._parse_nul_paths(raw):
        norm = packager._nfc(p.replace("\\", "/"))
        if norm and not packager._is_ignored_system_path(norm):
            out.add(norm)
    return out


def _read_git_index_entries(repo_path: str) -> Dict[str, List[str]]:
    rc, raw, err = packager._run_git_bytes(repo_path, ["ls-files", "--stage", "-z"])
    if rc != 0:
        raise WorkspaceTxError(
            "无法读取 Git 暂存区索引：%s" % err.decode("utf-8", errors="replace").strip(),
            code="git_index_read_error",
        )
    entries: Dict[str, List[str]] = {}
    if not raw:
        return entries
    for token in raw.split(b"\x00"):
        if not token:
            continue
        text = token.decode("utf-8", errors="surrogateescape")
        if "\t" not in text:
            continue
        meta_part, raw_path = text.split("\t", 1)
        fields = meta_part.strip().split()
        if len(fields) < 3:
            continue
        mode, oid, stage = fields[0], fields[1], fields[2]
        norm_p = packager._nfc(raw_path.replace("\\", "/"))
        entries[norm_p] = [mode, oid, stage]
    return entries


def _read_git_head(repo_path: str) -> Dict[str, Any]:
    rc_sym, out_sym, _ = packager._run_git_text(repo_path, ["symbolic-ref", "-q", "HEAD"])
    rc_oid, out_oid, _ = packager._run_git_text(repo_path, ["rev-parse", "--verify", "HEAD"])
    oid = out_oid.strip() if rc_oid == 0 else ""
    if rc_sym == 0 and out_sym.strip():
        return {"symbolic": True, "ref": out_sym.strip(), "oid": oid}
    return {"symbolic": False, "ref": "", "oid": oid}


def _read_git_refs(repo_path: str) -> Dict[str, str]:
    rc, out, err = packager._run_git_text(
        repo_path,
        ["for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags"],
    )
    if rc != 0:
        raise WorkspaceTxError("无法读取 Git 引用列表：%s" % err.strip(), code="git_refs_read_error")
    refs: Dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or " " not in line:
            continue
        refname, oid = line.split(" ", 1)
        refs[refname.strip()] = oid.strip()
    return refs


def _read_git_remotes(repo_path: str) -> Dict[str, str]:
    rc, out, _ = packager._run_git_text(repo_path, ["remote", "-v"])
    if rc != 0 or not out.strip():
        return {}
    remotes: Dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in remotes:
            remotes[parts[0]] = parts[1]
    return remotes


def _cleanup_temp_git_refs(repo_path: str) -> None:
    rc, out, _ = packager._run_git_text(
        repo_path,
        ["for-each-ref", "--format=%(refname)", _TEMP_REF_PREFIX.rstrip("/")],
    )
    if rc != 0 or not out.strip():
        return
    for line in out.splitlines():
        refname = line.strip()
        if refname.startswith(_TEMP_REF_PREFIX):
            packager._run_git_text(repo_path, ["update-ref", "-d", refname])


def _unpack_bundle_objects(repo_path: str, bundle_path: str) -> None:
    rc_lh, out_lh, err_lh = packager._run_git_text(repo_path, ["bundle", "list-heads", bundle_path])
    if rc_lh != 0:
        raise WorkspaceTxError("无法读取 Git bundle 头列表：%s" % err_lh.strip(), code="git_bundle_error")
    bundle_refs: List[str] = []
    for line in out_lh.splitlines():
        line = line.strip()
        if not line or " " not in line:
            continue
        _, refname = line.split(" ", 1)
        refname = refname.strip()
        if refname and refname not in bundle_refs:
            bundle_refs.append(refname)
    if not bundle_refs:
        return
    refspecs = [
        "+%s:%sitem_%d" % (bref, _TEMP_REF_PREFIX, idx)
        for idx, bref in enumerate(bundle_refs)
    ]
    rc_f, _, err_f = packager._run_git_text(
        repo_path,
        ["fetch", "--no-tags", "--no-write-fetch-head", "-q", bundle_path] + refspecs,
    )
    if rc_f != 0:
        raise WorkspaceTxError("解包 Git bundle 对象失败：%s" % err_f.strip(), code="git_bundle_unpack_error")


def _apply_git_head(repo_path: str, head_spec: Dict[str, Any]) -> None:
    if head_spec.get("symbolic"):
        ref = str(head_spec.get("ref") or "refs/heads/main")
        rc, _, err = packager._run_git_text(repo_path, ["symbolic-ref", "HEAD", ref])
        if rc != 0:
            raise WorkspaceTxError("设置 Git HEAD 引用失败：%s" % err.strip(), code="git_head_error")
    else:
        oid = str(head_spec.get("oid") or "")
        if oid:
            rc, _, err = packager._run_git_text(
                repo_path, ["update-ref", "--no-deref", "HEAD", oid]
            )
            if rc != 0:
                raise WorkspaceTxError("设置游离 Git HEAD 失败：%s" % err.strip(), code="git_head_error")


def _swap_git_index(repo_path: str, src_blob_path: Optional[Path]) -> None:
    index_path = Path(repo_path) / ".git" / "index"
    if src_blob_path is None:
        if os.path.lexists(str(index_path)):
            os.unlink(str(index_path))
        return
    with open(str(src_blob_path), "rb") as fh:
        raw = fh.read()
    _atomic_write_bytes(index_path, raw, mode=0o644)


def _git_object_exists(repo_path: str, oid: str) -> bool:
    rc, _, _ = packager._run_git_text(repo_path, ["cat-file", "-e", oid])
    return rc == 0


def _write_git_blob_object(repo_path: str, blob_file: Path, expected_oid: str) -> None:
    if _git_object_exists(repo_path, expected_oid):
        return
    with open(str(blob_file), "rb") as fh:
        data = fh.read()
    proc = subprocess.run(
        ["git", "-C", repo_path, "hash-object", "-w", "--stdin"],
        input=data,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise WorkspaceTxError(
            "写入 Git 暂存对象失败：%s" % proc.stderr.decode("utf-8", errors="replace").strip(),
            code="git_object_write_error",
        )
    actual_oid = proc.stdout.decode("utf-8", errors="replace").strip()
    if actual_oid != expected_oid:
        raise CorruptedRecoveryError(
            "恢复 Git 暂存对象校验失败（期望 %s，实际 %s）。" % (expected_oid, actual_oid)
        )


def _head_target_matches(cur_head: Dict[str, Any], expected_head: Dict[str, Any]) -> bool:
    if bool(cur_head.get("symbolic")) != bool(expected_head.get("symbolic")):
        return False
    if cur_head.get("symbolic"):
        return cur_head.get("ref") == expected_head.get("ref")
    return cur_head.get("oid") == expected_head.get("oid")


# ---------------------------------------------------------------- public journal APIs


def has_transaction(local_home: Path, relay_id: str) -> bool:
    """Return True if a transaction directory for `relay_id` exists on disk."""
    tx_dir = _tx_dir(local_home, relay_id)
    return os.path.lexists(str(tx_dir))


def list_transactions(local_home: Path) -> List[str]:
    """Return sorted list of transaction relay IDs under `<local_home>/transactions`."""
    root = _transactions_root(local_home)
    if not root.is_dir():
        return []
    out: List[str] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            out.append(entry.name)
    return out


def load_and_verify_journal(local_home: Path, relay_id: str) -> Dict[str, Any]:
    """Load and SHA-256 verify `<local_home>/transactions/<relay_id>/journal.json`."""
    rid = _validate_relay_id(relay_id)
    tx_dir = _tx_dir(local_home, rid)
    if not os.path.lexists(str(tx_dir)):
        raise WorkspaceTxError("找不到交接事务记录：%s" % rid, code="transaction_not_found")
    if not tx_dir.is_dir():
        raise CorruptedRecoveryError("恢复事务路径不是目录：%s" % rid)

    jpath = tx_dir / "journal.json"
    if not jpath.is_file():
        raise CorruptedRecoveryError("恢复事务缺少 journal.json：%s" % rid)
    try:
        with open(str(jpath), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise CorruptedRecoveryError("恢复日志 journal.json 损坏或无法解析（%s）：%s" % (rid, e))

    if not isinstance(data, dict):
        raise CorruptedRecoveryError("恢复日志 journal.json 格式错误（顶层不是对象）：%s" % rid)

    required_keys = (
        "formatVersion",
        "relayId",
        "projectId",
        "targetPath",
        "mode",
        "phase",
        "preFiles",
        "postFiles",
        "preservedEntries",
        "fileOps",
        "applyResult",
        "journalSha256",
    )
    for k in required_keys:
        if k not in data:
            raise CorruptedRecoveryError("恢复日志 journal.json 缺少必需字段 %s：%s" % (k, rid))

    if data.get("relayId") != rid:
        raise CorruptedRecoveryError("恢复日志 relayId 不匹配：%s" % rid)
    if data.get("formatVersion") not in (1, JOURNAL_FORMAT_VERSION):
        raise CorruptedRecoveryError("不支持的恢复日志版本：%s" % data.get("formatVersion"))
    if data.get("mode") not in ("git", "folder"):
        raise CorruptedRecoveryError("恢复日志 mode 无效：%s" % rid)
    if not isinstance(data.get("preFiles"), dict) or not isinstance(data.get("postFiles"), dict):
        raise CorruptedRecoveryError("恢复日志文件清单损坏：%s" % rid)
    if not isinstance(data.get("preservedEntries"), dict) or not isinstance(data.get("fileOps"), list):
        raise CorruptedRecoveryError("恢复日志操作清单损坏：%s" % rid)
    if not isinstance(data.get("applyResult"), dict):
        raise CorruptedRecoveryError("恢复日志 applyResult 损坏：%s" % rid)
    if data.get("writeStagingRel") and not re.fullmatch(r"\.relay-tx-write-[0-9a-f]{32}", str(data["writeStagingRel"])):
        raise CorruptedRecoveryError("恢复日志临时写入路径无效：%s" % rid)

    expected_sha = str(data.get("journalSha256") or "")
    actual_sha = _canonical_journal_sha256(data)
    if not expected_sha or not hmac.compare_digest(expected_sha, actual_sha):
        raise CorruptedRecoveryError("恢复日志 journal.json SHA-256 校验失败：%s" % rid)

    _validate_journal_paths(data, rid)
    return data


# Drive-like names ("c:notes") are plain file names on POSIX; only Windows treats them as drives.
_REJECT_DRIVE = os.name == "nt"


def _target_join(target_path: str, rel_p: str) -> str:
    """Destination of a journaled file op; refuses symlinked parents and anything outside the target."""
    return validation.safe_join(target_path, rel_p, reject_drive=_REJECT_DRIVE, field="workspace")


_BLOB_REL_RE = re.compile(r"(pre_blobs|post_blobs)/[A-Za-z0-9_.-]+\Z")
_GIT_OID_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?\Z")


def _validate_journal_paths(data: Dict[str, Any], rid: str) -> None:
    """Every path a recovery may write, delete or read must be a clean relative path.

    The checksum only proves the journal is intact, not that it was written by
    this engine, so a recovery never trusts its paths blindly.
    """
    def bad(detail: str) -> CorruptedRecoveryError:
        return CorruptedRecoveryError("恢复日志包含不安全的路径（%s）：%s" % (detail, rid))

    target = data.get("targetPath")
    if not isinstance(target, str) or not target or not os.path.isabs(target) or "\x00" in target:
        raise bad("targetPath")
    maps = {}
    for key in ("preFiles", "postFiles", "preservedEntries"):
        for rel, spec in data[key].items():
            try:
                if validation.normalize_rel_path(rel, reject_drive=_REJECT_DRIVE) != rel:
                    raise bad("%s: %r" % (key, rel))
            except validation.ValidationError:
                raise bad("%s: %r" % (key, rel))
            if not isinstance(spec, dict):
                raise bad("%s: %r" % (key, rel))
            blob = spec.get("blob")
            if blob is not None and (not isinstance(blob, str) or not _BLOB_REL_RE.match(blob) or ".." in blob):
                raise bad("blob: %r" % (blob,))
        maps[key] = data[key]
    for item in data["fileOps"]:
        if not isinstance(item, dict) or item.get("op") not in ("delete", "create", "modify"):
            raise bad("fileOps")
        rel = item.get("path")
        source = maps["preFiles"] if item["op"] == "delete" else maps["postFiles"]
        if not isinstance(rel, str) or rel not in source:
            raise bad("fileOps: %r" % (rel,))
    git_meta = data.get("git") or {}
    if not isinstance(git_meta, dict):
        raise bad("git")
    for key in ("preIndexBlob", "postIndexBlob", "bundleBlob"):
        blob = git_meta.get(key)
        if blob and (not isinstance(blob, str) or not _BLOB_REL_RE.match(blob) or ".." in blob):
            raise bad("git.%s" % key)
    for oid, blob in (git_meta.get("stagedBlobs") or {}).items():
        if not _GIT_OID_RE.match(str(oid)) or not isinstance(blob, str) or not _BLOB_REL_RE.match(blob) or ".." in blob:
            raise bad("git.stagedBlobs")
    for ref in (git_meta.get("affectedRefs") or {}).keys():
        if not isinstance(ref, str) or not ref.startswith("refs/") or ".." in ref or any(c.isspace() for c in ref):
            raise bad("git.affectedRefs: %r" % (ref,))
    staging = data.get("writeStagingRel")
    if staging is not None and staging != "" and not re.fullmatch(r"\.relay-tx-write-[0-9a-f]{32}", str(staging)):
        raise bad("writeStagingRel")


def load_transaction_meta(local_home: Path, relay_id: str) -> Dict[str, Any]:
    """Strictly load `<tx>/meta.json`; a missing or damaged record never falls back to defaults."""
    rid = _validate_relay_id(relay_id)
    meta_path = _tx_dir(local_home, rid) / "meta.json"
    try:
        meta = validation.read_local_json_object(meta_path, "recovery", "恢复记录 meta.json")
    except validation.LocalStateCorruptError as e:
        raise CorruptedRecoveryError(
            "恢复记录 meta.json 已损坏，不能继续接收、签收或回滚（%s）：%s。请保留事务目录并先备份。"
            % (rid, e.message)
        )
    if meta is None:
        raise CorruptedRecoveryError("恢复事务缺少 meta.json：%s" % rid)
    if meta.get("relayId") not in (None, rid):
        raise CorruptedRecoveryError("恢复记录 meta.json 的交接编号与事务目录不一致：%s" % rid)
    if not isinstance(meta.get("extra", {}), dict):
        raise CorruptedRecoveryError("恢复记录 meta.json 的 extra 字段损坏：%s" % rid)
    return meta


def mark_transaction_phase(local_home: Path, relay_id: str, phase: str) -> None:
    """Atomically update the phase of an existing verified transaction journal."""
    if not isinstance(phase, str) or not phase.strip():
        raise WorkspaceTxError("无效的事务阶段名。", code="invalid_phase")
    journal = load_and_verify_journal(local_home, relay_id)
    tx_dir = _tx_dir(local_home, relay_id)
    journal["phase"] = phase.strip()
    journal["updatedAt"] = _now_iso()
    _write_journal_atomic(tx_dir, journal)


def archive_completed_transaction(local_home: Path, relay_id: str) -> None:
    """Mark a transaction as COMPLETED and reclaim pre_blobs/post_blobs storage."""
    if not has_transaction(local_home, relay_id):
        return
    journal = load_and_verify_journal(local_home, relay_id)
    if journal["phase"] in (PHASE_ROLLING_BACK, PHASE_ROLLED_BACK):
        raise WorkspaceTxError("回滚中的交接不能标记为已签收。", code="rollback_in_progress")
    tx_dir = _tx_dir(local_home, relay_id)
    if journal.get("phase") != PHASE_COMPLETED:
        journal["phase"] = PHASE_COMPLETED
        now = _now_iso()
        journal["completedAt"] = journal.get("completedAt") or now
        journal["updatedAt"] = now
        _write_journal_atomic(tx_dir, journal)

    for sub in ("pre_blobs", "post_blobs"):
        _rmtree_robust(tx_dir / sub)
    _cleanup_write_staging(journal)
    staged_dir = journal.get("stagedDir") or ""
    if staged_dir and os.path.lexists(staged_dir):
        _rmtree_robust(Path(staged_dir))


# ---------------------------------------------------------------- drift inspection


def _check_workspace_drift(journal: Dict[str, Any], allow_pre: bool) -> List[str]:
    """Verify current workspace files and Git state against journal expectations.

    When `allow_pre=True` (mid-apply or PREPARED), every entry must match EITHER
    `pre` OR `post`. When `allow_pre=False` (post-commit rollback check), every
    entry must strictly match `post`.
    """
    reasons: List[str] = []
    target_path = str(journal["targetPath"])
    mode = str(journal["mode"])
    is_new_dir = bool(journal.get("isNewDirectory"))
    pre_files: Dict[str, Dict[str, Any]] = journal["preFiles"]
    post_files: Dict[str, Dict[str, Any]] = journal["postFiles"]
    preserved: Dict[str, Dict[str, Any]] = journal["preservedEntries"]

    if not os.path.lexists(target_path):
        if allow_pre and is_new_dir:
            return []
        if not allow_pre:
            reasons.append("目标目录在提交后已被删除或移走")
            return reasons
        # Existing directory vanished mid-apply
        reasons.append("目标目录在恢复前已被删除或移走")
        return reasons

    if os.path.islink(target_path) or not os.path.isdir(target_path):
        reasons.append("目标路径已不再是普通目录：%s" % target_path)
        return reasons
    scratch = journal.get("writeStagingRel")
    if scratch and os.path.islink(os.path.join(target_path, scratch)):
        return ["工作区事务临时目录已被替换为符号链接"]

    if allow_pre and is_new_dir and journal["phase"] != PHASE_ROLLING_BACK:
        # Target dir did not exist pre-tx and Stage B only performs an atomic os.replace;
        # if target_path already exists while phase is still PREPARED, it was created externally.
        reasons.append("目标路径在恢复前已被本地创建：%s" % target_path)
        return reasons

    ignored_known = set(preserved.keys())
    cur_regular, cur_preserved = _scan_worktree_entries(
        target_path, ignored_rel_paths=ignored_known,
        transaction_scratch=str(journal.get("writeStagingRel") or ""),
    )
    if is_new_dir:
        # Rolling a newly created directory back removes that directory. Files
        # excluded from normal snapshots must not be silently removed with it.
        for rel_p in cur_preserved:
            if rel_p not in post_files:
                reasons.append("新建目录中存在需要保留的本地文件：%s" % rel_p)
    # Include any newly created non-gitignored secret files? `_scan_worktree_entries` puts
    # `_is_secret_filename` into `cur_preserved`. Only pre-existing `preserved` keys are
    # guaranteed preserved; any brand-new secret file not in `preserved` shouldn't block
    # unless it's not in pre/post, wait: keep pre-recorded `preserved` ignored and ignore
    # secret files as packager does.
    all_cur = dict(cur_regular)
    for rel_p, info in cur_preserved.items():
        if rel_p not in preserved and not packager._is_secret_filename(rel_p):
            all_cur[rel_p] = info

    for rel_p in sorted(all_cur.keys()):
        if rel_p in preserved:
            continue
        if rel_p not in pre_files and rel_p not in post_files:
            reasons.append("检测到中断后新增本地文件：%s" % rel_p)
        elif not allow_pre and rel_p not in post_files:
            reasons.append("检测到提交后新增或残留本地文件：%s" % rel_p)

    check_keys = sorted(set(post_files.keys()) | (set(pre_files.keys()) if allow_pre else set()))
    for rel_p in check_keys:
        if rel_p in preserved:
            continue
        cur_sig = _entry_sig(all_cur.get(rel_p))
        post_sig = _entry_sig(post_files.get(rel_p))
        if allow_pre:
            pre_sig = _entry_sig(pre_files.get(rel_p))
            if cur_sig != pre_sig and cur_sig != post_sig:
                if not cur_sig[0] and pre_sig[0] and post_sig[0]:
                    reasons.append("检测到中断后本地删除文件：%s" % rel_p)
                else:
                    reasons.append("检测到中断后本地修改文件：%s" % rel_p)
        else:
            if cur_sig != post_sig:
                if not cur_sig[0] and post_sig[0]:
                    reasons.append("检测到提交后本地删除文件：%s" % rel_p)
                else:
                    reasons.append("检测到提交后本地修改文件：%s" % rel_p)

    if mode == "git":
        git_meta = journal.get("git") or {}
        was_git = bool(git_meta.get("wasGitRepo"))
        dot_git = os.path.join(target_path, ".git")
        has_dot_git = os.path.lexists(dot_git) and packager._is_git_repo_root(target_path)

        if not has_dot_git:
            if not allow_pre or was_git:
                reasons.append("检测到本地 .git 仓库目录丢失或损坏")
            return reasons

        pre_index = git_meta.get("preIndexEntries") or {}
        post_index = git_meta.get("postIndexEntries") or {}
        cur_index = _read_git_index_entries(target_path)
        if allow_pre:
            allowed_indexes = [post_index]
            if was_git:
                allowed_indexes.append(pre_index)
            else:
                allowed_indexes.append({})
            if cur_index not in allowed_indexes:
                reasons.append("检测到中断后本地 Git 暂存区（index）改动")
        else:
            if cur_index != post_index:
                reasons.append("检测到提交后本地 Git 暂存区（index）改动")

        pre_head = git_meta.get("preHead") or {}
        post_head = git_meta.get("postHead") or {}
        cur_head = _read_git_head(target_path)
        if allow_pre:
            head_ok = _head_target_matches(cur_head, post_head)
            if was_git and _head_target_matches(cur_head, pre_head):
                head_ok = True
            if not was_git and not cur_head.get("oid"):
                head_ok = True
            if not head_ok:
                reasons.append("检测到中断后本地 Git HEAD 改动")
        else:
            if cur_head != post_head:
                reasons.append("检测到提交后本地 Git HEAD 改动")

        cur_refs = _read_git_refs(target_path)
        affected_refs: Dict[str, Dict[str, Optional[str]]] = git_meta.get("affectedRefs") or {}
        untouched_refs: Dict[str, str] = git_meta.get("untouchedRefs") or {}

        for ref, spec in sorted(affected_refs.items()):
            cur_oid = cur_refs.get(ref)
            if allow_pre:
                allowed_oids = {spec.get("preOid"), spec.get("postOid")}
                if cur_oid not in allowed_oids:
                    reasons.append("检测到中断后本地 Git 引用改动：%s" % ref)
            else:
                if cur_oid != spec.get("postOid"):
                    reasons.append("检测到提交后本地 Git 引用改动：%s" % ref)

        for ref, expected_oid in sorted(untouched_refs.items()):
            if cur_refs.get(ref) != expected_oid:
                reasons.append("检测到中断后本地 Git 分支或标签被修改：%s" % ref)

        known_refs = set(affected_refs.keys()) | set(untouched_refs.keys())
        for ref in sorted(cur_refs.keys()):
            if ref not in known_refs:
                reasons.append("检测到中断后新增本地 Git 引用：%s" % ref)

    return reasons


def inspect_transaction(local_home: Path, relay_id: str) -> Dict[str, Any]:
    """Inspect a transaction's integrity, phase, and workspace drift status."""
    rid = _validate_relay_id(relay_id)
    if not has_transaction(local_home, rid):
        return {
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

    try:
        journal = load_and_verify_journal(local_home, rid)
    except CorruptedRecoveryError as e:
        return {
            "exists": True,
            "relayId": rid,
            "phase": "",
            "targetPath": "",
            "projectId": "",
            "state": "corrupted",
            "canResume": False,
            "canRollback": False,
            "reasons": [str(e)],
        }

    phase = str(journal["phase"])
    target_path = str(journal["targetPath"])
    project_id = str(journal["projectId"])
    moved_before_checkpoint = False
    if phase == PHASE_PREPARED and journal.get("isNewDirectory") and journal.get("stagedDirectoryIdentity"):
        if os.path.isdir(target_path) and not os.path.islink(target_path):
            info = os.stat(target_path)
            moved_before_checkpoint = (
                [info.st_dev, info.st_ino] == journal["stagedDirectoryIdentity"]
                and not os.path.lexists(str(journal.get("stagedDir") or ""))
            )

    if phase == PHASE_COMPLETED:
        return {
            "exists": True,
            "relayId": rid,
            "phase": phase,
            "targetPath": target_path,
            "projectId": project_id,
            "state": "completed",
            "canResume": False,
            "canRollback": False,
            "reasons": [],
        }

    if phase == PHASE_ROLLED_BACK:
        return {
            "exists": True,
            "relayId": rid,
            "phase": phase,
            "targetPath": target_path,
            "projectId": project_id,
            "state": "rolled_back",
            "canResume": False,
            "canRollback": False,
            "reasons": [],
        }

    if phase in _POST_COMMIT_PHASES or moved_before_checkpoint:
        post_drift = _check_workspace_drift(journal, allow_pre=False)
        ledger_started = phase in (PHASE_UPDATING_LEDGER, PHASE_LEDGER_UPDATED)
        if ledger_started:
            post_drift.append("接收已开始更新台账，请继续完成接收和回执补传，不能回滚工作区。")
        return {
            "exists": True,
            "relayId": rid,
            "phase": phase,
            "targetPath": target_path,
            "projectId": project_id,
            "state": "workspace_committed",
            "canResume": True,
            "canRollback": not ledger_started and len(post_drift) == 0,
            "reasons": post_drift,
        }

    # PREPARED, APPLYING_FILES, APPLYING_GIT
    reasons = _check_workspace_drift(journal, allow_pre=True)
    if reasons:
        return {
            "exists": True,
            "relayId": rid,
            "phase": phase,
            "targetPath": target_path,
            "projectId": project_id,
            "state": "conflict",
            "canResume": False,
            "canRollback": False,
            "reasons": reasons,
        }

    state = "rolling_back" if phase == PHASE_ROLLING_BACK else ("clean_pre" if phase == PHASE_PREPARED else "in_progress")
    return {
        "exists": True,
        "relayId": rid,
        "phase": phase,
        "targetPath": target_path,
        "projectId": project_id,
        "state": state,
        "canResume": phase != PHASE_ROLLING_BACK,
        "canRollback": True,
        "reasons": [],
    }


# ---------------------------------------------------------------- Stage A & Stage B


def _compute_file_ops(
    pre_files: Dict[str, Dict[str, Any]],
    post_files: Dict[str, Dict[str, Any]],
) -> List[Dict[str, str]]:
    deletes: List[Dict[str, str]] = []
    writes: List[Dict[str, str]] = []
    for rel_p in sorted(set(pre_files.keys()) | set(post_files.keys())):
        pre_e = pre_files.get(rel_p)
        post_e = post_files.get(rel_p)
        if pre_e is not None and post_e is None:
            deletes.append({"op": "delete", "path": rel_p})
        elif pre_e is None and post_e is not None:
            writes.append({"op": "create", "path": rel_p})
        elif pre_e is not None and post_e is not None:
            if _entry_sig(pre_e) != _entry_sig(post_e):
                writes.append({"op": "modify", "path": rel_p})
    # Deletions first so file<->dir replacements and case-only renames succeed cleanly.
    return deletes + writes


def _capture_git_post_state(
    tx_root: Path,
    staging_dir: str,
    manifest: Dict[str, Any],
    isolated_dir: str,
    was_git_repo: bool,
    pre_refs: Dict[str, str],
    pre_head: Dict[str, Any],
    pre_index_entries: Dict[str, List[str]],
    has_pre_index: bool,
) -> Dict[str, Any]:
    post_index_entries = _read_git_index_entries(isolated_dir)
    post_head = _read_git_head(isolated_dir)
    post_refs = _read_git_refs(isolated_dir)
    remotes = _read_git_remotes(isolated_dir)

    iso_index = os.path.join(isolated_dir, ".git", "index")
    has_post_index = os.path.isfile(iso_index)
    post_index_blob = ""
    if has_post_index:
        with open(iso_index, "rb") as fh:
            idx_bytes = fh.read()
        post_index_blob = "post_blobs/git_index.bin"
        _atomic_write_bytes(tx_root / post_index_blob, idx_bytes, mode=0o600)

    bundle_src = os.path.join(staging_dir, "repo.bundle")
    has_bundle = bool(manifest.get("hasCommits")) and os.path.isfile(bundle_src)
    bundle_blob = ""
    bundle_sha = ""
    if has_bundle:
        with open(bundle_src, "rb") as fh:
            b_bytes = fh.read()
        bundle_sha = _sha256_bytes(b_bytes)
        bundle_blob = "post_blobs/repo.bundle"
        _atomic_write_bytes(tx_root / bundle_blob, b_bytes, mode=0o600)

    # Save blob objects for staged entries since they are not inside repo.bundle.
    staged_blobs: Dict[str, str] = {}
    for entry in manifest.get("stagedEntries") or []:
        if entry.get("status") == "D":
            continue
        rel_p = packager._normalize_rel_path(entry["path"])
        idx_info = post_index_entries.get(rel_p)
        if not idx_info:
            continue
        oid = idx_info[1]
        if oid in staged_blobs:
            continue
        src_member = os.path.join(staging_dir, *packager._normalize_rel_path(entry["archiveMember"]).split("/"))
        if os.path.isfile(src_member):
            with open(src_member, "rb") as fh:
                raw_obj = fh.read()
            rel_obj_blob = "post_blobs/git_obj_%s.bin" % oid
            _atomic_write_bytes(tx_root / rel_obj_blob, raw_obj, mode=0o600)
            staged_blobs[oid] = rel_obj_blob

    manifest_refs = manifest.get("refs") or {}
    affected_refs: Dict[str, Dict[str, Optional[str]]] = {}
    untouched_refs: Dict[str, str] = {}

    for ref, post_oid in sorted(post_refs.items()):
        pre_oid = pre_refs.get(ref)
        if pre_oid != post_oid or ref in manifest_refs or ref == post_head.get("ref"):
            affected_refs[ref] = {"preOid": pre_oid, "postOid": post_oid}
        else:
            untouched_refs[ref] = post_oid

    return {
        "wasGitRepo": was_git_repo,
        "hasPreIndex": has_pre_index,
        "preIndexBlob": "pre_blobs/git_index.bin" if has_pre_index else "",
        "preIndexEntries": pre_index_entries,
        "preHead": pre_head,
        "hasPostIndex": has_post_index,
        "postIndexBlob": post_index_blob,
        "postIndexEntries": post_index_entries,
        "postHead": post_head,
        "hasBundle": has_bundle,
        "bundleBlob": bundle_blob,
        "bundleSha256": bundle_sha,
        "stagedBlobs": staged_blobs,
        "affectedRefs": affected_refs,
        "untouchedRefs": untouched_refs,
        "branch": str(manifest.get("branch") or ""),
        "remotes": remotes,
    }


def _verify_post_blobs(tx_dir: Path, journal: Dict[str, Any]) -> None:
    post_files: Dict[str, Dict[str, Any]] = journal["postFiles"]
    for op_item in journal["fileOps"]:
        if op_item["op"] in ("create", "modify"):
            entry = post_files.get(op_item["path"])
            if not entry or not entry.get("blob"):
                raise CorruptedRecoveryError("恢复日志缺少目标文件数据块记录：%s" % op_item["path"])
            _verify_and_read_blob_path(tx_dir, entry["blob"], entry["sha256"])

    if journal["mode"] == "git":
        git_meta = journal.get("git") or {}
        if git_meta.get("hasBundle"):
            _verify_and_read_blob_path(
                tx_dir,
                str(git_meta.get("bundleBlob") or ""),
                str(git_meta.get("bundleSha256") or ""),
            )
        if git_meta.get("hasPostIndex"):
            idx_path = tx_dir / str(git_meta.get("postIndexBlob") or "")
            if not idx_path.is_file():
                raise CorruptedRecoveryError("恢复事务缺少目标 Git index 数据块。")
        for oid, rel_blob in (git_meta.get("stagedBlobs") or {}).items():
            if not (tx_dir / rel_blob).is_file():
                raise CorruptedRecoveryError("恢复事务缺少 Git 暂存对象块：%s" % oid)


def _verify_pre_blobs(tx_dir: Path, journal: Dict[str, Any]) -> None:
    pre_files: Dict[str, Dict[str, Any]] = journal["preFiles"]
    for op_item in journal["fileOps"]:
        if op_item["op"] in ("delete", "modify"):
            entry = pre_files.get(op_item["path"])
            if not entry or not entry.get("blob"):
                raise CorruptedRecoveryError("回滚日志缺少原始文件数据块记录：%s" % op_item["path"])
            _verify_and_read_blob_path(tx_dir, entry["blob"], entry["sha256"])

    if journal["mode"] == "git":
        git_meta = journal.get("git") or {}
        if git_meta.get("wasGitRepo") and git_meta.get("hasPreIndex"):
            idx_path = tx_dir / str(git_meta.get("preIndexBlob") or "")
            if not idx_path.is_file():
                raise CorruptedRecoveryError("回滚事务缺少原始 Git index 数据块。")


def _cleanup_write_staging(journal: Dict[str, Any]) -> None:
    rel = journal.get("writeStagingRel")
    if rel:
        _rmtree_robust(Path(journal["targetPath"]) / rel)


def _replace_workspace_file(journal: Dict[str, Any], blob_path: Path, dest_path: str, mode: str) -> None:
    """Stage complete bytes on the target filesystem before replacing a file.

    The journal owns this exact scratch directory. A killed copy can leave a
    partial scratch file, but never partial destination bytes; recovery ignores
    only this recorded scratch directory, not arbitrary user files.
    """
    scratch = Path(journal["targetPath"]) / journal["writeStagingRel"]
    if scratch.is_symlink():
        raise CorruptedRecoveryError("工作区事务临时目录被替换为符号链接。")
    scratch.mkdir(mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(scratch), prefix="write-")
    os.close(fd)
    try:
        packager._write_restored_file(str(blob_path), tmp, mode)
        if mode != "120000":
            with open(tmp, "rb+") as fh:
                os.fsync(fh.fileno())
        maybe_inject_fault("tx_before_file_replace")
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        os.replace(tmp, dest_path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def _apply_stage_b_existing(tx_dir: Path, journal: Dict[str, Any]) -> Dict[str, Any]:
    target_path = str(journal["targetPath"])
    mode = str(journal["mode"])
    post_files: Dict[str, Dict[str, Any]] = journal["postFiles"]
    file_ops: List[Dict[str, str]] = journal["fileOps"]

    journal["phase"] = PHASE_APPLYING_FILES
    journal.setdefault("writeStagingRel", ".relay-tx-write-" + uuid.uuid4().hex)
    journal["updatedAt"] = _now_iso()
    journal = _write_journal_atomic(tx_dir, journal)

    mutated_count = 0
    for item in file_ops:
        op = item["op"]
        rel_p = item["path"]
        dest_p = _target_join(target_path, rel_p)
        if op == "delete":
            if os.path.lexists(dest_p):
                os.unlink(dest_p)
                packager._prune_empty_parents(dest_p, target_path)
            mutated_count += 1
        elif op in ("create", "modify"):
            spec = post_files[rel_p]
            blob_path = _verify_and_read_blob_path(tx_dir, spec["blob"], spec["sha256"])
            _replace_workspace_file(journal, blob_path, dest_p, spec["mode"])
            mutated_count += 1
        if mutated_count == 1:
            maybe_inject_fault("tx_mid_files")

    maybe_inject_fault("tx_after_files_before_git")
    _cleanup_write_staging(journal)

    if mode == "git":
        git_meta = journal.get("git") or {}
        journal["phase"] = PHASE_APPLYING_GIT
        journal["updatedAt"] = _now_iso()
        journal = _write_journal_atomic(tx_dir, journal)

        if not packager._is_git_repo_root(target_path):
            branch_init = git_meta.get("branch") or "main"
            rc_init, _, err_init = packager._run_git_text(target_path, ["init", "-b", branch_init])
            if rc_init != 0:
                rc_init2, _, err_init2 = packager._run_git_text(target_path, ["init"])
                if rc_init2 != 0:
                    raise WorkspaceTxError(
                        "初始化目标 Git 仓库失败：%s" % (err_init2 or err_init).strip(),
                        code="git_init_error",
                    )
                packager._run_git_text(target_path, ["symbolic-ref", "HEAD", "refs/heads/%s" % branch_init])
            for rname, rurl in (git_meta.get("remotes") or {}).items():
                packager._run_git_text(target_path, ["remote", "add", rname, rurl])

        if git_meta.get("hasBundle"):
            bundle_path = _verify_and_read_blob_path(
                tx_dir, str(git_meta["bundleBlob"]), str(git_meta["bundleSha256"])
            )
            _unpack_bundle_objects(target_path, str(bundle_path))

        for oid, rel_blob in (git_meta.get("stagedBlobs") or {}).items():
            _write_git_blob_object(target_path, tx_dir / rel_blob, oid)

        for ref, spec in sorted((git_meta.get("affectedRefs") or {}).items()):
            post_oid = spec.get("postOid")
            if post_oid:
                rc_u, _, err_u = packager._run_git_text(target_path, ["update-ref", ref, post_oid])
                if rc_u != 0:
                    raise WorkspaceTxError(
                        "更新 Git 引用失败（%s）：%s" % (ref, err_u.strip()),
                        code="git_ref_update_error",
                    )

        _cleanup_temp_git_refs(target_path)
        _apply_git_head(target_path, git_meta.get("postHead") or {})

        maybe_inject_fault("tx_after_git_refs_before_index")

        if git_meta.get("hasPostIndex"):
            _swap_git_index(target_path, tx_dir / str(git_meta["postIndexBlob"]))
        else:
            _swap_git_index(target_path, None)
        packager._run_git_text(target_path, ["update-index", "-q", "--refresh"])
    else:
        dot_git = os.path.join(target_path, ".git")
        if os.path.lexists(dot_git):
            _rmtree_robust(Path(dot_git))

    journal["phase"] = PHASE_WORKSPACE_COMMITTED
    journal["updatedAt"] = _now_iso()
    journal = _write_journal_atomic(tx_dir, journal)
    maybe_inject_fault("tx_after_workspace_commit")
    return dict(journal["applyResult"])


def _materialize_new_directory_from_blobs(tx_dir: Path, journal: Dict[str, Any], dest_dir: str) -> None:
    os.makedirs(dest_dir, exist_ok=True)
    post_files: Dict[str, Dict[str, Any]] = journal["postFiles"]
    for rel_p in sorted(post_files.keys()):
        spec = post_files[rel_p]
        blob_path = _verify_and_read_blob_path(tx_dir, spec["blob"], spec["sha256"])
        if spec.get("mode") == "120000":
            _check_symlink_blob(rel_p, blob_path)
        packager._write_restored_file(
            str(blob_path),
            _target_join(dest_dir, rel_p),
            spec["mode"],
        )

    if journal["mode"] == "git":
        git_meta = journal.get("git") or {}
        branch_init = git_meta.get("branch") or "main"
        rc_init, _, err_init = packager._run_git_text(dest_dir, ["init", "-b", branch_init])
        if rc_init != 0:
            rc_init2, _, err_init2 = packager._run_git_text(dest_dir, ["init"])
            if rc_init2 != 0:
                raise WorkspaceTxError(
                    "初始化新目录 Git 仓库失败：%s" % (err_init2 or err_init).strip(),
                    code="git_init_error",
                )
            packager._run_git_text(dest_dir, ["symbolic-ref", "HEAD", "refs/heads/%s" % branch_init])
        for rname, rurl in (git_meta.get("remotes") or {}).items():
            packager._run_git_text(dest_dir, ["remote", "add", rname, rurl])
        if git_meta.get("hasBundle"):
            bundle_path = _verify_and_read_blob_path(
                tx_dir, str(git_meta["bundleBlob"]), str(git_meta["bundleSha256"])
            )
            _unpack_bundle_objects(dest_dir, str(bundle_path))
        for oid, rel_blob in (git_meta.get("stagedBlobs") or {}).items():
            _write_git_blob_object(dest_dir, tx_dir / rel_blob, oid)
        for ref, spec in sorted((git_meta.get("affectedRefs") or {}).items()):
            if spec.get("postOid"):
                packager._run_git_text(dest_dir, ["update-ref", ref, spec["postOid"]])
        _cleanup_temp_git_refs(dest_dir)
        _apply_git_head(dest_dir, git_meta.get("postHead") or {})
        if git_meta.get("hasPostIndex"):
            _swap_git_index(dest_dir, tx_dir / str(git_meta["postIndexBlob"]))
        packager._run_git_text(dest_dir, ["update-index", "-q", "--refresh"])


def _check_symlink_blob(rel_p: str, blob_path: Path) -> None:
    with open(str(blob_path), "rb") as fh:
        target = fh.read().decode("utf-8", errors="replace")
    validation.check_symlink_target(rel_p, target, field="workspace")


def _precheck_post_state(work_dir: str, post_files: Dict[str, Dict[str, Any]],
                         target_abs: str, file_ops: List[Dict[str, str]]) -> None:
    """Refuse, before the journal exists, any post state that would write outside the target.

    Symlinks in the incoming state must stay inside the project (the sender
    enforces the same rule), and no file operation may pass through a symlinked
    directory already present in the target, such as a gitignored link.
    """
    for rel_p, info in post_files.items():
        if info.get("kind") == "symlink":
            link = os.readlink(os.path.join(work_dir, *rel_p.split("/")))
            validation.check_symlink_target(rel_p, link, field="workspace")
    for item in file_ops:
        _target_join(target_abs, item["path"])


def _preserved_collisions(preserved: Dict[str, Dict[str, Any]], post_files: Dict[str, Dict[str, Any]],
                          case_insensitive: bool) -> List[str]:
    """Preserved entries (secrets, gitignored files) that the incoming state would write over.

    Stage B never touches preserved entries, so a post-state file at the same path,
    a post-state directory where a preserved file sits, or a post-state file where a
    preserved file's parent directory sits would overwrite it or fail mid-apply, and
    a rollback could not restore it. Such a receive is refused before the journal.
    """
    def key(rel: str) -> str:
        rel = packager._nfc(rel)
        return rel.casefold() if case_insensitive else rel

    post_keys = {key(rel) for rel in post_files}
    post_dirs = set()
    for k in post_keys:
        parts = k.split("/")
        for i in range(1, len(parts)):
            post_dirs.add("/".join(parts[:i]))
    reasons: List[str] = []
    for rel in sorted(preserved):
        k = key(rel)
        parts = k.split("/")
        if k in post_keys:
            reasons.append("交接包会覆盖本机保留文件：%s" % rel)
        elif k in post_dirs:
            reasons.append("交接包需要在本机保留文件处建立目录：%s" % rel)
        elif any("/".join(parts[:i]) in post_keys for i in range(1, len(parts))):
            reasons.append("交接包文件会替换含本机保留文件的目录：%s" % rel)
    return reasons


def _unignored_after_apply(work_dir: str, target_abs: str, gitignored: Set[str],
                           preserved: Dict[str, Dict[str, Any]]) -> List[str]:
    """Gitignored local files that the incoming ignore rules would expose as untracked files.

    They would stay in place, but the next send would package them. `work_dir` holds
    the incoming state; preserved `.gitignore` files are copied in first because the
    target keeps them. Secret files are never packaged, so they are not checked.
    """
    check = sorted(p for p in gitignored if p in preserved and not packager._is_secret_filename(p))
    if not check or not packager._is_git_repo_root(work_dir):
        return []
    for rel in check:
        if rel.rsplit("/", 1)[-1] == ".gitignore" and preserved[rel].get("kind") == "file":
            dest = _target_join(work_dir, rel)
            if not os.path.lexists(dest):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(_target_join(target_abs, rel), dest)
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-C", work_dir, "check-ignore", "--stdin", "-z"],
            input=b"".join(p.encode("utf-8", errors="surrogateescape") + b"\x00" for p in check),
            capture_output=True,
        )
    except OSError as e:
        return ["无法检查交接包的忽略规则：%s" % e]
    if proc.returncode not in (0, 1):
        return ["无法检查交接包的忽略规则：%s" % proc.stderr.decode("utf-8", errors="replace").strip()]
    still = {packager._nfc(p.replace("\\", "/")) for p in packager._parse_nul_paths(proc.stdout)}
    return ["交接包的忽略规则不再忽略本机文件：%s" % p for p in check if p not in still]


def prepare_and_apply_workspace(
    local_home: Path,
    relay_id: str,
    project_id: str,
    package_sha256: str,
    staging_dir: str,
    target_path: str,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Prepare isolated workspace transaction journal, then commit into `target_path`."""
    rid = _validate_relay_id(relay_id)
    local_home_p = Path(local_home)
    staging_abs = os.path.abspath(os.fspath(staging_dir))
    target_abs = os.path.abspath(os.fspath(target_path))
    tx_dir = _tx_dir(local_home_p, rid)

    if os.path.lexists(str(tx_dir)):
        existing = load_and_verify_journal(local_home_p, rid)
        if existing.get("phase") == PHASE_ROLLED_BACK:
            _rmtree_robust(tx_dir)
        else:
            raise WorkspaceTxError(
                "交接事务已存在（phase=%s），请先恢复或回滚：%s" % (existing.get("phase"), rid),
                code="transaction_exists",
            )

    manifest_path = os.path.join(staging_abs, "payload_manifest.json")
    if not os.path.isfile(manifest_path):
        raise WorkspaceTxError("暂存目录缺少 payload_manifest.json：%s" % staging_abs, code="missing_manifest")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    mode = manifest.get("mode")
    if mode not in ("git", "folder"):
        raise WorkspaceTxError("未知的交接模式：%r" % (mode,), code="invalid_mode")

    tx_root = _transactions_root(local_home_p)
    validation.ensure_contained(local_home_p, tx_root, check_final=True, field="transactions")
    tx_root.mkdir(parents=True, exist_ok=True)
    prep_dir = Path(tempfile.mkdtemp(dir=str(tx_root), prefix=".preparing-%s-" % uuid.uuid4().hex[:8]))

    try:
        (prep_dir / "pre_blobs").mkdir(parents=True, exist_ok=True)
        (prep_dir / "post_blobs").mkdir(parents=True, exist_ok=True)

        now = _now_iso()
        meta_obj = {
            "relayId": rid,
            "projectId": project_id,
            "packageSha256": package_sha256,
            "targetPath": target_abs,
            "createdAt": now,
            "extra": dict(extra_meta or {}),
        }
        _atomic_write_json(prep_dir / "meta.json", meta_obj, mode=0o600)

        # ---- Case 1: New non-existent target directory ----
        if not os.path.lexists(target_abs):
            parent_dir = Path(target_abs).parent
            parent_dir.mkdir(parents=True, exist_ok=True)
            sibling_dir = tempfile.mkdtemp(
                dir=str(parent_dir),
                prefix=".relay-tx-%s-" % uuid.uuid4().hex[:8],
            )
            try:
                try:
                    os.chmod(sibling_dir, 0o755)
                except OSError:
                    pass
                apply_res = packager.apply_verified_payload(staging_abs, sibling_dir)
                apply_res["targetPath"] = target_abs

                post_files_raw, _ = _scan_worktree_entries(sibling_dir)
                post_files: Dict[str, Dict[str, Any]] = {}
                for rel_p, info in post_files_raw.items():
                    info_copy = dict(info)
                    info_copy["blob"] = _store_entry_blob(
                        prep_dir,
                        "post_blobs",
                        os.path.join(sibling_dir, *rel_p.split("/")),
                        info["kind"],
                        info["sha256"],
                    )
                    post_files[rel_p] = info_copy

                git_meta: Dict[str, Any] = {}
                if mode == "git":
                    git_meta = _capture_git_post_state(
                        prep_dir,
                        staging_abs,
                        manifest,
                        sibling_dir,
                        was_git_repo=False,
                        pre_refs={},
                        pre_head={},
                        pre_index_entries={},
                        has_pre_index=False,
                    )

                file_ops = _compute_file_ops({}, post_files)
                _precheck_post_state(sibling_dir, post_files, sibling_dir, file_ops)
                journal: Dict[str, Any] = {
                    "formatVersion": JOURNAL_FORMAT_VERSION,
                    "relayId": rid,
                    "projectId": project_id,
                    "packageSha256": package_sha256,
                    "targetPath": target_abs,
                    "mode": mode,
                    "isNewDirectory": True,
                    "stagedDir": sibling_dir,
                    "stagedDirectoryIdentity": [os.stat(sibling_dir).st_dev, os.stat(sibling_dir).st_ino],
                    "phase": PHASE_PREPARED,
                    "createdAt": now,
                    "updatedAt": now,
                    "preservedEntries": {},
                    "preFiles": {},
                    "postFiles": post_files,
                    "fileOps": file_ops,
                    "git": git_meta,
                    "applyResult": apply_res,
                }
                _write_journal_atomic(prep_dir, journal)
                os.replace(str(prep_dir), str(tx_dir))
            except BaseException:
                _rmtree_robust(Path(sibling_dir))
                raise

            maybe_inject_fault("tx_after_journal_before_apply")

            if os.path.lexists(target_abs):
                raise WorkspaceTxError("接收期间目标路径已被创建：%s" % target_abs, code="target_exists")
            os.replace(sibling_dir, target_abs)
            maybe_inject_fault("tx_after_new_directory_move")

            journal = load_and_verify_journal(local_home_p, rid)
            journal["stagedDir"] = ""
            journal["phase"] = PHASE_WORKSPACE_COMMITTED
            journal["updatedAt"] = _now_iso()
            _write_journal_atomic(tx_dir, journal)

            maybe_inject_fault("tx_after_workspace_commit")
            return dict(journal["applyResult"])

        # ---- Case 2: Existing target directory ----
        if os.path.islink(target_abs) or not os.path.isdir(target_abs):
            raise WorkspaceTxError("目标路径已存在且不是目录：%s" % target_abs, code="invalid_target")

        was_git_repo = packager._is_git_repo_root(target_abs)
        gitignored_paths = _collect_gitignored_paths(target_abs) if was_git_repo else set()
        pre_files_raw, preserved_entries = _scan_worktree_entries(target_abs, ignored_rel_paths=gitignored_paths)

        pre_refs: Dict[str, str] = {}
        pre_head: Dict[str, Any] = {}
        pre_index_entries: Dict[str, List[str]] = {}
        has_pre_index = False
        if mode == "git" and was_git_repo:
            pre_refs = _read_git_refs(target_abs)
            pre_head = _read_git_head(target_abs)
            pre_index_entries = _read_git_index_entries(target_abs)
            src_index = os.path.join(target_abs, ".git", "index")
            if os.path.isfile(src_index):
                with open(src_index, "rb") as fh:
                    pre_idx_bytes = fh.read()
                _atomic_write_bytes(prep_dir / "pre_blobs" / "git_index.bin", pre_idx_bytes, mode=0o600)
                has_pre_index = True

        tmp_iso = tempfile.mkdtemp(prefix=".relay-tx-iso-")
        try:
            isolated_dir = os.path.join(tmp_iso, "work")
            os.makedirs(isolated_dir, exist_ok=True)
            if mode == "git" and os.path.isdir(os.path.join(target_abs, ".git")):
                shutil.copytree(
                    os.path.join(target_abs, ".git"),
                    os.path.join(isolated_dir, ".git"),
                    symlinks=True,
                )

            apply_res = packager.apply_verified_payload(staging_abs, isolated_dir)
            apply_res["targetPath"] = target_abs

            post_files_raw, _ = _scan_worktree_entries(isolated_dir)

            pre_files: Dict[str, Dict[str, Any]] = {}
            for rel_p, info in pre_files_raw.items():
                info_copy = dict(info)
                post_counterpart = post_files_raw.get(rel_p)
                # Only persist pre_blobs for files that will be modified or deleted.
                if post_counterpart is None or _entry_sig(info) != _entry_sig(post_counterpart):
                    info_copy["blob"] = _store_entry_blob(
                        prep_dir,
                        "pre_blobs",
                        os.path.join(target_abs, *rel_p.split("/")),
                        info["kind"],
                        info["sha256"],
                    )
                pre_files[rel_p] = info_copy

            post_files: Dict[str, Dict[str, Any]] = {}
            for rel_p, info in post_files_raw.items():
                info_copy = dict(info)
                pre_counterpart = pre_files_raw.get(rel_p)
                # Only persist post_blobs for files that will be created or modified.
                if pre_counterpart is None or _entry_sig(info) != _entry_sig(pre_counterpart):
                    info_copy["blob"] = _store_entry_blob(
                        prep_dir,
                        "post_blobs",
                        os.path.join(isolated_dir, *rel_p.split("/")),
                        info["kind"],
                        info["sha256"],
                    )
                post_files[rel_p] = info_copy

            git_meta = {}
            if mode == "git":
                git_meta = _capture_git_post_state(
                    prep_dir,
                    staging_abs,
                    manifest,
                    isolated_dir,
                    was_git_repo=was_git_repo,
                    pre_refs=pre_refs,
                    pre_head=pre_head,
                    pre_index_entries=pre_index_entries,
                    has_pre_index=has_pre_index,
                )

            file_ops = _compute_file_ops(pre_files, post_files)
            _precheck_post_state(isolated_dir, post_files, target_abs, file_ops)
            collisions = _preserved_collisions(
                preserved_entries, post_files, packager._is_fs_case_insensitive(target_abs),
            )
            if not collisions and mode == "git":
                collisions = _unignored_after_apply(isolated_dir, target_abs, gitignored_paths, preserved_entries)
            if collisions:
                summary = "；".join(collisions[:6])
                if len(collisions) > 6:
                    summary += "（等共 %d 处）" % len(collisions)
                raise packager.TargetDirtyError(
                    "目标目录「%s」中本机保留的 Git 忽略文件或凭据文件与交接包冲突（%s）。"
                    "已保留原目录完整不动；请先移走这些文件，或使用 --into <新目录> 接收。"
                    % (target_abs, summary),
                    target_path=target_abs,
                    reasons=collisions,
                )
            journal = {
                "formatVersion": JOURNAL_FORMAT_VERSION,
                "relayId": rid,
                "projectId": project_id,
                "packageSha256": package_sha256,
                "targetPath": target_abs,
                "mode": mode,
                "isNewDirectory": False,
                "stagedDir": "",
                "phase": PHASE_PREPARED,
                "createdAt": now,
                "updatedAt": now,
                "preservedEntries": preserved_entries,
                "preFiles": pre_files,
                "postFiles": post_files,
                "fileOps": file_ops,
                "git": git_meta,
                "applyResult": apply_res,
            }
            _write_journal_atomic(prep_dir, journal)
            os.replace(str(prep_dir), str(tx_dir))
        finally:
            _rmtree_robust(Path(tmp_iso))

    except BaseException:
        if os.path.lexists(str(prep_dir)):
            _rmtree_robust(prep_dir)
        raise

    # Target directory is untouched up to this point.
    maybe_inject_fault("tx_after_journal_before_apply")

    verified_journal = load_and_verify_journal(local_home_p, rid)
    return _apply_stage_b_existing(tx_dir, verified_journal)


def resume_transaction(local_home: Path, relay_id: str) -> Dict[str, Any]:
    """Resume an interrupted transaction deterministically, or fail on drift conflict."""
    rid = _validate_relay_id(relay_id)
    local_home_p = Path(local_home)
    journal = load_and_verify_journal(local_home_p, rid)
    info = inspect_transaction(local_home_p, rid)

    if info["state"] == "corrupted":
        raise CorruptedRecoveryError(info["reasons"][0] if info["reasons"] else "恢复日志已损坏。")
    if info["state"] == "conflict":
        raise RecoveryConflictError(
            _format_conflict_msg(info["targetPath"], info["reasons"]),
            target_path=info["targetPath"],
            reasons=info["reasons"],
            relay_id=rid,
        )
    if info["state"] == "rolled_back":
        raise WorkspaceTxError("该交接事务已回滚，不能再恢复提交。", code="already_rolled_back")
    if journal["phase"] == PHASE_ROLLING_BACK:
        raise WorkspaceTxError(
            "交接事务回滚尚未完成，请运行 relay recover %s --rollback 继续回滚，不能签收。" % rid,
            code="rollback_in_progress",
        )

    if journal["phase"] in _POST_COMMIT_PHASES or journal["phase"] == PHASE_COMPLETED:
        return dict(journal["applyResult"])

    tx_dir = _tx_dir(local_home_p, rid)
    if journal.get("isNewDirectory"):
        target_abs = str(journal["targetPath"])
        if not os.path.lexists(target_abs):
            staged_dir = str(journal.get("stagedDir") or "")
            if staged_dir and os.path.isdir(staged_dir):
                Path(target_abs).parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_dir, target_abs)
            else:
                _verify_post_blobs(tx_dir, journal)
                Path(target_abs).parent.mkdir(parents=True, exist_ok=True)
                tmp_sib = tempfile.mkdtemp(
                    dir=str(Path(target_abs).parent),
                    prefix=".relay-tx-resume-%s-" % uuid.uuid4().hex[:8],
                )
                try:
                    _materialize_new_directory_from_blobs(tx_dir, journal, tmp_sib)
                    os.replace(tmp_sib, target_abs)
                except BaseException:
                    _rmtree_robust(Path(tmp_sib))
                    raise
        journal["stagedDir"] = ""
        journal["phase"] = PHASE_WORKSPACE_COMMITTED
        journal["updatedAt"] = _now_iso()
        _write_journal_atomic(tx_dir, journal)
        maybe_inject_fault("tx_after_workspace_commit")
        return dict(journal["applyResult"])

    _verify_post_blobs(tx_dir, journal)
    return _apply_stage_b_existing(tx_dir, journal)


def rollback_transaction(local_home: Path, relay_id: str) -> Dict[str, Any]:
    """Safely roll back an incomplete or un-receipted transaction to pre-tx state."""
    rid = _validate_relay_id(relay_id)
    local_home_p = Path(local_home)
    journal = load_and_verify_journal(local_home_p, rid)
    info = inspect_transaction(local_home_p, rid)

    if info["state"] == "corrupted":
        raise CorruptedRecoveryError(info["reasons"][0] if info["reasons"] else "恢复日志已损坏。")
    if info["phase"] == PHASE_COMPLETED:
        raise WorkspaceTxError("该交接已完成签收，不能回滚。", code="already_completed")
    if info["phase"] in (PHASE_UPDATING_LEDGER, PHASE_LEDGER_UPDATED):
        raise WorkspaceTxError("接收已开始更新台账，请重试 receive 完成交接，不能回滚。", code="rollback_not_allowed")
    if info["phase"] == PHASE_ROLLED_BACK:
        return {
            "ok": True,
            "relayId": rid,
            "phase": PHASE_ROLLED_BACK,
            "targetPath": str(journal["targetPath"]),
            "alreadyRolledBack": True,
        }
    if info["state"] == "conflict" or not info["canRollback"]:
        raise RecoveryConflictError(
            _format_conflict_msg(info["targetPath"], info["reasons"]),
            target_path=info["targetPath"],
            reasons=info["reasons"],
            relay_id=rid,
        )

    tx_dir = _tx_dir(local_home_p, rid)
    target_abs = str(journal["targetPath"])
    mode = str(journal["mode"])

    # The rollback direction must survive an interruption before the first
    # restoration/deletion. A retry can then finish rollback, never sign a
    # receipt for a partially reverted workspace.
    journal["phase"] = PHASE_ROLLING_BACK
    journal.setdefault("writeStagingRel", ".relay-tx-write-" + uuid.uuid4().hex)
    journal["updatedAt"] = _now_iso()
    journal = _write_journal_atomic(tx_dir, journal)
    maybe_inject_fault("tx_before_rollback")

    if journal.get("isNewDirectory"):
        if os.path.lexists(target_abs):
            _rmtree_robust(Path(target_abs))
            if os.path.lexists(target_abs):
                raise WorkspaceTxError("回滚未能删除新建目标目录，请重试回滚。", code="rollback_in_progress")
        staged_dir = str(journal.get("stagedDir") or "")
        if staged_dir and os.path.lexists(staged_dir):
            _rmtree_robust(Path(staged_dir))
    else:
        _verify_pre_blobs(tx_dir, journal)
        pre_files: Dict[str, Dict[str, Any]] = journal["preFiles"]
        file_ops: List[Dict[str, str]] = journal["fileOps"]

        # Remove newly created files first so file<->dir replacements undo cleanly.
        for item in file_ops:
            if item["op"] == "create":
                dest_p = _target_join(target_abs, item["path"])
                if os.path.lexists(dest_p):
                    os.unlink(dest_p)
                    packager._prune_empty_parents(dest_p, target_abs)

        for item in file_ops:
            if item["op"] in ("delete", "modify"):
                rel_p = item["path"]
                spec = pre_files[rel_p]
                blob_path = _verify_and_read_blob_path(tx_dir, spec["blob"], spec["sha256"])
                _replace_workspace_file(
                    journal, blob_path,
                    _target_join(target_abs, rel_p),
                    spec["mode"],
                )
                maybe_inject_fault("tx_mid_rollback")

        if mode == "git":
            git_meta = journal.get("git") or {}
            if not git_meta.get("wasGitRepo"):
                dot_git = os.path.join(target_abs, ".git")
                if os.path.lexists(dot_git):
                    _rmtree_robust(Path(dot_git))
            elif packager._is_git_repo_root(target_abs):
                _cleanup_temp_git_refs(target_abs)
                for ref, spec in sorted((git_meta.get("affectedRefs") or {}).items()):
                    pre_oid = spec.get("preOid")
                    if pre_oid:
                        rc_u, _, err_u = packager._run_git_text(target_abs, ["update-ref", ref, pre_oid])
                        if rc_u != 0:
                            raise WorkspaceTxError(
                                "回滚 Git 引用失败（%s）：%s" % (ref, err_u.strip()),
                                code="git_ref_rollback_error",
                            )
                    else:
                        packager._run_git_text(target_abs, ["update-ref", "-d", ref])

                _apply_git_head(target_abs, git_meta.get("preHead") or {})

                if git_meta.get("hasPreIndex"):
                    _swap_git_index(target_abs, tx_dir / str(git_meta["preIndexBlob"]))
                else:
                    _swap_git_index(target_abs, None)
                packager._run_git_text(target_abs, ["update-index", "-q", "--refresh"])

    _cleanup_write_staging(journal)
    journal["stagedDir"] = ""
    journal["phase"] = PHASE_ROLLED_BACK
    journal["updatedAt"] = _now_iso()
    _write_journal_atomic(tx_dir, journal)

    return {
        "ok": True,
        "relayId": rid,
        "phase": PHASE_ROLLED_BACK,
        "targetPath": target_abs,
        "alreadyRolledBack": False,
    }


__all__ = [
    "JOURNAL_FORMAT_VERSION",
    "PHASE_PREPARED",
    "PHASE_APPLYING_FILES",
    "PHASE_APPLYING_GIT",
    "PHASE_WORKSPACE_COMMITTED",
    "PHASE_ARCHIVED",
    "PHASE_UPDATING_LEDGER",
    "PHASE_LEDGER_UPDATED",
    "PHASE_COMPLETED",
    "PHASE_ROLLING_BACK",
    "PHASE_ROLLED_BACK",
    "WorkspaceTxError",
    "CorruptedRecoveryError",
    "RecoveryConflictError",
    "maybe_inject_fault",
    "has_transaction",
    "list_transactions",
    "load_and_verify_journal",
    "load_transaction_meta",
    "inspect_transaction",
    "prepare_and_apply_workspace",
    "resume_transaction",
    "rollback_transaction",
    "mark_transaction_phase",
    "archive_completed_transaction",
]
