import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from relaydesk import cli
from relaydesk import ledger as L


class FakeScan(types.ModuleType):
    def __init__(self):
        types.ModuleType.__init__(self, "relaydesk.scan")
        self.facts = {"isGit": True, "branch": "main", "dirty": 2, "unpushed": None, "remote": "origin",
                      "remotes": ["origin"], "worktrees": ["w"], "head": "abc1234",
                      "lastCommit": "abc1234（2026-11-17）：first"}
        self.pres = {"claudeMd": True, "agentsMd": False, "geminiMd": True}
        self.gpres = {"cxAgents": True, "ccCreds": False, "agSkills": True, "agAuth": False}
        self.claude = []
        self.codex = []
        self.anti = []
        self.calls = []

    def git_facts(self, path):
        self.calls.append(("git_facts", path))
        return dict(self.facts)

    def presence(self, path, home=None):
        return dict(self.pres)

    def global_presence(self, home=None):
        return dict(self.gpres)

    def claude_sessions(self, cwds, home=None):
        self.calls.append(("claude_sessions", list(cwds)))
        return [dict(s) for s in self.claude]

    def codex_sessions(self, cwds, home=None):
        return [dict(s) for s in self.codex]

    def anti_sessions(self, cwds, home=None):
        self.calls.append(("anti_sessions", list(cwds)))
        return [dict(s) for s in self.anti]


class FakePlan(types.ModuleType):
    def __init__(self):
        types.ModuleType.__init__(self, "relaydesk.plan")

    def tier_board(self, project, config):
        return {"native": [["原生项", "说明一", False]], "git": [["Git项", "说明二", False]], "extra": [], "manual": []}

    def resume_lines(self, project, session):
        return ["claude --resume %s" % session.get("sessionId"), {"n": "只能在原设备上恢复"}]

    def new_handoff(self, project, config, transport=None, today=None):
        to = L.other(config, project["device"])["id"]
        return {"tag": "20261117-win", "from": project["device"], "to": to, "transport": transport or "bundle",
                "state": "prep", "pre": {}, "post": {}, "hadDirty": project["dirty"] > 0,
                "branch": project["branch"], "baseCommit": "", "createdAt": "2026-11-17T00:00:00.000Z"}

    def plan_for(self, project, config):
        return {"preChecks": [["p1", "出发一", ""], ["p2", "出发二", ""]], "postChecks": [["q1", "到达一", ""]]}

    def render(self, plan, phase, checked=None):
        checked = checked or {}
        rows = plan["preChecks"] if phase == "pre" else plan["postChecks"]
        return "\n".join("[%s] %s  %s" % ("x" if k in checked else " ", k, t) for k, t, _ in rows)


class CliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.ledger_path = self.dir / "ledger.json"
        self.proj = self.dir / "proj"
        self.proj.mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("RELAY_DESK_") and k not in ("CODEX_HOME", "GEMINI_CONFIG_DIR")}
        env.update({"RELAY_DESK_HOME": str(self.dir / "home"), "RELAY_DESK_LEDGER": str(self.ledger_path),
                    "RELAY_DESK_DEVICE": "a", "RELAY_DESK_USER_HOME": str(self.dir / "uhome"),
                    "CODEX_HOME": str(self.dir / "codexhome"),
                    "GEMINI_CONFIG_DIR": str(self.dir / "geminiconfig")})
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.scan = FakeScan()
        self.plan = FakePlan()
        m = mock.patch.dict(sys.modules, {"relaydesk.scan": self.scan, "relaydesk.plan": self.plan})
        m.start()
        self.addCleanup(m.stop)

    def run_cli(self, *argv):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            code = cli.main([str(a) for a in argv])
        return code, o.getvalue(), e.getvalue()

    def ok(self, *argv):
        code, o, e = self.run_cli(*argv)
        self.assertEqual(code, 0, "argv=%r stderr=%r" % (argv, e))
        return o

    def led(self):
        return L.load(self.ledger_path)

    def proj_of(self, pid="animal-sim"):
        return [p for p in self.led()["projects"] if p["id"] == pid][0]

    def add(self, pid="animal-sim", name="Animal Sim"):
        self.ok("add", name, "--path", self.proj, "--id", pid)
        return self.proj_of(pid)

    def update(self, fn):
        L.update(self.ledger_path, fn)


class TestInitAndErrors(CliBase):
    def test_init_writes_local_config_and_ledger(self):
        target = self.dir / "elsewhere" / "led.json"
        with mock.patch.dict(os.environ):
            del os.environ["RELAY_DESK_LEDGER"]
            del os.environ["RELAY_DESK_DEVICE"]
            with mock.patch.object(L, "hostname", return_value="HOST1"):
                out = self.ok("init", "--device", "b", "--ledger", target, "--host")
            cfg = json.loads((self.dir / "home" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["device"], "b")
            self.assertEqual(os.path.abspath(cfg["ledger"]), os.path.abspath(str(target)))
            led = L.load(target)
            self.assertEqual(led["config"]["devices"][1]["host"], "HOST1")
            self.assertEqual(led["config"]["devices"][0]["host"], "")
            self.assertIn("本机设备", out)

    def test_needs_device_message(self):
        with mock.patch.dict(os.environ):
            del os.environ["RELAY_DESK_DEVICE"]
            with mock.patch.object(L, "hostname", return_value="x"), mock.patch.object(L, "local_os", return_value="linux"):
                code, _, err = self.run_cli("add", "X", "--path", self.proj)
        self.assertEqual(code, 1)
        self.assertIn("relay init", err)

    def test_unknown_project_exit_1(self):
        self.add()
        code, out, err = self.run_cli("status", "nope")
        self.assertEqual(code, 1)
        self.assertIn("找不到项目「nope」", err)
        self.assertIn("animal-sim", err)
        self.assertEqual(out, "")
        for argv in (("note", "nope", "--last", "x"), ("handoff", "nope"), ("cancel", "nope")):
            self.assertEqual(self.run_cli(*argv)[0], 1)

    def test_broken_ledger_exit_2(self):
        self.ledger_path.write_text("{oops", encoding="utf-8")
        for argv in (("status",), ("add", "X", "--path", str(self.proj)), ("export",)):
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("JSON", err)

    def test_usage_error_exit_1(self):
        code, _, err = self.run_cli("note")
        self.assertEqual(code, 1)
        self.assertIn("用法错误", err)
        self.assertEqual(self.run_cli("bogus")[0], 1)
        self.assertEqual(self.run_cli("conflict", "add", "p", "--kind", "zzz")[0], 1)

    def test_missing_plan_module_only_hurts_commands_that_need_it(self):
        self.add()
        with mock.patch.dict(sys.modules, {"relaydesk.plan": None}):
            code, _, err = self.run_cli("handoff", "animal-sim")
            self.assertEqual(code, 1)
            self.assertIn("relaydesk.plan", err)
            self.assertEqual(self.run_cli("status")[0], 0)
            self.assertEqual(self.run_cli("note", "animal-sim", "--next", "x")[0], 0)
            # detail view degrades instead of failing
            code, out, err = self.run_cli("status", "animal-sim")
            self.assertEqual(code, 0)
            self.assertIn("relaydesk.plan", err)

    def test_stdout_utf8_when_not_a_tty(self):
        class Fake(io.StringIO):
            def isatty(self):
                return False

            def reconfigure(self, **kw):
                self.kw = kw
        o = Fake()
        with mock.patch.object(sys, "stdout", o):
            cli._fix_streams()
        self.assertEqual(o.kw, {"encoding": "utf-8", "errors": "replace"})


class TestProjectCommands(CliBase):
    def test_add_scans_and_records_facts(self):
        out = self.ok("add", "Animal Sim", "--path", self.proj, "--tool", "both", "--id", "animal-sim")
        self.assertIn("已登记", out)
        p = self.proj_of()
        self.assertEqual(p["name"], "Animal Sim")
        self.assertEqual(p["tool"], "both")
        self.assertEqual(p["device"], "a")
        self.assertEqual(p["paths"]["a"], os.path.abspath(str(self.proj)))
        self.assertEqual(p["paths"]["b"], "")
        self.assertEqual(p["branch"], "main")
        self.assertEqual(p["dirty"], 2)
        self.assertEqual(p["remote"], "origin")
        self.assertIsNone(p["unpushed"])
        self.assertEqual(p["git"]["head"], "abc1234")
        self.assertEqual(p["git"]["scannedOn"], "a")
        self.assertTrue(p["reg"]["claudeMd"]["on"]["a"])
        self.assertTrue(p["reg"]["geminiMd"]["on"]["a"])
        self.assertFalse(p["reg"]["agentsMd"]["on"]["a"])
        self.assertEqual(p["updatedOn"], "a")

    def test_add_and_set_anti_tool_combinations(self):
        self.ok("add", "Anti Proj", "--path", self.proj, "--tool", "anti", "--id", "anti-p")
        self.assertEqual(self.proj_of("anti-p")["tool"], "anti")
        for t in ("claude+anti", "codex+anti", "all"):
            self.ok("set", "anti-p", "--tool", t)
            self.assertEqual(self.proj_of("anti-p")["tool"], t)

    def test_add_duplicate_and_default_id(self):
        self.ok("add", "Animal Sim", "--path", self.proj)
        self.assertEqual(self.led()["projects"][0]["id"], "Animal-Sim")
        code, _, err = self.run_cli("add", "Animal Sim", "--path", self.proj)
        self.assertEqual(code, 1)
        self.assertIn("已经有", err)
        self.ok("add", "中文项目", "--path", self.proj)
        self.assertTrue(self.led()["projects"][1]["id"].startswith("p_"))

    def test_scan_remote_rule_and_changes_output(self):
        self.add()
        self.update(lambda l: l["projects"][0].update(remote="origin https://example.invalid/x.git"))
        self.scan.facts.update(branch="dev", dirty=5, remote="origin")
        out = self.ok("scan", "animal-sim")
        self.assertIn("分支：main → dev", out)
        self.assertIn("未提交：2 → 5", out)
        p = self.proj_of()
        self.assertEqual(p["remote"], "origin https://example.invalid/x.git")  # real remote name kept
        self.update(lambda l: l["projects"][0].update(remote="gone something"))
        self.ok("scan", "animal-sim")
        self.assertEqual(self.proj_of()["remote"], "origin")
        out = self.ok("scan", "animal-sim")
        self.assertIn("没有变化", out)

    def test_scan_non_git(self):
        self.add()
        self.scan.facts = {"isGit": False, "branch": "", "dirty": 0, "unpushed": None, "remote": "",
                           "remotes": [], "worktrees": [], "head": "", "lastCommit": ""}
        self.ok("scan", "animal-sim")
        p = self.proj_of()
        self.assertEqual((p["branch"], p["dirty"], p["remote"]), ("", 0, ""))

    def test_scan_on_other_device_only_updates_presence_and_sessions(self):
        self.add()
        self.update(lambda l: l["projects"][0].update(device="b", branch="keep", dirty=9))
        self.update(lambda l: l["projects"][0]["sessions"].extend([
            {"id": "s_1", "tool": "claude", "device": "a", "sessionId": "sid1", "title": "old", "lastUsed": "2026-01-01"},
            {"id": "s_2", "tool": "anti", "device": "a", "sessionId": "ag1", "title": "old ag", "lastUsed": "2026-01-01"},
        ]))
        self.scan.claude = [{"tool": "claude", "sessionId": "sid1", "title": "new title", "cwd": "x",
                             "lastUsed": "2026-11-16", "mtime": 5.0}]
        self.scan.anti = [{"tool": "anti", "sessionId": "ag1", "title": "new ag", "cwd": "x",
                           "lastUsed": "2026-11-17", "mtime": 8.0}]
        self.scan.pres = {"claudeMd": True}
        self.ok("scan", "animal-sim")
        p = self.proj_of()
        self.assertEqual(p["branch"], "keep")
        self.assertEqual(p["dirty"], 9)
        self.assertEqual([c for c in self.scan.calls if c[0] == "git_facts"][1:], [])  # only the add-time scan
        self.assertEqual(p["sessions"][0]["title"], "new title")
        self.assertEqual(p["sessions"][0]["lastUsed"], "2026-11-16")
        self.assertEqual(p["sessions"][1]["title"], "new ag")
        self.assertEqual(p["sessions"][1]["lastUsed"], "2026-11-17")
        self.assertTrue(p["reg"]["claudeMd"]["on"]["a"])

    def test_scan_all_updates_global_reg(self):
        self.add("p1", "One")
        self.add("p2", "Two")
        out = self.ok("scan", "--all")
        self.assertIn("2 个项目", out)
        reg = self.led()["config"]["reg"]
        self.assertTrue(reg["cxAgents"]["on"]["a"])
        self.assertTrue(reg["agSkills"]["on"]["a"])
        self.assertFalse(reg["ccCreds"]["on"]["a"])
        self.assertFalse(reg["agAuth"]["on"]["a"])

    def test_scan_missing_path_is_reported(self):
        self.add()
        self.update(lambda l: l["projects"][0]["paths"].update(a=str(self.dir / "gone")))
        out = self.ok("scan", "animal-sim")
        self.assertIn("路径不存在", out)

    def test_scan_by_cwd(self):
        self.add()
        cwd = os.getcwd()
        try:
            os.chdir(str(self.proj))
            out = self.ok("scan")
        finally:
            os.chdir(cwd)
        self.assertIn("Animal Sim", out)

    def test_set_and_note(self):
        self.add()
        alt = self.dir / "alt"
        out = self.ok("set", "animal-sim", "--path-other", "/Users/x/animal", "--alt-path", alt, "--alt-path", alt,
                      "--tool", "codex", "--status", "blocked", "--transport", "bundle", "--worktree", "wt", "--device", "b")
        self.assertIn("已更新", out)
        p = self.proj_of()
        self.assertEqual(p["paths"]["b"], "/Users/x/animal")
        self.assertEqual(p["altPaths"]["a"], [os.path.abspath(str(alt))])
        self.assertEqual((p["tool"], p["status"], p["transport"], p["worktree"], p["device"]),
                         ("codex", "blocked", "bundle", "wt", "b"))
        newp = self.dir / "newpath"
        self.ok("set", "animal-sim", "--path-here", newp)
        self.assertEqual(self.proj_of()["paths"]["a"], os.path.abspath(str(newp)))
        self.assertEqual(self.run_cli("set", "animal-sim")[0], 1)  # nothing to change
        self.ok("note", "animal-sim", "--last", "做了A", "--next", "做B", "--note", "小心", "--verify", "partial",
                "--verify-cmd", "pytest", "--verify-note", "两个失败")
        p = self.proj_of()
        self.assertEqual((p["lastTask"], p["nextAction"], p["note"]), ("做了A", "做B", "小心"))
        self.assertEqual(p["verify"], {"state": "partial", "cmd": "pytest", "note": "两个失败"})
        self.assertEqual(self.run_cli("note", "animal-sim")[0], 1)

    def test_status_list_detail_and_json(self):
        self.add()
        self.ok("note", "animal-sim", "--next", "写测试")
        out = self.ok("status")
        self.assertIn("animal-sim", out)
        self.assertIn("main", out)
        self.assertIn("未提交 2", out)
        self.assertIn("下一步：写测试", out)
        self.assertNotIn("交接中", out)
        data = json.loads(self.ok("status", "--json"))
        self.assertEqual(data["projects"][0]["id"], "animal-sim")
        self.update(lambda l: l["projects"][0]["sessions"].append(
            {"id": "s_9", "tool": "claude", "device": "a", "kind": "local", "carry": "summary",
             "sessionId": "sidX", "title": "我的会话", "lastUsed": "2026-11-17", "cwd": ""}))
        out = self.ok("status", "animal-sim")
        self.assertIn("四级分层", out)
        self.assertIn("原生项：说明一", out)
        self.assertIn("claude --resume sidX", out)
        self.assertIn("# 只能在原设备上恢复", out)
        self.assertEqual(json.loads(self.ok("status", "animal-sim", "--json"))["id"], "animal-sim")

    def test_status_empty(self):
        self.assertIn("还没有项目", self.ok("status"))

    def test_conflict_add(self):
        self.add()
        self.ok("conflict", "add", "animal-sim", "--kind", "merge", "--files", "a.py", "--resolution", "手动合并")
        c = self.proj_of()["conflicts"][0]
        self.assertEqual((c["kind"], c["files"], c["resolution"], c["kept"], c["device"]),
                         ("merge", "a.py", "手动合并", "尚未解决", "a"))
        self.assertTrue(c["id"].startswith("c_"))
        self.assertRegex(c["date"], r"^\d{4}-\d\d-\d\d$")

    def test_device_view_and_modify(self):
        out = self.ok("device")
        self.assertIn("a  设备 A", out)
        self.assertIn("<- 本机", out)
        self.ok("device", "--which", "b", "--name", "My Mac", "--os", "linux", "--drop", "/tmp/drop")
        b = self.led()["config"]["devices"][1]
        self.assertEqual((b["name"], b["os"], b["drop"]), ("My Mac", "linux", "/tmp/drop"))
        self.ok("device", "--drop", "D:\\handoff")  # defaults to this device
        self.assertEqual(self.led()["config"]["devices"][0]["drop"], "D:\\handoff")


class TestSessions(CliBase):
    def setUp(self):
        CliBase.setUp(self)
        self.add()
        self.scan.claude = [
            {"tool": "claude", "sessionId": "old1", "title": "旧的", "cwd": str(self.proj), "lastUsed": "2026-11-01", "mtime": 1.0},
            {"tool": "claude", "sessionId": "new1", "title": "最新的", "cwd": str(self.proj), "lastUsed": "2026-11-16", "mtime": 9.0},
        ]
        self.scan.codex = [
            {"tool": "codex", "sessionId": "cx1", "title": "codex 线程", "cwd": str(self.proj), "lastUsed": "2026-11-15", "mtime": 5.0}]
        self.scan.anti = [
            {"tool": "anti", "sessionId": "ag1", "title": "anti 会话", "cwd": str(self.proj), "lastUsed": "2026-11-17", "mtime": 7.0}]

    def test_add_latest_and_explicit(self):
        self.ok("session", "add", "animal-sim", "latest")
        s = self.proj_of()["sessions"][0]
        self.assertEqual((s["sessionId"], s["title"], s["tool"], s["device"], s["kind"], s["carry"], s["lastUsed"]),
                         ("new1", "最新的", "claude", "a", "local", "summary", "2026-11-16"))
        self.assertTrue(s["id"].startswith("s_"))
        self.ok("session", "add", "animal-sim", "cx1", "--tool", "codex", "--carry", "transcript", "--kind", "cloud")
        s2 = self.proj_of()["sessions"][1]
        self.assertEqual((s2["tool"], s2["title"], s2["carry"], s2["kind"]), ("codex", "codex 线程", "transcript", "cloud"))
        self.ok("session", "add", "animal-sim", "latest", "--tool", "codex")
        self.assertEqual(len(self.proj_of()["sessions"]), 2)  # same sessionId updates, not duplicates

    def test_anti_session_add_and_filter(self):
        self.ok("set", "animal-sim", "--tool", "anti")
        self.ok("session", "add", "animal-sim", "latest")
        s = self.proj_of()["sessions"][0]
        self.assertEqual((s["sessionId"], s["title"], s["tool"]), ("ag1", "anti 会话", "anti"))
        data = json.loads(self.ok("sessions", "animal-sim", "--tool", "anti", "--json"))
        self.assertEqual([d["sessionId"] for d in data], ["ag1"])
        self.assertTrue(data[0]["registered"])

    def test_add_updates_existing_and_title_override(self):
        self.ok("session", "add", "animal-sim", "new1")
        sid = self.proj_of()["sessions"][0]["id"]
        self.ok("session", "add", "animal-sim", "new1", "--title", "改名了", "--cwd", "/x")
        ss = self.proj_of()["sessions"]
        self.assertEqual(len(ss), 1)
        self.assertEqual((ss[0]["id"], ss[0]["title"], ss[0]["cwd"]), (sid, "改名了", "/x"))

    def test_add_unknown_id_still_registers(self):
        code, out, err = self.run_cli("session", "add", "animal-sim", "ghost", "--title", "手填")
        self.assertEqual(code, 0)
        self.assertIn("没有发现", err)
        s = self.proj_of()["sessions"][0]
        self.assertEqual((s["sessionId"], s["title"]), ("ghost", "手填"))
        self.assertRegex(s["lastUsed"], r"^\d{4}-\d\d-\d\d$")

    def test_latest_without_sessions_fails(self):
        self.scan.claude = []
        code, _, err = self.run_cli("session", "add", "animal-sim", "latest")
        self.assertEqual(code, 1)
        self.assertIn("latest", err)

    def test_rm(self):
        self.ok("session", "add", "animal-sim", "new1")
        sid = self.proj_of()["sessions"][0]["id"]
        self.ok("session", "rm", "animal-sim", sid)
        self.assertEqual(self.proj_of()["sessions"], [])
        code, _, err = self.run_cli("session", "rm", "animal-sim", "zzz")
        self.assertEqual(code, 1)
        self.assertIn("没有登记过", err)

    def test_sessions_listing(self):
        self.ok("session", "add", "animal-sim", "new1")
        out = self.ok("sessions", "animal-sim")
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("* claude  new1"))  # newest first, registered
        self.assertIn("  anti  ag1", out)
        self.assertIn("  codex  cx1", out)
        self.assertIn("cx1", out)
        self.assertNotIn("* codex", out)
        data = json.loads(self.ok("sessions", "animal-sim", "--tool", "codex", "--json"))
        self.assertEqual([d["sessionId"] for d in data], ["cx1"])
        self.assertFalse(data[0]["registered"])

    def test_sessions_use_alt_paths(self):
        alt = self.dir / "alt"
        self.ok("set", "animal-sim", "--alt-path", alt)
        self.ok("sessions", "animal-sim")
        calls = [c for c in self.scan.calls if c[0] == "claude_sessions"]
        self.assertEqual(calls[-1][1], [os.path.abspath(str(self.proj)), os.path.abspath(str(alt))])
        acalls = [c for c in self.scan.calls if c[0] == "anti_sessions"]
        self.assertEqual(acalls[-1][1], [os.path.abspath(str(self.proj)), os.path.abspath(str(alt))])


class TestHandoffFlow(CliBase):
    def setUp(self):
        CliBase.setUp(self)
        self.add()

    def test_full_cycle(self):
        out = self.ok("handoff", "animal-sim", "--transport", "bundle")
        self.assertIn("标签 20261117-win", out)
        self.assertIn("[ ] p1  出发一", out)
        h = self.proj_of()["handoff"]
        self.assertEqual((h["state"], h["from"], h["to"], h["transport"], h["hadDirty"], h["branch"]),
                         ("prep", "a", "b", "bundle", True, "main"))
        # second call keeps the existing handoff
        out = self.ok("handoff", "animal-sim", "--transport", "patch")
        self.assertIn("没有重建", out)
        self.assertEqual(self.proj_of()["handoff"]["transport"], "bundle")

        self.ok("check", "animal-sim", "pre", "p1")
        self.assertEqual(self.proj_of()["handoff"]["pre"], {"p1": True})
        self.assertIn("[x] p1", self.ok("plan", "animal-sim"))
        code, _, err = self.run_cli("check", "animal-sim", "pre", "nokey")
        self.assertEqual(code, 1)
        self.assertIn("p1, p2", err)
        self.ok("check", "animal-sim", "pre", "p1", "--undo")
        self.assertEqual(self.proj_of()["handoff"]["pre"], {})
        self.ok("check", "animal-sim", "pre", "p1")

        code, out, err = self.run_cli("depart", "animal-sim")
        self.assertEqual(code, 0)
        self.assertIn("p2", err)  # unchecked warning
        self.assertEqual(self.proj_of()["handoff"]["state"], "transit")

        # default phase flips to post once in transit
        self.assertIn("q1", self.ok("plan", "animal-sim"))
        data = json.loads(self.ok("plan", "animal-sim", "--phase", "pre", "--json"))
        self.assertEqual(data["phase"], "pre")

        # now on the other device
        self.update(lambda l: l["projects"][0]["sessions"].append(
            {"id": "s_1", "tool": "claude", "device": "a", "kind": "local", "carry": "summary",
             "sessionId": "sid-home", "title": "原来的会话", "lastUsed": "2026-11-17", "cwd": ""}))
        self.ok("note", "animal-sim", "--last", "写完了A", "--next", "做B", "--note", "留意C", "--verify", "pass")
        os.environ["RELAY_DESK_DEVICE"] = "b"
        self.update(lambda l: l["projects"][0]["paths"].update(b=str(self.proj)))
        out = self.ok("pickup", "animal-sim")
        self.assertIn("写完了A", out)
        self.assertIn("做B", out)
        self.assertIn("留意C", out)
        self.assertIn("通过", out)
        self.assertIn("原来的会话", out)
        self.assertIn("原 Session 留在原设备", out)
        self.assertIn("[ ] q1  到达一", out)
        self.assertEqual(self.proj_of()["handoff"]["state"], "transit")  # pickup changes nothing
        self.ok("check", "animal-sim", "post", "q1")

        self.scan.facts.update(branch="main", dirty=0)
        out = self.ok("arrive", "animal-sim")
        p = self.proj_of()
        self.assertEqual(p["device"], "b")
        self.assertIsNone(p["handoff"])
        self.assertEqual(len(p["history"]), 1)
        h0 = p["history"][0]
        self.assertEqual((h0["tag"], h0["from"], h0["to"], h0["transport"]), ("20261117-win", "a", "b", "bundle"))
        self.assertTrue(h0["doneAt"])
        self.assertEqual(p["dirty"], 0)  # scanned after arrival (device is now local)
        self.assertEqual(p["git"]["scannedOn"], "b")

    def test_history_capped_at_ten(self):
        self.ok("handoff", "animal-sim")
        self.update(lambda l: l["projects"][0].__setitem__(
            "history", [{"tag": "old%d" % i, "from": "b", "to": "a", "transport": "bundle", "doneAt": "x"} for i in range(10)]))
        os.environ["RELAY_DESK_DEVICE"] = "b"
        self.update(lambda l: l["projects"][0]["paths"].update(b=str(self.proj)))
        self.ok("arrive", "animal-sim")
        hist = self.proj_of()["history"]
        self.assertEqual(len(hist), 10)
        self.assertEqual(hist[-1]["tag"], "20261117-win")
        self.assertEqual(hist[0]["tag"], "old1")

    def test_handoff_requires_project_on_this_device(self):
        self.update(lambda l: l["projects"][0].update(device="b"))
        code, _, err = self.run_cli("handoff", "animal-sim")
        self.assertEqual(code, 1)
        self.assertIn("不在本机", err)
        self.assertIsNone(self.proj_of()["handoff"])

    def test_handoff_to_same_device_rejected(self):
        code, _, err = self.run_cli("handoff", "animal-sim", "--to", "a")
        self.assertEqual(code, 1)
        self.assertIsNone(self.proj_of()["handoff"])

    def test_arrive_on_wrong_device_rejected(self):
        self.ok("handoff", "animal-sim")
        code, _, err = self.run_cli("arrive", "animal-sim")  # still device a
        self.assertEqual(code, 1)
        self.assertIn("不能在这里完成", err)
        self.assertIsNotNone(self.proj_of()["handoff"])

    def test_commands_without_handoff(self):
        for cmd in ("plan", "depart", "pickup", "arrive", "cancel"):
            code, _, err = self.run_cli(cmd, "animal-sim")
            self.assertEqual(code, 1, cmd)
            self.assertIn("没有进行中的接力", err)
        self.assertEqual(self.run_cli("check", "animal-sim", "pre", "p1")[0], 1)

    def test_cancel(self):
        self.ok("handoff", "animal-sim")
        self.ok("cancel", "animal-sim")
        p = self.proj_of()
        self.assertIsNone(p["handoff"])
        self.assertEqual(p["history"], [])
        self.assertEqual(p["device"], "a")


class TestExportImport(CliBase):
    def test_export_stdout_and_file(self):
        self.add()
        data = json.loads(self.ok("export"))
        self.assertEqual((data["app"], data["version"]), ("relaydesk", 1))
        self.assertIn("exportedAt", data)
        self.assertEqual(data["projects"][0]["id"], "animal-sim")
        self.assertIn("devices", data["config"])
        f = self.dir / "backup.json"
        out = self.ok("export", f)
        self.assertIn("已导出 1 个项目", out)
        self.assertEqual(json.loads(f.read_text(encoding="utf-8"))["projects"][0]["id"], "animal-sim")

    def write_backup(self, projects):
        f = self.dir / "in.json"
        f.write_text(json.dumps({"app": "relaydesk", "version": 1, "config": L.default_config(), "projects": projects},
                                ensure_ascii=False), encoding="utf-8")
        return f

    def test_import_merge_newer_wins(self):
        self.add()
        local = self.proj_of()
        old = {"id": "animal-sim", "name": "OLD", "updatedAt": "2000-01-01T00:00:00.000Z", "note": "from file"}
        f = self.write_backup([old, {"id": "extra", "name": "Extra", "updatedAt": "2026-01-01T00:00:00.000Z", "mystery": 1}])
        out = self.ok("import", f)
        self.assertIn("新增 1，更新 0，保留本地较新的 1", out)
        self.assertEqual(self.proj_of()["name"], "Animal Sim")  # local newer kept
        self.assertEqual(self.proj_of("extra")["mystery"], 1)
        new = {"id": "animal-sim", "name": "FROM FILE", "updatedAt": "2999-01-01T00:00:00.000Z", "paths": {"a": "q"}}
        f = self.write_backup([new])
        out = self.ok("import", f)
        self.assertIn("更新 1", out)
        self.assertEqual(self.proj_of()["name"], "FROM FILE")
        self.assertEqual(len(self.led()["projects"]), 2)
        self.assertEqual(self.proj_of()["status"], "active")  # normalised
        self.assertNotEqual(local["name"], "FROM FILE")

    def test_import_replace(self):
        self.add()
        self.add("second", "Second")
        f = self.write_backup([{"id": "only", "name": "Only"}])
        out = self.ok("import", f, "--replace")
        self.assertIn("整体替换", out)
        self.assertEqual([p["id"] for p in self.led()["projects"]], ["only"])

    def test_import_rejects_bad_files(self):
        self.add()
        bad = self.dir / "bad.json"
        bad.write_text("nope", encoding="utf-8")
        self.assertEqual(self.run_cli("import", bad)[0], 1)
        bad.write_text(json.dumps({"app": "x"}), encoding="utf-8")
        code, _, err = self.run_cli("import", bad)
        self.assertEqual(code, 1)
        self.assertIn("没有项目列表", err)
        self.assertEqual(self.run_cli("import", self.dir / "missing.json")[0], 1)
        self.assertEqual(len(self.led()["projects"]), 1)

class TestForeignPaths(CliBase):
    def setUp(self):
        CliBase.setUp(self)
        self.add()

    def other_path(self):
        return self.proj_of()["paths"]["b"]

    def test_msys_rewrite_repaired(self):
        for exe in ("C:\\Program Files\\Git", "C:\\Program Files\\Git\\bin", "c:\\program files\\git\\usr\\bin"):
            with mock.patch.dict(os.environ, {"EXEPATH": exe}):
                out = self.ok("set", "animal-sim", "--path-other", "C:/Program Files/Git/Users/dev/Developer/demo-app")
            self.assertEqual(self.other_path(), "/Users/dev/Developer/demo-app")
            self.assertIn("已还原", out)
            self.update(lambda l: l["projects"][0]["paths"].update(b=""))

    def test_unrepairable_windows_path_for_mac(self):
        with mock.patch.dict(os.environ, {"EXEPATH": "C:\\Program Files\\Git"}):
            code, _, err = self.run_cli("set", "animal-sim", "--path-other", "D:\\other\\x")
        self.assertEqual(code, 1)
        self.assertIn("MSYS_NO_PATHCONV=1", err)
        self.assertIn("//Users", err)
        self.assertEqual(self.other_path(), "")

    def test_double_slash_and_valid_posix(self):
        self.ok("set", "animal-sim", "--path-other", "//Users/x")
        self.assertEqual(self.other_path(), "/Users/x")
        self.ok("set", "animal-sim", "--path-other", "~/dev/x")
        self.assertEqual(self.other_path(), "~/dev/x")

    def test_posix_path_for_windows_target_rejected(self):
        self.update(lambda l: l["config"]["devices"][1].update(os="windows"))
        code, _, err = self.run_cli("set", "animal-sim", "--path-other", "/Users/x")
        self.assertEqual(code, 1)
        self.assertIn("Windows", err)
        self.ok("set", "animal-sim", "--path-other", "E:\\dev\\x")
        self.assertEqual(self.other_path(), "E:\\dev\\x")

    def test_device_drop_for_other_device(self):
        with mock.patch.dict(os.environ, {"EXEPATH": "C:\\Program Files\\Git\\bin"}):
            self.ok("device", "--which", "b", "--drop", "C:/Program Files/Git/Users/t/drop")
        self.assertEqual(self.led()["config"]["devices"][1]["drop"], "/Users/t/drop")
        code, _, _ = self.run_cli("device", "--which", "b", "--drop", "Z:\\no")
        self.assertEqual(code, 1)
        self.ok("device", "--drop", "/as/given")  # local device: kept as given
        self.assertEqual(self.led()["config"]["devices"][0]["drop"], "/as/given")


class TestImportKeepsHost(CliBase):
    def test_host_kept_when_incoming_empty(self):
        self.update(lambda l: l["config"]["devices"][0].update(host="WORKSTATION"))
        cfg = L.default_config()
        cfg["devices"][1]["host"] = "MacHost"
        f = self.dir / "in.json"
        f.write_text(json.dumps({"config": cfg, "projects": []}), encoding="utf-8")
        for extra in ((), ("--replace",)):
            self.ok("import", f, *extra)
            devs = self.led()["config"]["devices"]
            self.assertEqual(devs[0]["host"], "WORKSTATION")
            self.assertEqual(devs[1]["host"], "MacHost" if extra else "")


class TestInstallSkills(CliBase):
    def setUp(self):
        CliBase.setUp(self)
        self.tool = self.dir / "tool"
        for which in ("claude", "codex", "anti"):
            d = self.tool / "skills" / which / "relay-desk"
            (d / "ref").mkdir(parents=True)
            (d / "SKILL.md").write_text("---\nname: relay-desk\n---\n运行 {{RELAY}} status，再 {{RELAY}} pickup。%s\n" % which,
                                        encoding="utf-8")
            (d / "ref" / "x.md").write_text("原样 {{RELAY}}", encoding="utf-8")
        p = mock.patch.object(L, "tool_dir", return_value=self.tool)
        p.start()
        self.addCleanup(p.stop)
        self.claude_dst = self.dir / "uhome" / ".claude" / "skills" / "relay-desk"
        self.codex_dst = self.dir / "codexhome" / "skills" / "relay-desk"
        self.anti_dst = self.dir / "geminiconfig" / "skills" / "relay-desk"

    def test_dry_run_writes_nothing(self):
        out = self.ok("install-skills", "--dry-run")
        self.assertIn(str(self.claude_dst / "SKILL.md"), out)
        self.assertIn(str(self.codex_dst / "SKILL.md"), out)
        self.assertIn(str(self.anti_dst / "SKILL.md"), out)
        self.assertFalse((self.dir / "uhome").exists())
        self.assertFalse((self.dir / "codexhome").exists())
        self.assertFalse((self.dir / "geminiconfig").exists())

    def test_install_substitutes_relay_windows(self):
        with mock.patch.object(L, "local_os", return_value="windows"):
            self.ok("install-skills")
        text = (self.claude_dst / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("{{RELAY}}", text)
        expect = 'py -3 "%s"' % os.path.join(str(self.tool), "relay.py")
        self.assertIn("运行 %s status，再 %s pickup。claude" % (expect, expect), text)
        self.assertIn("codex", (self.codex_dst / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("anti", (self.anti_dst / "SKILL.md").read_text(encoding="utf-8"))
        # non-SKILL.md files are copied verbatim
        self.assertEqual((self.claude_dst / "ref" / "x.md").read_text(encoding="utf-8"), "原样 {{RELAY}}")

    def test_install_posix_invocation(self):
        with mock.patch.object(L, "local_os", return_value="mac"):
            self.ok("install-skills", "--claude")
        text = (self.claude_dst / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('python3 "%s"' % os.path.join(str(self.tool), "relay.py"), text)
        self.assertFalse(self.codex_dst.exists())  # only --claude requested
        self.assertFalse(self.anti_dst.exists())

    def test_codex_only(self):
        self.ok("install-skills", "--codex")
        self.assertTrue(self.codex_dst.exists())
        self.assertFalse(self.claude_dst.exists())
        self.assertFalse(self.anti_dst.exists())

    def test_anti_only_and_default_gemini_config_dir(self):
        self.ok("install-skills", "--anti")
        self.assertTrue(self.anti_dst.exists())
        self.assertFalse(self.claude_dst.exists())
        self.assertFalse(self.codex_dst.exists())
        with mock.patch.dict(os.environ):
            del os.environ["GEMINI_CONFIG_DIR"]
            self.ok("install-skills", "--anti")
        self.assertTrue((self.dir / "uhome" / ".gemini" / "config" / "skills" / "relay-desk" / "SKILL.md").exists())

    def test_default_codex_home_follows_user_home(self):
        with mock.patch.dict(os.environ):
            del os.environ["CODEX_HOME"]
            self.ok("install-skills", "--codex")
        self.assertTrue((self.dir / "uhome" / ".codex" / "skills" / "relay-desk" / "SKILL.md").exists())

    def test_refuses_to_overwrite_differing_target_without_force(self):
        self.ok("install-skills")
        out = self.ok("install-skills")
        self.assertIn("已是最新", out)
        target = self.claude_dst / "SKILL.md"
        target.write_text("我自己改过的", encoding="utf-8")
        code, _, err = self.run_cli("install-skills")
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(target.read_text(encoding="utf-8"), "我自己改过的")
        self.ok("install-skills", "--force")
        restored = target.read_text(encoding="utf-8")
        self.assertIn("运行", restored)
        self.assertNotIn("{{RELAY}}", restored)

    def test_dry_run_reports_refusal_but_writes_nothing(self):
        self.ok("install-skills", "--claude")
        (self.claude_dst / "SKILL.md").write_text("changed", encoding="utf-8")
        out = self.ok("install-skills", "--claude", "--dry-run")
        self.assertIn("--force", out)
        self.assertEqual((self.claude_dst / "SKILL.md").read_text(encoding="utf-8"), "changed")

    def test_missing_source(self):
        import shutil
        shutil.rmtree(str(self.tool / "skills" / "codex"))
        code, _, err = self.run_cli("install-skills", "--codex")
        self.assertEqual(code, 1)
        self.assertIn("skill 源目录", err)


class TestServe(CliBase):
    def test_serve_wraps_ledger_in_store(self):
        self.add()
        server = types.ModuleType("relaydesk.server")
        seen = {}

        def serve(html_path, store, port=8765, open_browser=True):
            seen.update(html=html_path, port=port, open=open_browser)
            led, rev = store.read()
            seen["ids"] = [p["id"] for p in led["projects"]]
            seen["rev"] = rev
            led2, rev2 = store.update(lambda l: l["projects"][0].update(note="via store"))
            seen["rev2"] = rev2
        server.serve = serve
        with mock.patch.dict(sys.modules, {"relaydesk.server": server}):
            self.ok("serve", "--port", "9999", "--no-open")
        self.assertEqual(seen["ids"], ["animal-sim"])
        self.assertEqual((seen["port"], seen["open"]), (9999, False))
        self.assertTrue(seen["html"].endswith("relay-desk.html"))
        self.assertNotEqual(seen["rev"], seen["rev2"])
        self.assertEqual(self.proj_of()["note"], "via store")

    def test_serve_missing_module(self):
        with mock.patch.dict(sys.modules, {"relaydesk.server": None}):
            code, _, err = self.run_cli("serve", "--no-open")
        self.assertEqual(code, 1)
        self.assertIn("relaydesk.server", err)


class TestCloudRelayCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cloud = self.root / "cloud"
        self.cloud.mkdir()
        self.home_a = self.root / "home_a"
        self.uhome_a = self.root / "uhome_a"
        self.led_a = self.root / "led_a.json"
        self.home_b = self.root / "home_b"
        self.uhome_b = self.root / "uhome_b"
        self.led_b = self.root / "led_b.json"

    def _run_as(self, dev, *argv):
        home = self.home_a if dev == "a" else self.home_b
        uhome = self.uhome_a if dev == "a" else self.uhome_b
        led = self.led_a if dev == "a" else self.led_b
        env = {k: v for k, v in os.environ.items() if not k.startswith("RELAY_DESK_")}
        env.update(
            {
                "RELAY_DESK_HOME": str(home),
                "RELAY_DESK_LEDGER": str(led),
                "RELAY_DESK_DEVICE": dev,
                "RELAY_DESK_USER_HOME": str(uhome),
            }
        )
        o, e = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
                code = cli.main([str(x) for x in argv])
        return code, o.getvalue(), e.getvalue()

    def test_cloud_remotes_text_and_json(self):
        rc_conf = self.root / "rclone.conf"
        rc_conf.write_text("[alice_drive]\ntype = local\n\n[bob_s3]\ntype = local\n", encoding="utf-8")
        from relaydesk import storage

        if storage.has_rclone():
            code, out_txt, _ = self._run_as("a", "cloud", "remotes", "--rclone-config", rc_conf)
            self.assertEqual(code, 0)
            self.assertIn("alice_drive", out_txt)
            self.assertIn("bob_s3", out_txt)
            code, out_js, _ = self._run_as("a", "cloud", "remotes", "--rclone-config", rc_conf, "--json")
            self.assertEqual(code, 0)
            parsed = json.loads(out_js)
            self.assertTrue(parsed["hasRclone"])
            self.assertEqual(parsed["remotes"], ["alice_drive", "bob_s3"])

        with mock.patch("relaydesk.storage.has_rclone", return_value=False):
            code, out_none, _ = self._run_as("a", "cloud", "remotes", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out_none), {"hasRclone": False, "remotes": []})

    def test_pair_send_inbox_receive_transcripts_cli_workflow(self):
        self._run_as("a", "init", "--device", "a")
        self._run_as("b", "init", "--device", "b")

        code, out_st, _ = self._run_as("a", "pair", "--status")
        self.assertEqual(code, 0)
        self.assertIn("未配置", out_st)

        code, out_pa1, _ = self._run_as("a", "pair", "--provider", "local", "--root", self.cloud)
        self.assertEqual(code, 0)
        self.assertIn("等待另一台设备", out_pa1)

        code, out_pb1, _ = self._run_as("b", "pair", "--provider", "local", "--root", self.cloud)
        self.assertEqual(code, 0)
        self.assertIn("配对指纹：", out_pb1)

        code, out_pa_check, _ = self._run_as("a", "pair")
        self.assertEqual(code, 0)
        self.assertIn("配对指纹：", out_pa_check)

        code, out_pa_acc, _ = self._run_as("a", "pair", "--accept", "--json")
        self.assertEqual(code, 0)
        js_a = json.loads(out_pa_acc)
        self.assertTrue(js_a["paired"])

        code, out_pb_acc, _ = self._run_as("b", "pair", "--accept", "--json")
        self.assertEqual(code, 0)
        js_b = json.loads(out_pb_acc)
        self.assertTrue(js_b["paired"])
        self.assertEqual(js_a["pairFingerprint"], js_b["pairFingerprint"])

        # Create project & Claude session on A
        proj_a = self.root / "proj_a"
        proj_a.mkdir()
        (proj_a / "app.py").write_text("print('v1')\n", encoding="utf-8")
        (proj_a / "HANDOFF.md").write_text("## Handoff\nNext: add tests\n", encoding="utf-8")
        self._run_as("a", "add", "CLI Relay App", "--path", proj_a, "--id", "cli-app")
        self._run_as("a", "note", "cli-app", "--last", "初始化项目", "--next", "补测试")

        from relaydesk import scan

        cdir = self.uhome_a / ".claude" / "projects" / scan.enc_path(str(proj_a))
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "sess_cli_1.jsonl").write_text(
            json.dumps({"type": "summary", "summary": "CLI 会话测试"}, ensure_ascii=False)
            + "\n"
            + json.dumps({"type": "user", "message": {"role": "user", "content": "特殊检索词_ALPHA"}}, ensure_ascii=False)
            + "\n"
            + json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "收到 ALPHA"}}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )

        # Send on A (text mode must output "已交棒，可以关机")
        code, out_send, err_send = self._run_as("a", "send", "cli-app", "--to", "b")
        self.assertEqual(code, 0, err_send)
        self.assertIn("已交棒，可以关机", out_send)

        # Inbox on B (text & json)
        code, out_ib_txt, _ = self._run_as("b", "inbox")
        self.assertEqual(code, 0)
        self.assertIn("CLI Relay App", out_ib_txt)
        code, out_ib_js, _ = self._run_as("b", "inbox", "--json")
        self.assertEqual(code, 0)
        ib_data = json.loads(out_ib_js)
        self.assertEqual(len(ib_data["incoming"]), 1)
        rid = ib_data["incoming"][0]["relayId"]

        # First receive on B without --path -> exit 1
        code, _, err_np = self._run_as("b", "receive", rid)
        self.assertEqual(code, 1)
        self.assertIn("--path", err_np)

        # Receive on B with --path
        proj_b = self.root / "proj_b"
        code, out_recv, err_recv = self._run_as("b", "receive", rid, "--path", proj_b)
        self.assertEqual(code, 0, err_recv)
        self.assertIn("接棒完成", out_recv)
        self.assertEqual((proj_b / "app.py").read_text(encoding="utf-8"), "print('v1')\n")

        # Duplicate receive -> exit 1
        code, _, err_dup = self._run_as("b", "receive", rid)
        self.assertEqual(code, 1)
        self.assertIn("不能重复领取", err_dup)

        # Transcripts list, --session, --grep, --json on B
        code, out_tr_list, _ = self._run_as("b", "transcripts", "cli-app")
        self.assertEqual(code, 0)
        self.assertIn("sess_cli_1", out_tr_list)

        code, out_tr_md, _ = self._run_as("b", "transcripts", "cli-app", "--session", "sess_cli_1")
        self.assertEqual(code, 0)
        self.assertIn("特殊检索词_ALPHA", out_tr_md)

        code, out_tr_grep, _ = self._run_as("b", "transcripts", "cli-app", "--grep", "特殊检索词_ALPHA")
        self.assertEqual(code, 0)
        self.assertIn("sess_cli_1", out_tr_grep)

        # Send back from B in --json mode (must contain "message": "已交棒，可以关机")
        (proj_b / "app.py").write_text("print('v2')\n", encoding="utf-8")
        code, out_send_js, err_s2 = self._run_as("b", "send", "cli-app", "--to", "a", "--json")
        self.assertEqual(code, 0, err_s2)
        send2_data = json.loads(out_send_js)
        self.assertEqual(send2_data["message"], "已交棒，可以关机")
        self.assertEqual(send2_data["parentRelayId"], rid)


if __name__ == "__main__":
    unittest.main()
