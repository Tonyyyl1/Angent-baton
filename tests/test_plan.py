# -*- coding: utf-8 -*-
import copy
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relaydesk import plan  # noqa: E402


def make_config(win_drop="D:\\handoff", mac_drop=""):
    return {
        "devices": [
            {"id": "a", "name": "Windows", "os": "windows", "drop": win_drop, "host": ""},
            {"id": "b", "name": "Mac", "os": "mac", "drop": mac_drop, "host": ""},
        ],
        "reg": {},
    }


def make_project(**kw):
    p = {
        "id": "animal-sim", "name": "animal-sim", "device": "a",
        "paths": {"a": "D:\\developer\\animal-sim", "b": ""},
        "branch": "main", "worktree": "", "remote": "", "tool": "claude", "status": "active",
        "dirty": 2, "unpushed": None, "lastTask": "", "nextAction": "", "note": "",
        "verify": {"state": "none", "cmd": "", "note": ""},
        "transport": "", "reg": {}, "sessions": [], "conflicts": [], "history": [],
        "handoff": None,
    }
    p.update(kw)
    return p


def make_handoff(transport, dirty=True, **kw):
    h = {"tag": "20261117-win", "from": "a", "to": "b", "transport": transport, "state": "prep",
         "pre": {}, "post": {}, "hadDirty": dirty, "branch": "main", "baseCommit": "", "createdAt": ""}
    h.update(kw)
    return h


def block(blocks, title):
    for b in blocks:
        if b["title"] == title:
            return b
    raise AssertionError("no block %r in %r" % (title, [b["title"] for b in blocks]))


def titles(blocks):
    return [b["title"] for b in blocks]


def plan_of(transport, dirty=True, config=None, **pk):
    p = make_project(**pk)
    p["handoff"] = make_handoff(transport, dirty)
    return plan.plan_for(p, config or make_config())


def no_notes(lines):
    return [l for l in lines if isinstance(l, str)]


class Golden(unittest.TestCase):
    def test_bundle_dirty(self):
        pl = plan_of("bundle", True)
        self.assertEqual(no_notes(block(pl["pre"], "打成 bundle 文件")["lines"]), [
            'cd "D:\\developer\\animal-sim"',
            "git switch -c wip/handoff-20261117-win",
            "if ($?) {",
            "  git add -A",
            '  git commit -m "WIP handoff 20261117-win (do not merge)"',
            "  git switch main",
            "}",
            'git bundle create "D:\\handoff\\animal-sim-20261117-win.bundle" main wip/handoff-20261117-win',
            'git bundle verify "D:\\handoff\\animal-sim-20261117-win.bundle"',
        ])
        self.assertEqual(no_notes(block(pl["post"], "从 bundle 取回")["lines"]), [
            'cd "<Mac 上的项目路径>"',
            'git bundle verify "<Mac 的中转目录>/animal-sim-20261117-win.bundle"',
            'git fetch "<Mac 的中转目录>/animal-sim-20261117-win.bundle" "refs/heads/*:refs/remotes/handoff/*"',
            "git switch main && {",
            '  git merge --ff-only "handoff/wip/handoff-20261117-win~1" && {',
            "    git cherry-pick -n handoff/wip/handoff-20261117-win && git reset -q",
            "  }",
            "}",
            "git status --short --branch",
        ])

    def test_remote_dirty(self):
        pl = plan_of("remote", True, name="Scala cw", remote="origin",
                     paths={"a": "D:\\developer\\Scala cw\\repo", "b": ""})
        push = block(pl["pre"], "推送这条 WIP 分支")
        self.assertEqual(push["kind"], "remote")
        self.assertEqual(no_notes(push["lines"]), [
            'cd "D:\\developer\\Scala cw\\repo"', "git push -u origin wip/handoff-20261117-win"])
        self.assertEqual(no_notes(block(pl["post"], "取回代码和未提交改动")["lines"]), [
            'cd "<Mac 上的项目路径>"',
            "git fetch origin",
            "git switch main && {",
            '  git merge --ff-only "origin/wip/handoff-20261117-win~1" && {',
            "    git cherry-pick -n origin/wip/handoff-20261117-win && git reset -q",
            "  }",
            "}",
            "git status --short --branch",
        ])


class Helpers(unittest.TestCase):
    def test_slug_and_enc(self):
        self.assertEqual(plan.file_slug("Scala cw"), "Scala-cw")
        self.assertEqual(plan.file_slug("--中文--"), "project")
        self.assertEqual(plan.file_slug(""), "project")
        self.assertEqual(plan.file_slug("a  b!!c.d_e"), "a-b-c.d_e")
        self.assertEqual(plan.enc_path("D:\\developer\\x y"), "D--developer-x-y")
        d = make_config()["devices"][1]
        self.assertEqual(plan.enc_of("<Mac 上的项目路径>", d), "<Mac 上项目路径的编码>")
        self.assertEqual(plan.enc_of("/Users/a/p", d), "-Users-a-p")

    def test_join_and_home(self):
        w, m = make_config()["devices"]
        self.assertEqual(plan.join_p(w, "D:\\handoff\\\\", "x"), "D:\\handoff\\x")
        self.assertEqual(plan.join_p(m, "/tmp/h//", "x"), "/tmp/h/x")
        self.assertEqual(plan.home_p(w, [".claude", "skills"]), "$env:USERPROFILE\\.claude\\skills")
        self.assertEqual(plan.home_p(m, [".claude", "skills"]), "$HOME/.claude/skills")

    def test_copy_cmds(self):
        w, m = make_config()["devices"]
        self.assertEqual(plan.cp_file(w, "a", "b"), 'Copy-Item "a" "b"')
        self.assertEqual(plan.cp_file(m, "a", "b"), 'cp "a" "b"')
        self.assertEqual(plan.mk_dir(w, "p"), 'New-Item -ItemType Directory -Force "p" | Out-Null')
        self.assertEqual(plan.mk_dir(m, "p"), 'mkdir -p "p"')
        self.assertEqual(plan.cp_dir(w, "s", "d"), 'robocopy "s" "d" /E')
        self.assertEqual(plan.cp_dir(w, "s", "d", {"xd": ["x", "y"], "xf": [".env"]}),
                         'robocopy "s" "d" /E /XD x y /XF .env')
        self.assertEqual(plan.cp_dir(m, "s", "d", {"xf": [".env"]}), 'rsync -a --exclude ".env" "s/" "d/"')
        self.assertEqual(plan.cp_dir(m, "s", "d", {"xd": ["x"]}), 'rsync -a --exclude "x/" "s/" "d/"')

    def test_guard(self):
        w, m = make_config()["devices"]
        self.assertEqual(plan.guard(w, "A", ["b", {"n": "x"}]), ["A", "if ($?) {", "  b", {"n": "x"}, "}"])
        self.assertEqual(plan.guard(m, "A", ["b"]), ["A && {", "  b", "}"])
        self.assertEqual(plan.guard(m, "A", plan.guard(m, "B", ["c"])), ["A && {", "  B && {", "    c", "  }", "}"])

    def test_os_slug(self):
        cfg = make_config()
        self.assertEqual(plan.os_slug(cfg["devices"][0], cfg), "win")
        self.assertEqual(plan.os_slug(cfg["devices"][1], cfg), "mac")
        cfg["devices"][1]["os"] = "windows"
        self.assertEqual(plan.os_slug(cfg["devices"][0], cfg), "win-a")
        cfg["devices"][1]["os"] = "linux"
        self.assertEqual(plan.os_slug(cfg["devices"][1], cfg), "linux")
        self.assertEqual(plan.shell_of(cfg["devices"][1]), "bash")

    def test_reg_on(self):
        p = make_project(reg={"x": {"on": {"a": True}, "note": ""}, "y": {"on": {"a": False}}, "z": {}})
        self.assertTrue(plan.reg_on(p, "x", "a"))
        self.assertFalse(plan.reg_on(p, "x", "b"))
        self.assertFalse(plan.reg_on(p, "y", "a"))
        self.assertFalse(plan.reg_on(p, "z", "a"))
        self.assertFalse(plan.reg_on(p, "nope", "a"))

    def test_dev_other_fallback(self):
        cfg = make_config()
        self.assertEqual(plan.dev(cfg, "zzz")["id"], "a")
        self.assertEqual(plan.other(cfg, "a")["id"], "b")
        self.assertEqual(plan.other(cfg, "b")["id"], "a")

    def test_vocab(self):
        self.assertEqual(plan.TIER_ORDER, ["native", "git", "extra", "manual"])
        self.assertEqual(plan.VERIFY["partial"], "部分通过")
        self.assertEqual(sorted(plan.TRANSPORT), ["bundle", "folder", "patch", "remote"])
        self.assertEqual(len(plan.KINDS), 19)
        self.assertTrue(all(k["scope"] == "project" for k in plan.KINDS))


class Transports(unittest.TestCase):
    def test_remote_clean(self):
        pl = plan_of("remote", False, remote="origin")
        self.assertEqual(titles(pl["pre"]), ["看清现状", "凭据检查", "推送还没推的提交"])
        push = pl["pre"][2]
        self.assertEqual(push["kind"], "remote")
        self.assertEqual(no_notes(push["lines"]), ['cd "D:\\developer\\animal-sim"', "git push origin main"])
        self.assertEqual(no_notes(pl["post"][1]["lines"]), [
            'cd "<Mac 上的项目路径>"', "git fetch origin",
            "git switch main && {", "  git pull --ff-only", "}", "git status --short --branch"])
        self.assertEqual(pl["post"][1]["title"], "取回代码")
        # no drop dir needed for remote without extras
        self.assertEqual(pl["miss"], ["Mac 上的项目路径"])

    def test_remote_name_regex(self):
        pl = plan_of("remote", False, remote="  upstream (github)")
        self.assertIn("git push upstream main", pl["pre"][2]["lines"])
        pl = plan_of("remote", False, remote="")
        self.assertIn("git push origin main", pl["pre"][2]["lines"])

    def test_remote_dirty_pre_blocks(self):
        pl = plan_of("remote", True, remote="origin")
        self.assertEqual(titles(pl["pre"]), ["看清现状", "凭据检查", "把未提交改动停到 WIP 分支", "推送这条 WIP 分支"])
        wip = pl["pre"][2]["lines"]
        self.assertEqual(wip[0], 'cd "D:\\developer\\animal-sim"')
        self.assertEqual(wip[-1], {"n": "做完后本机工作区是干净的，改动在 wip/handoff-20261117-win 上"})
        # look block gets the unpushed hint when a remote exists
        look = pl["pre"][0]["lines"]
        self.assertEqual(look[-1], 'git log --oneline "@{u}..HEAD"')
        self.assertEqual(look[-2], {"n": "下面这条列出还没推送的提交。分支没有上游时会报错，可以忽略"})

    def test_look_without_remote(self):
        pl = plan_of("bundle", False)
        self.assertEqual(pl["pre"][0]["lines"], [
            'cd "D:\\developer\\animal-sim"', "git status --short --branch", "git stash list", "git worktree list"])

    def test_bundle_clean(self):
        pl = plan_of("bundle", False)
        self.assertEqual(titles(pl["pre"]), ["看清现状", "凭据检查", "打成 bundle 文件"])
        self.assertEqual(pl["pre"][2]["lines"], [
            'cd "D:\\developer\\animal-sim"',
            'git bundle create "D:\\handoff\\animal-sim-20261117-win.bundle" main',
            'git bundle verify "D:\\handoff\\animal-sim-20261117-win.bundle"'])
        post = block(pl["post"], "从 bundle 取回")["lines"]
        self.assertEqual(no_notes(post)[3:], [
            "git switch main && {", "  git merge --ff-only handoff/main", "}", "git status --short --branch"])
        self.assertEqual(post[-1], {"n": 'Mac 上还没有这个仓库时：git clone -b main '
                                         '"<Mac 的中转目录>/animal-sim-20261117-win.bundle" "<Mac 上的项目路径>"'})
        self.assertEqual(pl["miss"], ["Mac 上的项目路径", "Mac 的中转目录"])

    def test_patch_clean_and_dirty(self):
        for dirty in (True, False):
            pl = plan_of("patch", dirty)
            pre = block(pl["pre"], "导出补丁")["lines"]
            self.assertEqual(no_notes(pre), [
                'cd "D:\\developer\\animal-sim"', "git rev-parse HEAD", "git add -A -N",
                'git diff HEAD --binary --output="D:\\handoff\\animal-sim-20261117-win.patch"', "git reset -q"])
            post = block(pl["post"], "套用补丁")["lines"]
            self.assertEqual(no_notes(post), [
                'cd "<Mac 上的项目路径>"', "git rev-parse HEAD",
                'git apply --check "<Mac 的中转目录>/animal-sim-20261117-win.patch" && {',
                '  git apply "<Mac 的中转目录>/animal-sim-20261117-win.patch"', "}", "git status --short"])
            self.assertEqual(post[1], {"n": "这条输出必须等于离开时记下的基底提交"})

    def test_patch_base_commit(self):
        p = make_project()
        p["handoff"] = make_handoff("patch", True, baseCommit="abc1234")
        pl = plan.plan_for(p, make_config())
        self.assertEqual(block(pl["post"], "套用补丁")["lines"][1], {"n": "这条输出必须等于离开时记下的基底提交：abc1234"})

    def test_folder(self):
        pl = plan_of("folder", False, branch="")
        self.assertEqual(titles(pl["pre"]), ["复制整个目录"])
        self.assertEqual(no_notes(pl["pre"][0]["lines"]), [
            'robocopy "D:\\developer\\animal-sim" "D:\\handoff\\animal-sim-20261117-win" /E /XF .env'])
        self.assertEqual(titles(pl["post"]), ["放回整个目录", "开新 Session 接上进度"])
        self.assertEqual(no_notes(pl["post"][0]["lines"]), [
            'rsync -a "<Mac 的中转目录>/animal-sim-20261117-win/" "<Mac 上的项目路径>/"'])
        keys = [c[0] for c in pl["preChecks"]]
        self.assertNotIn("stash", keys)
        self.assertEqual(pl["preChecks"][4][1], "按给出的命令复制整个目录")
        self.assertNotIn("clean", [c[0] for c in pl["postChecks"]])
        self.assertEqual(pl["BR"], "main")  # default when no branch

    def test_dirty_flag_changes_wording(self):
        a = plan_of("bundle", True)
        b = plan_of("bundle", False)
        self.assertEqual(a["preChecks"][4][1], "按给出的命令带走代码和 2 个未提交文件")
        self.assertEqual(b["preChecks"][4][1], "按给出的命令带走代码")
        self.assertEqual(a["postChecks"][1][1], "取回代码和未提交改动")
        self.assertEqual(b["postChecks"][1][1], "取回代码")
        self.assertEqual(a["WIP"], "wip/handoff-20261117-win")
        self.assertTrue(a["dirty"])
        self.assertFalse(b["dirty"])

    def test_dirty_flag_but_zero_count(self):
        p = make_project(dirty=0)
        p["handoff"] = make_handoff("bundle", True)
        pl = plan.plan_for(p, make_config())
        self.assertEqual(pl["preChecks"][4][1], "按给出的命令带走代码和未提交改动")

    def test_new_wording(self):
        pl = plan_of("bundle", False)
        self.assertEqual(pl["preChecks"][1][2], "对代理说「接力」，它会写 HANDOFF.md 并更新台账。Claude Code 的 handoff 插件（「交接」）还会提交并推送，不想写远程的项目不要用它。这是摘要，替代不了原 Session。")
        self.assertEqual(pl["postChecks"][1][2], "按给出的命令做。出现冲突就停下，不强推、不覆盖，记一条冲突记录。")
        self.assertEqual(pl["postChecks"][3][2], "开新 Session，对代理说「接棒」。云端 Session 可以直接打开原来那个。")

    def test_credential_check_by_os(self):
        pl = plan_of("bundle", False)
        self.assertIn("Select-String -Pattern '\\.env|auth\\.json|credentials|secret|token|\\.pem|\\.key$|id_rsa'",
                      pl["pre"][1]["lines"][2])
        self.assertEqual(pl["pre"][1]["kind"], "read")

    def test_patch_precheck_note(self):
        pl = plan_of("patch", True)
        self.assertIn("git stash push -u", pl["preChecks"][4][2])
        self.assertEqual(plan_of("bundle", True)["preChecks"][4][2], "")


class MacToWindows(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(win_drop="D:\\handoff", mac_drop="/Users/x/drop")

    def mk(self, transport, dirty=True, **pk):
        p = make_project(device="b", paths={"a": "D:\\work\\p", "b": "/Users/x/p"}, **pk)
        p["handoff"] = make_handoff(transport, dirty, **{"from": "b", "to": "a", "tag": "20261117-mac"})
        return plan.plan_for(p, self.cfg)

    def test_bundle_dirty(self):
        pl = self.mk("bundle", True)
        self.assertEqual(pl["pre"][2]["where"], "在 Mac 的 zsh 里")
        self.assertEqual(no_notes(pl["pre"][2]["lines"]), [
            'cd "/Users/x/p"',
            "git switch -c wip/handoff-20261117-mac && {",
            "  git add -A",
            '  git commit -m "WIP handoff 20261117-mac (do not merge)"',
            "  git switch main",
            "}",
            'git bundle create "/Users/x/drop/animal-sim-20261117-mac.bundle" main wip/handoff-20261117-mac',
            'git bundle verify "/Users/x/drop/animal-sim-20261117-mac.bundle"'])
        post = block(pl["post"], "从 bundle 取回")
        self.assertEqual(post["where"], "在 Windows 的 PowerShell 里")
        self.assertEqual(no_notes(post["lines"]), [
            'cd "D:\\work\\p"',
            'git bundle verify "D:\\handoff\\animal-sim-20261117-mac.bundle"',
            'git fetch "D:\\handoff\\animal-sim-20261117-mac.bundle" "refs/heads/*:refs/remotes/handoff/*"',
            "git switch main",
            "if ($?) {",
            '  git merge --ff-only "handoff/wip/handoff-20261117-mac~1"',
            "  if ($?) {",
            "    git cherry-pick -n handoff/wip/handoff-20261117-mac",
            "    if ($?) { git reset -q }",
            "  }",
            "}",
            "git status --short --branch"])
        self.assertEqual(pl["miss"], [])

    def test_folder_directions(self):
        pl = self.mk("folder", False, branch="")
        self.assertEqual(no_notes(pl["pre"][0]["lines"]), [
            'rsync -a --exclude ".env" "/Users/x/p/" "/Users/x/drop/animal-sim-20261117-win/"'.replace("win", "mac")])
        self.assertEqual(no_notes(pl["post"][0]["lines"]), [
            'robocopy "D:\\handoff\\animal-sim-20261117-mac" "D:\\work\\p" /E'])


class Miss(unittest.TestCase):
    def test_all_missing(self):
        cfg = make_config(win_drop="", mac_drop="")
        p = make_project(paths={})
        p["handoff"] = make_handoff("bundle", False)
        pl = plan.plan_for(p, cfg)
        self.assertEqual(pl["miss"], ["Windows 上的项目路径", "Mac 上的项目路径", "Windows 的中转目录", "Mac 的中转目录"])
        self.assertIn('cd "<Windows 上的项目路径>"', pl["pre"][0]["lines"])
        self.assertIn('git bundle verify "<Windows 的中转目录>\\animal-sim-20261117-win.bundle"', pl["pre"][2]["lines"])

    def test_remote_no_drop_placeholders(self):
        cfg = make_config(win_drop="", mac_drop="")
        p = make_project(remote="origin", paths={})
        p["handoff"] = make_handoff("remote", True)
        pl = plan.plan_for(p, cfg)
        self.assertEqual(pl["miss"], ["Windows 上的项目路径", "Mac 上的项目路径"])
        text = repr(pl["pre"]) + repr(pl["post"])
        self.assertNotIn("中转目录", text)

    def test_extras_force_drop(self):
        cfg = make_config(win_drop="", mac_drop="")
        p = make_project(remote="origin", paths={"a": "D:\\p", "b": "/p"},
                         reg={"claudeLocal": {"on": {"a": True}}})
        p["handoff"] = make_handoff("remote", False)
        pl = plan.plan_for(p, cfg)
        self.assertEqual(pl["miss"], ["Windows 的中转目录", "Mac 的中转目录"])


class Extras(unittest.TestCase):
    def test_claude_local(self):
        pl = plan_of("bundle", False, paths={"a": "D:\\p", "b": "/Users/x/p"},
                     reg={"claudeLocal": {"on": {"a": True}}}, config=make_config(mac_drop="/drop"))
        ex = block(pl["pre"], "带走额外同步的文件")
        self.assertEqual(ex["lines"], ['Copy-Item "D:\\p\\CLAUDE.local.md" "D:\\handoff\\animal-sim-20261117-win-CLAUDE.local.md"'])
        back = block(pl["post"], "放回额外同步的文件")
        self.assertEqual(back["lines"], ['cp "/drop/animal-sim-20261117-win-CLAUDE.local.md" "/Users/x/p/CLAUDE.local.md"'])
        self.assertEqual(pl["preChecks"][-2], ["extra", "带走需要额外同步的本地状态", "CLAUDE.local.md"])
        self.assertEqual(pl["postChecks"][2], ["extra", "放回额外同步的文件", "CLAUDE.local.md"])

    def test_memory_known_target(self):
        pl = plan_of("bundle", False, paths={"a": "D:\\p x", "b": "/Users/x/p"},
                     reg={"ccMemory": {"on": {"a": True}}}, config=make_config(mac_drop="/drop"))
        ex = block(pl["pre"], "带走额外同步的文件")["lines"]
        self.assertEqual(ex, ['robocopy "$env:USERPROFILE\\.claude\\projects\\D--p-x\\memory" '
                              '"D:\\handoff\\animal-sim-20261117-win-memory" /E'])
        back = block(pl["post"], "放回额外同步的文件")["lines"]
        self.assertEqual(back[1], 'mkdir -p "$HOME/.claude/projects/-Users-x-p/memory"')
        self.assertEqual(back[2], 'rsync -a "/drop/animal-sim-20261117-win-memory/" "$HOME/.claude/projects/-Users-x-p/memory/"')

    def test_memory_unknown_target_placeholder(self):
        pl = plan_of("bundle", False, reg={"ccMemory": {"on": {"a": True}}}, config=make_config(mac_drop="/drop"))
        back = block(pl["post"], "放回额外同步的文件")["lines"]
        self.assertEqual(back[0], {"n": "目录名是项目路径的编码：字母数字以外的字符都换成 -"})
        self.assertEqual(back[1], 'mkdir -p "$HOME/.claude/projects/<Mac 上项目路径的编码>/memory"')

    def test_transcript_session(self):
        s = {"id": "s_1", "tool": "claude", "device": "a", "kind": "local", "carry": "transcript",
             "title": "T", "sessionId": "abc-123", "cwd": "", "lastUsed": "", "note": ""}
        pl = plan_of("bundle", False, paths={"a": "D:\\p", "b": "/Users/x/p"}, sessions=[s],
                     config=make_config(mac_drop="/drop"))
        ex = block(pl["pre"], "带走额外同步的文件")["lines"]
        self.assertEqual(ex, ['Copy-Item "$env:USERPROFILE\\.claude\\projects\\D--p\\abc-123.jsonl" "D:\\handoff\\abc-123.jsonl"'])
        back = block(pl["post"], "放回额外同步的文件")["lines"]
        self.assertEqual(back, [
            {"n": "非官方做法。转录里的绝对路径仍指向 Windows，先拿不重要的 Session 试"},
            'mkdir -p "$HOME/.claude/projects/-Users-x-p"',
            'cp "/drop/abc-123.jsonl" "$HOME/.claude/projects/-Users-x-p/abc-123.jsonl"',
            'cd "/Users/x/p"', "claude --resume abc-123"])
        self.assertEqual(pl["preChecks"][-2][2], "转录文件：T")

    def test_anti_brain_win_to_mac_and_mac_to_win(self):
        cfg = make_config(win_drop="D:\\handoff", mac_drop="/Users/x/drop")
        s_win = {"id": "s_ag", "tool": "anti", "device": "a", "kind": "local", "carry": "transcript",
                 "title": "迁移任务", "sessionId": "7d5cf4ca"}
        pl = plan_of("bundle", False, paths={"a": "D:\\p", "b": "/Users/x/p"}, sessions=[s_win], config=cfg)
        self.assertEqual(block(pl["pre"], "带走额外同步的文件")["lines"], [
            'robocopy "$env:USERPROFILE\\.gemini\\antigravity\\brain\\7d5cf4ca" '
            '"D:\\handoff\\animal-sim-20261117-win-anti-brain-7d5cf4ca" /E'
        ])
        self.assertEqual(no_notes(block(pl["post"], "放回额外同步的文件")["lines"]), [
            'mkdir -p "$HOME/.gemini/antigravity/brain/7d5cf4ca"',
            'rsync -a "/Users/x/drop/animal-sim-20261117-win-anti-brain-7d5cf4ca/" '
            '"$HOME/.gemini/antigravity/brain/7d5cf4ca/"',
        ])
        self.assertEqual(pl["preChecks"][-2][2], "Antigravity brain：迁移任务")

        # Reverse direction: Mac -> Windows
        s_mac = dict(s_win, device="b")
        p_mac = make_project(device="b", paths={"a": "D:\\p", "b": "/Users/x/p"}, sessions=[s_mac])
        p_mac["handoff"] = make_handoff("bundle", False, **{"from": "b", "to": "a", "tag": "20261117-mac"})
        pl2 = plan.plan_for(p_mac, cfg)
        self.assertEqual(block(pl2["pre"], "带走额外同步的文件")["lines"], [
            'rsync -a "$HOME/.gemini/antigravity/brain/7d5cf4ca/" '
            '"/Users/x/drop/animal-sim-20261117-mac-anti-brain-7d5cf4ca/"'
        ])
        self.assertEqual(no_notes(block(pl2["post"], "放回额外同步的文件")["lines"]), [
            'New-Item -ItemType Directory -Force "$env:USERPROFILE\\.gemini\\antigravity\\brain\\7d5cf4ca" | Out-Null',
            'robocopy "D:\\handoff\\animal-sim-20261117-mac-anti-brain-7d5cf4ca" '
            '"$env:USERPROFILE\\.gemini\\antigravity\\brain\\7d5cf4ca" /E',
        ])

    def test_transcript_unknown_target_and_cwd(self):
        s = {"tool": "claude", "device": "a", "kind": "local", "carry": "transcript", "title": "",
             "sessionId": "zz", "cwd": "C:\\other dir"}
        pl = plan_of("bundle", False, sessions=[s], config=make_config(mac_drop="/drop"))
        ex = block(pl["pre"], "带走额外同步的文件")["lines"]
        self.assertIn("C--other-dir", ex[0])
        back = block(pl["post"], "放回额外同步的文件")["lines"]
        self.assertEqual(back[1], 'mkdir -p "$HOME/.claude/projects/<Mac 上项目路径的编码>"')
        self.assertEqual(pl["preChecks"][-2][2], "转录文件：zz")

    def test_sessions_not_extras(self):
        base = {"tool": "claude", "device": "a", "kind": "local", "carry": "transcript", "sessionId": "x"}
        for patch in ({"tool": "codex"}, {"kind": "cloud"}, {"device": "b"}, {"carry": "summary"}, {"sessionId": ""}):
            s = dict(base, **patch)
            pl = plan_of("remote", False, remote="origin", sessions=[s])
            self.assertNotIn("带走额外同步的文件", titles(pl["pre"]), patch)

    def test_extras_order(self):
        s = {"tool": "claude", "device": "a", "kind": "local", "carry": "transcript", "sessionId": "x", "title": "S"}
        pl = plan_of("bundle", False, sessions=[s], config=make_config(mac_drop="/d"),
                     reg={"ccMemory": {"on": {"a": True}}, "claudeLocal": {"on": {"a": True}}})
        self.assertEqual(pl["preChecks"][-2][2], "CLAUDE.local.md、Claude Code 自动记忆、转录文件：S")
        self.assertEqual(len(block(pl["pre"], "带走额外同步的文件")["lines"]), 3)


class WorktreeVerifyTool(unittest.TestCase):
    def test_worktree(self):
        pl = plan_of("bundle", False, worktree="D:\\wt\\x")
        wt = block(pl["post"], "重建 worktree")
        self.assertEqual(wt["lines"][1], {"n": "原来在 Windows 的 D:\\wt\\x。目录和分支换成这台设备上要用的"})
        self.assertEqual(wt["lines"][-1], "git worktree list")
        self.assertIn("worktree", [c[0] for c in pl["postChecks"]])
        self.assertNotIn("重建 worktree", titles(plan_of("bundle", False)["post"]))

    def test_verify(self):
        pl = plan_of("bundle", False, verify={"state": "partial", "cmd": "pytest -q", "note": "2 个失败"})
        v = block(pl["post"], "重跑验证")
        self.assertEqual(v["lines"], ['cd "<Mac 上的项目路径>"', {"n": "上次结果：部分通过。2 个失败"}, "pytest -q"])
        self.assertEqual(titles(pl["post"])[-1], "重跑验证")
        self.assertEqual(pl["postChecks"][-2], ["verify", "重跑验证，对照上次结果", "上次：部分通过。2 个失败"])
        self.assertNotIn("重跑验证", titles(plan_of("bundle", False)["post"]))
        self.assertEqual(plan_of("bundle", False)["postChecks"][-2][2], "上次：未验证")
        pl = plan_of("bundle", False, verify={"state": "pass", "cmd": "x", "note": ""})
        self.assertEqual(block(pl["post"], "重跑验证")["lines"][1], {"n": "上次结果：通过"})

    def test_tool_context(self):
        cases = (
            ("claude", ["claude"]),
            ("codex", ["codex"]),
            ("anti", ["agy"]),
            ("both", ["claude", "codex"]),
            ("claude+anti", ["claude", "agy"]),
            ("codex+anti", ["codex", "agy"]),
            ("all", ["claude", "codex", "agy"]),
        )
        for tool, want in cases:
            pl = plan_of("bundle", False, tool=tool)
            ctx = block(pl["post"], "开新 Session 接上进度")["lines"]
            self.assertEqual(no_notes(ctx), ['cd "<Mac 上的项目路径>"'] + want, tool)
            notes = [l["n"] for l in ctx if isinstance(l, dict)]
            self.assertEqual(any("这是新 Session。进入后说「接棒」" in n for n in notes), "claude" in want)
            self.assertEqual(any("Codex 开新线程" in n for n in notes), "codex" in want)
            self.assertEqual(any("Antigravity 开新会话" in n for n in notes), "agy" in want)
        pl = plan_of("bundle", False, tool="both")
        self.assertEqual(len(block(pl["post"], "开新 Session 接上进度")["lines"]), 5)


class Checks(unittest.TestCase):
    def test_pre_post_keys(self):
        pl = plan_of("bundle", True)
        self.assertEqual([c[0] for c in pl["preChecks"]],
                         ["stop", "summary", "sessions", "desk", "carry", "stash", "secrets"])
        self.assertEqual([c[0] for c in pl["postChecks"]],
                         ["clean", "pull", "login", "context", "verify", "desk"])
        self.assertTrue(all(len(c) == 3 for c in pl["preChecks"] + pl["postChecks"]))
        self.assertEqual(pl["postChecks"][0][1], "确认 Mac 的工作区是干净的")
        self.assertEqual(pl["preChecks"][2][1], "核对留在 Windows 的 Session")

    def test_all_keys(self):
        s = {"tool": "claude", "device": "a", "kind": "local", "carry": "transcript", "sessionId": "x"}
        pl = plan_of("bundle", True, sessions=[s], worktree="w", config=make_config(mac_drop="/d"),
                     reg={"claudeLocal": {"on": {"a": True}}})
        self.assertEqual([c[0] for c in pl["preChecks"]],
                         ["stop", "summary", "sessions", "desk", "carry", "stash", "extra", "secrets"])
        self.assertEqual([c[0] for c in pl["postChecks"]],
                         ["clean", "pull", "extra", "worktree", "login", "context", "verify", "desk"])
        self.assertEqual(pl["preChecks"][2][2], "已登记 1 个本地 Session。回到这台设备时可以原样恢复，记下它们的 ID。")

    def test_return_shape(self):
        pl = plan_of("bundle", True)
        self.assertEqual(sorted(pl), sorted(["F", "T", "miss", "pre", "post", "preChecks", "postChecks",
                                              "tr", "dirty", "WIP", "BR"]))
        self.assertEqual(pl["F"]["id"], "a")
        self.assertEqual(pl["T"]["id"], "b")
        self.assertEqual(pl["tr"], "bundle")

    def test_br_prefers_handoff_branch(self):
        p = make_project(branch="dev")
        p["handoff"] = make_handoff("bundle", False, branch="feature")
        self.assertEqual(plan.plan_for(p, make_config())["BR"], "feature")
        p["handoff"]["branch"] = ""
        self.assertEqual(plan.plan_for(p, make_config())["BR"], "dev")

    def test_pure(self):
        p = make_project(sessions=[{"tool": "claude", "device": "a", "kind": "local", "carry": "transcript", "sessionId": "x"}])
        p["handoff"] = make_handoff("bundle", True)
        cfg = make_config(mac_drop="/d")
        before = copy.deepcopy((p, cfg))
        plan.plan_for(p, cfg)
        self.assertEqual((p, cfg), before)


class TierBoard(unittest.TestCase):
    def test_with_remote(self):
        p = make_project(remote="origin", unpushed=3, dirty=2)
        b = plan.tier_board(p, make_config())
        self.assertEqual(sorted(b), ["extra", "git", "manual", "native"])
        self.assertEqual(b["git"], [["已提交的代码", "main → origin。还有 3 个提交没推送", False]])
        self.assertEqual(b["extra"], [["未提交改动", "2 个文件。WIP 分支、bundle 或补丁", False]])
        self.assertEqual(b["manual"], [["登录状态与凭据", "每台设备各自登录", True],
                                       ["运行中的进程、终端、未保存的编辑", "离开前收尾并保存", True]])
        self.assertEqual(b["native"], [])

    def test_unpushed_none_and_zero(self):
        for u in (None, 0):
            b = plan.tier_board(make_project(remote="origin", unpushed=u, dirty=0), make_config())
            self.assertEqual(b["git"][0][1], "main → origin")
            self.assertEqual(b["extra"], [])

    def test_no_remote(self):
        b = plan.tier_board(make_project(dirty=0), make_config())
        self.assertEqual(b["git"], [])
        self.assertEqual(b["extra"], [["已提交的代码", "main。没有远程，用 bundle 文件带过去", False]])

    def test_non_git(self):
        b = plan.tier_board(make_project(branch="", dirty=0), make_config())
        self.assertEqual(b["extra"], [["整个项目目录", "不是 Git 仓库，要整个复制", False]])
        self.assertEqual(b["git"], [])

    def test_sessions(self):
        S = [
            {"tool": "claude", "device": "a", "kind": "cloud", "title": "云", "sessionId": "c1"},
            {"tool": "codex", "device": "a", "kind": "remote", "title": "", "sessionId": "r1"},
            {"tool": "claude", "device": "b", "kind": "local", "title": "", "sessionId": ""},
            {"tool": "claude", "device": "a", "kind": "local", "carry": "transcript", "title": "转", "sessionId": "t"},
            {"tool": "anti", "device": "a", "kind": "local", "carry": "transcript", "title": "产物", "sessionId": "ag1"},
            {"tool": "anti", "device": "a", "kind": "local", "carry": "summary", "title": "会话", "sessionId": "ag2"},
            {"tool": "codex", "device": "a", "kind": "local", "carry": "summary", "title": "", "sessionId": "m"},
        ]
        b = plan.tier_board(make_project(sessions=S, branch="", dirty=0), make_config())
        self.assertEqual(b["native"], [
            ["Claude Code 云端 Session", "云", False],
            ["Codex Remote Control", "r1。仍在 Windows 上运行，那台设备要开着", False],
            ["Claude Code 本地 Session", "未命名 Session。本来就在 Mac", False]])
        self.assertEqual(b["extra"][0], ["Claude Code 本地 Session 的转录文件", "转。搬运转录是非官方做法", False])
        self.assertEqual(b["extra"][1], ["Antigravity 本地会话的 brain 目录", "产物。搬运产物与转录供查阅，非官方做法", False])
        self.assertEqual(b["manual"][0], ["Antigravity 本地 Session", "会话。读摘要开新 Session", False])
        self.assertEqual(b["manual"][1], ["Codex 本地 Session", "m。读摘要开新 Session", False])

    def test_registered_kinds(self):
        reg = {
            "claudeMd": {"on": {"a": True}, "note": ""},
            "ccSkills": {"on": {"a": True}, "note": "自定义说明"},
            "geminiMd": {"on": {"a": True}, "note": ""},
            "claudeLocal": {"on": {"a": True}, "note": ""},
            "envFile": {"on": {"a": True}},
            "mcpJson": {"on": {"b": True}},          # other device only: skipped
            "acctSkills": {"on": {"a": True}},       # global kind: skipped
        }
        b = plan.tier_board(make_project(remote="origin", dirty=0, reg=reg), make_config())
        self.assertEqual(b["git"][1:], [["CLAUDE.md", "项目指令。提交后随仓库到另一台设备。", False],
                                        [".claude/skills/", "自定义说明", False],
                                        ["GEMINI.md", "Antigravity 项目规则指令。提交后随仓库到另一台设备。", False]])
        self.assertEqual(b["extra"], [["CLAUDE.local.md", "个人本地指令，通常不进 Git。经中转目录复制。", False]])
        self.assertEqual(b["manual"][0][0], ".env 与本地密钥")
        b = plan.tier_board(make_project(remote="", dirty=0, reg=reg), make_config())
        self.assertEqual([x[0] for x in b["extra"]], ["已提交的代码", "CLAUDE.md", "CLAUDE.local.md", ".claude/skills/", "GEMINI.md"])
        self.assertEqual(b["extra"][1][1], "已提交的部分随 bundle 文件走")
        self.assertEqual(b["extra"][2][1], "个人本地指令，通常不进 Git。经中转目录复制。")
        self.assertEqual(b["extra"][3][1], "已提交的部分随 bundle 文件走")
        self.assertEqual(b["git"], [])

    def test_device_b_is_source(self):
        p = make_project(device="b", branch="main", remote="origin", reg={"claudeMd": {"on": {"b": True}}},
                         sessions=[{"tool": "claude", "device": "a", "kind": "local", "sessionId": "q", "title": "x"}])
        b = plan.tier_board(p, make_config())
        self.assertEqual(b["native"], [["Claude Code 本地 Session", "x。本来就在 Windows", False]])
        self.assertEqual(b["git"][1][0], "CLAUDE.md")


class SessionTiers(unittest.TestCase):
    def test_all(self):
        cfg = make_config()
        base = {"device": "a", "tool": "claude"}
        self.assertEqual(plan.session_tiers(dict(base, kind="cloud"), cfg), [["两台设备", "native", ""]])
        self.assertEqual(plan.session_tiers(dict(base, kind="remote"), cfg),
                         [["Windows", "native", "在这里运行"], ["Mac", "native", "远程操作"]])
        self.assertEqual(plan.session_tiers(dict(base, kind="local", carry="summary"), cfg),
                         [["Windows", "native", ""], ["Mac", "manual", "读摘要开新 Session"]])
        self.assertEqual(plan.session_tiers(dict(base, kind="local", carry="transcript"), cfg),
                         [["Windows", "native", ""], ["Mac", "extra", "搬运转录，非官方"]])
        self.assertEqual(plan.session_tiers({"device": "a", "tool": "anti", "kind": "local", "carry": "transcript"}, cfg),
                         [["Windows", "native", ""], ["Mac", "extra", "搬运 brain 目录供查阅，非官方"]])
        self.assertEqual(plan.session_tiers({"device": "b", "kind": "local"}, cfg)[0][0], "Mac")


class ResumeLines(unittest.TestCase):
    def setUp(self):
        self.p = make_project()

    def s(self, **kw):
        d = {"tool": "claude", "device": "a", "kind": "local", "sessionId": "", "cwd": ""}
        d.update(kw)
        return d

    def test_claude_local(self):
        self.assertEqual(plan.resume_lines(self.p, self.s(sessionId="abc")),
                         ['cd "D:\\developer\\animal-sim"', "claude --resume abc"])
        self.assertEqual(plan.resume_lines(self.p, self.s()),
                         ['cd "D:\\developer\\animal-sim"', "claude --continue"])

    def test_cwd_wins_and_missing_path(self):
        self.assertEqual(plan.resume_lines(self.p, self.s(cwd="C:\\x"))[0], 'cd "C:\\x"')
        self.assertEqual(plan.resume_lines(self.p, self.s(device="b")),
                         [{"n": "先进入启动这个 Session 的目录"}, "claude --continue"])

    def test_claude_remote(self):
        self.assertEqual(plan.resume_lines(self.p, self.s(kind="remote", sessionId="zz")),
                         ['cd "D:\\developer\\animal-sim"', "claude --remote-control"])

    def test_claude_cloud(self):
        self.assertEqual(plan.resume_lines(self.p, self.s(kind="cloud", sessionId="zz")),
                         ['cd "D:\\developer\\animal-sim"', "claude --teleport zz"])
        self.assertEqual(plan.resume_lines(self.p, self.s(kind="cloud")),
                         [{"n": "在 claude.ai/code 打开这个 Session"}])

    def test_codex(self):
        note = {"n": "用 Codex app 或 VS Code 扩展时，从线程列表打开同名线程"}
        self.assertEqual(plan.resume_lines(self.p, self.s(tool="codex", sessionId="u1")),
                         ['cd "D:\\developer\\animal-sim"', "codex resume u1", note])
        self.assertEqual(plan.resume_lines(self.p, self.s(tool="codex")),
                         ['cd "D:\\developer\\animal-sim"', "codex resume --last", note])
        # codex + remote still resumes via codex
        self.assertEqual(plan.resume_lines(self.p, self.s(tool="codex", kind="remote", sessionId="u"))[1], "codex resume u")

    def test_codex_cloud(self):
        self.assertEqual(plan.resume_lines(self.p, self.s(tool="codex", kind="cloud", sessionId="t9")),
                         [{"n": "在 ChatGPT 的 Codex 页面打开这个云端任务：t9"}])
        self.assertEqual(plan.resume_lines(self.p, self.s(tool="codex", kind="cloud")),
                         [{"n": "在 ChatGPT 的 Codex 页面打开这个云端任务"}])

    def test_anti(self):
        note = {"n": "用 Antigravity 桌面端或 IDE 时，从左侧会话列表打开同名会话"}
        self.assertEqual(plan.resume_lines(self.p, self.s(tool="anti", sessionId="7d5cf4ca")),
                         ['cd "D:\\developer\\animal-sim"', "agy --conversation 7d5cf4ca", note])
        self.assertEqual(plan.resume_lines(self.p, self.s(tool="anti")),
                         ['cd "D:\\developer\\animal-sim"', "agy --continue", note])
        self.assertEqual(plan.resume_lines(self.p, self.s(tool="anti", kind="cloud", sessionId="c9")),
                         [{"n": "在 Antigravity 打开这个云端会话：c9"}])


class DefaultTransportAndNew(unittest.TestCase):
    def test_default(self):
        self.assertEqual(plan.default_transport(make_project(transport="patch")), "patch")
        self.assertEqual(plan.default_transport(make_project(branch="")), "folder")
        self.assertEqual(plan.default_transport(make_project(remote="origin")), "remote")
        self.assertEqual(plan.default_transport(make_project()), "bundle")

    def test_new_handoff_shape(self):
        h = plan.new_handoff(make_project(dirty=2), make_config(), today=datetime.date(2026, 11, 17))
        self.assertEqual({k: h[k] for k in h if k != "createdAt"}, {
            "tag": "20261117-win", "from": "a", "to": "b", "transport": "bundle", "state": "prep",
            "pre": {}, "post": {}, "hadDirty": True, "branch": "main", "baseCommit": ""})
        self.assertRegex(h["createdAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

    def test_new_handoff_direction_and_clean(self):
        h = plan.new_handoff(make_project(device="b", dirty=0), make_config(), today=datetime.date(2026, 1, 2))
        self.assertEqual((h["tag"], h["from"], h["to"], h["hadDirty"]), ("20260102-mac", "b", "a", False))

    def test_default_today(self):
        h = plan.new_handoff(make_project(), make_config())
        self.assertTrue(h["tag"].startswith(datetime.date.today().strftime("%Y%m%d")))

    def test_same_os_slug(self):
        cfg = make_config()
        cfg["devices"][1]["os"] = "windows"
        h = plan.new_handoff(make_project(), cfg, today=datetime.date(2026, 11, 17))
        self.assertEqual(h["tag"], "20261117-win-a")

    def test_transport_fallbacks(self):
        cfg = make_config()
        d = datetime.date(2026, 11, 17)
        # requested wins when available
        self.assertEqual(plan.new_handoff(make_project(remote="o"), cfg, "patch", d)["transport"], "patch")
        self.assertEqual(plan.new_handoff(make_project(remote="o"), cfg, "remote", d)["transport"], "remote")
        # remote without a remote -> bundle
        self.assertEqual(plan.new_handoff(make_project(), cfg, "remote", d)["transport"], "bundle")
        # non-git: anything but folder -> folder
        for t in ("remote", "bundle", "patch", None):
            self.assertEqual(plan.new_handoff(make_project(branch=""), cfg, t, d)["transport"], "folder")
        # git project asking for folder -> bundle
        self.assertEqual(plan.new_handoff(make_project(), cfg, "folder", d)["transport"], "bundle")
        # project's own transport used when none requested
        self.assertEqual(plan.new_handoff(make_project(transport="patch"), cfg, None, d)["transport"], "patch")
        # unusable stored transport falls back
        self.assertEqual(plan.new_handoff(make_project(transport="remote"), cfg, None, d)["transport"], "bundle")
        self.assertEqual(plan.new_handoff(make_project(remote="o"), cfg, None, d)["transport"], "remote")


class Render(unittest.TestCase):
    def test_pre(self):
        pl = plan_of("remote", True, remote="origin")
        text = plan.render(pl, "pre", {"stop": True, "desk": False})
        lines = text.splitlines()
        self.assertEqual(lines[0], "[x] stop  让 Claude Code、Codex 和 Antigravity 停在一个完整回合")
        self.assertEqual(lines[1], "      后台任务、dev server 已结束，或者启动命令已写进交接备注。")
        self.assertIn("[ ] summary  让当前 Session 写下交接摘要", lines)
        self.assertIn("[ ] desk  在台账里更新最近任务、下一步动作、验证结果", lines)
        # empty extra gets no extra line
        i = lines.index("[ ] desk  在台账里更新最近任务、下一步动作、验证结果")
        self.assertTrue(lines[i + 1].startswith("[ ] carry"))
        self.assertIn("--- 看清现状 [只读] 在 Windows 的 PowerShell 里 ---", lines)
        self.assertIn("--- 把未提交改动停到 WIP 分支 [只改本机] 在 Windows 的 PowerShell 里 ---", lines)
        self.assertIn("--- 推送这条 WIP 分支 [写入远程，由用户确认后执行] 在 Windows 的 PowerShell 里 ---", lines)
        self.assertIn("# 只推这一条个人分支。不动 main，不用 --force", lines)
        self.assertIn("git push -u origin wip/handoff-20261117-win", lines)
        self.assertIn("  git add -A", lines)
        self.assertNotIn("取回代码和未提交改动", text)

    def test_post(self):
        pl = plan_of("bundle", True)
        text = plan.render(pl, "post", {"pull": True})
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("[ ] clean  确认 Mac 的工作区是干净的"))
        self.assertIn("[x] pull  取回代码和未提交改动", lines)
        self.assertIn("--- 从 bundle 取回 [只改本机] 在 Mac 的 zsh 里 ---", lines)
        self.assertNotIn("打成 bundle 文件", text)
        self.assertTrue(text.endswith("\n"))

    def test_default_checked(self):
        text = plan.render(plan_of("bundle", False), "pre")
        self.assertNotIn("[x]", text)


class MultiDevicePlan(unittest.TestCase):
    def setUp(self):
        self.cfg3 = {
            "devices": [
                {"id": "a", "name": "Win台式", "os": "windows", "drop": "D:\\drop", "host": "win", "deviceId": "dev_a"},
                {"id": "b", "name": "MacBook", "os": "mac", "drop": "/Users/b/drop", "host": "mbp", "deviceId": "dev_b"},
                {"id": "c", "name": "StudioMac", "os": "mac", "drop": "/Users/c/drop", "host": "studio", "deviceId": "dev_c"},
            ],
            "reg": {},
        }

    def test_three_device_os_slug(self):
        # Two macs (b, c) get disambiguated slugs while unique windows (a) stays 'win'
        self.assertEqual(plan.os_slug(self.cfg3["devices"][0], self.cfg3), "win")
        self.assertEqual(plan.os_slug(self.cfg3["devices"][1], self.cfg3), "mac-b")
        self.assertEqual(plan.os_slug(self.cfg3["devices"][2], self.cfg3), "mac-c")

        # Three distinct OSes all stay unprefixed
        cfg_distinct = copy.deepcopy(self.cfg3)
        cfg_distinct["devices"][2]["os"] = "linux"
        self.assertEqual(plan.os_slug(cfg_distinct["devices"][0], cfg_distinct), "win")
        self.assertEqual(plan.os_slug(cfg_distinct["devices"][1], cfg_distinct), "mac")
        self.assertEqual(plan.os_slug(cfg_distinct["devices"][2], cfg_distinct), "linux")

    def test_three_device_tier_board(self):
        sessions = [
            {"tool": "claude", "device": "b", "kind": "local", "title": "OnB", "sessionId": "sb"},
            {"tool": "claude", "device": "c", "kind": "local", "title": "OnC", "sessionId": "sc"},
        ]
        p = make_project(
            device="a",
            paths={"a": "D:\\p", "b": "/Users/b/p", "c": "/Users/c/p"},
            sessions=sessions,
            remote="origin",
            dirty=0,
        )
        # Explicit to_id="c" treats c's session as native and b's session as manual
        b_to_c = plan.tier_board(p, self.cfg3, to_id="c")
        self.assertEqual(b_to_c["native"], [["Claude Code 本地 Session", "OnC。本来就在 StudioMac", False]])
        self.assertEqual(b_to_c["manual"][0], ["Claude Code 本地 Session", "OnB。读摘要开新 Session", False])

        # Without to_id or handoff in 3-device setup, sessions on any non-source device show as native
        b_any = plan.tier_board(p, self.cfg3)
        self.assertEqual(
            b_any["native"],
            [
                ["Claude Code 本地 Session", "OnB。本来就在 MacBook", False],
                ["Claude Code 本地 Session", "OnC。本来就在 StudioMac", False],
            ],
        )

    def test_three_device_session_tiers(self):
        self.assertEqual(
            plan.session_tiers({"device": "a", "tool": "claude", "kind": "cloud"}, self.cfg3),
            [["全部已登录设备", "native", ""]],
        )
        self.assertEqual(
            plan.session_tiers({"device": "a", "tool": "claude", "kind": "remote"}, self.cfg3),
            [
                ["Win台式", "native", "在这里运行"],
                ["MacBook", "native", "远程操作"],
                ["StudioMac", "native", "远程操作"],
            ],
        )
        self.assertEqual(
            plan.session_tiers({"device": "a", "tool": "codex", "kind": "local", "carry": "summary"}, self.cfg3),
            [
                ["Win台式", "native", ""],
                ["MacBook", "manual", "读摘要开新 Session"],
                ["StudioMac", "manual", "读摘要开新 Session"],
            ],
        )
        self.assertEqual(
            plan.session_tiers({"device": "b", "tool": "anti", "kind": "local", "carry": "transcript"}, self.cfg3),
            [
                ["MacBook", "native", ""],
                ["Win台式", "extra", "搬运 brain 目录供查阅，非官方"],
                ["StudioMac", "extra", "搬运 brain 目录供查阅，非官方"],
            ],
        )

    def test_three_device_new_handoff_explicit_to_id_vs_ambiguity(self):
        p = make_project(
            device="a",
            paths={"a": "D:\\p", "b": "/Users/b/p", "c": "/Users/c/p"},
            dirty=1,
        )
        d = datetime.date(2026, 11, 18)

        # Omitting to_id with >1 other device raises LookupError
        with self.assertRaises(LookupError) as ctx:
            plan.new_handoff(p, self.cfg3, today=d)
        self.assertIn("b", str(ctx.exception))
        self.assertIn("c", str(ctx.exception))

        # Explicit to_id by alias and by deviceId both succeed
        h_c = plan.new_handoff(p, self.cfg3, transport="bundle", today=d, to_id="c")
        self.assertEqual(h_c["from"], "a")
        self.assertEqual(h_c["to"], "c")
        self.assertEqual(h_c["tag"], "20261118-win")

        h_c_did = plan.new_handoff(p, self.cfg3, transport="bundle", today=d, to_id="dev_c")
        self.assertEqual(h_c_did["to"], "c")

        # Same-OS source (b -> c) uses disambiguated os_slug in tag
        p_b = make_project(device="b", paths={"a": "D:\\p", "b": "/Users/b/p", "c": "/Users/c/p"}, dirty=0)
        h_b_to_c = plan.new_handoff(p_b, self.cfg3, today=d, to_id="c")
        self.assertEqual(h_b_to_c["tag"], "20261118-mac-b")
        self.assertEqual(h_b_to_c["to"], "c")

        # Plan generation routes to target c's path & drop dir
        p_b["handoff"] = h_b_to_c
        pl = plan.plan_for(p_b, self.cfg3)
        self.assertEqual(pl["F"]["id"], "b")
        self.assertEqual(pl["T"]["id"], "c")
        self.assertIn('cd "/Users/c/p"', block(pl["post"], "从 bundle 取回")["lines"])
        self.assertIn('git bundle verify "/Users/c/drop/animal-sim-20261118-mac-b.bundle"',
                      block(pl["post"], "从 bundle 取回")["lines"])

        # Unknown or self to_id raises error
        with self.assertRaises(LookupError):
            plan.new_handoff(p, self.cfg3, today=d, to_id="nope")
        with self.assertRaises(ValueError):
            plan.new_handoff(p, self.cfg3, today=d, to_id="a")

        # Single-device config cannot create handoff
        cfg1 = {"devices": [self.cfg3["devices"][0]], "reg": {}}
        with self.assertRaises(LookupError):
            plan.new_handoff(p, cfg1, today=d)


if __name__ == "__main__":
    unittest.main()
