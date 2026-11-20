"""The single adapter table between the acceptance tools and the relay CLI.

Every relay-desk command an acceptance script runs is listed in ``COMMANDS``
or ``OPTIONAL_COMMANDS``; nothing else builds ``relay`` argv by hand.  When a
later phase changes or adds a command, edit this table only.

* ``COMMANDS``: commands that exist today and are required.
* ``OPTIONAL_COMMANDS``: hooks for phases B to D (``relay doctor``,
  ``relay cache status/prune``, streaming ``sync`` output).  They are probed
  with ``--help`` and used only when present; results always say whether a
  hook was available, so a missing hook is never mistaken for a pass.

There is deliberately no entry that runs ``pair --accept`` without
``--fingerprint``: a public key is only pinned with a fingerprint value that a
human read off the other device (see ``ring.confirm_and_accept``).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from acceptance.common import REPO_ROOT, CmdResult, CommandLog, which

COMMANDS: Dict[str, List[str]] = {
    "init": ["init", "--device", "{alias}", "--ledger", "{ledger}"],
    "device_json": ["device", "--json"],
    # Phase D: rotation refuses while handoffs are in flight unless --force (the ring's K01 rotates on
    # purpose before linux has synced mac's receipt; --force keeps that scenario and prints the consequence).
    "rotate_keys": ["device", "--rotate-keys", "--force", "--json"],
    "pair_local": ["pair", "--provider", "local", "--root", "{root}", "--json"],
    "pair_rclone": ["pair", "--provider", "rclone", "--remote", "{remote}", "--root", "{root}", "--json"],
    "pair_republish": ["pair", "--json"],
    "pair_status": ["pair", "--status", "--json"],
    "pair_accept_confirmed": ["pair", "--with", "{peer}", "--accept", "--fingerprint", "{fingerprint}", "--json"],
    "add_project": ["add", "{name}", "--path", "{path}", "--id", "{project}"],
    "note": ["note", "{project}", "--note", "{note}"],
    "status_project": ["status", "{project}", "--json"],
    "publish": ["publish", "{project}", "--json"],
    "sync_once": ["sync", "--json"],
    "send": ["send", "{project}", "--to", "{to}", "--json"],
    "inbox": ["inbox", "--json"],
    "receive_new": ["receive", "{relay_id}", "--path", "{path}", "--json"],
    "receive": ["receive", "{relay_id}", "--json"],
    "receive_into": ["receive", "{relay_id}", "--into", "{path}", "--json"],
    "recover_status": ["recover", "{relay_id}", "--status", "--json"],
    "recover_rollback": ["recover", "{relay_id}", "--rollback", "--json"],
    "cloud_remotes": ["cloud", "remotes", "--json"],
}

OPTIONAL_COMMANDS: Dict[str, Dict[str, Any]] = {
    # Phase D (planned): read-only diagnosis.
    "doctor": {"argv": ["doctor", "{project}", "--json"], "probe": ["doctor", "--help"], "phase": "D"},
    # Phase D (planned): cache statistics and dry-run pruning (never --apply here).
    "cache_status": {"argv": ["cache", "status", "--json"], "probe": ["cache", "--help"], "phase": "D"},
    "cache_prune_dry_run": {
        "argv": ["cache", "prune", "--dry-run", "--json"],
        "probe": ["cache", "--help"],
        "phase": "D",
    },
    # Phase B (planned): JSON Lines output while watching.
    "sync_stream": {
        "argv": ["sync", "--watch", "--max-cycles", "{cycles}", "--interval", "{interval}", "--jsonl"],
        "probe": ["sync", "--help"],
        "probeContains": "--jsonl",
        "phase": "B",
    },
}

# The CLI prints errors as plain text on stderr, also with --json (no machine
# readable error code yet: recorded as a CLI gap).  These substrings classify
# a failure for the report; assertions never rely on them alone.
ERROR_SIGNATURES: Dict[str, List[str]] = {
    "unauthorized_receiver": ["未授权给当前设备"],
    "rollback_in_progress": ["回滚尚未完成", "回滚中的交接"],
    "rollback_not_allowed": ["已开始更新台账"],
    "peer_key_changed": ["密钥已变更"],
    "busy": ["本机有其他交棒、同步或接棒操作正在进行"],
    "target_dirty": ["检测到本地新改动", "未同步的本地提交"],
    "publish_blocked_by_handoff": ["项目已有待完成的交接"],
    "active_handoff_exists": ["尚未签收的在途交接"],
    "fingerprint_mismatch": ["配对指纹不匹配"],
    "relay_not_found": ["找不到可领取的交接包"],
    "storage_or_io": ["文件读写出错", "存储", "rclone", "Permission denied", "拒绝访问"],
    "injected_fault": ["Injected fault"],
}


def classify_error(stderr: str) -> List[str]:
    found = []
    for code, needles in ERROR_SIGNATURES.items():
        if any(n in (stderr or "") for n in needles):
            found.append(code)
    return found


def build_argv(key: str, **kw: Any) -> List[str]:
    if key in COMMANDS:
        template = COMMANDS[key]
    elif key in OPTIONAL_COMMANDS:
        template = OPTIONAL_COMMANDS[key]["argv"]
    else:
        raise KeyError("command %r is not in the adapter table" % key)
    return [str(part).format(**kw) for part in template]


def default_launcher() -> List[str]:
    return [sys.executable, str(REPO_ROOT / "relay.py")]


class Relay(object):
    """Run adapter-table commands for one isolated device instance."""

    def __init__(
        self,
        log: CommandLog,
        env: Dict[str, str],
        cwd: Optional[str] = None,
        launcher: Optional[List[str]] = None,
        role: str = "",
    ):
        self.log = log
        self.env = env
        self.cwd = cwd or str(REPO_ROOT)
        self.launcher = list(launcher or default_launcher())
        self.role = role
        self._optional: Dict[str, bool] = {}

    def argv(self, key: str, **kw: Any) -> List[str]:
        return self.launcher + build_argv(key, **kw)

    def run(
        self,
        key: str,
        *,
        extra_env: Optional[Dict[str, str]] = None,
        tag: str = "",
        timeout: Optional[float] = 900,
        cwd: Optional[str] = None,
        **kw: Any,
    ) -> CmdResult:
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return self.log.run(
            self.argv(key, **kw),
            env=env,
            cwd=cwd or self.cwd,
            timeout=timeout,
            tag="%s_%s%s" % (self.role, key, ("_" + tag) if tag else ""),
        )

    def available(self, key: str) -> bool:
        """Probe an optional (future) command once; False when absent."""
        if key in COMMANDS:
            return True
        if key in self._optional:
            return self._optional[key]
        spec = OPTIONAL_COMMANDS[key]
        try:
            proc = subprocess.run(
                self.launcher + list(spec["probe"]),
                env=self.env,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
            text = (proc.stdout + proc.stderr).decode("utf-8", "replace")
            ok = proc.returncode == 0
            if ok and spec.get("probeContains"):
                ok = spec["probeContains"] in text
        except (OSError, subprocess.SubprocessError):
            ok = False
        self._optional[key] = ok
        return ok

    def run_optional(self, key: str, **kw: Any) -> Dict[str, Any]:
        if not self.available(key):
            return {"hook": key, "available": False, "phase": OPTIONAL_COMMANDS[key]["phase"]}
        res = self.run(key, **kw)
        return {"hook": key, "available": True, "rc": res.rc, "json": res.json(), "seq": res.seq}


# ---------------------------------------------------------------- storage spec

ACCEPTANCE_MARKER = ".relay-acceptance-root"
_PREFIX_RE = re.compile(r"(^|/)relay-acceptance[A-Za-z0-9_.-]*(/|$)")


class StorageError(Exception):
    pass


class StorageSpec(object):
    """Where the acceptance devices meet.

    ``local:<dir>``           a local or LAN-shared folder (simulation, SMB/NFS share)
    ``rclone:<remote>:<prefix>`` an already configured rclone remote; the prefix
                              must contain a ``relay-acceptance...`` segment so
                              acceptance data never mixes with real relay data.
    """

    def __init__(self, kind: str, root: str, remote: str = ""):
        self.kind = kind
        self.root = root
        self.remote = remote

    @classmethod
    def parse(cls, text: str) -> "StorageSpec":
        if text.startswith("local:"):
            path = os.path.abspath(text[len("local:"):])
            if not path:
                raise StorageError("local storage needs a folder path")
            return cls("local", path)
        if text.startswith("rclone:"):
            rest = text[len("rclone:"):]
            if ":" not in rest:
                raise StorageError("rclone storage must look like rclone:<remote>:<prefix>")
            remote, prefix = rest.split(":", 1)
            prefix = prefix.strip("/")
            if not remote or not prefix:
                raise StorageError("rclone storage needs both a remote name and a prefix")
            if not _PREFIX_RE.search(prefix):
                raise StorageError(
                    "rclone prefix %r must contain a 'relay-acceptance...' path segment "
                    "(dedicated acceptance prefix, synthetic projects only)" % prefix
                )
            return cls("rclone", prefix, remote=remote)
        raise StorageError("storage must start with local: or rclone:")

    def describe(self) -> Dict[str, str]:
        return {"kind": self.kind, "root": self.root, "remote": self.remote}

    def prepare_local(self) -> None:
        """Create or re-use a local acceptance folder; refuse foreign folders."""
        if self.kind != "local":
            return
        root = Path(self.root)
        if root.exists():
            entries = [p.name for p in root.iterdir()]
            if entries and ACCEPTANCE_MARKER not in entries:
                raise StorageError(
                    "%s is not empty and was not created by the acceptance tools; refusing to use it" % root
                )
        root.mkdir(parents=True, exist_ok=True)
        marker = root / ACCEPTANCE_MARKER
        if not marker.exists():
            marker.write_text("relay-desk acceptance storage root\n", encoding="utf-8")

    def pair_kwargs(self) -> Dict[str, str]:
        if self.kind == "local":
            return {"key": "pair_local", "root": self.root}
        return {"key": "pair_rclone", "remote": self.remote, "root": self.root}

    def _rclone(self) -> Optional[str]:
        return os.environ.get("RELAY_DESK_RCLONE") or which("rclone")

    def list_keys(self, prefix: str) -> Optional[List[str]]:
        """Read-only listing inside the acceptance root; None when unreachable."""
        prefix = prefix.strip("/")
        if self.kind == "local":
            base = Path(self.root) / prefix if prefix else Path(self.root)
            try:
                if not base.exists():
                    return []
                out = []
                for dirpath, _dirs, files in os.walk(str(base)):
                    for f in files:
                        rel = (Path(dirpath) / f).relative_to(Path(self.root)).as_posix()
                        out.append(rel)
                return sorted(out)
            except OSError:
                return None
        rc = self._rclone()
        if not rc:
            return None
        target = "%s:%s" % (self.remote, "/".join(p for p in (self.root, prefix) if p))
        try:
            proc = subprocess.run(
                [rc, "lsf", "-R", "--files-only", target],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            text = proc.stderr.decode("utf-8", "replace")
            if "directory not found" in text.lower() or "not found" in text.lower():
                return []
            return None
        names = [l.strip() for l in proc.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
        return sorted(("%s/%s" % (prefix, n)) if prefix else n for n in names)

    def reachable(self) -> bool:
        if self.kind == "local":
            try:
                os.listdir(self.root)
                return True
            except OSError:
                return False
        rc = self._rclone()
        if not rc:
            return False
        try:
            proc = subprocess.run(
                [rc, "lsf", "--max-depth", "1", "%s:%s" % (self.remote, self.root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def read_bytes(self, key: str) -> Optional[bytes]:
        """Read one object inside the acceptance root (receipt digests)."""
        key = key.strip("/")
        if self.kind == "local":
            try:
                return (Path(self.root) / key).read_bytes()
            except OSError:
                return None
        rc = self._rclone()
        if not rc:
            return None
        try:
            proc = subprocess.run(
                [rc, "cat", "%s:%s/%s" % (self.remote, self.root, key)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None
