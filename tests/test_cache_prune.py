# -*- coding: utf-8 -*-
"""Phase D: local cache statistics, safe pruning and read-only diagnosis.

``TestPhaseDPruneRepro`` was written first and run against the post-Phase-C
source (digest 657d9657...), where it failed: a snapshot payload removed on
purpose (recorded in ``deliveries.json prunedSnapshots``) was downloaded and
cached again by the very next ``relay sync``, because the inbound "cache repair"
path did not look at ``prunedSnapshots`` (only the lost-cache ``needs`` path did).

Everything runs on temporary homes and a local-folder cloud (``MultiDeviceTestBase``).
"""
import builtins
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from unittest import mock

from relaydesk import cache_prune as CP
from relaydesk import crypto
from relaydesk import delivery_state as DS
from relaydesk import diagnostics as DX
from relaydesk import ledger as L
from relaydesk import relay_engine as E
from relaydesk import storage
from relaydesk import sync_runtime as SR
from relaydesk import workspace_tx as W
from test_key_rotation_redelivery import KILLED, REPO_ROOT, _RotationBase, _flip, _sha


def tree_state(root: Path, skip: Set[str] = frozenset()) -> Dict[str, Any]:
    """Every path under ``root`` with its type, bytes digest and mtime: detects any write, add or delete."""
    out: Dict[str, Any] = {}
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            if rel in skip or any(rel.startswith(s + os.sep) for s in skip):
                continue
            st = os.lstat(str(p))
            if os.path.islink(str(p)):
                out[rel] = ("link", os.readlink(str(p)))
            elif p.is_dir():
                out[rel] = ("dir",)
            else:
                out[rel] = ("file", _sha(p), st.st_mtime_ns, st.st_size)
    return out


class _PruneBase(_RotationBase):
    def home(self, alias: str) -> Path:
        return self.devices[alias]["home"]

    def doctor(self, alias: str, project: Optional[str] = None) -> Dict[str, Any]:
        with self.env(alias):
            return DX.run_doctor(project)

    def status(self, alias: str, project: Optional[str] = None) -> Dict[str, Any]:
        with self.env(alias):
            return CP.cache_status(project)

    def dry(self, alias: str, project: Optional[str] = None) -> Dict[str, Any]:
        with self.env(alias):
            return CP.prune(project)

    def apply(self, alias: str, project: Optional[str] = None, **kw) -> Dict[str, Any]:
        with self.env(alias):
            return CP.prune(project, apply=True, **kw)

    def three_versions(self) -> List[str]:
        """alpha publishes S1 -> S2 -> S3; beta and gamma cache all three; alpha learns both confirmed them."""
        snaps = [self.publish()["snapshotId"] for _ in range(3)]
        self.sync("beta")
        self.sync("gamma")
        self.sync("alpha")
        return snaps

    def cached(self, alias: str) -> List[str]:  # tombstones of an interrupted prune are not cache entries
        return [n for n in _RotationBase.cached(self, alias) if not n.startswith(".")]

    def deletable_payloads(self, plan: Dict[str, Any]) -> List[str]:
        return sorted(x["snapshotId"] for x in plan["deletable"]["payloads"])

    def kept_reasons(self, plan: Dict[str, Any]) -> Dict[str, List[str]]:
        return {x["snapshotId"]: x.get("reasons") or [x["reason"]] for x in plan["kept"]["payloads"]}

    def book(self, alias: str) -> Dict[str, Any]:
        return json.loads((self.home(alias) / "deliveries.json").read_text(encoding="utf-8"))


class TestPhaseDPruneRepro(_RotationBase):
    def test_sync_does_not_download_a_pruned_snapshot_again(self) -> None:
        s1 = self.publish()["snapshotId"]
        s2 = self.publish()["snapshotId"]
        self.sync("beta")
        self.assertIn(s1, self.cached("beta"))
        self.assertIn(s2, self.cached("beta"))
        home = self.devices["beta"]["home"]
        # What a prune records for s1 (neither the current version nor a head on beta).
        shutil.rmtree(str(home / "cache" / "snapshots" / s1))
        DS.mark_pruned(home, [s1])
        res = self.sync("beta")
        self.assertNotIn(s1, [e["snapshotId"] for e in res["syncedSnapshots"]])  # pre-fix: "repaired"
        self.assertNotIn(s1, self.cached("beta"))
        self.assertEqual(res["counters"]["snapshotsRepaired"], 0)

    def test_a_stale_prune_mark_never_blocks_repair_of_the_current_version(self) -> None:
        s1 = self.publish()["snapshotId"]
        home = self.devices["alpha"]["home"]
        DS.mark_pruned(home, [s1])  # a stale mark (e.g. a prune interrupted before it re-checked)
        shutil.rmtree(str(home / "cache" / "snapshots" / s1))
        self.sync("alpha")
        needs = self.have("alpha")["needs"]
        self.assertIn(s1, needs)  # alpha's own current version is still requested back
        self.sync("beta")
        self.sync("alpha")
        self.assert_cached_matches_author("alpha", s1)


class TestReadOnly(_PruneBase):
    def test_doctor_status_and_dry_run_write_nothing(self) -> None:
        self.three_versions()
        sent = self.send("alpha", "beta")
        # add some unclear state too: a corrupt cache entry and an interrupted-prune tombstone
        (self.home("alpha") / "cache" / "snapshots" / ".pruning-x.1234").mkdir(parents=True)
        before = {a: tree_state(self.home(a)) for a in ("alpha", "beta")}
        cloud_before = tree_state(self.cloud_dir)
        led_before = {a: self.devices[a]["led"].read_bytes() for a in ("alpha", "beta")}
        for alias in ("alpha", "beta"):
            self.doctor(alias)
            if alias == "alpha":
                self.doctor(alias, "demo")
            self.status(alias)
            self.dry(alias)
        for alias, cmd in (("alpha", ["doctor", "--json"]), ("alpha", ["cache", "status", "--json"]),
                           ("alpha", ["cache", "prune", "--json"]), ("alpha", ["cache", "prune", "--dry-run"]),
                           ("beta", ["doctor"])):
            with self.env(alias):
                from test_reissue import _run_cli
                _run_cli(cmd)
        for alias in ("alpha", "beta"):
            self.assertEqual(tree_state(self.home(alias)), before[alias], alias)
            self.assertEqual(self.devices[alias]["led"].read_bytes(), led_before[alias])
        self.assertEqual(tree_state(self.cloud_dir), cloud_before)
        self.assertFalse(self.state("alpha")["sentRelays"][sent["relayId"]]["acked"])


class TestRetentionAndPrune(_PruneBase):
    def test_prune_keeps_current_version_and_frees_confirmed_history(self) -> None:
        s1, s2, s3 = self.three_versions()
        st = self.status("alpha")
        self.assertEqual(st["payloads"]["count"], 3)
        self.assertEqual(st["checkpoints"]["count"], 6)  # 3 versions x (beta, gamma)
        plan = self.dry("alpha")
        self.assertEqual(self.deletable_payloads(plan), sorted([s1, s2]))
        self.assertEqual(set(self.kept_reasons(plan)[s3]), {"localCurrentVersion", "versionGraphHead"})
        self.assertEqual({x["reason"] for x in plan["deletable"]["checkpoints"]}, {"peerVerified"})
        self.assertEqual(plan["estimatedBytesFreed"], st["prunable"]["bytes"])
        res = self.apply("alpha", expect_plan=plan["planDigest"])
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["bytesFreed"], plan["estimatedBytesFreed"])
        self.assertEqual(self.cached("alpha"), [s3])
        self.assertEqual(list((self.home("alpha") / "sync_publications").iterdir()), [])
        for vid in (s1, s2):  # signed metadata survives as lineage, byte-for-byte the same record
            lineage = json.loads((self.home("alpha") / "cache" / "lineage" / ("%s.json" % vid)).read_text("utf-8"))
            self.assertEqual(crypto.snapshot_record_digest(lineage),
                             self.state("alpha")["versionGraph"]["demo"]["nodes"][vid]["recordSha256"])
        self.assertEqual(sorted(self.book("alpha")["prunedSnapshots"]), sorted([s1, s2]))
        again = self.dry("alpha")
        self.assertEqual(again["counts"]["deletablePayloads"] + again["counts"]["deletableCheckpoints"], 0)
        # Sync afterwards: nothing is re-downloaded or re-requested, nobody re-delivers.
        inbox_before = self.cloud_digests()
        for alias in ("alpha", "beta", "gamma"):
            self.assertEqual(self.sync(alias)["status"], SR.SUCCESS)
        self.assertEqual(self.cached("alpha"), [s3])
        self.assertEqual(self.have("alpha")["needs"], [])
        self.assertEqual(self.cloud_digests(), inbox_before)

    def test_forks_inflight_handoffs_needs_and_transactions_are_retained(self) -> None:
        s1, s2, s3 = self.three_versions()
        # 1) unresolved fork recorded in the ledger keeps both sides
        def add_fork(led: Dict[str, Any]) -> None:
            for p in led["projects"]:
                if p["id"] == "demo":
                    p.setdefault("conflicts", []).append({"id": "cf_1", "kind": "version_fork", "pairKey": "x",
                                                          "localVersion": s1, "incomingVersion": s3,
                                                          "resolved": False})
        L.update(self.devices["alpha"]["led"], add_fork)
        plan = self.dry("alpha")
        self.assertIn("unresolvedFork", self.kept_reasons(plan)[s1])
        self.assertEqual(self.deletable_payloads(plan), [s2])
        fork = [f for f in self.doctor("alpha", "demo")["findings"] if f["code"] == "version_fork"][0]
        self.assertEqual((fork["severity"], fork["command"]), ("warning", "relay status demo"))
        self.assertEqual(self.status("alpha", "demo")["payloads"]["count"], 3)
        # 2) a second graph head (fork head) is kept
        state_path = self.home("beta") / "relay_state.json"
        rs = json.loads(state_path.read_text("utf-8"))
        rs["versionGraph"]["demo"]["heads"] = [s3, s2]
        state_path.write_text(json.dumps(rs), encoding="utf-8")
        self.assertIn("versionGraphHead", self.kept_reasons(self.dry("beta"))[s2])
        # 3) a fresh peer asking for a snapshot keeps it; 4) an in-flight handoff keeps its snapshot
        shutil.rmtree(str(self.home("gamma") / "cache" / "snapshots" / s2))
        for d in self.deliveries_for("gamma", s2):
            shutil.rmtree(str(self.cloud_dir / "v3" / "inbox" / self.did["gamma"] / d))
        state_path = self.home("gamma") / "relay_state.json"
        rs = json.loads(state_path.read_text("utf-8"))
        rs["localCurrentVersion"]["demo"] = s2  # make it a snapshot gamma must have: lost -> needs
        state_path.write_text(json.dumps(rs), encoding="utf-8")
        self.sync("gamma")
        self.assertIn(s2, self.have("gamma")["needs"])
        with mock.patch.object(E._Outbound, "deliver", return_value={"action": "waiting"}):
            self.sync("alpha")  # alpha records gamma's fresh needs but does not re-deliver yet
        plan = self.dry("alpha")
        self.assertIn("peerNeeds", self.kept_reasons(plan)[s2])
        sent = self.send("alpha", "beta")
        plan = self.dry("alpha")
        self.assertIn("unackedHandoff", self.kept_reasons(plan)[sent["snapshotId"]])
        self.apply("alpha")
        self.assertIn(s1, self.cached("alpha"))
        self.assertIn(s2, self.cached("alpha"))
        self.assertIn(sent["snapshotId"], self.cached("alpha"))

    def test_unfinished_receive_transaction_keeps_snapshot_and_rollback_target(self) -> None:
        first = self.send("alpha", "beta")
        self.receive("beta", first["relayId"])
        back = self.send("beta", "alpha")
        self.receive("alpha", back["relayId"])
        third = self.send("alpha", "beta")
        dest = self.project("beta")["paths"]["beta"]
        with self.env("beta", {"RELAY_DESK_FAULT_POINT": "tx_after_workspace_commit"}):
            with self.assertRaises(RuntimeError):
                E.receive_relay(third["relayId"])
        tx_info = W.inspect_transaction(self.home("beta"), third["relayId"])
        self.assertEqual(tx_info["state"], "workspace_committed")
        plan = self.dry("beta")
        reasons = self.kept_reasons(plan)
        self.assertIn("recoveryTransaction", reasons[third["snapshotId"]])
        tx_before = tree_state(self.home("beta") / "transactions")
        self.apply("beta")
        self.assertEqual(tree_state(self.home("beta") / "transactions"), tx_before)
        self.assertIn(third["snapshotId"], self.cached("beta"))
        doc = self.doctor("beta")
        self.assertIn(("receive_interrupted_after_commit", "relay receive %s" % third["relayId"]),
                      [(f["code"], f["command"]) for f in doc["findings"]])
        # recovery still works after the prune
        with self.env("beta"):
            res = E.receive_relay(third["relayId"])
        self.assertEqual(res["targetPath"], dest)
        self.sync("alpha")
        self.assertTrue(self.state("alpha")["sentRelays"][third["relayId"]]["acked"])

    def test_corrupt_and_unclear_entries_are_kept_and_reported(self) -> None:
        s1, s2, s3 = self.three_versions()
        _flip(self.cache_payload("alpha", s1))
        (self.home("alpha") / "cache" / "snapshots" / "not a version").mkdir()
        orphan = self.home("alpha") / "sync_publications" / ("a" * 64)
        orphan.mkdir()
        (orphan / "ready.json").write_text("{", encoding="utf-8")
        plan = self.dry("alpha")
        self.assertEqual(self.deletable_payloads(plan), [s2])
        unclear = {(x["kind"], x.get("snapshotId") or x.get("name"), x["reason"]) for x in plan["unclear"]}
        self.assertIn(("payload", s1, "corrupt"), unclear)
        self.assertIn(("payload", "not a version", "foreign"), unclear)
        self.assertIn(("checkpoint", "a" * 64, "corrupt"), unclear)
        self.apply("alpha")
        self.assertIn(s1, self.cached("alpha"))
        self.assertTrue((orphan / "ready.json").is_file())
        codes = [f["code"] for f in self.doctor("alpha")["findings"]]
        self.assertIn("cache_entry_unclear", codes)

    def test_damaged_state_blocks_prune_and_writes_nothing(self) -> None:
        self.three_versions()
        (self.home("alpha") / "deliveries.json").write_text("{broken", encoding="utf-8")
        before = tree_state(self.home("alpha"))
        res = self.apply("alpha")
        self.assertEqual(res["status"], "blocked")
        self.assertEqual([b["code"] for b in res["blocked"]], ["delivery_state_corrupted"])
        self.assertEqual(res["deleted"], {"payloads": [], "checkpoints": [], "leftovers": []})
        after = tree_state(self.home("alpha"), skip={"workspace-operation.lock"})
        self.assertEqual(after, {k: v for k, v in before.items() if k != "workspace-operation.lock"})
        doc = self.doctor("alpha")
        f = [x for x in doc["findings"] if x["code"] == "delivery_state_corrupted"][0]
        self.assertEqual(f["command"], 'mv "%s" "%s.corrupt" && relay sync' % (
            self.home("alpha") / "deliveries.json", self.home("alpha") / "deliveries.json"))

    def test_fake_credentials_are_never_read_copied_or_deleted(self) -> None:
        s1, s2, s3 = self.three_versions()
        home, uhome = self.home("alpha"), self.devices["alpha"]["uhome"]
        secrets = {
            home / "rclone.conf": b"[remote]\ntype = s3\nsecret_access_key = FAKE-SECRET-1\n",
            home / ".env": b"API_TOKEN=FAKE-SECRET-2\n",
            home / "cache" / "credentials.json": b'{"token": "FAKE-SECRET-3"}',
            uhome / ".config" / "rclone" / "rclone.conf": b"[x]\ntoken = FAKE-SECRET-4\n",
            # inside an otherwise deletable payload and checkpoint: the whole entry becomes "unexpected"
            home / "cache" / "snapshots" / s1 / ".env": b"FAKE-SECRET-5\n",
        }
        plan0 = self.dry("alpha")
        ck = plan0["deletable"]["checkpoints"][0]["name"]
        secrets[home / "sync_publications" / ck / "token.json"] = b'{"t": "FAKE-SECRET-6"}'
        for p, data in secrets.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        before = {p: (p.read_bytes(), os.lstat(str(p)).st_mtime_ns) for p in secrets}
        opened: List[str] = []
        real_open, real_io_open = builtins.open, io.open

        def rec_open(file, *a, **kw):
            opened.append(os.path.abspath(str(file)) if isinstance(file, (str, os.PathLike)) else repr(file))
            return real_open(file, *a, **kw)

        def rec_io_open(file, *a, **kw):
            opened.append(os.path.abspath(str(file)) if isinstance(file, (str, os.PathLike)) else repr(file))
            return real_io_open(file, *a, **kw)

        with mock.patch("builtins.open", rec_open), mock.patch("io.open", rec_io_open):
            plan = self.dry("alpha")
            res = self.apply("alpha")
            self.status("alpha")
            self.doctor("alpha")
        self.assertEqual(res["status"], "success")
        for p, (data, mtime) in before.items():
            self.assertEqual(p.read_bytes(), data, p)
            self.assertEqual(os.lstat(str(p)).st_mtime_ns, mtime, p)
            self.assertNotIn(os.path.abspath(str(p)), opened, "prune opened %s" % p)
        kept = self.kept_reasons(plan)
        self.assertEqual(kept[s1], ["unexpected_files"])
        self.assertIn(ck, [x["name"] for x in plan["kept"]["checkpoints"] if x["reason"] == "unexpected_files"])
        self.assertEqual(self.deletable_payloads(plan), [s2])
        # no secret bytes were copied anywhere under the home
        for path in home.rglob("*"):
            if path.is_file() and path not in secrets:
                self.assertNotIn(b"FAKE-SECRET", path.read_bytes(), path)


class TestStaleDryRun(_PruneBase):
    def test_apply_recomputes_and_keeps_what_became_referenced(self) -> None:
        s1, s2, s3 = self.three_versions()
        plan = self.dry("alpha")
        self.assertEqual(self.deletable_payloads(plan), sorted([s1, s2]))

        def add_fork(led: Dict[str, Any]) -> None:
            for p in led["projects"]:
                if p["id"] == "demo":
                    p.setdefault("conflicts", []).append({"id": "cf_1", "kind": "version_fork", "pairKey": "x",
                                                          "localVersion": s3, "incomingVersion": s1,
                                                          "resolved": False})
        L.update(self.devices["alpha"]["led"], add_fork)  # state changed after the preview
        res = self.apply("alpha")  # no stale list is replayed: s1 is now kept
        self.assertEqual([x["snapshotId"] for x in res["deleted"]["payloads"]], [s2])
        self.assertIn(s1, self.cached("alpha"))
        self.assertIn("unresolvedFork", self.kept_reasons(res)[s1])

    def test_expect_plan_refuses_a_changed_plan_and_deletes_nothing(self) -> None:
        s1, s2, s3 = self.three_versions()
        plan = self.dry("alpha")
        sent = self.send("alpha", "beta")  # a new version and an in-flight handoff since the preview
        before = tree_state(self.home("alpha"), skip={"workspace-operation.lock"})
        with self.assertRaises(CP.PrunePlanChangedError) as cm:
            self.apply("alpha", expect_plan=plan["planDigest"])
        self.assertNotEqual(cm.exception.plan["planDigest"], plan["planDigest"])
        self.assertEqual(tree_state(self.home("alpha"), skip={"workspace-operation.lock"}), before)
        self.assertIn(sent["snapshotId"], self.cached("alpha"))
        # the CLI reports it with a machine-readable code and exit 3
        from test_reissue import _run_cli
        with self.env("alpha"):
            out = _run_cli(["cache", "prune", "--apply", "--expect-plan", plan["planDigest"], "--json"])
        self.assertEqual(out["rc"], 3)
        self.assertEqual(json.loads(out["out"])["code"], "prune_plan_changed")

    def test_apply_waits_for_the_local_lock(self) -> None:
        self.three_versions()
        before = tree_state(self.home("alpha"), skip={"workspace-operation.lock"})
        with E._workspace_operation(self.home("alpha")):
            with self.assertRaises(E.RelayError) as cm:
                self.apply("alpha")
        self.assertEqual(cm.exception.code, "busy")
        self.assertEqual(tree_state(self.home("alpha"), skip={"workspace-operation.lock"}), before)


class TestByteLevelProtection(_PruneBase):
    def test_cloud_objects_and_anti_replay_records_are_unchanged(self) -> None:
        s1, s2, s3 = self.three_versions()
        sent = self.send("alpha", "gamma")
        self.receive("gamma", sent["relayId"])
        self.sync("alpha")
        for alias in ("alpha", "beta", "gamma"):
            self.sync(alias)
        cloud_before = tree_state(self.cloud_dir)
        homes = {}
        for alias in ("alpha", "beta", "gamma"):
            h = self.home(alias)
            homes[alias] = {
                "relay_state": (h / "relay_state.json").read_bytes(),
                "identity": (h / "identity.json").read_bytes(),
                "cloud": (h / "cloud.json").read_bytes(),
                "book": self.book(alias),
                "transactions": tree_state(h / "transactions"),
                "archives": tree_state(h / "archives"),
                "lineage": tree_state(h / "cache" / "lineage"),
            }
        total = 0
        for alias in ("alpha", "beta", "gamma"):
            res = self.apply(alias)
            self.assertEqual(res["status"], "success", alias)
            total += len(res["deleted"]["payloads"]) + len(res["deleted"]["checkpoints"])
        self.assertGreater(total, 0)
        self.assertEqual(tree_state(self.cloud_dir), cloud_before)  # prune never contacts the cloud
        for alias in ("alpha", "beta", "gamma"):
            h, was = self.home(alias), homes[alias]
            self.assertEqual((h / "relay_state.json").read_bytes(), was["relay_state"])  # processedDeliveries etc.
            self.assertEqual((h / "identity.json").read_bytes(), was["identity"])
            self.assertEqual((h / "cloud.json").read_bytes(), was["cloud"])
            self.assertEqual(tree_state(h / "transactions"), was["transactions"])
            self.assertEqual(tree_state(h / "archives"), was["archives"])
            for rel, val in was["lineage"].items():  # lineage only ever grows
                self.assertEqual(tree_state(h / "cache" / "lineage")[rel], val)
            book = self.book(alias)
            for key in ("outbound", "inbound", "peerHave", "self", "migration"):
                self.assertEqual(book[key], was["book"][key], (alias, key))
            self.assertTrue(set(was["book"].get("prunedSnapshots") or {}) <= set(book["prunedSnapshots"]))


class TestFlowsAfterPrune(_PruneBase):
    def test_resend_reissue_and_maintenance_after_prune(self) -> None:
        s1, s2, s3 = self.three_versions()
        sent = self.send("alpha", "beta")
        self.apply("alpha")
        self.assertIn(sent["snapshotId"], self.cached("alpha"))
        # plain retry of the in-flight handoff still works
        again = self.send("alpha", "beta")
        self.assertEqual(again["relayId"], sent["relayId"])
        # the target rotates: the explicit re-issue needs the payload prune kept
        self.rotate("beta")
        self.reconfirm("alpha", "beta")
        with self.env("alpha"):
            res = E.send_project("demo", to_alias="beta", reissue=True)
        got = self.receive("beta", res["relayId"])
        self.assertEqual(got["snapshotId"], sent["snapshotId"])
        self.sync("alpha")
        self.assertTrue(self.state("alpha")["sentRelays"][res["relayId"]]["acked"])

    def test_cache_redelivery_and_checkpoint_restore_after_prune(self) -> None:
        snap = self.publish()["snapshotId"]
        self.sync("gamma")  # beta has not confirmed yet: its delivery checkpoint is kept by prune
        self.sync("alpha")
        plan = self.apply("alpha")
        kept = [x for x in plan["kept"]["checkpoints"] if x["toDeviceId"] == self.did["beta"]]
        self.assertEqual([x["reason"] for x in kept], ["awaitingPeerVerification"])
        self.assertIn(snap, self.cached("alpha"))  # current version
        # the published objects vanish from the cloud: maintenance restores them from the kept checkpoint
        deliv = self.deliveries_for("beta", snap)[0]
        ready_key = "v3/inbox/%s/%s/ready.json" % (self.did["beta"], deliv)
        ready_bytes = (self.cloud_dir / ready_key).read_bytes()
        shutil.rmtree(str(self.cloud_dir / "v3" / "inbox" / self.did["beta"] / deliv))
        with mock.patch.object(DS, "wall_now", return_value=DS.wall_now() + __import__("datetime").timedelta(hours=1)), \
                mock.patch.object(crypto, "encrypt_package", side_effect=AssertionError("re-encrypted")):
            self.sync("alpha")
        self.assertEqual((self.cloud_dir / ready_key).read_bytes(), ready_bytes)
        self.sync("beta")
        self.assert_cached_matches_author("beta", snap)
        # a peer losing its cache still gets the (retained) current version re-delivered after alpha pruned
        self.sync("alpha")
        self.apply("alpha")
        shutil.rmtree(str(self.home("gamma") / "cache" / "snapshots"))
        for d in self.inbox_deliveries("gamma"):
            shutil.rmtree(str(self.cloud_dir / "v3" / "inbox" / self.did["gamma"] / d))
        state_path = self.home("gamma") / "relay_state.json"
        rs = json.loads(state_path.read_text("utf-8"))
        rs["localCurrentVersion"]["demo"] = snap
        state_path.write_text(json.dumps(rs), encoding="utf-8")
        self.sync("gamma")
        self.assertIn(snap, self.have("gamma")["needs"])
        self.sync("alpha")
        self.sync("gamma")
        self.assert_cached_matches_author("gamma", snap)

    def test_unfinished_delivery_checkpoint_is_kept_and_resumes(self) -> None:
        snap = self.publish()["snapshotId"]
        self.sync("beta")
        self.sync("gamma")
        rotated = self.rotate("gamma")  # gamma's old-key deliveries become superseded on alpha after re-confirming
        self.reconfirm("alpha", "gamma")
        with self.env("alpha", {"RELAY_DESK_FAULT_POINT": "sync_fwd_after_checkpoint"}):
            E.sync_network()  # the delivery to gamma's new key stops after its checkpoint: generation "preparing"
        book = self.book("alpha")
        new_fp = DS.norm_fp(rotated["keyFingerprint"])
        ent = book["outbound"][DS.outbound_key(self.did["gamma"], new_fp, snap)]
        self.assertEqual(ent["generations"][-1]["state"], "preparing")
        plan = self.dry("alpha")
        reasons = {(x["toDeviceId"], x["reason"]) for x in plan["kept"]["checkpoints"]}
        self.assertIn((self.did["gamma"], "unfinishedDelivery"), reasons)
        deleted = {(x["toDeviceId"], x["reason"]) for x in plan["deletable"]["checkpoints"]}
        self.assertIn((self.did["gamma"], "recipientKeySuperseded"), deleted)
        self.assertIn((self.did["beta"], "peerVerified"), deleted)
        self.apply("alpha")
        with mock.patch.object(crypto, "encrypt_package", side_effect=AssertionError("re-encrypted")):
            res = self.sync("alpha")  # resumes from the kept checkpoint, nothing is encrypted again
        self.assertEqual(res["status"], SR.SUCCESS, res["unresolved"])
        self.sync("gamma")
        self.assert_cached_matches_author("gamma", snap)

    def test_parent_chain_verifies_from_lineage_after_prune(self) -> None:
        s1, s2, s3 = self.three_versions()
        self.apply("beta")
        self.assertEqual(self.cached("beta"), [s3])
        # Parent-chain verification needs the pruned ancestors' signed records: with the cloud copies gone,
        # it must succeed from cache/lineage alone.
        for vid in (s1, s2, s3):
            (self.cloud_dir / "v3" / "snapshots" / ("%s.json" % vid)).unlink()
        with self.env("beta"):
            identity = crypto.load_identity(self.home("beta"))
            cloud = E.load_cloud_config(self.home("beta"))
            provider = storage.LocalDirProvider(str(self.root / "empty_cloud"))
            temp = {"versionGraph": {"demo": {"nodes": {}, "heads": []}}}
            E._complete_version_ancestry(provider, self.home("beta"), identity, cloud, "demo", temp, s3, s1)
        nodes = temp["versionGraph"]["demo"]["nodes"]
        self.assertEqual(set(nodes), {s1, s2, s3})
        self.assertTrue(E._is_version_ancestor(temp["versionGraph"]["demo"], s1, s3))


class TestInterruptedPrune(_PruneBase):
    POINTS = ("prune_after_lineage", "prune_after_mark", "prune_after_rename", "prune_mid_delete",
              "prune_ckpt_after_rename", "prune_before_finish")

    def child_prune(self, alias: str, point: str) -> int:
        rec = self.devices[alias]
        env = dict(os.environ)
        env.update({"RELAY_DESK_HOME": str(rec["home"]), "RELAY_DESK_LEDGER": str(rec["led"]),
                    "RELAY_DESK_DEVICE": alias, "RELAY_DESK_USER_HOME": str(rec["uhome"]),
                    "RELAY_DESK_FAULT_POINT": point, "RELAY_DESK_FAULT_MODE": "sigkill",
                    "PYTHONPATH": REPO_ROOT, "PYTHONDONTWRITEBYTECODE": "1"})
        return subprocess.run([sys.executable, os.path.join(REPO_ROOT, "relay.py"), "cache", "prune", "--apply",
                               "--json"], env=env, cwd=REPO_ROOT, capture_output=True, timeout=120).returncode

    def test_kill_at_every_deletion_point_then_rescan(self) -> None:
        s1, s2, s3 = self.three_versions()
        sent = self.send("alpha", "beta")  # in flight: its snapshot must survive every interruption
        home = self.home("alpha")
        snapshot_before = tree_state(home)
        snap_store = self.root / "alpha_home_copy"
        shutil.copytree(str(home), str(snap_store), symlinks=True)
        for point in self.POINTS:
            with self.subTest(point=point):
                shutil.rmtree(str(home))
                shutil.copytree(str(snap_store), str(home), symlinks=True)
                self.assertEqual(tree_state(home), snapshot_before)
                rc = self.child_prune("alpha", point)
                self.assertIn(rc, KILLED, point)
                # retained data is intact at the moment of the kill (s3 is history now: the send made a new version)
                self.assertIn(sent["snapshotId"], self.cached("alpha"))
                self.assertFalse(self.state("alpha")["sentRelays"][sent["relayId"]]["acked"])
                leftovers = self.status("alpha")["leftovers"]["count"]
                if point in ("prune_after_rename", "prune_mid_delete", "prune_ckpt_after_rename"):
                    self.assertGreaterEqual(leftovers, 1)
                    doc = self.doctor("alpha")
                    self.assertIn(("prune_interrupted", "relay cache prune --apply"),
                                  [(f["code"], f["command"]) for f in doc["findings"]])
                # a fresh scan finishes the job
                res = self.apply("alpha")
                self.assertEqual(res["status"], "success", res.get("failed"))
                self.assertEqual(self.cached("alpha"), [sent["snapshotId"]])
                st = self.status("alpha")
                self.assertEqual(st["leftovers"]["count"], 0)
                self.assertEqual(st["prunable"]["bytes"], 0)
                self.assertTrue({s1, s2, s3} <= set(self.book("alpha")["prunedSnapshots"]))
                for vid in (s1, s2, s3):
                    self.assertTrue((home / "cache" / "lineage" / ("%s.json" % vid)).is_file())
                self.assertEqual(self.sync("alpha")["status"], SR.SUCCESS)
                self.assertEqual(self.cached("alpha"), [sent["snapshotId"]])

    def test_partial_prune_is_never_read_as_a_completed_claim(self) -> None:
        self.three_versions()
        sent = self.send("alpha", "beta")
        dest = self.root / "recv_beta"
        with self.env("beta", {"RELAY_DESK_FAULT_POINT": "rx_before_receipt"}):
            with self.assertRaises(RuntimeError):
                E.receive_relay(sent["relayId"], target_path=str(dest))
        self.assertFalse(self.state("beta")["receivedRelays"][sent["relayId"]]["receiptUploaded"])
        self.assertIn(self.child_prune("beta", "prune_mid_delete"), KILLED)
        # nothing about the handoff changed: no receipt, the sender still waits
        self.assertFalse((self.cloud_dir / "v3" / "inbox" / self.did["beta"] / sent["relayId"] / "receipt.json").exists())
        self.sync("alpha")
        self.assertFalse(self.state("alpha")["sentRelays"][sent["relayId"]]["acked"])
        self.assertEqual(self.project("alpha")["device"], "alpha")
        codes = [f["code"] for f in self.doctor("beta")["findings"]]
        self.assertIn("receipt_upload_pending", codes)
        self.assertIn("prune_interrupted", codes)
        self.apply("beta")
        with self.env("beta"):
            E.receive_relay(sent["relayId"])  # completes the receipt upload
        self.sync("alpha")
        self.assertTrue(self.state("alpha")["sentRelays"][sent["relayId"]]["acked"])
        self.assertEqual(self.project("alpha")["device"], "beta")


class TestDoctorFindings(_PruneBase):
    def test_findings_have_stable_codes_reasons_and_one_command(self) -> None:
        self.three_versions()
        sent = self.send("alpha", "beta")
        self.rotate("beta")
        self.reconfirm("alpha", "beta")
        self.rotate("gamma")
        self.sync("gamma")  # gamma's have is now signed with its new key
        self.sync("alpha")  # signature failure -> descriptor probe -> gamma is key_changed
        doc = self.doctor("alpha")
        by_code = {f["code"]: f for f in doc["findings"]}
        f = by_code["handoff_recipient_key_changed"]
        self.assertEqual(f["severity"], "error")
        self.assertEqual(f["command"], "relay send demo --to beta --reissue")
        self.assertEqual(f["relayId"], sent["relayId"])
        g = by_code["peer_key_changed"]
        self.assertEqual(g["command"], "relay pair --with %s --accept --fingerprint <核对后的新配对指纹>" % self.did["gamma"])
        for item in doc["findings"]:
            self.assertEqual(set(item) & {"code", "severity", "reason", "command"}, {"code", "severity", "reason", "command"})
            self.assertTrue(item["reason"] and item["command"].startswith(("relay ", "cp ", "mv ", "python3 ")))
        self.assertEqual(DX.exit_code_for(doc["findings"]), 4)
        pv = doc["projects"][0]
        self.assertEqual(pv["editor"]["alias"], "alpha")
        self.assertEqual(pv["handoff"]["relayId"], sent["relayId"])
        status = {d["alias"]: d["status"] for d in pv["devices"]}
        self.assertEqual(status["gamma"], "needs_action")
        # the engine's own error names the same command
        with self.env("alpha"), self.assertRaises(E.KeyChangedError) as cm:
            E.send_project("demo", to_alias="beta")
        self.assertEqual(cm.exception.action, f["command"])
        self.assertIn(f["command"], cm.exception.message)

    def test_unknown_device_is_unknown_never_offline(self) -> None:
        self.publish()
        doc = self.doctor("alpha")  # nobody synced: no manifests anywhere
        devices = doc["projects"][0]["devices"]
        self.assertEqual({d["status"] for d in devices}, {"awaiting"})  # alpha delivered, nobody confirmed yet
        snap = self.publish()["snapshotId"]
        rs_path = self.home("alpha") / "deliveries.json"
        book = json.loads(rs_path.read_text("utf-8"))
        book["outbound"] = {k: v for k, v in book["outbound"].items() if v["snapshotId"] != snap}
        rs_path.write_text(json.dumps(book), encoding="utf-8")
        doc = self.doctor("alpha")
        for d in doc["projects"][0]["devices"]:
            self.assertEqual((d["status"], d["label"]), ("unknown", "未知"))
            for word in ("离线", "缺少快照", "offline", "missing"):
                self.assertNotIn(word, json.dumps(d, ensure_ascii=False))

    def test_latest_sync_result_findings(self) -> None:
        self.publish()
        path = self.home("alpha") / "sync_status.json"
        base = {"schema": SR.STATUS_SCHEMA,
                "runId": "sr_x", "mode": "watch", "cycles": 3, "counters": {}}
        cases = [
            (dict(base, status="partial", running=False, unresolved=[
                {"key": "inbox_list||", "errorClass": SR.TRANSIENT, "code": "storage_transport", "message": "reset"}]),
             ("sync_partial", "warning", "relay sync")),
            (dict(base, status="needs_action", running=False, unresolved=[
                {"key": "x", "errorClass": SR.NEEDS_ACTION, "code": "storage_auth", "message": "云盘授权已过期",
                 "action": SR.ACTIONS["storage_auth"]}]),
             ("sync_problem", "error", "rclone config reconnect <远端>:")),
            (dict(base, status="success", running=True, heartbeatAt="2020-01-01T00:00:00.000Z", interval=5,
                  maxBackoff=30, unresolved=[]),
             ("sync_stale", "warning", "relay sync")),
        ]
        for data, (code, sev, cmd) in cases:
            with self.subTest(code=code):
                path.write_text(json.dumps(data), encoding="utf-8")
                found = [(f["code"], f["severity"], f["command"]) for f in self.doctor("alpha")["findings"]]
                self.assertIn((code, sev, cmd), found)
        path.unlink()
        self.assertIn("sync_never_run", [f["code"] for f in self.doctor("alpha")["findings"]])

    def test_recovery_and_receipt_findings(self) -> None:
        sent = self.send("alpha", "beta")
        dest = self.root / "recv_beta"
        with self.env("beta", {"RELAY_DESK_FAULT_POINT": "tx_after_journal_before_apply"}):
            with self.assertRaises(RuntimeError):
                E.receive_relay(sent["relayId"], target_path=str(dest))
        doc = self.doctor("beta")
        f = [x for x in doc["findings"] if x.get("relayId") == sent["relayId"]]
        self.assertEqual([(x["code"], x["command"]) for x in f],
                         [("receive_interrupted", "relay receive %s" % sent["relayId"])])
        meta = self.home("beta") / "transactions" / sent["relayId"] / "meta.json"
        meta.write_text("{not json", encoding="utf-8")
        doc = self.doctor("beta")
        f = [x for x in doc["findings"] if x["code"] == "recovery_corrupted"][0]
        tx_dir = self.home("beta") / "transactions" / sent["relayId"]
        self.assertEqual(f["command"], 'cp -R "%s" "%s.backup"' % (tx_dir, tx_dir))
        self.assertEqual(self.dry("beta")["counts"]["deletablePayloads"], 0)  # unclear recovery: payloads kept


if __name__ == "__main__":
    unittest.main()
