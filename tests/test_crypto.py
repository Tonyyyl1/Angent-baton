import base64
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from relaydesk import crypto as C


def _recompute_envelope_digest(enc_path: Path, env: dict) -> dict:
    """Helper simulating an attacker who updates packageSize/packageSha256 after tampering."""
    raw = enc_path.read_bytes()
    patched = dict(env)
    patched["packageSize"] = len(raw)
    patched["packageSha256"] = hashlib.sha256(raw).hexdigest()
    return patched


def _split_frames(raw: bytes) -> list:
    frames = []
    pos = 0
    while pos < len(raw):
        (ln,) = struct.unpack(">I", raw[pos : pos + 4])
        frames.append(raw[pos : pos + 4 + ln])
        pos += 4 + ln
    return frames


class CryptoTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)


class TestAvailabilityAndLazyImport(CryptoTestBase):
    def test_has_and_require_crypto_when_installed(self):
        self.assertTrue(C.has_crypto())
        C.require_crypto()

    def test_mocked_missing_nacl_raises_unavailable_error(self):
        blocked = {
            "nacl": None,
            "nacl.bindings": None,
            "nacl.signing": None,
            "nacl.public": None,
            "nacl.utils": None,
        }
        with mock.patch.dict(sys.modules, blocked):
            self.assertFalse(C.has_crypto())
            with self.assertRaises(C.CryptoUnavailableError) as ctx:
                C.require_crypto()
            self.assertEqual(
                str(ctx.exception),
                "云接力加密需要 PyNaCl（libsodium）。请先运行：python3 -m pip install pynacl",
            )
            with self.assertRaises(C.CryptoUnavailableError):
                C.ensure_identity(self.dir / "no_nacl_home", alias="a")
            with self.assertRaises(C.CryptoUnavailableError):
                C.sign_json({"k": "v"}, base64.b64encode(b"0" * 32).decode("ascii"))
            with self.assertRaises(C.CryptoUnavailableError):
                C.verify_json(
                    {"k": "v", "signature": base64.b64encode(b"0" * 64).decode("ascii")},
                    base64.b64encode(b"0" * 32).decode("ascii"),
                )
            with self.assertRaises(C.CryptoUnavailableError):
                C.encrypt_package("a", "b", {}, {})
            with self.assertRaises(C.CryptoUnavailableError):
                C.decrypt_package("a", "b", {}, {}, {})
            with self.assertRaises(C.CryptoUnavailableError):
                C.encrypt_meta({}, {}, {})
            with self.assertRaises(C.CryptoUnavailableError):
                C.decrypt_meta({}, {}, {})

    def test_no_top_level_nacl_import_in_fresh_interpreter(self):
        repo_root = Path(__file__).resolve().parent.parent
        code = (
            "import builtins, sys\n"
            "real_import = builtins.__import__\n"
            "def guard(name, *args, **kwargs):\n"
            "    if name == 'nacl' or name.startswith('nacl.'):\n"
            "        raise ImportError('nacl blocked for test')\n"
            "    return real_import(name, *args, **kwargs)\n"
            "builtins.__import__ = guard\n"
            "import relaydesk.crypto as C\n"
            "assert C.has_crypto() is False\n"
            "try:\n"
            "    C.require_crypto()\n"
            "except C.CryptoUnavailableError as e:\n"
            "    assert 'PyNaCl' in str(e)\n"
            "else:\n"
            "    raise AssertionError('expected CryptoUnavailableError')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)


class TestIdentityAndFingerprints(CryptoTestBase):
    def test_ensure_identity_permissions_persistence_and_public_descriptor(self):
        home_a = self.dir / "home_a"
        ident = C.ensure_identity(
            home_a,
            alias="a",
            name="设备 A（台式机）",
            os_name="windows",
            host="desktop-a",
        )
        ident_file = home_a / "identity.json"
        self.assertTrue(ident_file.is_file())

        if os.name != "nt":
            mode = stat.S_IMODE(ident_file.stat().st_mode)
            self.assertEqual(mode, 0o600)

        self.assertTrue(ident["deviceId"].startswith("dev_"))
        self.assertEqual(len(ident["deviceId"]), 4 + 16)
        for key_field in ("signPrivateKey", "verifyPublicKey", "boxPrivateKey", "boxPublicKey"):
            raw = base64.b64decode(ident[key_field])
            self.assertEqual(len(raw), 32)
        self.assertTrue(ident["createdAt"].endswith("Z"))
        self.assertEqual(
            ident["keyFingerprint"],
            C.device_fingerprint(ident["verifyPublicKey"], ident["boxPublicKey"]),
        )

        # Updating metadata keeps exact same deviceId and key material
        if os.name != "nt":
            os.chmod(ident_file, 0o644)
        ident_2 = C.ensure_identity(home_a, name="新名称 A")
        self.assertEqual(ident_2["deviceId"], ident["deviceId"])
        self.assertEqual(ident_2["signPrivateKey"], ident["signPrivateKey"])
        self.assertEqual(ident_2["boxPrivateKey"], ident["boxPrivateKey"])
        self.assertEqual(ident_2["alias"], "a")
        self.assertEqual(ident_2["name"], "新名称 A")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(ident_file.stat().st_mode), 0o600)

        pub = C.public_descriptor(ident_2)
        self.assertEqual(
            set(pub.keys()),
            {
                "deviceId",
                "alias",
                "name",
                "os",
                "host",
                "verifyPublicKey",
                "boxPublicKey",
                "keyFingerprint",
                "createdAt",
            },
        )
        self.assertNotIn("signPrivateKey", pub)
        self.assertNotIn("boxPrivateKey", pub)
        serialized_pub = json.dumps(pub)
        self.assertNotIn(ident_2["signPrivateKey"], serialized_pub)
        self.assertNotIn(ident_2["boxPrivateKey"], serialized_pub)

        # Corrupt identity.json raises CryptoError
        bad_home = self.dir / "bad_home"
        bad_home.mkdir()
        (bad_home / "identity.json").write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(C.CryptoError):
            C.ensure_identity(bad_home)

    def test_device_and_pair_fingerprint_format_and_symmetry(self):
        ident_a = C.ensure_identity(self.dir / "a", alias="a", name="MacBook", os_name="mac")
        ident_b = C.ensure_identity(self.dir / "b", alias="b", name="PC", os_name="windows")
        desc_a = C.public_descriptor(ident_a)
        desc_b = C.public_descriptor(ident_b)

        fp_a = C.device_fingerprint(desc_a["verifyPublicKey"], desc_a["boxPublicKey"])
        self.assertRegex(fp_a, r"^[0-9A-F]{4}(?:-[0-9A-F]{4}){3}$")
        expected_digest = (
            hashlib.sha256(
                b"relaydesk-dev-v1:"
                + base64.b64decode(desc_a["verifyPublicKey"])
                + b":"
                + base64.b64decode(desc_a["boxPublicKey"])
            )
            .hexdigest()
            .upper()[:16]
        )
        self.assertEqual(fp_a.replace("-", ""), expected_digest)

        pair_ab = C.pair_fingerprint(desc_a, desc_b)
        pair_ba = C.pair_fingerprint(desc_b, desc_a)
        self.assertEqual(pair_ab, pair_ba)
        self.assertRegex(pair_ab, r"^[0-9A-F]{4}(?:-[0-9A-F]{4}){5}$")

        # Signed descriptors / changed human labels do not alter SAS pair fingerprint
        signed_a = C.sign_json(desc_a, ident_a["signPrivateKey"])
        renamed_b = dict(desc_b, name="改名后的电脑", host="other-host")
        self.assertEqual(C.pair_fingerprint(signed_a, renamed_b), pair_ab)

        # Changing any cryptographic key or deviceId changes pair fingerprint
        ident_c = C.ensure_identity(self.dir / "c", alias="b")
        desc_c = C.public_descriptor(ident_c)
        self.assertNotEqual(C.pair_fingerprint(desc_a, desc_c), pair_ab)
        forged_b = dict(desc_b, boxPublicKey=desc_c["boxPublicKey"])
        self.assertNotEqual(C.pair_fingerprint(desc_a, forged_b), pair_ab)


class TestEd25519Signing(CryptoTestBase):
    def test_sign_and_verify_json_canonical_and_tamper_detection(self):
        ident_a = C.ensure_identity(self.dir / "a", alias="a")
        ident_b = C.ensure_identity(self.dir / "b", alias="b")

        payload_1 = {"project": "双机接力项目", "count": 3, "nested": {"z": True, "a": [1, 2]}}
        payload_2 = {"nested": {"a": [1, 2], "z": True}, "count": 3, "project": "双机接力项目"}

        signed_1 = C.sign_json(payload_1, ident_a["signPrivateKey"], signer_device_id=ident_a["deviceId"])
        signed_2 = C.sign_json(payload_2, ident_a["signPrivateKey"], signer_device_id=ident_a["deviceId"])
        self.assertEqual(signed_1["signature"], signed_2["signature"])
        self.assertNotIn("signature", payload_1)  # input not mutated

        verified = C.verify_json(
            signed_1,
            ident_a["verifyPublicKey"],
            expected_device_id=ident_a["deviceId"],
        )
        self.assertEqual(verified["project"], "双机接力项目")
        self.assertEqual(verified["signerDeviceId"], ident_a["deviceId"])
        self.assertNotIn("signature", verified)

        # Descriptor self-signature with expected_device_id resolved via deviceId
        desc_a = C.public_descriptor(ident_a)
        signed_desc = C.sign_json(desc_a, ident_a["signPrivateKey"])
        verified_desc = C.verify_json(
            signed_desc,
            desc_a["verifyPublicKey"],
            expected_device_id=desc_a["deviceId"],
        )
        self.assertEqual(verified_desc, desc_a)

        # Tampered field value
        tampered_val = dict(signed_1, count=4)
        with self.assertRaises(C.TamperError):
            C.verify_json(tampered_val, ident_a["verifyPublicKey"])

        # Wrong public key
        with self.assertRaises(C.TamperError):
            C.verify_json(signed_1, ident_b["verifyPublicKey"])

        # Wrong expected_device_id
        with self.assertRaises(C.TamperError):
            C.verify_json(
                signed_1,
                ident_a["verifyPublicKey"],
                expected_device_id=ident_b["deviceId"],
            )

        # Bit flip in signature bytes
        sig_bytes = bytearray(base64.b64decode(signed_1["signature"]))
        sig_bytes[10] ^= 0x01
        bad_sig = dict(signed_1, signature=base64.b64encode(bytes(sig_bytes)).decode("ascii"))
        with self.assertRaises(C.TamperError):
            C.verify_json(bad_sig, ident_a["verifyPublicKey"])

        # Missing signature or invalid type
        with self.assertRaises(C.TamperError):
            C.verify_json({"project": "x"}, ident_a["verifyPublicKey"])
        with self.assertRaises(C.TamperError):
            C.verify_json(["not-a-dict"], ident_a["verifyPublicKey"])  # type: ignore

        # Re-signing an already signed dict replaces old signature cleanly
        resigned = C.sign_json(dict(signed_1, count=99), ident_a["signPrivateKey"])
        verified_resigned = C.verify_json(resigned, ident_a["verifyPublicKey"])
        self.assertEqual(verified_resigned["count"], 99)


class TestSecretstreamPackageCrypto(CryptoTestBase):
    def setUp(self):
        super().setUp()
        self.ident_a = C.ensure_identity(self.dir / "a", alias="a", os_name="mac")
        self.ident_b = C.ensure_identity(self.dir / "b", alias="b", os_name="windows")
        self.ident_c = C.ensure_identity(self.dir / "c", alias="a", os_name="linux")
        self.desc_a = C.public_descriptor(self.ident_a)
        self.desc_b = C.public_descriptor(self.ident_b)
        self.desc_c = C.public_descriptor(self.ident_c)

    def test_zero_byte_package_roundtrip(self):
        plain = self.dir / "empty.tar.gz"
        enc = self.dir / "empty.enc"
        out_b = self.dir / "out_b" / "empty.tar.gz"
        out_a = self.dir / "out_a" / "empty.tar.gz"
        plain.write_bytes(b"")

        env = C.encrypt_package(str(plain), str(enc), self.ident_a, self.desc_b, chunk_size=1024)
        self.assertEqual(env["packageSize"], enc.stat().st_size)
        self.assertGreater(env["packageSize"], 0)

        C.decrypt_package(str(enc), str(out_b), env, self.ident_b, self.desc_a)
        self.assertEqual(out_b.read_bytes(), b"")

        # Sender can also decrypt its own sent package via wrappedKeySender
        C.decrypt_package(str(enc), str(out_a), env, self.ident_a, self.desc_a)
        self.assertEqual(out_a.read_bytes(), b"")

    def test_exact_chunk_boundary_and_multichunk_roundtrips(self):
        for size in (1, 1024, 2048, 10503):
            with self.subTest(size=size):
                data = os.urandom(size)
                plain = self.dir / ("in_%d.bin" % size)
                enc = self.dir / ("pkg_%d.enc" % size)
                dec = self.dir / ("dec_%d.bin" % size)
                plain.write_bytes(data)

                env = C.encrypt_package(
                    str(plain), str(enc), self.ident_a, self.desc_b, chunk_size=1024
                )
                self.assertEqual(env["chunkSize"], 1024)
                self.assertEqual(env["senderDeviceId"], self.ident_a["deviceId"])
                self.assertEqual(env["recipientDeviceId"], self.desc_b["deviceId"])

                C.decrypt_package(str(enc), str(dec), env, self.ident_b, self.desc_a)
                self.assertEqual(dec.read_bytes(), data)

    def test_tamper_bitflip_truncation_reordering_trailing_and_wrong_key(self):
        data = os.urandom(4500)  # 5 chunks at chunk_size=1024
        plain = self.dir / "orig.bin"
        enc = self.dir / "orig.enc"
        plain.write_bytes(data)
        env = C.encrypt_package(str(plain), str(enc), self.ident_a, self.desc_b, chunk_size=1024)
        orig_enc_bytes = enc.read_bytes()
        frames = _split_frames(orig_enc_bytes)
        self.assertEqual(len(frames), 5)

        out_dir = self.dir / "clean_out"
        out_file = out_dir / "restored.bin"

        # 1. Bit flip caught by outer SHA-256 check AND by inner secretstream Poly1305
        flipped = bytearray(orig_enc_bytes)
        flipped[-5] ^= 0x80
        enc_flip = self.dir / "flip.enc"
        enc_flip.write_bytes(bytes(flipped))
        with self.assertRaises(C.TamperError):
            C.decrypt_package(str(enc_flip), str(out_file), env, self.ident_b, self.desc_a)
        with self.assertRaises(C.TamperError):
            C.decrypt_package(
                str(enc_flip),
                str(out_file),
                _recompute_envelope_digest(enc_flip, env),
                self.ident_b,
                self.desc_a,
            )

        # 2. Stream truncated at frame boundary (missing final TAG_FINAL chunk!)
        enc_no_final = self.dir / "no_final.enc"
        enc_no_final.write_bytes(b"".join(frames[:-1]))
        with self.assertRaises(C.TamperError):
            C.decrypt_package(str(enc_no_final), str(out_file), env, self.ident_b, self.desc_a)
        with self.assertRaises(C.TamperError):
            C.decrypt_package(
                str(enc_no_final),
                str(out_file),
                _recompute_envelope_digest(enc_no_final, env),
                self.ident_b,
                self.desc_a,
            )

        # 3. Mid-frame truncation & 0-byte enc file
        enc_mid = self.dir / "mid_trunc.enc"
        enc_mid.write_bytes(orig_enc_bytes[:-10])
        with self.assertRaises(C.TamperError):
            C.decrypt_package(
                str(enc_mid),
                str(out_file),
                _recompute_envelope_digest(enc_mid, env),
                self.ident_b,
                self.desc_a,
            )
        enc_zero = self.dir / "zero.enc"
        enc_zero.write_bytes(b"")
        with self.assertRaises(C.TamperError):
            C.decrypt_package(
                str(enc_zero),
                str(out_file),
                _recompute_envelope_digest(enc_zero, env),
                self.ident_b,
                self.desc_a,
            )

        # 4. Chunk reordering (swap chunk 0 and chunk 1)
        reordered = [frames[1], frames[0]] + frames[2:]
        enc_reord = self.dir / "reordered.enc"
        enc_reord.write_bytes(b"".join(reordered))
        with self.assertRaises(C.TamperError):
            C.decrypt_package(
                str(enc_reord),
                str(out_file),
                _recompute_envelope_digest(enc_reord, env),
                self.ident_b,
                self.desc_a,
            )

        # 5. Trailing garbage / extra frame after TAG_FINAL
        for suffix in (b"\x00", b"garbage!", frames[0]):
            enc_trail = self.dir / "trailing.enc"
            enc_trail.write_bytes(orig_enc_bytes + suffix)
            with self.assertRaises(C.TamperError):
                C.decrypt_package(str(enc_trail), str(out_file), env, self.ident_b, self.desc_a)
            with self.assertRaises(C.TamperError):
                C.decrypt_package(
                    str(enc_trail),
                    str(out_file),
                    _recompute_envelope_digest(enc_trail, env),
                    self.ident_b,
                    self.desc_a,
                )

        # 6. Wrong keys: unrelated device C, forged deviceId with C's private key, wrong sender pub
        with self.assertRaises(C.TamperError):
            C.decrypt_package(str(enc), str(out_file), env, self.ident_c, self.desc_a)

        forged_b = dict(self.ident_c, deviceId=self.ident_b["deviceId"])
        with self.assertRaises(C.TamperError):
            C.decrypt_package(str(enc), str(out_file), env, forged_b, self.desc_a)

        forged_sender = dict(self.desc_c, deviceId=self.desc_a["deviceId"])
        with self.assertRaises(C.TamperError):
            C.decrypt_package(str(enc), str(out_file), env, self.ident_b, forged_sender)

        # Verify output file was NEVER created and zero temp files leaked in out_dir
        self.assertFalse(out_file.exists())
        if out_dir.exists():
            self.assertEqual(list(out_dir.iterdir()), [])


class TestEncryptedMetadataPreview(CryptoTestBase):
    def test_encrypt_and_decrypt_meta_for_recipient_and_sender(self):
        ident_a = C.ensure_identity(self.dir / "a", alias="a")
        ident_b = C.ensure_identity(self.dir / "b", alias="b")
        ident_c = C.ensure_identity(self.dir / "c", alias="a")
        desc_a = C.public_descriptor(ident_a)
        desc_b = C.public_descriptor(ident_b)
        desc_c = C.public_descriptor(ident_c)

        meta = {
            "projectId": "p_secret01",
            "projectName": "核心支付网关",
            "lastTask": "完成签名链路重构",
            "nextAction": "补充多机验收测试",
            "branch": "feat/cloud-relay",
            "dirty": 3,
            "sessionCount": 4,
        }
        env = C.encrypt_meta(meta, ident_a, desc_b)

        raw_env_json = json.dumps(env, ensure_ascii=False)
        for sensitive in ("p_secret01", "核心支付网关", "完成签名链路重构", "feat/cloud-relay"):
            self.assertNotIn(sensitive, raw_env_json)

        # Recipient B decrypts using pinned peer A
        self.assertEqual(C.decrypt_meta(env, ident_b, desc_a), meta)
        # Sender A decrypts using pinned peer B or own public descriptor A
        self.assertEqual(C.decrypt_meta(env, ident_a, desc_b), meta)
        self.assertEqual(C.decrypt_meta(env, ident_a, desc_a), meta)

        # Third-party viewer or wrong peer public key rejected
        with self.assertRaises(C.TamperError):
            C.decrypt_meta(env, ident_c, desc_a)
        with self.assertRaises(C.TamperError):
            C.decrypt_meta(env, ident_b, desc_c)
        with self.assertRaises(C.TamperError):
            C.decrypt_meta(env, ident_a, desc_c)

        # Tampering with either recipient or sender ciphertext raises TamperError
        ct_bytes = bytearray(base64.b64decode(env["ciphertextRecipient"]))
        ct_bytes[-1] ^= 0x01
        bad_recip_env = dict(
            env, ciphertextRecipient=base64.b64encode(bytes(ct_bytes)).decode("ascii")
        )
        with self.assertRaises(C.TamperError):
            C.decrypt_meta(bad_recip_env, ident_b, desc_a)
        with self.assertRaises(C.TamperError):
            C.decrypt_meta(bad_recip_env, ident_a, desc_b)


if __name__ == "__main__":
    unittest.main()
