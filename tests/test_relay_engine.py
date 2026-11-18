# -*- coding: utf-8 -*-
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from relaydesk import conversations
from relaydesk import crypto
from relaydesk import ledger as L
from relaydesk import packager
from relaydesk import relay_engine
from relaydesk import scan
from relaydesk import storage


def _git(cwd, *args):
    r = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-C", str(cwd)] + list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError("git %r failed: %s" % (args, r.stderr))
    return r.stdout


def _git_bytes(cwd, *args):
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(cwd)] + list(args),
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError("git %r failed" % (args,))
    return r.stdout


def _write_claude_session(user_home: Path, cwd: str, sid: str, title: str, turns):
    proj_dir = user_home / ".claude" / "projects" / scan.enc_path(cwd)
    proj_dir.mkdir(parents=True, exist_ok=True)
    fp = proj_dir / ("%s.jsonl" % sid)
    lines = [{"type": "summary", "summary": title}]
    for role, text in turns:
        lines.append({"type": role, "message": {"role": role, "content": text}, "timestamp": "2026-11-18T01:00:00Z"})
    fp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    return fp


def _write_codex_session(user_home: Path, cwd: str, sid: str, title: str, turns):
    sess_dir = user_home / ".codex" / "sessions" / "2026" / "11" / "18"
    sess_dir.mkdir(parents=True, exist_ok=True)
    idx = user_home / ".codex" / "session_index.jsonl"
    with open(str(idx), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": sid, "thread_name": title}, ensure_ascii=False) + "\n")
    fp = sess_dir / ("rollout-2026-11-18T01-00-00-%s.jsonl" % sid)
    lines = [{"type": "session_meta", "payload": {"id": sid, "cwd": cwd}}]
    for role, text in turns:
        ctype = "input_text" if role == "user" else "output_text"
        lines.append(
            {
                "timestamp": "2026-11-18T01:05:00Z",
                "type": "response_item",
                "payload": {"type": "message", "role": role, "content": [{"type": ctype, "text": text}]},
            }
        )
    fp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    return fp


def _write_anti_session(user_home: Path, cwd: str, sid: str, title: str, turns):
    logs_dir = user_home / ".gemini" / "antigravity" / "brain" / sid / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    fp = logs_dir / "transcript_full.jsonl"
    steps = [{"step_index": 0, "type": "META", "title": title, "workspacePath": cwd}]
    for i, (role, text) in enumerate(turns, 1):
        if role == "user":
            steps.append(
                {
                    "step_index": i,
                    "type": "USER_INPUT",
                    "created_at": "2026-11-18T01:10:00Z",
                    "content": "<USER_REQUEST>\n%s\n</USER_REQUEST>" % text,
                }
            )
        else:
            steps.append(
                {
                    "step_index": i,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-18T01:10:05Z",
                    "thinking": "internal chain of thought that should stay hidden",
                    "content": text,
                }
            )
    fp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in steps) + "\n", encoding="utf-8")
    return fp


class RelayEngineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cloud_dir = self.root / "cloud_shared"
        self.cloud_dir.mkdir()

        self.home_a = self.root / "home_a"
        self.uhome_a = self.root / "uhome_a"
        self.led_a = self.root / "ledger_a.json"

        self.home_b = self.root / "home_b"
        self.uhome_b = self.root / "uhome_b"
        self.led_b = self.root / "ledger_b.json"

        for d in (self.home_a, self.uhome_a, self.home_b, self.uhome_b):
            d.mkdir(parents=True, exist_ok=True)

        # Configure local device aliases
        (self.home_a / "config.json").write_text(
            json.dumps({"device": "a", "ledger": str(self.led_a)}), encoding="utf-8"
        )
        (self.home_b / "config.json").write_text(
            json.dumps({"device": "b", "ledger": str(self.led_b)}), encoding="utf-8"
        )
        L.update(self.led_a, lambda _l: None)
        L.update(self.led_b, lambda _l: None)

    def env_a(self):
        return mock.patch.dict(
            os.environ,
            {
                "RELAY_DESK_HOME": str(self.home_a),
                "RELAY_DESK_LEDGER": str(self.led_a),
                "RELAY_DESK_DEVICE": "a",
                "RELAY_DESK_USER_HOME": str(self.uhome_a),
            },
        )

    def env_b(self):
        return mock.patch.dict(
            os.environ,
            {
                "RELAY_DESK_HOME": str(self.home_b),
                "RELAY_DESK_LEDGER": str(self.led_b),
                "RELAY_DESK_DEVICE": "b",
                "RELAY_DESK_USER_HOME": str(self.uhome_b),
            },
        )

    def pair_both(self):
        with self.env_a():
            st_a1 = relay_engine.pair_device(root=str(self.cloud_dir), provider_type="local", accept=False)
            self.assertFalse(st_a1["paired"])
            self.assertIsNone(st_a1["peer"])
        with self.env_b():
            st_b1 = relay_engine.pair_device(root=str(self.cloud_dir), provider_type="local", accept=False)
            self.assertFalse(st_b1["paired"])
            self.assertIsNotNone(st_b1["peer"])
            self.assertTrue(st_b1["pairFingerprint"])
        with self.env_a():
            st_a_preview = relay_engine.cloud_status()
            self.assertEqual(st_a_preview["pairFingerprint"], st_b1["pairFingerprint"])
            self.assertFalse(st_a_preview["paired"])
            st_a2 = relay_engine.pair_device(accept=True)
            self.assertTrue(st_a2["paired"])
        with self.env_b():
            st_b2 = relay_engine.pair_device(accept=True)
            self.assertTrue(st_b2["paired"])
        self.assertEqual(st_a2["pairFingerprint"], st_b2["pairFingerprint"])
        return st_a2, st_b2

    def create_git_project_on_a(self, proj_id="demo", name="双机测试项目"):
        repo_a = self.root / "repo_a"
        repo_a.mkdir()
        _git(repo_a, "init", "-b", "main")
        (repo_a / "README.md").write_text("# 初始说明\n", encoding="utf-8")
        (repo_a / "keep.txt").write_text("line1\n", encoding="utf-8")
        (repo_a / "to_delete.txt").write_text("delete me\n", encoding="utf-8")
        _git(repo_a, "add", ".")
        _git(repo_a, "commit", "-m", "initial commit")

        def _reg(l):
            p = L.norm_project(
                {
                    "id": proj_id,
                    "name": name,
                    "device": "a",
                    "tool": "all",
                    "lastTask": "完成核心接口",
                    "nextAction": "编写端到端测试",
                    "note": "注意检查边界条件",
                    "verify": {"state": "pass", "cmd": "pytest", "note": "全绿"},
                    "paths": {"a": str(repo_a), "b": ""},
                },
                l["config"],
            )
            l["projects"].append(p)

        L.update(self.led_a, _reg)
        return repo_a


class TestPairingAndStatus(RelayEngineTestBase):
    def test_status_unconfigured_and_reject_premature_accept(self):
        with self.env_a():
            st0 = relay_engine.cloud_status()
            self.assertFalse(st0["configured"])
            self.assertFalse(st0["paired"])
            self.assertTrue(st0["hasCrypto"])
            with self.assertRaises(relay_engine.RelayError):
                relay_engine.pair_device(root=str(self.cloud_dir), provider_type="local", accept=True)

    def test_pairing_symmetry_and_key_rotation_unpins(self):
        st_a, st_b = self.pair_both()
        self.assertEqual(st_a["peer"]["deviceId"], st_b["deviceId"])
        self.assertEqual(st_b["peer"]["deviceId"], st_a["deviceId"])

        # Re-running pair_device(accept=False) when keys match keeps paired=True
        with self.env_a():
            st_again = relay_engine.pair_device(accept=False)
            self.assertTrue(st_again["paired"])

        # Rotate B's identity and republish -> A must drop paired=False until accept=True
        os.unlink(str(self.home_b / "identity.json"))
        with self.env_b():
            relay_engine.pair_device(accept=False)
        with self.env_a():
            st_rotated = relay_engine.pair_device(accept=False)
            self.assertFalse(st_rotated["paired"])
            self.assertNotEqual(st_rotated["pairFingerprint"], st_a["pairFingerprint"])
            st_reaccepted = relay_engine.pair_device(accept=True)
            self.assertTrue(st_reaccepted["paired"])


class TestFullRoundTripAndFaultRecovery(RelayEngineTestBase):
    def test_receive_protects_ignored_files_and_conflicting_git_refs(self):
        self.pair_both()
        repo_a = self.create_git_project_on_a()
        (repo_a / ".gitignore").write_text("local-notes.txt\n", encoding="utf-8")
        _git(repo_a, "add", ".gitignore")
        _git(repo_a, "commit", "-m", "add ignore rule")
        _git(repo_a, "branch", "side")
        _git(repo_a, "tag", "snapshot")
        with self.env_a():
            rid = relay_engine.send_project("demo")["relayId"]
        protected = self.root / "only-local-credentials" / "README.md"
        protected.mkdir(parents=True)
        (protected / ".env").write_text("local settings\n", encoding="utf-8")
        with self.env_b():
            with self.assertRaisesRegex(packager.TargetDirtyError, "凭据"):
                relay_engine.receive_relay(rid, target_path=str(protected.parent))
        self.assertTrue((protected / ".env").exists())
        repo_b = self.root / "local-checkout"
        _git(self.root, "clone", "--quiet", str(repo_a), str(repo_b))
        local_notes = repo_b / "local-notes.txt"
        local_notes.write_text("only on B\n", encoding="utf-8")
        self.assertEqual(_git(repo_b, "status", "--porcelain"), "")
        with self.env_b():
            with self.assertRaisesRegex(packager.TargetDirtyError, "local-notes.txt"):
                relay_engine.receive_relay(rid, target_path=str(repo_b))
        self.assertEqual(local_notes.read_text(encoding="utf-8"), "only on B\n")
        local_notes.unlink()

        _git(repo_b, "checkout", "-b", "side")
        _git(repo_b, "commit", "--allow-empty", "-m", "only on B")
        local_commit = _git(repo_b, "rev-parse", "HEAD").strip()
        _git(repo_b, "tag", "--force", "snapshot")
        _git(repo_b, "checkout", "main")
        with self.env_b():
            with self.assertRaisesRegex(packager.TargetDirtyError, "side"):
                relay_engine.receive_relay(rid, target_path=str(repo_b))
            self.assertEqual(_git(repo_b, "rev-parse", "side").strip(), local_commit)
            _git(repo_b, "branch", "-D", "side")
            with self.assertRaisesRegex(packager.TargetDirtyError, "snapshot"):
                relay_engine.receive_relay(rid, target_path=str(repo_b))
            self.assertEqual(_git(repo_b, "rev-parse", "snapshot").strip(), local_commit)
            other = self.root / "incoming"
            self.assertTrue(relay_engine.receive_relay(rid, into_path=str(other))["ok"])
            self.assertEqual(_git(repo_b, "rev-parse", "snapshot").strip(), local_commit)

    def test_receive_fast_forwards_branch_and_keeps_local_only_refs(self):
        self.pair_both()
        repo_a = self.create_git_project_on_a()
        repo_b = self.root / "local-checkout"
        _git(self.root, "clone", "--quiet", str(repo_a), str(repo_b))
        initial = _git(repo_b, "rev-parse", "HEAD").strip()
        _git(repo_b, "branch", "local-only")
        _git(repo_b, "tag", "local-tag")
        _git(repo_b, "config", "fetch.prune", "true")
        _git(repo_b, "config", "fetch.pruneTags", "true")
        _git(repo_a, "commit", "--allow-empty", "-m", "fast-forward")
        _git(repo_a, "tag", "incoming-tag")
        with self.env_a():
            rid = relay_engine.send_project("demo")["relayId"]
        with self.env_b():
            self.assertTrue(relay_engine.receive_relay(rid, target_path=str(repo_b))["ok"])
        self.assertEqual(_git(repo_b, "rev-parse", "HEAD"), _git(repo_a, "rev-parse", "HEAD"))
        self.assertEqual(_git(repo_b, "rev-parse", "local-only").strip(), initial)
        self.assertEqual(_git(repo_b, "rev-parse", "local-tag").strip(), initial)

    def test_retry_after_ready_is_visible_recovers_the_original_package(self):
        self.pair_both()
        repo_a = self.create_git_project_on_a()
        real_stat = storage.LocalDirProvider.stat

        def lose_ready_response(provider, key):
            if key.endswith("/ready.json"):
                raise storage.StorageError("lost ready verification response")
            return real_stat(provider, key)

        with self.env_a(), mock.patch.object(storage.LocalDirProvider, "stat", lose_ready_response):
            with self.assertRaisesRegex(storage.StorageError, "lost ready"):
                relay_engine.send_project("demo")

        repo_b = self.root / "received"
        with self.env_b():
            [published] = relay_engine.list_inbox()["incoming"]
            rid = published["relayId"]
            relay_engine.receive_relay(rid, target_path=str(repo_b))
        ready_dir = self.cloud_dir / "relays" / rid
        original_ciphertext = (ready_dir / "package.enc").read_bytes()
        original_ready = (ready_dir / "ready.json").read_bytes()
        (repo_a / "keep.txt").write_text("new local work\n", encoding="utf-8")

        with self.env_a():
            retried = relay_engine.send_project("demo")
            self.assertEqual(retried["relayId"], rid)
            self.assertEqual(retried["packageSha256"], published["packageSha256"])
            self.assertEqual((ready_dir / "package.enc").read_bytes(), original_ciphertext)
            self.assertEqual((ready_dir / "ready.json").read_bytes(), original_ready)
            self.assertEqual((repo_b / "keep.txt").read_text(encoding="utf-8"), "line1\n")
            self.assertEqual([r["relayId"] for r in relay_engine.list_inbox()["syncedReceipts"]], [rid])

    def test_receipt_must_match_the_published_package_digest(self):
        self.pair_both()
        self.create_git_project_on_a()
        with self.env_a():
            rid = relay_engine.send_project("demo")["relayId"]
        with self.env_b():
            relay_engine.receive_relay(rid, target_path=str(self.root / "received"))
        receipt_path = self.cloud_dir / "relays" / rid / "receipt.json"
        original_receipt = receipt_path.read_text(encoding="utf-8")
        identity = json.loads((self.home_b / "identity.json").read_text(encoding="utf-8"))
        receipt = crypto.verify_json(json.loads(original_receipt), identity["verifyPublicKey"])
        receipt["packageSha256"] = "0" * 64
        replacement = crypto.sign_json(receipt, identity["signPrivateKey"], signer_device_id=identity["deviceId"])
        receipt_path.write_text(json.dumps(replacement), encoding="utf-8")
        with self.env_a():
            with self.subTest(state="current"):
                self.assertEqual(relay_engine.list_inbox()["syncedReceipts"], [])
                self.assertEqual(L.load(self.led_a)["projects"][0]["device"], "a")
            with self.subTest(state="legacy"):
                relay_engine._update_relay_state(
                    self.home_a, lambda state: state["sentRelays"][rid].pop("packageSha256"),
                )
                self.assertEqual(relay_engine.list_inbox()["syncedReceipts"], [])
            receipt_path.write_text(original_receipt, encoding="utf-8")
            self.assertEqual([r["relayId"] for r in relay_engine.list_inbox()["syncedReceipts"]], [rid])

    def test_failed_new_workspace_apply_does_not_leave_partial_target(self):
        self.pair_both()
        self.create_git_project_on_a()
        with self.env_a():
            rid = relay_engine.send_project("demo")["relayId"]
        target = self.root / "destination"

        def apply_partway(_staging, workspace):
            Path(workspace).mkdir()
            (Path(workspace) / "partial.txt").write_text("partial", encoding="utf-8")
            raise OSError("workspace unavailable")

        with self.env_b():
            with mock.patch.object(packager, "apply_verified_payload", side_effect=apply_partway):
                with self.assertRaisesRegex(OSError, "workspace unavailable"):
                    relay_engine.receive_relay(rid, target_path=str(target))
            self.assertFalse(target.exists())
            self.assertEqual(L.load(self.led_b)["projects"], [])
            self.assertTrue(relay_engine.receive_relay(rid, target_path=str(target))["ok"])
            self.assertFalse((target / "partial.txt").exists())

    def test_retry_after_new_workspace_rename_fails_uses_saved_target(self):
        self.pair_both()
        self.create_git_project_on_a()
        with self.env_a():
            rid = relay_engine.send_project("demo")["relayId"]
        target = self.root / "destination"
        with self.env_b():
            with mock.patch.object(relay_engine.os, "rename", side_effect=OSError("rename interrupted")):
                with self.assertRaisesRegex(OSError, "rename interrupted"):
                    relay_engine.receive_relay(rid, target_path=str(target))
            self.assertFalse(target.exists())
            self.assertTrue(relay_engine.receive_relay(rid)["ok"])
            self.assertTrue((target / "README.md").is_file())

    def test_archive_write_failure_leaves_destination_untouched_and_retryable(self):
        self.pair_both()
        folder = self.root / "source-folder"
        folder.mkdir()
        (folder / "notes.txt").write_text("handoff contents\n", encoding="utf-8")
        L.update(self.led_a, lambda ledger: ledger["projects"].append(L.norm_project(
            {"name": "Folder", "id": "folder", "device": "a", "paths": {"a": str(folder)}},
            ledger["config"],
        )))
        with self.env_a():
            rid = relay_engine.send_project("folder")["relayId"]
        target = self.root / "destination"
        with self.env_b():
            with mock.patch.object(conversations, "merge_and_save_archive", side_effect=OSError("archive unavailable")):
                with self.assertRaisesRegex(OSError, "archive unavailable"):
                    relay_engine.receive_relay(rid, target_path=str(target))
            self.assertFalse(target.exists())
            self.assertEqual(L.load(self.led_b)["projects"], [])
            self.assertFalse((self.cloud_dir / "relays" / rid / "receipt.json").exists())
            self.assertTrue(relay_engine.receive_relay(rid, target_path=str(target))["ok"])
            self.assertEqual((target / "notes.txt").read_text(encoding="utf-8"), "handoff contents\n")

    def test_receive_resumes_after_ledger_failure_without_reapplying_workspace(self):
        self.pair_both()
        repo_a = self.create_git_project_on_a()
        (repo_a / "new.txt").write_text("untracked from A\n", encoding="utf-8")
        with self.env_a():
            rid = relay_engine.send_project("demo")["relayId"]
        target = self.root / "destination"
        with self.env_b():
            with mock.patch.object(L, "update", side_effect=L.LedgerError("ledger unavailable")):
                with self.assertRaisesRegex(L.LedgerError, "ledger unavailable"):
                    relay_engine.receive_relay(rid, target_path=str(target))
            self.assertTrue((target / "new.txt").is_file())
            self.assertEqual(L.load(self.led_b)["projects"], [])
            self.assertFalse((self.cloud_dir / "relays" / rid / "receipt.json").exists())
            (target / "new.txt").write_text("edited after interruption\n", encoding="utf-8")

            real_update = L.update

            def lose_ledger_result(path, update):
                real_update(path, update)
                raise L.LedgerError("ledger result lost")

            with mock.patch.object(packager, "apply_verified_payload", side_effect=AssertionError("reapplied")):
                with mock.patch.object(L, "update", side_effect=lose_ledger_result):
                    with self.assertRaisesRegex(L.LedgerError, "ledger result lost"):
                        relay_engine.receive_relay(rid)
                self.assertTrue(relay_engine.receive_relay(rid)["ok"])
            self.assertEqual((target / "new.txt").read_text(encoding="utf-8"), "edited after interruption\n")
            [project] = L.load(self.led_b)["projects"]
            self.assertEqual([entry["relayId"] for entry in project["history"]], [rid])

    def test_git_round_trip_a_to_b_to_a_with_staged_unstaged_untracked_and_3_tools(self):
        self.pair_both()
        repo_a = self.create_git_project_on_a()

        # Staged modification + staged new file + staged empty file
        (repo_a / "README.md").write_text("# 暂存版本\n", encoding="utf-8")
        (repo_a / "staged_new.py").write_text("x = 1\n", encoding="utf-8")
        (repo_a / "staged_empty.txt").write_text("", encoding="utf-8")
        _git(repo_a, "add", "README.md", "staged_new.py", "staged_empty.txt")

        # Unstaged modification on top of staged file + unstaged deletion
        (repo_a / "README.md").write_text("# 工作区再改一版\n", encoding="utf-8")
        os.unlink(str(repo_a / "to_delete.txt"))

        # Untracked file + local .env (must be excluded)
        (repo_a / "notes_draft.md").write_text("草稿内容\n", encoding="utf-8")
        (repo_a / ".env").write_text("API_SECRET=never_upload\n", encoding="utf-8")

        expected_head_a = _git(repo_a, "rev-parse", "HEAD").strip()
        expected_cached_a = _git_bytes(repo_a, "diff", "--cached", "--binary")
        expected_unstaged_a = _git_bytes(repo_a, "diff", "--binary")

        # Create Claude, Codex, Antigravity conversations on A
        uuid_cx = "11111111-2222-3333-4444-555555555555"
        _write_claude_session(self.uhome_a, str(repo_a), "claude_s1", "Claude 架构讨论", [("user", "请设计接口"), ("assistant", "已完成接口草案")])
        _write_codex_session(self.uhome_a, str(repo_a), uuid_cx, "Codex 调试线程", [("user", "跑一下单元"), ("assistant", "发现边界问题")])
        _write_anti_session(self.uhome_a, str(repo_a), "anti_s1", "Antigravity 重构", [("user", "梳理模块"), ("assistant", "已拆分为独立模块")])

        with self.env_a():
            send_1 = relay_engine.send_project("demo", to_alias="b")
        self.assertTrue(send_1["ok"])
        self.assertEqual(send_1["message"], "已交棒，可以关机")
        self.assertEqual(send_1["parentRelayId"], "")
        self.assertEqual(send_1["sessionCount"], 3)
        self.assertIn(".env", send_1["excludedFiles"])
        rid_1 = send_1["relayId"]

        # B checks inbox and receives without --path first -> NeedTargetPathError
        repo_b = self.root / "repo_b"
        with self.env_b():
            inbox_b = relay_engine.list_inbox()
            self.assertEqual(len(inbox_b["incoming"]), 1)
            self.assertEqual(inbox_b["incoming"][0]["relayId"], rid_1)
            self.assertEqual(inbox_b["incoming"][0]["projectName"], "双机测试项目")
            with self.assertRaises(relay_engine.NeedTargetPathError) as ctx_np:
                relay_engine.receive_relay(rid_1)
            self.assertEqual(ctx_np.exception.project_id, "demo")
            self.assertEqual(ctx_np.exception.project_name, "双机测试项目")

            recv_1 = relay_engine.receive_relay(rid_1, target_path=str(repo_b))
            self.assertTrue(recv_1["ok"])
            self.assertEqual(_git(repo_b, "rev-parse", "HEAD").strip(), expected_head_a)
            self.assertEqual(_git_bytes(repo_b, "diff", "--cached", "--binary"), expected_cached_a)
            self.assertEqual(_git_bytes(repo_b, "diff", "--binary"), expected_unstaged_a)
            self.assertEqual((repo_b / "notes_draft.md").read_text(encoding="utf-8"), "草稿内容\n")
            self.assertTrue((repo_b / "staged_empty.txt").is_file())
            self.assertFalse((repo_b / ".env").exists())

            # Query all 3 tools' transcripts on B
            tr_all = relay_engine.query_transcripts("demo")
            self.assertEqual(len(tr_all["sessions"]), 3)
            tr_grep = relay_engine.query_transcripts("demo", grep="边界问题")
            self.assertEqual(len(tr_grep["matches"]), 1)
            self.assertEqual(tr_grep["matches"][0]["tool"], "codex")
            tr_md = relay_engine.query_transcripts("demo", session_id="anti_s1")
            self.assertIn("已拆分为独立模块", tr_md["markdown"])
            self.assertNotIn("internal chain of thought", tr_md["markdown"])

            # Now B commits work, adds new staged/unstaged changes + local untracked .env + new conversation, and relays back to A!
            _git(repo_b, "add", "-A")
            _git(repo_b, "commit", "-m", "B committed progress")
            (repo_b / ".env").write_text("B_LOCAL_ENV=keep_on_b\n", encoding="utf-8")
            (repo_b / "b_staged.txt").write_text("from B staged\n", encoding="utf-8")
            _git(repo_b, "add", "b_staged.txt")
            (repo_b / "keep.txt").write_text("line1\nfrom B unstaged\n", encoding="utf-8")

            _write_claude_session(self.uhome_b, str(repo_b), "claude_b2", "B 端开发记录", [("user", "继续推进"), ("assistant", "已完成 B 端提交")])
            send_2 = relay_engine.send_project("demo", to_alias="a")
            self.assertEqual(send_2["message"], "已交棒，可以关机")
            self.assertEqual(send_2["parentRelayId"], rid_1)
            rid_2 = send_2["relayId"]

        expected_head_b = _git(repo_b, "rev-parse", "HEAD").strip()
        expected_cached_b = _git_bytes(repo_b, "diff", "--cached", "--binary")
        expected_unstaged_b = _git_bytes(repo_b, "diff", "--binary")

        # Back on A: inbox syncs rid_1 receipt and sees rid_2; receive updates repo_a cleanly while preserving A's .env!
        with self.env_a():
            inbox_a = relay_engine.list_inbox()
            self.assertEqual([r["relayId"] for r in inbox_a["syncedReceipts"]], [rid_1])
            self.assertEqual([x["relayId"] for x in inbox_a["incoming"]], [rid_2])

            recv_2 = relay_engine.receive_relay(rid_2)
            self.assertTrue(recv_2["ok"])
            self.assertEqual(recv_2["parentRelayId"], rid_1)
            self.assertEqual(_git(repo_a, "rev-parse", "HEAD").strip(), expected_head_b)
            self.assertEqual(_git_bytes(repo_a, "diff", "--cached", "--binary"), expected_cached_b)
            self.assertEqual(_git_bytes(repo_a, "diff", "--binary"), expected_unstaged_b)
            # Local .env on A was preserved untouched!
            self.assertEqual((repo_a / ".env").read_text(encoding="utf-8"), "API_SECRET=never_upload\n")

            # Zero duplicate conversations, and B's new session is queryable on A!
            tr_a = relay_engine.query_transcripts("demo")
            self.assertEqual(len(tr_a["sessions"]), 4)
            sids = sorted(s["sessionId"] for s in tr_a["sessions"])
            self.assertEqual(sids, sorted(["claude_s1", uuid_cx, "anti_s1", "claude_b2"]))

    def test_upload_interruption_hides_partial_and_reuses_same_relay_id_on_retry(self):
        self.pair_both()
        self.create_git_project_on_a()

        real_make = storage.make_provider

        class FailOnReadyProvider(storage.LocalDirProvider):
            def upload(self, local_path: str, remote_key: str):
                if remote_key.endswith("/ready.json"):
                    raise storage.StorageError("模拟网络中断：ready.json 上传失败")
                return super().upload(local_path, remote_key)

        with self.env_a():
            with mock.patch.object(storage, "make_provider", side_effect=lambda cfg: FailOnReadyProvider(cfg["root"])):
                with self.assertRaises(storage.StorageError):
                    relay_engine.send_project("demo", to_alias="b")

            pending_state = json.loads((self.home_a / "relay_state.json").read_text(encoding="utf-8"))
            pending_rid = pending_state["pendingSends"]["demo"]["relayId"]
            self.assertTrue((self.cloud_dir / "relays" / pending_rid / "package.enc").is_file())
            self.assertFalse((self.cloud_dir / "relays" / pending_rid / "ready.json").exists())

        # B must see 0 claimable packages while ready.json is absent!
        with self.env_b():
            inbox_b = relay_engine.list_inbox()
            self.assertEqual(inbox_b["incoming"], [])

        # A retries send -> MUST reuse exact same relayId!
        with self.env_a():
            res_retry = relay_engine.send_project("demo", to_alias="b")
            self.assertEqual(res_retry["relayId"], pending_rid)
            state_after = json.loads((self.home_a / "relay_state.json").read_text(encoding="utf-8"))
            self.assertNotIn("demo", state_after["pendingSends"])
            self.assertIn(pending_rid, state_after["sentRelays"])
        _ = real_make

    def test_conversation_gap_and_secret_gate_block_before_upload(self):
        self.pair_both()
        repo_a = self.create_git_project_on_a()

        # 1. Missing registered local session -> ConversationGapError
        def _add_missing_sess(l):
            l["projects"][0]["sessions"].append(
                {
                    "id": "s_miss",
                    "tool": "claude",
                    "device": "a",
                    "kind": "local",
                    "carry": "summary",
                    "sessionId": "missing_sid_999",
                    "title": "丢失的会话",
                }
            )

        L.update(self.led_a, _add_missing_sess)
        with self.env_a():
            with self.assertRaises(conversations.ConversationGapError) as ctx_gap:
                relay_engine.send_project("demo", to_alias="b")
            self.assertEqual(len(ctx_gap.exception.gaps), 1)
            self.assertIn("缺口", str(ctx_gap.exception))
        self.assertFalse((self.cloud_dir / "relays").exists())

        # Fix missing session
        _write_claude_session(self.uhome_a, str(repo_a), "missing_sid_999", "补齐的会话", [("user", "hi"), ("assistant", "ok")])

        # 2. Stage a private key file -> SecretLeakError before upload!
        (repo_a / " leaked.pem").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n", encoding="utf-8")
        _git(repo_a, "add", " leaked.pem")
        with self.env_a():
            with self.assertRaises(packager.SecretLeakError):
                relay_engine.send_project("demo", to_alias="b")
        self.assertFalse((self.cloud_dir / "relays").exists())

    def test_tamper_detection_leaves_workspace_and_ledger_untouched(self):
        self.pair_both()
        self.create_git_project_on_a()
        with self.env_a():
            sent = relay_engine.send_project("demo", to_alias="b")
        rid = sent["relayId"]

        pkg_file = self.cloud_dir / "relays" / rid / "package.enc"
        orig_bytes = pkg_file.read_bytes()

        # Flip one bit in package.enc
        tampered = bytearray(orig_bytes)
        tampered[-10] ^= 0xFF
        pkg_file.write_bytes(bytes(tampered))

        repo_b = self.root / "repo_b_tamper"
        with self.env_b():
            with self.assertRaises(crypto.TamperError):
                relay_engine.receive_relay(rid, target_path=str(repo_b))
            self.assertFalse(repo_b.exists())
            self.assertEqual(L.load(self.led_b)["projects"], [])

        # Truncate package.enc
        pkg_file.write_bytes(orig_bytes[:-5])
        with self.env_b():
            inbox_trunc = relay_engine.list_inbox()
            self.assertTrue(inbox_trunc["incoming"][0]["tampered"])
            with self.assertRaises(crypto.TamperError):
                relay_engine.receive_relay(rid, target_path=str(repo_b))
            self.assertFalse(repo_b.exists())

    def test_dirty_target_preserved_and_into_path_succeeds(self):
        self.pair_both()
        repo_a = self.create_git_project_on_a()
        with self.env_a():
            s1 = relay_engine.send_project("demo", to_alias="b")
        repo_b = self.root / "repo_b"
        with self.env_b():
            relay_engine.receive_relay(s1["relayId"], target_path=str(repo_b))

        # A makes another commit and sends s2 (syncing s1 receipt first so A needs ownership or B sends? Wait:
        # after B received s1, A's sync_receipts flips device='b'. Let's set device='a' on A to simulate A sending another update while B also edited locally!)
        L.update(self.led_a, lambda l: l["projects"][0].update(device="a"))
        (repo_a / "keep.txt").write_text("from A v2\n", encoding="utf-8")
        _git(repo_a, "add", "keep.txt")
        _git(repo_a, "commit", "-m", "A v2")
        with self.env_a():
            # Already acked s1 if synced earlier; force device='a' after sync by pre-syncing:
            relay_engine.list_inbox()
            L.update(self.led_a, lambda l: l["projects"][0].update(device="a"))
            s2 = relay_engine.send_project("demo", to_alias="b")

        # B modifies repo_b locally BEFORE receiving s2!
        (repo_b / "keep.txt").write_text("B unsaved local work!\n", encoding="utf-8")
        repo_b_alt = self.root / "repo_b_into"
        with self.env_b():
            with self.assertRaises(packager.TargetDirtyError) as ctx_dirty:
                relay_engine.receive_relay(s2["relayId"])
            self.assertIn("--into", str(ctx_dirty.exception))
            self.assertEqual((repo_b / "keep.txt").read_text(encoding="utf-8"), "B unsaved local work!\n")

            # Receive into a new directory preserves original repo_b intact!
            res_into = relay_engine.receive_relay(s2["relayId"], into_path=str(repo_b_alt))
            self.assertTrue(res_into["ok"])
            self.assertEqual((repo_b / "keep.txt").read_text(encoding="utf-8"), "B unsaved local work!\n")
            self.assertEqual((repo_b_alt / "keep.txt").read_text(encoding="utf-8"), "from A v2\n")

    def test_receipt_upload_failure_recovery_and_duplicate_receive_block(self):
        self.pair_both()
        self.create_git_project_on_a()
        with self.env_a():
            s1 = relay_engine.send_project("demo", to_alias="b")
        rid = s1["relayId"]

        class FailOnReceiptProvider(storage.LocalDirProvider):
            def upload(self, local_path: str, remote_key: str):
                if remote_key.endswith("/receipt.json"):
                    raise storage.StorageError("模拟网络超时：回执上传失败")
                return super().upload(local_path, remote_key)

        repo_b = self.root / "repo_b_rec"
        with self.env_b():
            with mock.patch.object(storage, "make_provider", side_effect=lambda cfg: FailOnReceiptProvider(cfg["root"])):
                with self.assertRaises(relay_engine.ReceiptUploadError) as ctx_ru:
                    relay_engine.receive_relay(rid, target_path=str(repo_b))
                self.assertEqual(ctx_ru.exception.relay_id, rid)

            # Files ARE applied on B, and inbox shows pendingAck=True
            self.assertTrue((repo_b / "README.md").is_file())
            ib = relay_engine.list_inbox()
            self.assertEqual(len(ib["incoming"]), 1)
            self.assertTrue(ib["incoming"][0]["pendingAck"])

            # User starts editing repo_b BEFORE retrying receipt upload!
            (repo_b / "README.md").write_text("B local edit after apply!\n", encoding="utf-8")

            # Retry receive_relay(rid) -> skips dirty check & apply, uploads saved signedReceipt!
            recovered = relay_engine.receive_relay(rid)
            self.assertTrue(recovered["ok"])
            self.assertEqual((repo_b / "README.md").read_text(encoding="utf-8"), "B local edit after apply!\n")

            # Duplicate receive now raises DuplicateReceiveError!
            with self.assertRaises(relay_engine.DuplicateReceiveError):
                relay_engine.receive_relay(rid)

        # A syncs receipt cleanly!
        with self.env_a():
            ib_a = relay_engine.list_inbox()
            self.assertEqual([r["relayId"] for r in ib_a["syncedReceipts"]], [rid])
            self.assertEqual(L.load(self.led_a)["projects"][0]["device"], "b")

    def test_non_git_folder_round_trip_and_chinese_paths(self):
        self.pair_both()
        dir_a = self.root / "测试工作区" / "非Git项目"
        (dir_a / "核心模块").mkdir(parents=True)
        (dir_a / "核心模块" / "说明文档.md").write_text("# 中文文件夹快照\n", encoding="utf-8")
        (dir_a / ".env").write_text("FOLDER_SECRET=1\n", encoding="utf-8")

        def _reg(l):
            l["projects"].append(
                L.norm_project(
                    {
                        "id": "folder-cn",
                        "name": "中文目录项目",
                        "device": "a",
                        "tool": "claude",
                        "paths": {"a": str(dir_a), "b": ""},
                    },
                    l["config"],
                )
            )

        L.update(self.led_a, _reg)

        with self.env_a():
            s1 = relay_engine.send_project("folder-cn", to_alias="b")
        dir_b = self.root / "接收端工作区" / "非Git项目"
        with self.env_b():
            r1 = relay_engine.receive_relay(s1["relayId"], target_path=str(dir_b))
            self.assertEqual(r1["mode"], "folder")
            self.assertEqual((dir_b / "核心模块" / "说明文档.md").read_text(encoding="utf-8"), "# 中文文件夹快照\n")
            self.assertFalse((dir_b / ".env").exists())

            (dir_b / "核心模块" / "新增.txt").write_text("B 新增文件\n", encoding="utf-8")
            s2 = relay_engine.send_project("folder-cn", to_alias="a")

        with self.env_a():
            r2 = relay_engine.receive_relay(s2["relayId"])
            self.assertTrue(r2["ok"])
            self.assertEqual((dir_a / "核心模块" / "新增.txt").read_text(encoding="utf-8"), "B 新增文件\n")
            self.assertEqual((dir_a / ".env").read_text(encoding="utf-8"), "FOLDER_SECRET=1\n")


if __name__ == "__main__":
    unittest.main()
