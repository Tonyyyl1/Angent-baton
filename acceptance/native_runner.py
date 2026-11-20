#!/usr/bin/env python3
"""Per-platform native acceptance runner for relay-desk.

Run it on the machine being accepted (macOS, Windows or Linux); the thin
wrappers ``run-native.sh`` / ``run-native.ps1`` call this file.

    python3 acceptance/native_runner.py [--results-root DIR] [--quick]
        [--pattern 'test*.py' | --tests tests.test_x ...] [--skip-unittest]
        [--skip-probes] [--skip-loopback] [--no-state-guard]

Writes ``<results-root>/<UTC>_<level>_<digest12>_native/`` containing:

  env.json                 environment probe (collect_env.py)
  unittest-<variant>.log   raw verbose unittest output
  unittest-<variant>.json  every test with status; every skip with its reason
  probes.json              platform probes (platform_probes.py)
  loopback/                two relay instances on this machine through the real
                           CLI: Git and folder round trip, kill -9 recovery,
                           receive while a workspace file is held open
  commands.jsonl, raw/     every command line with raw stdout/stderr
  summary.json             per-section status; never "passed" unless it ran

``level`` is detected from ``platform`` (native-macos / native-windows /
native-linux, or wsl-linux).  The loopback uses one machine, so it is native
OS/filesystem evidence and never multi-device evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptance import common  # noqa: E402
from acceptance.collect_env import collect as collect_env  # noqa: E402

OS_LABEL = {"Darwin": "mac", "Windows": "windows", "Linux": "linux"}


def _stat_fingerprint(path: Path) -> Optional[Dict[str, Any]]:
    """Existence / size / mtime only: lets us notice a change without reading the file."""
    try:
        st = os.stat(str(path))
    except OSError:
        return None
    return {"size": st.st_size, "mtimeNs": st.st_mtime_ns}


def _tree_fingerprint(path: Path) -> Optional[List[Any]]:
    if not path.exists():
        return None
    out = []
    for dirpath, _dirs, files in os.walk(str(path)):
        for f in sorted(files):
            fp = _stat_fingerprint(Path(dirpath) / f)
            out.append([os.path.relpath(os.path.join(dirpath, f), str(path)), fp])
    return sorted(out)


def run_unittest(results: Path, log: common.CommandLog, variant: str, args: argparse.Namespace,
                 extra_env: Dict[str, str], work: Path) -> Dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env)
    guard_dir = None
    if not args.no_state_guard:
        # Keep any test that forgets to isolate itself away from the real
        # ~/.relay-desk; RELAY_DESK_LEDGER is left alone because a ledger test
        # asserts the default path.
        guard_dir = Path(tempfile.mkdtemp(prefix="guard-%s-" % variant, dir=str(work)))
        env["RELAY_DESK_HOME"] = str(guard_dir / "relay-home")
        env["RELAY_DESK_USER_HOME"] = str(guard_dir / "user-home")
    log_path = results / ("unittest-%s.log" % variant)
    json_path = results / ("unittest-%s.json" % variant)
    argv = [sys.executable, "-m", "acceptance.unittest_capture", "--log", str(log_path), "--json", str(json_path),
            "--start", "tests"]
    if args.tests:
        argv += ["--names"] + list(args.tests)
    else:
        argv += ["--pattern", args.pattern]
    t0 = time.time()
    res = log.run(argv, env=env, cwd=str(common.REPO_ROOT), timeout=args.unittest_timeout, tag="unittest-" + variant)
    data = common.read_json(json_path, {}) or {}
    full_suite = not args.tests and args.pattern == "test*.py"
    status = "passed" if res.rc == 0 and data.get("wasSuccessful") else ("failed" if data else "error")
    return {
        "variant": variant,
        "status": status,
        "selection": "full-suite" if full_suite else "subset",
        "rc": res.rc,
        "durationSec": round(time.time() - t0, 1),
        "testsRun": data.get("testsRun"),
        "counts": data.get("counts"),
        "skipped": data.get("skipped"),
        "failures": [{"test": f["test"], "status": f["status"]} for f in data.get("failures") or []],
        "log": log_path.name,
        "json": json_path.name,
        "stateGuard": str(guard_dir) if guard_dir else None,
        "env": {k: v for k, v in extra_env.items()},
    }


def run_loopback(results: Path, info: Dict[str, Any], level: str, work: Path) -> Dict[str, Any]:
    from acceptance import ring

    doc = ring.load_doc(str(Path(__file__).resolve().parent / "steps" / "loopback-2device.json"))
    label = OS_LABEL.get(info["system"], "linux")
    for spec in doc["roles"].values():
        spec["osLabel"] = label
        spec["system"] = info["system"]
    out_dir = results / "loopback"
    out_dir.mkdir(parents=True, exist_ok=True)
    note = ("Two isolated relay instances on this %s machine through the real CLI and a local folder as storage. "
            "Native OS and filesystem evidence; not multi-device evidence." % info["system"])
    res = ring.run_local(doc, out_dir, level, "single-machine-loopback", note,
                         sim_root=work / "loopback")
    return {
        "status": "passed" if res["allPassed"] else "failed",
        "topology": "single-machine-loopback",
        "stepCounts": res["stepCounts"],
        "scenarios": res["scenarios"],
        "failedSteps": [k for k, v in res["steps"].items() if v not in ("passed", "skipped")],
        "comparisons": [{k: c.get(k) for k in ("receiveStep", "ok", "failed", "warnings")}
                        for c in res["crossDeviceComparisons"]],
        "summary": "loopback/ring-summary.json",
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="relay-desk native acceptance runner")
    ap.add_argument("--results-root", help="default: acceptance/results")
    ap.add_argument("--pattern", default="test*.py", help="unittest discovery pattern (default: full suite)")
    ap.add_argument("--tests", nargs="*", help="run only these test names (module or Class.method)")
    ap.add_argument("--skip-unittest", action="store_true")
    ap.add_argument("--skip-probes", action="store_true")
    ap.add_argument("--skip-loopback", action="store_true")
    ap.add_argument("--quick", action="store_true", help="skip the ~35 s ledger-lock probe")
    ap.add_argument("--no-state-guard", action="store_true")
    ap.add_argument("--unittest-timeout", type=float, default=3600)
    ap.add_argument("--work-root", help="scratch space on the filesystem under test (default: next to the results)")
    ap.add_argument("--keep-work", action="store_true", help="keep the scratch space for inspection")
    ap.add_argument("--invoked-via", default="python", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    info = common.platform_info()
    level = common.detect_level("native", info)
    source = common.source_identity()
    results = common.make_results_dir(Path(args.results_root or common.default_results_root()), level,
                                      source["sourceDigest"], "native")
    print("relay-desk native acceptance: level=%s digest=%s\nresults: %s" % (level, source["sourceDigest"][:12], results))
    log = common.CommandLog(results)
    # Scratch space sits next to the results (same filesystem as the checkout by
    # default) rather than in /tmp, which may be a different filesystem (tmpfs).
    work = Path(args.work_root) if args.work_root else results.parent / (".work-" + results.name)
    work.mkdir(parents=True, exist_ok=True)
    summary: Dict[str, Any] = {
        "schema": "relay-desk-acceptance-native/1",
        "startedAt": common.iso_now(),
        "level": level,
        "platform": info,
        "machineId": common.machine_fingerprint(),
        "source": source,
        "invokedVia": args.invoked_via,
        "workRoot": str(work),
        "sections": {},
    }
    home_state = Path.home() / ".relay-desk"
    repo_ledger = common.REPO_ROOT / "ledger.json"
    before = {"homeState": _tree_fingerprint(home_state), "repoLedger": _stat_fingerprint(repo_ledger)}

    def save() -> None:
        common.write_json(results / "summary.json", summary)

    common.write_json(results / "env.json", collect_env(work / "fs", kind="native"))
    summary["sections"]["env"] = {"status": "passed", "file": "env.json"}
    save()

    if args.skip_unittest:
        summary["sections"]["unittest"] = {"status": "skipped", "reason": "--skip-unittest"}
    else:
        variants = [("default", {})]
        if info["system"] == "Darwin":
            variants.append(("private-tmp", {"TMPDIR": "/private/tmp"}))
        runs = []
        for name, extra in variants:
            print("unittest (%s) ..." % name)
            runs.append(run_unittest(results, log, name, args, extra, work))
            save()
        all_ok = all(r["status"] == "passed" for r in runs)
        subset = any(r["selection"] != "full-suite" for r in runs)
        summary["sections"]["unittest"] = {
            # A subset never reports plain "passed": only the full suite can.
            "status": ("passed-subset-only" if subset else "passed") if all_ok else "failed",
            "runs": runs,
        }
        for r in runs:
            print("  %s: %s %s run=%s skipped=%d (%s)" % (r["variant"], r["status"], r["selection"], r["testsRun"],
                                                      len(r["skipped"] or []), r["log"]))
    save()

    if args.skip_probes:
        summary["sections"]["probes"] = {"status": "skipped", "reason": "--skip-probes"}
    else:
        from acceptance import platform_probes

        print("platform probes ...")
        probes = platform_probes.run_all(work / "probes", quick=args.quick)
        common.write_json(results / "probes.json", probes)
        bad = [p["name"] for p in probes if p["applicable"] and p["ok"] is False]
        summary["sections"]["probes"] = {
            "status": "failed" if bad else "passed",
            "failed": bad,
            "notApplicable": [p["name"] for p in probes if not p["applicable"]],
            "informational": [p["name"] for p in probes if p["applicable"] and p["ok"] is None],
            "file": "probes.json",
        }
        for p in probes:
            print("  %-38s %s" % (p["name"], "n/a" if not p["applicable"] else {True: "ok", False: "FAILED", None: "info"}[p["ok"]]))
    save()

    if args.skip_loopback:
        summary["sections"]["loopback"] = {"status": "skipped", "reason": "--skip-loopback"}
    else:
        print("loopback relay (two instances on this machine) ...")
        try:
            summary["sections"]["loopback"] = run_loopback(results, info, level, work)
        except Exception as e:
            summary["sections"]["loopback"] = {"status": "error", "error": "%s: %s" % (type(e).__name__, e)}
    save()

    end_source = common.source_identity()
    summary["sourceAtEnd"] = end_source
    summary["sourceChangedDuringRun"] = end_source["sourceDigest"] != source["sourceDigest"]
    if summary["sourceChangedDuringRun"]:
        print("WARNING: source digest changed during the run (%s -> %s); rerun on a frozen tree."
              % (source["sourceDigest"][:12], end_source["sourceDigest"][:12]))
    after = {"homeState": _tree_fingerprint(home_state), "repoLedger": _stat_fingerprint(repo_ledger)}
    summary["stateGuard"] = {
        "homeRelayDeskUnchanged": before["homeState"] == after["homeState"],
        "repoLedgerUnchanged": before["repoLedger"] == after["repoLedger"],
        "note": "existence/size/mtime only; contents are never read",
    }
    summary["endedAt"] = common.iso_now()
    statuses = [s.get("status") for s in summary["sections"].values()]
    if any(s in ("failed", "error") for s in statuses):
        summary["overall"] = "failed"
    elif all(s == "passed" for s in statuses):
        summary["overall"] = "passed"
    else:
        summary["overall"] = "passed-with-skipped-or-subset-sections"
    save()
    if not args.keep_work:
        import shutil

        shutil.rmtree(str(work), ignore_errors=True)
    print("overall: %s\nresults folder to send back: %s" % (summary["overall"], results))
    return 0 if summary["overall"].startswith("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
