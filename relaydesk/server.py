"""Local HTTP service: serves relay-desk.html and a small JSON API over the ledger.

The store is duck-typed: read() -> (ledger, rev) and update(fn) -> (ledger, rev).
Standard library only; binds to 127.0.0.1 only.
"""
import http.server
import json
import re
import sys
import threading
from urllib.parse import parse_qs, unquote, urlsplit

MAX_BODY = 2 * 1024 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
ALLOWED_HOSTS = ("127.0.0.1", "localhost")

HTML_HEAD = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    "<style>:root{color-scheme:light}body{margin:0;font:14px system-ui,sans-serif}"
    "img{max-width:100%}[hidden]{display:none!important}</style></head><body>"
)
HTML_TAIL = "</body></html>"


def _host_part(value):
    """Host header -> hostname without port (IPv6 literals are not accepted)."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return None
    return value.rsplit(":", 1)[0] if ":" in value else value


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    html_path = None
    store = None


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "relay-desk"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = 10  # socket timeout so a stalled client cannot block a handler thread
    _body_read = False

    # ---- logging: errors only ----
    def log_message(self, format, *args):
        pass

    def log_error(self, format, *args):
        try:
            sys.stderr.write("relay-desk: " + (format % args) + "\n")
        except Exception:
            pass

    # ---- responses ----
    def _drain(self):
        """Read and discard an unread request body (capped). Closing a socket with
        unread received data makes Windows reset the connection and the client can
        lose the response, so every response path goes through here via _send."""
        if self._body_read:
            return
        self._body_read = True
        try:
            left = min(int(self.headers.get("Content-Length") or 0), 16 * MAX_BODY)
        except (ValueError, TypeError):
            return
        try:
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
        except (OSError, ValueError):
            pass

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        self._drain()
        self.send_response(code)
        self.send_header("Cache-Control", "no-store")
        if code != 304:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if code != 304 and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _err(self, code, msg):
        self._json(code, {"error": msg})

    # ---- checks ----
    def _origins(self):
        port = self.server.server_address[1]
        return set("http://%s:%d" % (h, port) for h in ALLOWED_HOSTS)

    def _guard(self):
        if _host_part(self.headers.get("Host")) not in ALLOWED_HOSTS:
            self._err(403, "forbidden host")
            return False
        if self.command != "GET":
            if self.headers.get("X-Relay-Desk") != "1":
                self._err(403, "missing X-Relay-Desk header")
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in self._origins():
                self._err(403, "forbidden origin")
                return False
        return True

    def _read_json(self):
        """Return (ok, value). On failure the error response is already sent."""
        raw = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding"):
            self._err(411, "chunked bodies are not supported")
            return False, None
        try:
            length = int(raw) if raw is not None else 0
        except ValueError:
            self._err(400, "bad Content-Length")
            return False, None
        if length < 0:
            self._err(400, "bad Content-Length")
            return False, None
        if length > MAX_BODY:
            self._err(413, "body too large")
            return False, None
        data = self.rfile.read(length) if length else b""
        self._body_read = True
        try:
            return True, json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._err(400, "bad JSON")
            return False, None

    # ---- dispatch ----
    def _handle(self):
        self._body_read = False
        if not self._guard():
            return
        parts = urlsplit(self.path)
        path = parts.path
        try:
            if self.command == "GET":
                if path == "/":
                    return self._get_index()
                if path == "/api/ledger":
                    return self._get_ledger(parse_qs(parts.query))
            elif self.command == "PUT":
                if path == "/api/config":
                    return self._put_config()
                if path == "/api/ledger":
                    return self._put_ledger()
                pid = self._project_id(path)
                if pid is not None:
                    return self._put_project(pid)
            elif self.command == "DELETE":
                pid = self._project_id(path)
                if pid is not None:
                    return self._delete_project(pid)
        except (BrokenPipeError, ConnectionError):
            return
        except Exception as e:  # keep the service alive
            self.log_error("%s", repr(e))
            return self._err(500, "internal error")
        self._err(404, "not found")

    do_GET = do_PUT = do_DELETE = do_POST = do_HEAD = do_PATCH = do_OPTIONS = _handle

    def _project_id(self, path):
        """Id for /api/projects/<id>; None if the path is not that route;
        "" if the route matches but the id is invalid."""
        prefix = "/api/projects/"
        if not path.startswith(prefix):
            return None
        raw = path[len(prefix):]
        if "/" in raw or not raw:
            return None
        pid = unquote(raw)
        if "/" in pid or "\\" in pid:
            return None  # encoded path separators: treat as an unknown path
        if pid in (".", "..") or not ID_RE.match(pid):
            return ""
        return pid

    # ---- routes ----
    def _get_index(self):
        with open(self.server.html_path, "r", encoding="utf-8") as f:
            page = f.read()
        self._send(200, (HTML_HEAD + page + HTML_TAIL).encode("utf-8"), "text/html; charset=utf-8")

    def _get_ledger(self, query):
        ledger, rev = self.server.store.read()
        want = query.get("rev", [None])[0]
        if want is not None and want == str(rev):
            return self._send(304)
        self._json(200, {"rev": rev, "config": ledger.get("config", {}), "projects": ledger.get("projects", [])})

    def _put_project(self, pid):
        if pid == "":
            return self._err(400, "invalid project id")
        ok, body = self._read_json()
        if not ok:
            return
        if not isinstance(body, dict):
            return self._err(400, "project must be an object")
        body = dict(body)
        body["id"] = pid

        def fn(ledger):
            projects = ledger.setdefault("projects", [])
            for i, p in enumerate(projects):
                if isinstance(p, dict) and p.get("id") == pid:
                    projects[i] = body
                    return
            projects.append(body)

        _, rev = self.server.store.update(fn)
        self._json(200, {"rev": rev})

    def _delete_project(self, pid):
        if pid == "":
            return self._err(400, "invalid project id")
        ledger, _ = self.server.store.read()
        if not any(isinstance(p, dict) and p.get("id") == pid for p in ledger.get("projects", [])):
            return self._err(404, "no such project")

        def fn(ledger):
            ledger["projects"] = [p for p in ledger.get("projects", [])
                                  if not (isinstance(p, dict) and p.get("id") == pid)]

        _, rev = self.server.store.update(fn)
        self._json(200, {"rev": rev})

    def _put_config(self):
        ok, body = self._read_json()
        if not ok:
            return
        if not isinstance(body, dict):
            return self._err(400, "config must be an object")

        def fn(ledger):
            ledger["config"] = body

        _, rev = self.server.store.update(fn)
        self._json(200, {"rev": rev})

    def _put_ledger(self):
        ok, body = self._read_json()
        if not ok:
            return
        if not (isinstance(body, dict) and isinstance(body.get("config"), dict)
                and isinstance(body.get("projects"), list)
                and all(isinstance(p, dict) and ID_RE.match(str(p.get("id", ""))) for p in body["projects"])):
            return self._err(400, "body must be {config: object, projects: [project with id]}")

        def fn(ledger):
            ledger["config"] = body["config"]
            ledger["projects"] = body["projects"]

        _, rev = self.server.store.update(fn)
        self._json(200, {"rev": rev})


def make_server(html_path, store, port=0):
    """Return an unstarted server bound to 127.0.0.1 (port 0 = random free port)."""
    srv = _Server(("127.0.0.1", port), _Handler)
    srv.html_path = html_path
    srv.store = store
    return srv


def serve(html_path, store, port=8765, open_browser=True):
    """Run the service until interrupted. Binds 127.0.0.1 only."""
    srv = make_server(html_path, store, port)
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    print("本机服务已启动：%s （按 Ctrl+C 停止）" % url, flush=True)
    if open_browser:
        try:
            import webbrowser
            threading.Timer(0.3, webbrowser.open, args=(url,)).start()
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
