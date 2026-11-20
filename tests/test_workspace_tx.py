import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from relaydesk import packager
from relaydesk import workspace_tx as wtx


REPO_ROOT = str(Path(__file__).resolve().parent.parent)

ALL_TX_FAULT_POINTS = (
    "tx_after_journal_before_apply",
    "tx_mid_files",
    "tx_after_files_before_git",
    "tx_after_git_refs_before_index",
    "tx_after_workspace_commit",
)

FOLDER_TX_FAULT_POINTS = (
    "tx_after_journal_before_apply",
    "tx_mid_files",
    "tx_after_files_before_git",
    "tx_after_workspace_commit",
)


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", "-c", "core.autocrlf=false", "-C", cwd] + list(args),
        check=True,
        capture_output=True,
    )


def _init_git_repo(path: str, branch: str = "main") -> None:
    os.makedirs(path, exist_ok=True)
    proc = subprocess.run(["git", "init", "-b", branch, path], capture_output=True)
    if proc.returncode != 0:
        subprocess.run(["git", "init", path], check=True, capture_output=True)
        _git(path, "symbolic-ref", "HEAD", "refs/heads/%s" % branch)
    _git(path, "config", "user.email", "tester@example.com")
    _git(path, "config", "user.name", "Relay Tester")


def _write_bytes(root: str, rel_path: str, data: bytes, executable: bool = False) -> str:
    full = os.path.join(root, *rel_path.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)
    if executable:
        os.chmod(full, 0o755)
    else:
        os.chmod(full, 0o644)
    return full


def _read_bytes(root: str, rel_path: str) -> bytes:
    full = os.path.join(root, *rel_path.split("/"))
    with open(full, "rb") as fh:
        return fh.read()


def _snapshot_tree(root: str) -> Dict[str, Tuple[str, bytes]]:
    """Capture all non-.git files under `root` as `{rel_posix: (mode_str, raw_bytes)}`."""
    snap: Dict[str, Tuple[str, bytes]] = {}
    if not os.path.isdir(root):
        return snap
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept: List[str] = []
        for d in sorted(dirnames):
            if d == ".git":
                continue
            full_d = os.path.join(dirpath, d)
            if os.path.islink(full_d):
                rel_d = os.path.relpath(full_d, root).replace(os.sep, "/")
                snap[rel_d] = ("120000", os.readlink(full_d).encode("utf-8"))
                continue
            kept.append(d)
        dirnames[:] = kept
        for f in sorted(filenames):
            full_f = os.path.join(dirpath, f)
            rel_f = os.path.relpath(full_f, root).replace(os.sep, "/")
            if os.path.islink(full_f):
                snap[rel_f] = ("120000", os.readlink(full_f).encode("utf-8"))
            elif os.path.isfile(full_f):
                st = os.stat(full_f)
                mode_str = "100755" if (st.st_mode & stat.S_IXUSR) else "100644"
                with open(full_f, "rb") as fh:
                    snap[rel_f] = (mode_str, fh.read())
    return snap


def _capture_git_state(repo_path: str) -> Dict[str, Any]:
    rc_sym, out_sym, _ = packager._run_git_text(repo_path, ["symbolic-ref", "-q", "HEAD"])
    rc_head, out_head, _ = packager._run_git_text(repo_path, ["rev-parse", "--verify", "HEAD"])
    rc_refs, out_refs, _ = packager._run_git_text(
        repo_path,
        ["for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags"],
    )
    rc_idx, raw_idx, _ = packager._run_git_bytes(repo_path, ["ls-files", "--stage", "-z"])
    rc_cd, raw_cd, _ = packager._run_git_bytes(
        repo_path, ["diff", "--cached", "--binary", "--full-index"]
    )
    rc_wd, raw_wd, _ = packager._run_git_bytes(
        repo_path, ["diff", "--binary", "--full-index"]
    )
    rc_un, raw_un, _ = packager._run_git_bytes(
        repo_path, ["ls-files", "--others", "--exclude-standard", "-z"]
    )
    refs: Dict[str, str] = {}
    if rc_refs == 0 and out_refs:
        for line in sorted(out_refs.splitlines()):
            if " " in line:
                rname, oid = line.split(" ", 1)
                refs[rname] = oid
    return {
        "symbolicHead": out_sym.strip() if rc_sym == 0 else "",
        "headOid": out_head.strip() if rc_head == 0 else "",
        "refs": refs,
        "indexStageRaw": raw_idx if rc_idx == 0 else b"",
        "cachedDiff": raw_cd if rc_cd == 0 else b"",
        "workDiff": raw_wd if rc_wd == 0 else b"",
        "untracked": sorted(packager._parse_nul_paths(raw_un)) if rc_un == 0 else [],
    }


def _build_git_fixture(tmp_dir: str, existing_target: bool = True) -> Dict[str, Any]:
    """Build sender git repo + receiver git repo + extracted staging + expected pre/post states."""
    sender = os.path.join(tmp_dir, "sender_git")
    receiver = os.path.join(tmp_dir, "receiver_git")
    _init_git_repo(sender, "main")

    _write_bytes(sender, ".gitignore", b".env\n.env.*\nbuild/\n")
    _write_bytes(sender, "README.md", b"# Base Project\n")
    _write_bytes(sender, "app.py", b"print('v1')\n")
    _write_bytes(sender, "keep_unchanged.txt", b"unchanged across handoff\n")
    _write_bytes(sender, "old_only.txt", b"to be deleted by sender commit\n")
    _write_bytes(sender, "staged_mod.txt", b"staged v1\n")
    _write_bytes(sender, "staged_del.txt", b"staged delete v1\n")
    _write_bytes(sender, "unstaged_mod.txt", b"unstaged v1\n")
    _write_bytes(sender, "unstaged_del.txt", b"unstaged delete v1\n")
    _write_bytes(sender, "bin/run.sh", b"#!/bin/sh\necho run\n", executable=False)
    _git(sender, "add", ".")
    _git(sender, "commit", "-m", "base commit C1")

    if existing_target:
        shutil.copytree(sender, receiver, symlinks=True)
        _git(receiver, "config", "user.email", "tester@example.com")
        _git(receiver, "config", "user.name", "Relay Tester")
        # Uninvolved local branch and tag on receiver
        _git(receiver, "tag", "local-tag-v0")
        _git(receiver, "checkout", "-b", "local-wip")
        _write_bytes(receiver, "local_wip_only.txt", b"local branch commit\n")
        _git(receiver, "add", "local_wip_only.txt")
        _git(receiver, "commit", "-m", "local wip commit")
        _git(receiver, "checkout", "main")
        # Preserved .env and gitignored build output on receiver
        _write_bytes(receiver, ".env", b"RECEIVER_SECRET=keep-this-token\n")
        _write_bytes(receiver, "build/local_cache.bin", b"\x00\x01LOCAL_BUILD_CACHE\xff")
        # Pre-existing receiver staged + unstaged + untracked baseline to test full rollback
        _write_bytes(receiver, "pre_staged.txt", b"pre staged content\n")
        _git(receiver, "add", "pre_staged.txt")
        _write_bytes(receiver, "app.py", b"print('v1-receiver-dirty')\n")
        _write_bytes(receiver, "pre_note.txt", b"pre untracked note\n")

    # Advance sender with commits, side branch/tag, staged, unstaged, untracked, and .env
    _git(sender, "checkout", "-b", "feat/sender")
    _write_bytes(sender, "feat.txt", b"sender feature branch\n")
    _git(sender, "add", "feat.txt")
    _git(sender, "commit", "-m", "sender feature commit")
    _git(sender, "checkout", "main")

    _write_bytes(sender, "README.md", b"# Base Project\nUpdated in C2\n")
    _write_bytes(sender, "new_committed.txt", b"committed in C2\n")
    _git(sender, "rm", "-q", "old_only.txt")
    _git(sender, "add", "README.md", "new_committed.txt")
    _git(sender, "commit", "-m", "sender commit C2")
    _git(sender, "tag", "v1.0-sender")

    # Staged changes: add, modify, delete, executable chmod
    _write_bytes(sender, "staged_add.txt", b"brand new staged\n")
    _write_bytes(sender, "staged_mod.txt", b"staged v2\n")
    _git(sender, "rm", "-q", "staged_del.txt")
    os.chmod(os.path.join(sender, "bin", "run.sh"), 0o755)
    _git(sender, "add", "staged_add.txt", "staged_mod.txt", "bin/run.sh")

    # Unstaged changes: modify, delete
    _write_bytes(sender, "unstaged_mod.txt", b"unstaged v2 in worktree\n")
    os.unlink(os.path.join(sender, "unstaged_del.txt"))

    # Untracked files
    _write_bytes(sender, "untracked_doc.md", "交接说明文档\n".encode("utf-8"))
    _write_bytes(sender, "scripts/helper.sh", b"#!/bin/sh\necho helper\n", executable=True)

    # Sender .env must be excluded by packager
    _write_bytes(sender, ".env", b"SENDER_SECRET=must-never-transfer\n")

    tar_path = os.path.join(tmp_dir, "payload_git.tar.gz")
    staging_dir = os.path.join(tmp_dir, "staging_git")
    packager.build_payload(
        sender,
        tar_path,
        {"lastTask": "w2", "nextAction": "verify"},
        {"sessions": []},
        target_os=None,
    )
    packager.extract_and_verify_payload(tar_path, staging_dir)

    # Build expected post state in a reference directory so we know the exact desired outcome
    ref_post = os.path.join(tmp_dir, "ref_post_git")
    if existing_target:
        os.makedirs(ref_post, exist_ok=True)
        shutil.copytree(os.path.join(receiver, ".git"), os.path.join(ref_post, ".git"), symlinks=True)
        _write_bytes(ref_post, ".env", b"RECEIVER_SECRET=keep-this-token\n")
    packager.apply_verified_payload(staging_dir, ref_post)
    if existing_target:
        # workspace_tx preserves gitignored local build files in place (unlike raw apply_verified_payload)
        _write_bytes(ref_post, "build/local_cache.bin", b"\x00\x01LOCAL_BUILD_CACHE\xff")

    return {
        "sender": sender,
        "target": receiver,
        "staging": staging_dir,
        "local_home": Path(tmp_dir) / "local_home",
        "pre_files": _snapshot_tree(receiver) if existing_target else {},
        "pre_git": _capture_git_state(receiver) if existing_target else {},
        "expected_post_files": _snapshot_tree(ref_post),
        "expected_post_git": _capture_git_state(ref_post),
    }


def _build_folder_fixture(tmp_dir: str, existing_target: bool = True) -> Dict[str, Any]:
    sender = os.path.join(tmp_dir, "sender_folder")
    receiver = os.path.join(tmp_dir, "receiver_folder")
    os.makedirs(sender, exist_ok=True)

    _write_bytes(sender, "a.txt", b"new a content\n")
    _write_bytes(sender, "shared.txt", b"shared constant\n")
    _write_bytes(sender, "sub/b.txt", b"new sub b\n")
    _write_bytes(sender, "new_dir/c.sh", b"#!/bin/sh\necho ok\n", executable=True)
    _write_bytes(sender, ".env", b"SENDER_FOLDER_SECRET=excluded\n")

    if existing_target:
        os.makedirs(receiver, exist_ok=True)
        _write_bytes(receiver, "a.txt", b"old a content\n")
        _write_bytes(receiver, "shared.txt", b"shared constant\n")
        _write_bytes(receiver, "old_only.txt", b"remove me\n")
        _write_bytes(receiver, "old_sub/nested.txt", b"remove nested\n")
        _write_bytes(receiver, ".env", b"RECEIVER_FOLDER_SECRET=keep\n")

    tar_path = os.path.join(tmp_dir, "payload_folder.tar.gz")
    staging_dir = os.path.join(tmp_dir, "staging_folder")
    packager.build_payload(
        sender,
        tar_path,
        {"lastTask": "folder", "nextAction": "verify"},
        {"sessions": []},
        target_os=None,
    )
    packager.extract_and_verify_payload(tar_path, staging_dir)

    expected_post = {
        "a.txt": ("100644", b"new a content\n"),
        "shared.txt": ("100644", b"shared constant\n"),
        "sub/b.txt": ("100644", b"new sub b\n"),
        "new_dir/c.sh": ("100755", b"#!/bin/sh\necho ok\n"),
    }
    if existing_target:
        expected_post[".env"] = ("100644", b"RECEIVER_FOLDER_SECRET=keep\n")

    return {
        "sender": sender,
        "target": receiver,
        "staging": staging_dir,
        "local_home": Path(tmp_dir) / "local_home",
        "pre_files": _snapshot_tree(receiver) if existing_target else {},
        "expected_post_files": expected_post,
    }


def _run_child_apply(
    local_home: Path,
    relay_id: str,
    project_id: str,
    package_sha256: str,
    staging_dir: str,
    target_path: str,
    fault_point: str = "",
    fault_mode: str = "",
) -> subprocess.CompletedProcess:
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from relaydesk import workspace_tx\n"
        "res = workspace_tx.prepare_and_apply_workspace(\n"
        "    Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]\n"
        ")\n"
        "print(json.dumps(res))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT
    if fault_point:
        env["RELAY_DESK_FAULT_POINT"] = fault_point
    else:
        env.pop("RELAY_DESK_FAULT_POINT", None)
    if fault_mode:
        env["RELAY_DESK_FAULT_MODE"] = fault_mode
    else:
        env.pop("RELAY_DESK_FAULT_MODE", None)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(local_home),
            relay_id,
            project_id,
            package_sha256,
            staging_dir,
            target_path,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _run_child_recover(action: str, local_home: Path, relay_id: str) -> subprocess.CompletedProcess:
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from relaydesk import workspace_tx\n"
        "action, lh, rid = sys.argv[1], Path(sys.argv[2]), sys.argv[3]\n"
        "try:\n"
        "    if action == 'inspect':\n"
        "        out = workspace_tx.inspect_transaction(lh, rid)\n"
        "    elif action == 'resume':\n"
        "        out = workspace_tx.resume_transaction(lh, rid)\n"
        "    elif action == 'rollback':\n"
        "        out = workspace_tx.rollback_transaction(lh, rid)\n"
        "    else:\n"
        "        raise ValueError(action)\n"
        "    print(json.dumps({'ok': True, 'data': out}))\n"
        "except workspace_tx.WorkspaceTxError as exc:\n"
        "    print(json.dumps({'ok': False, 'errorType': type(exc).__name__, 'code': exc.code, 'msg': str(exc)}))\n"
        "    sys.exit(2)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT
    env.pop("RELAY_DESK_FAULT_POINT", None)
    env.pop("RELAY_DESK_FAULT_MODE", None)
    return subprocess.run(
        [sys.executable, "-c", code, action, str(local_home), relay_id],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


class TestWorkspaceTxGitMode(unittest.TestCase):
    def _assert_git_post_state(self, fix: Dict[str, Any]) -> None:
        self.assertEqual(_snapshot_tree(fix["target"]), fix["expected_post_files"])
        self.assertEqual(_capture_git_state(fix["target"]), fix["expected_post_git"])
        self.assertEqual(_read_bytes(fix["target"], ".env"), b"RECEIVER_SECRET=keep-this-token\n")
        self.assertEqual(
            _read_bytes(fix["target"], "build/local_cache.bin"),
            b"\x00\x01LOCAL_BUILD_CACHE\xff",
        )
        refs = _capture_git_state(fix["target"])["refs"]
        self.assertIn("refs/heads/local-wip", refs)
        self.assertIn("refs/tags/local-tag-v0", refs)
        self.assertIn("refs/heads/feat/sender", refs)
        self.assertIn("refs/tags/v1.0-sender", refs)

    def _assert_git_pre_state(self, fix: Dict[str, Any]) -> None:
        self.assertEqual(_snapshot_tree(fix["target"]), fix["pre_files"])
        self.assertEqual(_capture_git_state(fix["target"]), fix["pre_git"])
        self.assertEqual(_read_bytes(fix["target"], ".env"), b"RECEIVER_SECRET=keep-this-token\n")
        self.assertEqual(
            _read_bytes(fix["target"], "build/local_cache.bin"),
            b"\x00\x01LOCAL_BUILD_CACHE\xff",
        )

    def test_git_happy_path_and_archive_prevents_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            fix = _build_git_fixture(tmp, existing_target=True)
            res = wtx.prepare_and_apply_workspace(
                fix["local_home"],
                "relay_git_ok",
                "proj_1",
                "sha_pkg_1",
                fix["staging"],
                fix["target"],
            )
            self.assertEqual(res["mode"], "git")
            self._assert_git_post_state(fix)

            info = wtx.inspect_transaction(fix["local_home"], "relay_git_ok")
            self.assertEqual(info["state"], "workspace_committed")
            self.assertEqual(info["phase"], wtx.PHASE_WORKSPACE_COMMITTED)

            wtx.archive_completed_transaction(fix["local_home"], "relay_git_ok")
            tx_dir = fix["local_home"] / "transactions" / "relay_git_ok"
            self.assertFalse((tx_dir / "pre_blobs").exists())
            self.assertFalse((tx_dir / "post_blobs").exists())

            done_info = wtx.inspect_transaction(fix["local_home"], "relay_git_ok")
            self.assertEqual(done_info["state"], "completed")
            self.assertFalse(done_info["canRollback"])

            with self.assertRaises(wtx.WorkspaceTxError) as ctx:
                wtx.rollback_transaction(fix["local_home"], "relay_git_ok")
            self.assertEqual(ctx.exception.code, "already_completed")

    def test_git_in_process_fault_all_points_resume_and_rollback(self):
        for point in ALL_TX_FAULT_POINTS:
            for action in ("resume", "rollback"):
                with self.subTest(point=point, action=action):
                    with tempfile.TemporaryDirectory() as tmp:
                        fix = _build_git_fixture(tmp, existing_target=True)
                        rid = "relay_inproc_%s_%s" % (point, action)
                        old_pt = os.environ.get("RELAY_DESK_FAULT_POINT")
                        old_mode = os.environ.get("RELAY_DESK_FAULT_MODE")
                        os.environ["RELAY_DESK_FAULT_POINT"] = point
                        os.environ.pop("RELAY_DESK_FAULT_MODE", None)
                        try:
                            with self.assertRaises(RuntimeError) as ctx:
                                wtx.prepare_and_apply_workspace(
                                    fix["local_home"],
                                    rid,
                                    "proj_1",
                                    "sha_pkg_1",
                                    fix["staging"],
                                    fix["target"],
                                )
                            self.assertIn(point, str(ctx.exception))
                        finally:
                            if old_pt is None:
                                os.environ.pop("RELAY_DESK_FAULT_POINT", None)
                            else:
                                os.environ["RELAY_DESK_FAULT_POINT"] = old_pt
                            if old_mode is None:
                                os.environ.pop("RELAY_DESK_FAULT_MODE", None)
                            else:
                                os.environ["RELAY_DESK_FAULT_MODE"] = old_mode

                        info = wtx.inspect_transaction(fix["local_home"], rid)
                        self.assertTrue(info["exists"])
                        self.assertTrue(info["canResume"])
                        self.assertTrue(info["canRollback"])

                        if action == "resume":
                            res = wtx.resume_transaction(fix["local_home"], rid)
                            self.assertEqual(res["mode"], "git")
                            self._assert_git_post_state(fix)
                            post_info = wtx.inspect_transaction(fix["local_home"], rid)
                            self.assertEqual(post_info["phase"], wtx.PHASE_WORKSPACE_COMMITTED)
                        else:
                            rb = wtx.rollback_transaction(fix["local_home"], rid)
                            self.assertTrue(rb["ok"])
                            self.assertEqual(rb["phase"], wtx.PHASE_ROLLED_BACK)
                            self._assert_git_pre_state(fix)

    def test_git_sigkill_subprocess_all_points_cold_resume_and_rollback(self):
        for point in ALL_TX_FAULT_POINTS:
            for action in ("resume", "rollback"):
                with self.subTest(point=point, action=action):
                    with tempfile.TemporaryDirectory() as tmp:
                        fix = _build_git_fixture(tmp, existing_target=True)
                        rid = "relay_sigkill_%s_%s" % (point, action)
                        kill_proc = _run_child_apply(
                            fix["local_home"],
                            rid,
                            "proj_1",
                            "sha_pkg_1",
                            fix["staging"],
                            fix["target"],
                            fault_point=point,
                            fault_mode="sigkill",
                        )
                        if os.name == "nt":
                            self.assertEqual(kill_proc.returncode, 137)
                        else:
                            self.assertEqual(kill_proc.returncode, -signal.SIGKILL)

                        inspect_proc = _run_child_recover("inspect", fix["local_home"], rid)
                        self.assertEqual(inspect_proc.returncode, 0, inspect_proc.stderr)
                        inspect_data = json.loads(inspect_proc.stdout)["data"]
                        self.assertTrue(inspect_data["canResume"])
                        self.assertTrue(inspect_data["canRollback"])

                        rec_proc = _run_child_recover(action, fix["local_home"], rid)
                        self.assertEqual(rec_proc.returncode, 0, rec_proc.stderr + rec_proc.stdout)
                        if action == "resume":
                            self._assert_git_post_state(fix)
                        else:
                            self._assert_git_pre_state(fix)

    def test_git_new_directory_sigkill_and_recovery(self):
        for point in ("tx_after_journal_before_apply", "tx_after_workspace_commit"):
            for action in ("resume", "rollback"):
                with self.subTest(point=point, action=action):
                    with tempfile.TemporaryDirectory() as tmp:
                        fix = _build_git_fixture(tmp, existing_target=False)
                        rid = "relay_newgit_%s_%s" % (point, action)
                        kill_proc = _run_child_apply(
                            fix["local_home"],
                            rid,
                            "proj_new_git",
                            "sha_new_git",
                            fix["staging"],
                            fix["target"],
                            fault_point=point,
                            fault_mode="sigkill",
                        )
                        self.assertEqual(
                            kill_proc.returncode,
                            137 if os.name == "nt" else -signal.SIGKILL,
                        )
                        rec_proc = _run_child_recover(action, fix["local_home"], rid)
                        self.assertEqual(rec_proc.returncode, 0, rec_proc.stderr + rec_proc.stdout)
                        if action == "resume":
                            self.assertEqual(_snapshot_tree(fix["target"]), fix["expected_post_files"])
                            self.assertEqual(_capture_git_state(fix["target"]), fix["expected_post_git"])
                        else:
                            self.assertFalse(os.path.lexists(fix["target"]))


class TestWorkspaceTxFolderMode(unittest.TestCase):
    def test_folder_in_process_and_sigkill_faults_resume_and_rollback(self):
        for mode in ("in_process", "sigkill"):
            for point in FOLDER_TX_FAULT_POINTS:
                for action in ("resume", "rollback"):
                    with self.subTest(mode=mode, point=point, action=action):
                        with tempfile.TemporaryDirectory() as tmp:
                            fix = _build_folder_fixture(tmp, existing_target=True)
                            rid = "relay_folder_%s_%s_%s" % (mode, point, action)
                            if mode == "in_process":
                                old_pt = os.environ.get("RELAY_DESK_FAULT_POINT")
                                old_fm = os.environ.get("RELAY_DESK_FAULT_MODE")
                                os.environ["RELAY_DESK_FAULT_POINT"] = point
                                os.environ.pop("RELAY_DESK_FAULT_MODE", None)
                                try:
                                    with self.assertRaises(RuntimeError):
                                        wtx.prepare_and_apply_workspace(
                                            fix["local_home"],
                                            rid,
                                            "proj_folder",
                                            "sha_folder",
                                            fix["staging"],
                                            fix["target"],
                                        )
                                finally:
                                    if old_pt is None:
                                        os.environ.pop("RELAY_DESK_FAULT_POINT", None)
                                    else:
                                        os.environ["RELAY_DESK_FAULT_POINT"] = old_pt
                                    if old_fm is None:
                                        os.environ.pop("RELAY_DESK_FAULT_MODE", None)
                                    else:
                                        os.environ["RELAY_DESK_FAULT_MODE"] = old_fm

                                if action == "resume":
                                    wtx.resume_transaction(fix["local_home"], rid)
                                else:
                                    wtx.rollback_transaction(fix["local_home"], rid)
                            else:
                                kill_proc = _run_child_apply(
                                    fix["local_home"],
                                    rid,
                                    "proj_folder",
                                    "sha_folder",
                                    fix["staging"],
                                    fix["target"],
                                    fault_point=point,
                                    fault_mode="sigkill",
                                )
                                self.assertEqual(
                                    kill_proc.returncode,
                                    137 if os.name == "nt" else -signal.SIGKILL,
                                )
                                rec_proc = _run_child_recover(action, fix["local_home"], rid)
                                self.assertEqual(rec_proc.returncode, 0, rec_proc.stderr + rec_proc.stdout)

                            if action == "resume":
                                self.assertEqual(_snapshot_tree(fix["target"]), fix["expected_post_files"])
                                self.assertEqual(
                                    _read_bytes(fix["target"], ".env"),
                                    b"RECEIVER_FOLDER_SECRET=keep\n",
                                )
                            else:
                                self.assertEqual(_snapshot_tree(fix["target"]), fix["pre_files"])
                                self.assertEqual(
                                    _read_bytes(fix["target"], ".env"),
                                    b"RECEIVER_FOLDER_SECRET=keep\n",
                                )

    def test_folder_new_directory_sigkill_resume_and_rollback(self):
        for point in ("tx_after_journal_before_apply", "tx_after_workspace_commit"):
            for action in ("resume", "rollback"):
                with self.subTest(point=point, action=action):
                    with tempfile.TemporaryDirectory() as tmp:
                        fix = _build_folder_fixture(tmp, existing_target=False)
                        rid = "relay_newfolder_%s_%s" % (point, action)
                        kill_proc = _run_child_apply(
                            fix["local_home"],
                            rid,
                            "proj_new_folder",
                            "sha_nf",
                            fix["staging"],
                            fix["target"],
                            fault_point=point,
                            fault_mode="sigkill",
                        )
                        self.assertEqual(
                            kill_proc.returncode,
                            137 if os.name == "nt" else -signal.SIGKILL,
                        )
                        rec_proc = _run_child_recover(action, fix["local_home"], rid)
                        self.assertEqual(rec_proc.returncode, 0, rec_proc.stderr + rec_proc.stdout)
                        if action == "resume":
                            self.assertEqual(_snapshot_tree(fix["target"]), fix["expected_post_files"])
                        else:
                            self.assertFalse(os.path.lexists(fix["target"]))


class TestWorkspaceTxDriftPreservation(unittest.TestCase):
    def _sigkill_git(self, tmp: str, rid: str, fault_point: str) -> Dict[str, Any]:
        fix = _build_git_fixture(tmp, existing_target=True)
        kill_proc = _run_child_apply(
            fix["local_home"],
            rid,
            "proj_drift",
            "sha_drift",
            fix["staging"],
            fix["target"],
            fault_point=fault_point,
            fault_mode="sigkill",
        )
        self.assertEqual(
            kill_proc.returncode,
            137 if os.name == "nt" else -signal.SIGKILL,
        )
        return fix

    def _assert_conflict_blocks_both_and_preserves_workspace(
        self, local_home: Path, rid: str, target_path: str
    ) -> Dict[str, Any]:
        snap_before = _snapshot_tree(target_path)
        git_before = _capture_git_state(target_path)

        info = wtx.inspect_transaction(local_home, rid)
        self.assertEqual(info["state"], "conflict")
        self.assertFalse(info["canResume"])
        self.assertFalse(info["canRollback"])
        self.assertTrue(info["reasons"])

        with self.assertRaises(wtx.RecoveryConflictError) as res_ctx:
            wtx.resume_transaction(local_home, rid)
        self.assertEqual(res_ctx.exception.code, "recovery_conflict")
        self.assertEqual(_snapshot_tree(target_path), snap_before)
        self.assertEqual(_capture_git_state(target_path), git_before)

        with self.assertRaises(wtx.RecoveryConflictError) as rb_ctx:
            wtx.rollback_transaction(local_home, rid)
        self.assertEqual(rb_ctx.exception.code, "recovery_conflict")
        self.assertEqual(_snapshot_tree(target_path), snap_before)
        self.assertEqual(_capture_git_state(target_path), git_before)
        return info

    def test_drift_a_user_adds_new_file_after_sigkill(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = "relay_drift_add"
            fix = self._sigkill_git(tmp, rid, "tx_mid_files")
            _write_bytes(fix["target"], "notes_after_crash.txt", b"important user note\n")

            info = self._assert_conflict_blocks_both_and_preserves_workspace(
                fix["local_home"], rid, fix["target"]
            )
            self.assertTrue(any("notes_after_crash.txt" in r for r in info["reasons"]))
            self.assertEqual(_read_bytes(fix["target"], "notes_after_crash.txt"), b"important user note\n")

    def test_drift_b_user_modifies_existing_file_after_sigkill(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = "relay_drift_mod"
            fix = self._sigkill_git(tmp, rid, "tx_after_files_before_git")
            _write_bytes(fix["target"], "keep_unchanged.txt", b"USER_EDITED_AFTER_CRASH\n")

            info = self._assert_conflict_blocks_both_and_preserves_workspace(
                fix["local_home"], rid, fix["target"]
            )
            self.assertTrue(any("keep_unchanged.txt" in r for r in info["reasons"]))
            self.assertEqual(
                _read_bytes(fix["target"], "keep_unchanged.txt"),
                b"USER_EDITED_AFTER_CRASH\n",
            )

    def test_drift_c_user_deletes_untouched_file_after_sigkill(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = "relay_drift_del"
            fix = self._sigkill_git(tmp, rid, "tx_mid_files")
            target_file = os.path.join(fix["target"], "keep_unchanged.txt")
            self.assertTrue(os.path.isfile(target_file))
            os.unlink(target_file)

            info = self._assert_conflict_blocks_both_and_preserves_workspace(
                fix["local_home"], rid, fix["target"]
            )
            self.assertTrue(any("keep_unchanged.txt" in r for r in info["reasons"]))
            # Deletion must remain preserved (file must NOT be resurrected)
            self.assertFalse(os.path.lexists(target_file))

    def test_drift_d_user_stages_change_with_git_add_after_sigkill(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = "relay_drift_git_add"
            # At tx_after_files_before_git, worktree files match postFiles (including unstaged_mod.txt),
            # while .git/index still holds preIndexEntries. Running `git add` alters ONLY the index!
            fix = self._sigkill_git(tmp, rid, "tx_after_files_before_git")
            _git(fix["target"], "add", "unstaged_mod.txt")

            info = self._assert_conflict_blocks_both_and_preserves_workspace(
                fix["local_home"], rid, fix["target"]
            )
            self.assertTrue(any("index" in r or "暂存区" in r for r in info["reasons"]))
            # Verify the user's newly staged blob remains in index untouched
            staged_diff = _capture_git_state(fix["target"])["cachedDiff"]
            self.assertIn(b"unstaged v2 in worktree", staged_diff)

    def test_drift_ignores_preserved_env_and_gitignored_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = "relay_drift_preserved_ok"
            fix = self._sigkill_git(tmp, rid, "tx_mid_files")
            _write_bytes(fix["target"], ".env", b"RECEIVER_SECRET=updated-locally\n")
            _write_bytes(fix["target"], "build/local_cache.bin", b"UPDATED_BUILD_CACHE")

            info = wtx.inspect_transaction(fix["local_home"], rid)
            self.assertEqual(info["state"], "in_progress")
            self.assertTrue(info["canResume"])

            wtx.resume_transaction(fix["local_home"], rid)
            self.assertEqual(
                _read_bytes(fix["target"], ".env"),
                b"RECEIVER_SECRET=updated-locally\n",
            )
            self.assertEqual(
                _read_bytes(fix["target"], "build/local_cache.bin"),
                b"UPDATED_BUILD_CACHE",
            )


class TestWorkspaceTxCorruptedAndPostCommit(unittest.TestCase):
    def _assert_corrupted_fails_safely(self, local_home: Path, rid: str, target_path: str) -> None:
        snap_before = _snapshot_tree(target_path)
        git_before = _capture_git_state(target_path)

        info = wtx.inspect_transaction(local_home, rid)
        self.assertEqual(info["state"], "corrupted")
        self.assertFalse(info["canResume"])
        self.assertFalse(info["canRollback"])

        with self.assertRaises(wtx.CorruptedRecoveryError) as j_ctx:
            wtx.load_and_verify_journal(local_home, rid)
        self.assertEqual(j_ctx.exception.code, "recovery_corrupted")

        with self.assertRaises(wtx.CorruptedRecoveryError) as res_ctx:
            wtx.resume_transaction(local_home, rid)
        self.assertEqual(res_ctx.exception.code, "recovery_corrupted")

        with self.assertRaises(wtx.CorruptedRecoveryError) as rb_ctx:
            wtx.rollback_transaction(local_home, rid)
        self.assertEqual(rb_ctx.exception.code, "recovery_corrupted")

        self.assertEqual(_snapshot_tree(target_path), snap_before)
        self.assertEqual(_capture_git_state(target_path), git_before)

    def test_corrupted_journal_bad_sha_truncated_and_missing(self):
        for corruption in ("bad_sha", "truncated_json", "missing_journal"):
            with self.subTest(corruption=corruption):
                with tempfile.TemporaryDirectory() as tmp:
                    fix = _build_git_fixture(tmp, existing_target=True)
                    rid = "relay_corrupt_%s" % corruption
                    kill_proc = _run_child_apply(
                        fix["local_home"],
                        rid,
                        "proj_corrupt",
                        "sha_corrupt",
                        fix["staging"],
                        fix["target"],
                        fault_point="tx_mid_files",
                        fault_mode="sigkill",
                    )
                    self.assertEqual(
                        kill_proc.returncode,
                        137 if os.name == "nt" else -signal.SIGKILL,
                    )
                    jpath = fix["local_home"] / "transactions" / rid / "journal.json"
                    self.assertTrue(jpath.is_file())

                    if corruption == "bad_sha":
                        with open(str(jpath), "r", encoding="utf-8") as fh:
                            data = json.load(fh)
                        data["journalSha256"] = "0" * 64
                        with open(str(jpath), "w", encoding="utf-8") as fh:
                            json.dump(data, fh)
                    elif corruption == "truncated_json":
                        with open(str(jpath), "wb") as fh:
                            fh.write(b'{"formatVersion": 1, "relayId": ')
                    else:
                        os.unlink(str(jpath))

                    self._assert_corrupted_fails_safely(fix["local_home"], rid, fix["target"])

    def test_workspace_committed_post_commit_edit_preserves_workspace_on_resume_and_blocks_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            fix = _build_git_fixture(tmp, existing_target=True)
            rid = "relay_post_commit_edit"
            kill_proc = _run_child_apply(
                fix["local_home"],
                rid,
                "proj_post",
                "sha_post",
                fix["staging"],
                fix["target"],
                fault_point="tx_after_workspace_commit",
                fault_mode="sigkill",
            )
            self.assertEqual(
                kill_proc.returncode,
                137 if os.name == "nt" else -signal.SIGKILL,
            )

            # User edits workspace AFTER WORKSPACE_COMMITTED
            _write_bytes(fix["target"], "README.md", b"# Post-commit user work\n")
            _write_bytes(fix["target"], "post_commit_extra.txt", b"keep this new file\n")
            snap_after_edit = _snapshot_tree(fix["target"])

            info = wtx.inspect_transaction(fix["local_home"], rid)
            self.assertEqual(info["state"], "workspace_committed")
            self.assertEqual(info["phase"], wtx.PHASE_WORKSPACE_COMMITTED)
            self.assertTrue(info["canResume"])
            self.assertFalse(info["canRollback"])
            self.assertTrue(info["reasons"])

            # Resume must NOT overwrite workspace since workspace commit already completed
            res = wtx.resume_transaction(fix["local_home"], rid)
            self.assertEqual(res["mode"], "git")
            self.assertEqual(_snapshot_tree(fix["target"]), snap_after_edit)
            self.assertEqual(_read_bytes(fix["target"], "README.md"), b"# Post-commit user work\n")
            self.assertEqual(
                _read_bytes(fix["target"], "post_commit_extra.txt"),
                b"keep this new file\n",
            )

            # Rollback must detect post-commit edit and refuse to clobber it
            with self.assertRaises(wtx.RecoveryConflictError) as ctx:
                wtx.rollback_transaction(fix["local_home"], rid)
            self.assertEqual(ctx.exception.code, "recovery_conflict")
            self.assertEqual(_snapshot_tree(fix["target"]), snap_after_edit)


if __name__ == "__main__":
    unittest.main()
