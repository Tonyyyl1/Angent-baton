#!/usr/bin/env python3
"""Run the unittest suite and record every outcome, including skip reasons.

    python3 -m acceptance.unittest_capture --log RAW.log --json RESULT.json \
        [--start tests] [--pattern 'test*.py'] [--names tests.test_x ...]

The verbose runner output (same as ``python3 -m unittest discover -s tests -v``)
goes to ``--log``; ``--json`` lists each test with status, duration and, for
skips, the reason.  Run it from the repository root, like the plan's command.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
import unittest
from typing import Any, Dict, List, Optional


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.records: List[Dict[str, Any]] = []
        self._t0: Dict[str, float] = {}

    def startTest(self, test: unittest.TestCase) -> None:
        self._t0[test.id()] = time.time()
        super().startTest(test)

    def _rec(self, test: Any, status: str, detail: Optional[str] = None) -> None:
        tid = test.id() if hasattr(test, "id") else str(test)
        entry = {"test": tid, "status": status, "durationSec": round(time.time() - self._t0.get(tid, time.time()), 3)}
        if detail:
            entry["detail"] = detail[-4000:]
        self.records.append(entry)

    def addSuccess(self, test: unittest.TestCase) -> None:
        super().addSuccess(test)
        self._rec(test, "passed")

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:
        super().addFailure(test, err)
        self._rec(test, "failed", self._exc_info_to_string(err, test))

    def addError(self, test: unittest.TestCase, err: Any) -> None:
        super().addError(test, err)
        self._rec(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self._rec(test, "skipped", reason)

    def addExpectedFailure(self, test: unittest.TestCase, err: Any) -> None:
        super().addExpectedFailure(test, err)
        self._rec(test, "expected-failure")

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        self._rec(test, "unexpected-success")

    def addSubTest(self, test: Any, subtest: Any, err: Any) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._rec(subtest, "failed" if issubclass(err[0], test.failureException) else "error",
                      self._exc_info_to_string(err, test))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="tests")
    ap.add_argument("--pattern", default="test*.py")
    ap.add_argument("--names", nargs="*")
    ap.add_argument("--log", required=True)
    ap.add_argument("--json", required=True)
    args = ap.parse_args(argv)
    loader = unittest.TestLoader()
    t0 = time.time()
    if args.names:
        sys.path.insert(0, args.start)
        suite = loader.loadTestsFromNames(args.names)
    else:
        suite = loader.discover(args.start, pattern=args.pattern)
    with open(args.log, "w", encoding="utf-8") as stream:
        runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordingResult)
        result = runner.run(suite)
    records = result.records  # type: ignore[attr-defined]
    counts: Dict[str, int] = {}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    out = {
        "schema": "relay-desk-acceptance-unittest/1",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "selection": {"start": args.start, "pattern": args.pattern, "names": args.names or []},
        "testsRun": result.testsRun,
        "counts": counts,
        "wasSuccessful": result.wasSuccessful(),
        "durationSec": round(time.time() - t0, 2),
        "skipped": [{"test": r["test"], "reason": r.get("detail", "")} for r in records if r["status"] == "skipped"],
        "failures": [r for r in records if r["status"] in ("failed", "error", "unexpected-success")],
        "tests": records,
    }
    with open(args.json, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
