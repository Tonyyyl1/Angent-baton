#!/usr/bin/env python3
"""Record an exact digest of the relay-desk working tree.

The digest includes tracked changes and non-ignored untracked files so
acceptance results can be matched to the exact source that was tested.

Usage:
    python3 acceptance/source_digest.py [--repo DIR] [--out DIR] [--package]

Writes into --out (when given):
    workspace-manifest.json   HEAD, branch, per-file sha256, overall digest
    diff-vs-HEAD.patch        tracked changes against HEAD
    acceptance-source.tar.gz  (--package) every non-ignored file; ignored files
                              such as ledger.json, .env and caches are excluded
Without --out it prints the manifest summary as JSON.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

# Never package local identity, cloud config, credentials or real ledgers,
# even if someone forgets to ignore them.
DENY_NAMES = {"ledger.json", "config.json", ".env", "auth.json", ".claude.json"}
DENY_SUFFIXES = (".pem", ".key", ".backup.json")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def list_files(repo: Path) -> list[str]:
    raw = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    out = []
    for rel in sorted(p for p in raw.split("\0") if p):
        name = os.path.basename(rel)
        if name in DENY_NAMES or name.endswith(DENY_SUFFIXES):
            continue
        if (repo / rel).is_file():
            out.append(rel)
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(repo: Path) -> dict:
    files = list_files(repo)
    digests = {rel: sha256_file(repo / rel) for rel in files}
    overall = hashlib.sha256(
        "".join(f"{rel}\0{digests[rel]}\n" for rel in files).encode("utf-8")
    ).hexdigest()
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    modified = [l[3:] for l in status.splitlines() if l[:2].strip() and not l.startswith("??")]
    untracked = [l[3:] for l in status.splitlines() if l.startswith("??")]
    return {
        "schema": "relay-desk-source-digest/1",
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repository": str(repo),
        "head": _git(repo, "rev-parse", "HEAD").strip(),
        "branch": _git(repo, "branch", "--show-current").strip(),
        "sourceDigest": overall,
        "fileCount": len(files),
        "modified": modified,
        "untracked": untracked,
        "files": digests,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--out")
    ap.add_argument("--package", action="store_true")
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()
    manifest = build_manifest(repo)
    if not args.out:
        summary = {k: v for k, v in manifest.items() if k != "files"}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "workspace-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "diff-vs-HEAD.patch").write_text(_git(repo, "diff", "HEAD"), encoding="utf-8")
    if args.package:
        with tarfile.open(out / "acceptance-source.tar.gz", "w:gz") as tar:
            for rel in manifest["files"]:
                tar.add(repo / rel, arcname=f"relay-desk-src/{rel}")
            info = tarfile.TarInfo("relay-desk-src/SOURCE-DIGEST.json")
            data = json.dumps(
                {k: v for k, v in manifest.items() if k != "files"}, indent=2
            ).encode("utf-8")
            info.size = len(data)
            import io

            tar.addfile(info, io.BytesIO(data))
    print(json.dumps({"sourceDigest": manifest["sourceDigest"], "out": str(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
