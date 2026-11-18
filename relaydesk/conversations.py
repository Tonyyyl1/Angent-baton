"""Full visible conversation discovery, gap detection, archiving, and cross-device merging.

Supports Claude Code ('claude'), Codex ('codex'), and Antigravity ('anti').
Never reads or packages credential files; omits internal reasoning/thinking fields.
Python 3.9 compatible, standard library only.
"""
import datetime
import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from . import ledger as L
from . import scan

FORMAT_VERSION = 1

TOOL_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "anti": "Antigravity",
}

REASON_LABELS = {
    "missing": "缺失",
    "truncated": "截断",
    "read_error": "读取失败",
}

ROLE_LABELS = {
    "user": "用户",
    "assistant": "助手",
    "tool": "工具",
    "system": "系统",
}

_CLAUDE_TITLE_KEYS = ("customTitle", "aiTitle", "summary")
_ANTI_TITLE_KEYS = ("customTitle", "title", "aiTitle", "summary")
_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)
_CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
_WS_URI_RE = re.compile(
    r'"(?:workspaceUri|workspacePath|workspace|projectPath)"\s*:\s*"((?:[^"\\]|\\.)*)"'
)
_USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_ADD_META_RE = re.compile(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", re.DOTALL)
_AT_MENTION_RE = re.compile(r"@\[([^\]]+)\]")
_META_PATH_RE = re.compile(
    r"(?:Active [Ww]orkspace|active workspace is|[Ww]orkspace(?: [Pp]ath)?|"
    r"[Ww]orking directory|is a \[(?:Directory|File)\])\s*:\s*\r?\n?\s*([^\r\n<]+)"
)
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

_PRIMARY_ARG_KEYS = (
    "CommandLine",
    "command",
    "cmd",
    "Path",
    "file_path",
    "path",
    "TargetFile",
    "SearchPath",
    "Directory",
    "directory",
    "Cwd",
    "cwd",
    "query",
    "pattern",
    "url",
)


class ConversationGapError(Exception):
    """Raised when registered or discovered local sessions have missing/truncated/unreadable gaps."""

    def __init__(self, gaps, message=None):
        self.gaps = list(gaps or [])
        text = message or self._format_message(self.gaps)
        Exception.__init__(self, text)

    @staticmethod
    def _format_message(gaps):
        if not gaps:
            return "检测到会话缺口，请检查本地会话文件后重试。"
        lines = ["检测到 %d 处会话缺口，无法完整归档对话，请补齐或修复后重试：" % len(gaps)]
        for g in gaps:
            tool = g.get("tool") or ""
            tool_name = TOOL_LABELS.get(tool, tool or "未知工具")
            sid = g.get("sessionId") or "（未知）"
            title = g.get("title") or ""
            reason = g.get("reason") or "unknown"
            reason_zh = REASON_LABELS.get(reason, reason)
            reg_zh = "已登记" if g.get("registered") else "本地发现"
            path = g.get("path") or ""
            detail = g.get("detail") or ""
            title_part = ("「%s」" % title) if title else ""
            path_part = (" [路径：%s]" % path) if path else ""
            lines.append(
                "  - [%s / %s] %s%s（%s）：%s%s"
                % (tool_name, reg_zh, sid, title_part, reason_zh, detail, path_part)
            )
        lines.append(
            "处理建议：请等待对应工具将会话刷盘完成、补齐缺失或截断的日志（如 Antigravity 的 transcript_full.jsonl），"
            "或使用 relay session rm <项目> <sessionId> 移除已失效的登记后再重试。"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------- path helpers


def _resolve_user_home(user_home=None):
    if user_home:
        return Path(user_home)
    env = os.environ.get("RELAY_DESK_USER_HOME")
    if env:
        return Path(env)
    return Path.home()


def _resolve_local_home(local_home=None):
    if local_home:
        return Path(local_home)
    return L.local_dir()


def _is_win_path(p):
    s = str(p or "")
    return bool(re.match(r"^[A-Za-z]:[\\/]", s)) or s.startswith("\\\\")


def _clean_path_str(s):
    s = str(s or "").strip()
    low = s.lower()
    if low.startswith("file:///"):
        rest = unquote(s[len("file:///"):])
        s = rest if re.match(r"^[A-Za-z]:", rest) else ("/" + rest.lstrip("/"))
    elif low.startswith("file://"):
        s = unquote(s[len("file://"):])
    return s


def _norm_for_match(p):
    s = _clean_path_str(p)
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s)
    if _is_win_path(s):
        s = s.replace("\\", "/")
        while len(s) > 1 and s.endswith("/"):
            s = s[:-1]
        return s.lower()
    if os.name == "nt":
        if s.startswith("/") and not s.startswith("//"):
            while len(s) > 1 and s.endswith("/"):
                s = s[:-1]
            return s
        rp = unicodedata.normalize("NFC", os.path.realpath(os.path.abspath(s))).replace("\\", "/")
        while len(rp) > 1 and rp.endswith("/"):
            rp = rp[:-1]
        return rp.lower()
    if s.startswith("/"):
        s = os.path.realpath(s)
    s = unicodedata.normalize("NFC", s).replace("\\", "/")
    while len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    return s


def _within_norm(cand_path, norm_base):
    if not norm_base:
        return False
    c = _norm_for_match(cand_path)
    if not c:
        return False
    if c == norm_base:
        return True
    prefix = norm_base if norm_base.endswith("/") else (norm_base + "/")
    return c.startswith(prefix)


def _expand_cwd_variants(cwds):
    """Return ordered list of (variant_path, display_cwd) pairs covering NFC/NFD and POSIX symlinks."""
    out = []
    seen = set()

    def add(variant, orig):
        if not variant or variant in seen:
            return
        seen.add(variant)
        out.append((variant, orig))

    for raw in cwds or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        orig = raw.strip()
        variants = [
            orig,
            unicodedata.normalize("NFC", orig),
            unicodedata.normalize("NFD", orig),
        ]
        if os.name == "nt":
            if not (orig.startswith("/") and not orig.startswith("//")):
                try:
                    rp = os.path.realpath(os.path.abspath(orig))
                    variants.extend([
                        rp,
                        unicodedata.normalize("NFC", rp),
                        unicodedata.normalize("NFD", rp),
                    ])
                except OSError:
                    pass
        else:
            if not _is_win_path(orig):
                try:
                    rp = os.path.realpath(os.path.abspath(orig))
                    variants.extend([
                        rp,
                        unicodedata.normalize("NFC", rp),
                        unicodedata.normalize("NFD", rp),
                    ])
                    if rp.startswith("/private/var/") and os.path.islink("/var"):
                        alias = "/var/" + rp[len("/private/var/"):]
                        variants.extend([
                            alias,
                            unicodedata.normalize("NFC", alias),
                            unicodedata.normalize("NFD", alias),
                        ])
                    elif rp.startswith("/private/tmp/") and os.path.islink("/tmp"):
                        alias = "/tmp/" + rp[len("/private/tmp/"):]
                        variants.extend([
                            alias,
                            unicodedata.normalize("NFC", alias),
                            unicodedata.normalize("NFD", alias),
                        ])
                except OSError:
                    pass
        for v in variants:
            add(v, unicodedata.normalize("NFC", orig))
    return out


def _date_of(mtime):
    if not mtime or mtime <= 0:
        return ""
    try:
        return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def _safe_filename_part(s):
    raw = str(s or "").strip()
    cleaned = _SAFE_NAME_RE.sub("_", raw).strip("._")
    if not cleaned:
        h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        return "id_" + h
    if cleaned != raw:
        h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        return "%s_%s" % (cleaned[:64], h)
    return cleaned[:96]


def _validate_project_id(project_id):
    pid = str(project_id or "").strip()
    if not pid or pid in (".", "..") or "/" in pid or "\\" in pid:
        raise ValueError("非法的项目 ID：%r" % (project_id,))
    return pid


# ---------------------------------------------------------------- JSONL validation & turn extraction


def _clean_text(s):
    if not isinstance(s, str):
        return ""
    return s.replace("\r\n", "\n").replace("\r", "\n").strip()


def _extract_ts(obj):
    if not isinstance(obj, dict):
        return ""
    for k in ("timestamp", "created_at", "createdAt", "time"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    msg = obj.get("message")
    if isinstance(msg, dict):
        for k in ("timestamp", "created_at"):
            v = msg.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


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


def _summarize_tool_args(args, unwrap=False):
    if args is None:
        return ""
    if isinstance(args, str):
        s = args.strip()
        if not s:
            return ""
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    args = parsed
                else:
                    return re.sub(r"\s+", " ", s)[:120]
            except ValueError:
                return re.sub(r"\s+", " ", s)[:120]
        else:
            return re.sub(r"\s+", " ", s)[:120]
    if isinstance(args, dict):
        for k in _PRIMARY_ARG_KEYS:
            if k in args and args[k] is not None:
                val = args[k]
                if isinstance(val, str):
                    s = _unwrap_arg(val) if unwrap else val
                elif isinstance(val, (int, float, bool)):
                    s = str(val)
                else:
                    continue
                s = re.sub(r"\s+", " ", s.strip())
                if s:
                    return s[:120]
    return ""


def _read_and_validate_jsonl(fp, tool, is_anti_full=True):
    """Read and strictly validate a JSONL transcript file.

    Returns (objs, error_tuple_or_None, mtime).
    error_tuple is (reason, detail_zh) where reason in ('truncated', 'read_error').
    """
    p_str = str(fp)
    mtime = 0.0
    try:
        st = os.stat(p_str)
        mtime = st.st_mtime
        if os.path.isdir(p_str):
            return None, ("read_error", "会话路径是目录而非文件"), mtime
        with open(p_str, "rb") as f:
            raw_bytes = f.read()
    except OSError as e:
        return None, ("read_error", "无法读取会话文件：%s" % e), mtime

    if len(raw_bytes) == 0:
        return None, ("truncated", "会话文件为空（0 字节），可能尚未刷盘"), mtime
    if b"\x00" in raw_bytes:
        return None, ("truncated", "会话文件包含空字节（\\x00），疑似写入中断或截断"), mtime

    objs = []
    for line_no, raw_line in enumerate(raw_bytes.splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            text_line = stripped.decode("utf-8")
        except UnicodeDecodeError as e:
            return None, ("truncated", "第 %d 行 UTF-8 解码失败（疑似写入截断）：%s" % (line_no, e)), mtime
        try:
            obj = json.loads(text_line)
        except ValueError as e:
            return None, ("truncated", "第 %d 行 JSON 解析失败（疑似未刷盘或截断）：%s" % (line_no, e)), mtime
        if not isinstance(obj, dict):
            return None, ("truncated", "第 %d 行不是完整的 JSON 对象" % line_no), mtime
        if tool == "anti" and not is_anti_full:
            tf = obj.get("truncated_fields")
            if isinstance(tf, list) and len(tf) > 0:
                fields_desc = ", ".join(str(x) for x in tf)
                return (
                    None,
                    (
                        "truncated",
                        "transcript.jsonl 含截断字段且缺少 transcript_full.jsonl（第 %d 行：%s）"
                        % (line_no, fields_desc),
                    ),
                    mtime,
                )
        objs.append(obj)

    if not objs:
        return None, ("truncated", "会话文件不包含任何有效 JSON 记录"), mtime
    return objs, None, mtime


def _append_turn(turns, role, ts, content, dedup_adjacent=False):
    clean = _clean_text(content)
    if not clean:
        return
    r = role if role in ("user", "assistant", "tool", "system") else "assistant"
    t_str = str(ts or "")
    if dedup_adjacent and turns:
        prev = turns[-1]
        if prev["role"] == r and prev["content"] == clean:
            if not prev["timestamp"] and t_str:
                prev["timestamp"] = t_str
            return
    turns.append({"role": r, "timestamp": t_str, "content": clean})


def _extract_claude_tool_result(val):
    if isinstance(val, str):
        return _clean_text(val)
    if isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str) and item["text"].strip():
                    parts.append(item["text"].strip())
                elif isinstance(item.get("content"), str) and item["content"].strip():
                    parts.append(item["content"].strip())
        return _clean_text("\n".join(parts))
    if isinstance(val, dict):
        if isinstance(val.get("text"), str):
            return _clean_text(val["text"])
        if isinstance(val.get("content"), str):
            return _clean_text(val["content"])
        return _clean_text(json.dumps(val, ensure_ascii=False))
    return ""


def _parse_claude_objs(objs):
    found_titles = {}
    turns = []
    for obj in objs:
        for k in _CLAUDE_TITLE_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                found_titles[k] = v.strip()

        ts = _extract_ts(obj)
        otype = obj.get("type") or ""
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else None

        if otype == "summary" or (
            isinstance(obj.get("summary"), str)
            and obj.get("summary").strip()
            and msg is None
            and "role" not in obj
            and otype not in ("user", "assistant", "human", "model", "system", "tool")
        ):
            s_text = obj.get("summary", "").strip()
            if s_text:
                _append_turn(turns, "system", ts, "[会话摘要] " + s_text)
            continue

        if otype in ("custom-title", "ai-title", "file-history-snapshot", "queue-operation", "progress"):
            if msg is None and "role" not in obj:
                continue

        raw_role = (msg.get("role") if msg else None) or obj.get("role") or otype
        if raw_role in ("user", "human"):
            role = "user"
        elif raw_role in ("assistant", "model"):
            role = "assistant"
        elif raw_role in ("tool", "tool_result"):
            role = "tool"
        elif raw_role == "system":
            role = "system"
        else:
            continue

        if msg is not None and "content" in msg:
            raw_content = msg.get("content")
        elif "content" in obj:
            raw_content = obj.get("content")
        elif msg is not None and "text" in msg:
            raw_content = msg.get("text")
        else:
            raw_content = obj.get("text")

        if isinstance(raw_content, str):
            _append_turn(turns, role, ts, raw_content)
        elif isinstance(raw_content, list):
            main_parts = []
            tool_result_parts = []
            for block in raw_content:
                if isinstance(block, str):
                    if block.strip():
                        main_parts.append(block.strip())
                elif isinstance(block, dict):
                    btype = block.get("type") or ""
                    if btype in ("thinking", "redacted_thinking"):
                        continue
                    if btype in ("text", "input_text", "output_text") or (
                        not btype and isinstance(block.get("text"), str)
                    ):
                        bt = _clean_text(block.get("text") or "")
                        if bt:
                            main_parts.append(bt)
                    elif btype == "tool_use":
                        tname = str(block.get("name") or "tool")
                        hint = _summarize_tool_args(block.get("input"))
                        main_parts.append(("[调用工具 %s] %s" % (tname, hint)).rstrip())
                    elif btype == "tool_result":
                        tr_text = _extract_claude_tool_result(block.get("content"))
                        if tr_text:
                            prefix = "[工具错误] " if block.get("is_error") else "[工具结果] "
                            tool_result_parts.append(prefix + tr_text)
            if role == "user":
                if tool_result_parts:
                    _append_turn(turns, "tool", ts, "\n".join(tool_result_parts))
                if main_parts:
                    _append_turn(turns, "user", ts, "\n".join(main_parts))
            else:
                combined = main_parts + tool_result_parts
                if combined:
                    _append_turn(turns, role, ts, "\n".join(combined))

    title = ""
    for k in _CLAUDE_TITLE_KEYS:
        if k in found_titles:
            title = found_titles[k]
            break
    return turns, title


def _extract_codex_content(val):
    if isinstance(val, str):
        return _clean_text(val)
    if isinstance(val, list):
        parts = []
        for b in val:
            if isinstance(b, str) and b.strip():
                parts.append(b.strip())
            elif isinstance(b, dict):
                bt = b.get("type") or ""
                if bt in ("reasoning", "thinking", "redacted_thinking"):
                    continue
                if bt in ("input_text", "output_text", "text", "") or "text" in b or "content" in b:
                    if isinstance(b.get("text"), str) and b["text"].strip():
                        parts.append(b["text"].strip())
                    elif isinstance(b.get("content"), str) and b["content"].strip():
                        parts.append(b["content"].strip())
        return _clean_text("\n".join(parts))
    if isinstance(val, dict):
        for k in ("text", "output", "message", "content"):
            if isinstance(val.get(k), str) and val[k].strip():
                return _clean_text(val[k])
        return _clean_text(json.dumps(val, ensure_ascii=False))
    return ""


def _parse_codex_objs(objs):
    found_title = ""
    turns = []
    for obj in objs:
        ts = _extract_ts(obj)
        otype = obj.get("type") or ""
        pl = obj.get("payload") if isinstance(obj.get("payload"), dict) else None

        if otype == "session_meta":
            if pl:
                for k in ("thread_name", "title"):
                    if isinstance(pl.get(k), str) and pl[k].strip():
                        found_title = pl[k].strip()
            continue

        if otype == "event_msg" and pl is not None:
            ev_type = pl.get("type") or ""
            if ev_type == "user_message":
                raw = pl.get("message") if "message" in pl else pl.get("content", pl.get("text"))
                _append_turn(turns, "user", ts, _extract_codex_content(raw), dedup_adjacent=True)
            elif ev_type in ("agent_message", "assistant_message"):
                raw = pl.get("message") if "message" in pl else pl.get("content", pl.get("text"))
                _append_turn(turns, "assistant", ts, _extract_codex_content(raw), dedup_adjacent=True)
            continue

        if pl is not None:
            ptype = pl.get("type") or ""
            if ptype == "message":
                prole = pl.get("role") or ""
                if prole == "developer":
                    continue
                if prole in ("user", "human"):
                    _append_turn(
                        turns, "user", ts, _extract_codex_content(pl.get("content")), dedup_adjacent=True
                    )
                elif prole in ("assistant", "model"):
                    _append_turn(
                        turns, "assistant", ts, _extract_codex_content(pl.get("content")), dedup_adjacent=True
                    )
                elif prole in ("system", "tool"):
                    _append_turn(
                        turns, prole, ts, _extract_codex_content(pl.get("content")), dedup_adjacent=True
                    )
                continue
            if ptype in ("function_call", "custom_tool_call", "local_shell_call"):
                tname = str(pl.get("name") or ("shell" if ptype == "local_shell_call" else "tool"))
                hint = _summarize_tool_args(pl.get("arguments") or pl.get("input") or pl.get("action"))
                _append_turn(turns, "assistant", ts, ("[调用工具 %s] %s" % (tname, hint)).rstrip())
                continue
            if ptype in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                out_txt = _extract_codex_content(pl.get("output"))
                if out_txt:
                    _append_turn(turns, "tool", ts, out_txt)
                continue
            if ptype == "reasoning":
                continue

        if otype == "user_message":
            raw = obj.get("message") if "message" in obj else obj.get("content", obj.get("text"))
            _append_turn(turns, "user", ts, _extract_codex_content(raw), dedup_adjacent=True)
            continue
        if otype in ("agent_message", "assistant_message"):
            raw = obj.get("message") if "message" in obj else obj.get("content", obj.get("text"))
            _append_turn(turns, "assistant", ts, _extract_codex_content(raw), dedup_adjacent=True)
            continue
        if otype in ("function_call", "custom_tool_call", "local_shell_call"):
            tname = str(obj.get("name") or ("shell" if otype == "local_shell_call" else "tool"))
            hint = _summarize_tool_args(obj.get("arguments") or obj.get("input") or obj.get("action"))
            _append_turn(turns, "assistant", ts, ("[调用工具 %s] %s" % (tname, hint)).rstrip())
            continue
        if otype in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
            out_txt = _extract_codex_content(obj.get("output") if "output" in obj else obj.get("content"))
            if out_txt:
                _append_turn(turns, "tool", ts, out_txt)
            continue
        if otype == "reasoning":
            continue

        top_role = obj.get("role")
        if top_role in ("user", "human", "assistant", "model", "tool", "system"):
            if top_role == "developer":
                continue
            mapped = "user" if top_role in ("user", "human") else (
                "assistant" if top_role in ("assistant", "model") else top_role
            )
            raw = obj.get("content") if "content" in obj else obj.get("text")
            _append_turn(turns, mapped, ts, _extract_codex_content(raw), dedup_adjacent=True)

    return turns, found_title


def _extract_anti_user_text(content):
    if not isinstance(content, str) or not content.strip():
        return ""
    reqs = [m.group(1).strip() for m in _USER_REQ_RE.finditer(content) if m.group(1).strip()]
    if reqs:
        return _clean_text("\n\n".join(reqs))
    stripped = _ADD_META_RE.sub("", content).strip()
    return _clean_text(stripped or content)


def _parse_anti_objs(objs):
    found_titles = {}
    first_prompt = None
    turns = []
    for obj in objs:
        for k in _ANTI_TITLE_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                found_titles[k] = v.strip()

        ts = _extract_ts(obj)
        stype = obj.get("type") or ""
        if stype == "USER_INPUT":
            raw_c = obj.get("content")
            if isinstance(raw_c, str) and raw_c.strip():
                if first_prompt is None:
                    first_prompt = scan._extract_anti_prompt(raw_c)
                visible = _extract_anti_user_text(raw_c)
                role = "system" if obj.get("source") == "SYSTEM" and "<USER_REQUEST>" not in raw_c else "user"
                _append_turn(turns, role, ts, visible)
        elif stype == "PLANNER_RESPONSE":
            # Intentionally omit obj.get("thinking") — never archive internal chain-of-thought.
            parts = []
            raw_c = obj.get("content")
            if isinstance(raw_c, str) and raw_c.strip():
                parts.append(_clean_text(raw_c))
            for tc in obj.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tname = str(tc.get("name") or tc.get("tool_name") or tc.get("tool") or "tool")
                raw_args = tc.get("args") if isinstance(tc.get("args"), dict) else tc.get("arguments")
                hint = _summarize_tool_args(raw_args, unwrap=True)
                parts.append(("[调用工具 %s] %s" % (tname, hint)).rstrip())
            if parts:
                _append_turn(turns, "assistant", ts, "\n".join(parts))
        elif stype in ("GENERIC", "TOOL_RESPONSE", "OBSERVATION"):
            raw_c = obj.get("content") if isinstance(obj.get("content"), str) else (
                obj.get("output") if isinstance(obj.get("output"), str) else ""
            )
            if raw_c and raw_c.strip():
                role = "system" if obj.get("source") == "SYSTEM" else "tool"
                _append_turn(turns, role, ts, raw_c)
        elif not stype and obj.get("role") in ("user", "assistant", "tool", "system"):
            raw_c = obj.get("content")
            if isinstance(raw_c, str) and raw_c.strip():
                _append_turn(turns, obj["role"], ts, raw_c)

    title = ""
    for k in _ANTI_TITLE_KEYS:
        if k in found_titles:
            title = found_titles[k]
            break
    if not title and first_prompt:
        title = first_prompt
    return turns, title


def _first_user_title(turns):
    for t in turns:
        if t.get("role") == "user":
            for ln in (t.get("content") or "").splitlines():
                ln = re.sub(r"\s+", " ", ln.strip())
                if ln:
                    return ln[:60]
    return ""


def compute_content_version(turns):
    """Compute deterministic v1_<16hex> hash over canonical turns JSON."""
    normalized = [
        {
            "role": str(t.get("role") or ""),
            "timestamp": str(t.get("timestamp") or ""),
            "content": str(t.get("content") or ""),
        }
        for t in (turns or [])
    ]
    canonical = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "v1_" + hashlib.sha256(canonical).hexdigest()[:16]


# ---------------------------------------------------------------- session discovery for archiving


def _match_anti_cand(cand, norm_bases, keep_dir=False):
    cleaned = _clean_path_str(cand)
    if not cleaned:
        return ""
    for orig_b, nb in norm_bases:
        if _within_norm(cleaned, nb):
            return cleaned if keep_dir else orig_b
    return ""


def _anti_match_file(fp, norm_bases):
    """Inspect an Antigravity transcript (even if partially truncated) to see if it belongs to norm_bases."""
    if not norm_bases:
        return "", ""
    matched_cwd = ""
    explicit = False
    found_titles = {}
    first_prompt = None
    try:
        with open(str(fp), "rb") as f:
            for raw in f:
                text = raw[:524288].decode("utf-8", errors="replace")
                try:
                    obj = json.loads(text)
                except ValueError:
                    if not explicit:
                        for rx, keep in ((_CWD_RE, True), (_WS_URI_RE, False)):
                            m = rx.search(text)
                            if m:
                                try:
                                    cand = json.loads('"%s"' % m.group(1))
                                except ValueError:
                                    cand = m.group(1)
                                hit = _match_anti_cand(cand, norm_bases, keep_dir=keep)
                                if hit:
                                    matched_cwd = hit
                                    explicit = True
                                    break
                    if not explicit:
                        for m in _AT_MENTION_RE.finditer(text):
                            hit = _match_anti_cand(m.group(1), norm_bases, keep_dir=False)
                            if hit:
                                matched_cwd = hit
                                explicit = True
                                break
                    if not explicit:
                        for m in _META_PATH_RE.finditer(text):
                            hit = _match_anti_cand(m.group(1), norm_bases, keep_dir=False)
                            if hit:
                                matched_cwd = hit
                                explicit = True
                                break
                    continue

                if not isinstance(obj, dict):
                    continue
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
                                hit = _match_anti_cand(val, norm_bases, keep_dir=(k != "workspaceUri"))
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
                                        hit = _match_anti_cand(val, norm_bases, keep_dir=False)
                                        if hit:
                                            matched_cwd = hit
                                            explicit = True
                                            break
                            if explicit:
                                break

                stype = obj.get("type")
                if stype == "USER_INPUT":
                    c = obj.get("content")
                    if isinstance(c, str) and c:
                        if first_prompt is None:
                            first_prompt = scan._extract_anti_prompt(c)
                        if not explicit:
                            for m in _AT_MENTION_RE.finditer(c):
                                hit = _match_anti_cand(m.group(1), norm_bases, keep_dir=False)
                                if hit:
                                    matched_cwd = hit
                                    explicit = True
                                    break
                        if not explicit:
                            for m in _META_PATH_RE.finditer(c):
                                hit = _match_anti_cand(m.group(1), norm_bases, keep_dir=False)
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
                                hit = _match_anti_cand(
                                    u, norm_bases, keep_dir=(k in ("Cwd", "cwd", "Directory", "directory"))
                                )
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


def _stat_mtime_safe(fp):
    try:
        return os.stat(str(fp)).st_mtime
    except OSError:
        return 0.0


def collect_project_conversations(project: dict, here_id: str, local_home, user_home=None) -> dict:
    """Discover, validate, and archive all visible conversations for project on here_id,
    merging with any previously archived cross-device conversations.
    """
    project = project if isinstance(project, dict) else {}
    project_id = str(project.get("id") or "")
    u_home = _resolve_user_home(user_home)
    l_home = _resolve_local_home(local_home)

    reg_map = {}
    for s in project.get("sessions") or []:
        if not isinstance(s, dict):
            continue
        if s.get("device") != here_id:
            continue
        if (s.get("kind") or "local") != "local":
            continue
        sid = str(s.get("sessionId") or "").strip()
        tool = str(s.get("tool") or "").strip()
        if not sid or tool not in ("claude", "codex", "anti"):
            continue
        reg_map[(tool, sid)] = s

    base_cwds = []
    p_here = (project.get("paths") or {}).get(here_id)
    if isinstance(p_here, str) and p_here.strip():
        base_cwds.append(p_here.strip())
    for alt in (project.get("altPaths") or {}).get(here_id) or []:
        if isinstance(alt, str) and alt.strip() and alt.strip() not in base_cwds:
            base_cwds.append(alt.strip())

    cwd_variants = _expand_cwd_variants(base_cwds)
    norm_bases = sorted(
        [(b, _norm_for_match(b)) for b in base_cwds if _norm_for_match(b)],
        key=lambda x: len(x[1]),
        reverse=True,
    )

    gaps = []
    # Map (tool, sid) -> {"fp": Path, "cwd": str, "mtime": float, "is_anti_full": bool, "hint_title": str}
    candidates = {}

    # ---------------- 1. Claude Code ----------------
    claude_root = u_home / ".claude" / "projects"
    reg_claude_cwds = [
        str(s.get("cwd")).strip()
        for (t, _sid), s in reg_map.items()
        if t == "claude" and isinstance(s.get("cwd"), str) and str(s.get("cwd")).strip()
    ]
    claude_dir_variants = _expand_cwd_variants(base_cwds + reg_claude_cwds)
    seen_claude_dirs = set()

    for variant_cwd, disp_cwd in claude_dir_variants:
        d = claude_root / scan.enc_path(variant_cwd)
        if not os.path.lexists(str(d)):
            continue
        try:
            real_d = os.path.normcase(os.path.realpath(str(d)))
        except OSError:
            real_d = str(d)
        if real_d in seen_claude_dirs:
            continue
        seen_claude_dirs.add(real_d)
        try:
            names = sorted(os.listdir(str(d)))
        except OSError as e:
            gaps.append({
                "tool": "claude",
                "sessionId": "*",
                "title": "",
                "reason": "read_error",
                "path": str(d),
                "detail": "无法读取 Claude Code 项目会话目录：%s" % e,
                "registered": False,
            })
            continue
        is_project_dir = any(_within_norm(disp_cwd, nb) for _ob, nb in norm_bases) or (disp_cwd in base_cwds)
        for n in names:
            if not n.endswith(".jsonl"):
                continue
            sid = n[:-len(".jsonl")]
            if not sid:
                continue
            fp = d / n
            if os.path.isdir(str(fp)):
                continue
            if not is_project_dir and ("claude", sid) not in reg_map:
                continue
            mt = _stat_mtime_safe(fp)
            prev = candidates.get(("claude", sid))
            if prev is None or mt > prev["mtime"]:
                candidates[("claude", sid)] = {
                    "fp": fp,
                    "cwd": disp_cwd,
                    "mtime": mt,
                    "is_anti_full": True,
                    "hint_title": "",
                }

    # Fallback search across ~/.claude/projects/*/<sid>.jsonl for registered Claude sessions
    for (tool, sid), reg_s in reg_map.items():
        if tool != "claude" or ("claude", sid) in candidates:
            continue
        found_fp = None
        if os.path.isdir(str(claude_root)):
            try:
                for sub in sorted(os.listdir(str(claude_root))):
                    cand_fp = claude_root / sub / (sid + ".jsonl")
                    if os.path.lexists(str(cand_fp)) and not os.path.isdir(str(cand_fp)):
                        found_fp = cand_fp
                        break
            except OSError:
                pass
        if found_fp is not None:
            candidates[("claude", sid)] = {
                "fp": found_fp,
                "cwd": str(reg_s.get("cwd") or (base_cwds[0] if base_cwds else "")),
                "mtime": _stat_mtime_safe(found_fp),
                "is_anti_full": True,
                "hint_title": str(reg_s.get("title") or ""),
            }
        else:
            exp_cwd = str(reg_s.get("cwd") or (base_cwds[0] if base_cwds else ""))
            exp_path = (
                claude_root / scan.enc_path(exp_cwd) / (sid + ".jsonl")
                if exp_cwd
                else claude_root / (sid + ".jsonl")
            )
            gaps.append({
                "tool": "claude",
                "sessionId": sid,
                "title": str(reg_s.get("title") or ""),
                "reason": "missing",
                "path": str(exp_path),
                "detail": "未找到已登记的 Claude Code 会话文件",
                "registered": True,
            })

    # ---------------- 2. Codex ----------------
    codex_root = u_home / ".codex" / "sessions"
    codex_titles = scan._codex_titles(str(u_home / ".codex" / "session_index.jsonl"))
    reg_codex_sids = set(sid for (t, sid) in reg_map if t == "codex")

    if os.path.lexists(str(codex_root)):
        for dp, _dirs, files in os.walk(str(codex_root)):
            for n in sorted(files):
                if not (n.startswith("rollout-") and n.endswith(".jsonl")):
                    continue
                sid = ""
                m = _UUID_RE.search(n)
                if m:
                    sid = m.group(1)
                else:
                    for rsid in reg_codex_sids:
                        if n.endswith("-" + rsid + ".jsonl"):
                            sid = rsid
                            break
                if not sid:
                    continue
                fp = Path(dp) / n
                is_reg = sid in reg_codex_sids
                file_cwd = scan._codex_cwd(str(fp))
                matched_cwd = ""
                if file_cwd:
                    for _ob, nb in norm_bases:
                        if _within_norm(file_cwd, nb):
                            matched_cwd = file_cwd
                            break
                if not is_reg and not matched_cwd:
                    continue
                mt = _stat_mtime_safe(fp)
                prev = candidates.get(("codex", sid))
                if prev is None or mt > prev["mtime"]:
                    candidates[("codex", sid)] = {
                        "fp": fp,
                        "cwd": matched_cwd or file_cwd or (base_cwds[0] if base_cwds else ""),
                        "mtime": mt,
                        "is_anti_full": True,
                        "hint_title": codex_titles.get(sid, ""),
                    }

    for (tool, sid), reg_s in reg_map.items():
        if tool != "codex" or ("codex", sid) in candidates:
            continue
        gaps.append({
            "tool": "codex",
            "sessionId": sid,
            "title": str(reg_s.get("title") or codex_titles.get(sid, "")),
            "reason": "missing",
            "path": str(codex_root),
            "detail": "未找到已登记的 Codex 会话文件（rollout-*-%s.jsonl）" % sid,
            "registered": True,
        })

    # ---------------- 3. Antigravity ----------------
    anti_roots = [
        u_home / ".gemini" / "antigravity" / "brain",
        u_home / ".gemini" / "antigravity-cli" / "brain",
    ]
    reg_anti_sids = set(sid for (t, sid) in reg_map if t == "anti")

    for a_root in anti_roots:
        if not os.path.isdir(str(a_root)):
            continue
        try:
            sids = sorted(os.listdir(str(a_root)))
        except OSError:
            continue
        for sid in sids:
            if sid.startswith("."):
                continue
            logs_dir = a_root / sid / ".system_generated" / "logs"
            full_fp = logs_dir / "transcript_full.jsonl"
            compact_fp = logs_dir / "transcript.jsonl"
            has_full = os.path.lexists(str(full_fp)) and not os.path.isdir(str(full_fp))
            has_compact = os.path.lexists(str(compact_fp)) and not os.path.isdir(str(compact_fp))
            if has_full:
                chosen_fp = full_fp
                is_full = True
            elif has_compact:
                chosen_fp = compact_fp
                is_full = False
            else:
                continue

            is_reg = sid in reg_anti_sids
            matched_cwd, disc_title = _anti_match_file(chosen_fp, norm_bases)
            if not matched_cwd and has_full and has_compact:
                matched_cwd, fb_title = _anti_match_file(compact_fp, norm_bases)
                if not disc_title:
                    disc_title = fb_title
            if not is_reg and not matched_cwd:
                continue

            mt = _stat_mtime_safe(chosen_fp)
            if mt <= 0 and has_compact and chosen_fp != compact_fp:
                mt = _stat_mtime_safe(compact_fp)
            prev = candidates.get(("anti", sid))
            if prev is None or mt > prev["mtime"] or (mt == prev["mtime"] and is_full and not prev["is_anti_full"]):
                candidates[("anti", sid)] = {
                    "fp": chosen_fp,
                    "cwd": matched_cwd or (base_cwds[0] if base_cwds else ""),
                    "mtime": mt,
                    "is_anti_full": is_full,
                    "hint_title": disc_title,
                }

    for (tool, sid), reg_s in reg_map.items():
        if tool != "anti" or ("anti", sid) in candidates:
            continue
        exp_fp = (
            u_home
            / ".gemini"
            / "antigravity"
            / "brain"
            / sid
            / ".system_generated"
            / "logs"
            / "transcript_full.jsonl"
        )
        gaps.append({
            "tool": "anti",
            "sessionId": sid,
            "title": str(reg_s.get("title") or ""),
            "reason": "missing",
            "path": str(exp_fp),
            "detail": "未找到已登记的 Antigravity 会话日志",
            "registered": True,
        })

    # ---------------- 4. Validate & Extract ----------------
    local_entries = []
    for (tool, sid), cand in sorted(candidates.items(), key=lambda kv: kv[0]):
        reg_s = reg_map.get((tool, sid))
        objs, err, mtime = _read_and_validate_jsonl(
            cand["fp"], tool, is_anti_full=cand["is_anti_full"]
        )
        if err is not None:
            reason, detail = err
            gaps.append({
                "tool": tool,
                "sessionId": sid,
                "title": str(cand["hint_title"] or (reg_s.get("title") if reg_s else "") or ""),
                "reason": reason,
                "path": str(cand["fp"]),
                "detail": detail,
                "registered": reg_s is not None,
            })
            continue

        if tool == "claude":
            turns, parsed_title = _parse_claude_objs(objs)
            title = parsed_title or (str(reg_s.get("title")) if reg_s and reg_s.get("title") else "") or _first_user_title(turns)
        elif tool == "codex":
            turns, parsed_title = _parse_codex_objs(objs)
            title = (
                codex_titles.get(sid)
                or parsed_title
                or (str(reg_s.get("title")) if reg_s and reg_s.get("title") else "")
                or _first_user_title(turns)
            )
        else:
            turns, parsed_title = _parse_anti_objs(objs)
            title = (
                parsed_title
                or cand["hint_title"]
                or (str(reg_s.get("title")) if reg_s and reg_s.get("title") else "")
                or _first_user_title(turns)
            )

        last_used = _date_of(mtime or cand["mtime"]) or (
            str(reg_s.get("lastUsed")) if reg_s and reg_s.get("lastUsed") else ""
        )
        entry_cwd = (
            cand["cwd"]
            or (str(reg_s.get("cwd")) if reg_s and reg_s.get("cwd") else "")
            or (base_cwds[0] if base_cwds else "")
        )
        cv = compute_content_version(turns)
        local_entries.append({
            "tool": tool,
            "sessionId": sid,
            "contentVersion": cv,
            "originDevice": str(here_id or ""),
            "title": title,
            "cwd": entry_cwd,
            "lastUsed": last_used,
            "turnCount": len(turns),
            "turns": turns,
        })

    if gaps:
        gaps.sort(key=lambda g: (0 if g.get("registered") else 1, g.get("tool", ""), g.get("sessionId", "")))
        raise ConversationGapError(gaps)

    existing_archive = load_archive(l_home, project_id) if project_id else {"entries": []}
    merged_entries, _stats = _merge_entries(existing_archive.get("entries") or [], local_entries)
    return {
        "formatVersion": FORMAT_VERSION,
        "projectId": project_id,
        "entries": merged_entries,
    }


# ---------------------------------------------------------------- archive merge, persistence & query


def _norm_entry(raw):
    if not isinstance(raw, dict):
        return None
    tool = str(raw.get("tool") or "").strip()
    sid = str(raw.get("sessionId") or "").strip()
    if not tool or not sid:
        return None
    raw_turns = raw.get("turns")
    turns = []
    if isinstance(raw_turns, list):
        for t in raw_turns:
            if not isinstance(t, dict):
                continue
            turns.append({
                "role": str(t.get("role") or "assistant"),
                "timestamp": str(t.get("timestamp") or ""),
                "content": str(t.get("content") or ""),
            })
    cv = str(raw.get("contentVersion") or "").strip() or compute_content_version(turns)
    return {
        "tool": tool,
        "sessionId": sid,
        "contentVersion": cv,
        "originDevice": str(raw.get("originDevice") or ""),
        "title": str(raw.get("title") or ""),
        "cwd": str(raw.get("cwd") or ""),
        "lastUsed": str(raw.get("lastUsed") or ""),
        "turnCount": len(turns),
        "turns": turns,
    }


def _is_strict_turn_prefix(shorter_turns, longer_turns):
    if len(shorter_turns) >= len(longer_turns):
        return False
    for a, b in zip(shorter_turns, longer_turns):
        if a.get("role") != b.get("role") or a.get("content") != b.get("content"):
            return False
        ta = a.get("timestamp") or ""
        tb = b.get("timestamp") or ""
        if ta and tb and ta != tb:
            return False
    return True


def _sort_entries(entries):
    # Sort newest lastUsed first, then tool, sessionId, turnCount desc, contentVersion
    return sorted(
        entries,
        key=lambda e: (
            e.get("lastUsed") or "",
            e.get("turnCount") or 0,
        ),
        reverse=True,
    )


def _deterministic_sort(entries):
    # Deterministic tie-breaking across all fields
    by_id = sorted(
        entries,
        key=lambda e: (
            e.get("tool") or "",
            e.get("sessionId") or "",
            e.get("contentVersion") or "",
        ),
    )
    by_id.sort(key=lambda e: (e.get("lastUsed") or "", e.get("turnCount") or 0), reverse=True)
    return by_id


def _merge_entries(existing_raw, incoming_raw):
    """Merge incoming entries into existing entries.

    Deduplicates by (tool, sessionId, contentVersion), and supersedes strict turn-prefix
    versions of the same (tool, sessionId) while preserving genuinely diverged versions.
    """
    active = []
    for r in existing_raw or []:
        ne = _norm_entry(r)
        if ne is not None:
            # Ensure existing itself has no exact duplicates
            if not any(
                x["tool"] == ne["tool"]
                and x["sessionId"] == ne["sessionId"]
                and x["contentVersion"] == ne["contentVersion"]
                for x in active
            ):
                active.append(ne)

    added = 0
    updated = 0
    skipped = 0

    for r in incoming_raw or []:
        inc = _norm_entry(r)
        if inc is None:
            continue
        tool = inc["tool"]
        sid = inc["sessionId"]
        cv = inc["contentVersion"]

        exact = None
        same_session = []
        for ex in active:
            if ex["tool"] == tool and ex["sessionId"] == sid:
                same_session.append(ex)
                if ex["contentVersion"] == cv:
                    exact = ex

        if exact is not None:
            if not exact["title"] and inc["title"]:
                exact["title"] = inc["title"]
            if not exact["originDevice"] and inc["originDevice"]:
                exact["originDevice"] = inc["originDevice"]
            if not exact["cwd"] and inc["cwd"]:
                exact["cwd"] = inc["cwd"]
            if inc["lastUsed"] > exact["lastUsed"]:
                exact["lastUsed"] = inc["lastUsed"]
            skipped += 1
            continue

        if not same_session:
            active.append(inc)
            added += 1
            continue

        # Check if an existing version already strictly extends inc (inc is an older prefix)
        if any(_is_strict_turn_prefix(inc["turns"], ex["turns"]) for ex in same_session):
            skipped += 1
            continue

        # Check if inc strictly extends one or more existing prefix versions
        superseded = [ex for ex in same_session if _is_strict_turn_prefix(ex["turns"], inc["turns"])]
        if superseded:
            ref = superseded[0]
            if not inc["title"] and ref["title"]:
                inc["title"] = ref["title"]
            if not inc["originDevice"] and ref["originDevice"]:
                inc["originDevice"] = ref["originDevice"]
            if not inc["cwd"] and ref["cwd"]:
                inc["cwd"] = ref["cwd"]
            superseded_cvs = set(ex["contentVersion"] for ex in superseded)
            active = [
                ex
                for ex in active
                if not (ex["tool"] == tool and ex["sessionId"] == sid and ex["contentVersion"] in superseded_cvs)
            ]
            active.append(inc)
            updated += 1
            continue

        # Genuinely diverged conversation version for the same (tool, sessionId): keep both
        active.append(inc)
        added += 1

    merged = _deterministic_sort(active)
    return merged, {"added": added, "updated": updated, "skipped": skipped, "total": len(merged)}


def archive_dir(local_home, project_id: str) -> Path:
    pid = _validate_project_id(project_id)
    return _resolve_local_home(local_home) / "archives" / pid


def load_archive(local_home, project_id: str) -> dict:
    pid = _validate_project_id(project_id)
    adir = archive_dir(local_home, pid)
    idx_path = adir / "index.json"
    if not idx_path.exists():
        return {"formatVersion": FORMAT_VERSION, "projectId": pid, "entries": []}
    try:
        with open(str(idx_path), "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"formatVersion": FORMAT_VERSION, "projectId": pid, "entries": []}
    if not isinstance(data, dict):
        return {"formatVersion": FORMAT_VERSION, "projectId": pid, "entries": []}
    entries = []
    for raw in data.get("entries") or []:
        ne = _norm_entry(raw)
        if ne is not None:
            entries.append(ne)
    return {
        "formatVersion": int(data.get("formatVersion") or FORMAT_VERSION),
        "projectId": str(data.get("projectId") or pid),
        "entries": _deterministic_sort(entries),
    }


def _atomic_write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".conv-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        last_err = None
        for _ in range(20):
            try:
                os.replace(tmp, str(path))
                last_err = None
                break
            except PermissionError as e:
                last_err = e
                time.sleep(0.05)
        if last_err is not None:
            raise last_err
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def render_entry_markdown(entry: dict) -> str:
    """Format a single archived conversation entry into clean Chinese Markdown."""
    e = _norm_entry(entry) or {
        "tool": str((entry or {}).get("tool") or ""),
        "sessionId": str((entry or {}).get("sessionId") or ""),
        "contentVersion": str((entry or {}).get("contentVersion") or ""),
        "originDevice": str((entry or {}).get("originDevice") or ""),
        "title": str((entry or {}).get("title") or ""),
        "cwd": str((entry or {}).get("cwd") or ""),
        "lastUsed": str((entry or {}).get("lastUsed") or ""),
        "turnCount": len((entry or {}).get("turns") or []),
        "turns": list((entry or {}).get("turns") or []),
    }
    tool = e["tool"]
    tool_label = "%s (`%s`)" % (TOOL_LABELS[tool], tool) if tool in TOOL_LABELS else (tool or "未知")
    title = e["title"] or "（无标题）"
    heading_title = e["title"] or e["sessionId"] or "未命名会话"

    lines = [
        "# 历史对话归档：%s" % heading_title,
        "",
        "- **工具**：%s" % tool_label,
        "- **Session ID**：`%s`" % e["sessionId"],
        "- **标题**：%s" % title,
        "- **来源设备**：%s" % (e["originDevice"] or "（未知）"),
        "- **最近使用**：%s" % (e["lastUsed"] or "（未知）"),
        "- **内容版本**：`%s`" % e["contentVersion"],
        "- **回合数**：%d" % e["turnCount"],
    ]
    if e.get("cwd"):
        lines.append("- **原工作目录**：`%s`" % e["cwd"])
    lines.extend(["", "---", "", "## 对话全文", ""])

    turns = e.get("turns") or []
    if not turns:
        lines.append("（暂无可见对话内容）")
        lines.append("")
        return "\n".join(lines)

    for idx, t in enumerate(turns, 1):
        role = str(t.get("role") or "assistant")
        role_zh = ROLE_LABELS.get(role, role)
        ts = str(t.get("timestamp") or "").strip()
        ts_part = (" · %s" % ts) if ts else ""
        lines.append("### 第 %d 回合 · %s (`%s`)%s" % (idx, role_zh, role, ts_part))
        lines.append("")
        lines.append(str(t.get("content") or "").rstrip())
        lines.append("")
    return "\n".join(lines)


def merge_and_save_archive(local_home, project_id: str, incoming_bundle: dict) -> dict:
    """Merge incoming_bundle into local project archive and atomically write index + Markdown files."""
    pid = _validate_project_id(project_id)
    adir = archive_dir(local_home, pid)
    md_dir = adir / "markdown"
    latest_dir = adir / "latest"
    md_dir.mkdir(parents=True, exist_ok=True)
    latest_dir.mkdir(parents=True, exist_ok=True)

    existing = load_archive(local_home, pid)
    inc_entries = (incoming_bundle or {}).get("entries") if isinstance(incoming_bundle, dict) else []
    merged_entries, stats = _merge_entries(existing.get("entries") or [], inc_entries or [])

    active_md_names = set()
    latest_by_session = {}

    for e in merged_entries:
        safe_tool = _safe_filename_part(e["tool"])
        safe_sid = _safe_filename_part(e["sessionId"])
        safe_cv = _safe_filename_part(e["contentVersion"])
        md_name = "%s__%s__%s.md" % (safe_tool, safe_sid, safe_cv)
        active_md_names.add(md_name)

        rendered = render_entry_markdown(e)
        _atomic_write_text(md_dir / md_name, rendered)

        s_key = (e["tool"], e["sessionId"])
        prev_best = latest_by_session.get(s_key)
        if prev_best is None or (
            (e.get("lastUsed") or "", e.get("turnCount") or 0, e.get("contentVersion") or "")
            > (prev_best[0].get("lastUsed") or "", prev_best[0].get("turnCount") or 0, prev_best[0].get("contentVersion") or "")
        ):
            latest_by_session[s_key] = (e, rendered, "%s__%s.md" % (safe_tool, safe_sid))

    # Remove superseded versioned markdown files so no redundant prefix versions remain on disk
    try:
        for fname in os.listdir(str(md_dir)):
            if fname.endswith(".md") and not fname.startswith(".") and fname not in active_md_names:
                try:
                    os.unlink(str(md_dir / fname))
                except OSError:
                    pass
    except OSError:
        pass

    for (_tool, _sid), (_entry, rendered, latest_name) in latest_by_session.items():
        _atomic_write_text(latest_dir / latest_name, rendered)

    idx_path = adir / "index.json"
    idx_payload = {
        "formatVersion": FORMAT_VERSION,
        "projectId": pid,
        "updatedAt": L.now_iso(),
        "entries": merged_entries,
    }
    _atomic_write_text(idx_path, json.dumps(idx_payload, ensure_ascii=False, indent=1) + "\n")

    return {
        "total": stats["total"],
        "added": stats["added"],
        "updated": stats["updated"],
        "skipped": stats["skipped"],
        "archiveDir": str(adir),
        "indexPath": str(idx_path),
        "entries": merged_entries,
    }


def _make_matcher(grep_pattern):
    pat_str = str(grep_pattern or "")
    pat_cf = pat_str.casefold()
    compiled = None
    try:
        compiled = re.compile(pat_str, re.IGNORECASE)
    except re.error:
        compiled = None

    def find_span(text):
        if not text:
            return None
        pos = text.casefold().find(pat_cf)
        if pos >= 0:
            return (pos, pos + len(pat_str))
        if compiled is not None:
            m = compiled.search(text)
            if m:
                return m.span()
        return None

    return find_span


def _build_snippet(text, span, radius=70):
    s, e = span
    start = max(0, s - radius)
    end = min(len(text), e + radius)
    chunk = re.sub(r"\s+", " ", text[start:end]).strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return "%s%s%s" % (prefix, chunk, suffix)


def _entry_summary(e, adir):
    safe_tool = _safe_filename_part(e["tool"])
    safe_sid = _safe_filename_part(e["sessionId"])
    safe_cv = _safe_filename_part(e["contentVersion"])
    return {
        "tool": e["tool"],
        "sessionId": e["sessionId"],
        "contentVersion": e["contentVersion"],
        "originDevice": e["originDevice"],
        "title": e["title"],
        "cwd": e["cwd"],
        "lastUsed": e["lastUsed"],
        "turnCount": e["turnCount"],
        "markdownPath": str(adir / "markdown" / ("%s__%s__%s.md" % (safe_tool, safe_sid, safe_cv))),
        "latestPath": str(adir / "latest" / ("%s__%s.md" % (safe_tool, safe_sid))),
    }


def query_archive(
    local_home,
    project_id: str,
    session_id: Optional[str] = None,
    tool: Optional[str] = None,
    grep: Optional[str] = None,
) -> dict:
    """Query persisted project conversation archive by tool, session_id, and/or grep pattern."""
    pid = _validate_project_id(project_id)
    adir = archive_dir(local_home, pid)
    archive = load_archive(local_home, pid)
    entries = archive.get("entries") or []

    if tool:
        t_filter = str(tool).strip()
        entries = [e for e in entries if e["tool"] == t_filter]

    sid_query = str(session_id).strip() if session_id else ""
    if sid_query:
        exact = [e for e in entries if e["sessionId"] == sid_query]
        if exact:
            entries = exact
        else:
            entries = [e for e in entries if e["sessionId"].lower().startswith(sid_query.lower())]

    grep_query = str(grep).strip() if grep else ""
    matches = []
    if grep_query:
        find_span = _make_matcher(grep_query)
        matched_entries = []
        for e in entries:
            entry_hit = False
            title_text = e.get("title") or ""
            tspan = find_span(title_text)
            if tspan is not None:
                entry_hit = True
                matches.append({
                    "tool": e["tool"],
                    "sessionId": e["sessionId"],
                    "contentVersion": e["contentVersion"],
                    "originDevice": e["originDevice"],
                    "title": title_text,
                    "turnIndex": 0,
                    "role": "title",
                    "timestamp": e.get("lastUsed") or "",
                    "snippet": _build_snippet(title_text, tspan),
                })
            for idx, t in enumerate(e.get("turns") or [], 1):
                c_text = t.get("content") or ""
                cspan = find_span(c_text)
                if cspan is not None:
                    entry_hit = True
                    matches.append({
                        "tool": e["tool"],
                        "sessionId": e["sessionId"],
                        "contentVersion": e["contentVersion"],
                        "originDevice": e["originDevice"],
                        "title": title_text,
                        "turnIndex": idx,
                        "role": t.get("role") or "assistant",
                        "timestamp": t.get("timestamp") or "",
                        "snippet": _build_snippet(c_text, cspan),
                    })
            if entry_hit:
                matched_entries.append(e)
        entries = matched_entries

    sessions = [_entry_summary(e, adir) for e in entries]
    markdown_out = None
    if sid_query and entries:
        markdown_out = "\n\n---\n\n".join(render_entry_markdown(e) for e in entries)

    return {
        "projectId": pid,
        "archiveDir": str(adir),
        "sessions": sessions,
        "matches": matches,
        "markdown": markdown_out,
    }
