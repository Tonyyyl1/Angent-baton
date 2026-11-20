# -*- coding: utf-8 -*-
"""Phase B: continuous sync against the real engine (three devices, local-folder cloud).

The first class reproduces suspected defects S2, S3 and S8 from the Phase B/C
design. Those tests were written before the fix and failed on the Phase A
source (see the phase-B evidence folder); they pin the corrected behaviour.
"""
import json
import os
import random
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from relaydesk import crypto
from relaydesk import relay_engine as E
from relaydesk import storage
from test_multidevice_network import MultiDeviceTestBase


class _ThreeDeviceBase(MultiDeviceTestBase):
    """alpha, beta, gamma fully paired; alpha owns folder project ``demo``."""

    def setUp(self) -> None:
        MultiDeviceTestBase.setUp(self)
        for alias in ("alpha", "beta", "gamma"):
            self.init_device(alias)
        self.pair_pair("alpha", "beta")
        self.pair_pair("alpha", "gamma")
        self.pair_pair("beta", "gamma")
        self.repo = self.create_git_project("alpha", proj_id="demo", name="同步测试")
        self.did = {a: self.identity(a)["deviceId"] for a in ("alpha", "beta", "gamma")}

    def identity(self, alias: str) -> Dict[str, Any]:
        return json.loads((self.devices[alias]["home"] / "identity.json").read_text(encoding="utf-8"))

    def state(self, alias: str) -> Dict[str, Any]:
        return json.loads((self.devices[alias]["home"] / "relay_state.json").read_text(encoding="utf-8"))

    def inbox_deliveries(self, alias: str) -> List[str]:
        inbox = self.cloud_dir / "v3" / "inbox" / self.did[alias]
        if not inbox.is_dir():
            return []
        return sorted(p.name for p in inbox.iterdir() if (p / "ready.json").is_file())

    def cached(self, alias: str) -> List[str]:
        root = self.devices[alias]["home"] / "cache" / "snapshots"
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []

    def publish(self, alias: str = "alpha") -> Dict[str, Any]:
        (self.repo / "keep.txt").write_text("edit %s\n" % random.random(), encoding="utf-8")
        with self.env(alias):
            return E.publish_project("demo")


def _failing_upload(pattern: str, exc_factory=None):
    original = storage.LocalDirProvider.upload

    def upload(provider, local_path, remote_key):
        if pattern in remote_key:
            raise (exc_factory or (lambda: storage.StorageError("injected transport failure: %s" % remote_key)))()
        return original(provider, local_path, remote_key)

    return mock.patch.object(storage.LocalDirProvider, "upload", upload)


class TestSuspectedDefectRepro(_ThreeDeviceBase):
    # ---- S2: one peer's upload failure aborts publish for the remaining peers
    def test_s2_publish_one_peer_failure_does_not_skip_other_peers(self) -> None:
        beta_inbox = "v3/inbox/%s/" % self.did["beta"]
        with _failing_upload(beta_inbox):
            res = self.publish()  # pre-fix: raises StorageError, gamma never gets a delivery
        self.assertEqual([d["alias"] for d in res["deliveredTo"]], ["gamma"])
        self.assertEqual(len(self.inbox_deliveries("gamma")), 1)
        self.assertEqual(self.inbox_deliveries("beta"), [])
        failed = res["deliveryErrors"]
        self.assertEqual([f["peerDeviceId"] for f in failed], [self.did["beta"]])
        self.assertEqual(failed[0]["errorClass"], "transient_transport")
        self.assertEqual(res["status"], "partial")
        # The snapshot itself is published: later sync rounds deliver it to beta.
        with self.env("alpha"):
            E.sync_network()
        self.assertEqual(len(self.inbox_deliveries("beta")), 1)

    # ---- S3: _fetch_peer_have_ids swallows every error as "peer has nothing"
    def test_s3_have_fetch_reports_transport_and_signature_failures(self) -> None:
        self.publish()
        with self.env("beta"):
            E.sync_network()  # beta caches the snapshot and uploads a signed have manifest
        with self.env("alpha"):
            cloud = E.load_cloud_config(self.devices["alpha"]["home"])
        provider = storage.LocalDirProvider(str(self.cloud_dir))
        peer_beta = cloud["peers"][self.did["beta"]]
        self.assertTrue(E._fetch_peer_have_ids(provider, peer_beta))
        # transport failure is not "peer has nothing"
        with mock.patch.object(storage.LocalDirProvider, "download",
                               side_effect=storage.StorageError("connection reset")):
            with self.assertRaises(storage.StorageError):
                E._fetch_peer_have_ids(provider, peer_beta)
        # a have signed by another key is not "peer has nothing" either
        wrong = dict(peer_beta, verifyPublicKey=self.identity("gamma")["verifyPublicKey"])
        with self.assertRaises(crypto.TamperError):
            E._fetch_peer_have_ids(provider, wrong)
        # absent stays "unknown / nothing"
        absent = dict(peer_beta, deviceId="dev_0000000000000000")
        self.assertEqual(E._fetch_peer_have_ids(provider, absent), set())

    def test_s3_sync_surfaces_have_failure_and_keeps_forwarding_to_healthy_peer(self) -> None:
        with self.env("beta"):
            E.sync_network()  # beta's (empty) signed have manifest now exists
        snap = self.publish()["snapshotId"]
        # remove alpha's direct delivery to beta so only forwarding can reach it
        beta_inbox = self.cloud_dir / "v3" / "inbox" / self.did["beta"]
        for d in self.inbox_deliveries("beta"):
            for f in (beta_inbox / d).iterdir():
                f.unlink()
            (beta_inbox / d).rmdir()
        have_beta = "v3/have/%s.json" % self.did["beta"]
        original = storage.LocalDirProvider.download

        def download(provider, key, local_path):
            if key == have_beta:
                raise storage.StorageError("injected have read failure")
            return original(provider, key, local_path)

        with self.env("gamma"), mock.patch.object(storage.LocalDirProvider, "download", download):
            res = E.sync_network()
        self.assertIn(snap, self.cached("gamma"))
        # pre-fix: the failure is invisible and gamma forwards as if beta had nothing
        have_errors = [e for e in res["errors"] if e.get("peerDeviceId") == self.did["beta"]]
        self.assertTrue(have_errors, res["errors"])
        self.assertEqual(have_errors[0]["errorClass"], "transient_transport")
        self.assertEqual(res["status"], "partial")
        self.assertEqual(self.inbox_deliveries("beta"), [])
        # once the have is readable again, forwarding resumes
        with self.env("gamma"):
            again = E.sync_network()
        self.assertEqual([f["toDeviceId"] for f in again["forwarded"]], [self.did["beta"]])

    # ---- S8: watch dies on busy/storage errors; KeyboardInterrupt returns ok
    def test_s8_watch_survives_busy_rounds(self) -> None:
        home = self.devices["alpha"]["home"]
        with self.env("alpha"), E._workspace_operation(home):
            res = E.sync_network(watch=True, max_cycles=3, interval=0.01)  # pre-fix: RelayError(busy)
        self.assertEqual(res["cycles"], 3)
        self.assertEqual(res["counters"]["roundsSkippedBusy"], 3)
        self.assertEqual(res["status"], "partial")
        self.assertFalse(res["ok"])
        self.assertEqual([p["errorClass"] for p in res["unresolved"]], ["local_busy"])
        with E._workspace_operation(home):
            pass  # released

    def test_s8_watch_survives_storage_outage(self) -> None:
        snap = self.publish()["snapshotId"]
        rounds = {"n": 0}
        original = storage.LocalDirProvider.list_objects

        def list_objects(provider, prefix=""):
            if prefix.startswith("v3/inbox/"):
                rounds["n"] += 1
                if rounds["n"] <= 2:
                    raise storage.StorageError("injected outage")
            return original(provider, prefix)

        with self.env("beta"), mock.patch.object(storage.LocalDirProvider, "list_objects", list_objects):
            res = E.sync_network(watch=True, max_cycles=4, interval=0.01)
        self.assertEqual(res["cycles"], 4)
        self.assertIn(snap, [s["snapshotId"] for s in res["syncedSnapshots"]])
        self.assertEqual(res["status"], "success", res["unresolved"])
        self.assertEqual(res["counters"]["roundsAborted"], 2)

    def test_s8_receipt_storage_error_does_not_end_watch(self) -> None:
        with self.env("alpha"):
            sent = E.send_project("demo", to_alias="beta")
        original = storage.LocalDirProvider.stat
        calls = {"n": 0}

        def stat(provider, key):
            if key.endswith("/receipt.json"):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise storage.StorageError("injected stat failure")
            return original(provider, key)

        with self.env("alpha"), mock.patch.object(storage.LocalDirProvider, "stat", stat):
            res = E.sync_network(watch=True, max_cycles=2, interval=0.01)
        self.assertEqual(res["cycles"], 2)
        self.assertGreaterEqual(calls["n"], 2)
        self.assertNotIn(sent["relayId"], [r["relayId"] for r in res["syncedReceipts"]])

    def test_s8_keyboard_interrupt_is_cancelled_not_ok(self) -> None:
        home = self.devices["alpha"]["home"]
        with self.env("alpha"), mock.patch("time.sleep", side_effect=KeyboardInterrupt):
            res = E.sync_network(watch=True, max_cycles=5, interval=0.01)
        self.assertFalse(res["ok"])  # pre-fix: ok True, no status
        self.assertEqual(res["status"], "cancelled")
        self.assertEqual(res["cycles"], 1)
        with E._workspace_operation(home):
            pass  # lock released after the interrupt


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- engine-integrated rounds

import collections  # noqa: E402
import sys  # noqa: E402

from relaydesk import ledger as L  # noqa: E402
from relaydesk import sync_runtime as SR  # noqa: E402
from relaydesk import workspace_tx  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from acceptance.soak_faults import FlakyProvider  # noqa: E402


class _Device(object):
    def __init__(self, alias: str, session: "E.SyncSession", flaky: FlakyProvider) -> None:
        self.alias = alias
        self.session = session
        self.flaky = flaky
        self.kinds = collections.Counter()
        self.receipts = collections.Counter()
        self.next_at = 0.0


class _SessionsBase(_ThreeDeviceBase):
    def make_session(self, alias: str, clock: SR.FakeClock, rng: random.Random, **kw) -> _Device:
        flaky = FlakyProvider(storage.LocalDirProvider(str(self.cloud_dir)), rng=random.Random(sum(map(ord, alias))))
        dev = _Device(alias, None, flaky)

        def sink(ev: Dict[str, Any]) -> None:
            dev.kinds[ev["kind"]] += 1
            if ev["kind"] == "receipt_synced":
                dev.receipts[ev["relayId"]] += 1
            kw_sink = kw.get("extra_sink")
            if kw_sink:
                kw_sink(alias, ev)

        with self.env(alias), mock.patch.object(storage, "make_provider", lambda cfg: flaky):
            dev.session = E.SyncSession(clock=clock, rng=rng, on_event=sink, mode="watch",
                                        interval=kw.get("interval", 5.0), max_backoff=kw.get("max_backoff", 60.0))
        return dev

    def step(self, dev: _Device):
        with self.env(dev.alias), mock.patch.object(storage, "make_provider", lambda cfg: dev.flaky):
            return dev.session.step()

    def ledger(self, alias: str) -> Dict[str, Any]:
        return L.load(self.devices[alias]["led"])

    def project(self, alias: str) -> Dict[str, Any]:
        for p in self.ledger(alias).get("projects") or []:
            if p.get("id") == "demo":
                return p
        return {}


class TestEngineRounds(_SessionsBase):
    def test_rounds_disconnect_recover_partial(self) -> None:
        """N rounds (RELAY_DESK_SYNC_ROUNDS, default 300) of outages, a bad peer, random failures and handoffs."""
        n = int(os.environ.get("RELAY_DESK_SYNC_ROUNDS", "300"))
        clock = SR.FakeClock()
        rng = random.Random(7)
        devs = {a: self.make_session(a, clock, rng) for a in ("alpha", "beta", "gamma")}
        in_step = {"on": False}
        applied_by_sync = []
        original_apply = workspace_tx.prepare_and_apply_workspace

        def watched_apply(*a, **kw):
            if in_step["on"]:
                applied_by_sync.append(a)
            return original_apply(*a, **kw)

        beta_did = self.did["beta"]
        outages = [(int(n * 0.1), int(n * 0.15)), (int(n * 0.5), int(n * 0.53))]
        isolation = (int(n * 0.3), int(n * 0.4))
        noisy = (int(n * 0.6), int(n * 0.8))
        handoff_at = {int(n * 0.25): ("alpha", "beta"), int(n * 0.55): ("beta", "gamma"), int(n * 0.85): ("gamma", "alpha")}
        receive_at: Dict[int, Dict[str, Any]] = {}
        owner = "alpha"
        published: List[Dict[str, Any]] = []
        handoffs: List[Dict[str, Any]] = []
        receipt_seen_at: Dict[str, int] = {}
        ack_round: Dict[str, int] = {}
        work_dirs = {"alpha": self.repo}
        max_ring = 0

        with mock.patch.object(workspace_tx, "prepare_and_apply_workspace", watched_apply):
            total_iters = n + 60  # the tail: faults off, convergence
            for it in range(1, total_iters + 1):
                in_fault_window = it <= n
                outage = in_fault_window and any(a <= it < b for a, b in outages)
                isolated = in_fault_window and isolation[0] <= it < isolation[1]
                rate = 0.05 if in_fault_window and noisy[0] <= it < noisy[1] else 0.0
                for dev in devs.values():
                    dev.flaky.outage = outage
                    dev.flaky.fail_rate = rate
                    dev.flaky.fail_patterns = ({"v3/inbox/%s/" % beta_did: "*", "v3/have/%s.json" % beta_did: "*"}
                                               if isolated else {})
                # user actions (unaffected by the sync processes' faults)
                if it in handoff_at and handoff_at[it][0] == owner:
                    src, dst = handoff_at[it]
                    with self.env(src):
                        sent = E.send_project("demo", to_alias=dst)
                    handoffs.append({"relayId": sent["relayId"], "from": src, "to": dst, "sentAt": it})
                    receive_at[it + 3] = handoffs[-1]
                elif it in receive_at:
                    h = receive_at.pop(it)
                    target = self.root / ("work_%s" % h["to"])
                    with self.env(h["to"]):
                        kwargs = {} if h["to"] in work_dirs else {"target_path": str(target)}
                        res = E.receive_relay(h["relayId"], **kwargs)
                    self.assertTrue(res["ok"])
                    work_dirs.setdefault(h["to"], target)
                    owner = h["to"]
                    receipt_seen_at[h["relayId"]] = it
                    h["receivedAt"] = it
                elif it % max(1, n // 10) == 0 and not receive_at and it <= n:
                    (work_dirs[owner] / "keep.txt").write_text("iteration %d\n" % it, encoding="utf-8")
                    with self.env(owner):
                        pub = E.publish_project("demo")
                    published.append({"snapshotId": pub["snapshotId"], "sha": pub["contentSha256"], "at": it})
                clock.advance(5.0)
                for dev in devs.values():
                    if clock.monotonic() < dev.next_at:
                        continue
                    sender_device_before = self.project(dev.alias).get("device")
                    acks_before = sum(dev.receipts.values())
                    in_step["on"] = True
                    try:
                        out = self.step(dev)
                    finally:
                        in_step["on"] = False
                    max_ring = max(max_ring, len(dev.session.log.recent))
                    self.assertLessEqual(len(dev.session.log.recent), 200)
                    if sum(dev.receipts.values()) == acks_before:
                        # without a verified receipt, sync never moves the project's owner
                        self.assertEqual(self.project(dev.alias).get("device"), sender_device_before)
                    for rid, count in dev.receipts.items():
                        ack_round.setdefault(rid, it)
                    dev.next_at = clock.monotonic() + dev.session.next_delay(out)

        # no sync process ever uploads a receipt or applies a workspace
        for dev in devs.values():
            self.assertEqual([k for k in dev.flaky.uploads if k.endswith("receipt.json")], [])
        self.assertEqual(applied_by_sync, [])
        # every handoff was acknowledged exactly once, never before its receipt existed
        self.assertEqual(len(handoffs), 3)
        for h in handoffs:
            sender = devs[h["from"]]
            self.assertEqual(sender.receipts[h["relayId"]], 1, h)
            self.assertGreaterEqual(ack_round[h["relayId"]], receipt_seen_at[h["relayId"]])
            self.assertTrue(self.state(h["from"])["sentRelays"][h["relayId"]]["acked"])
        self.assertEqual(owner, "alpha")
        # counters are exact and the event containers stay bounded
        for dev in devs.values():
            c = dev.session.log.counters
            self.assertEqual(c["snapshotsCached"], dev.kinds["snapshot_cached"])
            self.assertEqual(c["forwarded"], dev.kinds["snapshot_forwarded"])
            self.assertEqual(c["receiptsSynced"], dev.kinds["receipt_synced"])
            self.assertEqual(c["roundsSkippedBusy"], dev.kinds["round_skipped"])
            self.assertEqual(sum(c["problemsOpened"].values()), dev.kinds["problem"])
            self.assertEqual(c["problemsResolved"], dev.kinds["resolved"])
            self.assertEqual(c["rounds"], dev.session.cycles)
            self.assertGreater(c["roundsAborted"], 0)
            self.assertGreater(c["failedOperations"][SR.TRANSIENT], 0)
            summary = dev.session.summary()
            for name in ("syncedReceipts", "syncedSnapshots", "forwarded", "conflictsDetected", "errors"):
                self.assertLessEqual(len(summary[name]), SR.LEGACY_LIST_LIMIT)
            self.assertEqual(summary["status"], SR.SUCCESS, (dev.alias, summary["unresolved"]))
        report_path = os.environ.get("RELAY_DESK_SYNC_ROUNDS_REPORT")
        if report_path:
            with open(report_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "iterations": total_iters, "faultIterations": n, "published": len(published),
                    "handoffs": handoffs, "maxRing": max_ring, "fakeSecondsElapsed": clock.monotonic() - 1000.0,
                    "devices": {d.alias: {"rounds": d.session.cycles, "status": d.session.summary()["status"],
                                          "counters": d.session.log.counters, "injectedFaults": d.flaky.injected,
                                          "uploads": len(d.flaky.uploads),
                                          "receiptUploads": [k for k in d.flaky.uploads if k.endswith("receipt.json")],
                                          "eventKinds": dict(d.kinds)} for d in devs.values()},
                    "workspaceApplicationsBySync": len(applied_by_sync),
                }, fh, indent=1, default=str)
        # convergence: the last published snapshot reached everyone with the author's digest
        final = published[-1]
        for alias in ("alpha", "beta", "gamma"):
            cdir = self.devices[alias]["home"] / "cache" / "snapshots" / final["snapshotId"]
            self.assertTrue((cdir / "payload.tar.gz").is_file(), alias)
            self.assertEqual(E._sha256_file_info(str(cdir / "payload.tar.gz"))[0], final["sha"])
        self.assertLessEqual(max_ring, 200)

    def test_peer_failure_isolated(self) -> None:
        beta_inbox = "v3/inbox/%s/" % self.did["beta"]
        # Phase C: publish records each delivery per recipient key and a published one is not
        # sent again, so the direct delivery to gamma is made to fail here (instead of being
        # deleted afterwards): only the sync round can then bring the snapshot to gamma.
        with _failing_upload("v3/inbox/"):
            pub = self.publish()
        self.assertEqual(self.inbox_deliveries("gamma"), [])
        failing = {beta_inbox: "*", "v3/have/%s.json" % self.did["beta"]: "*"}
        flaky = FlakyProvider(storage.LocalDirProvider(str(self.cloud_dir)))
        flaky.fail_patterns = failing
        with self.env("alpha"), mock.patch.object(storage, "make_provider", lambda cfg: flaky):
            res = E.sync_network()
        self.assertEqual([f["toDeviceId"] for f in res["forwarded"]], [self.did["gamma"]])
        self.assertEqual(res["peers"][self.did["beta"]]["state"], SR.BACKOFF)
        self.assertEqual(res["peers"][self.did["gamma"]]["state"], SR.HEALTHY)
        self.assertEqual(res["status"], SR.PARTIAL)
        self.assertFalse(res["ok"])
        self.assertEqual(self.inbox_deliveries("beta"), [])
        # Phase C delivery IDs: (sync|fwd)_<snapshot>_<sender6>_k<recipient fp8>[_g<n>]
        self.assertTrue(any(d.startswith("sync_%s_" % pub["snapshotId"]) for d in self.inbox_deliveries("gamma")),
                        self.inbox_deliveries("gamma"))

    def test_identity_rotation_detected_between_rounds(self) -> None:
        clock = SR.FakeClock()
        events: List[Dict[str, Any]] = []
        dev = self.make_session("alpha", clock, random.Random(1), extra_sink=lambda a, ev: events.append(ev))
        self.step(dev)
        old_fp = dev.session.identity["keyFingerprint"]
        rotated = crypto.rotate_identity_keys(self.devices["alpha"]["home"])
        clock.advance(5)
        self.step(dev)
        changed = [e for e in events if e["kind"] == "trust_changed" and e.get("what") == "identity"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["detail"]["oldFingerprint"], old_fp)
        self.assertEqual(dev.session.identity["keyFingerprint"], rotated["keyFingerprint"])
        have = json.loads((self.cloud_dir / "v3" / "have" / ("%s.json" % self.did["alpha"])).read_text(encoding="utf-8"))
        crypto.verify_json(have, rotated["verifyPublicKey"], expected_device_id=self.did["alpha"])

    def test_peer_key_changed_parks_without_retry_spam(self) -> None:
        home = self.devices["alpha"]["home"]
        cloud = json.loads((home / "cloud.json").read_text(encoding="utf-8"))
        cloud["peers"][self.did["beta"]]["status"] = "key_changed"
        cloud["peers"][self.did["beta"]]["paired"] = False
        (home / "cloud.json").write_text(json.dumps(cloud), encoding="utf-8")
        clock = SR.FakeClock()
        dev = self.make_session("alpha", clock, random.Random(1))
        snap = self.publish()["snapshotId"]
        for _ in range(6):
            clock.advance(5)
            self.step(dev)
        problems = [p for p in dev.session.log.unresolved_list() if p.get("peerDeviceId") == self.did["beta"]]
        self.assertEqual([(p["errorClass"], p["code"]) for p in problems], [(SR.NEEDS_ACTION, "peer_key_changed")])
        self.assertEqual(dev.kinds["problem"], 1)
        self.assertEqual([k for k in dev.flaky.uploads if ("/inbox/%s/" % self.did["beta"]) in k], [])
        self.assertEqual(dev.session.health.state(self.did["beta"]), SR.PARKED)
        summary = dev.session.summary()
        self.assertEqual(summary["status"], SR.NEEDS_ACTION_STATUS)
        self.assertIn("--accept", problems[0]["action"])
        self.assertIn(snap, self.cached("alpha"))

    def test_signature_failure_triggers_descriptor_probe(self) -> None:
        with self.env("beta"):
            E.sync_network()  # beta's have exists (signed by its current key)
        shown_before = json.loads((self.devices["alpha"]["home"] / "cloud.json").read_text(encoding="utf-8"))["shownFingerprints"]
        crypto.rotate_identity_keys(self.devices["beta"]["home"])
        with self.env("beta"):
            E.pair_device()  # republishes beta's descriptor with the new keys
            E.sync_network()  # re-signs its have with the new key
        self.publish()
        with self.env("alpha"):
            res = E.sync_network()
        cloud = json.loads((self.devices["alpha"]["home"] / "cloud.json").read_text(encoding="utf-8"))
        peer = cloud["peers"][self.did["beta"]]
        self.assertEqual(peer["status"], "key_changed")
        self.assertFalse(peer["paired"])
        self.assertEqual(cloud["shownFingerprints"], shown_before)
        self.assertEqual(res["peers"][self.did["beta"]]["state"], SR.PARKED)
        codes = {(p["errorClass"], p["code"]) for p in res["unresolved"] if p.get("peerDeviceId") == self.did["beta"]}
        self.assertEqual(codes, {(SR.NEEDS_ACTION, "peer_key_changed")})
        self.assertEqual(res["status"], SR.NEEDS_ACTION_STATUS)

    def test_corrupt_state_mid_watch_stops_writes_until_fixed(self) -> None:
        self.publish()
        clock = SR.FakeClock()
        dev = self.make_session("beta", clock, random.Random(1))
        self.step(dev)  # caches the snapshot: relay_state.json now exists
        path = self.devices["beta"]["home"] / "relay_state.json"
        good = path.read_bytes()
        path.write_bytes(b"{broken")
        uploads_before = len(dev.flaky.uploads)
        for _ in range(3):
            clock.advance(5)
            out = self.step(dev)
            self.assertTrue(out.aborted)
            self.assertEqual(out.global_error, SR.NEEDS_ACTION)
        self.assertEqual(path.read_bytes(), b"{broken")
        self.assertEqual(len(dev.flaky.uploads), uploads_before)
        self.assertEqual(dev.kinds["problem"], 1)
        self.assertEqual(dev.session.next_delay(out), dev.session.max_backoff)
        self.assertEqual(dev.session.summary()["status"], SR.NEEDS_ACTION_STATUS)
        path.write_bytes(good)
        clock.advance(5)
        self.step(dev)
        self.assertEqual(dev.session.summary()["status"], SR.SUCCESS)
        self.assertEqual(dev.kinds["resolved"], 1)

    def test_invalid_delivery_reported_once_and_not_redownloaded(self) -> None:
        bad = self.cloud_dir / "v3" / "inbox" / self.did["beta"] / "sync_forged_0001"
        bad.mkdir(parents=True)
        (bad / "ready.json").write_text("{not json", encoding="utf-8")
        (bad / "package.enc").write_bytes(b"x" * 32)
        clock = SR.FakeClock()
        dev = self.make_session("beta", clock, random.Random(1))
        dev.flaky.record = True
        for _ in range(8):
            clock.advance(5)
            self.step(dev)
        downloads = [k for op, k in dev.flaky.calls if op == "download" and "sync_forged_0001" in k]
        self.assertEqual(len(downloads), 1)
        self.assertEqual(dev.kinds["problem"], 1)
        summary = dev.session.summary()
        self.assertEqual([(p["errorClass"], p["code"]) for p in summary["unresolved"]],
                         [(SR.INTEGRITY, "remote_json_invalid")])
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(summary["errors"][0]["deliveryId"], "sync_forged_0001")
        for f in bad.iterdir():
            f.unlink()
        bad.rmdir()
        clock.advance(5)
        self.step(dev)
        self.assertEqual(dev.session.summary()["status"], SR.SUCCESS)

    def test_delivery_sealed_to_rotated_key_needs_action_not_tamper(self) -> None:
        """Phase E C03. Phase B reported needs_action; Phase C marks it obsolete and asks for a new delivery."""
        snap = self.publish()["snapshotId"]
        crypto.rotate_identity_keys(self.devices["beta"]["home"])
        events: List[Dict[str, Any]] = []
        with self.env("beta"):
            res = E.sync_network(on_event=events.append)
        self.assertEqual(res["unresolved"], [])  # neither tampering nor a problem: the delivery is obsolete
        self.assertEqual(res["status"], SR.SUCCESS)
        obsolete = [e for e in events if e["kind"] == "delivery_obsolete"]
        self.assertEqual([(e["snapshotId"], e["reason"], e["needed"]) for e in obsolete],
                         [(snap, "recipient_key_retired", True)])
        have = json.loads((self.cloud_dir / "v3" / "have" / ("%s.json" % self.did["beta"])).read_text(encoding="utf-8"))
        self.assertEqual(have["needs"], [snap])
        again = None
        with self.env("beta"):
            again = E.sync_network(on_event=events.append)
        self.assertEqual(again["status"], SR.SUCCESS)
        self.assertEqual(len([e for e in events if e["kind"] == "delivery_obsolete"]), 1)  # judged once

    def test_delivery_signed_by_retired_key_needs_action_not_tamper(self) -> None:
        """Phase E C02: after a re-confirmed key rotation, older deliveries signed with the old key."""
        self.create_git_project("beta", proj_id="bproj", name="beta 项目")
        with self.env("beta"):
            bsnap = E.publish_project("bproj")["snapshotId"]  # sync_ delivery to alpha, signed with beta's current key
        crypto.rotate_identity_keys(self.devices["beta"]["home"])
        with self.env("beta"):
            E.pair_device()  # republish the descriptor with the new keys
        with self.env("alpha"), mock.patch.object(L, "now_iso", return_value="2099-01-01T00:00:00.000Z"):
            view = E.pair_device(with_device=self.did["beta"], accept=False)
            fp = next(p for p in view["peers"] if p["deviceId"] == self.did["beta"])["pairFingerprint"]
            E.pair_device(with_device=self.did["beta"], accept=True, fingerprint=fp)
        with self.env("alpha"):
            res = E.sync_network()
            again = E.sync_network()
        # Phase C (was needs_action signed_by_retired_key in Phase B): alpha recorded beta's
        # old key when re-confirming, so the old delivery is recognised as obsolete and beta's
        # old have as stale. Neither is a problem, neither is tampering.
        self.assertEqual(res["unresolved"], [])
        self.assertEqual(res["status"], SR.SUCCESS)
        self.assertEqual(res["peers"][self.did["beta"]]["status"], "paired")
        self.assertEqual(again["status"], SR.SUCCESS)
        self.assertNotIn(bsnap, self.cached("alpha"))  # not cached from the obsolete delivery
        with self.env("beta"):
            E.sync_network()  # re-signs beta's have and re-delivers bproj for alpha with its current key
        with self.env("alpha"):
            third = E.sync_network()
        self.assertEqual(third["unresolved"], [])
        self.assertEqual([(x["snapshotId"], x["senderDeviceId"]) for x in third["syncedSnapshots"]],
                         [(bsnap, self.did["beta"])])
        self.assertIn(bsnap, self.cached("alpha"))

    def test_status_file_written_and_one_shot_summary_fields(self) -> None:
        snap = self.publish()["snapshotId"]
        with self.env("beta"):
            res = E.sync_network()
        for key in ("ok", "status", "cycles", "syncedReceipts", "syncedSnapshots", "forwarded", "conflictsDetected",
                    "errors", "counters", "recentEvents", "unresolved", "peers", "legacyListsTruncated", "runId"):
            self.assertIn(key, res)
        self.assertEqual(res["status"], SR.SUCCESS)
        self.assertTrue(res["ok"])
        self.assertEqual(res["counters"]["snapshotsCached"], 1)
        status = SR.read_status_file(self.devices["beta"]["home"] / "sync_status.json")
        self.assertEqual(status["status"], SR.SUCCESS)
        self.assertFalse(status["running"])
        self.assertNotIn("recentEvents", status)
        self.assertIn(snap, self.cached("beta"))
