# -*- coding: utf-8 -*-
"""Fix G: three defects found while running the documented CLI examples (phase F, E14-E17 and E24).

Each test was written first and run against the pre-fix source, where it failed
(see the fix-G evidence folder):

1. ``relay sync`` reported a version fork on a linear chain when the middle
   snapshot was signed by the author's retired key and was not cached. Sync now
   completes the signed ancestry the way ``receive`` does; when no verifiable
   copy exists it records "insufficient evidence" (plan section 2), never a fork.
   Real forks are still detected.
2. A delivery refused because ``cache/snapshots`` was a symlink stayed an
   integrity problem (exit 4) until a peer re-delivered it. It is now a local
   problem that needs the user, retried from the same cloud delivery once the
   directory is fixed (or requested again when the delivery is gone).
   Tampered payloads stay integrity failures and are not retried.
3. ``relay add --id .hidden`` registered an ID that ``publish`` refused. Project
   IDs are now validated wherever they enter the ledger; ledgers that already
   contain such an ID keep loading and ``doctor`` reports them.
"""
import contextlib
import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

from relaydesk import cli
from relaydesk import delivery_state as DS
from relaydesk import diagnostics as DX
from relaydesk import ledger as L
from relaydesk import relay_engine as E
from relaydesk import server
from relaydesk import storage
from relaydesk import sync_runtime as SR
from relaydesk import validation as V
from test_cli import CliBase
from test_key_rotation_redelivery import _RotationBase, _flip
from test_server import FakeStore


def _pair(a: str, b: str) -> Tuple[str, str]:
    return tuple(sorted((a, b)))  # type: ignore[return-value]


# ---------------------------------------------------------------- defect 1: false fork in sync


class TestSyncLineageEvidence(_RotationBase):
    def rotated_chain(self) -> Tuple[str, str, str]:
        """s1 cached on beta; s2 delivered to beta signed with alpha's old key; s3 (parent s2) with the new key."""
        s1 = self.publish()["snapshotId"]
        self.sync("beta")
        s2 = self.publish()["snapshotId"]
        self.old_fp = DS.norm_fp(E._identity_fingerprint(self.identity("alpha")))
        self.rotate("alpha")
        self.reconfirm("beta", "alpha")
        self.reconfirm("gamma", "alpha")
        s3 = self.publish()["snapshotId"]
        return s1, s2, s3

    def drop_all_copies_of(self, vid: str) -> None:
        (self.cloud_dir / "v3" / "snapshots" / (vid + ".json")).unlink()
        doomed = [marker.parent for marker in (self.cloud_dir / "v3" / "inbox").rglob("ready.json")
                  if (json.loads(marker.read_text(encoding="utf-8")).get("snapshot") or {}).get("versionId") == vid]
        self.assertTrue(doomed)
        for delivery_dir in doomed:
            shutil.rmtree(str(delivery_dir))

    def doctor(self, alias: str) -> Dict[str, Any]:
        with self.env(alias):
            return DX.run_doctor()

    def test_e24_rotated_middle_ancestor_is_verified_not_reported_as_fork(self) -> None:
        s1, s2, s3 = self.rotated_chain()
        res = self.sync("beta")
        self.assertEqual(res["conflictsDetected"], [])  # pre-fix: "fork" s1 vs s3
        self.assertEqual(res["counters"]["forksDetected"], 0)
        self.assertEqual(res.get("lineageUnproven") or [], [])
        self.assertEqual(res["status"], SR.SUCCESS, res["unresolved"])
        graph = self.state("beta")["versionGraph"]["demo"]
        self.assertEqual(graph["heads"], [s3])
        node = graph["nodes"][s2]
        # verified as historical evidence with alpha's previous key ...
        self.assertEqual((node["trust"], node["authorKeyFingerprint"]), ("historical", self.old_fp))
        self.assertEqual(node["parentVersionIds"], [s1])
        self.assertFalse(self.state("beta").get("lineagePending"))
        # ... which grants that key nothing: its delivery stays obsolete, the payload is not
        # accepted from it, and a fresh delivery under alpha's current key is requested instead.
        self.assertNotIn(s2, self.cached("beta"))
        old = [d for d, ent in DS.load(self.devices["beta"]["home"])["inbound"].items()
               if ent.get("snapshotId") == s2]
        self.assertEqual(len(old), 1)
        self.assertEqual(DS.load(self.devices["beta"]["home"])["inbound"][old[0]]["reason"], "sender_key_retired")
        self.assertIn(s2, self.have("beta").get("needs") or [])
        self.assertFalse([f for f in self.doctor("beta")["findings"]
                          if f["code"] in ("version_fork", "lineage_unproven")])

    def test_missing_ancestor_is_insufficient_evidence_then_resolved(self) -> None:
        s1, s2, s3 = self.rotated_chain()
        record = self.cloud_dir / "v3" / "snapshots" / (s2 + ".json")
        saved = record.read_bytes()
        self.drop_all_copies_of(s2)
        res = self.sync("beta")
        self.assertEqual(res["conflictsDetected"], [])  # pre-fix: reported as a fork
        self.assertEqual(res["counters"]["forksDetected"], 0)
        unproven = res["lineageUnproven"]
        self.assertEqual([_pair(u["localVersion"], u["incomingVersion"]) for u in unproven], [_pair(s1, s3)])
        self.assertEqual(sorted(unproven[0]["missingVersions"]), [s2])
        self.assertEqual(res["counters"]["lineageUnproven"], 1)
        pending = self.state("beta")["lineagePending"]["demo"]
        self.assertEqual(len(pending), 1)
        found = {f["code"]: f for f in self.doctor("beta")["findings"]}
        self.assertNotIn("version_fork", found)
        self.assertEqual((found["lineage_unproven"]["severity"], found["lineage_unproven"]["command"]),
                         ("warning", "relay sync demo"))
        self.assertIn(s2, found["lineage_unproven"]["reason"])
        self.assertIn("不是版本分叉", found["lineage_unproven"]["reason"])
        # A second sync does not turn missing evidence into a fork either.
        again = self.sync("beta")
        self.assertEqual(again["conflictsDetected"], [])
        # The signed record comes back (restored from a backup of the cloud folder): the next
        # sync re-reads it, verifies it with alpha's retired key and resolves the pair.
        record.write_bytes(saved)
        done = self.sync("beta")
        self.assertEqual(self.state("beta")["versionGraph"]["demo"]["nodes"][s2]["trust"], "historical")
        self.assertEqual(done["conflictsDetected"], [])
        self.assertFalse((self.state("beta").get("lineagePending") or {}).get("demo"))
        self.assertTrue(any(e["kind"] == "lineage_resolved" for e in done["recentEvents"]), done["recentEvents"])
        self.assertNotIn("lineage_unproven", {f["code"] for f in self.doctor("beta")["findings"]})
        self.assertEqual(self.state("beta")["versionGraph"]["demo"]["heads"], [s3])

    def test_real_fork_over_a_rotated_ancestor_is_still_detected(self) -> None:
        s1 = self.publish()["snapshotId"]
        self.sync("gamma")
        s2 = self.publish()["snapshotId"]  # parent s1, alpha's old key, never cached on gamma
        self.rotate("alpha")
        self.reconfirm("beta", "alpha")
        self.reconfirm("gamma", "alpha")
        s3a = self.publish()["snapshotId"]  # parent s2
        st_path = self.devices["alpha"]["home"] / "relay_state.json"
        st = json.loads(st_path.read_text(encoding="utf-8"))
        st["localCurrentVersion"]["demo"] = s1  # alpha continues from s1 (e.g. restored from a backup)
        st_path.write_text(json.dumps(st), encoding="utf-8")
        s3b = self.publish()["snapshotId"]  # parent s1: concurrent with s3a
        res = self.sync("gamma")
        pairs = {_pair(c["localVersion"], c["incomingVersion"]) for c in res["conflictsDetected"]}
        self.assertEqual(pairs, {_pair(s3a, s3b)})  # pre-fix: also the false pair s1/s3a
        self.assertEqual(res.get("lineageUnproven") or [], [])
        graph = self.state("gamma")["versionGraph"]["demo"]
        self.assertEqual(set(graph["heads"]), {s3a, s3b})
        self.assertEqual(graph["nodes"][s2]["trust"], "historical")
        codes = [f["code"] for f in self.doctor("gamma")["findings"]]
        self.assertNotIn("lineage_unproven", codes)

    def test_fork_against_unknown_ancestor_is_unproven_not_fork(self) -> None:
        s1 = self.publish()["snapshotId"]
        self.sync("gamma")
        s2 = self.publish()["snapshotId"]
        s3a = self.publish()["snapshotId"]  # s1 -> s2 -> s3a
        self.drop_all_copies_of(s2)
        res = self.sync("gamma")
        self.assertEqual(res["conflictsDetected"], [])
        self.assertEqual([_pair(u["localVersion"], u["incomingVersion"]) for u in res["lineageUnproven"]],
                         [_pair(s1, s3a)])


# ---------------------------------------------------------------- defect 2: refused symlink delivery stuck


class TestLocalSymlinkRefusal(_RotationBase):
    def link_cache(self, alias: str) -> Tuple[Path, Path]:
        snaps = self.devices[alias]["home"] / "cache" / "snapshots"
        snaps.mkdir(parents=True, exist_ok=True)
        outside = self.root / ("external_cache_%s" % alias)
        shutil.move(str(snaps), str(outside))
        os.symlink(str(outside), str(snaps))
        return snaps, outside

    def unlink_cache(self, snaps: Path, outside: Path) -> None:
        os.unlink(str(snaps))
        shutil.move(str(outside), str(snaps))

    def gamma_inbox(self) -> Dict[str, str]:
        return self.cloud_digests("v3/inbox/%s" % self.did["gamma"])

    def delivery_of(self, alias: str, snap: str) -> str:
        found = self.deliveries_for(alias, snap)
        self.assertEqual(len(found), 1, found)
        return found[0]

    def doctor(self, alias: str) -> Dict[str, Any]:
        with self.env(alias):
            return DX.run_doctor()

    @unittest.skipIf(os.name == "nt", "POSIX symlinks")
    def test_symlink_refusal_needs_user_and_retries_same_delivery_after_fix(self) -> None:
        s1 = self.publish()["snapshotId"]
        self.sync("gamma")
        snaps, outside = self.link_cache("gamma")
        s2 = self.publish()["snapshotId"]
        deliv = self.delivery_of("gamma", s2)
        inbox_before = self.gamma_inbox()
        have_before = self.have("gamma")
        r1 = self.sync("gamma")
        self.assertEqual(r1["status"], SR.NEEDS_ACTION_STATUS)
        self.assertTrue(r1["unresolved"])
        for prob in r1["unresolved"]:  # pre-fix: integrity / symlink_in_path
            self.assertEqual((prob["errorClass"], prob["code"]), (SR.NEEDS_ACTION, "local_dir_symlink"), prob)
            self.assertIn('rm "%s"' % snaps, prob.get("action", ""))
        self.assertEqual(sorted(os.listdir(str(outside))), [s1])  # nothing written through the link
        book = DS.load(self.devices["gamma"]["home"])
        self.assertEqual(book["inbound"][deliv]["state"], "blocked_local")
        self.assertFalse([e for e in book["inbound"].values() if e.get("state") in DS.NEEDS_STATES])
        self.assertEqual(self.have("gamma"), have_before)  # no have built from an unreadable cache
        dx = {f["code"]: f for f in self.doctor("gamma")["findings"]}
        self.assertEqual(dx["local_dir_symlink"]["severity"], "error")
        self.assertEqual(dx["local_dir_symlink"]["command"],
                         'rm "%s" && mv "%s" "%s"' % (snaps, os.path.realpath(str(outside)), snaps))
        self.assertNotIn("sync_problem", dx)  # one finding, from the fresh local check

        self.unlink_cache(snaps, outside)
        dx = {f["code"]: f for f in self.doctor("gamma")["findings"]}
        self.assertNotIn("local_dir_symlink", dx)
        self.assertEqual((dx["local_retry_pending"]["severity"], dx["local_retry_pending"]["command"]),
                         ("info", "relay sync"))
        r2 = self.sync("gamma")  # pre-fix: still needs_action until a peer re-delivers
        self.assertEqual(r2["status"], SR.SUCCESS, r2["unresolved"])
        self.assertEqual([s["deliveryId"] for s in r2["syncedSnapshots"]], [deliv])
        self.assertIn(s2, self.cached("gamma"))
        self.assert_cached_matches_author("gamma", s2)
        self.assertEqual(self.gamma_inbox(), inbox_before)  # the same immutable delivery, no new generation
        self.assertNotIn(s2, self.have("gamma").get("needs") or [])
        ent = DS.load(self.devices["gamma"]["home"])["inbound"][deliv]
        self.assertEqual(ent["state"], "cached")
        self.assertNotIn("problem", ent)
        self.assertNotIn("local_retry_pending", {f["code"] for f in self.doctor("gamma")["findings"]})

    @unittest.skipIf(os.name == "nt", "POSIX symlinks")
    def test_watch_session_retries_once_the_directory_is_fixed(self) -> None:
        self.publish()
        self.sync("gamma")
        snaps, outside = self.link_cache("gamma")
        s2 = self.publish()["snapshotId"]
        clock = SR.FakeClock()
        with self.env("gamma"):
            session = E.SyncSession(mode="watch", clock=clock)
            first = session.step()
            self.assertFalse(first.aborted)
            self.assertEqual({p["code"] for p in session.log.snapshot()["unresolved"]}, {"local_dir_symlink"})
            self.unlink_cache(snaps, outside)
            clock.advance(5.0)
            session.step()
            summary = session.finish()
        self.assertEqual(summary["status"], SR.SUCCESS, summary["unresolved"])
        self.assertIn(s2, self.cached("gamma"))

    @unittest.skipIf(os.name == "nt", "POSIX symlinks")
    def test_blocked_delivery_gone_from_inbox_is_requested_again(self) -> None:
        self.publish()
        self.sync("gamma")
        snaps, outside = self.link_cache("gamma")
        s2 = self.publish()["snapshotId"]
        deliv = self.delivery_of("gamma", s2)
        self.sync("gamma")
        shutil.rmtree(str(self.cloud_dir / "v3" / "inbox" / self.did["gamma"] / deliv))
        self.unlink_cache(snaps, outside)
        r2 = self.sync("gamma")
        self.assertEqual(r2["status"], SR.SUCCESS, r2["unresolved"])
        self.assertEqual(DS.load(self.devices["gamma"]["home"])["inbound"][deliv]["state"], "repair_pending")
        self.assertIn(s2, self.have("gamma").get("needs") or [])
        self.sync("alpha")  # answers the request with a new generation
        self.sync("gamma")
        self.assertIn(s2, self.cached("gamma"))
        self.assert_cached_matches_author("gamma", s2)

    def test_tampered_payload_stays_integrity_and_is_not_downloaded_again(self) -> None:
        s1 = self.publish()["snapshotId"]
        deliv = self.delivery_of("gamma", s1)
        _flip(self.cloud_dir / "v3" / "inbox" / self.did["gamma"] / deliv / "package.enc")
        r1 = self.sync("gamma")
        probs = [p for p in r1["unresolved"] if p.get("deliveryId") == deliv]
        self.assertEqual([p["errorClass"] for p in probs], [SR.INTEGRITY])
        self.assertEqual(DS.load(self.devices["gamma"]["home"])["inbound"][deliv]["state"], "quarantined")
        downloads: List[str] = []
        original = storage.LocalDirProvider.download

        def counting(provider, key, local_path):
            downloads.append(key)
            return original(provider, key, local_path)

        with mock.patch.object(storage.LocalDirProvider, "download", counting):
            r2 = self.sync("gamma")
        self.assertFalse([k for k in downloads if k.endswith("%s/package.enc" % deliv)], downloads)
        self.assertEqual([p["errorClass"] for p in r2["unresolved"] if p.get("deliveryId") == deliv],
                         [SR.INTEGRITY])
        self.assertNotIn(s1, self.cached("gamma"))

    def test_local_symlink_error_classifies_as_needs_action(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            (home / "cache").mkdir(parents=True)
            outside = Path(td) / "outside"
            outside.mkdir()
            if os.name == "nt":
                self.skipTest("POSIX symlinks")
            os.symlink(str(outside), str(home / "cache" / "snapshots"))
            with self.assertRaises(V.PathEscapeError) as caught:  # still a PathEscapeError for old handlers
                E._snapshot_cache_dir(home, "snap_20261119_abcdefgh")
            classified = SR.classify_error(caught.exception, "inbound")
            self.assertEqual((classified.error_class, classified.code), (SR.NEEDS_ACTION, "local_dir_symlink"))
            self.assertTrue(classified.action)


# ---------------------------------------------------------------- defect 3: invalid project IDs enter the ledger


class TestProjectIdAtRegistration(CliBase):
    BAD = (".hidden", "-x", "x.", "a..b", "con", " lead")

    def test_add_refuses_ids_that_publish_would_refuse(self) -> None:
        for bad in self.BAD:
            code, _, err = self.run_cli("add", "隐藏项目", "--path", self.proj, "--id=" + bad)
            self.assertEqual(code, 1, (bad, err))  # pre-fix: .hidden registered
            self.assertIn("projectId", err)
        self.assertEqual(self.led()["projects"], [])

    def test_add_derives_a_valid_default_id(self) -> None:
        for name in (".hidden", "trailing.", "a..b"):
            self.ok("add", name, "--path", self.proj)
        ids = [p["id"] for p in self.led()["projects"]]
        for pid in ids:
            V.validate_project_id(pid)
        self.assertEqual(ids[:3], ["hidden", "trailing", "a.b"])

    def test_import_refuses_new_invalid_ids_but_keeps_existing_ones(self) -> None:
        backup = self.dir / "backup.json"
        self.ok("add", "Good", "--path", self.proj, "--id", "good")
        exported = self.led()
        exported["projects"].append(dict(exported["projects"][0], id=".hidden", name="H"))
        backup.write_text(json.dumps(exported), encoding="utf-8")
        before = self.led()
        for extra in ((), ("--replace",)):
            code, _, err = self.run_cli("import", backup, *extra)
            self.assertEqual(code, 1, err)
            self.assertIn(".hidden", err)
            self.assertEqual(self.led()["projects"], before["projects"])
        # A ledger that already holds the old ID (registered by an older version) can still restore its backup.
        self.update(lambda l: l["projects"].append(L.norm_project({"id": ".hidden", "name": "H"}, l["config"])))
        self.ok("import", backup)

    def test_existing_invalid_id_keeps_loading_and_doctor_reports_it(self) -> None:
        self.ok("add", "Good", "--path", self.proj, "--id", "good")
        self.update(lambda l: l["projects"].append(L.norm_project(
            {"id": ".hidden", "name": "隐藏项目", "device": "a", "paths": {"a": str(self.proj)}}, l["config"])))
        self.assertIn(".hidden", self.ok("status"))
        self.ok("scan", "--all")
        code, out, _ = self.run_cli("doctor", "--json")
        rep = json.loads(out)
        bad = [f for f in rep["findings"] if f["code"] == "project_id_invalid"]
        self.assertEqual(len(bad), 1, rep["findings"])
        self.assertEqual((bad[0]["severity"], bad[0]["projectId"]), ("warning", ".hidden"))
        self.assertEqual(bad[0]["command"], 'relay add "隐藏项目" --path "%s" --id <新项目ID>' % self.proj)
        self.assertGreaterEqual(code, 3)
        self.assertEqual([pv["projectId"] for pv in rep["projects"]], ["good", ".hidden"])
        net = [pv for pv in rep["projects"] if pv["projectId"] == ".hidden"][0]
        self.assertEqual([a["code"] for a in net["needsAction"]], ["project_id_invalid"])
        code, out, _ = self.run_cli("doctor")
        self.assertIn("project_id_invalid", out)


class TestProjectIdServerApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.html = os.path.join(cls.tmp.name, "page.html")
        with open(cls.html, "w", encoding="utf-8") as f:
            f.write("<div id=app></div>")
        cls.store = FakeStore()
        cls.srv = server.make_server(cls.html, cls.store, 0)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def setUp(self) -> None:
        with self.store.lock:
            self.store.ledger = {"config": {"devices": []}, "projects": [{"id": "p1", "name": "one"}]}
            self.store.n = 1

    def put(self, path: str, body: Any) -> Tuple[int, Any]:
        data = json.dumps(body).encode("utf-8")
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request("PUT", path, body=data, headers={"X-Relay-Desk": "1", "Content-Type": "application/json",
                                                       "Host": "127.0.0.1:%d" % self.port})
            r = c.getresponse()
            raw = r.read()
            return r.status, (json.loads(raw.decode("utf-8")) if raw else None)
        finally:
            c.close()

    def test_new_invalid_project_id_is_rejected(self) -> None:
        for pid in (".hidden", "-x", "x.", "a..b", "con"):
            st, body = self.put("/api/projects/" + pid, {"name": "x"})
            self.assertEqual(st, 400, pid)  # pre-fix: 200 for .hidden
            self.assertIn("projectId", body["error"])
        st, _ = self.put("/api/ledger", {"config": {"devices": []}, "projects": [{"id": "p1"}, {"id": ".hidden"}]})
        self.assertEqual(st, 400)
        self.assertEqual(self.store.n, 1)
        self.assertEqual([p["id"] for p in self.store.ledger["projects"]], ["p1"])

    def test_existing_invalid_project_id_can_still_be_edited(self) -> None:
        with self.store.lock:
            self.store.ledger["projects"].append({"id": ".hidden", "name": "old"})
        st, _ = self.put("/api/projects/.hidden", {"name": "renamed"})
        self.assertEqual(st, 200)
        st, _ = self.put("/api/ledger", {"config": {"devices": []}, "projects": [{"id": ".hidden", "name": "kept"}]})
        self.assertEqual(st, 200)
        self.assertEqual(self.store.ledger["projects"], [{"id": ".hidden", "name": "kept"}])


if __name__ == "__main__":
    unittest.main()
