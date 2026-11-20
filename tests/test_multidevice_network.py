# -*- coding: utf-8 -*-
"""Comprehensive v3 multi-device network, conflict, recovery & fault-injection tests."""
import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from relaydesk import crypto
from relaydesk import ledger as L
from relaydesk import packager
from relaydesk import relay_engine
from relaydesk import scan
from relaydesk import storage
from relaydesk import workspace_tx


REPO_ROOT = str(Path(__file__).resolve().parent.parent)


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "core.quotepath=false",
            "-C",
            str(cwd),
        ]
        + list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError("git %r failed: %s" % (args, r.stderr))
    return r.stdout


def _git_bytes(cwd: Path, *args: str) -> bytes:
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(cwd)] + list(args),
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError("git %r failed" % (args,))
    return r.stdout


def _write_claude_session(user_home: Path, cwd: str, sid: str, title: str, turns) -> Path:
    proj_dir = user_home / ".claude" / "projects" / scan.enc_path(cwd)
    proj_dir.mkdir(parents=True, exist_ok=True)
    fp = proj_dir / ("%s.jsonl" % sid)
    lines = [{"type": "summary", "summary": title}]
    for role, text in turns:
        lines.append({"type": role, "message": {"role": role, "content": text}, "timestamp": "2026-11-18T01:00:00Z"})
    fp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    return fp


def _write_codex_session(user_home: Path, cwd: str, sid: str, title: str, turns) -> Path:
    sess_dir = user_home / ".codex" / "sessions" / "2026" / "11" / "18"
    sess_dir.mkdir(parents=True, exist_ok=True)
    idx = user_home / ".codex" / "session_index.jsonl"
    with open(str(idx), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": sid, "thread_name": title}, ensure_ascii=False) + "\n")
    fp = sess_dir / ("rollout-2026-11-18T01-00-00-%s.jsonl" % sid)
    lines = [{"type": "session_meta", "payload": {"id": sid, "cwd": cwd}}]
    for role, text in turns:
        ctype = "input_text" if role == "user" else "output_text"
        lines.append(
            {
                "timestamp": "2026-11-18T01:05:00Z",
                "type": "response_item",
                "payload": {"type": "message", "role": role, "content": [{"type": ctype, "text": text}]},
            }
        )
    fp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    return fp


def _write_anti_session(user_home: Path, cwd: str, sid: str, title: str, turns) -> Path:
    logs_dir = user_home / ".gemini" / "antigravity" / "brain" / sid / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    fp = logs_dir / "transcript_full.jsonl"
    steps = [{"step_index": 0, "type": "META", "title": title, "workspacePath": cwd}]
    for i, (role, text) in enumerate(turns, 1):
        if role == "user":
            steps.append(
                {
                    "step_index": i,
                    "type": "USER_INPUT",
                    "created_at": "2026-11-18T01:10:00Z",
                    "content": "<USER_REQUEST>\n%s\n</USER_REQUEST>" % text,
                }
            )
        else:
            steps.append(
                {
                    "step_index": i,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-18T01:10:05Z",
                    "thinking": "internal chain of thought",
                    "content": text,
                }
            )
    fp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in steps) + "\n", encoding="utf-8")
    return fp


class MultiDeviceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cloud_dir = self.root / "cloud_shared"
        self.cloud_dir.mkdir(parents=True, exist_ok=True)
        self.devices: Dict[str, Dict[str, Path]] = {}

    def init_device(self, alias: str, name: Optional[str] = None, os_name: str = "mac") -> Dict[str, Path]:
        home = self.root / ("home_%s" % alias)
        uhome = self.root / ("uhome_%s" % alias)
        led = self.root / ("ledger_%s.json" % alias)
        home.mkdir(parents=True, exist_ok=True)
        uhome.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(
            json.dumps({"device": alias, "ledger": str(led)}),
            encoding="utf-8",
        )

        def _init_led(l: Dict[str, Any]) -> None:
            l["config"] = L.norm_config(
                {
                    "devices": [
                        {
                            "id": alias,
                            "name": name or ("设备 %s" % alias),
                            "os": os_name,
                            "drop": "",
                            "host": "",
                            "deviceId": "",
                        }
                    ]
                }
            )

        L.update(led, _init_led)
        rec = {"home": home, "uhome": uhome, "led": led}
        self.devices[alias] = rec
        return rec

    def env(self, alias: str, extra: Optional[Dict[str, str]] = None):
        rec = self.devices[alias]
        env_map = {
            "RELAY_DESK_HOME": str(rec["home"]),
            "RELAY_DESK_LEDGER": str(rec["led"]),
            "RELAY_DESK_DEVICE": alias,
            "RELAY_DESK_USER_HOME": str(rec["uhome"]),
        }
        if extra:
            env_map.update(extra)
        return mock.patch.dict(os.environ, env_map)

    def publish_descriptor(self, alias: str) -> Dict[str, Any]:
        with self.env(alias):
            return relay_engine.pair_device(
                root=str(self.cloud_dir),
                provider_type="local",
                accept=False,
            )

    def pair_pair(self, alias_a: str, alias_b: str) -> Dict[str, Dict[str, Any]]:
        st_a0 = self.publish_descriptor(alias_a)
        st_b0 = self.publish_descriptor(alias_b)
        did_a = st_a0["deviceId"]
        did_b = st_b0["deviceId"]

        with self.env(alias_a):
            view_a = relay_engine.pair_device(with_device=did_b, accept=False)
            peer_b_view = next(p for p in view_a["peers"] if p["deviceId"] == did_b)
            fp_ab = peer_b_view["pairFingerprint"]
            res_a = relay_engine.pair_device(with_device=did_b, accept=True, fingerprint=fp_ab)
            self.assertTrue(any(p["deviceId"] == did_b and p["paired"] for p in res_a["peers"]))

        with self.env(alias_b):
            view_b = relay_engine.pair_device(with_device=did_a, accept=False)
            peer_a_view = next(p for p in view_b["peers"] if p["deviceId"] == did_a)
            fp_ba = peer_a_view["pairFingerprint"]
            self.assertEqual(fp_ab, fp_ba)
            res_b = relay_engine.pair_device(with_device=did_a, accept=True, fingerprint=fp_ba)
            self.assertTrue(any(p["deviceId"] == did_a and p["paired"] for p in res_b["peers"]))

        return {alias_a: res_a, alias_b: res_b}

    def create_git_project(self, alias: str, proj_id: str = "demo", name: str = "多机测试项目") -> Path:
        repo = self.root / ("repo_%s_%s" % (alias, proj_id))
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-b", "main")
        (repo / "README.md").write_text("# %s\n" % name, encoding="utf-8")
        (repo / "keep.txt").write_text("base\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "initial commit")

        def _reg(l: Dict[str, Any]) -> None:
            l["projects"].append(
                L.norm_project(
                    {
                        "id": proj_id,
                        "name": name,
                        "device": alias,
                        "tool": "all",
                        "lastTask": "初始化任务",
                        "nextAction": "继续开发",
                        "note": "多机接力",
                        "verify": {"state": "pass", "cmd": "python3 -m unittest", "note": "ok"},
                        "paths": {alias: str(repo)},
                    },
                    l["config"],
                )
            )

        L.update(self.devices[alias]["led"], _reg)
        return repo


class TestThreeDeviceRingAndSyncNetwork(MultiDeviceTestBase):
    def test_three_device_ring_alpha_beta_gamma_alpha(self) -> None:
        self.init_device("alpha", name="Alpha Work")
        self.init_device("beta", name="Beta Laptop")
        self.init_device("gamma", name="Gamma Server")

        self.pair_pair("alpha", "beta")
        self.pair_pair("beta", "gamma")
        self.pair_pair("gamma", "alpha")

        repo_alpha = self.create_git_project("alpha", proj_id="ring", name="三机环形项目")

        # Alpha prepares staged + unstaged + untracked + .env + 3 tool sessions
        (repo_alpha / "staged.txt").write_text("alpha staged\n", encoding="utf-8")
        _git(repo_alpha, "add", "staged.txt")
        (repo_alpha / "README.md").write_text("# alpha unstaged edit\n", encoding="utf-8")
        (repo_alpha / "untracked_a.txt").write_text("alpha untracked\n", encoding="utf-8")
        (repo_alpha / ".env").write_text("ALPHA_SECRET=keep_alpha\n", encoding="utf-8")

        uuid_cx = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        _write_claude_session(self.devices["alpha"]["uhome"], str(repo_alpha), "cl_a1", "Alpha Claude", [("user", "start"), ("assistant", "ready")])
        _write_codex_session(self.devices["alpha"]["uhome"], str(repo_alpha), uuid_cx, "Alpha Codex", [("user", "check"), ("assistant", "checked")])
        _write_anti_session(self.devices["alpha"]["uhome"], str(repo_alpha), "anti_a1", "Alpha Anti", [("user", "plan"), ("assistant", "planned")])

        head_1 = _git(repo_alpha, "rev-parse", "HEAD").strip()
        cached_1 = _git_bytes(repo_alpha, "diff", "--cached", "--binary")
        unstaged_1 = _git_bytes(repo_alpha, "diff", "--binary")

        with self.env("alpha"):
            s1 = relay_engine.send_project("ring", to_alias="beta")
        self.assertTrue(s1["ok"])
        self.assertEqual(s1["to"], "beta")
        self.assertEqual(s1["protocolVersion"], 3)
        self.assertIn(".env", s1["excludedFiles"])
        rid_1 = s1["relayId"]
        snap_1 = s1["snapshotId"]

        # Beta receives R1, makes commit + dirty edits + local .env + session, sends to Gamma
        repo_beta = self.root / "repo_beta_ring"
        with self.env("beta"):
            in_b = relay_engine.list_inbox()
            self.assertEqual([x["relayId"] for x in in_b["incoming"]], [rid_1])
            r1 = relay_engine.receive_relay(rid_1, target_path=str(repo_beta))
            self.assertTrue(r1["ok"])
            self.assertEqual(_git(repo_beta, "rev-parse", "HEAD").strip(), head_1)
            self.assertEqual(_git_bytes(repo_beta, "diff", "--cached", "--binary"), cached_1)
            self.assertEqual(_git_bytes(repo_beta, "diff", "--binary"), unstaged_1)
            self.assertEqual((repo_beta / "untracked_a.txt").read_text(encoding="utf-8"), "alpha untracked\n")
            self.assertFalse((repo_beta / ".env").exists())
            self.assertEqual(len(relay_engine.query_transcripts("ring")["sessions"]), 3)

            _git(repo_beta, "add", "-A")
            _git(repo_beta, "commit", "-m", "beta commit")
            (repo_beta / ".env").write_text("BETA_SECRET=keep_beta\n", encoding="utf-8")
            (repo_beta / "beta_staged.txt").write_text("from beta staged\n", encoding="utf-8")
            _git(repo_beta, "add", "beta_staged.txt")
            (repo_beta / "keep.txt").write_text("beta unstaged\n", encoding="utf-8")
            (repo_beta / "beta_untracked.txt").write_text("beta untracked\n", encoding="utf-8")
            _write_claude_session(self.devices["beta"]["uhome"], str(repo_beta), "cl_b2", "Beta Claude", [("user", "continue"), ("assistant", "done on beta")])

            s2 = relay_engine.send_project("ring", to_alias="gamma")
            self.assertEqual(s2["parentRelayId"], rid_1)
        rid_2 = s2["relayId"]
        snap_2 = s2["snapshotId"]
        head_2 = _git(repo_beta, "rev-parse", "HEAD").strip()
        cached_2 = _git_bytes(repo_beta, "diff", "--cached", "--binary")
        unstaged_2 = _git_bytes(repo_beta, "diff", "--binary")

        # Gamma receives R2, makes commit + edits + local .env + session, sends to Alpha
        repo_gamma = self.root / "repo_gamma_ring"
        with self.env("gamma"):
            in_g = relay_engine.list_inbox()
            self.assertEqual([x["relayId"] for x in in_g["incoming"]], [rid_2])
            r2 = relay_engine.receive_relay(rid_2, target_path=str(repo_gamma))
            self.assertTrue(r2["ok"])
            self.assertEqual(_git(repo_gamma, "rev-parse", "HEAD").strip(), head_2)
            self.assertEqual(_git_bytes(repo_gamma, "diff", "--cached", "--binary"), cached_2)
            self.assertEqual(_git_bytes(repo_gamma, "diff", "--binary"), unstaged_2)
            self.assertEqual((repo_gamma / "beta_untracked.txt").read_text(encoding="utf-8"), "beta untracked\n")
            self.assertFalse((repo_gamma / ".env").exists())
            self.assertEqual(len(relay_engine.query_transcripts("ring")["sessions"]), 4)

            _git(repo_gamma, "add", "-A")
            _git(repo_gamma, "commit", "-m", "gamma commit")
            (repo_gamma / ".env").write_text("GAMMA_SECRET=keep_gamma\n", encoding="utf-8")
            (repo_gamma / "gamma_only.txt").write_text("gamma file\n", encoding="utf-8")
            _write_claude_session(self.devices["gamma"]["uhome"], str(repo_gamma), "cl_g3", "Gamma Claude", [("user", "polish"), ("assistant", "ready for alpha")])

            s3 = relay_engine.send_project("ring", to_alias="alpha")
            self.assertEqual(s3["parentRelayId"], rid_2)
        rid_3 = s3["relayId"]
        snap_3 = s3["snapshotId"]
        head_3 = _git(repo_gamma, "rev-parse", "HEAD").strip()

        # Alpha syncs R1 receipt and receives R3 directly; ancestry is fetched automatically.
        with self.env("alpha"):
            in_a = relay_engine.list_inbox()
            self.assertEqual([r["relayId"] for r in in_a["syncedReceipts"]], [rid_1])
            self.assertEqual([x["relayId"] for x in in_a["incoming"]], [rid_3])

            r3 = relay_engine.receive_relay(rid_3)
            self.assertTrue(r3["ok"])
            self.assertEqual(_git(repo_alpha, "rev-parse", "HEAD").strip(), head_3)
            self.assertEqual((repo_alpha / "gamma_only.txt").read_text(encoding="utf-8"), "gamma file\n")
            self.assertEqual((repo_alpha / ".env").read_text(encoding="utf-8"), "ALPHA_SECRET=keep_alpha\n")

            tr_a = relay_engine.query_transcripts("ring")
            self.assertEqual(
                sorted(s["sessionId"] for s in tr_a["sessions"]),
                sorted(["cl_a1", uuid_cx, "anti_a1", "cl_b2", "cl_g3"]),
            )

        # Beta and Gamma run sync_network to pull remaining snapshot nodes into their versionGraph
        with self.env("beta"):
            relay_engine.sync_network()
        with self.env("gamma"):
            relay_engine.sync_network()

        # Verify versionGraph lineage across all 3 devices
        st_alpha = json.loads((self.devices["alpha"]["home"] / "relay_state.json").read_text(encoding="utf-8"))
        st_beta = json.loads((self.devices["beta"]["home"] / "relay_state.json").read_text(encoding="utf-8"))
        st_gamma = json.loads((self.devices["gamma"]["home"] / "relay_state.json").read_text(encoding="utf-8"))
        for st_obj in (st_alpha, st_beta, st_gamma):
            nodes = st_obj["versionGraph"]["ring"]["nodes"]
            self.assertEqual(nodes[snap_1]["parentVersionIds"], [])
            self.assertEqual(nodes[snap_2]["parentVersionIds"], [snap_1])
            self.assertEqual(nodes[snap_3]["parentVersionIds"], [snap_2])
        self.assertEqual(st_alpha["localCurrentVersion"]["ring"], snap_3)
        self.assertEqual(st_beta["localCurrentVersion"]["ring"], snap_2)
        self.assertEqual(st_gamma["localCurrentVersion"]["ring"], snap_3)

    def test_publish_sync_cache_vs_ownership_and_offline_forwarding(self) -> None:
        self.init_device("alpha")
        self.init_device("beta")
        self.init_device("gamma")

        # Initially alpha is ONLY paired with beta; beta is paired with gamma
        pair_ab = self.pair_pair("alpha", "beta")
        self.pair_pair("beta", "gamma")
        dev_alpha = pair_ab["alpha"]["deviceId"]
        dev_beta = pair_ab["beta"]["deviceId"]

        repo_alpha = self.create_git_project("alpha", proj_id="syncproj", name="同步缓存项目")
        (repo_alpha / "feature.py").write_text("print('s1')\n", encoding="utf-8")
        _git(repo_alpha, "add", "feature.py")
        _git(repo_alpha, "commit", "-m", "feat s1")

        # Alpha publishes S1 when paired ONLY with beta
        with self.env("alpha"):
            pub = relay_engine.publish_project("syncproj")
        self.assertTrue(pub["ok"])
        s1_id = pub["snapshotId"]
        self.assertEqual([d["alias"] for d in pub["deliveredTo"]], ["beta"])

        # Ownership on alpha remains alpha, no active handoff created
        led_a = L.load(self.devices["alpha"]["led"])
        self.assertEqual(led_a["projects"][0]["device"], "alpha")
        self.assertIsNone(led_a["projects"][0]["handoff"])

        # Alpha and gamma pair so gamma pins alpha's verification public key, then alpha goes completely offline
        self.pair_pair("alpha", "gamma")
        shutil.rmtree(self.devices["alpha"]["home"])
        self.devices["alpha"]["led"].unlink()

        # Beta syncs: caches S1 without creating workspace/claiming ownership, AND forwards S1 to gamma
        with self.env("beta"):
            sync_b = relay_engine.sync_network()
        self.assertEqual([x["snapshotId"] for x in sync_b["syncedSnapshots"]], [s1_id])
        self.assertEqual(len(sync_b["forwarded"]), 1)
        self.assertEqual(sync_b["forwarded"][0]["snapshotId"], s1_id)
        self.assertEqual(sync_b["forwarded"][0]["toAlias"], "gamma")
        self.assertEqual(L.load(self.devices["beta"]["led"])["projects"], [])
        self.assertFalse((self.root / "repo_beta_syncproj").exists())
        self.assertTrue((self.devices["beta"]["home"] / "cache" / "snapshots" / s1_id / "payload.tar.gz").is_file())

        # Gamma syncs: receives S1 forwarded by beta, verifies original author signature is alpha
        with self.env("gamma"):
            sync_g = relay_engine.sync_network()
        self.assertEqual(len(sync_g["syncedSnapshots"]), 1)
        snap_rec = sync_g["syncedSnapshots"][0]
        self.assertEqual(snap_rec["snapshotId"], s1_id)
        self.assertEqual(snap_rec["authorDeviceId"], dev_alpha)
        self.assertEqual(snap_rec["senderDeviceId"], dev_beta)
        self.assertTrue(snap_rec["isForwarded"])
        self.assertEqual(L.load(self.devices["gamma"]["led"])["projects"], [])

        cached_snap_g = json.loads(
            (self.devices["gamma"]["home"] / "cache" / "snapshots" / s1_id / "snapshot.json").read_text(encoding="utf-8")
        )
        self.assertEqual(cached_snap_g["authorDeviceId"], dev_alpha)
        self.assertEqual(cached_snap_g["signerDeviceId"], dev_alpha)
        self.assertEqual(cached_snap_g["contentSha256"], pub["contentSha256"])

        # Re-running sync on beta must not duplicate-forward S1
        with self.env("beta"):
            sync_b2 = relay_engine.sync_network()
        self.assertEqual(sync_b2["forwarded"], [])

    def test_non_target_device_cannot_claim_handoff(self) -> None:
        self.init_device("alpha")
        self.init_device("beta")
        self.init_device("gamma")
        self.pair_pair("alpha", "beta")
        self.pair_pair("beta", "gamma")
        self.pair_pair("alpha", "gamma")

        self.create_git_project("alpha", proj_id="authproj", name="授权隔离项目")
        with self.env("alpha"):
            sent = relay_engine.send_project("authproj", to_alias="beta")
        rid = sent["relayId"]
        snap_id = sent["snapshotId"]

        # Both beta and gamma sync (gamma caches the exact snapshot version!)
        with self.env("beta"):
            relay_engine.sync_network()
        with self.env("gamma"):
            sync_g = relay_engine.sync_network()
            self.assertIn(snap_id, [x["snapshotId"] for x in sync_g["syncedSnapshots"]])
            self.assertTrue((self.devices["gamma"]["home"] / "cache" / "snapshots" / snap_id / "payload.tar.gz").is_file())

            # Gamma's inbox MUST NOT show Beta's handoff
            self.assertEqual(relay_engine.list_inbox()["incoming"], [])

            # Gamma attempting to claim either relayId or cached snap_id MUST fail with UnauthorizedReceiverError
            steal_dir = self.root / "gamma_steal"
            with self.assertRaises(relay_engine.UnauthorizedReceiverError) as ctx_r:
                relay_engine.receive_relay(rid, target_path=str(steal_dir))
            self.assertEqual(ctx_r.exception.code, "unauthorized_receiver")
            self.assertFalse(steal_dir.exists())

            with self.assertRaises(relay_engine.UnauthorizedReceiverError) as ctx_s:
                relay_engine.receive_relay(snap_id, target_path=str(steal_dir))
            self.assertEqual(ctx_s.exception.code, "unauthorized_receiver")
            self.assertFalse(steal_dir.exists())

        # Beta claims normally
        repo_beta = self.root / "repo_beta_auth"
        with self.env("beta"):
            self.assertTrue(relay_engine.receive_relay(rid, target_path=str(repo_beta))["ok"])


class TestPeerConflictsAndKeyRotationIsolation(MultiDeviceTestBase):
    def test_ambiguity_key_rotation_isolation_and_alias_collision(self) -> None:
        self.init_device("alpha")
        self.init_device("beta")
        self.init_device("gamma")
        pair_ab = self.pair_pair("alpha", "beta")
        self.pair_pair("alpha", "gamma")
        self.pair_pair("beta", "gamma")
        did_beta = pair_ab["beta"]["deviceId"]

        repo_alpha = self.create_git_project("alpha", proj_id="iso", name="多端隔离项目")

        # 1. Ambiguous send without to_alias when multiple peers are paired
        with self.env("alpha"):
            with self.assertRaises(relay_engine.AmbiguousPeerError) as ctx_amb:
                relay_engine.send_project("iso")
            cand_aliases = {c["alias"] for c in ctx_amb.exception.candidates}
            self.assertEqual(cand_aliases, {"beta", "gamma"})

        # 2. Beta rotates keys -> Alpha pauses ONLY beta; alpha <-> gamma continues working!
        crypto.rotate_identity_keys(self.devices["beta"]["home"])
        self.publish_descriptor("beta")

        with self.env("alpha"):
            st_a = relay_engine.pair_device(accept=False)
            b_view = next(p for p in st_a["peers"] if p["deviceId"] == did_beta)
            self.assertEqual(b_view["status"], "key_changed")
            self.assertFalse(b_view["paired"])
            new_fp = b_view["pairFingerprint"]

            with self.assertRaises(relay_engine.KeyChangedError):
                relay_engine.send_project("iso", to_alias="beta")

            # Alpha -> Gamma works uninterrupted!
            s_ag = relay_engine.send_project("iso", to_alias="gamma")

        repo_gamma = self.root / "repo_gamma_iso"
        with self.env("gamma"):
            self.assertTrue(relay_engine.receive_relay(s_ag["relayId"], target_path=str(repo_gamma))["ok"])
            (repo_gamma / "gamma_edit.txt").write_text("from gamma\n", encoding="utf-8")
            s_ga = relay_engine.send_project("iso", to_alias="alpha")

        with self.env("alpha"):
            self.assertTrue(relay_engine.receive_relay(s_ga["relayId"])["ok"])
            self.assertEqual((repo_alpha / "gamma_edit.txt").read_text(encoding="utf-8"), "from gamma\n")

            # Re-accept Beta with new fingerprint -> Alpha -> Beta resumes!
            re_pair = relay_engine.pair_device(with_device="beta", accept=True, fingerprint=new_fp)
            b_view2 = next(p for p in re_pair["peers"] if p["deviceId"] == did_beta)
            self.assertTrue(b_view2["paired"])
            self.assertEqual(b_view2["status"], "paired")
            s_ab = relay_engine.send_project("iso", to_alias="beta")

        with self.env("beta"):
            self.assertTrue(relay_engine.receive_relay(s_ab["relayId"], target_path=str(self.root / "repo_beta_iso"))["ok"])

        # 3. 4th device claiming existing alias 'beta' with a different deviceId raises PeerConflictError
        impostor_home = self.root / "home_impostor"
        impostor_uhome = self.root / "uhome_impostor"
        impostor_led = self.root / "ledger_impostor.json"
        impostor_home.mkdir(parents=True, exist_ok=True)
        impostor_uhome.mkdir(parents=True, exist_ok=True)
        (impostor_home / "config.json").write_text(
            json.dumps({"device": "beta", "ledger": str(impostor_led)}),
            encoding="utf-8",
        )
        L.update(
            impostor_led,
            lambda l: l.__setitem__(
                "config",
                L.norm_config({"devices": [{"id": "beta", "name": "Impostor Beta", "os": "linux"}]}),
            ),
        )
        with mock.patch.dict(
            os.environ,
            {
                "RELAY_DESK_HOME": str(impostor_home),
                "RELAY_DESK_LEDGER": str(impostor_led),
                "RELAY_DESK_DEVICE": "beta",
                "RELAY_DESK_USER_HOME": str(impostor_uhome),
            },
        ):
            relay_engine.pair_device(root=str(self.cloud_dir), provider_type="local", accept=False)

        with self.env("alpha"):
            with self.assertRaises(relay_engine.PeerConflictError) as ctx_pc:
                relay_engine.pair_device(accept=False)
            self.assertEqual(ctx_pc.exception.code, "alias_conflict")


class TestVersionGraphConflictsAndLateReceipts(MultiDeviceTestBase):
    def test_clock_skew_immunity_accepts_backdated_child_snapshot(self) -> None:
        self.init_device("alpha")
        self.init_device("beta")
        self.pair_pair("alpha", "beta")
        repo_a = self.create_git_project("alpha", proj_id="skew", name="时钟回拨测试")

        with self.env("alpha"):
            s1 = relay_engine.send_project("skew", to_alias="beta")
        repo_b = self.root / "repo_b_skew"
        with self.env("beta"):
            relay_engine.receive_relay(s1["relayId"], target_path=str(repo_b))
            (repo_b / "child.txt").write_text("newer lineage despite 1999 clock\n", encoding="utf-8")
            with mock.patch.object(L, "now_iso", return_value="1999-01-01T00:00:00Z"):
                s2 = relay_engine.send_project("skew", to_alias="alpha")

        with self.env("alpha"):
            r2 = relay_engine.receive_relay(s2["relayId"])
            self.assertTrue(r2["ok"])
            self.assertEqual((repo_a / "child.txt").read_text(encoding="utf-8"), "newer lineage despite 1999 clock\n")

    def test_offline_fork_detection_and_into_path_materialization(self) -> None:
        self.init_device("alpha")
        self.init_device("beta")
        self.pair_pair("alpha", "beta")
        repo_a = self.create_git_project("alpha", proj_id="forkproj", name="离线分叉测试")

        with self.env("alpha"):
            s1 = relay_engine.send_project("forkproj", to_alias="beta")
        v1 = s1["snapshotId"]

        repo_b = self.root / "repo_b_fork"
        with self.env("beta"):
            relay_engine.receive_relay(s1["relayId"], target_path=str(repo_b))

        # Alpha syncs R1 receipt, then simulates offline concurrent work starting from V1
        with self.env("alpha"):
            relay_engine.list_inbox()
            L.update(self.devices["alpha"]["led"], lambda l: l["projects"][0].__setitem__("device", "alpha"))
            (repo_a / "alpha_branch.txt").write_text("alpha diverged\n", encoding="utf-8")
            _git(repo_a, "add", "alpha_branch.txt")
            _git(repo_a, "commit", "-m", "alpha fork from V1")
            pub_a = relay_engine.publish_project("forkproj")
            v2_a = pub_a["snapshotId"]

        # Beta concurrently creates a diverging snapshot from V1 and sends to Alpha
        (repo_b / "beta_branch.txt").write_text("beta diverged\n", encoding="utf-8")
        _git(repo_b, "add", "beta_branch.txt")
        _git(repo_b, "commit", "-m", "beta fork from V1")
        with self.env("beta"):
            s_fork = relay_engine.send_project("forkproj", to_alias="alpha")
        rid_fork = s_fork["relayId"]
        v2_b = s_fork["snapshotId"]

        # Alpha runs sync_network -> detects fork, preserves both heads, appends ledger conflict
        with self.env("alpha"):
            sync_a = relay_engine.sync_network()
            self.assertTrue(len(sync_a["conflictsDetected"]) >= 1)
            st_a = json.loads((self.devices["alpha"]["home"] / "relay_state.json").read_text(encoding="utf-8"))
            heads = set(st_a["versionGraph"]["forkproj"]["heads"])
            self.assertEqual(heads, {v2_a, v2_b})
            led_a = L.load(self.devices["alpha"]["led"])
            self.assertTrue(len(led_a["projects"][0].get("conflicts") or []) >= 1)

            # In-place receive MUST refuse with VersionConflictError (version_fork)
            with self.assertRaises(relay_engine.VersionConflictError) as ctx_vc:
                relay_engine.receive_relay(rid_fork)
            self.assertEqual(ctx_vc.exception.code, "version_fork")
            self.assertEqual(ctx_vc.exception.local_version, v2_a)
            self.assertEqual(ctx_vc.exception.incoming_version, v2_b)
            self.assertFalse((repo_a / "beta_branch.txt").exists())

            # Materializing into a separate directory with --into succeeds!
            fork_into = self.root / "repo_alpha_fork_into"
            res_into = relay_engine.receive_relay(rid_fork, into_path=str(fork_into))
            self.assertTrue(res_into["ok"])
            self.assertTrue((fork_into / "beta_branch.txt").is_file())
            self.assertFalse((repo_a / "beta_branch.txt").exists())
            self.assertTrue((repo_a / "alpha_branch.txt").is_file())

    def test_single_active_handoff_idempotency_and_conflict(self) -> None:
        self.init_device("alpha")
        self.init_device("beta")
        self.init_device("gamma")
        self.pair_pair("alpha", "beta")
        self.pair_pair("alpha", "gamma")
        repo_a = self.create_git_project("alpha", proj_id="active", name="在途唯一性")

        with self.env("alpha"):
            r1 = relay_engine.send_project("active", to_alias="beta")
            # Edit README.md locally after handoff is already in transit
            (repo_a / "README.md").write_text("# post-send local edit\n", encoding="utf-8")

            # Re-sending to same target returns identical relayId & packageSha256 without repackaging
            r1_again = relay_engine.send_project("active", to_alias="beta")
            self.assertEqual(r1_again["relayId"], r1["relayId"])
            self.assertEqual(r1_again["packageSha256"], r1["packageSha256"])

            # Sending to a different target while R1 is unacked raises ActiveHandoffConflictError
            with self.assertRaises(relay_engine.ActiveHandoffConflictError) as ctx_ah:
                relay_engine.send_project("active", to_alias="gamma")
            self.assertEqual(ctx_ah.exception.code, "active_handoff_exists")
            self.assertEqual(ctx_ah.exception.relay_id, r1["relayId"])

    def test_late_receipt_does_not_revert_ownership_or_clear_newer_handoff(self) -> None:
        self.init_device("alpha")
        self.init_device("beta")
        self.init_device("gamma")
        self.pair_pair("alpha", "beta")
        self.pair_pair("beta", "gamma")
        self.pair_pair("gamma", "alpha")
        self.create_git_project("alpha", proj_id="late", name="迟到回执测试")

        class FailOnReceiptProvider(storage.LocalDirProvider):
            def upload(self, local_path: str, remote_key: str):
                if remote_key.endswith("/receipt.json"):
                    raise storage.StorageError("delayed receipt")
                return super().upload(local_path, remote_key)

        # 1. Alpha sends R1 to Beta
        with self.env("alpha"):
            r1 = relay_engine.send_project("late", to_alias="beta")["relayId"]

        # 2. Beta receives R1, but receipt upload is delayed (fails before receipt.json reaches cloud)
        repo_b = self.root / "repo_b_late"
        with self.env("beta"):
            with mock.patch.object(storage, "make_provider", side_effect=lambda cfg: FailOnReceiptProvider(cfg["root"])):
                with self.assertRaises(relay_engine.ReceiptUploadError):
                    relay_engine.receive_relay(r1, target_path=str(repo_b))

            # Project moves on: Beta -> Gamma -> Alpha -> Gamma (R3 active handoff!)
            (repo_b / "step2.txt").write_text("from beta\n", encoding="utf-8")
            r_bg = relay_engine.send_project("late", to_alias="gamma")["relayId"]

        repo_g = self.root / "repo_g_late"
        with self.env("gamma"):
            relay_engine.receive_relay(r_bg, target_path=str(repo_g))
            (repo_g / "step3.txt").write_text("from gamma\n", encoding="utf-8")
            r_ga = relay_engine.send_project("late", to_alias="alpha")["relayId"]

        with self.env("alpha"):
            relay_engine.sync_network()
            relay_engine.receive_relay(r_ga)
            r2 = relay_engine.send_project("late", to_alias="gamma")["relayId"]

        # 3. Now Beta finally uploads delayed R1 receipt!
        with self.env("beta"):
            rec_r1 = relay_engine.receive_relay(r1)
            self.assertTrue(rec_r1["ok"])

        # 4. Alpha syncs receipts: R1 is marked acked & added to history, WITHOUT reverting device or clearing R2!
        with self.env("alpha"):
            inbox_a = relay_engine.list_inbox()
            r1_synced = next(r for r in inbox_a["syncedReceipts"] if r["relayId"] == r1)
            self.assertTrue(r1_synced["late"])

            led_a = L.load(self.devices["alpha"]["led"])["projects"][0]
            self.assertEqual(led_a["device"], "alpha")
            self.assertIsNotNone(led_a["handoff"])
            self.assertEqual(led_a["handoff"]["relayId"], r2)
            self.assertEqual(led_a["handoff"]["to"], "gamma")
            self.assertIn(r1, [h["relayId"] for h in led_a["history"]])

            st_a = json.loads((self.devices["alpha"]["home"] / "relay_state.json").read_text(encoding="utf-8"))
            self.assertTrue(st_a["sentRelays"][r1]["acked"])
            self.assertEqual(st_a["activeHandoffs"]["late"], r2)

    def test_more_than_ten_handoffs_still_rejects_replay_of_first_version(self) -> None:
        self.init_device("alpha")
        self.init_device("beta")
        pair_ab = self.pair_pair("alpha", "beta")
        did_beta = pair_ab["beta"]["deviceId"]
        repo_a = self.create_git_project("alpha", proj_id="long12", name="超过10条历史防重放")
        repo_b = self.root / "repo_b_long12"

        rids: List[str] = []
        for hop in range(1, 13):
            sender = "alpha" if hop % 2 == 1 else "beta"
            receiver = "beta" if sender == "alpha" else "alpha"
            sender_repo = repo_a if sender == "alpha" else repo_b
            (sender_repo / "hop.txt").write_text("hop %d\n" % hop, encoding="utf-8")
            _git(sender_repo, "add", "hop.txt")
            _git(sender_repo, "commit", "-m", "hop %d" % hop)
            with self.env(sender):
                s = relay_engine.send_project("long12", to_alias=receiver)
                rid = s["relayId"]
                rids.append(rid)
            with self.env(receiver):
                if hop == 1:
                    relay_engine.receive_relay(rid, target_path=str(repo_b))
                else:
                    relay_engine.receive_relay(rid)

        # Verify display history is truncated to 10 entries (R1 dropped from history!)
        with self.env("beta"):
            hist_ids = [h["relayId"] for h in L.load(self.devices["beta"]["led"])["projects"][0]["history"]]
            self.assertEqual(len(hist_ids), 10)
            self.assertNotIn(rids[0], hist_ids)

            # Normal replay of R1 raises DuplicateReceiveError
            with self.assertRaises(relay_engine.DuplicateReceiveError):
                relay_engine.receive_relay(rids[0])

            # Even if R1 receipt, receivedRelays entry, and tx dir were cleared,
            # persistent versionGraph STILL rejects R1 as version_regression!
            receipt_1 = self.cloud_dir / "v3" / "inbox" / did_beta / rids[0] / "receipt.json"
            receipt_1.unlink()
            tx_1 = self.devices["beta"]["home"] / "transactions" / rids[0]
            if tx_1.exists():
                shutil.rmtree(tx_1)
            relay_engine._update_relay_state(
                self.devices["beta"]["home"],
                lambda st: st.get("receivedRelays", {}).pop(rids[0], None),
            )
            with self.assertRaises(relay_engine.VersionConflictError) as ctx_reg:
                relay_engine.receive_relay(rids[0])
            self.assertEqual(ctx_reg.exception.code, "version_regression")

    def test_incoming_handoff_matching_local_current_version_rejected_without_prior_receipt(self) -> None:
        self.init_device("alpha")
        self.init_device("beta")
        pair_ab = self.pair_pair("alpha", "beta")
        did_beta = pair_ab["beta"]["deviceId"]
        self.create_git_project("alpha", proj_id="samever", name="同版本重放拒绝")
        repo_b = self.root / "repo_b_samever"

        with self.env("alpha"):
            r1 = relay_engine.send_project("samever", to_alias="beta")["relayId"]
            alpha_ident = crypto.ensure_identity(self.devices["alpha"]["home"])
        with self.env("beta"):
            relay_engine.receive_relay(r1, target_path=str(repo_b))
            st_b = json.loads((self.devices["beta"]["home"] / "relay_state.json").read_text(encoding="utf-8"))
            local_ver = st_b["localCurrentVersion"]["samever"]

        # Forge/deliver a brand-new relayId in beta's inbox carrying the exact same snapshot.versionId
        src_dir = self.cloud_dir / "v3" / "inbox" / did_beta / r1
        r_dup_ver = r1 + "_dupver"
        dst_dir = self.cloud_dir / "v3" / "inbox" / did_beta / r_dup_ver
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_dir / "package.enc"), str(dst_dir / "package.enc"))

        raw_ready = json.loads((src_dir / "ready.json").read_text(encoding="utf-8"))
        self.assertEqual(raw_ready["snapshot"]["versionId"], local_ver)
        unsigned_ready = {k: v for k, v in raw_ready.items() if k not in ("signature", "signerDeviceId")}
        unsigned_ready["relayId"] = r_dup_ver
        unsigned_ready["packageKey"] = "v3/inbox/%s/%s/package.enc" % (did_beta, r_dup_ver)
        signed_ready = crypto.sign_json(unsigned_ready, alpha_ident["signPrivateKey"], alpha_ident["deviceId"])
        (dst_dir / "ready.json").write_text(json.dumps(signed_ready, ensure_ascii=False), encoding="utf-8")

        with self.env("beta"):
            with self.assertRaises(relay_engine.VersionConflictError) as ctx:
                relay_engine.receive_relay(r_dup_ver)
            self.assertEqual(ctx.exception.code, "version_regression")


class TestEngineFaultInjectionAndSubprocessKill(MultiDeviceTestBase):
    def test_end_to_end_rx_faults_and_subprocess_sigkill_recovery(self) -> None:
        self.init_device("alpha")
        self.init_device("beta")
        self.pair_pair("alpha", "beta")

        repo_a = self.create_git_project("alpha", proj_id="fault", name="端到端故障恢复")
        _write_claude_session(self.devices["alpha"]["uhome"], str(repo_a), "cl_f1", "Fault Session", [("user", "u1"), ("assistant", "a1")])
        repo_b = self.root / "repo_b_fault"

        # 1. rx_before_archive -> retry resumes without reapplying workspace
        with self.env("alpha"):
            r1 = relay_engine.send_project("fault", to_alias="beta")["relayId"]
        with self.env("beta", {"RELAY_DESK_FAULT_POINT": "rx_before_archive"}):
            with self.assertRaisesRegex(RuntimeError, "rx_before_archive"):
                relay_engine.receive_relay(r1, target_path=str(repo_b))
        with self.env("beta"):
            st1 = relay_engine.recover_relay(r1, action="status")
            self.assertEqual(st1["phase"], workspace_tx.PHASE_WORKSPACE_COMMITTED)
            self.assertTrue(st1["canResume"])
            self.assertTrue(st1["canRollback"])
            self.assertEqual(L.load(self.devices["beta"]["led"])["projects"], [])

            with mock.patch.object(packager, "apply_verified_payload", side_effect=AssertionError("must not reapply")):
                res1 = relay_engine.receive_relay(r1)
            self.assertTrue(res1["ok"])
            self.assertEqual(len(relay_engine.query_transcripts("fault")["sessions"]), 1)

        # 2. rx_before_ledger -> retry resumes from ARCHIVED without reapplying workspace or duplicating history
        with self.env("alpha"):
            relay_engine.list_inbox()
            L.update(self.devices["alpha"]["led"], lambda l: l["projects"][0].__setitem__("device", "alpha"))
            (repo_a / "f2.txt").write_text("v2\n", encoding="utf-8")
            _git(repo_a, "add", "f2.txt")
            _git(repo_a, "commit", "-m", "v2")
            r2 = relay_engine.send_project("fault", to_alias="beta")["relayId"]

        with self.env("beta", {"RELAY_DESK_FAULT_POINT": "rx_before_ledger"}):
            with self.assertRaisesRegex(RuntimeError, "rx_before_ledger"):
                relay_engine.receive_relay(r2)
        with self.env("beta"):
            st2 = relay_engine.recover_relay(r2, action="status")
            self.assertEqual(st2["phase"], workspace_tx.PHASE_ARCHIVED)
            with mock.patch.object(packager, "apply_verified_payload", side_effect=AssertionError("must not reapply")):
                res2 = relay_engine.receive_relay(r2)
            self.assertTrue(res2["ok"])
            self.assertEqual([h["relayId"] for h in L.load(self.devices["beta"]["led"])["projects"][0]["history"]], [r1, r2])

        # 3. rx_before_receipt -> retry uploads receipt without duplicating history
        with self.env("alpha"):
            relay_engine.list_inbox()
            L.update(self.devices["alpha"]["led"], lambda l: l["projects"][0].__setitem__("device", "alpha"))
            (repo_a / "f3.txt").write_text("v3\n", encoding="utf-8")
            _git(repo_a, "add", "f3.txt")
            _git(repo_a, "commit", "-m", "v3")
            r3 = relay_engine.send_project("fault", to_alias="beta")["relayId"]

        with self.env("beta", {"RELAY_DESK_FAULT_POINT": "rx_before_receipt"}):
            with self.assertRaisesRegex(RuntimeError, "rx_before_receipt"):
                relay_engine.receive_relay(r3)
        with self.env("beta"):
            st3 = relay_engine.recover_relay(r3, action="status")
            self.assertTrue(st3["canResume"])
            self.assertEqual([h["relayId"] for h in L.load(self.devices["beta"]["led"])["projects"][0]["history"]], [r1, r2, r3])
            with mock.patch.object(packager, "apply_verified_payload", side_effect=AssertionError("must not reapply")):
                res3 = relay_engine.receive_relay(r3)
            self.assertTrue(res3["ok"])
            self.assertEqual([h["relayId"] for h in L.load(self.devices["beta"]["led"])["projects"][0]["history"]], [r1, r2, r3])

        # 4. Real subprocess SIGKILL during receive (tx_mid_files) + recover_relay --status / --rollback
        (repo_b / ".env").write_text("BETA_LOCAL=preserve_across_rollback\n", encoding="utf-8")
        pre_head_b = _git(repo_b, "rev-parse", "HEAD").strip()
        with self.env("alpha"):
            relay_engine.list_inbox()
            L.update(self.devices["alpha"]["led"], lambda l: l["projects"][0].__setitem__("device", "alpha"))
            (repo_a / "f4_new.txt").write_text("v4\n", encoding="utf-8")
            (repo_a / "README.md").write_text("# v4 modified\n", encoding="utf-8")
            _git(repo_a, "add", "f4_new.txt", "README.md")
            _git(repo_a, "commit", "-m", "v4")
            r4 = relay_engine.send_project("fault", to_alias="beta")["relayId"]

        sub_env = copy.deepcopy(os.environ)
        sub_env.update(
            {
                "PYTHONPATH": REPO_ROOT,
                "RELAY_DESK_HOME": str(self.devices["beta"]["home"]),
                "RELAY_DESK_LEDGER": str(self.devices["beta"]["led"]),
                "RELAY_DESK_DEVICE": "beta",
                "RELAY_DESK_USER_HOME": str(self.devices["beta"]["uhome"]),
                "RELAY_DESK_FAULT_POINT": "tx_mid_files",
                "RELAY_DESK_FAULT_MODE": "sigkill",
            }
        )
        proc = subprocess.run(
            [sys.executable, "-c", "from relaydesk import relay_engine; relay_engine.receive_relay(%r)" % r4],
            cwd=REPO_ROOT,
            env=sub_env,
            capture_output=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        if hasattr(signal, "SIGKILL"):
            self.assertEqual(proc.returncode, -signal.SIGKILL)

        with self.env("beta"):
            st4 = relay_engine.recover_relay(r4, action="status")
            self.assertEqual(st4["phase"], workspace_tx.PHASE_APPLYING_FILES)
            self.assertEqual(st4["state"], "in_progress")
            self.assertTrue(st4["canResume"])
            self.assertTrue(st4["canRollback"])

            rb4 = relay_engine.recover_relay(r4, action="rollback")
            self.assertEqual(rb4["phase"], workspace_tx.PHASE_ROLLED_BACK)
            self.assertEqual(_git(repo_b, "rev-parse", "HEAD").strip(), pre_head_b)
            self.assertFalse((repo_b / "f4_new.txt").exists())
            self.assertEqual((repo_b / ".env").read_text(encoding="utf-8"), "BETA_LOCAL=preserve_across_rollback\n")

            # Clean receive after rollback completes normally
            self.assertTrue(relay_engine.receive_relay(r4)["ok"])
            self.assertTrue((repo_b / "f4_new.txt").is_file())
            self.assertEqual((repo_b / ".env").read_text(encoding="utf-8"), "BETA_LOCAL=preserve_across_rollback\n")

        # 5. Subprocess SIGKILL at tx_after_workspace_commit -> resume completes without reapplying workspace
        with self.env("alpha"):
            relay_engine.list_inbox()
            L.update(self.devices["alpha"]["led"], lambda l: l["projects"][0].__setitem__("device", "alpha"))
            (repo_a / "f5.txt").write_text("v5\n", encoding="utf-8")
            _git(repo_a, "add", "f5.txt")
            _git(repo_a, "commit", "-m", "v5")
            r5 = relay_engine.send_project("fault", to_alias="beta")["relayId"]

        sub_env["RELAY_DESK_FAULT_POINT"] = "tx_after_workspace_commit"
        proc5 = subprocess.run(
            [sys.executable, "-c", "from relaydesk import relay_engine; relay_engine.receive_relay(%r)" % r5],
            cwd=REPO_ROOT,
            env=sub_env,
            capture_output=True,
        )
        self.assertNotEqual(proc5.returncode, 0)

        with self.env("beta"):
            st5 = relay_engine.recover_relay(r5, action="status")
            self.assertEqual(st5["phase"], workspace_tx.PHASE_WORKSPACE_COMMITTED)
            self.assertEqual(st5["state"], "workspace_committed")
            with mock.patch.object(packager, "apply_verified_payload", side_effect=AssertionError("must not reapply")):
                res5 = relay_engine.receive_relay(r5)
            self.assertTrue(res5["ok"])
            st5_done = relay_engine.recover_relay(r5, action="status")
            self.assertEqual(st5_done["state"], "completed")


if __name__ == "__main__":
    unittest.main()
