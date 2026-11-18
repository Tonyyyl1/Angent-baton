"""Unit and integration tests for ``relaydesk.storage``."""
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from relaydesk import storage as S
from tests.storage_contract import StorageProviderContractMixin


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_tcp_ready(port: int, proc: subprocess.Popen, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            err = ""
            if proc.stderr is not None:
                err = proc.stderr.read().decode("utf-8", errors="replace")
            raise RuntimeError("rclone serve 提前退出（code=%s）：%s" % (proc.returncode, err))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError("等待本地 rclone 测试服务端口 %d 超时" % port)


def _stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


class TestLocalDirProviderContract(StorageProviderContractMixin, unittest.TestCase):
    """Contract suite against ``LocalDirProvider`` (with initially non-existent root)."""

    def make_contract_provider(self) -> S.StorageProvider:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return S.LocalDirProvider(str(Path(td.name) / "uncreated_root"))


@unittest.skipUnless(S.has_rclone(), "rclone 未安装，跳过 RcloneProvider 集成测试")
class TestRcloneLocalBackendContract(StorageProviderContractMixin, unittest.TestCase):
    """Contract suite against ``RcloneProvider`` using an ``rclone.conf`` local remote."""

    def make_contract_provider(self) -> S.StorageProvider:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        conf = base / "rclone.conf"
        conf.write_text("[localdisk]\ntype = local\n", encoding="utf-8")
        store_root = base / "cloud_root"
        return S.RcloneProvider(
            remote="localdisk:",
            root=str(store_root),
            config_path=str(conf),
        )


@unittest.skipUnless(S.has_rclone(), "rclone 未安装，跳过 WebDAV 集成测试")
class TestRcloneWebDavContract(StorageProviderContractMixin, unittest.TestCase):
    """Contract suite against ``RcloneProvider`` backed by a live ``rclone serve webdav`` server."""

    _tmp_dir = None  # type: tempfile.TemporaryDirectory
    _proc = None  # type: subprocess.Popen
    _conf_path = ""
    _case_seq = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        rclone_exe = S.find_rclone_binary()
        assert rclone_exe is not None
        cls._tmp_dir = tempfile.TemporaryDirectory()
        base = Path(cls._tmp_dir.name)
        serve_dir = base / "webdav_data"
        serve_dir.mkdir(parents=True, exist_ok=True)
        port = _free_port()

        cls._proc = subprocess.Popen(
            [rclone_exe, "serve", "webdav", str(serve_dir), "--addr", "127.0.0.1:%d" % port],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_tcp_ready(port, cls._proc)
        except BaseException:
            _stop_proc(cls._proc)
            cls._tmp_dir.cleanup()
            raise

        conf = base / "rclone_dav.conf"
        conf.write_text(
            "[davremote]\n"
            "type = webdav\n"
            "url = http://127.0.0.1:%d\n"
            "vendor = other\n" % port,
            encoding="utf-8",
        )
        cls._conf_path = str(conf)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._proc is not None:
            _stop_proc(cls._proc)
        if cls._tmp_dir is not None:
            cls._tmp_dir.cleanup()
        super().tearDownClass()

    def make_contract_provider(self) -> S.StorageProvider:
        type(self)._case_seq += 1
        return S.RcloneProvider(
            remote="davremote",
            root="relay_case_%d" % type(self)._case_seq,
            config_path=self._conf_path,
        )


@unittest.skipUnless(S.has_rclone(), "rclone 未安装，跳过 S3 集成测试")
class TestRcloneS3Contract(StorageProviderContractMixin, unittest.TestCase):
    """Contract suite against ``RcloneProvider`` backed by a live ``rclone serve s3`` server."""

    _tmp_dir = None  # type: tempfile.TemporaryDirectory
    _proc = None  # type: subprocess.Popen
    _conf_path = ""
    _case_seq = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        rclone_exe = S.find_rclone_binary()
        assert rclone_exe is not None
        cls._tmp_dir = tempfile.TemporaryDirectory()
        base = Path(cls._tmp_dir.name)
        s3_dir = base / "s3_data"
        (s3_dir / "relaybucket").mkdir(parents=True, exist_ok=True)
        port = _free_port()

        cls._proc = subprocess.Popen(
            [
                rclone_exe,
                "serve",
                "s3",
                str(s3_dir),
                "--addr",
                "127.0.0.1:%d" % port,
                "--auth-key",
                "relaykey,relaysecret",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_tcp_ready(port, cls._proc)
        except BaseException:
            _stop_proc(cls._proc)
            cls._tmp_dir.cleanup()
            raise

        conf = base / "rclone_s3.conf"
        conf.write_text(
            "[s3remote]\n"
            "type = s3\n"
            "provider = Other\n"
            "access_key_id = relaykey\n"
            "secret_access_key = relaysecret\n"
            "endpoint = http://127.0.0.1:%d\n"
            "force_path_style = true\n" % port,
            encoding="utf-8",
        )
        cls._conf_path = str(conf)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._proc is not None:
            _stop_proc(cls._proc)
        if cls._tmp_dir is not None:
            cls._tmp_dir.cleanup()
        super().tearDownClass()

    def make_contract_provider(self) -> S.StorageProvider:
        type(self)._case_seq += 1
        return S.RcloneProvider(
            remote="s3remote:",
            root="relaybucket/case_%d" % type(self)._case_seq,
            config_path=self._conf_path,
        )


class TestStorageHelpersAndErrorHandling(unittest.TestCase):
    """Unit tests for ObjectInfo, validate_key, rclone discovery, make_provider, and errors."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_object_info_dual_access(self) -> None:
        info = S.ObjectInfo(
            key="relays/r1/ready.json",
            size=128,
            sha256="abc123",
            etag="etag-1",
            mtime="2026-11-18T00:00:00Z",
        )
        self.assertEqual(info.key, "relays/r1/ready.json")
        self.assertEqual(info.size, 128)
        self.assertEqual(info.sha256, "abc123")
        self.assertEqual(info.etag, "etag-1")
        self.assertEqual(info.mtime, "2026-11-18T00:00:00Z")

        self.assertEqual(info["key"], "relays/r1/ready.json")
        self.assertEqual(info["size"], 128)
        self.assertEqual(info.get("sha256"), "abc123")
        self.assertIsNone(info.get("nonexistent"))
        self.assertEqual(info.get("nonexistent", 42), 42)
        self.assertIn("key", info)
        self.assertNotIn("other", info)
        self.assertEqual(dict(info), info.to_dict())
        self.assertEqual(len(info), 5)

        with self.assertRaises(KeyError):
            _ = info["unknown_field"]

    def test_validate_key_rules(self) -> None:
        self.assertEqual(S.validate_key("/relays//r_1///ready.json/"), "relays/r_1/ready.json")
        self.assertEqual(S.validate_key("", allow_empty=True), "")
        self.assertEqual(S.validate_key("///", allow_empty=True), "")

        for invalid in ("", "/", ".", "..", "a/..", "../a", "a/./b", "a\\b", "\\a", "a\x00b"):
            with self.assertRaises(S.StorageError):
                S.validate_key(invalid)

    def test_find_rclone_binary_and_has_rclone_overrides(self) -> None:
        missing_bin = str(self.dir / "no_such_rclone_bin")
        self.assertIsNone(S.find_rclone_binary(missing_bin))
        self.assertFalse(S.has_rclone(missing_bin))

        with mock.patch.dict(os.environ, {"RELAY_DESK_RCLONE": missing_bin}, clear=False):
            self.assertIsNone(S.find_rclone_binary())
            self.assertFalse(S.has_rclone())

        # Create an executable stub binary and verify env/arg resolution
        stub = self.dir / "fake_rclone"
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(str(stub), 0o755)
        self.assertEqual(S.find_rclone_binary(str(stub)), str(stub))
        self.assertTrue(S.has_rclone(str(stub)))
        with mock.patch.dict(os.environ, {"RELAY_DESK_RCLONE": str(stub)}, clear=False):
            self.assertEqual(S.find_rclone_binary(), str(stub))

    @unittest.skipUnless(S.has_rclone(), "rclone 未安装")
    def test_discover_rclone_remotes(self) -> None:
        conf = self.dir / "custom_rclone.conf"
        conf.write_text(
            "[onedrive_work]\n"
            "type = local\n\n"
            "[s3relay]\n"
            "type = s3\n"
            "provider = Other\n",
            encoding="utf-8",
        )
        remotes = S.discover_rclone_remotes(config_path=str(conf))
        self.assertEqual(remotes, ["onedrive_work", "s3relay"])

        # Non-existent config file returns empty list cleanly
        empty_remotes = S.discover_rclone_remotes(config_path=str(self.dir / "absent.conf"))
        self.assertEqual(empty_remotes, [])

        # Missing binary raises StorageError
        with self.assertRaises(S.StorageError):
            S.discover_rclone_remotes(rclone_bin=str(self.dir / "missing_bin"))

    def test_make_provider_variants_and_validation(self) -> None:
        local_prov = S.make_provider({"provider": "local", "root": str(self.dir / "loc")})
        self.assertIsInstance(local_prov, S.LocalDirProvider)

        rclone_prov = S.make_provider(
            {
                "provider": "rclone",
                "remote": "mycloud:",
                "root": "/RelayDesk/",
                "configPath": str(self.dir / "rclone.conf"),
            }
        )
        self.assertIsInstance(rclone_prov, S.RcloneProvider)
        assert isinstance(rclone_prov, S.RcloneProvider)
        self.assertEqual(rclone_prov.remote, "mycloud")
        self.assertEqual(rclone_prov.root, "/RelayDesk")

        with self.assertRaises(S.StorageError):
            S.make_provider({"provider": "unknown_cloud", "root": "x"})
        with self.assertRaises(S.StorageError):
            S.make_provider({"provider": "rclone", "remote": ""})
        with self.assertRaises(S.StorageError):
            S.make_provider({"provider": "local", "root": ""})

    @unittest.skipUnless(S.has_rclone(), "rclone 未安装")
    def test_rclone_error_handling_bad_remote_and_unreachable_endpoint(self) -> None:
        closed_port = _free_port()
        conf = self.dir / "err.conf"
        conf.write_text(
            "[dead_webdav]\n"
            "type = webdav\n"
            "url = http://127.0.0.1:%d\n"
            "vendor = other\n" % closed_port,
            encoding="utf-8",
        )

        # Unconfigured remote section MUST raise StorageError (never silently return [] or None!)
        bad_remote = S.RcloneProvider(remote="unconfigured_section", root="box", config_path=str(conf))
        with self.assertRaises(S.StorageError):
            bad_remote.list_objects()
        with self.assertRaises(S.StorageError):
            bad_remote.stat("devices/a.json")
        with self.assertRaises(S.StorageError):
            bad_remote.delete("devices/a.json")

        # Unreachable WebDAV endpoint MUST raise StorageError
        dead_dav = S.RcloneProvider(remote="dead_webdav", root="box", config_path=str(conf), timeout=10.0)
        with self.assertRaises(S.StorageError):
            dead_dav.list_objects()
        with self.assertRaises(S.StorageError):
            dead_dav.stat("devices/a.json")

        # Missing local file on upload MUST raise StorageError
        with self.assertRaises(S.StorageError):
            dead_dav.upload(str(self.dir / "no_such_local_file.bin"), "devices/a.json")

    def test_local_dir_provider_blocks_symlink_escape(self) -> None:
        outside = self.dir / "outside_secret"
        outside.mkdir()
        secret_file = outside / "secret.txt"
        secret_file.write_text("top-secret", encoding="utf-8")

        store_dir = self.dir / "store"
        store_dir.mkdir()
        escape_link = store_dir / "escaped"
        os.symlink(str(outside), str(escape_link))

        prov = S.LocalDirProvider(str(store_dir))
        with self.assertRaises(S.StorageError):
            prov.stat("escaped/secret.txt")
        with self.assertRaises(S.StorageError):
            prov.download("escaped/secret.txt", str(self.dir / "leak.txt"))


if __name__ == "__main__":
    unittest.main()
