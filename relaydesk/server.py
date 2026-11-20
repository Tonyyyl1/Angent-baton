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


class _InvalidProjectId(ValueError):
    """A new project ID that publish/send/receive would refuse; nothing was written."""


def _require_new_project_ids(ids, existing):
    """Fix G: project IDs entering the ledger follow the engine's projectId rule (Phase A).

    IDs already in the ledger (registered by an older version) stay editable.
    """
    from . import validation as V
    for pid in ids:
        if pid in existing:
            continue
        try:
            V.validate_project_id(pid)
        except V.ValidationError as e:
            raise _InvalidProjectId(e.message)

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


class SyncWatchWorker(object):
    """Runs one continuous sync session in a background thread for the local web page.

    The session takes the workspace lock per round, so send/receive requests
    from the page get ``busy`` only while a round is running. Events are read
    from the session's bounded log; ``stop`` cancels between phases and waits.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.cancel = None
        self.session = None
        self.args = None
        self.final = None
        self.crash = None

    def running(self):
        t = self.thread
        return t is not None and t.is_alive()

    def start(self, led_path, project, interval, max_backoff):
        from . import relay_engine

        with self.lock:
            args = {"project": project or "", "interval": float(interval), "maxBackoff": float(max_backoff)}
            if self.running():
                if args == self.args:
                    return 200, self.state(0, events=False)
                return 409, {"error": "持续同步已在运行（参数不同），请先停止。", "code": "watch_running",
                             "errorClass": "local_busy", "args": self.args}
            cancel = threading.Event()
            session = relay_engine.SyncSession(project_key=project, ledger_path=led_path, interval=interval,
                                               max_backoff=max_backoff, cancel=cancel, mode="server",
                                               status_file=True)
            self.cancel, self.session, self.args, self.final, self.crash = cancel, session, args, None, None
            self.thread = threading.Thread(target=self._run, name="relay-sync-watch", daemon=True)
            self.thread.start()
        return 200, self.state(0, events=False)

    def _run(self):
        try:
            self.final = self.session.run(watch=True)
        except BaseException as e:  # keep the server alive; report through state()
            self.crash = "%s: %s" % (e.__class__.__name__, e)

    def brief(self):
        session = self.session
        if session is None:
            return {"running": False, "status": "", "runId": "", "lastSeq": 0, "cycles": 0}
        snap = self.final or session.summary()
        return {"running": self.running(), "status": snap.get("status", ""), "runId": session.log.run_id,
                "lastSeq": session.log.last_seq, "cycles": session.cycles}

    def state(self, since=0, events=True):
        session = self.session
        if session is None:
            return {"running": False, "status": "", "runId": "", "cycles": 0, "counters": None, "peers": {},
                    "unresolved": [], "events": [], "gap": 0, "lastSeq": 0}
        summary = self.final or session.summary()
        items, gap = session.log.events_since(since) if events else ([], 0)
        out = {k: summary.get(k) for k in ("status", "runId", "cycles", "counters", "peers", "unresolved",
                                            "unresolvedTruncated", "recentEventsTruncated", "startedAt",
                                            "finishedAt", "interval", "maxBackoff", "project")}
        out.update({"running": self.running(), "events": items, "gap": gap, "lastSeq": session.log.last_seq})
        if self.crash:
            out["crash"] = self.crash
        return out

    def stop(self, timeout=15.0):
        with self.lock:
            thread, cancel = self.thread, self.cancel
            if cancel is not None:
                cancel.set()
        if thread is not None:
            thread.join(timeout)
        session = self.session
        if session is None:
            return {"running": False, "status": ""}
        summary = dict(self.final or session.summary())
        summary["running"] = self.running()
        return summary


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    html_path = None
    store = None
    sync_worker = None


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
                if path == "/api/devices":
                    return self._get_devices()
                if path == "/api/cloud/status":
                    return self._get_cloud_status(parse_qs(parts.query))
                if path == "/api/cloud/inbox":
                    return self._get_cloud_inbox()
                if path == "/api/cloud/transcripts":
                    return self._get_cloud_transcripts(parse_qs(parts.query))
                if path == "/api/cloud/recover":
                    return self._get_cloud_recover(parse_qs(parts.query))
                if path == "/api/cloud/sync/watch":
                    return self._get_sync_watch(parse_qs(parts.query))
                if path == "/api/cloud/doctor":
                    return self._get_doctor(parse_qs(parts.query))
                if path == "/api/cloud/cache":
                    return self._get_cache_status(parse_qs(parts.query))
            elif self.command == "POST":
                if path == "/api/cloud/pair":
                    return self._post_cloud_pair()
                if path == "/api/cloud/publish":
                    return self._post_cloud_publish()
                if path == "/api/cloud/sync":
                    return self._post_cloud_sync()
                if path == "/api/cloud/sync/watch":
                    return self._post_sync_watch()
                if path == "/api/cloud/sync/watch/stop":
                    return self._post_sync_watch_stop()
                if path == "/api/cloud/send":
                    return self._post_cloud_send()
                if path == "/api/cloud/receive":
                    return self._post_cloud_receive()
                if path == "/api/cloud/recover":
                    return self._post_cloud_recover()
                if path == "/api/cloud/cache/prune":
                    return self._post_cache_prune()
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
            _require_new_project_ids([pid], ())  # Fix G: a new ID must be one the engine accepts
            projects.append(body)

        try:
            _, rev = self.server.store.update(fn)
        except _InvalidProjectId as e:
            return self._err(400, str(e))
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
            existing = [p.get("id") for p in ledger.get("projects", []) if isinstance(p, dict)]
            _require_new_project_ids([p.get("id") for p in body["projects"]], existing)
            ledger["config"] = body["config"]
            ledger["projects"] = body["projects"]

        try:
            _, rev = self.server.store.update(fn)
        except _InvalidProjectId as e:
            return self._err(400, str(e))
        self._json(200, {"rev": rev})

    # ---- cloud relay routes ----
    def _run_cloud(self, fn, mutating=False):
        from . import ledger as L
        from . import relay_engine

        store = self.server.store
        led_path = getattr(store, "ledger_path", None) or getattr(store, "path", None)
        using_bridge = False
        if led_path is None:
            using_bridge = True
            bridge = L.local_dir() / "_mem_store_ledger.json"
            mem_led, _ = store.read()
            L.save(bridge, L._normalize(mem_led))
            led_path = bridge

        try:
            res = fn(relay_engine, led_path)
        except Exception as e:
            return self._handle_cloud_error(e, relay_engine)

        if using_bridge and mutating:
            updated = L.load(led_path)

            def _sync_mem(ledger):
                ledger.clear()
                ledger["config"] = updated.get("config", {})
                ledger["projects"] = updated.get("projects", [])

            store.update(_sync_mem)
        return self._json(200, res)

    def _handle_cloud_error(self, e, relay_engine):
        status, body = self._cloud_error_body(e, relay_engine)
        try:
            from . import sync_runtime

            body.setdefault("errorClass", sync_runtime.classify_error(e, "api").error_class)
        except Exception:
            pass
        return self._json(status, body)

    def _cloud_error_body(self, e, relay_engine):
        """(HTTP status, body) for an engine error; re-raises errors that are not relay-desk's."""
        cls_name = e.__class__.__name__
        code_attr = getattr(e, "code", "")
        if isinstance(e, relay_engine.NeedTargetPathError) or code_attr == "need_path":
            return (
                409,
                {
                    "error": str(e),
                    "code": "need_path",
                    "projectId": getattr(e, "project_id", ""),
                    "projectName": getattr(e, "project_name", ""),
                },
            )
        if cls_name == "TargetDirtyError" or code_attr == "target_dirty":
            return (
                409,
                {
                    "error": str(e),
                    "code": "target_dirty",
                    "projectId": getattr(e, "project_id", ""),
                    "projectName": getattr(e, "project_name", ""),
                    "targetPath": getattr(e, "target_path", ""),
                    "reasons": getattr(e, "reasons", []),
                },
            )
        if isinstance(e, relay_engine.DuplicateReceiveError) or code_attr == "duplicate_receive":
            return (
                409,
                {
                    "error": str(e),
                    "code": "duplicate_receive",
                    "relayId": getattr(e, "relay_id", ""),
                },
            )
        if isinstance(e, relay_engine.ReceiptUploadError) or code_attr == "receipt_upload_failed":
            return (
                409,
                {
                    "error": str(e),
                    "code": "receipt_upload_failed",
                    "relayId": getattr(e, "relay_id", ""),
                },
            )
        if (
            code_attr == "ambiguous_peer"
            or cls_name in ("AmbiguousPeerError", "AmbiguousDeviceError")
            or isinstance(e, getattr(relay_engine, "AmbiguousPeerError", ()))
        ):
            return (
                409,
                {
                    "error": str(e),
                    "code": "ambiguous_peer",
                    "candidates": getattr(e, "candidates", []),
                },
            )
        if (
            code_attr in ("recovery_conflict", "recovery_corrupted")
            or cls_name in ("RecoveryConflictError", "CorruptedRecoveryError")
        ):
            default_code = "recovery_corrupted" if cls_name == "CorruptedRecoveryError" else "recovery_conflict"
            return (
                409,
                {
                    "error": str(e),
                    "code": code_attr or default_code,
                    "relayId": getattr(e, "relay_id", ""),
                    "targetPath": getattr(e, "target_path", ""),
                    "reasons": getattr(e, "reasons", []),
                },
            )
        if (
            code_attr in ("version_fork", "version_regression")
            or cls_name == "VersionConflictError"
            or isinstance(e, getattr(relay_engine, "VersionConflictError", ()))
        ):
            return (
                409,
                {
                    "error": str(e),
                    "code": code_attr or "version_fork",
                    "localVersion": getattr(e, "local_version", ""),
                    "incomingVersion": getattr(e, "incoming_version", ""),
                },
            )
        if (
            code_attr == "peer_key_changed"
            or cls_name == "KeyChangedError"
            or isinstance(e, getattr(relay_engine, "KeyChangedError", ()))
        ):
            return (
                409,
                {
                    "error": str(e),
                    # Phase C adds handoff_recipient_key_changed, handoff_sender_key_rotated, sealed_to_retired_key.
                    "code": code_attr or "peer_key_changed",
                    "peerId": getattr(e, "peer_id", ""),
                    "oldFingerprint": getattr(e, "old_fingerprint", ""),
                    "newFingerprint": getattr(e, "new_fingerprint", ""),
                    # Phase D: the same recovery command relay doctor and the CLI show.
                    "action": getattr(e, "action", "") or "",
                },
            )
        if cls_name == "PrunePlanChangedError" or code_attr == "prune_plan_changed":
            return (409, {"error": str(e), "code": "prune_plan_changed", "plan": getattr(e, "plan", None)})
        if isinstance(e, LookupError) or cls_name == "ObjectNotFoundError":
            return (404, {"error": str(e), "code": "not_found"})
        if hasattr(e, "gaps"):
            return (
                400,
                {"error": str(e), "code": "conversation_gap", "gaps": getattr(e, "gaps", [])},
            )
        if hasattr(e, "findings"):
            return (
                400,
                {"error": str(e), "code": "secret_leak", "findings": getattr(e, "findings", [])},
            )
        if hasattr(e, "conflicts"):
            return (
                400,
                {"error": str(e), "code": "path_conflict", "conflicts": getattr(e, "conflicts", [])},
            )
        mod_name = getattr(e.__class__, "__module__", "") or ""
        if mod_name.startswith("relaydesk."):
            body = {"error": str(e), "code": code_attr or cls_name}
            if getattr(e, "action", ""):
                body["action"] = e.action
            return (400, body)
        raise e

    def _read_json_obj(self):
        ok, body = self._read_json()
        if not ok:
            return False, None
        if not isinstance(body, dict):
            self._err(400, "body must be an object")
            return False, None
        return True, body

    def _read_optional_json_obj(self):
        raw_cl = self.headers.get("Content-Length")
        if not self.headers.get("Transfer-Encoding") and (raw_cl is None or raw_cl.strip() == "0"):
            self._body_read = True
            return True, {}
        return self._read_json_obj()

    def _get_devices(self):
        return self._run_cloud(
            lambda eng, lp: eng.list_devices(ledger_path=lp),
            mutating=False,
        )

    def _get_cloud_status(self, query=None):
        with_q = (query or {}).get("with", [None])[0]

        def _run(eng, lp):
            st = eng.cloud_status(ledger_path=lp)
            if with_q and str(with_q).strip():
                q = str(with_q).strip()
                ql = q.lower()
                peers = [p for p in (st.get("peers") or []) if isinstance(p, dict)]
                matches = [p for p in peers if str(p.get("deviceId") or "") == q]
                if not matches:
                    matches = [p for p in peers if str(p.get("alias") or "") == q]
                if not matches:
                    matches = [p for p in peers if str(p.get("alias") or "").lower() == ql]
                if not matches:
                    matches = [p for p in peers if str(p.get("name") or "").strip().lower() == ql]
                if len(matches) > 1:
                    cands = [
                        {
                            "alias": str(m.get("alias") or ""),
                            "deviceId": str(m.get("deviceId") or ""),
                            "name": str(m.get("name") or ""),
                            "os": str(m.get("os") or ""),
                        }
                        for m in matches
                    ]
                    raise eng.AmbiguousPeerError("「%s」匹配到多台远端设备。" % q, candidates=cands)
                if not matches:
                    raise LookupError("找不到远端设备「%s」。" % q)
                sel = matches[0]
                st = dict(st)
                st["peer"] = sel
                st["paired"] = bool(sel.get("paired") and sel.get("status") == "paired")
                st["pairFingerprint"] = str(sel.get("pairFingerprint") or "")
            st = dict(st)
            st["syncWatch"] = self._worker().brief()
            try:
                from . import ledger as L
                from . import sync_runtime

                st["lastSync"] = sync_runtime.read_status_file(L.local_dir() / "sync_status.json", brief=True)
            except Exception:
                st["lastSync"] = None
            return st

        return self._run_cloud(_run, mutating=False)

    # ---- continuous sync (one worker per server)
    def _worker(self):
        srv = self.server
        if getattr(srv, "sync_worker", None) is None:
            srv.sync_worker = SyncWatchWorker()
        return srv.sync_worker

    def _watch_ledger_path(self):
        store = self.server.store
        return getattr(store, "ledger_path", None) or getattr(store, "path", None)

    def _post_sync_watch(self):
        ok, body = self._read_optional_json_obj()
        if not ok:
            return
        led_path = self._watch_ledger_path()
        if led_path is None:
            return self._json(400, {"error": "持续同步需要文件台账（relay serve）。", "code": "watch_requires_file_store",
                                    "errorClass": "needs_user_action"})
        try:
            interval = float(body.get("interval", 5.0))
            max_backoff = float(body.get("maxBackoff", 300.0))
        except (TypeError, ValueError):
            return self._err(400, "interval and maxBackoff must be numbers")
        if not (2.0 <= interval <= 3600.0) or not (interval <= max_backoff <= 3600.0):
            return self._err(400, "need 2 <= interval <= maxBackoff <= 3600")
        project = body.get("project") or body.get("projectId") or None
        if project is not None and not isinstance(project, str):
            return self._err(400, "project must be a string")
        from . import relay_engine

        try:
            code, payload = self._worker().start(led_path, project, interval, max_backoff)
        except Exception as e:
            return self._handle_cloud_error(e, relay_engine)
        return self._json(code, payload)

    def _get_sync_watch(self, query):
        raw = (query.get("since", ["0"])[0] or "0").strip()
        try:
            since = max(0, int(raw))
        except ValueError:
            return self._err(400, "since must be an integer")
        return self._json(200, self._worker().state(since))

    def _post_sync_watch_stop(self):
        ok, _ = self._read_optional_json_obj()
        if not ok:
            return
        return self._json(200, self._worker().stop(15.0))

    def _post_cloud_pair(self):
        ok, body = self._read_json_obj()
        if not ok:
            return
        return self._run_cloud(
            lambda eng, lp: eng.pair_device(
                remote=body.get("remote"),
                root=body.get("root"),
                provider_type=body.get("provider"),
                rclone_config=body.get("configPath") or body.get("rcloneConfig"),
                accept=bool(body.get("accept")),
                with_device=body.get("with") or body.get("withDevice"),
                fingerprint=body.get("fingerprint"),
                alias=body.get("alias"),
                replace_device=bool(body.get("replaceDevice")),
                ledger_path=lp,
            ),
            mutating=True,
        )

    def _post_cloud_publish(self):
        ok, body = self._read_optional_json_obj()
        if not ok:
            return
        return self._run_cloud(
            lambda eng, lp: eng.publish_project(
                project_key=body.get("project") or body.get("projectId"),
                ledger_path=lp,
            ),
            mutating=True,
        )

    def _post_cloud_sync(self):
        ok, body = self._read_optional_json_obj()
        if not ok:
            return
        if self._worker().running():
            return self._json(409, {"error": "持续同步正在运行，请先停止或等待其结果。", "code": "watch_running",
                                    "errorClass": "local_busy"})
        return self._run_cloud(
            lambda eng, lp: eng.sync_network(
                project_key=body.get("project") or body.get("projectId"),
                ledger_path=lp,
            ),
            mutating=True,
        )

    def _get_cloud_inbox(self):
        return self._run_cloud(
            lambda eng, lp: eng.list_inbox(ledger_path=lp),
            mutating=True,
        )

    def _post_cloud_send(self):
        ok, body = self._read_json_obj()
        if not ok:
            return
        reissue = body.get("reissue", False)
        if not isinstance(reissue, bool):
            return self._err(400, "reissue must be a boolean")
        kw = {"reissue": True} if reissue else {}  # only an explicit request re-issues
        return self._run_cloud(
            lambda eng, lp: eng.send_project(
                project_key=body.get("project") or body.get("projectId"),
                to_alias=body.get("to"),
                ledger_path=lp,
                **kw
            ),
            mutating=True,
        )

    # ---- Phase D: read-only diagnosis, cache statistics, cache prune (same functions as the CLI)
    def _get_doctor(self, query):
        project = (query.get("project", [None])[0] or "").strip() or None

        def _run(eng, lp):
            from . import diagnostics
            return diagnostics.run_doctor(project, ledger_path=lp)

        return self._run_cloud(_run, mutating=False)

    def _get_cache_status(self, query):
        project = (query.get("project", [None])[0] or "").strip() or None

        def _run(eng, lp):
            from . import cache_prune
            return cache_prune.cache_status(project, ledger_path=lp)

        return self._run_cloud(_run, mutating=False)

    def _post_cache_prune(self):
        ok, body = self._read_optional_json_obj()
        if not ok:
            return
        mode = body.get("mode", "dry-run")
        if mode not in ("dry-run", "apply"):
            return self._err(400, "mode must be dry-run or apply")
        project = body.get("project") or body.get("projectId") or None
        expect = body.get("expectPlan") or None
        if (project is not None and not isinstance(project, str)) or (expect is not None and not isinstance(expect, str)):
            return self._err(400, "project and expectPlan must be strings")

        def _run(eng, lp):
            from . import cache_prune
            return cache_prune.prune(project, ledger_path=lp, apply=(mode == "apply"), expect_plan=expect)

        return self._run_cloud(_run, mutating=False)

    def _post_cloud_receive(self):
        ok, body = self._read_json_obj()
        if not ok:
            return
        relay_id = body.get("relayId") or body.get("relay_id") or body.get("id")
        if not isinstance(relay_id, str) or not relay_id.strip():
            return self._err(400, "missing relayId")
        return self._run_cloud(
            lambda eng, lp: eng.receive_relay(
                relay_id.strip(),
                target_path=body.get("path") or None,
                into_path=body.get("into") or None,
                ledger_path=lp,
            ),
            mutating=True,
        )

    def _get_cloud_recover(self, query):
        relay_id = (
            query.get("relayId", [None])[0]
            or query.get("relay_id", [None])[0]
            or query.get("id", [None])[0]
            or ""
        ).strip()
        if not relay_id:
            return self._err(400, "missing relayId")
        return self._run_cloud(
            lambda eng, lp: eng.recover_relay(
                relay_id,
                action="status",
                ledger_path=lp,
            ),
            mutating=False,
        )

    def _post_cloud_recover(self):
        ok, body = self._read_json_obj()
        if not ok:
            return
        relay_id = body.get("relayId") or body.get("relay_id") or body.get("id")
        if not isinstance(relay_id, str) or not relay_id.strip():
            return self._err(400, "missing relayId")
        action = body.get("action") or "status"
        if not isinstance(action, str):
            return self._err(400, "invalid action")
        return self._run_cloud(
            lambda eng, lp: eng.recover_relay(
                relay_id.strip(),
                action=action,
                ledger_path=lp,
            ),
            mutating=(action.strip().lower() == "rollback"),
        )

    def _get_cloud_transcripts(self, query):
        project = query.get("project", [None])[0]
        session = query.get("session", [None])[0]
        tool = query.get("tool", [None])[0]
        grep = query.get("q", [None])[0] or query.get("grep", [None])[0]
        return self._run_cloud(
            lambda eng, lp: eng.query_transcripts(
                project_key=project,
                session_id=session,
                tool=tool,
                grep=grep,
                ledger_path=lp,
            ),
            mutating=False,
        )


def make_server(html_path, store, port=0):
    """Return an unstarted server bound to 127.0.0.1 (port 0 = random free port)."""
    srv = _Server(("127.0.0.1", port), _Handler)
    srv.html_path = html_path
    srv.store = store
    srv.sync_worker = SyncWatchWorker()
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
        try:
            srv.sync_worker.stop(15.0)
        except Exception:
            pass
        srv.server_close()
