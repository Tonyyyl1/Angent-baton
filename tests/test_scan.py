import json
import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydesk import scan  # noqa: E402

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)


def commit(repo, msg="init", name="a.txt", content="x"):
    Path(repo, name).write_text(content, encoding="utf-8")
    run(["git", "add", "-A"], repo)
    run(GIT + ["commit", "-q", "-m", msg], repo)


def init_repo(path):
    os.makedirs(path, exist_ok=True)
    run(["git", "init", "-q", "-b", "main"], path)
    return path


def write(p, text=""):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(text, encoding="utf-8")


class Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = os.path.realpath(self._td.name)


class EncPath(unittest.TestCase):
    def test_windows(self):
        self.assertEqual(scan.enc_path(r"D:\developer\Scala cw"), "D--developer-Scala-cw")

    def test_posix(self):
        self.assertEqual(scan.enc_path("/Users/me/my.proj_1"), "-Users-me-my-proj-1")

    def test_non_ascii(self):
        self.assertEqual(scan.enc_path("C:\\项目\\ab"), "C-----ab")


class GitFacts(Tmp):
    def test_missing_dir(self):
        f = scan.git_facts(os.path.join(self.tmp, "nope"))
        self.assertFalse(f["isGit"])
        self.assertEqual((f["branch"], f["dirty"], f["unpushed"], f["remote"], f["head"]), ("", 0, None, "", ""))

    def test_not_repo(self):
        d = os.path.join(self.tmp, "plain")
        os.makedirs(d)
        self.assertFalse(scan.git_facts(d)["isGit"])

    def test_empty_repo(self):
        d = init_repo(os.path.join(self.tmp, "e"))
        f = scan.git_facts(d)
        self.assertTrue(f["isGit"])
        self.assertEqual(f["branch"], "main")
        self.assertEqual(f["head"], "")
        self.assertEqual(f["lastCommit"], "")
        self.assertIsNone(f["unpushed"])

    def test_clean_repo(self):
        d = init_repo(os.path.join(self.tmp, "c"))
        commit(d, "first commit")
        f = scan.git_facts(d)
        self.assertEqual(f["branch"], "main")
        self.assertEqual(f["dirty"], 0)
        self.assertEqual(f["remote"], "")
        self.assertEqual(f["remotes"], [])
        self.assertIsNone(f["unpushed"])
        self.assertEqual(len(f["worktrees"]), 1)
        self.assertTrue(f["head"])
        self.assertRegex(f["lastCommit"], r"^%s（\d{4}-\d{2}-\d{2}）：first commit$" % f["head"])

    def test_dirty_repo(self):
        d = init_repo(os.path.join(self.tmp, "d"))
        commit(d)
        write(os.path.join(d, "a.txt"), "changed")
        write(os.path.join(d, "new1.txt"), "n")
        write(os.path.join(d, "dir", "new2.txt"), "n")
        self.assertEqual(scan.git_facts(d)["dirty"], 3)

    def test_remote_and_unpushed(self):
        bare = os.path.join(self.tmp, "bare.git")
        os.makedirs(bare)
        run(["git", "init", "-q", "--bare", "-b", "main"], bare)
        d = init_repo(os.path.join(self.tmp, "r"))
        commit(d)
        run(["git", "remote", "add", "upstream", bare], d)
        self.assertEqual(scan.git_facts(d)["remote"], "upstream")
        run(["git", "remote", "add", "origin", bare], d)
        run(["git", "push", "-q", "-u", "origin", "main"], d)
        f = scan.git_facts(d)
        self.assertEqual(f["remote"], "origin")
        self.assertEqual(sorted(f["remotes"]), ["origin", "upstream"])
        self.assertEqual(f["unpushed"], 0)
        commit(d, "two", "b.txt")
        commit(d, "three", "c.txt")
        self.assertEqual(scan.git_facts(d)["unpushed"], 2)

    def test_detached_head(self):
        d = init_repo(os.path.join(self.tmp, "dh"))
        commit(d)
        run(["git", "checkout", "-q", "--detach"], d)
        f = scan.git_facts(d)
        self.assertTrue(f["isGit"])
        self.assertEqual(f["branch"], "")
        self.assertTrue(f["head"])

    def test_extra_worktree(self):
        d = init_repo(os.path.join(self.tmp, "w"))
        commit(d)
        wt = os.path.join(self.tmp, "w2")
        run(["git", "worktree", "add", "-q", "-b", "side", wt], d)
        f = scan.git_facts(d)
        self.assertEqual(len(f["worktrees"]), 2)
        self.assertTrue(scan.presence(d, home=self.tmp)["worktree"])


class Presence(Tmp):
    def test_empty_all_false(self):
        p = os.path.join(self.tmp, "proj")
        os.makedirs(p)
        res = scan.presence(p, home=os.path.join(self.tmp, "home"))
        self.assertEqual(set(res), {"claudeMd", "claudeLocal", "ccSkills", "ccSettings", "ccSettingsLocal",
                                    "ccCmdAgents", "ccMemory", "mcpJson", "agentsMd", "cxProject",
                                    "geminiMd", "agProject", "handoffMd", "worktree", "envFile"})
        self.assertFalse(any(res.values()))

    def test_each_key(self):
        home = os.path.join(self.tmp, "home")
        p = os.path.join(self.tmp, "proj")
        os.makedirs(p)
        cases = {
            "claudeMd": ["CLAUDE.md"], "claudeLocal": ["CLAUDE.local.md"],
            "ccSkills": [".claude/skills/x/SKILL.md"], "ccSettings": [".claude/settings.json"],
            "ccSettingsLocal": [".claude/settings.local.json"], "ccCmdAgents": [".claude/commands/a.md"],
            "mcpJson": [".mcp.json"], "agentsMd": ["AGENTS.md"], "cxProject": [".codex/config.toml"],
            "geminiMd": ["GEMINI.md"], "agProject": [".agents/skills/s/SKILL.md"],
            "handoffMd": ["HANDOFF.md"], "envFile": [".env"],
        }
        for key, files in cases.items():
            q = os.path.join(self.tmp, "p_" + key)
            os.makedirs(q)
            for f in files:
                write(os.path.join(q, *f.split("/")), "x")
            res = scan.presence(q, home=home)
            self.assertTrue(res[key], key)
            self.assertEqual([k for k, v in res.items() if v], [key], key)

    def test_agents_dir_and_memory(self):
        home = os.path.join(self.tmp, "home")
        p = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(p, ".claude", "agents"))
        self.assertTrue(scan.presence(p, home=home)["ccCmdAgents"])
        mem = os.path.join(home, ".claude", "projects", scan.enc_path(p), "memory")
        os.makedirs(mem)
        self.assertFalse(scan.presence(p, home=home)["ccMemory"])
        write(os.path.join(mem, "MEMORY.md"), "x")
        self.assertTrue(scan.presence(p, home=home)["ccMemory"])

    def test_empty_skills_and_ag_dirs_are_false(self):
        p = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(p, ".claude", "skills"))
        os.makedirs(os.path.join(p, ".agents"))
        res = scan.presence(p, home=self.tmp)
        self.assertFalse(res["ccSkills"])
        self.assertFalse(res["agProject"])
        write(os.path.join(p, ".agent", "rules", "r.md"), "x")
        self.assertTrue(scan.presence(p, home=self.tmp)["agProject"])


class GlobalPresence(Tmp):
    KEYS = {"acctSkills", "userSkills", "userClaudeMd", "userSettings", "ccPlugins", "ccProjects", "ccCreds",
            "cxAgents", "cxConfig", "cxSkills", "cxSessions", "cxAuth", "cxState",
            "agRules", "agSkills", "agPlugins", "agMcp", "agSettings", "agBrain", "agState", "agAuth"}

    def test_empty(self):
        res = scan.global_presence(home=self.tmp)
        self.assertEqual(set(res), self.KEYS)
        self.assertFalse(any(res.values()))

    def test_each_key(self):
        h = self.tmp
        cases = {
            "acctSkills": [".claude/skills/synced/a/SKILL.md"],
            "userSkills": [".claude/skills/mine/SKILL.md"],
            "userClaudeMd": [".claude/CLAUDE.md"], "userSettings": [".claude/settings.json"],
            "ccPlugins": [".claude/plugins/x.txt"], "ccProjects": [".claude/projects/x/a.jsonl"],
            "ccCreds": [".claude.json"], "cxAgents": [".codex/AGENTS.md"], "cxConfig": [".codex/config.toml"],
            "cxSkills": [".codex/skills/mine/SKILL.md"], "cxSessions": [".codex/sessions/2026/01/01/r.jsonl"],
            "cxAuth": [".codex/auth.json"], "cxState": [".codex/state_5.sqlite"],
            "agRules": [".gemini/GEMINI.md"], "agSkills": [".gemini/config/skills/mine/SKILL.md"],
            "agPlugins": [".gemini/config/plugins/p1/plugin.json"], "agMcp": [".gemini/config/mcp_config.json"],
            "agSettings": [".gemini/config/config.json"], "agBrain": [".gemini/antigravity/brain/u1/plan.md"],
            "agState": [".gemini/antigravity/conversation_summaries.db"], "agAuth": [".gemini/oauth_creds.json"],
        }
        for key, files in cases.items():
            home = os.path.join(h, "h_" + key)
            for f in files:
                write(os.path.join(home, *f.split("/")), "x")
            res = scan.global_presence(home=home)
            self.assertTrue(res[key], key)

    def test_synced_and_system_excluded(self):
        write(os.path.join(self.tmp, ".claude", "skills", "synced", "a", "SKILL.md"), "x")
        write(os.path.join(self.tmp, ".codex", "skills", ".system", "s", "SKILL.md"), "x")
        res = scan.global_presence(home=self.tmp)
        self.assertTrue(res["acctSkills"])
        self.assertFalse(res["userSkills"])
        self.assertFalse(res["cxSkills"])
        write(os.path.join(self.tmp, ".claude", "skills", "mine", "SKILL.md"), "x")
        write(os.path.join(self.tmp, ".codex", "skills", "mine", "SKILL.md"), "x")
        res = scan.global_presence(home=self.tmp)
        self.assertTrue(res["userSkills"])
        self.assertTrue(res["cxSkills"])


class ClaudeSessions(Tmp):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        self.cwd = os.path.join(self.tmp, "my proj")
        self.dir = os.path.join(self.home, ".claude", "projects", scan.enc_path(self.cwd))

    def put(self, name, lines, mtime=None):
        p = os.path.join(self.dir, name + ".jsonl")
        os.makedirs(self.dir, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write((ln if isinstance(ln, str) else json.dumps(ln, ensure_ascii=False)) + "\n")
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def test_no_dir(self):
        self.assertEqual(scan.claude_sessions([self.cwd], home=self.home), [])

    def test_title_priority_custom_beats_ai_anywhere(self):
        self.put("s1", [
            {"type": "custom-title", "customTitle": "手动标题"},
            {"type": "ai-title", "aiTitle": "AI later"},
            {"type": "summary", "summary": "sum later"},
        ])
        r = scan.claude_sessions([self.cwd], home=self.home)
        self.assertEqual(r[0]["title"], "手动标题")
        self.assertEqual(r[0]["sessionId"], "s1")
        self.assertEqual(r[0]["tool"], "claude")
        self.assertEqual(r[0]["cwd"], self.cwd)
        self.assertRegex(r[0]["lastUsed"], r"^\d{4}-\d{2}-\d{2}$")

    def test_last_occurrence_wins(self):
        self.put("s1", [{"aiTitle": "old"}, {"user": "x"}, {"aiTitle": "new"}, {"summary": "s"}])
        self.assertEqual(scan.claude_sessions([self.cwd], home=self.home)[0]["title"], "new")

    def test_ai_beats_summary_and_summary_fallback(self):
        self.put("a", [{"summary": "S"}, {"aiTitle": "A"}])
        self.put("b", [{"summary": "S1"}, {"summary": "S2"}])
        self.put("c", [{"nothing": 1}])
        r = {s["sessionId"]: s["title"] for s in scan.claude_sessions([self.cwd], home=self.home)}
        self.assertEqual(r, {"a": "A", "b": "S2", "c": ""})

    def test_marker_inside_message_text_ignored(self):
        self.put("m", [{"type": "user", "message": {"content": 'talking about "customTitle" here'}},
                       '{"broken json "customTitle": ',
                       {"aiTitle": "real"}])
        self.assertEqual(scan.claude_sessions([self.cwd], home=self.home)[0]["title"], "real")

    def test_streams_large_file(self):
        p = self.put("big", [{"customTitle": "first"}])
        filler = json.dumps({"type": "assistant", "text": "z" * 4000}) + "\n"
        with open(p, "a", encoding="utf-8") as f:
            for _ in range(5000):  # ~20 MB
                f.write(filler)
            f.write(json.dumps({"customTitle": "last"}) + "\n")
        self.assertGreater(os.path.getsize(p), 15 * 1024 * 1024)
        self.assertEqual(scan.claude_sessions([self.cwd], home=self.home)[0]["title"], "last")

    def test_sort_newest_first_and_ignores_non_jsonl(self):
        self.put("old", [{"aiTitle": "o"}], mtime=1_700_000_000)
        self.put("new", [{"aiTitle": "n"}], mtime=1_800_000_000)
        write(os.path.join(self.dir, "notes.txt"), "x")
        os.makedirs(os.path.join(self.dir, "memory"))
        r = scan.claude_sessions([self.cwd], home=self.home)
        self.assertEqual([s["sessionId"] for s in r], ["new", "old"])
        self.assertEqual(r[0]["mtime"], 1_800_000_000)

    def test_multiple_cwds_merged_and_sorted(self):
        alt = os.path.join(self.tmp, "alt")
        self.put("p1", [{"aiTitle": "p"}], mtime=1_700_000_000)
        d2 = os.path.join(self.home, ".claude", "projects", scan.enc_path(alt))
        os.makedirs(d2)
        write(os.path.join(d2, "p2.jsonl"), json.dumps({"aiTitle": "q"}))
        os.utime(os.path.join(d2, "p2.jsonl"), (1_800_000_000, 1_800_000_000))
        r = scan.claude_sessions([self.cwd, alt], home=self.home)
        self.assertEqual([(s["sessionId"], s["cwd"]) for s in r], [("p2", alt), ("p1", self.cwd)])


class CodexSessions(Tmp):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        self.root = os.path.join(self.home, ".codex", "sessions", "2026", "11", "05")

    def rollout(self, uuid, cwd, mtime=None, nested=True, day="05"):
        d = os.path.join(self.home, ".codex", "sessions", "2026", "11", day)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "rollout-2026-11-%sT10-00-00-%s.jsonl" % (day, uuid))
        meta = {"type": "session_meta", "payload": {"id": uuid, "cwd": cwd, "pad": "p" * 3000}}
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta) + "\n")
            f.write(json.dumps({"type": "event", "payload": {"cwd": r"E:\other"}}) + "\n")
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def index(self, rows):
        write(os.path.join(self.home, ".codex", "session_index.jsonl"),
              "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")

    U1 = "01aaaaaa-0000-4000-8000-000000000001"
    U2 = "01aaaaaa-0000-4000-8000-000000000002"
    U3 = "01aaaaaa-0000-4000-8000-000000000003"
    U4 = "01aaaaaa-0000-4000-8000-000000000004"

    def test_empty(self):
        self.assertEqual(scan.codex_sessions([r"D:\x"], home=self.home), [])
        self.assertEqual(scan.codex_sessions([], home=self.home), [])

    @unittest.skipUnless(os.name == "nt", "Windows path semantics")
    def test_windows_matching(self):
        base = r"D:\Work\Proj"
        self.rollout(self.U1, r"D:\Work\Proj", mtime=1_700_000_001)
        self.rollout(self.U2, r"D:\Work\Proj\sub\deeper", mtime=1_700_000_002)
        self.rollout(self.U3, r"D:\Work\ProjOther", mtime=1_700_000_003)
        self.rollout(self.U4, r"d:/work/proj", mtime=1_700_000_004)
        ids = {s["sessionId"] for s in scan.codex_sessions([base], home=self.home)}
        self.assertEqual(ids, {self.U1, self.U2, self.U4})

    def test_posix_style_matching(self):
        base = "/home/u/proj"
        self.rollout(self.U1, "/home/u/proj")
        self.rollout(self.U2, "/home/u/proj/sub")
        self.rollout(self.U3, "/home/u/proj-two")
        ids = {s["sessionId"] for s in scan.codex_sessions([base], home=self.home)}
        self.assertEqual(ids, {self.U1, self.U2})

    def test_trailing_separator_in_base(self):
        self.rollout(self.U1, "/home/u/proj/x")
        self.assertEqual(len(scan.codex_sessions(["/home/u/proj/"], home=self.home)), 1)

    def test_titles_last_line_wins_and_missing(self):
        self.rollout(self.U1, "/p")
        self.rollout(self.U2, "/p")
        self.index([
            {"id": self.U1, "thread_name": "old name", "updated_at": "2026-11-01T00:00:00Z"},
            {"id": "other", "thread_name": "zzz", "updated_at": "2026-11-01T00:00:00Z"},
            {"id": self.U1, "thread_name": "克隆文件夹", "updated_at": "2026-11-02T00:00:00Z"},
        ])
        r = {s["sessionId"]: s for s in scan.codex_sessions(["/p"], home=self.home)}
        self.assertEqual(r[self.U1]["title"], "克隆文件夹")
        self.assertEqual(r[self.U2]["title"], "")
        self.assertEqual(r[self.U1]["tool"], "codex")
        self.assertEqual(r[self.U1]["cwd"], "/p")
        self.assertRegex(r[self.U1]["lastUsed"], r"^\d{4}-\d{2}-\d{2}$")

    def test_sort_newest_first_across_days(self):
        self.rollout(self.U1, "/p", mtime=1_700_000_000, day="01")
        self.rollout(self.U2, "/p", mtime=1_800_000_000, day="02")
        self.rollout(self.U3, "/p", mtime=1_750_000_000, day="03")
        r = scan.codex_sessions(["/p"], home=self.home)
        self.assertEqual([s["sessionId"] for s in r], [self.U2, self.U3, self.U1])

    def test_ignores_non_rollout_and_missing_cwd(self):
        d = os.path.join(self.home, ".codex", "sessions", "2026", "11", "05")
        os.makedirs(d)
        write(os.path.join(d, "notes.jsonl"), json.dumps({"payload": {"cwd": "/p"}}))
        write(os.path.join(d, "rollout-x-%s.jsonl" % self.U1), json.dumps({"payload": {"id": "x"}}))
        self.assertEqual(scan.codex_sessions(["/p"], home=self.home), [])

    def test_oversized_first_line_falls_back_to_regex(self):
        d = os.path.join(self.home, ".codex", "sessions", "2026", "11", "05")
        os.makedirs(d)
        line = '{"type":"session_meta","payload":{"cwd":"/p/big","x":"' + "y" * 400000 + '"}}\n'
        write(os.path.join(d, "rollout-t-%s.jsonl" % self.U1), line)
        r = scan.codex_sessions(["/p"], home=self.home)
        self.assertEqual([s["cwd"] for s in r], ["/p/big"])


class AntiSessions(Tmp):
    A1 = "7d5cf4ca-1a27-4cd1-8e71-66f532b39ace"
    A2 = "11111111-2222-4333-8444-555555555555"
    A3 = "99999999-8888-4777-8666-555555555555"

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")

    def put_brain(self, sid, lines, cli=False, full_only=False, mtime=None):
        sub = "antigravity-cli" if cli else "antigravity"
        fname = "transcript_full.jsonl" if full_only else "transcript.jsonl"
        p = os.path.join(self.home, ".gemini", sub, "brain", sid, ".system_generated", "logs", fname)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write((ln if isinstance(ln, str) else json.dumps(ln, ensure_ascii=False)) + "\n")
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def test_empty(self):
        self.assertEqual(scan.anti_sessions([r"D:\x"], home=self.home), [])
        self.assertEqual(scan.anti_sessions([], home=self.home), [])

    def test_at_mention_and_user_request_title_windows(self):
        self.put_brain(self.A1, [
            {
                "step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                "content": "<USER_REQUEST>\n@[d:\\developer\\relay-desk] 你来负责把anti的接力部分也加上去\n</USER_REQUEST>\n"
                           "<ADDITIONAL_METADATA>\n@[d:\\developer\\relay-desk] is a [Directory]:\nd:\\developer\\relay-desk\n</ADDITIONAL_METADATA>",
            }
        ], mtime=1_800_000_000)
        r = scan.anti_sessions([r"D:\developer\relay-desk"], home=self.home)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["tool"], "anti")
        self.assertEqual(r[0]["sessionId"], self.A1)
        self.assertEqual(r[0]["title"], "你来负责把anti的接力部分也加上去")
        self.assertEqual(r[0]["cwd"], r"D:\developer\relay-desk")
        self.assertRegex(r[0]["lastUsed"], r"^\d{4}-\d{2}-\d{2}$")

    def test_posix_mac_workspace_uri_and_active_workspace(self):
        base = "/Users/dev/Developer/demo-app"
        self.put_brain(self.A1, [
            {"type": "USER_INPUT", "metadata": {"workspaceUri": "file:///Users/dev/Developer/demo-app"},
             "content": "<USER_REQUEST>修复 cohort 导出</USER_REQUEST>"},
        ], mtime=1_700_000_010)
        self.put_brain(self.A2, [
            {"type": "USER_INPUT",
             "content": "<USER_REQUEST>检查测试</USER_REQUEST>\n<ADDITIONAL_METADATA>\nActive Workspace: /Users/dev/Developer/demo-app/sub\n</ADDITIONAL_METADATA>"},
        ], mtime=1_700_000_020)
        self.put_brain(self.A3, [
            {"type": "USER_INPUT", "metadata": {"workspaceUri": "file:///Users/dev/Developer/demo-app-other"},
             "content": "<USER_REQUEST>别的项目</USER_REQUEST>"},
        ], mtime=1_700_000_030)
        r = scan.anti_sessions([base], home=self.home)
        self.assertEqual([s["sessionId"] for s in r], [self.A2, self.A1])
        self.assertEqual(r[0]["cwd"], base)
        self.assertEqual(r[1]["cwd"], base)

    def test_tool_calls_double_encoded_and_generic_ignored(self):
        base = "/Users/dev/proj"
        # GENERIC tool output mentioning the path must NOT match on its own
        self.put_brain(self.A1, [
            {"type": "USER_INPUT", "content": "<USER_REQUEST>随便看看</USER_REQUEST>"},
            {"type": "GENERIC", "content": "listing /Users/dev/proj/README.md"},
        ], mtime=1_700_000_001)
        # PLANNER_RESPONSE reading a file inside base matches and sets cwd to base (not file path)
        self.put_brain(self.A2, [
            {"type": "USER_INPUT", "content": "<USER_REQUEST>阅读说明文档</USER_REQUEST>"},
            {"type": "PLANNER_RESPONSE", "tool_calls": [
                {"name": "read_file", "args": {"Path": json.dumps("/Users/dev/proj/README.md")}},
            ]},
        ], mtime=1_700_000_002)
        r = scan.anti_sessions([base], home=self.home)
        self.assertEqual([s["sessionId"] for s in r], [self.A2])
        self.assertEqual(r[0]["cwd"], base)
        self.assertEqual(r[0]["title"], "阅读说明文档")

    def test_cli_brain_dedup_full_fallback_and_title_override(self):
        self.put_brain(self.A1, [
            {"type": "USER_INPUT", "cwd": "/p", "content": "<USER_REQUEST>旧标题</USER_REQUEST>"},
        ], cli=False, mtime=1_700_000_000)
        # Newer copy in antigravity-cli with customTitle wins
        self.put_brain(self.A1, [
            {"type": "USER_INPUT", "cwd": "/p", "content": "<USER_REQUEST>旧标题</USER_REQUEST>"},
            {"customTitle": "手动重命名会话"},
        ], cli=True, full_only=True, mtime=1_800_000_000)
        r = scan.anti_sessions(["/p"], home=self.home)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["title"], "手动重命名会话")
        self.assertEqual(r[0]["mtime"], 1_800_000_000)


class CrossPlatformAndSymlinkScan(Tmp):
    U1 = "01bbbbbb-0000-4000-8000-000000000001"
    U2 = "01bbbbbb-0000-4000-8000-000000000002"
    U3 = "01bbbbbb-0000-4000-8000-000000000003"
    A1 = "22222222-3333-4444-8555-666666666666"
    A2 = "33333333-4444-4555-8666-777777777777"

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")

    @unittest.skipIf(os.name == "nt", "POSIX symlinks and /var <-> /private/var")
    def test_claude_codex_anti_and_presence_across_symlinks_and_var_aliases(self):
        real_proj = os.path.join(self.tmp, "real_workspace", "双机项目")
        os.makedirs(real_proj, exist_ok=True)
        link_proj = os.path.join(self.tmp, "link_workspace")
        os.symlink(real_proj, link_proj)

        # 1. Claude session & memory stored under realpath, queried via symlink (and deduplicated)
        real_cc_dir = os.path.join(self.home, ".claude", "projects", scan.enc_path(real_proj))
        link_cc_dir = os.path.join(self.home, ".claude", "projects", scan.enc_path(link_proj))
        write(os.path.join(real_cc_dir, "s_sym.jsonl"), json.dumps({"customTitle": "新标题"}) + "\n")
        os.utime(os.path.join(real_cc_dir, "s_sym.jsonl"), (1_800_000_010, 1_800_000_010))
        write(os.path.join(link_cc_dir, "s_sym.jsonl"), json.dumps({"customTitle": "旧标题"}) + "\n")
        os.utime(os.path.join(link_cc_dir, "s_sym.jsonl"), (1_700_000_010, 1_700_000_010))
        write(os.path.join(real_cc_dir, "memory", "MEMORY.md"), "remember")

        cc = scan.claude_sessions([link_proj], home=self.home)
        self.assertEqual(len(cc), 1)
        self.assertEqual(cc[0]["sessionId"], "s_sym")
        self.assertEqual(cc[0]["title"], "新标题")
        self.assertEqual(cc[0]["cwd"], link_proj)
        self.assertTrue(scan.presence(link_proj, home=self.home)["ccMemory"])

        # Also test /var <-> /private/var and /tmp <-> /private/tmp reverse lookup for Claude
        if os.path.realpath("/tmp") == "/private/tmp":
            var_style = "/tmp/fake_claude_alias_proj"
            priv_style = "/private/tmp/fake_claude_alias_proj"
            write(os.path.join(self.home, ".claude", "projects", scan.enc_path(var_style), "s_tmp.jsonl"),
                  json.dumps({"aiTitle": "tmp别名"}) + "\n")
            found_from_priv = scan.claude_sessions([priv_style], home=self.home)
            self.assertEqual([s["sessionId"] for s in found_from_priv], ["s_tmp"])

        # 2. Codex session recorded under realpath, queried via symlink
        roll = os.path.join(self.home, ".codex", "sessions", "2026", "11", "08",
                            "rollout-2026-11-08T10-00-00-%s.jsonl" % self.U1)
        write(roll, json.dumps({"type": "session_meta", "payload": {"id": self.U1, "cwd": os.path.join(real_proj, "sub")}}) + "\n")
        cx = scan.codex_sessions([link_proj], home=self.home)
        self.assertEqual([s["sessionId"] for s in cx], [self.U1])

        # 3. Antigravity session recorded under realpath, queried via symlink; relative @[README.md] ignored
        brain1 = os.path.join(self.home, ".gemini", "antigravity", "brain", self.A1, ".system_generated", "logs", "transcript.jsonl")
        write(brain1, json.dumps({
            "type": "USER_INPUT",
            "metadata": {"workspacePath": real_proj},
            "content": "<USER_REQUEST>修复软链接目录识别</USER_REQUEST>",
        }, ensure_ascii=False) + "\n")
        brain2 = os.path.join(self.home, ".gemini", "antigravity", "brain", self.A2, ".system_generated", "logs", "transcript.jsonl")
        write(brain2, json.dumps({
            "type": "USER_INPUT",
            "content": "<USER_REQUEST>@[README.md] 相对引用不应误判为当前目录</USER_REQUEST>",
        }, ensure_ascii=False) + "\n")
        ag = scan.anti_sessions([link_proj, os.getcwd()], home=self.home)
        self.assertEqual([s["sessionId"] for s in ag], [self.A1])
        self.assertEqual(ag[0]["cwd"], real_proj)

    def test_chinese_nfc_nfd_across_claude_codex_and_anti(self):
        nfc_dir = os.path.join(self.tmp, unicodedata.normalize("NFC", "测试工作区/café_项目"))
        nfd_dir = os.path.join(self.tmp, unicodedata.normalize("NFD", "测试工作区/café_项目"))
        self.assertNotEqual(nfc_dir, nfd_dir)

        # Claude stored under NFC, queried via NFD
        write(os.path.join(self.home, ".claude", "projects", scan.enc_path(nfc_dir), "s_zh.jsonl"),
              json.dumps({"customTitle": "中文NFC会话"}, ensure_ascii=False) + "\n")
        cc = scan.claude_sessions([nfd_dir], home=self.home)
        self.assertEqual([s["sessionId"] for s in cc], ["s_zh"])
        self.assertEqual(cc[0]["title"], "中文NFC会话")

        # Codex stored under NFD, queried via NFC
        roll = os.path.join(self.home, ".codex", "sessions", "2026", "11", "09",
                            "rollout-2026-11-09T10-00-00-%s.jsonl" % self.U2)
        write(roll, json.dumps({"type": "session_meta", "payload": {"id": self.U2, "cwd": nfd_dir}}, ensure_ascii=False) + "\n")
        cx = scan.codex_sessions([nfc_dir], home=self.home)
        self.assertEqual([s["sessionId"] for s in cx], [self.U2])

        # Antigravity stored under NFD, queried via NFC
        brain = os.path.join(self.home, ".gemini", "antigravity", "brain", self.A1, ".system_generated", "logs", "transcript.jsonl")
        write(brain, json.dumps({
            "type": "USER_INPUT",
            "cwd": nfd_dir,
            "content": "<USER_REQUEST>中文NFD路径会话</USER_REQUEST>",
        }, ensure_ascii=False) + "\n")
        ag = scan.anti_sessions([nfc_dir], home=self.home)
        self.assertEqual([s["sessionId"] for s in ag], [self.A1])

    def test_windows_paths_on_posix_in_codex_and_within(self):
        self.assertTrue(scan._within(r"d:/work/proj/sub", r"D:\Work\Proj"))
        self.assertFalse(scan._within(r"D:\Work\ProjOther", r"D:\Work\Proj"))
        r1 = os.path.join(self.home, ".codex", "sessions", "2026", "11", "10",
                          "rollout-2026-11-10T10-00-00-%s.jsonl" % self.U1)
        r2 = os.path.join(self.home, ".codex", "sessions", "2026", "11", "10",
                          "rollout-2026-11-10T10-00-00-%s.jsonl" % self.U2)
        r3 = os.path.join(self.home, ".codex", "sessions", "2026", "11", "10",
                          "rollout-2026-11-10T10-00-00-%s.jsonl" % self.U3)
        write(r1, json.dumps({"type": "session_meta", "payload": {"id": self.U1, "cwd": r"d:/work/proj/sub"}}) + "\n")
        write(r2, json.dumps({"type": "session_meta", "payload": {"id": self.U2, "cwd": r"D:\Work\Proj"}}) + "\n")
        write(r3, json.dumps({"type": "session_meta", "payload": {"id": self.U3, "cwd": r"D:\Work\ProjOther"}}) + "\n")
        ids = {s["sessionId"] for s in scan.codex_sessions([r"D:\Work\Proj"], home=self.home)}
        self.assertEqual(ids, {self.U1, self.U2})


if __name__ == "__main__":
    unittest.main()
