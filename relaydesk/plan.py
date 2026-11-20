# -*- coding: utf-8 -*-
"""Plan / tier logic for relay-desk.

A faithful port of the JavaScript in relay-desk.html (TIER, TRANSPORT, VERIFY,
tierBoard, sessionTiers, resumeLines, defaultTransport, planFor and the handoff
creation in ACT.startHandoff).  Pure functions: no I/O, no subprocess.  All
commands appear only as strings to be printed, never executed.

Command-block lines are either a ``str`` (a command) or ``{"n": str}`` (an
annotation).
"""
import datetime
import re

TIER = {
    "native": {"label": "原生可恢复", "short": "原生", "desc": "工具自己把同一个 Session 原样打开，对话和上下文都在。"},
    "git": {"label": "可通过 Git 同步", "short": "Git", "desc": "提交后随仓库到另一台设备。带走的是文件，不含对话。"},
    "extra": {"label": "需要额外同步", "short": "额外", "desc": "在仓库之外或没提交的东西。靠中转目录、bundle 或补丁带过去。"},
    "manual": {"label": "仅能手动重建", "short": "重建", "desc": "带不走。到另一台设备重新登录、重启进程，或读摘要开新 Session。"},
}
TIER_ORDER = ["native", "git", "extra", "manual"]

TOOL_LABEL = {"claude": "Claude Code", "codex": "Codex", "anti": "Antigravity"}
TOOL_SETS = {
    "claude": ["claude"],
    "codex": ["codex"],
    "anti": ["anti"],
    "both": ["claude", "codex"],
    "claude+anti": ["claude", "anti"],
    "codex+anti": ["codex", "anti"],
    "all": ["claude", "codex", "anti"],
}

VERIFY = {"none": "未验证", "pass": "通过", "partial": "部分通过", "fail": "未通过"}

TRANSPORT = {
    "remote": ["推送个人 WIP 分支", "把未提交改动提交到一条个人 WIP 分支再推送。会写入远程，命令由你确认后执行；团队仓库慎用。"],
    "bundle": ["bundle 文件", "把分支和未提交改动打成一个文件放进中转目录。不碰任何远程，没有远程的项目用这个。"],
    "patch": ["补丁文件", "只把未提交改动导出成补丁。要求两台设备停在同一个提交上。"],
    "folder": ["复制整个目录", "目录不是 Git 仓库时用。整个文件夹经中转目录复制，默认排除 .env。"],
}

# Project-scope entries of KINDS in relay-desk.html (id, scope, label, tier, how).
KINDS = [
    {"id": "claudeMd", "scope": "project", "label": "CLAUDE.md", "tier": "git", "how": "项目指令。提交后随仓库到另一台设备。"},
    {"id": "claudeLocal", "scope": "project", "label": "CLAUDE.local.md", "tier": "extra", "how": "个人本地指令，通常不进 Git。经中转目录复制。"},
    {"id": "ccSkills", "scope": "project", "label": ".claude/skills/", "tier": "git", "how": "项目 Skills。提交后两台设备一致。"},
    {"id": "ccSettings", "scope": "project", "label": ".claude/settings.json", "tier": "git", "how": "共享的权限、hooks、启用的插件。提交前确认里面没有写死的令牌。"},
    {"id": "ccSettingsLocal", "scope": "project", "label": ".claude/settings.local.json", "tier": "manual", "how": "本机的权限放行记录，不提交。换设备后按提示重新放行。"},
    {"id": "ccCmdAgents", "scope": "project", "label": ".claude/commands/ 与 agents/", "tier": "git", "how": "项目内的自定义命令和子代理定义。"},
    {"id": "ccMemory", "scope": "project", "label": "Claude Code 自动记忆", "tier": "extra", "how": "按项目路径存在本机。两台设备路径不同，目录名也不同，复制时要放进对应目录。"},
    {"id": "mcpJson", "scope": "project", "label": ".mcp.json", "tier": "git", "how": "MCP 服务器定义可以提交。OAuth 登录和令牌在每台设备各自完成。"},
    {"id": "agentsMd", "scope": "project", "label": "AGENTS.md", "tier": "git", "how": "Codex 与 Antigravity 共用的项目指令。提交后随仓库走。"},
    {"id": "cxProject", "scope": "project", "label": ".codex/（项目内配置）", "tier": "git", "how": "项目里放了 Codex 配置或 Skills 时提交即可。先确认里面没有令牌。"},
    {"id": "cxWorkspace", "scope": "project", "label": "Codex 工作区与线程", "tier": "manual", "how": "线程保存在本机 app 的数据库里。另一台设备要重新打开文件夹、开新线程。"},
    {"id": "cxConn", "scope": "project", "label": "Codex 连接与接力信息", "tier": "manual", "how": "只登记名称和用途，比如哪台主机、哪个云环境。密钥和登录在每台设备各自配置。"},
    {"id": "geminiMd", "scope": "project", "label": "GEMINI.md", "tier": "git", "how": "Antigravity 项目规则指令。提交后随仓库到另一台设备。"},
    {"id": "agProject", "scope": "project", "label": ".agents/（工作区定制）", "tier": "git", "how": "Antigravity 项目内的 Skills、Rules、Plugins、hooks.json 与 skills.json。提交前确认无令牌。"},
    {"id": "agWorkspace", "scope": "project", "label": "Antigravity 工作区权限与设置", "tier": "manual", "how": "项目的沙盒开关、联网策略、目录放行只在本机生效，换设备后按需重新设定。"},
    {"id": "handoffMd", "scope": "project", "label": "HANDOFF.md", "tier": "git", "how": "交接摘要。帮新 Session 接上进度，不等于恢复原 Session。"},
    {"id": "worktree", "scope": "project", "label": "Git worktree 目录", "tier": "extra", "how": "分支随 Git 走，worktree 目录只在本机。另一台设备用 git worktree add 重建。"},
    {"id": "envFile", "scope": "project", "label": ".env 与本地密钥", "tier": "manual", "how": "不同步、不提交。每台设备各自配置，来源用密码管理器。"},
    {"id": "running", "scope": "project", "label": "运行中的进程与终端", "tier": "manual", "how": "带不走。把启动命令写进交接备注。"},
]


# ---------------------------------------------------------------- helpers

def tools_of(t):
    return list(TOOL_SETS.get(t) or ["claude"])


def dev(config, dev_id):
    devs = config["devices"]
    for d in devs:
        if d.get("id") == dev_id:
            return d
    if dev_id:
        for d in devs:
            if d.get("deviceId") == dev_id:
                return d
    return devs[0]


def other(config, dev_id):
    devs = config["devices"]
    for d in devs:
        if d.get("id") != dev_id:
            return d
    return devs[0]


def is_win(d):
    return d.get("os") == "windows"


def shell_of(d):
    return "PowerShell" if is_win(d) else ("zsh" if d.get("os") == "mac" else "bash")


def os_slug(d, config):
    s = "win" if is_win(d) else ("mac" if d.get("os") == "mac" else "linux")
    same_os_exists = any(
        x.get("id") != d.get("id") and x.get("os") == d.get("os")
        for x in config["devices"]
    )
    return s + "-" + d["id"] if same_os_exists else s


def join_p(d, base, name):
    return re.sub(r"[\\/]+$", "", base) + ("\\" if is_win(d) else "/") + name


def enc_path(p):
    return re.sub(r"[^A-Za-z0-9]", "-", str(p or ""))


def enc_of(p, d):
    if p.startswith("<"):
        return "<%s 上项目路径的编码>" % d["name"]
    return enc_path(p)


def home_p(d, rel):
    if is_win(d):
        return "$env:USERPROFILE\\" + "\\".join(rel)
    return "$HOME/" + "/".join(rel)


def file_slug(s):
    t = re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or ""))
    t = re.sub(r"^-+|-+$", "", t)
    return t or "project"


def cp_file(d, src, dst):
    if is_win(d):
        return 'Copy-Item "%s" "%s"' % (src, dst)
    return 'cp "%s" "%s"' % (src, dst)


def mk_dir(d, p):
    if is_win(d):
        return 'New-Item -ItemType Directory -Force "%s" | Out-Null' % p
    return 'mkdir -p "%s"' % p


def cp_dir(d, src, dst, o=None):
    o = o or {}
    xd = o.get("xd") or []
    xf = o.get("xf") or []
    if is_win(d):
        return ('robocopy "%s" "%s" /E' % (src, dst)
                + (" /XD " + " ".join(xd) if xd else "")
                + (" /XF " + " ".join(xf) if xf else ""))
    return ("rsync -a "
            + "".join('--exclude "%s/" ' % x for x in xd)
            + "".join('--exclude "%s" ' % x for x in xf)
            + '"%s/" "%s/"' % (src, dst))


def guard(d, first, rest):
    def ind(r):
        return "  " + r if isinstance(r, str) else r
    if is_win(d):
        return [first, "if ($?) {"] + [ind(r) for r in rest] + ["}"]
    return [first + " && {"] + [ind(r) for r in rest] + ["}"]


def reg_on(p, k, dev_id):
    r = (p.get("reg") or {}).get(k)
    if not r:
        return False
    on = r.get("on")
    return bool(on and on.get(dev_id))


def _pos(n):
    """JS `n > 0` for int / None."""
    return isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0


def _verify_label(state):
    return VERIFY.get(state, "undefined")


# ---------------------------------------------------------------- tiers

def tier_board(project, config, to_id=None):
    p = project
    frm = dev(config, p["device"])
    if to_id:
        to = dev(config, to_id)
    elif isinstance(p.get("handoff"), dict) and p["handoff"].get("to"):
        to = dev(config, p["handoff"]["to"])
    elif len([d for d in config["devices"] if d.get("id") != frm["id"]]) == 1:
        to = other(config, frm["id"])
    else:
        to = None
    B = {"native": [], "git": [], "extra": [], "manual": []}
    for s in p.get("sessions") or []:
        t = TOOL_LABEL.get(s.get("tool"), "Claude Code")
        name = s.get("title") or s.get("sessionId") or "未命名 Session"
        kind = s.get("kind")
        s_dev_raw = s.get("device")
        s_in_cfg = any(
            d.get("id") == s_dev_raw or (s_dev_raw and d.get("deviceId") == s_dev_raw)
            for d in config["devices"]
        )
        s_dev = dev(config, s_dev_raw) if s_in_cfg else None
        if kind == "cloud":
            B["native"].append([t + " 云端 Session", name, False])
        elif kind == "remote":
            B["native"].append([t + " Remote Control",
                                name + "。仍在 " + dev(config, s_dev_raw)["name"] + " 上运行，那台设备要开着", False])
        elif to is not None and s_dev is not None and s_dev["id"] == to["id"]:
            B["native"].append([t + " 本地 Session", name + "。本来就在 " + to["name"], False])
        elif to is None and s_dev is not None and s_dev["id"] != frm["id"]:
            B["native"].append([t + " 本地 Session", name + "。本来就在 " + s_dev["name"], False])
        elif s.get("carry") == "transcript":
            if s.get("tool") == "anti":
                B["extra"].append(["Antigravity 本地会话的 brain 目录", name + "。搬运产物与转录供查阅，非官方做法", False])
            else:
                B["extra"].append([t + " 本地 Session 的转录文件", name + "。搬运转录是非官方做法", False])
        else:
            B["manual"].append([t + " 本地 Session", name + "。读摘要开新 Session", False])
    if p.get("branch"):
        if p.get("remote"):
            tail = ""
            if _pos(p.get("unpushed")):
                tail = "。还有 " + str(p["unpushed"]) + " 个提交没推送"
            B["git"].append(["已提交的代码", p["branch"] + " → " + p["remote"] + tail, False])
        else:
            B["extra"].append(["已提交的代码", p["branch"] + "。没有远程，用 bundle 文件带过去", False])
    else:
        B["extra"].append(["整个项目目录", "不是 Git 仓库，要整个复制", False])
    if _pos(p.get("dirty")):
        B["extra"].append(["未提交改动", str(p["dirty"]) + " 个文件。WIP 分支、bundle 或补丁", False])
    for k in KINDS:
        if k["scope"] == "project" and reg_on(p, k["id"], frm["id"]):
            no_remote_git = k["tier"] == "git" and not p.get("remote")
            tier = "extra" if no_remote_git else k["tier"]
            note = (p["reg"][k["id"]].get("note") or k["how"])
            B[tier].append([k["label"], "已提交的部分随 bundle 文件走" if no_remote_git else note, False])
    B["manual"].append(["登录状态与凭据", "每台设备各自登录", True])
    B["manual"].append(["运行中的进程、终端、未保存的编辑", "离开前收尾并保存", True])
    return B


def session_tiers(session, config):
    s = session
    home = dev(config, s.get("device"))
    others = [d for d in config["devices"] if d.get("id") != home["id"]]
    kind = s.get("kind")
    if kind == "cloud":
        return [["两台设备" if len(config["devices"]) == 2 else "全部已登录设备", "native", ""]]
    if kind == "remote":
        return [[home["name"], "native", "在这里运行"]] + [[o["name"], "native", "远程操作"] for o in others]
    tx = s.get("carry") == "transcript"
    extra_note = "搬运 brain 目录供查阅，非官方" if s.get("tool") == "anti" else "搬运转录，非官方"
    return [[home["name"], "native", ""]] + [
        [o["name"], "extra" if tx else "manual", extra_note if tx else "读摘要开新 Session"]
        for o in others
    ]


def resume_lines(project, session):
    p, s = project, session
    path = s.get("cwd") or (p.get("paths") or {}).get(s.get("device")) or ""
    out = ['cd "%s"' % path] if path else [{"n": "先进入启动这个 Session 的目录"}]
    sid = s.get("sessionId")
    if s.get("kind") == "cloud":
        if s.get("tool") == "codex":
            return [{"n": "在 ChatGPT 的 Codex 页面打开这个云端任务" + ("：" + sid if sid else "")}]
        if s.get("tool") == "anti":
            return [{"n": "在 Antigravity 打开这个云端会话" + ("：" + sid if sid else "")}]
        if sid:
            return out + ["claude --teleport " + sid]
        return [{"n": "在 claude.ai/code 打开这个 Session"}]
    if s.get("tool") == "codex":
        return out + ["codex resume " + sid if sid else "codex resume --last",
                      {"n": "用 Codex app 或 VS Code 扩展时，从线程列表打开同名线程"}]
    if s.get("tool") == "anti":
        return out + ["agy --conversation " + sid if sid else "agy --continue",
                      {"n": "用 Antigravity 桌面端或 IDE 时，从左侧会话列表打开同名会话"}]
    if s.get("kind") == "remote":
        return out + ["claude --remote-control"]
    return out + ["claude --resume " + sid if sid else "claude --continue"]


# ---------------------------------------------------------------- handoff

def default_transport(project):
    p = project
    if p.get("transport"):
        return p["transport"]
    if not p.get("branch"):
        return "folder"
    return "remote" if p.get("remote") else "bundle"


def new_handoff(project, config, transport=None, today=None, to_id=None):
    c = project
    frm = dev(config, c["device"])
    if to_id:
        to = next(
            (d for d in config["devices"] if d.get("id") == to_id or (to_id and d.get("deviceId") == to_id)),
            None,
        )
        if to is None:
            raise LookupError("找不到目标设备：%s" % to_id)
        if to.get("id") == frm.get("id"):
            raise ValueError("目标设备不能与来源设备相同：%s" % to_id)
    else:
        others = [d for d in config["devices"] if d.get("id") != frm["id"]]
        if len(others) == 1:
            to = others[0]
        elif len(others) == 0:
            raise LookupError("台账里还没有其他可接力的目标设备，请先添加或配对设备。")
        else:
            raise LookupError("存在多个可用目标设备（%s），请用 --to 明确指定接棒设备。" % "、".join(d["id"] for d in others))
    avail = ["remote", "bundle", "patch"] if c.get("branch") else ["folder"]
    tr = transport or default_transport(c)
    if tr not in avail or (tr == "remote" and not c.get("remote")):
        tr = "bundle" if c.get("branch") else "folder"
    if today is None:
        today = datetime.date.today()
    now = datetime.datetime.now(datetime.timezone.utc)
    created = now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)
    return {
        "tag": today.strftime("%Y%m%d") + "-" + os_slug(frm, config),
        "from": frm["id"], "to": to["id"], "transport": tr, "state": "prep",
        "pre": {}, "post": {},
        "hadDirty": _pos(c.get("dirty")),
        "branch": c.get("branch", ""), "baseCommit": "", "createdAt": created,
    }


def plan_for(project, config):
    p = project
    h = p["handoff"]
    F = dev(config, h["from"])
    T = dev(config, h["to"])
    paths = p.get("paths") or {}
    miss = []

    PF = paths.get(F["id"])
    if not PF:
        miss.append(F["name"] + " 上的项目路径")
        PF = "<%s 上的项目路径>" % F["name"]
    PT = paths.get(T["id"])
    if not PT:
        miss.append(T["name"] + " 上的项目路径")
        PT = "<%s 上的项目路径>" % T["name"]

    BR = h.get("branch") or p.get("branch") or "main"
    WIP = "wip/handoff-" + h["tag"]
    base = file_slug(p.get("name")) + "-" + h["tag"]
    dirty = bool(h.get("hadDirty"))
    tr = h["transport"]
    m = re.search(r"[A-Za-z0-9._-]+", str(p.get("remote") or ""))
    remote = m.group(0) if m else "origin"

    extras = []
    if reg_on(p, "claudeLocal", F["id"]):
        extras.append({"k": "local", "label": "CLAUDE.local.md"})
    if reg_on(p, "ccMemory", F["id"]):
        extras.append({"k": "memory", "label": "Claude Code 自动记忆"})
    for s in p.get("sessions") or []:
        if (s.get("device") == F["id"] and s.get("kind") == "local" and s.get("carry") == "transcript"
                and s.get("sessionId")):
            if s.get("tool") == "anti":
                extras.append({"k": "ag_tx", "s": s, "label": "Antigravity brain：" + (s.get("title") or s["sessionId"])})
            elif s.get("tool") != "codex":
                extras.append({"k": "tx", "s": s, "label": "转录文件：" + (s.get("title") or s["sessionId"])})

    need_drop = tr != "remote" or len(extras) > 0
    DF = F.get("drop") or ""
    DT = T.get("drop") or ""
    if need_drop:
        if not DF:
            miss.append(F["name"] + " 的中转目录")
            DF = "<%s 的中转目录>" % F["name"]
        if not DT:
            miss.append(T["name"] + " 的中转目录")
            DT = "<%s 的中转目录>" % T["name"]

    whereF = "在 " + F["name"] + " 的 " + shell_of(F) + " 里"
    whereT = "在 " + T["name"] + " 的 " + shell_of(T) + " 里"

    def grep(d):
        if is_win(d):
            return ("git ls-files --others --modified --exclude-standard | Select-String -Pattern "
                    r"'\.env|auth\.json|credentials|secret|token|\.pem|\.key$|id_rsa'")
        return ("git ls-files --others --modified --exclude-standard | grep -Ei "
                r"'\.env|auth\.json|credentials|secret|token|\.pem|\.key$|id_rsa'")

    pre = []
    post = []
    git = tr != "folder"

    # ---- before leaving
    if git:
        look = ['cd "%s"' % PF, "git status --short --branch", "git stash list", "git worktree list"]
        if p.get("remote"):
            look.append({"n": "下面这条列出还没推送的提交。分支没有上游时会报错，可以忽略"})
            look.append('git log --oneline "@{u}..HEAD"')
        pre.append({"title": "看清现状", "kind": "read", "where": whereF, "lines": look})
        pre.append({"title": "凭据检查", "kind": "read", "where": whereF,
                    "lines": ['cd "%s"' % PF, {"n": "有输出就先把这些文件加进 .gitignore 或移出仓库，再往下做"}, grep(F)]})
    wip_lines = guard(F, "git switch -c " + WIP,
                      ["git add -A", 'git commit -m "WIP handoff %s (do not merge)"' % h["tag"], "git switch " + BR])
    if tr == "remote":
        if dirty:
            pre.append({"title": "把未提交改动停到 WIP 分支", "kind": "local", "where": whereF,
                        "lines": ['cd "%s"' % PF] + wip_lines + [{"n": "做完后本机工作区是干净的，改动在 " + WIP + " 上"}]})
            pre.append({"title": "推送这条 WIP 分支", "kind": "remote", "where": whereF,
                        "lines": ['cd "%s"' % PF, {"n": "只推这一条个人分支。不动 " + BR + "，不用 --force"},
                                  "git push -u %s %s" % (remote, WIP)]})
        else:
            pre.append({"title": "推送还没推的提交", "kind": "remote", "where": whereF,
                        "lines": ['cd "%s"' % PF, {"n": "上面看到有未推送的提交时才需要。按这个仓库的规矩来，不用 --force"},
                                  "git push %s %s" % (remote, BR)]})
    elif tr == "bundle":
        f = join_p(F, DF, base + ".bundle")
        lines = ['cd "%s"' % PF]
        if dirty:
            lines.extend(wip_lines)
        lines.append('git bundle create "%s" %s' % (f, BR) + (" " + WIP if dirty else ""))
        lines.append('git bundle verify "%s"' % f)
        pre.append({"title": "打成 bundle 文件", "kind": "local", "where": whereF, "lines": lines})
    elif tr == "patch":
        f = join_p(F, DF, base + ".patch")
        pre.append({"title": "导出补丁", "kind": "local", "where": whereF,
                    "lines": ['cd "%s"' % PF, {"n": "记下这条输出，它是补丁的基底提交"}, "git rev-parse HEAD",
                              {"n": "让没跟踪的新文件也进补丁。会清掉已暂存的标记，不丢内容"}, "git add -A -N",
                              'git diff HEAD --binary --output="%s"' % f, "git reset -q"]})
    else:
        pre.append({"title": "复制整个目录", "kind": "local", "where": whereF,
                    "lines": [{"n": "默认排除 .env。中转目录是网盘时，再看一眼有没有别的密钥文件"},
                              cp_dir(F, PF, join_p(F, DF, base), {"xf": [".env"]})]})

    exF = []
    exT = []
    for x in extras:
        if x["k"] == "local":
            exF.append(cp_file(F, join_p(F, PF, "CLAUDE.local.md"), join_p(F, DF, base + "-CLAUDE.local.md")))
            exT.append(cp_file(T, join_p(T, DT, base + "-CLAUDE.local.md"), join_p(T, PT, "CLAUDE.local.md")))
        elif x["k"] == "memory":
            tm = home_p(T, [".claude", "projects", enc_of(PT, T), "memory"])
            exF.append(cp_dir(F, home_p(F, [".claude", "projects", enc_of(PF, F), "memory"]),
                              join_p(F, DF, base + "-memory")))
            exT.extend([{"n": "目录名是项目路径的编码：字母数字以外的字符都换成 -"}, mk_dir(T, tm),
                        cp_dir(T, join_p(T, DT, base + "-memory"), tm)])
        elif x["k"] == "ag_tx":
            s = x["s"]
            sid = s["sessionId"]
            sd = home_p(F, [".gemini", "antigravity", "brain", sid])
            td = home_p(T, [".gemini", "antigravity", "brain", sid])
            exF.append(cp_dir(F, sd, join_p(F, DF, base + "-anti-brain-" + sid)))
            exT.extend([{"n": "非官方做法。只搬运产物与 transcript.jsonl 供新会话查阅，不会恢复原会话索引；里面的路径仍指向 " + F["name"]},
                        mk_dir(T, td),
                        cp_dir(T, join_p(T, DT, base + "-anti-brain-" + sid), td)])
        else:
            s = x["s"]
            sf = home_p(F, [".claude", "projects", enc_of(s.get("cwd") or PF, F), s["sessionId"] + ".jsonl"])
            td = home_p(T, [".claude", "projects", enc_of(PT, T)])
            exF.append(cp_file(F, sf, join_p(F, DF, s["sessionId"] + ".jsonl")))
            exT.extend([{"n": "非官方做法。转录里的绝对路径仍指向 " + F["name"] + "，先拿不重要的 Session 试"},
                        mk_dir(T, td),
                        cp_file(T, join_p(T, DT, s["sessionId"] + ".jsonl"), join_p(T, td, s["sessionId"] + ".jsonl")),
                        'cd "%s"' % PT, "claude --resume " + s["sessionId"]])
    if exF:
        pre.append({"title": "带走额外同步的文件", "kind": "local", "where": whereF, "lines": exF})

    # ---- after arriving
    if git:
        post.append({"title": "确认这边是干净的", "kind": "read", "where": whereT,
                     "lines": ['cd "%s"' % PT, {"n": "应当没有未提交改动。有的话先处理，见「冲突记录」里的命令"},
                               "git status --short --branch"]})

    def apply_wip(ref):
        if is_win(T):
            inner = ["git cherry-pick -n " + ref, "if ($?) { git reset -q }"]
        else:
            inner = ["git cherry-pick -n %s && git reset -q" % ref]
        return guard(T, "git switch " + BR, guard(T, 'git merge --ff-only "%s~1"' % ref, inner))

    if tr == "remote":
        ref = remote + "/" + WIP
        lines = ['cd "%s"' % PT, "git fetch " + remote]
        if dirty:
            lines.append({"n": "先把 " + BR + " 快进到离开时的提交，再把未提交改动套回工作区"})
            lines.extend(apply_wip(ref))
        else:
            lines.extend(guard(T, "git switch " + BR, ["git pull --ff-only"]))
        lines.append("git status --short --branch")
        lines.append({"n": "提示找不到分支或有多个远程同名时：git switch -c " + BR + " --track " + remote + "/" + BR})
        post.append({"title": "取回代码" + ("和未提交改动" if dirty else ""), "kind": "local", "where": whereT, "lines": lines})
    elif tr == "bundle":
        f = join_p(T, DT, base + ".bundle")
        lines = ['cd "%s"' % PT, 'git bundle verify "%s"' % f,
                 'git fetch "%s" "refs/heads/*:refs/remotes/handoff/*"' % f]
        if dirty:
            lines.extend(apply_wip("handoff/" + WIP))
        else:
            lines.extend(guard(T, "git switch " + BR, ["git merge --ff-only handoff/" + BR]))
        lines.append("git status --short --branch")
        lines.append({"n": T["name"] + " 上还没有这个仓库时：git clone -b " + BR + ' "' + f + '" "' + PT + '"'})
        post.append({"title": "从 bundle 取回", "kind": "local", "where": whereT, "lines": lines})
    elif tr == "patch":
        f = join_p(T, DT, base + ".patch")
        base_commit = h.get("baseCommit")
        post.append({"title": "套用补丁", "kind": "local", "where": whereT,
                     "lines": ['cd "%s"' % PT,
                               {"n": "这条输出必须等于离开时记下的基底提交" + ("：" + base_commit if base_commit else "")},
                               "git rev-parse HEAD"]
                     + guard(T, 'git apply --check "%s"' % f, ['git apply "%s"' % f])
                     + ["git status --short"]})
    else:
        post.append({"title": "放回整个目录", "kind": "local", "where": whereT,
                     "lines": [{"n": "会覆盖 " + T["name"] + " 上同名的旧文件。那边有自己的改动时先备份"},
                               cp_dir(T, join_p(T, DT, base), PT)]})
    if exT:
        post.append({"title": "放回额外同步的文件", "kind": "local", "where": whereT, "lines": exT})
    if p.get("worktree"):
        post.append({"title": "重建 worktree", "kind": "local", "where": whereT,
                     "lines": ['cd "%s"' % PT,
                               {"n": "原来在 " + F["name"] + " 的 " + p["worktree"] + "。目录和分支换成这台设备上要用的"},
                               "git worktree list"]})
    tools = tools_of(p.get("tool"))
    ctx = ['cd "%s"' % PT]
    if "claude" in tools:
        ctx.append({"n": "这是新 Session。进入后说「接棒」，它会读 HANDOFF.md 和台账"})
        ctx.append("claude")
    if "codex" in tools:
        ctx.append({"n": "Codex 开新线程。说「接棒」，它会读 AGENTS.md、HANDOFF.md 和台账"})
        ctx.append("codex")
    if "anti" in tools:
        ctx.append({"n": "Antigravity 开新会话（桌面端、IDE 或终端 agy）。说「接棒」，它会读 GEMINI.md、AGENTS.md、HANDOFF.md 和台账"})
        ctx.append("agy")
    post.append({"title": "开新 Session 接上进度", "kind": "local", "where": whereT, "lines": ctx})
    verify = p.get("verify") or {}
    if verify.get("cmd"):
        post.append({"title": "重跑验证", "kind": "local", "where": whereT,
                     "lines": ['cd "%s"' % PT,
                               {"n": "上次结果：" + _verify_label(verify.get("state"))
                                + ("。" + verify["note"] if verify.get("note") else "")},
                               verify["cmd"]]})

    n_sess = len([s for s in (p.get("sessions") or []) if s.get("device") == F["id"] and s.get("kind") == "local"])
    extra_label = "、".join(x["label"] for x in extras)
    if git:
        if dirty:
            tail = ("和 %s 个未提交文件" % p.get("dirty")) if _pos(p.get("dirty")) else "和未提交改动"
        else:
            tail = ""
        carry_text = "按给出的命令带走代码" + tail
    else:
        carry_text = "按给出的命令复制整个目录"
    pre_checks = [
        ["stop", "让 Claude Code、Codex 和 Antigravity 停在一个完整回合", "后台任务、dev server 已结束，或者启动命令已写进交接备注。"],
        ["summary", "让当前 Session 写下交接摘要",
         "对代理说「接力」，它会写 HANDOFF.md 并更新台账。Claude Code 的 handoff 插件（「交接」）还会提交并推送，不想写远程的项目不要用它。这是摘要，替代不了原 Session。"],
        ["sessions", "核对留在 " + F["name"] + " 的 Session",
         "已登记 " + str(n_sess) + " 个本地 Session。回到这台设备时可以原样恢复，记下它们的 ID。"],
        ["desk", "在台账里更新最近任务、下一步动作、验证结果", ""],
        ["carry", carry_text,
         "补丁不会清空本机改动。接力完成后回到这台设备时先 git stash push -u，免得两边各改一份。" if tr == "patch" else ""],
        ["stash", "确认 stash 和别的 worktree 里没有落下的工作", "stash 不随 push、bundle、补丁走。"] if git else None,
        ["extra", "带走需要额外同步的本地状态", extra_label] if extras else None,
        ["secrets", "确认凭据没有混进去", ".env、auth.json、令牌、私钥不在提交、bundle、补丁和中转目录里。"],
    ]
    pre_checks = [c for c in pre_checks if c]
    post_checks = [
        ["clean", "确认 " + T["name"] + " 的工作区是干净的", "那边有自己的改动时先收起来，再套用带过来的内容。"] if git else None,
        ["pull", "取回代码" + ("和未提交改动" if dirty else ""),
         "按给出的命令做。出现冲突就停下，不强推、不覆盖，记一条冲突记录。"],
        ["extra", "放回额外同步的文件", extra_label] if extras else None,
        ["worktree", "重建 worktree", ""] if p.get("worktree") else None,
        ["login", "确认这台设备已登录", "Claude Code、Codex、Antigravity、MCP 连接器、SSH 密钥、.env 都在这台设备各自配置。"],
        ["context", "接上上下文", "开新 Session，对代理说「接棒」。云端 Session 可以直接打开原来那个。"],
        ["verify", "重跑验证，对照上次结果",
         "上次：" + _verify_label(verify.get("state")) + ("。" + verify["note"] if verify.get("note") else "")],
        ["desk", "在台账里更新未提交改动数和下一步动作", ""],
    ]
    post_checks = [c for c in post_checks if c]
    return {"F": F, "T": T, "miss": miss, "pre": pre, "post": post,
            "preChecks": pre_checks, "postChecks": post_checks,
            "tr": tr, "dirty": dirty, "WIP": WIP, "BR": BR}


# ---------------------------------------------------------------- render

_KIND_TAG = {"read": "只读", "local": "只改本机", "remote": "写入远程，由用户确认后执行"}


def render(plan, phase, checked=None):
    checked = checked or {}
    checks = plan["preChecks"] if phase == "pre" else plan["postChecks"]
    blocks = plan["pre"] if phase == "pre" else plan["post"]
    out = []
    for key, text, extra in checks:
        out.append("[%s] %s  %s" % ("x" if checked.get(key) else " ", key, text))
        if extra:
            out.append("      " + extra)
    for b in blocks:
        out.append("")
        head = "--- %s [%s]" % (b["title"], _KIND_TAG.get(b["kind"], "只改本机"))
        if b.get("where"):
            head += " " + b["where"]
        out.append(head + " ---")
        for line in b["lines"]:
            out.append(line if isinstance(line, str) else "# " + line["n"])
    return "\n".join(out) + "\n"
