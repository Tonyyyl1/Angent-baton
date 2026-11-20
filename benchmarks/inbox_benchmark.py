# -*- coding: utf-8 -*-
"""Reproducible inbox performance & network I/O benchmark suite (v2 vs v2-compat vs v3).

Measures `first_read` and `repeat_refresh` across LocalDir, WebDAV (rclone), and S3
(rclone) backends for varying device counts (N=2,3,10) and historical relay counts
(H=100,1000,10000), recording median of 3 runs:
  - elapsed_sec
  - listed_objects
  - list_calls
  - download_count
  - downloaded_bytes
  - stat_count
  - verify_count (Ed25519 verify_json calls)

Python 3.9 compatible; UTF-8 everywhere.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from relaydesk import crypto  # noqa: E402
from relaydesk import ledger as L  # noqa: E402
from relaydesk import packager  # noqa: E402
from relaydesk import relay_engine  # noqa: E402
from relaydesk import storage  # noqa: E402
from tests.cloud_servers import find_rclone, s3_server, webdav_server  # noqa: E402

FROZEN_COMMIT = "dd0312ef84476901acb6550daf698811b756726b"


class InstrumentedProvider(storage.StorageProvider):
    """Transparent StorageProvider wrapper counting list, stat, download, and upload I/O."""

    def __init__(self, inner: storage.StorageProvider) -> None:
        self.inner = inner
        self.list_calls = 0
        self.listed_objects = 0
        self.download_count = 0
        self.downloaded_bytes = 0
        self.stat_count = 0
        self.upload_count = 0
        self.uploaded_bytes = 0
        self.delete_count = 0
        self.downloaded_keys: List[str] = []

    def reset_counters(self) -> None:
        self.list_calls = 0
        self.listed_objects = 0
        self.download_count = 0
        self.downloaded_bytes = 0
        self.stat_count = 0
        self.upload_count = 0
        self.uploaded_bytes = 0
        self.delete_count = 0
        self.downloaded_keys = []

    def metrics(self) -> Dict[str, int]:
        return {
            "list_calls": self.list_calls,
            "listed_objects": self.listed_objects,
            "download_count": self.download_count,
            "downloaded_bytes": self.downloaded_bytes,
            "stat_count": self.stat_count,
            "upload_count": self.upload_count,
            "uploaded_bytes": self.uploaded_bytes,
            "delete_count": self.delete_count,
        }

    def list_objects(self, prefix: str = "") -> List[storage.ObjectInfo]:
        self.list_calls += 1
        objs = self.inner.list_objects(prefix)
        self.listed_objects += len(objs)
        return objs

    def upload(self, local_path: str, remote_key: str) -> storage.ObjectInfo:
        self.upload_count += 1
        info = self.inner.upload(local_path, remote_key)
        self.uploaded_bytes += int(getattr(info, "size", 0) or 0)
        return info

    def download(self, remote_key: str, local_path: str) -> storage.ObjectInfo:
        self.download_count += 1
        self.downloaded_keys.append(str(remote_key))
        info = self.inner.download(remote_key, local_path)
        size = int(getattr(info, "size", 0) or 0)
        if size == 0 and os.path.isfile(local_path):
            size = os.path.getsize(local_path)
        self.downloaded_bytes += size
        return info

    def stat(self, remote_key: str) -> Optional[storage.ObjectInfo]:
        self.stat_count += 1
        return self.inner.stat(remote_key)

    def delete(self, remote_key: str) -> bool:
        self.delete_count += 1
        return self.inner.delete(remote_key)


@contextlib.contextmanager
def count_verify_json(crypto_mod: Any = crypto) -> Iterator[Dict[str, int]]:
    """Count invocations of `crypto.verify_json` (including nested calls via verify_snapshot)."""
    counter = {"count": 0}
    orig = crypto_mod.verify_json

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        counter["count"] += 1
        return orig(*args, **kwargs)

    with mock.patch.object(crypto_mod, "verify_json", _wrapped):
        yield counter


def extract_frozen_repo(dest_dir: Path, commit: str = FROZEN_COMMIT) -> Path:
    """Extract frozen dd0312e source tree into `dest_dir` via `git archive`."""
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", commit],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "git archive failed (%d): %s"
            % (proc.returncode, proc.stderr.decode("utf-8", errors="replace"))
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tf:
        tf.extractall(path=str(dest_dir))
    if not (dest_dir / "relay.py").is_file():
        raise RuntimeError("Extracted frozen archive missing relay.py")
    return dest_dir


def _write_json_fast(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))


def _build_sample_tar(work_dir: Path) -> Tuple[Path, str, int]:
    """Create a minimal real git repository and package it into payload.tar.gz."""
    repo_dir = work_dir / "sample_src_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-c", "user.name=Bench", "-c", "user.email=bench@relay.invalid", "-C", str(repo_dir), "init", "-q", "-b", "main"],
        check=True,
        capture_output=True,
    )
    (repo_dir / "README.md").write_text("# Benchmark Project\n", encoding="utf-8")
    (repo_dir / "app.py").write_text("def hello():\n    return 'relay-bench'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "README.md", "app.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Bench", "-c", "user.email=bench@relay.invalid", "-C", str(repo_dir), "commit", "-q", "-m", "bench init"],
        check=True,
        capture_output=True,
    )

    tar_path = work_dir / "sample_payload.tar.gz"
    metadata = {
        "id": "p_bench",
        "name": "BenchProject",
        "tool": "claude",
        "lastTask": "benchmark baseline",
        "nextAction": "continue on B",
        "note": "",
        "verify": {"state": "pass", "cmd": "pytest", "note": ""},
        "sessions": [],
        "handoffMd": "",
        "worktree": "",
        "remote": "",
        "branch": "main",
        "dirty": 0,
        "paths": {"a": str(repo_dir)},
        "fromAlias": "a",
        "toAlias": "b",
        "relayId": "r_template",
        "parentRelayId": "",
    }
    conv_bundle = {"projectId": "p_bench", "entries": []}
    packager.build_payload(str(repo_dir), str(tar_path), metadata, conv_bundle, target_os=None)
    raw = tar_path.read_bytes()
    return tar_path, hashlib.sha256(raw).hexdigest(), len(raw)


def _create_identities(ident_root: Path, num_devices: int) -> List[Dict[str, Any]]:
    identities: List[Dict[str, Any]] = []
    aliases = ["a", "b"] + ["dev_%d" % i for i in range(3, num_devices + 1)]
    for idx, alias in enumerate(aliases):
        dev_dir = ident_root / alias
        dev_dir.mkdir(parents=True, exist_ok=True)
        ident = crypto.ensure_identity(
            dev_dir,
            alias=alias,
            name="Bench Device %s" % alias.upper(),
            os_name="mac" if idx % 2 == 0 else "linux",
            host="bench-%s.local" % alias,
        )
        identities.append(ident)
    return identities


def _build_cloud_cfg_for_b(
    identities: List[Dict[str, Any]],
    mode: str,
    storage_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    ident_a = identities[0]
    ident_b = identities[1]
    fp_ab = crypto.pair_fingerprint(
        crypto.public_descriptor(ident_b),
        crypto.public_descriptor(ident_a),
    )

    if mode == "old_v2":
        peer_a = crypto.public_descriptor(ident_a)
        return {
            **storage_cfg,
            "paired": True,
            "pairFingerprint": fp_ab,
            "pairedAt": "2026-11-18T00:00:00.000Z",
            "peer": peer_a,
        }

    if mode == "new_v2_compat":
        peer_a = crypto.public_descriptor(ident_a)
        peer_a["paired"] = True
        peer_a["status"] = "paired"
        peer_a["pairFingerprint"] = fp_ab
        peer_a["pairedAt"] = "2026-11-18T00:00:00.000Z"
        peer_a["protocolVersion"] = 2
        peer_a["capabilities"] = ["v2"]
        peer_a["pendingDescriptor"] = None
        return {
            "version": 3,
            **storage_cfg,
            "shownFingerprints": {ident_a["deviceId"]: fp_ab},
            "peers": {ident_a["deviceId"]: peer_a},
        }

    # new_v3: all N-1 peers paired with v3 capabilities
    peers_map: Dict[str, Any] = {}
    shown: Dict[str, str] = {}
    desc_b = crypto.public_descriptor_v3(ident_b)
    for ident in identities:
        if ident["deviceId"] == ident_b["deviceId"]:
            continue
        desc = crypto.public_descriptor_v3(ident)
        fp = crypto.pair_fingerprint(desc_b, desc)
        shown[ident["deviceId"]] = fp
        peers_map[ident["deviceId"]] = {
            **desc,
            "pairFingerprint": fp,
            "paired": True,
            "status": "paired",
            "pairedAt": "2026-11-18T00:00:00.000Z",
            "protocolVersion": 3,
            "capabilities": ["v2", "v3"],
            "pendingDescriptor": None,
        }
    return {
        "version": 3,
        **storage_cfg,
        "shownFingerprints": shown,
        "peers": peers_map,
    }


def _populate_device_b_home(
    home_b: Path,
    ledger_b: Path,
    identities: List[Dict[str, Any]],
    mode: str,
    storage_cfg: Dict[str, Any],
    relay_state: Dict[str, Any],
) -> Dict[str, Any]:
    home_b.mkdir(parents=True, exist_ok=True)
    ident_b = identities[1]
    (home_b / "identity.json").write_text(
        json.dumps(ident_b, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    local_cfg = {"device": "b", "ledger": str(ledger_b)}
    _write_json_fast(home_b / "config.json", local_cfg)
    _write_json_fast(home_b / "cloud.json", _build_cloud_cfg_for_b(identities, mode, storage_cfg))
    _write_json_fast(home_b / "relay_state.json", relay_state)

    if mode == "old_v2":
        devs = [
            {
                "id": "a",
                "name": identities[0]["name"],
                "os": identities[0]["os"],
                "drop": "",
                "host": identities[0]["host"],
                "deviceId": identities[0]["deviceId"],
            },
            {
                "id": "b",
                "name": ident_b["name"],
                "os": ident_b["os"],
                "drop": "",
                "host": ident_b["host"],
                "deviceId": ident_b["deviceId"],
            },
        ]
        led = {"app": "relaydesk", "version": 1, "config": {"devices": devs, "reg": {}}, "projects": []}
    else:
        devs = [
            {
                "id": ident["alias"],
                "name": ident["name"],
                "os": ident["os"],
                "drop": "",
                "host": ident["host"],
                "deviceId": ident["deviceId"],
            }
            for ident in identities
        ]
        led = {"app": "relaydesk", "version": 2, "config": {"devices": devs, "reg": {}}, "projects": []}
    _write_json_fast(ledger_b, led)
    return local_cfg


def setup_v2_dataset(
    storage_root: Path,
    home_b: Path,
    ledger_b: Path,
    identities: List[Dict[str, Any]],
    tar_path: Path,
    num_history: int,
    num_active: int,
    mode: str,
    storage_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Populate a legacy v2 (`relays/<id>/`) dataset on `storage_root` and B's state."""
    ident_a = identities[0]
    ident_b = identities[1]
    n_dev = len(identities)

    # Publish device descriptors
    for ident in identities[:2]:
        desc = crypto.sign_json(
            crypto.public_descriptor(ident),
            ident["signPrivateKey"],
            signer_device_id=ident["deviceId"],
        )
        _write_json_fast(storage_root / "devices" / ("%s.json" % ident["alias"]), desc)

    # Encrypt one historical payload buffer reused for historical relays (list_inbox never downloads package.enc)
    hist_enc_path = storage_root.parent / "_hist_v2.enc"
    if not hist_enc_path.is_file():
        hist_env = crypto.encrypt_package(str(tar_path), str(hist_enc_path), ident_a, ident_b)
    else:
        hist_bytes_tmp = hist_enc_path.read_bytes()
        hist_env = {
            "packageSize": len(hist_bytes_tmp),
            "packageSha256": hashlib.sha256(hist_bytes_tmp).hexdigest(),
            "boxHeader": "",
            "encVersion": 1,
        }
    hist_enc_bytes = hist_enc_path.read_bytes()
    hist_sha = hashlib.sha256(hist_enc_bytes).hexdigest()
    hist_size = len(hist_enc_bytes)

    meta_hist_enc = crypto.encrypt_meta(
        {
            "projectId": "p_bench",
            "projectName": "BenchProject",
            "lastTask": "hist",
            "nextAction": "done",
            "branch": "main",
            "dirty": 0,
            "sessionCount": 0,
        },
        ident_a,
        ident_b,
    )

    received_relays: Dict[str, Any] = {}
    sent_relays: Dict[str, Any] = {}

    n_b_rx = num_history // n_dev
    n_b_tx = num_history // n_dev

    for i in range(num_history):
        rid = "r_20261117_%06d" % i
        rdir = storage_root / "relays" / rid
        rdir.mkdir(parents=True, exist_ok=True)

        if i < n_b_rx:
            sender, receiver = ident_a, ident_b
            received_relays[rid] = {
                "projectId": "p_bench",
                "targetPath": str(storage_root.parent / "ws_b"),
                "receivedAt": "2026-11-17T10:00:00.000Z",
                "receiptUploaded": True,
            }
        elif i < n_b_rx + n_b_tx:
            sender, receiver = ident_b, ident_a
            sent_relays[rid] = {
                "projectId": "p_bench",
                "toAlias": "a",
                "toDeviceId": ident_a["deviceId"],
                "packageSha256": hist_sha,
                "sentAt": "2026-11-17T10:00:00.000Z",
                "acked": True,
                "ackedAt": "2026-11-17T10:01:00.000Z",
            }
        else:
            s_idx = (i % (n_dev - 1)) + 2 if n_dev > 2 else 0
            if s_idx >= n_dev:
                s_idx = 0
            r_idx = 2 if s_idx == 0 and n_dev > 2 else 0
            sender, receiver = identities[s_idx], identities[r_idx]

        ready_unsigned = {
            "formatVersion": 1,
            "relayId": rid,
            "fromAlias": sender["alias"],
            "fromDeviceId": sender["deviceId"],
            "toAlias": receiver["alias"],
            "toDeviceId": receiver["deviceId"],
            "parentRelayId": "",
            "createdAt": "2026-11-17T10:00:00.000Z",
            "packageKey": "relays/%s/package.enc" % rid,
            "packageSize": hist_size,
            "packageSha256": hist_sha,
            "encVersion": hist_env.get("encVersion", 1),
            "meta": meta_hist_enc,
        }
        signed_ready = crypto.sign_json(
            ready_unsigned,
            sender["signPrivateKey"],
            signer_device_id=sender["deviceId"],
        )
        receipt_unsigned = {
            "formatVersion": 1,
            "relayId": rid,
            "projectId": "p_bench",
            "fromAlias": sender["alias"],
            "toAlias": receiver["alias"],
            "receiverDeviceId": receiver["deviceId"],
            "packageSha256": hist_sha,
            "receivedAt": "2026-11-17T10:01:00.000Z",
        }
        signed_receipt = crypto.sign_json(
            receipt_unsigned,
            receiver["signPrivateKey"],
            signer_device_id=receiver["deviceId"],
        )
        _write_json_fast(rdir / "ready.json", signed_ready)
        (rdir / "package.enc").write_bytes(hist_enc_bytes)
        _write_json_fast(rdir / "receipt.json", signed_receipt)

    # 10 active unclaimed incoming relays for Device B (real encrypted payload!)
    active_ids: List[str] = []
    for j in range(num_active):
        rid = "r_20261118_act%03d" % j
        active_ids.append(rid)
        rdir = storage_root / "relays" / rid
        rdir.mkdir(parents=True, exist_ok=True)
        enc_path = rdir / "package.enc"
        envelope = crypto.encrypt_package(str(tar_path), str(enc_path), ident_a, ident_b)
        meta_enc = crypto.encrypt_meta(
            {
                "projectId": "p_bench_%d" % j,
                "projectName": "ActiveProj_%d" % j,
                "lastTask": "task %d" % j,
                "nextAction": "next %d" % j,
                "branch": "main",
                "dirty": 0,
                "sessionCount": 0,
            },
            ident_a,
            ident_b,
        )
        ready_unsigned = dict(envelope)
        ready_unsigned.update(
            {
                "formatVersion": 1,
                "relayId": rid,
                "fromAlias": "a",
                "fromDeviceId": ident_a["deviceId"],
                "toAlias": "b",
                "toDeviceId": ident_b["deviceId"],
                "parentRelayId": "",
                "createdAt": "2026-11-18T04:%02d:00.000Z" % (j % 60),
                "packageKey": "relays/%s/package.enc" % rid,
                "meta": meta_enc,
            }
        )
        signed_ready = crypto.sign_json(
            ready_unsigned,
            ident_a["signPrivateKey"],
            signer_device_id=ident_a["deviceId"],
        )
        _write_json_fast(rdir / "ready.json", signed_ready)

    relay_state: Dict[str, Any] = {
        "pendingSends": {},
        "sentRelays": sent_relays,
        "appliedReceives": {},
        "receivedRelays": received_relays,
        "projectBaselines": {},
    }
    if mode == "new_v2_compat":
        relay_state["version"] = 3
        relay_state["activeHandoffs"] = {}
        relay_state["versionGraph"] = {}
        relay_state["localCurrentVersion"] = {}
        relay_state["processedDeliveries"] = {}
        relay_state["archivedRelayIds"] = {}
        relay_state["forwardedSnapshots"] = {}

    local_cfg = _populate_device_b_home(home_b, ledger_b, identities, mode, storage_cfg, relay_state)
    return {"local": local_cfg, "activeIds": active_ids, "initialRelayState": relay_state}


def setup_v3_dataset(
    storage_root: Path,
    home_b: Path,
    ledger_b: Path,
    identities: List[Dict[str, Any]],
    tar_path: Path,
    tar_sha: str,
    tar_size: int,
    num_history: int,
    num_active: int,
    storage_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Populate a v3 (`v3/inbox/<devId>/<id>/`) dataset on `storage_root` and B's state."""
    ident_a = identities[0]
    ident_b = identities[1]
    n_dev = len(identities)

    for ident in identities:
        desc = crypto.sign_json(
            crypto.public_descriptor_v3(ident),
            ident["signPrivateKey"],
            signer_device_id=ident["deviceId"],
        )
        _write_json_fast(storage_root / "v3" / "devices" / ("%s.json" % ident["deviceId"]), desc)

    hist_enc_path = storage_root.parent / "_hist_v3.enc"
    if not hist_enc_path.is_file():
        crypto.encrypt_package(str(tar_path), str(hist_enc_path), ident_a, ident_b)
    hist_enc_bytes = hist_enc_path.read_bytes()
    hist_sha = hashlib.sha256(hist_enc_bytes).hexdigest()
    hist_size = len(hist_enc_bytes)

    meta_hist_enc = crypto.encrypt_meta(
        {
            "projectId": "p_bench",
            "projectName": "BenchProject",
            "lastTask": "hist",
            "nextAction": "done",
            "branch": "main",
            "dirty": 0,
            "sessionCount": 0,
        },
        ident_a,
        ident_b,
    )

    received_relays: Dict[str, Any] = {}
    sent_relays: Dict[str, Any] = {}
    n_b_rx = num_history // n_dev
    n_b_tx = num_history // n_dev

    for i in range(num_history):
        rid = "r_20261117_%06d" % i
        if i < n_b_rx:
            sender, receiver = ident_a, ident_b
            received_relays[rid] = {
                "projectId": "p_bench",
                "targetPath": str(storage_root.parent / "ws_b"),
                "receivedAt": "2026-11-17T10:00:00.000Z",
                "protocolVersion": 3,
                "receiptUploaded": True,
            }
        elif i < n_b_rx + n_b_tx:
            sender, receiver = ident_b, ident_a
            sent_relays[rid] = {
                "projectId": "p_bench",
                "toAlias": "a",
                "toDeviceId": ident_a["deviceId"],
                "protocolVersion": 3,
                "packageSha256": hist_sha,
                "sentAt": "2026-11-17T10:00:00.000Z",
                "acked": True,
                "ackedAt": "2026-11-17T10:01:00.000Z",
            }
        else:
            s_idx = (i % (n_dev - 1)) + 2 if n_dev > 2 else 0
            if s_idx >= n_dev:
                s_idx = 0
            r_idx = (s_idx + 1) % n_dev
            if r_idx == 1:
                r_idx = 0
            sender, receiver = identities[s_idx], identities[r_idx]

        rdir = storage_root / "v3" / "inbox" / receiver["deviceId"] / rid
        rdir.mkdir(parents=True, exist_ok=True)

        snap_signed = crypto.sign_snapshot(
            {
                "formatVersion": 3,
                "projectId": "p_bench",
                "projectName": "BenchProject",
                "versionId": "snap_20261117_%06d" % i,
                "authorDeviceId": sender["deviceId"],
                "authorAlias": sender["alias"],
                "parentVersionIds": [],
                "parentRelayId": "",
                "relayId": rid,
                "contentSha256": tar_sha,
                "contentSize": tar_size,
                "createdAt": "2026-11-17T10:00:00.000Z",
            },
            sender["signPrivateKey"],
            author_device_id=sender["deviceId"],
        )
        ready_unsigned = {
            "formatVersion": 3,
            "purpose": "handoff",
            "deliveryId": rid,
            "relayId": rid,
            "senderDeviceId": sender["deviceId"],
            "senderAlias": sender["alias"],
            "fromAlias": sender["alias"],
            "fromDeviceId": sender["deviceId"],
            "toAlias": receiver["alias"],
            "toDeviceId": receiver["deviceId"],
            "authorizedDeviceId": receiver["deviceId"],
            "parentRelayId": "",
            "isForwarded": False,
            "createdAt": "2026-11-17T10:00:00.000Z",
            "packageKey": "v3/inbox/%s/%s/package.enc" % (receiver["deviceId"], rid),
            "packageSize": hist_size,
            "packageSha256": hist_sha,
            "snapshot": snap_signed,
            "meta": meta_hist_enc,
        }
        signed_ready = crypto.sign_json(
            ready_unsigned,
            sender["signPrivateKey"],
            signer_device_id=sender["deviceId"],
        )
        receipt_unsigned = {
            "formatVersion": 3,
            "purpose": "handoff",
            "relayId": rid,
            "projectId": "p_bench",
            "snapshotId": snap_signed["versionId"],
            "fromAlias": sender["alias"],
            "fromDeviceId": sender["deviceId"],
            "toAlias": receiver["alias"],
            "receiverDeviceId": receiver["deviceId"],
            "packageSha256": hist_sha,
            "receivedAt": "2026-11-17T10:01:00.000Z",
        }
        signed_receipt = crypto.sign_json(
            receipt_unsigned,
            receiver["signPrivateKey"],
            signer_device_id=receiver["deviceId"],
        )
        _write_json_fast(rdir / "ready.json", signed_ready)
        (rdir / "package.enc").write_bytes(hist_enc_bytes)
        _write_json_fast(rdir / "receipt.json", signed_receipt)

    # 10 active unclaimed incoming relays for Device B (real encrypted packages + snapshots!)
    active_ids: List[str] = []
    for j in range(num_active):
        rid = "r_20261118_act%03d" % j
        active_ids.append(rid)
        rdir = storage_root / "v3" / "inbox" / ident_b["deviceId"] / rid
        rdir.mkdir(parents=True, exist_ok=True)
        enc_path = rdir / "package.enc"
        envelope = crypto.encrypt_package(str(tar_path), str(enc_path), ident_a, ident_b)
        meta_enc = crypto.encrypt_meta(
            {
                "projectId": "p_bench",
                "projectName": "BenchProject",
                "lastTask": "active task %d" % j,
                "nextAction": "next %d" % j,
                "branch": "main",
                "dirty": 0,
                "sessionCount": 0,
            },
            ident_a,
            ident_b,
        )
        snap_signed = crypto.sign_snapshot(
            {
                "formatVersion": 3,
                "projectId": "p_bench",
                "projectName": "BenchProject",
                "versionId": "snap_20261118_act%03d" % j,
                "authorDeviceId": ident_a["deviceId"],
                "authorAlias": "a",
                "parentVersionIds": [],
                "parentRelayId": "",
                "relayId": rid,
                "contentSha256": tar_sha,
                "contentSize": tar_size,
                "createdAt": "2026-11-18T04:%02d:00.000Z" % (j % 60),
            },
            ident_a["signPrivateKey"],
            author_device_id=ident_a["deviceId"],
        )
        ready_unsigned = dict(envelope)
        ready_unsigned.update(
            {
                "formatVersion": 3,
                "purpose": "handoff",
                "deliveryId": rid,
                "relayId": rid,
                "senderDeviceId": ident_a["deviceId"],
                "senderAlias": "a",
                "fromAlias": "a",
                "fromDeviceId": ident_a["deviceId"],
                "toAlias": "b",
                "toDeviceId": ident_b["deviceId"],
                "authorizedDeviceId": ident_b["deviceId"],
                "parentRelayId": "",
                "isForwarded": False,
                "createdAt": "2026-11-18T04:%02d:00.000Z" % (j % 60),
                "packageKey": "v3/inbox/%s/%s/package.enc" % (ident_b["deviceId"], rid),
                "snapshot": snap_signed,
                "meta": meta_enc,
            }
        )
        signed_ready = crypto.sign_json(
            ready_unsigned,
            ident_a["signPrivateKey"],
            signer_device_id=ident_a["deviceId"],
        )
        _write_json_fast(rdir / "ready.json", signed_ready)

    relay_state: Dict[str, Any] = {
        "version": 3,
        "pendingSends": {},
        "sentRelays": sent_relays,
        "appliedReceives": {},
        "receivedRelays": received_relays,
        "projectBaselines": {},
        "activeHandoffs": {},
        "versionGraph": {},
        "localCurrentVersion": {},
        "processedDeliveries": {},
        "archivedRelayIds": {},
        "forwardedSnapshots": {},
    }
    local_cfg = _populate_device_b_home(home_b, ledger_b, identities, "new_v3", storage_cfg, relay_state)
    return {"local": local_cfg, "activeIds": active_ids, "initialRelayState": relay_state}


def _pick_median_run(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    ordered = sorted(samples, key=lambda x: x["elapsed_sec"])
    med = dict(ordered[len(ordered) // 2])
    med["elapsed_sec"] = round(statistics.median([s["elapsed_sec"] for s in samples]), 6)
    med["all_elapsed_sec"] = [round(s["elapsed_sec"], 6) for s in samples]
    return med


def benchmark_new_engine(
    home_b: Path,
    ledger_b: Path,
    local_cfg: Dict[str, Any],
    initial_relay_state: Dict[str, Any],
    runs: int = 3,
) -> Dict[str, Any]:
    """Benchmark `relay_engine.list_inbox` for first_read and repeat_refresh."""
    orig_make_provider = storage.make_provider
    state_file = home_b / "relay_state.json"

    def _run_one() -> Dict[str, Any]:
        holder: Dict[str, InstrumentedProvider] = {}

        def _inst_make(cfg: Dict[str, Any]) -> storage.StorageProvider:
            inst = InstrumentedProvider(orig_make_provider(cfg))
            holder["prov"] = inst
            return inst

        with mock.patch.object(storage, "make_provider", _inst_make):
            with count_verify_json(crypto) as vcnt:
                t0 = time.perf_counter()
                res = relay_engine.list_inbox(ledger_path=ledger_b, local=local_cfg, local_home=home_b)
                elapsed = time.perf_counter() - t0
        m = holder["prov"].metrics()
        return {
            "elapsed_sec": elapsed,
            "listed_objects": m["listed_objects"],
            "list_calls": m["list_calls"],
            "download_count": m["download_count"],
            "downloaded_bytes": m["downloaded_bytes"],
            "stat_count": m["stat_count"],
            "verify_count": vcnt["count"],
            "incoming_count": len(res.get("incoming") or []),
            "downloaded_keys": list(holder["prov"].downloaded_keys),
        }

    first_samples: List[Dict[str, Any]] = []
    for _ in range(runs):
        _write_json_fast(state_file, initial_relay_state)
        first_samples.append(_run_one())

    # Warm-up once so archivedRelayIds / caches are populated for repeat_refresh
    _write_json_fast(state_file, initial_relay_state)
    _run_one()

    repeat_samples: List[Dict[str, Any]] = []
    for _ in range(runs):
        repeat_samples.append(_run_one())

    return {
        "first_read": _pick_median_run(first_samples),
        "repeat_refresh": _pick_median_run(repeat_samples),
    }


def benchmark_old_engine(
    old_repo_dir: Path,
    home_b: Path,
    ledger_b: Path,
    initial_relay_state: Dict[str, Any],
    runs: int = 3,
) -> Dict[str, Any]:
    """Benchmark frozen `dd0312e` `relay_engine.list_inbox` inside an isolated subprocess."""
    runner_code = r"""
import json, os, statistics, sys, time
from pathlib import Path

old_repo = sys.argv[1]
home_b = Path(sys.argv[2])
ledger_b = Path(sys.argv[3])
runs = int(sys.argv[4])
state_file = home_b / "relay_state.json"
init_state = json.loads(state_file.read_text(encoding="utf-8"))

sys.path.insert(0, old_repo)
from relaydesk import crypto, relay_engine, storage

class Inst(storage.StorageProvider):
    def __init__(self, inner):
        self.inner = inner
        self.list_calls = 0
        self.listed_objects = 0
        self.download_count = 0
        self.downloaded_bytes = 0
        self.stat_count = 0
    def list_objects(self, prefix=""):
        self.list_calls += 1
        objs = self.inner.list_objects(prefix)
        self.listed_objects += len(objs)
        return objs
    def upload(self, local_path, remote_key):
        return self.inner.upload(local_path, remote_key)
    def download(self, remote_key, local_path):
        self.download_count += 1
        info = self.inner.download(remote_key, local_path)
        sz = int(getattr(info, "size", 0) or 0)
        if sz == 0 and os.path.isfile(local_path):
            sz = os.path.getsize(local_path)
        self.downloaded_bytes += sz
        return info
    def stat(self, remote_key):
        self.stat_count += 1
        return self.inner.stat(remote_key)
    def delete(self, remote_key):
        return self.inner.delete(remote_key)

orig_make = storage.make_provider
orig_verify = crypto.verify_json
vcnt = {"n": 0}

def wrapped_verify(*args, **kwargs):
    vcnt["n"] += 1
    return orig_verify(*args, **kwargs)

crypto.verify_json = wrapped_verify
holder = {}

def inst_make(cfg):
    p = Inst(orig_make(cfg))
    holder["p"] = p
    return p

storage.make_provider = inst_make
local_cfg = json.loads((home_b / "config.json").read_text(encoding="utf-8"))

def run_one():
    vcnt["n"] = 0
    t0 = time.perf_counter()
    res = relay_engine.list_inbox(ledger_path=ledger_b, local=local_cfg, local_home=home_b)
    elapsed = time.perf_counter() - t0
    p = holder["p"]
    return {
        "elapsed_sec": elapsed,
        "listed_objects": p.listed_objects,
        "list_calls": p.list_calls,
        "download_count": p.download_count,
        "downloaded_bytes": p.downloaded_bytes,
        "stat_count": p.stat_count,
        "verify_count": vcnt["n"],
        "incoming_count": len(res.get("incoming") or []),
    }

def pick_med(samples):
    ordered = sorted(samples, key=lambda x: x["elapsed_sec"])
    med = dict(ordered[len(ordered) // 2])
    med["elapsed_sec"] = round(statistics.median([s["elapsed_sec"] for s in samples]), 6)
    med["all_elapsed_sec"] = [round(s["elapsed_sec"], 6) for s in samples]
    return med

first_samples = []
for _ in range(runs):
    state_file.write_text(json.dumps(init_state), encoding="utf-8")
    first_samples.append(run_one())

state_file.write_text(json.dumps(init_state), encoding="utf-8")
run_one()
repeat_samples = [run_one() for _ in range(runs)]

print(json.dumps({"first_read": pick_med(first_samples), "repeat_refresh": pick_med(repeat_samples)}))
"""
    _write_json_fast(home_b / "relay_state.json", initial_relay_state)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("RELAY_DESK_DEVICE", None)
    env["RELAY_DESK_HOME"] = str(home_b)
    env["RELAY_DESK_LEDGER"] = str(ledger_b)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", runner_code, str(old_repo_dir), str(home_b), str(ledger_b), str(runs)],
        cwd=str(old_repo_dir),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Old dd0312e benchmark subprocess failed (%d):\nSTDOUT:\n%s\nSTDERR:\n%s"
            % (proc.returncode, proc.stdout, proc.stderr)
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_cell_on_storage(
    backend_name: str,
    storage_root: Path,
    storage_cfg: Dict[str, Any],
    work_dir: Path,
    old_repo_dir: Path,
    num_devices: int,
    num_history: int,
    num_active: int,
    runs: int,
    include_old: bool = True,
) -> Dict[str, Any]:
    tar_path, tar_sha, tar_size = _build_sample_tar(work_dir)
    identities = _create_identities(work_dir / "idents", num_devices)

    # 1. Legacy v2 layout (old_v2 + new_v2_compat)
    v2_root = storage_root if backend_name != "local" else (work_dir / "cloud_v2")
    v2_root.mkdir(parents=True, exist_ok=True)
    home_b_old = work_dir / "home_b_old"
    led_b_old = work_dir / "ledger_b_old.json"
    v2_info_old = setup_v2_dataset(
        v2_root,
        home_b_old,
        led_b_old,
        identities,
        tar_path,
        num_history,
        num_active,
        mode="old_v2",
        storage_cfg=storage_cfg if backend_name != "local" else {"provider": "local", "root": str(v2_root)},
    )
    if backend_name != "local":
        time.sleep(0.05)

    old_res = None
    if include_old:
        old_res = benchmark_old_engine(
            old_repo_dir,
            home_b_old,
            led_b_old,
            v2_info_old["initialRelayState"],
            runs=runs,
        )

    home_b_compat = work_dir / "home_b_compat"
    led_b_compat = work_dir / "ledger_b_compat.json"
    v2_info_compat = setup_v2_dataset(
        v2_root,
        home_b_compat,
        led_b_compat,
        identities,
        tar_path,
        num_history,
        num_active,
        mode="new_v2_compat",
        storage_cfg=storage_cfg if backend_name != "local" else {"provider": "local", "root": str(v2_root)},
    )
    compat_res = benchmark_new_engine(
        home_b_compat,
        led_b_compat,
        v2_info_compat["local"],
        v2_info_compat["initialRelayState"],
        runs=runs,
    )

    # 2. New v3 layout
    v3_root = storage_root if backend_name != "local" else (work_dir / "cloud_v3")
    if backend_name != "local":
        # Clean v2 relays/ from shared HTTP server dir before measuring pure v3
        for sub in ("relays", "devices"):
            sp = v3_root / sub
            if sp.exists():
                import shutil
                shutil.rmtree(str(sp))
        time.sleep(0.05)
    v3_root.mkdir(parents=True, exist_ok=True)

    home_b_v3 = work_dir / "home_b_v3"
    led_b_v3 = work_dir / "ledger_b_v3.json"
    v3_info = setup_v3_dataset(
        v3_root,
        home_b_v3,
        led_b_v3,
        identities,
        tar_path,
        tar_sha,
        tar_size,
        num_history,
        num_active,
        storage_cfg=storage_cfg if backend_name != "local" else {"provider": "local", "root": str(v3_root)},
    )
    if backend_name != "local":
        time.sleep(0.05)

    v3_res = benchmark_new_engine(
        home_b_v3,
        led_b_v3,
        v3_info["local"],
        v3_info["initialRelayState"],
        runs=runs,
    )

    return {
        "backend": backend_name,
        "devices": num_devices,
        "history": num_history,
        "active": num_active,
        "runs": runs,
        "engines": {
            "old_v2": old_res,
            "new_v2_compat": compat_res,
            "new_v3": v3_res,
        },
    }


def run_benchmark_suite(
    backends: List[str],
    device_counts: List[int],
    history_counts: List[int],
    http_history: int = 50,
    num_active: int = 10,
    runs: int = 3,
    include_old: bool = True,
) -> Dict[str, Any]:
    """Run the benchmark matrix across requested backends and dataset dimensions."""
    results: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as td:
        base_td = Path(td)
        old_repo_dir = extract_frozen_repo(base_td / "old_dd0312e")

        if "local" in backends:
            for n_dev in device_counts:
                for n_hist in history_counts:
                    cell_dir = base_td / ("local_%d_%d" % (n_dev, n_hist))
                    cell_dir.mkdir(parents=True, exist_ok=True)
                    results.append(
                        _run_cell_on_storage(
                            "local",
                            cell_dir,
                            {"provider": "local", "root": str(cell_dir)},
                            cell_dir,
                            old_repo_dir,
                            num_devices=n_dev,
                            num_history=n_hist,
                            num_active=num_active,
                            runs=runs,
                            include_old=include_old,
                        )
                    )

        for http_backend in ("webdav", "s3"):
            if http_backend not in backends:
                continue
            rclone_bin = find_rclone()
            if not rclone_bin:
                results.append(
                    {
                        "backend": http_backend,
                        "skipped": True,
                        "reason": "rclone binary not found",
                    }
                )
                continue
            srv_ctx = webdav_server(prefer_rclone=True) if http_backend == "webdav" else s3_server(prefer_rclone=True)
            with srv_ctx as env:
                cell_dir = base_td / ("%s_3_%d" % (http_backend, http_history))
                cell_dir.mkdir(parents=True, exist_ok=True)
                scfg = {
                    "provider": "rclone",
                    "remote": env.remote_b,
                    "root": env.shared_root,
                    "configPath": str(env.conf_b),
                }
                cell = _run_cell_on_storage(
                    "%s(%s)" % (http_backend, env.backend),
                    env.storage_root_dir,
                    scfg,
                    cell_dir,
                    old_repo_dir,
                    num_devices=3,
                    num_history=http_history,
                    num_active=num_active,
                    runs=runs,
                    include_old=include_old,
                )
                results.append(cell)

    return {
        "commit": FROZEN_COMMIT,
        "runs": runs,
        "active": num_active,
        "results": results,
    }


def format_markdown_table(report: Dict[str, Any]) -> str:
    lines = [
        "| Backend | N | H | Engine | Phase | Elapsed(s) | Listed | Downloads | Bytes | VerifyCalls | Incoming |",
        "|---|---:|---:|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report.get("results") or []:
        if item.get("skipped"):
            lines.append("| %s | - | - | skipped | - | - | - | - | - | - | %s |" % (item["backend"], item.get("reason", "")))
            continue
        b = item["backend"]
        n = item["devices"]
        h = item["history"]
        for eng_name in ("old_v2", "new_v2_compat", "new_v3"):
            eng_data = (item.get("engines") or {}).get(eng_name)
            if not eng_data:
                continue
            for phase in ("first_read", "repeat_refresh"):
                m = eng_data[phase]
                lines.append(
                    "| %s | %d | %d | %s | %s | %.4f | %d | %d | %d | %d | %d |"
                    % (
                        b,
                        n,
                        h,
                        eng_name,
                        phase,
                        m["elapsed_sec"],
                        m["listed_objects"],
                        m["download_count"],
                        m["downloaded_bytes"],
                        m["verify_count"],
                        m["incoming_count"],
                    )
                )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="RelayDesk inbox scalability benchmark (dd0312e vs v3)")
    ap.add_argument("--full", action="store_true", help="Run full matrix: LocalDir 2/3/10 x 100/1k/10k + WebDAV & S3")
    ap.add_argument("--backends", default=None, help="Comma-separated backends: local,webdav,s3")
    ap.add_argument("--devices", default=None, help="Comma-separated device counts, e.g. 2,3,10")
    ap.add_argument("--history", default=None, help="Comma-separated historical relay counts, e.g. 100,1000,10000")
    ap.add_argument("--http-history", type=int, default=50, help="Historical relay count for WebDAV/S3 loopback")
    ap.add_argument("--active", type=int, default=10, help="Active incoming relay count for Device B")
    ap.add_argument("--runs", type=int, default=3, help="Runs per phase for median calculation")
    ap.add_argument("--json-out", default=None, help="Write full JSON benchmark report to this path")
    args = ap.parse_args(argv)

    if args.full:
        backends = [s.strip() for s in (args.backends or "local,webdav,s3").split(",") if s.strip()]
        device_counts = [int(x) for x in (args.devices or "2,3,10").split(",") if x.strip()]
        history_counts = [int(x) for x in (args.history or "100,1000,10000").split(",") if x.strip()]
    else:
        backends = [s.strip() for s in (args.backends or "local").split(",") if s.strip()]
        device_counts = [int(x) for x in (args.devices or "3").split(",") if x.strip()]
        history_counts = [int(x) for x in (args.history or "50").split(",") if x.strip()]

    report = run_benchmark_suite(
        backends=backends,
        device_counts=device_counts,
        history_counts=history_counts,
        http_history=args.http_history,
        num_active=args.active,
        runs=args.runs,
    )
    table = format_markdown_table(report)
    sys.stdout.write(table + "\n")

    if args.json_out:
        out_p = Path(args.json_out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        sys.stdout.write("JSON report written to: %s\n" % out_p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
