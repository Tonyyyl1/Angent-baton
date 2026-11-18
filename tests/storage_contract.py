"""Shared contract test suite for any ``relaydesk.storage.StorageProvider``.

Custom cloud storage adapters can validate full compliance with Relay Desk's
atomicity, error-handling, and UTF-8 key semantics by subclassing
``(StorageProviderContractMixin, unittest.TestCase)`` and implementing
``make_contract_provider(self) -> StorageProvider``.
"""
import tempfile
import unicodedata
from pathlib import Path
from unittest import mock

from relaydesk.storage import (
    ObjectInfo,
    ObjectNotFoundError,
    StorageError,
    StorageProvider,
)


class StorageProviderContractMixin(object):
    """Reusable contract mixin for ``StorageProvider`` implementations."""

    def make_contract_provider(self) -> StorageProvider:
        raise NotImplementedError("Subclasses must implement make_contract_provider()")

    def _get_provider(self) -> StorageProvider:
        prov = getattr(self, "provider", None)
        if prov is not None:
            return prov
        return self.make_contract_provider()

    def _make_work_dir(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)  # type: ignore[attr-defined]
        return Path(td.name)

    def test_contract_empty_store(self) -> None:
        prov = self._get_provider()
        work = self._make_work_dir()

        self.assertEqual(prov.list_objects(), [])  # type: ignore[attr-defined]
        self.assertEqual(prov.list_objects(""), [])  # type: ignore[attr-defined]
        self.assertEqual(prov.list_objects("/"), [])  # type: ignore[attr-defined]
        self.assertEqual(prov.list_objects("relays/"), [])  # type: ignore[attr-defined]
        self.assertEqual(prov.list_objects("nonexistent/deep/prefix"), [])  # type: ignore[attr-defined]

        self.assertIsNone(prov.stat("devices/a.json"))  # type: ignore[attr-defined]
        self.assertFalse(prov.delete("devices/a.json"))  # type: ignore[attr-defined]

        dst = work / "should_not_exist.bin"
        with self.assertRaises(ObjectNotFoundError):  # type: ignore[attr-defined]
            prov.download("devices/a.json", str(dst))
        self.assertFalse(dst.exists())  # type: ignore[attr-defined]

    def test_contract_upload_stat_download_and_zero_byte(self) -> None:
        prov = self._get_provider()
        work = self._make_work_dir()

        payload = b"\x00\x01\x02relay-desk-\xe4\xba\xa4\xe6\x8e\xa5\xff"
        src = work / "src.bin"
        src.write_bytes(payload)

        up_info = prov.upload(str(src), "/devices//a.json/")
        self.assertIsInstance(up_info, ObjectInfo)  # type: ignore[attr-defined]
        self.assertEqual(up_info.key, "devices/a.json")  # type: ignore[attr-defined]
        self.assertEqual(up_info.size, len(payload))  # type: ignore[attr-defined]
        self.assertEqual(up_info["key"], "devices/a.json")  # type: ignore[attr-defined]
        self.assertEqual(up_info.get("size"), len(payload))  # type: ignore[attr-defined]
        self.assertEqual(up_info.to_dict()["key"], "devices/a.json")  # type: ignore[attr-defined]

        st_info = prov.stat("devices/a.json")
        self.assertIsNotNone(st_info)  # type: ignore[attr-defined]
        assert st_info is not None
        self.assertEqual(st_info.key, "devices/a.json")  # type: ignore[attr-defined]
        self.assertEqual(st_info.size, len(payload))  # type: ignore[attr-defined]

        dst = work / "downloaded" / "a.json"
        dl_info = prov.download("devices/a.json", str(dst))
        self.assertEqual(dl_info.key, "devices/a.json")  # type: ignore[attr-defined]
        self.assertEqual(dl_info.size, len(payload))  # type: ignore[attr-defined]
        self.assertEqual(dst.read_bytes(), payload)  # type: ignore[attr-defined]

        # 0-byte empty file round-trip
        empty_src = work / "empty.bin"
        empty_src.write_bytes(b"")
        prov.upload(str(empty_src), "relays/r_empty/marker")
        st_empty = prov.stat("relays/r_empty/marker")
        self.assertIsNotNone(st_empty)  # type: ignore[attr-defined]
        assert st_empty is not None
        self.assertEqual(st_empty.size, 0)  # type: ignore[attr-defined]
        empty_dst = work / "empty_out.bin"
        prov.download("relays/r_empty/marker", str(empty_dst))
        self.assertEqual(empty_dst.read_bytes(), b"")  # type: ignore[attr-defined]

    def test_contract_nested_keys_sorting_and_directory_safety(self) -> None:
        prov = self._get_provider()
        work = self._make_work_dir()

        files = {
            "relays/r_002/ready.json": b'{"id":"r_002"}',
            "relays/r_001/package.enc": b"ciphertext-bytes-001",
            "relays/r_001/ready.json": b'{"id":"r_001"}',
            "devices/b.json": b'{"id":"b"}',
            "relays_other/note.txt": b"unrelated-prefix",
        }
        for idx, (k, data) in enumerate(files.items()):
            f = work / ("in_%d.bin" % idx)
            f.write_bytes(data)
            prov.upload(str(f), k)

        all_objs = prov.list_objects()
        self.assertEqual(  # type: ignore[attr-defined]
            [o.key for o in all_objs],
            [
                "devices/b.json",
                "relays/r_001/package.enc",
                "relays/r_001/ready.json",
                "relays/r_002/ready.json",
                "relays_other/note.txt",
            ],
        )

        relay_objs = prov.list_objects("relays/")
        self.assertEqual(  # type: ignore[attr-defined]
            [o.key for o in relay_objs],
            [
                "relays/r_001/package.enc",
                "relays/r_001/ready.json",
                "relays/r_002/ready.json",
            ],
        )

        r1_objs = prov.list_objects("relays/r_001")
        self.assertEqual(  # type: ignore[attr-defined]
            [o.key for o in r1_objs],
            [
                "relays/r_001/package.enc",
                "relays/r_001/ready.json",
            ],
        )

        # Directories must NEVER be mistaken for file objects
        self.assertIsNone(prov.stat("relays"))  # type: ignore[attr-defined]
        self.assertIsNone(prov.stat("relays/r_001"))  # type: ignore[attr-defined]
        dir_dst = work / "dir_dl"
        with self.assertRaises(ObjectNotFoundError):  # type: ignore[attr-defined]
            prov.download("relays/r_001", str(dir_dst))
        self.assertFalse(dir_dst.exists())  # type: ignore[attr-defined]

    def test_contract_chinese_utf8_and_unicode_normalization(self) -> None:
        prov = self._get_provider()
        work = self._make_work_dir()

        src = work / "zh.md"
        text = "# 双机异步接力\n状态：已交棒，可以关机\n"
        src.write_text(text, encoding="utf-8")

        raw_key_nfd = unicodedata.normalize("NFD", "测试工作区/双机接力_café/说明文档.md")
        expected_nfc = unicodedata.normalize("NFC", raw_key_nfd)

        info = prov.upload(str(src), raw_key_nfd)
        self.assertEqual(info.key, expected_nfc)  # type: ignore[attr-defined]

        listed = prov.list_objects("测试工作区/")
        self.assertEqual([o.key for o in listed], [expected_nfc])  # type: ignore[attr-defined]

        st = prov.stat(raw_key_nfd)
        self.assertIsNotNone(st)  # type: ignore[attr-defined]
        assert st is not None
        self.assertEqual(st.key, expected_nfc)  # type: ignore[attr-defined]

        dst = work / "恢复说明.md"
        prov.download(expected_nfc, str(dst))
        self.assertEqual(dst.read_text(encoding="utf-8"), text)  # type: ignore[attr-defined]

    def test_contract_overwrite_same_size_and_different_size(self) -> None:
        prov = self._get_provider()
        work = self._make_work_dir()

        src = work / "item.bin"
        dst = work / "out.bin"

        # Same-size overwrite within the same wall-clock second (tests WebDAV/S3 mtime window)
        src.write_bytes(b"alpha")
        prov.upload(str(src), "state/marker.bin")
        src.write_bytes(b"omega")
        prov.upload(str(src), "state/marker.bin")

        prov.download("state/marker.bin", str(dst))
        self.assertEqual(dst.read_bytes(), b"omega")  # type: ignore[attr-defined]

        # Different-size overwrite
        longer = b"updated-longer-payload-0123456789"
        src.write_bytes(longer)
        prov.upload(str(src), "state/marker.bin")
        st = prov.stat("state/marker.bin")
        self.assertIsNotNone(st)  # type: ignore[attr-defined]
        assert st is not None
        self.assertEqual(st.size, len(longer))  # type: ignore[attr-defined]
        prov.download("state/marker.bin", str(dst))
        self.assertEqual(dst.read_bytes(), longer)  # type: ignore[attr-defined]

    def test_contract_atomic_download_failure_leaves_no_partial_target(self) -> None:
        prov = self._get_provider()
        work = self._make_work_dir()

        dl_dir = work / "target_dir"
        dl_dir.mkdir()
        fresh_target = dl_dir / "fresh.bin"

        # 1. Missing object into non-existent target -> target stays absent, dir stays empty
        with self.assertRaises(ObjectNotFoundError):  # type: ignore[attr-defined]
            prov.download("missing/object.bin", str(fresh_target))
        self.assertFalse(fresh_target.exists())  # type: ignore[attr-defined]
        self.assertEqual(list(dl_dir.iterdir()), [])  # type: ignore[attr-defined]

        # 2. Missing object over existing target -> existing file preserved byte-for-byte
        existing_target = dl_dir / "existing.bin"
        existing_target.write_bytes(b"keep-me-intact")
        with self.assertRaises(ObjectNotFoundError):  # type: ignore[attr-defined]
            prov.download("missing/object.bin", str(existing_target))
        self.assertEqual(existing_target.read_bytes(), b"keep-me-intact")  # type: ignore[attr-defined]
        self.assertEqual([p.name for p in dl_dir.iterdir()], ["existing.bin"])  # type: ignore[attr-defined]

        # 3. Simulated failure during final os.replace after remote fetch -> no partial/temp leftovers
        src = work / "remote_src.bin"
        src.write_bytes(b"new-remote-content-that-fails-replace")
        prov.upload(str(src), "relays/r_atomic/package.enc")

        with mock.patch("relaydesk.storage.os.replace", side_effect=OSError("disk full during replace")):
            with self.assertRaises(StorageError):  # type: ignore[attr-defined]
                prov.download("relays/r_atomic/package.enc", str(existing_target))

        self.assertEqual(existing_target.read_bytes(), b"keep-me-intact")  # type: ignore[attr-defined]
        self.assertEqual([p.name for p in dl_dir.iterdir()], ["existing.bin"])  # type: ignore[attr-defined]

    def test_contract_delete_existing_and_missing(self) -> None:
        prov = self._get_provider()
        work = self._make_work_dir()

        src = work / "receipt.json"
        src.write_text('{"ok":true}', encoding="utf-8")
        prov.upload(str(src), "relays/r_del/receipt.json")

        self.assertIsNotNone(prov.stat("relays/r_del/receipt.json"))  # type: ignore[attr-defined]
        self.assertTrue(prov.delete("relays/r_del/receipt.json"))  # type: ignore[attr-defined]
        self.assertIsNone(prov.stat("relays/r_del/receipt.json"))  # type: ignore[attr-defined]
        self.assertFalse(prov.delete("relays/r_del/receipt.json"))  # type: ignore[attr-defined]

    def test_contract_invalid_keys_rejected(self) -> None:
        prov = self._get_provider()
        work = self._make_work_dir()
        src = work / "dummy.txt"
        src.write_text("x", encoding="utf-8")
        dst = work / "out.txt"

        for empty_key in ("", "/", "///"):
            with self.assertRaises(StorageError):  # type: ignore[attr-defined]
                prov.upload(str(src), empty_key)
            with self.assertRaises(StorageError):  # type: ignore[attr-defined]
                prov.download(empty_key, str(dst))
            with self.assertRaises(StorageError):  # type: ignore[attr-defined]
                prov.stat(empty_key)
            with self.assertRaises(StorageError):  # type: ignore[attr-defined]
                prov.delete(empty_key)

        for bad_key in ("..", ".", "a/../b", "../escape", "a/./b", "a\\b", "..\\escape", "a\x00b"):
            with self.assertRaises(StorageError):  # type: ignore[attr-defined]
                prov.list_objects(bad_key)
            with self.assertRaises(StorageError):  # type: ignore[attr-defined]
                prov.upload(str(src), bad_key)
            with self.assertRaises(StorageError):  # type: ignore[attr-defined]
                prov.download(bad_key, str(dst))
            with self.assertRaises(StorageError):  # type: ignore[attr-defined]
                prov.stat(bad_key)
            with self.assertRaises(StorageError):  # type: ignore[attr-defined]
                prov.delete(bad_key)
