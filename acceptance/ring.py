#!/usr/bin/env python3
"""Three-device ring and network-recovery acceptance driver.

Mac -> Windows -> Linux -> Mac, plus crash / rollback / key-rotation /
offline / network-loss scenarios, all through the real ``relay send`` and
``relay receive`` commands (see ``acceptance/adapter.py``).  The scenario is
the step file ``acceptance/steps/ring-3device.json``.

Subcommands
-----------
simulate   Run every step on this machine with three isolated device
           instances (own state dir, ledger, user home and HOME each) and a
           local shared folder as storage.  Level: ``<os>-local-simulation``.
device     Run this device's steps on a real machine (Mac, Windows or Linux),
           waiting for the other devices through the shared storage, asking
           the human for fingerprints and offline/online actions.
runbook    Print the step-by-step runbook humans follow.
aggregate  Merge per-device result folders, compare states across devices
           and compute the ring level (``real-multidevice`` only for three
           distinct machines whose OS matches their role).

Safety rules enforced here
--------------------------
* Public keys are pinned only with ``pair --with X --accept --fingerprint FP``
  where FP is a value the human read off the *other* device (in a
  simulation: read from the other instance's own display).  Without such a
  value, or when it differs from this device's display, nothing is accepted.
* ``level`` comes from ``platform``; a role (mac/win/linux) whose name does not
  match the real OS is refused unless ``--allow-role-mismatch`` is given, and
  then the result is labelled ``role-mismatch-simulation``.
* Only synthetic fixtures; rclone storage must use a ``relay-acceptance...``
  prefix; ``~/.relay-desk`` and the repository ledger are never used.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptance import adapter, common, fixtures  # noqa: E402
from acceptance import compare_state as cs  # noqa: E402
from acceptance.collect_env import collect as collect_env  # noqa: E402

DEFAULT_STEPS = Path(__file__).resolve().parent / "steps" / "ring-3device.json"
STEP_SCHEMA = "relay-desk-acceptance-step/1"
DEVICE_SCHEMA = "relay-desk-acceptance-device/1"
RING_SCHEMA = "relay-desk-acceptance-ring/1"


class StepFailed(Exception):
    pass


class NeedsHuman(Exception):
    pass


def norm_fp(fp: Any) -> str:
    return "".join(c for c in str(fp or "").upper() if c in "0123456789ABCDEF")


def tail(text: str, n: int = 600) -> str:
    text = (text or "").strip()
    return text[-n:]


# ---------------------------------------------------------------- checks


class Checks(object):
    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []

    def expect(self, name: str, ok: Any, **details: Any) -> bool:
        entry = {"check": name, "ok": bool(ok)}
        entry.update({k: v for k, v in details.items() if v is not None})
        self.items.append(entry)
        return bool(ok)

    def failed(self) -> List[str]:
        return [c["check"] for c in self.items if not c["ok"]]


# ---------------------------------------------------------------- device


class Device(object):
    def __init__(self, role: str, spec: Dict[str, Any], workdir: Path, results: Path, sim: bool):
        self.role = role
        self.spec = spec
        self.alias = spec["alias"]
        self.workdir = Path(workdir)
        self.results = Path(results)
        self.sim = sim
        self.online = True
        self.results.mkdir(parents=True, exist_ok=True)
        (self.results / "captures").mkdir(exist_ok=True)
        self.log = common.CommandLog(self.results)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.progress = common.read_json(self.progress_path, {}) or {}
        self.progress.setdefault("steps", {})
        self.progress.setdefault("paths", {})

    # paths -----------------------------------------------------------
    @property
    def progress_path(self) -> Path:
        return self.workdir / "progress.json"

    @property
    def state_dir(self) -> Path:
        return self.workdir / "state"

    @property
    def ledger(self) -> Path:
        return self.state_dir / "ledger.json"

    @property
    def userhome(self) -> Path:
        return self.workdir / "userhome"

    @property
    def home(self) -> Path:
        return self.workdir / "home"

    def ws(self, project: str, suffix: str = "") -> Path:
        return self.workdir / "ws" / (project + suffix)

    def project_path(self, project: str) -> Path:
        p = self.progress["paths"].get(project)
        if not p:
            raise StepFailed("project %s has no workspace on %s yet" % (project, self.role))
        return Path(p)

    def env(self) -> Dict[str, str]:
        drop = ("CODEX_HOME", "GEMINI_CONFIG_DIR", "PYTHONPATH")
        env = {k: v for k, v in os.environ.items() if not k.startswith("RELAY_DESK_") and k not in drop}
        for d in (self.state_dir, self.userhome, self.ws("").parent):
            d.mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "RELAY_DESK_HOME": str(self.state_dir),
                "RELAY_DESK_LEDGER": str(self.ledger),
                "RELAY_DESK_USER_HOME": str(self.userhome),
                "RELAY_DESK_DEVICE": self.alias,
                "CODEX_HOME": str(self.userhome / ".codex"),
                "GEMINI_CONFIG_DIR": str(self.userhome / ".gemini" / "config"),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PATH": common.tool_path(),
            }
        )
        if self.sim:
            # Fully isolated simulated machine: own HOME, no rclone config, and the
            # role's OS label so senders apply the target-OS path rules.
            self.home.mkdir(parents=True, exist_ok=True)
            env.pop("RCLONE_CONFIG", None)
            # Keep the interpreter's per-user site-packages (pynacl installed with
            # --user) visible after HOME moves.
            import site

            env.setdefault("PYTHONUSERBASE", site.getuserbase())
            env["HOME"] = str(self.home)
            env["USERPROFILE"] = str(self.home)
            env["XDG_CONFIG_HOME"] = str(self.home / ".config")
            env["RELAY_DESK_OS"] = self.spec.get("osLabel", "")
        return env

    @property
    def relay(self) -> adapter.Relay:
        if not self.online:
            # Never touch (or recreate) a machine that is powered off in a simulation.
            raise StepFailed("device %s is offline" % self.role)
        return adapter.Relay(self.log, self.env(), cwd=str(common.REPO_ROOT), role=self.role)

    def capture(self, path: Path, label: str) -> (Dict[str, Any], str):
        st = cs.capture_state(Path(path), raw_dir=self.results / "captures" / (label + "-raw"), label=label)
        rel = "captures/%s.json" % label
        common.write_json(self.results / rel, st)
        return st, rel

    def save(self) -> None:
        common.write_json(self.progress_path, self.progress)

    def step_out(self, step_id: str) -> Dict[str, Any]:
        rec = self.progress["steps"].get(step_id) or {}
        return rec.get("out") or {}

    def read_state(self) -> Dict[str, Any]:
        """Read-only view of relay_state.json (no keys or credentials live there)."""
        return common.read_json(self.state_dir / "relay_state.json", {}) or {}

    @property
    def device_id(self) -> str:
        return str(self.progress.get("deviceId") or "")


# ---------------------------------------------------------------- humans


class InteractiveHuman(object):
    """Real devices: the person at the keyboard confirms fingerprints and network changes."""

    def __init__(self, fingerprints: Dict[str, str], interactive: bool):
        self.fingerprints = dict(fingerprints)
        self.interactive = interactive

    def confirm_fingerprint(self, rt: "Runtime", dev: Device, peer_role: str, shown: str) -> Optional[str]:
        print("")
        print("[%s] Pairing fingerprint for %s as shown on THIS device: %s" % (dev.role, rt.alias(peer_role), shown))
        if peer_role in self.fingerprints:
            print("[%s] Using the value passed with --fingerprint %s=..." % (dev.role, peer_role))
            return self.fingerprints[peer_role]
        if not self.interactive:
            return None
        print("Read the pairing fingerprint for %s that the %s device shows, and type it here." % (dev.alias, peer_role))
        print("Do not copy the value above. Leave empty to stop without accepting.")
        try:
            value = input("fingerprint shown on %s> " % peer_role).strip()
        except EOFError:
            value = ""
        return value or None

    def pause(self, message: str) -> None:
        print("")
        print("=" * 72)
        print(message)
        print("=" * 72)
        if not self.interactive:
            raise NeedsHuman(message)
        try:
            input("Press Enter when done... ")
        except EOFError:
            raise NeedsHuman(message)


class SimulatedHuman(object):
    """Simulation only: the 'human' reads the other instance's own display."""

    def confirm_fingerprint(self, rt: "Runtime", dev: Device, peer_role: str, shown: str) -> Optional[str]:
        peer = rt.devices[peer_role]
        r = peer.relay.run("pair_republish", tag="display-for-%s" % dev.role)
        view = r.json() or {}
        for p in view.get("peers") or []:
            if p.get("alias") == dev.alias:
                return p.get("pairFingerprint") or None
        return None

    def pause(self, message: str) -> None:  # pragma: no cover - simulation never pauses
        raise NeedsHuman(message)


# ---------------------------------------------------------------- runtime


class Runtime(object):
    def __init__(
        self,
        doc: Dict[str, Any],
        mode: str,
        storage: adapter.StorageSpec,
        devices: Dict[str, Device],
        human: Any,
        results: Path,
        poll_timeout: float = 3600.0,
        poll_interval: float = 10.0,
        relay_overrides: Optional[Dict[str, str]] = None,
    ):
        self.doc = doc
        self.mode = mode
        self.storage = storage
        self.devices = devices
        self.human = human
        self.results = Path(results)
        self.poll_timeout = poll_timeout
        self.poll_interval = poll_interval
        self.relay_overrides = relay_overrides or {}
        self.shared: Dict[str, Dict[str, Any]] = {}
        self.sim_root: Optional[Path] = None
        self._storage_mode: Optional[int] = None

    @property
    def sim(self) -> bool:
        return self.mode == "simulation"

    def alias(self, role: str) -> str:
        return self.doc["roles"][role]["alias"]

    def project_kind(self, project: str) -> str:
        return self.doc["projects"][project]["kind"]

    def step_by_id(self, step_id: str) -> Dict[str, Any]:
        for s in self.doc["steps"]:
            if s["id"] == step_id:
                return s
        raise KeyError(step_id)

    def poll(self, fn: Callable[[], Any], what: str) -> Any:
        value = fn()
        if value or self.sim:
            return value
        deadline = time.time() + self.poll_timeout
        last_note = 0.0
        while not value and time.time() < deadline:
            if time.time() - last_note > 60:
                print("  ... waiting for %s (polling every %ds, timeout %ds)" % (what, self.poll_interval, self.poll_timeout))
                last_note = time.time()
            time.sleep(self.poll_interval)
            value = fn()
        return value

    def peer_device_id(self, dev: Device, role: str) -> str:
        alias = self.alias(role)
        if role in self.devices and self.devices[role].device_id:
            return self.devices[role].device_id
        r = dev.relay.run("pair_status", tag="peers")
        for p in (r.json() or {}).get("peers") or []:
            if p.get("alias") == alias:
                return str(p.get("deviceId") or "")
        return ""

    # simulation-only machine controls ---------------------------------
    def power_off(self, dev: Device) -> None:
        off = dev.workdir.with_name(dev.workdir.name + ".offline")
        os.rename(str(dev.workdir), str(off))
        dev.online = False

    def power_on(self, dev: Device) -> None:
        off = dev.workdir.with_name(dev.workdir.name + ".offline")
        os.rename(str(off), str(dev.workdir))
        dev.online = True

    def storage_down(self) -> None:
        root = Path(self.storage.root)
        self._storage_mode = stat.S_IMODE(os.stat(str(root)).st_mode)
        os.chmod(str(root), 0)

    def storage_up(self) -> None:
        root = Path(self.storage.root)
        os.chmod(str(root), self._storage_mode if self._storage_mode is not None else 0o755)
        self._storage_mode = None


# ---------------------------------------------------------------- helpers used by actions


def confirm_and_accept(relay: adapter.Relay, peer_alias: str, shown_fp: str, confirmed_fp: Optional[str]) -> common.CmdResult:
    """Pin a peer key only with a human-confirmed fingerprint equal to this device's display."""
    if not confirmed_fp:
        raise NeedsHuman("No fingerprint was confirmed by a human for %s; nothing was accepted." % peer_alias)
    if not shown_fp or norm_fp(confirmed_fp) != norm_fp(shown_fp):
        raise StepFailed(
            "Fingerprint mismatch for %s: this device shows %s, the other device shows %s. Nothing was accepted."
            % (peer_alias, shown_fp, confirmed_fp)
        )
    return relay.run("pair_accept_confirmed", peer=peer_alias, fingerprint=confirmed_fp)


def _find_peer(view: Dict[str, Any], alias: str) -> Optional[Dict[str, Any]]:
    for p in view.get("peers") or []:
        if p.get("alias") == alias:
            return p
    return None


def _fault_env(fault: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not fault:
        return {}
    env = {"RELAY_DESK_FAULT_POINT": fault["point"]}
    if fault.get("mode") == "sigkill":
        env["RELAY_DESK_FAULT_MODE"] = "sigkill"
    return env


def _killed(rc: Any) -> bool:
    return rc in (-9, 137)


def _expect_refusal(ck: Checks, r: common.CmdResult, step: Dict[str, Any], label: str) -> List[str]:
    expect = step.get("expect", "ok")
    classes = adapter.classify_error(r.stderr)
    if expect == "killed":
        ck.expect("processKilled", _killed(r.rc), rc=r.rc, stderr=tail(r.stderr, 300))
    else:
        ck.expect(label + "Refused", r.rc not in (0,), rc=r.rc, stderr=tail(r.stderr, 300))
    if step.get("expectError"):
        ck.expect("errorKind", step["expectError"] in classes, expected=step["expectError"], classified=classes)
    return classes


def _inbox_items(dev: Device, project: str, from_alias: Optional[str]) -> List[Dict[str, Any]]:
    r = dev.relay.run("inbox", tag="find")
    data = r.json() or {}
    items = []
    for it in data.get("incoming") or []:
        if it.get("tampered") or it.get("projectId") != project:
            continue
        if from_alias and it.get("fromAlias") != from_alias:
            continue
        items.append(it)
    return items


def _assert_no_receipt(rt: Runtime, dev: Device, rid: str, ck: Checks) -> None:
    keys = rt.storage.list_keys("v3/inbox/%s/%s" % (dev.device_id, rid))
    if keys is None:
        ck.expect("noCloudReceipt", False, note="storage unreachable: cannot verify, not counted as passed")
    else:
        ck.expect("noCloudReceipt", not any(k.endswith("/receipt.json") for k in keys), keys=keys)
    rec = (dev.read_state().get("receivedRelays") or {}).get(rid) or {}
    ck.expect("noLocalReceiptUpload", not rec.get("receiptUploaded"), receiptUploaded=rec.get("receiptUploaded"))


def _status(dev: Device, project: str) -> Dict[str, Any]:
    r = dev.relay.run("status_project", project=project)
    return r.json() or {}


def _scribble_out(dev: Device, project: str) -> Optional[Dict[str, Any]]:
    for sid, rec in dev.progress["steps"].items():
        out = rec.get("out") or {}
        if out.get("scribble") and out.get("project") == project:
            return out
    return None


def _check_scribble(dev: Device, project: str, ck: Checks) -> None:
    out = _scribble_out(dev, project)
    if not out:
        ck.expect("scribbleKept", False, note="no scribble step recorded")
        return
    path = Path(out["scribblePath"])
    sha = common.sha256_file(path) if path.exists() else None
    ck.expect("scribbleKept", sha == out["scribble"]["sha256"], path=str(path))


def _sender_send_for(rt: Runtime, rid: str) -> Optional[Dict[str, Any]]:
    for sid, out in rt.shared.items():
        if out.get("relayId") == rid and out.get("send"):
            return out
    return None


# ---------------------------------------------------------------- actions


def act_setup(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    env_kind = "local-simulation" if rt.sim else "native"
    common.write_json(dev.results / "env.json", collect_env(dev.workdir, kind=env_kind))
    r = dev.relay.run("init", alias=dev.alias, ledger=dev.ledger)
    ck.expect("init", r.ok, rc=r.rc, stderr=tail(r.stderr, 300))
    kw = rt.storage.pair_kwargs()
    key = kw.pop("key")
    r = dev.relay.run(key, **kw)
    data = r.json() or {}
    ck.expect("publishDescriptor", r.ok and bool(data.get("deviceId")), rc=r.rc, stderr=tail(r.stderr, 300))
    dev.progress["deviceId"] = data.get("deviceId")
    dev.progress["keyFingerprint"] = data.get("keyFingerprint")
    return {"deviceId": data.get("deviceId"), "keyFingerprint": data.get("keyFingerprint"), "storage": rt.storage.describe()}


def act_pair_accept(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    peer_role = step["peer"]
    peer_alias = rt.alias(peer_role)
    require = step.get("requireStatus")
    seen: Dict[str, Any] = {}

    def look() -> Optional[Dict[str, Any]]:
        view = dev.relay.run("pair_republish", tag="discover").json() or {}
        peer = _find_peer(view, peer_alias)
        seen["peer"] = peer
        if peer is None:
            return None
        if require and peer.get("status") != require:
            return None
        return peer

    peer = rt.poll(look, "%s descriptor%s" % (peer_alias, (" with status " + require) if require else ""))
    if not peer:
        raise StepFailed("peer %s not found (last view: %r)" % (peer_alias, seen.get("peer")))
    if not require and peer.get("paired") and peer.get("status") == "paired":
        ck.expect("alreadyPaired", True)
        return {"peer": peer_alias, "alreadyPaired": True, "pairFingerprint": peer.get("pairFingerprint")}
    shown = str(peer.get("pairFingerprint") or "")
    confirmed = rt.human.confirm_fingerprint(rt, dev, peer_role, shown)
    source = "simulated: value read from the %s instance's own display" % peer_role if rt.sim else "human"
    r = confirm_and_accept(dev.relay, peer_alias, shown, confirmed)
    data = r.json() or {}
    after = _find_peer(data, peer_alias) or {}
    ck.expect("accepted", r.ok and after.get("paired") and after.get("status") == "paired",
              rc=r.rc, stderr=tail(r.stderr, 300))
    ck.expect("bothDisplaysEqual", norm_fp(shown) == norm_fp(confirmed))
    return {"peer": peer_alias, "pairFingerprint": shown, "confirmationSource": source,
            "statusBefore": peer.get("status"), "keyFingerprint": after.get("keyFingerprint")}


def act_make_fixture(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    project = step["project"]
    ws = dev.ws(project)
    kind = rt.project_kind(project)
    manifest = fixtures.make_git_fixture(ws, owner=dev.role) if kind == "git" else fixtures.make_plain_fixture(ws, owner=dev.role)
    r = dev.relay.run("add_project", name=rt.doc["projects"][project]["name"], path=ws, project=project)
    ck.expect("registered", r.ok, rc=r.rc, stderr=tail(r.stderr, 300))
    dev.progress["paths"][project] = str(ws)
    return {"project": project, "path": str(ws), "fixture": manifest}


def act_send(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    project, to_alias = step["project"], rt.alias(step["to"])
    ws = dev.project_path(project)
    pre, pre_rel = dev.capture(ws, "%s-sender" % step["id"])
    summary = cs.transfer_summary(pre)
    r = dev.relay.run("note", project=project, note=cs.encode_note(summary))
    ck.expect("summaryNoteRecorded", r.ok, rc=r.rc)
    r = dev.relay.run("send", project=project, to=to_alias)
    out: Dict[str, Any] = {"project": project, "to": to_alias, "capture": pre_rel, "summary": summary}
    if step.get("expect", "ok") == "ok":
        data = r.json() or {}
        ck.expect("sendOk", r.ok and data.get("ok"), rc=r.rc, stderr=tail(r.stderr, 300))
        ck.expect("sentToTarget", data.get("to") == to_alias, to=data.get("to"))
        out.update({k: data.get(k) for k in ("relayId", "snapshotId", "packageSha256", "toDeviceId", "projectId",
                                             "parentRelayId", "excludedFiles")})
        out["send"] = data
    else:
        out["errorClasses"] = _expect_refusal(ck, r, step, "send")
        out["stderr"] = tail(r.stderr, 400)
    return out


def _resolve_incoming(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks):
    project = step["project"]
    item: Optional[Dict[str, Any]] = None
    if step.get("relayFromStep"):
        prev = dev.step_out(step["relayFromStep"])
        rid = prev.get("relayId")
        item = prev.get("inboxItem")
        if not rid:
            raise StepFailed("step %s recorded no relay id" % step["relayFromStep"])
    else:
        from_alias = rt.alias(step["from"]) if step.get("from") else None
        items = rt.poll(lambda: _inbox_items(dev, project, from_alias), "a %s handoff from %s" % (project, from_alias))
        if not items:
            raise StepFailed("no incoming %s handoff from %s" % (project, from_alias))
        if len(items) > 1:
            ck.expect("singleIncoming", False, relayIds=[i.get("relayId") for i in items])
        item = items[-1]
        rid = item["relayId"]
    return rid, item


def act_receive(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    project = step["project"]
    expect = step.get("expect", "ok")
    rid, item = _resolve_incoming(rt, dev, step, ck)
    target = step.get("target", "new")
    if target == "new":
        dest = dev.ws(project)
        key, kw = "receive_new", {"path": dest}
    elif target == "into":
        dest = dev.ws(project, "-into-" + step["id"])
        key, kw = "receive_into", {"path": dest}
    else:
        dest = dev.project_path(project)
        key, kw = "receive", {}
    pre_state, pre_rel = (None, None)
    if dest.exists() or step.get("assertWorkspaceUnchanged"):
        pre_state, pre_rel = dev.capture(dest, "%s-receiver-pre" % step["id"])
    original = dev.progress["paths"].get(project)
    r = dev.relay.run(key, relay_id=rid, extra_env=_fault_env(step.get("fault")), **kw)
    out: Dict[str, Any] = {"project": project, "relayId": rid, "inboxItem": item, "target": target,
                           "dest": str(dest), "preCapture": pre_rel, "rc": r.rc}
    if expect != "ok":
        out["errorClasses"] = _expect_refusal(ck, r, step, "receive")
        out["stderr"] = tail(r.stderr, 400)
        if step.get("assertWorkspaceUnchanged") and pre_state is not None:
            after, after_rel = dev.capture(dest, "%s-receiver-after" % step["id"])
            delta = cs.state_delta(pre_state, after)
            ck.expect("workspaceUnchanged", not delta, delta=delta)
            out["afterCapture"] = after_rel
        if step.get("assertNoReceipt"):
            _assert_no_receipt(rt, dev, rid, ck)
        return out
    data = r.json() or {}
    ck.expect("receiveOk", r.ok and data.get("ok"), rc=r.rc, stderr=tail(r.stderr, 400))
    if not (r.ok and data.get("ok")):
        return out
    return _after_receive_ok(rt, dev, step, ck, rid, item, data, pre_state, dest, out, original)


def _after_receive_ok(rt, dev, step, ck, rid, item, data, pre_state, dest, out, original) -> Dict[str, Any]:
    project = step["project"]
    target = step.get("target", "new")
    dest = Path(data.get("targetPath") or dest)
    post, post_rel = dev.capture(dest, "%s-receiver" % step["id"])
    out.update({"receive": data, "capture": post_rel, "dest": str(dest)})
    # 1. On-device check against the sender's summary carried inside the encrypted package
    sender_summary = cs.decode_note(data.get("note") or "")
    ck.expect("senderSummaryCarried", sender_summary is not None)
    if sender_summary is not None:
        mine = cs.transfer_summary(post)
        mine["markersSeen"] = sorted((post.get("credentialMarkers") or {}).keys())
        verdict = cs.compare_summaries(sender_summary, mine)
        common.write_json(dev.results / "captures" / ("%s-verify.json" % step["id"]), verdict)
        ck.expect("matchesSenderSummary", verdict["ok"], failed=verdict["failed"], warnings=verdict["warnings"])
        out["verify"] = {"ok": verdict["ok"], "failed": verdict["failed"], "warnings": verdict["warnings"]}
    # 2. Simulation: full comparison against the sender's capture right away
    if rt.sim:
        sender = _sender_send_for(rt, rid)
        if sender:
            sdev = rt.devices[sender["role"]]
            s_state = common.read_json(sdev.results / sender["capture"])
            report = cs.compare_states(s_state, post, pre_state)
            common.write_json(rt.results / "compare" / ("%s-%s.json" % (step["id"], rid)), report)
            ck.expect("fullStateComparison", report["ok"], failed=report["failed"], warnings=report["warnings"])
    # 3. Receipt and history
    rec = (dev.read_state().get("receivedRelays") or {}).get(rid)
    raw = rt.storage.read_bytes("v3/inbox/%s/%s/receipt.json" % (dev.device_id, rid))
    cloud = None
    if raw is not None:
        try:
            cloud = json.loads(raw.decode("utf-8"))
        except ValueError:
            cloud = None
    send_view = _sender_send_for(rt, rid) if rt.sim else None
    send_view = (send_view or {}).get("send") or {
        "relayId": rid,
        "packageSha256": (item or {}).get("packageSha256"),
        "snapshotId": (item or {}).get("snapshotId"),
        "toDeviceId": dev.device_id,
        "projectId": project,
    }
    hist = (_status(dev, project).get("history")) or []
    relay_rep = cs.compare_relay_records(send=send_view, receive=data, receiver_record=rec or {},
                                         receiver_history=hist, cloud_receipt=cloud)
    for c in relay_rep["checks"]:
        ck.expect(c["check"], c["ok"])
    out["receiptDigest"] = cs.sha256_bytes(cs.canonical_json((rec or {}).get("signedReceipt") or {}))
    out["cloudReceiptRead"] = cloud is not None
    if target == "into" and original and not step.get("adoptInto"):
        dev.progress["paths"][project + "@into"] = str(dest)
    else:
        dev.progress["paths"][project] = str(dest)
    if step.get("adoptInto"):
        ledger_path = ((_status(dev, project).get("paths") or {}).get(dev.alias)) or ""
        ck.expect("ledgerPathFollowsInto", cs._nfc(os.path.realpath(ledger_path)) == cs._nfc(os.path.realpath(str(dest))),
                  ledgerPath=ledger_path, dest=str(dest))
    if step.get("assertScribbleKept"):
        _check_scribble(dev, project, ck)
    return out


HOLDER_CODE = r"""
import os, sys, time
held, ready, release = sys.argv[1:4]
fh = open(held, "rb")  # on Windows: no FILE_SHARE_DELETE, so replacing this file is refused
with open(ready, "w") as r:
    r.write(str(os.getpid()))
t0 = time.time()
while not os.path.exists(release) and time.time() - t0 < 300:
    time.sleep(0.05)
fh.close()
"""


def act_receive_file_open(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    """Receive while another process keeps a workspace file open (Windows file-in-use)."""
    import subprocess

    project = step["project"]
    rid, item = _resolve_incoming(rt, dev, step, ck)
    dest = dev.project_path(project)
    held = dest / step.get("holdFile", "src/app.py")
    ready = dev.results / ("held-%s.txt" % step["id"])
    release = dev.results / ("release-%s.txt" % step["id"])
    pre_state, pre_rel = dev.capture(dest, "%s-receiver-pre" % step["id"])
    holder = subprocess.Popen([sys.executable, "-c", HOLDER_CODE, str(held), str(ready), str(release)])
    t0 = time.time()
    while not ready.exists() and time.time() - t0 < 30 and holder.poll() is None:
        time.sleep(0.05)
    ck.expect("fileHeldOpen", ready.exists(), file=str(held))
    first = dev.relay.run("receive", relay_id=rid, tag="file-open")
    release.write_text("release", encoding="utf-8")
    try:
        holder.wait(timeout=60)
    except subprocess.TimeoutExpired:
        holder.kill()
    out: Dict[str, Any] = {"project": project, "relayId": rid, "inboxItem": item, "target": "existing",
                           "dest": str(dest), "preCapture": pre_rel, "heldFile": str(held),
                           "firstAttempt": {"rc": first.rc, "classified": adapter.classify_error(first.stderr),
                                            "stderr": tail(first.stderr, 400)}}
    if first.ok:
        out["outcome"] = "succeeded-while-file-open"
        data = first.json() or {}
    else:
        status = dev.relay.run("recover_status", relay_id=rid, tag="after-file-open").json() or {}
        out["recoverStatusAfterFailure"] = {k: status.get(k) for k in ("exists", "state", "phase", "canResume", "canRollback")}
        _assert_no_receipt(rt, dev, rid, ck)
        second = dev.relay.run("receive", relay_id=rid, tag="after-release")
        out["secondAttempt"] = {"rc": second.rc, "stderr": tail(second.stderr, 400)}
        data = second.json() or {}
        out["outcome"] = "failed-then-completed-after-release" if second.ok else "failed-and-not-completed"
    ck.expect("eventuallyReceived", bool(data.get("ok")), outcome=out["outcome"])
    if not data.get("ok"):
        return out
    return _after_receive_ok(rt, dev, step, ck, rid, item, data, pre_state, dest, out, str(dest))


def act_recover(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    rid = dev.step_out(step["relayFromStep"]).get("relayId")
    if not rid:
        raise StepFailed("no relay id from %s" % step["relayFromStep"])
    key = "recover_rollback" if step.get("mode") == "rollback" else "recover_status"
    r = dev.relay.run(key, relay_id=rid, extra_env=_fault_env(step.get("fault")))
    out: Dict[str, Any] = {"relayId": rid, "mode": step.get("mode"), "rc": r.rc}
    if step.get("expect", "ok") != "ok":
        out["errorClasses"] = _expect_refusal(ck, r, step, "recover")
        out["stderr"] = tail(r.stderr, 400)
        return out
    data = r.json() or {}
    out["result"] = data
    ck.expect("recoverOk", r.ok, rc=r.rc, stderr=tail(r.stderr, 300))
    if "expectCanResume" in step:
        ck.expect("canResume", data.get("canResume") == step["expectCanResume"], actual=data.get("canResume"))
    if step.get("expectPhase"):
        ck.expect("phase", data.get("phase") == step["expectPhase"], actual=data.get("phase"))
    return out


def act_assert_cannot_claim(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    project = step["project"]
    target_role = step["target"]
    rid = rt.relay_overrides.get(step["id"])
    snap = None
    if not rid and rt.sim:
        sent = rt.shared.get(step["sendStep"]) or {}
        rid, snap = sent.get("relayId"), sent.get("snapshotId")
    if not rid:
        target_did = rt.peer_device_id(dev, target_role)

        def discover() -> Optional[str]:
            keys = rt.storage.list_keys("v3/inbox/%s" % target_did) or []
            ready = {k.split("/")[3] for k in keys if k.endswith("/ready.json") and k.split("/")[3].startswith("r_")}
            done = {k.split("/")[3] for k in keys if k.endswith("/receipt.json")}
            open_ids = sorted(ready - done)
            if len(open_ids) == 1:
                return open_ids[0]
            # The target may already have claimed it; with a single handoff ever
            # addressed to it the id is still unambiguous.
            return sorted(ready)[0] if len(ready) == 1 else None

        rid = rt.poll(discover, "exactly one open handoff addressed to %s" % target_role)
    if not rid:
        raise StepFailed("could not determine the relay id addressed to %s; pass --relay-id %s=<id>" % (target_role, step["id"]))
    # Sync first: the non-target device should hold the snapshot in its cache and
    # still be unable to claim the handoff.
    sync = dev.relay.run("sync_once", tag="before-claim")
    cached = [s.get("snapshotId") for s in (sync.json() or {}).get("syncedSnapshots") or []]
    listed = [i.get("relayId") for i in _inbox_items(dev, project, None)]
    ck.expect("notInOwnInbox", rid not in listed, inbox=listed)
    attempts = [("relay", rid)] + ([("snapshot", snap)] if snap else [])
    for i, (kind, ident) in enumerate(attempts):
        steal = dev.ws(project, "-steal-%s-%d" % (step["id"], i))
        r = dev.relay.run("receive_new", relay_id=ident, path=steal, tag="steal")
        classes = adapter.classify_error(r.stderr)
        ck.expect("claimRefused[%s]" % kind, not r.ok, rc=r.rc, classified=classes, stderr=tail(r.stderr, 200))
        if kind == "relay":
            ck.expect("unauthorized[relay]", "unauthorized_receiver" in classes, classified=classes)
        ck.expect("noWorkspaceCreated[%s]" % kind, not steal.exists())
    return {"relayId": rid, "snapshotId": snap, "snapshotCachedBeforeClaim": snap in cached if snap else None,
            "syncedSnapshots": cached}


def act_edit(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    ws = dev.project_path(step["project"])
    if rt.project_kind(step["project"]) == "git":
        info = fixtures.edit_git_workspace(ws, dev.role, int(step.get("round", 1)))
    else:
        info = fixtures.edit_plain_folder(ws, dev.role, int(step.get("round", 1)))
    ck.expect("edited", True)
    return info


def act_scribble(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    ws = dev.project_path(step["project"])
    info = fixtures.scribble(ws, dev.role)
    ck.expect("edited", True)
    return {"project": step["project"], "scribble": info, "scribblePath": str(ws / info["file"])}


def act_publish(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    r = dev.relay.run("publish", project=step["project"])
    out: Dict[str, Any] = {"project": step["project"], "rc": r.rc}
    if step.get("expect", "ok") == "ok":
        data = r.json() or {}
        ck.expect("publishOk", r.ok and data.get("ok"), rc=r.rc, stderr=tail(r.stderr, 300))
        out.update({"snapshotId": data.get("snapshotId"),
                    "deliveredTo": [d.get("alias") for d in data.get("deliveredTo") or []]})
    else:
        out["errorClasses"] = _expect_refusal(ck, r, step, "publish")
    if step.get("assertScribbleKept"):
        _check_scribble(dev, step["project"], ck)
    return out


# `relay sync` exit codes (Phase B): 0 success, 3 partial (retryable problems
# left), 4 needs user action, 130 cancelled.  A step passes on 0 unless it
# lists other statuses it expects, e.g. "allowSyncStatus": ["needs-action"].
SYNC_RC_STATUS = {0: "success", 3: "partial", 4: "needs-action", 130: "cancelled"}


def _sync_rc_ok(rc: Optional[int], step: Dict[str, Any]) -> bool:
    status = SYNC_RC_STATUS.get(rc) if rc is not None else None
    if status == "success":
        return True
    return status is not None and status in (step.get("allowSyncStatus") or [])


def act_sync(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    r = dev.relay.run("sync_once")
    data = r.json() or {}
    ck.expect("syncOk", _sync_rc_ok(r.rc, step), rc=r.rc, status=data.get("status"), stderr=tail(r.stderr, 300))
    out = {
        "syncedSnapshots": [
            {k: s.get(k) for k in ("snapshotId", "projectId", "authorDeviceId", "senderDeviceId", "isForwarded")}
            for s in data.get("syncedSnapshots") or []
        ],
        "forwarded": data.get("forwarded") or [],
        "errors": data.get("errors") or [],
        "conflicts": data.get("conflictsDetected") or [],
    }
    if step.get("expectSnapshotFrom"):
        author = rt.peer_device_id(dev, step["expectSnapshotFrom"])
        project = step.get("project")
        in_sync = any(s.get("authorDeviceId") == author and (not project or s.get("projectId") == project)
                      for s in out["syncedSnapshots"])
        nodes = (((dev.read_state().get("versionGraph") or {}).get(project or "") or {}).get("nodes") or {})
        in_graph = any(n.get("authorDeviceId") == author for n in nodes.values())
        ck.expect("snapshotFromOfflinePublisher", in_sync or in_graph, inThisSync=in_sync, inVersionGraph=in_graph)
    # Phase B hook: streaming sync output, used only when the CLI offers it.
    out["streamHook"] = dev.relay.run_optional("sync_stream", cycles=1, interval=0)
    return out


def act_go_offline(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    started = common.iso_now()
    if rt.sim:
        rt.power_off(dev)
        ck.expect("poweredOff", not dev.workdir.exists())
        return {"offlineAt": started, "method": "simulation: device directory moved away"}
    until = step.get("until", "")
    rt.human.pause(
        "Take THIS device (%s) offline now: disconnect Wi-Fi/Ethernet (or unplug).\n"
        "Then press Enter. The script checks that the storage is unreachable." % dev.role
    )
    down = rt.poll(lambda: not rt.storage.reachable(), "storage to become unreachable")
    ck.expect("storageUnreachableWhileOffline", down)
    offline_at = common.iso_now()
    rt.human.pause(
        "Stay offline. Continue on the other devices until step %s is next for this device\n"
        "(the other devices finish their steps before %s). Then reconnect this device and press Enter." % (until, until)
    )
    up = rt.poll(lambda: rt.storage.reachable(), "storage to become reachable again")
    ck.expect("storageReachableAgain", up)
    return {"offlineAt": offline_at, "onlineAt": common.iso_now(), "method": "human disconnected the network"}


def act_come_online(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    if rt.sim:
        rt.power_on(dev)
        ck.expect("poweredOn", dev.workdir.exists())
        return {"onlineAt": common.iso_now()}
    ok = rt.poll(lambda: rt.storage.reachable(), "storage reachable")
    ck.expect("storageReachable", ok)
    return {"onlineAt": common.iso_now()}


def act_network_down(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    if rt.sim:
        rt.storage_down()
        ck.expect("storageUnreachable", not rt.storage.reachable())
        return {"method": "simulation: storage folder permissions removed (proxy for network loss)"}
    rt.human.pause("Disconnect THIS device (%s) from the network now, then press Enter." % dev.role)
    down = rt.poll(lambda: not rt.storage.reachable(), "storage to become unreachable")
    ck.expect("storageUnreachable", down)
    return {"method": "human disconnected the network"}


def act_network_up(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    if rt.sim:
        rt.storage_up()
        ck.expect("storageReachable", rt.storage.reachable())
        return {"method": "simulation: storage permissions restored"}
    rt.human.pause("Reconnect THIS device (%s) to the network now, then press Enter." % dev.role)
    up = rt.poll(lambda: rt.storage.reachable(), "storage reachable")
    ck.expect("storageReachable", up)
    return {"method": "human reconnected the network"}


def act_inbox_receipts(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    sent = dev.step_out(step["expectReceiptForStep"])
    rid = sent.get("relayId")
    project = sent.get("project")
    r = dev.relay.run("inbox")
    data = r.json() or {}
    synced = [x.get("relayId") for x in data.get("syncedReceipts") or []]
    acked = bool(((dev.read_state().get("sentRelays") or {}).get(rid) or {}).get("acked"))
    ck.expect("receiptSynced", rid in synced or acked, synced=synced, acked=acked)
    st = _status(dev, project)
    hist = [h.get("relayId") or h.get("tag") for h in st.get("history") or []]
    ck.expect("senderHistoryHasRelay", rid in hist, history=hist)
    return {"relayId": rid, "syncedNow": rid in synced, "owner": st.get("device"), "handoff": st.get("handoff")}


def act_inbox_expect(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    project = step["project"]
    from_alias = rt.alias(step["from"])
    items = rt.poll(lambda: _inbox_items(dev, project, from_alias), "%s handoff from %s" % (project, from_alias)) or []
    ck.expect("incomingCount", len(items) == int(step.get("expectCount", 1)), relayIds=[i.get("relayId") for i in items])
    item = items[-1] if items else {}
    return {"project": project, "relayId": item.get("relayId"), "inboxItem": item}


def act_rotate_keys(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    before = dev.relay.run("pair_status").json() or {}
    r = dev.relay.run("rotate_keys")
    ck.expect("rotated", r.ok, rc=r.rc, stderr=tail(r.stderr, 300))
    pub = dev.relay.run("pair_republish")
    after = pub.json() or {}
    ck.expect("republished", pub.ok)
    ck.expect("keyFingerprintChanged", before.get("keyFingerprint") != after.get("keyFingerprint"),
              before=before.get("keyFingerprint"), after=after.get("keyFingerprint"))
    ck.expect("deviceIdKept", before.get("deviceId") == after.get("deviceId"))
    dev.progress["keyFingerprint"] = after.get("keyFingerprint")
    shown = {p.get("alias"): p.get("pairFingerprint") for p in after.get("peers") or []}
    print("[%s] New pairing fingerprints shown on THIS device after the key rotation:" % dev.role)
    for alias, fp in sorted(shown.items()):
        print("    %-14s %s" % (alias, fp))
    print("    The other devices must re-confirm against these values.")
    return {"before": before.get("keyFingerprint"), "after": after.get("keyFingerprint"), "pairFingerprintsShown": shown}


def act_expect_key_changed(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    alias = rt.alias(step["peer"])

    def look() -> Optional[Dict[str, Any]]:
        p = _find_peer(dev.relay.run("pair_republish", tag="watch").json() or {}, alias)
        return p if p and p.get("status") == "key_changed" else None

    peer = rt.poll(look, "%s to show key_changed" % alias)
    ck.expect("peerKeyChanged", bool(peer), view=peer)
    ck.expect("edgePaused", bool(peer) and not peer.get("paired"))
    return {"peer": alias, "newPairFingerprint": (peer or {}).get("pairFingerprint")}


def act_converge(rt: Runtime, dev: Device, step: Dict[str, Any], ck: Checks) -> Dict[str, Any]:
    inbox = dev.relay.run("inbox")
    ck.expect("inboxOk", inbox.ok, rc=inbox.rc, stderr=tail(inbox.stderr, 300))
    sync = dev.relay.run("sync_once")
    sdata = sync.json() or {}
    ck.expect("syncOk", _sync_rc_ok(sync.rc, step), rc=sync.rc, status=sdata.get("status"), stderr=tail(sync.stderr, 300))
    projects = {}
    for key, path in sorted(dev.progress["paths"].items()):
        project = key.split("@")[0]
        st = _status(dev, project)
        _, rel = dev.capture(Path(path), "%s-%s-final" % (step["id"], key.replace("@", "-")))
        projects[key] = {"owner": st.get("device"), "handoff": st.get("handoff"), "history": st.get("history"),
                             "capture": rel, "path": path}
    state = dev.read_state()
    summary = {
        "sentRelays": {rid: {k: v.get(k) for k in ("projectId", "acked", "ackedAt", "packageSha256", "snapshotId", "toDeviceId")}
                       for rid, v in (state.get("sentRelays") or {}).items() if isinstance(v, dict)},
        "receivedRelays": {rid: {"projectId": v.get("projectId"), "receiptUploaded": v.get("receiptUploaded"),
                                 "signedReceipt": v.get("signedReceipt")}
                           for rid, v in (state.get("receivedRelays") or {}).items() if isinstance(v, dict)},
        "localCurrentVersion": state.get("localCurrentVersion") or {},
        "activeHandoffs": state.get("activeHandoffs") or {},
        "pendingSends": sorted((state.get("pendingSends") or {}).keys()),
    }
    unacked = [rid for rid, v in summary["sentRelays"].items() if not v.get("acked")]
    pending_upload = [rid for rid, v in summary["receivedRelays"].items() if not v.get("receiptUploaded")]
    ck.expect("allSentRelaysAcked", not unacked, unacked=unacked)
    ck.expect("allReceiptsUploaded", not pending_upload, pending=pending_upload)
    ck.expect("noSyncErrors", not (sdata.get("errors") or []), errors=sdata.get("errors"))
    hooks = {
        "doctor": [dev.relay.run_optional("doctor", project=p) for p in sorted(projects)],
        "cacheStatus": dev.relay.run_optional("cache_status"),
        "cachePruneDryRun": dev.relay.run_optional("cache_prune_dry_run"),
    }
    common.write_json(dev.results / "captures" / ("%s-relay-state.json" % step["id"]), summary)
    return {"projects": projects, "relayState": "captures/%s-relay-state.json" % step["id"],
            "syncErrors": sdata.get("errors") or [], "hooks": hooks}


ACTIONS: Dict[str, Callable[[Runtime, Device, Dict[str, Any], Checks], Dict[str, Any]]] = {
    "setup": act_setup,
    "pair-accept": act_pair_accept,
    "make-fixture": act_make_fixture,
    "send": act_send,
    "receive": act_receive,
    "receive-while-file-open": act_receive_file_open,
    "recover": act_recover,
    "assert-cannot-claim": act_assert_cannot_claim,
    "edit": act_edit,
    "scribble": act_scribble,
    "publish": act_publish,
    "sync": act_sync,
    "go-offline": act_go_offline,
    "come-online": act_come_online,
    "network-down": act_network_down,
    "network-up": act_network_up,
    "inbox-receipts": act_inbox_receipts,
    "inbox-expect": act_inbox_expect,
    "rotate-keys": act_rotate_keys,
    "expect-key-changed": act_expect_key_changed,
    "converge": act_converge,
}


# ---------------------------------------------------------------- step execution


def run_step(rt: Runtime, step: Dict[str, Any]) -> Dict[str, Any]:
    dev = rt.devices[step["role"]]
    ck = Checks()
    started = common.iso_now()
    t0 = time.time()
    status, error, out = "passed", None, {}
    cond = step.get("onlyIfFailed")
    if cond and (dev.progress["steps"].get(cond) or {}).get("status") == "passed":
        rec = {"schema": STEP_SCHEMA, "id": step["id"], "phase": step.get("phase"), "role": step["role"],
               "alias": dev.alias, "action": step["action"], "title": step.get("title", ""), "status": "skipped",
               "startedAt": started, "endedAt": started, "durationSec": 0.0, "checks": [],
               "error": None, "out": {"reason": "%s passed; fallback not needed" % cond}}
        common.append_jsonl(dev.results / "steps.jsonl", rec)
        if rt.sim:
            common.append_jsonl(rt.results / "steps.jsonl", rec)
        dev.progress["steps"][step["id"]] = {"status": "skipped", "out": rec["out"]}
        print("[%s] %-6s %-20s %-12s" % (step["id"], dev.role, step["action"], "skipped"))
        return rec
    try:
        if not dev.online and step["action"] != "come-online":
            raise StepFailed("device %s is offline in this simulation" % dev.role)
        out = ACTIONS[step["action"]](rt, dev, step, ck) or {}
        if ck.failed():
            status = "failed"
    except NeedsHuman as e:
        status, error = "needs-human", str(e)
    except StepFailed as e:
        status, error = "failed", str(e)
    except Exception as e:  # keep going; the record says exactly what broke
        status, error = "error", "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-1500:])
    rec = {
        "schema": STEP_SCHEMA,
        "id": step["id"],
        "phase": step.get("phase"),
        "role": step["role"],
        "alias": dev.alias,
        "action": step["action"],
        "title": step.get("title", ""),
        "status": status,
        "startedAt": started,
        "endedAt": common.iso_now(),
        "durationSec": round(time.time() - t0, 3),
        "checks": ck.items,
        "error": error,
        "out": out,
    }
    common.append_jsonl(dev.results / "steps.jsonl", rec)
    if rt.sim:
        common.append_jsonl(rt.results / "steps.jsonl", rec)
        out = dict(out)
        out["role"] = dev.role
        rt.shared[step["id"]] = out
    if dev.online:
        dev.progress["steps"][step["id"]] = {"status": status, "out": rec["out"]}
        dev.save()
    else:
        dev.progress["steps"][step["id"]] = {"status": status, "out": rec["out"]}
    print("[%s] %-6s %-20s %-12s %6.1fs  %s" % (step["id"], dev.role, step["action"], status, rec["durationSec"],
                                               (error or ", ".join(ck.failed()))[:160]))
    return rec


def write_device_summary(rt: Runtime, dev: Device, level: str, info: Dict[str, Any], source: Dict[str, Any],
                         topology: str = "") -> None:
    common.write_json(
        dev.results / "device-summary.json",
        {
            "schema": DEVICE_SCHEMA,
            "updatedAt": common.iso_now(),
            "role": dev.role,
            "alias": dev.alias,
            "level": level,
            "topology": topology or ("single-machine-multiinstance" if rt.sim else "multi-device"),
            "simulation": rt.sim,
            "platform": info,
            "machineId": common.machine_fingerprint(),
            "roleSystemExpected": common.ROLE_SYSTEMS.get(dev.role),
            "platformMatchesRole": common.role_matches_platform(dev.role, info) and not rt.sim,
            "source": source,
            "storage": rt.storage.describe() if rt.storage.kind == "rclone" else {"kind": "local", "root": rt.storage.root},
            "deviceId": dev.device_id,
            "workdir": str(dev.workdir),
            "steps": {sid: rec.get("status") for sid, rec in sorted(dev.progress["steps"].items())},
        },
    )


# ---------------------------------------------------------------- aggregate


def _load_device_dir(path: Path) -> Dict[str, Any]:
    summary = common.read_json(path / "device-summary.json", {}) or {}
    steps: Dict[str, Dict[str, Any]] = {}
    fp = path / "steps.jsonl"
    if fp.exists():
        for line in fp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                steps[rec["id"]] = rec  # the last attempt of a step wins
    return {"dir": path, "summary": summary, "steps": steps}


def aggregate(device_dirs: List[Path], out_dir: Path, doc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    doc = doc or json.loads(DEFAULT_STEPS.read_text(encoding="utf-8"))
    devs = [_load_device_dir(Path(d)) for d in device_dirs]
    by_role = {d["summary"].get("role"): d for d in devs}
    level = common.aggregate_ring_level([d["summary"] for d in devs])
    digests = sorted({(d["summary"].get("source") or {}).get("sourceDigest") for d in devs})
    status: Dict[str, str] = {}
    all_steps: Dict[str, Dict[str, Any]] = {}
    for step in doc["steps"]:
        d = by_role.get(step["role"])
        rec = (d or {}).get("steps", {}).get(step["id"]) if d else None
        status[step["id"]] = rec["status"] if rec else "not-run"
        if rec:
            all_steps[step["id"]] = rec
    # Cross-device state and receipt comparison for every completed handoff.
    sends: Dict[str, Dict[str, Any]] = {}
    for sid, rec in all_steps.items():
        if rec["action"] == "send" and rec["status"] == "passed" and (rec.get("out") or {}).get("relayId"):
            sends[rec["out"]["relayId"]] = rec
    final_state: Dict[str, Dict[str, Any]] = {}
    histories: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for d in devs:
        role = d["summary"].get("role")
        for sid, rec in d["steps"].items():
            if rec["action"] == "converge" and rec.get("out"):
                final_state[role] = common.read_json(d["dir"] / rec["out"]["relayState"], {}) or {}
                histories[role] = {p: v.get("history") or [] for p, v in (rec["out"].get("projects") or {}).items()}
    comparisons = []
    compare_dir = Path(out_dir) / "compare-aggregate"
    for sid, rec in sorted(all_steps.items()):
        out = rec.get("out") or {}
        if rec["action"] != "receive" or rec["status"] != "passed" or not out.get("receive"):
            continue
        rid = out["relayId"]
        send = sends.get(rid)
        entry = {"receiveStep": sid, "relayId": rid, "sendStep": send["id"] if send else None}
        if not send:
            entry.update({"ok": False, "reason": "matching send step not found"})
            comparisons.append(entry)
            continue
        s_dir = by_role[send["role"]]["dir"]
        r_dir = by_role[rec["role"]]["dir"]
        s_state = common.read_json(s_dir / send["out"]["capture"])
        r_state = common.read_json(r_dir / out["capture"])
        r_pre = common.read_json(r_dir / out["preCapture"]) if out.get("preCapture") else None
        if not isinstance(s_state, dict) or not isinstance(r_state, dict) or (out.get("preCapture") and r_pre is None):
            entry.update({"ok": False, "from": send["role"], "to": rec["role"],
                          "reason": "capture file missing or unreadable; comparison not performed"})
            comparisons.append(entry)
            continue
        project = out.get("project")
        relay = {
            "send": send["out"].get("send"),
            "receive": out.get("receive"),
            "receiver_record": (final_state.get(rec["role"], {}).get("receivedRelays") or {}).get(rid),
            "sender_record": (final_state.get(send["role"], {}).get("sentRelays") or {}).get(rid),
            "receiver_history": histories.get(rec["role"], {}).get(project),
            "sender_history": histories.get(send["role"], {}).get(project),
        }
        report = cs.compare_states(s_state, r_state, r_pre, relay=relay)
        common.write_json(compare_dir / ("%s-%s.json" % (sid, rid)), report)
        entry.update({"ok": report["ok"], "failed": report["failed"], "warnings": report["warnings"],
                      "from": send["role"], "to": rec["role"]})
        comparisons.append(entry)
    scenarios = {}
    for name, ids in (doc.get("scenarios") or {}).items():
        sts = [status.get(i, "not-run") for i in ids]
        scenarios[name] = "passed" if all(s in ("passed", "skipped") for s in sts) else (
            "not-run" if all(s == "not-run" for s in sts) else "failed-or-incomplete")
    counts: Dict[str, int] = {}
    for s in status.values():
        counts[s] = counts.get(s, 0) + 1
    result = {
        "schema": RING_SCHEMA,
        "createdAt": common.iso_now(),
        "level": level["level"],
        "levelReasons": level["reasons"],
        "sourceDigests": digests,
        "sameSource": len(digests) == 1,
        "devices": {
            d["summary"].get("role"): {
                "dir": str(d["dir"]),
                "level": d["summary"].get("level"),
                "machineId": d["summary"].get("machineId"),
                "system": (d["summary"].get("platform") or {}).get("system"),
                "platformMatchesRole": d["summary"].get("platformMatchesRole"),
                "storage": d["summary"].get("storage"),
            }
            for d in devs
        },
        "stepCounts": counts,
        "steps": status,
        "scenarios": scenarios,
        "crossDeviceComparisons": comparisons,
        "allPassed": all(s in ("passed", "skipped") for s in status.values()) and all(c.get("ok") for c in comparisons),
    }
    out_dir = Path(out_dir)
    common.write_json(out_dir / "ring-summary.json", result)
    (out_dir / "ring-summary.md").write_text(render_summary_md(result, doc), encoding="utf-8")
    return result


def render_summary_md(res: Dict[str, Any], doc: Dict[str, Any]) -> str:
    lines = [
        "# relay-desk ring acceptance summary",
        "",
        "- level: `%s` (%s)" % (res["level"], "; ".join(res["levelReasons"])),
        "- source digest(s): %s" % ", ".join("`%s`" % d for d in res["sourceDigests"]),
        "- all passed: %s" % res["allPassed"],
        "- step counts: %s" % ", ".join("%s=%d" % kv for kv in sorted(res["stepCounts"].items())),
        "- source changed during run: %s" % res.get("sourceChangedDuringRun", "not recorded"),
        "",
        "## Scenarios",
        "",
        "| scenario | result |",
        "| --- | --- |",
    ]
    for name, st in res["scenarios"].items():
        lines.append("| %s | %s |" % (name, st))
    lines += ["", "## Steps", "", "| step | device | action | result | title |", "| --- | --- | --- | --- | --- |"]
    for step in doc["steps"]:
        lines.append("| %s | %s | %s | %s | %s |" % (step["id"], step["role"], step["action"],
                                                    res["steps"].get(step["id"], "not-run"), step.get("title", "")))
    lines += ["", "## Cross-device state comparisons", "", "| receive | relay | from -> to | ok | failed | warnings |",
              "| --- | --- | --- | --- | --- | --- |"]
    for c in res["crossDeviceComparisons"]:
        lines.append("| %s | %s | %s -> %s | %s | %s | %s |" % (
            c["receiveStep"], c["relayId"], c.get("from"), c.get("to"), c.get("ok"),
            ", ".join(c.get("failed") or []), ", ".join(c.get("warnings") or [])))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- runbook


HUMAN_ACTIONS = {
    "pair-accept": "核对两台设备显示的配对指纹完全一致，在本机输入对方屏幕上的指纹 / compare both screens, type the value shown on the other device",
    "go-offline": "按提示断网并保持离线，直到提示的步骤再联网 / disconnect and stay offline until told",
    "network-down": "按提示断网 / disconnect the network when asked",
    "network-up": "按提示恢复网络 / reconnect when asked",
}


def render_runbook(doc: Dict[str, Any], run_id: str = "<RUN-ID>") -> str:
    lines = [
        "# relay-desk 三设备接力验收 Runbook / three-device ring runbook",
        "",
        "Every device runs its own command once; the script waits for the others through the shared storage.",
        "",
        "| device | command |",
        "| --- | --- |",
    ]
    for role, spec in doc["roles"].items():
        launcher = "py -3" if role == "win" else "python3"
        work = ("$env:USERPROFILE\\relay-acceptance\\%s" % run_id) if role == "win" else "~/relay-acceptance/%s" % run_id
        lines.append(
            "| %s (%s) | `%s acceptance/ring.py device --role %s --workdir %s --storage rclone:<REMOTE>:relay-acceptance/%s` |"
            % (role, spec["system"], launcher, role, work, run_id)
        )
    lines += ["", "| step | device | action | needs a human | what happens |", "| --- | --- | --- | --- | --- |"]
    for s in doc["steps"]:
        lines.append("| %s | %s | %s | %s | %s |" % (s["id"], s["role"], s["action"],
                                                    HUMAN_ACTIONS.get(s["action"], ""), s.get("title", "")))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- entry points


def load_doc(path: Optional[str]) -> Dict[str, Any]:
    return json.loads(Path(path or DEFAULT_STEPS).read_text(encoding="utf-8"))


def run_local(
    doc: Dict[str, Any],
    results: Path,
    level: str,
    topology: str,
    note: str,
    sim_root: Optional[Path] = None,
    keep_sim_root: bool = False,
    only_phase: Optional[List[str]] = None,
    stop_on_fail: bool = False,
) -> Dict[str, Any]:
    """Run a step file with isolated device instances on this machine.

    Used by ``simulate`` (three roles, level ``<os>-local-simulation``) and by
    the native runner's two-instance loopback (level ``native-<os>`` with
    topology ``single-machine-loopback``).
    """
    info = common.platform_info()
    source = common.source_identity()
    own_root = sim_root is None
    sim_root = Path(sim_root) if sim_root else Path(tempfile.mkdtemp(prefix="relay-acc-local-"))
    sim_root.mkdir(parents=True, exist_ok=True)
    storage = adapter.StorageSpec("local", str(sim_root / "cloud"))
    storage.prepare_local()
    devices = {
        role: Device(role, spec, sim_root / "machines" / role, results / "devices" / role, sim=True)
        for role, spec in doc["roles"].items()
    }
    rt = Runtime(doc, "simulation", storage, devices, SimulatedHuman(), results)
    rt.sim_root = sim_root
    common.write_json(results / "simulation.json", {
        "schema": "relay-desk-acceptance-simulation/1",
        "level": level,
        "topology": topology,
        "note": note,
        "platform": info,
        "source": source,
        "simRoot": str(sim_root),
        "storage": storage.describe(),
        "osLabels": {r: s.get("osLabel") for r, s in doc["roles"].items()},
        "networkLossProxy": "chmod 000 on the shared folder",
        "offlineProxy": "device directory renamed away",
        "fingerprintConfirmation": "value read from the other instance's own pair display; accepted only when equal",
    })
    selected = [s for s in doc["steps"] if not only_phase or s.get("phase") in only_phase]
    print("relay-desk local run: level=%s topology=%s digest=%s results=%s"
          % (level, topology, source["sourceDigest"][:12], results))
    try:
        for step in selected:
            rec = run_step(rt, step)
            if stop_on_fail and rec["status"] not in ("passed", "skipped"):
                break
    finally:
        if rt._storage_mode is not None:
            rt.storage_up()
        for dev in devices.values():
            if not dev.online:
                rt.power_on(dev)
            write_device_summary(rt, dev, level, info, source, topology)
    res = aggregate([results / "devices" / r for r in doc["roles"]], results, doc)
    # The code under test must not change while a run is in progress; say so if it did.
    end_source = common.source_identity()
    sim_meta = common.read_json(results / "simulation.json", {}) or {}
    sim_meta["sourceAtEnd"] = end_source
    sim_meta["sourceChangedDuringRun"] = end_source["sourceDigest"] != source["sourceDigest"]
    common.write_json(results / "simulation.json", sim_meta)
    res["sourceChangedDuringRun"] = sim_meta["sourceChangedDuringRun"]
    summary_path = results / "ring-summary.json"
    ring_summary = common.read_json(summary_path, {}) or {}
    ring_summary["sourceChangedDuringRun"] = sim_meta["sourceChangedDuringRun"]
    common.write_json(summary_path, ring_summary)
    (results / "ring-summary.md").write_text(render_summary_md(ring_summary, doc), encoding="utf-8")
    print("aggregate: level=%s allPassed=%s counts=%s" % (res["level"], res["allPassed"], res["stepCounts"]))
    if sim_meta["sourceChangedDuringRun"]:
        print("WARNING: the source digest changed during this run (%s -> %s); rerun on a frozen tree."
              % (source["sourceDigest"][:12], end_source["sourceDigest"][:12]))
    if own_root and not keep_sim_root:
        shutil.rmtree(str(sim_root), ignore_errors=True)
    return res


def cmd_simulate(args: argparse.Namespace) -> int:
    doc = load_doc(args.steps)
    info = common.platform_info()
    level = common.detect_level("local-simulation", info)
    source = common.source_identity()
    results = common.make_results_dir(Path(args.results_root or common.default_results_root()), level,
                                      source["sourceDigest"], "ring-sim")
    common.write_json(results / "env.json", collect_env(results, kind="local-simulation"))
    note = ("Single-machine simulation: %d isolated device instances (own RELAY_DESK_HOME, ledger, user home, HOME) "
            "on %s with a local shared folder as storage. Role OS labels are simulated via RELAY_DESK_OS; nothing "
            "here is native Windows/Linux or real multi-device evidence." % (len(doc["roles"]), info["system"]))
    res = run_local(doc, results, level, "single-machine-multiinstance", note,
                    sim_root=Path(args.sim_root) if args.sim_root else None, keep_sim_root=args.keep_sim_root,
                    only_phase=args.only_phase, stop_on_fail=args.stop_on_fail)
    print("results: %s" % results)
    return 0 if res["allPassed"] else 1


def _parse_pairs(values: Optional[List[str]], what: str) -> Dict[str, str]:
    out = {}
    for v in values or []:
        if "=" not in v:
            raise SystemExit("%s must look like KEY=VALUE: %r" % (what, v))
        k, val = v.split("=", 1)
        out[k.strip()] = val.strip()
    return out


def cmd_device(args: argparse.Namespace) -> int:
    doc = load_doc(args.steps)
    role = args.role
    if role not in doc["roles"]:
        raise SystemExit("unknown role %r (choose from %s)" % (role, ", ".join(doc["roles"])))
    info = common.platform_info()
    matches = common.role_matches_platform(role, info)
    if not matches and not args.allow_role_mismatch:
        raise SystemExit(
            "Role %r expects %s but this machine is %s. Refusing to run: a mislabelled run cannot count as native or "
            "real multi-device evidence. Use 'ring.py simulate' instead, or --allow-role-mismatch to run anyway "
            "(results will be labelled role-mismatch-simulation)." % (role, common.ROLE_SYSTEMS[role], info["system"])
        )
    if not matches:
        print("WARNING: role/platform mismatch; this device's results will be labelled role-mismatch-simulation.")
    storage = adapter.StorageSpec.parse(args.storage)
    if storage.kind == "local":
        storage.prepare_local()
    level = common.detect_level("native", info)
    source = common.source_identity()
    workdir = Path(args.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    meta_path = workdir / "run.json"
    meta = common.read_json(meta_path, {}) or {}
    if meta and meta.get("sourceDigest") != source["sourceDigest"]:
        raise SystemExit("This workdir was started with source %s but the code is now %s. Start a new run in a new "
                         "workdir so results stay bound to one source digest." % (meta.get("sourceDigest", "")[:12],
                                                                                 source["sourceDigest"][:12]))
    if not meta:
        res_root = Path(args.results_root) if args.results_root else workdir / "results"
        results = common.make_results_dir(res_root, level, source["sourceDigest"], "ring-%s" % role)
        meta = {"role": role, "sourceDigest": source["sourceDigest"], "results": str(results),
                "storage": storage.describe(), "createdAt": common.iso_now()}
        common.write_json(meta_path, meta)
    results = Path(meta["results"])
    dev = Device(role, doc["roles"][role], workdir / "device", results, sim=False)
    human = InteractiveHuman(_parse_pairs(args.fingerprint, "--fingerprint"), interactive=not args.non_interactive)
    rt = Runtime(doc, "device", storage, {role: dev}, human, results, poll_timeout=args.poll_timeout,
                 poll_interval=args.poll_interval, relay_overrides=_parse_pairs(args.relay_id, "--relay-id"))
    mine = [s for s in doc["steps"] if s["role"] == role]
    if args.only:
        mine = [s for s in mine if s["id"] in args.only]
    if args.from_step:
        ids = [s["id"] for s in mine]
        mine = mine[ids.index(args.from_step):] if args.from_step in ids else mine
    print("relay-desk ring device run: role=%s level=%s digest=%s results=%s" % (role, level, source["sourceDigest"][:12], results))
    rc = 0
    try:
        for step in mine:
            prev = dev.progress["steps"].get(step["id"], {}).get("status")
            if prev == "passed" and not args.redo:
                print("[%s] already passed, skipping" % step["id"])
                continue
            rec = run_step(rt, step)
            write_device_summary(rt, dev, level, info, source)
            if rec["status"] == "needs-human":
                print("Stopped: this step needs a human decision. Re-run the same command to continue.")
                rc = 3
                break
            if rec["status"] != "passed":
                rc = 1
                if not args.keep_going:
                    print("Stopped after a failed step. Fix or record it, then re-run (passed steps are skipped).")
                    break
    finally:
        write_device_summary(rt, dev, level, info, source)
    print("results folder to send back: %s" % results)
    return rc


def cmd_runbook(args: argparse.Namespace) -> int:
    text = render_runbook(load_doc(args.steps), args.run_id)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    res = aggregate([Path(d) for d in args.dirs], out, load_doc(args.steps))
    print(json.dumps({k: res[k] for k in ("level", "levelReasons", "allPassed", "stepCounts", "scenarios")},
                     ensure_ascii=False, indent=2))
    return 0 if res["allPassed"] else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="relay-desk three-device ring acceptance")
    ap.add_argument("--steps", help="step file (default acceptance/steps/ring-3device.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("simulate", help="run the whole ring on this machine (local simulation)")
    s.add_argument("--results-root")
    s.add_argument("--sim-root", help="keep simulated machines and storage here")
    s.add_argument("--keep-sim-root", action="store_true")
    s.add_argument("--only-phase", action="append")
    s.add_argument("--stop-on-fail", action="store_true")
    d = sub.add_parser("device", help="run this device's steps on a real machine")
    d.add_argument("--role", required=True, choices=sorted(common.ROLE_SYSTEMS))
    d.add_argument("--workdir", required=True)
    d.add_argument("--storage", required=True, help="rclone:<remote>:relay-acceptance/<run> or local:<shared folder>")
    d.add_argument("--results-root")
    d.add_argument("--fingerprint", action="append", help="ROLE=FP read off that device's screen (repeatable)")
    d.add_argument("--relay-id", action="append", help="STEP=RELAY_ID override for assert-cannot-claim")
    d.add_argument("--only", action="append")
    d.add_argument("--from-step")
    d.add_argument("--redo", action="store_true")
    d.add_argument("--keep-going", action="store_true")
    d.add_argument("--non-interactive", action="store_true")
    d.add_argument("--poll-timeout", type=float, default=3600.0)
    d.add_argument("--poll-interval", type=float, default=10.0)
    d.add_argument("--allow-role-mismatch", action="store_true")
    r = sub.add_parser("runbook", help="print the human runbook")
    r.add_argument("--run-id", default="<RUN-ID>")
    r.add_argument("--out")
    a = sub.add_parser("aggregate", help="merge per-device results folders")
    a.add_argument("dirs", nargs="+")
    a.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    return {"simulate": cmd_simulate, "device": cmd_device, "runbook": cmd_runbook, "aggregate": cmd_aggregate}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
