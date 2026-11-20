"""Ledger storage for relay-desk: load/save, normalisation, locking, device detection.

Normalisation mirrors normConfig / normProject / defaultConfig in relay-desk.html.
Unknown fields are always preserved.
"""
import copy
import json
import ntpath
import os
import platform
import posixpath
import re
import secrets
import socket
import string
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from relaydesk import devices as _devices
from relaydesk import validation as _validation

APP = "relaydesk"
VERSION = 2

LOCK_WAIT = 5.0    # seconds to wait for the lock
LOCK_STALE = 30.0  # a lock file older than this is considered left over


class LedgerError(Exception):
    """Ledger file cannot be read, written or locked (CLI exit code 2)."""


class LocalConfigCorruptError(LedgerError, _validation.LocalStateCorruptError):
    """config.json exists but cannot be used (code ``local_state_corrupted``)."""

    def __init__(self, message, path=""):
        _validation.LocalStateCorruptError.__init__(self, message, path=path, kind="config")


class _LedgerDeviceConflictError(LedgerError, _devices.DeviceConflictError):
    """Conflict in ledger config['devices'] (satisfies both LedgerError and DeviceConflictError)."""


def tool_dir():
    return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- normalisation

_OS_ALIASES = {
    "windows": "windows",
    "win": "windows",
    "win32": "windows",
    "mac": "mac",
    "macos": "mac",
    "darwin": "mac",
    "osx": "mac",
    "linux": "linux",
}


def _norm_os(val, default_os):
    if isinstance(val, str):
        mapped = _OS_ALIASES.get(val.strip().lower())
        if mapped:
            return mapped
    return default_os


def default_config():
    return {
        "devices": [
            {"id": "a", "name": "设备 A", "os": "windows", "drop": "", "host": "", "deviceId": ""},
            {"id": "b", "name": "设备 B", "os": "mac", "drop": "", "host": "", "deviceId": ""},
        ],
        "reg": {},
    }


def norm_config(c):
    d = default_config()
    c = c if isinstance(c, dict) else {}
    out = dict(c)
    devs = c.get("devices")
    if isinstance(devs, list) and len(devs) >= 1:
        merged = []
        seen_ids = set()
        seen_dids = set()
        seen_names = set()
        for i, x in enumerate(devs):
            if not isinstance(x, dict):
                raise LedgerError("台账配置里的设备条目不是对象。")
            raw_id = str(x.get("id") or "").strip()
            if i < 2 and (not raw_id or raw_id == d["devices"][i]["id"]):
                base = dict(d["devices"][i])
            else:
                fallback_id = raw_id or ("dev%d" % (i + 1))
                base = {
                    "id": fallback_id,
                    "name": x.get("name") if "name" in x else ("设备 %s" % fallback_id),
                    "os": "linux",
                    "drop": "",
                    "host": "",
                    "deviceId": "",
                }
            m = dict(base)
            m.update(x)
            m["id"] = str(m.get("id") or "").strip() or base["id"]
            m["name"] = str(m.get("name") or "")
            m["os"] = _norm_os(m.get("os"), base["os"])
            m["drop"] = str(m.get("drop") or "")
            m["host"] = str(m.get("host") or "")
            m["deviceId"] = str(m.get("deviceId") or "")

            id_key = m["id"].lower()
            if id_key in seen_ids:
                raise _LedgerDeviceConflictError(
                    "台账配置中存在重复的设备别名「%s」。" % m["id"],
                    code="alias_conflict",
                )
            seen_ids.add(id_key)

            did_key = m["deviceId"].strip()
            if did_key:
                if did_key in seen_dids:
                    raise _LedgerDeviceConflictError(
                        "台账配置中存在重复的 deviceId「%s」。" % did_key,
                        code="device_id_conflict",
                    )
                seen_dids.add(did_key)

            name_key = m["name"].strip().lower()
            if name_key:
                if name_key in seen_names:
                    raise _LedgerDeviceConflictError(
                        "台账配置中存在重复的设备名称「%s」。" % m["name"],
                        code="name_conflict",
                    )
                seen_names.add(name_key)

            merged.append(m)
        out["devices"] = merged
    else:
        out["devices"] = d["devices"]
    reg = c.get("reg")
    out["reg"] = dict(reg) if isinstance(reg, dict) else {}
    return out


def _number(v):
    """Like JS Number(v) || 0 (ints stay ints)."""
    if isinstance(v, bool):
        return int(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    if f != f:
        return 0
    return int(f) if f == int(f) else f


def _dict_or_empty(v):
    return dict(v) if isinstance(v, dict) else {}


def norm_project(p, config):
    p = p if isinstance(p, dict) else {}
    devices = config["devices"]
    n = {
        "name": "未命名项目", "device": devices[0]["id"], "branch": "", "worktree": "",
        "remote": "", "tool": "claude", "status": "active", "dirty": 0, "unpushed": None,
        "lastTask": "", "nextAction": "", "note": "", "transport": "", "handoff": None,
        "updatedAt": "", "updatedOn": "",
    }
    n.update(p)
    n["paths"] = _dict_or_empty(p.get("paths"))
    if "pathsByDeviceId" in p:
        n["pathsByDeviceId"] = _dict_or_empty(p.get("pathsByDeviceId"))
    v = {"state": "none", "cmd": "", "note": ""}
    v.update(_dict_or_empty(p.get("verify")))
    n["verify"] = v
    n["reg"] = _dict_or_empty(p.get("reg"))
    for k in ("sessions", "conflicts", "history"):
        n[k] = list(p[k]) if isinstance(p.get(k), list) else []
    n["dirty"] = _number(n.get("dirty"))
    if not any(d["id"] == n["device"] for d in devices):
        n["device"] = devices[0]["id"]
    return n


def _normalize(data):
    config = norm_config(data.get("config"))
    projects = []
    for p in data.get("projects") or []:
        if not isinstance(p, dict):
            raise LedgerError("台账里的项目条目不是对象，文件可能已损坏。")
        if not p.get("id"):
            p = dict(p)
            p["id"] = new_id("p_")
        projects.append(norm_project(p, config))
    out = {"app": APP, "version": data.get("version", VERSION), "config": config, "projects": projects}
    for k, v in data.items():
        if k not in out and k != "app":
            out[k] = v
    return out


# ---------------------------------------------------------------- file access

def load(path):
    path = Path(path)
    if not path.exists():
        return _normalize({"projects": []})
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (ValueError, UnicodeDecodeError) as e:
        raise LedgerError("台账文件不是有效的 JSON：%s（%s）" % (path, e))
    except OSError as e:
        raise LedgerError("读不了台账文件 %s：%s" % (path, e))
    if not isinstance(data, dict):
        raise LedgerError("台账文件格式不对（顶层不是对象）：%s" % path)
    if not isinstance(data.get("projects", []), list):
        raise LedgerError("台账文件格式不对（projects 不是列表）：%s" % path)
    return _normalize(data)


def save(path, ledger):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ledger-", suffix=".tmp")
    except OSError as e:
        raise LedgerError("写不了台账文件 %s：%s" % (path, e))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=1)
            f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        last = None
        for _ in range(20):
            try:
                os.replace(tmp, str(path))
                last = None
                break
            except PermissionError as e:  # Windows: target briefly held open by a reader
                last = e
                time.sleep(0.05)
        if last is not None:
            raise last
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise LedgerError("写不了台账文件 %s：%s" % (path, e))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _Lock(object):
    def __init__(self, path):
        self.lock_path = str(path) + ".lock"
        self.fd = None

    def __enter__(self):
        parent = os.path.dirname(os.path.abspath(self.lock_path))
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            raise LedgerError("无法创建台账目录 %s：%s" % (parent, e))
        start = time.time()
        while True:
            try:
                self.fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(self.fd, str(os.getpid()).encode("ascii"))
                except OSError:
                    pass
                return self
            except (FileExistsError, PermissionError):
                try:
                    age = time.time() - os.stat(self.lock_path).st_mtime
                except OSError:
                    continue  # vanished between the two calls: retry at once
                if age > LOCK_STALE:
                    try:
                        os.remove(self.lock_path)
                    except OSError:
                        pass
                    continue
                if time.time() - start >= LOCK_WAIT:
                    raise LedgerError("台账正被另一个进程占用（锁文件 %s）。稍后再试。" % self.lock_path)
                time.sleep(0.05)
            except OSError as e:
                raise LedgerError("无法创建锁文件 %s：%s" % (self.lock_path, e))

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        try:
            os.remove(self.lock_path)
        except OSError:
            pass
        return False


def update(path, fn):
    """Lock, load, apply fn(ledger) in place, save, unlock. Returns the saved ledger."""
    p = Path(path)
    with _Lock(path):
        existed = p.exists()
        raw_version = None
        if existed:
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    raw_version = raw.get("version")
            except (OSError, ValueError, UnicodeDecodeError):
                raw_version = None
        ledger = load(path)
        fn(ledger)
        ledger["version"] = VERSION
        ledger = _normalize(ledger)
        if existed and (
            isinstance(raw_version, bool)
            or not isinstance(raw_version, int)
            or raw_version < VERSION
        ):
            _devices.ensure_backup_once(p, Path(str(p) + ".v1.backup.json"))
        save(path, ledger)
        return ledger


def rev(path):
    try:
        st = os.stat(str(path))
    except OSError:
        return "0"
    return "%d-%d" % (st.st_mtime_ns, st.st_size)


# ---------------------------------------------------------------- lookup helpers

_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WIN_INVALID_CHARS = frozenset('<>:"|?*')
_WIN_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {"COM%d" % i for i in range(1, 10)}
    | {"LPT%d" % i for i in range(1, 10)}
)


def _is_win_path(p):
    return bool(_WIN_DRIVE_RE.match(p) or p.startswith("\\\\"))


def _is_posix_abs(p):
    return p.startswith("/") and not p.startswith("//")


def canonical_local_path(p: str) -> str:
    """Canonicalize a local path without mangling foreign-OS paths."""
    if not p:
        return ""
    s = str(p)
    if not s:
        return ""
    if os.name != "nt" and _is_win_path(s):
        norm = ntpath.normpath(unicodedata.normalize("NFC", s)).lower()
        if norm.startswith("\\\\") and len(norm) > 2:
            norm = "\\\\" + norm[2:].rstrip("\\")
        return norm
    if os.name == "nt" and _is_posix_abs(s):
        return posixpath.normpath(unicodedata.normalize("NFC", s))
    nfc = unicodedata.normalize("NFC", s)
    return unicodedata.normalize("NFC", os.path.realpath(os.path.abspath(nfc)))


def check_case_collisions(rel_paths) -> list:
    """Return sorted (p1, p2) pairs where NFC '/'-normalized p1 != p2 collide under casefold()."""
    buckets = {}
    for raw in rel_paths or []:
        if not raw:
            continue
        s = unicodedata.normalize("NFC", str(raw)).replace("\\", "/")
        s = re.sub(r"/+", "/", s)
        while s.startswith("./"):
            s = s[2:]
        s = s.strip("/")
        if not s:
            continue
        buckets.setdefault(s.casefold(), set()).add(s)
    pairs = []
    for group in buckets.values():
        if len(group) > 1:
            items = sorted(group)
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    pairs.append((items[i], items[j]))
    pairs.sort()
    return pairs


def check_windows_path_issues(rel_paths) -> list:
    """Return (rel_path, reason_zh) for paths violating Windows filename rules."""
    issues = []
    for raw in rel_paths or []:
        if not raw:
            continue
        s = unicodedata.normalize("NFC", str(raw)).replace("\\", "/")
        s = re.sub(r"/+", "/", s)
        while s.startswith("./"):
            s = s[2:]
        s = s.lstrip("/").rstrip("/")
        if not s:
            continue
        reasons = []
        for comp in s.split("/"):
            if not comp or comp in (".", ".."):
                continue
            bad_chars = sorted(set(ch for ch in comp if ch in _WIN_INVALID_CHARS))
            has_ctrl = any(ord(ch) < 32 for ch in comp)
            if bad_chars:
                reasons.append("路径组件「%s」包含 Windows 非法字符（%s）" % (comp, "".join(bad_chars)))
            if has_ctrl:
                reasons.append("路径组件「%s」包含控制字符" % comp)
            if comp.endswith(" ") or comp.endswith("."):
                reasons.append("路径组件「%s」不能以空格或句点结尾" % comp)
            stem = comp.split(".", 1)[0].rstrip(" ").upper()
            if stem in _WIN_RESERVED_NAMES:
                reasons.append("路径组件「%s」使用了 Windows 保留设备名（%s）" % (comp, stem))
        if reasons:
            issues.append((s, "；".join(reasons)))
    return issues


def _norm_path(p):
    c = canonical_local_path(p)
    if not c:
        return ""
    if os.name == "nt" and not _is_posix_abs(c):
        return os.path.normcase(c)
    return c


def _project_dirs(p):
    out = []
    for v in (p.get("paths") or {}).values():
        if isinstance(v, str) and v:
            out.append(v)
    for lst in (p.get("altPaths") or {}).values():
        if isinstance(lst, list):
            out.extend(x for x in lst if isinstance(x, str) and x)
    return out


def _inside(child, parent):
    c, p = _norm_path(child), _norm_path(parent)
    if not c or not p:
        return False
    if c == p:
        return True
    sep = "\\" if _is_win_path(p) else "/"
    return c.startswith(p.rstrip(sep) + sep)


def find_project(ledger, key=None, cwd=None):
    projects = ledger.get("projects") or []
    names = ", ".join(p.get("id", "") for p in projects)
    if key:
        k = key.strip()
        kl = k.lower()
        for p in projects:
            if p.get("id") == k:
                return p
        by_name = [p for p in projects if str(p.get("name", "")).lower() == kl]
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            raise LookupError("名称「%s」对应多个项目：%s。请改用 id。" % (k, ", ".join(p["id"] for p in by_name)))
        by_id_ci = [p for p in projects if str(p.get("id", "")).lower() == kl]
        if len(by_id_ci) == 1:
            return by_id_ci[0]
        pref = [p for p in projects
                if str(p.get("id", "")).lower().startswith(kl) or str(p.get("name", "")).lower().startswith(kl)]
        if len(pref) == 1:
            return pref[0]
        if len(pref) > 1:
            raise LookupError("「%s」匹配到多个项目：%s。请写得更完整一些。" % (k, ", ".join(p["id"] for p in pref)))
        if not projects:
            raise LookupError("找不到项目「%s」：台账里还没有项目，先用 add 登记一个。" % k)
        raise LookupError("找不到项目「%s」。已有项目：%s。" % (k, names))
    cwd = cwd or os.getcwd()
    best, best_len = None, -1
    for p in projects:
        for d in _project_dirs(p):
            if _inside(cwd, d):
                n = len(_norm_path(d))
                if n > best_len:
                    best, best_len = p, n
    if best is not None:
        return best
    if not projects:
        raise LookupError("台账里还没有项目，先用 add 登记一个。")
    raise LookupError("当前目录（%s）不在任何已登记项目的路径里。请指定项目：%s。" % (cwd, names))


def dev(config, dev_id):
    for d in config["devices"]:
        if d["id"] == dev_id:
            return d
    return config["devices"][0]


def other(config, dev_id):
    devices = config["devices"]
    for d in devices:
        if d["id"] != dev_id:
            return d
    if len(devices) > 1:
        return devices[1]
    return devices[0]


# ---------------------------------------------------------------- local config

def local_dir():
    env = os.environ.get("RELAY_DESK_HOME")
    if env:
        return Path(env)
    return Path.home() / ".relay-desk"


def load_local():
    """This machine's config.json: ``{}`` if never created, LedgerError if present but unreadable.

    A damaged file must not silently fall back to ``{}``: the ledger path would
    then default to the tool directory and later writes would go to the wrong
    ledger.
    """
    f = local_dir() / "config.json"
    if not os.path.lexists(str(f)):
        return {}
    try:
        with open(str(f), "r", encoding="utf-8-sig") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        raise LocalConfigCorruptError(
            "本机设备配置 %s 已损坏或无法读取（%s）。为避免写错台账，操作已停止；"
            "请修复该文件，或备份后移走它再重新运行 relay init。" % (f, e),
            path=str(f),
        )
    if not isinstance(d, dict):
        raise LocalConfigCorruptError(
            "本机设备配置 %s 格式不对（顶层不是对象）。请修复该文件，或备份后移走它再重新运行 relay init。" % f,
            path=str(f),
        )
    return d


def save_local(d):
    base = local_dir()
    base.mkdir(parents=True, exist_ok=True)
    f = base / "config.json"
    fd, tmp = tempfile.mkstemp(dir=str(base), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, str(f))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ledger_path(local=None):
    env = os.environ.get("RELAY_DESK_LEDGER")
    if env:
        return Path(env)
    local = load_local() if local is None else local
    if local.get("ledger"):
        return Path(local["ledger"])
    return tool_dir() / "ledger.json"


def hostname():
    return socket.gethostname()


def local_os():
    s = platform.system().lower()
    if s == "windows":
        return "windows"
    if s == "darwin":
        return "mac"
    return "linux"


def _load_identity():
    f = local_dir() / "identity.json"
    try:
        with open(str(f), "r", encoding="utf-8-sig") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def here(ledger, local=None):
    """Device id of this machine, or None."""
    devices = ledger["config"]["devices"]
    ids = [d["id"] for d in devices]
    env = os.environ.get("RELAY_DESK_DEVICE")
    if env:
        env_s = env.strip()
        if env_s in ids:
            return env_s
        by_env_did = [d for d in devices if d.get("deviceId") == env_s]
        if len(by_env_did) == 1:
            return by_env_did[0]["id"]
        by_env_ci = [d for d in devices if d["id"].lower() == env_s.lower()]
        if len(by_env_ci) == 1:
            return by_env_ci[0]["id"]
    local_cfg = load_local() if local is None else local
    local_dev_id = local_cfg.get("deviceId")
    if local_dev_id:
        by_did = [d for d in devices if d.get("deviceId") == local_dev_id]
        if len(by_did) == 1:
            return by_did[0]["id"]
    if local_cfg.get("device") in ids:
        return local_cfg["device"]
    ident = _load_identity()
    ident_did = ident.get("deviceId")
    if ident_did:
        by_did = [d for d in devices if d.get("deviceId") == ident_did]
        if len(by_did) == 1:
            return by_did[0]["id"]
    if ident.get("alias") in ids:
        return ident["alias"]
    host = hostname().lower()
    short = host.split(".")[0]
    host_match = []
    for d in devices:
        h = str(d.get("host") or "").strip().lower()
        if h and (h == host or h == short):
            host_match.append(d)
    if len(host_match) == 1:
        return host_match[0]["id"]
    me = local_os()
    match = [d for d in devices if d.get("os") == me]
    if len(match) == 1:
        return match[0]["id"]
    return None


def now_iso():
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


def touch(project, here_id):
    project["updatedAt"] = now_iso()
    project["updatedOn"] = here_id or ""


def new_id(prefix):
    alphabet = string.ascii_lowercase + string.digits
    return prefix + "".join(secrets.choice(alphabet) for _ in range(8))


def clone(x):
    return copy.deepcopy(x)
