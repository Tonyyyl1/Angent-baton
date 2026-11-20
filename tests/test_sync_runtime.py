# -*- coding: utf-8 -*-
"""Phase B: sync runtime (classification, bounded events, backoff, status) and a 10,000-round model.

The model test drives the real ``SyncSession`` scheduling logic (``step``,
``next_delay``, per-peer health, quarantine, problem lifecycle) with a scripted
round body and a fake clock, so ten thousand rounds of outages, flapping
peers, a key change, an integrity failure and busy rounds run in seconds.
"""
import collections
import contextlib
import errno
import json
import os
import random
import tempfile
import threading
import tracemalloc
import unittest
from pathlib import Path
from typing import Any, Dict, List

from relaydesk import crypto
from relaydesk import ledger as L
from relaydesk import packager
from relaydesk import relay_engine as E
from relaydesk import storage
from relaydesk import sync_runtime as SR
from relaydesk import validation as V
from relaydesk import workspace_tx as W


class TestEventLog(unittest.TestCase):
    def test_ring_is_bounded_and_counters_are_exact(self) -> None:
        sink: List[Dict[str, Any]] = []
        log = SR.EventLog(limit=200, on_event=sink.append, clock=SR.FakeClock())
        kinds = ["snapshot_cached", "snapshot_forwarded", "receipt_synced", "fork_detected", "have_published"]
        for i in range(10000):
            log.emit(kinds[i % len(kinds)], n=i)
        self.assertEqual(len(log.recent), 200)
        self.assertEqual(log.dropped, 9800)
        snap = log.snapshot()
        self.assertTrue(snap["recentEventsTruncated"])
        self.assertEqual(snap["recentEventsDropped"], 9800)
        seqs = [e["seq"] for e in snap["recentEvents"]]
        self.assertEqual(seqs, list(range(9801, 10001)))
        self.assertEqual([e["seq"] for e in sink], list(range(1, 10001)))
        tallies = collections.Counter(e["kind"] for e in sink)
        c = snap["counters"]
        self.assertEqual(c["snapshotsCached"], tallies["snapshot_cached"])
        self.assertEqual(c["forwarded"], tallies["snapshot_forwarded"])
        self.assertEqual(c["receiptsSynced"], tallies["receipt_synced"])
        self.assertEqual(c["forksDetected"], tallies["fork_detected"])
        self.assertEqual(c["havePublished"], tallies["have_published"])
        self.assertEqual(c["eventsEmitted"], 10000)

    def test_not_retained_events_are_streamed_but_not_kept(self) -> None:
        sink: List[Dict[str, Any]] = []
        log = SR.EventLog(limit=5, on_event=sink.append, clock=SR.FakeClock())
        for _ in range(50):
            log.emit("round_finished", retain=False, changed=False)
        self.assertEqual(len(log.recent), 0)
        self.assertEqual(len(sink), 50)
        self.assertEqual(log.counters["eventsNotRetained"], 50)
        self.assertFalse(log.snapshot()["recentEventsTruncated"])
        self.assertEqual(log.events_since(0), ([], 0))

    def test_events_since_reports_gap_only_when_retained_events_were_lost(self) -> None:
        log = SR.EventLog(limit=10, clock=SR.FakeClock())
        for i in range(5):
            log.emit("x", i=i)
        items, gap = log.events_since(2)
        self.assertEqual([e["seq"] for e in items], [3, 4, 5])
        self.assertEqual(gap, 0)
        for i in range(20):
            log.emit("x", i=i)
        items, gap = log.events_since(2)
        self.assertEqual(items[0]["seq"], 16)
        self.assertGreater(gap, 0)
        items, gap = log.events_since(20)
        self.assertEqual([e["seq"] for e in items], list(range(21, 26)))
        self.assertEqual(gap, 0)

    def test_problems_are_reported_once_and_resolved_once(self) -> None:
        sink: List[Dict[str, Any]] = []
        log = SR.EventLog(on_event=sink.append, clock=SR.FakeClock())
        transient = SR.ClassifiedError(SR.TRANSIENT, "storage_transport", "down")
        for r in range(1, 51):
            log.begin_round(r)
            if r <= 30:
                log.report_problem("inbox_list||", transient)
            log.end_round()
        problems = [e for e in sink if e["kind"] == "problem"]
        resolved = [e for e in sink if e["kind"] == "resolved"]
        self.assertEqual(len(problems), 1)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["round"], 31)
        self.assertEqual(log.counters["failedOperations"][SR.TRANSIENT], 30)
        self.assertEqual(log.counters["problemsOpened"][SR.TRANSIENT], 1)
        self.assertEqual(log.counters["problemsResolved"], 1)
        self.assertEqual(log.unresolved_list(), [])

    def test_carry_keeps_problem_without_new_event_and_class_change_reemits(self) -> None:
        sink: List[Dict[str, Any]] = []
        log = SR.EventLog(on_event=sink.append, clock=SR.FakeClock())
        log.begin_round(1)
        log.report_problem("forward|b|s", SR.ClassifiedError(SR.TRANSIENT, "storage_transport", "x"), peer="b")
        log.end_round()
        for r in range(2, 6):
            log.begin_round(r)
            log.carry_peer("b")
            log.end_round()
        self.assertEqual(len(log.unresolved_list()), 1)
        log.begin_round(6)
        log.report_problem("forward|b|s", SR.ClassifiedError(SR.INTEGRITY, "tamper", "x"), peer="b")
        log.end_round()
        self.assertEqual([e["errorClass"] for e in sink if e["kind"] == "problem"], [SR.TRANSIENT, SR.INTEGRITY])

    def test_unexpected_errors_escalate_after_three_occurrences(self) -> None:
        log = SR.EventLog(clock=SR.FakeClock())
        classes = []
        for r in range(1, 6):
            log.begin_round(r)
            p, _ = log.report_problem("forward|b|s", SR.classify_error(RuntimeError("boom"), "forward"))
            classes.append((p["errorClass"], p["code"]))
            log.end_round()
        self.assertEqual(classes[:2], [(SR.TRANSIENT, "unexpected")] * 2)
        self.assertEqual(classes[2:], [(SR.NEEDS_ACTION, "internal_error")] * 3)

    def test_torn_have_read_escalates_to_integrity_after_three(self) -> None:
        log = SR.EventLog(clock=SR.FakeClock())
        seen = []
        for r in range(1, 6):
            log.begin_round(r)
            p, _ = log.report_problem("have_fetch|b|", SR.classify_error(json.JSONDecodeError("x", "{", 0), "have_fetch"))
            seen.append((p["errorClass"], p["code"]))
            log.end_round()
        self.assertEqual(seen[:2], [(SR.TRANSIENT, "torn_read")] * 2)
        self.assertEqual(seen[2:], [(SR.INTEGRITY, "remote_json_invalid")] * 3)

    def test_transient_problem_marked_stuck_after_twenty(self) -> None:
        log = SR.EventLog(clock=SR.FakeClock())
        for r in range(1, 22):
            log.begin_round(r)
            p, _ = log.report_problem("k", SR.ClassifiedError(SR.TRANSIENT, "storage_transport", "x"))
            log.end_round()
        self.assertTrue(p.get("stuck"))
        self.assertEqual(p["errorClass"], SR.TRANSIENT)

    def test_unresolved_set_is_capped(self) -> None:
        log = SR.EventLog(limit=50, unresolved_limit=50, clock=SR.FakeClock())
        log.begin_round(1)
        for i in range(120):
            log.report_problem("inbound||d%d" % i, SR.ClassifiedError(SR.INTEGRITY, "tamper", "x"))
        log.end_round()
        snap = log.snapshot()
        self.assertEqual(len(snap["unresolved"]), 50)
        self.assertTrue(snap["unresolvedTruncated"])
        self.assertEqual(snap["unresolvedDropped"], 70)

    def test_broken_pipe_sink_cancels(self) -> None:
        closed = threading.Event()

        def sink(ev):
            raise BrokenPipeError()

        log = SR.EventLog(on_event=sink, clock=SR.FakeClock(), on_sink_closed=closed.set)
        log.emit("x")
        self.assertTrue(closed.is_set())
        self.assertIsNone(log.on_event)
        log.emit("y")  # no sink any more, no error


class TestBackoffAndHealth(unittest.TestCase):
    def test_backoff_envelope_and_determinism(self) -> None:
        for base, cap in ((5.0, 300.0), (2.0, 30.0), (0.01, 0.05), (10.0, 10.0)):
            b1 = SR.Backoff(base, cap, random.Random(42))
            b2 = SR.Backoff(base, cap, random.Random(42))
            for attempt in range(1, 10001):
                d = b1.delay(attempt)
                self.assertEqual(d, b2.delay(attempt))
                self.assertGreaterEqual(d, min(base, cap) / 2.0)
                self.assertLessEqual(d, cap)
                lo, hi = b1.envelope(attempt)
                self.assertTrue(lo <= d <= hi)
            self.assertEqual(b1.raw(1), min(base, cap))
            self.assertEqual(b1.raw(10000), cap)

    def test_health_backoff_then_success_resets(self) -> None:
        clock = SR.FakeClock()
        table = SR.HealthTable(SR.Backoff(5.0, 300.0, random.Random(1)), clock)
        self.assertTrue(table.is_due("b"))
        before, after, attempt, delay = table.fail("b", SR.TRANSIENT, "storage_transport")
        self.assertEqual((before, after, attempt), (SR.HEALTHY, SR.BACKOFF, 1))
        self.assertTrue(2.5 <= delay <= 5.0)
        self.assertFalse(table.is_due("b"))
        clock.advance(delay)
        self.assertTrue(table.is_due("b"))
        self.assertEqual(table.fail("b", SR.TRANSIENT, "x")[2], 2)
        self.assertEqual(table.succeed("b"), (SR.BACKOFF, SR.HEALTHY))
        self.assertEqual(table.get("b").attempt, 0)
        self.assertTrue(table.is_due("b"))

    def test_parked_peer_is_never_due_until_reset(self) -> None:
        clock = SR.FakeClock()
        table = SR.HealthTable(SR.Backoff(5.0, 300.0, random.Random(1)), clock)
        table.park("b", SR.NEEDS_ACTION, "peer_key_changed")
        clock.advance(10 ** 6)
        self.assertFalse(table.is_due("b"))
        self.assertEqual(table.succeed("b"), (SR.PARKED, SR.PARKED))
        self.assertEqual(table.fail("b", SR.TRANSIENT, "x")[1], SR.PARKED)
        table.reset("b")
        self.assertTrue(table.is_due("b"))

    def test_quarantine_lru_and_tag_change(self) -> None:
        q = SR.Quarantine(limit=3)
        q.put("a", ("t1",), "pa", peer="x")
        self.assertEqual(q.match("a", ("t1",)), "pa")
        self.assertIsNone(q.match("a", ("t2",)))  # object changed: retried
        self.assertIsNone(q.match("a", ("t1",)))  # stale entry dropped
        for k in "bcde":
            q.put(k, None, "p" + k, peer="y" if k == "e" else "x")
        self.assertEqual(len(q), 3)
        self.assertIsNone(q.match("b", None))
        q.clear_peer("x")
        self.assertEqual(len(q), 1)
        self.assertEqual(q.match("e", None), "pe")

    def test_clocks(self) -> None:
        fake = SR.FakeClock(start=10.0)
        cancel = threading.Event()
        self.assertFalse(fake.sleep(5, cancel))
        self.assertEqual(fake.monotonic(), 15.0)
        self.assertTrue(fake.now_iso().endswith("Z"))
        cancel.set()
        self.assertTrue(fake.sleep(5, cancel))
        self.assertEqual(fake.monotonic(), 15.0)
        real = SR.RealClock()
        self.assertTrue(real.sleep(5, cancel))  # returns at once when already cancelled
        self.assertFalse(real.sleep(0.001))


class TestClassification(unittest.TestCase):
    def cls(self, exc, stage=""):
        c = SR.classify_error(exc, stage)
        return c.error_class, c.code

    def test_table(self) -> None:
        busy = E.RelayError("忙", code="busy")
        ledger_lock = L.LedgerError("台账正被另一个进程占用（锁文件 x）。稍后再试。")
        cases = [
            (busy, "", (SR.LOCAL_BUSY, "busy")),
            (ledger_lock, "", (SR.LOCAL_BUSY, "lock_timeout")),
            (L.LedgerError("无法写入台账"), "", (SR.NEEDS_ACTION, "ledger_error")),
            (storage.StorageError("rclone 操作超时（超过 120.0 秒）。"), "", (SR.TRANSIENT, "storage_transport")),
            (storage.StorageError("connection refused"), "", (SR.TRANSIENT, "storage_transport")),
            (storage.ObjectNotFoundError("gone", key="k"), "inbound", (SR.TRANSIENT, "listing_lag")),
            (storage.StorageError("列举失败：403 Forbidden"), "", (SR.NEEDS_ACTION, "storage_auth")),
            (storage.StorageError("Failed: InvalidAccessKeyId"), "", (SR.NEEDS_ACTION, "storage_auth")),
            (storage.StorageError("未找到 rclone 可执行文件，请先安装 rclone 并检查 PATH。"), "", (SR.NEEDS_ACTION, "storage_config")),
            (storage.StorageError("didn't find section in config file"), "", (SR.NEEDS_ACTION, "storage_config")),
            # seen in the WebDAV soak: rclone wraps a reset connection in "Failed to create file system"
            (storage.StorageError('列出远端目录失败（v3/inbox/d）：2026/11/18 23:06:53 CRITICAL: Failed to create file system '
                                  'for "soak_dav:relay-space/v3/inbox/d": read metadata failed: Propfind '
                                  '"http://127.0.0.1:56884/relay-space/v3/inbox/d": read tcp 127.0.0.1:58452->'
                                  '127.0.0.1:56884: read: connection reset by peer'), "inbox_list",
             (SR.TRANSIENT, "storage_transport")),
            (storage.StorageError('CRITICAL: Failed to create file system for "x:y": write tcp a->b: write: broken pipe'),
             "", (SR.TRANSIENT, "storage_transport")),
            (storage.StorageError('CRITICAL: Failed to create file system for "x:y": 403 Forbidden'), "",
             (SR.NEEDS_ACTION, "storage_auth")),
            (storage.StorageError('CRITICAL: Failed to create file system for "nosuch:": didn\'t find section in config file'),
             "", (SR.NEEDS_ACTION, "storage_config")),
            (storage.StorageError("远端对象键不能包含反斜杠"), "", (SR.INTEGRITY, "invalid_key")),
            (crypto.CryptoUnavailableError("no nacl"), "", (SR.NEEDS_ACTION, "crypto_unavailable")),
            (E.NotConfiguredError("未配置"), "", (SR.NEEDS_ACTION, "not_configured")),
            (E.RelayError("x", code="need_device"), "", (SR.NEEDS_ACTION, "need_device")),
            (E.KeyChangedError("x"), "", (SR.NEEDS_ACTION, "peer_key_changed")),
            (E.RelayError("x", code="key_changed"), "forward", (SR.NEEDS_ACTION, "key_changed")),
            (E.RelayError("x", code="untrusted_author"), "", (SR.NEEDS_ACTION, "untrusted_author")),
            (E.PeerConflictError("x"), "", (SR.NEEDS_ACTION, "alias_conflict")),
            (V.LocalStateCorruptError("bad", path="/x/cloud.json", kind="cloud"), "", (SR.NEEDS_ACTION, "local_state_corrupted")),
            (crypto.IdentityCorruptError("bad"), "", (SR.NEEDS_ACTION, "local_state_corrupted")),
            (L.LocalConfigCorruptError("bad"), "", (SR.NEEDS_ACTION, "local_state_corrupted")),
            (W.CorruptedRecoveryError("bad"), "", (SR.NEEDS_ACTION, "recovery_corrupted")),
            (crypto.TamperError("Ed25519 签名验证失败，数据可能已被篡改。"), "inbound", (SR.INTEGRITY, "signature_invalid")),
            (crypto.TamperError("包裹密钥解封失败：密钥不匹配或包裹已被篡改。"), "inbound", (SR.INTEGRITY, "unseal_failed")),
            (crypto.TamperError("快照 x 载荷 SHA-256 与作者签名摘要不一致。"), "inbound", (SR.INTEGRITY, "digest_mismatch")),
            (crypto.InvalidSignedFieldError("bad", code="path_escape"), "inbound", (SR.INTEGRITY, "path_escape")),
            (V.InvalidIdentifierError("bad"), "inbound", (SR.INTEGRITY, "invalid_identifier")),
            (packager.PackageError("bad tar"), "inbound", (SR.INTEGRITY, "package_invalid")),
            (E.RelayError("x", code="checkpoint_corrupted"), "forward", (SR.INTEGRITY, "checkpoint_corrupted")),
            (json.JSONDecodeError("x", "{", 0), "inbound", (SR.INTEGRITY, "remote_json_invalid")),
            (json.JSONDecodeError("x", "{", 0), "have_fetch", (SR.TRANSIENT, "torn_read")),
            (crypto.CryptoError("other"), "trust_check", (SR.NEEDS_ACTION, "local_state_corrupted")),
            (crypto.CryptoError("other"), "inbound", (SR.INTEGRITY, "crypto_error")),
            (OSError(errno.ENOSPC, "No space left"), "inbound", (SR.NEEDS_ACTION, "disk_full")),
            (OSError(errno.EAGAIN, "again"), "inbound", (SR.TRANSIENT, "local_io_retry")),
            (RuntimeError("boom"), "forward", (SR.TRANSIENT, "unexpected")),
            (ValueError("plain"), "round", (SR.TRANSIENT, "unexpected")),
        ]
        if os.name != "nt":
            cases.append((PermissionError(errno.EACCES, "denied"), "inbound", (SR.NEEDS_ACTION, "permission_denied")))
        for exc, stage, want in cases:
            with self.subTest(exc=repr(exc), stage=stage):
                self.assertEqual(self.cls(exc, stage), want)

    def test_storage_error_kind_attribute_is_honoured(self) -> None:
        for kind, want in (("auth", "storage_auth"), ("config", "storage_config"), ("transport", "storage_transport")):
            exc = storage.StorageError("whatever")
            exc.kind = kind
            self.assertEqual(SR.classify_error(exc).code, want)

    def test_actions_and_flags(self) -> None:
        c = SR.classify_error(E.NotConfiguredError("x"))
        self.assertEqual(c.action, "relay pair")
        self.assertFalse(c.retryable)
        self.assertTrue(SR.classify_error(storage.StorageError("reset")).retryable)
        self.assertTrue(SR.ClassifiedError(SR.NEEDS_ACTION, "peer_key_changed", "x").peer_level)
        path_err = V.LocalStateCorruptError("bad", path="/h/relay_state.json", kind="relay_state")
        self.assertIn("/h/relay_state.json", SR.classify_error(path_err).action)


class TestStatus(unittest.TestCase):
    def test_completion_matrix(self) -> None:
        mk = lambda c: {"errorClass": c}  # noqa: E731
        self.assertEqual(SR.completion_status([]), SR.SUCCESS)
        self.assertEqual(SR.completion_status([mk(SR.TRANSIENT)]), SR.PARTIAL)
        self.assertEqual(SR.completion_status([mk(SR.LOCAL_BUSY)]), SR.PARTIAL)
        self.assertEqual(SR.completion_status([mk(SR.TRANSIENT), mk(SR.INTEGRITY)]), SR.NEEDS_ACTION_STATUS)
        self.assertEqual(SR.completion_status([mk(SR.NEEDS_ACTION)]), SR.NEEDS_ACTION_STATUS)
        self.assertEqual(SR.completion_status([mk(SR.NEEDS_ACTION)], cancelled=True), SR.CANCELLED)
        self.assertEqual(SR.completion_status([], cancelled=True), SR.CANCELLED)
        self.assertEqual([SR.exit_code_for(s) for s in SR.COMPLETION_STATUSES], [0, 3, 4, 130])

    def test_status_file_roundtrip_and_staleness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sync_status.json"
            self.assertIsNone(SR.read_status_file(path))
            summary = {"schema": SR.SUMMARY_SCHEMA, "status": "partial", "ok": False, "cycles": 3,
                       "recentEvents": [{"seq": i} for i in range(50)], "unresolved": [{"key": "k"}],
                       "interval": 5.0, "maxBackoff": 30.0}
            SR.write_status_file(path, summary, extra={"running": True, "heartbeatAt": "2026-11-18T00:00:00Z",
                                                       "pid": 1})
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("recentEvents", data)
            self.assertEqual(len(data["recentEventsTail"]), 20)
            from datetime import datetime, timezone

            fresh = SR.read_status_file(path, now=datetime(2026, 11, 18, 0, 1, tzinfo=timezone.utc))
            self.assertFalse(fresh["stale"])
            stale = SR.read_status_file(path, brief=True, now=datetime(2026, 11, 18, 1, 0, tzinfo=timezone.utc))
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["status"], "partial")
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(SR.read_status_file(path))


# ---------------------------------------------------------------- 10,000-round model


class _BusyAt(object):
    """Stand-in for ``_workspace_operation``: busy on scripted rounds, otherwise a no-op lock."""

    def __init__(self, model: "ModelSession") -> None:
        self.model = model

    def __call__(self, base):
        model = self.model

        @contextlib.contextmanager
        def cm():
            if model.cycles % 97 == 0:
                raise E.RelayError("本机忙", code="busy")
            yield

        return cm()


class ModelSession(E.SyncSession):
    """The real session scheduling with a scripted round body (three peers, no storage or crypto).

    * global outage in rounds 100-160 and 4000-4100 (the inbox listing fails);
    * beta's transport flaps with p=0.3 in rounds 2000-4000;
    * beta's key changes at round 5000 (parked) and is re-accepted at 5200;
    * gamma's object ``g-obj`` fails integrity until its tag changes at round 7000;
    * busy every 97th round (see ``_BusyAt``).
    """

    def __init__(self, tmp: str, clock: SR.FakeClock, rng: random.Random, sink) -> None:
        identity = {"deviceId": "dev_self", "verifyPublicKey": "v", "boxPublicKey": "b", "keyFingerprint": "FP",
                    "alias": "self"}
        cloud = {"provider": "local", "root": tmp, "peers": {
            d: {"deviceId": d, "alias": d, "paired": True, "status": "paired", "capabilities": ["v2", "v3"],
                "keyFingerprint": "fp-" + d} for d in ("alpha", "beta", "gamma")}}
        E.SyncSession.__init__(self, interval=5.0, max_backoff=300.0, on_event=sink, clock=clock, rng=rng,
                               mode="watch", _ctx=(None, Path(tmp), Path(tmp) / "ledger.json", {}, "self", cloud,
                                                   identity, object()))
        self.flap_rng = random.Random(99)
        self.work = collections.Counter()           # (peer) -> successful operations
        self.attempts = collections.Counter()       # (peer) -> attempted operations
        self.injected = collections.Counter()       # error class -> injected failures
        self.alpha_rounds: List[int] = []
        self.beta_attempt_rounds: List[int] = []
        self.gamma_object_attempts = collections.Counter()  # tag -> attempts
        self.gamma_tag = "t1"
        self.outage_rounds: List[int] = []

    # trust: the scripted key change and re-acceptance mirror _recheck_trust's peer handling
    def _recheck_trust(self, out):
        # A fresh int allocated here, so the model's own bookkeeping is not attributed to relaydesk.
        self.r = r = self.cycles + 0
        if r == 5200:
            self.log.emit("trust_changed", what="peer", peerDeviceId="beta", detail={"toStatus": "paired"})
            before, after = self.health.reset("beta")
            self.quarantine.clear_peer("beta")
            self._peer_state_event("beta", before, after)
        if 5000 <= r < 5200:
            problem, _ = self._report("peer|beta|trust", SR.ClassifiedError(
                SR.NEEDS_ACTION, "peer_key_changed", "beta keys changed"), "trust_check", peer="beta")
            self._park("beta", problem)
        return True

    def _round_body(self, out):
        r = self.r
        if 100 <= r <= 160 or 4000 <= r <= 4100:
            self.outage_rounds.append(r)
            self.injected[SR.TRANSIENT] += 1
            problem, _ = self._report("inbox_list||", storage.StorageError("connection reset"), "inbox_list")
            out.global_error = problem["errorClass"]
            out.aborted = True
            return
        # alpha: always healthy
        if self.health.is_due("alpha"):
            self.attempts["alpha"] += 1
            self.work["alpha"] += 1
            self.alpha_rounds.append(r)
            self._round_ok.add("alpha")
        # beta: flaps, then key change
        if not self.health.is_due("beta"):
            self.log.carry_peer("beta")
        else:
            self.attempts["beta"] += 1
            self.beta_attempt_rounds.append(r)
            if 2000 <= r <= 4000 and self.flap_rng.random() < 0.3:
                self.injected[SR.TRANSIENT] += 1
                self._item_failure("forward|beta|snap", storage.StorageError("reset by peer"), "forward", "beta")
            else:
                self.work["beta"] += 1
                self._round_ok.add("beta")
        # gamma: one object fails integrity until its tag changes
        if r == 7000:
            self.gamma_tag = "t2"
        if self.health.is_due("gamma"):
            self._round_ok.add("gamma")
            held = self.quarantine.match("inbound|g-obj", (self.gamma_tag,))
            if held and self.log.carry(held):
                pass
            elif self.gamma_tag == "t1":
                self.gamma_object_attempts["t1"] += 1
                self.injected[SR.INTEGRITY] += 1
                self._item_failure("inbound||g-obj", crypto.TamperError("Ed25519 签名验证失败"), "inbound", "",
                                   "inbound|g-obj", (self.gamma_tag,))
            else:
                self.gamma_object_attempts["t2"] += 1
                self.work["gamma"] += 1
        out.changed = bool(out.changed)


def _engine_memory() -> int:
    """Bytes currently allocated by relaydesk code (the test's own bookkeeping lists are excluded)."""
    snap = tracemalloc.take_snapshot().filter_traces([tracemalloc.Filter(True, "*relaydesk*")])
    return sum(stat.size for stat in snap.statistics("filename"))


class TestTenThousandRoundModel(unittest.TestCase):
    ROUNDS = 10000

    def test_ten_thousand_rounds(self) -> None:
        sink_counts = collections.Counter()
        problem_events: List[Dict[str, Any]] = []
        peer_backoffs: List[Dict[str, Any]] = []

        def sink(ev):
            # Copies only: keeping the engine's own objects would show up as engine memory growth.
            sink_counts[ev["kind"]] += 1
            if ev["kind"] == "problem":
                problem_events.append({"key": ev["key"].encode(), "round": ev["round"] + 0,
                                       "errorClass": ev["errorClass"].encode()})
            if ev["kind"] == "peer_state" and ev.get("to") == SR.BACKOFF:
                peer_backoffs.append({"attempt": ev["attempt"] + 0, "retryInSec": ev["retryInSec"] + 0.0})

        clock = SR.FakeClock()
        original = E._workspace_operation
        with tempfile.TemporaryDirectory() as tmp:
            model = ModelSession(tmp, clock, random.Random(1234), sink)
            E._workspace_operation = _BusyAt(model)
            try:
                busy_rounds, delays_ok = 0, True
                bad_delays: List[Any] = []
                mem_at_1000 = None
                tracemalloc.start()
                try:
                    for r in range(1, self.ROUNDS + 1):
                        out = model.step()
                        self.assertLessEqual(len(model.log.recent), 200)
                        if out.busy:
                            busy_rounds += 1
                        attempt_before = model.global_attempt
                        delay = model.next_delay(out)
                        if out.busy:
                            ok = model.interval <= delay <= 1.25 * model.interval
                        elif out.global_error == SR.TRANSIENT:
                            lo, hi = model.backoff.envelope(attempt_before + 1)
                            ok = lo <= delay <= hi
                        else:
                            ok = delay == model.interval
                        if not ok:
                            delays_ok = False
                            bad_delays.append((r, delay, out.as_dict()))
                        clock.sleep(delay)
                        if r == 1000:
                            mem_at_1000 = _engine_memory()
                    mem_end = _engine_memory()
                finally:
                    tracemalloc.stop()
            finally:
                E._workspace_operation = original
            summary = model.finish()

        self.assertTrue(delays_ok, bad_delays[:5])
        self.assertEqual(model.cycles, self.ROUNDS)
        c = summary["counters"]
        # counters equal the tallies of what the sink saw and of what the script injected
        self.assertEqual(c["rounds"], self.ROUNDS)
        self.assertEqual(c["roundsSkippedBusy"], busy_rounds)
        self.assertEqual(sink_counts["round_skipped"], busy_rounds)
        self.assertEqual(busy_rounds, self.ROUNDS // 97)
        self.assertEqual(sum(c["problemsOpened"].values()), sink_counts["problem"])
        self.assertEqual(c["problemsResolved"], sink_counts["resolved"])
        self.assertEqual(c["failedOperations"][SR.INTEGRITY], model.injected[SR.INTEGRITY])
        self.assertEqual(c["failedOperations"][SR.TRANSIENT], model.injected[SR.TRANSIENT])
        self.assertEqual(c["failedOperations"][SR.LOCAL_BUSY], busy_rounds)
        self.assertEqual(c["eventsEmitted"], sum(sink_counts.values()) - 1)  # the final summary event is not counted in itself
        outage = set(model.outage_rounds)
        self.assertEqual(c["roundsAborted"], len(outage))
        # isolation: alpha's work happens in every round that is neither an outage nor busy
        expected_alpha = [r for r in range(1, self.ROUNDS + 1) if r not in outage and r % 97 != 0]
        self.assertEqual(model.alpha_rounds, expected_alpha)
        # parked beta is never attempted while its key is unconfirmed
        self.assertEqual([r for r in model.beta_attempt_rounds if 5000 <= r < 5200], [])
        self.assertTrue(any(r >= 5200 for r in model.beta_attempt_rounds))
        # peer backoff delays respect the envelope of their attempt number
        for ev in peer_backoffs:
            lo, hi = model.backoff.envelope(ev["attempt"])
            self.assertTrue(lo <= ev["retryInSec"] <= hi + 0.001, ev)
        # the gamma integrity problem: attempted once, opened once, until its object changed
        self.assertEqual(model.gamma_object_attempts["t1"], 1)
        g_events = [p for p in problem_events if p["key"] == b"inbound||g-obj"]
        self.assertEqual(len(g_events), 1)
        self.assertEqual(g_events[0]["errorClass"], SR.INTEGRITY.encode())
        self.assertLess(g_events[0]["round"], 7000)
        self.assertGreater(model.gamma_object_attempts["t2"], 0)
        # problems are reported once per episode, not once per round
        inbox_events = [p for p in problem_events if p["key"] == b"inbox_list||"]
        self.assertEqual(len(inbox_events), 2)  # two outage windows, 162 failing rounds
        self.assertLess(sink_counts["problem"], sum(c["failedOperations"].values()) / 2)
        # the script ends healthy
        self.assertEqual(summary["status"], SR.SUCCESS, summary["unresolved"])
        self.assertEqual(summary["unresolved"], [])
        self.assertTrue(summary["recentEventsTruncated"])
        self.assertLessEqual(len(summary["recentEvents"]), 200)
        for name in ("syncedReceipts", "syncedSnapshots", "forwarded", "conflictsDetected", "errors"):
            self.assertLessEqual(len(summary[name]), SR.LEGACY_LIST_LIMIT)
        # memory held by relaydesk does not grow with the number of rounds
        self.assertLess(mem_end - mem_at_1000, 256 * 1024, (mem_at_1000, mem_end))


if __name__ == "__main__":
    unittest.main()
