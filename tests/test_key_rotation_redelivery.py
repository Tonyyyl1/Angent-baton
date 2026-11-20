# -*- coding: utf-8 -*-
"""Phase C: key versions, delivery state and cache recovery (three devices, local-folder cloud).

``TestPhaseCDefectRepro`` was written first and run against the post-Phase-B
source, where every test failed (see the phase-C evidence folder). Each test
states the corrected behaviour for one defect: S1, S4, S5, S6, the duplicate
deliveries found in Phase B, Phase E's C02/C03, and cache loss / corruption
blocked by ``processedDeliveries``.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from relaydesk import crypto
from relaydesk import delivery_state as DS
from relaydesk import ledger as L
from relaydesk import relay_engine as E
from relaydesk import storage
from relaydesk import sync_runtime as SR
from test_sync_watch import _ThreeDeviceBase, _failing_upload


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _RotationBase(_ThreeDeviceBase):
    def rotate(self, alias: str) -> Dict[str, Any]:
        rotated = crypto.rotate_identity_keys(self.devices[alias]["home"])
        with self.env(alias):
            E.pair_device()  # republish the descriptor with the new keys
        return rotated

    def reconfirm(self, viewer: str, target: str) -> None:
        with self.env(viewer):
            view = E.pair_device(with_device=self.did[target], accept=False)
            fp = next(p for p in view["peers"] if p["deviceId"] == self.did[target])["pairFingerprint"]
            E.pair_device(with_device=self.did[target], accept=True, fingerprint=fp)

    def sync(self, alias: str, **kw) -> Dict[str, Any]:
        with self.env(alias):
            return E.sync_network(**kw)

    def cloud_digests(self, prefix: str = "v3/inbox") -> Dict[str, str]:
        root = self.cloud_dir / prefix
        out: Dict[str, str] = {}
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    out[str(p.relative_to(self.cloud_dir))] = _sha(p)
        return out

    def deliveries_for(self, alias: str, snap: str) -> List[str]:
        return [d for d in self.inbox_deliveries(alias) if ("_%s_" % snap) in d]

    def have(self, alias: str) -> Dict[str, Any]:
        return json.loads((self.cloud_dir / "v3" / "have" / ("%s.json" % self.did[alias])).read_text(encoding="utf-8"))

    def peer_record(self, viewer: str, target: str) -> Dict[str, Any]:
        cloud = json.loads((self.devices[viewer]["home"] / "cloud.json").read_text(encoding="utf-8"))
        rec = dict(cloud["peers"][self.did[target]])
        for volatile in ("name", "host", "os"):
            rec.pop(volatile, None)
        return rec

    def cache_payload(self, alias: str, snap: str) -> Path:
        return self.devices[alias]["home"] / "cache" / "snapshots" / snap / "payload.tar.gz"

    def assert_cached_matches_author(self, alias: str, snap: str) -> None:
        cdir = self.devices[alias]["home"] / "cache" / "snapshots" / snap
        signed = json.loads((cdir / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(_sha(cdir / "payload.tar.gz"), signed["contentSha256"])

    def project(self, alias: str, proj: str = "demo") -> Dict[str, Any]:
        for p in L.load(self.devices[alias]["led"]).get("projects") or []:
            if p.get("id") == proj:
                return p
        return {}

    def send(self, src: str, dst: str, proj: str = "demo") -> Dict[str, Any]:
        with self.env(src):
            return E.send_project(proj, to_alias=dst)

    def receive(self, alias: str, rid: str) -> Dict[str, Any]:
        dest = self.root / ("recv_%s_%s" % (alias, rid))
        with self.env(alias):
            return E.receive_relay(rid, target_path=str(dest))


class TestPhaseCDefectRepro(_RotationBase):
    # ---- S1: a delivery ID bound to the old key is reused after rotation -> key_changed forever
    def test_s1_redelivery_after_rotation_uses_new_delivery_id(self) -> None:
        snap = self.publish()["snapshotId"]
        beta_inbox = self.cloud_dir / "v3" / "inbox" / self.did["beta"]
        shutil.rmtree(str(beta_inbox))  # alpha's publish delivery to beta was lost
        # alpha's next forward to beta fails after the local checkpoint was written
        with _failing_upload("v3/inbox/%s/" % self.did["beta"]):
            self.sync("alpha")
        self.rotate("beta")
        self.reconfirm("alpha", "beta")
        self.sync("beta")  # re-signs beta's have with the new key; beta holds nothing
        first = self.sync("alpha")
        second = self.sync("alpha")
        codes = [p["code"] for p in first["unresolved"] + second["unresolved"]]
        self.assertNotIn("key_changed", codes)  # pre-fix: key_changed on every attempt
        self.sync("beta")
        self.assertIn(snap, self.cached("beta"))
        self.assert_cached_matches_author("beta", snap)

    # ---- S4: a receipt signed by the receiver's now-retired key is never acked
    def test_s4_receipt_signed_by_retired_key_is_acked(self) -> None:
        sent = self.send("alpha", "beta")
        self.receive("beta", sent["relayId"])  # receipt signed with beta's old key
        self.rotate("beta")
        self.reconfirm("alpha", "beta")
        res = self.sync("alpha")
        entry = self.state("alpha")["sentRelays"][sent["relayId"]]
        self.assertTrue(entry["acked"], res["unresolved"])  # pre-fix: never acked
        self.assertEqual(self.project("alpha")["device"], "beta")
        self.assertEqual(res["status"], SR.SUCCESS, res["unresolved"])

    # ---- S5: resending an unacked handoff after a recipient/sender rotation reports tampering
    def test_s5_resend_after_recipient_rotation_reports_key_change(self) -> None:
        self.send("alpha", "beta")
        self.rotate("beta")
        self.reconfirm("alpha", "beta")
        with self.assertRaises(E.KeyChangedError) as cm:  # pre-fix: TamperError
            self.send("alpha", "beta")
        self.assertEqual(cm.exception.code, "handoff_recipient_key_changed")

    def test_s5_resend_after_sender_rotation_reports_key_change(self) -> None:
        self.send("alpha", "beta")
        self.rotate("alpha")
        with self.assertRaises(E.KeyChangedError) as cm:  # pre-fix: TamperError
            self.send("alpha", "beta")
        self.assertEqual(cm.exception.code, "handoff_sender_key_rotated")

    # ---- Phase B finding: each snapshot reaches the same peer 2-3 times
    def test_one_delivery_per_snapshot_and_recipient_key(self) -> None:
        snap = self.publish()["snapshotId"]
        self.sync("alpha")  # pre-fix: forwards fwd_ copies next to the publish deliveries
        self.sync("beta")   # pre-fix: beta forwards a third copy to gamma
        self.assertEqual(len(self.deliveries_for("beta", snap)), 1, self.inbox_deliveries("beta"))
        self.assertEqual(len(self.deliveries_for("gamma", snap)), 1, self.inbox_deliveries("gamma"))
        self.sync("gamma")
        self.assertIn(snap, self.cached("gamma"))

    # ---- Phase E C03: deliveries sealed to the receiver's old key are never re-sent
    def test_c03_sealed_to_retired_key_is_obsolete_and_redelivered(self) -> None:
        snap = self.publish()["snapshotId"]
        before = self.cloud_digests()
        self.rotate("beta")
        self.reconfirm("alpha", "beta")
        r1 = self.sync("beta")
        self.assertEqual(r1["status"], SR.SUCCESS, r1["unresolved"])  # pre-fix: needs_action
        self.assertIn(snap, self.have("beta").get("needs") or [])
        self.sync("alpha")
        r2 = self.sync("beta")
        self.assertEqual(r2["status"], SR.SUCCESS, r2["unresolved"])
        self.assertIn(snap, self.cached("beta"))
        self.assert_cached_matches_author("beta", snap)
        after = self.cloud_digests()
        self.assertEqual({k: after.get(k) for k in before}, before)  # published objects unchanged

    # ---- Phase E C02: deliveries signed with a re-confirmed peer's old key flag on every sync
    def test_c02_signed_by_retired_sender_key_is_obsolete_and_redelivered(self) -> None:
        self.create_git_project("beta", proj_id="bproj", name="beta 项目")
        with self.env("beta"):
            snap = E.publish_project("bproj")["snapshotId"]
        self.rotate("beta")
        self.reconfirm("alpha", "beta")
        r1 = self.sync("alpha")
        self.assertEqual(r1["status"], SR.SUCCESS, r1["unresolved"])  # pre-fix: needs_action every sync
        self.assertEqual(r1["errors"], [])
        self.sync("beta")  # re-signs its have; re-delivers bproj for alpha with its current key
        r2 = self.sync("alpha")
        self.assertEqual(r2["status"], SR.SUCCESS, r2["unresolved"])
        self.assertIn(snap, self.cached("alpha"))
        self.assert_cached_matches_author("alpha", snap)

    # ---- cache directory lost: processedDeliveries must not block the refill
    def test_lost_cache_directory_is_refilled_from_inbox(self) -> None:
        snap = self.publish()["snapshotId"]
        self.sync("beta")
        self.assertIn(snap, self.cached("beta"))
        inbox_before = self.cloud_digests()
        shutil.rmtree(str(self.devices["beta"]["home"] / "cache" / "snapshots"))
        res = self.sync("beta")
        self.assertIn(snap, self.cached("beta"))  # pre-fix: skipped as processed
        self.assert_cached_matches_author("beta", snap)
        self.assertEqual(self.cloud_digests(), inbox_before)  # repaired from the inbox; nothing new published
        self.assertIn(snap, self.have("beta")["snapshots"])
        self.assertEqual(res["counters"]["snapshotsRepaired"], 1)

    # ---- cache payload corrupted: excluded from the have and repaired
    def test_corrupt_cache_payload_is_repaired(self) -> None:
        snap = self.publish()["snapshotId"]
        self.sync("beta")
        payload = self.cache_payload("beta", snap)
        data = bytearray(payload.read_bytes())
        data[len(data) // 2] ^= 0xFF
        payload.write_bytes(bytes(data))
        self.sync("beta")
        self.assert_cached_matches_author("beta", snap)  # pre-fix: stays corrupt

    # ---- S6: forwarding ignores the author's trust status
    def test_s6_forwarding_requires_trusted_or_attested_author(self) -> None:
        snap = self.publish()["snapshotId"]
        self.sync("beta")
        gamma_inbox = self.cloud_dir / "v3" / "inbox" / self.did["gamma"]
        shutil.rmtree(str(gamma_inbox))
        # beta still caches alpha's snapshot, but no longer has graph evidence for it,
        # and alpha's key changed on beta without re-confirmation
        st_path = self.devices["beta"]["home"] / "relay_state.json"
        st = json.loads(st_path.read_text(encoding="utf-8"))
        st["versionGraph"] = {}
        st["forwardedSnapshots"] = {}  # and no dedupe record either (isolates the author check)
        st_path.write_text(json.dumps(st), encoding="utf-8")
        dstate = self.devices["beta"]["home"] / "deliveries.json"
        if dstate.exists():
            dstate.unlink()
        self.rotate("alpha")
        with self.env("beta"):
            E.pair_device()  # beta discovers alpha's new keys: status key_changed
        self.assertEqual(self.peer_record("beta", "alpha")["status"], "key_changed")
        self.sync("beta")
        self.assertEqual(self.deliveries_for("gamma", snap), [])  # pre-fix: forwarded anyway



def _flip(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))


class TestThreeDeviceCompletion(_RotationBase):
    """Plan section 6 completion test, one continuous story on three devices.

    Covers: a single peer rotating its key, a lost cache directory, a cache that
    fails verification, the original author offline while another device
    re-sends, an old ``have`` arriving late (and one signed by the retired key),
    and a process restart in the middle of a redelivery. Asserts that the other
    peers' trust is unchanged, the target ends with the correct snapshots, and
    every object published before the rotation keeps its exact bytes (snapshot
    D0 before, compare after; no upload ever targets a D0 key).
    """

    def trust_view(self) -> Dict[Any, Any]:
        """alpha<->gamma records in full; beta's own pins of alpha and gamma (its pair fingerprints change with its key)."""
        out: Dict[Any, Any] = {pair: self.peer_record(*pair) for pair in (("alpha", "gamma"), ("gamma", "alpha"))}
        for target in ("alpha", "gamma"):
            rec = self.peer_record("beta", target)
            out[("beta", target)] = {k: rec.get(k) for k in ("status", "paired", "verifyPublicKey", "boxPublicKey",
                                                             "keyFingerprint", "pairedAt", "keyHistory")}
        return out

    def book(self, alias: str) -> Dict[str, Any]:
        return json.loads((self.devices[alias]["home"] / "deliveries.json").read_text(encoding="utf-8"))

    def graph_node(self, alias: str, proj: str, snap: str) -> Dict[str, Any]:
        return self.state(alias)["versionGraph"][proj]["nodes"][snap]

    def go_offline(self, alias: str) -> None:
        home = self.devices[alias]["home"]
        home.rename(home.with_name(home.name + ".offline"))

    def come_online(self, alias: str) -> None:
        home = self.devices[alias]["home"]
        home.with_name(home.name + ".offline").rename(home)

    def test_rotation_cache_loss_corruption_offline_author_late_have_restart(self) -> None:
        uploads: List[str] = []
        original_upload = storage.LocalDirProvider.upload

        def recording(provider, local_path, remote_key):
            uploads.append(remote_key)
            return original_upload(provider, local_path, remote_key)

        patcher = mock.patch.object(storage.LocalDirProvider, "upload", recording)
        patcher.start()
        self.addCleanup(patcher.stop)
        have_key = self.cloud_dir / "v3" / "have" / ("%s.json" % self.did["beta"])

        # ---- baseline: S1 (alpha, demo) everywhere; Sb0 (beta, bproj) on alpha only
        s1 = self.publish()["snapshotId"]
        self.sync("beta")
        self.sync("gamma")
        self.create_git_project("beta", proj_id="bproj", name="beta 项目")
        with self.env("beta"):
            sb0 = E.publish_project("bproj")["snapshotId"]
        self.sync("alpha")
        self.assertEqual(self.sync("beta")["status"], SR.SUCCESS)
        d0 = dict(self.cloud_digests("v3/inbox"), **self.cloud_digests("v3/snapshots"))
        trust0 = self.trust_view()
        have_old_key = have_key.read_bytes()  # signed by beta's key before the rotation
        beta_old_fp = self.identity("beta")["keyFingerprint"]
        mark = d0_mark = len(uploads)

        # ---- 1. beta rotates; alpha and gamma detect it and pause beta only
        self.rotate("beta")
        self.sync("beta")  # re-signs beta's have with the new key
        self.sync("alpha")
        self.assertEqual(self.peer_record("alpha", "beta")["status"], "key_changed")
        s2 = self.publish()["snapshotId"]
        self.sync("gamma", project_key="demo")  # gamma takes S2; bproj stays for later
        self.assertIn(s2, self.cached("gamma"))
        self.assertNotIn(sb0, self.cached("gamma"))
        self.assertEqual(self.peer_record("gamma", "beta")["status"], "key_changed")
        beta_inbox = "v3/inbox/%s/" % self.did["beta"]
        self.assertEqual([k for k in uploads[mark:] if k.startswith(beta_inbox)], [])  # nothing for a paused peer
        self.assertEqual(self.trust_view(), trust0)

        # ---- 2. re-confirmation on alpha and gamma; beta also lost its cache directory
        self.reconfirm("alpha", "beta")
        self.reconfirm("gamma", "beta")
        self.assertEqual(DS.norm_fp(self.peer_record("alpha", "beta")["keyHistory"][-1]["keyFingerprint"]),
                         DS.norm_fp(beta_old_fp))
        shutil.rmtree(str(self.devices["beta"]["home"] / "cache" / "snapshots"))
        rb = self.sync("beta")
        self.assertEqual(rb["status"], SR.SUCCESS, rb["unresolved"])  # sealed to the old key: obsolete, not an error
        self.assertEqual(sorted(self.have("beta")["needs"]), sorted([s1, sb0]))
        late_have = have_key.read_bytes()

        # ---- 3. the original author goes offline; gamma re-sends what beta asks for
        self.go_offline("alpha")
        events: List[Dict[str, Any]] = []
        rg = self.sync("gamma", on_event=events.append)
        self.assertEqual(rg["status"], SR.SUCCESS, rg["unresolved"])
        # beta's original Sb0 delivery to gamma is signed with beta's retired key: obsolete. Beta already
        # re-issued it with its new key after rotating (reason sender_key_rotated), which gamma takes now.
        self.assertIn(("delivery_obsolete", sb0, "sender_key_retired"),
                      {(e["kind"], e.get("snapshotId"), e.get("reason")) for e in events})
        self.assertEqual(self.graph_node("gamma", "bproj", sb0)["trust"], "historical")  # signed by beta's old key
        self.assertEqual(DS.norm_fp(self.graph_node("gamma", "bproj", sb0)["authorKeyFingerprint"]),
                         DS.norm_fp(beta_old_fp))
        sent_by_gamma = {(r["snapshotId"], r["deliveryId"].split("_k")[-1][:8]) for r in rg["forwarded"]
                         if r["toDeviceId"] == self.did["beta"]}
        beta_fp8 = DS.fp8(self.identity("beta")["keyFingerprint"])
        self.assertEqual(sent_by_gamma, {(s1, beta_fp8), (s2, beta_fp8), (sb0, beta_fp8)})
        rb = self.sync("beta")
        self.assertEqual(rb["status"], SR.SUCCESS, rb["unresolved"])
        for snap in (s1, s2, sb0):
            self.assert_cached_matches_author("beta", snap)
        self.assertEqual(self.graph_node("beta", "demo", s2)["trust"], "current")
        self.assertEqual(self.have("beta")["needs"], [])

        # ---- 4. gamma's cached S2 fails verification and its inbox copy is corrupt too; beta re-sends
        _flip(self.cache_payload("gamma", s2))
        gamma_s2 = [d for d in self.deliveries_for("gamma", s2)]
        self.assertEqual(len(gamma_s2), 1)
        corrupt_pkg = self.cloud_dir / "v3" / "inbox" / self.did["gamma"] / gamma_s2[0] / "package.enc"
        _flip(corrupt_pkg)
        corrupt_bytes = corrupt_pkg.read_bytes()
        rg = self.sync("gamma")
        problems = [(p["errorClass"], p["code"]) for p in rg["unresolved"]]
        self.assertEqual(problems, [(SR.INTEGRITY, "digest_mismatch")])
        self.assertIn(s2, self.have("gamma")["needs"])
        self.assertNotIn(s2, self.have("gamma")["snapshots"])
        with mock.patch.object(crypto, "decrypt_package", side_effect=AssertionError("downloaded again")):
            again = self.sync("gamma")  # a new run: the quarantined object is reported, never re-processed
        self.assertEqual([(p["errorClass"], p["code"]) for p in again["unresolved"]], problems)
        rb = self.sync("beta")
        self.assertIn((self.did["gamma"], s2), {(r["toDeviceId"], r["snapshotId"]) for r in rb["forwarded"]})
        rg = self.sync("gamma")
        self.assertEqual(rg["status"], SR.SUCCESS, rg["unresolved"])
        self.assert_cached_matches_author("gamma", s2)
        self.assertEqual(corrupt_pkg.read_bytes(), corrupt_bytes)  # nobody rewrote the damaged object

        # ---- 5. alpha back; its process dies in the middle of publishing S3, a new process resumes
        self.come_online("alpha")
        (self.repo / "keep.txt").write_text("S3\n", encoding="utf-8")
        with self.env("alpha", {"RELAY_DESK_FAULT_POINT": "sync_fwd_after_package_upload"}):
            pub = E.publish_project("demo")
        s3 = pub["snapshotId"]
        self.assertEqual(sorted(e["peerDeviceId"] for e in pub["deliveryErrors"]),
                         sorted([self.did["beta"], self.did["gamma"]]))
        pending = [v for v in self.book("alpha")["outbound"].values() if v["snapshotId"] == s3]
        self.assertEqual(sorted((v["recipientDeviceId"], v["status"]) for v in pending),
                         sorted([(self.did["beta"], "pending"), (self.did["gamma"], "pending")]))
        with mock.patch.object(crypto, "encrypt_package", side_effect=AssertionError("re-encrypted")):
            second = self.sync("alpha")  # a fresh process: resumes from the checkpoints, nothing new encrypted
        self.assertEqual(second["status"], SR.SUCCESS, second["unresolved"])
        for ent in self.book("alpha")["outbound"].values():
            if ent["snapshotId"] == s3:
                self.assertEqual([g["state"] for g in ent["generations"]], ["published"])
                gen = ent["generations"][0]
                ckpt = (self.devices["alpha"]["home"] / "sync_publications"
                        / hashlib.sha256(gen["readyKey"].encode("utf-8")).hexdigest() / "ready.json")
                self.assertEqual((self.cloud_dir / gen["readyKey"]).read_bytes(), ckpt.read_bytes())
        self.assertEqual(self.sync("beta")["status"], SR.SUCCESS)
        self.assertEqual(self.sync("gamma")["status"], SR.SUCCESS)
        self.assert_cached_matches_author("beta", s3)
        self.assert_cached_matches_author("gamma", s3)
        # beta's own node keeps the evidence it recorded at publish time: the key that signed it
        self.assertEqual(DS.norm_fp(self.graph_node("beta", "bproj", sb0)["authorKeyFingerprint"]), DS.norm_fp(beta_old_fp))

        # ---- 6. a late have (older seq) and a have signed by beta's retired key change nothing
        res = self.sync("alpha")
        self.assertEqual(res["status"], SR.SUCCESS, res["unresolved"])
        verified_before = {k: v["status"] for k, v in self.book("alpha")["outbound"].items()}
        for stale in (late_have, have_old_key):
            have_key.write_bytes(stale)
            mark = len(uploads)
            res = self.sync("alpha")
            self.assertEqual(res["status"], SR.SUCCESS, res["unresolved"])  # no alarm
            self.assertEqual([k for k in uploads[mark:] if k.startswith(beta_inbox)], [])  # no regeneration
            self.assertEqual({k: v["status"] for k, v in self.book("alpha")["outbound"].items()}, verified_before)
            self.assertEqual(self.peer_record("alpha", "beta")["status"], "paired")

        # ---- end state
        for alias in ("alpha", "beta", "gamma"):
            res = self.sync(alias)
            self.assertEqual(res["status"], SR.SUCCESS, (alias, res["unresolved"]))
        for snap in (s1, s2, s3, sb0):
            self.assert_cached_matches_author("beta", snap)
            self.assert_cached_matches_author("gamma", snap)
        self.assertEqual(self.state("beta")["versionGraph"]["demo"]["heads"], [s3])  # the target holds the latest
        after = dict(self.cloud_digests("v3/inbox"), **self.cloud_digests("v3/snapshots"))
        self.assertEqual({k: after.get(k) for k in d0}, d0)  # every pre-rotation object byte-identical
        self.assertEqual(sorted(set(uploads[d0_mark:]) & set(d0)), [])  # and never re-uploaded
        self.assertEqual(self.trust_view(), trust0)
        ids = [g["deliveryId"] for alias in ("alpha", "beta", "gamma")
               for ent in self.book(alias)["outbound"].values() for g in ent["generations"]]
        self.assertEqual(len(ids), len(set(ids)))  # never one delivery ID for two generations


REPO_ROOT = str(Path(__file__).resolve().parent.parent)
KILLED = (-9, 137)  # SIGKILL on POSIX; os._exit(137) on Windows


class TestRestartAtEveryFaultPoint(_RotationBase):
    """Process restart (SIGKILL) at each new fault point resumes only the missing steps."""

    SENDER_POINTS = ("sync_fwd_after_entry_preparing", "sync_fwd_after_checkpoint",
                     "sync_fwd_after_package_upload", "sync_fwd_after_ready_upload")
    RECEIVER_POINTS = ("sync_rx_after_cache_save", "sync_rx_after_state_update")

    def child_sync(self, alias: str, point: str) -> int:
        rec = self.devices[alias]
        env = dict(os.environ)
        env.update({"RELAY_DESK_HOME": str(rec["home"]), "RELAY_DESK_LEDGER": str(rec["led"]),
                    "RELAY_DESK_DEVICE": alias, "RELAY_DESK_USER_HOME": str(rec["uhome"]),
                    "RELAY_DESK_FAULT_POINT": point, "RELAY_DESK_FAULT_MODE": "sigkill",
                    "PYTHONPATH": REPO_ROOT, "PYTHONDONTWRITEBYTECODE": "1"})
        code = "from relaydesk import relay_engine as E; E.sync_network(status_file=False)"
        return subprocess.run([sys.executable, "-c", code], env=env, cwd=REPO_ROOT,
                              capture_output=True, timeout=120).returncode

    def publish_skipping(self, peer_alias: str) -> str:
        original = E._Outbound.deliver
        skip = self.did[peer_alias]

        def deliver(outbound, peer, item, *args, **kwargs):
            if peer["deviceId"] == skip:
                raise storage.StorageError("injected: %s unreachable during publish" % peer_alias)
            return original(outbound, peer, item, *args, **kwargs)

        with mock.patch.object(E._Outbound, "deliver", deliver):
            res = self.publish()
        self.assertEqual([d["deviceId"] for d in res["deliveredTo"]], [self.did["gamma"]])
        return res["snapshotId"]

    def test_sender_side_kill_then_resume(self) -> None:
        for point in self.SENDER_POINTS:
            with self.subTest(point=point):
                snap = self.publish_skipping("beta")
                rc = self.child_sync("alpha", point)
                self.assertIn(rc, KILLED)  # killed at the fault point
                ent = json.loads((self.devices["alpha"]["home"] / "deliveries.json").read_text(encoding="utf-8"))
                entry = [e for e in ent["outbound"].values()
                         if e["snapshotId"] == snap and e["recipientDeviceId"] == self.did["beta"]]
                self.assertEqual(len(entry), 1)
                gen = entry[0]["generations"][0]
                ckpt = (self.devices["alpha"]["home"] / "sync_publications"
                        / hashlib.sha256(gen["readyKey"].encode("utf-8")).hexdigest())
                had_checkpoint = (ckpt / "ready.json").is_file()
                self.assertEqual(had_checkpoint, point != "sync_fwd_after_entry_preparing")
                guard = (mock.patch.object(crypto, "encrypt_package", side_effect=AssertionError("re-encrypted"))
                         if had_checkpoint else mock.patch.object(crypto, "encrypt_package", wraps=crypto.encrypt_package))
                with guard:
                    res = self.sync("alpha")
                self.assertEqual(res["status"], SR.SUCCESS, res["unresolved"])
                ent = json.loads((self.devices["alpha"]["home"] / "deliveries.json").read_text(encoding="utf-8"))
                entry = [e for e in ent["outbound"].values()
                         if e["snapshotId"] == snap and e["recipientDeviceId"] == self.did["beta"]][0]
                self.assertEqual([g["state"] for g in entry["generations"]], ["published"])
                self.assertEqual((self.cloud_dir / gen["readyKey"]).read_bytes(), (ckpt / "ready.json").read_bytes())
                self.assertEqual(len(self.deliveries_for("beta", snap)), 1)
                self.sync("beta")
                self.assert_cached_matches_author("beta", snap)

    def test_receiver_side_kill_then_resume(self) -> None:
        for point in self.RECEIVER_POINTS:
            with self.subTest(point=point):
                snap = self.publish()["snapshotId"]
                rc = self.child_sync("beta", point)
                self.assertIn(rc, KILLED)
                self.assert_cached_matches_author("beta", snap)  # the cache write had completed
                with mock.patch.object(crypto, "decrypt_package", side_effect=AssertionError("downloaded again")):
                    res = self.sync("beta")
                self.assertEqual(res["status"], SR.SUCCESS, res["unresolved"])
                processed = self.state("beta")["processedDeliveries"]
                self.assertIn(snap, {v["snapshotId"] for v in processed.values()})
                self.assertIn(snap, self.have("beta")["snapshots"])

    def test_have_upload_kill_then_resume(self) -> None:
        snap = self.publish()["snapshotId"]
        self.sync("beta")
        seq_before = self.have("beta")["seq"]
        _flip(self.cache_payload("beta", snap))  # changes the have content, so a new one is due
        self.assertIn(self.child_sync("beta", "sync_have_before_upload"), KILLED)
        self.assertEqual(self.have("beta")["seq"], seq_before)  # killed before the upload
        res = self.sync("beta")
        self.assertEqual(res["status"], SR.SUCCESS, res["unresolved"])
        self.assertGreater(self.have("beta")["seq"], seq_before + 1)  # the burnt sequence is never reused
        self.assert_cached_matches_author("beta", snap)


class TestHaveAndTrustRules(_RotationBase):
    def test_have_never_counts_as_claim(self) -> None:
        sent = self.send("alpha", "beta")
        self.sync("beta")  # caches the handoff snapshot; its have lists it
        self.assertIn(sent["snapshotId"], self.have("beta")["snapshots"])
        res = self.sync("alpha")
        self.assertFalse(self.state("alpha")["sentRelays"][sent["relayId"]]["acked"])
        self.assertEqual(self.project("alpha")["device"], "alpha")
        self.assertEqual(res["syncedReceipts"], [])

    def test_key_changed_peer_never_regains_trust_by_itself(self) -> None:
        self.rotate("beta")
        self.sync("beta")
        self.create_git_project("beta", proj_id="bproj", name="beta 项目")
        with self.env("beta"):
            bsnap = E.publish_project("bproj")["snapshotId"]  # signed with beta's new key
        for _ in range(3):
            res = self.sync("alpha")
            self.assertEqual(self.peer_record("alpha", "beta")["status"], "key_changed")
            self.assertFalse(self.peer_record("alpha", "beta")["paired"])
        self.assertNotIn(bsnap, self.cached("alpha"))
        self.assertNotIn("bproj", (E.load_relay_state(self.devices["alpha"]["home"]) or {}).get("versionGraph") or {})
        self.assertTrue(self.deliveries_for("alpha", bsnap))  # it is there, just not trusted
        self.assertIn(("needs_user_action", "peer_key_changed"), {(p["errorClass"], p["code"]) for p in res["unresolved"]})

    def test_legacy_have_without_seq_is_accepted_and_new_have_parses_old_way(self) -> None:
        snap = self.publish()["snapshotId"]
        self.sync("beta")
        have = self.have("beta")
        self.assertEqual((have["haveFormat"], have["keyFingerprint"]), (2, self.identity("beta")["keyFingerprint"]))
        self.assertEqual(E._extract_have_snapshot_ids(have), {snap})  # pre-Phase-C readers still parse it
        legacy = {"formatVersion": 3, "deviceId": self.did["beta"], "snapshots": [snap], "updatedAt": "x"}
        signed = crypto.sign_json(legacy, self.identity("beta")["signPrivateKey"], signer_device_id=self.did["beta"])
        with self.env("alpha"):
            cloud = E.load_cloud_config(self.devices["alpha"]["home"])
        provider = storage.LocalDirProvider(str(self.cloud_dir))
        (self.cloud_dir / "v3" / "have" / ("%s.json" % self.did["beta"])).write_text(json.dumps(signed), encoding="utf-8")
        view = E._fetch_peer_have(provider, cloud["peers"][self.did["beta"]], None)
        self.assertEqual((view.status, view.seq, view.snapshots), ("ok", -1, {snap}))

    def test_have_seq_survives_lost_delivery_state(self) -> None:
        self.publish()
        self.sync("beta")
        seq = self.have("beta")["seq"]
        (self.devices["beta"]["home"] / "deliveries.json").unlink()
        self.sync("beta", )
        self.assertGreater(self.have("beta")["seq"], seq)

    def test_corrupt_delivery_state_stops_sync_without_writes(self) -> None:
        self.publish()
        self.sync("beta")
        path = self.devices["beta"]["home"] / "deliveries.json"
        path.write_bytes(b"{broken")
        before_cloud = self.cloud_digests("v3")
        before_state = (self.devices["beta"]["home"] / "relay_state.json").read_bytes()
        res = self.sync("beta")
        self.assertEqual(res["status"], SR.NEEDS_ACTION_STATUS)
        self.assertEqual([p["code"] for p in res["unresolved"]], ["delivery_state_corrupted"])
        self.assertEqual(path.read_bytes(), b"{broken")
        self.assertEqual(self.cloud_digests("v3"), before_cloud)
        self.assertEqual((self.devices["beta"]["home"] / "relay_state.json").read_bytes(), before_state)

    def test_key_history_and_previous_keys_survive_normalization(self) -> None:
        self.rotate("beta")
        self.reconfirm("alpha", "beta")
        home = self.devices["alpha"]["home"]
        raw = json.loads((home / "cloud.json").read_text(encoding="utf-8"))
        from relaydesk import devices
        migrated = devices.migrate_cloud_config(raw)
        self.assertEqual(migrated["peers"][self.did["beta"]]["keyHistory"], raw["peers"][self.did["beta"]]["keyHistory"])
        ident = self.identity("beta")
        self.assertEqual(len(ident["previousKeys"]), 1)
        self.assertNotIn("signPrivateKey", ident["previousKeys"][0])
        self.assertNotIn("boxPrivateKey", ident["previousKeys"][0])
        again = crypto.ensure_identity(self.devices["beta"]["home"], alias="beta")
        self.assertEqual(again["previousKeys"], ident["previousKeys"])
        self.assertNotIn("previousKeys", crypto.public_descriptor_v3(again))

    def test_retired_receipt_accepted_only_for_relay_bound_to_that_key(self) -> None:
        sent = self.send("alpha", "beta")
        self.receive("beta", sent["relayId"])
        self.rotate("beta")
        self.reconfirm("alpha", "beta")
        st_path = self.devices["alpha"]["home"] / "relay_state.json"
        st = json.loads(st_path.read_text(encoding="utf-8"))
        self.assertEqual(DS.norm_fp(st["sentRelays"][sent["relayId"]]["recipientKeyFingerprint"]),
                         DS.norm_fp(self.peer_record("alpha", "beta")["keyHistory"][-1]["keyFingerprint"]))
        st["sentRelays"][sent["relayId"]]["recipientKeyFingerprint"] = "FFFF-FFFF-FFFF-FFFF"
        st_path.write_text(json.dumps(st), encoding="utf-8")
        res = self.sync("alpha")
        self.assertFalse(json.loads(st_path.read_text(encoding="utf-8"))["sentRelays"][sent["relayId"]]["acked"])
        self.assertIn("receipts", {p["stage"] for p in res["unresolved"]})

    def test_legacy_deliveries_are_adopted_without_duplicates(self) -> None:
        """State written by a pre-Phase-C engine (legacy IDs, no deliveries.json) migrates with no new deliveries."""
        snap = self.publish()["snapshotId"]
        home = self.devices["alpha"]["home"]
        # Rebuild the legacy layout: the old engine used sync_<snap>_<me6> for every peer.
        shutil.rmtree(str(self.cloud_dir / "v3" / "inbox"))
        shutil.rmtree(str(home / "sync_publications"))
        (home / "deliveries.json").unlink()
        with self.env("alpha"):
            identity = crypto.load_identity(home)
            identity["keyFingerprint"] = E._identity_fingerprint(identity)
            cloud = E.load_cloud_config(home)
            provider = storage.LocalDirProvider(str(self.cloud_dir))
            cdir = home / "cache" / "snapshots" / snap
            signed = json.loads((cdir / "snapshot.json").read_text(encoding="utf-8"))
            meta = json.loads((cdir / "meta_plain.json").read_text(encoding="utf-8"))
            legacy = "sync_%s_%s" % (snap, self.did["alpha"][-6:])
            for alias in ("beta", "gamma"):
                E._upload_v3_sync_delivery(provider, identity, "alpha", cloud["peers"][self.did[alias]], legacy,
                                           str(cdir / "payload.tar.gz"), signed, meta, local_home=home)
        before = self.cloud_digests("v3/inbox")
        for _ in range(2):
            self.sync("alpha")
        self.assertEqual(self.cloud_digests("v3/inbox"), before)  # adopted: no duplicate delivery
        book = json.loads((home / "deliveries.json").read_text(encoding="utf-8"))
        self.assertEqual(book["migration"]["checkpoints"], 2)
        gens = [g for e in book["outbound"].values() for g in e["generations"]]
        self.assertEqual({(g["deliveryId"], g["reason"]) for g in gens}, {(legacy, "legacy")})
        self.sync("beta")
        self.assert_cached_matches_author("beta", snap)



class TestS7AuthorTrustOnHandoff(_RotationBase):
    """S7: the handoff paths (list_inbox, receive) did not check the snapshot author's trust status."""

    def test_s7_handoff_wrapping_a_key_changed_authors_snapshot_is_refused(self) -> None:
        sent = self.send("alpha", "beta")
        rid = sent["relayId"]
        self.rotate("alpha")
        with self.env("beta"):
            E.pair_device()  # beta sees alpha's new keys: alpha is key_changed (not re-confirmed) on beta
        self.assertEqual(self.peer_record("beta", "alpha")["status"], "key_changed")
        # A paired third device (gamma) re-wraps alpha's signed snapshot into a handoff for beta.
        home_b = self.devices["beta"]["home"]
        beta_id = crypto.load_identity(home_b)
        cloud_b = json.loads((home_b / "cloud.json").read_text(encoding="utf-8"))
        alpha_old = cloud_b["peers"][self.did["alpha"]]
        gamma_id = crypto.load_identity(self.devices["gamma"]["home"])
        cloud_g = json.loads((self.devices["gamma"]["home"] / "cloud.json").read_text(encoding="utf-8"))
        beta_desc = cloud_g["peers"][self.did["beta"]]
        ddir = self.cloud_dir / "v3" / "inbox" / self.did["beta"] / rid
        ready = json.loads((ddir / "ready.json").read_text(encoding="utf-8"))
        tar = self.root / "s7_payload.tar.gz"
        crypto.decrypt_package(str(ddir / "package.enc"), str(tar), ready, beta_id, alpha_old)
        meta = crypto.decrypt_meta(ready["meta"], beta_id, alpha_old)
        enc2 = self.root / "s7_package.enc"
        envelope = crypto.encrypt_package(str(tar), str(enc2), gamma_id, beta_desc)
        forged = {k: v for k, v in ready.items() if k not in ("signature", "signerDeviceId")}
        forged.update(envelope)
        forged.update({"senderDeviceId": self.did["gamma"], "fromDeviceId": self.did["gamma"], "senderAlias": "gamma",
                       "fromAlias": "gamma", "meta": crypto.encrypt_meta(meta, gamma_id, beta_desc)})
        forged = crypto.sign_json(forged, gamma_id["signPrivateKey"], signer_device_id=self.did["gamma"])
        shutil.copyfile(str(enc2), str(ddir / "package.enc"))
        (ddir / "ready.json").write_text(json.dumps(forged), encoding="utf-8")
        with self.env("beta"):
            listed = [i for i in E.list_inbox()["incoming"] if i["relayId"] == rid]
            self.assertEqual(len(listed), 1)
            self.assertTrue(listed[0]["tampered"])  # pre-fix: listed as a valid handoff
            with self.assertRaises(E.RelayError) as cm:  # pre-fix: received and applied
                E.receive_relay(rid, target_path=str(self.root / "s7_target"))
        self.assertEqual(cm.exception.code, "untrusted_author")
        self.assertFalse((self.root / "s7_target").exists())


if __name__ == "__main__":
    unittest.main()
