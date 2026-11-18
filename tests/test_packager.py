import io
import os
import stat
import subprocess
import tarfile
import tempfile
import unittest
import unicodedata
from pathlib import Path

from relaydesk import packager as P


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", "-c", "core.autocrlf=false", "-C", cwd] + list(args),
        check=True,
        capture_output=True,
    )


def _init_git_repo(path, branch="main"):
    os.makedirs(path, exist_ok=True)
    r = subprocess.run(["git", "init", "-b", branch, path], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["git", "init", path], check=True, capture_output=True)
        _git(path, "symbolic-ref", "HEAD", "refs/heads/%s" % branch)
    _git(path, "config", "user.email", "tester@example.com")
    _git(path, "config", "user.name", "Relay Tester")


def _write_bytes(root, rel_path, data, executable=False):
    full = os.path.join(root, *rel_path.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)
    if executable:
        os.chmod(full, 0o755)
    return full


class PackagerGitRoundTripTests(unittest.TestCase):
    def test_git_commits_staged_unstaged_empty_binary_untracked_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_a = os.path.join(tmp, "repo_a")
            _init_git_repo(repo_a, "feat/relay")

            _write_bytes(repo_a, ".gitignore", b".env\n.env.local\n")
            _write_bytes(repo_a, ".env.example", b"API_URL=https://example.com\n")
            _write_bytes(repo_a, "README.md", b"# Relay Project\n")
            _write_bytes(repo_a, "staged_mod.txt", b"base line\n")
            _write_bytes(repo_a, "staged_del.txt", b"delete in index\n")
            _write_bytes(repo_a, "both_mm.txt", b"v1 committed\n")
            _write_bytes(repo_a, "unstaged_mod.txt", b"original\n")
            _write_bytes(repo_a, "unstaged_del.txt", b"remove from worktree\n")
            _write_bytes(repo_a, "truncate_me.txt", b"non-empty before\n")
            _write_bytes(repo_a, "bin/tool.sh", b"#!/bin/sh\necho v1\n", executable=False)
            _write_bytes(repo_a, "HANDOFF.md", "已完成模块拆分，下一步补齐端到端验证。\n".encode("utf-8"))

            _git(repo_a, "add", ".")
            _git(repo_a, "commit", "-m", "initial commit")

            # Second commit + tag + side branch
            _git(repo_a, "tag", "v0.1.0")
            _git(repo_a, "checkout", "-b", "side-branch")
            _write_bytes(repo_a, "side.txt", b"side branch commit\n")
            _git(repo_a, "add", "side.txt")
            _git(repo_a, "commit", "-m", "side work")
            _git(repo_a, "checkout", "feat/relay")
            _write_bytes(repo_a, "README.md", b"# Relay Project\nCommitted v2\n")
            _git(repo_a, "add", "README.md")
            _git(repo_a, "commit", "-m", "second commit on feat/relay")

            # Staged changes: new text, modified text, 0-byte empty file, binary file,
            # deletion, executable mode change, MM file, AD file
            _write_bytes(repo_a, "staged_new.txt", b"brand new staged\n")
            _write_bytes(repo_a, "staged_mod.txt", b"staged modification\n")
            _write_bytes(repo_a, "empty_staged.txt", b"")
            binary_staged = bytes(range(256)) + b"\x00\xff\x10BINARY_STAGED"
            _write_bytes(repo_a, "assets/icon.bin", binary_staged)
            _write_bytes(repo_a, "both_mm.txt", b"v2 staged\n")
            _write_bytes(repo_a, "staged_then_deleted.txt", b"added then unlinked\n")
            os.chmod(os.path.join(repo_a, "bin", "tool.sh"), 0o755)
            _git(repo_a, "rm", "-q", "staged_del.txt")
            _git(
                repo_a,
                "add",
                "staged_new.txt",
                "staged_mod.txt",
                "empty_staged.txt",
                "assets/icon.bin",
                "both_mm.txt",
                "staged_then_deleted.txt",
                "bin/tool.sh",
            )

            # Unstaged changes on top of staged index
            _write_bytes(repo_a, "both_mm.txt", b"v3 unstaged worktree\n")
            os.unlink(os.path.join(repo_a, "staged_then_deleted.txt"))
            _write_bytes(repo_a, "unstaged_mod.txt", b"modified in worktree\x00with_nul\n")
            os.unlink(os.path.join(repo_a, "unstaged_del.txt"))
            _write_bytes(repo_a, "truncate_me.txt", b"")

            # Eligible untracked files
            _write_bytes(repo_a, "notes/todo.txt", "待办事项 123\n".encode("utf-8"))
            _write_bytes(repo_a, "scripts/local_helper.sh", b"#!/bin/sh\necho helper\n", executable=True)

            # Excluded secret files: make .env unreadable on POSIX and put a fake key marker inside
            # to prove packager NEVER opens/reads excluded .env files!
            fake_pk = ("-----BEGIN " + "OPENSSH PRIVATE KEY-----\nSECRET_NEVER_READ\n").encode("utf-8")
            env_a = _write_bytes(repo_a, ".env", b"DB_PASS=topsecret\n" + fake_pk)
            _write_bytes(repo_a, ".env.local", b"LOCAL_TOKEN=xyz\n")
            _write_bytes(repo_a, "auth.json", b'{"token":"secret"}')
            if os.name != "nt":
                os.chmod(env_a, 0o000)

            try:
                expected_head = _git(repo_a, "rev-parse", "HEAD").stdout.decode("utf-8").strip()
                expected_cached_diff = _git(repo_a, "diff", "--cached", "--binary", "--full-index").stdout
                expected_work_diff = _git(repo_a, "diff", "--binary", "--full-index").stdout
                raw_status_a = _git(repo_a, "status", "--porcelain").stdout.decode("utf-8")
                # auth.json is untracked & excluded by secret gate, so filter it from expected status on B
                expected_status_b = "\n".join(
                    ln for ln in raw_status_a.splitlines() if not ln.endswith(" auth.json")
                ) + "\n"

                tar_path = os.path.join(tmp, "payload.tar.gz")
                meta = {
                    "id": "p_demo",
                    "name": "Demo",
                    "lastTask": "实现打包器",
                    "nextAction": "运行单测",
                    "note": "注意保留本地 .env",
                    "verify": {"state": "pass", "cmd": "python3 -m unittest", "note": "ok"},
                }
                convs = {
                    "formatVersion": 1,
                    "projectId": "p_demo",
                    "entries": [{"tool": "claude", "sessionId": "s1", "contentVersion": "v1_abc", "turns": []}],
                }

                summary = P.build_payload(repo_a, tar_path, meta, convs, target_os="mac")
                self.assertEqual(summary["mode"], "git")
                self.assertEqual(summary["branch"], "feat/relay")
                self.assertEqual(summary["headCommit"], expected_head)
                self.assertIn(".env", summary["excludedFiles"])
                self.assertIn(".env.local", summary["excludedFiles"])
                self.assertIn("auth.json", summary["excludedFiles"])
                self.assertGreaterEqual(summary["stagedCount"], 8)
                self.assertGreaterEqual(summary["unstagedCount"], 5)
                self.assertEqual(summary["untrackedCount"], 2)

                staging = os.path.join(tmp, "staging")
                unpacked = P.extract_and_verify_payload(tar_path, staging)
                self.assertEqual(unpacked["metadata"]["lastTask"], "实现打包器")
                self.assertIn("已完成模块拆分", unpacked["metadata"]["handoffContent"])
                self.assertEqual(len(unpacked["conversations"]["entries"]), 1)

                # Pre-populate receiver target with its own local .env that MUST be preserved
                repo_b = os.path.join(tmp, "repo_b")
                _write_bytes(repo_b, ".env", b"RECEIVER_LOCAL_ENV=keep_me\n")
                _write_bytes(repo_b, ".env.production", b"PROD_SECRET=keep_me_too\n")

                P.check_target_clean_for_receive(repo_b, unpacked["manifest"])
                apply_res = P.apply_verified_payload(staging, repo_b)
                self.assertEqual(apply_res["mode"], "git")
                self.assertEqual(apply_res["headCommit"], expected_head)
                self.assertEqual(apply_res["branch"], "feat/relay")

                # Verify exact git state on B
                actual_head = _git(repo_b, "rev-parse", "HEAD").stdout.decode("utf-8").strip()
                actual_branch = _git(repo_b, "symbolic-ref", "--short", "HEAD").stdout.decode("utf-8").strip()
                actual_cached_diff = _git(repo_b, "diff", "--cached", "--binary", "--full-index").stdout
                actual_work_diff = _git(repo_b, "diff", "--binary", "--full-index").stdout
                actual_status_b = _git(repo_b, "status", "--porcelain").stdout.decode("utf-8")

                self.assertEqual(actual_head, expected_head)
                self.assertEqual(actual_branch, "feat/relay")
                self.assertEqual(actual_cached_diff, expected_cached_diff)
                self.assertEqual(actual_work_diff, expected_work_diff)
                # Filter .env.production (untracked secret on B) when comparing porcelain status
                filtered_actual_status = "\n".join(
                    ln for ln in actual_status_b.splitlines() if not ln.endswith(" .env.production")
                ) + "\n"
                self.assertEqual(filtered_actual_status, expected_status_b)

                # Side branch & tag also carried
                side_sha = _git(repo_b, "rev-parse", "refs/heads/side-branch").stdout.decode("utf-8").strip()
                self.assertTrue(len(side_sha) == 40)
                tag_sha = _git(repo_b, "rev-parse", "refs/tags/v0.1.0").stdout.decode("utf-8").strip()
                self.assertTrue(len(tag_sha) == 40)

                # Excluded files from A never arrived; B's own .env was preserved untouched!
                self.assertFalse(os.path.exists(os.path.join(repo_b, "auth.json")))
                self.assertFalse(os.path.exists(os.path.join(repo_b, ".env.local")))
                with open(os.path.join(repo_b, ".env"), "rb") as fh:
                    self.assertEqual(fh.read(), b"RECEIVER_LOCAL_ENV=keep_me\n")
                with open(os.path.join(repo_b, ".env.production"), "rb") as fh:
                    self.assertEqual(fh.read(), b"PROD_SECRET=keep_me_too\n")
                self.assertTrue(os.path.isfile(os.path.join(repo_b, ".env.example")))

                # Executable bits preserved on tracked & untracked scripts
                if os.name != "nt":
                    self.assertTrue(os.stat(os.path.join(repo_b, "bin", "tool.sh")).st_mode & stat.S_IXUSR)
                    self.assertTrue(
                        os.stat(os.path.join(repo_b, "scripts", "local_helper.sh")).st_mode & stat.S_IXUSR
                    )
            finally:
                if os.name != "nt" and os.path.exists(env_a):
                    os.chmod(env_a, 0o600)

    def test_unborn_zero_commit_git_repo_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_a = os.path.join(tmp, "unborn_a")
            _init_git_repo(repo_a, "unborn-main")

            _write_bytes(repo_a, "staged.txt", b"v1 staged\n")
            _write_bytes(repo_a, "empty.txt", b"")
            _git(repo_a, "add", "staged.txt", "empty.txt")
            _write_bytes(repo_a, "staged.txt", b"v2 unstaged\n")
            _write_bytes(repo_a, "untracked.md", b"# Draft\n")
            _write_bytes(repo_a, ".env", b"SECRET=123\n")

            expected_cached = _git(repo_a, "diff", "--cached", "--binary", "--full-index").stdout
            expected_work = _git(repo_a, "diff", "--binary", "--full-index").stdout

            tar_path = os.path.join(tmp, "unborn.tar.gz")
            summary = P.build_payload(repo_a, tar_path, {}, {})
            self.assertEqual(summary["mode"], "git")
            self.assertEqual(summary["branch"], "unborn-main")
            self.assertEqual(summary["headCommit"], "")
            self.assertIn(".env", summary["excludedFiles"])

            staging = os.path.join(tmp, "staging")
            unpacked = P.extract_and_verify_payload(tar_path, staging)
            repo_b = os.path.join(tmp, "unborn_b")
            P.check_target_clean_for_receive(repo_b, unpacked["manifest"])
            res = P.apply_verified_payload(staging, repo_b)

            self.assertEqual(res["mode"], "git")
            self.assertEqual(res["headCommit"], "")
            self.assertEqual(res["branch"], "unborn-main")

            # Verify repo_b is an unborn git repo on unborn-main with identical diffs & status
            rc_head = subprocess.run(["git", "-C", repo_b, "rev-parse", "--verify", "HEAD"], capture_output=True)
            self.assertNotEqual(rc_head.returncode, 0)
            self.assertEqual(
                _git(repo_b, "symbolic-ref", "--short", "HEAD").stdout.decode("utf-8").strip(),
                "unborn-main",
            )
            self.assertEqual(_git(repo_b, "diff", "--cached", "--binary", "--full-index").stdout, expected_cached)
            self.assertEqual(_git(repo_b, "diff", "--binary", "--full-index").stdout, expected_work)
            self.assertTrue(os.path.isfile(os.path.join(repo_b, "empty.txt")))
            self.assertEqual(os.path.getsize(os.path.join(repo_b, "empty.txt")), 0)
            self.assertTrue(os.path.isfile(os.path.join(repo_b, "untracked.md")))
            self.assertFalse(os.path.exists(os.path.join(repo_b, ".env")))

    def test_non_git_folder_snapshot_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "folder_a")
            _write_bytes(src, "app/main.py", b"print('hello')\n")
            _write_bytes(src, "run.sh", b"#!/bin/sh\npython3 app/main.py\n", executable=True)
            _write_bytes(src, "empty.dat", b"")
            _write_bytes(src, ".env.template", b"PORT=8080\n")
            _write_bytes(src, ".env", b"REAL_SECRET=never_pack\n")
            _write_bytes(src, "id_ed25519", b"ssh-private-key-material")

            tar_path = os.path.join(tmp, "folder.tar.gz")
            summary = P.build_payload(src, tar_path, {"lastTask": "folder sync"}, {"entries": []})
            self.assertEqual(summary["mode"], "folder")
            self.assertEqual(summary["fileCount"], 4)
            self.assertEqual(sorted(summary["excludedFiles"]), [".env", "id_ed25519"])
            self.assertEqual(set(summary["folderHashes"].keys()), {"app/main.py", "run.sh", "empty.dat", ".env.template"})

            staging = os.path.join(tmp, "staging")
            unpacked = P.extract_and_verify_payload(tar_path, staging)

            dst = os.path.join(tmp, "folder_b")
            _write_bytes(dst, ".env", b"B_LOCAL_ENV=1\n")
            P.check_target_clean_for_receive(dst, unpacked["manifest"])
            res = P.apply_verified_payload(staging, dst)
            self.assertEqual(res["mode"], "folder")
            self.assertEqual(res["folderHashes"], summary["folderHashes"])
            self.assertFalse(os.path.exists(os.path.join(dst, "id_ed25519")))
            with open(os.path.join(dst, ".env"), "rb") as fh:
                self.assertEqual(fh.read(), b"B_LOCAL_ENV=1\n")
            if os.name != "nt":
                self.assertTrue(os.stat(os.path.join(dst, "run.sh")).st_mode & stat.S_IXUSR)


class PackagerUnicodeAndPathSafetyTests(unittest.TestCase):
    def test_chinese_paths_and_nfd_to_nfc_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, "中文工作区", "双机接力项目")
            os.makedirs(proj, exist_ok=True)
            nfd_rel = unicodedata.normalize("NFD", "中文项目/子目录/测试文件_café.txt")
            nfc_rel = unicodedata.normalize("NFC", nfd_rel)
            _write_bytes(proj, nfd_rel, "跨平台中文与 NFC 归一化验证\n".encode("utf-8"))

            tar_path = os.path.join(tmp, "zh.tar.gz")
            summary = P.build_payload(proj, tar_path, {}, {}, target_os="windows")
            self.assertIn(nfc_rel, summary["folderHashes"])
            for k in summary["folderHashes"]:
                self.assertEqual(k, unicodedata.normalize("NFC", k))

            staging = os.path.join(tmp, "staging")
            unpacked = P.extract_and_verify_payload(tar_path, staging)
            dst = os.path.join(tmp, "接收目录", "项目副本")
            P.check_target_clean_for_receive(dst, unpacked["manifest"])
            P.apply_verified_payload(staging, dst)

            target_file = os.path.join(dst, *nfc_rel.split("/"))
            self.assertTrue(os.path.isfile(target_file))
            with open(target_file, "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "跨平台中文与 NFC 归一化验证\n")

    def test_case_collision_rejected_in_build_and_receive(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_a = os.path.join(tmp, "case_repo")
            _init_git_repo(repo_a)
            # Construct Foo.txt and foo.txt directly in Git index so this works even on macOS APFS!
            b1 = _git(repo_a, "hash-object", "-w", "--stdin") if False else subprocess.run(
                ["git", "-C", repo_a, "hash-object", "-w", "--stdin"],
                input=b"upper\n",
                check=True,
                capture_output=True,
            ).stdout.decode("utf-8").strip()
            b2 = subprocess.run(
                ["git", "-C", repo_a, "hash-object", "-w", "--stdin"],
                input=b"lower\n",
                check=True,
                capture_output=True,
            ).stdout.decode("utf-8").strip()
            _git(repo_a, "update-index", "--add", "--cacheinfo", "100644,%s,Foo.txt" % b1)
            _git(repo_a, "update-index", "--add", "--cacheinfo", "100644,%s,foo.txt" % b2)
            tree = _git(repo_a, "write-tree").stdout.decode("utf-8").strip()
            commit = _git(repo_a, "commit-tree", tree, "-m", "case collision commit").stdout.decode("utf-8").strip()
            _git(repo_a, "update-ref", "HEAD", commit)

            tar_mac = os.path.join(tmp, "bad_mac.tar.gz")
            with self.assertRaises(P.PathConflictError) as ctx_mac:
                P.build_payload(repo_a, tar_mac, {}, {}, target_os="mac")
            self.assertFalse(os.path.exists(tar_mac))
            self.assertIn(("Foo.txt", "foo.txt"), ctx_mac.exception.conflicts)

            tar_win = os.path.join(tmp, "bad_win.tar.gz")
            with self.assertRaises(P.PathConflictError):
                P.build_payload(repo_a, tar_win, {}, {}, target_os="windows")

            # Build for Linux (case-sensitive): archive extracts safely even on macOS APFS!
            tar_linux = os.path.join(tmp, "ok_linux.tar.gz")
            P.build_payload(repo_a, tar_linux, {}, {}, target_os="linux")
            staging = os.path.join(tmp, "staging")
            unpacked = P.extract_and_verify_payload(tar_linux, staging)

            # If local test temp dir is case-insensitive (macOS default APFS / Windows),
            # check_target_clean_for_receive dynamically detects it and raises PathConflictError!
            target_b = os.path.join(tmp, "target_b")
            if P._is_fs_case_insensitive(target_b):
                with self.assertRaises(P.PathConflictError):
                    P.check_target_clean_for_receive(target_b, unpacked["manifest"])
                self.assertFalse(os.path.exists(target_b))

    def test_windows_reserved_names_and_invalid_chars_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, "win_bad")
            _write_bytes(proj, "docs/CON.txt", b"reserved device\n")
            tar_out = os.path.join(tmp, "out.tar.gz")
            with self.assertRaises(P.PathConflictError):
                P.build_payload(proj, tar_out, {}, {}, target_os="windows")

    def test_symlink_escape_rejected(self):
        if os.name == "nt":
            self.skipTest("POSIX symlink test")
        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "outside.txt")
            with open(outside, "w", encoding="utf-8") as fh:
                fh.write("outside\n")
            proj = os.path.join(tmp, "proj")
            os.makedirs(proj, exist_ok=True)
            os.symlink(outside, os.path.join(proj, "escape.txt"))
            with self.assertRaises(P.PathConflictError):
                P.build_payload(proj, os.path.join(tmp, "p.tar.gz"), {}, {})


class PackagerSecretGateTests(unittest.TestCase):
    def test_staged_secret_filename_raises_secret_leak_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            _init_git_repo(repo)
            _write_bytes(repo, ".env", b"API_KEY=123\n")
            _git(repo, "add", "-f", ".env")

            with self.assertRaises(P.SecretLeakError) as ctx:
                P.scan_secrets_before_pack(repo)
            self.assertTrue(any(f["path"] == ".env" for f in ctx.exception.findings))

    def test_committed_and_deleted_private_key_in_git_history_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo_hist")
            _init_git_repo(repo)
            pk_block = ("-----BEGIN " + "OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU=\n-----END " + "OPENSSH PRIVATE KEY-----\n").encode("utf-8")
            _write_bytes(repo, "config/deploy.txt", pk_block)
            _git(repo, "add", "config/deploy.txt")
            _git(repo, "commit", "-m", "accidentally committed key")
            bad_commit = _git(repo, "rev-parse", "HEAD").stdout.decode("utf-8").strip()

            # Even if removed in a subsequent commit, git bundle --all still carries it!
            _git(repo, "rm", "config/deploy.txt")
            _write_bytes(repo, "README.md", b"clean head\n")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "remove key in head")

            tar_path = os.path.join(tmp, "out.tar.gz")
            with self.assertRaises(P.SecretLeakError) as ctx:
                P.build_payload(repo, tar_path, {}, {})
            self.assertFalse(os.path.exists(tar_path))
            commits = [f.get("commit") for f in ctx.exception.findings]
            self.assertIn(bad_commit, commits)

    def test_committed_env_in_git_history_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo_env_hist")
            _init_git_repo(repo)
            _write_bytes(repo, "sub/.env.production", b"PROD=1\n")
            _git(repo, "add", "-f", "sub/.env.production")
            _git(repo, "commit", "-m", "add env")
            bad_commit = _git(repo, "rev-parse", "HEAD").stdout.decode("utf-8").strip()
            _git(repo, "rm", "sub/.env.production")
            _git(repo, "commit", "-m", "delete env")

            with self.assertRaises(P.SecretLeakError) as ctx:
                P.scan_secrets_before_pack(repo)
            self.assertTrue(
                any(f["path"] == "sub/.env.production" and f["commit"] == bad_commit for f in ctx.exception.findings)
            )


class PackagerDirtyTargetAndTamperTests(unittest.TestCase):
    def test_dirty_git_target_uncommitted_and_diverged_commit_preserved_and_into_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_a = os.path.join(tmp, "repo_a")
            _init_git_repo(repo_a)
            _write_bytes(repo_a, "app.py", b"v1\n")
            _git(repo_a, "add", "app.py")
            _git(repo_a, "commit", "-m", "c1")

            # Clone/apply initial relay to repo_b
            tar1 = os.path.join(tmp, "r1.tar.gz")
            P.build_payload(repo_a, tar1, {}, {})
            st1 = os.path.join(tmp, "st1")
            u1 = P.extract_and_verify_payload(tar1, st1)
            repo_b = os.path.join(tmp, "repo_b")
            P.check_target_clean_for_receive(repo_b, u1["manifest"])
            P.apply_verified_payload(st1, repo_b)

            # Advance repo_a to c2
            _write_bytes(repo_a, "app.py", b"v2 from A\n")
            _git(repo_a, "add", "app.py")
            _git(repo_a, "commit", "-m", "c2")
            tar2 = os.path.join(tmp, "r2.tar.gz")
            P.build_payload(repo_a, tar2, {}, {})
            st2 = os.path.join(tmp, "st2")
            u2 = P.extract_and_verify_payload(tar2, st2)

            # 1. Uncommitted local edit on repo_b -> TargetDirtyError, repo_b untouched!
            _write_bytes(repo_b, "app.py", b"local unsaved edit on B\n")
            with self.assertRaises(P.TargetDirtyError) as ctx1:
                P.check_target_clean_for_receive(repo_b, u2["manifest"])
            self.assertEqual(ctx1.exception.target_path, repo_b)
            with open(os.path.join(repo_b, "app.py"), "rb") as fh:
                self.assertEqual(fh.read(), b"local unsaved edit on B\n")

            # 2. Diverged local commit on repo_b -> TargetDirtyError, repo_b & refs untouched!
            _git(repo_b, "add", "app.py")
            _git(repo_b, "commit", "-m", "diverged B commit")
            b_head = _git(repo_b, "rev-parse", "HEAD").stdout.decode("utf-8").strip()
            refs_before = _git(repo_b, "for-each-ref").stdout

            with self.assertRaises(P.TargetDirtyError):
                P.check_target_clean_for_receive(repo_b, u2["manifest"])
            self.assertEqual(_git(repo_b, "rev-parse", "HEAD").stdout.decode("utf-8").strip(), b_head)
            self.assertEqual(_git(repo_b, "for-each-ref").stdout, refs_before)

            # 3. Receiving into a new `--into` directory succeeds while repo_b keeps local work!
            repo_b_into = os.path.join(tmp, "repo_b_into")
            P.check_target_clean_for_receive(repo_b_into, u2["manifest"])
            P.apply_verified_payload(st2, repo_b_into)
            with open(os.path.join(repo_b_into, "app.py"), "rb") as fh:
                self.assertEqual(fh.read(), b"v2 from A\n")
            with open(os.path.join(repo_b, "app.py"), "rb") as fh:
                self.assertEqual(fh.read(), b"local unsaved edit on B\n")

    def test_dirty_folder_target_protected_and_into_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            _write_bytes(src, "doc.md", b"v1\n")
            tar1 = os.path.join(tmp, "f1.tar.gz")
            s1 = P.build_payload(src, tar1, {}, {})
            st1 = os.path.join(tmp, "st1")
            u1 = P.extract_and_verify_payload(tar1, st1)

            dst = os.path.join(tmp, "dst")
            P.check_target_clean_for_receive(dst, u1["manifest"])
            r1 = P.apply_verified_payload(st1, dst)

            # Prepare v2 on src
            _write_bytes(src, "doc.md", b"v2\n")
            tar2 = os.path.join(tmp, "f2.tar.gz")
            P.build_payload(src, tar2, {}, {})
            st2 = os.path.join(tmp, "st2")
            u2 = P.extract_and_verify_payload(tar2, st2)

            # Clean baseline passes
            P.check_target_clean_for_receive(dst, u2["manifest"], expected_folder_hashes=r1["folderHashes"])

            # Local edit in dst raises TargetDirtyError
            _write_bytes(dst, "doc.md", b"local edit\n")
            with self.assertRaises(P.TargetDirtyError):
                P.check_target_clean_for_receive(dst, u2["manifest"], expected_folder_hashes=r1["folderHashes"])
            with open(os.path.join(dst, "doc.md"), "rb") as fh:
                self.assertEqual(fh.read(), b"local edit\n")

            # Receiving into new directory succeeds
            dst_into = os.path.join(tmp, "dst_into")
            P.check_target_clean_for_receive(dst_into, u2["manifest"])
            P.apply_verified_payload(st2, dst_into)
            with open(os.path.join(dst_into, "doc.md"), "rb") as fh:
                self.assertEqual(fh.read(), b"v2\n")

    def test_malicious_traversal_and_corrupted_sha_rejected_in_extract(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 1. Path traversal tar member rejected
            evil_tar = os.path.join(tmp, "evil.tar.gz")
            with tarfile.open(evil_tar, "w:gz") as tf:
                data = b"pwned"
                info = tarfile.TarInfo(name="../escaped.txt")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            with self.assertRaises(P.PackageError):
                P.extract_and_verify_payload(evil_tar, os.path.join(tmp, "stage_evil"))
            self.assertFalse(os.path.exists(os.path.join(tmp, "escaped.txt")))

            # 2. Tampered file bytes inside tar.gz caught by manifest SHA-256 verification
            src = os.path.join(tmp, "src")
            _write_bytes(src, "hello.txt", b"authentic\n")
            good_tar = os.path.join(tmp, "good.tar.gz")
            P.build_payload(src, good_tar, {}, {})

            tampered_tar = os.path.join(tmp, "tampered.tar.gz")
            with tarfile.open(good_tar, "r:gz") as rin, tarfile.open(tampered_tar, "w:gz") as wout:
                for m in rin.getmembers():
                    raw = rin.extractfile(m).read()
                    if m.name.startswith("blobs/"):
                        raw = b"tampered!\n"
                    info = tarfile.TarInfo(name=m.name)
                    info.size = len(raw)
                    wout.addfile(info, io.BytesIO(raw))

            with self.assertRaises(P.PackageError):
                P.extract_and_verify_payload(tampered_tar, os.path.join(tmp, "stage_tampered"))

    def test_a_to_b_to_a_roundtrip_with_dirty_sender_baseline_and_post_send_edit_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_a = os.path.join(tmp, "repo_a")
            _init_git_repo(repo_a)
            _write_bytes(repo_a, "base.txt", b"commit 1\n")
            _git(repo_a, "add", "base.txt")
            _git(repo_a, "commit", "-m", "c1")

            # A creates staged + unstaged + untracked changes and packages Relay 1
            _write_bytes(repo_a, "staged_a.txt", b"staged on A\n")
            _git(repo_a, "add", "staged_a.txt")
            _write_bytes(repo_a, "base.txt", b"unstaged on A\n")
            _write_bytes(repo_a, "untracked_a.txt", b"untracked on A\n")
            _write_bytes(repo_a, ".env", b"A_ENV_SECRET=keep\n")

            tar1 = os.path.join(tmp, "r1.tar.gz")
            sum1 = P.build_payload(repo_a, tar1, {}, {})
            st1 = os.path.join(tmp, "st1")
            u1 = P.extract_and_verify_payload(tar1, st1)

            # B receives Relay 1, commits work, adds new staged/unstaged state, packages Relay 2
            repo_b = os.path.join(tmp, "repo_b")
            P.check_target_clean_for_receive(repo_b, u1["manifest"])
            P.apply_verified_payload(st1, repo_b)

            _git(repo_b, "add", ".")
            _git(repo_b, "commit", "-m", "c2 committed on B")
            _write_bytes(repo_b, "from_b_staged.txt", b"staged on B\n")
            _git(repo_b, "add", "from_b_staged.txt")
            _write_bytes(repo_b, "base.txt", b"further unstaged on B\n")

            tar2 = os.path.join(tmp, "r2.tar.gz")
            P.build_payload(repo_b, tar2, {}, {})
            st2 = os.path.join(tmp, "st2")
            u2 = P.extract_and_verify_payload(tar2, st2)

            # If A made ANY extra edit after sending Relay 1, TargetDirtyError protects A!
            _write_bytes(repo_a, "untracked_a.txt", b"edited after handoff!\n")
            with self.assertRaises(P.TargetDirtyError):
                P.check_target_clean_for_receive(
                    repo_a,
                    u2["manifest"],
                    expected_base_head=sum1["headCommit"],
                    expected_folder_hashes=sum1["folderHashes"],
                )

            # Restore A to exact state at Relay 1 send time -> A cleanly receives Relay 2!
            _write_bytes(repo_a, "untracked_a.txt", b"untracked on A\n")
            P.check_target_clean_for_receive(
                repo_a,
                u2["manifest"],
                expected_base_head=sum1["headCommit"],
                expected_folder_hashes=sum1["folderHashes"],
            )
            P.apply_verified_payload(st2, repo_a)

            self.assertEqual(
                _git(repo_a, "rev-parse", "HEAD").stdout,
                _git(repo_b, "rev-parse", "HEAD").stdout,
            )
            self.assertEqual(
                _git(repo_a, "diff", "--cached", "--binary").stdout,
                _git(repo_b, "diff", "--cached", "--binary").stdout,
            )
            self.assertEqual(
                _git(repo_a, "diff", "--binary").stdout,
                _git(repo_b, "diff", "--binary").stdout,
            )
            with open(os.path.join(repo_a, ".env"), "rb") as fh:
                self.assertEqual(fh.read(), b"A_ENV_SECRET=keep\n")

    def test_detached_head_and_git_chinese_paths_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_a = os.path.join(tmp, "det_a")
            _init_git_repo(repo_a)
            _write_bytes(repo_a, "中文项目/子目录/测试文件.txt", "第一版\n".encode("utf-8"))
            _git(repo_a, "add", ".")
            _git(repo_a, "commit", "-m", "c1")
            c1 = _git(repo_a, "rev-parse", "HEAD").stdout.decode("utf-8").strip()

            _write_bytes(repo_a, "中文项目/子目录/测试文件.txt", "第二版\n".encode("utf-8"))
            _git(repo_a, "add", ".")
            _git(repo_a, "commit", "-m", "c2")

            # Detach HEAD at c1 and stage a Chinese NFD filename
            _git(repo_a, "checkout", "--detach", c1)
            nfd_rel = unicodedata.normalize("NFD", "中文项目/子目录/说明_café.md")
            _write_bytes(repo_a, nfd_rel, "分离头指针测试\n".encode("utf-8"))
            _git(repo_a, "add", ".")

            tar_p = os.path.join(tmp, "det.tar.gz")
            s = P.build_payload(repo_a, tar_p, {}, {})
            self.assertEqual(s["branch"], "")
            self.assertEqual(s["headCommit"], c1)

            st = os.path.join(tmp, "st")
            u = P.extract_and_verify_payload(tar_p, st)
            repo_b = os.path.join(tmp, "det_b")
            P.check_target_clean_for_receive(repo_b, u["manifest"])
            res = P.apply_verified_payload(st, repo_b)
            self.assertEqual(res["branch"], "")
            self.assertEqual(res["headCommit"], c1)
            self.assertEqual(_git(repo_b, "rev-parse", "HEAD").stdout.decode("utf-8").strip(), c1)
            nfc_rel = unicodedata.normalize("NFC", nfd_rel)
            self.assertTrue(os.path.isfile(os.path.join(repo_b, *nfc_rel.split("/"))))


if __name__ == "__main__":
    unittest.main()
