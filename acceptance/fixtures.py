#!/usr/bin/env python3
"""Synthetic acceptance fixtures (never real projects, never real secrets).

    python3 acceptance/fixtures.py git   DEST [--owner mac]
    python3 acceptance/fixtures.py plain DEST [--owner mac]
    python3 acceptance/fixtures.py edit  DEST --actor win --round 2

``git`` builds a repository with commits on two branches, a lightweight and
an annotated tag, staged changes (new, empty, modified, deleted), unstaged
changes (modified, deleted), untracked files, ignored files, Unicode / space /
mixed-case names, a binary file, an executable script and two mock credential
files whose content is a dummy marker ``DUMMY-NOT-A-SECRET-<owner>-<salt>``.

``plain`` builds a non-Git directory with the same kinds of names.

``edit`` is "the user keeps working" between hops: it commits what was
received, then leaves new staged, unstaged and untracked work and the
device's own mock credential and ignored files.

All names avoid case collisions and Windows-reserved names so the same
fixture can travel Mac -> Windows -> Linux; those edge cases are exercised
separately by the native runner probes.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DUMMY_PREFIX = "DUMMY-NOT-A-SECRET"
GIT_IDENTITY = ["-c", "user.name=Relay Acceptance", "-c", "user.email=acceptance@relay.invalid"]
GIT_COMMON = ["-c", "core.quotepath=false", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]
FIXED_DATE = "2026-11-18T09:00:00+00:00"


def new_marker(owner: str) -> str:
    safe = "".join(c for c in owner if c.isalnum() or c in "-_") or "dev"
    return "%s-%s-%s" % (DUMMY_PREFIX, safe, secrets.token_hex(4))


def git(repo: Path, *args: str, check: bool = True, date: Optional[str] = None) -> str:
    env = dict(os.environ)
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    proc = subprocess.run(
        ["git"] + GIT_IDENTITY + GIT_COMMON + ["-C", str(repo)] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if check and proc.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), proc.stderr.decode("utf-8", "replace")))
    return proc.stdout.decode("utf-8", "replace")


def write(root: Path, rel: str, data: Any, executable: bool = False) -> Path:
    p = root.joinpath(*rel.split("/"))
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = data if isinstance(data, bytes) else str(data).encode("utf-8")
    with open(str(p), "wb") as fh:
        fh.write(raw)
    if executable and os.name != "nt":
        os.chmod(str(p), os.stat(str(p)).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _credential_text(marker: str, owner: str) -> str:
    return (
        "# relay-desk acceptance mock credential: dummy value, not a secret\n"
        "RELAY_ACCEPTANCE_OWNER=%s\n"
        "RELAY_ACCEPTANCE_DUMMY_TOKEN=%s\n" % (owner, marker)
    )


# ---------------------------------------------------------------- git fixture

GIT_FILES = {
    "README.md": "# Relay acceptance fixture\n\nSynthetic project for relay-desk acceptance.\n",
    "src/app.py": "def run():\n    return 'v1'\n",
    "src/My Module/Mixed Case.PY": "VALUE = 'mixed case path with spaces'\n",
    "docs/中文 说明.md": "# 中文说明\n用于验收的合成项目。\n",
    "naïve café.txt": "unicode NFC name\n",
    "UPPER.TXT": "upper case name\n",
    "lower.txt": "lower case name\n",
    "both_stages.py": "STAGE = 'committed'\n",
    "to_stage_delete.txt": "deleted in the index before the handoff\n",
    "to_unstage_delete.txt": "deleted in the worktree before the handoff\n",
    ".env.example": "RELAY_ACCEPTANCE_DUMMY_TOKEN=placeholder\n",
}


def make_git_fixture(dest: Path, owner: str = "mac", marker: Optional[str] = None) -> Dict[str, Any]:
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError("fixture destination is not empty: %s" % dest)
    dest.mkdir(parents=True, exist_ok=True)
    marker = marker or new_marker(owner)
    git(dest, "init", "-q")
    git(dest, "symbolic-ref", "HEAD", "refs/heads/main")
    git(dest, "config", "core.autocrlf", "false")

    write(dest, ".gitignore", ".env\n*.log\nbuild/\nnode_modules/\n")
    for rel, text in GIT_FILES.items():
        write(dest, rel, text)
    write(dest, "data/blob.bin", bytes(range(256)) * 4)
    write(dest, "scripts/run.sh", "#!/bin/sh\necho relay acceptance\n", executable=True)
    git(dest, "add", "-A")
    git(dest, "update-index", "--chmod=+x", "scripts/run.sh")
    git(dest, "commit", "-q", "-m", "base: synthetic acceptance project", date=FIXED_DATE)
    git(dest, "tag", "v0.1")

    git(dest, "checkout", "-q", "-b", "feature/side")
    write(dest, "side.txt", "side branch work\n")
    git(dest, "add", "side.txt")
    git(dest, "commit", "-q", "-m", "side branch commit", date="2026-11-18T09:05:00+00:00")
    git(dest, "tag", "-a", "v0.2-annotated", "-m", "annotated acceptance tag", date="2026-11-18T09:06:00+00:00")
    git(dest, "checkout", "-q", "main")
    write(dest, "src/app.py", "def run():\n    return 'v2'\n")
    git(dest, "commit", "-q", "-am", "main: second commit", date="2026-11-18T09:10:00+00:00")

    git(dest, "checkout", "-q", "-b", "feat/ring-acceptance")
    write(dest, "docs/progress.md", "- hop 0 on %s\n" % owner)
    git(dest, "add", "docs/progress.md")
    git(dest, "commit", "-q", "-m", "feat: start ring acceptance", date="2026-11-18T09:15:00+00:00")

    # Staged: new file, empty file, modification, deletion
    write(dest, "src/staged_new.py", "STAGED = True\n")
    write(dest, "src/staged_empty.txt", b"")
    write(dest, "both_stages.py", "STAGE = 'staged'\n")
    git(dest, "add", "src/staged_new.py", "src/staged_empty.txt", "both_stages.py")
    git(dest, "rm", "-q", "to_stage_delete.txt")
    # Unstaged: modification on top of a staged change, plain modification, deletion
    write(dest, "both_stages.py", "STAGE = 'staged'\nWORKTREE = 'unstaged on top'\n")
    write(dest, "README.md", GIT_FILES["README.md"] + "\nUnstaged edit by %s.\n" % owner)
    (dest / "to_unstage_delete.txt").unlink()
    # Untracked (transferred)
    write(dest, "notes/untracked note.md", "untracked design note from %s\n" % owner)
    write(dest, "notes/未跟踪.txt", "未跟踪文件\n")
    write(dest, "scripts/local-tool.sh", "#!/bin/sh\necho untracked executable\n", executable=True)
    # Ignored (never transferred, preserved on the device that owns them)
    write(dest, "build/output.log", "build log on %s\n" % owner)
    write(dest, "debug.log", "debug log on %s\n" % owner)
    write(dest, "node_modules/pkg/index.js", "module.exports = 1;\n")
    # Mock credentials: one ignored (.env) and one untracked by name only (.env.local)
    write(dest, ".env", _credential_text(marker, owner))
    write(dest, ".env.local", _credential_text(marker + "-local", owner))
    return {
        "kind": "git",
        "path": str(dest),
        "owner": owner,
        "marker": marker,
        "branch": "feat/ring-acceptance",
        "branches": ["main", "feature/side", "feat/ring-acceptance"],
        "tags": ["v0.1", "v0.2-annotated"],
        "staged": ["src/staged_new.py", "src/staged_empty.txt", "both_stages.py", "to_stage_delete.txt"],
        "unstaged": ["both_stages.py", "README.md", "to_unstage_delete.txt"],
        "untracked": ["notes/untracked note.md", "notes/未跟踪.txt", "scripts/local-tool.sh"],
        "ignored": ["build/output.log", "debug.log", "node_modules/pkg/index.js"],
        "credentials": [".env", ".env.local"],
    }


# ---------------------------------------------------------------- plain fixture


def make_plain_fixture(dest: Path, owner: str = "mac", marker: Optional[str] = None) -> Dict[str, Any]:
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError("fixture destination is not empty: %s" % dest)
    dest.mkdir(parents=True, exist_ok=True)
    marker = marker or new_marker(owner)
    write(dest, "README.txt", "plain folder acceptance fixture from %s\n" % owner)
    write(dest, "sub dir/中文 文件.txt", "中文内容\n")
    write(dest, "nested/deeper/data.bin", bytes(range(255, -1, -1)) * 2)
    write(dest, "tools/run.sh", "#!/bin/sh\necho plain\n", executable=True)
    write(dest, "empty.txt", b"")
    write(dest, "naïve café.txt", "unicode NFC name\n")
    write(dest, "MixedCase/File Name.TXT", "mixed case\n")
    write(dest, ".env", _credential_text(marker, owner))
    write(dest, ".DS_Store", b"\x00\x00\x00\x01Bud1")
    return {
        "kind": "plain",
        "path": str(dest),
        "owner": owner,
        "marker": marker,
        "credentials": [".env"],
        "system": [".DS_Store"],
    }


# ---------------------------------------------------------------- edits between hops


def _secret_like(rel: str) -> bool:
    from acceptance.compare_state import is_secret_name

    return is_secret_name(rel)


def stage_all_except_secrets(repo: Path) -> None:
    git(repo, "add", "-A")
    staged = git(repo, "diff", "--cached", "--name-only", "-z").split("\0")
    secret = [p for p in staged if p and _secret_like(p)]
    if secret:
        git(repo, "reset", "-q", "--", *secret)


def edit_git_workspace(
    repo: Path, actor: str, round_no: int, marker: Optional[str] = None, ignored_output: bool = False
) -> Dict[str, Any]:
    """Commit the received state, then leave new staged/unstaged/untracked work.

    ``ignored_output`` also leaves an ignored build file.  It is off for the
    ring: relay-desk currently refuses an in-place receive into a Git
    workspace holding non-credential ignored files (exercised once, at R10).
    """
    repo = Path(repo)
    marker = marker or new_marker(actor)
    date = "2026-11-18T%02d:%02d:00+00:00" % (10 + round_no % 12, (round_no * 7) % 60)
    stage_all_except_secrets(repo)
    git(repo, "commit", "-q", "--allow-empty", "-m", "hop %d: %s continues the work" % (round_no, actor), date=date)
    tag = "hop-%d-%s" % (round_no, "".join(c for c in actor if c.isalnum()))
    git(repo, "tag", tag)
    with open(str(repo / "docs" / "progress.md"), "a", encoding="utf-8") as fh:
        fh.write("- hop %d on %s\n" % (round_no, actor))
    git(repo, "add", "docs/progress.md")
    write(repo, "hops/%s-%d staged.txt" % (actor, round_no), "staged on %s round %d\n" % (actor, round_no))
    git(repo, "add", "hops/%s-%d staged.txt" % (actor, round_no))
    app = repo / "src" / "app.py"
    with open(str(app), "a", encoding="utf-8") as fh:
        fh.write("# unstaged edit by %s round %d\n" % (actor, round_no))
    write(repo, "hops/%s-%d 未跟踪.md" % (actor, round_no), "untracked on %s\n" % actor)
    write(repo, ".env", _credential_text(marker, actor))
    if ignored_output:
        write(repo, "build/%s.log" % actor, "ignored build output on %s\n" % actor)
    return {"actor": actor, "round": round_no, "tag": tag, "marker": marker, "ignoredOutput": ignored_output}


def edit_plain_folder(root: Path, actor: str, round_no: int, marker: Optional[str] = None) -> Dict[str, Any]:
    root = Path(root)
    marker = marker or new_marker(actor)
    with open(str(root / "README.txt"), "a", encoding="utf-8") as fh:
        fh.write("round %d edited on %s\n" % (round_no, actor))
    write(root, "rounds/%s-%d.txt" % (actor, round_no), "new file from %s\n" % actor)
    if (root / "empty.txt").exists():
        (root / "empty.txt").unlink()
    write(root, ".env", _credential_text(marker, actor))
    return {"actor": actor, "round": round_no, "marker": marker}


def scribble(path: Path, actor: str) -> Dict[str, Any]:
    """A small uncommitted edit made after a send ("user keeps editing")."""
    path = Path(path)
    target = path / "KEEP-EDITING.md"
    with open(str(target), "a", encoding="utf-8") as fh:
        fh.write("edit by %s after the handoff was sent\n" % actor)
    from acceptance.common import sha256_file

    return {"file": "KEEP-EDITING.md", "sha256": sha256_file(target)}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="relay-desk acceptance fixtures")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("git")
    g.add_argument("dest")
    g.add_argument("--owner", default="mac")
    p = sub.add_parser("plain")
    p.add_argument("dest")
    p.add_argument("--owner", default="mac")
    e = sub.add_parser("edit")
    e.add_argument("dest")
    e.add_argument("--actor", required=True)
    e.add_argument("--round", type=int, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "git":
        res = make_git_fixture(Path(args.dest), owner=args.owner)
    elif args.cmd == "plain":
        res = make_plain_fixture(Path(args.dest), owner=args.owner)
    else:
        if (Path(args.dest) / ".git").exists():
            res = edit_git_workspace(Path(args.dest), args.actor, args.round)
        else:
            res = edit_plain_folder(Path(args.dest), args.actor, args.round)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
