"""Regression scenarios found by the independent v3 delivery review."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from relaydesk import crypto, ledger as L, packager, relay_engine as E, storage, workspace_tx as W
from test_multidevice_network import MultiDeviceTestBase
from test_workspace_tx import _build_folder_fixture, _build_git_fixture, _snapshot_tree, _capture_git_state


class TestReviewedNetworkRegressions(MultiDeviceTestBase):
    def setup_network(self, aliases=("alpha", "beta", "gamma"), pairs=None):
        for alias in aliases:
            self.init_device(alias)
        for left, right in pairs or [(a, b) for i, a in enumerate(aliases) for b in aliases[i + 1:]]:
            self.pair_pair(left, right)
        source = self.root / "folder_alpha"
        source.mkdir()
        for name in ("a.txt", "b.txt"):
            (source / name).write_text("v1 " + name)
        def register(ledger):
            ledger["projects"].append(L.norm_project({
                "id": "demo", "name": "demo", "device": "alpha", "tool": "claude",
                "paths": {"alpha": str(source)},
            }, ledger["config"]))
        L.update(self.devices["alpha"]["led"], register)
        return source

    def state(self, alias):
        return E._load_relay_state(self.devices[alias]["home"])

    def ring(self):
        source = self.setup_network()
        with self.env("alpha"):
            first = E.send_project("demo", to_alias="beta")
        with self.env("beta"):
            E.receive_relay(first["relayId"], target_path=str(self.root / "work_beta"))
            second = E.send_project("demo", to_alias="gamma")
        with self.env("gamma"):
            E.receive_relay(second["relayId"], target_path=str(self.root / "work_gamma"))
            third = E.send_project("demo", to_alias="alpha")
        return source, second, third

    def test_three_device_ring_does_not_require_manual_sync(self):
        source, second, third = self.ring()
        with self.env("alpha"), mock.patch.object(E, "sync_network", side_effect=AssertionError("full sync unnecessary")):
            self.assertTrue(E.receive_relay(third["relayId"])["ok"])
        self.assertIn(second["snapshotId"], self.state("alpha")["versionGraph"]["demo"]["nodes"])
        self.assertEqual((source / "a.txt").read_text(), "v1 a.txt")

    def test_existing_v3_ready_records_supply_missing_ancestor_index(self):
        _, second, third = self.ring()
        (self.cloud_dir / "v3" / "snapshots" / (second["snapshotId"] + ".json")).unlink()
        with self.env("alpha"):
            self.assertTrue(E.receive_relay(third["relayId"])["ok"])

    def test_tampered_ancestor_never_changes_workspace(self):
        source, second, third = self.ring()
        marker = self.cloud_dir / "v3" / "snapshots" / (second["snapshotId"] + ".json")
        raw = json.loads(marker.read_text())
        raw["parentVersionIds"] = []
        marker.write_text(json.dumps(raw))
        before = _snapshot_tree(str(source))
        with self.env("alpha"), self.assertRaises(crypto.TamperError):
            E.receive_relay(third["relayId"])
        self.assertEqual(_snapshot_tree(str(source)), before)
        self.assertNotIn(third["relayId"], self.state("alpha")["receivedRelays"])

    def test_missing_ancestor_is_not_reported_as_a_fork(self):
        source, second, third = self.ring()
        (self.cloud_dir / "v3" / "snapshots" / (second["snapshotId"] + ".json")).unlink()
        for marker in (self.cloud_dir / "v3" / "inbox").rglob("ready.json"):
            if json.loads(marker.read_text()).get("snapshot", {}).get("versionId") == second["snapshotId"]:
                marker.unlink()
        before = _snapshot_tree(str(source))
        with self.env("alpha"), self.assertRaises(E.RelayError) as caught:
            E.receive_relay(third["relayId"])
        self.assertEqual(caught.exception.code, "lineage_incomplete")
        self.assertEqual(_snapshot_tree(str(source)), before)
        self.assertEqual(L.load(self.devices["alpha"]["led"])["projects"][0]["conflicts"], [])

    def test_publish_cannot_advance_an_active_handoff_or_ignore_its_receipt(self):
        source = self.setup_network()
        with self.env("alpha"):
            sent = E.send_project("demo", to_alias="beta")
            (source / "a.txt").write_text("later local edits")
            before = self.state("alpha")
            with self.assertRaises(E.ActiveHandoffConflictError):
                E.publish_project("demo")
            self.assertEqual(self.state("alpha")["localCurrentVersion"], before["localCurrentVersion"])
            self.assertEqual(self.state("alpha")["projectBaselines"], before["projectBaselines"])
        with self.env("beta"):
            E.receive_relay(sent["relayId"], target_path=str(self.root / "work_beta"))
        with self.env("alpha"):
            with self.assertRaises(E.RelayError) as caught:
                E.publish_project("demo")
            self.assertEqual(caught.exception.code, "wrong_device")
            with self.assertRaises(E.RelayError):
                E.send_project("demo", to_alias="gamma")
        self.assertEqual(L.load(self.devices["alpha"]["led"])["projects"][0]["device"], "beta")
        self.assertEqual((source / "a.txt").read_text(), "later local edits")

    def test_publish_is_blocked_by_an_unpublished_pending_send(self):
        self.setup_network()
        original = storage.LocalDirProvider.upload
        def fail_package(provider, path, key):
            if key.endswith("/package.enc"):
                raise storage.StorageError("package upload failed")
            return original(provider, path, key)
        with self.env("alpha"):
            with mock.patch.object(storage.LocalDirProvider, "upload", fail_package), self.assertRaises(storage.StorageError):
                E.send_project("demo", to_alias="beta")
            self.assertTrue(self.state("alpha")["pendingSends"])
            with self.assertRaises(E.ActiveHandoffConflictError):
                E.publish_project("demo")

    def test_interrupted_snapshot_index_publication_keeps_parent_chain(self):
        self.setup_network()
        original = storage.LocalDirProvider.upload
        def lose_index_result(provider, path, key):
            value = original(provider, path, key)
            if key.startswith("v3/snapshots/"):
                raise storage.StorageError("index published, reply lost")
            return value
        with self.env("alpha"):
            with mock.patch.object(storage.LocalDirProvider, "upload", lose_index_result), self.assertRaises(storage.StorageError):
                E.publish_project("demo")
            previous = self.state("alpha")["localCurrentVersion"]["demo"]
            published = E.publish_project("demo")
        self.assertEqual(published["parentVersionIds"], [previous])

    def test_forwarding_retry_keeps_exact_ciphertext_before_and_after_ready(self):
        self.setup_network(pairs=[("alpha", "beta"), ("beta", "gamma")])
        with self.env("alpha"):
            E.publish_project("demo")
        self.pair_pair("alpha", "gamma")
        original_upload = storage.LocalDirProvider.upload
        first_ready = []
        def fail_before_ready(provider, path, key):
            if "/fwd_" in key and key.endswith("/ready.json"):
                first_ready.append(key)
                raise storage.StorageError("before ready")
            return original_upload(provider, path, key)
        with self.env("beta"), mock.patch.object(storage.LocalDirProvider, "upload", fail_before_ready):
            self.assertTrue(E.sync_network()["errors"])
        self.assertEqual(len(first_ready), 1)
        ready = self.cloud_dir / first_ready[0]
        package = ready.parent / "package.enc"
        encrypted = package.read_bytes()
        def lose_ready_result(provider, path, key):
            value = original_upload(provider, path, key)
            if key == first_ready[0]:
                raise storage.StorageError("ready published, reply lost")
            return value
        with self.env("beta"), mock.patch.object(crypto, "encrypt_package", side_effect=AssertionError("re-encrypted")):
            with mock.patch.object(storage.LocalDirProvider, "upload", lose_ready_result):
                self.assertTrue(E.sync_network()["errors"])
            marker = ready.read_bytes()
            self.assertEqual(package.read_bytes(), encrypted)
            retry = E.sync_network()
        self.assertEqual(retry["errors"], [])
        self.assertEqual(len(retry["forwarded"]), 1)
        self.assertEqual(ready.read_bytes(), marker)
        self.assertEqual(package.read_bytes(), encrypted)
        with self.env("gamma"):
            synced = E.sync_network()
        self.assertEqual(synced["errors"], [])
        self.assertEqual(len(synced["syncedSnapshots"]), 1)

    def pending_existing_receive(self):
        source = self.setup_network(aliases=("alpha", "beta"))
        target = self.root / "work_beta"
        with self.env("alpha"):
            first = E.send_project("demo", to_alias="beta")
        with self.env("beta"):
            E.receive_relay(first["relayId"], target_path=str(target))
            for name in ("a.txt", "b.txt"):
                (target / name).write_text("v2 " + name)
            second = E.send_project("demo", to_alias="alpha")
        with self.env("alpha"):
            E.receive_relay(second["relayId"])
            for name in ("a.txt", "b.txt"):
                (source / name).write_text("v3 " + name)
            third = E.send_project("demo", to_alias="beta")
        return target, third

    def test_interrupted_rollback_cannot_emit_receipt_and_can_finish_rollback(self):
        target, sent = self.pending_existing_receive()
        rid = sent["relayId"]
        with self.env("beta", {"RELAY_DESK_FAULT_POINT": "rx_before_archive"}), self.assertRaises(RuntimeError):
            E.receive_relay(rid)
        with self.env("beta", {"RELAY_DESK_FAULT_POINT": "tx_mid_rollback"}), self.assertRaises(RuntimeError):
            E.recover_relay(rid, action="rollback")
        with self.env("beta"):
            info = E.recover_relay(rid)
            self.assertEqual(info["phase"], W.PHASE_ROLLING_BACK)
            self.assertFalse(info["canResume"])
            with self.assertRaises(W.WorkspaceTxError) as caught:
                E.receive_relay(rid)
            self.assertEqual(caught.exception.code, "rollback_in_progress")
            self.assertFalse(list((self.cloud_dir / "v3" / "inbox").glob("*/%s/receipt.json" % rid)))
            self.assertTrue(E.recover_relay(rid, action="rollback")["ok"])
            self.assertEqual([(target / name).read_text() for name in ("a.txt", "b.txt")], ["v2 a.txt", "v2 b.txt"])
            self.assertTrue(E.receive_relay(rid)["ok"])
            self.assertEqual([(target / name).read_text() for name in ("a.txt", "b.txt")], ["v3 a.txt", "v3 b.txt"])

    def test_receipt_stage_only_allows_finishing_receive(self):
        _, sent = self.pending_existing_receive()
        with self.env("beta", {"RELAY_DESK_FAULT_POINT": "rx_before_receipt"}), self.assertRaises(RuntimeError):
            E.receive_relay(sent["relayId"])
        with self.env("beta"):
            with self.assertRaises(W.WorkspaceTxError) as caught:
                E.recover_relay(sent["relayId"], action="rollback")
            self.assertEqual(caught.exception.code, "rollback_not_allowed")
            self.assertTrue(E.receive_relay(sent["relayId"])["ok"])


class TestReviewedWorkspaceRegressions(unittest.TestCase):
    def test_directory_move_before_checkpoint_preserves_subsequent_edits(self):
        with tempfile.TemporaryDirectory() as td:
            fx = _build_folder_fixture(td, existing_target=False)
            with mock.patch.dict(os.environ, {"RELAY_DESK_FAULT_POINT": "tx_after_new_directory_move"}), self.assertRaises(RuntimeError):
                W.prepare_and_apply_workspace(fx["local_home"], "moved", "demo", "a" * 64, fx["staging"], fx["target"])
            changed = Path(fx["target"]) / "a.txt"
            changed.write_text("user edit after atomic move")
            info = W.inspect_transaction(fx["local_home"], "moved")
            self.assertTrue(info["canResume"])
            self.assertFalse(info["canRollback"])
            W.resume_transaction(fx["local_home"], "moved")
            self.assertEqual(changed.read_text(), "user edit after atomic move")

    def test_existing_format_one_journal_is_readable_and_unknown_format_is_rejected(self):
        for version in (1, 99):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as td:
                fx = _build_folder_fixture(td)
                with mock.patch.dict(os.environ, {"RELAY_DESK_FAULT_POINT": "tx_after_journal_before_apply"}), self.assertRaises(RuntimeError):
                    W.prepare_and_apply_workspace(fx["local_home"], "versioned", "demo", "a" * 64, fx["staging"], fx["target"])
                journal_file = fx["local_home"] / "transactions" / "versioned" / "journal.json"
                journal = json.loads(journal_file.read_text())
                journal["formatVersion"] = version
                journal["journalSha256"] = W._canonical_journal_sha256(journal)
                journal_file.write_text(json.dumps(journal))
                if version == 1:
                    W.resume_transaction(fx["local_home"], "versioned")
                    self.assertEqual(_snapshot_tree(fx["target"]), fx["expected_post_files"])
                    self.assertEqual(W.load_and_verify_journal(fx["local_home"], "versioned")["formatVersion"], 2)
                else:
                    with self.assertRaises(W.CorruptedRecoveryError):
                        W.resume_transaction(fx["local_home"], "versioned")
                    self.assertEqual(_snapshot_tree(fx["target"]), fx["pre_files"])

    def test_partial_copy_keeps_original_bytes_and_resumes(self):
        with tempfile.TemporaryDirectory() as td:
            fx = _build_folder_fixture(td)
            original = packager.shutil.copyfile
            failed = []
            def partial_copy(src, dst, *args, **kwargs):
                if Path(dst).parent.name.startswith(".relay-tx-write-"):
                    Path(dst).write_bytes(b"half a file")
                    failed.append(True)
                    self.assertEqual((Path(fx["target"]) / "a.txt").read_bytes(), b"old a content\n")
                    raise OSError("disk full")
                return original(src, dst, *args, **kwargs)
            with mock.patch.object(packager.shutil, "copyfile", partial_copy), self.assertRaises(OSError):
                W.prepare_and_apply_workspace(fx["local_home"], "partial", "demo", "a" * 64, fx["staging"], fx["target"])
            self.assertTrue(failed)
            self.assertTrue(W.inspect_transaction(fx["local_home"], "partial")["canResume"])
            W.resume_transaction(fx["local_home"], "partial")
            self.assertEqual(_snapshot_tree(fx["target"]), fx["expected_post_files"])

    def test_process_death_during_copy_allows_resume_and_rollback(self):
        for action in ("resume", "rollback"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as td:
                fx = _build_folder_fixture(td)
                code = """
import os, sys
from pathlib import Path
from relaydesk import packager, workspace_tx as W
original = packager.shutil.copyfile
def interrupted(src, dst, *a, **kw):
    if Path(dst).parent.name.startswith('.relay-tx-write-'):
        Path(dst).write_bytes(b'half a file')
        os._exit(137)
    return original(src, dst, *a, **kw)
packager.shutil.copyfile = interrupted
W.prepare_and_apply_workspace(Path(sys.argv[1]), 'deadcopy', 'demo', 'a'*64, sys.argv[2], sys.argv[3])
"""
                result = subprocess.run([sys.executable, "-B", "-c", code, str(fx["local_home"]), fx["staging"], fx["target"]],
                                        cwd=str(Path(__file__).resolve().parents[1]), capture_output=True)
                self.assertEqual(result.returncode, 137, result.stderr)
                self.assertEqual((Path(fx["target"]) / "a.txt").read_bytes(), b"old a content\n")
                getattr(W, action + "_transaction")(fx["local_home"], "deadcopy")
                self.assertEqual(_snapshot_tree(fx["target"]), fx["expected_post_files"] if action == "resume" else fx["pre_files"])

    def test_rollback_process_death_preserves_git_and_user_edits(self):
        with tempfile.TemporaryDirectory() as td:
            fx = _build_git_fixture(td)
            W.prepare_and_apply_workspace(fx["local_home"], "rollbackkill", "demo", "a" * 64, fx["staging"], fx["target"])
            env = dict(os.environ, RELAY_DESK_FAULT_POINT="tx_mid_rollback", RELAY_DESK_FAULT_MODE="sigkill")
            code = "from pathlib import Path; import sys; from relaydesk import workspace_tx as W; W.rollback_transaction(Path(sys.argv[1]), 'rollbackkill')"
            result = subprocess.run([sys.executable, "-B", "-c", code, str(fx["local_home"])], env=env,
                                    cwd=str(Path(__file__).resolve().parents[1]), capture_output=True)
            self.assertIn(result.returncode, (-9, 137), result.stderr)
            with self.assertRaises(W.WorkspaceTxError):
                W.resume_transaction(fx["local_home"], "rollbackkill")
            edited = Path(fx["target"]) / "after-crash.txt"
            edited.write_text("user edit after rollback interruption")
            with self.assertRaises(W.RecoveryConflictError):
                W.rollback_transaction(fx["local_home"], "rollbackkill")
            self.assertEqual(edited.read_text(), "user edit after rollback interruption")
            edited.unlink()
            W.rollback_transaction(fx["local_home"], "rollbackkill")
            self.assertEqual(_snapshot_tree(fx["target"]), fx["pre_files"])
            self.assertEqual(_capture_git_state(fx["target"]), fx["pre_git"])
