"""End-to-end acceptance test suite for RelayDesk encrypted async cloud relay.

Covers all four acceptance pillars:
1. True time-shifted async relay (A -> B -> A) with physical directory isolation
   during peer offline windows, exact Git commit/branch/staged/unstaged/untracked
   restoration, `.env` exclusion, and deduplicated multi-agent conversation archives
   across Claude Code, Codex, and Antigravity.
2. True cloud swappability across real `rclone` + WebDAV and real `rclone` + S3
   (and pure-Python stdlib fallback verification), using distinct remote names
   (`alice_cloud` vs `bob_storage`).
3. Cross-OS matrix (all 6 desktop OS pairs), Chinese & NFD/NFC paths, case-folding
   collision rejection, session gap detection & retry, secret/history gate,
   interrupted upload with same-relayId retry, duplicate receive protection,
   ciphertext/metadata/truncation tamper rejection, dirty receiver protection with
   `--into` recovery, post-apply receipt upload failure recovery, and Web Workbench
   HTTP API E2E (`/api/cloud/*`).
4. `/var` <-> `/private/var` symlinked `cwd` regression checks.
"""
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unicodedata
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydesk import cli  # noqa: E402
from relaydesk import ledger as L  # noqa: E402
from relaydesk import scan  # noqa: E402
from relaydesk import server  # noqa: E402
from tests.cloud_servers import CloudServerEnv, find_rclone, s3_server, webdav_server  # noqa: E402

GIT_CMD = ["git", "-c", "user.name=RelayTester", "-c", "user.email=tester@relay.invalid"]
PAIR_FP_RE = re.compile(r"\b[0-9A-F]{4}(?:-[0-9A-F]{4}){5}\b")
RELAY_ID_RE = re.compile(r"\br_\d{8}_[a-z0-9]{6,16}\b")


def _ensure_tool_path() -> str:
    cur = os.environ.get("PATH") or ""
    parts = cur.split(os.pathsep) if cur else []
    for extra in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        if extra not in parts and os.path.isdir(extra):
            parts.insert(0, extra)
    return os.pathsep.join(parts)


def run_git(repo: Path, *args: str, binary: bool = False, check: bool = True) -> Any:
    cmd = GIT_CMD + ["-c", "core.quotepath=false", "-C", str(repo)] + list(args)
    if binary:
        return subprocess.run(cmd, capture_output=True, check=check)
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=check)


def init_git_repo(repo: Path, branch: str = "main") -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    run_git(repo, "init", "-q", "-b", branch)
    return repo


def write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_jsonl(path: Path, rows: List[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        for row in rows:
            if isinstance(row, str):
                fh.write(row + ("\n" if not row.endswith("\n") else ""))
            else:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def put_claude_session(uhome: Path, cwd: Path, session_id: str, title: str, turns: List[Tuple[str, str]]) -> Path:
    proj_dir = uhome / ".claude" / "projects" / scan.enc_path(str(cwd))
    rows = [{"type": "custom-title", "customTitle": title}]  # type: List[Any]
    for idx, (role, text) in enumerate(turns):
        rows.append(
            {
                "type": role,
                "timestamp": "2026-11-18T02:%02d:00Z" % (idx % 60),
                "message": {"role": role, "content": [{"type": "text", "text": text}]},
            }
        )
    return write_jsonl(proj_dir / ("%s.jsonl" % session_id), rows)


def put_codex_session(uhome: Path, cwd: Path, uuid_str: str, title: str, turns: List[Tuple[str, str]]) -> Path:
    sess_dir = uhome / ".codex" / "sessions" / "2026" / "11" / "18"
    fp = sess_dir / ("rollout-2026-11-18T02-00-00-%s.jsonl" % uuid_str)
    rows = [
        {"type": "session_meta", "timestamp": "2026-11-18T02:00:00Z", "payload": {"id": uuid_str, "cwd": str(cwd)}}
    ]  # type: List[Any]
    for idx, (role, text) in enumerate(turns):
        ctype = "input_text" if role == "user" else "output_text"
        rows.append(
            {
                "timestamp": "2026-11-18T02:%02d:10Z" % (idx % 60),
                "type": "response_item",
                "payload": {"type": "message", "role": role, "content": [{"type": ctype, "text": text}]},
            }
        )
    write_jsonl(fp, rows)
    idx_path = uhome / ".codex" / "session_index.jsonl"
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(idx_path), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": uuid_str, "thread_name": title, "updated_at": "2026-11-18T02:05:00Z"}, ensure_ascii=False) + "\n")
    return fp


def put_anti_session(
    uhome: Path,
    cwd: Path,
    session_id: str,
    title: str,
    turns: List[Tuple[str, str]],
    include_full: bool = True,
    truncated_compact: bool = False,
    secret_thinking: str = "INTERNAL_CHAIN_OF_THOUGHT_DO_NOT_ARCHIVE",
) -> Tuple[Path, Optional[Path]]:
    logs_dir = uhome / ".gemini" / "antigravity" / "brain" / session_id / ".system_generated" / "logs"
    uri = "file://" + (str(cwd) if str(cwd).startswith("/") else "/" + str(cwd).replace("\\", "/"))
    full_rows = [{"customTitle": title}]  # type: List[Any]
    compact_rows = [{"customTitle": title}]  # type: List[Any]
    step = 0
    for role, text in turns:
        step += 1
        if role == "user":
            obj = {
                "step_index": step,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2026-11-18T02:%02d:20Z" % (step % 60),
                "metadata": {"workspaceUri": uri},
                "content": "<USER_REQUEST>\n%s\n</USER_REQUEST>" % text,
            }
            full_rows.append(obj)
            compact_rows.append(dict(obj))
        else:
            full_obj = {
                "step_index": step,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-18T02:%02d:25Z" % (step % 60),
                "thinking": secret_thinking,
                "content": text,
                "tool_calls": [{"name": "read_file", "args": {"Path": str(cwd / "README.md")}}],
            }
            full_rows.append(full_obj)
            comp_obj = dict(full_obj)
            if truncated_compact:
                comp_obj["content"] = text[:10] + "..."
                comp_obj["truncated_fields"] = ["content"]
            compact_rows.append(comp_obj)

    compact_path = write_jsonl(logs_dir / "transcript.jsonl", compact_rows)
    full_path = write_jsonl(logs_dir / "transcript_full.jsonl", full_rows) if include_full else None
    return compact_path, full_path


class DeviceSim(object):
    """Isolated simulated desktop machine with its own home, ledger, user_home, and workspaces."""

    def __init__(self, base_dir: Path, alias: str):
        self.alias = alias
        self.root_dir = base_dir / ("machine_%s" % alias)
        self.offline_dir = base_dir / ("machine_%s.powered_off" % alias)
        self.home = self.root_dir / "relay_home"
        self.uhome = self.root_dir / "user_home"
        self.workspaces = self.root_dir / "workspaces"
        self.ledger_path = self.home / "ledger.json"
        self.is_online = True
        self.home.mkdir(parents=True, exist_ok=True)
        self.uhome.mkdir(parents=True, exist_ok=True)
        self.workspaces.mkdir(parents=True, exist_ok=True)

    def env_dict(self) -> Dict[str, str]:
        if not self.is_online:
            raise AssertionError("Device %s is currently powered off!" % self.alias)
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("RELAY_DESK_")
            and k not in ("CODEX_HOME", "GEMINI_CONFIG_DIR", "RCLONE_CONFIG")
        }
        env["PATH"] = _ensure_tool_path()
        env["RELAY_DESK_HOME"] = str(self.home)
        env["RELAY_DESK_USER_HOME"] = str(self.uhome)
        env["CODEX_HOME"] = str(self.uhome / ".codex")
        env["GEMINI_CONFIG_DIR"] = str(self.uhome / ".gemini" / "config")
        rclone_bin = find_rclone()
        if rclone_bin:
            env["RELAY_DESK_RCLONE"] = rclone_bin
        return env

    def run_cli(self, *argv: Any, cwd: Optional[Path] = None) -> Tuple[int, str, str]:
        out_buf, err_buf = io.StringIO(), io.StringIO()
        prev_cwd = os.getcwd()
        with mock.patch.dict(os.environ, self.env_dict(), clear=True):
            try:
                if cwd is not None:
                    os.chdir(str(cwd))
                with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                    code = cli.main([str(a) for a in argv])
            finally:
                try:
                    os.chdir(prev_cwd)
                except OSError:
                    pass
        return int(code or 0), out_buf.getvalue(), err_buf.getvalue()

    def ok(self, *argv: Any, cwd: Optional[Path] = None) -> str:
        code, out_s, err_s = self.run_cli(*argv, cwd=cwd)
        if code != 0:
            raise AssertionError(
                "CLI failed on device %s: argv=%r code=%d\nstdout=%s\nstderr=%s"
                % (self.alias, argv, code, out_s, err_s)
            )
        return out_s

    def ok_json(self, *argv: Any, cwd: Optional[Path] = None) -> Any:
        args = list(argv)
        if "--json" not in [str(x) for x in args]:
            args.append("--json")
        raw = self.ok(*args, cwd=cwd)
        return json.loads(raw)

    def init_device(self, os_name: Optional[str] = None, dev_name: Optional[str] = None) -> None:
        self.ok("init", "--device", self.alias, "--ledger", self.ledger_path)
        if os_name or dev_name:
            cmd = ["device", "--which", self.alias]
            if os_name:
                cmd.extend(["--os", os_name])
            if dev_name:
                cmd.extend(["--name", dev_name])
            self.ok(*cmd)

    def power_off(self) -> None:
        """Physically move this machine's entire directory tree offline so peer cannot touch it."""
        if not self.is_online:
            return
        os.rename(str(self.root_dir), str(self.offline_dir))
        self.is_online = False

    def power_on(self) -> None:
        """Restore this machine's directory tree back to its exact original paths."""
        if self.is_online:
            return
        os.rename(str(self.offline_dir), str(self.root_dir))
        self.is_online = True

    def load_ledger(self) -> Dict[str, Any]:
        return L.load(self.ledger_path)


def pair_two_devices(
    dev_a: DeviceSim,
    dev_b: DeviceSim,
    cloud_env: CloudServerEnv,
    provider_type: str = "rclone",
) -> str:
    """Pair Device A and Device B using separate rclone connection names and verify SAS fingerprint."""
    if provider_type == "rclone":
        rem_a = dev_a.ok_json("cloud", "remotes", "--rclone-config", cloud_env.conf_a)
        rem_b = dev_b.ok_json("cloud", "remotes", "--rclone-config", cloud_env.conf_b)
        remotes_list_a = rem_a.get("remotes", rem_a) if isinstance(rem_a, dict) else rem_a
        remotes_list_b = rem_b.get("remotes", rem_b) if isinstance(rem_b, dict) else rem_b
        assert cloud_env.remote_a in remotes_list_a, remotes_list_a
        assert cloud_env.remote_b in remotes_list_b, remotes_list_b

        res_a1 = dev_a.ok_json(
            "pair",
            "--provider",
            "rclone",
            "--remote",
            cloud_env.remote_a,
            "--root",
            cloud_env.shared_root,
            "--rclone-config",
            cloud_env.conf_a,
        )
        assert not res_a1.get("paired"), res_a1

        res_b1 = dev_b.ok_json(
            "pair",
            "--provider",
            "rclone",
            "--remote",
            cloud_env.remote_b,
            "--root",
            cloud_env.shared_root,
            "--rclone-config",
            cloud_env.conf_b,
        )
        assert not res_b1.get("paired"), res_b1
    else:
        local_root = str(cloud_env.storage_root_dir)
        res_a1 = dev_a.ok_json("pair", "--provider", "local", "--root", local_root)
        res_b1 = dev_b.ok_json("pair", "--provider", "local", "--root", local_root)

    # Check human-readable fingerprint output on both A and B before --accept
    txt_a = dev_a.ok("pair")
    txt_b = dev_b.ok("pair")
    m_a = PAIR_FP_RE.search(txt_a)
    m_b = PAIR_FP_RE.search(txt_b)
    assert m_a is not None, "Expected 24-hex pairing fingerprint in A output: %r" % txt_a
    assert m_b is not None, "Expected 24-hex pairing fingerprint in B output: %r" % txt_b
    assert m_a.group(0) == m_b.group(0), (txt_a, txt_b)
    fp = m_a.group(0)
    assert fp == res_b1.get("pairFingerprint"), (fp, res_b1)

    # Confirm with --accept on both machines
    acc_a = dev_a.ok_json("pair", "--accept")
    acc_b = dev_b.ok_json("pair", "--accept")
    assert acc_a.get("paired") is True, acc_a
    assert acc_b.get("paired") is True, acc_b
    assert acc_a.get("pairFingerprint") == fp
    assert acc_b.get("pairFingerprint") == fp
    return fp


class E2EBase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.base = Path(os.path.realpath(self._td.name))


# ===========================================================================
# 0. Cloud Server Fixtures Self-Verification (rclone & stdlib fallbacks)
# ===========================================================================

class TestCloudServersSelfCheck(E2EBase):
    def test_webdav_and_s3_servers_both_backends(self) -> None:
        rclone_bin = find_rclone()
        self.assertIsNotNone(rclone_bin, "rclone binary must be installed for E2E verification")
        for prefer_rclone in (True, False):
            for cm in (webdav_server, s3_server):
                with cm(prefer_rclone=prefer_rclone) as cenv:
                    src = self.base / ("in_%s_%s.bin" % (cenv.protocol, cenv.backend))
                    dst = self.base / ("out_%s_%s.bin" % (cenv.protocol, cenv.backend))
                    payload = ("云端验证-%s-%s-\x00\xff" % (cenv.protocol, cenv.backend)).encode("latin1", errors="ignore") + "中文".encode("utf-8")
                    src.write_bytes(payload)
                    subprocess.run(
                        [
                            rclone_bin,
                            "--config",
                            str(cenv.conf_a),
                            "copyto",
                            str(src),
                            "%s:%s/中文目录/obj.bin" % (cenv.remote_a, cenv.shared_root),
                        ],
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        [
                            rclone_bin,
                            "--config",
                            str(cenv.conf_b),
                            "copyto",
                            "%s:%s/中文目录/obj.bin" % (cenv.remote_b, cenv.shared_root),
                            str(dst),
                        ],
                        check=True,
                        capture_output=True,
                    )
                    self.assertEqual(dst.read_bytes(), payload)


# ===========================================================================
# 1. 真正错时接力 (True Time-Shifted Async Relay: A -> B -> A)
# ===========================================================================

class TestTrueTimeShiftedAsyncRelay(E2EBase):
    U_CODEX_A = "01111111-2222-4333-8444-555555555501"
    U_ANTI_A = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeee0001"
    U_CODEX_B = "02222222-3333-4444-8555-666666666602"
    U_ANTI_B = "bbbbbbbb-cccc-4ddd-8eee-ffffffff0002"

    def test_full_time_shifted_roundtrip_a_to_b_to_a(self) -> None:
        dev_a = DeviceSim(self.base, "a")
        dev_b = DeviceSim(self.base, "b")
        self.addCleanup(dev_a.power_on)
        self.addCleanup(dev_b.power_on)

        dev_a.init_device(os_name="mac", dev_name="Alice MacBook")
        dev_b.init_device(os_name="mac", dev_name="Bob Mac Studio")

        with webdav_server(base_dir=str(self.base / "cloud_dav"), prefer_rclone=True) as cloud_env:
            self.assertEqual(cloud_env.backend, "rclone")
            self.assertNotEqual(cloud_env.remote_a, cloud_env.remote_b)
            pair_fp = pair_two_devices(dev_a, dev_b, cloud_env)
            self.assertTrue(PAIR_FP_RE.match(pair_fp))

            # --- Prepare rich Git repository on A ---
            repo_a = init_git_repo(dev_a.workspaces / "relay-core", branch="feat/async-relay")
            write_text(repo_a / "README.md", "# Relay Core\nInitial commit\n")
            write_text(repo_a / "src" / "app.py", "def run():\n    return 'v1'\n")
            write_text(repo_a / "src" / "both_stages.py", "STAGE = 'committed_v1'\n")
            write_text(repo_a / "src" / "to_delete.txt", "delete me in worktree\n")
            write_text(repo_a / ".env.example", "API_KEY=placeholder\n")
            run_git(repo_a, "add", "-A")
            run_git(repo_a, "commit", "-q", "-m", "feat: initial structure")

            write_text(repo_a / "HANDOFF.md", "## 交接摘要\n- 已完成加密传输层\n- 下一步：验证 B 机接棒\n")
            run_git(repo_a, "add", "HANDOFF.md")
            run_git(repo_a, "commit", "-q", "-m", "docs: add handoff summary")

            # Staged changes: new file + 0-byte new file + staged modification
            write_text(repo_a / "src" / "staged_new.py", "STAGED_FEATURE = True\n")
            write_text(repo_a / "src" / "staged_empty.txt", "")
            write_text(repo_a / "src" / "both_stages.py", "STAGE = 'staged_v2'\n")
            run_git(repo_a, "add", "src/staged_new.py", "src/staged_empty.txt", "src/both_stages.py")

            # Unstaged changes: further edit to `both_stages.py` + worktree deletion of `to_delete.txt`
            write_text(repo_a / "src" / "both_stages.py", "STAGE = 'staged_v2'\nWORKTREE_EXTRA = 'unstaged_v3'\n")
            (repo_a / "src" / "to_delete.txt").unlink()

            # Untracked file + excluded local `.env` secrets
            write_text(repo_a / "notes" / "scratch.md", "A机未跟踪设计草稿：支持流式分块\n")
            write_text(repo_a / ".env", "SUPER_SECRET_KEY_A=never_upload_12345\n")
            write_text(repo_a / ".env.local", "DB_PASSWORD_A=local_only_67890\n")

            head_a = run_git(repo_a, "rev-parse", "HEAD").stdout.strip()
            branch_a = run_git(repo_a, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            staged_diff_a = run_git(repo_a, "diff", "--cached", "--binary", "--full-index", binary=True).stdout
            unstaged_diff_a = run_git(repo_a, "diff", "--binary", "--full-index", binary=True).stdout
            self.assertEqual(branch_a, "feat/async-relay")
            self.assertTrue(len(staged_diff_a) > 0)
            self.assertTrue(len(unstaged_diff_a) > 0)

            # --- Prepare conversations across Claude Code, Codex, and Antigravity on A ---
            put_claude_session(
                dev_a.uhome,
                repo_a,
                "claude-a-unchanged",
                "A机早期只读调研",
                [
                    ("user", "请调研 XChaCha20-Poly1305 secretstream 的分块防截断机制"),
                    ("assistant", "CLAUDE_ARCHIVE_IMMUTABLE_001：通过 TAG_FINAL 标记结束帧即可防截断。"),
                ],
            )
            put_claude_session(
                dev_a.uhome,
                repo_a,
                "claude-a-active",
                "A机主开发会话",
                [
                    ("user", "实现双阶段 ready.json 发布"),
                    ("assistant", "CLAUDE_TOKEN_ALPHA_101：先上传 package.enc 校验大小，再上传签名 ready.json。"),
                ],
            )
            put_codex_session(
                dev_a.uhome,
                repo_a,
                self.U_CODEX_A,
                "A机Codex补丁调试",
                [
                    ("user", "为什么 0 字节暂存文件用 git apply 会丢失？"),
                    ("assistant", "CODEX_TOKEN_BETA_202：需额外记录 stagedEntries 元数据以确保空文件精确恢复。"),
                ],
            )
            put_anti_session(
                dev_a.uhome,
                repo_a,
                self.U_ANTI_A,
                "A机Antigravity验收设计",
                [
                    ("user", "设计三工具历史对话归档与去重策略"),
                    ("assistant", "ANTI_TOKEN_GAMMA_303：按 (tool, sessionId, contentVersion) 去重并生成 Markdown。"),
                ],
                secret_thinking="SECRET_INTERNAL_THOUGHT_8888",
            )

            # Register project & sessions on A
            dev_a.ok("add", "Relay Core", "--path", repo_a, "--id", "relay-core", "--tool", "all")
            dev_a.ok(
                "note",
                "relay-core",
                "--last",
                "完成 A 机流式加密与工作区快照",
                "--next",
                "在 B 机接收并继续开发回执同步",
                "--verify",
                "pass",
                "--verify-cmd",
                "python3 -m unittest",
            )
            dev_a.ok("session", "add", "relay-core", "latest", "--tool", "claude")
            dev_a.ok("session", "add", "relay-core", "latest", "--tool", "codex")
            dev_a.ok("session", "add", "relay-core", "latest", "--tool", "anti")

            # --- Send from A and verify "已交棒，可以关机" ---
            send_out_a = dev_a.ok("send", "relay-core")
            self.assertIn("已交棒，可以关机", send_out_a)

            led_a_after_send = dev_a.load_ledger()
            proj_a = led_a_after_send["projects"][0]
            relay_id_1 = proj_a.get("lastRelayId") or (proj_a.get("handoff") or {}).get("relayId")
            self.assertTrue(relay_id_1, "Expected relayId recorded in A's ledger: %r" % proj_a)

            # Verify cloud storage contains encrypted objects only (no plaintext leaks)
            dev_b_did = dev_b.ok_json("device")["deviceId"]
            raw_ready_1 = cloud_env.read_object("v3/inbox/%s/%s/ready.json" % (dev_b_did, relay_id_1)).decode("utf-8")
            self.assertNotIn("完成 A 机流式加密与工作区快照", raw_ready_1)
            self.assertNotIn("SUPER_SECRET_KEY_A", raw_ready_1)
            raw_pkg_1 = cloud_env.read_object("v3/inbox/%s/%s/package.enc" % (dev_b_did, relay_id_1))
            self.assertNotIn(b"SUPER_SECRET_KEY_A", raw_pkg_1)
            self.assertNotIn(b"CLAUDE_TOKEN_ALPHA_101", raw_pkg_1)

            # --- SIMULATE A SHUTTING DOWN COMPLETELY ---
            dev_a.power_off()
            self.assertFalse(repo_a.exists())
            self.assertFalse(dev_a.home.exists())

            # --- On Device B: check inbox -> receive -> verify code + transcripts ---
            inbox_b = dev_b.ok_json("inbox")
            incoming_b = inbox_b.get("incoming") or []
            self.assertEqual(len(incoming_b), 1, inbox_b)
            item_1 = incoming_b[0]
            self.assertEqual(item_1["relayId"], relay_id_1)
            self.assertEqual(item_1.get("projectId"), "relay-core")
            self.assertEqual(item_1.get("projectName"), "Relay Core")
            self.assertIn("完成 A 机流式加密与工作区快照", item_1.get("lastTask", ""))
            self.assertEqual(item_1.get("branch"), "feat/async-relay")

            # First-time receive without --path must fail cleanly and ask for --path
            code_no_path, _, err_no_path = dev_b.run_cli("receive", relay_id_1)
            self.assertNotEqual(code_no_path, 0)
            self.assertIn("--path", err_no_path)

            repo_b = dev_b.workspaces / "relay-core-on-b"
            recv_b = dev_b.ok_json("receive", relay_id_1, "--path", repo_b)
            self.assertTrue(recv_b.get("ok", True))

            # Verify Git commit, branch, staged index, unstaged worktree, untracked, and `.env` exclusion!
            self.assertEqual(run_git(repo_b, "rev-parse", "HEAD").stdout.strip(), head_a)
            self.assertEqual(run_git(repo_b, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), branch_a)
            staged_diff_b_check = run_git(repo_b, "diff", "--cached", "--binary", "--full-index", binary=True).stdout
            unstaged_diff_b_check = run_git(repo_b, "diff", "--binary", "--full-index", binary=True).stdout
            self.assertEqual(staged_diff_b_check, staged_diff_a)
            self.assertEqual(unstaged_diff_b_check, unstaged_diff_a)

            self.assertTrue((repo_b / "src" / "staged_empty.txt").is_file())
            self.assertEqual((repo_b / "src" / "staged_empty.txt").stat().st_size, 0)
            porcelain_b = run_git(repo_b, "status", "--porcelain").stdout
            self.assertIn("A  src/staged_empty.txt", porcelain_b)
            self.assertFalse((repo_b / "src" / "to_delete.txt").exists())
            self.assertEqual((repo_b / "notes" / "scratch.md").read_text(encoding="utf-8"), "A机未跟踪设计草稿：支持流式分块\n")
            self.assertEqual((repo_b / ".env.example").read_text(encoding="utf-8"), "API_KEY=placeholder\n")
            self.assertFalse((repo_b / ".env").exists())
            self.assertFalse((repo_b / ".env.local").exists())

            # Verify full conversation archive on B across Claude Code, Codex, and Antigravity
            tr_list_b = dev_b.ok_json("transcripts", "relay-core")
            sessions_b = tr_list_b.get("sessions") or []
            tools_on_b = {s["tool"] for s in sessions_b}
            self.assertEqual(tools_on_b, {"claude", "codex", "anti"})
            self.assertEqual(len(sessions_b), 4)

            md_claude_b = dev_b.ok("transcripts", "relay-core", "--session", "claude-a-active")
            self.assertIn("CLAUDE_TOKEN_ALPHA_101", md_claude_b)

            grep_codex_b = dev_b.ok_json("transcripts", "relay-core", "--grep", "CODEX_TOKEN_BETA_202")
            self.assertTrue(len(grep_codex_b.get("matches") or []) >= 1, grep_codex_b)

            md_anti_b = dev_b.ok("transcripts", "relay-core", "--session", self.U_ANTI_A)
            self.assertIn("ANTI_TOKEN_GAMMA_303", md_anti_b)
            self.assertNotIn("SECRET_INTERNAL_THOUGHT_8888", md_anti_b)

            # --- Continue development on B & create new conversations + code ---
            run_git(repo_b, "add", "-A")
            run_git(repo_b, "commit", "-q", "-m", "feat: commit A handoff state and finish B milestone")

            # New staged + unstaged + untracked + B's own local `.env`
            write_text(repo_b / "src" / "b_module.py", "def from_b():\n    return 'b_staged'\n")
            run_git(repo_b, "add", "src/b_module.py")
            write_text(repo_b / "src" / "app.py", "def run():\n    return 'v2_unstaged_on_b'\n")
            write_text(repo_b / "notes" / "b_note.md", "B机新增未跟踪说明\n")
            write_text(repo_b / ".env", "B_PRIVATE_SECRET=stay_on_b_forever\n")

            head_b = run_git(repo_b, "rev-parse", "HEAD").stdout.strip()
            branch_b = run_git(repo_b, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            staged_diff_b = run_git(repo_b, "diff", "--cached", "--binary", "--full-index", binary=True).stdout
            unstaged_diff_b = run_git(repo_b, "diff", "--binary", "--full-index", binary=True).stdout

            # Add brand-new conversations on B across all 3 tools (while A's archived ones sit in B's archive)
            put_claude_session(
                dev_b.uhome,
                repo_b,
                "claude-b-new",
                "B机Claude回执开发",
                [
                    ("user", "补齐 receipt.json 幂等重传逻辑"),
                    ("assistant", "CLAUDE_B_NEW_404：已在本地落盘 signedReceipt 后再执行上传。"),
                ],
            )
            put_codex_session(
                dev_b.uhome,
                repo_b,
                self.U_CODEX_B,
                "B机Codex联合测试",
                [
                    ("user", "检查 S3 路径样式兼容"),
                    ("assistant", "CODEX_B_NEW_505：已通过 force_path_style 验证。"),
                ],
            )
            put_anti_session(
                dev_b.uhome,
                repo_b,
                self.U_ANTI_B,
                "B机Antigravity收尾",
                [
                    ("user", "准备交棒回 A 机"),
                    ("assistant", "ANTI_B_NEW_606：所有状态已就绪，可安全交棒回 A。"),
                ],
            )

            dev_b.ok("note", "relay-core", "--last", "B 机完成回执幂等与多云支持", "--next", "A 机最终验收")
            send_out_b = dev_b.ok("send", "relay-core", "--to", "a")
            self.assertIn("已交棒，可以关机", send_out_b)
            relay_id_2 = dev_b.load_ledger()["projects"][0]["lastRelayId"]
            self.assertNotEqual(relay_id_2, relay_id_1)

            # --- SIMULATE B SHUTTING DOWN & POWERING A BACK ON ---
            dev_b.power_off()
            dev_a.power_on()
            self.assertFalse(repo_b.exists())
            self.assertTrue((repo_a / ".env").is_file())

            # Also test appending a turn to `claude-a-active` or keeping A's local transcripts intact:
            # First run `relay inbox` on A -> verifies receipt sync for `relay_id_1` AND sees `relay_id_2`!
            inbox_a = dev_a.ok_json("inbox")
            incoming_a = inbox_a.get("incoming") or []
            self.assertEqual(len(incoming_a), 1, inbox_a)
            self.assertEqual(incoming_a[0]["relayId"], relay_id_2)
            self.assertEqual(incoming_a[0].get("parentRelayId"), relay_id_1)

            # Verify A synced B's receipt for relay_id_1
            relay_state_a = json.loads((dev_a.home / "relay_state.json").read_text(encoding="utf-8"))
            self.assertTrue(relay_state_a["sentRelays"][relay_id_1]["acked"])

            # Receive `relay_id_2` back onto A's existing registered path (`repo_a`)!
            recv_a = dev_a.ok_json("receive", relay_id_2)
            self.assertTrue(recv_a.get("ok", True))

            # Verify `repo_a` matches B's exact commit, branch, staged, unstaged, untracked, and kept A's `.env`!
            self.assertEqual(run_git(repo_a, "rev-parse", "HEAD").stdout.strip(), head_b)
            self.assertEqual(run_git(repo_a, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), branch_b)
            self.assertEqual(
                run_git(repo_a, "diff", "--cached", "--binary", "--full-index", binary=True).stdout,
                staged_diff_b,
            )
            self.assertEqual(
                run_git(repo_a, "diff", "--binary", "--full-index", binary=True).stdout,
                unstaged_diff_b,
            )
            self.assertEqual((repo_a / "notes" / "b_note.md").read_text(encoding="utf-8"), "B机新增未跟踪说明\n")
            # A's `.env` is preserved and B's `.env` was never brought over!
            self.assertEqual((repo_a / ".env").read_text(encoding="utf-8"), "SUPER_SECRET_KEY_A=never_upload_12345\n")

            # Verify conversation deduplication on A: 4 original + 3 new from B = 7 total, ZERO duplicates!
            tr_a = dev_a.ok_json("transcripts", "relay-core")
            sessions_a = tr_a.get("sessions") or []
            keys_a = [(s["tool"], s["sessionId"], s["contentVersion"]) for s in sessions_a]
            self.assertEqual(len(keys_a), len(set(keys_a)), "Duplicate (tool, sessionId, contentVersion) found!")
            by_tool_sid = [(s["tool"], s["sessionId"]) for s in sessions_a]
            self.assertEqual(len(by_tool_sid), 7, sessions_a)
            self.assertEqual(by_tool_sid.count(("claude", "claude-a-unchanged")), 1)
            self.assertEqual(by_tool_sid.count(("claude", "claude-a-active")), 1)

            for token in ("CLAUDE_B_NEW_404", "CODEX_B_NEW_505", "ANTI_B_NEW_606", "CLAUDE_ARCHIVE_IMMUTABLE_001"):
                g = dev_a.ok_json("transcripts", "relay-core", "--grep", token)
                self.assertTrue(len(g.get("matches") or []) >= 1, (token, g))

            # Extend `claude-a-active` on A with new turns & relay back to B -> verify prefix superseding (still 7 sessions, 0 stale duplicates!)
            put_claude_session(
                dev_a.uhome,
                repo_a,
                "claude-a-active",
                "A机主开发会话",
                [
                    ("user", "实现双阶段 ready.json 发布"),
                    ("assistant", "CLAUDE_TOKEN_ALPHA_101：先上传 package.enc 校验大小，再上传签名 ready.json。"),
                    ("user", "A机接回后继续补充收尾验证"),
                    ("assistant", "CLAUDE_EXTENDED_TURN_707：严格前缀升级且不产生旧版本重复条目。"),
                ],
            )
            dev_a.ok("send", "relay-core")
            relay_id_3 = dev_a.load_ledger()["projects"][0]["lastRelayId"]
            dev_b.power_on()
            dev_b.ok("receive", relay_id_3)
            tr_b_after_3 = dev_b.ok_json("transcripts", "relay-core")
            sess_b_3 = tr_b_after_3.get("sessions") or []
            self.assertEqual(len(sess_b_3), 7, sess_b_3)
            active_entries = [s for s in sess_b_3 if s["sessionId"] == "claude-a-active"]
            self.assertEqual(len(active_entries), 1)
            self.assertEqual(active_entries[0]["turnCount"], 4)
            g_ext = dev_b.ok_json("transcripts", "relay-core", "--grep", "CLAUDE_EXTENDED_TURN_707")
            self.assertTrue(len(g_ext.get("matches") or []) >= 1)


# ===========================================================================
# 2. 真正可换云 (True Cloud Swappability: Real rclone WebDAV <-> S3)
# ===========================================================================

class TestTrueCloudSwappability(E2EBase):
    def _execute_roundtrip(self, dev_a: DeviceSim, dev_b: DeviceSim, proj_id: str, repo_a: Path, repo_b: Path) -> Tuple[str, str]:
        send_1 = dev_a.ok("send", proj_id)
        self.assertIn("已交棒，可以关机", send_1)
        rid_1 = dev_a.load_ledger()["projects"][0]["lastRelayId"]

        dev_a.power_off()
        inbox_b = dev_b.ok_json("inbox")
        self.assertIn(rid_1, [x["relayId"] for x in inbox_b.get("incoming", [])])
        if repo_b.exists():
            dev_b.ok("receive", rid_1)
        else:
            dev_b.ok("receive", rid_1, "--path", repo_b)

        write_text(repo_b / "cloud_hop.txt", "hop via %s\n" % rid_1)
        if (repo_b / ".git").exists():
            run_git(repo_b, "add", "cloud_hop.txt")
            run_git(repo_b, "commit", "-q", "-m", "hop on B")
        send_2 = dev_b.ok("send", proj_id, "--to", "a")
        self.assertIn("已交棒，可以关机", send_2)
        rid_2 = dev_b.load_ledger()["projects"][0]["lastRelayId"]

        dev_b.power_off()
        dev_a.power_on()
        dev_a.ok("receive", rid_2)
        dev_b.power_on()
        self.assertEqual((repo_a / "cloud_hop.txt").read_text(encoding="utf-8"), "hop via %s\n" % rid_1)
        return rid_1, rid_2

    def test_webdav_then_switch_to_s3_with_identical_cli_and_folder_snapshot(self) -> None:
        dev_a = DeviceSim(self.base, "a")
        dev_b = DeviceSim(self.base, "b")
        self.addCleanup(dev_a.power_on)
        self.addCleanup(dev_b.power_on)
        dev_a.init_device(os_name="mac", dev_name="A")
        dev_b.init_device(os_name="linux", dev_name="B")

        repo_a = init_git_repo(dev_a.workspaces / "swap-proj")
        write_text(repo_a / "main.py", "print('hello cloud')\n")
        run_git(repo_a, "add", "main.py")
        run_git(repo_a, "commit", "-q", "-m", "init")
        put_claude_session(dev_a.uhome, repo_a, "s-webdav", "WebDAV阶段", [("user", "测试WebDAV"), ("assistant", "WEBDAV_OK")])
        dev_a.ok("add", "Swap Proj", "--path", repo_a, "--id", "swap-proj")
        repo_b = dev_b.workspaces / "swap-proj"

        # Phase 1: Full round-trip over real `rclone` + WebDAV
        with webdav_server(base_dir=str(self.base / "dav"), prefer_rclone=True) as dav_env:
            self.assertEqual(dav_env.backend, "rclone")
            pair_two_devices(dev_a, dev_b, dav_env)
            rid_dav_1, rid_dav_2 = self._execute_roundtrip(dev_a, dev_b, "swap-proj", repo_a, repo_b)
            self.assertTrue(rid_dav_1 and rid_dav_2)

        # Phase 2: Switch ONLY cloud configuration to real `rclone` + S3-compatible server!
        with s3_server(base_dir=str(self.base / "s3"), prefer_rclone=True) as s3_env:
            self.assertEqual(s3_env.backend, "rclone")
            pair_two_devices(dev_a, dev_b, s3_env)
            write_text(repo_a / "s3_stage.txt", "switched to S3 seamlessly\n")
            put_claude_session(dev_a.uhome, repo_a, "s-s3", "S3阶段", [("user", "切换S3存储"), ("assistant", "S3_SWITCH_OK")])
            rid_s3_1, rid_s3_2 = self._execute_roundtrip(dev_a, dev_b, "swap-proj", repo_a, repo_b)
            self.assertNotEqual(rid_s3_1, rid_dav_1)
            self.assertEqual((repo_b / "s3_stage.txt").read_text(encoding="utf-8"), "switched to S3 seamlessly\n")
            g = dev_b.ok_json("transcripts", "swap-proj", "--grep", "S3_SWITCH_OK")
            self.assertTrue(len(g.get("matches") or []) >= 1)

            # Also verify non-Git folder project over S3 (preserving executable bit)
            folder_a = dev_a.workspaces / "plain-folder"
            script_a = write_text(folder_a / "bin" / "run.sh", "#!/bin/sh\necho ok\n")
            os.chmod(str(script_a), 0o755)
            write_text(folder_a / ".env", "FOLDER_SECRET=hidden\n")
            dev_a.ok("add", "Plain Folder", "--path", folder_a, "--id", "plain-folder")
            dev_a.ok("send", "plain-folder")
            rid_folder = [p for p in dev_a.load_ledger()["projects"] if p["id"] == "plain-folder"][0]["lastRelayId"]
            folder_b = dev_b.workspaces / "plain-folder"
            dev_b.ok("receive", rid_folder, "--path", folder_b)
            self.assertTrue((folder_b / "bin" / "run.sh").is_file())
            self.assertTrue(os.stat(str(folder_b / "bin" / "run.sh")).st_mode & stat.S_IXUSR)
            self.assertFalse((folder_b / ".env").exists())


# ===========================================================================
# 3A. 跨系统矩阵、中文路径与大小写冲突 (Cross-OS, Chinese NFD/NFC, Case Collisions)
# ===========================================================================

class TestCrossOsAndPaths(E2EBase):
    def test_all_six_desktop_os_combinations_and_windows_filename_guard(self) -> None:
        os_pairs = [
            ("windows", "mac"),
            ("mac", "linux"),
            ("windows", "linux"),
            ("mac", "mac"),
            ("windows", "windows"),
            ("linux", "linux"),
        ]
        for idx, (os_a, os_b) in enumerate(os_pairs):
            sub = self.base / ("os_pair_%d_%s_%s" % (idx, os_a, os_b))
            dev_a = DeviceSim(sub, "a")
            dev_b = DeviceSim(sub, "b")
            dev_a.init_device(os_name=os_a, dev_name="DevA-%s" % os_a)
            dev_b.init_device(os_name=os_b, dev_name="DevB-%s" % os_b)

            local_cloud_dir = sub / "cloud_local"
            local_cloud_dir.mkdir(parents=True, exist_ok=True)
            dummy_env = CloudServerEnv(
                protocol="local",
                backend="local",
                port=0,
                endpoint="",
                data_dir=local_cloud_dir,
                storage_root_dir=local_cloud_dir,
                shared_root="",
                conf_a=sub / "na.conf",
                conf_b=sub / "nb.conf",
            )
            pair_two_devices(dev_a, dev_b, dummy_env, provider_type="local")

            repo_a = init_git_repo(dev_a.workspaces / "proj")
            write_text(repo_a / "hello.txt", "%s -> %s\n" % (os_a, os_b))
            run_git(repo_a, "add", "hello.txt")
            run_git(repo_a, "commit", "-q", "-m", "init")
            dev_a.ok("add", "CrossOS", "--path", repo_a, "--id", "cross-os")
            if os_b == "windows":
                # Verify foreign Windows path in `path-other` is never mangled on POSIX
                dev_a.ok("set", "cross-os", "--path-other", r"D:\Work\CrossOS")
                self.assertIn("CrossOS", dev_a.load_ledger()["projects"][0]["paths"].get("b", ""))

            # If target_os == "windows", verify invalid Windows filename (e.g. `CON.txt` or `bad:name.txt`) is caught!
            if os_b == "windows":
                bad_file = repo_a / "AUX.txt"
                write_text(bad_file, "reserved on windows\n")
                code_bad, _, err_bad = dev_a.run_cli("send", "cross-os")
                self.assertNotEqual(code_bad, 0, "Expected Windows reserved name AUX.txt to be rejected")
                bad_file.unlink()

            send_out = dev_a.ok("send", "cross-os")
            self.assertIn("已交棒，可以关机", send_out)
            rid = dev_a.load_ledger()["projects"][0]["lastRelayId"]
            repo_b = dev_b.workspaces / "proj_b"
            dev_b.ok("receive", rid, "--path", repo_b)
            self.assertEqual((repo_b / "hello.txt").read_text(encoding="utf-8"), "%s -> %s\n" % (os_a, os_b))

    def test_chinese_paths_and_macos_nfd_nfc_normalization(self) -> None:
        dev_a = DeviceSim(self.base, "a")
        dev_b = DeviceSim(self.base, "b")
        dev_a.init_device(os_name="mac", dev_name="A")
        dev_b.init_device(os_name="windows", dev_name="B")

        with webdav_server(base_dir=str(self.base / "zh_dav"), prefer_rclone=True) as cloud_env:
            pair_two_devices(dev_a, dev_b, cloud_env)

            # Chinese workspace path + nested Chinese dirs + decomposed NFD filename
            repo_a = init_git_repo(dev_a.workspaces / "测试工作区" / "双机接力项目")
            doc_rel = Path("核心模块") / "说明文档.md"
            nfd_name = unicodedata.normalize("NFD", "café_苹果分解_说明.md")
            write_text(repo_a / doc_rel, "# 核心模块说明\n初始版本\n")
            write_text(repo_a / "核心模块" / nfd_name, "NFD 内容测试\n")
            run_git(repo_a, "add", "-A")
            run_git(repo_a, "commit", "-q", "-m", "feat: 中文目录初始化")

            # Add staged + unstaged + untracked Chinese files
            write_text(repo_a / "核心模块" / "暂存脚本.py", "VALUE = '暂存修改'\n")
            run_git(repo_a, "add", "核心模块/暂存脚本.py")
            write_text(repo_a / doc_rel, "# 核心模块说明\n工作区未暂存改动\n")
            write_text(repo_a / "未跟踪目录" / "草稿备忘.txt", "纯中文未跟踪文件\n")

            put_claude_session(
                dev_a.uhome,
                repo_a,
                "zh-session-1",
                "中文路径会话验证",
                [("user", "检查中文目录归档"), ("assistant", "ZH_PATH_CONV_OK：中文目录对话已归档")],
            )

            dev_a.ok("add", "双机接力中文项目", "--path", repo_a, "--id", "zh-proj")
            # Run `send` without project argument, relying on `cwd` inside nested Chinese folder!
            send_out = dev_a.ok("send", cwd=repo_a / "核心模块")
            self.assertIn("已交棒，可以关机", send_out)
            rid = dev_a.load_ledger()["projects"][0]["lastRelayId"]

            repo_b = dev_b.workspaces / "接收端工作区" / "双机接力项目"
            dev_b.ok("receive", rid, "--path", repo_b)
            self.assertEqual((repo_b / doc_rel).read_text(encoding="utf-8"), "# 核心模块说明\n工作区未暂存改动\n")
            self.assertEqual((repo_b / "核心模块" / "暂存脚本.py").read_text(encoding="utf-8"), "VALUE = '暂存修改'\n")
            self.assertEqual((repo_b / "未跟踪目录" / "草稿备忘.txt").read_text(encoding="utf-8"), "纯中文未跟踪文件\n")
            nfc_name = unicodedata.normalize("NFC", nfd_name)
            self.assertTrue((repo_b / "核心模块" / nfc_name).is_file())
            g = dev_b.ok_json("transcripts", "zh-proj", "--grep", "ZH_PATH_CONV_OK")
            self.assertTrue(len(g.get("matches") or []) >= 1)

    def test_case_collision_rejected_without_corrupting_target(self) -> None:
        dev_a = DeviceSim(self.base, "a")
        dev_b = DeviceSim(self.base, "b")
        # Even if A is Linux (case-sensitive) and B is Linux, let's test both:
        # 1) A -> B when B is mac/windows: rejected at send time!
        # 2) A -> B when B is declared linux, then received onto case-insensitive macOS directory: rejected at receive time!
        dev_a.init_device(os_name="linux", dev_name="LinuxA")
        dev_b.init_device(os_name="mac", dev_name="MacB")

        cloud_dir = self.base / "case_cloud"
        cloud_dir.mkdir()
        cenv = CloudServerEnv("local", "local", 0, "", cloud_dir, cloud_dir, "", self.base / "a", self.base / "b")
        pair_two_devices(dev_a, dev_b, cenv, provider_type="local")

        # Craft a real Git commit containing BOTH `Foo.txt` and `foo.txt` in the Git index/tree
        # using `git hash-object` + `git update-index` so this works even on case-insensitive macOS APFS!
        repo_a = init_git_repo(dev_a.workspaces / "case-repo")
        b1 = subprocess.run(
            GIT_CMD + ["-C", str(repo_a), "hash-object", "-w", "--stdin"],
            input="upper Foo\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        b2 = subprocess.run(
            GIT_CMD + ["-C", str(repo_a), "hash-object", "-w", "--stdin"],
            input="lower foo\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        run_git(repo_a, "update-index", "--add", "--cacheinfo", "100644,%s,Foo.txt" % b1)
        run_git(repo_a, "update-index", "--add", "--cacheinfo", "100644,%s,foo.txt" % b2)
        run_git(repo_a, "commit", "-q", "-m", "commit with Foo.txt and foo.txt")

        dev_a.ok("add", "Case Repo", "--path", repo_a, "--id", "case-repo")
        # Target B is `mac`, so `relay send` must reject with case collision!
        code_send, _, err_send = dev_a.run_cli("send", "case-repo")
        self.assertNotEqual(code_send, 0)
        self.assertTrue("Foo.txt" in err_send or "大小写" in err_send or "冲突" in err_send, err_send)

        # Now set B's declared OS to `linux` so `send` packages it, then test `receive` on case-insensitive FS (if host FS is case-insensitive)!
        dev_a.ok("device", "--which", "b", "--os", "linux")
        dev_b.ok("device", "--which", "b", "--os", "linux")
        probe_upper = self.base / "CASE_PROBE_TMP"
        probe_lower = self.base / "case_probe_tmp"
        probe_upper.write_text("1", encoding="utf-8")
        host_is_case_insensitive = probe_lower.exists()
        probe_upper.unlink()

        if host_is_case_insensitive:
            dev_a.ok("send", "case-repo")
            rid = dev_a.load_ledger()["projects"][0]["lastRelayId"]
            repo_b = dev_b.workspaces / "case-target"
            code_recv, _, err_recv = dev_b.run_cli("receive", rid, "--path", repo_b)
            self.assertNotEqual(code_recv, 0)
            self.assertFalse((repo_b / ".git").exists(), "Target dir must not be partially initialized on PathConflictError")


# ===========================================================================
# 3B. 故障恢复与安全门禁 (Gaps, Secrets, Retry, Duplicate, Tamper, Dirty, Receipt)
# ===========================================================================

class TestFaultRecoveryAndSecurityGates(E2EBase):
    def _setup_paired_local(self) -> Tuple[DeviceSim, DeviceSim, CloudServerEnv]:
        dev_a = DeviceSim(self.base, "a")
        dev_b = DeviceSim(self.base, "b")
        dev_a.init_device(os_name="mac", dev_name="A")
        dev_b.init_device(os_name="mac", dev_name="B")
        cloud_dir = self.base / "cloud_store"
        cloud_dir.mkdir(parents=True, exist_ok=True)
        cenv = CloudServerEnv("local", "local", 0, "", cloud_dir, cloud_dir, "", self.base / "a.conf", self.base / "b.conf")
        pair_two_devices(dev_a, dev_b, cenv, provider_type="local")
        return dev_a, dev_b, cenv

    def test_conversation_gaps_reported_and_retry_succeeds_after_fix(self) -> None:
        dev_a, dev_b, _ = self._setup_paired_local()
        repo_a = init_git_repo(dev_a.workspaces / "gap-proj")
        write_text(repo_a / "README.md", "gap check\n")
        run_git(repo_a, "add", "README.md")
        run_git(repo_a, "commit", "-q", "-m", "init")

        # 1) Create a Claude session, register it, then DELETE its file -> missing gap!
        claude_fp = put_claude_session(
            dev_a.uhome, repo_a, "missing-claude-sid", "将被删除的会话", [("user", "hi"), ("assistant", "hello")]
        )
        dev_a.ok("add", "Gap Proj", "--path", repo_a, "--id", "gap-proj")
        dev_a.ok("session", "add", "gap-proj", "missing-claude-sid", "--tool", "claude")
        claude_fp.unlink()

        # 2) Create a truncated Codex JSONL (unterminated JSON line) -> truncated gap!
        codex_uid = "09999999-8888-4777-8666-555555555599"
        codex_fp = put_codex_session(dev_a.uhome, repo_a, codex_uid, "截断会话", [("user", "ok")])
        with open(str(codex_fp), "a", encoding="utf-8") as fh:
            fh.write('{"type": "response_item", "payload": {"unterminated_json\n')

        # 3) Create an Antigravity session with `truncated_fields` in `transcript.jsonl` and NO `transcript_full.jsonl`!
        anti_sid = "77777777-6666-4555-8444-333333333322"
        put_anti_session(
            dev_a.uhome,
            repo_a,
            anti_sid,
            "Anti截断缺口",
            [("user", "长输出测试"), ("assistant", "这是一段被截断的模型回复全文内容")],
            include_full=False,
            truncated_compact=True,
        )

        code_gap, _, err_gap = dev_a.run_cli("send", "gap-proj")
        self.assertNotEqual(code_gap, 0)
        # Must list ALL specific gaps clearly!
        self.assertIn("missing-claude-sid", err_gap)
        self.assertIn(codex_uid, err_gap)
        self.assertIn(anti_sid, err_gap)
        self.assertEqual(dev_b.ok_json("inbox").get("incoming"), [])

        # Fix all 3 gaps: restore Claude file, fix Codex file, supply Antigravity `transcript_full.jsonl`
        put_claude_session(
            dev_a.uhome, repo_a, "missing-claude-sid", "已补齐的会话", [("user", "hi"), ("assistant", "FIXED_CLAUDE_OK")]
        )
        put_codex_session(dev_a.uhome, repo_a, codex_uid, "已修复会话", [("user", "ok"), ("assistant", "FIXED_CODEX_OK")])
        put_anti_session(
            dev_a.uhome,
            repo_a,
            anti_sid,
            "Anti已补齐全文",
            [("user", "长输出测试"), ("assistant", "FIXED_ANTI_FULL_OK")],
            include_full=True,
            truncated_compact=True,
        )

        out_fixed = dev_a.ok("send", "gap-proj")
        self.assertIn("已交棒，可以关机", out_fixed)

    def test_secret_gate_blocks_staged_env_and_git_history_private_key(self) -> None:
        dev_a, dev_b, cenv = self._setup_paired_local()

        # Subcase 1: Staged `.env` file must be blocked!
        repo_env = init_git_repo(dev_a.workspaces / "sec-env")
        write_text(repo_env / "app.py", "x = 1\n")
        run_git(repo_env, "add", "app.py")
        run_git(repo_env, "commit", "-q", "-m", "init")
        write_text(repo_env / ".env", "AWS_SECRET_ACCESS_KEY=abcd\n")
        run_git(repo_env, "add", "-f", ".env")
        dev_a.ok("add", "Sec Env", "--path", repo_env, "--id", "sec-env")

        code_1, _, err_1 = dev_a.run_cli("send", "sec-env")
        self.assertNotEqual(code_1, 0)
        self.assertIn(".env", err_1)
        self.assertEqual(dev_b.ok_json("inbox").get("incoming"), [])

        # Subcase 2: Private key committed in an OLD Git commit (deleted in HEAD, still in Git history!)
        repo_pk = init_git_repo(dev_a.workspaces / "sec-pk")
        pem_block = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU=\n-----END OPENSSH PRIVATE KEY-----\n"
        write_text(repo_pk / "notes.txt", "oops pasted key:\n" + pem_block)
        run_git(repo_pk, "add", "notes.txt")
        run_git(repo_pk, "commit", "-q", "-m", "accidental key commit")
        # Delete the key in the next commit so HEAD looks clean, while Git history still contains it!
        write_text(repo_pk / "notes.txt", "cleaned head\n")
        run_git(repo_pk, "add", "notes.txt")
        run_git(repo_pk, "commit", "-q", "-m", "remove key from worktree")

        dev_a.ok("add", "Sec PK", "--path", repo_pk, "--id", "sec-pk")
        code_2, _, err_2 = dev_a.run_cli("send", "sec-pk")
        self.assertNotEqual(code_2, 0, "Private key in Git commit history must be rejected before upload!")
        self.assertEqual(dev_b.ok_json("inbox").get("incoming"), [])
        # Ensure nothing was uploaded under `relays/` or `v3/inbox/`
        relays_dir = cenv.storage_root_dir / "relays"
        if relays_dir.exists():
            self.assertEqual(list(relays_dir.rglob("ready.json")), [])
        v3_inbox_dir = cenv.storage_root_dir / "v3" / "inbox"
        if v3_inbox_dir.exists():
            self.assertEqual(list(v3_inbox_dir.rglob("ready.json")), [])

    def test_interrupted_upload_invisible_in_inbox_and_reuses_same_relay_id(self) -> None:
        from relaydesk import storage

        dev_a, dev_b, cenv = self._setup_paired_local()
        repo_a = init_git_repo(dev_a.workspaces / "retry-proj")
        write_text(repo_a / "README.md", "atomic publish test\n")
        run_git(repo_a, "add", "README.md")
        run_git(repo_a, "commit", "-q", "-m", "init")
        dev_a.ok("add", "Retry Proj", "--path", repo_a, "--id", "retry-proj")

        orig_upload = storage.LocalDirProvider.upload

        def fail_on_ready(provider_self: Any, local_path: str, remote_key: str) -> Any:
            if remote_key.endswith("/ready.json"):
                raise storage.StorageError("模拟网络中断：ready.json 上传失败")
            return orig_upload(provider_self, local_path, remote_key)

        with mock.patch.object(storage.LocalDirProvider, "upload", fail_on_ready):
            code_fail, _, err_fail = dev_a.run_cli("send", "retry-proj")
            self.assertNotEqual(code_fail, 0)

        # `package.enc` was uploaded, `ready.json` was NOT -> B's inbox MUST see 0 claimable packages!
        enc_files = list((cenv.storage_root_dir / "v3" / "inbox").rglob("package.enc"))
        self.assertEqual(len(enc_files), 1)
        interrupted_relay_id = enc_files[0].parent.name
        self.assertFalse((enc_files[0].parent / "ready.json").exists())
        self.assertEqual(dev_b.ok_json("inbox").get("incoming"), [])

        # Retry `relay send` on A -> MUST reuse the exact same `relayId`!
        retry_out = dev_a.ok("send", "retry-proj")
        self.assertIn("已交棒，可以关机", retry_out)
        final_relay_id = dev_a.load_ledger()["projects"][0]["lastRelayId"]
        self.assertEqual(final_relay_id, interrupted_relay_id)

        incoming_b = dev_b.ok_json("inbox").get("incoming") or []
        self.assertEqual([x["relayId"] for x in incoming_b], [interrupted_relay_id])

    def test_duplicate_receive_rejected_without_touching_workspace(self) -> None:
        dev_a, dev_b, _ = self._setup_paired_local()
        repo_a = init_git_repo(dev_a.workspaces / "dup-proj")
        write_text(repo_a / "a.txt", "v1\n")
        run_git(repo_a, "add", "a.txt")
        run_git(repo_a, "commit", "-q", "-m", "init")
        dev_a.ok("add", "Dup Proj", "--path", repo_a, "--id", "dup-proj")
        dev_a.ok("send", "dup-proj")
        rid = dev_a.load_ledger()["projects"][0]["lastRelayId"]

        repo_b = dev_b.workspaces / "dup-proj"
        dev_b.ok("receive", rid, "--path", repo_b)
        write_text(repo_b / "a.txt", "local_edit_after_receive\n")

        # Calling `receive` a second time must fail cleanly and leave `repo_b` untouched!
        code_dup, _, err_dup = dev_b.run_cli("receive", rid)
        self.assertNotEqual(code_dup, 0)
        self.assertEqual((repo_b / "a.txt").read_text(encoding="utf-8"), "local_edit_after_receive\n")

    def test_tamper_detection_ciphertext_truncation_and_ready_signature(self) -> None:
        dev_a, dev_b, cenv = self._setup_paired_local()
        repo_a = init_git_repo(dev_a.workspaces / "tamper-proj")
        # Make incompressible payload > 160 KB so `secretstream` exercises multi-chunk framing + TAG_FINAL!
        (repo_a / "big.bin").write_bytes(os.urandom(180000))
        write_text(repo_a / "big.txt", "multi-chunk companion\n")
        run_git(repo_a, "add", "big.bin", "big.txt")
        run_git(repo_a, "commit", "-q", "-m", "multi-chunk payload")
        dev_a.ok("add", "Tamper Proj", "--path", repo_a, "--id", "tamper-proj")
        dev_a.ok("send", "tamper-proj")
        rid = dev_a.load_ledger()["projects"][0]["lastRelayId"]

        dev_b_did = dev_b.ok_json("device")["deviceId"]
        enc_key = "v3/inbox/%s/%s/package.enc" % (dev_b_did, rid)
        ready_key = "v3/inbox/%s/%s/ready.json" % (dev_b_did, rid)
        orig_enc = cenv.read_object(enc_key)
        orig_ready = cenv.read_object(ready_key)
        self.assertGreater(len(orig_enc), 130000)

        repo_b = dev_b.workspaces / "tamper-target"

        # 1) Flip 1 byte in `package.enc`
        flipped = bytearray(orig_enc)
        flipped[len(flipped) // 2] ^= 0xFF
        cenv.write_object(enc_key, bytes(flipped))
        code_1, _, _ = dev_b.run_cli("receive", rid, "--path", repo_b)
        self.assertNotEqual(code_1, 0)
        self.assertFalse(repo_b.exists())
        self.assertEqual(dev_b.load_ledger()["projects"], [])

        # 2) Truncate `package.enc` (drop trailing bytes / TAG_FINAL)
        cenv.write_object(enc_key, orig_enc[:-32])
        code_2, _, _ = dev_b.run_cli("receive", rid, "--path", repo_b)
        self.assertNotEqual(code_2, 0)
        self.assertFalse(repo_b.exists())
        self.assertEqual(dev_b.load_ledger()["projects"], [])

        # 3) Tamper with `ready.json` (recompute SHA-256 of truncated package without valid Ed25519 signature)
        ready_obj = json.loads(orig_ready.decode("utf-8"))
        ready_obj["toAlias"] = "b"
        ready_obj["tamperedField"] = "evil"
        cenv.write_object(enc_key, orig_enc)
        cenv.write_object(ready_key, json.dumps(ready_obj).encode("utf-8"))
        code_3, _, _ = dev_b.run_cli("receive", rid, "--path", repo_b)
        self.assertNotEqual(code_3, 0)
        self.assertFalse(repo_b.exists())
        self.assertEqual(dev_b.load_ledger()["projects"], [])

        # Restore untampered objects -> now `receive` succeeds!
        cenv.write_object(ready_key, orig_ready)
        dev_b.ok("receive", rid, "--path", repo_b)
        self.assertTrue((repo_b / "big.txt").is_file())

    def test_receiver_dirty_target_preserved_and_into_new_dir_succeeds(self) -> None:
        dev_a, dev_b, _ = self._setup_paired_local()
        repo_a = init_git_repo(dev_a.workspaces / "dirty-guard")
        write_text(repo_a / "code.py", "VERSION = 1\n")
        run_git(repo_a, "add", "code.py")
        run_git(repo_a, "commit", "-q", "-m", "v1")
        dev_a.ok("add", "Dirty Guard", "--path", repo_a, "--id", "dirty-guard")

        # Initial handoff A -> B -> A so B already has `repo_b` registered
        dev_a.ok("send", "dirty-guard")
        rid_1 = dev_a.load_ledger()["projects"][0]["lastRelayId"]
        repo_b = dev_b.workspaces / "dirty-guard"
        dev_b.ok("receive", rid_1, "--path", repo_b)
        dev_b.ok("send", "dirty-guard", "--to", "a")
        rid_2 = dev_b.load_ledger()["projects"][0]["lastRelayId"]
        dev_a.ok("receive", rid_2)

        # A makes v2 and sends `rid_3` to B
        write_text(repo_a / "code.py", "VERSION = 2\n")
        run_git(repo_a, "add", "code.py")
        run_git(repo_a, "commit", "-q", "-m", "v2 on A")
        dev_a.ok("send", "dirty-guard")
        rid_3 = dev_a.load_ledger()["projects"][0]["lastRelayId"]

        # Meanwhile on B, user accidentally made unsynced local edits AND a local commit in `repo_b`!
        write_text(repo_b / "code.py", "VERSION = 'b_local_diverged_commit'\n")
        run_git(repo_b, "add", "code.py")
        run_git(repo_b, "commit", "-q", "-m", "local diverged commit on B")
        write_text(repo_b / "unsaved_local.txt", "precious local work on B\n")
        b_diverged_head = run_git(repo_b, "rev-parse", "HEAD").stdout.strip()

        # `relay receive rid_3` MUST refuse and keep `repo_b` 100% intact!
        code_dirty, _, err_dirty = dev_b.run_cli("receive", rid_3)
        self.assertNotEqual(code_dirty, 0)
        self.assertIn("--into", err_dirty)
        self.assertEqual(run_git(repo_b, "rev-parse", "HEAD").stdout.strip(), b_diverged_head)
        self.assertEqual((repo_b / "unsaved_local.txt").read_text(encoding="utf-8"), "precious local work on B\n")
        self.assertEqual((repo_b / "code.py").read_text(encoding="utf-8"), "VERSION = 'b_local_diverged_commit'\n")

        # Receiving with `--into <repo_b_fresh>` succeeds while `repo_b` STILL retains its local work!
        repo_b_fresh = dev_b.workspaces / "dirty-guard-incoming"
        dev_b.ok("receive", rid_3, "--into", repo_b_fresh)
        self.assertEqual((repo_b_fresh / "code.py").read_text(encoding="utf-8"), "VERSION = 2\n")
        self.assertEqual((repo_b / "unsaved_local.txt").read_text(encoding="utf-8"), "precious local work on B\n")
        self.assertEqual((repo_b / "code.py").read_text(encoding="utf-8"), "VERSION = 'b_local_diverged_commit'\n")

    def test_post_apply_receipt_upload_failure_recovery_is_idempotent(self) -> None:
        from relaydesk import storage

        dev_a, dev_b, cenv = self._setup_paired_local()
        repo_a = init_git_repo(dev_a.workspaces / "ack-proj")
        write_text(repo_a / "README.md", "applied from A\n")
        run_git(repo_a, "add", "README.md")
        run_git(repo_a, "commit", "-q", "-m", "init")
        dev_a.ok("add", "Ack Proj", "--path", repo_a, "--id", "ack-proj")
        dev_a.ok("send", "ack-proj")
        rid = dev_a.load_ledger()["projects"][0]["lastRelayId"]

        orig_upload = storage.LocalDirProvider.upload

        def fail_on_receipt(provider_self: Any, local_path: str, remote_key: str) -> Any:
            if remote_key.endswith("/receipt.json"):
                raise storage.StorageError("模拟网络故障：receipt.json 上传中断")
            return orig_upload(provider_self, local_path, remote_key)

        repo_b = dev_b.workspaces / "ack-proj"
        with mock.patch.object(storage.LocalDirProvider, "upload", fail_on_receipt):
            code_ack_fail, _, err_ack_fail = dev_b.run_cli("receive", rid, "--path", repo_b)
            self.assertNotEqual(code_ack_fail, 0)

        # Local apply MUST already be complete on B, even though receipt.json is missing on cloud!
        dev_b_did = dev_b.ok_json("device")["deviceId"]
        receipt_key = "v3/inbox/%s/%s/receipt.json" % (dev_b_did, rid)
        self.assertEqual((repo_b / "README.md").read_text(encoding="utf-8"), "applied from A\n")
        self.assertFalse(cenv.object_path(receipt_key).exists())

        # User immediately starts editing `repo_b` before retrying `relay receive <rid>`!
        write_text(repo_b / "README.md", "edited on B right after apply!\n")
        write_text(repo_b / "new_b_work.py", "WORK = 42\n")

        # Re-running `relay receive <rid>` MUST only upload the saved receipt WITHOUT overwriting `repo_b` or complaining about dirty worktree!
        dev_b.ok("receive", rid)
        self.assertTrue(cenv.object_path(receipt_key).is_file())
        self.assertEqual((repo_b / "README.md").read_text(encoding="utf-8"), "edited on B right after apply!\n")
        self.assertEqual((repo_b / "new_b_work.py").read_text(encoding="utf-8"), "WORK = 42\n")

        # Device A's `relay inbox` processes the recovered receipt!
        inbox_a = dev_a.ok_json("inbox")
        relay_state_a = json.loads((dev_a.home / "relay_state.json").read_text(encoding="utf-8"))
        self.assertTrue(relay_state_a["sentRelays"][rid]["acked"], inbox_a)


# ===========================================================================
# 3C. Web Workbench HTTP API E2E (`/api/cloud/*`)
# ===========================================================================

class TestWebWorkbenchCloudApiE2E(E2EBase):
    def _http_req(
        self,
        dev: DeviceSim,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Any]:
        html_path = L.tool_dir() / "relay-desk.html"
        store = cli._FileStore(dev.ledger_path)
        with mock.patch.dict(os.environ, dev.env_dict(), clear=True):
            srv = server.make_server(str(html_path), store, port=0)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
                try:
                    req_headers = {"Host": "127.0.0.1:%d" % port}
                    if method != "GET":
                        req_headers["X-Relay-Desk"] = "1"
                        req_headers["Content-Type"] = "application/json"
                    if headers is not None:
                        req_headers.update(headers)
                    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
                    conn.request(method, path, body=payload, headers=req_headers)
                    resp = conn.getresponse()
                    raw = resp.read().decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(raw) if raw else {}
                    except ValueError:
                        parsed = raw
                    return resp.status, parsed
                finally:
                    conn.close()
            finally:
                srv.shutdown()
                srv.server_close()
                t.join(timeout=2)

    def test_cloud_http_endpoints_end_to_end(self) -> None:
        dev_a = DeviceSim(self.base, "a")
        dev_b = DeviceSim(self.base, "b")
        dev_a.init_device(os_name="mac", dev_name="WebA")
        dev_b.init_device(os_name="mac", dev_name="WebB")

        # Security check: POST without `X-Relay-Desk` header must be rejected with 403
        st_403, _ = self._http_req(dev_a, "POST", "/api/cloud/send", {"project": "x"}, headers={"X-Relay-Desk": ""})
        self.assertEqual(st_403, 403)

        # GET `/` returns workbench HTML with new cloud UI features
        st_idx, html_doc = self._http_req(dev_a, "GET", "/")
        self.assertEqual(st_idx, 200)
        self.assertIn("待接棒", html_doc)
        self.assertIn("已交棒，可以关机", html_doc)

        with webdav_server(base_dir=str(self.base / "api_dav"), prefer_rclone=True) as cenv:
            # Status before pairing
            st_a0, status_a0 = self._http_req(dev_a, "GET", "/api/cloud/status")
            self.assertEqual(st_a0, 200)
            self.assertTrue(status_a0.get("hasCrypto"))
            self.assertTrue(status_a0.get("hasRclone"))

            # Publish keys via POST /api/cloud/pair (accept=False then accept=True)
            st_pa1, pa1 = self._http_req(
                dev_a,
                "POST",
                "/api/cloud/pair",
                {"provider": "rclone", "remote": cenv.remote_a, "root": cenv.shared_root, "configPath": str(cenv.conf_a), "accept": False},
            )
            self.assertEqual(st_pa1, 200, pa1)
            st_pb1, pb1 = self._http_req(
                dev_b,
                "POST",
                "/api/cloud/pair",
                {"provider": "rclone", "remote": cenv.remote_b, "root": cenv.shared_root, "configPath": str(cenv.conf_b), "accept": False},
            )
            self.assertEqual(st_pb1, 200, pb1)
            self.assertTrue(pb1.get("pairFingerprint"))

            st_a1, status_a1 = self._http_req(dev_a, "GET", "/api/cloud/status")
            self.assertEqual(st_a1, 200, status_a1)
            self.assertEqual(status_a1.get("pairFingerprint"), pb1.get("pairFingerprint"))

            st_pa2, pa2 = self._http_req(dev_a, "POST", "/api/cloud/pair", {"accept": True})
            st_pb2, pb2 = self._http_req(dev_b, "POST", "/api/cloud/pair", {"accept": True})
            self.assertEqual(st_pa2, 200)
            self.assertEqual(st_pb2, 200)
            self.assertTrue(pa2.get("paired"))
            self.assertTrue(pb2.get("paired"))
            self.assertEqual(pa2.get("pairFingerprint"), pb2.get("pairFingerprint"))

            # Create project + conversation on A and send via POST /api/cloud/send
            repo_a = init_git_repo(dev_a.workspaces / "web-proj")
            write_text(repo_a / "index.js", "console.log('web v1');\n")
            run_git(repo_a, "add", "index.js")
            run_git(repo_a, "commit", "-q", "-m", "web v1")
            put_claude_session(
                dev_a.uhome, repo_a, "web-claude-1", "网页工作台对话", [("user", "测试 API"), ("assistant", "WEB_API_TRANSCRIPT_999")]
            )
            dev_a.ok("add", "Web Proj", "--path", repo_a, "--id", "web-proj")
            dev_a.ok("note", "web-proj", "--last", "通过网页端交棒", "--next", "B 端网页接棒")

            st_send, send_res = self._http_req(dev_a, "POST", "/api/cloud/send", {"project": "web-proj", "to": "b"})
            self.assertEqual(st_send, 200, send_res)
            self.assertEqual(send_res.get("message"), "已交棒，可以关机")
            rid_1 = send_res["relayId"]

            # B checks GET /api/cloud/inbox
            st_inb, inb_res = self._http_req(dev_b, "GET", "/api/cloud/inbox")
            self.assertEqual(st_inb, 200, inb_res)
            self.assertEqual(len(inb_res.get("incoming") or []), 1)
            self.assertEqual(inb_res["incoming"][0]["relayId"], rid_1)

            # First receive on B without `path` -> HTTP 409 `need_path`!
            st_409_path, err_need_path = self._http_req(dev_b, "POST", "/api/cloud/receive", {"relayId": rid_1})
            self.assertEqual(st_409_path, 409, err_need_path)
            self.assertEqual(err_need_path.get("code"), "need_path")

            # Receive with `path` -> HTTP 200
            repo_b = dev_b.workspaces / "web-proj"
            st_rcv, rcv_res = self._http_req(
                dev_b, "POST", "/api/cloud/receive", {"relayId": rid_1, "path": str(repo_b)}
            )
            self.assertEqual(st_rcv, 200, rcv_res)
            self.assertEqual((repo_b / "index.js").read_text(encoding="utf-8"), "console.log('web v1');\n")

            # Query transcripts via GET /api/cloud/transcripts
            st_tr1, tr1 = self._http_req(dev_b, "GET", "/api/cloud/transcripts?project=web-proj")
            self.assertEqual(st_tr1, 200, tr1)
            self.assertEqual(len(tr1.get("sessions") or []), 1)

            st_tr2, tr2 = self._http_req(dev_b, "GET", "/api/cloud/transcripts?project=web-proj&q=WEB_API_TRANSCRIPT_999")
            self.assertEqual(st_tr2, 200, tr2)
            self.assertTrue(len(tr2.get("matches") or []) >= 1)

            st_tr3, tr3 = self._http_req(dev_b, "GET", "/api/cloud/transcripts?project=web-proj&session=web-claude-1")
            self.assertEqual(st_tr3, 200, tr3)
            self.assertIn("WEB_API_TRANSCRIPT_999", tr3.get("markdown") or "")

            # Roundtrip B -> A -> B to test HTTP 409 `target_dirty` + `into`
            st_sb, sb_res = self._http_req(dev_b, "POST", "/api/cloud/send", {"project": "web-proj", "to": "a"})
            self.assertEqual(st_sb, 200, sb_res)
            st_ra, ra_res = self._http_req(dev_a, "POST", "/api/cloud/receive", {"relayId": sb_res["relayId"]})
            self.assertEqual(st_ra, 200, ra_res)

            write_text(repo_a / "index.js", "console.log('web v2');\n")
            run_git(repo_a, "add", "index.js")
            run_git(repo_a, "commit", "-q", "-m", "web v2")
            st_sa2, sa2_res = self._http_req(dev_a, "POST", "/api/cloud/send", {"project": "web-proj", "to": "b"})
            self.assertEqual(st_sa2, 200, sa2_res)
            rid_3 = sa2_res["relayId"]

            # Make `repo_b` dirty on B -> POST /api/cloud/receive returns HTTP 409 `target_dirty`!
            write_text(repo_b / "dirty.txt", "unsynced web edit\n")
            st_409_dirty, err_dirty = self._http_req(dev_b, "POST", "/api/cloud/receive", {"relayId": rid_3})
            self.assertEqual(st_409_dirty, 409, err_dirty)
            self.assertEqual(err_dirty.get("code"), "target_dirty")
            self.assertEqual((repo_b / "dirty.txt").read_text(encoding="utf-8"), "unsynced web edit\n")

            # Retry with `into` -> HTTP 200 into `repo_b_alt`, preserving `repo_b`!
            repo_b_alt = dev_b.workspaces / "web-proj-alt"
            st_into, into_res = self._http_req(
                dev_b, "POST", "/api/cloud/receive", {"relayId": rid_3, "into": str(repo_b_alt)}
            )
            self.assertEqual(st_into, 200, into_res)
            self.assertEqual((repo_b_alt / "index.js").read_text(encoding="utf-8"), "console.log('web v2');\n")
            self.assertEqual((repo_b / "dirty.txt").read_text(encoding="utf-8"), "unsynced web edit\n")


# ===========================================================================
# 4. Symlink Alias CWD Regression (`/var` <-> `/private/var` / symlinked workspace)
# ===========================================================================

class TestSymlinkCwdRegression(E2EBase):
    @unittest.skipIf(os.name == "nt", "POSIX symlink alias regression")
    def test_symlink_alias_cwd_project_and_session_lookup(self) -> None:
        dev_a = DeviceSim(self.base, "a")
        dev_a.init_device(os_name="mac", dev_name="MacA")

        real_ws = init_git_repo(self.base / "real_dir" / "proj")
        write_text(real_ws / "README.md", "symlink test\n")
        run_git(real_ws, "add", "README.md")
        run_git(real_ws, "commit", "-q", "-m", "init")

        link_parent = self.base / "alias_link"
        os.symlink(str(self.base / "real_dir"), str(link_parent))
        alias_ws = link_parent / "proj"

        # Register project via symlink path, create sessions referencing realpath (and vice versa!)
        put_claude_session(dev_a.uhome, real_ws, "sym-claude", "SymlinkClaude", [("user", "u"), ("assistant", "a")])
        put_codex_session(
            dev_a.uhome,
            alias_ws,
            "05555555-4444-4333-8222-111111111100",
            "SymlinkCodex",
            [("user", "u"), ("assistant", "a")],
        )
        put_anti_session(
            dev_a.uhome,
            real_ws,
            "55555555-4444-4333-8222-111111111199",
            "SymlinkAnti",
            [("user", "u"), ("assistant", "a")],
        )

        dev_a.ok("add", "Sym Proj", "--path", alias_ws, "--id", "sym-proj")

        # `status` and `sessions` with NO project arg must match whether `cwd` is `real_ws` or `alias_ws`!
        st_real = dev_a.ok_json("status", cwd=real_ws)
        st_alias = dev_a.ok_json("status", cwd=alias_ws)
        self.assertEqual(len(st_real.get("projects") or []), 1)
        self.assertEqual(len(st_alias.get("projects") or []), 1)

        sess_from_real = dev_a.ok_json("sessions", cwd=real_ws)
        found_list = sess_from_real.get("sessions", sess_from_real) if isinstance(sess_from_real, dict) else sess_from_real
        found_tools = {s["tool"] for s in found_list}
        self.assertEqual(found_tools, {"claude", "codex", "anti"}, sess_from_real)

    @unittest.skipUnless(os.path.islink("/var") and os.path.isdir("/private/var/tmp"), "macOS /var -> /private/var symlink check")
    def test_macos_var_vs_private_var_cwd_relay_send_and_scan(self) -> None:
        raw_td = tempfile.mkdtemp(prefix="relay_var_test_", dir="/var/tmp")
        self.addCleanup(lambda: shutil.rmtree(raw_td, ignore_errors=True))
        var_root = Path(raw_td)
        priv_root = Path("/private" + raw_td)
        self.assertTrue(str(var_root).startswith("/var/"))
        self.assertTrue(priv_root.is_dir())

        dev_a = DeviceSim(self.base, "a")
        dev_b = DeviceSim(self.base, "b")
        dev_a.init_device(os_name="mac", dev_name="MacVarA")
        dev_b.init_device(os_name="mac", dev_name="MacVarB")
        cdir = self.base / "var_cloud"
        cdir.mkdir()
        pair_two_devices(
            dev_a,
            dev_b,
            CloudServerEnv("local", "local", 0, "", cdir, cdir, "", self.base / "va", self.base / "vb"),
            provider_type="local",
        )

        var_repo = init_git_repo(var_root / "proj")
        priv_repo = priv_root / "proj"
        write_text(var_repo / "hello.py", "print('var vs private/var')\n")
        run_git(var_repo, "add", "hello.py")
        run_git(var_repo, "commit", "-q", "-m", "var commit")
        put_claude_session(dev_a.uhome, var_repo, "var-claude-1", "VarAlias", [("user", "hi"), ("assistant", "VAR_ALIAS_OK")])

        # Register via `/private/var/...` and invoke `scan` and `send` with `cwd=/var/...` (no project arg!)
        dev_a.ok("add", "Var Proj", "--path", priv_repo, "--id", "var-proj")
        dev_a.ok("scan", cwd=var_repo)
        send_out = dev_a.ok("send", cwd=var_repo)
        self.assertIn("已交棒，可以关机", send_out)
        rid = dev_a.load_ledger()["projects"][0]["lastRelayId"]
        dev_b.ok("receive", rid, "--path", dev_b.workspaces / "var-proj-b")
        g = dev_b.ok_json("transcripts", "var-proj", "--grep", "VAR_ALIAS_OK")
        self.assertTrue(len(g.get("matches") or []) >= 1)


if __name__ == "__main__":
    unittest.main()
