# -*- coding: utf-8 -*-
"""Unified input validation and on-disk containment checks for relay-desk.

One place for the rules every layer applies before it trusts a value:

* identifiers (``versionId``, ``projectId``, ``deviceId``, relay/delivery IDs,
  parent version lists) are checked for type, length, charset, Unicode
  normalization and path legality, even after a signature has verified;
* relative paths taken from payload manifests or recovery journals are
  normalized and rejected when absolute, traversing or malformed;
* write destinations are checked to resolve inside their designated root with
  no symlinked component in between;
* local state files distinguish "never created" (``None``) from "corrupt or
  unreadable" (:class:`LocalStateCorruptError`).

Stdlib only and free of other ``relaydesk`` imports so every module can use it.
Python 3.9 compatible.
"""
import json
import os
import re
import stat
import unicodedata
from typing import Any, Dict, List, Optional

__all__ = [
    "CATEGORY_INTEGRITY",
    "CATEGORY_NEEDS_ACTION",
    "ValidationError",
    "InvalidIdentifierError",
    "FieldMismatchError",
    "PathEscapeError",
    "LocalPathError",
    "LocalStateCorruptError",
    "TOKEN_MAX_LEN",
    "DELIVERY_ID_MAX_LEN",
    "PROJECT_ID_MAX_LEN",
    "MAX_PARENT_VERSIONS",
    "validate_token",
    "validate_optional_token",
    "validate_version_id",
    "validate_device_id",
    "validate_delivery_id",
    "validate_project_id",
    "validate_parent_version_ids",
    "validate_snapshot_fields",
    "require_equal",
    "normalize_rel_path",
    "check_symlink_target",
    "ensure_contained",
    "safe_join",
    "read_local_json_object",
]

# Error categories shared with later phases (sync error classification).
CATEGORY_INTEGRITY = "integrity"              # data failed validation; never fixed by retrying
CATEGORY_NEEDS_ACTION = "needs_user_action"   # local state needs the user to restore or repair it

TOKEN_MAX_LEN = 128
DELIVERY_ID_MAX_LEN = 160
PROJECT_ID_MAX_LEN = 128
MAX_PARENT_VERSIONS = 64

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + ["COM%d" % i for i in range(10)]
    + ["LPT%d" % i for i in range(10)]
)


# ---------------------------------------------------------------- errors


class ValidationError(ValueError):
    """A value failed validation. Integrity category: retrying cannot fix it."""

    category = CATEGORY_INTEGRITY
    default_code = "invalid_input"

    def __init__(self, message: str, code: Optional[str] = None, field: str = "") -> None:
        ValueError.__init__(self, message)
        self.message = message
        self.code = code or self.default_code
        self.field = field


class InvalidIdentifierError(ValidationError):
    """An identifier has the wrong type, length, charset or would be unsafe as a path."""

    default_code = "invalid_identifier"


class FieldMismatchError(ValidationError):
    """Two fields that must agree (signed record vs envelope, key path, signer) differ."""

    default_code = "field_mismatch"


class PathEscapeError(ValidationError):
    """A path is absolute, traverses, or would resolve outside its designated root."""

    default_code = "path_escape"


class LocalPathError(PathEscapeError):
    """A relay-desk *local* directory (cache, archives, ...) is reached through a symlink.

    This is the local environment, not the data: nothing about the delivery or
    payload is wrong, so it needs the user (``needs_user_action``) and the same
    delivery can be processed again once the directory is fixed. Still a
    :class:`PathEscapeError`, so every existing refusal path keeps refusing.
    ``link`` is the symlinked component, ``target`` where it currently resolves
    (``""`` when the link dangles).
    """

    category = CATEGORY_NEEDS_ACTION
    default_code = "local_dir_symlink"

    def __init__(self, message: str, code: Optional[str] = None, field: str = "", link: str = "",
                 target: str = "") -> None:
        PathEscapeError.__init__(self, message, code=code, field=field)
        self.link = str(link or "")
        self.target = str(target or "")


class LocalStateCorruptError(Exception):
    """A local state file exists but cannot be read or parsed.

    Never-created state is reported as ``None`` by :func:`read_local_json_object`;
    this error means the file is present and must not be replaced by defaults.
    """

    category = CATEGORY_NEEDS_ACTION

    def __init__(self, message: str, path: str = "", kind: str = "", code: str = "local_state_corrupted") -> None:
        Exception.__init__(self, message)
        self.message = message
        self.path = str(path or "")
        self.kind = kind
        self.code = code


def _show(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


# ---------------------------------------------------------------- identifiers


def _is_windows_reserved(name: str) -> bool:
    stem = name.split(".", 1)[0].rstrip(" ").upper()
    return stem in _WINDOWS_RESERVED


def validate_token(value: Any, field: str, max_len: int = TOKEN_MAX_LEN) -> str:
    """Validate a machine identifier usable as one path segment and one object-key segment.

    Allowed: ASCII letters, digits, ``_``, ``-`` and ``.``; starts with a letter
    or digit; no ``..``; no trailing ``.``; not a Windows device name.
    """
    if not isinstance(value, str):
        raise InvalidIdentifierError("%s 必须是字符串，实际为 %s。" % (field, type(value).__name__), field=field)
    if not value:
        raise InvalidIdentifierError("%s 不能为空。" % field, field=field)
    if len(value) > max_len:
        raise InvalidIdentifierError("%s 超过 %d 个字符。" % (field, max_len), field=field)
    if not _TOKEN_RE.match(value):
        raise InvalidIdentifierError(
            "%s 含非法字符（只允许字母、数字、下划线、点、连字符，且以字母或数字开头）：%s" % (field, _show(value)),
            field=field,
        )
    if ".." in value or value.endswith("."):
        raise InvalidIdentifierError("%s 含连续或结尾的点，可能被当作路径：%s" % (field, _show(value)), field=field)
    if _is_windows_reserved(value):
        raise InvalidIdentifierError("%s 是 Windows 保留设备名：%s" % (field, _show(value)), field=field)
    return value


def validate_optional_token(value: Any, field: str, max_len: int = TOKEN_MAX_LEN) -> str:
    """Like :func:`validate_token` but ``None`` and ``""`` mean "absent" and return ``""``."""
    if value is None or value == "":
        return ""
    return validate_token(value, field, max_len=max_len)


def validate_version_id(value: Any, field: str = "versionId") -> str:
    return validate_token(value, field)


def validate_device_id(value: Any, field: str = "deviceId") -> str:
    return validate_token(value, field)


def validate_delivery_id(value: Any, field: str = "deliveryId") -> str:
    # Delivery IDs embed a version ID plus a prefix and device suffix ("fwd_<versionId>_<6>").
    return validate_token(value, field, max_len=DELIVERY_ID_MAX_LEN)


def validate_project_id(value: Any, field: str = "projectId") -> str:
    """Validate a project ID. More permissive than tokens: Unicode letters are allowed.

    Rejects path separators, control/format characters, text that changes under
    NFKC (NFD forms, fullwidth dots or slashes), leading/trailing dots or spaces,
    ``..`` and Windows device names.
    """
    if not isinstance(value, str):
        raise InvalidIdentifierError("%s 必须是字符串，实际为 %s。" % (field, type(value).__name__), field=field)
    if not value:
        raise InvalidIdentifierError("%s 不能为空。" % field, field=field)
    if len(value) > PROJECT_ID_MAX_LEN:
        raise InvalidIdentifierError("%s 超过 %d 个字符。" % (field, PROJECT_ID_MAX_LEN), field=field)
    if unicodedata.normalize("NFKC", value) != value:
        raise InvalidIdentifierError(
            "%s 含需要 Unicode 规范化的字符（如分解形式或全角符号）：%s" % (field, _show(value)), field=field
        )
    for ch in value:
        if ch in "_-. ":
            continue
        if unicodedata.category(ch)[0] in ("L", "N", "M"):
            continue
        raise InvalidIdentifierError("%s 含非法字符 %r：%s" % (field, ch, _show(value)), field=field)
    if value[0] in ". -" or value[-1] in ". ":
        raise InvalidIdentifierError("%s 不能以点、空格或连字符开头，也不能以点或空格结尾：%s" % (field, _show(value)), field=field)
    if ".." in value:
        raise InvalidIdentifierError("%s 含连续的点，可能被当作路径：%s" % (field, _show(value)), field=field)
    if _is_windows_reserved(value):
        raise InvalidIdentifierError("%s 是 Windows 保留设备名：%s" % (field, _show(value)), field=field)
    return value


def validate_parent_version_ids(value: Any, self_id: Optional[str] = None, field: str = "parentVersionIds") -> List[str]:
    """Validate a parent version list: list of distinct valid version IDs, never the version itself."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidIdentifierError("%s 必须是列表。" % field, field=field)
    if len(value) > MAX_PARENT_VERSIONS:
        raise InvalidIdentifierError("%s 超过 %d 项。" % (field, MAX_PARENT_VERSIONS), field=field)
    seen = set()
    for idx, parent in enumerate(value):
        validate_version_id(parent, "%s[%d]" % (field, idx))
        if self_id is not None and parent == self_id:
            raise InvalidIdentifierError("%s 把版本自身列为父版本：%s" % (field, parent), field=field)
        if parent in seen:
            raise InvalidIdentifierError("%s 含重复的父版本：%s" % (field, parent), field=field)
        seen.add(parent)
    return list(value)


def validate_snapshot_fields(snapshot: Any) -> Dict[str, Any]:
    """Validate identifiers inside a snapshot record (signed or about to be signed)."""
    if not isinstance(snapshot, dict):
        raise InvalidIdentifierError("快照对象必须是字典。", field="snapshot")
    validate_project_id(snapshot.get("projectId"), "snapshot.projectId")
    vid = validate_version_id(snapshot.get("versionId"), "snapshot.versionId")
    if "authorDeviceId" in snapshot:
        validate_device_id(snapshot.get("authorDeviceId"), "snapshot.authorDeviceId")
    validate_parent_version_ids(snapshot.get("parentVersionIds"), self_id=vid, field="snapshot.parentVersionIds")
    validate_optional_token(snapshot.get("parentRelayId"), "snapshot.parentRelayId")
    validate_optional_token(snapshot.get("relayId"), "snapshot.relayId")
    return snapshot


def require_equal(actual: Any, expected: Any, field: str, context: str = "") -> None:
    """Raise :class:`FieldMismatchError` when two fields that must agree differ."""
    if actual != expected:
        raise FieldMismatchError(
            "%s%s 不一致：期望 %s，实际 %s。" % (context + "：" if context else "", field, _show(expected), _show(actual)),
            field=field,
        )


# ---------------------------------------------------------------- relative paths & containment


def normalize_rel_path(rel_path: Any, field: str = "path", reject_drive: bool = True) -> str:
    """Normalize a manifest/journal relative path to NFC with ``/`` separators.

    Backslashes count as separators. Rejects empty, NUL, absolute, drive-letter
    or UNC paths, and empty, ``.`` or ``..`` segments. ``reject_drive=False``
    keeps names like ``c:notes`` that are ordinary file names on POSIX (used for
    paths of files that already exist on this machine).
    """
    if not isinstance(rel_path, str) or not rel_path:
        raise PathEscapeError("检测到空相对路径。", code="invalid_path", field=field)
    if "\x00" in rel_path:
        raise PathEscapeError("路径包含非法空字节：%s" % _show(rel_path), code="invalid_path", field=field)
    unified = unicodedata.normalize("NFC", rel_path.replace("\\", "/"))
    if unified.startswith("/") or (reject_drive and _DRIVE_RE.match(unified)):
        raise PathEscapeError("不允许使用绝对路径：%s" % unified, field=field)
    parts = unified.split("/")
    for seg in parts:
        if seg == "" or seg == ".":
            raise PathEscapeError("路径包含非法空段或当前目录段：%s" % unified, code="invalid_path", field=field)
        if seg == "..":
            raise PathEscapeError("不允许路径穿越（..）：%s" % unified, field=field)
    return "/".join(parts)


def check_symlink_target(link_rel_path: str, target: Any, field: str = "symlink") -> str:
    """Reject a symlink target that is absolute or lexically escapes the root.

    ``link_rel_path`` is the link's own root-relative location.
    """
    if not isinstance(target, str) or not target or "\x00" in target:
        raise PathEscapeError("符号链接目标无效：%s" % link_rel_path, field=field)
    unified = target.replace("\\", "/")
    if unified.startswith("/") or _DRIVE_RE.match(unified):
        raise PathEscapeError("符号链接指向绝对路径：%s -> %s" % (link_rel_path, target), field=field)
    stack = normalize_rel_path(link_rel_path, field=field).split("/")[:-1]
    for seg in unified.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not stack:
                raise PathEscapeError("符号链接逃逸出目标目录：%s -> %s" % (link_rel_path, target), field=field)
            stack.pop()
        else:
            stack.append(seg)
    return target


def _is_within(child: str, root: str) -> bool:
    c = os.path.normcase(child)
    r = os.path.normcase(root)
    return c == r or c.startswith(r.rstrip(os.sep) + os.sep)


def ensure_contained(root: Any, path: Any, check_final: bool = False, field: str = "path",
                     local: bool = False) -> str:
    """Verify that writing at ``path`` stays inside ``root``.

    Every existing component strictly between ``root`` and the final component
    must be a real directory, not a symlink (a planted symlinked parent would
    redirect the write). With ``check_final`` the final component may not be a
    symlink either (use it for directories that will be written into). The
    deepest existing ancestor must also resolve inside ``realpath(root)``.
    Returns the absolute path.

    ``local=True`` marks ``root`` as one of relay-desk's own local directories
    (never a payload staging area): a symlinked component is then reported as
    :class:`LocalPathError` (the user must fix the directory) instead of an
    integrity failure.
    """
    root_abs = os.path.abspath(os.fspath(root))
    path_abs = os.path.abspath(os.fspath(path))
    try:
        rel = os.path.relpath(path_abs, root_abs)
    except ValueError:
        raise PathEscapeError("写入路径不在指定根目录内：%s（根目录 %s）" % (path_abs, root_abs), field=field)
    if os.path.isabs(rel) or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise PathEscapeError("写入路径不在指定根目录内：%s（根目录 %s）" % (path_abs, root_abs), field=field)
    parts = [] if rel == os.curdir else rel.split(os.sep)
    to_check = parts if check_final else parts[:-1]
    cur = root_abs
    deepest = root_abs
    for part in to_check:
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except (FileNotFoundError, NotADirectoryError):
            break
        if stat.S_ISLNK(st.st_mode):
            message = "写入路径经过符号链接，拒绝写入：%s（链接 %s）" % (path_abs, cur)
            if local:
                target = os.path.realpath(cur) if os.path.exists(cur) else ""
                raise LocalPathError(message, field=field, link=cur, target=target)
            raise PathEscapeError(message, code="symlink_in_path", field=field)
        deepest = cur
    real_root = os.path.realpath(root_abs)
    if not _is_within(os.path.realpath(deepest), real_root):
        raise PathEscapeError("写入路径解析到根目录之外：%s（根目录 %s）" % (path_abs, real_root), field=field)
    return path_abs


def safe_join(root: Any, rel_path: Any, check_final: bool = False, field: str = "path",
              reject_drive: bool = True) -> str:
    """Join a manifest/journal relative path under ``root`` and verify containment."""
    rel = normalize_rel_path(rel_path, field=field, reject_drive=reject_drive)
    full = os.path.join(os.path.abspath(os.fspath(root)), *rel.split("/"))
    return ensure_contained(root, full, check_final=check_final, field=field)


# ---------------------------------------------------------------- local state files


def read_local_json_object(path: Any, kind: str, label: str) -> Optional[Dict[str, Any]]:
    """Read a local JSON object file.

    Returns ``None`` only when the file has never been created. A present file
    that is unreadable, empty, not UTF-8, not JSON, or not a JSON object raises
    :class:`LocalStateCorruptError` so callers never fall back to defaults and
    overwrite it.
    """
    p = os.fspath(path)
    if not os.path.lexists(p):
        return None

    def _corrupt(reason: str) -> LocalStateCorruptError:
        return LocalStateCorruptError(
            "本机%s已损坏或无法读取（%s）：%s。为避免覆盖已有数据，操作已停止；"
            "请先备份并修复或恢复该文件，不要直接删除。" % (label, reason, p),
            path=p,
            kind=kind,
        )

    try:
        with open(p, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        raise _corrupt("读取失败：%s" % (e.strerror or e))
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _corrupt("不是有效的 UTF-8")
    if not text.strip():
        raise _corrupt("文件为空")
    try:
        data = json.loads(text)
    except ValueError as e:
        raise _corrupt("JSON 解析失败：%s" % e)
    if not isinstance(data, dict):
        raise _corrupt("顶层不是对象")
    return data
