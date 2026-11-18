"""Project packaging, secret gate, cross-OS path safety, and clean restoration.

Supports:
- Git repositories (commits/branches via git bundle + exact staged index +
  exact unstaged working tree + eligible untracked files, including 0-commit
  unborn branches and detached HEAD)
- Non-Git folder snapshots (preserving relative paths and executable bits)
- Pre-packaging credential / private-key gate (excluding untracked `.env` and
  credentials without ever opening them; aborting if tracked/staged/history or
  packaged content leaks credentials or private keys)
- Cross-OS path validation, UTF-8 NFC normalization, Windows reserved-name
  checks, and case-folding collision detection
- Receiver local modification protection (`TargetDirtyError`) without mutating
  the target directory
"""
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


FORMAT_VERSION = 1

ALLOWED_ENV_TEMPLATES = frozenset((
    ".env.example",
    ".env.sample",
    ".env.template",
))

FORBIDDEN_EXACT_NAMES = frozenset((
    ".env",
    ".credentials.json",
    "auth.json",
    "oauth_creds.json",
    ".claude.json",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
))

FORBIDDEN_EXTENSIONS = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)

IGNORED_SYSTEM_NAMES = frozenset((
    ".ds_store",
))

WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%d" % i for i in range(1, 10)]
    + ["LPT%d" % i for i in range(1, 10)]
)

WINDOWS_INVALID_CHARS = set('<>:"|?*')

_PRIVATE_KEY_RE = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
_GIT_PRIVATE_KEY_REGEX = "-----BEGIN ([A-Z0-9 ]+ )?PRIVATE KEY-----"


# ---------------------------------------------------------------- exceptions


class PackageError(Exception):
    """Base error for packaging, verification, path safety, and unpacking."""


class SecretLeakError(PackageError):
    """Raised when forbidden credentials or private keys are detected before pack."""

    def __init__(
        self,
        message: Optional[str] = None,
        findings: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.findings = list(findings or [])
        if not message:
            message = _format_secret_findings(self.findings)
        super().__init__(message)


class TargetDirtyError(PackageError):
    """Raised when the receive target directory has local unsynced changes."""

    def __init__(
        self,
        message: str,
        target_path: str = "",
        reasons: Optional[List[str]] = None,
    ) -> None:
        super().__init__(message)
        self.target_path = target_path
        self.reasons = list(reasons or [])


class PathConflictError(PackageError):
    """Raised on unsafe relative paths, Windows path errors, or case collisions."""

    def __init__(
        self,
        message: Optional[str] = None,
        conflicts: Optional[List[Any]] = None,
    ) -> None:
        self.conflicts = list(conflicts or [])
        if not message:
            message = _format_path_conflicts(self.conflicts)
        super().__init__(message)


def _format_secret_findings(findings: List[Dict[str, Any]]) -> str:
    if not findings:
        return "打包前机密门禁拦截：检测到凭据、私钥或 .env 泄露风险，已中止打包。"
    lines = ["打包前机密门禁拦截：检测到受保护的凭据、私钥或 .env 文件，已中止打包："]
    for item in findings[:20]:
        path = item.get("path") or ""
        commit = item.get("commit") or ""
        detail = item.get("detail") or item.get("kind") or "敏感内容"
        if commit:
            lines.append("  - [commit %s] %s (%s)" % (commit[:12], path, detail))
        else:
            lines.append("  - %s (%s)" % (path, detail))
    if len(findings) > 20:
        lines.append("  - ... 其余 %d 项已省略" % (len(findings) - 20))
    return "\n".join(lines)


def _format_path_conflicts(conflicts: List[Any]) -> str:
    if not conflicts:
        return "检测到跨系统路径冲突或不安全路径。"
    lines = ["检测到跨系统路径冲突或不合法文件名："]
    for item in conflicts[:20]:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            lines.append("  - %s <-> %s" % (item[0], item[1]))
        elif isinstance(item, dict):
            lines.append("  - %s" % (item.get("detail") or json.dumps(item, ensure_ascii=False)))
        else:
            lines.append("  - %s" % (item,))
    if len(conflicts) > 20:
        lines.append("  - ... 其余 %d 项已省略" % (len(conflicts) - 20))
    return "\n".join(lines)


# ---------------------------------------------------------------- path & secret helpers


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _normalize_rel_path(rel_path: str) -> str:
    """Validate and normalize a relative archive/project path to UTF-8 NFC with '/'."""
    if not isinstance(rel_path, str) or not rel_path:
        raise PathConflictError("检测到空相对路径。", conflicts=[{"path": rel_path, "detail": "空路径"}])
    if "\x00" in rel_path:
        raise PathConflictError(
            "路径包含非法空字节：%r" % (rel_path,),
            conflicts=[{"path": rel_path, "detail": "包含空字节"}],
        )
    unified = _nfc(rel_path.replace("\\", "/"))
    if unified.startswith("/") or re.match(r"^[A-Za-z]:", unified):
        raise PathConflictError(
            "不允许使用绝对路径：%s" % unified,
            conflicts=[{"path": unified, "detail": "绝对路径"}],
        )
    parts = unified.split("/")
    cleaned = []
    for seg in parts:
        if seg == "" or seg == ".":
            raise PathConflictError(
                "路径包含非法空段或当前目录段：%s" % unified,
                conflicts=[{"path": unified, "detail": "非法路径段"}],
            )
        if seg == "..":
            raise PathConflictError(
                "不允许路径穿越（..）：%s" % unified,
                conflicts=[{"path": unified, "detail": "路径穿越 .."}],
            )
        cleaned.append(seg)
    return "/".join(cleaned)


def _is_ignored_system_path(rel_path: str) -> bool:
    norm = _nfc(rel_path.replace("\\", "/")).strip("/")
    if not norm:
        return True
    parts = norm.split("/")
    for seg in parts:
        sl = seg.lower()
        if sl == ".git" or sl in IGNORED_SYSTEM_NAMES:
            return True
    return False


def _is_secret_filename(rel_path: str) -> bool:
    """Return True if basename matches excluded/forbidden credential or private-key names."""
    norm = _nfc(rel_path.replace("\\", "/")).strip("/")
    if not norm:
        return False
    base = norm.split("/")[-1].lower()
    if base in ALLOWED_ENV_TEMPLATES:
        return False
    if base in FORBIDDEN_EXACT_NAMES:
        return True
    if base.startswith(".env."):
        return True
    if base.endswith(FORBIDDEN_EXTENSIONS):
        return True
    return False


def _find_case_collisions(rel_paths: Iterable[str]) -> List[Tuple[str, str]]:
    """Return sorted list of (p1, p2) pairs that collide under Unicode casefold.

    Includes both file-vs-file collisions (e.g. `Foo.txt` vs `foo.txt`) and
    directory-prefix collisions (e.g. `Dir/a.txt` vs `dir/b.txt`).
    """
    unique = sorted(set(_normalize_rel_path(p) for p in rel_paths))
    by_full_fold: Dict[str, List[str]] = {}
    for p in unique:
        by_full_fold.setdefault(p.casefold(), []).append(p)

    pairs: Set[Tuple[str, str]] = set()
    for group in by_full_fold.values():
        if len(group) > 1:
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    pairs.add((group[i], group[j]))

    # Also catch directory component case collisions like `Sub/a.txt` vs `sub/b.txt`
    dir_seen: Dict[str, Tuple[str, str]] = {}
    for p in unique:
        segs = p.split("/")
        for depth in range(1, len(segs)):
            prefix = "/".join(segs[:depth])
            folded = prefix.casefold()
            if folded in dir_seen:
                orig_prefix, orig_path = dir_seen[folded]
                if orig_prefix != prefix:
                    pair = (orig_path, p) if orig_path < p else (p, orig_path)
                    pairs.add(pair)
            else:
                dir_seen[folded] = (prefix, p)

    # Also catch file vs directory collisions (`foo` file vs `FOO/bar.txt` or `foo/bar.txt`)
    file_folds: Dict[str, str] = {p.casefold(): p for p in unique}
    for p in unique:
        segs = p.split("/")
        for depth in range(1, len(segs)):
            folded_dir = "/".join(segs[:depth]).casefold()
            if folded_dir in file_folds:
                other = file_folds[folded_dir]
                if other != p:
                    pair = (other, p) if other < p else (p, other)
                    pairs.add(pair)

    return sorted(pairs)


def _find_windows_path_issues(rel_paths: Iterable[str]) -> List[Tuple[str, str]]:
    issues: List[Tuple[str, str]] = []
    for raw in sorted(set(_normalize_rel_path(p) for p in rel_paths)):
        for seg in raw.split("/"):
            if any(ch in WINDOWS_INVALID_CHARS or ord(ch) < 32 for ch in seg):
                issues.append((raw, "包含 Windows 非法字符（<>:\"|?* 或控制字符）"))
                break
            if seg.endswith(" ") or seg.endswith("."):
                issues.append((raw, "路径段不能以空格或句点结尾（Windows 限制）"))
                break
            stem = seg.split(".", 1)[0].upper()
            if stem in WINDOWS_RESERVED_NAMES:
                issues.append((raw, "使用了 Windows 保留设备名：%s" % stem))
                break
    return issues


def _is_fs_case_insensitive(target_path: str) -> bool:
    """Dynamically probe whether target_path's filesystem is case-insensitive without mutating target_path."""
    if os.name == "nt":
        return True

    abs_target = os.path.abspath(target_path)
    # Probe in the nearest existing ancestor directory OUTSIDE target_path if possible,
    # so target_path itself is never touched.
    probe_dir = Path(abs_target).parent
    while not probe_dir.exists() and probe_dir != probe_dir.parent:
        probe_dir = probe_dir.parent
    if not probe_dir.exists():
        return False

    # 1. Read-only inode probe using any existing entry with ASCII letters
    try:
        for name in os.listdir(str(probe_dir)):
            swapped = name.swapcase()
            if swapped != name:
                p1 = probe_dir / name
                p2 = probe_dir / swapped
                try:
                    st1 = os.lstat(str(p1))
                    st2 = os.lstat(str(p2))
                except FileNotFoundError:
                    return False
                except OSError:
                    continue
                if st1.st_dev == st2.st_dev and st1.st_ino == st2.st_ino:
                    return True
                # If swapped happens to exist as a distinct inode, filesystem is case-sensitive!
                if st1.st_dev == st2.st_dev and st1.st_ino != st2.st_ino:
                    return False
    except OSError:
        pass

    # 2. Check probe_dir's own basename in its parent if on same device
    if probe_dir.name.swapcase() != probe_dir.name and probe_dir.parent.exists():
        try:
            st1 = os.lstat(str(probe_dir))
            st2 = os.lstat(str(probe_dir.parent / probe_dir.name.swapcase()))
            if st1.st_dev == st2.st_dev:
                return st1.st_ino == st2.st_ino
        except FileNotFoundError:
            return False
        except OSError:
            pass

    # 3. Fallback ephemeral probe file in probe_dir (outside target_path)
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(probe_dir), prefix=".relaycase_", suffix="a")
        os.close(fd)
        fd = None
        alt_path = tmp_path[:-1] + "A"
        st1 = os.lstat(tmp_path)
        try:
            st2 = os.lstat(alt_path)
        except FileNotFoundError:
            return False
        return st1.st_dev == st2.st_dev and st1.st_ino == st2.st_ino
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _file_has_private_key(file_path: str) -> bool:
    """Scan a regular non-excluded file on disk for PEM/OpenSSH private key headers."""
    overlap = 64
    tail = b""
    try:
        with open(file_path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                buf = tail + chunk
                if _PRIVATE_KEY_RE.search(buf):
                    return True
                tail = buf[-overlap:]
    except OSError as e:
        raise PackageError("无法读取待打包文件 %s：%s" % (file_path, e))
    return False


def _sha256_file(file_path: str) -> Tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with open(file_path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


def _ensure_symlink_safe(full_path: str, real_project_root: str, rel_display: str) -> str:
    """Validate that full_path (if a symlink) stays strictly inside real_project_root."""
    resolved = os.path.realpath(full_path)
    root_prefix = real_project_root.rstrip(os.sep) + os.sep
    if resolved != real_project_root and not resolved.startswith(root_prefix):
        raise PathConflictError(
            "检测到逃逸出项目目录的符号链接：%s -> %s" % (rel_display, resolved),
            conflicts=[{"path": rel_display, "target": resolved, "detail": "符号链接逃逸"}],
        )
    return resolved


# ---------------------------------------------------------------- git helpers


def _run_git_text(cwd: str, args: List[str]) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-c", "core.autocrlf=false", "-C", cwd] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        raise PackageError("无法调用 git 命令：%s" % e)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _run_git_bytes(cwd: str, args: List[str]) -> Tuple[int, bytes, bytes]:
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-c", "core.autocrlf=false", "-C", cwd] + args,
            capture_output=True,
        )
    except OSError as e:
        raise PackageError("无法调用 git 命令：%s" % e)
    return proc.returncode, proc.stdout or b"", proc.stderr or b""


def _is_git_repo_root(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    dot_git = os.path.join(path, ".git")
    if not os.path.lexists(dot_git):
        return False
    rc, out, _ = _run_git_text(path, ["rev-parse", "--show-toplevel"])
    if rc != 0 or not out.strip():
        return False
    return os.path.realpath(out.strip()) == os.path.realpath(path)


def _git_has_commits(repo_path: str) -> Tuple[bool, str]:
    rc, out, _ = _run_git_text(repo_path, ["rev-parse", "--verify", "HEAD"])
    if rc == 0 and out.strip():
        return True, out.strip()
    return False, ""


def _git_branch_info(repo_path: str) -> Tuple[str, str]:
    """Return (branch_short_name, symbolic_head_ref)."""
    rc, out, _ = _run_git_text(repo_path, ["symbolic-ref", "-q", "HEAD"])
    if rc == 0 and out.strip():
        ref = out.strip()
        short = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        return short, ref
    return "", ""


def _parse_nul_paths(raw: bytes) -> List[str]:
    if not raw:
        return []
    parts = raw.split(b"\x00")
    out = []
    for item in parts:
        if item:
            out.append(item.decode("utf-8", errors="surrogateescape"))
    return out


def _parse_git_raw_diff_z(raw: bytes) -> List[Dict[str, str]]:
    """Parse `git diff [--cached] --full-index --no-renames --raw -z` output."""
    if not raw:
        return []
    tokens = raw.split(b"\x00")
    entries: List[Dict[str, str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        header = tok.decode("utf-8", errors="replace")
        if not header.startswith(":"):
            i += 1
            continue
        if i + 1 >= len(tokens):
            break
        raw_path = tokens[i + 1].decode("utf-8", errors="surrogateescape")
        i += 2
        # Header format: :<src_mode> <dst_mode> <src_oid> <dst_oid> <status>
        fields = header[1:].split(" ")
        if len(fields) < 5:
            continue
        src_mode, dst_mode, src_oid, dst_oid, status = fields[:5]
        status_code = status[0]
        if status_code in ("R", "C") and i < len(tokens):
            raw_path = tokens[i].decode("utf-8", errors="surrogateescape")
            i += 1
        entries.append({
            "srcMode": src_mode,
            "dstMode": dst_mode,
            "srcOid": src_oid,
            "dstOid": dst_oid,
            "status": status_code,
            "rawPath": raw_path,
        })
    return entries


def _compute_git_dirty_baseline(repo_path: str) -> Dict[str, str]:
    """Compute a deterministic fingerprint map of staged, unstaged, and untracked files in a Git repo.

    Allows `check_target_clean_for_receive` to distinguish unchanged baseline dirty state
    (left over from when this device itself sent or received the previous relay) from
    any genuine post-relay local modification.
    """
    baseline: Dict[str, str] = {}

    rc_s, raw_s, _ = _run_git_bytes(
        repo_path,
        ["diff", "--cached", "--full-index", "--no-renames", "--raw", "-z"],
    )
    if rc_s == 0 and raw_s:
        for item in _parse_git_raw_diff_z(raw_s):
            norm_p = _nfc(item["rawPath"].replace("\\", "/"))
            if _is_ignored_system_path(norm_p) or _is_secret_filename(norm_p):
                continue
            if item["status"] == "D":
                baseline["staged:" + norm_p] = "D"
            else:
                baseline["staged:" + norm_p] = "%s:%s:%s" % (
                    item["status"],
                    item["dstMode"],
                    item["dstOid"],
                )

    rc_u, raw_u, _ = _run_git_bytes(
        repo_path,
        ["diff", "--full-index", "--no-renames", "--raw", "-z"],
    )
    if rc_u == 0 and raw_u:
        for item in _parse_git_raw_diff_z(raw_u):
            norm_p = _nfc(item["rawPath"].replace("\\", "/"))
            if _is_ignored_system_path(norm_p) or _is_secret_filename(norm_p):
                continue
            full_p = os.path.join(repo_path, item["rawPath"])
            if item["status"] == "D" or not os.path.lexists(full_p):
                baseline["unstaged:" + norm_p] = "D"
            elif os.path.islink(full_p):
                sym_sha = hashlib.sha256(os.readlink(full_p).encode("utf-8")).hexdigest()
                baseline["unstaged:" + norm_p] = "%s:120000:%s" % (item["status"], sym_sha)
            elif os.path.isfile(full_p):
                sha, _ = _sha256_file(full_p)
                baseline["unstaged:" + norm_p] = "%s:%s:%s" % (
                    item["status"],
                    _file_git_mode(full_p),
                    sha,
                )

    rc_o, raw_o, _ = _run_git_bytes(
        repo_path,
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    if rc_o == 0 and raw_o:
        for raw_p in _parse_nul_paths(raw_o):
            norm_p = _nfc(raw_p.replace("\\", "/"))
            if _is_ignored_system_path(norm_p) or _is_secret_filename(norm_p):
                continue
            full_p = os.path.join(repo_path, raw_p)
            if os.path.islink(full_p):
                sym_sha = hashlib.sha256(os.readlink(full_p).encode("utf-8")).hexdigest()
                baseline["untracked:" + norm_p] = "120000:%s" % sym_sha
            elif os.path.isfile(full_p):
                sha, _ = _sha256_file(full_p)
                baseline["untracked:" + norm_p] = "%s:%s" % (_file_git_mode(full_p), sha)

    return baseline


# ---------------------------------------------------------------- 1. Secret Gate


def scan_secrets_before_pack(project_path: str, is_git: Optional[bool] = None) -> Dict[str, Any]:
    """Check file manifest and Git history before packaging.

    Automatically excludes untracked / folder-snapshot credential and `.env`
    files without ever opening them. Raises `SecretLeakError` if any tracked/
    staged/committed file has a forbidden credential filename or if any
    packaged file / commit / staged blob contains a private key block.
    """
    if not project_path or not os.path.isdir(project_path):
        raise PackageError("项目目录不存在或不是目录：%s" % project_path)

    real_root = os.path.realpath(project_path)
    git_mode = _is_git_repo_root(project_path) if is_git is None else bool(is_git)

    excluded: Set[str] = set()
    findings: List[Dict[str, Any]] = []
    seen_finding_keys: Set[Tuple[str, str, str]] = set()

    def _add_finding(kind: str, scope: str, path: str, commit: str, detail: str) -> None:
        key = (kind, path, commit)
        if key not in seen_finding_keys:
            seen_finding_keys.add(key)
            findings.append({
                "kind": kind,
                "scope": scope,
                "path": path,
                "commit": commit,
                "detail": detail,
            })

    if git_mode:
        # 1. Check all tracked / staged index filenames (`git ls-files --stage -z`)
        rc, raw_ls, err_ls = _run_git_bytes(project_path, ["ls-files", "--stage", "-z"])
        if rc != 0:
            raise PackageError("读取 Git 索引失败：%s" % err_ls.decode("utf-8", errors="replace").strip())

        for rec in raw_ls.split(b"\x00"):
            if not rec:
                continue
            tab_idx = rec.find(b"\t")
            if tab_idx < 0:
                continue
            raw_path = rec[tab_idx + 1:].decode("utf-8", errors="surrogateescape")
            rel = _normalize_rel_path(raw_path)
            if _is_secret_filename(rel):
                _add_finding(
                    "forbidden_file",
                    "index",
                    rel,
                    "",
                    "Git 暂存区或已跟踪文件中包含敏感凭据文件：%s" % rel,
                )

        # 2. Check staged/tracked index blobs for private key headers (never opens local .env)
        rc_grep, raw_grep, _ = _run_git_bytes(
            project_path,
            ["grep", "-a", "-l", "-z", "-E", "-e", _GIT_PRIVATE_KEY_REGEX, "--cached"],
        )
        if rc_grep == 0 and raw_grep:
            for raw_path in _parse_nul_paths(raw_grep):
                rel = _normalize_rel_path(raw_path)
                _add_finding(
                    "private_key",
                    "index",
                    rel,
                    "",
                    "Git 暂存区或已跟踪文件内容包含私钥块：%s" % rel,
                )

        # 3. Check working tree modified tracked files for private key headers
        rc_diff, raw_diff, _ = _run_git_bytes(
            project_path,
            ["diff", "--full-index", "--no-renames", "--raw", "-z"],
        )
        if rc_diff == 0 and raw_diff:
            for item in _parse_git_raw_diff_z(raw_diff):
                if item["status"] == "D":
                    continue
                rel = _normalize_rel_path(item["rawPath"])
                if _is_secret_filename(rel):
                    continue
                full_p = os.path.join(project_path, item["rawPath"])
                if os.path.islink(full_p):
                    target = _ensure_symlink_safe(full_p, real_root, rel)
                    target_rel = _nfc(os.path.relpath(target, real_root).replace("\\", "/"))
                    if _is_secret_filename(target_rel):
                        _add_finding(
                            "forbidden_file",
                            "worktree",
                            rel,
                            "",
                            "符号链接指向敏感凭据文件：%s" % rel,
                        )
                        continue
                if os.path.isfile(full_p) and not os.path.islink(full_p):
                    if _file_has_private_key(full_p):
                        _add_finding(
                            "private_key",
                            "worktree",
                            rel,
                            "",
                            "工作区未暂存改动包含私钥块：%s" % rel,
                        )

        # 4. Check untracked files: exclude secret filenames WITHOUT reading them; scan eligible untracked
        rc_un, raw_un, _ = _run_git_bytes(
            project_path,
            ["ls-files", "--others", "--exclude-standard", "-z"],
        )
        if rc_un == 0 and raw_un:
            for raw_path in _parse_nul_paths(raw_un):
                rel = _normalize_rel_path(raw_path)
                if _is_ignored_system_path(rel):
                    continue
                if _is_secret_filename(rel):
                    excluded.add(rel)
                    continue
                full_p = os.path.join(project_path, raw_path)
                if os.path.islink(full_p):
                    target = _ensure_symlink_safe(full_p, real_root, rel)
                    target_rel = _nfc(os.path.relpath(target, real_root).replace("\\", "/"))
                    if _is_secret_filename(target_rel):
                        excluded.add(rel)
                        continue
                if os.path.isfile(full_p):
                    if _file_has_private_key(full_p):
                        _add_finding(
                            "private_key",
                            "untracked",
                            rel,
                            "",
                            "待打包未跟踪文件包含私钥块：%s" % rel,
                        )

        # Also record gitignored secret files (like .env in .gitignore) in excludedFiles without opening them
        rc_ig, raw_ig, _ = _run_git_bytes(
            project_path,
            ["ls-files", "--others", "-i", "--exclude-standard", "--directory", "-z"],
        )
        if rc_ig == 0 and raw_ig:
            for raw_path in _parse_nul_paths(raw_ig):
                if raw_path.endswith("/"):
                    continue
                rel = _normalize_rel_path(raw_path)
                if _is_secret_filename(rel):
                    excluded.add(rel)

        # 5. Inspect Git commit history to be bundled (`git log --all`)
        has_commits, _ = _git_has_commits(project_path)
        if has_commits:
            # 5a. Forbidden credential filenames in commit history
            rc_log, out_log, _ = _run_git_text(
                project_path,
                ["log", "--all", "HEAD", "-m", "--root", "--format=COMMIT:%H", "--name-only"],
            )
            if rc_log == 0 and out_log:
                cur_commit = ""
                for line in out_log.splitlines():
                    s = line.strip("\r\n")
                    if not s:
                        continue
                    if s.startswith("COMMIT:") and len(s) == 47:
                        cur_commit = s[7:]
                        continue
                    rel = _nfc(s.replace("\\", "/")).lstrip("/")
                    if _is_secret_filename(rel):
                        _add_finding(
                            "forbidden_file",
                            "history",
                            rel,
                            cur_commit,
                            "Git 历史提交中包含敏感凭据文件：%s" % rel,
                        )

            # 5b. Private key headers in commit history (`-G` + `-S "PRIVATE KEY"`)
            for search_args in (
                ["log", "--all", "HEAD", "-m", "--root", "-a", "-G", _GIT_PRIVATE_KEY_REGEX, "--format=COMMIT:%H", "--name-only"],
                ["log", "--all", "HEAD", "-m", "--root", "-a", "-S", "PRIVATE KEY", "--format=COMMIT:%H", "--name-only"],
            ):
                rc_pk, out_pk, _ = _run_git_text(project_path, search_args)
                if rc_pk != 0 or not out_pk:
                    continue
                cur_commit = ""
                for line in out_pk.splitlines():
                    s = line.strip("\r\n")
                    if not s:
                        continue
                    if s.startswith("COMMIT:") and len(s) == 47:
                        cur_commit = s[7:]
                        continue
                    rel = _nfc(s.replace("\\", "/")).lstrip("/")
                    if search_args[6] == "-S":
                        # Verify actual blob in cur_commit matches _PRIVATE_KEY_RE (not just word "PRIVATE KEY")
                        rc_b, blob_bytes, _ = _run_git_bytes(
                            project_path,
                            ["show", "%s:%s" % (cur_commit, s)],
                        )
                        if rc_b != 0 or not _PRIVATE_KEY_RE.search(blob_bytes):
                            continue
                    _add_finding(
                        "private_key",
                        "history",
                        rel,
                        cur_commit,
                        "Git 历史提交中包含私钥内容：%s" % rel,
                    )
    else:
        # Folder snapshot mode
        for dirpath, dirnames, filenames in os.walk(project_path, followlinks=False):
            # Prune .git and check symlinked directories
            kept_dirs: List[str] = []
            for dname in sorted(dirnames):
                if dname.lower() == ".git" or dname.lower() in IGNORED_SYSTEM_NAMES:
                    continue
                full_d = os.path.join(dirpath, dname)
                rel_d = _normalize_rel_path(os.path.relpath(full_d, project_path))
                if os.path.islink(full_d):
                    _ensure_symlink_safe(full_d, real_root, rel_d)
                    continue
                kept_dirs.append(dname)
            dirnames[:] = kept_dirs

            for fname in sorted(filenames):
                full_f = os.path.join(dirpath, fname)
                rel_f = _normalize_rel_path(os.path.relpath(full_f, project_path))
                if _is_ignored_system_path(rel_f):
                    continue
                if _is_secret_filename(rel_f):
                    excluded.add(rel_f)
                    continue
                if os.path.islink(full_f):
                    target = _ensure_symlink_safe(full_f, real_root, rel_f)
                    target_rel = _nfc(os.path.relpath(target, real_root).replace("\\", "/"))
                    if _is_secret_filename(target_rel):
                        excluded.add(rel_f)
                        continue
                if os.path.isfile(full_f):
                    if _file_has_private_key(full_f):
                        _add_finding(
                            "private_key",
                            "folder",
                            rel_f,
                            "",
                            "待打包文件包含私钥块：%s" % rel_f,
                        )

    if findings:
        raise SecretLeakError(findings=findings)

    return {
        "clean": True,
        "excludedFiles": sorted(excluded),
    }


# ---------------------------------------------------------------- 2. Build Payload


def _validate_cross_os_paths(
    rel_paths: Iterable[str],
    target_os: Optional[str],
    check_case: bool,
    check_windows: bool,
) -> None:
    normalized = [_normalize_rel_path(p) for p in rel_paths]
    conflicts: List[Any] = []
    if check_case:
        for p1, p2 in _find_case_collisions(normalized):
            conflicts.append((p1, p2))
    if check_windows:
        for path, reason in _find_windows_path_issues(normalized):
            conflicts.append({"path": path, "detail": "%s: %s" % (path, reason)})
    if conflicts:
        raise PathConflictError(conflicts=conflicts)


def _file_git_mode(full_path: str) -> str:
    try:
        st = os.stat(full_path)
        if st.st_mode & stat.S_IXUSR:
            return "100755"
    except OSError:
        pass
    return "100644"


def _add_bytes_to_tar(tf: tarfile.TarFile, arcname: str, data: bytes) -> Dict[str, Any]:
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    tf.addfile(info, io.BytesIO(data))
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def _add_file_to_tar(tf: tarfile.TarFile, arcname: str, src_path: str) -> Dict[str, Any]:
    sha, size = _sha256_file(src_path)
    info = tarfile.TarInfo(name=arcname)
    info.size = size
    info.mtime = int(time.time())
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    with open(src_path, "rb") as fh:
        tf.addfile(info, fh)
    return {
        "sha256": sha,
        "size": size,
    }


def build_payload(
    project_path: str,
    out_tar_gz_path: str,
    metadata: dict,
    conversation_bundle: dict,
    target_os: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate secrets & paths, then atomically build a verified relay tar.gz."""
    if not project_path or not os.path.isdir(project_path):
        raise PackageError("项目目录不存在或不是目录：%s" % project_path)

    real_root = os.path.realpath(project_path)
    git_mode = _is_git_repo_root(project_path)
    secret_res = scan_secrets_before_pack(project_path, is_git=git_mode)
    excluded_files: List[str] = secret_res["excludedFiles"]

    target_os_norm = (target_os or "").strip().lower()
    if target_os_norm in ("darwin", "macos"):
        target_os_norm = "mac"
    elif target_os_norm in ("win32", "nt"):
        target_os_norm = "windows"

    check_case = target_os_norm in ("windows", "mac")
    check_windows = (target_os_norm == "windows") or (os.name == "nt")

    meta_out = dict(metadata or {})
    meta_out.setdefault("lastTask", "")
    meta_out.setdefault("nextAction", "")
    meta_out.setdefault("note", "")
    meta_out.setdefault("verify", {"state": "none", "cmd": "", "note": ""})
    handoff_file = os.path.join(project_path, "HANDOFF.md")
    if os.path.isfile(handoff_file) and not os.path.islink(handoff_file):
        if _file_has_private_key(handoff_file):
            raise SecretLeakError(findings=[{
                "kind": "private_key",
                "scope": "handoff",
                "path": "HANDOFF.md",
                "commit": "",
                "detail": "HANDOFF.md 包含私钥块",
            }])
        with open(handoff_file, "r", encoding="utf-8", errors="replace") as fh:
            handoff_text = fh.read()
        meta_out.setdefault("handoffContent", handoff_text)
        if not meta_out.get("handoffMd"):
            meta_out["handoffMd"] = handoff_text

    conv_out = dict(conversation_bundle or {})

    out_parent = Path(out_tar_gz_path).parent
    out_parent.mkdir(parents=True, exist_ok=True)

    archive_files: Dict[str, Dict[str, Any]] = {}
    staged_entries: List[Dict[str, Any]] = []
    unstaged_entries: List[Dict[str, Any]] = []
    untracked_entries: List[Dict[str, Any]] = []
    folder_entries: List[Dict[str, Any]] = []
    folder_hashes: Dict[str, str] = {}
    all_paths: Set[str] = set()
    nfc_origin_map: Dict[str, str] = {}

    def _register_path(raw_rel: str) -> str:
        raw_posix = raw_rel.replace("\\", "/")
        norm_rel = _normalize_rel_path(raw_posix)
        prev = nfc_origin_map.get(norm_rel)
        if prev is not None and prev != raw_posix:
            p_prev = os.path.join(project_path, *prev.split("/"))
            p_curr = os.path.join(project_path, *raw_posix.split("/"))
            same_inode = False
            if os.path.lexists(p_prev) and os.path.lexists(p_curr):
                try:
                    st_a = os.lstat(p_prev)
                    st_b = os.lstat(p_curr)
                    same_inode = (st_a.st_dev == st_b.st_dev and st_a.st_ino == st_b.st_ino)
                except OSError:
                    same_inode = False
            if not same_inode:
                raise PathConflictError(
                    "检测到 Unicode NFC 规范化同名冲突：%r <-> %r" % (prev, raw_posix),
                    conflicts=[(prev, raw_posix)],
                )
        nfc_origin_map[norm_rel] = raw_posix
        all_paths.add(norm_rel)
        return norm_rel

    mode_str = "git" if git_mode else "folder"
    branch = ""
    head_ref = ""
    head_commit = ""
    has_commits = False
    head_ancestors: List[str] = []
    refs_map: Dict[str, str] = {}
    remotes_map: Dict[str, str] = {}
    file_count = 0

    with tempfile.TemporaryDirectory() as build_tmp:
        fd, tmp_tar = tempfile.mkstemp(dir=str(out_parent), prefix=".payload-", suffix=".tar.gz.tmp")
        os.close(fd)
        try:
            with tarfile.open(tmp_tar, mode="w:gz") as tf:
                if git_mode:
                    # Ensure no unmerged merge conflicts exist in index
                    rc_u, raw_u, _ = _run_git_bytes(project_path, ["ls-files", "-u", "-z"])
                    if rc_u == 0 and raw_u.strip(b"\x00"):
                        raise PackageError("Git 仓库存在未解决的合并冲突，请先解决后再打包。")

                    has_commits, head_commit = _git_has_commits(project_path)
                    branch, head_ref = _git_branch_info(project_path)

                    # Collect local branches & tags + remotes metadata
                    rc_refs, out_refs, _ = _run_git_text(
                        project_path,
                        ["for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags"],
                    )
                    if rc_refs == 0 and out_refs:
                        for ln in out_refs.splitlines():
                            parts = ln.strip().split(" ", 1)
                            if len(parts) == 2:
                                refs_map[parts[1]] = parts[0]

                    rc_rem, out_rem, _ = _run_git_text(project_path, ["remote"])
                    if rc_rem == 0 and out_rem:
                        for rname in [x.strip() for x in out_rem.splitlines() if x.strip()]:
                            rc_u2, out_u2, _ = _run_git_text(project_path, ["remote", "get-url", rname])
                            if rc_u2 == 0 and out_u2.strip():
                                remotes_map[rname] = out_u2.strip()

                    # Collect all tracked/staged paths in index to validate case/Windows safety
                    rc_idx, raw_idx, _ = _run_git_bytes(project_path, ["ls-files", "--stage", "-z"])
                    if rc_idx != 0:
                        raise PackageError("无法读取 Git 索引。")
                    index_paths: Set[str] = set()
                    for rec in raw_idx.split(b"\x00"):
                        if not rec:
                            continue
                        tab_idx = rec.find(b"\t")
                        if tab_idx < 0:
                            continue
                        meta_part = rec[:tab_idx].decode("utf-8", errors="replace").split(" ")
                        raw_p = rec[tab_idx + 1:].decode("utf-8", errors="surrogateescape")
                        norm_p = _register_path(raw_p)
                        index_paths.add(norm_p)
                        if meta_part and meta_part[0] == "120000":
                            # Validate tracked/staged symlink does not escape repo root
                            rc_cat, sym_target_b, _ = _run_git_bytes(project_path, ["cat-file", "blob", meta_part[1]])
                            if rc_cat == 0:
                                sym_target = sym_target_b.decode("utf-8", errors="replace")
                                if os.path.isabs(sym_target):
                                    raise PathConflictError(
                                        "Git 符号链接指向绝对路径：%s -> %s" % (norm_p, sym_target),
                                        conflicts=[{"path": norm_p, "target": sym_target, "detail": "绝对符号链接"}],
                                    )
                                joined = os.path.normpath(os.path.join(real_root, os.path.dirname(norm_p), sym_target))
                                root_prefix = real_root.rstrip(os.sep) + os.sep
                                if joined != real_root and not joined.startswith(root_prefix):
                                    raise PathConflictError(
                                        "Git 符号链接逃逸出仓库目录：%s -> %s" % (norm_p, sym_target),
                                        conflicts=[{"path": norm_p, "target": sym_target, "detail": "符号链接逃逸"}],
                                    )

                    if has_commits:
                        rc_anc, out_anc, _ = _run_git_text(project_path, ["rev-list", head_commit])
                        if rc_anc == 0 and out_anc:
                            head_ancestors = [ln.strip() for ln in out_anc.splitlines() if ln.strip()]

                        bundle_tmp = os.path.join(build_tmp, "repo.bundle")
                        rc_b, _, err_b = _run_git_bytes(
                            project_path,
                            ["bundle", "create", bundle_tmp, "--all", "HEAD"],
                        )
                        if rc_b != 0 or not os.path.isfile(bundle_tmp):
                            raise PackageError(
                                "创建 Git bundle 失败：%s" % err_b.decode("utf-8", errors="replace").strip()
                            )
                        archive_files["repo.bundle"] = _add_file_to_tar(tf, "repo.bundle", bundle_tmp)

                    # Raw binary patches (`staged.patch` & `unstaged.patch`)
                    rc_sp, staged_patch_bytes, err_sp = _run_git_bytes(
                        project_path,
                        ["diff", "--cached", "--binary", "--full-index"],
                    )
                    if rc_sp != 0:
                        raise PackageError(
                            "读取 Git 暂存补丁失败：%s" % err_sp.decode("utf-8", errors="replace").strip()
                        )
                    archive_files["staged.patch"] = _add_bytes_to_tar(tf, "staged.patch", staged_patch_bytes)

                    rc_up, unstaged_patch_bytes, err_up = _run_git_bytes(
                        project_path,
                        ["diff", "--binary", "--full-index"],
                    )
                    if rc_up != 0:
                        raise PackageError(
                            "读取 Git 未暂存补丁失败：%s" % err_up.decode("utf-8", errors="replace").strip()
                        )
                    archive_files["unstaged.patch"] = _add_bytes_to_tar(tf, "unstaged.patch", unstaged_patch_bytes)

                    # Explicit `stagedEntries` with exact index blobs (handles 0-byte files, mode changes, deletions, unborn repos)
                    rc_sr, raw_staged, _ = _run_git_bytes(
                        project_path,
                        ["diff", "--cached", "--full-index", "--no-renames", "--raw", "-z"],
                    )
                    if rc_sr != 0:
                        raise PackageError("读取 Git 暂存条目失败。")
                    for idx, item in enumerate(_parse_git_raw_diff_z(raw_staged)):
                        norm_p = _register_path(item["rawPath"])
                        st_code = item["status"]
                        if st_code == "D":
                            staged_entries.append({
                                "path": norm_p,
                                "status": "D",
                                "mode": "",
                                "archiveMember": "",
                                "sha256": "",
                                "size": 0,
                            })
                        else:
                            rc_cb, blob_data, err_cb = _run_git_bytes(
                                project_path,
                                ["cat-file", "blob", item["dstOid"]],
                            )
                            if rc_cb != 0:
                                raise PackageError(
                                    "无法读取暂存对象 %s：%s"
                                    % (item["dstOid"], err_cb.decode("utf-8", errors="replace").strip())
                                )
                            sha = hashlib.sha256(blob_data).hexdigest()
                            member = "blobs/staged_%04d_%s" % (idx, sha[:16])
                            archive_files[member] = _add_bytes_to_tar(tf, member, blob_data)
                            staged_entries.append({
                                "path": norm_p,
                                "status": st_code,
                                "mode": item["dstMode"],
                                "archiveMember": member,
                                "sha256": sha,
                                "size": len(blob_data),
                            })

                    # Explicit `unstagedEntries` with exact worktree files
                    rc_ur, raw_unstaged, _ = _run_git_bytes(
                        project_path,
                        ["diff", "--full-index", "--no-renames", "--raw", "-z"],
                    )
                    if rc_ur != 0:
                        raise PackageError("读取 Git 工作区改动条目失败。")
                    unstaged_deleted: Set[str] = set()
                    for idx, item in enumerate(_parse_git_raw_diff_z(raw_unstaged)):
                        norm_p = _register_path(item["rawPath"])
                        st_code = item["status"]
                        full_p = os.path.join(project_path, item["rawPath"])
                        if st_code == "D" or not os.path.lexists(full_p):
                            unstaged_deleted.add(norm_p)
                            unstaged_entries.append({
                                "path": norm_p,
                                "status": "D",
                                "mode": "",
                                "archiveMember": "",
                                "sha256": "",
                                "size": 0,
                            })
                        else:
                            if os.path.islink(full_p):
                                _ensure_symlink_safe(full_p, real_root, norm_p)
                                sym_bytes = os.readlink(full_p).encode("utf-8")
                                sha = hashlib.sha256(sym_bytes).hexdigest()
                                member = "blobs/unstaged_%04d_%s" % (idx, sha[:16])
                                archive_files[member] = _add_bytes_to_tar(tf, member, sym_bytes)
                                unstaged_entries.append({
                                    "path": norm_p,
                                    "status": st_code,
                                    "mode": "120000",
                                    "archiveMember": member,
                                    "sha256": sha,
                                    "size": len(sym_bytes),
                                })
                            else:
                                mode = _file_git_mode(full_p)
                                sha, sz = _sha256_file(full_p)
                                member = "blobs/unstaged_%04d_%s" % (idx, sha[:16])
                                archive_files[member] = _add_file_to_tar(tf, member, full_p)
                                unstaged_entries.append({
                                    "path": norm_p,
                                    "status": st_code,
                                    "mode": mode,
                                    "archiveMember": member,
                                    "sha256": sha,
                                    "size": sz,
                                })

                    # Eligible untracked files (`--exclude-standard`, excluding secrets & system files)
                    rc_un, raw_un, _ = _run_git_bytes(
                        project_path,
                        ["ls-files", "--others", "--exclude-standard", "-z"],
                    )
                    if rc_un == 0 and raw_un:
                        un_idx = 0
                        for raw_p in sorted(_parse_nul_paths(raw_un)):
                            rel_check = _normalize_rel_path(raw_p)
                            if _is_ignored_system_path(rel_check) or _is_secret_filename(rel_check):
                                continue
                            full_p = os.path.join(project_path, raw_p)
                            if os.path.islink(full_p):
                                target = _ensure_symlink_safe(full_p, real_root, rel_check)
                                target_rel = _nfc(os.path.relpath(target, real_root).replace("\\", "/"))
                                if _is_secret_filename(target_rel):
                                    continue
                            if not os.path.isfile(full_p):
                                continue
                            norm_p = _register_path(raw_p)
                            mode = _file_git_mode(full_p)
                            sha, sz = _sha256_file(full_p)
                            member = "blobs/untracked_%04d_%s" % (un_idx, sha[:16])
                            un_idx += 1
                            archive_files[member] = _add_file_to_tar(tf, member, full_p)
                            untracked_entries.append({
                                "path": norm_p,
                                "mode": mode,
                                "archiveMember": member,
                                "sha256": sha,
                                "size": sz,
                            })

                    live_tracked = index_paths - unstaged_deleted
                    file_count = len(live_tracked) + len(untracked_entries)
                else:
                    # Non-Git folder snapshot
                    f_idx = 0
                    for dirpath, dirnames, filenames in os.walk(project_path, followlinks=False):
                        kept_dirs = []
                        for dname in sorted(dirnames):
                            if dname.lower() == ".git" or dname.lower() in IGNORED_SYSTEM_NAMES:
                                continue
                            full_d = os.path.join(dirpath, dname)
                            rel_d = _normalize_rel_path(os.path.relpath(full_d, project_path))
                            if os.path.islink(full_d):
                                _ensure_symlink_safe(full_d, real_root, rel_d)
                                continue
                            kept_dirs.append(dname)
                        dirnames[:] = kept_dirs

                        for fname in sorted(filenames):
                            full_f = os.path.join(dirpath, fname)
                            raw_rel = os.path.relpath(full_f, project_path)
                            rel_check = _normalize_rel_path(raw_rel)
                            if _is_ignored_system_path(rel_check) or _is_secret_filename(rel_check):
                                continue
                            if os.path.islink(full_f):
                                target = _ensure_symlink_safe(full_f, real_root, rel_check)
                                target_rel = _nfc(os.path.relpath(target, real_root).replace("\\", "/"))
                                if _is_secret_filename(target_rel):
                                    continue
                            if not os.path.isfile(full_f):
                                continue
                            norm_p = _register_path(raw_rel)
                            mode = _file_git_mode(full_f)
                            sha, sz = _sha256_file(full_f)
                            member = "blobs/folder_%04d_%s" % (f_idx, sha[:16])
                            f_idx += 1
                            archive_files[member] = _add_file_to_tar(tf, member, full_f)
                            folder_entries.append({
                                "path": norm_p,
                                "mode": mode,
                                "archiveMember": member,
                                "sha256": sha,
                                "size": sz,
                            })
                            folder_hashes[norm_p] = sha

                    folder_entries.sort(key=lambda e: e["path"])
                    file_count = len(folder_entries)

                # Validate cross-OS path collisions & Windows reserved names BEFORE finalizing archive
                _validate_cross_os_paths(all_paths, target_os_norm, check_case, check_windows)

                meta_bytes = (json.dumps(meta_out, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
                archive_files["metadata.json"] = _add_bytes_to_tar(tf, "metadata.json", meta_bytes)

                conv_bytes = (json.dumps(conv_out, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
                archive_files["conversations.json"] = _add_bytes_to_tar(tf, "conversations.json", conv_bytes)

                manifest_obj = {
                    "formatVersion": FORMAT_VERSION,
                    "mode": mode_str,
                    "targetOs": target_os_norm,
                    "hasCommits": has_commits,
                    "branch": branch,
                    "headRef": head_ref,
                    "headCommit": head_commit,
                    "headAncestors": head_ancestors,
                    "refs": refs_map,
                    "remotes": remotes_map,
                    "stagedCount": len(staged_entries),
                    "unstagedCount": len(unstaged_entries),
                    "untrackedCount": len(untracked_entries),
                    "fileCount": file_count,
                    "excludedFiles": excluded_files,
                    "allPaths": sorted(all_paths),
                    "stagedEntries": staged_entries,
                    "unstagedEntries": unstaged_entries,
                    "untrackedEntries": untracked_entries,
                    "folderEntries": folder_entries,
                    "folderHashes": folder_hashes,
                    "archiveFiles": archive_files,
                }
                manifest_bytes = (
                    json.dumps(manifest_obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
                ).encode("utf-8")
                _add_bytes_to_tar(tf, "payload_manifest.json", manifest_bytes)

            os.replace(tmp_tar, out_tar_gz_path)
        except BaseException:
            try:
                if os.path.exists(tmp_tar):
                    os.unlink(tmp_tar)
            except OSError:
                pass
            raise

    return {
        "mode": mode_str,
        "branch": branch,
        "headCommit": head_commit,
        "stagedCount": len(staged_entries),
        "unstagedCount": len(unstaged_entries),
        "untrackedCount": len(untracked_entries),
        "fileCount": file_count,
        "excludedFiles": excluded_files,
        "folderHashes": _compute_git_dirty_baseline(project_path) if git_mode else folder_hashes,
    }


# ---------------------------------------------------------------- 3. Extract & Verify Payload


def extract_and_verify_payload(tar_gz_path: str, staging_dir: str) -> Dict[str, Any]:
    """Safely extract tar.gz into staging_dir and verify every file SHA-256 and git bundle."""
    if not tar_gz_path or not os.path.isfile(tar_gz_path):
        raise PackageError("交接载荷压缩包不存在：%s" % tar_gz_path)

    os.makedirs(staging_dir, exist_ok=True)
    real_staging = os.path.realpath(staging_dir)
    staging_prefix = real_staging.rstrip(os.sep) + os.sep

    extracted_files: Set[str] = set()

    try:
        with tarfile.open(tar_gz_path, mode="r:gz") as tf:
            members = tf.getmembers()
            for m in members:
                if m.issym() or m.islnk():
                    raise PackageError("安全校验失败：压缩包内禁止包含符号链接或硬链接（%s）。" % m.name)
                if not (m.isfile() or m.isdir()):
                    raise PackageError("安全校验失败：压缩包内包含非法特殊文件类型（%s）。" % m.name)
                try:
                    rel = _normalize_rel_path(m.name)
                except PathConflictError as e:
                    raise PackageError("安全校验失败：压缩包成员路径非法（%s）：%s" % (m.name, e))
                dest = os.path.realpath(os.path.join(real_staging, *rel.split("/")))
                if dest != real_staging and not dest.startswith(staging_prefix):
                    raise PackageError("安全校验失败：压缩包成员越界（%s）。" % m.name)

            for m in members:
                rel = _normalize_rel_path(m.name)
                dest = os.path.join(real_staging, *rel.split("/"))
                if m.isdir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                if rel in extracted_files:
                    raise PackageError("安全校验失败：压缩包包含重复成员（%s）。" % rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                fobj = tf.extractfile(m)
                if fobj is None:
                    raise PackageError("无法读取压缩包成员：%s" % rel)
                with fobj, open(dest, "wb") as out_fh:
                    shutil.copyfileobj(fobj, out_fh)
                os.chmod(dest, 0o600)
                extracted_files.add(rel)
    except PackageError:
        raise
    except (tarfile.TarError, EOFError, OSError, ValueError) as e:
        raise PackageError("交接载荷压缩包已损坏或无法解压：%s" % e)

    manifest_path = os.path.join(real_staging, "payload_manifest.json")
    if not os.path.isfile(manifest_path):
        raise PackageError("交接载荷缺少 payload_manifest.json。")

    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as e:
        raise PackageError("payload_manifest.json 无法解析：%s" % e)

    if not isinstance(manifest, dict) or manifest.get("mode") not in ("git", "folder"):
        raise PackageError("payload_manifest.json 格式无效。")

    archive_files = manifest.get("archiveFiles")
    if not isinstance(archive_files, dict):
        raise PackageError("payload_manifest.json 缺少有效的 archiveFiles 清单。")

    expected_set = set(_normalize_rel_path(k) for k in archive_files.keys()) | {"payload_manifest.json"}
    extra = extracted_files - expected_set
    if extra:
        raise PackageError("交接载荷包含未登记的额外文件：%s" % ", ".join(sorted(extra)))

    for rel_key, spec in archive_files.items():
        rel_norm = _normalize_rel_path(rel_key)
        full_p = os.path.join(real_staging, *rel_norm.split("/"))
        if not os.path.isfile(full_p):
            raise PackageError("交接载荷缺少清单文件：%s" % rel_norm)
        if not isinstance(spec, dict):
            raise PackageError("交接载荷清单条目损坏：%s" % rel_norm)
        actual_sha, actual_size = _sha256_file(full_p)
        if actual_size != spec.get("size") or actual_sha != spec.get("sha256"):
            raise PackageError(
                "交接载荷文件校验失败（SHA-256 或大小不匹配）：%s" % rel_norm
            )

    def _load_json_dict(name: str) -> Dict[str, Any]:
        p = os.path.join(real_staging, name)
        if not os.path.isfile(p):
            raise PackageError("交接载荷缺少 %s。" % name)
        try:
            with open(p, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
        except (OSError, ValueError) as e:
            raise PackageError("%s 解析失败：%s" % (name, e))
        if not isinstance(obj, dict):
            raise PackageError("%s 顶层结构不是对象。" % name)
        return obj

    metadata = _load_json_dict("metadata.json")
    conversations = _load_json_dict("conversations.json")

    if manifest["mode"] == "git" and manifest.get("hasCommits"):
        bundle_path = os.path.join(real_staging, "repo.bundle")
        if not os.path.isfile(bundle_path):
            raise PackageError("Git 交接载荷缺少 repo.bundle。")
        with tempfile.TemporaryDirectory() as tmp_git:
            rc_init, _, err_init = _run_git_text(real_staging, ["init", "--bare", tmp_git])
            if rc_init != 0:
                raise PackageError("初始化校验用临时 Git 仓库失败：%s" % err_init.strip())
            rc_ver, _, err_ver = _run_git_text(
                real_staging,
                ["--git-dir", tmp_git, "bundle", "verify", bundle_path],
            )
            if rc_ver != 0:
                raise PackageError("Git bundle 完整性校验失败：%s" % err_ver.strip())
            # Also unpack objects into temp bare repo to verify pack checksums and headCommit presence
            rc_fetch, _, err_fetch = _run_git_text(
                real_staging,
                ["--git-dir", tmp_git, "fetch", "-q", bundle_path, "HEAD:refs/verify/head"],
            )
            if rc_fetch != 0:
                raise PackageError("Git bundle 对象解包校验失败：%s" % err_fetch.strip())
            head_commit = manifest.get("headCommit") or ""
            if head_commit:
                rc_cat, _, _ = _run_git_text(
                    real_staging,
                    ["--git-dir", tmp_git, "cat-file", "-e", "%s^{commit}" % head_commit],
                )
                if rc_cat != 0:
                    raise PackageError("Git bundle 中缺少声明的 HEAD 提交：%s" % head_commit)

    manifest_ret = dict(manifest)
    manifest_ret["_stagingDir"] = os.path.abspath(staging_dir)
    return {
        "manifest": manifest_ret,
        "metadata": metadata,
        "conversations": conversations,
    }


# ---------------------------------------------------------------- 4. Check Target Clean


def _collect_manifest_rel_paths(payload_manifest: Dict[str, Any]) -> List[str]:
    paths: Set[str] = set()
    for p in payload_manifest.get("allPaths") or []:
        paths.add(_normalize_rel_path(p))
    for key in ("stagedEntries", "unstagedEntries", "untrackedEntries", "folderEntries"):
        for entry in payload_manifest.get(key) or []:
            if isinstance(entry, dict) and entry.get("path"):
                paths.add(_normalize_rel_path(entry["path"]))
    for p in (payload_manifest.get("folderHashes") or {}).keys():
        paths.add(_normalize_rel_path(p))
    return sorted(paths)


def _resolve_git_objects_dir(repo_path: str) -> Optional[str]:
    rc, out, _ = _run_git_text(repo_path, ["rev-parse", "--git-path", "objects"])
    if rc != 0 or not out.strip():
        return None
    p = out.strip()
    if not os.path.isabs(p):
        p = os.path.join(repo_path, p)
    return os.path.abspath(p)


def _is_local_head_ancestor_of_incoming(
    target_repo: str,
    local_head: str,
    incoming_head: str,
    payload_manifest: Dict[str, Any],
) -> bool:
    if local_head == incoming_head:
        return True

    staging_dir = payload_manifest.get("_stagingDir")
    if staging_dir:
        bundle_path = os.path.join(staging_dir, "repo.bundle")
        obj_dir = _resolve_git_objects_dir(target_repo)
        if os.path.isfile(bundle_path) and obj_dir and os.path.isdir(obj_dir):
            with tempfile.TemporaryDirectory() as tmp_git:
                rc_i, _, _ = _run_git_text(staging_dir, ["init", "--bare", tmp_git])
                if rc_i == 0:
                    alt_file = os.path.join(tmp_git, "objects", "info", "alternates")
                    os.makedirs(os.path.dirname(alt_file), exist_ok=True)
                    with open(alt_file, "w", encoding="utf-8") as fh:
                        fh.write(obj_dir + "\n")
                    refspecs = ["HEAD:refs/incoming/head"]
                    refs = payload_manifest.get("refs") or {}
                    if any(k.startswith("refs/heads/") for k in refs):
                        refspecs.append("+refs/heads/*:refs/incoming/heads/*")
                    rc_f, _, _ = _run_git_text(
                        staging_dir,
                        ["--git-dir", tmp_git, "fetch", "-q", bundle_path] + refspecs,
                    )
                    if rc_f == 0:
                        rc_mb, _, _ = _run_git_text(
                            staging_dir,
                            ["--git-dir", tmp_git, "merge-base", "--is-ancestor", local_head, incoming_head],
                        )
                        return rc_mb == 0

    ancestors = payload_manifest.get("headAncestors")
    if isinstance(ancestors, list) and ancestors:
        return local_head in set(ancestors)

    rc_cat, _, _ = _run_git_text(target_repo, ["cat-file", "-e", "%s^{commit}" % incoming_head])
    if rc_cat == 0:
        rc_mb, _, _ = _run_git_text(
            target_repo,
            ["merge-base", "--is-ancestor", local_head, incoming_head],
        )
        return rc_mb == 0
    return False


def _parse_git_status_porcelain_z(raw: bytes) -> List[Tuple[str, str]]:
    """Return list of (XY, path) from `git status --porcelain -z --untracked-files=all`."""
    if not raw:
        return []
    tokens = raw.split(b"\x00")
    out: List[Tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        text = tok.decode("utf-8", errors="surrogateescape")
        if len(text) < 4:
            i += 1
            continue
        xy = text[:2]
        path = text[3:]
        i += 1
        if ("R" in xy or "C" in xy) and i < len(tokens):
            # In porcelain v1 -z, R/C has `XY new_path\0orig_path\0`
            i += 1
        out.append((xy, path))
    return out


def _directory_is_effectively_empty(target_path: str) -> bool:
    """Return True if target_path has no .git and no non-excluded, non-.DS_Store entries."""
    for dirpath, dirnames, filenames in os.walk(target_path, followlinks=False):
        kept_dirs = []
        for d in dirnames:
            if d.lower() == ".git":
                return False
            if d.lower() in IGNORED_SYSTEM_NAMES:
                continue
            full_d = os.path.join(dirpath, d)
            if os.path.islink(full_d):
                return False
            kept_dirs.append(d)
        dirnames[:] = kept_dirs
        for fname in filenames:
            full_f = os.path.join(dirpath, fname)
            rel_f = _nfc(os.path.relpath(full_f, target_path).replace("\\", "/"))
            if _is_ignored_system_path(rel_f):
                continue
            if os.path.islink(full_f):
                return False
            if _is_secret_filename(rel_f):
                continue
            return False
    return True


def _fetch_bundle_refs(target_path: str, bundle_path: str, refs: dict, dry_run: bool = False) -> Tuple[int, str]:
    args = [
        "fetch", "--atomic", "--no-tags", "--no-prune", "--no-prune-tags",
        "--no-recurse-submodules", "--no-write-fetch-head", "--update-head-ok",
    ]
    if dry_run:
        args.append("--dry-run")
    refspecs = [
        namespace + "*:" + namespace + "*"
        for namespace in ("refs/heads/", "refs/tags/")
        if any(ref.startswith(namespace) for ref in refs)
    ]
    if not refspecs:
        return 0, ""
    rc, _, err = _run_git_text(target_path, args + [bundle_path] + refspecs)
    return rc, err.strip()


def _preserved_file_conflict(target_path: str, incoming_paths: List[str]) -> Optional[str]:
    for rel in incoming_paths:
        parts = rel.split("/")
        dest = target_path
        for part in parts:
            dest = os.path.join(dest, part)
            if _is_secret_filename(part) and os.path.lexists(dest) and (
                os.path.islink(dest) or not os.path.isdir(dest)
            ):
                return os.path.relpath(dest, target_path)
        # Restoring a file here would replace the entire existing directory.
        if os.path.isdir(dest) and not os.path.islink(dest):
            for parent, _, files in os.walk(dest):
                for name in files:
                    if _is_secret_filename(name):
                        return os.path.relpath(os.path.join(parent, name), target_path)
    return None


def check_target_clean_for_receive(
    target_path: str,
    payload_manifest: dict,
    expected_base_head: Optional[str] = None,
    expected_folder_hashes: Optional[dict] = None,
) -> None:
    """Verify cross-OS path safety and ensure target_path has no unsynced local changes.

    Never mutates target_path.
    """
    if not isinstance(payload_manifest, dict):
        raise PackageError("无效的 payload_manifest。")

    mode = payload_manifest.get("mode")
    if mode not in ("git", "folder"):
        raise PackageError("未知的交接模式：%r" % (mode,))

    rel_paths = _collect_manifest_rel_paths(payload_manifest)
    target_os_norm = (payload_manifest.get("targetOs") or "").strip().lower()
    check_case = _is_fs_case_insensitive(target_path) or target_os_norm in ("windows", "mac")
    check_win = (os.name == "nt") or (target_os_norm == "windows")
    _validate_cross_os_paths(rel_paths, target_os_norm, check_case=check_case, check_windows=check_win)

    if not os.path.lexists(target_path):
        return

    if os.path.islink(target_path) or not os.path.isdir(target_path):
        raise TargetDirtyError(
            "目标路径「%s」已存在且不是普通目录，已保留原路径不变；可使用 --into 接收至新目录。" % target_path,
            target_path=target_path,
            reasons=["目标路径已存在且不是目录"],
        )

    conflict = _preserved_file_conflict(target_path, rel_paths)
    if conflict:
        reason = "交接包路径与本机保留的凭据文件冲突：%s" % conflict
        raise TargetDirtyError(
            "%s。请用 --into 接收至新目录。" % reason, target_path=target_path, reasons=[reason],
        )

    if _directory_is_effectively_empty(target_path):
        return

    reasons: List[str] = []

    if mode == "git":
        if not _is_git_repo_root(target_path):
            reasons.append("目标目录已存在非空文件且不是独立的 Git 仓库")
        else:
            has_local_commits, local_head = _git_has_commits(target_path)
            incoming_has_commits = bool(payload_manifest.get("hasCommits"))
            incoming_head = payload_manifest.get("headCommit") or ""

            rc_ignored, ignored_paths, ignored_error = _run_git_bytes(
                target_path, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"]
            )
            if rc_ignored != 0:
                reasons.append("无法检查目标仓库的忽略文件：%s" % ignored_error.decode("utf-8", errors="replace").strip())
            else:
                for path in _parse_nul_paths(ignored_paths):
                    if not (_is_secret_filename(path) and not os.path.isdir(os.path.join(target_path, path))):
                        reasons.append("存在本机 Git 忽略文件：%s" % path)

            staging = payload_manifest.get("_stagingDir")
            if incoming_has_commits and staging:
                rc_refs, ref_error = _fetch_bundle_refs(
                    target_path, os.path.join(staging, "repo.bundle"), payload_manifest.get("refs") or {}, dry_run=True,
                )
                if rc_refs != 0:
                    reasons.append("交接包的 Git 分支或标签与本机冲突：%s" % ref_error)

            rc_st, raw_st, err_st = _run_git_bytes(
                target_path,
                ["status", "--porcelain", "-z", "--untracked-files=all"],
            )
            worktree_reasons: List[str] = []
            if rc_st != 0:
                reasons.append(
                    "无法检查目标 Git 仓库状态：%s" % err_st.decode("utf-8", errors="replace").strip()
                )
            else:
                for xy, raw_p in _parse_git_status_porcelain_z(raw_st):
                    rel_p = _nfc(raw_p.replace("\\", "/"))
                    if xy == "??":
                        if _is_ignored_system_path(rel_p) or _is_secret_filename(rel_p):
                            continue
                        worktree_reasons.append("存在本地未跟踪文件：%s" % rel_p)
                    else:
                        worktree_reasons.append("存在本地未提交改动 (%s)：%s" % (xy.strip(), rel_p))

            if worktree_reasons:
                # Allow receiving cleanly if target_path's dirty state is 100% identical to the
                # baseline state saved when this device itself last sent or received the relay.
                matches_relay_baseline = False
                if (
                    isinstance(expected_folder_hashes, dict)
                    and expected_folder_hashes
                    and (expected_base_head is None or local_head == expected_base_head)
                ):
                    rc_u, raw_u, _ = _run_git_bytes(target_path, ["ls-files", "-u", "-z"])
                    has_unmerged = (rc_u == 0 and bool(raw_u.strip(b"\x00")))
                    if not has_unmerged and _compute_git_dirty_baseline(target_path) == expected_folder_hashes:
                        matches_relay_baseline = True
                if not matches_relay_baseline:
                    reasons.extend(worktree_reasons)

            if has_local_commits:
                if not incoming_has_commits or not incoming_head:
                    reasons.append(
                        "目标仓库已有本地提交（%s），而交接包为尚无提交的空分支" % local_head[:12]
                    )
                elif not _is_local_head_ancestor_of_incoming(
                    target_path, local_head, incoming_head, payload_manifest
                ):
                    reasons.append(
                        "目标仓库存在未同步的本地提交（本地 HEAD=%s 不是交接包 HEAD=%s 的祖先）"
                        % (local_head[:12], incoming_head[:12])
                    )
            elif expected_base_head:
                reasons.append("目标仓库缺少预期的基线提交（%s）" % expected_base_head[:12])
    else:
        # Folder snapshot target check
        if expected_folder_hashes is None:
            reasons.append("目标目录已存在文件，且没有历史接力基线哈希可比对")
        else:
            norm_expected: Dict[str, str] = {
                _normalize_rel_path(k): v for k, v in expected_folder_hashes.items()
            }
            current_hashes: Dict[str, str] = {}
            for dirpath, dirnames, filenames in os.walk(target_path, followlinks=False):
                kept_dirs = []
                for d in sorted(dirnames):
                    if d.lower() == ".git":
                        reasons.append("目标目录内存在本地 .git 仓库")
                        continue
                    if d.lower() in IGNORED_SYSTEM_NAMES:
                        continue
                    full_d = os.path.join(dirpath, d)
                    if os.path.islink(full_d):
                        rel_d = _nfc(os.path.relpath(full_d, target_path).replace("\\", "/"))
                        reasons.append("目标目录新增了符号链接目录：%s" % rel_d)
                        continue
                    kept_dirs.append(d)
                dirnames[:] = kept_dirs

                for fname in sorted(filenames):
                    full_f = os.path.join(dirpath, fname)
                    rel_f = _normalize_rel_path(os.path.relpath(full_f, target_path))
                    if _is_ignored_system_path(rel_f) or _is_secret_filename(rel_f):
                        continue
                    if os.path.islink(full_f) or not os.path.isfile(full_f):
                        reasons.append("目标目录存在本地特殊文件或符号链接：%s" % rel_f)
                        continue
                    sha, _ = _sha256_file(full_f)
                    current_hashes[rel_f] = sha

            for rel_f, cur_sha in sorted(current_hashes.items()):
                if rel_f not in norm_expected:
                    reasons.append("目标目录存在新增本地文件：%s" % rel_f)
                elif norm_expected[rel_f] != cur_sha:
                    reasons.append("目标目录文件已被本地修改：%s" % rel_f)
            for rel_f in sorted(norm_expected.keys()):
                if rel_f not in current_hashes:
                    reasons.append("目标目录文件已被本地删除：%s" % rel_f)

    if reasons:
        summary = "；".join(reasons[:6])
        if len(reasons) > 6:
            summary += "（等共 %d 处改动）" % len(reasons)
        msg = (
            "目标目录「%s」检测到本地新改动（%s）。已保留原目录完整不动；请先处理本地改动，或使用 --into <新目录> 接收。"
            % (target_path, summary)
        )
        raise TargetDirtyError(msg, target_path=target_path, reasons=reasons)


# ---------------------------------------------------------------- 5. Apply Verified Payload


def _clean_worktree_preserving_secrets(target_path: str, keep_dot_git: bool) -> None:
    """Remove non-secret files in target_path while leaving `.git` (if keep_dot_git) and `.env`/secret files untouched."""
    if not os.path.isdir(target_path):
        return
    for dirpath, dirnames, filenames in os.walk(target_path, topdown=False, followlinks=False):
        rel_dir = os.path.relpath(dirpath, target_path)
        if rel_dir != ".":
            parts = _nfc(rel_dir.replace("\\", "/")).split("/")
            if keep_dot_git and ".git" in [p.lower() for p in parts]:
                continue

        for fname in filenames:
            if keep_dot_git and dirpath == target_path and fname.lower() == ".git":
                continue
            full_f = os.path.join(dirpath, fname)
            rel_f = _nfc(os.path.relpath(full_f, target_path).replace("\\", "/"))
            if _is_secret_filename(rel_f):
                # Preserve local .env / .env.* / local credentials untouched in place!
                continue
            try:
                os.unlink(full_f)
            except OSError:
                pass

        for dname in dirnames:
            if keep_dot_git and dirpath == target_path and dname.lower() == ".git":
                continue
            full_d = os.path.join(dirpath, dname)
            if os.path.islink(full_d):
                try:
                    os.unlink(full_d)
                except OSError:
                    pass
            elif os.path.isdir(full_d):
                try:
                    os.rmdir(full_d)
                except OSError:
                    # Non-empty because a preserved .env.* file lives inside it
                    pass


def _prune_empty_parents(file_path: str, stop_root: str) -> None:
    stop_real = os.path.realpath(stop_root)
    cur = os.path.dirname(os.path.abspath(file_path))
    while os.path.realpath(cur) != stop_real and os.path.realpath(cur).startswith(stop_real.rstrip(os.sep) + os.sep):
        try:
            os.rmdir(cur)
        except OSError:
            break
        cur = os.path.dirname(cur)


def _write_restored_file(src_blob: str, dest_path: str, git_mode_str: str) -> None:
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    if os.path.lexists(dest_path):
        os.unlink(dest_path)
    if git_mode_str == "120000":
        with open(src_blob, "rb") as fh:
            link_target = fh.read().decode("utf-8")
        os.symlink(link_target, dest_path)
        return
    shutil.copyfile(src_blob, dest_path)
    if git_mode_str == "100755":
        os.chmod(dest_path, 0o755)
    else:
        os.chmod(dest_path, 0o644)


def apply_verified_payload(staging_dir: str, target_path: str) -> Dict[str, Any]:
    """Apply an already-extracted & verified payload from staging_dir into target_path."""
    manifest_path = os.path.join(staging_dir, "payload_manifest.json")
    if not os.path.isfile(manifest_path):
        raise PackageError("暂存目录缺少 payload_manifest.json：%s" % staging_dir)

    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    mode = manifest.get("mode")
    if mode not in ("git", "folder"):
        raise PackageError("无效的交接模式：%r" % (mode,))

    os.makedirs(target_path, exist_ok=True)

    if mode == "git":
        if not _is_git_repo_root(target_path):
            branch_init = manifest.get("branch") or "main"
            rc_init, _, err_init = _run_git_text(target_path, ["init", "-b", branch_init])
            if rc_init != 0:
                # Fallback for older git versions without `init -b`
                rc_init2, _, err_init2 = _run_git_text(target_path, ["init"])
                if rc_init2 != 0:
                    raise PackageError("初始化目标 Git 仓库失败：%s" % (err_init2 or err_init).strip())
                _run_git_text(target_path, ["symbolic-ref", "HEAD", "refs/heads/%s" % branch_init])

            for rname, rurl in (manifest.get("remotes") or {}).items():
                _run_git_text(target_path, ["remote", "add", rname, rurl])

        has_commits = bool(manifest.get("hasCommits"))
        head_commit = manifest.get("headCommit") or ""
        branch = manifest.get("branch") or ""

        if has_commits:
            bundle_path = os.path.join(staging_dir, "repo.bundle")
            if not os.path.isfile(bundle_path):
                raise PackageError("暂存目录缺少 repo.bundle。")

            refs = manifest.get("refs") or {}
            if branch and refs.get("refs/heads/" + branch) != head_commit:
                raise PackageError("交接包的 Git 分支与声明的 HEAD 不一致：%s" % branch)
            rc_f, err_f = _fetch_bundle_refs(target_path, bundle_path, refs)
            if rc_f != 0:
                raise PackageError("从 Git bundle 导入分支或标签失败：%s" % err_f)

            # Always ensure headCommit is unpacked even if HEAD was detached and not on any branch
            rc_cat, _, _ = _run_git_text(target_path, ["cat-file", "-e", "%s^{commit}" % head_commit])
            if rc_cat != 0:
                rc_fh, _, err_fh = _run_git_text(target_path, ["fetch", "-q", bundle_path, "HEAD"])
                if rc_fh != 0:
                    raise PackageError("从 Git bundle 导入 HEAD 失败：%s" % err_fh.strip())

            _clean_worktree_preserving_secrets(target_path, keep_dot_git=True)
            if branch:
                rc_co, _, err_co = _run_git_text(
                    target_path,
                    ["checkout", "-f", "-B", branch, head_commit],
                )
            else:
                rc_co, _, err_co = _run_git_text(
                    target_path,
                    ["checkout", "-f", "--detach", head_commit],
                )
            if rc_co != 0:
                raise PackageError("检出目标分支/提交失败：%s" % err_co.strip())

            rc_rst, _, err_rst = _run_git_text(target_path, ["reset", "--hard", head_commit])
            if rc_rst != 0:
                raise PackageError("重置目标仓库提交状态失败：%s" % err_rst.strip())
        else:
            # 0-commit unborn Git repository
            _clean_worktree_preserving_secrets(target_path, keep_dot_git=True)
            unborn_branch = branch or "main"
            _run_git_text(target_path, ["symbolic-ref", "HEAD", "refs/heads/%s" % unborn_branch])
            _run_git_text(target_path, ["rm", "-rf", "--cached", "--ignore-unmatch", "."])

        # Apply exact staged index state + matching working-tree baseline
        for entry in manifest.get("stagedEntries") or []:
            rel_p = _normalize_rel_path(entry["path"])
            dest_p = os.path.join(target_path, *rel_p.split("/"))
            if entry.get("status") == "D":
                rc_rm, _, err_rm = _run_git_text(
                    target_path,
                    ["update-index", "--force-remove", "--", rel_p],
                )
                if rc_rm != 0:
                    raise PackageError("恢复暂存删除状态失败（%s）：%s" % (rel_p, err_rm.strip()))
                if os.path.lexists(dest_p) and not _is_secret_filename(rel_p):
                    os.unlink(dest_p)
                    _prune_empty_parents(dest_p, target_path)
            else:
                blob_src = os.path.join(staging_dir, *_normalize_rel_path(entry["archiveMember"]).split("/"))
                entry_mode = entry.get("mode") or "100644"
                rc_ho, out_ho, err_ho = _run_git_text(
                    target_path,
                    ["hash-object", "-w", "--no-filters", blob_src],
                )
                if rc_ho != 0 or not out_ho.strip():
                    raise PackageError("写入暂存对象失败（%s）：%s" % (rel_p, err_ho.strip()))
                oid = out_ho.strip()
                rc_ui, _, err_ui = _run_git_text(
                    target_path,
                    ["update-index", "--add", "--cacheinfo", "%s,%s,%s" % (entry_mode, oid, rel_p)],
                )
                if rc_ui != 0:
                    raise PackageError("更新暂存索引失败（%s）：%s" % (rel_p, err_ui.strip()))
                _write_restored_file(blob_src, dest_p, entry_mode)

        # Apply exact unstaged working-tree modifications / deletions without touching index
        for entry in manifest.get("unstagedEntries") or []:
            rel_p = _normalize_rel_path(entry["path"])
            dest_p = os.path.join(target_path, *rel_p.split("/"))
            if entry.get("status") == "D":
                if os.path.lexists(dest_p) and not _is_secret_filename(rel_p):
                    os.unlink(dest_p)
                    _prune_empty_parents(dest_p, target_path)
            else:
                blob_src = os.path.join(staging_dir, *_normalize_rel_path(entry["archiveMember"]).split("/"))
                entry_mode = entry.get("mode") or "100644"
                _write_restored_file(blob_src, dest_p, entry_mode)

        # Restore eligible untracked files
        for entry in manifest.get("untrackedEntries") or []:
            rel_p = _normalize_rel_path(entry["path"])
            if _is_secret_filename(rel_p):
                continue
            dest_p = os.path.join(target_path, *rel_p.split("/"))
            blob_src = os.path.join(staging_dir, *_normalize_rel_path(entry["archiveMember"]).split("/"))
            entry_mode = entry.get("mode") or "100644"
            _write_restored_file(blob_src, dest_p, entry_mode)

        # Refresh Git stat cache so clean files don't show mtime-only diffs
        _run_git_text(target_path, ["update-index", "-q", "--refresh"])

        return {
            "mode": "git",
            "targetPath": os.path.abspath(target_path),
            "headCommit": head_commit,
            "branch": branch,
            "folderHashes": _compute_git_dirty_baseline(target_path),
        }

    # Folder snapshot mode
    _clean_worktree_preserving_secrets(target_path, keep_dot_git=False)
    restored_hashes: Dict[str, str] = {}
    for entry in manifest.get("folderEntries") or []:
        rel_p = _normalize_rel_path(entry["path"])
        if _is_secret_filename(rel_p):
            continue
        dest_p = os.path.join(target_path, *rel_p.split("/"))
        blob_src = os.path.join(staging_dir, *_normalize_rel_path(entry["archiveMember"]).split("/"))
        entry_mode = entry.get("mode") or "100644"
        _write_restored_file(blob_src, dest_p, entry_mode)
        restored_hashes[rel_p] = entry["sha256"]

    return {
        "mode": "folder",
        "targetPath": os.path.abspath(target_path),
        "headCommit": "",
        "branch": "",
        "folderHashes": restored_hashes,
    }
