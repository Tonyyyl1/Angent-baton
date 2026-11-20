import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from relaydesk import devices as D
from relaydesk import ledger as L


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("RELAY_DESK_") and k != "CODEX_HOME"
        }
        self.home = self.dir / "home"
        env["RELAY_DESK_HOME"] = str(self.home)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ledger_file = self.dir / "ledger.json"


class TestMultiDeviceLedgerNormalizationAndHere(Base):
    def test_one_three_and_ten_devices_normalization(self):
        # 1 device
        cfg1 = L.norm_config({"devices": [{"id": "solo", "os": "mac", "customDev": 1}], "extraCfg": "v1"})
        self.assertEqual(len(cfg1["devices"]), 1)
        self.assertEqual(cfg1["devices"][0]["id"], "solo")
        self.assertEqual(cfg1["devices"][0]["os"], "mac")
        self.assertEqual(cfg1["devices"][0]["deviceId"], "")
        self.assertEqual(cfg1["devices"][0]["customDev"], 1)
        self.assertEqual(cfg1["extraCfg"], "v1")

        # 3 devices
        cfg3 = L.norm_config(
            {
                "devices": [
                    {"id": "a", "name": "WinBox", "os": "windows", "host": "win-host", "deviceId": "dev_a"},
                    {"id": "b", "name": "MacMini", "os": "macos", "host": "mac-mini", "deviceId": "dev_b"},
                    {"id": "c", "name": "LinuxLab", "os": "linux", "host": "lab-box", "tag": "gpu"},
                ]
            }
        )
        self.assertEqual([d["id"] for d in cfg3["devices"]], ["a", "b", "c"])
        self.assertEqual([d["os"] for d in cfg3["devices"]], ["windows", "mac", "linux"])
        self.assertEqual(cfg3["devices"][2]["deviceId"], "")
        self.assertEqual(cfg3["devices"][2]["tag"], "gpu")

        # 10 devices
        devs10 = [
            {
                "id": "node-%02d" % i,
                "name": "Node %d" % i,
                "os": ("windows", "mac", "linux")[i % 3],
                "host": "host-%d" % i,
                "deviceId": "dev_%02d" % i,
                "rack": i,
            }
            for i in range(10)
        ]
        cfg10 = L.norm_config({"devices": devs10})
        self.assertEqual(len(cfg10["devices"]), 10)
        self.assertEqual(cfg10["devices"][9]["id"], "node-09")
        self.assertEqual(cfg10["devices"][9]["rack"], 9)

        # Empty devices list falls back to default 2-device config
        cfg_empty = L.norm_config({"devices": []})
        self.assertEqual([d["id"] for d in cfg_empty["devices"]], ["a", "b"])

    def test_conflict_rejection_alias_name_and_device_id(self):
        # Alias conflict (case-insensitive)
        with self.assertRaises(L.LedgerError) as ctx1:
            L.norm_config(
                {
                    "devices": [
                        {"id": "MacStudio", "name": "Studio 1", "os": "mac"},
                        {"id": "macstudio", "name": "Studio 2", "os": "mac"},
                    ]
                }
            )
        self.assertIsInstance(ctx1.exception, D.DeviceConflictError)
        self.assertEqual(ctx1.exception.code, "alias_conflict")

        # Name conflict (case-insensitive)
        with self.assertRaises(L.LedgerError) as ctx2:
            L.norm_config(
                {
                    "devices": [
                        {"id": "d1", "name": "Workstation", "os": "linux"},
                        {"id": "d2", "name": "workstation", "os": "windows"},
                    ]
                }
            )
        self.assertIsInstance(ctx2.exception, D.DeviceConflictError)
        self.assertEqual(ctx2.exception.code, "name_conflict")

        # deviceId conflict
        with self.assertRaises(L.LedgerError) as ctx3:
            L.norm_config(
                {
                    "devices": [
                        {"id": "d1", "name": "One", "os": "mac", "deviceId": "dev_dup"},
                        {"id": "d2", "name": "Two", "os": "linux", "deviceId": "dev_dup"},
                    ]
                }
            )
        self.assertIsInstance(ctx3.exception, D.DeviceConflictError)
        self.assertEqual(ctx3.exception.code, "device_id_conflict")

        # Empty names and empty deviceIds do NOT conflict
        cfg_ok = L.norm_config(
            {
                "devices": [
                    {"id": "d1", "name": "", "os": "mac", "deviceId": ""},
                    {"id": "d2", "name": "", "os": "linux", "deviceId": ""},
                ]
            }
        )
        self.assertEqual(len(cfg_ok["devices"]), 2)

    def test_validate_alias(self):
        self.assertEqual(D.validate_alias(" mac-mini_01.local "), "mac-mini_01.local")
        self.assertEqual(D.validate_alias("a" * 40), "a" * 40)
        for bad in ("", "   ", "a" * 41, "bad alias", "mac/mini", "dev@home"):
            with self.assertRaises(ValueError):
                D.validate_alias(bad)
        with self.assertRaises(ValueError):
            D.validate_alias(123)  # type: ignore

    def test_here_resolution_by_device_id_alias_host_and_os(self):
        led = L.load("nonexistent.json")
        led["config"] = L.norm_config(
            {
                "devices": [
                    {"id": "win-pc", "name": "WinPC", "os": "windows", "host": "gaming-pc", "deviceId": "dev_win_1"},
                    {"id": "mac-book", "name": "MacBook", "os": "mac", "host": "mbp-office", "deviceId": "dev_mac_2"},
                    {"id": "linux-srv", "name": "LinuxSrv", "os": "linux", "host": "gpu-srv", "deviceId": "dev_lnx_3"},
                ]
            }
        )
        with mock.patch.object(L, "hostname", return_value="unknown-host"), mock.patch.object(
            L, "local_os", return_value="linux"
        ):
            # 1. By OS (unique among 3)
            self.assertEqual(L.here(led), "linux-srv")

            # 2. Ambiguous OS returns None when host/identity/local/env absent
            led_dup_os = copy.deepcopy(led)
            led_dup_os["config"]["devices"][0]["os"] = "linux"
            self.assertIsNone(L.here(led_dup_os))

            # 3. By host (exact and short hostname before '.')
            with mock.patch.object(L, "hostname", return_value="mbp-office.local"):
                self.assertEqual(L.here(led), "mac-book")
            with mock.patch.object(L, "hostname", return_value="GAMING-PC"):
                self.assertEqual(L.here(led), "win-pc")

            # 4. By identity.json deviceId and alias (beats host & os)
            self.home.mkdir(parents=True, exist_ok=True)
            ident_path = self.home / "identity.json"
            ident_path.write_text(
                json.dumps({"deviceId": "dev_win_1", "alias": "mac-book"}), encoding="utf-8"
            )
            self.assertEqual(L.here(led), "win-pc")  # deviceId takes precedence over alias

            ident_path.write_text(
                json.dumps({"deviceId": "dev_unmapped", "alias": "mac-book"}), encoding="utf-8"
            )
            self.assertEqual(L.here(led), "mac-book")

            # 5. By local config.json (beats identity.json)
            self.assertEqual(L.here(led, local={"deviceId": "dev_lnx_3"}), "linux-srv")
            self.assertEqual(L.here(led, local={"device": "win-pc"}), "win-pc")

            # 6. By RELAY_DESK_DEVICE env (by alias, deviceId, or CI alias; beats local)
            with mock.patch.dict(os.environ, {"RELAY_DESK_DEVICE": "dev_mac_2"}):
                self.assertEqual(L.here(led, local={"device": "win-pc"}), "mac-book")
            with mock.patch.dict(os.environ, {"RELAY_DESK_DEVICE": "LINUX-SRV"}):
                self.assertEqual(L.here(led, local={"device": "win-pc"}), "linux-srv")


class TestDeviceAndTargetResolution(unittest.TestCase):
    def setUp(self):
        self.cfg3 = {
            "devices": [
                {"id": "a", "name": "Win Desk", "os": "windows", "host": "win", "deviceId": "dev_aaa"},
                {"id": "b", "name": "Mac Pro", "os": "mac", "host": "mac", "deviceId": "dev_bbb"},
                {"id": "c", "name": "Linux Box", "os": "linux", "host": "lnx", "deviceId": "dev_ccc"},
            ]
        }

    def test_resolve_device_entry(self):
        self.assertEqual(D.resolve_device_entry(self.cfg3, "dev_ccc")["id"], "c")
        self.assertEqual(D.resolve_device_entry(self.cfg3, "b")["id"], "b")
        self.assertEqual(D.resolve_device_entry(self.cfg3, "B")["id"], "b")
        self.assertEqual(D.resolve_device_entry(self.cfg3, "win desk")["id"], "a")
        with self.assertRaises(LookupError):
            D.resolve_device_entry(self.cfg3, "missing")
        with self.assertRaises(LookupError):
            D.resolve_device_entry(self.cfg3, "   ")

        # Duplicate names trigger AmbiguousDeviceError
        dup_cfg = {
            "devices": [
                {"id": "m1", "name": "Office Mac", "os": "mac", "deviceId": "dev_1"},
                {"id": "m2", "name": "office mac", "os": "mac", "deviceId": "dev_2"},
            ]
        }
        with self.assertRaises(D.AmbiguousDeviceError) as ctx:
            D.resolve_device_entry(dup_cfg, "Office Mac")
        self.assertEqual(ctx.exception.code, "ambiguous_peer")
        self.assertEqual([c["alias"] for c in ctx.exception.candidates], ["m1", "m2"])

    def test_resolve_target_device_manual_mode(self):
        # Single peer -> omission succeeds
        cfg2 = {"devices": self.cfg3["devices"][:2]}
        self.assertEqual(D.resolve_target_device(cfg2, "a")["id"], "b")

        # Only self -> LookupError
        cfg1 = {"devices": self.cfg3["devices"][:1]}
        with self.assertRaises(LookupError):
            D.resolve_target_device(cfg1, "a")

        # Multi-peer -> omission raises AmbiguousDeviceError with candidates
        with self.assertRaises(D.AmbiguousDeviceError) as ctx:
            D.resolve_target_device(self.cfg3, "a")
        self.assertEqual(ctx.exception.code, "ambiguous_peer")
        self.assertEqual([c["alias"] for c in ctx.exception.candidates], ["b", "c"])
        self.assertEqual([c["deviceId"] for c in ctx.exception.candidates], ["dev_bbb", "dev_ccc"])

        # Explicit target by alias / name / deviceId
        self.assertEqual(D.resolve_target_device(self.cfg3, "a", "c")["id"], "c")
        self.assertEqual(D.resolve_target_device(self.cfg3, "a", "mac pro")["id"], "b")
        self.assertEqual(D.resolve_target_device(self.cfg3, "a", "dev_ccc")["id"], "c")

        # Self target rejected
        with self.assertRaises(ValueError):
            D.resolve_target_device(self.cfg3, "a", "a")
        with self.assertRaises(ValueError):
            D.resolve_target_device(self.cfg3, "a", "dev_aaa")

    def test_resolve_target_device_cloud_mode(self):
        cloud_one_paired = {
            "version": 3,
            "peers": {
                "dev_bbb": {
                    "deviceId": "dev_bbb",
                    "alias": "b",
                    "name": "Mac Pro",
                    "os": "mac",
                    "paired": True,
                    "status": "paired",
                },
                "dev_ccc": {
                    "deviceId": "dev_ccc",
                    "alias": "c",
                    "name": "Linux Box",
                    "os": "linux",
                    "paired": False,
                    "status": "discovered",
                },
                "dev_ddd": {
                    "deviceId": "dev_ddd",
                    "alias": "d",
                    "name": "Old Box",
                    "os": "linux",
                    "paired": True,
                    "status": "key_changed",
                },
            },
        }
        # Only 1 paired peer (discovered and key_changed excluded) -> omission returns dev_bbb
        target = D.resolve_target_device(
            self.cfg3, "a", to_query=None, cloud_cfg=cloud_one_paired, require_paired_cloud=True
        )
        self.assertEqual(target["deviceId"], "dev_bbb")

        # Targeting unpaired/key_changed explicitly fails with LookupError
        with self.assertRaises(LookupError):
            D.resolve_target_device(
                self.cfg3, "a", to_query="c", cloud_cfg=cloud_one_paired, require_paired_cloud=True
            )
        with self.assertRaises(LookupError):
            D.resolve_target_device(
                self.cfg3, "a", to_query="d", cloud_cfg=cloud_one_paired, require_paired_cloud=True
            )

        # 0 paired peers -> LookupError
        cloud_zero = {"version": 3, "peers": {"dev_ccc": cloud_one_paired["peers"]["dev_ccc"]}}
        with self.assertRaises(LookupError):
            D.resolve_target_device(
                self.cfg3, "a", to_query=None, cloud_cfg=cloud_zero, require_paired_cloud=True
            )

        # 2+ paired peers -> omission raises AmbiguousDeviceError with candidates
        cloud_two = copy.deepcopy(cloud_one_paired)
        cloud_two["peers"]["dev_ccc"]["paired"] = True
        cloud_two["peers"]["dev_ccc"]["status"] = "paired"
        with self.assertRaises(D.AmbiguousDeviceError) as ctx:
            D.resolve_target_device(
                self.cfg3, "a", to_query=None, cloud_cfg=cloud_two, require_paired_cloud=True
            )
        self.assertEqual(ctx.exception.code, "ambiguous_peer")
        self.assertEqual(sorted(c["deviceId"] for c in ctx.exception.candidates), ["dev_bbb", "dev_ccc"])

        # Explicit selection by alias or deviceId works
        self.assertEqual(
            D.resolve_target_device(
                self.cfg3, "a", to_query="c", cloud_cfg=cloud_two, require_paired_cloud=True
            )["deviceId"],
            "dev_ccc",
        )
        # Self rejected
        with self.assertRaises(ValueError):
            D.resolve_target_device(
                self.cfg3, "a", to_query="a", cloud_cfg=cloud_two, require_paired_cloud=True
            )


class TestBackupsAndIdempotentMigrations(Base):
    def test_ensure_backup_once_behavior(self):
        src = self.dir / "data.json"
        bak = self.dir / "data.v1.backup.json"
        self.assertFalse(D.ensure_backup_once(src, bak))
        src.write_text('{"v": 1}', encoding="utf-8")
        self.assertTrue(D.ensure_backup_once(src, bak))
        self.assertEqual(bak.read_text(encoding="utf-8"), '{"v": 1}')

        # Mutating src does NOT overwrite existing backup
        src.write_text('{"v": 2}', encoding="utf-8")
        self.assertFalse(D.ensure_backup_once(src, bak))
        self.assertEqual(bak.read_text(encoding="utf-8"), '{"v": 1}')

    def test_ledger_v1_backup_and_idempotent_updates_preserve_unknown_fields(self):
        raw_v1 = {
            "app": "relaydesk",
            "version": 1,
            "topCustom": {"nested": [1, "中文"]},
            "config": {
                "cfgCustom": "keep-me",
                "devices": [
                    {"id": "a", "name": "Win", "os": "windows", "drop": "", "host": "", "devCustom": 42},
                    {"id": "b", "name": "Mac", "os": "mac", "drop": "", "host": "", "devTag": "m1"},
                ],
                "reg": {"claudeMd": {"on": {"a": True}}},
            },
            "projects": [
                {
                    "id": "p1",
                    "name": "测试项目",
                    "device": "a",
                    "paths": {"a": "D:\\p1", "b": "/Users/x/p1"},
                    "projCustom": {"flag": True},
                    "sessions": [{"id": "s1", "device": "a", "sessCustom": "abc"}],
                    "handoff": {"tag": "t1", "from": "a", "to": "b", "state": "prep", "hoCustom": 9},
                }
            ],
        }
        raw_text = json.dumps(raw_v1, ensure_ascii=False, indent=2)
        self.ledger_file.write_text(raw_text, encoding="utf-8")
        backup_file = Path(str(self.ledger_file) + ".v1.backup.json")

        for i in range(3):
            led = L.update(self.ledger_file, lambda l, idx=i: l["projects"][0].update(note="run-%d" % idx))
            self.assertEqual(led["version"], 2)
            self.assertTrue(backup_file.exists())
            self.assertEqual(backup_file.read_text(encoding="utf-8"), raw_text)
            self.assertEqual(led["topCustom"], {"nested": [1, "中文"]})
            self.assertEqual(led["config"]["cfgCustom"], "keep-me")
            self.assertEqual(led["config"]["devices"][0]["devCustom"], 42)
            self.assertEqual(led["config"]["devices"][1]["devTag"], "m1")
            p = led["projects"][0]
            self.assertEqual(p["projCustom"], {"flag": True})
            self.assertEqual(p["sessions"][0]["sessCustom"], "abc")
            self.assertEqual(p["handoff"]["hoCustom"], 9)

    def test_cloud_v2_migration_backup_idempotency_and_unknown_fields(self):
        conf_path = str(self.dir / "rclone.conf")
        raw_cloud = {
            "version": 2,
            "provider": "rclone",
            "remote": "gdrive",
            "root": "RelayDesk",
            "configPath": conf_path,
            "customCloudTop": {"region": "apac"},
            "paired": True,
            "pairFingerprint": "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF",
            "pairedAt": "2026-10-01T00:00:00.000Z",
            "peer": {
                "deviceId": "dev_peer_1",
                "alias": "b",
                "name": "MacBook",
                "os": "mac",
                "host": "mbp",
                "verifyPublicKey": "vk_b64",
                "boxPublicKey": "bk_b64",
                "keyFingerprint": "1111-2222-3333-4444",
                "customPeerField": {"note": "preserve-me"},
            },
        }
        cloud_path = self.home / "cloud.json"
        self.home.mkdir(parents=True, exist_ok=True)
        raw_text = json.dumps(raw_cloud, ensure_ascii=False, indent=2)
        cloud_path.write_text(raw_text, encoding="utf-8")
        backup_path = self.home / "cloud.v2.backup.json"

        run1 = D.migrate_cloud_config(raw_cloud, local_home=self.home)
        self.assertTrue(backup_path.exists())
        self.assertEqual(backup_path.read_text(encoding="utf-8"), raw_text)

        # Mutate cloud.json on disk and run 2 more times -> backup stays intact, migration idempotent
        cloud_path.write_text(json.dumps(run1, ensure_ascii=False), encoding="utf-8")
        run2 = D.migrate_cloud_config(run1, local_home=self.home)
        run3 = D.migrate_cloud_config(run2, local_home=self.home)
        self.assertEqual(run1, run2)
        self.assertEqual(run2, run3)
        self.assertEqual(backup_path.read_text(encoding="utf-8"), raw_text)

        self.assertEqual(run3["version"], 3)
        self.assertEqual(run3["customCloudTop"], {"region": "apac"})
        self.assertEqual(run3["shownFingerprints"], {})
        self.assertNotIn("peer", run3)
        self.assertIn("dev_peer_1", run3["peers"])
        peer = run3["peers"]["dev_peer_1"]
        self.assertTrue(peer["paired"])
        self.assertEqual(peer["status"], "paired")
        self.assertEqual(peer["protocolVersion"], 2)
        self.assertEqual(peer["capabilities"], ["v2"])
        self.assertIsNone(peer["pendingDescriptor"])
        self.assertEqual(peer["pairFingerprint"], "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF")
        self.assertEqual(peer["pairedAt"], "2026-10-01T00:00:00.000Z")
        self.assertEqual(peer["customPeerField"], {"note": "preserve-me"})

    def test_relay_state_v2_migration_rules_backup_and_idempotency(self):
        raw_state = {
            "version": 2,
            "customStateTop": [1, 2, 3],
            "pendingSends": {
                "proj_prepared": {
                    "relayId": "r_prep",
                    "prepared": {"relayId": "r_prep", "packageSha256": "abc"},
                    "customPending": "keep1",
                },
                "proj_unknown_pub": {
                    "relayId": "r_unk",
                    "status": "uploading",
                    "customPending": "keep2",
                },
                "proj_checkpointed": {
                    "relayId": "r_chk",
                    "checkpointBeforeReady": True,
                    "status": "uploading",
                },
            },
            "appliedReceives": {
                "r_done": {
                    "relayId": "r_done",
                    "projectId": "p1",
                    "customApplied": {"ok": True},
                }
            },
            "sentRelays": {
                "r_unacked": {"relayId": "r_unacked", "projectId": "p_act", "acked": False, "x": 1},
                "r_acked": {"relayId": "r_acked", "projectId": "p_done", "acked": True},
            },
            "receivedRelays": {},
            "projectBaselines": {"p1": {"sha": "deadbeef"}},
        }
        state_path = self.home / "relay_state.json"
        self.home.mkdir(parents=True, exist_ok=True)
        raw_text = json.dumps(raw_state, ensure_ascii=False, indent=2)
        state_path.write_text(raw_text, encoding="utf-8")
        backup_path = self.home / "relay_state.v2.backup.json"

        s1 = D.migrate_relay_state(raw_state, local_home=self.home)
        self.assertTrue(backup_path.exists())
        self.assertEqual(backup_path.read_text(encoding="utf-8"), raw_text)

        state_path.write_text(json.dumps(s1, ensure_ascii=False), encoding="utf-8")
        s2 = D.migrate_relay_state(s1, local_home=self.home)
        s3 = D.migrate_relay_state(s2, local_home=self.home)
        self.assertEqual(s1, s2)
        self.assertEqual(s2, s3)
        self.assertEqual(backup_path.read_text(encoding="utf-8"), raw_text)

        self.assertEqual(s3["version"], 3)
        self.assertEqual(s3["customStateTop"], [1, 2, 3])
        self.assertEqual(s3["projectBaselines"], {"p1": {"sha": "deadbeef"}})
        for v3_key in (
            "activeHandoffs",
            "versionGraph",
            "localCurrentVersion",
            "processedDeliveries",
            "archivedRelayIds",
            "forwardedSnapshots",
        ):
            self.assertIsInstance(s3[v3_key], dict)

        # pendingSends rules
        prep = s3["pendingSends"]["proj_prepared"]
        self.assertEqual(prep["protocolVersion"], 2)
        self.assertNotIn("verificationRequired", prep)
        self.assertEqual(prep["customPending"], "keep1")

        unk = s3["pendingSends"]["proj_unknown_pub"]
        self.assertEqual(unk["status"], "needs_verification")
        self.assertTrue(unk["verificationRequired"])
        self.assertEqual(unk["customPending"], "keep2")

        chk = s3["pendingSends"]["proj_checkpointed"]
        self.assertEqual(chk["status"], "uploading")
        self.assertNotIn("verificationRequired", chk)

        # appliedReceives rules
        app = s3["appliedReceives"]["r_done"]
        self.assertTrue(app["workspaceApplied"])
        self.assertTrue(app["migratedFromV2"])
        self.assertEqual(app["customApplied"], {"ok": True})

        # sentRelays -> activeHandoffs
        self.assertEqual(s3["activeHandoffs"], {"p_act": "r_unacked"})
        self.assertNotIn("p_done", s3["activeHandoffs"])


class TestMigrateLedgerIdentities(unittest.TestCase):
    def test_identity_mapping_needs_pairing_and_idempotency(self):
        ledger = {
            "app": "relaydesk",
            "version": 2,
            "topCustom": "ok",
            "config": {
                "devices": [
                    {"id": "a", "name": "Win", "os": "windows", "drop": "", "host": "win-h", "deviceId": "", "x": 1},
                    {"id": "b", "name": "Mac", "os": "mac", "drop": "", "host": "mac-h", "deviceId": "", "x": 2},
                    {"id": "c", "name": "UnknownPeer", "os": "linux", "drop": "", "host": "lnx-h", "deviceId": "", "x": 3},
                    {"id": "d", "name": "UnusedPeer", "os": "linux", "drop": "", "host": "", "deviceId": ""},
                ],
                "reg": {},
            },
            "projects": [
                {
                    "id": "p1",
                    "name": "Proj",
                    "device": "a",
                    "paths": {"a": "D:\\proj", "b": "/Users/mac/proj", "c": "/home/lnx/proj"},
                    "sessions": [
                        {"id": "s1", "device": "a", "extra": "s1"},
                        {"id": "s2", "device": "b", "extra": "s2"},
                        {"id": "s3", "device": "c", "extra": "s3"},
                    ],
                    "history": [
                        {"from": "a", "to": "b", "at": "2026-01-01", "hExtra": 1},
                        {"from": "b", "to": "c", "at": "2026-01-02", "hExtra": 2},
                    ],
                    "handoff": {"from": "a", "to": "c", "state": "prep", "hoExtra": True},
                }
            ],
        }
        local_ident = {"deviceId": "dev_local_a", "alias": "a", "host": "win-h"}
        cloud_cfg = {
            "version": 3,
            "peers": {
                "dev_peer_b": {"deviceId": "dev_peer_b", "alias": "b", "host": "mac-h"},
            },
        }

        m1 = D.migrate_ledger_identities(ledger, local_ident=local_ident, cloud_cfg=cloud_cfg)
        m2 = D.migrate_ledger_identities(m1, local_ident=local_ident, cloud_cfg=cloud_cfg)
        m3 = D.migrate_ledger_identities(m2, local_ident=local_ident, cloud_cfg=cloud_cfg)
        self.assertEqual(m1, m2)
        self.assertEqual(m2, m3)

        devs = {d["id"]: d for d in m3["config"]["devices"]}
        self.assertEqual(devs["a"]["deviceId"], "dev_local_a")
        self.assertNotIn("needsPairing", devs["a"])
        self.assertEqual(devs["b"]["deviceId"], "dev_peer_b")
        self.assertNotIn("needsPairing", devs["b"])
        # Unknown referenced device c is NOT guessed and gets needsPairing=True
        self.assertEqual(devs["c"]["deviceId"], "")
        self.assertTrue(devs["c"]["needsPairing"])
        # Unreferenced unknown device d stays untouched without needsPairing
        self.assertEqual(devs["d"]["deviceId"], "")
        self.assertNotIn("needsPairing", devs["d"])
        # Unknown fields kept
        self.assertEqual(devs["c"]["x"], 3)
        self.assertEqual(m3["topCustom"], "ok")

        proj = m3["projects"][0]
        self.assertEqual(
            proj["pathsByDeviceId"],
            {"dev_local_a": "D:\\proj", "dev_peer_b": "/Users/mac/proj"},
        )
        self.assertEqual(proj["sessions"][0]["deviceId"], "dev_local_a")
        self.assertEqual(proj["sessions"][1]["deviceId"], "dev_peer_b")
        self.assertNotIn("deviceId", proj["sessions"][2])
        self.assertEqual(proj["sessions"][2]["extra"], "s3")

        self.assertEqual(proj["history"][0]["fromDeviceId"], "dev_local_a")
        self.assertEqual(proj["history"][0]["toDeviceId"], "dev_peer_b")
        self.assertEqual(proj["history"][1]["fromDeviceId"], "dev_peer_b")
        self.assertNotIn("toDeviceId", proj["history"][1])
        self.assertEqual(proj["history"][1]["hExtra"], 2)

        self.assertEqual(proj["handoff"]["fromDeviceId"], "dev_local_a")
        self.assertNotIn("toDeviceId", proj["handoff"])
        self.assertTrue(proj["handoff"]["hoExtra"])

    def test_identity_mapping_with_legacy_v2_cloud_peer(self):
        ledger = {
            "config": {
                "devices": [
                    {"id": "a", "name": "A", "os": "windows", "deviceId": ""},
                    {"id": "b", "name": "B", "os": "mac", "deviceId": ""},
                ]
            },
            "projects": [{"id": "p1", "device": "a", "paths": {"a": "D:\\a", "b": "/b"}}],
        }
        legacy_cloud = {"version": 2, "peer": {"deviceId": "dev_legacy_b", "alias": "b"}}
        out = D.migrate_ledger_identities(
            ledger,
            local_ident={"deviceId": "dev_local_a", "alias": "a"},
            cloud_cfg=legacy_cloud,
        )
        devs = {d["id"]: d for d in out["config"]["devices"]}
        self.assertEqual(devs["a"]["deviceId"], "dev_local_a")
        self.assertEqual(devs["b"]["deviceId"], "dev_legacy_b")
        self.assertEqual(
            out["projects"][0]["pathsByDeviceId"],
            {"dev_local_a": "D:\\a", "dev_legacy_b": "/b"},
        )


if __name__ == "__main__":
    unittest.main()
