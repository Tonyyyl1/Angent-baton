"""relay-desk command line (SPEC section 6).

scan / plan / server are imported lazily inside the handlers that need them.
This module never runs a git command and never touches the network.
"""
import argparse
import importlib
import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import ledger as L

TOOL = {
    "claude": "Claude Code",
    "codex": "Codex",
    "anti": "Antigravity",
    "both": "Claude Code + Codex",
    "claude+anti": "Claude Code + Antigravity",
    "codex+anti": "Codex + Antigravity",
    "all": "三个都用",
}
PROJECT_TOOLS = ["claude", "codex", "anti", "both", "claude+anti", "codex+anti", "all"]
SESSION_TOOLS = ["claude", "codex", "anti"]
STATUS = {"active": "进行中", "paused": "暂停", "blocked": "受阻", "done": "已完成"}
VERIFY = {"none": "未验证", "pass": "通过", "partial": "部分通过", "fail": "未通过"}
TIER_LABEL = {"native": "原生可恢复", "git": "可通过 Git 同步", "extra": "需要额外同步", "manual": "仅能手动重建"}
CONFLICT_KIND = ["both", "merge", "instr", "session", "other"]
NEED_DEVICE = "无法识别本机是哪台设备。请先运行 relay init --device a（或 b）。"


class CliError(Exception):
    def __init__(self, message, code=1):
        Exception.__init__(self, message)
        self.message = message
        self.code = code


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError("%s\n%s" % (message, self.format_usage().rstrip()))


# ---------------------------------------------------------------- small helpers

def out(s=""):
    sys.stdout.write(s + "\n")


def warn(s):
    sys.stderr.write(s + "\n")


def _mod(name):
    try:
        return importlib.import_module("relaydesk." + name)
    except ImportError as e:
        raise CliError("缺少模块 relaydesk.%s，无法执行这个命令：%s" % (name, e), 1)


def user_home():
    env = os.environ.get("RELAY_DESK_USER_HOME")
    return Path(env) if env else Path.home()


def _scan_kw():
    h = os.environ.get("RELAY_DESK_USER_HOME")
    return {"home": h} if h else {}


def ctx():
    local = L.load_local()
    return L.ledger_path(local), local


def need_here(led, local):
    h = L.here(led, local)
    if not h:
        raise CliError(NEED_DEVICE, 1)
    return h


def pick(led, args):
    return L.find_project(led, getattr(args, "project", None), os.getcwd())


def get_project(led, pid):
    for p in led["projects"]:
        if p["id"] == pid:
            return p
    raise LookupError("项目已不存在：%s" % pid)


def mutate(args, fn, key_attr="project"):
    """Locate the project under the lock, apply fn(ledger, project, here), return (ledger, project_id)."""
    path, local = ctx()
    box = {}

    def inner(led):
        p = L.find_project(led, getattr(args, key_attr, None), os.getcwd())
        box["id"] = p["id"]
        fn(led, p, L.here(led, local))

    led = L.update(path, inner)
    return led, box["id"]


def today():
    return date.today().isoformat()


def _paths_for(project, dev_id):
    paths = []
    p = (project.get("paths") or {}).get(dev_id)
    if p:
        paths.append(p)
    alt = (project.get("altPaths") or {}).get(dev_id) or []
    for a in alt:
        if a and a not in paths:
            paths.append(a)
    return paths


def _dev_label(config, dev_id):
    d = L.dev(config, dev_id)
    return "%s（%s）" % (d.get("name", ""), d["id"])


# ---------------------------------------------------------------- scan support

def collect_facts(project, here_id, scan):
    """Read-only gathering (no ledger lock held)."""
    info = {"warn": [], "facts": None, "presence": None, "sessions": None}
    path = (project.get("paths") or {}).get(here_id) or ""
    if not path:
        info["warn"].append("本机（%s）还没有登记路径，跳过。可用 set --path-here 补上。" % here_id)
        return info
    if not os.path.isdir(path):
        info["warn"].append("路径不存在，跳过：%s" % path)
        return info
    kw = _scan_kw()
    if project.get("device") == here_id:
        info["facts"] = scan.git_facts(path)
    info["presence"] = scan.presence(path, **kw)
    mine = [s for s in project.get("sessions", []) if s.get("device") == here_id and s.get("sessionId")]
    if mine:
        cwds = _paths_for(project, here_id)
        found = []
        found.extend(scan.claude_sessions(cwds, **kw))
        found.extend(scan.codex_sessions(cwds, **kw))
        if hasattr(scan, "anti_sessions"):
            found.extend(scan.anti_sessions(cwds, **kw))
        info["sessions"] = found
    return info


def _set_reg(reg, key, here_id, value, label, changes):
    entry = reg.get(key)
    if not isinstance(entry, dict):
        entry = {"on": {}, "note": ""}
        reg[key] = entry
    if not isinstance(entry.get("on"), dict):
        entry["on"] = {}
    old = entry["on"].get(here_id)
    entry["on"][here_id] = bool(value)
    if bool(old) != bool(value):
        changes.append("%s：%s → %s" % (label, "有" if old else "无", "有" if value else "无"))


def apply_facts(project, here_id, info):
    """Apply gathered facts to a project dict (inside the lock). Returns a list of change lines."""
    changes = []
    facts = info.get("facts")
    if facts is not None:
        def setf(k, v, label):
            if project.get(k) != v:
                changes.append("%s：%s → %s" % (label, project.get(k) if project.get(k) not in (None, "") else "空",
                                               v if v not in (None, "") else "空"))
                project[k] = v
        if facts.get("isGit"):
            setf("branch", facts.get("branch", ""), "分支")
            setf("dirty", facts.get("dirty", 0), "未提交")
            setf("unpushed", facts.get("unpushed"), "未推送")
            remotes = facts.get("remotes") or []
            cur = str(project.get("remote") or "")
            first = cur.split()[0] if cur.split() else ""
            if not (first and first in remotes):
                setf("remote", facts.get("remote", ""), "远程")
            g = dict(project.get("git") or {})
            if g.get("head") != facts.get("head", ""):
                changes.append("提交：%s → %s" % (g.get("head") or "空", facts.get("head", "") or "空"))
            g.update({"head": facts.get("head", ""), "lastCommit": facts.get("lastCommit", ""),
                      "worktrees": len(facts.get("worktrees") or []) or 1,
                      "scannedAt": L.now_iso(), "scannedOn": here_id})
            project["git"] = g
        else:
            setf("branch", "", "分支")
            setf("dirty", 0, "未提交")
            setf("unpushed", None, "未推送")
            setf("remote", "", "远程")
    pres = info.get("presence")
    if pres:
        reg = project.setdefault("reg", {})
        for k, v in pres.items():
            _set_reg(reg, k, here_id, v, "状态 %s" % k, changes)
    found = info.get("sessions")
    if found is not None:
        index = {}
        for s in found:
            index[(s.get("tool"), s.get("sessionId"))] = s
        for s in project.get("sessions", []):
            if s.get("device") != here_id:
                continue
            f = index.get((s.get("tool"), s.get("sessionId")))
            if not f:
                continue
            if f.get("title") and f["title"] != s.get("title"):
                changes.append("Session %s 标题：%s → %s" % (s.get("sessionId"), s.get("title") or "空", f["title"]))
                s["title"] = f["title"]
            if f.get("lastUsed") and f["lastUsed"] != s.get("lastUsed"):
                changes.append("Session %s 最近使用：%s → %s" % (s.get("sessionId"), s.get("lastUsed") or "空", f["lastUsed"]))
                s["lastUsed"] = f["lastUsed"]
    if changes:
        L.touch(project, here_id)
    return changes


def run_scan(path, local, ids, with_global=False):
    """Refresh local facts for the given project ids. Returns printable lines."""
    led = L.load(path)
    h = need_here(led, local)
    scan = _mod("scan")
    infos = {}
    lines = []
    for pid in ids:
        p = get_project(led, pid)
        infos[pid] = collect_facts(p, h, scan)
    gp = scan.global_presence(**_scan_kw()) if with_global else None

    def fn(l):
        for pid, info in infos.items():
            try:
                p = get_project(l, pid)
            except LookupError:
                continue
            for w in info["warn"]:
                lines.append("%s：%s" % (p["name"], w))
            ch = apply_facts(p, h, info)
            if ch:
                lines.append("%s：" % p["name"])
                lines.extend("  " + c for c in ch)
            elif not info["warn"]:
                lines.append("%s：没有变化" % p["name"])
        if gp is not None:
            reg = l["config"].setdefault("reg", {})
            ch = []
            for k, v in gp.items():
                _set_reg(reg, k, h, v, "全局 %s" % k, ch)
            if ch:
                lines.append("全局状态：")
                lines.extend("  " + c for c in ch)

    L.update(path, fn)
    return lines


# ---------------------------------------------------------------- commands: setup

def cmd_init(args):
    path, local = ctx()
    changed = False
    if args.ledger:
        local["ledger"] = os.path.abspath(args.ledger)
        changed = True
    if args.device:
        local["device"] = args.device
        changed = True
    led_path = L.ledger_path(local)
    existed = led_path.exists()
    if not existed:
        L.update(led_path, lambda led: None)
    led = L.load(led_path)
    if args.host:
        h = args.device or L.here(led, local)
        if not h:
            raise CliError(NEED_DEVICE, 1)
        if args.device is None:
            local["device"] = h
            changed = True

        def sethost(l):
            L.dev(l["config"], h)["host"] = L.hostname()
        led = L.update(led_path, sethost)
    if changed or not (L.local_dir() / "config.json").exists():
        L.save_local(local)
    me = L.here(led, local)
    out("已写入本机配置：%s" % (L.local_dir() / "config.json"))
    out("台账：%s%s" % (led_path, "" if existed else "（新建）"))
    out("本机设备：%s" % (_dev_label(led["config"], me) if me else "尚未确定，请加 --device a 或 --device b"))
    return 0


def _project_row(p, config):
    bits = [p["id"], L.dev(config, p["device"]).get("name", p["device"]), p["branch"] or "无分支",
            "未提交 %s" % p["dirty"], STATUS.get(p["status"], p["status"])]
    h = p.get("handoff")
    if h:
        bits.append("交接中（%s，%s → %s）" % (h.get("state", ""), h.get("from", ""), h.get("to", "")))
    if p.get("nextAction"):
        bits.append("下一步：%s" % p["nextAction"])
    return "  ".join(bits)


def _status_detail(led, p):
    cfg = led["config"]
    out("项目：%s（%s）" % (p["name"], p["id"]))
    out("所在设备：%s" % _dev_label(cfg, p["device"]))
    for d in cfg["devices"]:
        alt = (p.get("altPaths") or {}).get(d["id"]) or []
        out("路径 %s：%s%s" % (d["name"], p["paths"].get(d["id"]) or "（未填）",
                              ("  另有启动目录：" + "；".join(alt)) if alt else ""))
    out("分支：%s    远程：%s    未提交：%s    未推送：%s" % (
        p["branch"] or "无（不是 Git 仓库）", p["remote"] or "无", p["dirty"],
        "未知" if p["unpushed"] is None else p["unpushed"]))
    if p.get("worktree"):
        out("工作树：%s" % p["worktree"])
    g = p.get("git") or {}
    if g.get("lastCommit"):
        out("最近提交：%s（扫描于 %s，%s）" % (g["lastCommit"], g.get("scannedOn", ""), g.get("scannedAt", "")))
    out("工具：%s    状态：%s    传输方式：%s" % (TOOL.get(p["tool"], p["tool"]), STATUS.get(p["status"], p["status"]),
                                          p["transport"] or "未选"))
    v = p["verify"]
    out("验证：%s%s%s" % (VERIFY.get(v.get("state"), v.get("state")),
                       ("  命令：" + v["cmd"]) if v.get("cmd") else "", ("  " + v["note"]) if v.get("note") else ""))
    out("最近任务：%s" % (p["lastTask"] or "（空）"))
    out("下一步：%s" % (p["nextAction"] or "（空）"))
    out("备注：%s" % (p["note"] or "（空）"))
    h = p.get("handoff")
    if h:
        out("接力：标签 %s，%s → %s，%s，阶段 %s" % (h.get("tag"), h.get("from"), h.get("to"), h.get("transport"), h.get("state")))
    else:
        out("接力：没有进行中的接力")
    for c in p["conflicts"]:
        out("冲突：[%s] %s %s 保留：%s %s" % (c.get("kind"), c.get("date"), c.get("files") or "", c.get("kept") or "",
                                          ("解决：" + c["resolution"]) if c.get("resolution") else ""))
    if p["history"]:
        last = p["history"][-1]
        out("上次接力：%s（%s → %s，%s）" % (last.get("tag"), last.get("from"), last.get("to"), last.get("doneAt")))
    try:
        plan = _mod("plan")
    except CliError as e:
        warn(e.message)
        plan = None
    if plan is not None:
        out("")
        out("四级分层：")
        board = plan.tier_board(p, cfg)
        for tier in ("native", "git", "extra", "manual"):
            rows = board.get(tier) or []
            out("[%s]" % TIER_LABEL[tier])
            if not rows:
                out("  （无）")
            for r in rows:
                out("  - %s：%s" % (r[0], r[1]))
    out("")
    out("已登记的 Session（原样恢复只能在对应设备上做）：")
    if not p["sessions"]:
        out("  （无）")
    for s in p["sessions"]:
        out("  [%s] %s  id=%s  设备 %s  %s  带走方式 %s  最近使用 %s" % (
            s.get("tool"), s.get("title") or "（无标题）", s.get("sessionId") or "（未填）",
            s.get("device"), s.get("kind"), s.get("carry"), s.get("lastUsed")))
        if plan is not None:
            for line in plan.resume_lines(p, s):
                if isinstance(line, dict):
                    out("      # %s" % line.get("n", ""))
                else:
                    out("      %s" % line)


def cmd_status(args):
    path, local = ctx()
    led = L.load(path)
    if args.project is None:
        if args.json:
            out(json.dumps({"config": led["config"], "projects": led["projects"]}, ensure_ascii=False, indent=1))
            return 0
        if not led["projects"]:
            out("台账里还没有项目。用 add NAME --path PATH 登记一个。")
            return 0
        for p in led["projects"]:
            out(_project_row(p, led["config"]))
        return 0
    p = L.find_project(led, args.project, os.getcwd())
    if args.json:
        out(json.dumps(p, ensure_ascii=False, indent=1))
        return 0
    _status_detail(led, p)
    return 0


def cmd_scan(args):
    path, local = ctx()
    led = L.load(path)
    if args.all:
        ids = [p["id"] for p in led["projects"]]
        if not ids:
            out("台账里还没有项目。")
    else:
        ids = [L.find_project(led, args.project, os.getcwd())["id"]]
    for line in run_scan(path, local, ids, with_global=args.all):
        out(line)
    out("已完成扫描（%d 个项目）。" % len(ids))
    return 0


def cmd_add(args):
    path, local = ctx()
    box = {}

    def fn(led):
        h = need_here(led, local)
        pid = args.id or re.sub(r"[^A-Za-z0-9_.-]+", "-", args.name).strip("-")
        if not pid:
            pid = L.new_id("p_")
        if not re.match(r"^[A-Za-z0-9_.-]+$", pid):
            raise CliError("项目 id 只能含字母、数字、下划线、点和连字符：%s" % pid, 1)
        if any(p["id"] == pid for p in led["projects"]):
            raise CliError("已经有 id 为 %s 的项目。用 --id 指定另一个。" % pid, 1)
        o = L.other(led["config"], h)["id"]
        p = L.norm_project({"id": pid, "name": args.name, "device": h, "tool": args.tool or "claude",
                            "paths": {h: os.path.abspath(args.path), o: ""}}, led["config"])
        L.touch(p, h)
        led["projects"].append(p)
        box["id"] = pid

    L.update(path, fn)
    out("已登记项目 %s（id=%s）。" % (args.name, box["id"]))
    try:
        for line in run_scan(path, local, [box["id"]]):
            out(line)
    except CliError as e:
        warn("登记成功，但扫描没有做：" + e.message)
    return 0


_WIN_PATH = re.compile(r"^[A-Za-z]:[\/]")


def _git_root():
    root = os.environ.get("EXEPATH", "").replace("\\", "/").rstrip("/")
    for suffix in ("/usr/bin", "/bin"):
        if root.lower().endswith(suffix):
            root = root[:-len(suffix)]
            break
    return root


def fix_foreign_path(config, dev_id, value, label="路径"):
    """Validate/repair a path meant for another device. Returns (value, notice or None)."""
    if not value:
        return value, None
    d = L.dev(config, dev_id)
    if d.get("os") == "windows":
        if value.startswith("/"):
            raise CliError("%s：%s 是 Windows 设备，路径要写成 D:\\... 这样的形式，不能以 / 开头。" % (
                label, d.get("name", dev_id)), 1)
        return value, None
    if value.startswith("//"):
        return re.sub(r"^/{2,}", "/", value), None
    if value.startswith("/") or value.startswith("~"):
        return value, None
    if _WIN_PATH.match(value):
        root = _git_root()
        norm = value.replace("\\", "/")
        if root and (norm.lower() == root.lower() or norm.lower().startswith(root.lower() + "/")):
            fixed = norm[len(root):] or "/"
            return fixed, "已还原被 Git Bash 改写的路径：%s → %s" % (value, fixed)
    raise CliError("%s：%s 是 macOS/Linux 设备，路径必须以 / 开头（得到的是 %s）。在 Git Bash 里请把开头写成两个斜杠"
                   "（//Users/...），或在命令前加 MSYS_NO_PATHCONV=1。" % (label, d.get("name", dev_id), value), 1)


def cmd_set(args):
    changed = []

    def fn(led, p, h):
        o = None
        if args.path_here is not None or args.alt_path or args.path_other is not None:
            if not h:
                raise CliError(NEED_DEVICE, 1)
            o = L.other(led["config"], h)["id"]
        if args.path_here is not None:
            p["paths"][h] = os.path.abspath(args.path_here) if args.path_here else ""
            changed.append("本机路径")
        if args.path_other is not None:
            val, notice = fix_foreign_path(led["config"], o, args.path_other, "--path-other")
            if notice:
                out(notice)
            p["paths"][o] = val
            changed.append("另一台设备的路径")
        if args.alt_path:
            alt = p.setdefault("altPaths", {})
            lst = alt.setdefault(h, [])
            for a in args.alt_path:
                a = os.path.abspath(a)
                if a not in lst:
                    lst.append(a)
            changed.append("其他启动目录")
        for attr, key, label in (("tool", "tool", "工具"), ("status", "status", "状态"),
                                 ("transport", "transport", "传输方式"), ("worktree", "worktree", "工作树"),
                                 ("device", "device", "所在设备")):
            v = getattr(args, attr)
            if v is not None:
                p[key] = v
                changed.append(label)
        if not changed:
            raise CliError("没有要修改的内容。用 --path-here、--tool、--status 等选项指定。", 1)
        L.touch(p, h)

    led, pid = mutate(args, fn)
    out("已更新 %s：%s。" % (get_project(led, pid)["name"], "、".join(changed)))
    return 0


def cmd_note(args):
    changed = []

    def fn(led, p, h):
        for attr, key, label in (("last", "lastTask", "最近任务"), ("next", "nextAction", "下一步"),
                                 ("note", "note", "备注")):
            v = getattr(args, attr)
            if v is not None:
                p[key] = v
                changed.append(label)
        for attr, key, label in (("verify", "state", "验证结果"), ("verify_cmd", "cmd", "验证命令"),
                                 ("verify_note", "note", "验证说明")):
            v = getattr(args, attr)
            if v is not None:
                p["verify"][key] = v
                changed.append(label)
        if not changed:
            raise CliError("没有要记录的内容。用 --last、--next、--note、--verify 等选项。", 1)
        L.touch(p, h)

    led, pid = mutate(args, fn)
    out("已记录 %s：%s。" % (get_project(led, pid)["name"], "、".join(changed)))
    return 0


# ---------------------------------------------------------------- sessions

def _discover(project, h, tool=None):
    scan = _mod("scan")
    cwds = _paths_for(project, h)
    if not cwds:
        raise CliError("项目 %s 在本机没有登记路径，无法发现 Session。先用 set --path-here。" % project["name"], 1)
    kw = _scan_kw()
    found = []
    if tool in (None, "claude", "both", "claude+anti", "all"):
        found.extend(scan.claude_sessions(cwds, **kw))
    if tool in (None, "codex", "both", "codex+anti", "all"):
        found.extend(scan.codex_sessions(cwds, **kw))
    if tool in (None, "anti", "claude+anti", "codex+anti", "all") and hasattr(scan, "anti_sessions"):
        found.extend(scan.anti_sessions(cwds, **kw))
    found.sort(key=lambda s: s.get("mtime", 0), reverse=True)
    return found


def cmd_sessions(args):
    path, local = ctx()
    led = L.load(path)
    h = need_here(led, local)
    p = L.find_project(led, args.project, os.getcwd())
    found = _discover(p, h, args.tool)
    if args.tool in SESSION_TOOLS:
        found = [s for s in found if s.get("tool") == args.tool]
    reg = set(s.get("sessionId") for s in p["sessions"])
    if args.json:
        out(json.dumps([dict(s, registered=s.get("sessionId") in reg) for s in found], ensure_ascii=False, indent=1))
        return 0
    if not found:
        out("本机没有发现 %s 的 Session。" % p["name"])
        return 0
    for s in found:
        out("%s %s  %s  %s  %s" % ("*" if s.get("sessionId") in reg else " ", s.get("tool"), s.get("sessionId"),
                                   s.get("lastUsed", ""), s.get("title") or "（无标题）"))
    out("带 * 的已登记。共 %d 个。" % len(found))
    return 0


def cmd_session_add(args):
    path, local = ctx()
    led = L.load(path)
    h = need_here(led, local)
    p = L.find_project(led, args.project, os.getcwd())
    pt = p["tool"]
    default_tool = pt if pt in SESSION_TOOLS else ("codex" if pt == "codex+anti" else "claude")
    tool = args.tool or default_tool
    disc = None
    if args.id == "latest":
        found = [s for s in _discover(p, h, tool) if s.get("tool") == tool]
        if not found:
            raise CliError("本机没有发现 %s 的 %s Session，没法取 latest。" % (p["name"], TOOL[tool]), 1)
        disc = found[0]
        sid = disc["sessionId"]
    else:
        sid = args.id
        try:
            found = _discover(p, h, tool)
        except Exception:
            found = []
        for s in found:
            if s.get("tool") == tool and s.get("sessionId") == sid:
                disc = s
                break
        if disc is None:
            warn("提示：本机没有发现这个 Session，按你给的信息登记。")
    box = {}

    def fn(l, proj, here_id):
        cur = None
        for s in proj["sessions"]:
            if s.get("sessionId") == sid:
                cur = s
                break
        if cur is None:
            cur = {"id": L.new_id("s_"), "tool": tool, "device": h, "kind": "local", "carry": "summary",
                   "title": "", "sessionId": sid, "cwd": "", "lastUsed": "", "note": ""}
            proj["sessions"].append(cur)
            box["new"] = True
        cur["tool"] = tool
        if args.kind:
            cur["kind"] = args.kind
        if args.carry:
            cur["carry"] = args.carry
        title = args.title if args.title is not None else (disc.get("title") if disc else None)
        if title:
            cur["title"] = title
        cwd = args.cwd or (disc.get("cwd") if disc else None) or (cur.get("cwd") or (proj["paths"].get(h) or ""))
        cur["cwd"] = cwd
        cur["lastUsed"] = (disc.get("lastUsed") if disc else None) or cur.get("lastUsed") or today()
        L.touch(proj, h)
        box["s"] = cur

    mutate_args = argparse.Namespace(project=p["id"])
    mutate(mutate_args, fn)
    s = box["s"]
    out("已%s Session：%s（%s，%s）。" % ("登记" if box.get("new") else "更新", s.get("title") or sid, s["tool"], s["id"]))
    return 0


def cmd_session_rm(args):
    box = {}

    def fn(led, p, h):
        hit = [s for s in p["sessions"] if args.id in (s.get("id"), s.get("sessionId"))]
        if not hit:
            raise LookupError("项目 %s 里没有登记过 Session「%s」。" % (p["name"], args.id))
        p["sessions"] = [s for s in p["sessions"] if s not in hit]
        box["n"] = len(hit)
        L.touch(p, h)

    mutate(args, fn)
    out("已取消登记 %d 个 Session。" % box["n"])
    return 0


# ---------------------------------------------------------------- handoff flow

def _need_handoff(p):
    if not p.get("handoff"):
        raise CliError("项目 %s 没有进行中的接力。先运行 handoff。" % p["name"], 1)
    return p["handoff"]


def _print_plan(plan, led, p, phase):
    h = p["handoff"]
    pl = plan.plan_for(p, led["config"])
    out(plan.render(pl, phase, h.get(phase) or {}))


def cmd_handoff(args):
    path, local = ctx()
    led = L.load(path)
    h = need_here(led, local)
    p = L.find_project(led, args.project, os.getcwd())
    if p["device"] != h:
        raise CliError("项目 %s 的最新现场在 %s，不在本机（%s）。要从那台设备发起接力；"
                       "如果现场其实在这里，先 set --device %s。" % (p["name"], _dev_label(led["config"], p["device"]), h, h), 1)
    plan = _mod("plan")
    if not p.get("handoff"):
        try:
            for line in run_scan(path, local, [p["id"]]):
                out(line)
        except CliError as e:
            warn("没有扫描：" + e.message)
    box = {}

    def fn(l, proj, here_id):
        if proj.get("handoff"):
            box["existing"] = True
            return
        ho = plan.new_handoff(proj, l["config"], args.transport)
        if args.to:
            if args.to == ho.get("from"):
                raise CliError("--to 不能是出发的设备自己（%s）。" % args.to, 1)
            ho["to"] = args.to
        proj["handoff"] = ho
        L.touch(proj, h)

    led, pid = mutate(argparse.Namespace(project=p["id"]), fn)
    p = get_project(led, pid)
    ho = p["handoff"]
    out("接力：%s，%s → %s，传输方式 %s，标签 %s，阶段 %s" % (
        p["name"], _dev_label(led["config"], ho["from"]), _dev_label(led["config"], ho["to"]),
        ho["transport"], ho["tag"], ho["state"]))
    if box.get("existing"):
        out("（已有进行中的接力，没有重建。要重来请先 cancel。）")
    out("")
    _print_plan(plan, led, p, "pre")
    out("")
    out("已%s接力。做完一项就用 check %s pre KEY 勾选，全部做完再 depart。" % ("沿用" if box.get("existing") else "建立", p["id"]))
    return 0


def cmd_plan(args):
    path, local = ctx()
    led = L.load(path)
    p = L.find_project(led, args.project, os.getcwd())
    ho = _need_handoff(p)
    plan = _mod("plan")
    phase = args.phase or ("pre" if ho.get("state") == "prep" else "post")
    if args.json:
        pl = plan.plan_for(p, led["config"])
        out(json.dumps(dict(pl, phase=phase, checked=ho.get(phase) or {}), ensure_ascii=False, indent=1, default=str))
        return 0
    _print_plan(plan, led, p, phase)
    return 0


def cmd_check(args):
    path, local = ctx()
    led = L.load(path)
    p0 = L.find_project(led, args.project, os.getcwd())
    _need_handoff(p0)
    try:
        plan = _mod("plan")
        keys = [c[0] for c in (plan.plan_for(p0, led["config"]).get(args.phase + "Checks") or [])]
    except CliError:
        keys = None
    if keys is not None and args.key not in keys:
        raise CliError("清单里没有「%s」。可用的 KEY：%s。" % (args.key, ", ".join(keys)), 1)

    def fn(l, p, h):
        ho = _need_handoff(p)
        d = ho.setdefault(args.phase, {})
        if args.undo:
            d.pop(args.key, None)
        else:
            d[args.key] = True
        L.touch(p, h)

    mutate(args, fn)
    out("已%s：%s（%s）。" % ("取消勾选" if args.undo else "勾选", args.key, args.phase))
    return 0


def _unchecked(led, p, phase):
    try:
        plan = _mod("plan")
        pl = plan.plan_for(p, led["config"])
    except Exception:
        return None
    done = (p["handoff"].get(phase) or {})
    return [c[0] for c in (pl.get(phase + "Checks") or []) if not done.get(c[0])]


def cmd_depart(args):
    path, local = ctx()
    led = L.load(path)
    p0 = L.find_project(led, args.project, os.getcwd())
    _need_handoff(p0)
    miss = _unchecked(led, p0, "pre")
    if miss:
        warn("注意：出发前清单还有 %d 项没勾选：%s" % (len(miss), ", ".join(miss)))

    def fn(l, p, h):
        _need_handoff(p)["state"] = "transit"
        L.touch(p, h)

    mutate(args, fn)
    out("已标记为在途（transit）。到另一台设备后运行 pickup。")
    return 0


def cmd_pickup(args):
    path, local = ctx()
    led = L.load(path)
    h = L.here(led, local)
    p = L.find_project(led, args.project, os.getcwd())
    ho = _need_handoff(p)
    plan = _mod("plan")
    cfg = led["config"]
    if h and h != ho.get("to"):
        warn("注意：这次接力的目的地是 %s，本机是 %s。" % (_dev_label(cfg, ho.get("to")), _dev_label(cfg, h)))
    if ho.get("state") == "prep":
        warn("注意：对方还没有标记出发（depart），内容可能还没准备完。")
    out("接棒：%s（标签 %s，%s → %s，传输方式 %s）" % (p["name"], ho.get("tag"), ho.get("from"), ho.get("to"), ho.get("transport")))
    out("最近任务：%s" % (p["lastTask"] or "（空）"))
    out("下一步：%s" % (p["nextAction"] or "（空）"))
    v = p["verify"]
    out("验证结果：%s%s" % (VERIFY.get(v.get("state"), v.get("state")), ("  " + v["note"]) if v.get("note") else ""))
    out("交接备注：%s" % (p["note"] or "（空）"))
    out("原设备（%s）上的 Session：" % _dev_label(cfg, ho.get("from")))
    src = [s for s in p["sessions"] if s.get("device") == ho.get("from")]
    if not src:
        out("  （没有登记）")
    for s in src:
        out("  [%s] %s  id=%s  %s  最近使用 %s" % (s.get("tool"), s.get("title") or "（无标题）", s.get("sessionId") or "（未填）",
                                              s.get("kind"), s.get("lastUsed")))
    out("")
    out("说明：原 Session 留在原设备上，只能在那里原样恢复；这里开的是读摘要（HANDOFF.md 和台账）的新 Session，不是恢复。")
    out("")
    _print_plan(plan, led, p, "post")
    out("")
    out("按清单做完后，用 check %s post KEY 勾选，最后 arrive。" % p["id"])
    return 0


def cmd_arrive(args):
    path, local = ctx()
    led = L.load(path)
    h = need_here(led, local)
    p0 = L.find_project(led, args.project, os.getcwd())
    ho0 = _need_handoff(p0)
    if ho0.get("to") != h:
        raise CliError("这次接力的目的地是 %s，本机是 %s，不能在这里完成。" % (
            _dev_label(led["config"], ho0.get("to")), _dev_label(led["config"], h)), 1)
    miss = _unchecked(led, p0, "post")
    if miss:
        warn("注意：到达清单还有 %d 项没勾选：%s" % (len(miss), ", ".join(miss)))

    def fn(l, p, here_id):
        ho = _need_handoff(p)
        p["history"] = (p["history"] + [{"tag": ho.get("tag"), "from": ho.get("from"), "to": ho.get("to"),
                                         "transport": ho.get("transport"), "doneAt": L.now_iso()}])[-10:]
        p["device"] = ho.get("to")
        p["handoff"] = None
        L.touch(p, h)

    led, pid = mutate(args, fn)
    p = get_project(led, pid)
    out("接力完成：%s 现在在 %s。" % (p["name"], _dev_label(led["config"], p["device"])))
    try:
        for line in run_scan(path, local, [pid]):
            out(line)
    except CliError as e:
        warn("没有扫描：" + e.message)
    return 0


def cmd_cancel(args):
    def fn(led, p, h):
        _need_handoff(p)
        p["handoff"] = None
        L.touch(p, h)

    led, pid = mutate(args, fn)
    out("已取消 %s 的接力。" % get_project(led, pid)["name"])
    return 0


def cmd_conflict_add(args):
    box = {}

    def fn(led, p, h):
        c = {"id": L.new_id("c_"), "kind": args.kind, "date": today(), "device": h or p["device"],
             "kept": args.kept if args.kept is not None else "尚未解决", "files": args.files or "",
             "resolution": args.resolution or ""}
        p["conflicts"].append(c)
        box["c"] = c
        L.touch(p, h)

    led, pid = mutate(args, fn)
    out("已记录冲突 %s（%s）。" % (box["c"]["id"], args.kind))
    return 0


# ---------------------------------------------------------------- devices, export/import

def cmd_device(args):
    path, local = ctx()
    modify = args.name is not None or args.os is not None or args.drop is not None
    if modify:
        box = {}

        def fn(led):
            which = args.which or L.here(led, local)
            if not which:
                raise CliError(NEED_DEVICE + "或者加 --which a|b。", 1)
            d = L.dev(led["config"], which)
            if d["id"] != which:
                raise CliError("没有设备 %s。" % which, 1)
            if args.name is not None:
                d["name"] = args.name
            if args.os is not None:
                d["os"] = args.os
            if args.drop is not None:
                if which != L.here(led, local):
                    val, notice = fix_foreign_path(led["config"], which, args.drop, "--drop")
                    if notice:
                        out(notice)
                    d["drop"] = val
                else:
                    d["drop"] = args.drop
            box["id"] = which
        L.update(path, fn)
        out("已更新设备 %s。" % box["id"])
    led = L.load(path)
    me = L.here(led, local)
    for d in led["config"]["devices"]:
        out("%s  %s  系统 %s  中转目录 %s  主机名 %s%s" % (
            d["id"], d.get("name", ""), d.get("os", ""), d.get("drop") or "（未填）", d.get("host") or "（未填）",
            "  <- 本机" if d["id"] == me else ""))
    return 0


def cmd_export(args):
    path, local = ctx()
    led = L.load(path)
    data = {"app": L.APP, "version": L.VERSION, "exportedAt": L.now_iso(),
            "config": led["config"], "projects": led["projects"]}
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.file:
        with open(args.file, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
        out("已导出 %d 个项目到 %s。" % (len(led["projects"]), args.file))
    else:
        out(text)
    return 0


def _ts(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def _newer(a, b):
    """True if timestamp a is strictly newer than b."""
    ta, tb = _ts(a), _ts(b)
    if ta is not None and tb is not None:
        return ta > tb
    if ta is not None:
        return True
    if tb is not None:
        return False
    return str(a or "") > str(b or "")


def cmd_import(args):
    try:
        with open(args.file, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except OSError as e:
        raise CliError("读不了备份文件 %s：%s" % (args.file, e), 1)
    except ValueError:
        raise CliError("这不是有效的备份内容：%s" % args.file, 1)
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        raise CliError("备份里没有项目列表。", 1)
    path, local = ctx()
    stat = {"add": 0, "upd": 0, "keep": 0}

    def fn(led):
        if args.replace:
            old_hosts = dict((d["id"], d.get("host") or "") for d in led["config"]["devices"])
            led["config"] = L.norm_config(data.get("config"))
            for d in led["config"]["devices"]:
                if not d.get("host") and old_hosts.get(d["id"]):
                    d["host"] = old_hosts[d["id"]]
            projects = []
            for p in data["projects"]:
                if isinstance(p, dict):
                    if not p.get("id"):
                        p = dict(p, id=L.new_id("p_"))
                    projects.append(L.norm_project(p, led["config"]))
            led["projects"] = projects
            stat["add"] = len(projects)
            return
        for p in data["projects"]:
            if not isinstance(p, dict):
                continue
            if not p.get("id"):
                p = dict(p, id=L.new_id("p_"))
            n = L.norm_project(p, led["config"])
            for i, cur in enumerate(led["projects"]):
                if cur["id"] == n["id"]:
                    if _newer(n.get("updatedAt"), cur.get("updatedAt")):
                        led["projects"][i] = n
                        stat["upd"] += 1
                    else:
                        stat["keep"] += 1
                    break
            else:
                led["projects"].append(n)
                stat["add"] += 1

    L.update(path, fn)
    if args.replace:
        out("已用备份整体替换台账（%d 个项目）。" % stat["add"])
    else:
        out("已合并备份：新增 %d，更新 %d，保留本地较新的 %d。" % (stat["add"], stat["upd"], stat["keep"]))
    return 0


class _FileStore(object):
    def __init__(self, path):
        self.path = path

    def read(self):
        return L.load(self.path), L.rev(self.path)

    def update(self, fn):
        led = L.update(self.path, fn)
        return led, L.rev(self.path)


def cmd_serve(args):
    path, local = ctx()
    server = _mod("server")
    html = L.tool_dir() / "relay-desk.html"
    out("本机服务：http://127.0.0.1:%d/ （Ctrl+C 停止）台账：%s" % (args.port, path))
    sys.stdout.flush()
    try:
        server.serve(str(html), _FileStore(path), port=args.port, open_browser=not args.no_open)
    except KeyboardInterrupt:
        out("已停止。")
    return 0


# ---------------------------------------------------------------- install-skills

def relay_invocation():
    root = str(L.tool_dir())
    if L.local_os() == "windows":
        return 'py -3 "%s"' % os.path.join(root, "relay.py")
    return 'python3 "%s"' % os.path.join(root, "relay.py")


def _skill_files(src, relay):
    files = {}
    for dirpath, _dirs, names in os.walk(str(src)):
        for n in names:
            full = os.path.join(dirpath, n)
            rel = os.path.relpath(full, str(src))
            with open(full, "rb") as f:
                data = f.read()
            if n == "SKILL.md":
                data = data.decode("utf-8").replace("{{RELAY}}", relay).encode("utf-8")
            files[rel] = data
    return files


def _same_dir(target, files):
    if not os.path.isdir(str(target)):
        return False
    found = {}
    for dirpath, _dirs, names in os.walk(str(target)):
        for n in names:
            full = os.path.join(dirpath, n)
            with open(full, "rb") as f:
                found[os.path.relpath(full, str(target))] = f.read()
    return found == files


def cmd_install_skills(args):
    anti_flag = getattr(args, "anti", False)
    all_tools = not args.claude and not args.codex and not anti_flag
    jobs = []
    if args.claude or all_tools:
        jobs.append(("Claude Code", L.tool_dir() / "skills" / "claude" / "relay-desk",
                     user_home() / ".claude" / "skills" / "relay-desk"))
    if args.codex or all_tools:
        ch = os.environ.get("CODEX_HOME")
        base = Path(ch) if ch else user_home() / ".codex"
        jobs.append(("Codex", L.tool_dir() / "skills" / "codex" / "relay-desk", base / "skills" / "relay-desk"))
    if anti_flag or all_tools:
        gh = os.environ.get("GEMINI_CONFIG_DIR")
        gbase = Path(gh) if gh else user_home() / ".gemini" / "config"
        jobs.append(("Antigravity", L.tool_dir() / "skills" / "anti" / "relay-desk", gbase / "skills" / "relay-desk"))
    relay = relay_invocation()
    plans = []
    for label, src, dst in jobs:
        if not src.is_dir():
            raise CliError("找不到 %s 的 skill 源目录：%s" % (label, src), 1)
        plans.append((label, dst, _skill_files(src, relay)))
    for label, dst, files in plans:
        if dst.exists() and not _same_dir(dst, files) and not args.force and not args.dry_run:
            raise CliError("%s 的目标已存在且内容不同：%s。确认要覆盖就加 --force。" % (label, dst), 1)
    for label, dst, files in plans:
        exists = dst.exists()
        same = exists and _same_dir(dst, files)
        if args.dry_run:
            state = "已是最新，不会改动" if same else ("已存在且内容不同，需要 --force 才会覆盖" if exists and not args.force
                                                  else ("将覆盖" if exists else "将新建"))
            out("[演练] %s：%s（%s）" % (label, dst, state))
            for rel in sorted(files):
                out("  写入 %s" % (dst / rel))
            continue
        if same:
            out("%s：已是最新：%s" % (label, dst))
            continue
        if exists:
            shutil.rmtree(str(dst))
        for rel, data in files.items():
            tp = dst / rel
            tp.parent.mkdir(parents=True, exist_ok=True)
            with open(str(tp), "wb") as f:
                f.write(data)
        out("已安装 %s skill：%s" % (label, dst))
    if args.dry_run:
        out("演练结束，没有写入任何文件。")
    return 0


# ---------------------------------------------------------------- parser

def build_parser():
    ap = _Parser(prog="relay", description="relay-desk：双机接力台账命令行")
    sub = ap.add_subparsers(dest="cmd", parser_class=_Parser)
    sub.required = True

    def P(name, help_text, func):
        sp = sub.add_parser(name, help=help_text)
        sp.set_defaults(func=func)
        return sp

    sp = P("init", "写本机配置", cmd_init)
    sp.add_argument("--device", choices=["a", "b"])
    sp.add_argument("--ledger")
    sp.add_argument("--host", action="store_true", help="把本机主机名记到该设备的 host")

    sp = P("status", "查看台账", cmd_status)
    sp.add_argument("project", nargs="?")
    sp.add_argument("--json", action="store_true")

    sp = P("scan", "刷新本机事实", cmd_scan)
    sp.add_argument("project", nargs="?")
    sp.add_argument("--all", action="store_true")

    sp = P("add", "登记项目", cmd_add)
    sp.add_argument("name")
    sp.add_argument("--path", required=True)
    sp.add_argument("--tool", choices=PROJECT_TOOLS)
    sp.add_argument("--id")

    sp = P("set", "改字段", cmd_set)
    sp.add_argument("project")
    sp.add_argument("--path-here")
    sp.add_argument("--path-other")
    sp.add_argument("--alt-path", action="append")
    sp.add_argument("--tool", choices=PROJECT_TOOLS)
    sp.add_argument("--status", choices=["active", "paused", "blocked", "done"])
    sp.add_argument("--transport", choices=["", "remote", "bundle", "patch", "folder"])
    sp.add_argument("--worktree")
    sp.add_argument("--device", choices=["a", "b"])

    sp = P("note", "记最近任务、下一步、验证", cmd_note)
    sp.add_argument("project")
    sp.add_argument("--last")
    sp.add_argument("--next")
    sp.add_argument("--note")
    sp.add_argument("--verify", choices=["none", "pass", "partial", "fail"])
    sp.add_argument("--verify-cmd")
    sp.add_argument("--verify-note")

    sp = P("sessions", "列出本机发现的 Session", cmd_sessions)
    sp.add_argument("project", nargs="?")
    sp.add_argument("--tool", choices=SESSION_TOOLS)
    sp.add_argument("--json", action="store_true")

    sp = P("session", "登记或取消登记 Session", None)
    ss = sp.add_subparsers(dest="sub", parser_class=_Parser)
    ss.required = True
    a = ss.add_parser("add", help="登记 Session")
    a.set_defaults(func=cmd_session_add)
    a.add_argument("project")
    a.add_argument("id", help="sessionId，或 latest")
    a.add_argument("--tool", choices=SESSION_TOOLS)
    a.add_argument("--kind", choices=["local", "cloud", "remote"])
    a.add_argument("--title")
    a.add_argument("--carry", choices=["summary", "transcript"])
    a.add_argument("--cwd")
    r = ss.add_parser("rm", help="取消登记")
    r.set_defaults(func=cmd_session_rm)
    r.add_argument("project")
    r.add_argument("id")

    sp = P("handoff", "建立接力", cmd_handoff)
    sp.add_argument("project")
    sp.add_argument("--transport", choices=["remote", "bundle", "patch", "folder"])
    sp.add_argument("--to", choices=["a", "b"])

    sp = P("plan", "重新打印清单与命令", cmd_plan)
    sp.add_argument("project")
    sp.add_argument("--phase", choices=["pre", "post"])
    sp.add_argument("--json", action="store_true")

    sp = P("check", "勾选清单项", cmd_check)
    sp.add_argument("project")
    sp.add_argument("phase", choices=["pre", "post"])
    sp.add_argument("key")
    sp.add_argument("--undo", action="store_true")

    for name, fn, h in (("depart", cmd_depart, "标记在途"), ("pickup", cmd_pickup, "接棒：打印上下文和到达清单"),
                        ("arrive", cmd_arrive, "完成接力"), ("cancel", cmd_cancel, "取消接力")):
        sp = P(name, h, fn)
        sp.add_argument("project")

    sp = P("conflict", "冲突记录", None)
    cs = sp.add_subparsers(dest="sub", parser_class=_Parser)
    cs.required = True
    c = cs.add_parser("add", help="记一条冲突")
    c.set_defaults(func=cmd_conflict_add)
    c.add_argument("project")
    c.add_argument("--kind", required=True, choices=CONFLICT_KIND)
    c.add_argument("--files")
    c.add_argument("--resolution")
    c.add_argument("--kept")

    sp = P("device", "查看或修改设备设置", cmd_device)
    sp.add_argument("--which", choices=["a", "b"])
    sp.add_argument("--name")
    sp.add_argument("--os", choices=["windows", "mac", "linux"])
    sp.add_argument("--drop")

    sp = P("export", "导出备份", cmd_export)
    sp.add_argument("file", nargs="?")

    sp = P("import", "导入备份", cmd_import)
    sp.add_argument("file")
    sp.add_argument("--replace", action="store_true")

    sp = P("serve", "启动本机服务", cmd_serve)
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-open", action="store_true")

    sp = P("install-skills", "安装 skill", cmd_install_skills)
    sp.add_argument("--claude", action="store_true")
    sp.add_argument("--codex", action="store_true")
    sp.add_argument("--anti", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--force", action="store_true")
    return ap


def _fix_streams():
    for s in (sys.stdout, sys.stderr):
        try:
            if not s.isatty() and hasattr(s, "reconfigure"):
                s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None):
    _fix_streams()
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except UsageError as e:
        warn("用法错误：%s" % e)
        return 1
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0
    try:
        return args.func(args) or 0
    except CliError as e:
        warn(e.message)
        return e.code
    except LookupError as e:
        warn(e.args[0] if e.args else str(e))
        return 1
    except L.LedgerError as e:
        warn(str(e))
        return 2
    except OSError as e:
        warn("文件读写出错：%s" % e)
        return 2
    except KeyboardInterrupt:
        return 130
