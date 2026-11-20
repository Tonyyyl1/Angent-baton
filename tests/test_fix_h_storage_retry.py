# -*- coding: utf-8 -*-
"""Fix H: robustness under flaky storage (found by the final 2-hour WebDAV soak).

Each defect test was written first and run against the pre-fix source, where it
failed (see the fix-H evidence folder):

1. One transient read failure (a reset PROPFIND of ``receipt.json`` or
   ``ready.json``) aborted ``relay receive``: commands had no bounded retry for
   idempotent reads. Reads (list / stat / download) are now retried with capped,
   jittered backoff and a deadline; a not-found answer, configuration, auth and
   integrity failures are never retried, and writes stay single-shot (a re-run
   reads first and confirms an identical write-once record).
2. A failed ``publish`` (the snapshot record upload failed) had already made the
   unpublished snapshot the local head, so the next publish chained onto a
   parent that is not in the cloud and every receiver reported "缺少可验证的父版本
   ... 请恢复云端记录" (needs action) for a snapshot whose author still had it.
   Publish now writes the signed record first and commits nothing locally when
   that fails; the author re-publishes records of its own ancestors that an
   older engine left unpublished. A transport failure while fetching an ancestor
   stays transient and is never reported as a missing parent.
3. ``receive`` gave up at once when a ``sync --watch`` round held the workspace
   lock; it now waits a bounded time for a sync round only.
4. A publish to two peers read the same snapshot record five times and probed
   each delivery's ready marker twice.
"""
import json
import os
import random
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from relaydesk import crypto
from relaydesk import relay_engine as E
from relaydesk import storage
from relaydesk import sync_runtime as SR
from test_sync_watch import _ThreeDeviceBase


class ScriptedProvider(object):
    """StorageProvider wrapper: records every call and fails scripted (op, key substring) calls a given number of times."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.rules: List[List[Any]] = []    # [op, substring, remaining (-1 = always), exception factory]
        self.calls: List[Tuple[str, str]] = []
        self.injected: List[Tuple[str, str]] = []

    def fail(self, op: str, substring: str, times: int = 1, exc=None) -> None:
        self.rules.append([op, substring, times,
                           exc or (lambda o, k: storage.StorageError("connection reset by peer (%s %s)" % (o, k)))])

    def clear(self) -> None:
        self.rules = []

    def _gate(self, op: str, key: str) -> None:
        self.calls.append((op, key))
        for rule in self.rules:
            if rule[0] in (op, "*") and rule[1] in key and rule[2] != 0:
                if rule[2] > 0:
                    rule[2] -= 1
                self.injected.append((op, key))
                raise rule[3](op, key)

    def list_objects(self, prefix: str = ""):
        self._gate("list", prefix)
        return self.inner.list_objects(prefix)

    def stat(self, key: str):
        self._gate("stat", key)
        return self.inner.stat(key)

    def download(self, key: str, local: str):
        self._gate("download", key)
        return self.inner.download(key, local)

    def upload(self, local: str, key: str):
        self._gate("upload", key)
        return self.inner.upload(local, key)

    def delete(self, key: str):
        self._gate("delete", key)
        return self.inner.delete(key)

    def count(self, op: str, substring: str = "") -> int:
        return sum(1 for o, k in self.calls if o == op and substring in k)


def fake_policy(clock: Optional[SR.FakeClock] = None, **kw: Any):
    """The production read-retry policy driven by a fake clock (no real sleeping)."""
    clock = clock or SR.FakeClock()
    policy_cls = getattr(storage, "RetryPolicy", None)
    if policy_cls is None:  # the pre-fix source (repro runs) has no retry policy
        return None, clock
    return policy_cls(clock=clock, rng=random.Random(5), **kw), clock


class _FixHBase(_ThreeDeviceBase):
    def setUp(self) -> None:
        _ThreeDeviceBase.setUp(self)
        self.scripted = ScriptedProvider(storage.LocalDirProvider(str(self.cloud_dir)))
        self.policy, self.clock = fake_policy()

    def run_as(self, alias: str, fn, scripted: bool = True):
        patches = [self.env(alias), mock.patch.object(storage, "READ_RETRY", self.policy, create=True)]
        if scripted:
            patches.append(mock.patch.object(storage, "make_provider", lambda cfg: self.scripted))
        for p in patches:
            p.start()
        try:
            return fn()
        finally:
            for p in reversed(patches):
                p.stop()

    def send(self, src: str, dst: str) -> Dict[str, Any]:
        return self.run_as(src, lambda: E.send_project("demo", to_alias=dst), scripted=False)

    def receive(self, alias: str, relay_id: str, path: Optional[Path] = None, scripted: bool = True):
        kw = {"target_path": str(path)} if path is not None else {}
        return self.run_as(alias, lambda: E.receive_relay(relay_id, **kw), scripted=scripted)

    def edit(self, path: Path, text: str) -> None:
        (path / "keep.txt").write_text("%s %s\n" % (text, random.random()), encoding="utf-8")

    def publish_on(self, alias: str, path: Path, scripted: bool = False) -> Dict[str, Any]:
        self.edit(path, "publish")
        return self.run_as(alias, lambda: E.publish_project("demo"), scripted=scripted)

    def graph_nodes(self, alias: str) -> Dict[str, Any]:
        return ((self.state(alias).get("versionGraph") or {}).get("demo") or {}).get("nodes") or {}

    def current(self, alias: str) -> str:
        return str((self.state(alias).get("localCurrentVersion") or {}).get("demo") or "")

    def beta_owns_then_returns(self) -> Path:
        """alpha -> beta -> alpha: beta now has a local current version (its own handoff snapshot)."""
        h1 = self.send("alpha", "beta")
        beta_dir = self.root / "work_beta"
        self.receive("beta", h1["relayId"], beta_dir, scripted=False)
        self.edit(beta_dir, "beta")
        h2 = self.send("beta", "alpha")
        self.receive("alpha", h2["relayId"], scripted=False)
        return beta_dir


# ---------------------------------------------------------------- 1. transient reads during receive


class TestReceiveTransientReads(_FixHBase):
    def test_one_reset_receipt_stat_does_not_abort_receive(self) -> None:
        h = self.send("alpha", "beta")
        self.scripted.fail("stat", "%s/receipt.json" % h["relayId"], times=1)
        res = self.receive("beta", h["relayId"], self.root / "work_beta")  # pre-fix: StorageError
        self.assertTrue(res["ok"])
        self.assertEqual(len(self.scripted.injected), 1)
        self.assertEqual(self.clock.sleep_count, 1)
        self.assertTrue((self.cloud_dir / "v3" / "inbox" / self.did["beta"] / h["relayId"] / "receipt.json").is_file())

    def test_one_reset_ready_download_does_not_abort_receive(self) -> None:
        h = self.send("alpha", "beta")
        self.scripted.fail("download", "%s/ready.json" % h["relayId"], times=1)
        res = self.receive("beta", h["relayId"], self.root / "work_beta")  # pre-fix: StorageError
        self.assertTrue(res["ok"])
        self.assertEqual(self.scripted.injected, [("download", "v3/inbox/%s/%s/ready.json" % (self.did["beta"], h["relayId"]))])

    def test_persistent_outage_still_fails_as_transient_within_the_deadline(self) -> None:
        h = self.send("alpha", "beta")
        self.scripted.fail("*", "v3/", times=-1)
        with self.assertRaises(storage.StorageError) as cm:
            self.receive("beta", h["relayId"], self.root / "work_beta")
        self.assertEqual(SR.classify_error(cm.exception).error_class, SR.TRANSIENT)
        # bounded: the first failing read is attempted at most ``attempts`` times within the deadline
        self.assertEqual(len(self.scripted.injected), self.policy.attempts)
        self.assertLessEqual(self.clock.slept, self.policy.deadline)
        beta_state = E.load_relay_state(self.devices["beta"]["home"]) or {}
        self.assertNotIn("demo", beta_state.get("localCurrentVersion") or {})


# ---------------------------------------------------------------- 2. ancestors: transport vs. verified absence


class TestAncestorFetch(_FixHBase):
    def chain_beta_has_not_seen(self) -> Tuple[Dict[str, Any], List[str]]:
        """alpha publishes p1, p2 (beta never syncs) and hands p3 to beta: beta must fetch p2 and p1."""
        self.beta_owns_then_returns()
        p1 = self.publish_on("alpha", self.repo)["snapshotId"]
        p2 = self.publish_on("alpha", self.repo)["snapshotId"]
        self.edit(self.repo, "handoff")
        return self.send("alpha", "beta"), [p1, p2]

    def test_one_reset_ancestor_read_is_retried(self) -> None:
        h, (p1, p2) = self.chain_beta_has_not_seen()
        self.scripted.fail("download", "v3/snapshots/%s.json" % p1, times=1)
        res = self.receive("beta", h["relayId"])  # pre-fix: StorageError, handoff not received
        self.assertTrue(res["ok"])
        self.assertIn(p1, self.graph_nodes("beta"))
        self.assertIn(p2, self.graph_nodes("beta"))

    def test_ancestor_transport_failure_is_transient_never_missing_parent(self) -> None:
        h, (p1, _p2) = self.chain_beta_has_not_seen()
        self.scripted.fail("download", "v3/snapshots/%s.json" % p1, times=-1)
        self.scripted.fail("list", "v3/inbox/", times=-1)
        before = self.state("beta")
        with self.assertRaises(Exception) as cm:
            self.receive("beta", h["relayId"])
        c = SR.classify_error(cm.exception)
        self.assertEqual(c.error_class, SR.TRANSIENT, c)
        self.assertNotEqual(getattr(cm.exception, "code", ""), "lineage_incomplete")
        self.assertNotIn("缺少可验证的父版本", str(cm.exception))
        self.assertEqual(self.state("beta").get("localCurrentVersion"), before.get("localCurrentVersion"))

    def test_verified_absence_is_still_lineage_incomplete(self) -> None:
        h, (p1, _p2) = self.chain_beta_has_not_seen()
        (self.cloud_dir / "v3" / "snapshots" / ("%s.json" % p1)).unlink()
        for ready in (self.cloud_dir / "v3" / "inbox").glob("*/*/ready.json"):
            if p1 in ready.parent.name:
                ready.unlink()
        with self.assertRaises(E.RelayError) as cm:
            self.receive("beta", h["relayId"])
        self.assertEqual(cm.exception.code, "lineage_incomplete")
        self.assertEqual(SR.classify_error(cm.exception).error_class, SR.NEEDS_ACTION)

    def test_failed_record_upload_is_republished_before_the_next_snapshot_chains_onto_it(self) -> None:
        """The soak's 90-minute failure: a publish whose record upload failed became the next parent.

        The failed snapshot stays the local head (the recovery window "index
        published, reply lost" keeps the chain), but it is not confirmed in the
        cloud; the next publish writes its record from the cache first.
        """
        self.beta_owns_then_returns()
        good = self.publish_on("alpha", self.repo)["snapshotId"]
        self.assertTrue(self.graph_nodes("alpha")[good].get("recordPublished"))
        self.scripted.fail("upload", "v3/snapshots/", times=-1)
        with self.assertRaises(storage.StorageError):
            self.publish_on("alpha", self.repo, scripted=True)
        self.scripted.clear()
        failed = self.current("alpha")
        self.assertNotEqual(failed, good)
        self.assertFalse(self.graph_nodes("alpha")[failed].get("recordPublished"))
        self.assertFalse((self.cloud_dir / "v3" / "snapshots" / ("%s.json" % failed)).exists())
        nxt = self.publish_on("alpha", self.repo)
        self.assertEqual(nxt["parentVersionIds"], [failed])
        # pre-fix: the parent's record was never written
        self.assertTrue((self.cloud_dir / "v3" / "snapshots" / ("%s.json" % failed)).is_file())
        self.assertTrue(self.graph_nodes("alpha")[failed].get("recordPublished"))
        self.edit(self.repo, "handoff")
        h = self.send("alpha", "beta")
        res = self.receive("beta", h["relayId"], scripted=False)  # pre-fix: lineage_incomplete
        self.assertTrue(res["ok"])

    def test_republish_failure_stops_the_next_publish_before_it_commits(self) -> None:
        """While the missing parent record cannot be written, no new snapshot chains onto it."""
        self.publish_on("alpha", self.repo)
        self.scripted.fail("upload", "v3/snapshots/", times=-1)
        with self.assertRaises(storage.StorageError):
            self.publish_on("alpha", self.repo, scripted=True)
        failed = self.current("alpha")
        nodes = sorted(self.graph_nodes("alpha"))
        with self.assertRaises(storage.StorageError):
            self.publish_on("alpha", self.repo, scripted=True)  # repair of ``failed`` fails first
        self.assertEqual(self.current("alpha"), failed)
        self.assertEqual(sorted(self.graph_nodes("alpha")), nodes)

    def test_soak_sequence_failed_publish_publish_handoff_is_received(self) -> None:
        """End to end, as in the soak: pre-fix the receiver reported 缺少可验证的父版本 (lineage_incomplete)."""
        self.beta_owns_then_returns()
        self.publish_on("alpha", self.repo)
        self.scripted.fail("upload", "v3/snapshots/", times=-1)
        with self.assertRaises(storage.StorageError):
            self.publish_on("alpha", self.repo, scripted=True)
        self.scripted.clear()
        self.publish_on("alpha", self.repo)
        self.edit(self.repo, "handoff")
        h = self.send("alpha", "beta")
        res = self.receive("beta", h["relayId"], scripted=False)
        self.assertTrue(res["ok"])
        for vid, node in self.graph_nodes("beta").items():
            for parent in node.get("parentVersionIds") or []:
                self.assertTrue((self.cloud_dir / "v3" / "snapshots" / ("%s.json" % parent)).is_file(), parent)

    def test_author_republishes_ancestor_records_an_older_engine_left_unpublished(self) -> None:
        """State written by the pre-fix engine: an own ancestor is in the graph and cache but not in the cloud."""
        self.beta_owns_then_returns()
        lost = self.publish_on("alpha", self.repo)["snapshotId"]
        nxt = self.publish_on("alpha", self.repo)["snapshotId"]
        (self.cloud_dir / "v3" / "snapshots" / ("%s.json" % lost)).unlink()
        for ready in (self.cloud_dir / "v3" / "inbox").glob("*/*/ready.json"):
            if lost in ready.parent.name:
                ready.unlink()

        def legacy(st: Dict[str, Any]) -> None:
            for node in st["versionGraph"]["demo"]["nodes"].values():
                node.pop("recordPublished", None)

        E._update_relay_state(self.devices["alpha"]["home"], legacy)
        self.edit(self.repo, "handoff")
        h = self.send("alpha", "beta")  # the author repairs its own chain before handing off
        self.assertTrue((self.cloud_dir / "v3" / "snapshots" / ("%s.json" % lost)).is_file())
        self.assertTrue(self.graph_nodes("alpha")[lost].get("recordPublished"))
        self.assertTrue(self.graph_nodes("alpha")[nxt].get("recordPublished"))
        res = self.receive("beta", h["relayId"], scripted=False)
        self.assertTrue(res["ok"])
        self.assertIn(lost, self.graph_nodes("beta"))

    def test_resend_of_a_stuck_handoff_repairs_the_missing_ancestor(self) -> None:
        """Exactly the soak's stuck handoff: re-running send for the active handoff heals its chain."""
        self.beta_owns_then_returns()
        lost = self.publish_on("alpha", self.repo)["snapshotId"]
        self.publish_on("alpha", self.repo)
        self.edit(self.repo, "handoff")
        h = self.send("alpha", "beta")
        (self.cloud_dir / "v3" / "snapshots" / ("%s.json" % lost)).unlink()
        for ready in (self.cloud_dir / "v3" / "inbox").glob("*/*/ready.json"):
            if lost in ready.parent.name:
                ready.unlink()

        def legacy(st: Dict[str, Any]) -> None:
            for node in st["versionGraph"]["demo"]["nodes"].values():
                node.pop("recordPublished", None)

        E._update_relay_state(self.devices["alpha"]["home"], legacy)
        with self.assertRaises(E.RelayError) as cm:
            self.receive("beta", h["relayId"], scripted=False)
        self.assertEqual(cm.exception.code, "lineage_incomplete")
        again = self.send("alpha", "beta")
        self.assertEqual(again["relayId"], h["relayId"])
        self.assertTrue((self.cloud_dir / "v3" / "snapshots" / ("%s.json" % lost)).is_file())
        self.assertTrue(self.receive("beta", h["relayId"], scripted=False)["ok"])


# ---------------------------------------------------------------- 3. busy: a sync round holds the lock


class TestReceiveWaitsForSyncRound(_FixHBase):
    def hold(self, alias: str, purpose: str, pid: Optional[int] = None):
        home = self.devices[alias]["home"]
        cm = E._workspace_operation(home)
        cm.__enter__()
        holder = home / "workspace-operation.holder.json"
        holder.write_text(json.dumps({"purpose": purpose, "pid": pid or os.getpid()}), encoding="utf-8")
        released = {"done": False}

        def release() -> None:
            if not released["done"]:
                released["done"] = True
                holder.unlink()
                cm.__exit__(None, None, None)

        self.addCleanup(release)
        return release

    def test_receive_waits_for_a_sync_round_then_succeeds(self) -> None:
        h = self.send("alpha", "beta")
        release = self.hold("beta", "sync")
        lock_clock = SR.FakeClock()
        lock_clock.on_sleep = lambda s: release() if lock_clock.sleep_count >= 2 else None
        with mock.patch.object(E, "_LOCK_WAIT_CLOCK", lock_clock, create=True):
            res = self.receive("beta", h["relayId"], self.root / "work_beta", scripted=False)  # pre-fix: busy
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(lock_clock.sleep_count, 3)
        self.assertLess(lock_clock.slept, E.LOCK_WAIT_SEC)

    def test_wait_is_bounded_and_busy_stays_explicit(self) -> None:
        h = self.send("alpha", "beta")
        self.hold("beta", "sync")
        lock_clock = SR.FakeClock()
        with mock.patch.object(E, "_LOCK_WAIT_CLOCK", lock_clock, create=True):
            with self.assertRaises(E.RelayError) as cm:
                self.receive("beta", h["relayId"], self.root / "work_beta", scripted=False)
        self.assertEqual(cm.exception.code, "busy")
        self.assertEqual(SR.classify_error(cm.exception).error_class, SR.LOCAL_BUSY)
        self.assertGreaterEqual(lock_clock.slept, E.LOCK_WAIT_SEC * 0.9)
        self.assertLessEqual(lock_clock.slept, E.LOCK_WAIT_SEC + 1.0)

    def test_no_wait_when_another_command_holds_the_lock(self) -> None:
        h = self.send("alpha", "beta")
        self.hold("beta", "receive")
        lock_clock = SR.FakeClock()
        with mock.patch.object(E, "_LOCK_WAIT_CLOCK", lock_clock, create=True):
            with self.assertRaises(E.RelayError) as cm:
                self.receive("beta", h["relayId"], self.root / "work_beta", scripted=False)
        self.assertEqual(cm.exception.code, "busy")
        self.assertEqual(lock_clock.sleep_count, 0)

    def test_no_wait_for_a_dead_sync_process(self) -> None:
        h = self.send("alpha", "beta")
        self.hold("beta", "sync", pid=2 ** 22 + 12345)
        lock_clock = SR.FakeClock()
        with mock.patch.object(E, "_LOCK_WAIT_CLOCK", lock_clock, create=True):
            with self.assertRaises(E.RelayError) as cm:
                self.receive("beta", h["relayId"], self.root / "work_beta", scripted=False)
        self.assertEqual(cm.exception.code, "busy")
        if os.name != "nt":
            self.assertEqual(lock_clock.sleep_count, 0)

    def test_sync_round_marks_itself_as_the_holder_only_while_it_runs(self) -> None:
        home = self.devices["beta"]["home"]
        seen: List[Any] = []
        original = E.SyncSession._round_body

        def body(session, out):
            seen.append(json.loads((home / "workspace-operation.holder.json").read_text(encoding="utf-8")))
            return original(session, out)

        with self.env("beta"), mock.patch.object(E.SyncSession, "_round_body", body):
            E.sync_network(status_file=False)
        self.assertEqual([(s["purpose"], s["pid"]) for s in seen], [("sync", os.getpid())])
        self.assertFalse((home / "workspace-operation.holder.json").exists())


# ---------------------------------------------------------------- 4. storage calls per operation


class TestStorageCallsPerOperation(_FixHBase):
    def test_publish_reads_its_snapshot_record_once(self) -> None:
        self.edit(self.repo, "count")
        pub = self.run_as("alpha", lambda: E.publish_project("demo"))
        rec = "v3/snapshots/%s.json" % pub["snapshotId"]
        self.assertEqual([p["alias"] for p in pub["deliveredTo"]], ["beta", "gamma"])
        # pre-fix: 5 record downloads (put, then 2 per delivery) and 2 ready probes per delivery
        self.assertEqual(self.scripted.count("download", rec), 2)   # absent probe is a download too; then verify
        for alias in ("beta", "gamma"):
            inbox = "v3/inbox/%s/" % self.did[alias]
            self.assertEqual(self.scripted.count("download", inbox), 2, self.scripted.calls)  # absent probe + verify
            self.assertEqual(self.scripted.count("stat", inbox + "sync_"), 1)  # the package check only
        self.assertLessEqual(len(self.scripted.calls), 15, self.scripted.calls)


# ---------------------------------------------------------------- 5. the retry helper itself


class _Flaky(object):
    def __init__(self, errors: List[BaseException], value: Any = "ok") -> None:
        self.errors = list(errors)
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.value


class TestRetryPolicy(unittest.TestCase):
    def test_transient_read_retried_with_capped_jittered_backoff(self) -> None:
        policy, clock = fake_policy(attempts=5, base_delay=0.5, max_delay=1.0, deadline=60.0)
        fn = _Flaky([storage.StorageError("connection reset by peer")] * 4)
        self.assertEqual(policy.call(fn), "ok")
        self.assertEqual(fn.calls, 5)
        delays = list(clock.sleeps)
        self.assertEqual(len(delays), 4)
        for n, d in enumerate(delays, 1):
            raw = min(1.0, 0.5 * 2 ** (n - 1))
            self.assertTrue(raw / 2 <= d <= raw, (n, d))

    def test_attempts_and_deadline_bound_the_retries(self) -> None:
        policy, clock = fake_policy(attempts=3, base_delay=1.0, max_delay=8.0, deadline=60.0)
        fn = _Flaky([storage.StorageError("connection reset by peer")] * 10)
        with self.assertRaises(storage.StorageError):
            policy.call(fn)
        self.assertEqual(fn.calls, 3)
        policy, clock = fake_policy(attempts=50, base_delay=1.0, max_delay=8.0, deadline=10.0)
        fn = _Flaky([storage.StorageError("timed out")] * 100)
        with self.assertRaises(storage.StorageError):
            policy.call(fn)
        self.assertLessEqual(clock.slept, 10.0)
        self.assertLess(fn.calls, 50)

    def test_never_retried(self) -> None:
        cases = [
            storage.ObjectNotFoundError("远端对象不存在：x", key="x"),          # a verified absence
            storage.StorageError("didn't find section in config file"),        # configuration
            storage.StorageError("403 Forbidden"),                            # authorization
            storage.StorageError("远端对象键不能包含 '..' 路径段"),              # invalid key
            crypto.TamperError("Ed25519 签名验证失败"),                         # integrity
            ValueError("Expecting value"),                                     # not a storage error
        ]
        for exc in cases:
            policy, clock = fake_policy()
            fn = _Flaky([exc])
            with self.assertRaises(type(exc)):
                policy.call(fn)
            self.assertEqual((fn.calls, clock.sleep_count), (1, 0), exc)

    def test_provider_retries_reads_but_never_uploads_or_deletes(self) -> None:
        inner = ScriptedProvider(_Memory())
        policy, _ = fake_policy()
        prov = storage.with_read_retry(inner, policy)
        self.assertIs(storage.with_read_retry(prov, policy), prov)
        inner.fail("stat", "a", times=1)
        inner.fail("list", "", times=1)
        inner.fail("download", "a", times=1)
        self.assertIsNone(prov.stat("a"))
        self.assertEqual(prov.list_objects(""), [])
        with self.assertRaises(storage.ObjectNotFoundError):
            prov.download("a", "/nonexistent/never-written")
        self.assertEqual([c[0] for c in inner.calls], ["stat", "stat", "list", "list", "download", "download"])
        inner.calls.clear()
        inner.fail("upload", "a", times=1)
        inner.fail("delete", "a", times=1)
        with self.assertRaises(storage.StorageError):
            prov.upload(__file__, "a")
        with self.assertRaises(storage.StorageError):
            prov.delete("a")
        self.assertEqual([c[0] for c in inner.calls], ["upload", "delete"])
        self.assertIs(storage.without_retry(prov), inner)

    def test_immutable_record_write_is_single_shot_and_a_rerun_confirms_it(self) -> None:
        """Writes are never repeated in a call; the read-first re-run confirms a lost answer, never rewrites."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            inner = ScriptedProvider(storage.LocalDirProvider(tmp))
            policy, clock = fake_policy()
            prov = storage.with_read_retry(inner, policy)
            key = "v3/snapshots/snap_20261119_abcdefgh.json"
            doc = {"versionId": "snap_20261119_abcdefgh", "x": 1}
            original_upload = inner.inner.upload

            def lost_answer(local, k):
                original_upload(local, k)
                raise storage.StorageError("connection reset by peer")

            with mock.patch.object(inner.inner, "upload", side_effect=lost_answer):
                with self.assertRaises(storage.StorageError):
                    E._put_immutable_json(prov, key, doc)
            self.assertEqual(inner.count("upload"), 1)
            self.assertEqual(clock.sleep_count, 0)
            # the next run reads first and finds exactly this record: nothing is written again
            inner.calls.clear()
            E._put_immutable_json(prov, key, doc)
            self.assertEqual(inner.count("upload"), 0)
            # a different existing record is integrity: never retried, never overwritten
            with self.assertRaises(crypto.TamperError):
                E._put_immutable_json(prov, key, dict(doc, x=2))
            self.assertEqual(inner.count("upload"), 0)
            self.assertEqual(clock.sleep_count, 0)
            # the reads around the write are retried
            inner.fail("download", "snap_20261119_zzzzzzzz", times=1)
            E._put_immutable_json(prov, "v3/snapshots/snap_20261119_zzzzzzzz.json",
                                  dict(doc, versionId="snap_20261119_zzzzzzzz"))
            self.assertEqual(inner.count("upload", "zzzzzzzz"), 1)
            self.assertEqual(clock.sleep_count, 1)


class _Memory(object):
    """Minimal empty provider."""

    def list_objects(self, prefix: str = ""):
        return []

    def stat(self, key: str):
        return None

    def download(self, key: str, local: str):
        raise storage.ObjectNotFoundError("远端对象不存在：%s" % key, key=key)

    def upload(self, local: str, key: str):
        return storage.ObjectInfo(key, 1)

    def delete(self, key: str):
        return False


class TestSyncRoundsKeepFailingFast(_FixHBase):
    def test_sync_rounds_do_not_retry_in_round(self) -> None:
        """Sync keeps its round-level backoff (Phase B) and its short lock: no in-round read retry."""
        self.scripted.fail("list", "v3/inbox/%s/" % self.did["beta"], times=1)
        res = self.run_as("beta", lambda: E.sync_network(status_file=False))
        self.assertEqual(res["counters"]["roundsAborted"], 1)
        self.assertEqual(self.scripted.count("list", "v3/inbox/%s/" % self.did["beta"]), 1)
        self.assertEqual(self.clock.sleep_count, 0)


if __name__ == "__main__":
    unittest.main()
