"""Read-only local facts: git state, state-file presence, Claude/Codex session discovery.

Never touches the network, never runs a git command that writes, never opens
credential files (existence checks only).
"""
import datetime
import json
import ntpath
import os
import posixpath
import re
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import unquote


_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_win_path(s):
    return bool(_WIN_DRIVE_RE.match(s) or s.startswith("\\\\"))


def _home(home):
    return Path(home) if home else Path.home()


def enc_path(p):
    """Claude Code project-directory encoding: every non [A-Za-z0-9] char becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(p))


def _claude_enc_candidates(cwd):
    """Return deduplicated enc_path candidates across raw, NFC/NFD, and realpath aliases."""
    raw = str(cwd or "")
    if not raw:
        return []
    nfc = unicodedata.normalize("NFC", raw)
    nfd = unicodedata.normalize("NFD", raw)
    variants = [raw, nfc, nfd]
    is_foreign = (os.name != "nt" and _is_win_path(raw)) or (
        os.name == "nt" and raw.startswith("/") and not raw.startswith("//")
    )
    if not is_foreign:
        for base in (raw, nfc):
            try:
                rp = os.path.realpath(base)
            except OSError:
                rp = ""
            if rp:
                variants.append(rp)
                variants.append(unicodedata.normalize("NFC", rp))
                variants.append(unicodedata.normalize("NFD", rp))
        if os.name != "nt":
            for v in list(variants):
                for link_prefix, real_prefix in (("/var/", "/private/var/"), ("/tmp/", "/private/tmp/")):
                    if v.startswith(real_prefix):
                        try:
                            if os.path.realpath(link_prefix.rstrip("/")) == real_prefix.rstrip("/"):
                                alias = link_prefix + v[len(real_prefix):]
                                variants.append(alias)
                                variants.append(unicodedata.normalize("NFC", alias))
                                variants.append(unicodedata.normalize("NFD", alias))
                        except OSError:
                            pass
    out = []
    seen = set()
    for v in variants:
        if len(v) > 1 and v.endswith(("/", "\\")) and not re.match(r"^[A-Za-z]:[\\/]$", v):
            v_trimmed = v.rstrip("/\\")
        else:
            v_trimmed = v
        for cand in (v, v_trimmed):
            if not cand:
                continue
            ep = enc_path(cand)
            if ep and ep not in seen:
                seen.add(ep)
                out.append(ep)
    return out


# ---------------------------------------------------------------- git

def _git(path, *args):
    """Run a read-only git command. Returns (returncode, stdout) or (None, '') if unavailable."""
    try:
        r = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-C", path] + list(args),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, ValueError):
        return None, ""
    return r.returncode, (r.stdout or "")


def git_facts(path):
    facts = {"isGit": False, "branch": "", "dirty": 0, "unpushed": None, "remote": "",
             "remotes": [], "worktrees": [], "head": "", "lastCommit": ""}
    if not path or not os.path.isdir(path):
        return facts
    rc, out = _git(path, "rev-parse", "--is-inside-work-tree")
    if rc != 0 or out.strip() != "true":
        return facts
    facts["isGit"] = True

    rc, out = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    branch = out.strip() if rc == 0 else ""
    if branch == "HEAD":
        branch = ""
    if rc != 0:
        # zero commits: abbrev-ref fails, symbolic-ref still knows the unborn branch
        rc2, out2 = _git(path, "symbolic-ref", "--short", "-q", "HEAD")
        branch = out2.strip() if rc2 == 0 else ""
    facts["branch"] = branch

    rc, out = _git(path, "status", "--porcelain")
    if rc == 0:
        facts["dirty"] = len([ln for ln in out.splitlines() if ln.strip()])

    rc, out = _git(path, "remote")
    remotes = [ln.strip() for ln in out.splitlines() if ln.strip()] if rc == 0 else []
    facts["remotes"] = remotes
    if "origin" in remotes:
        facts["remote"] = "origin"
    elif remotes:
        facts["remote"] = remotes[0]

    rc, out = _git(path, "rev-list", "--count", "@{u}..HEAD")
    if rc == 0:
        try:
            facts["unpushed"] = int(out.strip())
        except ValueError:
            facts["unpushed"] = None

    rc, out = _git(path, "worktree", "list", "--porcelain")
    if rc == 0:
        facts["worktrees"] = [ln[len("worktree "):].strip()
                              for ln in out.splitlines() if ln.startswith("worktree ")]

    rc, out = _git(path, "log", "-1", "--format=%h%x09%cd%x09%s", "--date=short")
    if rc == 0 and out.strip():
        parts = out.rstrip("\r\n").split("\t", 2)
        if len(parts) == 3:
            facts["head"] = parts[0]
            facts["lastCommit"] = "%s（%s）：%s" % (parts[0], parts[1], parts[2])
    return facts


# ---------------------------------------------------------------- presence

def _nonempty_dir(p, ignore=()):
    try:
        return any(n not in ignore for n in os.listdir(str(p)))
    except OSError:
        return False


def presence(path, home=None):
    h = _home(home)
    p = Path(path)
    ex = os.path.exists
    cc_memory = any(
        _nonempty_dir(h / ".claude" / "projects" / ep / "memory")
        for ep in _claude_enc_candidates(path)
    )
    out = {
        "claudeMd": ex(p / "CLAUDE.md"),
        "claudeLocal": ex(p / "CLAUDE.local.md"),
        "ccSkills": _nonempty_dir(p / ".claude" / "skills"),
        "ccSettings": ex(p / ".claude" / "settings.json"),
        "ccSettingsLocal": ex(p / ".claude" / "settings.local.json"),
        "ccCmdAgents": os.path.isdir(str(p / ".claude" / "commands"))
        or os.path.isdir(str(p / ".claude" / "agents")),
        "ccMemory": cc_memory,
        "mcpJson": ex(p / ".mcp.json"),
        "agentsMd": ex(p / "AGENTS.md"),
        "cxProject": os.path.isdir(str(p / ".codex")),
        "geminiMd": ex(p / "GEMINI.md"),
        "agProject": _nonempty_dir(p / ".agents") or _nonempty_dir(p / ".agent"),
        "handoffMd": ex(p / "HANDOFF.md"),
        "worktree": False,
        "envFile": ex(p / ".env"),  # existence only, never opened
    }
    if os.path.isdir(str(p)):
        rc, o = _git(str(p), "worktree", "list", "--porcelain")
        if rc == 0:
            out["worktree"] = len([ln for ln in o.splitlines() if ln.startswith("worktree ")]) > 1
    return out


def global_presence(home=None):
    h = _home(home)
    c = h / ".claude"
    x = h / ".codex"
    g = h / ".gemini"
    try:
        sqlite = any(n.lower().endswith(".sqlite") for n in os.listdir(str(x)))
    except OSError:
        sqlite = False
    return {
        "acctSkills": _nonempty_dir(c / "skills" / "synced"),
        "userSkills": _nonempty_dir(c / "skills", ignore=("synced",)),
        "userClaudeMd": os.path.exists(str(c / "CLAUDE.md")),
        "userSettings": os.path.exists(str(c / "settings.json")),
        "ccPlugins": os.path.isdir(str(c / "plugins")),
        "ccProjects": os.path.isdir(str(c / "projects")),
        "ccCreds": os.path.exists(str(h / ".claude.json")),  # existence only
        "cxAgents": os.path.exists(str(x / "AGENTS.md")),
        "cxConfig": os.path.exists(str(x / "config.toml")),
        "cxSkills": _nonempty_dir(x / "skills", ignore=(".system",)),
        "cxSessions": os.path.isdir(str(x / "sessions")),
        "cxAuth": os.path.exists(str(x / "auth.json")),  # existence only
        "cxState": sqlite,
        "agRules": os.path.exists(str(g / "GEMINI.md")),
        "agSkills": _nonempty_dir(g / "config" / "skills"),
        "agPlugins": _nonempty_dir(g / "config" / "plugins"),
        "agMcp": os.path.exists(str(g / "config" / "mcp_config.json")),
        "agSettings": os.path.exists(str(g / "config" / "config.json"))
        or os.path.exists(str(g / "antigravity-cli" / "settings.json")),
        "agBrain": _nonempty_dir(g / "antigravity" / "brain")
        or _nonempty_dir(g / "antigravity-cli" / "brain"),
        "agState": os.path.isdir(str(g / "antigravity" / "conversations"))
        or os.path.exists(str(g / "antigravity" / "conversation_summaries.db")),
        "agAuth": os.path.exists(str(g / "oauth_creds.json"))
        or os.path.exists(str(g / "antigravity-cli" / "oauth_creds.json")),  # existence only
    }


# ---------------------------------------------------------------- sessions

def _date_of(mtime):
    return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


_TITLE_KEYS = ("customTitle", "aiTitle", "summary")
_TITLE_MARKERS = tuple(('"%s"' % k).encode("ascii") for k in _TITLE_KEYS)


def _claude_title(fp):
    """Stream a transcript; last value of each title key wins, priority custom > ai > summary."""
    found = {}
    try:
        with open(fp, "rb") as f:
            for line in f:
                if not any(m in line for m in _TITLE_MARKERS):
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                for k in _TITLE_KEYS:
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        found[k] = v.strip()
    except OSError:
        return ""
    for k in _TITLE_KEYS:
        if k in found:
            return found[k]
    return ""


def claude_sessions(cwd_paths, home=None):
    h = _home(home)
    root = h / ".claude" / "projects"
    by_id = {}
    for cwd in cwd_paths or []:
        if not cwd:
            continue
        for ep in _claude_enc_candidates(cwd):
            d = root / ep
            try:
                names = os.listdir(str(d))
            except OSError:
                continue
            for n in names:
                if not n.endswith(".jsonl"):
                    continue
                sid = n[:-len(".jsonl")]
                fp = d / n
                try:
                    if not fp.is_file():
                        continue
                    mt = fp.stat().st_mtime
                except OSError:
                    continue
                prev = by_id.get(sid)
                if prev is not None and prev["mtime"] >= mt:
                    continue
                by_id[sid] = {
                    "tool": "claude",
                    "sessionId": sid,
                    "title": _claude_title(str(fp)),
                    "cwd": cwd,
                    "lastUsed": _date_of(mt),
                    "mtime": mt,
                }
    out = list(by_id.values())
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return out


_UUID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$")
_CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _norm(p):
    if not p:
        return ""
    s = str(p)
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s)
    if os.name != "nt":
        if _is_win_path(s):
            norm = ntpath.normpath(s).replace("\\", "/").lower()
            if norm.startswith("//") and len(norm) > 2:
                norm = "//" + norm[2:].rstrip("/")
            return norm
        if s.startswith("/"):
            return unicodedata.normalize("NFC", os.path.realpath(s))
        s = s.replace("\\", "/")
        while len(s) > 1 and s.endswith("/"):
            s = s[:-1]
        return s
    if s.startswith("/") and not s.startswith("//"):
        return posixpath.normpath(s)
    if _is_win_path(s):
        real = unicodedata.normalize("NFC", os.path.realpath(s)).replace("\\", "/").lower()
        while len(real) > 1 and real.endswith("/") and not re.match(r"^[a-z]:/$", real):
            real = real[:-1]
        return real
    s = s.replace("\\", "/")
    while len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    return s.lower()


def _within(cwd, base):
    c, b = _norm(cwd), _norm(base)
    if not c or not b:
        return False
    if c == b:
        return True
    return c.startswith(b if b.endswith("/") else b + "/")


def _codex_cwd(fp):
    try:
        with open(fp, "rb") as f:
            for i, line in enumerate(f):
                if i >= 8:
                    break
                text = line[:262144].decode("utf-8", errors="replace")
                if '"cwd"' not in text:
                    continue
                try:
                    obj = json.loads(text)
                    pl = obj.get("payload") if isinstance(obj, dict) else None
                    if isinstance(pl, dict) and isinstance(pl.get("cwd"), str):
                        return pl["cwd"]
                    if isinstance(obj, dict) and isinstance(obj.get("cwd"), str):
                        return obj["cwd"]
                except ValueError:
                    pass
                m = _CWD_RE.search(text)
                if m:
                    try:
                        return json.loads('"%s"' % m.group(1))
                    except ValueError:
                        return m.group(1)
    except OSError:
        pass
    return ""


def _codex_titles(idx):
    titles = {}
    try:
        with open(idx, "rb") as f:
            for line in f:
                try:
                    obj = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("id"), str):
                    titles[obj["id"]] = obj.get("thread_name") or ""
    except OSError:
        pass
    return titles


def codex_sessions(cwd_paths, home=None):
    h = _home(home)
    bases = [p for p in (cwd_paths or []) if p]
    if not bases:
        return []
    root = h / ".codex" / "sessions"
    titles = _codex_titles(str(h / ".codex" / "session_index.jsonl"))
    out = []
    for dp, _dirs, files in os.walk(str(root)):
        for n in files:
            if not (n.startswith("rollout-") and n.endswith(".jsonl")):
                continue
            m = _UUID_RE.search(n)
            if not m:
                continue
            fp = os.path.join(dp, n)
            cwd = _codex_cwd(fp)
            if not cwd or not any(_within(cwd, b) for b in bases):
                continue
            try:
                mt = os.stat(fp).st_mtime
            except OSError:
                continue
            sid = m.group(1)
            out.append({"tool": "codex", "sessionId": sid, "title": titles.get(sid, ""),
                        "cwd": cwd, "lastUsed": _date_of(mt), "mtime": mt})
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return out


_ANTI_TITLE_KEYS = ("customTitle", "title", "aiTitle", "summary")
_ANTI_TITLE_MARKERS = tuple(('"%s"' % k).encode("ascii") for k in _ANTI_TITLE_KEYS)
_USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_AT_MENTION_RE = re.compile(r"@\[([^\]]+)\]")
_META_PATH_RE = re.compile(
    r"(?:Active [Ww]orkspace|active workspace is|[Ww]orkspace(?: [Pp]ath)?|[Ww]orking directory|is a \[(?:Directory|File)\])\s*:\s*\r?\n?\s*([^\r\n<]+)"
)


def _clean_path_str(s):
    s = str(s or "").strip()
    low = s.lower()
    if low.startswith("file:///"):
        rest = unquote(s[len("file:///"):])
        s = rest if re.match(r"^[A-Za-z]:", rest) else ("/" + rest.lstrip("/"))
    elif low.startswith("file://"):
        s = unquote(s[len("file://"):])
    return s


def _unwrap_arg(v):
    if not isinstance(v, str):
        return ""
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        try:
            u = json.loads(v)
            if isinstance(u, str):
                return u
        except ValueError:
            pass
    return v


def _extract_anti_prompt(content):
    m = _USER_REQ_RE.search(content)
    body = (m.group(1) if m else content).strip()
    if not body:
        return ""
    stripped = re.sub(r"@\[[^\]]+\]\s*", "", body).strip()
    use = stripped if stripped else body
    for ln in use.splitlines():
        ln = re.sub(r"\s+", " ", ln.strip())
        if ln:
            return ln[:60]
    return ""


def _match_anti_base(cand, norm_bases, keep_dir=False):
    cleaned = _clean_path_str(cand)
    if not cleaned:
        return ""
    for orig_b, nb in norm_bases:
        if _within(cleaned, nb):
            return cleaned if keep_dir else orig_b
    return ""


def _anti_inspect(fp, norm_bases):
    matched_cwd = ""
    explicit = False
    found_titles = {}
    first_prompt = None
    try:
        with open(fp, "rb") as f:
            for raw in f:
                has_title = any(m in raw for m in _ANTI_TITLE_MARKERS)
                if matched_cwd and explicit and first_prompt is not None and not has_title:
                    continue
                if (b'"type":"GENERIC"' in raw or b'"type": "GENERIC"' in raw) and not has_title:
                    continue
                text = raw[:524288].decode("utf-8", errors="replace")
                try:
                    obj = json.loads(text)
                except ValueError:
                    if not explicit:
                        m = _CWD_RE.search(text)
                        if m:
                            try:
                                cand = json.loads('"%s"' % m.group(1))
                            except ValueError:
                                cand = m.group(1)
                            hit = _match_anti_base(cand, norm_bases, keep_dir=True)
                            if hit:
                                matched_cwd = hit
                                explicit = True
                    continue
                if not isinstance(obj, dict):
                    continue
                if has_title:
                    for k in _ANTI_TITLE_KEYS:
                        v = obj.get(k)
                        if isinstance(v, str) and v.strip():
                            found_titles[k] = v.strip()
                if not explicit:
                    for container in (obj, obj.get("payload"), obj.get("metadata")):
                        if not isinstance(container, dict):
                            continue
                        for k in ("cwd", "workspace", "workspacePath", "workspaceUri", "projectPath"):
                            val = container.get(k)
                            if isinstance(val, str) and val.strip():
                                hit = _match_anti_base(val, norm_bases, keep_dir=(k != "workspaceUri"))
                                if hit:
                                    matched_cwd = hit
                                    explicit = True
                                    break
                        if explicit:
                            break
                        for lk in ("workspaceUris", "workspaces"):
                            vals = container.get(lk)
                            if isinstance(vals, list):
                                for val in vals:
                                    if isinstance(val, str):
                                        hit = _match_anti_base(val, norm_bases, keep_dir=False)
                                        if hit:
                                            matched_cwd = hit
                                            explicit = True
                                            break
                            if explicit:
                                break
                stype = obj.get("type")
                if stype == "USER_INPUT":
                    content = obj.get("content")
                    if isinstance(content, str) and content:
                        if first_prompt is None:
                            first_prompt = _extract_anti_prompt(content)
                        if not explicit:
                            for m in _AT_MENTION_RE.finditer(content):
                                hit = _match_anti_base(m.group(1), norm_bases, keep_dir=False)
                                if hit:
                                    matched_cwd = hit
                                    explicit = True
                                    break
                        if not explicit:
                            for m in _META_PATH_RE.finditer(content):
                                hit = _match_anti_base(m.group(1), norm_bases, keep_dir=False)
                                if hit:
                                    matched_cwd = hit
                                    explicit = True
                                    break
                elif stype == "PLANNER_RESPONSE" and not matched_cwd:
                    for tc in obj.get("tool_calls") or []:
                        if not isinstance(tc, dict):
                            continue
                        args = tc.get("args")
                        if not isinstance(args, dict):
                            continue
                        for k in ("Cwd", "cwd", "Directory", "directory", "Path", "path", "SearchPath", "TargetFile"):
                            if k in args:
                                u = _unwrap_arg(args[k])
                                hit = _match_anti_base(u, norm_bases, keep_dir=(k in ("Cwd", "cwd", "Directory", "directory")))
                                if hit:
                                    matched_cwd = hit
                                    break
                        if matched_cwd:
                            break
    except OSError:
        return "", ""
    title = ""
    for k in _ANTI_TITLE_KEYS:
        if k in found_titles:
            title = found_titles[k]
            break
    if not title and first_prompt:
        title = first_prompt
    return matched_cwd, title


def anti_sessions(cwd_paths, home=None):
    """Discover Antigravity conversations under ~/.gemini/antigravity(-cli)/brain/<id>/."""
    h = _home(home)
    bases = [p for p in (cwd_paths or []) if p]
    if not bases:
        return []
    norm_bases = sorted([(b, _norm(b)) for b in bases if _norm(b)], key=lambda x: len(x[1]), reverse=True)
    if not norm_bases:
        return []
    roots = [
        h / ".gemini" / "antigravity" / "brain",
        h / ".gemini" / "antigravity-cli" / "brain",
    ]
    by_id = {}
    for root in roots:
        try:
            names = os.listdir(str(root))
        except OSError:
            continue
        for sid in names:
            if sid.startswith("."):
                continue
            logs_dir = root / sid / ".system_generated" / "logs"
            fp = logs_dir / "transcript.jsonl"
            try:
                if not fp.is_file():
                    fp = logs_dir / "transcript_full.jsonl"
                    if not fp.is_file():
                        continue
                mt = fp.stat().st_mtime
            except OSError:
                continue
            if sid in by_id and by_id[sid]["mtime"] >= mt:
                continue
            cwd, title = _anti_inspect(str(fp), norm_bases)
            if not cwd:
                continue
            by_id[sid] = {
                "tool": "anti",
                "sessionId": sid,
                "title": title,
                "cwd": cwd,
                "lastUsed": _date_of(mt),
                "mtime": mt,
            }
    out = list(by_id.values())
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return out
