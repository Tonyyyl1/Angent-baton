import copy
import http.client
import json
import os
import tempfile
import threading
import unittest

from relaydesk import server


class FakeStore(object):
    def __init__(self):
        self.ledger = {"config": {"devices": []}, "projects": [{"id": "p1", "name": "one"}]}
        self.n = 1
        self.lock = threading.Lock()

    def read(self):
        with self.lock:
            return copy.deepcopy(self.ledger), str(self.n)

    def update(self, fn):
        with self.lock:
            fn(self.ledger)
            self.n += 1
            return copy.deepcopy(self.ledger), str(self.n)


class ServerTest(unittest.TestCase):
    PAGE = u"<div id=app>中文 relay-desk</div><script>1</script>"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.html = os.path.join(cls.tmp.name, "page.html")
        with open(cls.html, "w", encoding="utf-8") as f:
            f.write(cls.PAGE)
        cls.store = FakeStore()
        cls.srv = server.make_server(cls.html, cls.store, 0)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, headers=None, raw=None, host=True):
        h = {}
        if method != "GET":
            h["X-Relay-Desk"] = "1"
        if headers:
            h.update(headers)
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.putrequest(method, path, skip_host=True)
            if host:
                h.setdefault("Host", "127.0.0.1:%d" % self.port)
            for k, v in h.items():
                if v is not None:
                    c.putheader(k, v)
            if data is not None:
                c.putheader("Content-Length", str(len(data)))
            c.endheaders(data)
            r = c.getresponse()
            return r.status, dict((k.lower(), v) for k, v in r.getheaders()), r.read()
        finally:
            c.close()

    def jreq(self, *a, **k):
        st, hd, body = self.req(*a, **k)
        return st, hd, (json.loads(body.decode("utf-8")) if body else None)

    def setUp(self):
        with self.store.lock:
            self.store.ledger = {"config": {"devices": []}, "projects": [{"id": "p1", "name": "one"}]}
            self.store.n = 1

    # ---- GET ----
    def test_index_wrapped(self):
        st, hd, body = self.req("GET", "/")
        text = body.decode("utf-8")
        self.assertEqual(st, 200)
        self.assertTrue(text.startswith("<!doctype html>"))
        self.assertIn(self.PAGE, text)
        self.assertTrue(text.endswith("</body></html>"))
        self.assertIn("text/html", hd["content-type"])
        self.assertEqual(hd["cache-control"], "no-store")

    def test_index_rereads_file(self):
        with open(self.html, "w", encoding="utf-8") as f:
            f.write("<p>v2</p>")
        try:
            _, _, body = self.req("GET", "/")
            self.assertIn(b"<p>v2</p>", body)
        finally:
            with open(self.html, "w", encoding="utf-8") as f:
                f.write(self.PAGE)

    def test_get_ledger_and_304(self):
        st, hd, d = self.jreq("GET", "/api/ledger")
        self.assertEqual(st, 200)
        self.assertEqual(d["rev"], "1")
        self.assertEqual(d["projects"][0]["id"], "p1")
        self.assertIn("config", d)
        self.assertEqual(hd["cache-control"], "no-store")
        st, hd, body = self.req("GET", "/api/ledger?rev=1")
        self.assertEqual(st, 304)
        self.assertEqual(body, b"")
        self.assertEqual(hd["cache-control"], "no-store")
        st, _, d = self.jreq("GET", "/api/ledger?rev=0")
        self.assertEqual(st, 200)

    def test_304_stops_after_change(self):
        self.jreq("PUT", "/api/config", {"devices": [1]})
        st, _, d = self.jreq("GET", "/api/ledger?rev=1")
        self.assertEqual(st, 200)
        self.assertEqual(d["rev"], "2")

    # ---- writes ----
    def test_put_project_create_and_replace(self):
        st, hd, d = self.jreq("PUT", "/api/projects/new-1", {"name": "N"})
        self.assertEqual(st, 200)
        self.assertEqual(d, {"rev": "2"})
        self.assertEqual(self.store.ledger["projects"][-1], {"name": "N", "id": "new-1"})
        st, _, d = self.jreq("PUT", "/api/projects/p1", {"id": "ignored", "name": "two", "x": 1})
        self.assertEqual(st, 200)
        self.assertEqual(d["rev"], "3")
        self.assertEqual(len(self.store.ledger["projects"]), 2)
        self.assertEqual(self.store.ledger["projects"][0], {"id": "p1", "name": "two", "x": 1})
        self.assertNotIn("access-control-allow-origin", hd)

    def test_delete(self):
        st, _, d = self.jreq("DELETE", "/api/projects/p1")
        self.assertEqual(st, 200)
        self.assertEqual(d, {"rev": "2"})
        self.assertEqual(self.store.ledger["projects"], [])
        st, _, _ = self.jreq("DELETE", "/api/projects/p1")
        self.assertEqual(st, 404)
        self.assertEqual(self.store.n, 2)

    def test_put_config(self):
        cfg = {"devices": [{"id": "a"}, {"id": "b"}], "reg": {}}
        st, _, d = self.jreq("PUT", "/api/config", cfg)
        self.assertEqual(st, 200)
        self.assertEqual(self.store.ledger["config"], cfg)
        self.assertEqual(self.store.ledger["projects"][0]["id"], "p1")
        st, _, _ = self.jreq("PUT", "/api/config", [1])
        self.assertEqual(st, 400)

    def test_put_ledger(self):
        body = {"config": {"devices": [{"id": "z"}]}, "projects": [{"id": "q1", "name": "Q"}, {"id": "q2"}]}
        st, _, d = self.jreq("PUT", "/api/ledger", body)
        self.assertEqual(st, 200)
        self.assertEqual(d["rev"], "2")
        self.assertEqual(self.store.ledger["config"], body["config"])
        self.assertEqual([p["id"] for p in self.store.ledger["projects"]], ["q1", "q2"])
        for bad in ({"config": {}}, {"projects": []}, {"config": {}, "projects": {}},
                    {"config": {}, "projects": [{"name": "no id"}]}, [], "x"):
            st, _, _ = self.jreq("PUT", "/api/ledger", bad)
            self.assertEqual(st, 400, bad)

    # ---- security ----
    def test_missing_header_403(self):
        for method, path, body in (("PUT", "/api/projects/p1", {"a": 1}), ("DELETE", "/api/projects/p1", None),
                                   ("PUT", "/api/config", {}), ("PUT", "/api/ledger", {"config": {}, "projects": []})):
            st, _, _ = self.req(method, path, body, headers={"X-Relay-Desk": None})
            self.assertEqual(st, 403, (method, path))
            st, _, _ = self.req(method, path, body, headers={"X-Relay-Desk": "0"})
            self.assertEqual(st, 403, (method, path))
        self.assertEqual(self.store.n, 1)

    def test_foreign_origin_403(self):
        for origin in ("http://evil.example", "null", "http://127.0.0.1:1", "https://127.0.0.1:%d" % self.port,
                       "http://127.0.0.1.evil.example:%d" % self.port):
            st, hd, _ = self.req("PUT", "/api/config", {}, headers={"Origin": origin})
            self.assertEqual(st, 403, origin)
            self.assertNotIn("access-control-allow-origin", hd)
        self.assertEqual(self.store.n, 1)

    def test_own_origin_ok(self):
        for h in ("127.0.0.1", "localhost"):
            st, _, _ = self.req("PUT", "/api/config", {}, headers={"Origin": "http://%s:%d" % (h, self.port)})
            self.assertEqual(st, 200)

    def test_foreign_host_403(self):
        for host in ("evil.example", "evil.example:%d" % self.port, "127.0.0.1.evil.example", "[::1]:%d" % self.port, ""):
            st, _, _ = self.req("GET", "/api/ledger", headers={"Host": host})
            self.assertEqual(st, 403, host)
            st, _, _ = self.req("GET", "/", headers={"Host": host})
            self.assertEqual(st, 403, host)
        st, _, _ = self.req("GET", "/api/ledger", headers={"Host": "localhost:%d" % self.port})
        self.assertEqual(st, 200)
        st, _, _ = self.req("GET", "/api/ledger", headers={"Host": "LOCALHOST"})
        self.assertEqual(st, 200)

    def test_no_cors_headers(self):
        for method, path in (("GET", "/"), ("GET", "/api/ledger"), ("OPTIONS", "/api/ledger"), ("GET", "/nope")):
            _, hd, _ = self.req(method, path, headers={"Origin": "http://evil.example"} if method == "GET" else None)
            self.assertFalse([k for k in hd if k.startswith("access-control-")], (method, path))

    def test_oversize_body_rejected(self):
        big = b'{"name":"' + b"x" * (server.MAX_BODY + 10) + b'"}'
        st, _, _ = self.req("PUT", "/api/projects/p1", raw=big)
        self.assertEqual(st, 413)
        self.assertEqual(self.store.n, 1)
        ok = b'{"name":"' + b"x" * (server.MAX_BODY - 100) + b'"}'
        st, _, _ = self.req("PUT", "/api/projects/p1", raw=ok)
        self.assertEqual(st, 200)

    def test_invalid_id_rejected(self):
        for pid in ("a%20b", "a%00b", "x" * 81, "..", ".", "a$b", "%E4%B8%AD"):
            st, _, _ = self.req("PUT", "/api/projects/" + pid, {"a": 1})
            self.assertEqual(st, 400, pid)
            st, _, _ = self.req("DELETE", "/api/projects/" + pid)
            self.assertEqual(st, 400, pid)
        st, _, _ = self.req("PUT", "/api/projects/" + "x" * 80, {"a": 1})
        self.assertEqual(st, 200)
        self.assertEqual(self.store.n, 2)

    def test_path_traversal_404(self):
        for path in ("/../server.py", "/..%2f..%2fetc/passwd", "/api/../api/ledger/x", "/api/projects/..%2fx",
                     "/api/projects/a%5Cb", "/api/projects/a/b", "/api/projects/", "/api/projects",
                     "/etc/passwd", "/page.html", "/relay-desk.html", "/api/ledger/", "/api", "/index.html",
                     "/api/projects/../../x", "//etc/passwd"):
            st, _, _ = self.req("GET", path)
            self.assertEqual(st, 404, path)
            st, _, _ = self.req("PUT", path, {})
            self.assertIn(st, (404,), path)
            st, _, _ = self.req("DELETE", path)
            self.assertEqual(st, 404, path)

    def test_unknown_methods_404(self):
        for method in ("POST", "PATCH", "OPTIONS"):
            st, _, _ = self.req(method, "/api/ledger", {})
            self.assertEqual(st, 404, method)
        st, _, _ = self.req("DELETE", "/api/config")
        self.assertEqual(st, 404)
        st, _, _ = self.req("PUT", "/")
        self.assertEqual(st, 404)

    def test_bad_json_400(self):
        for raw in (b"{not json", b"", b"\xff\xfe"):
            st, _, _ = self.req("PUT", "/api/projects/p1", raw=raw)
            self.assertEqual(st, 400, raw)
            st, _, _ = self.req("PUT", "/api/config", raw=raw)
            self.assertEqual(st, 400, raw)
            st, _, _ = self.req("PUT", "/api/ledger", raw=raw)
            self.assertEqual(st, 400, raw)
        st, _, _ = self.req("PUT", "/api/projects/p1", raw=b"[1,2]")
        self.assertEqual(st, 400)
        self.assertEqual(self.store.n, 1)

    def test_sync_watch_requires_file_store(self):
        st, _, d = self.jreq("POST", "/api/cloud/sync/watch", {})
        self.assertEqual(st, 400)
        self.assertEqual(d["code"], "watch_requires_file_store")
        st, _, d = self.jreq("GET", "/api/cloud/sync/watch")
        self.assertEqual((st, d["running"], d["events"]), (200, False, []))

    def test_binds_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")


class CloudServerApiTest(unittest.TestCase):
    PAGE = u"<div id=app>cloud server test</div>"

    def setUp(self):
        from unittest import mock
        from pathlib import Path
        from relaydesk import cli
        from relaydesk import ledger as L

        self.mock = mock
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cloud = self.root / "cloud"
        self.cloud.mkdir()

        self.html = str(self.root / "page.html")
        with open(self.html, "w", encoding="utf-8") as f:
            f.write(self.PAGE)

        self.devs = {}
        for dev in ("a", "b", "c"):
            home = self.root / ("home_" + dev)
            uhome = self.root / ("uhome_" + dev)
            led = self.root / ("led_" + dev + ".json")
            home.mkdir(parents=True, exist_ok=True)
            uhome.mkdir(parents=True, exist_ok=True)
            (home / "config.json").write_text(json.dumps({"device": dev, "ledger": str(led)}), encoding="utf-8")
            if dev in ("a", "b"):
                L.update(led, lambda _l: None)
            else:
                def _init_c(l):
                    l["config"]["devices"].append(
                        {"id": "c", "name": "设备 C", "os": "linux", "drop": "", "host": "", "deviceId": ""}
                    )
                L.update(led, _init_c)
            srv = server.make_server(self.html, cli._FileStore(led), 0)
            port = srv.server_address[1]
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            self.devs[dev] = {"home": home, "uhome": uhome, "led": led, "srv": srv, "port": port}

        self.home_a = self.devs["a"]["home"]
        self.uhome_a = self.devs["a"]["uhome"]
        self.led_a = self.devs["a"]["led"]
        self.home_b = self.devs["b"]["home"]
        self.uhome_b = self.devs["b"]["uhome"]
        self.led_b = self.devs["b"]["led"]

    def _call(self, dev, method, path, body=None, headers=None, extra_env=None):
        info = self.devs[dev]
        port = info["port"]
        env = {
            "RELAY_DESK_HOME": str(info["home"]),
            "RELAY_DESK_LEDGER": str(info["led"]),
            "RELAY_DESK_DEVICE": dev,
            "RELAY_DESK_USER_HOME": str(info["uhome"]),
        }
        if extra_env:
            env.update(extra_env)
        h = {"Host": "127.0.0.1:%d" % port}
        if method != "GET":
            h["X-Relay-Desk"] = "1"
        if headers:
            h.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        with self.mock.patch.dict(os.environ, env):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                c.putrequest(method, path, skip_host=True)
                for k, v in h.items():
                    if v is not None:
                        c.putheader(k, v)
                if data is not None:
                    c.putheader("Content-Length", str(len(data)))
                c.endheaders(data)
                r = c.getresponse()
                raw = r.read()
                return r.status, (json.loads(raw.decode("utf-8")) if raw else None)
            finally:
                c.close()

    def test_cloud_http_routes_and_409_conflict_codes(self):
        from relaydesk import ledger as L
        from relaydesk import scan

        # Guard check on POST /api/cloud/pair without X-Relay-Desk header
        st_403, _ = self._call("a", "POST", "/api/cloud/pair", {"accept": False}, headers={"X-Relay-Desk": None})
        self.assertEqual(st_403, 403)

        st, d0 = self._call("a", "GET", "/api/cloud/status")
        self.assertEqual(st, 200)
        self.assertFalse(d0["configured"])

        # Pair A and B via HTTP API
        st, pa1 = self._call("a", "POST", "/api/cloud/pair", {"provider": "local", "root": str(self.cloud), "accept": False})
        self.assertEqual(st, 200)
        self.assertFalse(pa1["paired"])

        st, pb0 = self._call("b", "POST", "/api/cloud/pair", {"provider": "local", "root": str(self.cloud), "accept": False})
        self.assertEqual(st, 200)
        self.assertFalse(pb0["paired"])
        self.assertTrue(pb0["pairFingerprint"])

        st, pb1 = self._call("b", "POST", "/api/cloud/pair", {"accept": True, "fingerprint": pb0["pairFingerprint"]})
        self.assertEqual(st, 200)
        self.assertTrue(pb1["paired"])

        st, pa_st = self._call("a", "GET", "/api/cloud/status")
        self.assertEqual(st, 200)
        self.assertTrue(pa_st["pairFingerprint"])

        st, pa2 = self._call("a", "POST", "/api/cloud/pair", {"accept": True})
        self.assertEqual(st, 200)
        self.assertTrue(pa2["paired"])
        self.assertEqual(pa2["pairFingerprint"], pb1["pairFingerprint"])

        # Register folder project + session on A
        proj_a = self.root / "web_proj_a"
        proj_a.mkdir()
        (proj_a / "index.js").write_text("console.log(1);\n", encoding="utf-8")

        def _add_p(l):
            l["projects"].append(
                L.norm_project(
                    {
                        "id": "web-demo",
                        "name": "Web 工作台项目",
                        "device": "a",
                        "lastTask": "完成前端",
                        "nextAction": "联调接口",
                        "paths": {"a": str(proj_a), "b": ""},
                    },
                    l["config"],
                )
            )

        L.update(self.led_a, _add_p)
        cdir = self.uhome_a / ".claude" / "projects" / scan.enc_path(str(proj_a))
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "s_web_1.jsonl").write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "HTTP_QUERY_TOKEN"}}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        # POST /api/cloud/send on A
        st, send_res = self._call("a", "POST", "/api/cloud/send", {"project": "web-demo", "to": "b"})
        self.assertEqual(st, 200)
        self.assertTrue(send_res["ok"])
        self.assertEqual(send_res["message"], "已交棒，可以关机")
        rid1 = send_res["relayId"]

        # GET /api/cloud/inbox on B
        st, ib_b = self._call("b", "GET", "/api/cloud/inbox")
        self.assertEqual(st, 200)
        self.assertEqual(len(ib_b["incoming"]), 1)
        self.assertEqual(ib_b["incoming"][0]["projectName"], "Web 工作台项目")

        # POST /api/cloud/receive without path -> 409 need_path!
        st, err_np = self._call("b", "POST", "/api/cloud/receive", {"relayId": rid1})
        self.assertEqual(st, 409)
        self.assertEqual(err_np["code"], "need_path")
        self.assertEqual(err_np["projectName"], "Web 工作台项目")

        # POST /api/cloud/receive with path -> 200!
        proj_b = self.root / "web_proj_b"
        st, recv1 = self._call("b", "POST", "/api/cloud/receive", {"relayId": rid1, "path": str(proj_b)})
        self.assertEqual(st, 200)
        self.assertTrue(recv1["ok"])
        self.assertEqual((proj_b / "index.js").read_text(encoding="utf-8"), "console.log(1);\n")

        # GET /api/cloud/transcripts on B
        st, tr_b = self._call("b", "GET", "/api/cloud/transcripts?project=web-demo&q=HTTP_QUERY_TOKEN")
        self.assertEqual(st, 200)
        self.assertGreaterEqual(len(tr_b["matches"]), 1)
        st, tr_md = self._call("b", "GET", "/api/cloud/transcripts?project=web-demo&session=s_web_1")
        self.assertEqual(st, 200)
        self.assertIn("HTTP_QUERY_TOKEN", tr_md["markdown"])

        # Send back from B, modify proj_a locally so A gets 409 target_dirty, then recover with `into`!
        (proj_b / "index.js").write_text("console.log(2);\n", encoding="utf-8")
        st, send2 = self._call("b", "POST", "/api/cloud/send", {"project": "web-demo", "to": "a"})
        self.assertEqual(st, 200)
        rid2 = send2["relayId"]

        (proj_a / "index.js").write_text("console.log('dirty local A');\n", encoding="utf-8")
        st, err_dirty = self._call("a", "POST", "/api/cloud/receive", {"relayId": rid2})
        self.assertEqual(st, 409)
        self.assertEqual(err_dirty["code"], "target_dirty")
        self.assertEqual(err_dirty["projectName"], "Web 工作台项目")
        self.assertEqual((proj_a / "index.js").read_text(encoding="utf-8"), "console.log('dirty local A');\n")

        proj_a_into = self.root / "web_proj_a_into"
        st, recv2 = self._call("a", "POST", "/api/cloud/receive", {"relayId": rid2, "into": str(proj_a_into)})
        self.assertEqual(st, 200)
        self.assertTrue(recv2["ok"])
        self.assertEqual((proj_a_into / "index.js").read_text(encoding="utf-8"), "console.log(2);\n")
        self.assertEqual((proj_a / "index.js").read_text(encoding="utf-8"), "console.log('dirty local A');\n")

    def _pair_all_three(self):
        for dev in ("a", "b", "c"):
            st, _ = self._call(dev, "POST", "/api/cloud/pair", {"provider": "local", "root": str(self.cloud), "accept": False})
            self.assertEqual(st, 200)
        for me in ("a", "b", "c"):
            for peer in ("a", "b", "c"):
                if me == peer:
                    continue
                st, st_res = self._call(me, "GET", "/api/cloud/status?with=" + peer)
                self.assertEqual(st, 200, (me, peer, st_res))
                fp = st_res["pairFingerprint"]
                self.assertTrue(fp)
                st, acc_res = self._call(me, "POST", "/api/cloud/pair", {"accept": True, "with": peer, "fingerprint": fp})
                self.assertEqual(st, 200, (me, peer, acc_res))
                self.assertTrue(acc_res["paired"])

    def test_multi_device_and_recovery_http_routes(self):
        from relaydesk import ledger as L

        self._pair_all_three()

        # Guard checks: Host & CSRF X-Relay-Desk on all new routes
        st_host, _ = self._call("a", "GET", "/api/devices", headers={"Host": "evil.example"})
        self.assertEqual(st_host, 403)
        st_host_rec, _ = self._call("a", "GET", "/api/cloud/recover?relayId=r_1", headers={"Host": "evil.example"})
        self.assertEqual(st_host_rec, 403)
        for route, body in (
            ("/api/cloud/publish", {"project": "srv-multi"}),
            ("/api/cloud/sync", {}),
            ("/api/cloud/recover", {"relayId": "r_1", "action": "status"}),
        ):
            st_csrf, _ = self._call("a", "POST", route, body, headers={"X-Relay-Desk": None})
            self.assertEqual(st_csrf, 403, route)
            st_csrf2, _ = self._call("a", "POST", route, body, headers={"X-Relay-Desk": "0"})
            self.assertEqual(st_csrf2, 403, route)
            st_h, _ = self._call("a", "POST", route, body, headers={"Host": "evil.example"})
            self.assertEqual(st_h, 403, route)

        # GET /api/devices returns local + paired devices
        st, devs = self._call("a", "GET", "/api/devices")
        self.assertEqual(st, 200)
        self.assertEqual(devs["here"], "a")
        by_alias = {d["alias"]: d for d in devs["devices"]}
        self.assertEqual(by_alias["a"]["status"], "self")
        self.assertTrue(by_alias["b"]["paired"])
        self.assertTrue(by_alias["c"]["paired"])

        # Add project on A
        proj_a = self.root / "srv_multi_a"
        proj_a.mkdir()
        (proj_a / "main.py").write_text("x = 1\n", encoding="utf-8")
        L.update(
            self.devs["a"]["led"],
            lambda l: l["projects"].append(
                L.norm_project(
                    {
                        "id": "srv-multi",
                        "name": "多机服务项目",
                        "device": "a",
                        "paths": {"a": str(proj_a)},
                    },
                    l["config"],
                )
            ),
        )

        # POST /api/cloud/publish on A (keeps ownership on A, delivers to B and C)
        st, pub = self._call("a", "POST", "/api/cloud/publish", {"project": "srv-multi"})
        self.assertEqual(st, 200)
        self.assertTrue(pub["ok"])
        self.assertEqual(pub["device"], "a")
        self.assertEqual({d["alias"] for d in pub["deliveredTo"]}, {"b", "c"})
        self.assertEqual(L.load(self.devs["a"]["led"])["projects"][0]["device"], "a")

        # POST /api/cloud/sync on B
        st, sync_b = self._call("b", "POST", "/api/cloud/sync", {})
        self.assertEqual(st, 200)
        self.assertTrue(sync_b["ok"])
        self.assertEqual(len(sync_b["syncedSnapshots"]), 1)
        self.assertEqual(sync_b["syncedSnapshots"][0]["snapshotId"], pub["snapshotId"])

        # POST /api/cloud/send without `to` when 2 peers paired -> 409 ambiguous_peer with candidates
        st, err_amb = self._call("a", "POST", "/api/cloud/send", {"project": "srv-multi"})
        self.assertEqual(st, 409)
        self.assertEqual(err_amb["code"], "ambiguous_peer")
        cand_aliases = {c["alias"] for c in err_amb["candidates"]}
        self.assertEqual(cand_aliases, {"b", "c"})

        # Send to C and receive cleanly into proj_c
        st, send_c = self._call("a", "POST", "/api/cloud/send", {"project": "srv-multi", "to": "c"})
        self.assertEqual(st, 200)
        rid1 = send_c["relayId"]
        proj_c = self.root / "srv_multi_c"
        st, recv_c = self._call("c", "POST", "/api/cloud/receive", {"relayId": rid1, "path": str(proj_c)})
        self.assertEqual(st, 200)
        self.assertTrue(recv_c["ok"])
        self.assertEqual((proj_c / "main.py").read_text(encoding="utf-8"), "x = 1\n")

        # Send from C -> A, edit on A? No, send back A -> C after editing on C -> A -> C
        (proj_c / "main.py").write_text("x = 2\n", encoding="utf-8")
        st, send_a = self._call("c", "POST", "/api/cloud/send", {"project": "srv-multi", "to": "a"})
        self.assertEqual(st, 200)
        st, recv_a = self._call("a", "POST", "/api/cloud/receive", {"relayId": send_a["relayId"]})
        self.assertEqual(st, 200)
        (proj_a / "main.py").write_text("x = 3\n", encoding="utf-8")
        st, send_c2 = self._call("a", "POST", "/api/cloud/send", {"project": "srv-multi", "to": "c"})
        self.assertEqual(st, 200)
        rid_tx = send_c2["relayId"]

        # Inject fault during receive on C to leave transaction at WORKSPACE_COMMITTED
        with self.mock.patch.object(server._Handler, "log_error", lambda *a, **k: None):
            st_fault, _ = self._call(
                "c",
                "POST",
                "/api/cloud/receive",
                {"relayId": rid_tx},
                extra_env={"RELAY_DESK_FAULT_POINT": "tx_after_workspace_commit"},
            )
        self.assertEqual(st_fault, 500)
        self.assertEqual((proj_c / "main.py").read_text(encoding="utf-8"), "x = 3\n")

        # GET /api/cloud/recover missing relayId -> 400; valid -> 200 status
        st_bad_q, _ = self._call("c", "GET", "/api/cloud/recover")
        self.assertEqual(st_bad_q, 400)
        st_rec_get, rec_st = self._call("c", "GET", "/api/cloud/recover?relayId=" + rid_tx)
        self.assertEqual(st_rec_get, 200)
        self.assertTrue(rec_st["exists"])
        self.assertEqual(rec_st["phase"], "WORKSPACE_COMMITTED")
        self.assertTrue(rec_st["canRollback"])

        # Simulate post-crash user modification in proj_c -> recovery_conflict (409)
        (proj_c / "main.py").write_text("x = 999 # user edit\n", encoding="utf-8")
        st_conf_st, conf_st = self._call("c", "POST", "/api/cloud/recover", {"relayId": rid_tx, "action": "status"})
        self.assertEqual(st_conf_st, 200)
        self.assertEqual(conf_st["state"], "workspace_committed")
        self.assertFalse(conf_st["canRollback"])
        self.assertTrue(conf_st["reasons"])

        st_conf_409, conf_409 = self._call("c", "POST", "/api/cloud/recover", {"relayId": rid_tx, "action": "rollback"})
        self.assertEqual(st_conf_409, 409)
        self.assertEqual(conf_409["code"], "recovery_conflict")
        self.assertEqual(conf_409["relayId"], rid_tx)
        self.assertTrue(conf_409["targetPath"])
        self.assertTrue(conf_409["reasons"])

        # Revert post-crash edit back to post-tx content -> rollback succeeds and restores x = 2
        (proj_c / "main.py").write_text("x = 3\n", encoding="utf-8")
        st_rb, rb_res = self._call("c", "POST", "/api/cloud/recover", {"relayId": rid_tx, "action": "rollback"})
        self.assertEqual(st_rb, 200)
        self.assertEqual(rb_res["phase"], "ROLLED_BACK")
        self.assertEqual((proj_c / "main.py").read_text(encoding="utf-8"), "x = 2\n")


    # ---- Phase B: continuous sync worker
    def _wait_watch(self, dev, pred, timeout=30.0):
        import time as _time

        deadline = _time.monotonic() + timeout
        seen = []
        since = 0
        while _time.monotonic() < deadline:
            st, w = self._call(dev, "GET", "/api/cloud/sync/watch?since=%d" % since)
            self.assertEqual(st, 200)
            seen.extend(w["events"])
            since = w["lastSeq"]
            if pred(w, seen):
                return w, seen
            _time.sleep(0.1)
        self.fail("watch condition not reached; events=%r" % [e["kind"] for e in seen])

    def test_sync_watch_worker_routes(self):
        from relaydesk import ledger as L
        from relaydesk import relay_engine as E

        self._pair_all_three()
        proj_a = self.root / "watch_proj_a"
        proj_a.mkdir()
        (proj_a / "main.py").write_text("x = 1\n", encoding="utf-8")
        L.update(self.devs["a"]["led"], lambda l: l["projects"].append(
            L.norm_project({"id": "watch-demo", "name": "Watch", "device": "a", "paths": {"a": str(proj_a)}}, l["config"])))
        st, pub = self._call("a", "POST", "/api/cloud/publish", {"project": "watch-demo"})
        self.assertEqual(st, 200, pub)

        st, w0 = self._call("b", "GET", "/api/cloud/sync/watch")
        self.assertEqual((st, w0["running"]), (200, False))
        st, bad = self._call("b", "POST", "/api/cloud/sync/watch", {"interval": 0.5})
        self.assertEqual(st, 400)
        st, started = self._call("b", "POST", "/api/cloud/sync/watch", {"interval": 2, "maxBackoff": 30})
        self.assertEqual(st, 200, started)
        self.assertTrue(started["running"])
        run_id = started["runId"]
        st, again = self._call("b", "POST", "/api/cloud/sync/watch", {"interval": 2, "maxBackoff": 30})
        self.assertEqual((st, again["runId"]), (200, run_id))
        st, other = self._call("b", "POST", "/api/cloud/sync/watch", {"interval": 3, "maxBackoff": 30})
        self.assertEqual((st, other["code"]), (409, "watch_running"))
        st, once = self._call("b", "POST", "/api/cloud/sync", {})
        self.assertEqual((st, once["code"], once["errorClass"]), (409, "watch_running", "local_busy"))

        w, seen = self._wait_watch("b", lambda w, seen: any(e["kind"] == "snapshot_cached" for e in seen))
        self.assertEqual(seen[0]["kind"], "run_started")
        self.assertEqual(seen[0]["seq"], 1)
        self.assertEqual(w["gap"], 0)
        cached = next(e for e in seen if e["kind"] == "snapshot_cached")
        self.assertEqual(cached["snapshotId"], pub["snapshotId"])
        st, later = self._call("b", "GET", "/api/cloud/sync/watch?since=%d" % w["lastSeq"])
        self.assertEqual(st, 200)
        self.assertTrue(all(e["seq"] > w["lastSeq"] for e in later["events"]))
        st, status = self._call("b", "GET", "/api/cloud/status")
        self.assertEqual(st, 200)
        self.assertTrue(status["syncWatch"]["running"])
        self.assertEqual(status["syncWatch"]["runId"], run_id)

        st, stopped = self._call("b", "POST", "/api/cloud/sync/watch/stop", {})
        self.assertEqual(st, 200)
        self.assertEqual(stopped["status"], "cancelled")
        self.assertFalse(stopped["running"])
        self.assertGreaterEqual(stopped["counters"]["snapshotsCached"], 1)
        with E._workspace_operation(self.home_b):
            pass  # the worker released the per-round lock
        st, status = self._call("b", "GET", "/api/cloud/status")
        self.assertFalse(status["syncWatch"]["running"])
        self.assertEqual(status["lastSync"]["status"], "cancelled")
        self.assertFalse(status["lastSync"]["running"])
        st, once = self._call("b", "POST", "/api/cloud/sync", {})
        self.assertEqual(st, 200)
        self.assertEqual(once["status"], "success")

    def test_cloud_error_bodies_carry_error_class(self):
        st, d = self._call("a", "POST", "/api/cloud/sync", {})
        self.assertEqual(st, 400)
        self.assertEqual((d["code"], d["errorClass"]), ("not_configured", "needs_user_action"))
        st, d = self._call("a", "POST", "/api/cloud/sync/watch", {})
        self.assertEqual(st, 400)
        self.assertEqual(d["errorClass"], "needs_user_action")


class PhaseDDiagnosticsApiTest(unittest.TestCase):
    """CLI, local API and web page show the same reasons and recovery commands (one shared function)."""

    def setUp(self):
        from test_reissue import _ReissueBase

        class _Net(_ReissueBase):
            def runTest(self):  # pragma: no cover - only used as a fixture holder
                pass

        self.net = _Net()
        self.net.setUp()
        self.addCleanup(self.net.doCleanups)

    def _server(self, alias):
        from relaydesk import cli
        html = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay-desk.html")
        srv = server.make_server(html, cli._FileStore(self.net.devices[alias]["led"]), 0)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def _call(self, alias, port, method, path, body=None):
        h = {"Host": "127.0.0.1:%d" % port}
        if method != "GET":
            h["X-Relay-Desk"] = "1"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        with self.net.env(alias):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            try:
                c.putrequest(method, path, skip_host=True)
                for k, v in h.items():
                    c.putheader(k, v)
                if data is not None:
                    c.putheader("Content-Type", "application/json")
                    c.putheader("Content-Length", str(len(data)))
                c.endheaders(data)
                r = c.getresponse()
                raw = r.read()
                return r.status, (json.loads(raw.decode("utf-8")) if raw else None)
            finally:
                c.close()

    def test_cli_api_and_web_show_identical_reasons_and_commands(self):
        from test_reissue import _run_cli
        net = self.net
        sent = net.send("alpha", "beta")
        net.rotate("beta")
        net.reconfirm("alpha", "beta")
        net.rotate("gamma")
        net.sync("gamma")
        net.sync("alpha")  # gamma becomes key_changed on alpha
        port = self._server("alpha")
        with net.env("alpha"):
            cli_json = json.loads(_run_cli(["doctor", "--json"])["out"])
            cli_text = _run_cli(["doctor"])
        st, api = self._call("alpha", port, "GET", "/api/cloud/doctor")
        self.assertEqual(st, 200, api)
        key = lambda d: [(f["code"], f["severity"], f["reason"], f["command"]) for f in d["findings"]]
        self.assertEqual(key(api), key(cli_json))
        self.assertEqual(api["projects"], cli_json["projects"])
        self.assertEqual(cli_text["rc"], 4)
        codes = [f["code"] for f in api["findings"]]
        self.assertIn("handoff_recipient_key_changed", codes)
        self.assertIn("peer_key_changed", codes)
        for f in api["findings"]:
            self.assertIn("原因：%s" % f["reason"], cli_text["out"])
            self.assertIn("处理命令：%s" % f["command"], cli_text["out"])
        reissue = [f for f in api["findings"] if f["code"] == "handoff_recipient_key_changed"][0]
        # the engine's error (CLI and API) carries the very same command
        st, err = self._call("alpha", port, "POST", "/api/cloud/send", {"project": "demo", "to": "beta"})
        self.assertEqual((st, err["code"], err["action"]), (409, "handoff_recipient_key_changed", reissue["command"]))
        with net.env("alpha"):
            cli_err = _run_cli(["send", "demo", "--to", "beta"])
        self.assertIn(reissue["command"], cli_err["err"])
        # the web page renders exactly those reasons and commands
        from test_ui_skills import _find_node, esc_html, phase_d_fixture, render_phase_d
        node_bin = _find_node()
        if node_bin:
            html = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay-desk.html")
            out = render_phase_d(node_bin, html, phase_d_fixture(api))
            for f in api["findings"]:
                self.assertIn(esc_html(f["reason"]), out["inbox"])
                self.assertIn('<code class="dx-command">%s</code>' % esc_html(f["command"]), out["inbox"])
            for pv in api["projects"]:
                self.assertIn("当前编辑设备：%s" % esc_html(pv["editor"]["name"]), out["inbox"])
        # cache status and dry-run: same function behind CLI and API
        with net.env("alpha"):
            cli_status = json.loads(_run_cli(["cache", "status", "--json"])["out"])
            cli_dry = json.loads(_run_cli(["cache", "prune", "--json"])["out"])
        st, api_status = self._call("alpha", port, "GET", "/api/cloud/cache")
        self.assertEqual((st, api_status), (200, cli_status))
        st, api_dry = self._call("alpha", port, "POST", "/api/cloud/cache/prune", {"mode": "dry-run"})
        self.assertEqual(st, 200)
        self.assertEqual(api_dry["planDigest"], cli_dry["planDigest"])
        st, bad = self._call("alpha", port, "POST", "/api/cloud/cache/prune", {"mode": "now"})
        self.assertEqual(st, 400)
        st, changed = self._call("alpha", port, "POST", "/api/cloud/cache/prune",
                                 {"mode": "apply", "expectPlan": "0" * 64})
        self.assertEqual((st, changed["code"]), (409, "prune_plan_changed"))
        # explicit re-issue through the API
        st, res = self._call("alpha", port, "POST", "/api/cloud/send", {"project": "demo", "to": "beta", "reissue": True})
        self.assertEqual(st, 200, res)
        self.assertEqual(res["supersedesRelayId"], sent["relayId"])
        st, bad = self._call("alpha", port, "POST", "/api/cloud/send", {"project": "demo", "to": "beta", "reissue": "yes"})
        self.assertEqual(st, 400)
        st, after = self._call("alpha", port, "GET", "/api/cloud/doctor?project=demo")
        self.assertIn("handoff_superseded", [f["code"] for f in after["findings"]])
        self.assertNotIn("handoff_recipient_key_changed", [f["code"] for f in after["findings"]])


if __name__ == "__main__":
    unittest.main()
