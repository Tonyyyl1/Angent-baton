"""Pluggable cloud object storage abstraction for Relay Desk.

Provides a minimal, storage-agnostic ``StorageProvider`` contract plus two
built-in implementations:
- ``RcloneProvider``: delegates authentication and transport to the user's
  ``rclone`` configuration (OneDrive, Google Drive, Dropbox, WebDAV, S3, etc.).
- ``LocalDirProvider``: pure local directory backend for local testing and
  shared-filesystem setups.
"""
import abc
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


class StorageError(Exception):
    """云端存储操作失败的基础异常。"""


class ObjectNotFoundError(StorageError):
    """请求的远端对象不存在。"""

    def __init__(self, message: str, key: Optional[str] = None) -> None:
        super().__init__(message)
        self.key = key


class ObjectInfo(object):
    """远端对象的元数据描述，同时支持属性访问与字典式访问。"""

    _FIELDS = ("key", "size", "sha256", "etag", "mtime")

    def __init__(
        self,
        key: str,
        size: int,
        sha256: Optional[str] = None,
        etag: Optional[str] = None,
        mtime: Optional[str] = None,
    ) -> None:
        self.key = str(key)
        self.size = int(size)
        self.sha256 = sha256
        self.etag = etag
        self.mtime = mtime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "size": self.size,
            "sha256": self.sha256,
            "etag": self.etag,
            "mtime": self.mtime,
        }

    def __getitem__(self, item: str) -> Any:
        d = self.to_dict()
        if item not in d:
            raise KeyError(item)
        return d[item]

    def get(self, item: str, default: Any = None) -> Any:
        return self.to_dict().get(item, default)

    def __contains__(self, item: object) -> bool:
        return item in self._FIELDS

    def keys(self):
        return self.to_dict().keys()

    def values(self):
        return self.to_dict().values()

    def items(self):
        return self.to_dict().items()

    def __iter__(self) -> Iterator[str]:
        return iter(self._FIELDS)

    def __len__(self) -> int:
        return len(self._FIELDS)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ObjectInfo):
            return self.to_dict() == other.to_dict()
        return False

    def __repr__(self) -> str:
        return (
            "ObjectInfo(key=%r, size=%r, sha256=%r, etag=%r, mtime=%r)"
            % (self.key, self.size, self.sha256, self.etag, self.mtime)
        )


def validate_key(remote_key: str, allow_empty: bool = False) -> str:
    """校验并规范化远端对象键（使用 ``/`` 分隔并转为 NFC）。

    - 拒绝包含空字符、反斜杠 ``\\``、``.`` 或 ``..`` 路径段的键，防止越界逃逸。
    - 合并连续 ``/`` 并去除首尾 ``/``。
    - 当 ``allow_empty=False``（默认，用于 upload/download/stat/delete）时拒绝空键。
    """
    if not isinstance(remote_key, str):
        raise StorageError("远端对象键必须是字符串。")
    if "\x00" in remote_key:
        raise StorageError("远端对象键不能包含空字符（NUL）。")
    if "\\" in remote_key:
        raise StorageError("远端对象键不能包含反斜杠（\\）：%r" % (remote_key,))
    norm = unicodedata.normalize("NFC", remote_key)
    parts = [seg for seg in norm.split("/") if seg != ""]
    for seg in parts:
        if seg in (".", ".."):
            raise StorageError("远端对象键不能包含 '.' 或 '..' 路径段：%r" % (remote_key,))
    cleaned = "/".join(parts)
    if not cleaned and not allow_empty:
        raise StorageError("远端对象键不能为空。")
    return cleaned


class StorageProvider(abc.ABC):
    """可插拔云存储提供方抽象基类。"""

    @abc.abstractmethod
    def list_objects(self, prefix: str = "") -> List[ObjectInfo]:
        """递归列出 ``prefix`` 下的所有对象（按 ``key`` 升序）；前缀或根目录不存在时返回 ``[]``。"""
        raise NotImplementedError

    @abc.abstractmethod
    def upload(self, local_path: str, remote_key: str) -> ObjectInfo:
        """将本地文件上传至 ``remote_key``（若已存在则覆盖），返回 ``ObjectInfo``。"""
        raise NotImplementedError

    @abc.abstractmethod
    def download(self, remote_key: str, local_path: str) -> ObjectInfo:
        """原子下载 ``remote_key`` 到 ``local_path``；对象不存在时抛出 ``ObjectNotFoundError``。"""
        raise NotImplementedError

    @abc.abstractmethod
    def stat(self, remote_key: str) -> Optional[ObjectInfo]:
        """查询单个远端文件对象的元数据；不存在时返回 ``None``。"""
        raise NotImplementedError

    @abc.abstractmethod
    def delete(self, remote_key: str) -> bool:
        """删除远端文件对象；存在并删除返回 ``True``，原本不存在返回 ``False``。"""
        raise NotImplementedError


# ---------------------------------------------------------------- internal helpers

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _iso_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_replace(src_tmp: str, dst_path: str) -> None:
    """将同目录下的临时文件原子替换为目标文件，兼容 Windows 短暂句柄占用。"""
    last_err = None  # type: Optional[OSError]
    for _ in range(20):
        try:
            os.replace(src_tmp, dst_path)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(0.05)
        except OSError as e:
            raise StorageError("原子写入目标文件失败（%s）：%s" % (dst_path, e))
    if last_err is not None:
        raise StorageError("原子写入目标文件失败（%s）：%s" % (dst_path, last_err))


# ---------------------------------------------------------------- rclone helpers

def _check_executable(candidate: str) -> Optional[str]:
    if not candidate:
        return None
    expanded = os.path.expanduser(candidate)
    if os.path.sep in expanded or (os.path.altsep and os.path.altsep in expanded):
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        return None
    found = shutil.which(expanded)
    if found:
        return found
    if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
        return os.path.abspath(expanded)
    return None


def find_rclone_binary(rclone_bin: Optional[str] = None) -> Optional[str]:
    """按优先级查找可用的 ``rclone`` 可执行文件路径。

    若显式传入 ``rclone_bin`` 或设置了环境变量 ``RELAY_DESK_RCLONE``，则仅检查该指定路径，
    不再回退到系统默认路径，以便调用方和错误测试可控。
    """
    if rclone_bin is not None and str(rclone_bin).strip() != "":
        return _check_executable(str(rclone_bin).strip())

    env_bin = os.environ.get("RELAY_DESK_RCLONE", "").strip()
    if env_bin:
        return _check_executable(env_bin)

    which_bin = shutil.which("rclone")
    if which_bin:
        return which_bin

    for fallback in ("/opt/homebrew/bin/rclone", "/usr/local/bin/rclone"):
        if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
            return fallback
    return None


def has_rclone(rclone_bin: Optional[str] = None) -> bool:
    """检查当前环境是否已安装可用的 ``rclone``。"""
    return find_rclone_binary(rclone_bin) is not None


def _clean_rclone_err(stderr: str, stdout: str = "") -> str:
    text = (stderr or stdout or "").strip()
    if not text:
        return "未知 rclone 错误"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else text


def discover_rclone_remotes(
    rclone_bin: Optional[str] = None,
    config_path: Optional[str] = None,
) -> List[str]:
    """调用 ``rclone listremotes`` 获取已配置的远端连接名称列表（不含末尾冒号）。"""
    exe = find_rclone_binary(rclone_bin)
    if not exe:
        raise StorageError("未找到 rclone 可执行文件，请先安装 rclone。")

    cmd = [exe]
    cfg = config_path if config_path else os.environ.get("RCLONE_CONFIG")
    if cfg:
        cmd.extend(["--config", str(cfg)])
    cmd.extend(["--ask-password=false", "listremotes"])

    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30.0,
        )
    except subprocess.TimeoutExpired:
        raise StorageError("调用 rclone listremotes 超时。")
    except OSError as e:
        raise StorageError("无法启动 rclone：%s" % e)

    if proc.returncode != 0:
        raise StorageError(
            "获取 rclone 远端列表失败：%s" % _clean_rclone_err(proc.stderr, proc.stdout)
        )

    remotes = []  # type: List[str]
    for line in proc.stdout.splitlines():
        name = line.strip()
        if not name:
            continue
        if name.endswith(":"):
            name = name[:-1]
        if name:
            remotes.append(name)
    return remotes


def _normalize_rclone_root(root: str) -> str:
    if not root:
        return ""
    if "\x00" in root:
        raise StorageError("存储根目录不能包含空字符（NUL）。")
    norm = unicodedata.normalize("NFC", str(root)).strip().replace("\\", "/")
    if not norm:
        return ""
    if norm == "/":
        return "/"
    leading_slash = norm.startswith("/")
    segs = [s for s in norm.split("/") if s != ""]
    for s in segs:
        if s == "..":
            raise StorageError("存储根目录不能包含 '..' 路径段：%r" % (root,))
    joined = "/".join(segs)
    if not joined:
        return "/" if leading_slash else ""
    return ("/" + joined) if leading_slash else joined


def _is_rclone_not_found(returncode: int, stderr: str) -> bool:
    """判断 rclone 非零退出是否表示远端目录或文件不存在（而非网络/配置/鉴权故障）。"""
    # rclone exit code 3 = directory not found, 4 = file not found
    if returncode in (3, 4):
        return True
    low = (stderr or "").lower()
    fatal_markers = (
        "didn't find section in config file",
        "failed to create file system",
        "connection refused",
        "no such host",
        "unauthorized",
        "401 ",
        "403 ",
        "forbidden",
        "invalidaccesskeyid",
        "signaturedoesnotmatch",
    )
    if any(m in low for m in fatal_markers):
        return False
    not_found_markers = (
        "directory not found",
        "file not found",
        "object not found",
        "doesn't exist",
        "does not exist",
        "nosuchkey",
        "nosuchbucket",
    )
    return any(m in low for m in not_found_markers)


class RcloneProvider(StorageProvider):
    """基于 ``rclone`` CLI 的通用云存储提供方（支持 OneDrive/GDrive/Dropbox/WebDAV/S3 等）。"""

    def __init__(
        self,
        remote: str,
        root: str = "",
        rclone_bin: Optional[str] = None,
        config_path: Optional[str] = None,
        timeout: float = 120.0,
    ) -> None:
        if not isinstance(remote, str):
            raise StorageError("rclone 远端名称必须是字符串。")
        clean_remote = remote.strip().rstrip(":").strip()
        if not clean_remote or "\x00" in clean_remote:
            raise StorageError("rclone 远端名称不能为空。")
        self.remote = clean_remote
        self.root = _normalize_rclone_root(root)
        self.rclone_bin = rclone_bin
        self.config_path = str(config_path) if config_path else None
        self.timeout = float(timeout)

    def _remote_spec(self, clean_subkey: str = "") -> str:
        if self.root == "":
            return "%s:%s" % (self.remote, clean_subkey)
        if self.root == "/":
            return "%s:/%s" % (self.remote, clean_subkey)
        if clean_subkey:
            return "%s:%s/%s" % (self.remote, self.root, clean_subkey)
        return "%s:%s" % (self.remote, self.root)

    def _run_rclone(self, args: List[str]) -> subprocess.CompletedProcess:
        exe = find_rclone_binary(self.rclone_bin)
        if not exe:
            raise StorageError("未找到 rclone 可执行文件，请先安装 rclone 并检查 PATH。")

        cmd = [exe]
        cfg = self.config_path or os.environ.get("RCLONE_CONFIG")
        if cfg:
            cmd.extend(["--config", str(cfg)])
        cmd.extend(["--ask-password=false", "--retries", "1", "--low-level-retries", "2"])
        cmd.extend(args)

        try:
            return subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise StorageError("rclone 操作超时（超过 %.1f 秒）。" % self.timeout)
        except OSError as e:
            raise StorageError("无法执行 rclone 命令：%s" % e)

    @staticmethod
    def _extract_etag(item: Dict[str, Any]) -> Optional[str]:
        hashes = item.get("Hashes")
        if isinstance(hashes, dict):
            for k in ("sha256", "md5", "sha1", "quickxorhash"):
                if hashes.get(k):
                    return str(hashes[k])
        if item.get("ID"):
            return str(item["ID"])
        return None

    def list_objects(self, prefix: str = "") -> List[ObjectInfo]:
        clean_prefix = validate_key(prefix, allow_empty=True)
        had_trailing_slash = isinstance(prefix, str) and prefix.endswith("/") and bool(clean_prefix)

        spec = self._remote_spec(clean_prefix)
        proc = self._run_rclone(["lsjson", "-R", "--files-only", spec])
        if proc.returncode != 0:
            if _is_rclone_not_found(proc.returncode, proc.stderr):
                return []
            raise StorageError(
                "列出远端目录失败（%s）：%s"
                % (clean_prefix or "/", _clean_rclone_err(proc.stderr, proc.stdout))
            )

        try:
            raw_items = json.loads(proc.stdout or "[]")
        except ValueError as e:
            raise StorageError("解析 rclone lsjson 输出失败：%s" % e)
        if not isinstance(raw_items, list):
            raise StorageError("rclone lsjson 返回格式异常。")

        # 当 clean_prefix 本身是一个普通文件时，rclone lsjson 会返回 [{"Path": basename}]。
        # 需要区分“单文件”与“同名目录下包含同名文件”这两种情形。
        if clean_prefix and len(raw_items) == 1 and isinstance(raw_items[0], dict):
            leaf_name = clean_prefix.rsplit("/", 1)[-1]
            rel_zero = unicodedata.normalize("NFC", str(raw_items[0].get("Path", "")))
            if rel_zero == leaf_name:
                direct_stat = self.stat(clean_prefix)
                if direct_stat is not None:
                    if had_trailing_slash:
                        return []
                    return [direct_stat]

        results = []  # type: List[ObjectInfo]
        for item in raw_items:
            if not isinstance(item, dict) or item.get("IsDir", False):
                continue
            rel_path = str(item.get("Path", "")).strip("/")
            if not rel_path:
                continue
            rel_key = validate_key(rel_path, allow_empty=False)
            full_key = ("%s/%s" % (clean_prefix, rel_key)) if clean_prefix else rel_key
            hashes = item.get("Hashes") if isinstance(item.get("Hashes"), dict) else {}
            sha256 = str(hashes["sha256"]) if hashes and hashes.get("sha256") else None
            results.append(
                ObjectInfo(
                    key=full_key,
                    size=max(0, int(item.get("Size", 0))),
                    sha256=sha256,
                    etag=self._extract_etag(item),
                    mtime=str(item["ModTime"]) if item.get("ModTime") else None,
                )
            )

        results.sort(key=lambda obj: obj.key)
        return results

    def stat(self, remote_key: str) -> Optional[ObjectInfo]:
        key = validate_key(remote_key, allow_empty=False)
        proc = self._run_rclone(["lsjson", "--stat", self._remote_spec(key)])
        if proc.returncode != 0:
            if _is_rclone_not_found(proc.returncode, proc.stderr):
                return None
            raise StorageError(
                "查询远端对象状态失败（%s）：%s"
                % (key, _clean_rclone_err(proc.stderr, proc.stdout))
            )

        text = (proc.stdout or "").strip()
        if not text:
            return None
        try:
            item = json.loads(text)
        except ValueError as e:
            raise StorageError("解析 rclone stat 输出失败（%s）：%s" % (key, e))
        if not isinstance(item, dict) or item.get("IsDir", False):
            return None

        hashes = item.get("Hashes") if isinstance(item.get("Hashes"), dict) else {}
        sha256 = str(hashes["sha256"]) if hashes and hashes.get("sha256") else None
        return ObjectInfo(
            key=key,
            size=max(0, int(item.get("Size", 0))),
            sha256=sha256,
            etag=self._extract_etag(item),
            mtime=str(item["ModTime"]) if item.get("ModTime") else None,
        )

    def upload(self, local_path: str, remote_key: str) -> ObjectInfo:
        key = validate_key(remote_key, allow_empty=False)
        src = Path(local_path)
        if not src.exists() or not src.is_file():
            raise StorageError("待上传的本地文件不存在或不是普通文件：%s" % local_path)

        try:
            size = src.stat().st_size
            sha256 = _sha256_file(src)
        except OSError as e:
            raise StorageError("读取待上传本地文件失败（%s）：%s" % (local_path, e))

        # --ignore-times 确保在 WebDAV 等秒级时间戳后端上，同大小快速覆盖写入也一定生效
        proc = self._run_rclone(["copyto", "--ignore-times", str(src), self._remote_spec(key)])
        if proc.returncode != 0:
            raise StorageError(
                "上传文件到远端失败（%s）：%s"
                % (key, _clean_rclone_err(proc.stderr, proc.stdout))
            )

        return ObjectInfo(key=key, size=size, sha256=sha256)

    def download(self, remote_key: str, local_path: str) -> ObjectInfo:
        key = validate_key(remote_key, allow_empty=False)
        remote_info = self.stat(key)
        if remote_info is None:
            raise ObjectNotFoundError("远端对象不存在：%s" % key, key=key)

        dst = Path(local_path)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp_dir = tempfile.mkdtemp(dir=str(dst.parent), prefix=".relay-dl-")
        except OSError as e:
            raise StorageError("无法在目标目录创建下载临时文件（%s）：%s" % (dst.parent, e))

        try:
            tmp_file = os.path.join(tmp_dir, "download.part")
            proc = self._run_rclone(["copyto", "--ignore-times", self._remote_spec(key), tmp_file])
            if proc.returncode != 0:
                if _is_rclone_not_found(proc.returncode, proc.stderr):
                    raise ObjectNotFoundError("远端对象不存在：%s" % key, key=key)
                raise StorageError(
                    "下载远端对象失败（%s）：%s"
                    % (key, _clean_rclone_err(proc.stderr, proc.stdout))
                )
            if not os.path.isfile(tmp_file):
                raise ObjectNotFoundError("远端对象不存在或不是普通文件：%s" % key, key=key)

            size = os.path.getsize(tmp_file)
            sha256 = _sha256_file(Path(tmp_file))
            _atomic_replace(tmp_file, str(dst))
            return ObjectInfo(
                key=key,
                size=size,
                sha256=sha256,
                etag=remote_info.etag,
                mtime=remote_info.mtime,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def delete(self, remote_key: str) -> bool:
        key = validate_key(remote_key, allow_empty=False)
        proc = self._run_rclone(["deletefile", self._remote_spec(key)])
        if proc.returncode == 0:
            return True
        if _is_rclone_not_found(proc.returncode, proc.stderr):
            return False
        raise StorageError(
            "删除远端对象失败（%s）：%s"
            % (key, _clean_rclone_err(proc.stderr, proc.stdout))
        )


class LocalDirProvider(StorageProvider):
    """纯本地目录实现的 ``StorageProvider``，提供原子写入/下载与 SHA-256 校验。"""

    _TMP_PREFIX = ".relay-tmp-"

    def __init__(self, root_dir: str) -> None:
        if not isinstance(root_dir, str) or not root_dir.strip():
            raise StorageError("本地存储根目录不能为空。")
        self.root_dir = Path(os.path.abspath(root_dir))

    def _root_real(self) -> Path:
        return Path(os.path.realpath(str(self.root_dir)))

    def _resolve_key_path(self, clean_key: str) -> Tuple[Path, Path]:
        root_real = self._root_real()
        target = root_real.joinpath(*clean_key.split("/")) if clean_key else root_real
        target_real = Path(os.path.realpath(str(target)))
        if target_real != root_real and root_real not in target_real.parents:
            raise StorageError("检测到越界符号链接或路径访问：%s" % clean_key)
        return root_real, target

    @staticmethod
    def _build_file_info(file_path: Path, key: str) -> ObjectInfo:
        st = file_path.stat()
        digest = _sha256_file(file_path)
        return ObjectInfo(
            key=key,
            size=st.st_size,
            sha256=digest,
            etag=digest,
            mtime=_iso_mtime(st.st_mtime),
        )

    def list_objects(self, prefix: str = "") -> List[ObjectInfo]:
        clean_prefix = validate_key(prefix, allow_empty=True)
        had_trailing_slash = isinstance(prefix, str) and prefix.endswith("/") and bool(clean_prefix)

        if not self.root_dir.exists():
            return []

        root_real, base = self._resolve_key_path(clean_prefix)
        if not base.exists():
            return []
        if base.is_symlink():
            raise StorageError("检测到非法符号链接对象：%s" % clean_prefix)
        if base.is_file():
            if had_trailing_slash:
                return []
            return [self._build_file_info(base, clean_prefix)]
        if not base.is_dir():
            return []

        results = []  # type: List[ObjectInfo]
        for cur_root, dirnames, filenames in os.walk(str(base), followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith(self._TMP_PREFIX)]
            for fname in filenames:
                if fname.startswith(self._TMP_PREFIX):
                    continue
                fpath = Path(cur_root) / fname
                if fpath.is_symlink() or not fpath.is_file():
                    continue
                f_real = Path(os.path.realpath(str(fpath)))
                if root_real not in f_real.parents:
                    continue
                rel_posix = f_real.relative_to(root_real).as_posix()
                norm_key = validate_key(rel_posix, allow_empty=False)
                results.append(self._build_file_info(f_real, norm_key))

        results.sort(key=lambda obj: obj.key)
        return results

    def upload(self, local_path: str, remote_key: str) -> ObjectInfo:
        key = validate_key(remote_key, allow_empty=False)
        src = Path(local_path)
        if not src.exists() or not src.is_file():
            raise StorageError("待上传的本地文件不存在或不是普通文件：%s" % local_path)

        try:
            self.root_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StorageError("无法创建本地存储根目录（%s）：%s" % (self.root_dir, e))

        _, dst = self._resolve_key_path(key)
        if dst.exists() and dst.is_dir():
            raise StorageError("远端目标路径已存在同名目录：%s" % key)

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(dst.parent),
                prefix=self._TMP_PREFIX,
                suffix=".part",
            )
        except OSError as e:
            raise StorageError("无法创建上传临时文件（%s）：%s" % (key, e))

        h = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as out_f, open(src, "rb") as in_f:
                while True:
                    chunk = in_f.read(65536)
                    if not chunk:
                        break
                    out_f.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
                out_f.flush()
                try:
                    os.fsync(out_f.fileno())
                except OSError:
                    pass
            _atomic_replace(tmp_path, str(dst))
            st = dst.stat()
            digest = h.hexdigest()
            return ObjectInfo(
                key=key,
                size=size,
                sha256=digest,
                etag=digest,
                mtime=_iso_mtime(st.st_mtime),
            )
        except BaseException as e:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            if isinstance(e, StorageError):
                raise
            if isinstance(e, OSError):
                raise StorageError("写入本地存储对象失败（%s）：%s" % (key, e))
            raise

    def download(self, remote_key: str, local_path: str) -> ObjectInfo:
        key = validate_key(remote_key, allow_empty=False)
        if not self.root_dir.exists():
            raise ObjectNotFoundError("远端对象不存在：%s" % key, key=key)

        _, src = self._resolve_key_path(key)
        if not src.exists() or src.is_symlink() or not src.is_file():
            raise ObjectNotFoundError("远端对象不存在：%s" % key, key=key)

        dst = Path(local_path)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(dst.parent),
                prefix=".relay-dl-",
                suffix=".part",
            )
        except OSError as e:
            raise StorageError("无法创建下载临时文件（%s）：%s" % (dst.parent, e))

        h = hashlib.sha256()
        size = 0
        try:
            src_mtime = _iso_mtime(src.stat().st_mtime)
            with os.fdopen(fd, "wb") as out_f, open(src, "rb") as in_f:
                while True:
                    chunk = in_f.read(65536)
                    if not chunk:
                        break
                    out_f.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
                out_f.flush()
                try:
                    os.fsync(out_f.fileno())
                except OSError:
                    pass
            _atomic_replace(tmp_path, str(dst))
            digest = h.hexdigest()
            return ObjectInfo(
                key=key,
                size=size,
                sha256=digest,
                etag=digest,
                mtime=src_mtime,
            )
        except BaseException as e:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            if isinstance(e, StorageError):
                raise
            if isinstance(e, OSError):
                raise StorageError("下载本地存储对象失败（%s）：%s" % (key, e))
            raise

    def stat(self, remote_key: str) -> Optional[ObjectInfo]:
        key = validate_key(remote_key, allow_empty=False)
        if not self.root_dir.exists():
            return None
        _, target = self._resolve_key_path(key)
        if not target.exists() or target.is_symlink() or not target.is_file():
            return None
        return self._build_file_info(target, key)

    def delete(self, remote_key: str) -> bool:
        key = validate_key(remote_key, allow_empty=False)
        if not self.root_dir.exists():
            return False
        root_real, target = self._resolve_key_path(key)
        if not target.exists() or target.is_symlink() or not target.is_file():
            return False
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        except OSError as e:
            raise StorageError("删除本地存储对象失败（%s）：%s" % (key, e))

        # 顺带清理空的中间子目录（停在 root_real 之前）
        parent = target.parent
        while parent != root_real and root_real in parent.parents:
            try:
                parent.rmdir()
                parent = parent.parent
            except OSError:
                break
        return True


def make_provider(cloud_cfg: Dict[str, Any]) -> StorageProvider:
    """根据 ``cloud.json`` 配置字典构建对应的 ``StorageProvider`` 实例。"""
    if not isinstance(cloud_cfg, dict):
        raise StorageError("云端存储配置必须是对象（dict）。")

    provider_type = str(cloud_cfg.get("provider") or "rclone").strip().lower()
    if provider_type == "rclone":
        remote = str(cloud_cfg.get("remote") or "")
        root = str(cloud_cfg.get("root") or "")
        config_path = cloud_cfg.get("configPath") or cloud_cfg.get("config_path") or None
        rclone_bin = cloud_cfg.get("rcloneBin") or cloud_cfg.get("rclone_bin") or None
        timeout = float(cloud_cfg.get("timeout") or 120.0)
        return RcloneProvider(
            remote=remote,
            root=root,
            rclone_bin=str(rclone_bin) if rclone_bin else None,
            config_path=str(config_path) if config_path else None,
            timeout=timeout,
        )

    if provider_type == "local":
        root = str(
            cloud_cfg.get("root")
            or cloud_cfg.get("rootDir")
            or cloud_cfg.get("root_dir")
            or ""
        )
        return LocalDirProvider(root_dir=root)

    raise StorageError(
        "不支持的云存储提供方类型：%r（目前支持 'rclone' 或 'local'）。" % (provider_type,)
    )
