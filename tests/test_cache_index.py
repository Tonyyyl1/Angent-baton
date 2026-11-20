# -*- coding: utf-8 -*-
"""Phase F: the cross-round verified-cache index used by sync sessions.

``delivery_state.CacheIndex`` lets a watch-mode sync skip re-reading cache
entries whose files are byte-for-byte unchanged since the last full check. These
tests pin that it never accepts more than ``cache_ok`` would (changed, missing,
replaced or symlinked entries, changed attestation, expired memos), that idle
rounds really stop re-reading the cache, that corruption between rounds is still
found and repaired, that lazily loaded metadata still reaches forwarded
deliveries, and that claiming a handoff still downloads, decrypts and checks
the digest regardless of what the cache holds.
"""
import hashlib
import json
import os
import random
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from relaydesk import crypto
from relaydesk import delivery_state as DS
from relaydesk import relay_engine as E
from relaydesk import sync_runtime as SR
from test_sync_watch import _SessionsBase


def _write_entry(cdir: Path, payload: bytes, version_id: str = "") -> Dict[str, Any]:
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "payload.tar.gz").write_bytes(payload)
    record = {"versionId": version_id or cdir.name, "projectId": "demo",
              "contentSha256": hashlib.sha256(payload).hexdigest(), "contentSize": len(payload)}
    (cdir / "snapshot.json").write_text(json.dumps(record), encoding="utf-8")
    return record


class TestCacheIndexUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name) / "cache" / "snapshots"
        self.cdir = self.root / "snap_20261119_aaaa0001"
        self.record = _write_entry(self.cdir, b"payload-bytes-" * 100)

    def tearDown(self) -> None:
        self.td.cleanup()

    def settled(self, index: DS.CacheIndex, **kw: Any) -> None:
        """First check writes the digest memo; the second records signatures of the settled files."""
        self.assertTrue(index.check(self.cdir, **kw)[0])
        self.assertTrue(index.check(self.cdir, **kw)[0])

    def test_unchanged_entry_is_answered_without_reading_or_hashing(self) -> None:
        index = DS.CacheIndex()
        self.settled(index)
        with mock.patch.object(DS, "_read_json", side_effect=AssertionError("re-read")), \
                mock.patch.object(DS, "_hash_file", side_effect=AssertionError("re-hashed")):
            ok, record = index.check(self.cdir)
        self.assertTrue(ok)
        self.assertEqual(record, self.record)
        self.assertGreaterEqual(index.hits, 1)
        record["versionId"] = "mutated"  # callers get a copy; the index keeps its own
        self.assertEqual(index.check(self.cdir)[1]["versionId"], self.cdir.name)

    def test_changed_payload_is_checked_again_and_rejected(self) -> None:
        index = DS.CacheIndex()
        self.settled(index)
        tar = self.cdir / "payload.tar.gz"
        tar.write_bytes(tar.read_bytes() + b"x")
        self.assertFalse(index.check(self.cdir)[0])
        self.assertFalse(DS.cache_ok(self.cdir))
        # same verdict while nothing changes, and recovery once the right bytes are back
        self.assertFalse(index.check(self.cdir)[0])
        tar.write_bytes(b"payload-bytes-" * 100)
        self.assertTrue(index.check(self.cdir)[0])

    def test_never_accepts_more_than_cache_ok(self) -> None:
        """An in-place rewrite that keeps size and mtime is trusted by the memo in cache_ok too."""
        index = DS.CacheIndex()
        self.settled(index)
        tar = self.cdir / "payload.tar.gz"
        st = tar.stat()
        data = bytearray(tar.read_bytes())
        data[0] ^= 0x01
        with open(str(tar), "r+b") as fh:
            fh.write(bytes(data))
        os.utime(str(tar), ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(index.check(self.cdir)[0], DS.cache_ok(self.cdir))
        # with a real re-hash (budget available, memo expired) both reject it
        later = DS.wall_now() + DS.MEMO_MAX_AGE + timedelta(days=1)
        with mock.patch.object(DS, "wall_now", return_value=later):
            self.assertFalse(index.check(self.cdir, budget=DS.HashBudget())[0])

    def test_record_replaced_is_checked_again(self) -> None:
        index = DS.CacheIndex()
        self.settled(index)
        (self.cdir / "snapshot.json").write_text(json.dumps(dict(self.record, versionId="snap_other")),
                                                 encoding="utf-8")
        self.assertFalse(index.check(self.cdir)[0])
        self.assertFalse(DS.cache_ok(self.cdir))

    def test_attestation_change_is_never_answered_from_the_index(self) -> None:
        index = DS.CacheIndex()
        digest = crypto.snapshot_record_digest
        good = digest(self.record)
        self.settled(index, record_digest=digest, expected_record_sha=good)
        with mock.patch.object(DS, "_read_json", wraps=DS._read_json) as reads:
            self.assertFalse(index.check(self.cdir, record_digest=digest, expected_record_sha="0" * 64)[0])
        self.assertTrue(reads.called)
        self.assertTrue(index.check(self.cdir, record_digest=digest, expected_record_sha=good)[0])

    def test_missing_payload_then_restored(self) -> None:
        index = DS.CacheIndex()
        self.settled(index)
        tar = self.cdir / "payload.tar.gz"
        data = tar.read_bytes()
        tar.unlink()
        self.assertFalse(index.check(self.cdir)[0])
        tar.write_bytes(data)
        self.assertTrue(index.check(self.cdir)[0])

    def test_directory_replaced_by_symlink_is_never_reused(self) -> None:
        index = DS.CacheIndex()
        self.settled(index)
        moved = self.root / "elsewhere"
        shutil.move(str(self.cdir), str(moved))
        os.symlink(str(moved), str(self.cdir))
        self.assertIsNone(DS.CacheIndex.signature(self.cdir))
        self.assertFalse(index.check(self.cdir)[0])
        self.assertNotIn(str(self.cdir), index.entries)

    def test_symlinked_payload_file_is_never_reused(self) -> None:
        index = DS.CacheIndex()
        target = Path(self.td.name) / "outside.tar.gz"
        tar = self.cdir / "payload.tar.gz"
        shutil.move(str(tar), str(target))
        os.symlink(str(target), str(tar))
        self.assertIsNone(DS.CacheIndex.signature(self.cdir))
        for _ in range(2):
            self.assertEqual(index.check(self.cdir)[0], DS.cache_ok(self.cdir))
        self.assertNotIn(str(self.cdir), index.entries)

    def test_a_failure_is_never_remembered(self) -> None:
        """A transient read error must not stick: the next check runs cache_ok again."""
        index = DS.CacheIndex()
        with mock.patch.object(DS, "_hash_file", side_effect=OSError("EIO")):
            self.assertFalse(index.check(self.cdir)[0])
        self.assertNotIn(str(self.cdir), index.entries)
        self.assertTrue(index.check(self.cdir)[0])

    def test_expired_memo_goes_back_to_the_budgeted_rehash(self) -> None:
        index = DS.CacheIndex()
        self.settled(index)
        later = DS.wall_now() + DS.MEMO_MAX_AGE + timedelta(days=1)
        with mock.patch.object(DS, "wall_now", return_value=later):
            with mock.patch.object(DS, "_hash_file", wraps=DS._hash_file) as hashed:
                self.assertTrue(index.check(self.cdir, budget=DS.HashBudget(0))[0])  # no budget: trusted
                self.assertFalse(hashed.called)
                # trusted only because the budget was spent: not reusable, the next check re-hashes
                self.assertTrue(index.check(self.cdir, budget=DS.HashBudget())[0])
                self.assertTrue(hashed.called)

    def test_limit_starts_over_instead_of_growing(self) -> None:
        index = DS.CacheIndex(limit=2)
        dirs = [self.cdir]
        for n in range(2, 5):
            d = self.root / ("snap_20261119_aaaa000%d" % n)
            _write_entry(d, b"p%d" % n)
            dirs.append(d)
        for _ in range(2):
            for d in dirs:
                self.assertTrue(index.check(d)[0])
        self.assertLessEqual(len(index.entries), 2)


class TestListingAndLazyMetadata(unittest.TestCase):
    def test_listing_items_load_metadata_only_when_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cdir = home / "cache" / "snapshots" / "snap_20261119_bbbb0001"
            _write_entry(cdir, b"abc" * 50)
            (cdir / "meta_plain.json").write_text(json.dumps({"projectId": "demo", "projectName": "Demo"}),
                                                  encoding="utf-8")
            (cdir / "metadata.json").write_text(json.dumps({"name": "Demo", "lastTask": "t"}), encoding="utf-8")
            for checker in (None, E._CacheChecker(home), E._CacheChecker(home, index=DS.CacheIndex())):
                items = E._list_cached_snapshots(home, checker=checker)
                self.assertEqual([i["versionId"] for i in items], [cdir.name])
                self.assertNotIn("metaPlain", items[0])
                self.assertEqual(E._item_meta_plain(items[0], "demo"), {"projectId": "demo", "projectName": "Demo"})
            (cdir / "meta_plain.json").unlink()
            item = E._list_cached_snapshots(home)[0]
            self.assertEqual(E._item_meta_plain(item, "demo")["projectName"], "Demo")  # metadata fallback

    def test_indexed_listing_refuses_symlinked_cache_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            outside = Path(td) / "outside" / "snapshots"
            _write_entry(outside / "snap_20261119_cccc0001", b"zzz")
            (home / "cache").mkdir(parents=True)
            os.symlink(str(outside), str(home / "cache" / "snapshots"))
            checker = E._CacheChecker(home, index=DS.CacheIndex())
            self.assertFalse(checker.ok("snap_20261119_cccc0001"))
            self.assertEqual(E._list_cached_snapshots(home, checker=checker), [])
            self.assertFalse(checker.ok("../escape"))


class TestSessionCacheIndex(_SessionsBase):
    def test_idle_rounds_stop_rereading_and_corruption_is_still_repaired(self) -> None:
        snaps = [self.publish()["snapshotId"] for _ in range(3)]
        clock = SR.FakeClock()
        beta = self.make_session("beta", clock, random.Random(3))
        for _ in range(2):  # round 1 caches; round 2 records the settled signatures
            out = self.step(beta)
            self.assertFalse(out.aborted)
            clock.advance(5.0)
        self.assertTrue(set(snaps) <= set(self.cached("beta")))
        reads: List[str] = []
        orig_ds, orig_re = DS._read_json, E._read_json_file

        def ds_read(path: Any) -> Any:
            reads.append(str(path))
            return orig_ds(path)

        def re_read(path: Any, default: Any) -> Any:
            reads.append(str(path))
            return orig_re(path, default)

        hits_before = beta.session._cache_index.hits
        with mock.patch.object(DS, "_read_json", ds_read), mock.patch.object(E, "_read_json_file", re_read), \
                mock.patch.object(DS, "_hash_file", side_effect=AssertionError("idle round re-hashed")):
            out = self.step(beta)
        self.assertFalse(out.aborted)
        self.assertEqual([p for p in reads if os.sep + "snapshots" + os.sep in p], [])
        self.assertGreaterEqual(beta.session._cache_index.hits - hits_before, len(snaps))

        # Corrupt the cached head between rounds: the next round finds it and repairs it from the inbox.
        home = self.devices["beta"]["home"]
        head = snaps[-1]
        tar = home / "cache" / "snapshots" / head / "payload.tar.gz"
        tar.write_bytes(b"corrupted" + tar.read_bytes())
        clock.advance(5.0)
        repaired_before = beta.session.log.counters["snapshotsRepaired"]
        out = self.step(beta)
        self.assertFalse(out.aborted)
        self.assertEqual(beta.session.log.counters["snapshotsRepaired"], repaired_before + 1)
        record = json.loads((tar.parent / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256(tar.read_bytes()).hexdigest(), record["contentSha256"])

        # A payload deleted between rounds is requested again the same way (missing, not trusted).
        tar.unlink()
        clock.advance(5.0)
        out = self.step(beta)
        self.assertFalse(out.aborted)
        self.assertEqual(beta.session.log.counters["snapshotsRepaired"], repaired_before + 2)
        self.assertTrue(DS.cache_ok(tar.parent))

    def test_forwarded_delivery_still_carries_the_cached_metadata(self) -> None:
        snap = self.publish()["snapshotId"]
        gamma_inbox = self.cloud_dir / "v3" / "inbox" / self.did["gamma"]
        for d in list(gamma_inbox.iterdir()):
            shutil.rmtree(str(d))  # only beta's forward can bring the snapshot to gamma now
        clock = SR.FakeClock()
        beta = self.make_session("beta", clock, random.Random(5))
        self.step(beta)
        fwd = [d for d in self.inbox_deliveries("gamma") if d.startswith("fwd_%s_" % snap)]
        self.assertEqual(len(fwd), 1, self.inbox_deliveries("gamma"))
        with self.env("gamma"):
            res = E.sync_network()
        self.assertEqual(res["status"], "success", res["unresolved"])
        self.assertIn(snap, self.cached("gamma"))
        meta_beta = json.loads((self.devices["beta"]["home"] / "cache" / "snapshots" / snap / "meta_plain.json")
                               .read_text(encoding="utf-8"))
        meta_gamma = json.loads((self.devices["gamma"]["home"] / "cache" / "snapshots" / snap / "meta_plain.json")
                                .read_text(encoding="utf-8"))
        self.assertEqual(meta_gamma, meta_beta)
        self.assertEqual(meta_gamma.get("projectId"), "demo")

    def test_claim_still_downloads_decrypts_and_checks_digest(self) -> None:
        with self.env("alpha"):
            sent = E.send_project("demo", to_alias="beta")
        rid = sent["relayId"]
        clock = SR.FakeClock()
        beta = self.make_session("beta", clock, random.Random(9))
        for _ in range(3):  # the handoff's snapshot is cached and indexed by the sync session
            self.assertFalse(self.step(beta).aborted)
            clock.advance(5.0)
        snap = sent.get("snapshotId") or ""
        if snap:
            self.assertIn(snap, self.cached("beta"))
        pkg = self.cloud_dir / "v3" / "inbox" / self.did["beta"] / rid / "package.enc"
        good = pkg.read_bytes()
        bad = bytearray(good)
        bad[-10] ^= 0xFF
        pkg.write_bytes(bytes(bad))
        target = self.root / "work_beta"
        with self.env("beta"):
            with self.assertRaises(crypto.TamperError):
                E.receive_relay(rid, target_path=str(target))
        pkg.write_bytes(good)
        with self.env("beta"), mock.patch.object(crypto, "decrypt_package", wraps=crypto.decrypt_package) as dec:
            res = E.receive_relay(rid, target_path=str(target))
        self.assertTrue(res["ok"])
        self.assertTrue(dec.called)


if __name__ == "__main__":
    unittest.main()
