# -*- coding: utf-8 -*-
"""Phase A: identifier validation, cross-field consistency and write-path containment.

Every malicious scenario here is signed by a paired, trusted key: signatures
verify, so only the post-signature checks can stop it. Each test snapshots the
whole temporary tree before and after and asserts that nothing outside the
allowed roots was added or modified.
"""
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from unittest import mock

from relaydesk import crypto
from relaydesk import ledger as L
from relaydesk import packager
from relaydesk import relay_engine as E
from relaydesk import storage
from relaydesk import validation as V
from relaydesk import workspace_tx as W
from test_multidevice_network import MultiDeviceTestBase


# ---------------------------------------------------------------- tree snapshots


def tree_state(root: Path) -> Dict[str, Any]:
    """Map every entry under root (no symlink following) to its type and content digest."""
    out: Dict[str, Any] = {}
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        for name in list(dirnames) + list(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, str(root)).replace(os.sep, "/")
            if os.path.islink(full):
                out[rel] = ("link", os.readlink(full))
            elif os.path.isdir(full):
                out[rel] = ("dir",)
            else:
                with open(full, "rb") as fh:
                    out[rel] = ("file", hashlib.sha256(fh.read()).hexdigest())
    return out


def changed_paths(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def outside(paths: Iterable[str], allowed_prefixes: Iterable[str]) -> List[str]:
    allowed = tuple(p.rstrip("/") + "/" for p in allowed_prefixes)
    return [p for p in paths if not (p + "/").startswith(allowed)]


def make_payload(
    tar_path: str,
    project_id: str = "demo",
    files: Optional[Dict[str, bytes]] = None,
    folder_entries: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    target_os: str = "",
) -> str:
    """Write a folder-mode payload tar by hand (lets tests express manifests a sender would refuse)."""
    files = dict(files if files is not None else {"a.txt": b"hello\n"})
    archive_files: Dict[str, Dict[str, Any]] = {}
    blobs: Dict[str, bytes] = {}
    entries: List[Dict[str, Any]] = []
    for idx, (rel, data) in enumerate(sorted(files.items())):
        member = "blobs/folder_%04d" % idx
        blobs[member] = data
        entries.append({"path": rel, "mode": "100644", "archiveMember": member,
                        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    for idx, entry in enumerate(folder_entries or []):
        member = "blobs/extra_%04d" % idx
        data = entry.pop("data")
        blobs[member] = data
        entry.update({"archiveMember": member, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        entries.append(entry)
    entries.sort(key=lambda e: e["path"])
    meta = {"id": project_id, "name": project_id, "tool": "claude", "lastTask": "", "nextAction": "",
            "sessions": [], "paths": {}, "pathsByDeviceId": {}, "relayId": "", "parentRelayId": ""}
    meta.update(metadata or {})
    blobs["metadata.json"] = json.dumps(meta).encode("utf-8")
    blobs["conversations.json"] = json.dumps({"projectId": meta["id"], "entries": []}).encode("utf-8")
    for member, data in blobs.items():
        archive_files[member] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    manifest = {
        "formatVersion": 1, "mode": "folder", "targetOs": target_os, "hasCommits": False,
        "branch": "", "headRef": "", "headCommit": "", "headAncestors": [], "refs": {}, "remotes": {},
        "stagedCount": 0, "unstagedCount": 0, "untrackedCount": 0, "fileCount": len(entries),
        "excludedFiles": [], "allPaths": sorted(e["path"] for e in entries),
        "stagedEntries": [], "unstagedEntries": [], "untrackedEntries": [],
        "folderEntries": entries, "folderHashes": {e["path"]: e["sha256"] for e in entries},
        "archiveFiles": archive_files,
    }
    blobs["payload_manifest.json"] = json.dumps(manifest).encode("utf-8")
    with tarfile.open(tar_path, "w:gz") as tf:
        for member, data in blobs.items():
            info = tarfile.TarInfo(member)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return tar_path


class NetworkFixture(MultiDeviceTestBase):
    """alpha (trusted sender) and beta (receiver), paired over a local cloud directory."""

    def setUp(self) -> None:
        MultiDeviceTestBase.setUp(self)
        self.init_device("alpha")
        self.init_device("beta")
        self.pair_pair("alpha", "beta")
        self.outside_dir = self.root / "outside"
        self.outside_dir.mkdir()
        (self.outside_dir / "keep.txt").write_text("outside data\n")

    def identity(self, alias: str) -> Dict[str, Any]:
        return json.loads((self.devices[alias]["home"] / "identity.json").read_text(encoding="utf-8"))

    def forge(
        self,
        delivery_id: str,
        tar_path: str,
        purpose: str = "sync",
        snap: Optional[Dict[str, Any]] = None,
        ready: Optional[Dict[str, Any]] = None,
        meta: Optional[Dict[str, Any]] = None,
        sender: str = "alpha",
        recipient: str = "beta",
        key_delivery_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Publish a delivery signed by the trusted sender with arbitrary field values."""
        ident = self.identity(sender)
        r_ident = self.identity(recipient)
        r_desc = crypto.public_descriptor_v3(r_ident)
        with open(tar_path, "rb") as fh:
            data = fh.read()
        snapshot = {
            "formatVersion": 3, "projectId": "demo", "projectName": "demo",
            "versionId": "snap_20261118_forged01", "authorDeviceId": ident["deviceId"],
            "authorAlias": sender, "parentVersionIds": [], "parentRelayId": "", "relayId": "",
            "contentSha256": hashlib.sha256(data).hexdigest(), "contentSize": len(data),
            "createdAt": "2026-11-18T00:00:00.000Z",
        }
        snapshot.update(snap or {})
        signed_snap = crypto.sign_json(snapshot, ident["signPrivateKey"], signer_device_id=ident["deviceId"])
        meta_plain = {"projectId": snapshot["projectId"], "projectName": "demo", "lastTask": "",
                      "nextAction": "", "branch": "", "dirty": 0, "sessionCount": 0}
        meta_plain.update(meta or {})
        prefix = "v3/inbox/%s/%s/" % (r_ident["deviceId"], key_delivery_id or delivery_id)
        provider = storage.LocalDirProvider(str(self.cloud_dir))
        with tempfile.TemporaryDirectory() as td:
            enc = os.path.join(td, "package.enc")
            envelope = crypto.encrypt_package(tar_path, enc, ident, r_desc)
            body = dict(envelope)
            body.update({
                "formatVersion": 3, "purpose": purpose, "deliveryId": delivery_id, "relayId": delivery_id,
                "senderDeviceId": ident["deviceId"], "senderAlias": sender, "fromAlias": sender,
                "fromDeviceId": ident["deviceId"], "toAlias": recipient, "toDeviceId": r_ident["deviceId"],
                "authorizedDeviceId": r_ident["deviceId"] if purpose == "handoff" else "",
                "parentRelayId": "", "isForwarded": False, "createdAt": "2026-11-18T00:00:00.000Z",
                "packageKey": prefix + "package.enc", "snapshot": signed_snap,
                "recipientKeyFingerprint": r_desc["keyFingerprint"],
                "meta": crypto.encrypt_meta(meta_plain, ident, r_desc),
            })
            body.update(ready or {})
            signed_ready = crypto.sign_json(body, ident["signPrivateKey"], signer_device_id=ident["deviceId"])
            ready_path = os.path.join(td, "ready.json")
            with open(ready_path, "w", encoding="utf-8") as fh:
                json.dump(signed_ready, fh)
            provider.upload(enc, prefix + "package.enc")
            provider.upload(ready_path, prefix + "ready.json")
        return signed_snap

    def payload(self, name: str = "payload.tar.gz", **kw) -> str:
        return make_payload(str(self.root / name), **kw)

    def beta_state(self) -> Dict[str, Any]:
        return E._load_relay_state(self.devices["beta"]["home"])

    def assert_only_inside(self, before: Dict[str, Any], allowed: Iterable[str]) -> List[str]:
        changes = changed_paths(before, tree_state(self.root))
        stray = outside(changes, allowed)
        self.assertEqual(stray, [], "files outside the allowed roots changed: %r" % stray)
        return changes

    def assert_nothing_cached(self, changes: List[str]) -> None:
        for prefix in ("home_beta/cache/snapshots/", "home_beta/archives/", "home_beta/transactions/"):
            self.assertEqual([c for c in changes if c.startswith(prefix)], [], prefix)


# ---------------------------------------------------------------- unit: identifiers


class TestIdentifierRules(unittest.TestCase):
    GOOD_TOKENS = ("snap_20261118_abcd1234", "r_20261118_k3j9x2ab", "dev_0123456789abcdef", "dev_1", "v1",
                   "fwd_snap_20261118_abcd1234_abcdef", "sync_snap_x_y", "A-b.c_d")
    BAD_TOKENS = ("", ".", "..", "../x", "a/b", "a\\b", "C:x", "/abs", "a\x00b", "a\nb", "-x", ".hidden",
                  "a..b", "trail.", "CON", "con.txt", "nul", "COM1", "x" * 129, "é", "ａ", "a b",
                  "\u202ea", "a\u200b")

    def test_tokens(self) -> None:
        for good in self.GOOD_TOKENS:
            self.assertEqual(V.validate_token(good, "id"), good)
        for bad in self.BAD_TOKENS:
            with self.subTest(bad=bad), self.assertRaises(V.InvalidIdentifierError):
                V.validate_token(bad, "id")
        for bad_type in (None, 1, b"x", ["x"], {"x": 1}, True):
            with self.assertRaises(V.InvalidIdentifierError):
                V.validate_token(bad_type, "id")

    def test_project_ids_accept_existing_forms(self) -> None:
        for good in ("p_abcd1234", "demo", "animal-sim", "zh-proj", "scala-cw", "s_1", "中文项目",
                     "My Project 2", "Proj.v2", "café"):
            self.assertEqual(V.validate_project_id(good), good)

    def test_project_ids_reject_paths_and_unicode_tricks(self) -> None:
        nfd = "cafe\u0301"
        for bad in ("", ".", "..", "../x", "a/b", "a\\b", "C:", "x\x00", "tab\tx", "a\nb", " lead",
                    "trail ", "trail.", ".dot", "-dash", "a..b", "CON", "aux.proj", nfd,
                    "\uff0e\uff0e", "\uff0e\uff0e\uff0fx", "x\uff0fy", "ｐ1", "\u202eevil", "zero\u200bwidth",
                    "x" * 129):
            with self.subTest(bad=bad), self.assertRaises(V.InvalidIdentifierError):
                V.validate_project_id(bad)
        with self.assertRaises(V.InvalidIdentifierError):
            V.validate_project_id(None)

    def test_parent_lists(self) -> None:
        self.assertEqual(V.validate_parent_version_ids(None, "v2"), [])
        self.assertEqual(V.validate_parent_version_ids(["v1"], "v2"), ["v1"])
        for bad in ("v1", ["v2"], ["v1", "v1"], ["../v1"], [1], ["v%d" % i for i in range(65)], {"v1": 1}):
            with self.subTest(bad=bad), self.assertRaises(V.InvalidIdentifierError):
                V.validate_parent_version_ids(bad, "v2")

    def test_signed_snapshot_validation_rejects_bad_ids_after_signature(self) -> None:
        sk_home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, sk_home)
        ident = crypto.ensure_identity(sk_home)
        base = {"projectId": "demo", "versionId": "snap_1", "authorDeviceId": ident["deviceId"],
                "parentVersionIds": [], "contentSha256": "0" * 64}
        good = crypto.sign_json(base, ident["signPrivateKey"], signer_device_id=ident["deviceId"])
        self.assertEqual(crypto.verify_snapshot(good, ident["verifyPublicKey"])["versionId"], "snap_1")
        for field, value in (("versionId", "../../x"), ("projectId", "a/b"), ("parentVersionIds", ["snap_1"]),
                             ("parentVersionIds", "snap_0"), ("relayId", "../r"), ("parentRelayId", "a b")):
            body = dict(base)
            body[field] = value
            signed = crypto.sign_json(body, ident["signPrivateKey"], signer_device_id=ident["deviceId"])
            with self.subTest(field=field, value=value), self.assertRaises(crypto.TamperError) as caught:
                crypto.verify_snapshot(signed, ident["verifyPublicKey"])
            self.assertEqual(getattr(caught.exception, "code", ""), "invalid_identifier")
            with self.assertRaises(crypto.CryptoError):
                crypto.sign_snapshot(body, ident["signPrivateKey"], author_device_id=ident["deviceId"])


# ---------------------------------------------------------------- unit: paths


class TestPathRules(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "target"
        self.target.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()

    def test_rel_path_rules(self) -> None:
        self.assertEqual(V.normalize_rel_path("a\\b/c"), "a/b/c")
        self.assertEqual(V.normalize_rel_path("cafe\u0301/x"), "café/x")
        for bad in ("", "/etc/passwd", "\\\\server\\share", "C:\\x", "c:x", "a/../b", "..", "a/./b",
                    "a//b", "a\x00b", "..\\..\\x", "a/"):
            with self.subTest(bad=bad), self.assertRaises(V.PathEscapeError):
                V.normalize_rel_path(bad)

    def test_fullwidth_dots_are_plain_names_not_traversal(self) -> None:
        dest = V.safe_join(str(self.target), "\uff0e\uff0e/x.txt")
        self.assertTrue(dest.startswith(str(self.target) + os.sep))
        os.makedirs(os.path.dirname(dest))
        Path(dest).write_text("x")
        self.assertTrue((self.target / "\uff0e\uff0e" / "x.txt").is_file())

    @unittest.skipIf(os.name == "nt", "POSIX symlinks")
    def test_symlinked_parent_is_rejected(self) -> None:
        os.symlink(str(self.outside), str(self.target / "link"))
        with self.assertRaises(V.PathEscapeError) as caught:
            V.safe_join(str(self.target), "link/evil.txt")
        self.assertEqual(caught.exception.code, "symlink_in_path")
        # Final component may be a symlink: it is replaced, never written through.
        self.assertTrue(V.safe_join(str(self.target), "link").endswith("link"))
        with self.assertRaises(V.PathEscapeError):
            V.safe_join(str(self.target), "link", check_final=True)
        nested = self.target / "real"
        nested.mkdir()
        os.symlink(str(self.outside), str(nested / "deep"))
        with self.assertRaises(V.PathEscapeError):
            V.safe_join(str(self.target), "real/deep/x/y.txt")

    @unittest.skipIf(os.name == "nt", "POSIX symlinks")
    def test_root_reached_through_symlink_is_allowed(self) -> None:
        alias = self.root / "alias"
        os.symlink(str(self.target), str(alias))
        self.assertTrue(V.safe_join(str(alias), "sub/x.txt").endswith(os.path.join("sub", "x.txt")))
        with self.assertRaises(V.PathEscapeError):
            V.ensure_contained(str(self.target), str(self.outside / "x"))

    def test_symlink_targets(self) -> None:
        self.assertEqual(V.check_symlink_target("a/link", "../b.txt"), "../b.txt")
        for bad in ("/etc/passwd", "C:\\Windows", "../../x", "..\\..\\x", "a/../../..", "", "x\x00"):
            with self.subTest(bad=bad), self.assertRaises(V.PathEscapeError):
                V.check_symlink_target("a/link", bad)


# ---------------------------------------------------------------- sync entry point


class TestMaliciousSyncDeliveries(NetworkFixture):
    ALLOWED = ("home_beta", "cloud_shared")

    def sync_beta(self) -> Dict[str, Any]:
        with self.env("beta"):
            return E.sync_network()

    def assert_rejected(self, result: Dict[str, Any], delivery_id: str, before: Dict[str, Any]) -> None:
        self.assertEqual([s for s in result["syncedSnapshots"] if s["deliveryId"] == delivery_id], [])
        self.assertTrue(any(e.get("deliveryId") == delivery_id for e in result["errors"]), result["errors"])
        changes = self.assert_only_inside(before, self.ALLOWED)
        self.assert_nothing_cached(changes)
        self.assertNotIn(delivery_id, self.beta_state()["processedDeliveries"])

    def test_version_id_traversal_never_writes_outside(self) -> None:
        tar = self.payload()
        self.forge("sync_traversal", tar, snap={"versionId": "../../../escaped"})
        before = tree_state(self.root)
        result = self.sync_beta()
        self.assertFalse((self.root / "escaped").exists())
        self.assert_rejected(result, "sync_traversal", before)

    def test_project_id_with_separator_is_rejected_before_cache(self) -> None:
        tar = self.payload(project_id="demo")
        self.forge("sync_badproj", tar, snap={"projectId": "../evil"}, meta={"projectId": "../evil"})
        before = tree_state(self.root)
        result = self.sync_beta()
        self.assert_rejected(result, "sync_badproj", before)

    def test_snapshot_project_must_match_payload_and_preview(self) -> None:
        tar = self.payload(project_id="demo")
        self.forge("sync_mismatch", tar, snap={"projectId": "victim"}, meta={"projectId": "victim"})
        before = tree_state(self.root)
        result = self.sync_beta()
        self.assert_rejected(result, "sync_mismatch", before)
        self.assertNotIn("victim", self.beta_state()["versionGraph"])

    def test_preview_project_must_match_snapshot(self) -> None:
        tar = self.payload(project_id="demo")
        self.forge("sync_meta_mismatch", tar, meta={"projectId": "victim"})
        before = tree_state(self.root)
        result = self.sync_beta()
        self.assert_rejected(result, "sync_meta_mismatch", before)

    def test_self_referential_parent_is_rejected(self) -> None:
        tar = self.payload()
        self.forge("sync_selfparent", tar, snap={"versionId": "snap_self", "parentVersionIds": ["snap_self"]})
        before = tree_state(self.root)
        result = self.sync_beta()
        self.assert_rejected(result, "sync_selfparent", before)

    def test_ready_relay_id_must_match_key_path(self) -> None:
        tar = self.payload()
        self.forge("sync_original", tar, key_delivery_id="sync_replayed")
        before = tree_state(self.root)
        result = self.sync_beta()
        self.assert_rejected(result, "sync_replayed", before)

    def test_ready_recipient_must_be_this_device(self) -> None:
        tar = self.payload()
        self.forge("sync_otherdev", tar, ready={"toDeviceId": "dev_someone_else"})
        before = tree_state(self.root)
        result = self.sync_beta()
        self.assert_rejected(result, "sync_otherdev", before)

    def test_inbox_listing_skips_invalid_relay_directories(self) -> None:
        tar = self.payload()
        self.forge("bad id", tar, purpose="handoff")
        self.forge("r_20261118_listok01", tar, purpose="handoff", snap={"relayId": "r_20261118_listok01"})
        with self.env("beta"):
            ids = [item["relayId"] for item in E.list_inbox()["incoming"]]
        self.assertEqual(ids, ["r_20261118_listok01"])

    def test_invalid_delivery_directory_is_skipped_without_download(self) -> None:
        tar = self.payload()
        self.forge("bad id", tar)
        before = tree_state(self.root)
        with mock.patch.object(storage.LocalDirProvider, "download", wraps=storage.LocalDirProvider.download,
                               autospec=True) as dl:
            result = self.sync_beta()
        self.assertFalse(any("bad id" in str(c.args[1]) for c in dl.call_args_list))
        self.assert_rejected(result, "bad id", before)

    def test_author_must_be_signer_already_enforced(self) -> None:
        tar = self.payload()
        self.forge("sync_author", tar, snap={"authorDeviceId": self.identity("beta")["deviceId"]})
        before = tree_state(self.root)
        result = self.sync_beta()
        self.assert_rejected(result, "sync_author", before)

    def test_valid_forged_delivery_still_syncs(self) -> None:
        tar = self.payload()
        self.forge("sync_ok", tar)
        before = tree_state(self.root)
        result = self.sync_beta()
        self.assertEqual([s["deliveryId"] for s in result["syncedSnapshots"]], ["sync_ok"], result["errors"])
        self.assert_only_inside(before, self.ALLOWED)
        self.assertTrue((self.devices["beta"]["home"] / "cache" / "snapshots" / "snap_20261118_forged01").is_dir())

    @unittest.skipIf(os.name == "nt", "POSIX symlinks")
    def test_symlinked_cache_directory_is_not_written_through(self) -> None:
        tar = self.payload()
        self.forge("sync_cachelink", tar)
        cache_root = self.devices["beta"]["home"] / "cache"
        cache_root.mkdir(parents=True, exist_ok=True)
        os.symlink(str(self.outside_dir), str(cache_root / "snapshots"))
        before = tree_state(self.root)
        result = self.sync_beta()
        self.assertEqual(sorted(os.listdir(str(self.outside_dir))), ["keep.txt"])
        self.assert_rejected(result, "sync_cachelink", before)

    @unittest.skipIf(os.name == "nt", "POSIX symlinks")
    def test_symlinked_archive_directory_is_not_written_through(self) -> None:
        tar = self.payload()
        self.forge("sync_archlink", tar)
        archives = self.devices["beta"]["home"] / "archives"
        archives.mkdir(parents=True, exist_ok=True)
        os.symlink(str(self.outside_dir), str(archives / "demo"))
        before = tree_state(self.root)
        result = self.sync_beta()
        self.assertEqual(sorted(os.listdir(str(self.outside_dir))), ["keep.txt"])
        self.assert_rejected(result, "sync_archlink", before)


# ---------------------------------------------------------------- device descriptors


class TestDescriptorDiscovery(NetworkFixture):
    def publish_fake_descriptor(self, key_device_id: str, claimed_device_id: str) -> None:
        home = self.root / ("fake_%s" % len(os.listdir(str(self.root))))
        ident = crypto.ensure_identity(home)
        ident["deviceId"] = claimed_device_id
        desc = crypto.public_descriptor_v3(ident)
        signed = crypto.sign_json(desc, ident["signPrivateKey"], signer_device_id=claimed_device_id)
        path = self.root / "fake_desc.json"
        path.write_text(json.dumps(signed), encoding="utf-8")
        storage.LocalDirProvider(str(self.cloud_dir)).upload(str(path), "v3/devices/%s.json" % key_device_id)

    def discovered_ids(self) -> List[str]:
        with self.env("beta"):
            return [p["deviceId"] for p in E.cloud_status()["peers"]]

    def test_descriptor_device_id_must_match_key_path(self) -> None:
        alpha_did = self.identity("alpha")["deviceId"]
        self.publish_fake_descriptor("dev_fakekey00000000", "dev_claimed000000")
        ids = self.discovered_ids()
        self.assertIn(alpha_did, ids)
        self.assertNotIn("dev_claimed000000", ids)
        self.assertNotIn("dev_fakekey00000000", ids)

    def test_descriptor_with_unsafe_device_id_is_not_discovered(self) -> None:
        self.publish_fake_descriptor("dev..x", "dev..x")
        self.assertNotIn("dev..x", self.discovered_ids())
        cloud = json.loads((self.devices["beta"]["home"] / "cloud.json").read_text(encoding="utf-8"))
        self.assertNotIn("dev..x", cloud["peers"])


# ---------------------------------------------------------------- receive entry point


class TestMaliciousHandoffs(NetworkFixture):
    ALLOWED = ("home_beta", "cloud_shared", "work_beta", "ledger_beta.json")

    def receive(self, relay_id: str, **kw) -> Dict[str, Any]:
        with self.env("beta"):
            return E.receive_relay(relay_id, target_path=str(self.root / "work_beta"), **kw)

    def assert_receive_rejected(self, relay_id: str, before: Dict[str, Any], exc_types=(Exception,)) -> Exception:
        with self.assertRaises(exc_types) as caught:
            self.receive(relay_id)
        changes = self.assert_only_inside(before, ("home_beta", "cloud_shared"))
        self.assert_nothing_cached(changes)
        self.assertFalse((self.root / "work_beta").exists())
        self.assertEqual(L.load(self.devices["beta"]["led"])["projects"], [])
        self.assertNotIn(relay_id, self.beta_state()["receivedRelays"])
        return caught.exception

    def test_valid_forged_handoff_is_received(self) -> None:
        tar = self.payload()
        self.forge("r_20261118_okokokok", tar, purpose="handoff", snap={"relayId": "r_20261118_okokokok"},
                   ready={"authorizedDeviceId": self.identity("beta")["deviceId"]})
        before = tree_state(self.root)
        res = self.receive("r_20261118_okokokok")
        self.assertTrue(res["ok"])
        self.assert_only_inside(before, self.ALLOWED)
        self.assertEqual((self.root / "work_beta" / "a.txt").read_text(), "hello\n")

    def test_version_id_traversal_in_handoff(self) -> None:
        tar = self.payload()
        self.forge("r_20261118_travers1", tar, purpose="handoff", snap={"versionId": "../../../escaped_rx"})
        before = tree_state(self.root)
        err = self.assert_receive_rejected("r_20261118_travers1", before, (crypto.TamperError,))
        self.assertEqual(getattr(err, "code", ""), "invalid_identifier")
        self.assertFalse((self.root / "escaped_rx").exists())

    def test_snapshot_project_must_match_payload_metadata(self) -> None:
        tar = self.payload(project_id="demo")
        self.forge("r_20261118_mismatch", tar, purpose="handoff", snap={"projectId": "other"},
                   meta={"projectId": "other"})
        before = tree_state(self.root)
        err = self.assert_receive_rejected("r_20261118_mismatch", before, (crypto.TamperError,))
        self.assertEqual(getattr(err, "code", ""), "field_mismatch")

    def test_snapshot_relay_id_must_match_handoff(self) -> None:
        tar = self.payload()
        self.forge("r_20261118_relaymis", tar, purpose="handoff", snap={"relayId": "r_20261118_someelse"})
        before = tree_state(self.root)
        err = self.assert_receive_rejected("r_20261118_relaymis", before, (crypto.TamperError,))
        self.assertEqual(getattr(err, "code", ""), "field_mismatch")

    @unittest.skipIf(os.name == "nt", "POSIX symlinks")
    def test_manifest_symlink_then_nested_file_on_case_sensitive_target(self) -> None:
        """Simulated Linux semantics: the case-collision check is off on case-sensitive filesystems."""
        tar = self.payload(folder_entries=[
            {"path": "link", "mode": "120000", "data": str(self.outside_dir).encode("utf-8")},
            {"path": "link/evil.txt", "mode": "100644", "data": b"pwned\n"},
        ])
        self.forge("r_20261118_symlinkx", tar, purpose="handoff")
        before = tree_state(self.root)
        with mock.patch.object(packager, "_is_fs_case_insensitive", return_value=False):
            self.assert_receive_rejected("r_20261118_symlinkx", before)
        self.assertEqual(sorted(os.listdir(str(self.outside_dir))), ["keep.txt"])
        self.assertEqual([n for n in os.listdir(str(self.root)) if n.startswith(".relay-tx")], [])


# ---------------------------------------------------------------- workspace transaction entry points


@unittest.skipIf(os.name == "nt", "POSIX symlinks")
class TestWorkspaceContainment(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.outside = self.root / "outside"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("outside data\n")

    def staging_from(self, tar_path: str) -> str:
        staging = str(self.root / ("staging_%d" % len(os.listdir(str(self.root)))))
        packager.extract_and_verify_payload(tar_path, staging)
        return staging

    def raw_staging(self, tar_path: str) -> str:
        """Unpack without the static manifest check: the apply layer must hold on its own."""
        staging = str(self.root / ("raw_staging_%d" % len(os.listdir(str(self.root)))))
        with tarfile.open(tar_path, "r:gz") as tf:
            tf.extractall(staging)
        return staging

    def test_extract_rejects_symlink_escape_and_writes_beneath_links(self) -> None:
        cases = {
            "abs": [{"path": "link", "mode": "120000", "data": str(self.outside).encode("utf-8")}],
            "rel": [{"path": "up", "mode": "120000", "data": b"../../outside"}],
            "beneath": [{"path": "inner", "mode": "120000", "data": b"sub"},
                        {"path": "inner/x.txt", "mode": "100644", "data": b"x"}],
        }
        for name, entries in cases.items():
            tar = make_payload(str(self.root / ("x_%s.tar.gz" % name)), folder_entries=entries)
            with self.subTest(case=name), self.assertRaises(packager.PackageError) as caught:
                packager.extract_and_verify_payload(tar, str(self.root / ("x_staging_%s" % name)))
            self.assertIn(caught.exception.code, ("path_escape", "symlink_in_path"))
        ok = make_payload(str(self.root / "ok.tar.gz"), folder_entries=[
            {"path": "docs/link", "mode": "120000", "data": b"../a.txt"}])
        packager.extract_and_verify_payload(ok, str(self.root / "ok_staging"))

    def git(self, cwd: Path, *args: str) -> None:
        import subprocess
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(cwd)]
                       + list(args), check=True, capture_output=True)

    def test_apply_payload_never_writes_through_manifest_symlink(self) -> None:
        tar = make_payload(str(self.root / "p.tar.gz"), folder_entries=[
            {"path": "link", "mode": "120000", "data": str(self.outside).encode("utf-8")},
            {"path": "link/evil.txt", "mode": "100644", "data": b"pwned\n"},
        ])
        staging = self.raw_staging(tar)
        dest = self.root / "fresh"
        before = tree_state(self.outside)
        with self.assertRaises(packager.PackageError):
            packager.apply_verified_payload(staging, str(dest))
        self.assertEqual(tree_state(self.outside), before)

    def test_relative_escaping_symlink_entry_is_rejected(self) -> None:
        tar = make_payload(str(self.root / "p2.tar.gz"), folder_entries=[
            {"path": "up", "mode": "120000", "data": b"../../outside"},
        ])
        staging = self.raw_staging(tar)
        with self.assertRaises(packager.PackageError):
            packager.apply_verified_payload(staging, str(self.root / "fresh2"))
        self.assertFalse(os.path.lexists(str(self.root / "fresh2" / "up")))

    def test_new_directory_transaction_never_writes_outside(self) -> None:
        tar = make_payload(str(self.root / "p3.tar.gz"), folder_entries=[
            {"path": "link", "mode": "120000", "data": str(self.outside).encode("utf-8")},
            {"path": "link/evil.txt", "mode": "100644", "data": b"pwned\n"},
        ])
        staging = self.raw_staging(tar)
        before = tree_state(self.root)
        with self.assertRaises(V.PathEscapeError):
            W.prepare_and_apply_workspace(self.home, "rx_new", "demo", "0" * 64, staging, str(self.root / "work"))
        stray = outside(changed_paths(before, tree_state(self.root)), ("home",))
        self.assertEqual(stray, [])
        self.assertFalse(W.has_transaction(self.home, "rx_new"))

    def test_planted_symlinked_parent_in_existing_target(self) -> None:
        """A gitignored symlink inside the target must never redirect a tracked file write."""
        sender = self.root / "sender"
        sender.mkdir()
        self.git(sender, "init", "-b", "main")
        (sender / ".gitignore").write_text("out\n")
        (sender / "a.txt").write_text("v1\n")
        self.git(sender, "add", ".")
        self.git(sender, "commit", "-m", "c1")
        target = self.root / "target"
        shutil.copytree(str(sender), str(target), symlinks=True)
        os.symlink(str(self.outside), str(target / "out"))
        (sender / "out").mkdir()
        (sender / "out" / "keep.txt").write_text("tracked in v2\n")
        self.git(sender, "add", "-f", "out/keep.txt")
        self.git(sender, "commit", "-m", "c2")
        tar = str(self.root / "git.tar.gz")
        packager.build_payload(str(sender), tar, {"id": "demo", "name": "demo"}, {"projectId": "demo", "entries": []})
        staging = self.staging_from(tar)
        before = tree_state(self.root)
        with self.assertRaises(V.PathEscapeError):
            W.prepare_and_apply_workspace(self.home, "rx_git", "demo", "0" * 64, staging, str(target))
        self.assertFalse((self.outside / "keep.txt").read_text() == "tracked in v2\n")
        stray = outside(changed_paths(before, tree_state(self.root)), ("home",))
        self.assertEqual(stray, [])
        self.assertFalse(W.has_transaction(self.home, "rx_git"))

    def test_symlinked_transactions_root_is_not_written_through(self) -> None:
        tar = make_payload(str(self.root / "p5.tar.gz"))
        staging = self.staging_from(tar)
        self.home.mkdir()
        os.symlink(str(self.outside), str(self.home / "transactions"))
        before = tree_state(self.root)
        with self.assertRaises(V.PathEscapeError):
            W.prepare_and_apply_workspace(self.home, "rx_txlink", "demo", "0" * 64, staging, str(self.root / "w5"))
        with self.assertRaises(V.PathEscapeError):
            W.has_transaction(self.home, "rx_txlink")
        self.assertEqual(changed_paths(before, tree_state(self.root)), [])

    def test_tampered_journal_path_is_rejected_on_resume(self) -> None:
        tar = make_payload(str(self.root / "p4.tar.gz"), files={"a.txt": b"new\n"})
        staging = self.staging_from(tar)
        target = self.root / "target4"
        target.mkdir()
        (target / "a.txt").write_text("old\n")
        with mock.patch.dict(os.environ, {"RELAY_DESK_FAULT_POINT": "tx_after_journal_before_apply"}):
            with self.assertRaises(RuntimeError):
                W.prepare_and_apply_workspace(self.home, "rx_tamper", "demo", "0" * 64, staging, str(target))
        jpath = self.home / "transactions" / "rx_tamper" / "journal.json"
        journal = json.loads(jpath.read_text(encoding="utf-8"))
        spec = journal["postFiles"].pop("a.txt")
        journal["postFiles"]["../escaped.txt"] = spec
        journal["fileOps"] = [{"op": "create", "path": "../escaped.txt"}]
        journal["journalSha256"] = W._canonical_journal_sha256(journal)
        jpath.write_text(json.dumps(journal), encoding="utf-8")
        before = tree_state(self.root)
        with self.assertRaises(W.CorruptedRecoveryError):
            W.resume_transaction(self.home, "rx_tamper")
        self.assertFalse((self.root / "escaped.txt").exists())
        self.assertEqual(changed_paths(before, tree_state(self.root)), [])


if __name__ == "__main__":
    unittest.main()
