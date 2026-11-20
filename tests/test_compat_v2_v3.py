# -*- coding: utf-8 -*-
"""Real subprocess compatibility tests between frozen v2 commit `dd0312e` and current v3.

Verifies:
(a) Old A (dd0312e) <-> Old B (dd0312e): pair, send, inbox, receive, receipt sync.
(b) Old A (dd0312e) <-> New B (current): bidirectional pairing with identical 24-hex
    pairFingerprint, Old A -> New B -> Old A via v2 adapter, plus strict no-downgrade
    for New B -> New C (v3) on storage failure.
(c) Old A (dd0312e) <-> New B <-> New C (alias='c') on the same cloud root.
(d) Old A (dd0312e) rejects --device c / --to c; New C -> Old A gets v2 upgrade guidance.
(e) Live upgrade migration of genuine dd0312e state (prepared pendingSend, unknown
    pendingSend -> PendingVerificationError, appliedReceives recovery without workspace
    overwrite, and byte-for-byte preservation of published relays/<id>/ files).
"""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from relaydesk import ledger as L  # noqa: E402
from relaydesk import relay_engine  # noqa: E402
from relaydesk import storage  # noqa: E402

FROZEN_COMMIT = "dd0312ef84476901acb6550daf698811b756726b"
PAIR_FP_RE = re.compile(r"^[0-9A-F]{4}(?:-[0-9A-F]{4}){5}$")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.name=CompatTester",
            "-c",
            "user.email=compat@relay.invalid",
            "-c",
            "core.quotepath=false",
            "-C",
            str(repo),
        ]
        + list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError("git %r failed (%d): %s" % (args, proc.returncode, proc.stderr))
    return proc.stdout


def _init_git_repo(repo: Path, branch: str = "main") -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", branch)
    return repo


def _commit_file(repo: Path, rel_path: str, content: str, msg: str) -> str:
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", rel_path)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot_tree_bytes(root: Path) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    if not root.exists():
        return out
    for dirpath, _dirnames, filenames in os.walk(str(root)):
        for fname in sorted(filenames):
            fp = Path(dirpath) / fname
            rel = fp.relative_to(root).as_posix()
            out[rel] = fp.read_bytes()
    return out


class TestCompatV2V3(unittest.TestCase):
    _old_repo_td: Optional[tempfile.TemporaryDirectory] = None
    old_repo_dir: Path = REPO_ROOT

    @classmethod
    def setUpClass(cls) -> None:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", FROZEN_COMMIT],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "git archive %s failed (%d): %s"
                % (
                    FROZEN_COMMIT,
                    proc.returncode,
                    proc.stderr.decode("utf-8", errors="replace"),
                )
            )
        cls._old_repo_td = tempfile.TemporaryDirectory()
        cls.old_repo_dir = Path(cls._old_repo_td.name)
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tf:
            tf.extractall(path=str(cls.old_repo_dir))
        if not (cls.old_repo_dir / "relay.py").is_file():
            raise AssertionError("Extracted dd0312e archive missing relay.py")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._old_repo_td is not None:
            cls._old_repo_td.cleanup()
            cls._old_repo_td = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cloud_dir = self.root / "cloud"
        self.cloud_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _build_env(
        home_dir: Path,
        ledger_path: Path,
        user_home: Path,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        env = dict(os.environ)
        env.pop("RELAY_DESK_DEVICE", None)
        env.pop("PYTHONPATH", None)
        env["RELAY_DESK_HOME"] = str(home_dir)
        env["RELAY_DESK_LEDGER"] = str(ledger_path)
        env["RELAY_DESK_USER_HOME"] = str(user_home)
        env["PYTHONIOENCODING"] = "utf-8"
        if extra_env:
            env.update(extra_env)
        return env

    def _run_cli(
        self,
        code_dir: Path,
        home_dir: Path,
        ledger_path: Path,
        user_home: Path,
        *args: str,
        cwd: Optional[Path] = None,
        check: bool = True,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        home_dir.mkdir(parents=True, exist_ok=True)
        user_home.mkdir(parents=True, exist_ok=True)
        env = self._build_env(home_dir, ledger_path, user_home, extra_env=extra_env)
        cmd = [sys.executable, str(Path(code_dir) / "relay.py")] + list(args)
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                "CLI failed (rc=%d): %r\nSTDOUT:\n%s\nSTDERR:\n%s"
                % (proc.returncode, cmd, proc.stdout, proc.stderr)
            )
        return proc

    def _run_old_py(
        self,
        home_dir: Path,
        ledger_path: Path,
        user_home: Path,
        script: str,
        cwd: Optional[Path] = None,
    ) -> subprocess.CompletedProcess:
        env = self._build_env(
            home_dir,
            ledger_path,
            user_home,
            extra_env={"PYTHONPATH": str(self.old_repo_dir)},
        )
        full_script = "import sys\nsys.path.insert(0, %r)\n%s" % (str(self.old_repo_dir), script)
        proc = subprocess.run(
            [sys.executable, "-c", full_script],
            cwd=str(cwd or self.old_repo_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(
                "Old python helper failed (rc=%d)\nSTDOUT:\n%s\nSTDERR:\n%s"
                % (proc.returncode, proc.stdout, proc.stderr)
            )
        return proc

    @staticmethod
    def _init_new_device(
        home_dir: Path,
        ledger_path: Path,
        user_home: Path,
        alias: str,
        name: str = "",
        os_name: str = "mac",
        extra_devices: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        home_dir.mkdir(parents=True, exist_ok=True)
        user_home.mkdir(parents=True, exist_ok=True)
        local_cfg = {"device": alias, "ledger": str(ledger_path)}
        (home_dir / "config.json").write_text(
            json.dumps(local_cfg, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        def _upd(led: Dict[str, Any]) -> None:
            devs = list(led.get("config", {}).get("devices") or [])
            if alias not in ("a", "b") and not any(d.get("id") == alias for d in devs):
                devs.append(
                    {
                        "id": alias,
                        "name": name or ("设备 %s" % alias.upper()),
                        "os": os_name,
                        "drop": "",
                        "host": "",
                        "deviceId": "",
                    }
                )
            for ed in extra_devices or []:
                if not any(d.get("id") == ed.get("id") for d in devs):
                    devs.append(dict(ed))
            led.setdefault("config", {})["devices"] = devs
            if name:
                for d in led["config"]["devices"]:
                    if d.get("id") == alias:
                        d["name"] = name
                        d["os"] = os_name

        L.update(ledger_path, _upd)
        return local_cfg

    @staticmethod
    def _register_project_new(
        ledger_path: Path,
        alias: str,
        proj_id: str,
        proj_name: str,
        proj_path: Path,
        device_id: str = "",
    ) -> None:
        def _upd(led: Dict[str, Any]) -> None:
            p = L.norm_project(
                {
                    "id": proj_id,
                    "name": proj_name,
                    "device": alias,
                    "tool": "claude",
                    "paths": {alias: str(proj_path)},
                },
                led["config"],
            )
            if device_id:
                p.setdefault("pathsByDeviceId", {})[device_id] = str(proj_path)
            led["projects"].append(p)

        L.update(ledger_path, _upd)

    def _make_device_dirs(self, label: str) -> Dict[str, Path]:
        d = self.root / label
        home = d / "home"
        uhome = d / "uhome"
        led = d / "ledger.json"
        repo = d / "repo"
        home.mkdir(parents=True, exist_ok=True)
        uhome.mkdir(parents=True, exist_ok=True)
        return {"dir": d, "home": home, "uhome": uhome, "ledger": led, "repo": repo}

    # ---------------------------------------------------------------- (a) Old A <-> Old B
    def test_a_old_to_old_full_roundtrip(self) -> None:
        a = self._make_device_dirs("old_a")
        b = self._make_device_dirs("old_b")

        self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "init", "--device", "a")
        self._run_cli(self.old_repo_dir, b["home"], b["ledger"], b["uhome"], "init", "--device", "b")

        pa1 = json.loads(
            self._run_cli(
                self.old_repo_dir,
                a["home"],
                a["ledger"],
                a["uhome"],
                "pair",
                "--provider",
                "local",
                "--root",
                str(self.cloud_dir),
                "--json",
            ).stdout
        )
        pb1 = json.loads(
            self._run_cli(
                self.old_repo_dir,
                b["home"],
                b["ledger"],
                b["uhome"],
                "pair",
                "--provider",
                "local",
                "--root",
                str(self.cloud_dir),
                "--json",
            ).stdout
        )
        self.assertRegex(pb1["pairFingerprint"], PAIR_FP_RE)
        pa2 = json.loads(
            self._run_cli(
                self.old_repo_dir,
                a["home"],
                a["ledger"],
                a["uhome"],
                "pair",
                "--accept",
                "--json",
            ).stdout
        )
        pb2 = json.loads(
            self._run_cli(
                self.old_repo_dir,
                b["home"],
                b["ledger"],
                b["uhome"],
                "pair",
                "--accept",
                "--json",
            ).stdout
        )
        self.assertTrue(pa2["paired"])
        self.assertTrue(pb2["paired"])
        self.assertEqual(pa2["pairFingerprint"], pb2["pairFingerprint"])

        _init_git_repo(a["repo"])
        c1 = _commit_file(a["repo"], "hello.txt", "from old a\n", "init old a")
        self._run_cli(
            self.old_repo_dir,
            a["home"],
            a["ledger"],
            a["uhome"],
            "add",
            "CompatProj",
            "--path",
            str(a["repo"]),
            "--id",
            "p_compat",
        )
        send_res = json.loads(
            self._run_cli(
                self.old_repo_dir,
                a["home"],
                a["ledger"],
                a["uhome"],
                "send",
                "p_compat",
                "--to",
                "b",
                "--json",
            ).stdout
        )
        rid = send_res["relayId"]
        self.assertTrue((self.cloud_dir / "relays" / rid / "ready.json").is_file())
        self.assertTrue((self.cloud_dir / "relays" / rid / "package.enc").is_file())

        inbox_b = json.loads(
            self._run_cli(
                self.old_repo_dir,
                b["home"],
                b["ledger"],
                b["uhome"],
                "inbox",
                "--json",
            ).stdout
        )
        self.assertEqual([item["relayId"] for item in inbox_b["incoming"]], [rid])

        rec_b = json.loads(
            self._run_cli(
                self.old_repo_dir,
                b["home"],
                b["ledger"],
                b["uhome"],
                "receive",
                rid,
                "--path",
                str(b["repo"]),
                "--json",
            ).stdout
        )
        self.assertEqual(rec_b["headCommit"], c1)
        self.assertEqual((b["repo"] / "hello.txt").read_text(encoding="utf-8"), "from old a\n")
        self.assertTrue((self.cloud_dir / "relays" / rid / "receipt.json").is_file())

        inbox_a = json.loads(
            self._run_cli(
                self.old_repo_dir,
                a["home"],
                a["ledger"],
                a["uhome"],
                "inbox",
                "--json",
            ).stdout
        )
        self.assertEqual([r["relayId"] for r in inbox_a["syncedReceipts"]], [rid])

    # ---------------------------------------------------------------- (b) Old A <-> New B
    def test_b_old_a_new_b_bidirectional_and_no_v3_downgrade(self) -> None:
        a = self._make_device_dirs("old_a")
        b = self._make_device_dirs("new_b")
        c = self._make_device_dirs("new_c")

        self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "init", "--device", "a")
        local_b = self._init_new_device(b["home"], b["ledger"], b["uhome"], "b", name="New Mac B")

        # Old A publishes devices/a.json
        self._run_cli(
            self.old_repo_dir,
            a["home"],
            a["ledger"],
            a["uhome"],
            "pair",
            "--provider",
            "local",
            "--root",
            str(self.cloud_dir),
            "--json",
        )
        # New B publishes v3/devices/<id_b>.json + devices/b.json and discovers Old A
        st_b1 = relay_engine.pair_device(
            provider_type="local",
            root=str(self.cloud_dir),
            accept=False,
            ledger_path=b["ledger"],
            local=local_b,
            local_home=b["home"],
        )
        # Old A refreshes to discover New B's devices/b.json
        st_a1 = json.loads(
            self._run_cli(
                self.old_repo_dir,
                a["home"],
                a["ledger"],
                a["uhome"],
                "pair",
                "--json",
            ).stdout
        )
        self.assertRegex(st_a1["pairFingerprint"], PAIR_FP_RE)
        self.assertEqual(st_a1["pairFingerprint"], st_b1["pairFingerprint"])
        self.assertEqual(st_b1["peer"]["capabilities"], ["v2"])
        self.assertEqual(st_b1["peer"]["protocolVersion"], 2)

        # Both accept
        st_a2 = json.loads(
            self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "pair", "--accept", "--json").stdout
        )
        st_b2 = relay_engine.pair_device(
            accept=True,
            ledger_path=b["ledger"],
            local=local_b,
            local_home=b["home"],
        )
        self.assertTrue(st_a2["paired"])
        self.assertTrue(st_b2["paired"])
        self.assertEqual(st_a2["pairFingerprint"], st_b2["pairFingerprint"])

        # Old A sends -> New B receives
        _init_git_repo(a["repo"])
        c1 = _commit_file(a["repo"], "step1.txt", "from old A\n", "step 1")
        self._run_cli(
            self.old_repo_dir,
            a["home"],
            a["ledger"],
            a["uhome"],
            "add",
            "BiProj",
            "--path",
            str(a["repo"]),
            "--id",
            "p_bi",
        )
        r1 = json.loads(
            self._run_cli(
                self.old_repo_dir,
                a["home"],
                a["ledger"],
                a["uhome"],
                "send",
                "p_bi",
                "--to",
                "b",
                "--json",
            ).stdout
        )["relayId"]

        with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": str(b["uhome"])}):
            inbox_b = relay_engine.list_inbox(ledger_path=b["ledger"], local=local_b, local_home=b["home"])
            self.assertEqual([x["relayId"] for x in inbox_b["incoming"]], [r1])
            self.assertEqual(inbox_b["incoming"][0]["protocolVersion"], 2)

            rec_b = relay_engine.receive_relay(
                r1,
                target_path=str(b["repo"]),
                ledger_path=b["ledger"],
                local=local_b,
                local_home=b["home"],
            )
            self.assertEqual(rec_b["headCommit"], c1)
            self.assertEqual((b["repo"] / "step1.txt").read_text(encoding="utf-8"), "from old A\n")

        # Old A syncs receipt
        inbox_a = json.loads(
            self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "inbox", "--json").stdout
        )
        self.assertEqual([r["relayId"] for r in inbox_a["syncedReceipts"]], [r1])

        # New B commits and sends back to Old A using v2 adapter
        c2 = _commit_file(b["repo"], "step2.txt", "from new B\n", "step 2")
        with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": str(b["uhome"])}):
            send_b = relay_engine.send_project(
                project_key="p_bi",
                to_alias="a",
                ledger_path=b["ledger"],
                local=local_b,
                local_home=b["home"],
            )
        r2 = send_b["relayId"]
        self.assertEqual(send_b["protocolVersion"], 2)
        self.assertTrue((self.cloud_dir / "relays" / r2 / "ready.json").is_file())
        self.assertFalse((self.cloud_dir / "v3" / "inbox" / st_a2["deviceId"] / r2).exists())

        # Old A lists inbox and receives r2
        inbox_a2 = json.loads(
            self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "inbox", "--json").stdout
        )
        self.assertEqual([x["relayId"] for x in inbox_a2["incoming"]], [r2])
        rec_a2 = json.loads(
            self._run_cli(
                self.old_repo_dir,
                a["home"],
                a["ledger"],
                a["uhome"],
                "receive",
                r2,
                "--json",
            ).stdout
        )
        self.assertEqual(rec_a2["headCommit"], c2)
        self.assertEqual((a["repo"] / "step2.txt").read_text(encoding="utf-8"), "from new B\n")

        # New B syncs r2 receipt
        with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": str(b["uhome"])}):
            inbox_b2 = relay_engine.list_inbox(ledger_path=b["ledger"], local=local_b, local_home=b["home"])
        self.assertEqual([r["relayId"] for r in inbox_b2["syncedReceipts"]], [r2])

        # Also verify: New B -> New C (v3 peer) NEVER downgrades to relays/ on storage error
        local_c = self._init_new_device(c["home"], c["ledger"], c["uhome"], "c", name="New Linux C")
        relay_engine.pair_device(
            provider_type="local",
            root=str(self.cloud_dir),
            accept=False,
            ledger_path=c["ledger"],
            local=local_c,
            local_home=c["home"],
        )
        st_b_disc = relay_engine.pair_device(
            accept=False,
            ledger_path=b["ledger"],
            local=local_b,
            local_home=b["home"],
        )
        c_did = next(p["deviceId"] for p in st_b_disc["peers"] if p.get("alias") == "c")
        relay_engine.pair_device(
            accept=True,
            with_device=c_did,
            ledger_path=b["ledger"],
            local=local_b,
            local_home=b["home"],
        )

        # Wait until New B owns another project or wait for New B to own p_bi again
        _commit_file(a["repo"], "step3.txt", "back to B\n", "step 3")
        r3 = json.loads(
            self._run_cli(
                self.old_repo_dir,
                a["home"],
                a["ledger"],
                a["uhome"],
                "send",
                "p_bi",
                "--to",
                "b",
                "--json",
            ).stdout
        )["relayId"]
        with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": str(b["uhome"])}):
            relay_engine.receive_relay(r3, ledger_path=b["ledger"], local=local_b, local_home=b["home"])

        relays_before = set(p.name for p in (self.cloud_dir / "relays").iterdir() if p.is_dir())
        orig_upload = storage.LocalDirProvider.upload

        def _fail_v3_upload(self_prov, local_path, remote_key):
            if str(remote_key).startswith("v3/inbox/"):
                raise storage.StorageError("simulated v3 upload failure")
            return orig_upload(self_prov, local_path, remote_key)

        with mock.patch.object(storage.LocalDirProvider, "upload", _fail_v3_upload):
            with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": str(b["uhome"])}):
                with self.assertRaises(storage.StorageError):
                    relay_engine.send_project(
                        project_key="p_bi",
                        to_alias="c",
                        ledger_path=b["ledger"],
                        local=local_b,
                        local_home=b["home"],
                    )

        relays_after = set(p.name for p in (self.cloud_dir / "relays").iterdir() if p.is_dir())
        self.assertEqual(relays_before, relays_after)

    # ---------------------------------------------------------------- (c) Old A <-> New B <-> New C
    def test_c_three_device_bridge_old_a_new_b_new_c(self) -> None:
        a = self._make_device_dirs("old_a")
        b = self._make_device_dirs("new_b")
        c = self._make_device_dirs("new_c")

        self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "init", "--device", "a")
        local_b = self._init_new_device(b["home"], b["ledger"], b["uhome"], "b", name="Bridge Mac B")
        local_c = self._init_new_device(c["home"], c["ledger"], c["uhome"], "c", name="Workstation C")

        # Publish descriptors on same cloud root
        self._run_cli(
            self.old_repo_dir,
            a["home"],
            a["ledger"],
            a["uhome"],
            "pair",
            "--provider",
            "local",
            "--root",
            str(self.cloud_dir),
            "--json",
        )
        st_b_init = relay_engine.pair_device(
            provider_type="local",
            root=str(self.cloud_dir),
            accept=False,
            ledger_path=b["ledger"],
            local=local_b,
            local_home=b["home"],
        )
        st_c_init = relay_engine.pair_device(
            provider_type="local",
            root=str(self.cloud_dir),
            accept=False,
            ledger_path=c["ledger"],
            local=local_c,
            local_home=c["home"],
        )
        # Old A pairs with B (Old A ignores v3/devices/*)
        st_a1 = json.loads(
            self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "pair", "--json").stdout
        )
        self.assertEqual(st_a1["peer"]["deviceId"], st_b_init["deviceId"])
        self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "pair", "--accept", "--json")

        # New B discovers & accepts both A and C
        st_b_disc = relay_engine.pair_device(
            accept=False,
            ledger_path=b["ledger"],
            local=local_b,
            local_home=b["home"],
        )
        a_did = st_a1["deviceId"]
        c_did = st_c_init["deviceId"]
        self.assertIn(a_did, {p["deviceId"] for p in st_b_disc["peers"]})
        self.assertIn(c_did, {p["deviceId"] for p in st_b_disc["peers"]})
        relay_engine.pair_device(accept=True, with_device=a_did, ledger_path=b["ledger"], local=local_b, local_home=b["home"])
        relay_engine.pair_device(accept=True, with_device=c_did, ledger_path=b["ledger"], local=local_b, local_home=b["home"])

        # New C discovers & accepts B
        relay_engine.pair_device(accept=False, ledger_path=c["ledger"], local=local_c, local_home=c["home"])
        relay_engine.pair_device(
            accept=True,
            with_device=st_b_init["deviceId"],
            ledger_path=c["ledger"],
            local=local_c,
            local_home=c["home"],
        )

        # 1. Old A -> New B (v2)
        _init_git_repo(a["repo"])
        c_a = _commit_file(a["repo"], "chain.txt", "v1 from A\n", "commit from A")
        self._run_cli(
            self.old_repo_dir,
            a["home"],
            a["ledger"],
            a["uhome"],
            "add",
            "BridgeProj",
            "--path",
            str(a["repo"]),
            "--id",
            "p_bridge",
        )
        r_ab = json.loads(
            self._run_cli(
                self.old_repo_dir,
                a["home"],
                a["ledger"],
                a["uhome"],
                "send",
                "p_bridge",
                "--to",
                "b",
                "--json",
            ).stdout
        )["relayId"]

        with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": str(b["uhome"])}):
            rec_b = relay_engine.receive_relay(
                r_ab, target_path=str(b["repo"]), ledger_path=b["ledger"], local=local_b, local_home=b["home"]
            )
            self.assertEqual(rec_b["headCommit"], c_a)

            # 2. New B -> New C (v3)
            send_bc = relay_engine.send_project(
                project_key="p_bridge", to_alias="c", ledger_path=b["ledger"], local=local_b, local_home=b["home"]
            )
        self.assertEqual(send_bc["protocolVersion"], 3)
        r_bc = send_bc["relayId"]
        self.assertTrue((self.cloud_dir / "v3" / "inbox" / c_did / r_bc / "ready.json").is_file())

        # 3. New C receives, commits, and sends -> New B (v3)
        with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": str(c["uhome"])}):
            inbox_c = relay_engine.list_inbox(ledger_path=c["ledger"], local=local_c, local_home=c["home"])
            self.assertEqual([x["relayId"] for x in inbox_c["incoming"]], [r_bc])
            relay_engine.receive_relay(
                r_bc, target_path=str(c["repo"]), ledger_path=c["ledger"], local=local_c, local_home=c["home"]
            )
            c_c = _commit_file(c["repo"], "from_c.txt", "added on device C\n", "commit from C")
            send_cb = relay_engine.send_project(
                project_key="p_bridge", to_alias="b", ledger_path=c["ledger"], local=local_c, local_home=c["home"]
            )
        r_cb = send_cb["relayId"]
        self.assertEqual(send_cb["protocolVersion"], 3)

        # 4. New B receives from C, then sends -> Old A (v2)
        with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": str(b["uhome"])}):
            inbox_b = relay_engine.list_inbox(ledger_path=b["ledger"], local=local_b, local_home=b["home"])
            self.assertEqual([x["relayId"] for x in inbox_b["incoming"]], [r_cb])
            rec_b2 = relay_engine.receive_relay(
                r_cb, ledger_path=b["ledger"], local=local_b, local_home=b["home"]
            )
            self.assertEqual(rec_b2["headCommit"], c_c)
            send_ba = relay_engine.send_project(
                project_key="p_bridge", to_alias="a", ledger_path=b["ledger"], local=local_b, local_home=b["home"]
            )
        r_ba = send_ba["relayId"]
        self.assertEqual(send_ba["protocolVersion"], 2)

        # 5. Old A receives New C's commit!
        inbox_a = json.loads(
            self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "inbox", "--json").stdout
        )
        self.assertIn(r_ba, [x["relayId"] for x in inbox_a["incoming"]])
        rec_a = json.loads(
            self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "receive", r_ba, "--json").stdout
        )
        self.assertEqual(rec_a["headCommit"], c_c)
        self.assertEqual((a["repo"] / "from_c.txt").read_text(encoding="utf-8"), "added on device C\n")

    # ---------------------------------------------------------------- (d) Old A & dynamic slot c
    def test_d_old_a_rejects_dynamic_c_and_new_c_rejects_v2_only_handoff(self) -> None:
        a = self._make_device_dirs("old_a")
        c = self._make_device_dirs("new_c")

        # Old dd0312e CLI rejects --device c and --to c
        proc_init = self._run_cli(
            self.old_repo_dir,
            a["home"],
            a["ledger"],
            a["uhome"],
            "init",
            "--device",
            "c",
            check=False,
        )
        self.assertNotEqual(proc_init.returncode, 0)
        self.assertIn("c", proc_init.stderr)

        proc_send = self._run_cli(
            self.old_repo_dir,
            a["home"],
            a["ledger"],
            a["uhome"],
            "send",
            "p_any",
            "--to",
            "c",
            check=False,
        )
        self.assertNotEqual(proc_send.returncode, 0)
        self.assertIn("c", proc_send.stderr)

        # New C paired with Old A (v2-only) receives clear upgrade guidance on send_project
        self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "init", "--device", "a")
        self._run_cli(
            self.old_repo_dir,
            a["home"],
            a["ledger"],
            a["uhome"],
            "pair",
            "--provider",
            "local",
            "--root",
            str(self.cloud_dir),
            "--json",
        )

        local_c = self._init_new_device(c["home"], c["ledger"], c["uhome"], "c", name="Dynamic C")
        relay_engine.pair_device(
            provider_type="local",
            root=str(self.cloud_dir),
            accept=False,
            ledger_path=c["ledger"],
            local=local_c,
            local_home=c["home"],
        )
        relay_engine.pair_device(
            accept=True,
            with_device="a",
            ledger_path=c["ledger"],
            local=local_c,
            local_home=c["home"],
        )
        _init_git_repo(c["repo"])
        _commit_file(c["repo"], "c.txt", "c work\n", "c commit")
        self._register_project_new(c["ledger"], "c", "p_dyn", "DynProj", c["repo"])

        with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": str(c["uhome"])}):
            with self.assertRaises(relay_engine.RelayError) as ctx:
                relay_engine.send_project(
                    project_key="p_dyn",
                    to_alias="a",
                    ledger_path=c["ledger"],
                    local=local_c,
                    local_home=c["home"],
                )
        self.assertEqual(ctx.exception.code, "v2_incompatible_alias")
        self.assertIn("v2", str(ctx.exception))
        self.assertIn("v3", str(ctx.exception))

    # ---------------------------------------------------------------- (e) Live upgrade migration of dd0312e state
    def test_e_live_upgrade_migration_of_old_state(self) -> None:
        a = self._make_device_dirs("mig_a")
        b = self._make_device_dirs("mig_b")

        # 1. Pair Old A and Old B via dd0312e subprocesses
        self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "init", "--device", "a")
        self._run_cli(self.old_repo_dir, b["home"], b["ledger"], b["uhome"], "init", "--device", "b")
        self._run_cli(
            self.old_repo_dir,
            a["home"],
            a["ledger"],
            a["uhome"],
            "pair",
            "--provider",
            "local",
            "--root",
            str(self.cloud_dir),
            "--json",
        )
        self._run_cli(
            self.old_repo_dir,
            b["home"],
            b["ledger"],
            b["uhome"],
            "pair",
            "--provider",
            "local",
            "--root",
            str(self.cloud_dir),
            "--json",
        )
        self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "pair", "--accept", "--json")
        self._run_cli(self.old_repo_dir, b["home"], b["ledger"], b["uhome"], "pair", "--accept", "--json")

        # Create a fully completed & published v2 handoff (r_pub) to verify byte-for-byte preservation
        _init_git_repo(a["repo"])
        _commit_file(a["repo"], "base.txt", "base v1\n", "base commit")
        self._run_cli(
            self.old_repo_dir,
            a["home"],
            a["ledger"],
            a["uhome"],
            "add",
            "MigProj",
            "--path",
            str(a["repo"]),
            "--id",
            "p_mig",
        )
        r_pub = json.loads(
            self._run_cli(
                self.old_repo_dir,
                a["home"],
                a["ledger"],
                a["uhome"],
                "send",
                "p_mig",
                "--to",
                "b",
                "--json",
            ).stdout
        )["relayId"]
        self._run_cli(
            self.old_repo_dir,
            b["home"],
            b["ledger"],
            b["uhome"],
            "receive",
            r_pub,
            "--path",
            str(b["repo"]),
            "--json",
        )
        self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "inbox", "--json")

        pub_cloud_snapshot = _snapshot_tree_bytes(self.cloud_dir / "relays" / r_pub)
        self.assertIn("package.enc", pub_cloud_snapshot)
        self.assertIn("ready.json", pub_cloud_snapshot)
        self.assertIn("receipt.json", pub_cloud_snapshot)

        # (iii) Create genuine dd0312e appliedReceives state on B (crash before _finish_receive)
        _commit_file(b["repo"], "b_step.txt", "from B\n", "b step")
        r_ba = json.loads(
            self._run_cli(
                self.old_repo_dir,
                b["home"],
                b["ledger"],
                b["uhome"],
                "send",
                "p_mig",
                "--to",
                "a",
                "--json",
            ).stdout
        )["relayId"]
        self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "receive", r_ba, "--json")
        self._run_cli(self.old_repo_dir, b["home"], b["ledger"], b["uhome"], "inbox", "--json")

        _commit_file(a["repo"], "a_step.txt", "from A for appliedReceives\n", "a step")
        r_app = json.loads(
            self._run_cli(
                self.old_repo_dir,
                a["home"],
                a["ledger"],
                a["uhome"],
                "send",
                "p_mig",
                "--to",
                "b",
                "--json",
            ).stdout
        )["relayId"]

        crash_rx_script = (
            "from relaydesk import relay_engine\n"
            "def boom(*args, **kwargs):\n"
            "    raise RuntimeError('simulated crash after appliedReceives')\n"
            "relay_engine._finish_receive = boom\n"
            "try:\n"
            "    relay_engine.receive_relay(%r)\n"
            "except RuntimeError:\n"
            "    pass\n"
            % (r_app,)
        )
        self._run_old_py(b["home"], b["ledger"], b["uhome"], crash_rx_script)
        raw_state_b_v2 = json.loads((b["home"] / "relay_state.json").read_text(encoding="utf-8"))
        self.assertIn(r_app, raw_state_b_v2.get("appliedReceives") or {})
        self.assertNotIn("version", raw_state_b_v2)

        # Simulate user making a local edit in b["repo"] after workspace apply but before finish
        (b["repo"] / "user_local_edit.txt").write_text("keep my post-apply edit!\n", encoding="utf-8")

        # Upgrade B to NEW client and resume r_app: must finish archive/ledger/receipt without touching user_local_edit.txt!
        local_b = {"device": "b", "ledger": str(b["ledger"])}
        with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": str(b["uhome"])}):
            rec_new_b = relay_engine.receive_relay(
                r_app, ledger_path=b["ledger"], local=local_b, local_home=b["home"]
            )
        self.assertEqual(rec_new_b["relayId"], r_app)
        self.assertEqual(
            (b["repo"] / "user_local_edit.txt").read_text(encoding="utf-8"),
            "keep my post-apply edit!\n",
        )
        self.assertTrue((self.cloud_dir / "relays" / r_app / "receipt.json").is_file())
        self.assertTrue((b["home"] / "cloud.v2.backup.json").is_file())
        self.assertTrue((b["home"] / "relay_state.v2.backup.json").is_file())

        # Old A syncs r_app receipt
        inbox_a_sync = json.loads(
            self._run_cli(self.old_repo_dir, a["home"], a["ledger"], a["uhome"], "inbox", "--json").stdout
        )
        self.assertIn(r_app, [x["relayId"] for x in inbox_a_sync["syncedReceipts"]])

        # (i) Genuine dd0312e `prepared` pendingSend on Old A -> New A resumes on original relayId
        _commit_file(a["repo"], "prepared_send.txt", "pending prepared\n", "prepared commit")
        # Wait: make sure Old A receives r_app? No, Old A already synced receipt so Old A's project is now on b;
        # send from B back to A using New B first, or create a second project on Old A for clean prepared test!
        repo_p2_a = self.root / "p2_a"
        _init_git_repo(repo_p2_a)
        _commit_file(repo_p2_a, "p2.txt", "proj2 v1\n", "p2 init")
        self._run_cli(
            self.old_repo_dir,
            a["home"],
            a["ledger"],
            a["uhome"],
            "add",
            "Proj2",
            "--path",
            str(repo_p2_a),
            "--id",
            "p_prep",
        )
        crash_tx_script = (
            "from relaydesk import relay_engine\n"
            "def boom(*args, **kwargs):\n"
            "    raise RuntimeError('simulated crash before ready.json upload')\n"
            "relay_engine._publish_prepared_send = boom\n"
            "try:\n"
            "    relay_engine.send_project(project_key='p_prep', to_alias='b')\n"
            "except RuntimeError:\n"
            "    pass\n"
        )
        self._run_old_py(a["home"], a["ledger"], a["uhome"], crash_tx_script)
        raw_state_a_v2 = json.loads((a["home"] / "relay_state.json").read_text(encoding="utf-8"))
        pend_p2 = (raw_state_a_v2.get("pendingSends") or {}).get("p_prep") or {}
        orig_rid = pend_p2.get("relayId") or ""
        self.assertTrue(orig_rid)
        self.assertIn("prepared", pend_p2)
        self.assertTrue((self.cloud_dir / "relays" / orig_rid / "package.enc").is_file())
        self.assertFalse((self.cloud_dir / "relays" / orig_rid / "ready.json").exists())

        # Upgrade A to NEW client and call send_project -> must publish on ORIGINAL orig_rid!
        local_a = {"device": "a", "ledger": str(a["ledger"])}
        with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": str(a["uhome"])}):
            resumed = relay_engine.send_project(
                project_key="p_prep",
                to_alias="b",
                ledger_path=a["ledger"],
                local=local_a,
                local_home=a["home"],
            )
        self.assertEqual(resumed["relayId"], orig_rid)
        self.assertTrue((self.cloud_dir / "relays" / orig_rid / "ready.json").is_file())
        self.assertTrue((a["home"] / "cloud.v2.backup.json").is_file())
        self.assertTrue((a["home"] / "relay_state.v2.backup.json").is_file())

        # (ii) Unknown-publication pendingSend (v2 state without checkpointBeforeReady / prepared / remote ready.json)
        repo_p3_a = self.root / "p3_a"
        _init_git_repo(repo_p3_a)
        _commit_file(repo_p3_a, "p3.txt", "proj3\n", "p3 init")
        self._register_project_new(a["ledger"], "a", "p_unk", "ProjUnk", repo_p3_a)
        a_cloud = json.loads((a["home"] / "cloud.json").read_text(encoding="utf-8"))
        peers_dict = a_cloud.get("peers") or {}
        if peers_dict:
            b_did = next(iter(peers_dict.keys()))
        else:
            b_did = a_cloud["peer"]["deviceId"]

        # Write a v2-style relay_state.json (version omitted / 2) with unknown pendingSend
        cur_state_a = json.loads((a["home"] / "relay_state.json").read_text(encoding="utf-8"))
        cur_state_a["version"] = 2
        cur_state_a.setdefault("pendingSends", {})["p_unk"] = {
            "relayId": "r_20261118_unk00001",
            "toAlias": "b",
            "toDeviceId": b_did,
        }
        (a["home"] / "relay_state.json").write_text(
            json.dumps(cur_state_a, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": str(a["uhome"])}):
            with self.assertRaises(relay_engine.PendingVerificationError) as pver_ctx:
                relay_engine.send_project(
                    project_key="p_unk",
                    to_alias="b",
                    ledger_path=a["ledger"],
                    local=local_a,
                    local_home=a["home"],
                )
        self.assertEqual(pver_ctx.exception.relay_id, "r_20261118_unk00001")
        st_after_unk = json.loads((a["home"] / "relay_state.json").read_text(encoding="utf-8"))
        unk_ent = st_after_unk["pendingSends"]["p_unk"]
        self.assertEqual(unk_ent["relayId"], "r_20261118_unk00001")
        rec_unk_status = relay_engine.recover_relay(
            "r_20261118_unk00001",
            action="status",
            ledger_path=a["ledger"],
            local=local_a,
            local_home=a["home"],
        )
        self.assertEqual(rec_unk_status["state"], "needs_verification")
        self.assertEqual(rec_unk_status["phase"], "NEEDS_VERIFICATION")
        self.assertFalse(rec_unk_status["canResume"])
        self.assertTrue(rec_unk_status["canRollback"])

        # (iv) Verify already published relays/<r_pub>/ files remain byte-for-byte untouched
        pub_cloud_after = _snapshot_tree_bytes(self.cloud_dir / "relays" / r_pub)
        self.assertEqual(set(pub_cloud_snapshot.keys()), set(pub_cloud_after.keys()))
        for rel_k, raw_before in pub_cloud_snapshot.items():
            self.assertEqual(
                _sha256_bytes(pub_cloud_after[rel_k]),
                _sha256_bytes(raw_before),
                "Published cloud artifact %s/%s was modified!" % (r_pub, rel_k),
            )


if __name__ == "__main__":
    unittest.main()
