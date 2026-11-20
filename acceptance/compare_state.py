#!/usr/bin/env python3
"""Capture and compare workspace state across a relay-desk handoff.

    python3 acceptance/compare_state.py capture PATH --out STATE.json [--raw-dir DIR]
    python3 acceptance/compare_state.py compare --sender A.json --receiver B.json \
        [--receiver-pre C.json] [--out REPORT.json]
    python3 acceptance/compare_state.py summary PATH

The comparison covers commits, branch, refs and tags, the Git index, staged
and unstaged diffs, untracked files, ignored files, mock credentials, the
worktree content, and (when relay records are attached) handoff history and
receipt digests.  The report is JSON with one entry per check.

Expected semantics (from the relay-desk protocol):

* commits, branch, sender refs/tags, index, staged/unstaged changes, untracked
  files and other non-ignored content arrive exactly;
* ignored files and credential-named files never travel; whatever the
  receiver had of them before the handoff stays byte-identical;
* the sender's dummy credential marker never appears on the receiver.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptance.common import canonical_json, iso_now, platform_info, sha256_bytes, write_json  # noqa: E402

SCHEMA = "relay-desk-acceptance-state/1"
REPORT_SCHEMA = "relay-desk-acceptance-compare/1"
NOTE_PREFIX = "relay-acceptance-state:"
MARKER_RE = re.compile(rb"DUMMY-NOT-A-SECRET-[A-Za-z0-9_-]+")
SCAN_LIMIT = 8 * 1024 * 1024

# Mirror of relay-desk's credential-name rule (relaydesk/packager.py); the
# unit tests check that both agree on the fixture names.
SECRET_EXACT = {
    ".env", ".credentials.json", "auth.json", "oauth_creds.json", ".claude.json",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
}
SECRET_TEMPLATES = {".env.example", ".env.sample", ".env.template"}
SECRET_EXTENSIONS = (".pem", ".key", ".p12", ".pfx")
SYSTEM_NAMES = {".ds_store"}
NULL_OID = "0" * 40


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def is_secret_name(rel: str) -> bool:
    base = _nfc(rel.replace("\\", "/")).strip("/").split("/")[-1].lower()
    if not base or base in SECRET_TEMPLATES:
        return False
    if base in SECRET_EXACT or base.startswith(".env."):
        return True
    return base.endswith(SECRET_EXTENSIONS)


def is_system_name(rel: str) -> bool:
    return any(seg.lower() in SYSTEM_NAMES for seg in _nfc(rel.replace("\\", "/")).split("/"))


# ---------------------------------------------------------------- git helpers


def _git_bytes(repo: Path, *args: str) -> Tuple[int, bytes]:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repo)] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.returncode, proc.stdout


def _git_text(repo: Path, *args: str) -> Tuple[int, str]:
    rc, out = _git_bytes(repo, *args)
    return rc, out.decode("utf-8", "replace")


def _nul_paths(raw: bytes) -> List[str]:
    return sorted(_nfc(p.decode("utf-8", "replace")) for p in raw.split(b"\0") if p)


def _parse_raw_diff(raw: bytes) -> Dict[str, Dict[str, str]]:
    """Parse ``git diff --raw -z --full-index --no-renames`` output."""
    out: Dict[str, Dict[str, str]] = {}
    parts = raw.split(b"\0")
    i = 0
    while i < len(parts):
        head = parts[i].decode("utf-8", "replace")
        if not head.startswith(":"):
            i += 1
            continue
        fields = head[1:].split()
        path = _nfc(parts[i + 1].decode("utf-8", "replace")) if i + 1 < len(parts) else ""
        i += 2
        if len(fields) >= 5:
            out[path] = {
                "srcMode": fields[0],
                "dstMode": fields[1],
                "srcOid": fields[2],
                "dstOid": fields[3],
                "status": fields[4][:1],
            }
    return out


def _capture_git(repo: Path, raw_dir: Optional[Path]) -> Dict[str, Any]:
    g: Dict[str, Any] = {}
    rc, sym = _git_text(repo, "symbolic-ref", "-q", "HEAD")
    g["symbolicHead"] = sym.strip() if rc == 0 else ""
    rc, head = _git_text(repo, "rev-parse", "--verify", "-q", "HEAD")
    g["head"] = head.strip() if rc == 0 else ""
    rc, refs_raw = _git_text(
        repo, "for-each-ref", "--format=%(refname)%09%(objectname)%09%(*objectname)", "refs/heads", "refs/tags"
    )
    refs: Dict[str, str] = {}
    peeled: Dict[str, str] = {}
    for line in refs_raw.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2:
            refs[_nfc(cols[0])] = cols[1]
            if len(cols) >= 3 and cols[2]:
                peeled[_nfc(cols[0])] = cols[2]
    g["refs"] = dict(sorted(refs.items()))
    g["peeledTags"] = dict(sorted(peeled.items()))
    rc, idx_raw = _git_bytes(repo, "ls-files", "--stage", "-z")
    index = []
    for ent in idx_raw.split(b"\0"):
        if not ent:
            continue
        meta, _, path = ent.partition(b"\t")
        mode, oid, stage = meta.decode("ascii", "replace").split()
        index.append({"path": _nfc(path.decode("utf-8", "replace")), "mode": mode, "oid": oid, "stage": stage})
    index.sort(key=lambda e: (e["path"], e["stage"]))
    g["index"] = index
    g["indexSha256"] = sha256_bytes(canonical_json(index))
    rc, staged_raw = _git_bytes(repo, "diff", "--cached", "--raw", "-z", "--full-index", "--no-renames")
    g["stagedEntries"] = _parse_raw_diff(staged_raw)
    rc, unstaged_raw = _git_bytes(repo, "diff", "--raw", "-z", "--full-index", "--no-renames")
    g["unstagedEntries"] = _parse_raw_diff(unstaged_raw)
    rc, sd = _git_bytes(repo, "diff", "--cached", "--binary", "--full-index", "--no-renames")
    rc2, ud = _git_bytes(repo, "diff", "--binary", "--full-index", "--no-renames")
    g["stagedDiff"] = {"sha256": sha256_bytes(sd), "size": len(sd)}
    g["unstagedDiff"] = {"sha256": sha256_bytes(ud), "size": len(ud)}
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "staged.diff").write_bytes(sd)
        (raw_dir / "unstaged.diff").write_bytes(ud)
    rc, un = _git_bytes(repo, "ls-files", "--others", "--exclude-standard", "-z")
    g["untracked"] = _nul_paths(un)
    rc, ig = _git_bytes(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    g["ignored"] = _nul_paths(ig)
    rc, ver = _git_text(repo, "--version")
    g["gitVersion"] = ver.strip()
    return g


# ---------------------------------------------------------------- capture


def _walk_files(root: Path) -> Dict[str, Dict[str, Any]]:
    files: Dict[str, Dict[str, Any]] = {}
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                rel = _nfc(os.path.relpath(full, str(root)).replace(os.sep, "/"))
                files[rel] = {"symlink": os.readlink(full), "sha256": sha256_bytes(os.readlink(full).encode("utf-8"))}
                dirnames.remove(name)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = _nfc(os.path.relpath(full, str(root)).replace(os.sep, "/"))
            if os.path.islink(full):
                target = os.readlink(full)
                files[rel] = {"symlink": target, "sha256": sha256_bytes(target.encode("utf-8"))}
                continue
            try:
                with open(full, "rb") as fh:
                    data = fh.read()
                st = os.stat(full)
            except OSError as e:
                files[rel] = {"error": str(e)}
                continue
            files[rel] = {
                "sha256": sha256_bytes(data),
                "size": len(data),
                "exec": None if os.name == "nt" else bool(st.st_mode & 0o100),
                "markers": sorted({m.decode("ascii") for m in MARKER_RE.findall(data[:SCAN_LIMIT])}),
            }
    return files


def capture_state(path: Path, raw_dir: Optional[Path] = None, label: str = "") -> Dict[str, Any]:
    path = Path(path)
    st: Dict[str, Any] = {
        "schema": SCHEMA,
        "label": label,
        "capturedAt": iso_now(),
        "path": str(path),
        "system": platform_info()["system"],
        "exists": path.is_dir(),
    }
    if not st["exists"]:
        return st
    is_git = (path / ".git").exists()
    st["mode"] = "git" if is_git else "folder"
    files = _walk_files(path)
    ignored = set()
    untracked: List[str] = []
    if is_git:
        st["git"] = _capture_git(path, raw_dir)
        ignored = set(st["git"]["ignored"])
        untracked = st["git"]["untracked"]
    buckets: Dict[str, Dict[str, Any]] = {"content": {}, "ignored": {}, "secret": {}, "system": {}}
    markers: Dict[str, List[str]] = {}
    for rel, info in sorted(files.items()):
        for m in info.get("markers") or []:
            markers.setdefault(m, []).append(rel)
        if is_secret_name(rel):
            buckets["secret"][rel] = info
        elif rel in ignored:
            buckets["ignored"][rel] = info
        elif is_system_name(rel):
            buckets["system"][rel] = info
        else:
            buckets["content"][rel] = info
    st["files"] = buckets
    st["untrackedTransferable"] = [p for p in untracked if not is_secret_name(p) and not is_system_name(p)]
    st["credentialMarkers"] = markers
    st["ownMarkers"] = sorted({m for rel, info in buckets["secret"].items() for m in (info.get("markers") or [])})
    return st


def comparable(st: Dict[str, Any]) -> Dict[str, Any]:
    """Everything that must stay identical when a workspace is 'left untouched'."""
    files = {
        bucket: {p: v.get("sha256") for p, v in ((st.get("files") or {}).get(bucket) or {}).items()}
        for bucket in ("content", "ignored", "secret", "system")
    }
    g = st.get("git") or {}
    keys = ("head", "symbolicHead", "refs", "index", "stagedEntries", "unstagedEntries", "untracked", "ignored",
            "stagedDiff", "unstagedDiff")
    return {"exists": st.get("exists"), "mode": st.get("mode"), "files": files, "git": {k: g.get(k) for k in keys}}


def state_delta(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Which parts differ between two captures of the same workspace."""
    a, b = comparable(before), comparable(after)
    delta: Dict[str, Any] = {}
    for key in ("exists", "mode"):
        if a[key] != b[key]:
            delta[key] = {"before": a[key], "after": b[key]}
    for bucket in a["files"]:
        d = _map_diff(a["files"][bucket], b["files"][bucket])
        if not _empty(d):
            delta["files." + bucket] = d
    for key in a["git"]:
        if a["git"][key] != b["git"][key]:
            delta["git." + key] = "changed"
    return delta


def _content_map(st: Dict[str, Any]) -> Dict[str, str]:
    return {p: v.get("sha256", "") for p, v in ((st.get("files") or {}).get("content") or {}).items()}


def _filtered_index(st: Dict[str, Any]) -> List[Dict[str, str]]:
    return [e for e in (st.get("git") or {}).get("index") or [] if not is_secret_name(e["path"])]


def _staged_view(st: Dict[str, Any]) -> Dict[str, Any]:
    ents = (st.get("git") or {}).get("stagedEntries") or {}
    return {p: e for p, e in ents.items() if not is_secret_name(p)}


def _unstaged_view(st: Dict[str, Any]) -> Dict[str, Any]:
    ents = (st.get("git") or {}).get("unstagedEntries") or {}
    content = _content_map(st)
    out = {}
    for p, e in ents.items():
        if is_secret_name(p):
            continue
        out[p] = {"status": e["status"], "srcMode": e["srcMode"], "srcOid": e["srcOid"], "worktree": content.get(p)}
    return out


def transfer_summary(st: Dict[str, Any]) -> Dict[str, Any]:
    """Compact per-category digests of everything a handoff must carry."""
    g = st.get("git") or {}
    untracked = {p: _content_map(st).get(p) for p in st.get("untrackedTransferable") or []}
    digests = {
        "content": sha256_bytes(canonical_json(_content_map(st))),
        "untracked": sha256_bytes(canonical_json(untracked)),
    }
    if st.get("mode") == "git":
        digests.update(
            {
                "head": g.get("head"),
                "branch": g.get("symbolicHead"),
                "index": sha256_bytes(canonical_json(_filtered_index(st))),
                "staged": sha256_bytes(canonical_json(_staged_view(st))),
                "unstaged": sha256_bytes(canonical_json(_unstaged_view(st))),
                "stagedDiff": (g.get("stagedDiff") or {}).get("sha256"),
                "unstagedDiff": (g.get("unstagedDiff") or {}).get("sha256"),
            }
        )
    return {
        "v": 1,
        "mode": st.get("mode"),
        "digests": digests,
        "refs": g.get("refs") or {},
        "markers": st.get("ownMarkers") or [],
    }


def encode_note(summary: Dict[str, Any]) -> str:
    return NOTE_PREFIX + json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_note(note: str) -> Optional[Dict[str, Any]]:
    if not isinstance(note, str) or not note.startswith(NOTE_PREFIX):
        return None
    try:
        return json.loads(note[len(NOTE_PREFIX):])
    except ValueError:
        return None


# ---------------------------------------------------------------- comparison


class _Report(object):
    def __init__(self) -> None:
        self.checks: List[Dict[str, Any]] = []

    def add(self, name: str, ok: bool, severity: str = "error", **details: Any) -> None:
        entry = {"check": name, "ok": bool(ok), "severity": severity}
        entry.update({k: v for k, v in details.items() if v not in (None, [], {})})
        self.checks.append(entry)

    def result(self, **extra: Any) -> Dict[str, Any]:
        failed = [c["check"] for c in self.checks if not c["ok"] and c["severity"] == "error"]
        warned = [c["check"] for c in self.checks if not c["ok"] and c["severity"] != "error"]
        out = {"schema": REPORT_SCHEMA, "createdAt": iso_now(), "ok": not failed, "failed": failed, "warnings": warned}
        out.update(extra)
        out["checks"] = self.checks
        return out


def _map_diff(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, List[str]]:
    return {
        "onlyInSender": sorted(set(a) - set(b)),
        "onlyInReceiver": sorted(set(b) - set(a)),
        "changed": sorted(p for p in set(a) & set(b) if a[p] != b[p]),
    }


def _empty(d: Dict[str, List[str]]) -> bool:
    return not any(d.values())


def compare_summaries(sender: Dict[str, Any], receiver: Dict[str, Any]) -> Dict[str, Any]:
    """On-device check: sender's embedded summary vs the receiver's capture."""
    rep = _Report()
    sd, rd = sender.get("digests") or {}, receiver.get("digests") or {}
    rep.add("mode", sender.get("mode") == receiver.get("mode"), sender=sender.get("mode"), receiver=receiver.get("mode"))
    for key in ("head", "branch", "index", "staged", "unstaged", "untracked", "content"):
        if key in sd or key in rd:
            rep.add(key, sd.get(key) == rd.get(key), sender=sd.get(key), receiver=rd.get(key))
    for key in ("stagedDiff", "unstagedDiff"):
        if key in sd or key in rd:
            base = key.replace("Diff", "")
            sev = "warning" if sd.get(base) == rd.get(base) else "error"
            rep.add(key, sd.get(key) == rd.get(key), severity=sev)
    s_refs, r_refs = sender.get("refs") or {}, receiver.get("refs") or {}
    missing = sorted(r for r in s_refs if r not in r_refs)
    moved = sorted(r for r in s_refs if r in r_refs and r_refs[r] != s_refs[r])
    rep.add("refs", not missing and not moved, missing=missing, moved=moved,
            receiverOnly=sorted(set(r_refs) - set(s_refs)))
    leaked = sorted(set(sender.get("markers") or []) & set(receiver.get("markersSeen") or []))
    rep.add("credentialNotTransferred", not leaked, leaked=leaked)
    return rep.result(kind="summary")


def compare_states(
    sender: Dict[str, Any],
    receiver: Dict[str, Any],
    receiver_pre: Optional[Dict[str, Any]] = None,
    relay: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    rep = _Report()
    rep.add("receiverExists", bool(receiver.get("exists")), path=receiver.get("path"))
    if not receiver.get("exists"):
        return rep.result(sender=sender.get("label"), receiver=receiver.get("label"))
    rep.add("mode", sender.get("mode") == receiver.get("mode"), sender=sender.get("mode"), receiver=receiver.get("mode"))
    sg, rg = sender.get("git") or {}, receiver.get("git") or {}
    pre_g = (receiver_pre or {}).get("git") or {}
    if sender.get("mode") == "git":
        rep.add("commits", sg.get("head") == rg.get("head"), sender=sg.get("head"), receiver=rg.get("head"))
        rep.add("branch", sg.get("symbolicHead") == rg.get("symbolicHead"),
                sender=sg.get("symbolicHead"), receiver=rg.get("symbolicHead"))
        s_refs, r_refs = sg.get("refs") or {}, rg.get("refs") or {}
        pre_refs = pre_g.get("refs") or {}
        for label, prefix in (("branches", "refs/heads/"), ("tags", "refs/tags/")):
            s_sel = {k: v for k, v in s_refs.items() if k.startswith(prefix)}
            r_sel = {k: v for k, v in r_refs.items() if k.startswith(prefix)}
            missing = sorted(k for k in s_sel if k not in r_sel)
            moved = sorted(k for k in s_sel if k in r_sel and r_sel[k] != s_sel[k])
            extra = sorted(set(r_sel) - set(s_sel))
            unexplained = [k for k in extra if receiver_pre is not None and k not in pre_refs]
            rep.add(label, not missing and not moved, missing=missing, moved=moved, receiverOnly=extra)
            if receiver_pre is not None:
                rep.add(label + "ReceiverOnlyPreexisting", not unexplained, severity="warning", unexplained=unexplained)
        s_idx = {e["path"] + "#" + e["stage"]: e for e in _filtered_index(sender)}
        r_idx = {e["path"] + "#" + e["stage"]: e for e in _filtered_index(receiver)}
        d = _map_diff(s_idx, r_idx)
        rep.add("gitIndex", _empty(d), **d)
        d = _map_diff(_staged_view(sender), _staged_view(receiver))
        staged_ok = _empty(d)
        rep.add("stagedChanges", staged_ok, **d)
        rep.add("stagedDiffBytes", sg.get("stagedDiff") == rg.get("stagedDiff"),
                severity="warning" if staged_ok else "error",
                sender=sg.get("stagedDiff"), receiver=rg.get("stagedDiff"))
        d = _map_diff(_unstaged_view(sender), _unstaged_view(receiver))
        unstaged_ok = _empty(d)
        rep.add("unstagedChanges", unstaged_ok, **d)
        rep.add("unstagedDiffBytes", sg.get("unstagedDiff") == rg.get("unstagedDiff"),
                severity="warning" if unstaged_ok else "error",
                sender=sg.get("unstagedDiff"), receiver=rg.get("unstagedDiff"))
        s_un = {p: _content_map(sender).get(p) for p in sender.get("untrackedTransferable") or []}
        r_un = {p: _content_map(receiver).get(p) for p in receiver.get("untrackedTransferable") or []}
        d = _map_diff(s_un, r_un)
        rep.add("untracked", _empty(d), **d)
    # Worktree content (everything that is neither ignored nor a credential nor a system file)
    s_content, r_content = _content_map(sender), _content_map(receiver)
    d = _map_diff(s_content, r_content)
    rep.add("worktreeContent", _empty(d), **d)
    s_files = (sender.get("files") or {}).get("content") or {}
    r_files = (receiver.get("files") or {}).get("content") or {}
    exec_diff, exec_skipped = [], 0
    for p in sorted(set(s_files) & set(r_files)):
        a, b = s_files[p].get("exec"), r_files[p].get("exec")
        if a is None or b is None:
            exec_skipped += 1
        elif a != b:
            exec_diff.append(p)
    rep.add("execBits", not exec_diff, changed=exec_diff,
            note=("%d files skipped (Windows side has no exec bit)" % exec_skipped) if exec_skipped else None)
    # Ignored files
    s_ign = (sender.get("files") or {}).get("ignored") or {}
    r_ign = (receiver.get("files") or {}).get("ignored") or {}
    pre_ign = ((receiver_pre or {}).get("files") or {}).get("ignored") or {}
    # A sender-only ignored file showing up on the receiver with the sender's bytes
    # means it travelled; files the receiver already had are judged by the
    # preservation check below.
    arrived = sorted(
        p for p in s_ign
        if p in r_ign and p not in pre_ign and r_ign[p].get("sha256") == s_ign[p].get("sha256")
    )
    rep.add("ignoredNotTransferred", not arrived, transferred=arrived)
    if receiver_pre is not None:
        lost = sorted(p for p, v in pre_ign.items() if (r_ign.get(p) or {}).get("sha256") != v.get("sha256"))
        rep.add("receiverIgnoredPreserved", not lost, changedOrMissing=lost)
    # Mock credentials
    s_markers = set(sender.get("ownMarkers") or [])
    r_seen = set((receiver.get("credentialMarkers") or {}).keys())
    pre_seen = set(((receiver_pre or {}).get("credentialMarkers") or {}).keys())
    leaked = sorted((s_markers & r_seen) - pre_seen)
    rep.add("mockCredentialNotTransferred", not leaked, leaked=leaked,
            where={m: receiver["credentialMarkers"][m] for m in leaked})
    if receiver_pre is not None:
        pre_sec = ((receiver_pre.get("files") or {}).get("secret")) or {}
        r_sec = (receiver.get("files") or {}).get("secret") or {}
        changed = sorted(p for p, v in pre_sec.items() if (r_sec.get(p) or {}).get("sha256") != v.get("sha256"))
        rep.add("receiverCredentialPreserved", not changed, changedOrMissing=changed)
    if relay:
        for c in compare_relay_records(**relay)["checks"]:
            rep.checks.append(c)
    return rep.result(sender=sender.get("label"), receiver=receiver.get("label"))


def compare_relay_records(
    send: Optional[Dict[str, Any]] = None,
    receive: Optional[Dict[str, Any]] = None,
    receiver_record: Optional[Dict[str, Any]] = None,
    receiver_history: Optional[List[Dict[str, Any]]] = None,
    sender_record: Optional[Dict[str, Any]] = None,
    sender_history: Optional[List[Dict[str, Any]]] = None,
    cloud_receipt: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Handoff history and receipt digest checks.

    ``send``/``receive`` are the ``--json`` outputs of ``relay send`` and
    ``relay receive``; ``receiver_record``/``sender_record`` are the
    receivedRelays / sentRelays entries read (read-only) from the device's
    acceptance state directory; ``cloud_receipt`` is the parsed receipt object
    from storage when the acceptance tools can read it.
    """
    rep = _Report()
    send = send or {}
    rid = send.get("relayId") or (receive or {}).get("relayId")
    if receive is not None:
        rep.add("receiveMatchesSend", receive.get("relayId") == rid and receive.get("snapshotId") == send.get("snapshotId")
                and receive.get("projectId") == send.get("projectId"),
                relayId=rid, sendSnapshot=send.get("snapshotId"), receiveSnapshot=receive.get("snapshotId"))
    if receiver_record is not None:
        rec = receiver_record.get("signedReceipt") or {}
        ok = (
            rec.get("relayId") == rid
            and rec.get("packageSha256") == send.get("packageSha256")
            and rec.get("snapshotId", send.get("snapshotId")) == send.get("snapshotId")
            and rec.get("receiverDeviceId") == send.get("toDeviceId")
            and rec.get("projectId") == send.get("projectId")
        )
        rep.add("receiptMatchesSend", ok, relayId=rid, receiptDigest=sha256_bytes(canonical_json(rec)) if rec else None,
                packageSha256=send.get("packageSha256"), receiptPackageSha256=rec.get("packageSha256"))
        rep.add("receiptUploaded", bool(receiver_record.get("receiptUploaded")))
        if cloud_receipt is not None:
            rep.add("cloudReceiptEqualsLocal", canonical_json(cloud_receipt) == canonical_json(rec),
                    cloudDigest=sha256_bytes(canonical_json(cloud_receipt)))
    if receiver_history is not None:
        hit = [h for h in receiver_history if (h.get("relayId") or h.get("tag")) == rid]
        rep.add("receiverHistory", bool(hit), entries=hit)
    if sender_record is not None:
        rep.add("senderAcked", bool(sender_record.get("acked")), ackedAt=sender_record.get("ackedAt"))
    if sender_history is not None:
        hit = [h for h in sender_history if (h.get("relayId") or h.get("tag")) == rid]
        rep.add("senderHistory", bool(hit), entries=hit)
    return rep.result(kind="relay", relayId=rid)


# ---------------------------------------------------------------- CLI


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="relay-desk acceptance state capture / compare")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("path")
    c.add_argument("--out", required=True)
    c.add_argument("--raw-dir")
    c.add_argument("--label", default="")
    m = sub.add_parser("compare")
    m.add_argument("--sender", required=True)
    m.add_argument("--receiver", required=True)
    m.add_argument("--receiver-pre")
    m.add_argument("--out")
    s = sub.add_parser("summary")
    s.add_argument("path")
    args = ap.parse_args(argv)
    if args.cmd == "capture":
        st = capture_state(Path(args.path), Path(args.raw_dir) if args.raw_dir else None, label=args.label)
        write_json(Path(args.out), st)
        print(json.dumps({"out": args.out, "summary": transfer_summary(st)}, ensure_ascii=False))
        return 0
    if args.cmd == "summary":
        print(json.dumps(transfer_summary(capture_state(Path(args.path))), ensure_ascii=False, indent=2))
        return 0

    def load(p: Optional[str]) -> Optional[Dict[str, Any]]:
        if not p:
            return None
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)

    rep = compare_states(load(args.sender), load(args.receiver), load(args.receiver_pre))  # type: ignore[arg-type]
    if args.out:
        write_json(Path(args.out), rep)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
