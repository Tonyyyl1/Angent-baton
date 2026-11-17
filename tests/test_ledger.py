import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from relaydesk import ledger as L


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("RELAY_DESK_") and k != "CODEX_HOME"}
        env["RELAY_DESK_HOME"] = str(self.dir / "home")
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.path = self.dir / "ledger.json"


class TestNormalise(Base):
    def test_default_config(self):
        c = L.default_config()
        self.assertEqual([d["id"] for d in c["devices"]], ["a", "b"])
        self.assertEqual(c["devices"][0]["os"], "windows")
        self.assertEqual(c["devices"][1]["os"], "mac")
        self.assertEqual(c["devices"][0]["host"], "")
        self.assertEqual(c["reg"], {})

    def test_norm_config_defaults_and_merge(self):
        c = L.norm_config(None)
        self.assertEqual(c, L.default_config())
        c = L.norm_config({"devices": [{"id": "a", "name": "X"}, {"id": "b", "os": "linux"}], "reg": {"k": 1}, "extra": 5})
        self.assertEqual(c["devices"][0]["name"], "X")
        self.assertEqual(c["devices"][0]["os"], "windows")  # filled from defaults
        self.assertEqual(c["devices"][1]["os"], "linux")
        self.assertEqual(c["devices"][1]["drop"], "")
        self.assertEqual(c["reg"], {"k": 1})
        self.assertEqual(c["extra"], 5)  # unknown field kept

    def test_norm_config_wrong_device_count_resets(self):
        c = L.norm_config({"devices": [{"id": "a"}]})
        self.assertEqual(len(c["devices"]), 2)
        self.assertEqual(c["devices"][0]["name"], "设备 A")

    def test_norm_project_defaults(self):
        cfg = L.default_config()
        p = L.norm_project({"id": "x"}, cfg)
        self.assertEqual(p["name"], "未命名项目")
        self.assertEqual(p["device"], "a")
        self.assertEqual(p["tool"], "claude")
        self.assertEqual(p["status"], "active")
        self.assertEqual(p["dirty"], 0)
        self.assertIsNone(p["unpushed"])
        self.assertIsNone(p["handoff"])
        self.assertEqual(p["paths"], {})
        self.assertEqual(p["verify"], {"state": "none", "cmd": "", "note": ""})
        self.assertEqual(p["reg"], {})
        for k in ("sessions", "conflicts", "history"):
            self.assertEqual(p[k], [])

    def test_norm_project_fixes(self):
        cfg = L.default_config()
        p = L.norm_project({"id": "x", "device": "zzz", "dirty": "3", "verify": {"state": "pass"},
                            "sessions": "bad", "custom": {"k": 1}}, cfg)
        self.assertEqual(p["device"], "a")
        self.assertEqual(p["dirty"], 3)
        self.assertEqual(p["verify"], {"state": "pass", "cmd": "", "note": ""})
        self.assertEqual(p["sessions"], [])
        self.assertEqual(p["custom"], {"k": 1})
        p = L.norm_project({"id": "x", "dirty": "abc"}, cfg)
        self.assertEqual(p["dirty"], 0)
        p = L.norm_project({"id": "x", "device": "b"}, cfg)
        self.assertEqual(p["device"], "b")


class TestStorage(Base):
    def test_load_missing_is_empty(self):
        led = L.load(self.path)
        self.assertEqual(led["app"], "relaydesk")
        self.assertEqual(led["version"], 1)
        self.assertEqual(led["projects"], [])
        self.assertEqual(led["config"], L.default_config())
        self.assertFalse(self.path.exists())

    def test_round_trip_preserves_unknown_fields(self):
        raw = {
            "app": "relaydesk", "version": 1, "topExtra": {"x": [1, 2]},
            "config": {"devices": [{"id": "a", "name": "W", "os": "windows", "drop": "", "host": "H", "zz": 1},
                                   {"id": "b", "name": "M", "os": "mac", "drop": "", "host": ""}],
                       "reg": {"claudeMd": {"on": {"a": True}, "note": ""}}, "cfgExtra": "keep"},
            "projects": [{"id": "p1", "name": "项目一", "device": "a", "paths": {"a": "D:\\x"},
                          "altPaths": {"a": ["C:\\y"]}, "unknownField": {"deep": [1, {"z": None}]},
                          "git": {"head": "abc", "future": 1},
                          "sessions": [{"id": "s_1", "sessionId": "q", "extraS": 7}],
                          "handoff": {"tag": "t", "state": "prep", "pre": {}, "post": {}, "weird": True}}],
        }
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        def fn(led):
            led["projects"][0]["note"] = "changed"
        L.update(self.path, fn)
        back = L.load(self.path)
        self.assertEqual(back["topExtra"], {"x": [1, 2]})
        self.assertEqual(back["config"]["cfgExtra"], "keep")
        self.assertEqual(back["config"]["devices"][0]["zz"], 1)
        p = back["projects"][0]
        self.assertEqual(p["note"], "changed")
        self.assertEqual(p["unknownField"], {"deep": [1, {"z": None}]})
        self.assertEqual(p["altPaths"], {"a": ["C:\\y"]})
        self.assertEqual(p["git"]["future"], 1)
        self.assertEqual(p["sessions"][0]["extraS"], 7)
        self.assertTrue(p["handoff"]["weird"])

    def test_save_format_and_no_temp_left(self):
        led = L.load(self.path)
        led["projects"].append(L.norm_project({"id": "p", "name": "中文名"}, led["config"]))
        L.save(self.path, led)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("中文名", text)  # ensure_ascii=False
        self.assertIn("\n ", text)       # indent=1
        self.assertEqual([f.name for f in self.dir.iterdir() if f.name.endswith(".tmp")], [])

    def test_load_broken_file_raises(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(L.LedgerError):
            L.load(self.path)
        self.path.write_text("[1,2]", encoding="utf-8")
        with self.assertRaises(L.LedgerError):
            L.load(self.path)

    def test_update_returns_saved_ledger_and_rev(self):
        self.assertEqual(L.rev(self.path), "0")
        led = L.update(self.path, lambda l: l["projects"].append({"id": "n1", "name": "N"}))
        self.assertEqual(led["projects"][0]["status"], "active")  # normalised on the way out
        r1 = L.rev(self.path)
        self.assertNotEqual(r1, "0")
        self.assertRegex(r1, r"^\d+-\d+$")
        L.update(self.path, lambda l: l["projects"][0].update(note="more text"))
        self.assertNotEqual(L.rev(self.path), r1)

    def test_update_failure_does_not_save_and_releases_lock(self):
        L.update(self.path, lambda l: l["projects"].append({"id": "n1"}))

        def boom(l):
            l["projects"].append({"id": "n2"})
            raise RuntimeError("x")
        with self.assertRaises(RuntimeError):
            L.update(self.path, boom)
        self.assertEqual([p["id"] for p in L.load(self.path)["projects"]], ["n1"])
        self.assertFalse(Path(str(self.path) + ".lock").exists())

    def test_lock_contention_times_out(self):
        lock = Path(str(self.path) + ".lock")
        lock.write_text("123")
        with mock.patch.object(L, "LOCK_WAIT", 0.3):
            t0 = time.time()
            with self.assertRaises(L.LedgerError):
                L.update(self.path, lambda l: None)
            self.assertGreaterEqual(time.time() - t0, 0.25)
        self.assertTrue(lock.exists())  # somebody else's fresh lock is left alone

    def test_stale_lock_is_cleared(self):
        lock = Path(str(self.path) + ".lock")
        lock.write_text("123")
        old = time.time() - 120
        os.utime(str(lock), (old, old))
        L.update(self.path, lambda l: l["projects"].append({"id": "after-stale"}))
        self.assertEqual(L.load(self.path)["projects"][0]["id"], "after-stale")
        self.assertFalse(lock.exists())

    def test_lock_released_by_other_thread_unblocks_waiter(self):
        lock = Path(str(self.path) + ".lock")
        lock.write_text("123")
        threading.Timer(0.3, lambda: os.remove(str(lock))).start()
        L.update(self.path, lambda l: l["projects"].append({"id": "waited"}))
        self.assertEqual(L.load(self.path)["projects"][0]["id"], "waited")

    def test_concurrent_updates_lose_nothing(self):
        errors = []

        def work(i):
            try:
                L.update(self.path, lambda l: l["projects"].append({"id": "t%d" % i}))
            except Exception as e:  # pragma: no cover
                errors.append(e)
        ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(p["id"] for p in L.load(self.path)["projects"]), ["t%d" % i for i in range(8)])


def make_ledger():
    led = L.load("does-not-exist.json")
    cfg = led["config"]
    led["projects"] = [
        L.norm_project({"id": "animal-sim", "name": "Animal Sim", "paths": {"a": os.path.join(tempfile.gettempdir(), "zz_animal"), "b": ""},
                        "altPaths": {"a": [os.path.join(tempfile.gettempdir(), "zz_alt")]}}, cfg),
        L.norm_project({"id": "animal-tools", "name": "Tools", "paths": {"a": os.path.join(tempfile.gettempdir(), "zz_tools")}}, cfg),
        L.norm_project({"id": "scala-cw", "name": "Scala CW", "paths": {"a": os.path.join(tempfile.gettempdir(), "zz_scala")}}, cfg),
    ]
    return led


class TestFind(Base):
    def test_by_id_name_prefix(self):
        led = make_ledger()
        self.assertEqual(L.find_project(led, "scala-cw")["id"], "scala-cw")
        self.assertEqual(L.find_project(led, "SCALA CW")["id"], "scala-cw")  # name, case-insensitive
        self.assertEqual(L.find_project(led, "tools")["id"], "animal-tools")
        self.assertEqual(L.find_project(led, "scala")["id"], "scala-cw")     # unique prefix
        self.assertEqual(L.find_project(led, "animal-s")["id"], "animal-sim")

    def test_ambiguous_prefix(self):
        led = make_ledger()
        with self.assertRaises(LookupError) as cm:
            L.find_project(led, "animal")
        msg = str(cm.exception)
        self.assertIn("多个项目", msg)
        self.assertIn("animal-sim", msg)
        self.assertIn("animal-tools", msg)

    def test_not_found_message_lists_projects(self):
        led = make_ledger()
        with self.assertRaises(LookupError) as cm:
            L.find_project(led, "nope")
        msg = str(cm.exception)
        self.assertIn("找不到项目「nope」", msg)
        self.assertIn("scala-cw", msg)
        with self.assertRaises(LookupError) as cm:
            L.find_project(L.load("missing.json"), "nope")
        self.assertIn("还没有项目", str(cm.exception))

    def test_by_cwd(self):
        led = make_ledger()
        base = os.path.join(tempfile.gettempdir(), "zz_animal")
        self.assertEqual(L.find_project(led, None, os.path.join(base, "sub", "dir"))["id"], "animal-sim")
        self.assertEqual(L.find_project(led, None, base)["id"], "animal-sim")
        self.assertEqual(L.find_project(led, None, os.path.join(tempfile.gettempdir(), "zz_alt", "x"))["id"], "animal-sim")
        # sibling with the same text prefix must not match
        with self.assertRaises(LookupError):
            L.find_project(led, None, base + "_other")

    def test_by_cwd_failure_message(self):
        led = make_ledger()
        with self.assertRaises(LookupError) as cm:
            L.find_project(led, None, os.path.join(tempfile.gettempdir(), "elsewhere"))
        self.assertIn("当前目录", str(cm.exception))
        self.assertIn("scala-cw", str(cm.exception))

    def test_by_cwd_longest_match_wins(self):
        led = make_ledger()
        inner = os.path.join(tempfile.gettempdir(), "zz_animal", "inner")
        led["projects"][1]["paths"]["a"] = inner
        self.assertEqual(L.find_project(led, None, os.path.join(inner, "x"))["id"], "animal-tools")


class TestDevices(Base):
    def test_dev_other(self):
        cfg = L.default_config()
        self.assertEqual(L.dev(cfg, "b")["id"], "b")
        self.assertEqual(L.dev(cfg, "zz")["id"], "a")
        self.assertEqual(L.other(cfg, "a")["id"], "b")
        self.assertEqual(L.other(cfg, "b")["id"], "a")

    def test_here_by_host(self):
        led = L.load("missing.json")
        led["config"]["devices"][1]["host"] = "MacBook"
        with mock.patch.object(L, "hostname", return_value="macbook"), mock.patch.object(L, "local_os", return_value="windows"):
            self.assertEqual(L.here(led), "b")  # host wins over os
        with mock.patch.object(L, "hostname", return_value="MacBook.local"), mock.patch.object(L, "local_os", return_value="windows"):
            self.assertEqual(L.here(led), "b")  # short host name

    def test_here_by_os(self):
        led = L.load("missing.json")
        with mock.patch.object(L, "hostname", return_value="whatever"):
            with mock.patch.object(L, "local_os", return_value="mac"):
                self.assertEqual(L.here(led), "b")
            with mock.patch.object(L, "local_os", return_value="windows"):
                self.assertEqual(L.here(led), "a")
            with mock.patch.object(L, "local_os", return_value="linux"):
                self.assertIsNone(L.here(led))
            led["config"]["devices"][1]["os"] = "windows"  # not unique any more
            with mock.patch.object(L, "local_os", return_value="windows"):
                self.assertIsNone(L.here(led))

    def test_here_by_local_config_and_env(self):
        led = L.load("missing.json")
        with mock.patch.object(L, "hostname", return_value="x"), mock.patch.object(L, "local_os", return_value="windows"):
            self.assertEqual(L.here(led, {"device": "b"}), "b")      # local config beats os
            self.assertEqual(L.here(led, {"device": "zzz"}), "a")    # invalid -> falls through
            with mock.patch.dict(os.environ, {"RELAY_DESK_DEVICE": "b"}):
                self.assertEqual(L.here(led, {"device": "a"}), "b")  # env beats config
            with mock.patch.dict(os.environ, {"RELAY_DESK_DEVICE": "q"}):
                self.assertEqual(L.here(led, {"device": "b"}), "b")  # invalid env ignored

    def test_local_config_and_ledger_path(self):
        self.assertEqual(L.local_dir(), self.dir / "home")
        self.assertEqual(L.load_local(), {})
        L.save_local({"ledger": str(self.dir / "mine.json"), "device": "b"})
        self.assertEqual(L.load_local()["device"], "b")
        self.assertEqual(L.ledger_path(), self.dir / "mine.json")
        with mock.patch.dict(os.environ, {"RELAY_DESK_LEDGER": str(self.dir / "env.json")}):
            self.assertEqual(L.ledger_path(), self.dir / "env.json")
        self.assertEqual(L.ledger_path({}), L.tool_dir() / "ledger.json")

    def test_touch_and_new_id(self):
        p = {}
        L.touch(p, "a")
        self.assertRegex(p["updatedAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(p["updatedOn"], "a")
        i = L.new_id("s_")
        self.assertRegex(i, r"^s_[a-z0-9]{8}$")
        self.assertNotEqual(i, L.new_id("s_"))


if __name__ == "__main__":
    unittest.main()
