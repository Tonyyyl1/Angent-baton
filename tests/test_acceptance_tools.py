"""Fast checks for the phase-E acceptance tools (acceptance/).

Covers the fixture generator, the round-trip state comparator, level
detection (never "native" for a simulation, never trusting a role name) and
the rule that a public key is never accepted without a human-confirmed
fingerprint.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from acceptance import adapter, common, fixtures, ring  # noqa: E402
from acceptance import compare_state as cs  # noqa: E402


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repo)] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout.decode("utf-8")


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(os.path.realpath(self._td.name))


# ---------------------------------------------------------------- levels


class TestLevelDetection(unittest.TestCase):
    def test_native_levels_follow_the_platform(self):
        self.assertEqual(common.detect_level("native", {"system": "Windows"}), "native-windows")
        self.assertEqual(common.detect_level("native", {"system": "Linux"}), "native-linux")
        self.assertEqual(common.detect_level("native", {"system": "Linux", "wsl": True}), "wsl-linux")
        self.assertEqual(common.detect_level("native", {"system": "Darwin"}), "native-macos")

    def test_simulation_is_never_native(self):
        for system in ("Darwin", "Linux", "Windows"):
            level = common.detect_level("local-simulation", {"system": system})
            self.assertTrue(level.endswith("-local-simulation"), level)
            self.assertFalse(level.startswith("native"), level)
        self.assertEqual(common.detect_level("local-simulation", {"system": "Darwin"}), "macos-local-simulation")

    def test_default_detection_uses_the_real_platform(self):
        import platform

        expected = common.NATIVE_LEVELS.get(platform.system())
        if expected and not common.platform_info().get("wsl"):
            self.assertEqual(common.detect_level("native"), expected)
        with mock.patch("platform.system", return_value="Windows"):
            self.assertEqual(common.detect_level("native"), "native-windows")

    def test_role_must_match_the_real_os(self):
        self.assertTrue(common.role_matches_platform("mac", {"system": "Darwin"}))
        self.assertFalse(common.role_matches_platform("win", {"system": "Darwin"}))
        self.assertFalse(common.role_matches_platform("linux", {"system": "Linux", "wsl": True}))
        self.assertFalse(common.role_matches_platform("unknown", {"system": "Linux"}))

    def test_ring_level_requires_three_distinct_matching_machines(self):
        def rec(role, machine, match=True):
            return {"role": role, "machineId": machine, "platformMatchesRole": match, "simulation": False}

        real = [rec("mac", "m1"), rec("win", "m2"), rec("linux", "m3")]
        self.assertEqual(common.aggregate_ring_level(real)["level"], "real-multidevice")
        one_box = [rec("mac", "m1"), rec("win", "m1"), rec("linux", "m1")]
        self.assertEqual(common.aggregate_ring_level(one_box)["level"], "single-machine-multiinstance")
        self.assertEqual(common.aggregate_ring_level(real[:2])["level"], "partial-multidevice")
        spoofed = [rec("mac", "m1"), rec("win", "m2", match=False), rec("linux", "m3")]
        self.assertEqual(common.aggregate_ring_level(spoofed)["level"], "role-mismatch-simulation")
        sim = [dict(r, simulation=True, level="macos-local-simulation") for r in real]
        self.assertEqual(common.aggregate_ring_level(sim)["level"], "macos-local-simulation")

    def test_device_mode_refuses_a_role_that_does_not_match_this_os(self):
        wrong = next(r for r in sorted(common.ROLE_SYSTEMS) if not common.role_matches_platform(r))
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "storage"
            with self.assertRaises(SystemExit) as caught:
                ring.main(["device", "--role", wrong, "--workdir", str(Path(td) / "work"),
                           "--storage", "local:%s" % storage, "--non-interactive"])
            self.assertIn("Refusing", str(caught.exception))
            self.assertFalse(storage.exists())
            self.assertFalse((Path(td) / "work").exists())

    def test_results_folder_names_level_and_digest(self):
        with tempfile.TemporaryDirectory() as td:
            d = common.make_results_dir(Path(td), "native-linux", "ab" * 32, "native")
            self.assertIn("_native-linux_abababababab_native", d.name)


# ---------------------------------------------------------------- no auto-accept


class _FakeRelay(object):
    def __init__(self, views=None):
        self.calls = []
        self.views = views or {}

    def run(self, key, **kw):
        self.calls.append((key, kw))
        payload = self.views.get(key, {})
        return common.CmdResult(len(self.calls), [key], 0, json.dumps(payload), "", 0.0)


class TestNoAutoAccept(unittest.TestCase):
    FP = "ABCD-1234-EF56-7890-ABCD-1234"

    def test_adapter_table_never_accepts_without_a_fingerprint(self):
        for key, argv in adapter.COMMANDS.items():
            if "--accept" in argv:
                self.assertIn("--fingerprint", argv, key)
                self.assertIn("{fingerprint}", argv, key)
        for spec in adapter.OPTIONAL_COMMANDS.values():
            self.assertNotIn("--accept", spec["argv"])

    def test_missing_confirmation_accepts_nothing(self):
        relay = _FakeRelay()
        with self.assertRaises(ring.NeedsHuman):
            ring.confirm_and_accept(relay, "ring-win", self.FP, None)
        self.assertEqual(relay.calls, [])

    def test_mismatched_confirmation_accepts_nothing(self):
        relay = _FakeRelay()
        with self.assertRaises(ring.StepFailed):
            ring.confirm_and_accept(relay, "ring-win", self.FP, "ABCD-1234-EF56-7890-ABCD-0000")
        self.assertEqual(relay.calls, [])

    def test_matching_confirmation_passes_the_value_explicitly(self):
        relay = _FakeRelay()
        ring.confirm_and_accept(relay, "ring-win", self.FP, self.FP.lower().replace("-", " "))
        self.assertEqual(relay.calls, [("pair_accept_confirmed",
                                        {"peer": "ring-win", "fingerprint": self.FP.lower().replace("-", " ")})])

    def test_pair_step_without_a_human_never_runs_accept(self):
        doc = ring.load_doc(None)
        view = {"peers": [{"alias": "ring-win", "status": "discovered", "paired": False, "pairFingerprint": self.FP}]}
        relay = _FakeRelay({"pair_republish": view})
        dev = mock.Mock()
        dev.relay = relay
        dev.role, dev.alias = "mac", "ring-mac"
        human = ring.InteractiveHuman({}, interactive=False)
        rt = mock.Mock()
        rt.alias = lambda role: doc["roles"][role]["alias"]
        rt.poll = lambda fn, what: fn()
        rt.human = human
        rt.sim = False
        with mock.patch("builtins.print"):
            with self.assertRaises(ring.NeedsHuman):
                ring.act_pair_accept(rt, dev, {"id": "P04", "peer": "win"}, ring.Checks())
        self.assertNotIn("pair_accept_confirmed", [c[0] for c in relay.calls])

    def test_flag_value_is_compared_with_this_devices_display(self):
        human = ring.InteractiveHuman({"win": "0000-0000-0000-0000-0000-0000"}, interactive=False)
        dev = mock.Mock(role="mac", alias="ring-mac")
        rt = mock.Mock()
        rt.alias = lambda role: "ring-" + role
        with mock.patch("builtins.print"):
            value = human.confirm_fingerprint(rt, dev, "win", self.FP)
        relay = _FakeRelay()
        with self.assertRaises(ring.StepFailed):
            ring.confirm_and_accept(relay, "ring-win", self.FP, value)
        self.assertEqual(relay.calls, [])


# ---------------------------------------------------------------- fixtures


class TestFixtures(_TmpCase):
    def test_git_fixture_has_every_category(self):
        m = fixtures.make_git_fixture(self.root / "g", owner="mac")
        repo = Path(m["path"])
        status = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored")
        entries = {e[3:]: e[:2] for e in status.split("\0") if e}
        self.assertEqual(entries["src/staged_new.py"], "A ")
        self.assertEqual(entries["src/staged_empty.txt"], "A ")
        self.assertEqual(entries["both_stages.py"], "MM")
        self.assertEqual(entries["to_stage_delete.txt"], "D ")
        self.assertEqual(entries["to_unstage_delete.txt"], " D")
        self.assertEqual(entries["README.md"], " M")
        self.assertEqual(entries["notes/未跟踪.txt"], "??")
        self.assertEqual(entries[".env.local"], "??")
        for ignored in ("build/output.log", "debug.log", "node_modules/pkg/index.js", ".env"):
            self.assertEqual(entries[ignored], "!!", ignored)
        self.assertEqual(_git(repo, "symbolic-ref", "HEAD").strip(), "refs/heads/feat/ring-acceptance")
        self.assertEqual(sorted(_git(repo, "tag").split()), ["v0.1", "v0.2-annotated"])
        self.assertIn("100755 ", _git(repo, "ls-files", "-s", "scripts/run.sh"))
        self.assertTrue((repo / "docs" / "中文 说明.md").is_file())
        self.assertIn(m["marker"], (repo / ".env").read_text(encoding="utf-8"))
        self.assertTrue(m["marker"].startswith("DUMMY-NOT-A-SECRET-mac-"))

    def test_plain_fixture_is_not_git(self):
        m = fixtures.make_plain_fixture(self.root / "p", owner="linux")
        p = Path(m["path"])
        self.assertFalse((p / ".git").exists())
        self.assertEqual((p / "empty.txt").stat().st_size, 0)
        self.assertIn(m["marker"], (p / ".env").read_text(encoding="utf-8"))

    def test_edit_commits_received_state_but_never_credentials(self):
        m = fixtures.make_git_fixture(self.root / "g", owner="mac")
        repo = Path(m["path"])
        info = fixtures.edit_git_workspace(repo, "win", 1)
        committed = _git(repo, "ls-files")
        self.assertNotIn(".env.local", committed)
        self.assertIn("notes/untracked note.md", committed)
        self.assertIn(info["tag"], _git(repo, "tag").split())
        status = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").split("\0")
        self.assertIn("A  hops/win-1 staged.txt", status)
        self.assertIn(" M src/app.py", status)
        self.assertIn("?? hops/win-1 未跟踪.md", status)

    def test_mirrored_credential_rule_matches_relay_desk(self):
        from relaydesk import packager

        names = [".env", ".env.local", ".env.example", "a/b/.env.production", "id_rsa", "x.pem", "k.KEY",
                 "auth.json", "README.md", "env.txt", ".env.sample", "notes/.claude.json"]
        for n in names:
            self.assertEqual(cs.is_secret_name(n), packager._is_secret_filename(n), n)


# ---------------------------------------------------------------- comparator


class TestComparator(_TmpCase):
    def _transfer_copy(self, src, dst):
        """What a perfect handoff produces: everything but ignored and credential files."""
        shutil.copytree(str(src), str(dst), symlinks=True)
        for rel in ("build", "node_modules"):
            shutil.rmtree(str(dst / rel), ignore_errors=True)
        for rel in ("debug.log", ".env", ".env.local"):
            if (dst / rel).exists():
                (dst / rel).unlink()

    def setUp(self):
        super().setUp()
        self.src = Path(fixtures.make_git_fixture(self.root / "sender", owner="mac")["path"])
        self.sender = cs.capture_state(self.src, label="sender")

    def test_perfect_transfer_passes(self):
        dst = self.root / "receiver"
        self._transfer_copy(self.src, dst)
        rep = cs.compare_states(self.sender, cs.capture_state(dst, label="receiver"))
        self.assertTrue(rep["ok"], rep["failed"])
        self.assertEqual(rep["warnings"], [])

    def test_detects_content_branch_tag_ignored_and_credential_problems(self):
        dst = self.root / "receiver"
        self._transfer_copy(self.src, dst)
        (dst / "lower.txt").write_text("changed\n", encoding="utf-8")
        (dst / "debug.log").write_text((self.src / "debug.log").read_text(encoding="utf-8"), encoding="utf-8")
        marker = cs.capture_state(self.src)["ownMarkers"][0]
        (dst / "notes" / "leak.txt").write_text("token=%s\n" % marker, encoding="utf-8")
        _git(dst, "tag", "-d", "v0.1")
        rep = cs.compare_states(self.sender, cs.capture_state(dst))
        for check in ("worktreeContent", "unstagedChanges", "ignoredNotTransferred", "mockCredentialNotTransferred",
                      "tags", "untracked"):
            self.assertIn(check, rep["failed"])
        _git(dst, "checkout", "-q", "main")
        self.assertIn("branch", cs.compare_states(self.sender, cs.capture_state(dst))["failed"])

    def test_receiver_preexisting_credential_and_ignored_files_must_survive(self):
        dst = self.root / "receiver"
        self._transfer_copy(self.src, dst)
        (dst / ".env").write_text("RECEIVER=own\n", encoding="utf-8")
        (dst / "build").mkdir()
        (dst / "build" / "mine.log").write_text("receiver build\n", encoding="utf-8")
        pre = cs.capture_state(dst)
        self.assertTrue(cs.compare_states(self.sender, cs.capture_state(dst), pre)["ok"])
        (dst / ".env").write_text("RECEIVER=overwritten\n", encoding="utf-8")
        (dst / "build" / "mine.log").unlink()
        rep = cs.compare_states(self.sender, cs.capture_state(dst), pre)
        self.assertIn("receiverCredentialPreserved", rep["failed"])
        self.assertIn("receiverIgnoredPreserved", rep["failed"])

    def test_summary_note_round_trip_and_on_device_check(self):
        summary = cs.transfer_summary(self.sender)
        decoded = cs.decode_note(cs.encode_note(summary))
        self.assertEqual(decoded, json.loads(json.dumps(summary)))
        self.assertIsNone(cs.decode_note("ordinary note"))
        dst = self.root / "receiver"
        self._transfer_copy(self.src, dst)
        recv = cs.capture_state(dst)
        mine = cs.transfer_summary(recv)
        mine["markersSeen"] = sorted(recv["credentialMarkers"])
        self.assertTrue(cs.compare_summaries(decoded, mine)["ok"])
        (dst / "lower.txt").write_text("drift\n", encoding="utf-8")
        mine2 = cs.transfer_summary(cs.capture_state(dst))
        self.assertIn("content", cs.compare_summaries(decoded, mine2)["failed"])

    def test_state_delta_detects_any_change(self):
        a = cs.capture_state(self.src)
        self.assertEqual(cs.state_delta(a, cs.capture_state(self.src)), {})
        (self.src / ".env").write_text("changed\n", encoding="utf-8")
        self.assertIn("files.secret", cs.state_delta(a, cs.capture_state(self.src)))

    def test_receipt_digest_checks(self):
        send = {"relayId": "r_1", "packageSha256": "aa", "snapshotId": "snap_1", "toDeviceId": "dev_b", "projectId": "p"}
        receipt = {"relayId": "r_1", "packageSha256": "aa", "snapshotId": "snap_1", "receiverDeviceId": "dev_b",
                   "projectId": "p", "signature": "sig"}
        rep = cs.compare_relay_records(send=send, receiver_record={"signedReceipt": receipt, "receiptUploaded": True},
                                       receiver_history=[{"relayId": "r_1"}], cloud_receipt=dict(receipt),
                                       sender_record={"acked": True}, sender_history=[{"tag": "r_1"}])
        self.assertTrue(rep["ok"], rep["failed"])
        bad = dict(receipt, packageSha256="bb")
        rep = cs.compare_relay_records(send=send, receiver_record={"signedReceipt": bad, "receiptUploaded": True},
                                       cloud_receipt=receipt)
        self.assertIn("receiptMatchesSend", rep["failed"])
        self.assertIn("cloudReceiptEqualsLocal", rep["failed"])


# ---------------------------------------------------------------- source identity & storage


class TestSourceIdentityAndStorage(_TmpCase):
    def test_extracted_package_digest_matches_git_digest(self):
        repo = self.root / "src"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        (repo / "a.py").write_text("print(1)\n", encoding="utf-8")
        (repo / "ignored.txt").write_text("no\n", encoding="utf-8")
        (repo / "ledger.json").write_text("{}\n", encoding="utf-8")
        _git(repo, "add", ".gitignore", "a.py")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")
        (repo / "new.py").write_text("print(2)\n", encoding="utf-8")
        git_id = common.source_identity(repo)
        self.assertEqual(git_id["method"], "git-worktree")
        pkg = self.root / "pkg"
        pkg.mkdir()
        for rel in (".gitignore", "a.py", "new.py"):
            shutil.copy2(str(repo / rel), str(pkg / rel))
        (pkg / "SOURCE-DIGEST.json").write_text(json.dumps({"sourceDigest": git_id["sourceDigest"]}), encoding="utf-8")
        pkg_id = common.source_identity(pkg)
        self.assertEqual(pkg_id["method"], "extracted-package")
        self.assertEqual(pkg_id["sourceDigest"], git_id["sourceDigest"])
        self.assertTrue(pkg_id["verifiedAgainstPackage"])
        (pkg / "a.py").write_text("print('tampered')\n", encoding="utf-8")
        self.assertFalse(common.source_identity(pkg)["verifiedAgainstPackage"])

    def test_storage_spec_rules(self):
        ok = adapter.StorageSpec.parse("rclone:gdrive:relay-acceptance/2026-11-18-run1")
        self.assertEqual((ok.remote, ok.root), ("gdrive", "relay-acceptance/2026-11-18-run1"))
        for bad in ("rclone:gdrive:relay-baton", "rclone:gdrive:", "rclone:onlyremote", "s3:x"):
            with self.assertRaises(adapter.StorageError):
                adapter.StorageSpec.parse(bad)
        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / "v3").mkdir()
        with self.assertRaises(adapter.StorageError):
            adapter.StorageSpec.parse("local:%s" % foreign).prepare_local()
        fresh = adapter.StorageSpec.parse("local:%s" % (self.root / "fresh"))
        fresh.prepare_local()
        self.assertTrue((self.root / "fresh" / adapter.ACCEPTANCE_MARKER).exists())
        fresh.prepare_local()  # re-usable once marked


# ---------------------------------------------------------------- step files


class TestStepFiles(unittest.TestCase):
    def _check(self, name):
        doc = json.loads((REPO_ROOT / "acceptance" / "steps" / name).read_text(encoding="utf-8"))
        seen = {}
        for step in doc["steps"]:
            self.assertIn(step["action"], ring.ACTIONS, step["id"])
            self.assertIn(step["role"], doc["roles"], step["id"])
            self.assertNotIn(step["id"], seen)
            for ref_key in ("relayFromStep", "onlyIfFailed"):
                if step.get(ref_key):
                    ref = step[ref_key]
                    self.assertIn(ref, seen, "%s.%s" % (step["id"], ref_key))
                    self.assertEqual(seen[ref]["role"], step["role"], "%s must reference its own device" % step["id"])
            for role_key in ("to", "from", "peer", "target"):
                if role_key in step and step[role_key] in ("new", "existing", "into"):
                    continue
                if role_key in step:
                    self.assertIn(step[role_key], doc["roles"], "%s.%s" % (step["id"], role_key))
            if step.get("project"):
                self.assertIn(step["project"], doc["projects"], step["id"])
            seen[step["id"]] = step
        for scen, ids in doc.get("scenarios", {}).items():
            self.assertTrue(set(ids) <= set(seen), scen)
        return doc

    def test_ring_step_file_is_consistent(self):
        doc = self._check("ring-3device.json")
        self.assertEqual(set(doc["roles"]), set(common.ROLE_SYSTEMS))
        actions = {s["action"] for s in doc["steps"]}
        for needed in ("assert-cannot-claim", "rotate-keys", "go-offline", "network-down", "recover", "scribble"):
            self.assertIn(needed, actions)
        text = ring.render_runbook(doc, "run1")
        for step in doc["steps"]:
            self.assertIn("| %s |" % step["id"], text)

    def test_loopback_step_file_is_consistent(self):
        self._check("loopback-2device.json")


if __name__ == "__main__":
    unittest.main()
