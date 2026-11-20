# -*- coding: utf-8 -*-
"""Fast smoke test (< 3s) for the reproducible inbox benchmark suite.

Verifies on a 3-device x 50-history + 10-active LocalDir dataset that:
1. New v3 `list_inbox` downloads ONLY the 10 active incoming `ready.json` files
   (zero historical files downloaded) on both `first_read` and `repeat_refresh`.
2. New v2-compat caches `archivedRelayIds` on `first_read` so `repeat_refresh`
   downloads 0 historical files.
3. `receive_relay` on an active incoming relay performs full Ed25519 signature,
   SHA-256, and XChaCha20-Poly1305 secretstream verification (and rejects tampering).
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks import inbox_benchmark as bench  # noqa: E402
from relaydesk import crypto  # noqa: E402
from relaydesk import relay_engine  # noqa: E402


class TestBenchmarkSmoke(unittest.TestCase):
    def test_v3_inbox_skips_history_and_receive_verifies_crypto(self) -> None:
        t_start = time.perf_counter()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tar_path, tar_sha, tar_size = bench._build_sample_tar(root)
            identities = bench._create_identities(root / "idents", num_devices=3)
            ident_b = identities[1]

            # 1. Benchmark New v3 (3 devices x 50 history + 10 active)
            cloud_v3 = root / "cloud_v3"
            cloud_v3.mkdir()
            home_b_v3 = root / "home_b_v3"
            led_b_v3 = root / "ledger_b_v3.json"
            v3_info = bench.setup_v3_dataset(
                cloud_v3,
                home_b_v3,
                led_b_v3,
                identities,
                tar_path,
                tar_sha,
                tar_size,
                num_history=50,
                num_active=10,
                storage_cfg={"provider": "local", "root": str(cloud_v3)},
            )
            res_v3 = bench.benchmark_new_engine(
                home_b_v3,
                led_b_v3,
                v3_info["local"],
                v3_info["initialRelayState"],
                runs=1,
            )

            expected_active_keys = {
                "v3/inbox/%s/%s/ready.json" % (ident_b["deviceId"], rid)
                for rid in v3_info["activeIds"]
            }
            for phase in ("first_read", "repeat_refresh"):
                m = res_v3[phase]
                self.assertEqual(m["incoming_count"], 10)
                self.assertEqual(m["download_count"], 10)
                self.assertEqual(set(m["downloaded_keys"]), expected_active_keys)
                self.assertEqual(m["verify_count"], 20)  # ready.json + snapshot per active relay
                for k in m["downloaded_keys"]:
                    self.assertNotIn("20261117", k)

            # 2. Verify New v2-compat downloads 0 historical files on repeat_refresh
            cloud_v2 = root / "cloud_v2"
            cloud_v2.mkdir()
            home_b_v2 = root / "home_b_v2"
            led_b_v2 = root / "ledger_b_v2.json"
            v2_info = bench.setup_v2_dataset(
                cloud_v2,
                home_b_v2,
                led_b_v2,
                identities,
                tar_path,
                num_history=50,
                num_active=10,
                mode="new_v2_compat",
                storage_cfg={"provider": "local", "root": str(cloud_v2)},
            )
            res_v2 = bench.benchmark_new_engine(
                home_b_v2,
                led_b_v2,
                v2_info["local"],
                v2_info["initialRelayState"],
                runs=1,
            )
            self.assertEqual(res_v2["repeat_refresh"]["incoming_count"], 10)
            self.assertEqual(res_v2["repeat_refresh"]["download_count"], 10)
            for k in res_v2["repeat_refresh"]["downloaded_keys"]:
                self.assertNotIn("20261117", k)

            # 3. Verify receive_relay performs full signature + SHA-256 + secretstream verification
            ws_b = root / "ws_b_recv"
            uhome_b = root / "uhome_b"
            uhome_b.mkdir()
            with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": str(uhome_b)}):
                with bench.count_verify_json(crypto) as vcnt:
                    rec = relay_engine.receive_relay(
                        v3_info["activeIds"][0],
                        target_path=str(ws_b),
                        ledger_path=led_b_v3,
                        local=v3_info["local"],
                        local_home=home_b_v3,
                    )
            self.assertGreaterEqual(vcnt["count"], 2)
            self.assertEqual(rec["projectId"], "p_bench")
            self.assertEqual(
                (ws_b / "app.py").read_text(encoding="utf-8"),
                "def hello():\n    return 'relay-bench'\n",
            )

            # 4. Tamper package.enc on second active relay -> must fail secretstream/MAC check
            tampered_rid = v3_info["activeIds"][1]
            enc_file = cloud_v3 / "v3" / "inbox" / ident_b["deviceId"] / tampered_rid / "package.enc"
            raw_enc = bytearray(enc_file.read_bytes())
            raw_enc[-10] ^= 0xFF
            enc_file.write_bytes(bytes(raw_enc))

            ws_b_bad = root / "ws_b_bad"
            with mock.patch.dict(os.environ, {"RELAY_DESK_USER_HOME": str(uhome_b)}):
                with self.assertRaises(crypto.TamperError):
                    relay_engine.receive_relay(
                        tampered_rid,
                        target_path=str(ws_b_bad),
                        ledger_path=led_b_v3,
                        local=v3_info["local"],
                        local_home=home_b_v3,
                    )

        elapsed = time.perf_counter() - t_start
        self.assertLess(elapsed, 3.0, "Smoke test took too long: %.3fs" % elapsed)


if __name__ == "__main__":
    unittest.main()
