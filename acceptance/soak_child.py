#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One soak device: ``relay sync --watch --jsonl`` with optional fault injection and audit hooks.

Usage (environment selects the device: RELAY_DESK_HOME, RELAY_DESK_LEDGER, ...):

    python3 acceptance/soak_child.py --control CTRL.json --audit AUDIT.json [--stats 15] -- sync --watch --jsonl ...

* ``--control``: wrap the storage provider in ``FlakyProvider`` driven by the
  control file (local-folder backend). Omit it for rclone backends, whose
  faults come from the TCP proxy.
* ``--audit``: on exit, write every uploaded key and the number of workspace
  applications, so the driver can prove sync never uploads a receipt and
  never applies a workspace.
* ``--stats``: emit ``stats`` events every N seconds (the CLI has no flag for it).

Production code is not modified: the hooks are installed by wrapping module
attributes in this process only.
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from acceptance.soak_faults import ControlFile, FlakyProvider  # noqa: E402
from relaydesk import relay_engine, storage, workspace_tx  # noqa: E402
from relaydesk import cli  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--control")
    ap.add_argument("--audit")
    ap.add_argument("--stats", type=float, default=15.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    rest = list(args.rest)
    if rest and rest[0] == "--":
        rest = rest[1:]

    uploads = []
    flaky_holder = []
    applied = {"n": 0}

    original_make = storage.make_provider

    def make_provider(cfg):
        inner = original_make(cfg)
        flaky = FlakyProvider(inner, rng=__import__("random").Random(args.seed), record=False)
        if args.control:
            flaky.control = ControlFile(args.control)
        flaky_holder.append(flaky)
        return flaky

    storage.make_provider = make_provider

    original_apply = workspace_tx.prepare_and_apply_workspace

    def counted_apply(*a, **kw):
        applied["n"] += 1
        return original_apply(*a, **kw)

    workspace_tx.prepare_and_apply_workspace = counted_apply
    if hasattr(relay_engine, "prepare_and_apply_workspace"):
        relay_engine.prepare_and_apply_workspace = counted_apply

    original_sync = relay_engine.sync_network

    def sync_with_stats(*a, **kw):
        kw.setdefault("stats_every", args.stats)
        return original_sync(*a, **kw)

    relay_engine.sync_network = sync_with_stats

    code = 1
    try:
        code = cli.main(rest)
    finally:
        if args.audit:
            for f in flaky_holder:
                uploads.extend(f.receipt_uploads)
            audit = {
                "exitCode": code,
                "uploads": sum(f.upload_count for f in flaky_holder),
                "receiptUploads": uploads,
                "workspaceApplications": applied["n"],
                "injectedFaults": sum(f.injected for f in flaky_holder),
                "storageCalls": sum(f.call_count for f in flaky_holder),
                "providersMade": len(flaky_holder),
                "pid": os.getpid(),
            }
            tmp = args.audit + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(audit, fh, indent=1)
            os.replace(tmp, args.audit)
    return code


if __name__ == "__main__":
    sys.exit(main())
