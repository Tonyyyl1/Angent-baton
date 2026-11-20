# -*- coding: utf-8 -*-
"""Phase C: unit tests for relaydesk.delivery_state (no network, no engine rounds)."""
import hashlib
import json
import os
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from relaydesk import crypto
from relaydesk import delivery_state as DS
from relaydesk import sync_runtime as SR
from relaydesk import validation as V

SNAP = "snap_20261119_ab12cd34"
SENDER = "dev_00112233aabbccdd"
FP = "ABCD-EF01-2345-6789"


class TestIdentifiers(unittest.TestCase):
    def test_delivery_ids_are_bound_to_the_recipient_key(self) -> None:
        a = DS.delivery_id_for(SNAP, SENDER, FP, 1, author_is_self=True)
        self.assertEqual(a, "sync_%s_bbccdd_kabcdef01" % SNAP)
        b = DS.delivery_id_for(SNAP, SENDER, "1111-2222-3333-4444", 1, author_is_self=False)
        self.assertEqual(b, "fwd_%s_bbccdd_k11112222" % SNAP)
        g3 = DS.delivery_id_for(SNAP, SENDER, FP, 3, author_is_self=False)
        self.assertTrue(g3.endswith("_g3"))
        for did in (a, b, g3):
            V.validate_delivery_id(did)
            self.assertLessEqual(len(did), 80)  # also within RELAY_ID_RE of older engines
            self.assertTrue(did.startswith(("sync_", "fwd_")))  # older v3 inbox lists skip these
        self.assertNotEqual(a, DS.delivery_id_for(SNAP, SENDER, "9999-0000-0000-0000", 1, True))

    def test_legacy_ids_and_parsing(self) -> None:
        self.assertEqual(DS.legacy_delivery_ids(SNAP, SENDER), ["sync_%s_bbccdd" % SNAP, "fwd_%s_bbccdd" % SNAP])
        self.assertEqual(DS.match_snapshot_delivery("fwd_%s_bbccdd" % SNAP, SNAP),
                         {"prefix": "fwd", "sender6": "bbccdd", "fp8": "", "gen": 1})
        self.assertEqual(DS.match_snapshot_delivery("sync_%s_bbccdd_kabcdef01_g2" % SNAP, SNAP),
                         {"prefix": "sync", "sender6": "bbccdd", "fp8": "abcdef01", "gen": 2})
        self.assertIsNone(DS.match_snapshot_delivery("r_20261119_abcdefgh", SNAP))
        self.assertIsNone(DS.match_snapshot_delivery("sync_%s_bbccdd_extra" % SNAP, SNAP))
        self.assertIsNone(DS.match_snapshot_delivery("sync_snap_other_bbccdd", SNAP))

    def test_outbound_key_normalizes_fingerprints(self) -> None:
        self.assertEqual(DS.outbound_key("dev_x", "abcd-ef01", SNAP), DS.outbound_key("dev_x", "ABCDEF01", SNAP))


class _HomeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()


class TestLoadAndUpdate(_HomeCase):
    def test_missing_is_none_and_update_creates(self) -> None:
        self.assertIsNone(DS.load(self.home))
        calls = []
        st = DS.update(self.home, lambda s: s["self"].update({"haveSeq": 3}), init=lambda s: calls.append(1))
        self.assertEqual(st["self"]["haveSeq"], 3)
        self.assertEqual(calls, [1])
        DS.update(self.home, lambda s: None, init=lambda s: calls.append(2))
        self.assertEqual(calls, [1])  # init runs only when the file is first created
        self.assertEqual(DS.load(self.home)["format"], DS.FORMAT)

    def test_corrupt_file_raises_and_is_never_rewritten(self) -> None:
        path = self.home / DS.FILE_NAME
        for raw in (b"{broken", b"", b"[]", b"\xff\xfe", json.dumps({"format": 1, "outbound": []}).encode(),
                    json.dumps({"format": 99}).encode(), json.dumps({"format": "1"}).encode(),
                    json.dumps({"format": 1, "inbound": {"x": 3}}).encode()):
            with self.subTest(raw=raw[:30]):
                path.write_bytes(raw)
                before = (path.read_bytes(), os.stat(str(path)).st_ino)
                with self.assertRaises(DS.DeliveryStateCorruptedError) as cm:
                    DS.load(self.home)
                with self.assertRaises(DS.DeliveryStateCorruptedError):
                    DS.update(self.home, lambda s: s["self"].update({"x": 1}))
                with self.assertRaises(DS.DeliveryStateCorruptedError):
                    DS.DeliveryBook(self.home)
                self.assertEqual((path.read_bytes(), os.stat(str(path)).st_ino), before)
                classified = SR.classify_error(cm.exception, "trust_check")
                self.assertEqual((classified.error_class, classified.code),
                                 (SR.NEEDS_ACTION, "delivery_state_corrupted"))
                self.assertIn("deliveries.json", classified.action)
                self.assertTrue(isinstance(cm.exception, V.LocalStateCorruptError))

    def test_book_writes_through(self) -> None:
        book = DS.DeliveryBook(self.home)
        book.set_inbound("sync_x", state="obsolete", snapshotId=SNAP)
        self.assertEqual(DS.load(self.home)["inbound"]["sync_x"]["state"], "obsolete")
        self.assertEqual(book.inbound("sync_x")["snapshotId"], SNAP)


class TestOutboundBookkeeping(_HomeCase):
    def _entry(self, book, did, fp, status, verified=False):
        def fn(st):
            ent = DS.new_outbound_entry(did, fp, SNAP, "demo", SENDER, FP)
            ent["status"] = status
            if verified:
                ent["peerVerified"] = {"haveSeq": 1, "at": "x"}
            st["outbound"][DS.outbound_key(did, fp, SNAP)] = ent
        book.mutate(fn)

    def test_supersede_and_verified_under_other_key(self) -> None:
        book = DS.DeliveryBook(self.home)
        self._entry(book, "dev_b", "OLD1-0000", "peer_verified", verified=True)
        self._entry(book, "dev_c", "CCCC-0000", "published")
        self.assertTrue(book.verified_under_other_key("dev_b", "NEW1-0000", SNAP))
        self.assertFalse(book.verified_under_other_key("dev_b", "OLD1-0000", SNAP))
        self.assertEqual(book.supersede_other_keys("dev_b", "NEW1-0000"), 1)
        self.assertEqual(book.outbound("dev_b", "OLD1-0000", SNAP)["status"], "superseded")
        self.assertEqual(book.outbound("dev_b", "OLD1-0000", SNAP)["supersededFrom"], "peer_verified")
        self.assertTrue(book.verified_under_other_key("dev_b", "NEW1-0000", SNAP))  # evidence survives
        self.assertEqual(book.outbound("dev_c", "CCCC-0000", SNAP)["status"], "published")  # other peers untouched
        self.assertEqual(book.supersede_other_keys("dev_b", "NEW1-0000"), 0)

    def test_generation_numbers(self) -> None:
        self.assertEqual(DS.next_generation_number(None), 1)
        ent = {"generations": [{"gen": 1}, {"gen": 2, "state": "preparing"}]}
        self.assertEqual(DS.next_generation_number(ent), 3)
        self.assertEqual(DS.latest_generation(ent)["gen"], 2)

    def test_needs_lists_only_unsatisfied_snapshots(self) -> None:
        book = DS.DeliveryBook(self.home)
        book.set_inbound("d1", state="obsolete", snapshotId="snap_a")
        book.set_inbound("d2", state="quarantined", snapshotId="snap_b")
        book.set_inbound("d3", state="repair_pending", snapshotId="snap_c")
        book.set_inbound("d4", state="cached", snapshotId="snap_d")
        book.set_inbound("d5", state="obsolete", snapshotId="snap_e")
        DS.mark_pruned(self.home, ["snap_e"])
        book = DS.DeliveryBook(self.home)
        self.assertEqual(book.needs(lambda sid: sid == "snap_b"), ["snap_a", "snap_c"])


class TestCacheMemo(_HomeCase):
    def _cache(self, payload: bytes = b"payload-bytes" * 100) -> Path:
        cdir = self.home / "cache" / "snapshots" / SNAP
        cdir.mkdir(parents=True)
        (cdir / "payload.tar.gz").write_bytes(payload)
        snap = {"versionId": SNAP, "projectId": "demo", "contentSha256": hashlib.sha256(payload).hexdigest(),
                "signature": "sig"}
        (cdir / "snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
        return cdir

    def test_memo_written_and_reused(self) -> None:
        cdir = self._cache()
        self.assertTrue(DS.cache_ok(cdir))
        memo = json.loads((cdir / DS.MEMO_NAME).read_text(encoding="utf-8"))
        self.assertEqual(memo["size"], (cdir / "payload.tar.gz").stat().st_size)
        with mock.patch.object(DS, "_hash_file", side_effect=AssertionError("re-hashed")):
            self.assertTrue(DS.cache_ok(cdir))  # size and mtime unchanged: memo trusted

    def test_changed_payload_is_rehashed_and_rejected(self) -> None:
        cdir = self._cache()
        self.assertTrue(DS.cache_ok(cdir))
        tar = cdir / "payload.tar.gz"
        data = bytearray(tar.read_bytes())
        data[5] ^= 0xFF
        st = tar.stat()
        tar.write_bytes(bytes(data))
        os.utime(str(tar), ns=(st.st_atime_ns, st.st_mtime_ns + 1000))
        self.assertFalse(DS.cache_ok(cdir))

    def test_missing_files_and_record_mismatch(self) -> None:
        cdir = self._cache()
        self.assertFalse(DS.cache_ok(cdir, crypto.snapshot_record_digest, "0" * 64))
        record = json.loads((cdir / "snapshot.json").read_text(encoding="utf-8"))
        self.assertTrue(DS.cache_ok(cdir, crypto.snapshot_record_digest, crypto.snapshot_record_digest(record)))
        (cdir / "payload.tar.gz").unlink()
        self.assertFalse(DS.cache_ok(cdir))
        self.assertFalse(DS.cache_ok(self.home / "cache" / "snapshots" / "snap_missing"))

    def test_old_memo_rehash_within_budget(self) -> None:
        cdir = self._cache()
        self.assertTrue(DS.cache_ok(cdir))
        later = DS.wall_now() + DS.MEMO_MAX_AGE + timedelta(days=1)
        with mock.patch.object(DS, "wall_now", return_value=later):
            spent = DS.HashBudget(0)
            with mock.patch.object(DS, "_hash_file", side_effect=AssertionError("no budget left")):
                self.assertTrue(DS.cache_ok(cdir, budget=spent))
            calls = []
            real = DS._hash_file

            def counting(path):
                calls.append(path)
                return real(path)

            with mock.patch.object(DS, "_hash_file", side_effect=counting):
                self.assertTrue(DS.cache_ok(cdir, budget=DS.HashBudget()))
            self.assertEqual(len(calls), 1)


class TestViewsForPhaseD(unittest.TestCase):
    def test_retention_inputs(self) -> None:
        state = {"outbound": {
            "dev_b|BBBB|snap_1": {"recipientDeviceId": "dev_b", "recipientKeyFingerprint": "BBBB", "snapshotId": "snap_1",
                                  "status": "published",
                                  "generations": [{"readyKey": "k1", "state": "published"}]},
            "dev_b|OLD0|snap_1": {"recipientDeviceId": "dev_b", "recipientKeyFingerprint": "OLD0", "snapshotId": "snap_1",
                                  "status": "superseded",
                                  "generations": [{"readyKey": "k0", "state": "published"}]},
            "dev_c|CCCC|snap_2": {"recipientDeviceId": "dev_c", "recipientKeyFingerprint": "CCCC", "snapshotId": "snap_2",
                                  "status": "peer_verified",
                                  "generations": [{"readyKey": "k2", "state": "published"}]},
            "dev_c|CCCC|snap_3": {"recipientDeviceId": "dev_c", "recipientKeyFingerprint": "CCCC", "snapshotId": "snap_3",
                                  "status": "pending",
                                  "generations": [{"readyKey": "k3", "state": "preparing"}]},
        }, "inbound": {"d": {"state": "quarantined", "snapshotId": "snap_q"}}}
        relay_state = {"localCurrentVersion": {"demo": "snap_cur"},
                       "versionGraph": {"demo": {"heads": ["snap_cur", "snap_fork"]}},
                       "sentRelays": {"r1": {"acked": False, "snapshotId": "snap_h"}, "r0": {"acked": True,
                                                                                          "snapshotId": "snap_old"}}}
        out = DS.retention_inputs(state, relay_state, {"dev_b": "BBBB", "dev_c": "CCCC"}, {"dev_c": ["snap_n"]})
        self.assertEqual(sorted(out["snapshots"]),
                         ["snap_1", "snap_3", "snap_cur", "snap_fork", "snap_h", "snap_n", "snap_q"])
        self.assertIn("awaitingPeerVerification", out["snapshots"]["snap_1"])
        self.assertEqual(out["checkpointReadyKeys"], ["k1", "k3"])
        self.assertEqual(out["deletableCheckpointReadyKeys"], ["k0", "k2"])

    def test_delivery_view_keeps_states_apart(self) -> None:
        state = {"outbound": {"a": {"recipientDeviceId": "dev_b", "status": "published"},
                              "b": {"recipientDeviceId": "dev_b", "status": "peer_verified"}}}
        rs = {"sentRelays": {"r1": {"toDeviceId": "dev_b", "acked": True}, "r2": {"toDeviceId": "dev_b"}}}
        row = DS.delivery_view(state, rs)["peers"]["dev_b"]
        self.assertEqual((row["published"], row["peerVerified"], row["claimed"], row["awaitingReceipt"]), (1, 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
