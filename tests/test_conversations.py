import json
import os
import stat
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydesk import conversations as C  # noqa: E402
from relaydesk import scan  # noqa: E402


def write_jsonl(path, lines, mtime=None):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(str(p), "w", encoding="utf-8") as f:
        for ln in lines:
            if isinstance(ln, str):
                f.write(ln if ln.endswith("\n") else (ln + "\n"))
            else:
                f.write(json.dumps(ln, ensure_ascii=False) + "\n")
    if mtime is not None:
        os.utime(str(p), (mtime, mtime))
    return p


class BaseConvTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = os.path.realpath(self._td.name)
        self.local_a = os.path.join(self.tmp, "relay_home_a")
        self.local_b = os.path.join(self.tmp, "relay_home_b")
        self.uhome_a = os.path.join(self.tmp, "user_a")
        self.uhome_b = os.path.join(self.tmp, "user_b")
        self.proj_a = os.path.join(self.tmp, "workspace_a", "baton-demo")
        self.proj_b = os.path.join(self.tmp, "workspace_b", "baton-demo")
        os.makedirs(self.proj_a, exist_ok=True)
        os.makedirs(self.proj_b, exist_ok=True)

    def make_project(self, pid="baton-demo", sessions=None, alt_a=None):
        return {
            "id": pid,
            "name": "Baton Demo",
            "device": "a",
            "paths": {"a": self.proj_a, "b": self.proj_b},
            "altPaths": {"a": list(alt_a or []), "b": []},
            "sessions": list(sessions or []),
        }

    def put_claude(self, uhome, cwd, sid, lines, mtime=1_800_000_100):
        fp = Path(uhome) / ".claude" / "projects" / scan.enc_path(cwd) / (sid + ".jsonl")
        return write_jsonl(fp, lines, mtime=mtime)

    def put_codex(self, uhome, sid, lines, day="18", mtime=1_800_000_200):
        fp = (
            Path(uhome)
            / ".codex"
            / "sessions"
            / "2026"
            / "11"
            / day
            / ("rollout-2026-11-%sT10-00-00-%s.jsonl" % (day, sid))
        )
        return write_jsonl(fp, lines, mtime=mtime)

    def put_codex_index(self, uhome, rows):
        fp = Path(uhome) / ".codex" / "session_index.jsonl"
        return write_jsonl(fp, rows)

    def put_anti(self, uhome, sid, lines, full=True, cli=False, mtime=1_800_000_300):
        sub = "antigravity-cli" if cli else "antigravity"
        fname = "transcript_full.jsonl" if full else "transcript.jsonl"
        fp = (
            Path(uhome)
            / ".gemini"
            / sub
            / "brain"
            / sid
            / ".system_generated"
            / "logs"
            / fname
        )
        return write_jsonl(fp, lines, mtime=mtime)


class ExtractionTests(BaseConvTest):
    CODEX_SID = "019a0000-1111-4222-8333-000000000001"
    ANTI_SID = "7d5cf4ca-1a27-4cd1-8e71-66f532b39ace"

    def test_extracts_claude_codex_and_antigravity_visible_turns(self):
        # 1. Claude Code: string content, content blocks (text, tool_use, tool_result, thinking omitted), summary
        self.put_claude(self.uhome_a, self.proj_a, "claude-s1", [
            {"type": "custom-title", "customTitle": "Claude 导出修复"},
            {"type": "summary", "summary": "前期已定位 CSV 编码问题"},
            {
                "type": "user",
                "timestamp": "2026-11-18T01:00:00Z",
                "message": {"role": "user", "content": "请检查 export_csv 的 UTF-8 BOM 处理"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-11-18T01:00:05Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "INTERNAL_SECRET_THOUGHT_CLAUDE"},
                        {"type": "text", "text": "我先读取 exporter.py 看一下实现。"},
                        {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "exporter.py"}},
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-11-18T01:00:06Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": [{"type": "text", "text": "def export_csv(): pass"}],
                        }
                    ],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-11-18T01:00:10Z",
                "message": {"role": "assistant", "content": "已补上 utf-8-sig 写入并验证通过。"},
            },
        ])

        # 2. Codex: session_meta, event_msg + response_item (deduped), function_call, function_call_output, top-level role
        self.put_codex_index(self.uhome_a, [
            {"id": self.CODEX_SID, "thread_name": "Codex 队列重试实现"},
        ])
        self.put_codex(self.uhome_a, self.CODEX_SID, [
            {"type": "session_meta", "payload": {"id": self.CODEX_SID, "cwd": self.proj_a}},
            {
                "timestamp": "2026-11-18T02:00:00Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "DEV_PROMPT_HIDDEN"}]},
            },
            {
                "timestamp": "2026-11-18T02:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "给上传模块加上同号重试"},
            },
            {
                "timestamp": "2026-11-18T02:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "给上传模块加上同号重试"}],
                },
            },
            {
                "timestamp": "2026-11-18T02:00:03Z",
                "type": "response_item",
                "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "INTERNAL_CODEX_REASONING"}]},
            },
            {
                "timestamp": "2026-11-18T02:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "arguments": json.dumps({"command": "pytest tests/test_retry.py"}),
                },
            },
            {
                "timestamp": "2026-11-18T02:00:05Z",
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": "3 passed in 0.12s"},
            },
            {
                "timestamp": "2026-11-18T02:00:08Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "同号重试逻辑已经完成并通过单测。"}],
                },
            },
            {
                "timestamp": "2026-11-18T02:00:09Z",
                "role": "user",
                "content": "再确认一下异常分支",
            },
        ])

        # 3. Antigravity: prefer transcript_full.jsonl over truncated transcript.jsonl; omit thinking
        self.put_anti(self.uhome_a, self.ANTI_SID, [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "created_at": "2026-11-18T03:00:00Z",
                "metadata": {"workspaceUri": Path(self.proj_a).as_uri()},
                "content": "<USER_REQUEST>\n实现双机对话归档搜索\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nActive Workspace: %s\n</ADDITIONAL_METADATA>" % self.proj_a,
                "truncated_fields": ["content"],
            },
        ], full=False)
        self.put_anti(self.uhome_a, self.ANTI_SID, [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2026-11-18T03:00:00Z",
                "metadata": {"workspaceUri": Path(self.proj_a).as_uri()},
                "content": "<USER_REQUEST>\n实现双机对话归档搜索\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nActive Workspace: %s\n</ADDITIONAL_METADATA>" % self.proj_a,
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-18T03:00:04Z",
                "thinking": "INTERNAL_ANTI_THINKING_DO_NOT_LEAK",
                "content": "正在扫描并合并三个工具的历史记录。",
                "tool_calls": [
                    {"name": "read_file", "args": {"Path": json.dumps(os.path.join(self.proj_a, "README.md"))}},
                ],
            },
            {
                "step_index": 2,
                "source": "TOOL",
                "type": "GENERIC",
                "created_at": "2026-11-18T03:00:05Z",
                "content": "README loaded: 42 lines",
            },
        ], full=True)

        proj = self.make_project()
        bundle = C.collect_project_conversations(proj, "a", self.local_a, user_home=self.uhome_a)
        self.assertEqual(bundle["formatVersion"], 1)
        self.assertEqual(bundle["projectId"], "baton-demo")
        by_tool = {e["tool"]: e for e in bundle["entries"]}
        self.assertEqual(set(by_tool.keys()), {"claude", "codex", "anti"})

        # Verify Claude entry
        ce = by_tool["claude"]
        self.assertEqual(ce["sessionId"], "claude-s1")
        self.assertEqual(ce["title"], "Claude 导出修复")
        self.assertEqual(ce["originDevice"], "a")
        self.assertTrue(ce["contentVersion"].startswith("v1_"))
        self.assertEqual(ce["turnCount"], 5)
        claude_blob = json.dumps(ce["turns"], ensure_ascii=False)
        self.assertNotIn("INTERNAL_SECRET_THOUGHT_CLAUDE", claude_blob)
        self.assertIn("前期已定位 CSV 编码问题", ce["turns"][0]["content"])
        self.assertIn("[调用工具 Read]", ce["turns"][2]["content"])
        self.assertEqual(ce["turns"][3]["role"], "tool")
        self.assertIn("def export_csv(): pass", ce["turns"][3]["content"])

        # Verify Codex entry
        xe = by_tool["codex"]
        self.assertEqual(xe["sessionId"], self.CODEX_SID)
        self.assertEqual(xe["title"], "Codex 队列重试实现")
        codex_blob = json.dumps(xe["turns"], ensure_ascii=False)
        self.assertNotIn("DEV_PROMPT_HIDDEN", codex_blob)
        self.assertNotIn("INTERNAL_CODEX_REASONING", codex_blob)
        # Deduped adjacent event_msg + response_item for same user message
        user_retry_turns = [t for t in xe["turns"] if "给上传模块加上同号重试" in t["content"]]
        self.assertEqual(len(user_retry_turns), 1)
        self.assertTrue(any("[调用工具 shell]" in t["content"] for t in xe["turns"]))
        self.assertTrue(any(t["role"] == "tool" and "3 passed" in t["content"] for t in xe["turns"]))
        self.assertEqual(xe["turns"][-1]["content"], "再确认一下异常分支")

        # Verify Antigravity entry
        ae = by_tool["anti"]
        self.assertEqual(ae["sessionId"], self.ANTI_SID)
        self.assertEqual(ae["title"], "实现双机对话归档搜索")
        anti_blob = json.dumps(ae["turns"], ensure_ascii=False)
        self.assertNotIn("INTERNAL_ANTI_THINKING_DO_NOT_LEAK", anti_blob)
        self.assertEqual(ae["turns"][0], {
            "role": "user",
            "timestamp": "2026-11-18T03:00:00Z",
            "content": "实现双机对话归档搜索",
        })
        self.assertIn("[调用工具 read_file]", ae["turns"][1]["content"])
        self.assertEqual(ae["turns"][2]["role"], "tool")
        self.assertIn("README loaded: 42 lines", ae["turns"][2]["content"])

    def test_symlink_realpath_and_chinese_nfd_nfc_paths(self):
        zh_name_nfc = unicodedata.normalize("NFC", "双机接力项目_测试")
        zh_name_nfd = unicodedata.normalize("NFD", "双机接力项目_测试")
        real_dir = os.path.join(self.tmp, zh_name_nfc)
        os.makedirs(real_dir, exist_ok=True)
        link_dir = os.path.join(self.tmp, "symlink_proj")
        os.symlink(real_dir, link_dir)

        # Store Claude transcript under realpath NFD encoding while project registers symlink path
        self.put_claude(self.uhome_a, os.path.join(self.tmp, zh_name_nfd), "zh-s1", [
            {"type": "user", "message": {"role": "user", "content": "测试中文路径归档"}},
        ])
        # Store Codex rollout referencing real_dir while project registers link_dir
        codex_sid = "019a0000-2222-4333-8444-000000000002"
        self.put_codex(self.uhome_a, codex_sid, [
            {"type": "session_meta", "payload": {"id": codex_sid, "cwd": real_dir}},
            {"role": "user", "content": "Codex 中文与软链匹配"},
        ])

        proj = {
            "id": "zh-proj",
            "name": "中文项目",
            "device": "a",
            "paths": {"a": link_dir, "b": ""},
            "sessions": [],
        }
        with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": self.uhome_a}):
            bundle = C.collect_project_conversations(proj, "a", self.local_a)
        tools = {e["tool"]: e for e in bundle["entries"]}
        self.assertIn("claude", tools)
        self.assertIn("codex", tools)
        self.assertEqual(tools["claude"]["sessionId"], "zh-s1")
        self.assertEqual(tools["codex"]["sessionId"], codex_sid)

    def test_anti_compact_fallback_works_when_no_truncated_fields(self):
        sid = "22222222-3333-4444-8555-666666666666"
        self.put_anti(self.uhome_a, sid, [
            {
                "type": "USER_INPUT",
                "cwd": self.proj_a,
                "content": "<USER_REQUEST>短对话无需 full 文件</USER_REQUEST>",
                "truncated_fields": [],
            },
            {
                "type": "PLANNER_RESPONSE",
                "content": "一切正常。",
            },
        ], full=False)
        bundle = C.collect_project_conversations(self.make_project(), "a", self.local_a, user_home=self.uhome_a)
        self.assertEqual(len(bundle["entries"]), 1)
        self.assertEqual(bundle["entries"][0]["sessionId"], sid)
        self.assertEqual(bundle["entries"][0]["turnCount"], 2)


class GapDetectionTests(BaseConvTest):
    def test_missing_registered_sessions_across_all_tools_and_ignores_other_device(self):
        proj = self.make_project(sessions=[
            {"id": "s1", "tool": "claude", "device": "a", "kind": "local", "sessionId": "miss-claude", "title": "缺失的 Claude"},
            {"id": "s2", "tool": "codex", "device": "a", "kind": "local", "sessionId": "019a0000-0000-4000-8000-000000000099", "title": "缺失的 Codex"},
            {"id": "s3", "tool": "anti", "device": "a", "kind": "local", "sessionId": "88888888-0000-4000-8000-000000000099", "title": "缺失的 Anti"},
            # Remote/cloud or other-device sessions must NOT be flagged as missing on device 'a'
            {"id": "s4", "tool": "claude", "device": "b", "kind": "local", "sessionId": "on-device-b", "title": "B 上的会话"},
            {"id": "s5", "tool": "codex", "device": "a", "kind": "cloud", "sessionId": "cloud-sid", "title": "云端会话"},
        ])
        with self.assertRaises(C.ConversationGapError) as ctx:
            C.collect_project_conversations(proj, "a", self.local_a, user_home=self.uhome_a)

        err = ctx.exception
        self.assertEqual(len(err.gaps), 3)
        by_tool = {g["tool"]: g for g in err.gaps}
        self.assertEqual(set(by_tool.keys()), {"claude", "codex", "anti"})
        for g in err.gaps:
            self.assertEqual(g["reason"], "missing")
            self.assertTrue(g["registered"])
        msg = str(err)
        self.assertIn("缺口", msg)
        self.assertIn("miss-claude", msg)
        self.assertIn("缺失的 Anti", msg)

    def test_zero_byte_nul_byte_and_truncated_json_line_detected(self):
        # 1. 0-byte Claude file (unregistered discovered in project dir)
        zero_fp = Path(self.uhome_a) / ".claude" / "projects" / scan.enc_path(self.proj_a) / "zero-session.jsonl"
        zero_fp.parent.mkdir(parents=True, exist_ok=True)
        zero_fp.write_bytes(b"")

        # 2. Half-flushed JSON line in Codex rollout
        codex_sid = "019a0000-5555-4666-8777-000000000005"
        bad_codex = (
            Path(self.uhome_a)
            / ".codex"
            / "sessions"
            / "2026"
            / "11"
            / "18"
            / ("rollout-2026-11-18T12-00-00-%s.jsonl" % codex_sid)
        )
        bad_codex.parent.mkdir(parents=True, exist_ok=True)
        bad_codex.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": codex_sid, "cwd": self.proj_a}})
            + '\n{"type":"event_msg","payload":{"type":"user_message","message":"半截未刷盘',
            encoding="utf-8",
        )

        # 3. NUL byte in Claude file
        nul_fp = Path(self.uhome_a) / ".claude" / "projects" / scan.enc_path(self.proj_a) / "nul-session.jsonl"
        nul_fp.write_bytes(b'{"type":"user","content":"ok"}\n\x00\x00')

        with self.assertRaises(C.ConversationGapError) as ctx:
            C.collect_project_conversations(self.make_project(), "a", self.local_a, user_home=self.uhome_a)

        gaps = {g["sessionId"]: g for g in ctx.exception.gaps}
        self.assertIn("zero-session", gaps)
        self.assertEqual(gaps["zero-session"]["reason"], "truncated")
        self.assertIn("0 字节", gaps["zero-session"]["detail"])

        self.assertIn(codex_sid, gaps)
        self.assertEqual(gaps[codex_sid]["reason"], "truncated")

        self.assertIn("nul-session", gaps)
        self.assertEqual(gaps["nul-session"]["reason"], "truncated")

        # Fix all three files and retry -> succeeds cleanly
        write_jsonl(zero_fp, [{"type": "user", "content": "已补齐零字节会话"}])
        write_jsonl(bad_codex, [
            {"type": "session_meta", "payload": {"id": codex_sid, "cwd": self.proj_a}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "完整刷盘内容"}},
        ])
        write_jsonl(nul_fp, [{"type": "user", "content": "已清除损坏空字节"}])

        bundle = C.collect_project_conversations(self.make_project(), "a", self.local_a, user_home=self.uhome_a)
        self.assertEqual(len(bundle["entries"]), 3)

    def test_antigravity_truncated_fields_without_full_transcript_raises_gap(self):
        anti_sid = "33333333-4444-4555-8666-777777777777"
        self.put_anti(self.uhome_a, anti_sid, [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "cwd": self.proj_a,
                "content": "<USER_REQUEST>长对话测试</USER_REQUEST>",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "这是被截断的紧凑预览...",
                "truncated_fields": ["content"],
            },
        ], full=False)

        with self.assertRaises(C.ConversationGapError) as ctx:
            C.collect_project_conversations(self.make_project(), "a", self.local_a, user_home=self.uhome_a)

        err = ctx.exception
        self.assertEqual(len(err.gaps), 1)
        g = err.gaps[0]
        self.assertEqual(g["tool"], "anti")
        self.assertEqual(g["sessionId"], anti_sid)
        self.assertEqual(g["reason"], "truncated")
        self.assertIn("transcript.jsonl 含截断字段且缺少 transcript_full.jsonl", g["detail"])
        self.assertIn("transcript.jsonl 含截断字段且缺少 transcript_full.jsonl", str(err))

        # Supplement transcript_full.jsonl and retry -> succeeds!
        self.put_anti(self.uhome_a, anti_sid, [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "cwd": self.proj_a,
                "content": "<USER_REQUEST>长对话测试</USER_REQUEST>",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "这是完整未截断的回复全文。",
            },
        ], full=True)
        bundle = C.collect_project_conversations(self.make_project(), "a", self.local_a, user_home=self.uhome_a)
        self.assertEqual(len(bundle["entries"]), 1)
        self.assertIn("这是完整未截断的回复全文。", bundle["entries"][0]["turns"][1]["content"])

    @unittest.skipIf(os.name == "nt", "POSIX chmod read permission test")
    def test_permission_read_error_detected(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root bypasses chmod 000")
        fp = self.put_claude(self.uhome_a, self.proj_a, "unreadable-s1", [
            {"type": "user", "content": "权限受限会话"},
        ])
        os.chmod(str(fp), 0)
        self.addCleanup(lambda: os.chmod(str(fp), stat.S_IRUSR | stat.S_IWUSR) if fp.exists() else None)

        proj = self.make_project(sessions=[
            {"id": "s_perm", "tool": "claude", "device": "a", "kind": "local", "sessionId": "unreadable-s1", "title": "权限测试"},
        ])
        with self.assertRaises(C.ConversationGapError) as ctx:
            C.collect_project_conversations(proj, "a", self.local_a, user_home=self.uhome_a)

        self.assertEqual(len(ctx.exception.gaps), 1)
        g = ctx.exception.gaps[0]
        self.assertEqual(g["reason"], "read_error")
        self.assertEqual(g["sessionId"], "unreadable-s1")
        self.assertTrue(g["registered"])


class RoundTripMergeAndQueryTests(BaseConvTest):
    CODEX_A = "019a0000-aaaa-4bbb-8ccc-000000000001"
    CODEX_B = "019a0000-bbbb-4ccc-8ddd-000000000002"
    ANTI_A = "aaaa1111-2222-4333-8444-555555555555"

    def test_a_to_b_to_a_roundtrip_dedup_prefix_supersede_and_query(self):
        # -------- Step 1: Author conversations on Device A --------
        self.put_claude(self.uhome_a, self.proj_a, "claude-a1", [
            {"customTitle": "A 端架构设计"},
            {"type": "user", "timestamp": "2026-11-18T01:00:00Z", "content": "设计云接力加密信封格式"},
            {"type": "assistant", "timestamp": "2026-11-18T01:00:10Z", "content": "采用 libsodium secretstream 分块加密。"},
        ], mtime=1_800_000_100)
        self.put_codex_index(self.uhome_a, [{"id": self.CODEX_A, "thread_name": "A 端存储适配"}])
        self.put_codex(self.uhome_a, self.CODEX_A, [
            {"type": "session_meta", "payload": {"id": self.CODEX_A, "cwd": self.proj_a}},
            {"type": "event_msg", "timestamp": "2026-11-18T01:10:00Z", "payload": {"type": "user_message", "message": "封装 rclone WebDAV 接口"}},
            {"type": "event_msg", "timestamp": "2026-11-18T01:10:08Z", "payload": {"type": "agent_message", "message": "已实现 StorageProvider 与 RcloneProvider。"}},
        ], mtime=1_800_000_200)
        self.put_anti(self.uhome_a, self.ANTI_A, [
            {"type": "USER_INPUT", "created_at": "2026-11-18T01:20:00Z", "cwd": self.proj_a, "content": "<USER_REQUEST>检查跨平台大小写冲突</USER_REQUEST>"},
            {"type": "PLANNER_RESPONSE", "created_at": "2026-11-18T01:20:05Z", "content": "已加入 casefold 冲突检测。"},
        ], full=True, mtime=1_800_000_300)

        proj = self.make_project()
        bundle_a1 = C.collect_project_conversations(proj, "a", self.local_a, user_home=self.uhome_a)
        self.assertEqual(len(bundle_a1["entries"]), 3)
        # collect_project_conversations is side-effect free until merge_and_save_archive is called
        self.assertFalse((C.archive_dir(self.local_a, "baton-demo") / "index.json").exists())
        C.merge_and_save_archive(self.local_a, "baton-demo", bundle_a1)

        # -------- Step 2: Receive on Device B --------
        res_b1 = C.merge_and_save_archive(self.local_b, "baton-demo", bundle_a1)
        self.assertEqual((res_b1["total"], res_b1["added"], res_b1["updated"], res_b1["skipped"]), (3, 3, 0, 0))
        self.assertTrue(os.path.isfile(res_b1["indexPath"]))
        md_dir_b = Path(res_b1["archiveDir"]) / "markdown"
        latest_dir_b = Path(res_b1["archiveDir"]) / "latest"
        self.assertEqual(len([n for n in os.listdir(str(md_dir_b)) if n.endswith(".md")]), 3)
        self.assertEqual(len([n for n in os.listdir(str(latest_dir_b)) if n.endswith(".md")]), 3)

        # Idempotent re-merge of exact same bundle on B -> 0 added, 3 skipped
        res_b_dup = C.merge_and_save_archive(self.local_b, "baton-demo", bundle_a1)
        self.assertEqual((res_b_dup["total"], res_b_dup["added"], res_b_dup["updated"], res_b_dup["skipped"]), (3, 0, 0, 3))

        # -------- Step 3: Device B adds a new Codex conversation & relays back to A --------
        self.put_codex_index(self.uhome_b, [{"id": self.CODEX_B, "thread_name": "B 端断点续传测试"}])
        self.put_codex(self.uhome_b, self.CODEX_B, [
            {"type": "session_meta", "payload": {"id": self.CODEX_B, "cwd": self.proj_b}},
            {"type": "event_msg", "timestamp": "2026-11-18T04:00:00Z", "payload": {"type": "user_message", "message": "在 Mac 上补充 S3 与 WebDAV 切换验证"}},
            {"type": "event_msg", "timestamp": "2026-11-18T04:00:12Z", "payload": {"type": "agent_message", "message": "双后端契约测试全部绿灯。"}},
        ], mtime=1_800_050_000)

        # Note: A's sessions are recorded on device 'a' in ledger, so B does not fail missing-check for them
        proj_on_b = dict(proj, device="b", sessions=[
            {"id": "s_a1", "tool": "claude", "device": "a", "kind": "local", "sessionId": "claude-a1"},
            {"id": "s_b1", "tool": "codex", "device": "b", "kind": "local", "sessionId": self.CODEX_B},
        ])
        bundle_b1 = C.collect_project_conversations(proj_on_b, "b", self.local_b, user_home=self.uhome_b)
        self.assertEqual(len(bundle_b1["entries"]), 4)
        C.merge_and_save_archive(self.local_b, "baton-demo", bundle_b1)
        origins = {e["sessionId"]: e["originDevice"] for e in bundle_b1["entries"]}
        self.assertEqual(origins["claude-a1"], "a")
        self.assertEqual(origins[self.CODEX_B], "b")

        # -------- Step 4: Device A receives bundle_b1 --------
        res_a1 = C.merge_and_save_archive(self.local_a, "baton-demo", bundle_b1)
        self.assertEqual((res_a1["total"], res_a1["added"], res_a1["updated"], res_a1["skipped"]), (4, 1, 0, 3))

        # -------- Step 5: Device A resumes claude-a1 (appending turns) & sends back to B --------
        old_claude_cv = [e["contentVersion"] for e in res_a1["entries"] if e["sessionId"] == "claude-a1"][0]
        self.put_claude(self.uhome_a, self.proj_a, "claude-a1", [
            {"customTitle": "A 端架构设计"},
            {"type": "user", "timestamp": "2026-11-18T01:00:00Z", "content": "设计云接力加密信封格式"},
            {"type": "assistant", "timestamp": "2026-11-18T01:00:10Z", "content": "采用 libsodium secretstream 分块加密。"},
            {"type": "user", "timestamp": "2026-11-18T05:00:00Z", "content": "补充回执签名字段 packageSha256"},
            {"type": "assistant", "timestamp": "2026-11-18T05:00:09Z", "content": "已在 receipt.json 中加入 packageSha256 校验。"},
        ], mtime=1_800_100_000)

        bundle_a2 = C.collect_project_conversations(proj, "a", self.local_a, user_home=self.uhome_a)
        # Still 4 sessions total (no prefix duplicate for claude-a1, and B's CODEX_B preserved!)
        self.assertEqual(len(bundle_a2["entries"]), 4)
        claude_entries = [e for e in bundle_a2["entries"] if e["sessionId"] == "claude-a1"]
        self.assertEqual(len(claude_entries), 1)
        self.assertEqual(claude_entries[0]["turnCount"], 4)
        self.assertNotEqual(claude_entries[0]["contentVersion"], old_claude_cv)

        # Merge on B -> claude-a1 updated=1, other 3 skipped=3, added=0, total=4!
        res_b2 = C.merge_and_save_archive(self.local_b, "baton-demo", bundle_a2)
        self.assertEqual((res_b2["total"], res_b2["added"], res_b2["updated"], res_b2["skipped"]), (4, 0, 1, 3))
        # Superseded markdown file removed from disk
        md_files_b = os.listdir(str(md_dir_b))
        self.assertFalse(any(old_claude_cv in fn for fn in md_files_b))
        self.assertTrue(any(claude_entries[0]["contentVersion"] in fn for fn in md_files_b))

        # -------- Step 6: Query archive (list, filter, grep, render markdown) --------
        q_all = C.query_archive(self.local_b, "baton-demo")
        self.assertEqual(len(q_all["sessions"]), 4)
        self.assertIsNone(q_all["markdown"])
        for s_sum in q_all["sessions"]:
            self.assertNotIn("turns", s_sum)

        q_codex = C.query_archive(self.local_b, "baton-demo", tool="codex")
        self.assertEqual(len(q_codex["sessions"]), 2)

        q_one = C.query_archive(self.local_b, "baton-demo", session_id="claude-a1")
        self.assertEqual(len(q_one["sessions"]), 1)
        self.assertIsNotNone(q_one["markdown"])
        for header_kw in ("工具", "Session ID", "标题", "来源设备", "最近使用", "内容版本", "回合数"):
            self.assertIn(header_kw, q_one["markdown"])
        self.assertIn("packageSha256", q_one["markdown"])

        # Grep across turns and titles (case-insensitive + regex)
        q_grep = C.query_archive(self.local_b, "baton-demo", grep="SECRETSTREAM")
        self.assertEqual(len(q_grep["sessions"]), 1)
        self.assertEqual(q_grep["sessions"][0]["sessionId"], "claude-a1")
        self.assertGreaterEqual(len(q_grep["matches"]), 1)

        q_grep_b = C.query_archive(self.local_b, "baton-demo", grep="S3.*WebDAV")
        self.assertEqual([s["sessionId"] for s in q_grep_b["sessions"]], [self.CODEX_B])

    def test_diverged_versions_of_same_session_both_preserved(self):
        b1 = {
            "formatVersion": 1,
            "projectId": "div-proj",
            "entries": [{
                "tool": "claude",
                "sessionId": "s-div",
                "originDevice": "a",
                "title": "分支一",
                "cwd": "/a",
                "lastUsed": "2026-11-18",
                "turns": [{"role": "user", "timestamp": "t1", "content": "方案甲"}],
            }],
        }
        b2 = {
            "formatVersion": 1,
            "projectId": "div-proj",
            "entries": [{
                "tool": "claude",
                "sessionId": "s-div",
                "originDevice": "b",
                "title": "分支二",
                "cwd": "/b",
                "lastUsed": "2026-11-18",
                "turns": [{"role": "user", "timestamp": "t2", "content": "方案乙（完全分叉）"}],
            }],
        }
        C.merge_and_save_archive(self.local_a, "div-proj", b1)
        res = C.merge_and_save_archive(self.local_a, "div-proj", b2)
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["added"], 1)

    def test_older_prefix_never_regresses_newer_archive_and_prefix_query(self):
        longer = {
            "formatVersion": 1,
            "projectId": "pref-proj",
            "entries": [{
                "tool": "anti",
                "sessionId": "abcdef12-3456-4789-8000-111122223333",
                "originDevice": "a",
                "title": "完整进度",
                "cwd": "/a",
                "lastUsed": "2026-11-18",
                "turns": [
                    {"role": "user", "timestamp": "t1", "content": "第一步"},
                    {"role": "assistant", "timestamp": "t2", "content": "[调用工具 Read] config.py"},
                    {"role": "user", "timestamp": "t3", "content": "第二步补充"},
                ],
            }],
        }
        shorter = {
            "formatVersion": 1,
            "projectId": "pref-proj",
            "entries": [{
                "tool": "anti",
                "sessionId": "abcdef12-3456-4789-8000-111122223333",
                "originDevice": "a",
                "title": "早期进度",
                "cwd": "/a",
                "lastUsed": "2026-11-17",
                "turns": [
                    {"role": "user", "timestamp": "t1", "content": "第一步"},
                ],
            }],
        }
        C.merge_and_save_archive(self.local_a, "pref-proj", longer)
        res = C.merge_and_save_archive(self.local_a, "pref-proj", shorter)
        self.assertEqual((res["total"], res["added"], res["updated"], res["skipped"]), (1, 0, 0, 1))
        self.assertEqual(res["entries"][0]["turnCount"], 3)

        # Query by session_id prefix + literal bracket grep (invalid unclosed regex fallback)
        q_pref = C.query_archive(self.local_a, "pref-proj", session_id="abcdef12")
        self.assertEqual(len(q_pref["sessions"]), 1)
        self.assertIn("第二步补充", q_pref["markdown"])

        q_bracket = C.query_archive(self.local_a, "pref-proj", grep="[调用工具 Read")
        self.assertEqual(len(q_bracket["matches"]), 1)

    @unittest.skipUnless(sys.platform == "darwin" and os.path.islink("/var"), "macOS /var <-> /private/var alias")
    def test_macos_var_and_private_var_reverse_alias(self):
        raw_td = self._td.name  # Often /var/folders/... under default macOS TMPDIR
        real_td = os.path.realpath(raw_td)
        if real_td.startswith("/private/var/"):
            var_path = "/var/" + real_td[len("/private/var/"):] + "/p1"
            priv_path = real_td + "/p1"
        elif real_td.startswith("/private/tmp/"):
            var_path = "/tmp/" + real_td[len("/private/tmp/"):] + "/p1"
            priv_path = real_td + "/p1"
        else:
            self.skipTest("TMPDIR not under /private/var or /private/tmp")
        os.makedirs(priv_path, exist_ok=True)
        # Transcript encoded under /var/... while project registers /private/var/...
        self.put_claude(self.uhome_a, var_path, "var-alias-s1", [
            {"type": "user", "content": "验证 /var 与 /private/var 互通"},
        ])
        proj = {
            "id": "mac-var-proj",
            "name": "Mac Var",
            "device": "a",
            "paths": {"a": priv_path, "b": ""},
            "sessions": [],
        }
        bundle = C.collect_project_conversations(proj, "a", self.local_a, user_home=self.uhome_a)
        self.assertEqual([e["sessionId"] for e in bundle["entries"]], ["var-alias-s1"])


if __name__ == "__main__":
    unittest.main()
