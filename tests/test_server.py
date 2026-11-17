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

    def test_binds_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
