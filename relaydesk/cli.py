"""relay-desk command line (SPEC section 6).

scan / plan / server are imported lazily inside the handlers that need them.
This module never runs a git command and never touches the network.
"""
import argparse
import importlib
import inspect
import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import devices
from . import ledger as L
from . import validation as V

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
NEED_DEVICE = "无法识别本机是哪台设备。请先运行 relay init --device a（或 b，或自定义别名）。"


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

def _format_candidates_msg(msg, candidates):
    if not candidates:
        return msg
    parts = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        alias = str(c.get("alias") or c.get("id") or "")
        did = str(c.get("deviceId") or "")
        name = str(c.get("name") or "")
        label = alias or name or did or "?"
        if (did and did not in msg) or (alias and alias not in msg):
            parts.append("%s（deviceId=%s）" % (label, did or "-"))
    if parts:
        return "%s\n候选设备：%s" % (msg, "；".join(parts))
    return msg


def cmd_init(args):
    path, local = ctx()
    changed = False
    alias = None
    if args.device is not None:
        try:
            alias = devices.validate_alias(args.device)
        except ValueError as e:
            raise CliError(str(e), 1)
        local["device"] = alias
        changed = True
    if args.ledger:
        local["ledger"] = os.path.abspath(args.ledger)
        changed = True

    led_path = L.ledger_path(local)
    existed = led_path.exists()
    ident = L._load_identity()
    ident_did = str(ident.get("deviceId") or "").strip()

    try:
        if not existed:
            def init_new(led):
                if alias and alias not in ("a", "b"):
                    led["config"]["devices"] = [{
                        "id": alias,
                        "name": args.name if args.name is not None else alias,
                        "os": L.local_os(),
                        "drop": "",
                        "host": L.hostname() if args.host else "",
                        "deviceId": ident_did,
                    }]
                elif alias:
                    d = L.dev(led["config"], alias)
                    if args.name is not None:
                        d["name"] = args.name
                    if args.host:
                        d["host"] = L.hostname()
                    if ident_did and not d.get("deviceId"):
                        d["deviceId"] = ident_did
                elif args.host:
                    h = L.here(led, local)
                    if not h:
                        raise CliError(NEED_DEVICE, 1)
                    d = L.dev(led["config"], h)
                    d["host"] = L.hostname()
                    if args.name is not None:
                        d["name"] = args.name

            led = L.update(led_path, init_new)
        else:
            def init_existing(led):
                devs = led["config"]["devices"]
                if alias:
                    exact = next((d for d in devs if d.get("id") == alias), None)
                    if exact is not None:
                        if args.name is not None:
                            exact["name"] = args.name
                        if args.host:
                            exact["host"] = L.hostname()
                        if ident_did and not exact.get("deviceId"):
                            if not any(x.get("deviceId") == ident_did for x in devs):
                                exact["deviceId"] = ident_did
                    else:
                        ci = next((d for d in devs if str(d.get("id") or "").lower() == alias.lower()), None)
                        if ci is not None:
                            raise CliError("台账中已存在别名「%s」（与「%s」大小写冲突），请使用一致的别名。" % (ci["id"], alias), 1)
                        new_name = args.name if args.name is not None else alias
                        used_dids = {str(x.get("deviceId") or "").strip() for x in devs if x.get("deviceId")}
                        devs.append({
                            "id": alias,
                            "name": new_name,
                            "os": L.local_os(),
                            "drop": "",
                            "host": L.hostname() if args.host else "",
                            "deviceId": ident_did if (ident_did and ident_did not in used_dids) else "",
                        })
                elif args.host:
                    h = L.here(led, local)
                    if not h:
                        raise CliError(NEED_DEVICE, 1)
                    d = L.dev(led["config"], h)
                    d["host"] = L.hostname()
                    if args.name is not None:
                        d["name"] = args.name

            led = L.update(led_path, init_existing)
    except devices.DeviceConflictError as e:
        raise CliError(str(e), 1)

    if args.host and alias is None:
        h = L.here(led, local)
        if h and local.get("device") != h:
            local["device"] = h
            changed = True
    if ident_did and local.get("deviceId") != ident_did:
        local["deviceId"] = ident_did
        changed = True
    if changed or not (L.local_dir() / "config.json").exists():
        L.save_local(local)
    me = L.here(led, local)
    out("已写入本机配置：%s" % (L.local_dir() / "config.json"))
    out("台账：%s%s" % (led_path, "" if existed else "（新建）"))
    out("本机设备：%s" % (_dev_label(led["config"], me) if me else "尚未确定，请加 --device <别名>"))
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


def _default_project_id(name):
    """An ID derived from the project name that the engine accepts (Phase A rules); "" when none fits."""
    pid = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name or ""))).strip("-. ")
    try:
        V.validate_project_id(pid)
    except V.ValidationError:
        return ""
    return pid


def _project_id_problem(pid):
    """Why a new project ID cannot enter the ledger ("" when it can): the same rule publish/send/receive apply."""
    try:
        V.validate_project_id(pid)
    except V.ValidationError as e:
        return e.message
    return ""


def cmd_add(args):
    path, local = ctx()
    box = {}

    def fn(led):
        h = need_here(led, local)
        pid = args.id or _default_project_id(args.name)
        if not pid:
            pid = L.new_id("p_")
        problem = _project_id_problem(pid)
        if problem:
            # Fix G: refused at registration, not first at publish (Phase A projectId rule).
            raise CliError("%s 请用 --id 指定合法的项目 ID（以字母或数字开头，例如 my-project）。" % problem, 1)
        if not re.match(r"^[A-Za-z0-9_.-]+$", pid):
            raise CliError("项目 id 只能含字母、数字、下划线、点和连字符：%s" % pid, 1)
        if any(p["id"] == pid for p in led["projects"]):
            raise CliError("已经有 id 为 %s 的项目。用 --id 指定另一个。" % pid, 1)
        others = devices.list_other_devices(led["config"], h)
        abs_path = os.path.abspath(args.path)
        paths = {h: abs_path}
        if len(others) == 1:
            paths.setdefault(others[0]["id"], "")
        proj_init = {"id": pid, "name": args.name, "device": h, "tool": args.tool or "claude", "paths": paths}
        h_did = str(L.dev(led["config"], h).get("deviceId") or "").strip()
        if h_did:
            proj_init["pathsByDeviceId"] = {h_did: abs_path}
        p = L.norm_project(proj_init, led["config"])
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
        if args.path_other is not None:
            if args.for_device:
                try:
                    target_dev = devices.resolve_target_device(
                        led["config"], h, to_query=args.for_device, require_paired_cloud=False
                    )
                except devices.AmbiguousDeviceError as e:
                    raise CliError(_format_candidates_msg(str(e), e.candidates), 1)
                except (LookupError, ValueError) as e:
                    raise CliError(str(e), 1)
                o = target_dev["id"]
            else:
                others = devices.list_other_devices(led["config"], h)
                if len(others) == 0:
                    raise CliError("台账里没有其他目标设备，请先使用 relay device --add <别名> 添加。", 1)
                if len(others) > 1:
                    cand_text = "、".join("%s（%s）" % (d.get("name") or d["id"], d["id"]) for d in others)
                    raise CliError("台账中有多台其他设备（%s），请使用 --for-device <别名> 指定要设置路径的设备。" % cand_text, 1)
                o = others[0]["id"]
        if args.path_here is not None:
            abs_here = os.path.abspath(args.path_here) if args.path_here else ""
            p["paths"][h] = abs_here
            h_did = str(L.dev(led["config"], h).get("deviceId") or "").strip()
            if h_did and isinstance(p.get("pathsByDeviceId"), dict):
                p["pathsByDeviceId"][h_did] = abs_here
            changed.append("本机路径")
        if args.path_other is not None:
            val, notice = fix_foreign_path(led["config"], o, args.path_other, "--path-other")
            if notice:
                out(notice)
            p["paths"][o] = val
            o_did = str(L.dev(led["config"], o).get("deviceId") or "").strip()
            if o_did and isinstance(p.get("pathsByDeviceId"), dict):
                p["pathsByDeviceId"][o_did] = val
            changed.append("另一台设备的路径" if len(devices.list_other_devices(led["config"], h)) <= 1 else ("设备 %s 的路径" % o))
        if args.alt_path:
            alt = p.setdefault("altPaths", {})
            lst = alt.setdefault(h, [])
            for a in args.alt_path:
                a = os.path.abspath(a)
                if a not in lst:
                    lst.append(a)
            changed.append("其他启动目录")
        for attr, key, label in (("tool", "tool", "工具"), ("status", "status", "状态"),
                                 ("transport", "transport", "传输方式"), ("worktree", "worktree", "工作树")):
            v = getattr(args, attr)
            if v is not None:
                p[key] = v
                changed.append(label)
        if args.device is not None:
            try:
                resolved_dev = devices.resolve_device_entry(led["config"], args.device)
            except devices.AmbiguousDeviceError as e:
                raise CliError(_format_candidates_msg(str(e), e.candidates), 1)
            except LookupError as e:
                raise CliError(str(e), 1)
            p["device"] = resolved_dev["id"]
            changed.append("所在设备")
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
        target_id = None
        if args.to is not None:
            try:
                target_dev = devices.resolve_target_device(
                    l["config"], proj["device"], to_query=args.to, require_paired_cloud=False
                )
                target_id = target_dev["id"]
            except devices.AmbiguousDeviceError as e:
                raise CliError(_format_candidates_msg(str(e), e.candidates), 1)
            except (LookupError, ValueError) as e:
                raise CliError(str(e), 1)
        else:
            try:
                target_dev = devices.resolve_target_device(
                    l["config"], proj["device"], to_query=None, require_paired_cloud=False
                )
                target_id = target_dev["id"]
            except devices.AmbiguousDeviceError as e:
                raise CliError(_format_candidates_msg(str(e), e.candidates), 1)
            except LookupError:
                target_id = None
        if "to_id" in inspect.signature(plan.new_handoff).parameters:
            ho = plan.new_handoff(proj, l["config"], args.transport, to_id=target_id)
        else:
            ho = plan.new_handoff(proj, l["config"], args.transport)
            if target_id:
                ho["to"] = target_id
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

def _fallback_list_devices(path, local):
    led = L.load(path)
    here_id = L.here(led, local) or ""
    ident = L._load_identity()
    dev_id = str(ident.get("deviceId") or "")
    key_fp = str(ident.get("keyFingerprint") or "")
    cloud_path = L.local_dir() / "cloud.json"
    peers_map = {}
    if cloud_path.exists():
        try:
            with open(str(cloud_path), "r", encoding="utf-8-sig") as fh:
                raw_cl = json.load(fh)
            if isinstance(raw_cl, dict):
                peers_map = (devices.migrate_cloud_config(raw_cl).get("peers") or {})
        except (OSError, ValueError, UnicodeDecodeError):
            peers_map = {}
    out_devices = []
    seen_dids = set()
    for d in led["config"]["devices"]:
        if not isinstance(d, dict):
            continue
        d_id = str(d.get("id") or "")
        d_did = str(d.get("deviceId") or "")
        is_here = bool(here_id and d_id == here_id)
        if is_here and not d_did and dev_id:
            d_did = dev_id
        peer = peers_map.get(d_did) if d_did else None
        if peer is None and not is_here and not d_did:
            for p in peers_map.values():
                if isinstance(p, dict) and str(p.get("alias") or "") == d_id:
                    peer = p
                    break
        if d_did:
            seen_dids.add(d_did)
        if is_here:
            status, paired, caps, proto_ver, kfp, pfp, paired_at = (
                "self", True, ["v2", "v3"], 3, key_fp, "", ""
            )
        elif isinstance(peer, dict):
            status = str(peer.get("status") or ("paired" if peer.get("paired") else "discovered"))
            paired = bool(peer.get("paired") and status == "paired")
            caps = list(peer.get("capabilities") or ["v2"])
            proto_ver = int(peer.get("protocolVersion") or 2)
            kfp = str(peer.get("keyFingerprint") or "")
            pfp = str(peer.get("pairFingerprint") or "")
            paired_at = str(peer.get("pairedAt") or "")
        else:
            status, paired, caps, proto_ver, kfp, pfp, paired_at = (
                "unpaired", False, [], 0, "", "", ""
            )
        out_devices.append({
            "id": d_id,
            "alias": d_id,
            "name": str(d.get("name") or (peer.get("name") if isinstance(peer, dict) else "") or ""),
            "os": str(d.get("os") or (peer.get("os") if isinstance(peer, dict) else "") or ""),
            "host": str(d.get("host") or (peer.get("host") if isinstance(peer, dict) else "") or ""),
            "deviceId": d_did or (str(peer.get("deviceId") or "") if isinstance(peer, dict) else ""),
            "isHere": is_here,
            "paired": paired,
            "status": status,
            "keyFingerprint": kfp,
            "pairFingerprint": pfp,
            "protocolVersion": proto_ver,
            "capabilities": caps,
            "pairedAt": paired_at,
            "needsPairing": bool(d.get("needsPairing")),
        })
    for did, p in sorted(peers_map.items()):
        if not isinstance(p, dict) or did in seen_dids:
            continue
        out_devices.append({
            "id": str(p.get("alias") or ""),
            "alias": str(p.get("alias") or ""),
            "name": str(p.get("name") or ""),
            "os": str(p.get("os") or ""),
            "host": str(p.get("host") or ""),
            "deviceId": did,
            "isHere": False,
            "paired": bool(p.get("paired") and p.get("status") == "paired"),
            "status": str(p.get("status") or "discovered"),
            "keyFingerprint": str(p.get("keyFingerprint") or ""),
            "pairFingerprint": str(p.get("pairFingerprint") or ""),
            "protocolVersion": int(p.get("protocolVersion") or 2),
            "capabilities": list(p.get("capabilities") or ["v2"]),
            "pairedAt": str(p.get("pairedAt") or ""),
            "needsPairing": False,
        })
    return {
        "here": here_id,
        "deviceId": dev_id,
        "keyFingerprint": key_fp,
        "capabilities": ["v2", "v3"],
        "devices": out_devices,
    }


def _rotation_guard(args):
    """Phase D: refuse a key rotation that would strand in-flight handoffs, unless --force (consequences printed)."""
    dx = _mod("diagnostics")
    blockers = dx.rotation_blockers(L.local_dir())
    if not blockers:
        return
    lines = []
    for f in blockers:
        lines.append("  - %s" % f["reason"])
        lines.append("    处理命令：%s" % f["command"])
    if not getattr(args, "force", False):
        raise CliError("本机还有未完成的交接或恢复事务；现在换钥，这些交接包将无法被对方领取或验证：\n%s\n"
                       "请先按上面的命令处理完再换钥。确需立即换钥请加 --force，之后在途交接需要用 "
                       "relay send <项目> --to <设备> --reissue 重新签发（原交接包保持不变）。" % "\n".join(lines), 4)
    warn("已按 --force 换钥。以下交接或事务会受影响：\n%s\n在途交接需要在对方重新确认本机新指纹后，用 "
         "relay send <项目> --to <设备> --reissue 重新签发；原交接包保持不变。" % "\n".join(lines))


def cmd_device(args):
    if args.rotate_keys:
        _rotation_guard(args)
    path, local = ctx()
    if args.add is not None:
        if args.which is not None:
            raise CliError("--add 与 --which 不能同时使用。", 1)
        try:
            new_alias = devices.validate_alias(args.add)
        except ValueError as e:
            raise CliError(str(e), 1)
        box = {}

        def fn_add(led):
            devs = led["config"]["devices"]
            if any(str(d.get("id") or "").lower() == new_alias.lower() for d in devs):
                raise CliError("设备别名「%s」已存在。" % new_alias, 1)
            entry = {
                "id": new_alias,
                "name": args.name if args.name is not None else ("设备 %s" % new_alias),
                "os": args.os or "linux",
                "drop": "",
                "host": "",
                "deviceId": "",
            }
            if args.os is not None:
                entry["osExplicit"] = True
            devs.append(entry)
            if args.drop is not None:
                if new_alias != L.here(led, local):
                    val, notice = fix_foreign_path(led["config"], new_alias, args.drop, "--drop")
                    if notice:
                        out(notice)
                    entry["drop"] = val
                else:
                    entry["drop"] = args.drop
            box["id"] = new_alias

        try:
            L.update(path, fn_add)
        except devices.DeviceConflictError as e:
            raise CliError(str(e), 1)
        if not args.json:
            out("已添加设备 %s。" % box["id"])
    else:
        modify = args.name is not None or args.os is not None or args.drop is not None
        if modify:
            box = {}

            def fn(led):
                which = args.which or L.here(led, local)
                if not which:
                    raise CliError(NEED_DEVICE + "或者加 --which <别名>。", 1)
                try:
                    d = devices.resolve_device_entry(led["config"], which)
                except devices.AmbiguousDeviceError as e:
                    raise CliError(_format_candidates_msg(str(e), e.candidates), 1)
                except LookupError:
                    raise CliError("没有设备 %s。" % which, 1)
                resolved_id = d["id"]
                if args.name is not None:
                    d["name"] = args.name
                if args.os is not None:
                    d["os"] = args.os
                    d["osExplicit"] = True
                if args.drop is not None:
                    if resolved_id != L.here(led, local):
                        val, notice = fix_foreign_path(led["config"], resolved_id, args.drop, "--drop")
                        if notice:
                            out(notice)
                        d["drop"] = val
                    else:
                        d["drop"] = args.drop
                box["id"] = resolved_id

            try:
                L.update(path, fn)
            except devices.DeviceConflictError as e:
                raise CliError(str(e), 1)
            if not args.json:
                out("已更新设备 %s。" % box["id"])

    if args.rotate_keys:
        crypto = _mod("crypto")
        rotated = _call_engine(crypto.rotate_identity_keys, L.local_dir())
        rot_did = str(rotated.get("deviceId") or "")
        rot_fp = str(rotated.get("keyFingerprint") or "")
        rot_at = str(rotated.get("rotatedAt") or "")
        rot_alias_box = {"alias": str(rotated.get("alias") or "")}

        def fn_sync_rot(led):
            me = L.here(led, local)
            target = None
            for d in led["config"]["devices"]:
                if me and d.get("id") == me:
                    target = d
                    break
            if target is None and rot_did:
                for d in led["config"]["devices"]:
                    if d.get("deviceId") == rot_did:
                        target = d
                        break
            if target is None and rot_alias_box["alias"]:
                for d in led["config"]["devices"]:
                    if d.get("id") == rot_alias_box["alias"]:
                        target = d
                        break
            if target is not None:
                target["deviceId"] = rot_did
                target["keyFingerprint"] = rot_fp
                if rot_at:
                    target["rotatedAt"] = rot_at
                rot_alias_box["alias"] = target["id"]

        L.update(path, fn_sync_rot)
        if rot_did and local.get("deviceId") != rot_did:
            local["deviceId"] = rot_did
            L.save_local(local)
        if not args.json:
            out("已轮换本机密钥（deviceId=%s，新公钥指纹=%s）。请在对端设备重新运行 relay pair --with %s --accept 核对新指纹。" % (
                rot_did, rot_fp, rot_alias_box["alias"] or rot_did))

    if args.json:
        try:
            eng = _mod("relay_engine")
            res = _call_engine(eng.list_devices, ledger_path=path, local=local)
        except Exception:
            res = _fallback_list_devices(path, local)
        out(json.dumps(res, ensure_ascii=False, indent=1))
        return 0

    led = L.load(path)
    me = L.here(led, local)
    for d in led["config"]["devices"]:
        extra = []
        if d.get("deviceId"):
            extra.append("ID:%s" % d["deviceId"])
        if d.get("needsPairing"):
            extra.append("待配对")
        tail = ("  [" + ", ".join(extra) + "]") if extra else ""
        out("%s  %s  系统 %s  中转目录 %s  主机名 %s%s%s" % (
            d["id"], d.get("name", ""), d.get("os", ""), d.get("drop") or "（未填）", d.get("host") or "（未填）",
            "  <- 本机" if d["id"] == me else "", tail))
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

    def check_ids(led):
        # Fix G: a backup must not bring in project IDs the engine refuses; IDs already in this
        # ledger (registered by an older version) may come back unchanged.
        existing = {cur.get("id") for cur in led["projects"]}
        bad = []
        for p in data["projects"]:
            pid = p.get("id") if isinstance(p, dict) else None
            if pid and pid not in existing:
                problem = _project_id_problem(pid)
                if problem:
                    bad.append(problem)
        if bad:
            raise CliError("备份里有不符合项目 ID 规则的新项目，未导入任何内容：%s 请在备份文件中改成合法的 ID 后重试。"
                           % " ".join(bad[:5]), 1)

    def fn(led):
        check_ids(led)
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
        self.ledger_path = path

    def read(self):
        return L.load(self.path), L.rev(self.path)

    def update(self, fn):
        led = L.update(self.path, fn)
        return led, L.rev(self.path)


# ---------------------------------------------------------------- cloud relay commands

def _call_engine(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except devices.AmbiguousDeviceError as e:
        raise CliError(_format_candidates_msg(str(e), e.candidates), 1)
    except (CliError, LookupError, L.LedgerError, OSError):
        raise
    except Exception as e:
        mod_name = getattr(e.__class__, "__module__", "") or ""
        if mod_name.startswith("relaydesk."):
            msg = str(e)
            if hasattr(e, "gaps") and "缺口" not in msg:
                msg = "检测到会话缺口：\n" + msg
            if hasattr(e, "candidates") and e.candidates:
                msg = _format_candidates_msg(msg, e.candidates)
            if hasattr(e, "reasons") and e.reasons:
                missing_reasons = [str(r) for r in e.reasons if str(r) not in msg]
                if missing_reasons:
                    msg = msg + "\n原因：" + "；".join(missing_reasons)
            raise CliError(msg, 1)
        raise


def cmd_cloud_remotes(args):
    storage = _mod("storage")
    has_rc = bool(_call_engine(storage.has_rclone))
    if not has_rc:
        if args.json:
            out(json.dumps({"hasRclone": False, "remotes": []}, ensure_ascii=False, indent=1))
        else:
            out("未检测到 rclone。请先安装 rclone（如 brew install rclone）并运行 rclone config 配置网盘。")
        return 0
    remotes = list(_call_engine(storage.discover_rclone_remotes, config_path=args.rclone_config))
    if args.json:
        out(json.dumps({"hasRclone": True, "remotes": remotes}, ensure_ascii=False, indent=1))
        return 0
    if not remotes:
        out("没有发现已配置的 rclone 连接。请先运行 rclone config。")
        return 0
    for r in remotes:
        out(r)
    out("共发现 %d 个 rclone 连接。" % len(remotes))
    return 0


def _match_peer_list(peers, query):
    q = str(query or "").strip()
    ql = q.lower()
    if not q:
        return []
    by_did = [p for p in peers if str(p.get("deviceId") or "") == q]
    if by_did:
        return by_did
    by_al_exact = [p for p in peers if str(p.get("alias") or "") == q]
    if by_al_exact:
        return by_al_exact
    by_al_ci = [p for p in peers if str(p.get("alias") or "").lower() == ql]
    if by_al_ci:
        return by_al_ci
    return [p for p in peers if str(p.get("name") or "").strip().lower() == ql]


def _peer_status_zh(peer):
    st_val = str(peer.get("status") or "")
    if peer.get("paired") and st_val == "paired":
        return "已配对"
    if st_val == "key_changed":
        return "密钥已变更（需重新核对）"
    if st_val == "conflict":
        return "别名冲突（%s）" % (peer.get("conflict") or "alias_conflict")
    return "待确认"


def cmd_pair(args):
    path, local = ctx()
    eng = _mod("relay_engine")
    only_status = (
        args.status
        and args.remote is None
        and args.root is None
        and args.provider is None
        and args.rclone_config is None
        and not args.accept
        and args.fingerprint is None
        and args.alias is None
        and not args.replace_device
    )
    if only_status:
        st = _call_engine(eng.cloud_status, ledger_path=path, local=local)
        if args.with_device:
            matches = _match_peer_list(st.get("peers") or [], args.with_device)
            if not matches:
                raise CliError("找不到远端设备「%s」。" % args.with_device, 1)
            if len(matches) > 1:
                raise CliError(_format_candidates_msg("「%s」匹配到多台远端设备。" % args.with_device, matches), 1)
            sel = matches[0]
            st = dict(st)
            st["peer"] = sel
            st["paired"] = bool(sel.get("paired") and sel.get("status") == "paired")
            st["pairFingerprint"] = str(sel.get("pairFingerprint") or "")
    else:
        st = _call_engine(
            eng.pair_device,
            remote=args.remote,
            root=args.root,
            provider_type=args.provider,
            rclone_config=args.rclone_config,
            accept=args.accept,
            with_device=args.with_device,
            fingerprint=args.fingerprint,
            alias=args.alias,
            replace_device=args.replace_device,
            ledger_path=path,
            local=local,
        )
    if args.json:
        out(json.dumps(st, ensure_ascii=False, indent=1))
        return 0
    cl = st.get("cloud") or {}
    if st.get("configured"):
        if cl.get("provider") == "local":
            out("云存储：本地共享目录（%s）" % (cl.get("root") or ""))
        else:
            out("云存储：rclone（连接 %s，目录 %s）" % (cl.get("remote") or "", cl.get("root") or "/"))
    else:
        out("云存储：未配置（运行 relay pair --remote <连接> --root <目录> 配置）")
    out("本机设备：%s（%s）" % (st.get("alias") or "未指定", st.get("deviceId") or "尚未生成密钥"))
    if st.get("keyFingerprint"):
        out("本机公钥指纹：%s" % st["keyFingerprint"])
    rot = st.get("keyRotationConfirmed")
    if isinstance(rot, dict) and rot.get("deviceId"):
        out("已确认设备 %s 的新公钥（%s，原公钥 %s 已记为历史密钥）。之后 relay sync 会为新公钥重新投递对方缺失的快照；"
            "原投递、回执和加密包保持不变，旧密钥不再获得新的发送权限。" % (
                rot["deviceId"], rot.get("keyFingerprint") or "", rot.get("previousKeyFingerprint") or ""))
    peers = st.get("peers") or []
    if len(peers) > 1 and not args.with_device:
        out("远端设备列表（共 %d 台）：" % len(peers))
        for p in peers:
            out("  - %s（%s，系统 %s）  状态：%s  配对指纹：%s" % (
                p.get("alias") or "-",
                p.get("deviceId") or "-",
                p.get("os") or "-",
                _peer_status_zh(p),
                p.get("pairFingerprint") or "-",
            ))
            if p.get("keyFingerprint"):
                out("    公钥指纹：%s" % p["keyFingerprint"])
            if p.get("error"):
                out("    冲突说明：%s" % p["error"])
        paired_cnt = sum(1 for p in peers if p.get("paired") and p.get("status") == "paired")
        if paired_cnt:
            out("配对状态：已有 %d 台设备完成配对；新增或密钥变更设备可运行 relay pair --with <别名或deviceId> --accept" % paired_cnt)
        else:
            out("配对状态：存在待确认设备，核对配对指纹后运行 relay pair --with <别名或deviceId> --accept")
        return 0
    peer = st.get("peer")
    if isinstance(peer, dict):
        out("对端设备：%s（%s，系统 %s）" % (peer.get("alias") or "", peer.get("deviceId") or "", peer.get("os") or ""))
        if peer.get("keyFingerprint"):
            out("对端公钥指纹：%s" % peer["keyFingerprint"])
    else:
        out("对端设备：尚未在云端发现对端公钥")
    if st.get("pairFingerprint"):
        out("配对指纹：%s" % st["pairFingerprint"])
    if st.get("paired"):
        out("配对状态：已配对（双机公钥已锁定）")
    elif isinstance(peer, dict) and peer.get("status") == "key_changed":
        out("配对状态：对端密钥已变更（请重新核对指纹后运行 relay pair --with %s --accept）" % (
            peer.get("deviceId") or peer.get("alias") or ""))
    elif st.get("pairFingerprint"):
        out("配对状态：待确认（请核对两台设备显示的配对指纹完全一致后，运行 relay pair --accept）")
    else:
        out("配对状态：等待另一台设备发布公钥")
    return 0


def cmd_publish(args):
    path, local = ctx()
    eng = _mod("relay_engine")
    res = _call_engine(
        eng.publish_project,
        project_key=args.project,
        cwd=os.getcwd(),
        ledger_path=path,
        local=local,
    )
    if args.json:
        out(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    out(res.get("message") or ("项目快照已发布：%s（版本 %s）" % (res.get("projectName", ""), res.get("snapshotId", ""))))
    out("快照编号：%s    项目：%s（%s）" % (res.get("snapshotId", ""), res.get("projectName", ""), res.get("projectId", "")))
    if res.get("parentVersionIds"):
        out("父版本：%s" % ", ".join(res["parentVersionIds"]))
    out("SHA-256：%s    大小：%s 字节    归档会话：%s 条" % (
        res.get("contentSha256", ""), res.get("contentSize", 0), res.get("sessionCount", 0)))
    if res.get("excludedFiles"):
        out("已自动排除本地敏感文件：%s" % "、".join(res["excludedFiles"]))
    delivered = res.get("deliveredTo") or []
    failed = res.get("deliveryErrors") or []
    if delivered:
        out("已推送同步至：%s" % "、".join((d.get("alias") or d.get("deviceId") or "") for d in delivered))
    elif not failed:
        out("当前没有其他已配对的 v3 节点，快照已缓存至本机。")
    for f in failed:
        warn("  [推送失败] %s（%s）：%s" % (f.get("alias") or f.get("peerDeviceId", ""), f.get("errorClass", ""),
                                       f.get("error", "")))
    if failed:
        warn("快照已发布到本机缓存和云端索引；运行 relay sync 会继续向失败的设备投递。")
    return 0


_SYNC_TEXT_EVENTS = ("snapshot_cached", "snapshot_forwarded", "receipt_synced", "fork_detected", "problem",
                     "resolved", "peer_state", "trust_changed", "round_skipped", "backoff", "run_started",
                     "peer_verified", "delivery_obsolete", "lineage_unproven", "lineage_resolved")
_OBSOLETE_REASON = {"sender_key_retired": "发送方换钥前的旧密钥签名", "recipient_key_retired": "发给本机换钥前的公钥"}
_PEER_STATE_LABEL = {"healthy": "正常", "backoff": "重试中", "parked": "需要处理"}


def _lineage_unproven_text(ev):
    return ("证据不足（项目 %s）：无法证明本地 %s 与远端 %s 的先后关系，缺少可验证的父版本 %s；"
            "这不是版本分叉，补齐签名记录后会自动复核（relay doctor 可查看）" % (
                ev.get("projectId", ""), ev.get("localVersion", ""), ev.get("incomingVersion", ""),
                "、".join(ev.get("missingVersions") or []) or "（未知）"))


def _lineage_resolved_text(ev):
    return "已补齐父版本证据（项目 %s）：%s 与 %s %s" % (
        ev.get("projectId", ""), ev.get("localVersion", ""), ev.get("incomingVersion", ""),
        "确认是版本分叉（已记入台账）" if ev.get("relation") == "fork" else "在同一条版本链上，不是分叉")


def _short_time(iso):
    text = str(iso or "")
    return text[11:19] if len(text) >= 19 else text


class _SyncOutput(object):
    """Printers for `relay sync`: legacy text, streamed watch text, --json summary, --jsonl events."""

    def __init__(self, args, sr):
        self.args = args
        self.sr = sr
        self.jsonl = bool(getattr(args, "jsonl", False))
        self.json = bool(getattr(args, "json", False))
        self.watch_text = bool(args.watch) and not self.jsonl and not self.json
        self.closed = False

    def on_event(self, ev):
        if self.closed:
            return
        if self.jsonl:
            self._write_line(json.dumps(ev, ensure_ascii=False, separators=(",", ":")))
        elif self.watch_text:
            self._text_event(ev)

    def _write_line(self, line, stream=None):
        stream = stream or sys.stdout
        try:
            stream.write(line + "\n")
            stream.flush()
        except BrokenPipeError:
            # The reader went away (e.g. `relay sync --jsonl | head`): silence stdout and let the
            # engine treat this as cancellation; nothing more is printed.
            self.closed = True
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, stream.fileno())
            except (OSError, ValueError, AttributeError):
                pass
            raise

    def _text_event(self, ev):
        kind = ev.get("kind")
        if kind not in _SYNC_TEXT_EVENTS:
            return
        t = _short_time(ev.get("at"))
        if kind == "run_started":
            out("持续同步已启动：每 %s 秒一轮，暂时失败时最长退避 %s 秒（Ctrl+C 停止）。" % (
                _fmt_num(ev.get("interval")), _fmt_num(ev.get("maxBackoff"))))
        elif kind == "snapshot_cached":
            out("[%s] + 快照 %s（项目 %s，作者 %s%s%s）" % (t, ev.get("snapshotId", ""), ev.get("projectId", ""),
                                                   ev.get("authorDeviceId", ""), "，经转发" if ev.get("isForwarded") else "",
                                                   "，修复本机缓存" if ev.get("repaired") else ""))
        elif kind == "peer_verified":
            out("[%s] = 对端 %s 已确认缓存快照 %s" % (t, ev.get("peerDeviceId", ""), ev.get("snapshotId", "")))
        elif kind == "delivery_obsolete":
            out("[%s] ~ 旧投递 %s 已过期（%s）%s" % (
                t, ev.get("deliveryId", ""), _OBSOLETE_REASON.get(ev.get("reason"), ev.get("reason", "")),
                "，已请求重新投递" if ev.get("needed") else ""))
        elif kind == "snapshot_forwarded":
            out("[%s] > 转发 %s → %s（%s）" % (t, ev.get("snapshotId", ""), ev.get("toAlias") or ev.get("toDeviceId", ""),
                                          ev.get("projectId", "")))
        elif kind == "receipt_synced":
            out("[%s] 签收回执 %s（项目 %s，接收设备 %s%s）" % (t, ev.get("relayId", ""), ev.get("projectId", ""),
                                                  ev.get("toDeviceId", ""), "，迟到" if ev.get("late") else ""))
        elif kind == "fork_detected":
            out("[%s] ! 检测到版本分叉（项目 %s）：本地 %s vs 远端 %s" % (
                t, ev.get("projectId", ""), ev.get("localVersion", ""), ev.get("incomingVersion", "")))
        elif kind == "lineage_unproven":
            out("[%s] ? %s" % (t, _lineage_unproven_text(ev)))
        elif kind == "lineage_resolved":
            out("[%s] = %s" % (t, _lineage_resolved_text(ev)))
        elif kind == "problem":
            action = ("（处理：%s）" % ev["action"]) if ev.get("action") else ""
            warn("[%s] [%s] %s%s" % (t, self.sr.class_label(ev.get("errorClass", "")), ev.get("message", ""), action))
        elif kind == "resolved":
            out("[%s] 已恢复：%s" % (t, ev.get("key", "")))
        elif kind == "peer_state":
            label_to = _PEER_STATE_LABEL.get(ev.get("to"), ev.get("to"))
            extra = ("（%s 秒后重试）" % _fmt_num(ev.get("retryInSec"))) if ev.get("to") == "backoff" else ""
            out("[%s] 对端 %s：%s → %s%s" % (t, ev.get("peerDeviceId", ""), _PEER_STATE_LABEL.get(ev.get("from"), ev.get("from")),
                                        label_to, extra))
        elif kind == "trust_changed":
            what = {"identity": "本机密钥", "connection": "云存储连接", "peer": "对端设备 %s" % ev.get("peerDeviceId", "")}
            out("[%s] 信任配置已变化：%s" % (t, what.get(ev.get("what"), ev.get("what"))))
        elif kind == "round_skipped" and int(ev.get("streak") or 1) == 1:
            out("[%s] 本机有其他交棒、同步或接棒操作正在进行，跳过本轮（持续忙时不再重复提示）。" % t)
        elif kind == "backoff" and int(ev.get("attempt") or 1) == 1:
            out("[%s] 云端暂时不可用，%s 秒后重试（之后逐步延长，最长 %s 秒）。" % (
                t, _fmt_num(ev.get("retryInSec")), _fmt_num(self.args.max_backoff)))

    def summary(self, res):
        if self.closed:
            return
        if self.jsonl:
            return  # the engine already streamed the final {"kind":"summary"} event
        if self.json:
            out(json.dumps(res, ensure_ascii=False, indent=1))
            return
        c = res.get("counters") or {}
        status = res.get("status", "")
        if self.watch_text:
            fo = c.get("failedOperations") or {}
            unresolved = res.get("unresolved") or []
            worst = self.sr.status_label(status)
            out("持续同步已停止（%s）：共 %d 轮，缓存 %d、转发 %d、回执 %d；重试 %d 次；未解决 %d 项%s" % (
                worst, res.get("cycles", 0), c.get("snapshotsCached", 0), c.get("forwarded", 0),
                c.get("receiptsSynced", 0), c.get("retriesScheduled", 0), len(unresolved),
                ("（%s）" % self.sr.class_label(unresolved[0].get("errorClass", ""))) if unresolved else ""))
            if sum(int(v or 0) for v in fo.values()):
                out("  失败操作：%s" % "，".join("%s %d" % (self.sr.class_label(k), v) for k, v in sorted(fo.items()) if v))
        else:
            out("网络同步完成（共 %d 轮）：" % res.get("cycles", 1))
            out("  签收回执：%d 条    同步快照：%d 个    转发快照：%d 个    版本分叉：%d 个" % (
                c.get("receiptsSynced", len(res.get("syncedReceipts") or [])),
                c.get("snapshotsCached", len(res.get("syncedSnapshots") or [])),
                c.get("forwarded", len(res.get("forwarded") or [])),
                c.get("forksDetected", len(res.get("conflictsDetected") or [])),
            ))
            for sn in res.get("syncedSnapshots") or []:
                out("  + 快照 %s（项目 %s，作者 %s%s）" % (
                    sn.get("snapshotId", ""), sn.get("projectId", ""), sn.get("authorDeviceId", ""),
                    "，经转发" if sn.get("isForwarded") else ""))
            for f in res.get("forwarded") or []:
                out("  > 转发 %s → %s（%s）" % (
                    f.get("snapshotId", ""), f.get("toAlias") or f.get("toDeviceId", ""), f.get("projectId", "")))
            for cf in res.get("conflictsDetected") or []:
                out("  ! 检测到版本分叉（项目 %s）：本地 %s vs 远端 %s" % (
                    cf.get("projectId", ""), cf.get("localVersion", ""), cf.get("incomingVersion", "")))
            for lu in res.get("lineageUnproven") or []:
                out("  ? %s" % _lineage_unproven_text(lu))
            for ev in res.get("recentEvents") or []:
                if ev.get("kind") == "lineage_resolved":
                    out("  = %s" % _lineage_resolved_text(ev))
            for e in res.get("errors") or []:
                warn("  [同步警告] %s" % (e.get("error") or str(e)))
            if any((res.get("legacyListsTruncated") or {}).values()):
                out("  （明细仅列出最近 200 条，数量以上方计数为准）")
        out("同步状态：%s" % self.sr.status_label(status))
        for p in res.get("unresolved") or []:
            warn("  未解决 [%s] %s%s" % (self.sr.class_label(p.get("errorClass", "")), p.get("message", ""),
                                     ("\n    处理：%s" % p["action"]) if p.get("action") else ""))

    def preloop_error(self, e):
        """Map an error raised before the first round to (exit code, printed message)."""
        sr = self.sr
        if isinstance(e, devices.AmbiguousDeviceError):
            msg, code = _format_candidates_msg(str(e), e.candidates), 1
            classified = sr.ClassifiedError(sr.NEEDS_ACTION, "ambiguous_device", msg)
        else:
            classified = sr.classify_error(e, "trust_check")
            msg = str(e) or classified.message
            mod_name = getattr(e.__class__, "__module__", "") or ""
            if hasattr(e, "candidates") and getattr(e, "candidates", None):
                msg = _format_candidates_msg(msg, e.candidates)
            if classified.error_class == sr.NEEDS_ACTION and (mod_name.startswith("relaydesk.") or isinstance(e, OSError)):
                code = 4
            elif isinstance(e, (L.LedgerError, OSError)):
                code = 2
                if isinstance(e, OSError):
                    msg = "文件读写出错：%s" % e
            elif isinstance(e, LookupError) or mod_name.startswith("relaydesk."):
                code = 1
            else:
                raise e
        if self.json or self.jsonl:
            body = {"ok": False, "status": sr.NEEDS_ACTION_STATUS if code == 4 else "error", "error": msg,
                    "code": classified.code, "errorClass": classified.error_class}
            if classified.action:
                body["action"] = classified.action
            if self.jsonl:
                body = dict({"schema": sr.EVENT_SCHEMA, "kind": "error"}, **body)
                self._write_line(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
            else:
                out(json.dumps(body, ensure_ascii=False, indent=1))
        warn(msg)
        if classified.action and code == 4:
            warn("处理：%s" % classified.action)
        return code


def _fmt_num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return ("%d" % f) if f == int(f) else ("%.1f" % f)


def _install_sigterm(cancel):
    """POSIX: SIGTERM requests cancellation (final summary, locks released). Returns a restore callable."""
    if os.name == "nt":
        return lambda: None
    try:
        import signal
        import threading

        if threading.current_thread() is not threading.main_thread():
            return lambda: None
        previous = signal.signal(signal.SIGTERM, lambda signum, frame: cancel.set())
    except (ImportError, ValueError, OSError):
        return lambda: None

    def restore():
        try:
            signal.signal(signal.SIGTERM, previous)
        except (ValueError, OSError):
            pass

    return restore


def cmd_sync(args):
    """`relay sync`: exit 0 success, 3 partial (retryable problems left), 4 needs action, 130 cancelled."""
    import threading

    sr = _mod("sync_runtime")
    if args.max_backoff is not None and args.max_backoff < args.interval:
        raise CliError("用法错误：--max-backoff 不能小于 --interval。", 1)
    printer = _SyncOutput(args, sr)
    cancel = threading.Event()
    restore = _install_sigterm(cancel)
    try:
        try:
            path, local = ctx()
            eng = _mod("relay_engine")
            res = eng.sync_network(
                project_key=args.project,
                watch=args.watch,
                interval=args.interval,
                max_cycles=args.max_cycles,
                cwd=os.getcwd(),
                ledger_path=path,
                local=local,
                max_backoff=args.max_backoff if args.max_backoff is not None else max(300.0, args.interval),
                on_event=printer.on_event,
                cancel=cancel,
                mode="watch" if args.watch else "once",
            )
        except KeyboardInterrupt:
            warn("已取消。")
            return 130
        except CliError:
            raise
        except Exception as e:
            return printer.preloop_error(e)
    finally:
        restore()
    printer.summary(res)
    return sr.exit_code_for(res.get("status", ""))


def cmd_send(args):
    path, local = ctx()
    eng = _mod("relay_engine")
    kw = {"reissue": True} if getattr(args, "reissue", False) else {}
    res = _call_engine(
        eng.send_project,
        project_key=args.project,
        to_alias=args.to,
        cwd=os.getcwd(),
        ledger_path=path,
        local=local,
        **kw
    )
    if args.json:
        out(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    if res.get("reissued"):
        out("已重新签发交接：新交接 %s 取代 %s（原交接包和回执保持不变，对方只能领取新交接）。" % (
            res["relayId"], res.get("supersedesRelayId", "")))
    out("交接包已上传并校验通过：%s（%s）" % (res["projectName"], res["projectId"]))
    out("交接编号：%s%s" % (res["relayId"], ("（父交接：%s）" % res["parentRelayId"]) if res.get("parentRelayId") else ""))
    out("流向：%s → %s    大小：%s 字节    归档会话：%s 条" % (
        res["from"], res["to"], res["packageSize"], res["sessionCount"]))
    out("SHA-256：%s" % res["packageSha256"])
    if res.get("excludedFiles"):
        out("已自动排除本地敏感文件：%s" % "、".join(res["excludedFiles"]))
    out(res["message"])
    return 0


def cmd_inbox(args):
    path, local = ctx()
    eng = _mod("relay_engine")
    res = _call_engine(eng.list_inbox, ledger_path=path, local=local)
    if args.json:
        out(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    for rec in res.get("syncedReceipts") or []:
        out("已同步签收回执：%s（项目 %s 已由 %s 签收）" % (
            rec.get("relayId", ""), rec.get("projectId", ""), rec.get("toAlias", "")))
    incoming = res.get("incoming") or []
    if not incoming:
        out("当前没有待领取的交接包。")
        return 0
    for item in incoming:
        if item.get("tampered"):
            out("[警告：签名或校验异常] %s  来源：%s  错误：%s" % (
                item.get("relayId", ""), item.get("fromAlias", "?"), item.get("error", "数据可能被篡改")))
            continue
        ack_tag = " [待补传回执]" if item.get("pendingAck") else ""
        out("%s%s  项目：%s（%s）  来自：%s → %s  分支：%s  未提交：%s  会话：%s" % (
            item.get("relayId", ""),
            ack_tag,
            item.get("projectName", ""),
            item.get("projectId", ""),
            item.get("fromAlias", ""),
            item.get("toAlias", ""),
            item.get("branch") or "无分支",
            item.get("dirty", 0),
            item.get("sessionCount", 0),
        ))
        if item.get("lastTask"):
            out("  最近任务：%s" % item["lastTask"])
        if item.get("nextAction"):
            out("  下一步：%s" % item["nextAction"])
    out("共 %d 个待接棒交接包。运行 relay receive <交接编号> 领取。" % len(incoming))
    return 0


def cmd_receive(args):
    path, local = ctx()
    eng = _mod("relay_engine")
    res = _call_engine(
        eng.receive_relay,
        args.relay_id,
        target_path=args.path,
        into_path=args.into,
        ledger_path=path,
        local=local,
    )
    if args.json:
        out(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    out(res.get("message") or ("接棒完成：%s" % res.get("projectName", "")))
    out("交接编号：%s%s" % (res["relayId"], ("（父交接：%s）" % res["parentRelayId"]) if res.get("parentRelayId") else ""))
    out("落盘目录：%s" % res.get("targetPath", ""))
    if res.get("lastTask"):
        out("最近任务：%s" % res["lastTask"])
    if res.get("nextAction"):
        out("下一步：%s" % res["nextAction"])
    conv = res.get("conversations") or {}
    out("历史对话归档：共 %d 条（新增 %d，更新 %d）。可用 relay transcripts %s 查阅。" % (
        conv.get("total", 0), conv.get("added", 0), conv.get("updated", 0), res.get("projectId", "")))
    out("说明：原设备的本地原生 Session 仍留在原设备上；本机可读取 HANDOFF.md、台账与归档对话开启新会话继续工作。")
    return 0


def cmd_recover(args):
    path, local = ctx()
    eng = _mod("relay_engine")
    action = "rollback" if args.rollback else "status"
    res = _call_engine(
        eng.recover_relay,
        args.relay_id,
        action=action,
        ledger_path=path,
        local=local,
    )
    if args.json:
        out(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    if action == "rollback":
        out("已回滚事务：%s（阶段：%s）" % (res.get("relayId", args.relay_id), res.get("phase", "")))
        if res.get("targetPath"):
            out("目标目录：%s" % res["targetPath"])
        if res.get("alreadyRolledBack"):
            out("说明：该事务此前已回滚，未重复修改。")
        return 0
    if not res.get("exists"):
        out("没有找到与 %s 相关的恢复事务记录。" % res.get("relayId", args.relay_id))
        return 0
    out("恢复事务：%s    状态：%s（阶段：%s）" % (
        res.get("relayId", args.relay_id), res.get("state", ""), res.get("phase", "")))
    if res.get("projectId"):
        out("项目：%s" % res["projectId"])
    if res.get("targetPath"):
        out("目标目录：%s" % res["targetPath"])
    out("可继续恢复：%s    可回滚：%s" % (
        "是" if res.get("canResume") else "否",
        "是" if res.get("canRollback") else "否",
    ))
    if res.get("pendingReceiptUpload"):
        out("提示：本地工作区已就绪，待补传云端签收回执（可运行 relay receive %s）。" % res.get("relayId", args.relay_id))
    for r in res.get("reasons") or []:
        out("  原因：%s" % r)
    return 0


_SEVERITY_LABEL = {"error": "需要处理", "warning": "警告", "info": "提示"}
_DOCTOR_STATUS = {"ok": "正常", "warning": "有警告", "needs_action": "需要处理"}


def _fmt_bytes(n):
    n = int(n or 0)
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= size:
            return "%.1f %s" % (n / float(size), unit)
    return "%d 字节" % n


def _json_error(args, code, message, rc, **extra):
    if getattr(args, "json", False):
        body = {"ok": False, "code": code, "error": message}
        body.update(extra)
        out(json.dumps(body, ensure_ascii=False, indent=1))
    warn(message)
    return rc


def cmd_doctor(args):
    """`relay doctor [PROJECT]`: strictly read-only; exit 0 healthy, 3 warnings only, 4 something needs the user."""
    dx = _mod("diagnostics")
    try:
        rep = dx.run_doctor(project_key=args.project, cwd=os.getcwd())
    except LookupError as e:
        return _json_error(args, "project_not_found", e.args[0] if e.args else str(e), 1)
    if args.json:
        out(json.dumps(rep, ensure_ascii=False, indent=1))
        return dx.exit_code_for(rep["findings"])
    dev = rep.get("device") or {}
    c = rep.get("counts") or {}
    out("relay doctor：本机 %s（%s）  状态：%s（需要处理 %d，警告 %d，提示 %d）" % (
        dev.get("alias") or "未识别", dev.get("deviceId") or "无设备身份", _DOCTOR_STATUS.get(rep["status"], rep["status"]),
        c.get("error", 0), c.get("warning", 0), c.get("info", 0)))
    for pv in rep.get("projects") or []:
        ed = pv.get("editor") or {}
        line = "项目 %s「%s」：当前编辑设备 %s%s" % (pv["projectId"], pv["projectName"], ed.get("name") or ed.get("alias")
                                                or "未知", "（本机）" if ed.get("isHere") else "")
        ho = pv.get("handoff")
        if ho:
            line += "；在途交接 %s：%s → %s" % (ho.get("relayId", ""), ho.get("from", ""), ho.get("to", ""))
        out(line)
        out("  已确认缓存：%s；等待确认：%s；待补发：%s；未知：%s" % tuple(
            "、".join(pv.get(k) or []) or "—" for k in ("confirmedCache", "awaiting", "pendingResend", "unknown")))
    for f in rep.get("findings") or []:
        where = "  ".join(x for x in (
            ("项目 %s" % f["projectId"]) if f.get("projectId") else "",
            ("交接 %s" % f["relayId"]) if f.get("relayId") else "") if x)
        out("[%s] %s%s" % (_SEVERITY_LABEL.get(f["severity"], f["severity"]), f["code"], ("  " + where) if where else ""))
        out("  原因：%s" % f["reason"])
        out("  处理命令：%s" % f["command"])
    if not rep.get("findings"):
        out("未发现需要处理的问题。")
    return dx.exit_code_for(rep["findings"])


def cmd_cache_status(args):
    cp = _mod("cache_prune")
    try:
        res = cp.cache_status(project_key=args.project, cwd=os.getcwd())
    except LookupError as e:
        return _json_error(args, "project_not_found", e.args[0] if e.args else str(e), 1)
    blocked_rc = 4 if any(b.get("severity") == "error" for b in res.get("blocked") or []) else 0
    if args.json:
        out(json.dumps(res, ensure_ascii=False, indent=1))
        return blocked_rc
    pl, sm, ck, rc, lo, pr = (res[k] for k in ("payloads", "signedMetadata", "checkpoints", "recovery", "leftovers",
                                               "prunable"))
    states = pl.get("states") or {}
    out("本机缓存（%s）%s：" % (res["home"], ("，项目 %s" % res["project"]) if res.get("project") else ""))
    out("  快照载荷：%d 个，%s（已验证 %d，损坏或状态不明 %d）；保留 %d，可清理 %d" % (
        pl["count"], _fmt_bytes(pl["bytes"]), states.get("verified", 0),
        sum(v for k, v in states.items() if k != "verified"), pl["retained"], pl["deletable"]))
    out("  签名元数据：%d 个，%s（其中谱系文件 %d 个，始终保留）" % (sm["count"], _fmt_bytes(sm["bytes"]), sm["lineageFiles"]))
    out("  密文检查点：%d 个，%s；保留 %d，可清理 %d" % (ck["count"], _fmt_bytes(ck["bytes"]), ck["kept"], ck["deletable"]))
    out("  恢复数据：%d 个事务，%s（未完成 %d，无法读取 %d；从不清理）" % (
        rc["count"], _fmt_bytes(rc["bytes"]), rc["incomplete"], rc["unreadable"]))
    if lo["count"]:
        out("  上次清理中断留下的目录：%d 个，%s" % (lo["count"], _fmt_bytes(lo["bytes"])))
    for b in res.get("blocked") or []:
        warn("[%s] %s\n  处理命令：%s" % (_SEVERITY_LABEL.get(b["severity"], ""), b["reason"], b["command"]))
    out("预计可释放：%s（运行 relay cache prune --dry-run 查看明细）" % _fmt_bytes(pr["bytes"]))
    return blocked_rc


def _print_prune_items(res):
    for x in res["deletable"]["payloads"]:
        out("  [删除] 快照 %s（项目 %s，%s）：%s" % (x["snapshotId"], x.get("projectId") or "?", _fmt_bytes(x["bytes"]),
                                              x["reasonText"]))
    for x in res["deletable"]["checkpoints"]:
        out("  [删除] 检查点 %s（投递 %s → %s，%s）：%s" % (x["name"][:12], x.get("deliveryId", ""),
                                                   x.get("toDeviceId", ""), _fmt_bytes(x["bytes"]), x["reasonText"]))
    for x in res["deletable"]["leftovers"]:
        out("  [删除] %s（%s）：%s" % (x["name"], _fmt_bytes(x["bytes"]), x["reasonText"]))
    for x in res["kept"]["payloads"]:
        texts = x.get("reasonTexts") or [x["reasonText"]]
        tag = "保留并报告" if x["reason"] in ("corrupt", "unexpected_files", "foreign", "lineageConflict") else "保留"
        out("  [%s] 快照 %s：%s%s" % (tag, x["snapshotId"], "、".join(texts),
                                    ("（%s）" % x["problem"]) if x.get("problem") else ""))
    for x in res["kept"]["checkpoints"]:
        out("  [保留] 检查点 %s（投递 %s）：%s%s" % (x["name"][:12], x.get("deliveryId", "") or "?", x["reasonText"],
                                             ("（%s）" % x["problem"]) if x.get("problem") else ""))


def cmd_cache_prune(args):
    cp = _mod("cache_prune")
    eng = _mod("relay_engine")
    try:
        res = cp.prune(project_key=args.project, cwd=os.getcwd(), apply=bool(args.apply),
                       expect_plan=args.expect_plan or None)
    except LookupError as e:
        return _json_error(args, "project_not_found", e.args[0] if e.args else str(e), 1)
    except cp.PrunePlanChangedError as e:
        return _json_error(args, e.code, e.message, 3, planDigest=e.plan.get("planDigest"), plan=e.plan)
    except eng.RelayError as e:
        return _json_error(args, e.code, e.message, 3 if e.code == "busy" else 1)
    if args.json:
        out(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        c = res["counts"]
        if res["status"] == "blocked":
            warn("本机状态无法读取，已停止清理，没有删除任何内容：")
            for b in res.get("blocked") or []:
                warn("  原因：%s\n  处理命令：%s" % (b["reason"], b["command"]))
        elif res["mode"] == "dry-run":
            out("清理预览（不会删除任何内容）：可删除快照载荷 %d 个、检查点 %d 个、残留目录 %d 个，预计释放 %s。" % (
                c["deletablePayloads"], c["deletableCheckpoints"], c["leftovers"], _fmt_bytes(res["estimatedBytesFreed"])))
            _print_prune_items(res)
            out("计划摘要：%s（确认后运行 relay cache prune --apply --expect-plan %s；计划变化时不会删除）" % (
                res["planDigest"], res["planDigest"]))
        else:
            d = res.get("deleted") or {}
            out("已清理：删除快照载荷 %d 个、检查点 %d 个、残留目录 %d 个，释放 %s。保留 %d 个快照载荷、%d 个检查点。" % (
                len(d.get("payloads") or []), len(d.get("checkpoints") or []), len(d.get("leftovers") or []),
                _fmt_bytes(res.get("bytesFreed")), c["keptPayloads"], c["keptCheckpoints"]))
            for x in res.get("failed") or []:
                warn("  [未删除] %s：%s" % (x.get("snapshotId") or x.get("name"), x.get("error", "")))
            out("签名元数据已保存到 cache/lineage；缓存清单会在下次 relay sync 时重新发布。")
        if c["unclear"]:
            warn("有 %d 个条目损坏或状态不明，已保留，详见 --json 中的 unclear。" % c["unclear"])
    if res["status"] == "blocked":
        return 4 if any(b.get("severity") == "error" for b in res.get("blocked") or []) else 3
    return {"success": 0, "partial": 3}.get(res["status"], 1)


def cmd_transcripts(args):
    path, local = ctx()
    eng = _mod("relay_engine")
    res = _call_engine(
        eng.query_transcripts,
        project_key=args.project,
        session_id=args.session,
        tool=args.tool,
        grep=args.grep,
        cwd=os.getcwd(),
        ledger_path=path,
        local=local,
    )
    if args.json:
        out(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    if args.session:
        md = res.get("markdown")
        if not md:
            raise CliError("在项目 %s 的归档历史中找不到会话：%s" % (res.get("projectId", ""), args.session), 1)
        out(md.rstrip("\n"))
        return 0
    if args.grep:
        matches = res.get("matches") or []
        if not matches:
            out("在项目 %s 的历史对话中未找到匹配「%s」的内容。" % (res.get("projectId", ""), args.grep))
            return 0
        for m in matches:
            snippet = m.get("snippet") or m.get("content") or m.get("text") or ""
            role = (" [%s]" % m["role"]) if m.get("role") else ""
            out("[%s] %s%s  %s：%s" % (
                m.get("tool", ""),
                m.get("sessionId", ""),
                role,
                m.get("title") or "（无标题）",
                snippet,
            ))
        out("共 %d 处匹配。" % len(matches))
        return 0
    sessions = res.get("sessions") or []
    if not sessions:
        out("项目 %s 暂无已归档的历史对话。" % res.get("projectId", ""))
        return 0
    for s in sessions:
        out("[%s] %s  来源：%s  回合：%s  最近使用：%s  版本：%s  %s" % (
            s.get("tool", ""),
            s.get("sessionId", ""),
            s.get("originDevice", ""),
            s.get("turnCount", 0),
            s.get("lastUsed", ""),
            s.get("contentVersion", ""),
            s.get("title") or "（无标题）",
        ))
    out("共 %d 条归档会话。用 --session <ID> 查看全文，或 --grep <关键词> 搜索。" % len(sessions))
    return 0


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
    sp.add_argument("--device")
    sp.add_argument("--name")
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
    sp.add_argument("--for-device", dest="for_device")
    sp.add_argument("--alt-path", action="append")
    sp.add_argument("--tool", choices=PROJECT_TOOLS)
    sp.add_argument("--status", choices=["active", "paused", "blocked", "done"])
    sp.add_argument("--transport", choices=["", "remote", "bundle", "patch", "folder"])
    sp.add_argument("--worktree")
    sp.add_argument("--device")

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
    sp.add_argument("--to")

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
    sp.add_argument("--which")
    sp.add_argument("--add", metavar="ALIAS")
    sp.add_argument("--name")
    sp.add_argument("--os", choices=["windows", "mac", "linux"])
    sp.add_argument("--drop")
    sp.add_argument("--rotate-keys", action="store_true")
    sp.add_argument("--force", action="store_true",
                    help="与 --rotate-keys 同用：即使有在途交接或未完成的恢复事务也换钥（会列出受影响的交接）")
    sp.add_argument("--json", action="store_true")

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

    sp = P("cloud", "云存储辅助命令", None)
    cls = sp.add_subparsers(dest="sub", parser_class=_Parser)
    cls.required = True
    rm = cls.add_parser("remotes", help="列出本机已授权的 rclone 连接")
    rm.set_defaults(func=cmd_cloud_remotes)
    rm.add_argument("--rclone-config")
    rm.add_argument("--json", action="store_true")

    sp = P("pair", "配置云存储并配对多机公钥", cmd_pair)
    sp.add_argument("--remote")
    sp.add_argument("--root")
    sp.add_argument("--provider", choices=["rclone", "local"], default=None)
    sp.add_argument("--rclone-config")
    sp.add_argument("--accept", action="store_true")
    sp.add_argument("--status", action="store_true")
    sp.add_argument("--with", dest="with_device")
    sp.add_argument("--fingerprint")
    sp.add_argument("--alias")
    sp.add_argument("--replace-device", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = P("publish", "打包并发布项目快照到同步网络", cmd_publish)
    sp.add_argument("project", nargs="?")
    sp.add_argument("--json", action="store_true")

    sp = P("sync", "同步云端回执、快照缓存与版本谱系（退出码：0 成功，3 部分失败，4 需要处理，130 已取消）", cmd_sync)
    sp.add_argument("project", nargs="?")
    sp.add_argument("--watch", action="store_true", help="持续同步；暂时性失败按上限退避重试，成功后恢复正常间隔")
    sp.add_argument("--interval", type=float, default=5.0, help="两轮之间的秒数（默认 5）")
    sp.add_argument("--max-cycles", type=int, default=None)
    sp.add_argument("--max-backoff", type=float, default=None,
                    help="暂时性失败时的最长重试间隔秒数（默认 300，不得小于 --interval）")
    so = sp.add_mutually_exclusive_group()
    so.add_argument("--json", action="store_true",
                    help="结束（或中断）时输出一个汇总 JSON 对象；持续同步时也不会逐轮输出")
    so.add_argument("--jsonl", action="store_true",
                    help="逐条输出 JSON Lines 事件，最后一行 kind=summary；可与 --watch 一起使用，不能与 --json 同用")

    sp = P("send", "加密打包并自动上传交棒", cmd_send)
    sp.add_argument("project", nargs="?")
    sp.add_argument("--to")
    sp.add_argument("--reissue", action="store_true",
                    help="目标设备换钥并重新确认后（或本机换钥后），为在途交接的同一快照重新签发：新交接编号、新密文，原交接包保持不变")
    sp.add_argument("--json", action="store_true")

    sp = P("inbox", "查看云端待接棒交接包并同步回执", cmd_inbox)
    sp.add_argument("--json", action="store_true")

    sp = P("receive", "领取、验签解密并恢复云端交接包", cmd_receive)
    sp.add_argument("relay_id")
    sp.add_argument("--path")
    sp.add_argument("--into")
    sp.add_argument("--json", action="store_true")

    sp = P("recover", "查看或回滚中断的接力事务", cmd_recover)
    sp.add_argument("relay_id")
    rg = sp.add_mutually_exclusive_group()
    rg.add_argument("--status", action="store_true")
    rg.add_argument("--rollback", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = P("doctor", "只读诊断：身份、配对、当前编辑位置、未完成交接、恢复事务和最近同步结果（退出码 0/3/4）", cmd_doctor)
    sp.add_argument("project", nargs="?")
    sp.add_argument("--json", action="store_true")

    cp = sub.add_parser("cache", help="本机缓存统计与安全清理")
    css = cp.add_subparsers(dest="cache_cmd", metavar="{status,prune}", parser_class=_Parser)
    css.required = True
    a = css.add_parser("status", help="只读：快照载荷、签名元数据、密文检查点和恢复数据的数量与占用")
    a.set_defaults(func=cmd_cache_status)
    a.add_argument("project", nargs="?")
    a.add_argument("--json", action="store_true")
    a = css.add_parser("prune", help="清理本机缓存；默认 --dry-run 只预览，--apply 在本机锁内重新计算后删除")
    a.set_defaults(func=cmd_cache_prune)
    a.add_argument("project", nargs="?")
    mg = a.add_mutually_exclusive_group()
    mg.add_argument("--dry-run", action="store_true", help="只列出可删除项、保留原因和预计释放空间（默认）")
    mg.add_argument("--apply", action="store_true", help="在本机锁内重新计算引用后删除允许删除的本地缓存")
    a.add_argument("--expect-plan", metavar="DIGEST", help="与 --apply 同用：计划摘要与预览不同就不删除")
    a.add_argument("--json", action="store_true")

    sp = P("transcripts", "查阅或检索跨机归档的历史对话全文", cmd_transcripts)
    sp.add_argument("project", nargs="?")
    sp.add_argument("--session")
    sp.add_argument("--tool", choices=SESSION_TOOLS)
    sp.add_argument("--grep")
    sp.add_argument("--json", action="store_true")
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
    except devices.AmbiguousDeviceError as e:
        warn(_format_candidates_msg(str(e), e.candidates))
        return 1
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
