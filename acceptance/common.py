"""Shared helpers for the relay-desk acceptance tools.

* Level detection.  A result's ``level`` always comes from the running
  interpreter's real ``platform``/``sys`` data, never from a user argument.
* Source identity.  Every result carries the working-tree digest from
  ``acceptance/source_digest.py`` (or, inside an extracted acceptance source
  package without ``.git``, the same digest recomputed from the files and
  checked against the package's ``SOURCE-DIGEST.json``).
* Results folders named ``<UTC stamp>_<level>_<digest12>_<label>``.
* ``CommandLog`` runs subprocesses and keeps raw stdout/stderr per command.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCHEMA_PREFIX = "relay-desk-acceptance"

# Real OS expected for each ring role.  The role is a label the human picks;
# the platform check below decides whether the run may call itself native.
ROLE_SYSTEMS = {"mac": "Darwin", "win": "Windows", "linux": "Linux"}

NATIVE_LEVELS = {"Windows": "native-windows", "Linux": "native-linux", "Darwin": "native-macos"}
SIM_LEVELS = {
    "Darwin": "macos-local-simulation",
    "Linux": "linux-local-simulation",
    "Windows": "windows-local-simulation",
}
ALL_LEVELS = sorted(
    set(NATIVE_LEVELS.values())
    | set(SIM_LEVELS.values())
    | {"wsl-linux", "real-multidevice", "partial-multidevice", "single-machine-multiinstance", "role-mismatch-simulation"}
)


# ---------------------------------------------------------------- small utils


def utc_stamp(t: Optional[float] = None) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t if t is not None else time.time()))


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def write_json(path: Path, obj: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(str(tmp), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(str(tmp), str(path))
    return path


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(str(path), "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def append_jsonl(path: Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, sort_keys=False) + "\n")


# ---------------------------------------------------------------- platform & level


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _os_pretty(system: str) -> Dict[str, str]:
    if system == "Darwin":
        out = {"product": "macOS", "version": platform.mac_ver()[0]}
        try:
            out["build"] = subprocess.run(
                ["sw_vers", "-buildVersion"], capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return out
    if system == "Windows":
        rel, ver, csd, ptype = platform.win32_ver()
        out = {"product": "Windows", "release": rel, "version": ver, "servicePack": csd, "type": ptype}
        try:
            out["edition"] = platform.win32_edition()  # type: ignore[attr-defined]
        except Exception:
            pass
        return out
    if system == "Linux":
        rel = {}
        for line in _read_text("/etc/os-release").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                rel[k.strip()] = v.strip().strip('"')
        return {
            "product": rel.get("NAME", "Linux"),
            "version": rel.get("VERSION_ID", ""),
            "prettyName": rel.get("PRETTY_NAME", ""),
            "kernel": platform.release(),
        }
    return {"product": system, "version": platform.version()}


def platform_info() -> Dict[str, Any]:
    """Facts about the running OS, taken from ``platform``/``sys`` only."""
    system = platform.system()
    release = platform.release()
    info = {
        "system": system,
        "sysPlatform": sys.platform,
        "osName": os.name,
        "release": release,
        "version": platform.version(),
        "machine": platform.machine(),
        "os": _os_pretty(system),
        "wsl": bool(system == "Linux" and ("microsoft" in release.lower() or os.environ.get("WSL_DISTRO_NAME"))),
        "container": bool(system == "Linux" and (os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"))),
    }
    return info


def detect_level(kind: str, info: Optional[Dict[str, Any]] = None) -> str:
    """Return the acceptance level for this run.

    ``kind`` is ``native`` (this OS, this filesystem, real processes) or
    ``local-simulation`` (several isolated device instances on one machine).
    The OS part always comes from ``platform_info()``; a simulation can never
    be reported as native.
    """
    info = info or platform_info()
    system = str(info.get("system") or "")
    if kind == "native":
        if system == "Linux" and info.get("wsl"):
            return "wsl-linux"
        return NATIVE_LEVELS.get(system, "native-" + (system.lower() or "unknown"))
    if kind == "local-simulation":
        return SIM_LEVELS.get(system, (system.lower() or "unknown") + "-local-simulation")
    raise ValueError("unknown level kind: %r" % (kind,))


def role_matches_platform(role: str, info: Optional[Dict[str, Any]] = None) -> bool:
    info = info or platform_info()
    expected = ROLE_SYSTEMS.get(role)
    if expected is None:
        return False
    if expected == "Linux" and info.get("wsl"):
        return False
    return info.get("system") == expected


def machine_fingerprint() -> str:
    """Stable, non-reversible id of this machine (to tell devices apart in a ring)."""
    parts = [platform.node(), "%012x" % uuid.getnode()]
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        txt = _read_text(p).strip()
        if txt:
            parts.append(txt)
            break
    return sha256_bytes("|".join(parts).encode("utf-8"))[:16]


def aggregate_ring_level(device_records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Level of a ring assembled from per-device records.

    ``real-multidevice`` requires one record per role, every role running on
    the OS it is named after (``platformMatchesRole``), and pairwise distinct
    machine fingerprints.  Anything weaker is labelled for what it is.
    """
    recs = list(device_records)
    reasons: List[str] = []
    roles = {}
    for r in recs:
        roles.setdefault(r.get("role"), []).append(r)
    if any(r.get("simulation") for r in recs):
        return {"level": recs[0].get("level") or "local-simulation", "reasons": ["records come from a local simulation"]}
    if any(not r.get("platformMatchesRole") for r in recs):
        reasons.append("at least one role ran on a different OS than its name")
        return {"level": "role-mismatch-simulation", "reasons": reasons}
    machines = {r.get("machineId") for r in recs}
    missing = [role for role in ROLE_SYSTEMS if role not in roles]
    if missing:
        reasons.append("missing roles: %s" % ", ".join(missing))
    if len(machines) >= 3 and not missing:
        return {"level": "real-multidevice", "reasons": ["3 roles on 3 distinct machines with matching OS"]}
    if len(machines) >= 2:
        reasons.append("%d distinct machines" % len(machines))
        return {"level": "partial-multidevice", "reasons": reasons}
    reasons.append("all records come from one machine")
    return {"level": "single-machine-multiinstance", "reasons": reasons}


# ---------------------------------------------------------------- source identity

_WALK_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}
_WALK_SKIP_FILES = {"SOURCE-DIGEST.json", ".DS_Store", "Thumbs.db"}


def _walk_source_files(root: Path) -> List[str]:
    from acceptance import source_digest as sd

    out = []
    results_dir = (root / "acceptance" / "results").resolve()
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dp = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            if d in _WALK_SKIP_DIRS:
                continue
            if (dp / d).resolve() == results_dir:
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if f in _WALK_SKIP_FILES or f.endswith((".pyc", ".pyo")):
                continue
            if f in sd.DENY_NAMES or f.endswith(sd.DENY_SUFFIXES):
                continue
            rel = (dp / f).relative_to(root).as_posix()
            if rel == "SOURCE-DIGEST.json":
                continue
            out.append(rel)
    return sorted(out)


def source_identity(repo: Optional[Path] = None) -> Dict[str, Any]:
    """Digest of the code under test, recorded with every result."""
    repo = Path(repo or REPO_ROOT).resolve()
    from acceptance import source_digest as sd

    if (repo / ".git").exists() and shutil.which("git"):
        try:
            m = sd.build_manifest(repo)
            return {
                "method": "git-worktree",
                "sourceDigest": m["sourceDigest"],
                "head": m["head"],
                "branch": m["branch"],
                "fileCount": m["fileCount"],
                "modifiedCount": len(m["modified"]),
                "untrackedCount": len(m["untracked"]),
                "packageDigest": None,
                "verifiedAgainstPackage": None,
            }
        except Exception as e:  # fall back to the file walk below
            git_error = str(e)
        else:
            git_error = ""
    else:
        git_error = "no .git directory"
    files = _walk_source_files(repo)
    digests = {rel: sd.sha256_file(repo / rel) for rel in files}
    overall = hashlib.sha256("".join("%s\0%s\n" % (rel, digests[rel]) for rel in files).encode("utf-8")).hexdigest()
    pkg = read_json(repo / "SOURCE-DIGEST.json") or {}
    pkg_digest = pkg.get("sourceDigest")
    return {
        "method": "extracted-package",
        "sourceDigest": overall,
        "head": pkg.get("head"),
        "branch": pkg.get("branch"),
        "fileCount": len(files),
        "packageDigest": pkg_digest,
        "verifiedAgainstPackage": (pkg_digest == overall) if pkg_digest else None,
        "note": git_error,
    }


# ---------------------------------------------------------------- results folders


def make_results_dir(root: Path, level: str, digest: str, label: str) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    base = "%s_%s_%s_%s" % (utc_stamp(), level, (digest or "nodigest")[:12], label)
    path = root / base
    n = 1
    while path.exists():
        n += 1
        path = root / ("%s-%d" % (base, n))
    path.mkdir(parents=True)
    return path


def default_results_root() -> Path:
    return REPO_ROOT / "acceptance" / "results"


# ---------------------------------------------------------------- subprocess logging


class CmdResult(object):
    def __init__(self, seq: int, argv: List[str], rc: Any, stdout: str, stderr: str, duration: float):
        self.seq = seq
        self.argv = argv
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.duration = duration

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def json(self) -> Any:
        try:
            return json.loads(self.stdout)
        except ValueError:
            return None

    def brief(self) -> Dict[str, Any]:
        return {"seq": self.seq, "rc": self.rc, "durationSec": round(self.duration, 3)}


_LOGGED_ENV_PREFIXES = ("RELAY_DESK_", "TMPDIR", "CODEX_HOME", "GEMINI_CONFIG_DIR")


class CommandLog(object):
    """Run commands and keep every raw stdout/stderr under ``<dir>/raw``."""

    def __init__(self, results_dir: Path, name: str = "commands.jsonl"):
        self.dir = Path(results_dir)
        self.raw = self.dir / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.index = self.dir / name
        self.seq = 0

    def run(
        self,
        argv: List[str],
        *,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: Optional[float] = 900,
        tag: str = "",
        stdin_text: Optional[str] = None,
    ) -> CmdResult:
        self.seq += 1
        seq = self.seq
        argv = [str(a) for a in argv]
        t0 = time.time()
        rc: Any
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                input=stdin_text.encode("utf-8") if stdin_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
            rc = proc.returncode
            out_b, err_b = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as e:
            rc = "timeout"
            out_b, err_b = e.stdout or b"", (e.stderr or b"") + b"\n[acceptance] command timed out"
        except OSError as e:
            rc = "spawn-error"
            out_b, err_b = b"", ("[acceptance] cannot start command: %s" % e).encode("utf-8")
        dur = time.time() - t0
        stdout = out_b.decode("utf-8", "replace")
        stderr = err_b.decode("utf-8", "replace")
        safe_tag = "".join(c if c.isalnum() or c in "-_." else "_" for c in tag)[:60]
        base = "%04d_%s" % (seq, safe_tag or "cmd")
        (self.raw / (base + ".out")).write_text(stdout, encoding="utf-8")
        (self.raw / (base + ".err")).write_text(stderr, encoding="utf-8")
        env_log = {}
        if env is not None:
            env_log = {k: v for k, v in sorted(env.items()) if k.startswith(_LOGGED_ENV_PREFIXES)}
        append_jsonl(
            self.index,
            {
                "seq": seq,
                "at": iso_now(),
                "tag": tag,
                "argv": argv,
                "cwd": cwd,
                "env": env_log,
                "rc": rc,
                "durationSec": round(dur, 3),
                "stdout": "raw/%s.out" % base,
                "stderr": "raw/%s.err" % base,
            },
        )
        return CmdResult(seq, argv, rc, stdout, stderr, dur)


def which(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for extra in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        cand = os.path.join(extra, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def tool_path() -> str:
    cur = os.environ.get("PATH") or ""
    parts = cur.split(os.pathsep) if cur else []
    for extra in ("/opt/homebrew/bin", "/usr/local/bin"):
        if extra not in parts and os.path.isdir(extra):
            parts.append(extra)
    return os.pathsep.join(parts)
