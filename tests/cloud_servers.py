"""Local WebDAV and S3-compatible test servers and rclone config fixtures.

Provides real local HTTP cloud endpoints on 127.0.0.1:<free_port> for end-to-end
verification of rclone-backed StorageProvider and RelayEngine workflows:
- Primary mode: `rclone serve webdav` and `rclone serve s3` subprocesses.
- Fallback mode: pure-Python stdlib ThreadingHTTPServer implementations of
  WebDAV and path-style S3 so tests remain self-contained and deterministic.
- Separate rclone configuration files (`rclone_a.conf` with `alice_cloud` and
  `rclone_b.conf` with `bob_storage`) pointing to the same backing store under
  different connection names.
"""
import contextlib
import email.utils
import hashlib
import http.server
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlsplit
from xml.sax.saxutils import escape as xml_escape


def find_rclone(explicit: Optional[str] = None) -> Optional[str]:
    """Locate the rclone binary on PATH or standard macOS/Linux install paths."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    env_bin = os.environ.get("RELAY_DESK_RCLONE")
    if env_bin:
        candidates.append(env_bin)
    which_bin = shutil.which("rclone")
    if which_bin:
        candidates.append(which_bin)
    candidates.extend(["/opt/homebrew/bin/rclone", "/usr/local/bin/rclone"])
    for cand in candidates:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_tcp(host: str, port: int, timeout: float = 8.0, proc: Optional[subprocess.Popen] = None) -> None:
    deadline = time.monotonic() + timeout
    last_err = None  # type: Optional[Exception]
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError("Cloud server process exited early with code %s" % proc.returncode)
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError as e:
            last_err = e
            time.sleep(0.03)
    raise RuntimeError("Timed out waiting for %s:%d (%s)" % (host, port, last_err))


# ---------------------------------------------------------------------------
# Pure-Python stdlib HTTP helpers (WebDAV & S3 fallback servers)
# ---------------------------------------------------------------------------

class _QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _read_http_body(handler: http.server.BaseHTTPRequestHandler) -> bytes:
    """Read request body supporting both Content-Length and chunked transfer encoding."""
    te = (handler.headers.get("Transfer-Encoding") or "").lower()
    if "chunked" in te:
        chunks = []  # type: List[bytes]
        while True:
            line = handler.rfile.readline()
            if not line:
                break
            semi = line.find(b";")
            hex_part = line[:semi] if semi != -1 else line
            hex_str = hex_part.strip()
            if not hex_str:
                continue
            size = int(hex_str, 16)
            if size == 0:
                # Consume trailing CRLF / headers
                while True:
                    tail = handler.rfile.readline()
                    if not tail or tail in (b"\r\n", b"\n"):
                        break
                break
            data = handler.rfile.read(size)
            chunks.append(data)
            # Consume chunk CRLF
            crlf = handler.rfile.read(2)
            if crlf == b"\n":
                pass
        raw = b"".join(chunks)
    else:
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length > 0 else b""

    # Unwrap AWS SigV4 aws-chunked payload if client used streaming signatures
    content_sha = handler.headers.get("x-amz-content-sha256") or ""
    if content_sha.startswith("STREAMING-"):
        out = []  # type: List[bytes]
        idx = 0
        n = len(raw)
        while idx < n:
            nl = raw.find(b"\r\n", idx)
            if nl == -1:
                break
            header_line = raw[idx:nl]
            semi = header_line.find(b";")
            csize = int((header_line[:semi] if semi != -1 else header_line).strip(), 16)
            idx = nl + 2
            if csize == 0:
                break
            out.append(raw[idx:idx + csize])
            idx += csize + 2
        return b"".join(out)
    return raw


def _safe_join(root: Path, rel_parts: List[str]) -> Optional[Path]:
    cur = root.resolve()
    target = cur
    for part in rel_parts:
        if not part or part == ".":
            continue
        if part == ".." or "/" in part or "\\" in part:
            return None
        target = target / part
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target
    if resolved != cur and not str(resolved).startswith(str(cur) + os.sep):
        return None
    return target


def _http_date(ts: float) -> str:
    return email.utils.formatdate(ts, usegmt=True)


def _iso_utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _make_webdav_handler(root_dir: Path):
    class StdlibWebDAVHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8",
                  extra_headers: Optional[List[Tuple[str, str]]] = None) -> None:
            self.send_response(code)
            self.send_header("DAV", "1, 2")
            self.send_header("Content-Length", str(len(body)))
            if content_type:
                self.send_header("Content-Type", content_type)
            for k, v in (extra_headers or []):
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)

        def _resolve(self, raw_path: str) -> Tuple[Optional[Path], str]:
            parsed = urlsplit(raw_path)
            decoded = unquote(parsed.path)
            parts = [p for p in decoded.split("/") if p]
            return _safe_join(root_dir, parts), decoded

        def do_OPTIONS(self):
            _read_http_body(self)
            self._send(
                200,
                b"",
                extra_headers=[("Allow", "OPTIONS, GET, HEAD, PUT, DELETE, MKCOL, PROPFIND, MOVE, COPY")],
            )

        def do_MKCOL(self):
            _read_http_body(self)
            target, _ = self._resolve(self.path)
            if target is None:
                return self._send(403, b"Forbidden")
            if target.exists():
                return self._send(405, b"Already exists")
            if not target.parent.exists():
                return self._send(409, b"Parent missing")
            target.mkdir(parents=False, exist_ok=True)
            self._send(201, b"")

        def do_PUT(self):
            body = _read_http_body(self)
            target, _ = self._resolve(self.path)
            if target is None:
                return self._send(403, b"Forbidden")
            target.parent.mkdir(parents=True, exist_ok=True)
            existed = target.is_file()
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".dav-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(body)
                os.replace(tmp, str(target))
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            etag = '"%s"' % hashlib.md5(body).hexdigest()
            self._send(204 if existed else 201, b"", extra_headers=[("ETag", etag)])

        def _serve_file(self):
            _read_http_body(self)
            target, _ = self._resolve(self.path)
            if target is None:
                return self._send(403, b"Forbidden")
            if not target.exists():
                return self._send(404, b"Not Found")
            if target.is_dir():
                return self._send(200, b"", extra_headers=[("Last-Modified", _http_date(target.stat().st_mtime))])
            data = target.read_bytes()
            st = target.stat()
            etag = '"%s"' % hashlib.md5(data).hexdigest()
            range_hdr = self.headers.get("Range")
            if range_hdr and range_hdr.startswith("bytes="):
                spec = range_hdr[len("bytes="):].split(",")[0].strip()
                s_str, e_str = spec.split("-", 1)
                start = int(s_str) if s_str else 0
                end = int(e_str) if e_str else len(data) - 1
                end = min(end, len(data) - 1)
                sliced = data[start:end + 1] if start <= end else b""
                return self._send(
                    206,
                    sliced,
                    content_type="application/octet-stream",
                    extra_headers=[
                        ("ETag", etag),
                        ("Last-Modified", _http_date(st.st_mtime)),
                        ("Content-Range", "bytes %d-%d/%d" % (start, end, len(data))),
                    ],
                )
            self._send(
                200,
                data,
                content_type="application/octet-stream",
                extra_headers=[("ETag", etag), ("Last-Modified", _http_date(st.st_mtime))],
            )

        do_GET = _serve_file
        do_HEAD = _serve_file

        def do_DELETE(self):
            _read_http_body(self)
            target, _ = self._resolve(self.path)
            if target is None:
                return self._send(403, b"Forbidden")
            if not target.exists():
                return self._send(404, b"Not Found")
            if target.is_dir():
                shutil.rmtree(str(target))
            else:
                target.unlink()
            self._send(204, b"")

        def do_MOVE(self):
            _read_http_body(self)
            src, _ = self._resolve(self.path)
            dest_hdr = self.headers.get("Destination") or ""
            dst, _ = self._resolve(dest_hdr)
            if src is None or dst is None:
                return self._send(403, b"Forbidden")
            if not src.exists():
                return self._send(404, b"Not Found")
            dst.parent.mkdir(parents=True, exist_ok=True)
            existed = dst.exists()
            os.replace(str(src), str(dst))
            self._send(204 if existed else 201, b"")

        def do_PROPFIND(self):
            _read_http_body(self)
            target, _ = self._resolve(self.path)
            if target is None:
                return self._send(403, b"Forbidden")
            if not target.exists():
                return self._send(404, b"Not Found")
            depth = (self.headers.get("Depth") or "1").strip().lower()
            items = []  # type: List[Path]
            items.append(target)
            if target.is_dir() and depth in ("1", "infinity"):
                if depth == "1":
                    items.extend(sorted(target.iterdir(), key=lambda p: p.name))
                else:
                    for dp, dirs, files in os.walk(str(target)):
                        dirs.sort()
                        files.sort()
                        for d in dirs:
                            items.append(Path(dp) / d)
                        for fn in files:
                            items.append(Path(dp) / fn)

            xml_rows = ['<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">']
            root_resolved = root_dir.resolve()
            for item in items:
                rel = item.resolve().relative_to(root_resolved)
                rel_parts = [] if str(rel) == "." else list(rel.parts)
                href = "/" + "/".join(quote(seg, safe="") for seg in rel_parts)
                is_dir = item.is_dir()
                if is_dir and not href.endswith("/"):
                    href += "/"
                st = item.stat()
                disp = xml_escape(item.name or "/")
                lastmod = xml_escape(_http_date(st.st_mtime))
                if is_dir:
                    xml_rows.append(
                        "<D:response><D:href>%s</D:href><D:propstat><D:prop>"
                        "<D:displayname>%s</D:displayname>"
                        "<D:getlastmodified>%s</D:getlastmodified>"
                        "<D:resourcetype><D:collection/></D:resourcetype>"
                        "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
                        % (href, disp, lastmod)
                    )
                else:
                    xml_rows.append(
                        "<D:response><D:href>%s</D:href><D:propstat><D:prop>"
                        "<D:displayname>%s</D:displayname>"
                        "<D:getcontentlength>%d</D:getcontentlength>"
                        "<D:getlastmodified>%s</D:getlastmodified>"
                        "<D:resourcetype/>"
                        "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
                        % (href, disp, st.st_size, lastmod)
                    )
            xml_rows.append("</D:multistatus>")
            payload = "".join(xml_rows).encode("utf-8")
            self._send(207, payload, content_type="application/xml; charset=utf-8")

    return StdlibWebDAVHandler


def _make_s3_handler(root_dir: Path, access_key: str, secret_key: str):
    class StdlibS3Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body: bytes = b"", content_type: str = "application/xml; charset=utf-8",
                  extra_headers: Optional[List[Tuple[str, str]]] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            if content_type:
                self.send_header("Content-Type", content_type)
            for k, v in (extra_headers or []):
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)

        def _s3_err(self, code: int, err_code: str, message: str) -> None:
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Error><Code>%s</Code><Message>%s</Message></Error>"
                % (xml_escape(err_code), xml_escape(message))
            ).encode("utf-8")
            self._send(code, xml)

        def _check_auth(self) -> bool:
            if not access_key:
                return True
            auth = self.headers.get("Authorization") or ""
            if ("Credential=%s/" % access_key) in auth:
                return True
            qs = parse_qs(urlsplit(self.path).query)
            if any(v.startswith(access_key + "/") for v in qs.get("X-Amz-Credential", [])):
                return True
            self._s3_err(403, "AccessDenied", "Invalid or missing access key")
            return False

        def _parse_bucket_key(self) -> Tuple[str, str]:
            decoded = unquote(urlsplit(self.path).path).lstrip("/")
            if not decoded:
                return "", ""
            if "/" not in decoded:
                return decoded, ""
            bucket, key = decoded.split("/", 1)
            return bucket, key

        def do_GET(self):
            _read_http_body(self)
            if not self._check_auth():
                return
            parts = urlsplit(self.path)
            qs = parse_qs(parts.query)
            bucket, key = self._parse_bucket_key()
            if not bucket:
                # ListBuckets
                rows = [
                    '<?xml version="1.0" encoding="UTF-8"?>',
                    '<ListAllMyBucketsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
                    "<Buckets>",
                ]
                for p in sorted(root_dir.iterdir(), key=lambda x: x.name):
                    if p.is_dir() and not p.name.startswith("."):
                        rows.append(
                            "<Bucket><Name>%s</Name><CreationDate>%s</CreationDate></Bucket>"
                            % (xml_escape(p.name), _iso_utc(p.stat().st_mtime))
                        )
                rows.append("</Buckets></ListAllMyBucketsResult>")
                return self._send(200, "".join(rows).encode("utf-8"))

            bdir = _safe_join(root_dir, [bucket])
            if bdir is None or not bdir.is_dir():
                return self._s3_err(404, "NoSuchBucket", "Bucket does not exist")

            if not key:
                if "location" in qs:
                    return self._send(
                        200,
                        b'<?xml version="1.0" encoding="UTF-8"?>'
                        b'<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/">us-east-1</LocationConstraint>',
                    )
                prefix = qs.get("prefix", [""])[0]
                delimiter = qs.get("delimiter", [""])[0]
                all_files = []  # type: List[Tuple[str, Path]]
                bdir_res = bdir.resolve()
                for dp, dirs, files in os.walk(str(bdir_res)):
                    dirs[:] = [d for d in sorted(dirs) if not d.startswith(".")]
                    for fn in sorted(files):
                        if fn.startswith("."):
                            continue
                        fp = Path(dp) / fn
                        rel_k = "/".join(fp.relative_to(bdir_res).parts)
                        if rel_k.startswith(prefix):
                            all_files.append((rel_k, fp))

                contents = []  # type: List[Tuple[str, Path]]
                common_prefixes = set()
                for rel_k, fp in all_files:
                    rest = rel_k[len(prefix):]
                    if delimiter and delimiter in rest:
                        idx = rest.index(delimiter)
                        common_prefixes.add(prefix + rest[:idx + len(delimiter)])
                    else:
                        contents.append((rel_k, fp))

                is_v2 = qs.get("list-type", [""])[0] == "2"
                rows = [
                    '<?xml version="1.0" encoding="UTF-8"?>',
                    '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
                    "<Name>%s</Name>" % xml_escape(bucket),
                    "<Prefix>%s</Prefix>" % xml_escape(prefix),
                    "<IsTruncated>false</IsTruncated>",
                ]
                if delimiter:
                    rows.append("<Delimiter>%s</Delimiter>" % xml_escape(delimiter))
                if is_v2:
                    rows.append("<KeyCount>%d</KeyCount>" % (len(contents) + len(common_prefixes)))
                for rel_k, fp in contents:
                    st = fp.stat()
                    md5_hex = hashlib.md5(fp.read_bytes()).hexdigest()
                    rows.append(
                        "<Contents><Key>%s</Key><LastModified>%s</LastModified>"
                        "<ETag>&quot;%s&quot;</ETag><Size>%d</Size>"
                        "<StorageClass>STANDARD</StorageClass></Contents>"
                        % (xml_escape(rel_k), _iso_utc(st.st_mtime), md5_hex, st.st_size)
                    )
                for cp in sorted(common_prefixes):
                    rows.append("<CommonPrefixes><Prefix>%s</Prefix></CommonPrefixes>" % xml_escape(cp))
                rows.append("</ListBucketResult>")
                return self._send(200, "".join(rows).encode("utf-8"))

            target = _safe_join(bdir, [p for p in key.split("/") if p])
            if target is None or not target.is_file():
                return self._s3_err(404, "NoSuchKey", "Key does not exist")
            data = target.read_bytes()
            st = target.stat()
            etag = '"%s"' % hashlib.md5(data).hexdigest()
            range_hdr = self.headers.get("Range")
            if range_hdr and range_hdr.startswith("bytes="):
                spec = range_hdr[len("bytes="):].split(",")[0].strip()
                s_str, e_str = spec.split("-", 1)
                start = int(s_str) if s_str else 0
                end = int(e_str) if e_str else len(data) - 1
                end = min(end, len(data) - 1)
                sliced = data[start:end + 1] if start <= end else b""
                return self._send(
                    206,
                    sliced,
                    content_type="application/octet-stream",
                    extra_headers=[
                        ("ETag", etag),
                        ("Last-Modified", _http_date(st.st_mtime)),
                        ("Content-Range", "bytes %d-%d/%d" % (start, end, len(data))),
                    ],
                )
            self._send(
                200,
                data,
                content_type="application/octet-stream",
                extra_headers=[("ETag", etag), ("Last-Modified", _http_date(st.st_mtime))],
            )

        def do_HEAD(self):
            _read_http_body(self)
            if not self._check_auth():
                return
            bucket, key = self._parse_bucket_key()
            bdir = _safe_join(root_dir, [bucket]) if bucket else None
            if bdir is None or not bdir.is_dir():
                return self._send(404, b"", content_type="")
            if not key:
                return self._send(200, b"", content_type="")
            target = _safe_join(bdir, [p for p in key.split("/") if p])
            if target is None or not target.is_file():
                return self._send(404, b"", content_type="")
            data = target.read_bytes()
            st = target.stat()
            etag = '"%s"' % hashlib.md5(data).hexdigest()
            self._send(
                200,
                b"",
                content_type="application/octet-stream",
                extra_headers=[
                    ("Content-Length", str(len(data))),
                    ("ETag", etag),
                    ("Last-Modified", _http_date(st.st_mtime)),
                ],
            )
            # Note: BaseHTTPRequestHandler saw Content-Length 0 in _send, so override via custom head:

        def do_PUT(self):
            body = _read_http_body(self)
            if not self._check_auth():
                return
            bucket, key = self._parse_bucket_key()
            if not bucket:
                return self._s3_err(400, "InvalidBucketName", "Missing bucket")
            bdir = _safe_join(root_dir, [bucket])
            if bdir is None:
                return self._s3_err(400, "InvalidBucketName", "Invalid bucket")
            if not key:
                bdir.mkdir(parents=True, exist_ok=True)
                return self._send(200, b"", content_type="")
            bdir.mkdir(parents=True, exist_ok=True)
            target = _safe_join(bdir, [p for p in key.split("/") if p])
            if target is None:
                return self._s3_err(403, "AccessDenied", "Invalid key")
            copy_src = self.headers.get("x-amz-copy-source")
            if copy_src:
                src_decoded = unquote(copy_src).lstrip("/")
                src_bucket, src_key = src_decoded.split("/", 1)
                sbdir = _safe_join(root_dir, [src_bucket])
                starget = _safe_join(sbdir, [p for p in src_key.split("/") if p]) if sbdir else None
                if starget is None or not starget.is_file():
                    return self._s3_err(404, "NoSuchKey", "Copy source missing")
                data = starget.read_bytes()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                md5_hex = hashlib.md5(data).hexdigest()
                xml = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    "<CopyObjectResult><LastModified>%s</LastModified>"
                    "<ETag>&quot;%s&quot;</ETag></CopyObjectResult>"
                    % (_iso_utc(target.stat().st_mtime), md5_hex)
                ).encode("utf-8")
                return self._send(200, xml)

            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".s3-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(body)
                os.replace(tmp, str(target))
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            etag = '"%s"' % hashlib.md5(body).hexdigest()
            self._send(200, b"", content_type="", extra_headers=[("ETag", etag)])

        def do_DELETE(self):
            _read_http_body(self)
            if not self._check_auth():
                return
            bucket, key = self._parse_bucket_key()
            bdir = _safe_join(root_dir, [bucket]) if bucket else None
            if bdir is None or not bdir.is_dir():
                return self._s3_err(404, "NoSuchBucket", "Bucket missing")
            if not key:
                return self._send(204, b"", content_type="")
            target = _safe_join(bdir, [p for p in key.split("/") if p])
            if target is not None and target.is_file():
                target.unlink()
            self._send(204, b"", content_type="")

    # Fix HEAD Content-Length cleanly without duplicating headers:
    def _do_head_exact(self):
        _read_http_body(self)
        if not self._check_auth():
            return
        bucket, key = self._parse_bucket_key()
        bdir = _safe_join(root_dir, [bucket]) if bucket else None
        if bdir is None or not bdir.is_dir():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not key:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        target = _safe_join(bdir, [p for p in key.split("/") if p])
        if target is None or not target.is_file():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = target.read_bytes()
        st = target.stat()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", '"%s"' % hashlib.md5(data).hexdigest())
        self.send_header("Last-Modified", _http_date(st.st_mtime))
        self.end_headers()

    StdlibS3Handler.do_HEAD = _do_head_exact
    return StdlibS3Handler


# ---------------------------------------------------------------------------
# Unified CloudServerEnv fixture & context managers
# ---------------------------------------------------------------------------

class CloudServerEnv(object):
    """Represents a running local WebDAV or S3-compatible server + paired rclone configs."""

    def __init__(
        self,
        protocol: str,
        backend: str,
        port: int,
        endpoint: str,
        data_dir: Path,
        storage_root_dir: Path,
        shared_root: str,
        conf_a: Path,
        conf_b: Path,
        remote_a: str = "alice_cloud",
        remote_b: str = "bob_storage",
        rclone_bin: Optional[str] = None,
    ):
        self.protocol = protocol
        self.backend = backend
        self.port = port
        self.endpoint = endpoint
        self.url = endpoint
        self.data_dir = data_dir
        self.storage_root_dir = storage_root_dir
        self.shared_root = shared_root
        self.conf_a = conf_a
        self.conf_b = conf_b
        self.remote_a = remote_a
        self.remote_b = remote_b
        self.rclone_bin = rclone_bin or find_rclone()

    def object_path(self, rel_key: str) -> Path:
        """Return the local filesystem path backing `rel_key` inside `shared_root`."""
        clean = rel_key.strip("/")
        return self.storage_root_dir / clean if clean else self.storage_root_dir

    def read_object(self, rel_key: str) -> bytes:
        """Read raw bytes of an object under `shared_root`."""
        return self.object_path(rel_key).read_bytes()

    def write_object(self, rel_key: str, data: bytes) -> None:
        """Write/tamper raw bytes of an object through the cloud protocol (or disk fallback)."""
        clean = rel_key.strip("/")
        if self.rclone_bin and self.conf_a.is_file():
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tf.write(data)
                tmp_path = tf.name
            try:
                dest = "%s:%s/%s" % (self.remote_a, self.shared_root.strip("/"), clean)
                subprocess.run(
                    [self.rclone_bin, "--config", str(self.conf_a), "copyto", tmp_path, dest],
                    check=True,
                    capture_output=True,
                )
                return
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        fp = self.object_path(clean)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(data)
        time.sleep(0.03)

    def delete_object(self, rel_key: str) -> None:
        """Delete an object under `shared_root`."""
        clean = rel_key.strip("/")
        if self.rclone_bin and self.conf_a.is_file():
            dest = "%s:%s/%s" % (self.remote_a, self.shared_root.strip("/"), clean)
            subprocess.run(
                [self.rclone_bin, "--config", str(self.conf_a), "deletefile", dest],
                check=False,
                capture_output=True,
            )
            return
        fp = self.object_path(clean)
        if fp.is_file():
            fp.unlink()
        time.sleep(0.03)


@contextlib.contextmanager
def webdav_server(
    base_dir: Optional[str] = None,
    remote_a: str = "alice_cloud",
    remote_b: str = "bob_storage",
    shared_root: str = "relay-space",
    prefer_rclone: bool = True,
) -> Iterator[CloudServerEnv]:
    """Start a real local WebDAV HTTP server on 127.0.0.1:<free_port> and write two rclone configs."""
    own_td = tempfile.TemporaryDirectory() if base_dir is None else None
    root = Path(base_dir) if base_dir is not None else Path(own_td.name)
    dav_data = root / "webdav_data"
    storage_root = dav_data / shared_root.strip("/")
    storage_root.mkdir(parents=True, exist_ok=True)

    rclone_bin = find_rclone()
    port = _free_port()
    endpoint = "http://127.0.0.1:%d/" % port

    conf_a = root / "rclone_webdav_a.conf"
    conf_b = root / "rclone_webdav_b.conf"
    conf_a.write_text(
        "[%s]\ntype = webdav\nurl = %s\nvendor = %s\n"
        % (remote_a, endpoint, "rclone" if (prefer_rclone and rclone_bin) else "other"),
        encoding="utf-8",
    )
    conf_b.write_text(
        "[%s]\ntype = webdav\nurl = %s\nvendor = other\n" % (remote_b, endpoint),
        encoding="utf-8",
    )

    proc = None  # type: Optional[subprocess.Popen]
    httpd = None  # type: Optional[_QuietThreadingHTTPServer]
    thread = None  # type: Optional[threading.Thread]
    log_fh = None
    backend = "stdlib"
    try:
        if prefer_rclone and rclone_bin:
            log_path = root / "webdav_server.log"
            log_fh = open(str(log_path), "wb")
            proc = subprocess.Popen(
                [
                    rclone_bin,
                    "serve",
                    "webdav",
                    str(dav_data),
                    "--addr",
                    "127.0.0.1:%d" % port,
                    "--dir-cache-time",
                    "10ms",
                    "--config",
                    "/dev/null",
                ],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
            _wait_tcp("127.0.0.1", port, proc=proc)
            backend = "rclone"
        else:
            httpd = _QuietThreadingHTTPServer(("127.0.0.1", port), _make_webdav_handler(dav_data))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            _wait_tcp("127.0.0.1", port)
            backend = "stdlib"

        yield CloudServerEnv(
            protocol="webdav",
            backend=backend,
            port=port,
            endpoint=endpoint,
            data_dir=dav_data,
            storage_root_dir=storage_root,
            shared_root=shared_root.strip("/"),
            conf_a=conf_a,
            conf_b=conf_b,
            remote_a=remote_a,
            remote_b=remote_b,
            rclone_bin=rclone_bin,
        )
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=2)
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass
        if own_td is not None:
            own_td.cleanup()


@contextlib.contextmanager
def s3_server(
    base_dir: Optional[str] = None,
    remote_a: str = "alice_cloud",
    remote_b: str = "bob_storage",
    bucket: str = "relaybucket",
    prefix: str = "relay-space",
    access_key: str = "testkey",
    secret_key: str = "testsecret",
    prefer_rclone: bool = True,
) -> Iterator[CloudServerEnv]:
    """Start a real local S3-compatible HTTP server on 127.0.0.1:<free_port> and write two rclone configs."""
    own_td = tempfile.TemporaryDirectory() if base_dir is None else None
    root = Path(base_dir) if base_dir is not None else Path(own_td.name)
    s3_data = root / "s3_data"
    bucket_dir = s3_data / bucket
    clean_prefix = prefix.strip("/")
    storage_root = (bucket_dir / clean_prefix) if clean_prefix else bucket_dir
    storage_root.mkdir(parents=True, exist_ok=True)
    shared_root = ("%s/%s" % (bucket, clean_prefix)) if clean_prefix else bucket

    rclone_bin = find_rclone()
    port = _free_port()
    endpoint = "http://127.0.0.1:%d/" % port

    conf_a = root / "rclone_s3_a.conf"
    conf_b = root / "rclone_s3_b.conf"
    provider_a = "Rclone" if (prefer_rclone and rclone_bin) else "Other"
    conf_a.write_text(
        "[%s]\n"
        "type = s3\n"
        "provider = %s\n"
        "env_auth = false\n"
        "access_key_id = %s\n"
        "secret_access_key = %s\n"
        "endpoint = %s\n"
        "force_path_style = true\n"
        % (remote_a, provider_a, access_key, secret_key, endpoint),
        encoding="utf-8",
    )
    conf_b.write_text(
        "[%s]\n"
        "type = s3\n"
        "provider = Other\n"
        "env_auth = false\n"
        "access_key_id = %s\n"
        "secret_access_key = %s\n"
        "endpoint = %s\n"
        "force_path_style = true\n"
        % (remote_b, access_key, secret_key, endpoint),
        encoding="utf-8",
    )

    proc = None  # type: Optional[subprocess.Popen]
    httpd = None  # type: Optional[_QuietThreadingHTTPServer]
    thread = None  # type: Optional[threading.Thread]
    log_fh = None
    backend = "stdlib"
    try:
        if prefer_rclone and rclone_bin:
            log_path = root / "s3_server.log"
            log_fh = open(str(log_path), "wb")
            proc = subprocess.Popen(
                [
                    rclone_bin,
                    "serve",
                    "s3",
                    str(s3_data),
                    "--addr",
                    "127.0.0.1:%d" % port,
                    "--auth-key",
                    "%s,%s" % (access_key, secret_key),
                    "--dir-cache-time",
                    "10ms",
                    "--config",
                    "/dev/null",
                ],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
            _wait_tcp("127.0.0.1", port, proc=proc)
            backend = "rclone"
        else:
            httpd = _QuietThreadingHTTPServer(("127.0.0.1", port), _make_s3_handler(s3_data, access_key, secret_key))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            _wait_tcp("127.0.0.1", port)
            backend = "stdlib"

        yield CloudServerEnv(
            protocol="s3",
            backend=backend,
            port=port,
            endpoint=endpoint,
            data_dir=s3_data,
            storage_root_dir=storage_root,
            shared_root=shared_root,
            conf_a=conf_a,
            conf_b=conf_b,
            remote_a=remote_a,
            remote_b=remote_b,
            rclone_bin=rclone_bin,
        )
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=2)
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass
        if own_td is not None:
            own_td.cleanup()
