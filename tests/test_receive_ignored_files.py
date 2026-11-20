# -*- coding: utf-8 -*-
"""R10: receiving a project back into its original folder keeps Git-ignored files.

SPEC section 6: ignored files in an existing target are preservedEntries and stay
untouched. They must not block an in-place receive. Local edits, untracked files
that are not ignored, and paths the incoming state would write over an ignored
entry must still be refused.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from relaydesk import packager
from relaydesk import relay_engine
from relaydesk import workspace_tx as W
from test_relay_engine import RelayEngineTestBase, _git
from test_workspace_tx import _capture_git_state, _init_git_repo, _snapshot_tree, _write_bytes


IGNORE_RULES = "*.log\nbuild/\nnode_modules/\n.env\n"

IGNORED_FILES = {
    "build/output.log": b"build log on A\n",
    "debug.log": b"debug log on A\n",
    "node_modules/pkg/index.js": b"module.exports = 1;\n",
}

NESTED_IGNORED_FILES = {
    "build/x/y/z/deep.o": b"\x00deep object\xff",
    "node_modules/a/node_modules/b/lib/index.js": b"nested dependency\n",
}

IGNORED_SYMLINKS = {
    "latest.log": "build/output.log",
    "node_modules/.bin/tool": "../pkg/index.js",
    "build/dangling.log": "does-not-exist",
}


def _write(root: Path, rel: str, data: bytes) -> Path:
    path = root.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _entry_state(path: Path):
    """Content and identity of one entry: a changed inode or mtime means it was rewritten."""
    st = os.lstat(str(path))
    if os.path.islink(str(path)):
        return ("link", os.readlink(str(path)), st.st_ino, st.st_mtime_ns)
    return ("file", path.read_bytes(), st.st_mode, st.st_ino, st.st_mtime_ns)


def _states(root: Path, rels):
    return {rel: _entry_state(root.joinpath(*rel.split("/"))) for rel in rels}


class ReceiveIgnoredFilesBase(RelayEngineTestBase):
    def make_project_with_ignored_files(self, extra_ignored=None):
        self.pair_both()
        repo_a = self.create_git_project_on_a()
        (repo_a / ".gitignore").write_text(IGNORE_RULES, encoding="utf-8")
        _git(repo_a, "add", ".gitignore")
        _git(repo_a, "commit", "-m", "ignore build output")
        for rel, data in dict(IGNORED_FILES, **(extra_ignored or {})).items():
            _write(repo_a, rel, data)
        (repo_a / ".env").write_text("A_SECRET=stay-on-a\n", encoding="utf-8")
        self.assertEqual(_git(repo_a, "status", "--porcelain"), "")
        return repo_a

    def hand_to_b_and_back(self, edit_on_b=None):
        """A sends to B, B receives into a new folder, commits, and sends back to A."""
        with self.env_a():
            rid_1 = relay_engine.send_project("demo", to_alias="b")["relayId"]
        repo_b = self.root / "repo_b"
        with self.env_b():
            self.assertTrue(relay_engine.receive_relay(rid_1, target_path=str(repo_b))["ok"])
            for rel in IGNORED_FILES:
                self.assertFalse(repo_b.joinpath(*rel.split("/")).exists(), rel)
            (repo_b / "keep.txt").write_text("line1\nfrom B\n", encoding="utf-8")
            (repo_b / "b_new.txt").write_text("new on B\n", encoding="utf-8")
            if edit_on_b:
                edit_on_b(repo_b)
            _git(repo_b, "add", "-A")
            _git(repo_b, "commit", "-m", "B progress")
            rid_2 = relay_engine.send_project("demo", to_alias="a")["relayId"]
        return repo_b, rid_2

    def assert_received_from_b(self, repo_a, repo_b):
        self.assertEqual(_git(repo_a, "rev-parse", "HEAD").strip(), _git(repo_b, "rev-parse", "HEAD").strip())
        self.assertEqual((repo_a / "keep.txt").read_text(encoding="utf-8"), "line1\nfrom B\n")
        self.assertEqual((repo_a / "b_new.txt").read_text(encoding="utf-8"), "new on B\n")
        self.assertEqual(_git(repo_a, "status", "--porcelain"), "")


class TestReceiveBackIntoOriginalFolder(ReceiveIgnoredFilesBase):
    def test_ignored_files_do_not_block_and_stay_untouched(self):
        repo_a = self.make_project_with_ignored_files()
        before = _states(repo_a, list(IGNORED_FILES) + [".env"])
        repo_b, rid_2 = self.hand_to_b_and_back()

        with self.env_a():
            result = relay_engine.receive_relay(rid_2)  # original folder from the ledger

        self.assertTrue(result["ok"])
        self.assert_received_from_b(repo_a, repo_b)
        self.assertEqual(_states(repo_a, list(IGNORED_FILES) + [".env"]), before)

    def test_nested_ignored_directories_and_ignored_symlinks_are_kept(self):
        repo_a = self.make_project_with_ignored_files(extra_ignored=NESTED_IGNORED_FILES)
        outside = self.root / "outside_sdk"
        _write(outside, "lib.h", b"outside the project\n")
        links = dict(IGNORED_SYMLINKS, **{"build/sdk": str(outside)})
        for rel, link_target in links.items():
            path = repo_a.joinpath(*rel.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(link_target, str(path))
        self.assertEqual(_git(repo_a, "status", "--porcelain"), "")
        kept = list(IGNORED_FILES) + list(NESTED_IGNORED_FILES) + list(links)
        before = _states(repo_a, kept)
        repo_b, rid_2 = self.hand_to_b_and_back()
        for rel in list(NESTED_IGNORED_FILES) + list(links):
            self.assertFalse(os.path.lexists(str(repo_b.joinpath(*rel.split("/")))), rel)

        with self.env_a():
            self.assertTrue(relay_engine.receive_relay(rid_2)["ok"])

        self.assert_received_from_b(repo_a, repo_b)
        self.assertEqual(_states(repo_a, kept), before)
        self.assertEqual(os.readlink(str(repo_a / "build" / "sdk")), str(outside))
        self.assertEqual(sorted(os.listdir(str(outside))), ["lib.h"])
        self.assertEqual((outside / "lib.h").read_bytes(), b"outside the project\n")

    def test_local_edits_and_untracked_files_still_block(self):
        repo_a = self.make_project_with_ignored_files()
        repo_b, rid_2 = self.hand_to_b_and_back()
        head_a = _git(repo_a, "rev-parse", "HEAD").strip()
        (repo_a / "README.md").write_text("# local edit on A\n", encoding="utf-8")
        (repo_a / "notes.txt").write_text("untracked on A\n", encoding="utf-8")
        before = _states(repo_a, list(IGNORED_FILES) + ["README.md", "notes.txt", ".env"])

        with self.env_a():
            with self.assertRaises(packager.TargetDirtyError) as cm:
                relay_engine.receive_relay(rid_2)
        reasons = cm.exception.reasons
        self.assertIn("存在本地未跟踪文件：notes.txt", reasons)
        self.assertIn("存在本地未提交改动 (M)：README.md", reasons)
        self.assertFalse([r for r in reasons if "忽略" in r], reasons)
        self.assertIn("--into", str(cm.exception))
        self.assertEqual(_states(repo_a, list(IGNORED_FILES) + ["README.md", "notes.txt", ".env"]), before)
        self.assertEqual(_git(repo_a, "rev-parse", "HEAD").strip(), head_a)
        self.assertFalse(W.has_transaction(self.home_a, rid_2))

        # Only the real local edits blocked: without them the ignored files are no obstacle.
        _git(repo_a, "checkout", "--", "README.md")
        (repo_a / "notes.txt").unlink()
        with self.env_a():
            self.assertTrue(relay_engine.receive_relay(rid_2)["ok"])
        self.assert_received_from_b(repo_a, repo_b)

    def test_incoming_file_over_ignored_file_is_refused(self):
        repo_a = self.make_project_with_ignored_files()
        before = _states(repo_a, list(IGNORED_FILES) + ["keep.txt", ".env"])
        head_a = _git(repo_a, "rev-parse", "HEAD").strip()

        def track_debug_log(repo_b):
            (repo_b / "debug.log").write_text("tracked on B\n", encoding="utf-8")
            _git(repo_b, "add", "-f", "debug.log")

        _repo_b, rid_2 = self.hand_to_b_and_back(edit_on_b=track_debug_log)
        with self.env_a():
            with self.assertRaises(packager.TargetDirtyError) as cm:
                relay_engine.receive_relay(rid_2)
        self.assertEqual(cm.exception.reasons, ["交接包会覆盖本机保留文件：debug.log"])
        self.assertEqual(getattr(cm.exception, "project_id", ""), "demo")
        self.assertIn("--into", str(cm.exception))
        self.assertEqual(_states(repo_a, list(IGNORED_FILES) + ["keep.txt", ".env"]), before)
        self.assertEqual(_git(repo_a, "rev-parse", "HEAD").strip(), head_a)
        self.assertFalse(W.has_transaction(self.home_a, rid_2))

        into = self.root / "a_into"
        with self.env_a():
            self.assertTrue(relay_engine.receive_relay(rid_2, into_path=str(into))["ok"])
        self.assertEqual((into / "debug.log").read_text(encoding="utf-8"), "tracked on B\n")
        self.assertEqual(_states(repo_a, list(IGNORED_FILES) + ["keep.txt", ".env"]), before)


class TestInterruptedReceiveKeepsIgnoredFiles(ReceiveIgnoredFilesBase):
    KEPT = list(IGNORED_FILES) + list(NESTED_IGNORED_FILES) + ["latest.log", ".env"]

    def interrupt_receive(self, fault_point, expected_phase):
        repo_a = self.make_project_with_ignored_files(extra_ignored=NESTED_IGNORED_FILES)
        os.symlink("build/output.log", str(repo_a / "latest.log"))
        repo_b, rid_2 = self.hand_to_b_and_back()
        before = _states(repo_a, self.KEPT)
        with self.env_a():
            with mock.patch.dict(os.environ, {"RELAY_DESK_FAULT_POINT": fault_point}):
                with self.assertRaisesRegex(RuntimeError, fault_point):
                    relay_engine.receive_relay(rid_2)
        self.assertEqual(W.load_and_verify_journal(self.home_a, rid_2)["phase"], expected_phase)
        self.assertEqual(_states(repo_a, self.KEPT), before)
        return repo_a, repo_b, rid_2

    def test_rollback_leaves_ignored_files_intact(self):
        repo_a, repo_b, rid_2 = self.interrupt_receive("tx_mid_files", W.PHASE_APPLYING_FILES)
        # A build runs while the receive is interrupted and rewrites an ignored file.
        (repo_a / "build" / "output.log").write_bytes(b"rebuilt during the interruption\n")
        before_rollback = _states(repo_a, self.KEPT)

        with self.env_a():
            self.assertTrue(relay_engine.recover_relay(rid_2, action="rollback")["ok"])

        self.assertEqual((repo_a / "keep.txt").read_text(encoding="utf-8"), "line1\n")
        self.assertFalse((repo_a / "b_new.txt").exists())
        self.assertEqual(_git(repo_a, "status", "--porcelain"), "")
        self.assertEqual(_states(repo_a, self.KEPT), before_rollback)
        self.assertEqual((repo_a / "build" / "output.log").read_bytes(), b"rebuilt during the interruption\n")

        with self.env_a():
            self.assertTrue(relay_engine.receive_relay(rid_2)["ok"])
        self.assert_received_from_b(repo_a, repo_b)
        self.assertEqual(_states(repo_a, self.KEPT), before_rollback)

    def test_resume_after_interruption_keeps_ignored_files(self):
        repo_a, repo_b, rid_2 = self.interrupt_receive("tx_after_files_before_git", W.PHASE_APPLYING_FILES)
        kept = self.KEPT
        (repo_a / "debug.log").write_bytes(b"appended while interrupted\n")
        before_resume = _states(repo_a, kept)

        with self.env_a():
            self.assertTrue(relay_engine.receive_relay(rid_2)["ok"])

        self.assert_received_from_b(repo_a, repo_b)
        self.assertEqual(_states(repo_a, kept), before_resume)


class TestPreservedPathCollisions(unittest.TestCase):
    """Workspace-level: the incoming state must never write over or around a preserved entry."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.sender = str(self.root / "sender")
        _init_git_repo(self.sender)
        _write_bytes(self.sender, ".gitignore", IGNORE_RULES.encode("utf-8"))
        _write_bytes(self.sender, "README.md", b"# v1\n")
        _git(self.sender, "add", ".")
        _git(self.sender, "commit", "-m", "v1")
        self.target = str(self.root / "target")
        shutil.copytree(self.sender, self.target, symlinks=True)
        for rel, data in IGNORED_FILES.items():
            _write_bytes(self.target, rel, data)

    def commit_on_sender(self, rel, data, force=False):
        _write_bytes(self.sender, rel, data)
        _git(self.sender, "add", "-f", rel) if force else _git(self.sender, "add", rel)
        _git(self.sender, "commit", "-m", "add " + rel)

    def staged_payload(self):
        tar = str(self.root / "payload.tar.gz")
        staging = str(self.root / "staging")
        packager.build_payload(self.sender, tar, {"lastTask": "t"}, {"sessions": []}, target_os=None)
        return packager.extract_and_verify_payload(tar, staging), staging

    def assert_refused(self, expected_reason):
        _unpacked, staging = self.staged_payload()
        files_before = _snapshot_tree(self.target)
        git_before = _capture_git_state(self.target)
        with self.assertRaises(packager.TargetDirtyError) as cm:
            W.prepare_and_apply_workspace(self.home, "rx_collide", "demo", "0" * 64, staging, self.target)
        self.assertIn(expected_reason, cm.exception.reasons)
        self.assertEqual(_snapshot_tree(self.target), files_before)
        self.assertEqual(_capture_git_state(self.target), git_before)
        self.assertFalse(W.has_transaction(self.home, "rx_collide"))
        self.assertEqual([p for p in os.listdir(str(self.home / "transactions")) if not p.startswith(".")], [])

    def test_incoming_file_where_ignored_directory_holds_files(self):
        self.commit_on_sender("node_modules", b"now a tracked file\n")
        self.assert_refused("交接包文件会替换含本机保留文件的目录：node_modules/pkg/index.js")

    def test_incoming_directory_where_ignored_file_sits(self):
        self.commit_on_sender("debug.log/part.txt", b"tracked inside a directory\n", force=True)
        self.assert_refused("交接包需要在本机保留文件处建立目录：debug.log")

    def test_incoming_symlink_over_ignored_file(self):
        os.symlink("README.md", os.path.join(self.sender, "debug.log"))
        _git(self.sender, "add", "-f", "debug.log")
        _git(self.sender, "commit", "-m", "tracked link")
        self.assert_refused("交接包会覆盖本机保留文件：debug.log")

    def test_case_only_collision_on_case_insensitive_filesystem(self):
        if not packager._is_fs_case_insensitive(self.target):
            self.skipTest("filesystem is case-sensitive")
        self.commit_on_sender("Debug.log", b"tracked with different case\n", force=True)
        self.assert_refused("交接包会覆盖本机保留文件：debug.log")

    def test_incoming_ignore_rules_that_expose_a_local_file_are_refused(self):
        """The file would stay, but as an untracked file the next send would package it."""
        self.commit_on_sender(".gitignore", b"build/\nnode_modules/\n.env\n")
        self.assert_refused("交接包的忽略规则不再忽略本机文件：debug.log")

    def test_ignored_files_without_collision_apply_and_stay(self):
        # A self-ignoring cache directory (its own `.gitignore` holds `*`) stays ignored after the apply.
        cache = {".pytest_cache/.gitignore": b"*\n", ".pytest_cache/v/cache/nodeids": b"[]\n"}
        for rel, data in cache.items():
            _write_bytes(self.target, rel, data)
        kept = list(IGNORED_FILES) + list(cache)
        self.commit_on_sender("src/app.py", b"print('v2')\n")
        _unpacked, staging = self.staged_payload()
        before = {rel: _entry_state(Path(self.target).joinpath(*rel.split("/"))) for rel in kept}
        W.prepare_and_apply_workspace(self.home, "rx_ok", "demo", "0" * 64, staging, self.target)
        after = {rel: _entry_state(Path(self.target).joinpath(*rel.split("/"))) for rel in kept}
        self.assertEqual(after, before)
        self.assertEqual(_snapshot_tree(self.target)["src/app.py"][1], b"print('v2')\n")
        self.assertEqual(_git(self.target, "status", "--porcelain"), "")

    def test_raw_check_still_refuses_ignored_files_by_default(self):
        """Without the transaction, `apply_verified_payload` would delete them, so the raw check refuses."""
        self.commit_on_sender("src/app.py", b"print('v2')\n")
        unpacked, _staging = self.staged_payload()
        with self.assertRaisesRegex(packager.TargetDirtyError, "存在本机 Git 忽略文件：debug.log"):
            packager.check_target_clean_for_receive(self.target, unpacked["manifest"])
        packager.check_target_clean_for_receive(self.target, unpacked["manifest"], preserve_ignored=True)


if __name__ == "__main__":
    unittest.main()
