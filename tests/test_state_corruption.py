# -*- coding: utf-8 -*-
"""Phase A: local state that was never created vs local state that is corrupt.

A corrupt identity, cloud config, relay state, device config or recovery record
must stop the operation with a coded error and leave the file byte-for-byte
unchanged. State that was never created keeps working as before.
"""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest import mock

from relaydesk import crypto
from relaydesk import ledger as L
from relaydesk import relay_engine as E
from relaydesk import storage
from relaydesk import validation as V
from relaydesk import workspace_tx as W
from test_input_validation import changed_paths, tree_state
from test_multidevice_network import MultiDeviceTestBase

CORRUPTIONS = {
    "garbage": b"{not json",
    "empty": b"",
    "whitespace": b"  \n",
    "truncated": b'{"version": 3, "peers": {',
    "list": b"[1, 2]",
    "string": b'"text"',
    "bad_utf8": b"\xff\xfe\x00{",
}


def file_fingerprint(path: Path):
    st = os.lstat(str(path))
    data = os.readlink(str(path)).encode() if stat.S_ISLNK(st.st_mode) else path.read_bytes()
    return st.st_ino, data


class TestReadLocalJsonObject(unittest.TestCase):
    def test_missing_vs_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.assertIsNone(V.read_local_json_object(path, "k", "label"))
            path.write_text('{"a": 1}', encoding="utf-8")
            self.assertEqual(V.read_local_json_object(path, "k", "label"), {"a": 1})
            path.write_bytes(b"\xef\xbb\xbf{\"bom\": true}")
            self.assertEqual(V.read_local_json_object(path, "k", "label"), {"bom": True})
            for name, raw in CORRUPTIONS.items():
                path.write_bytes(raw)
                with self.subTest(case=name), self.assertRaises(V.LocalStateCorruptError) as caught:
                    V.read_local_json_object(path, "k", "label")
                self.assertEqual(caught.exception.code, "local_state_corrupted")
                self.assertEqual(caught.exception.category, V.CATEGORY_NEEDS_ACTION)
                self.assertEqual(caught.exception.path, str(path))
            path.unlink()
            path.mkdir()
            with self.assertRaises(V.LocalStateCorruptError):
                V.read_local_json_object(path, "k", "label")
            path.rmdir()
            if os.name != "nt":
                os.symlink(str(Path(tmp) / "nowhere.json"), str(path))
                with self.assertRaises(V.LocalStateCorruptError):
                    V.read_local_json_object(path, "k", "label")


class NetworkStateFixture(MultiDeviceTestBase):
    def setUp(self) -> None:
        MultiDeviceTestBase.setUp(self)
        self.init_device("alpha")
        self.init_device("beta")
        self.pair_pair("alpha", "beta")
        self.home = self.devices["alpha"]["home"]

    def corrupt(self, name: str, raw: bytes) -> Path:
        path = self.home / name
        path.write_bytes(raw)
        return path


class TestRelayStateCorruption(NetworkStateFixture):
    def test_corrupt_relay_state_is_never_replaced(self) -> None:
        for case, raw in list(CORRUPTIONS.items()) + [
            ("wrong_type", b'{"version": 3, "processedDeliveries": []}'),
            ("wrong_type_str", b'{"version": 3, "versionGraph": "x"}'),
        ]:
            with self.subTest(case=case):
                path = self.corrupt("relay_state.json", raw)
                before = file_fingerprint(path)
                with self.assertRaises(V.LocalStateCorruptError) as caught:
                    E._update_relay_state(self.home, lambda st: st.setdefault("processedDeliveries", {}).update(x={}))
                self.assertEqual(caught.exception.kind, "relay_state")
                with self.env("alpha"), self.assertRaises(V.LocalStateCorruptError):
                    E.sync_network()
                with self.env("alpha"), self.assertRaises(V.LocalStateCorruptError):
                    E.recover_relay("r_20261118_abcdefgh", action="status")
                self.assertEqual(file_fingerprint(path), before)
                with self.assertRaises(V.LocalStateCorruptError):
                    E.load_relay_state(self.home)

    def test_null_sections_and_missing_file_still_work(self) -> None:
        path = self.home / "relay_state.json"
        path.write_text('{"version": 3, "processedDeliveries": null}', encoding="utf-8")
        self.assertEqual(E._load_relay_state(self.home)["processedDeliveries"], {})
        path.unlink()
        self.assertIsNone(E.load_relay_state(self.home))
        self.assertFalse(path.exists())
        E._update_relay_state(self.home, lambda st: st["processedDeliveries"].update(d1={"x": 1}))
        self.assertEqual(E._load_relay_state(self.home)["processedDeliveries"], {"d1": {"x": 1}})

    def test_v2_relay_state_still_migrates(self) -> None:
        path = self.home / "relay_state.json"
        path.write_text(json.dumps({"pendingSends": {}, "sentRelays": {}, "appliedReceives": {},
                                    "receivedRelays": {}, "projectBaselines": {}}), encoding="utf-8")
        state = E._load_relay_state(self.home)
        self.assertEqual(state["version"], 3)
        self.assertTrue((self.home / "relay_state.v2.backup.json").exists())


class TestCloudConfigCorruption(NetworkStateFixture):
    def test_corrupt_cloud_config_is_never_replaced(self) -> None:
        for case, raw in list(CORRUPTIONS.items()) + [
            ("peers_list", b'{"version": 3, "provider": "local", "root": "/x", "peers": []}'),
            ("peer_not_object", b'{"version": 3, "provider": "local", "root": "/x", "peers": {"dev_a": 1}}'),
            ("root_not_str", b'{"version": 3, "provider": "local", "root": 5, "peers": {}}'),
            ("paired_bad_id", b'{"version": 3, "provider": "local", "root": "/x", '
                              b'"peers": {"../x": {"deviceId": "../x", "paired": true, "status": "paired"}}}'),
        ]:
            with self.subTest(case=case):
                path = self.corrupt("cloud.json", raw)
                before = file_fingerprint(path)
                with self.env("alpha"), self.assertRaises(V.LocalStateCorruptError) as caught:
                    E.pair_device(root=str(self.cloud_dir), provider_type="local")
                self.assertEqual(caught.exception.kind, "cloud")
                with self.env("alpha"), self.assertRaises(V.LocalStateCorruptError):
                    E.cloud_status()
                with self.env("alpha"), self.assertRaises(V.LocalStateCorruptError):
                    E.sync_network()
                self.assertEqual(file_fingerprint(path), before)
                with self.assertRaises(V.LocalStateCorruptError):
                    E.load_cloud_config(self.home)

    def test_missing_cloud_config_is_not_configured(self) -> None:
        (self.home / "cloud.json").unlink()
        self.assertIsNone(E.load_cloud_config(self.home))
        with self.env("alpha"):
            self.assertFalse(E.cloud_status()["configured"])
            with self.assertRaises(E.NotConfiguredError):
                E.sync_network()
        self.assertFalse((self.home / "cloud.json").exists())

    def test_unconfirmed_peer_with_unusable_id_is_dropped_not_fatal(self) -> None:
        cloud = json.loads((self.home / "cloud.json").read_text(encoding="utf-8"))
        cloud["peers"]["../x"] = {"deviceId": "../x", "paired": False, "status": "discovered"}
        (self.home / "cloud.json").write_text(json.dumps(cloud), encoding="utf-8")
        loaded = E.load_cloud_config(self.home)
        self.assertNotIn("../x", loaded["peers"])
        self.assertEqual(len([p for p in loaded["peers"].values() if p["paired"]]), 1)

    def test_v2_cloud_config_still_migrates(self) -> None:
        cloud = json.loads((self.home / "cloud.json").read_text(encoding="utf-8"))
        peer = next(iter(cloud["peers"].values()))
        v2 = {"provider": "local", "root": cloud["root"], "remote": "", "configPath": "", "peer": peer,
              "paired": True, "pairFingerprint": peer.get("pairFingerprint", ""), "pairedAt": ""}
        (self.home / "cloud.json").write_text(json.dumps(v2), encoding="utf-8")
        loaded = E._load_cloud(self.home)
        self.assertIn(peer["deviceId"], loaded["peers"])
        self.assertTrue((self.home / "cloud.v2.backup.json").exists())


class TestIdentityCorruption(NetworkStateFixture):
    def test_corrupt_identity_is_never_replaced(self) -> None:
        ident_path = self.home / "identity.json"
        good = json.loads(ident_path.read_text(encoding="utf-8"))
        cases = dict(CORRUPTIONS)
        cases["missing_key"] = json.dumps({k: v for k, v in good.items() if k != "boxPrivateKey"}).encode()
        cases["bad_key_len"] = json.dumps(dict(good, signPrivateKey="AAAA")).encode()
        cases["bad_device_id"] = json.dumps(dict(good, deviceId="../dev")).encode()
        for case, raw in cases.items():
            with self.subTest(case=case):
                ident_path.write_bytes(raw)
                before = file_fingerprint(ident_path)
                with self.assertRaises(crypto.IdentityCorruptError) as caught:
                    crypto.ensure_identity(self.home, alias="alpha")
                self.assertIsInstance(caught.exception, crypto.CryptoError)
                self.assertIsInstance(caught.exception, V.LocalStateCorruptError)
                self.assertEqual(caught.exception.code, "local_state_corrupted")
                with self.assertRaises(crypto.IdentityCorruptError):
                    crypto.load_identity(self.home)
                with self.env("alpha"), self.assertRaises(crypto.IdentityCorruptError):
                    E.cloud_status()
                with self.env("alpha"), self.assertRaises(crypto.IdentityCorruptError):
                    E.list_devices()
                with self.env("alpha"), self.assertRaises(crypto.IdentityCorruptError):
                    E.sync_network()
                self.assertEqual(file_fingerprint(ident_path), before)

    @unittest.skipIf(os.name == "nt", "POSIX symlinks")
    def test_dangling_identity_symlink_is_not_treated_as_missing(self) -> None:
        ident_path = self.home / "identity.json"
        ident_path.unlink()
        os.symlink(str(self.root / "unmounted" / "identity.json"), str(ident_path))
        before = file_fingerprint(ident_path)
        with self.assertRaises(crypto.IdentityCorruptError):
            crypto.ensure_identity(self.home, alias="alpha")
        self.assertTrue(os.path.islink(str(ident_path)))
        self.assertEqual(file_fingerprint(ident_path), before)

    def test_read_only_loader_never_writes(self) -> None:
        ident_path = self.home / "identity.json"
        before = file_fingerprint(ident_path)
        mtime = os.stat(str(ident_path)).st_mtime_ns
        ident = crypto.load_identity(self.home)
        self.assertEqual(ident["deviceId"], json.loads(before[1])["deviceId"])
        self.assertEqual(file_fingerprint(ident_path), before)
        self.assertEqual(os.stat(str(ident_path)).st_mtime_ns, mtime)
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(crypto.load_identity(empty))
            self.assertEqual(os.listdir(empty), [])

    def test_unchanged_identity_is_not_rewritten_by_paired_context(self) -> None:
        ident_path = self.home / "identity.json"
        before = file_fingerprint(ident_path)
        with self.env("alpha"):
            E._require_paired_context()
            E._require_paired_context()
        self.assertEqual(file_fingerprint(ident_path), before)

    def test_missing_identity_is_still_created(self) -> None:
        with tempfile.TemporaryDirectory() as fresh:
            ident = crypto.ensure_identity(fresh, alias="x")
            self.assertTrue(ident["deviceId"].startswith("dev_"))
            self.assertTrue((Path(fresh) / "identity.json").is_file())


class TestDeviceConfigCorruption(unittest.TestCase):
    def test_corrupt_config_does_not_fall_back_to_the_tool_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.json").write_bytes(b"{broken")
            env = {k: v for k, v in os.environ.items() if k not in ("RELAY_DESK_LEDGER", "RELAY_DESK_DEVICE")}
            env["RELAY_DESK_HOME"] = str(home)
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(V.LocalStateCorruptError) as caught:
                    E._resolve_ctx(local_home=home)
                self.assertEqual(caught.exception.kind, "config")
                with self.assertRaises(L.LedgerError) as caught_l:
                    L.load_local()
                self.assertIsInstance(caught_l.exception, V.LocalStateCorruptError)
                with self.assertRaises(V.LocalStateCorruptError):
                    E._resolve_ctx()
            self.assertEqual((home / "config.json").read_bytes(), b"{broken")

    def test_missing_config_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"RELAY_DESK_HOME": tmp}):
                self.assertEqual(L.load_local(), {})


class TestRecoveryRecordCorruption(NetworkStateFixture):
    def setUp(self) -> None:
        NetworkStateFixture.setUp(self)
        source = self.root / "folder_alpha"
        source.mkdir()
        (source / "a.txt").write_text("v1\n")

        def register(ledger: Dict[str, Any]) -> None:
            ledger["projects"].append(L.norm_project({
                "id": "demo", "name": "demo", "device": "alpha", "tool": "claude",
                "paths": {"alpha": str(source)},
            }, ledger["config"]))

        L.update(self.devices["alpha"]["led"], register)
        with self.env("alpha"):
            self.sent = E.send_project("demo", to_alias="beta")
        self.rid = self.sent["relayId"]
        self.beta_home = self.devices["beta"]["home"]
        self.work = self.root / "work_beta"
        with self.env("beta", {"RELAY_DESK_FAULT_POINT": "rx_before_archive"}):
            with self.assertRaises(RuntimeError):
                E.receive_relay(self.rid, target_path=str(self.work))
        self.assertEqual(W.load_and_verify_journal(self.beta_home, self.rid)["phase"], W.PHASE_WORKSPACE_COMMITTED)

    def receipt_key(self) -> str:
        beta_did = json.loads((self.beta_home / "identity.json").read_text(encoding="utf-8"))["deviceId"]
        return "v3/inbox/%s/%s/receipt.json" % (beta_did, self.rid)

    def test_corrupt_meta_stops_resume_before_any_write(self) -> None:
        meta = self.beta_home / "transactions" / self.rid / "meta.json"
        for case, raw in list(CORRUPTIONS.items()) + [("other_relay", b'{"relayId": "r_other", "extra": {}}'),
                                                       ("extra_list", b'{"extra": []}')]:
            with self.subTest(case=case):
                meta.write_bytes(raw)
                before = tree_state(self.root)
                with self.env("beta"), self.assertRaises(W.CorruptedRecoveryError) as caught:
                    E.receive_relay(self.rid)
                self.assertEqual(caught.exception.code, "recovery_corrupted")
                self.assertEqual(caught.exception.category, V.CATEGORY_NEEDS_ACTION)
                self.assertEqual(changed_paths(before, tree_state(self.root)), [])
                provider = storage.LocalDirProvider(str(self.cloud_dir))
                self.assertIsNone(provider.stat(self.receipt_key()))
                self.assertIsNone(provider.stat("relays/%s/receipt.json" % self.rid))
        meta.unlink()
        with self.env("beta"), self.assertRaises(W.CorruptedRecoveryError):
            E.receive_relay(self.rid)

    def test_corrupt_meta_stops_rollback_before_any_write(self) -> None:
        meta = self.beta_home / "transactions" / self.rid / "meta.json"
        meta.write_bytes(b"{broken")
        before = tree_state(self.root)
        with self.env("beta"), self.assertRaises(W.CorruptedRecoveryError):
            E.recover_relay(self.rid, action="rollback")
        self.assertEqual(changed_paths(before, tree_state(self.root)), [])
        self.assertEqual(W.load_and_verify_journal(self.beta_home, self.rid)["phase"], W.PHASE_WORKSPACE_COMMITTED)

    def test_intact_meta_still_resumes(self) -> None:
        with self.env("beta"):
            res = E.receive_relay(self.rid)
        self.assertTrue(res["ok"])
        self.assertIsNotNone(storage.LocalDirProvider(str(self.cloud_dir)).stat(self.receipt_key()))


class TestSyncCheckpointCorruption(NetworkStateFixture):
    def test_corrupt_checkpoint_is_classified(self) -> None:
        source = self.root / "folder_alpha"
        source.mkdir()
        (source / "a.txt").write_text("v1\n")

        def register(ledger: Dict[str, Any]) -> None:
            ledger["projects"].append(L.norm_project({
                "id": "demo", "name": "demo", "device": "alpha", "tool": "claude",
                "paths": {"alpha": str(source)},
            }, ledger["config"]))

        L.update(self.devices["alpha"]["led"], register)
        with self.env("alpha"):
            published = E.publish_project("demo")
        checkpoints = list((self.home / "sync_publications").glob("*/ready.json"))
        self.assertEqual(len(checkpoints), 1)
        checkpoints[0].write_bytes(b"{broken")
        cdir = self.home / "cache" / "snapshots" / published["snapshotId"]
        snap = json.loads((cdir / "snapshot.json").read_text(encoding="utf-8"))
        with self.env("alpha"):
            base, _lp, _lc, _led, here_id, cloud, identity, provider = E._require_paired_context()
        peer = next(p for p in cloud["peers"].values() if p["paired"])
        with self.assertRaises(E.RelayError) as caught:
            E._upload_v3_sync_delivery(provider, identity, here_id, peer, published["deliveredTo"][0]["deliveryId"],
                                       str(cdir / "payload.tar.gz"), snap, {}, local_home=base)
        self.assertEqual(caught.exception.code, "checkpoint_corrupted")


if __name__ == "__main__":
    unittest.main()
