#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase B loopback soak: several `relay sync --watch --jsonl` devices under injected faults.

    python3 acceptance/sync_soak.py --minutes 30 --out /private/tmp/relay-soak --seed 7 \
        [--backends local,webdav] [--interval 2] [--max-backoff 30]

For each backend a three-device network (alpha, beta, gamma, fully paired) is
built in ``--out`` with synthetic data only. Every device runs
``acceptance/soak_child.py -- sync --watch --jsonl`` as a subprocess while the
driver:

* publishes from the current owner of the folder project every ``--publish-every`` s;
* runs a handoff ring alpha -> beta -> gamma -> alpha through the real
  ``relay send`` / ``relay receive`` every ``--ring-every`` s;
* injects faults: ``local`` -- a global outage of 20 s every 2 min, a random
  failure rate, a persistent failure of one peer's inbox/have, a held
  workspace lock (local busy) and a forged delivery (integrity), all through
  ``FlakyProvider`` in the child; ``webdav`` -- the stdlib/rclone WebDAV server
  from ``tests/cloud_servers.py`` behind ``FaultProxy`` (outage = connection
  resets, plus a random reset rate);
* samples each child's RSS every ``--rss-every`` s with ``ps``.

At the end faults stop, the owner publishes once more, the driver waits for
convergence (final snapshot cached everywhere, every handoff acknowledged,
no unresolved problem), sends SIGINT to every child (expects exit 130 and a
final ``summary`` line) and verifies the disk. Output: ``soak-report.json``,
``soak-summary.md``, per-device ``events-*.jsonl`` / ``rss-*.csv`` /
``audit-*.json`` and command logs. Never touches ``~/.relay-desk``, real
remotes or the user's rclone configuration.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from acceptance.soak_faults import ControlFile, FaultProxy  # noqa: E402
from relaydesk import ledger as L  # noqa: E402
from relaydesk import relay_engine  # noqa: E402

ALIASES = ("alpha", "beta", "gamma")
PROJECT = "soak"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rss_kb(pid: int) -> Optional[int]:
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout
        return int(out.strip()) if out.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def slope_mb_per_hour(points: List[List[float]]) -> Optional[float]:
    """Least-squares slope of (seconds, kB) points in MB/h."""
    if len(points) < 3:
        return None
    n = float(len(points))
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    den = n * sxx - sx * sx
    if den == 0:
        return None
    kb_per_s = (n * sxy - sx * sy) / den
    return kb_per_s * 3600.0 / 1024.0


class Log(object):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def __call__(self, msg: str) -> None:
        line = "%s %s" % (now_iso(), msg)
        with self.lock:
            with open(str(self.path), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        print(line, flush=True)


class Device(object):
    def __init__(self, net: "Network", alias: str) -> None:
        self.net = net
        self.alias = alias
        root = net.root
        self.home = root / ("home_%s" % alias)
        self.uhome = root / ("uhome_%s" % alias)
        self.led = root / ("ledger_%s.json" % alias)
        self.work = root / ("work_%s" % alias)
        self.events = net.out / ("events-%s-%s.jsonl" % (net.name, alias))
        self.stderr = net.out / ("stderr-%s-%s.log" % (net.name, alias))
        self.audit = net.out / ("audit-%s-%s.json" % (net.name, alias))
        self.rss_csv = net.out / ("rss-%s-%s.csv" % (net.name, alias))
        self.proc: Optional[subprocess.Popen] = None
        self.device_id = ""
        self.rss: List[List[float]] = []

    def env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env.update({
            "RELAY_DESK_HOME": str(self.home),
            "RELAY_DESK_LEDGER": str(self.led),
            "RELAY_DESK_DEVICE": self.alias,
            "RELAY_DESK_USER_HOME": str(self.uhome),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        if self.net.rclone_conf:
            env["RCLONE_CONFIG"] = str(self.net.rclone_conf)
        return env

    def init(self) -> None:
        for d in (self.home, self.uhome):
            d.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(json.dumps({"device": self.alias, "ledger": str(self.led)}),
                                               encoding="utf-8")

        def _init_led(l: Dict[str, Any]) -> None:
            l["config"] = L.norm_config({"devices": [{"id": self.alias, "name": "Soak %s" % self.alias, "os": "mac",
                                                       "drop": "", "host": "", "deviceId": ""}]})

        L.update(self.led, _init_led)

    def engine_env(self):
        return mock.patch.dict(os.environ, self.env(), clear=True)

    def relay(self, *argv: str, timeout: float = 300.0) -> Dict[str, Any]:
        cmd = [sys.executable, str(REPO_ROOT / "relay.py")] + list(argv)
        started = time.monotonic()
        proc = subprocess.run(cmd, env=self.env(), capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        res = {"argv": list(argv), "rc": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-2000:],
               "seconds": round(time.monotonic() - started, 2), "device": self.alias, "network": self.net.name}
        try:
            res["json"] = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else None
        except ValueError:
            res["json"] = None
        self.net.commands.append({k: v for k, v in res.items() if k != "json"})
        with open(str(self.net.out / ("commands-%s.jsonl" % self.net.name)), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({k: v for k, v in res.items() if k != "json"}, ensure_ascii=False) + "\n")
        return res

    def start(self, interval: float, max_backoff: float, stats: float, seed: int) -> None:
        cmd = [sys.executable, str(REPO_ROOT / "acceptance" / "soak_child.py"), "--audit", str(self.audit),
               "--stats", str(stats), "--seed", str(seed)]
        if self.net.control_path:
            cmd += ["--control", str(self.net.control_path)]
        cmd += ["--", "sync", "--watch", "--jsonl", "--interval", str(interval), "--max-backoff", str(max_backoff)]
        self.proc = subprocess.Popen(cmd, env=self.env(), stdout=open(str(self.events), "wb"),
                                     stderr=open(str(self.stderr), "wb"), start_new_session=True)

    def state(self) -> Dict[str, Any]:
        try:
            return json.loads((self.home / "relay_state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def cached(self) -> Dict[str, Path]:
        root = self.home / "cache" / "snapshots"
        if not root.is_dir():
            return {}
        return {p.name: p for p in root.iterdir() if (p / "payload.tar.gz").is_file()}

    def unresolved_from_events(self) -> Dict[str, Dict[str, Any]]:
        open_set: Dict[str, Dict[str, Any]] = {}
        try:
            with open(str(self.events), "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("kind") == "problem":
                        open_set[ev.get("key")] = ev
                    elif ev.get("kind") == "resolved":
                        open_set.pop(ev.get("key"), None)
        except OSError:
            pass
        return open_set

    @contextlib.contextmanager
    def hold_workspace_lock(self):
        self.home.mkdir(parents=True, exist_ok=True)
        with open(str(self.home / "workspace-operation.lock"), "a+b") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


class Network(object):
    def __init__(self, name: str, out: Path, rng: random.Random, log: Log) -> None:
        self.name = name
        self.out = out
        self.root = out / ("net-%s" % name)
        self.rng = rng
        self.log = log
        self.devices = {a: Device(self, a) for a in ALIASES}
        self.cloud_dir = self.root / "cloud_shared"
        self.control_path: Optional[Path] = None
        self.rclone_conf: Optional[Path] = None
        self.proxy: Optional[FaultProxy] = None
        self.server_cm = None
        self.server_env = None
        self.commands: List[Dict[str, Any]] = []
        self.published: List[Dict[str, Any]] = []
        self.handoffs: List[Dict[str, Any]] = []
        self.owner = "alpha"
        self.in_flight: Optional[Dict[str, Any]] = None
        self.fault_log: List[Dict[str, Any]] = []
        self.last_fault_end = 0.0
        self.received_count = 0

    # ---- setup
    def setup(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.name == "local":
            self.cloud_dir.mkdir(parents=True, exist_ok=True)
            self.control_path = self.out / "control-local.json"
            ControlFile.write(str(self.control_path), {"outage": False, "failRate": 0.0, "failPatterns": {}})
        else:
            from cloud_servers import webdav_server

            self.server_cm = webdav_server(base_dir=str(self.root / "server"), shared_root="relay-space")
            self.server_env = self.server_cm.__enter__()
            self.proxy = FaultProxy(self.server_env.port, seed=self.rng.randrange(1 << 30)).start()
            self.rclone_conf = self.root / "rclone-soak.conf"
            vendor = "rclone" if self.server_env.backend == "rclone" else "other"
            self.rclone_conf.write_text("[soak_dav]\ntype = webdav\nurl = http://127.0.0.1:%d/\nvendor = %s\n"
                                        % (self.proxy.port, vendor), encoding="utf-8")
            self.log("[%s] WebDAV backend=%s server port %d via fault proxy %d" % (
                self.name, self.server_env.backend, self.server_env.port, self.proxy.port))
        for dev in self.devices.values():
            dev.init()
        for dev in self.devices.values():
            with dev.engine_env():
                st = self._pair_call(dev)
            dev.device_id = st["deviceId"]
        for a, b in (("alpha", "beta"), ("alpha", "gamma"), ("beta", "gamma")):
            self._pair_pair(self.devices[a], self.devices[b])
        alpha = self.devices["alpha"]
        alpha.work.mkdir(parents=True, exist_ok=True)
        (alpha.work / "README.md").write_text("# soak project\n", encoding="utf-8")
        (alpha.work / "notes.txt").write_text("round 0\n", encoding="utf-8")

        def _reg(l: Dict[str, Any]) -> None:
            l["projects"].append(L.norm_project({"id": PROJECT, "name": "Soak 项目", "device": "alpha", "tool": "all",
                                                 "lastTask": "soak", "nextAction": "keep syncing",
                                                 "paths": {"alpha": str(alpha.work)}}, l["config"]))

        L.update(alpha.led, _reg)

    def _pair_call(self, dev: Device, **kw: Any) -> Dict[str, Any]:
        if self.name == "local":
            return relay_engine.pair_device(root=str(self.cloud_dir), provider_type="local", **kw)
        return relay_engine.pair_device(remote="soak_dav", root="relay-space", provider_type="rclone",
                                        rclone_config=str(self.rclone_conf), **kw)

    def _pair_pair(self, a: Device, b: Device) -> None:
        for x, y in ((a, b), (b, a)):
            with x.engine_env():
                view = relay_engine.pair_device(with_device=y.device_id, accept=False)
                fp = next(p for p in view["peers"] if p["deviceId"] == y.device_id)["pairFingerprint"]
                res = relay_engine.pair_device(with_device=y.device_id, accept=True, fingerprint=fp)
                assert any(p["deviceId"] == y.device_id and p["paired"] for p in res["peers"]), res

    # ---- faults
    def set_outage(self, on: bool) -> None:
        if self.name == "local":
            data = json.loads(self.control_path.read_text(encoding="utf-8"))
            data["outage"] = bool(on)
            ControlFile.write(str(self.control_path), data)
        else:
            self.proxy.set("reset" if on else "pass")
        self.fault_log.append({"at": now_iso(), "fault": "outage", "on": bool(on)})
        if not on:
            self.last_fault_end = time.monotonic()

    def set_random_rate(self, rate: float) -> None:
        if self.name == "local":
            data = json.loads(self.control_path.read_text(encoding="utf-8"))
            data["failRate"] = float(rate)
            ControlFile.write(str(self.control_path), data)
        else:
            self.proxy.set(self.proxy.mode, reset_rate=rate)
        self.fault_log.append({"at": now_iso(), "fault": "randomRate", "rate": rate})

    def set_peer_fault(self, alias: Optional[str]) -> None:
        if self.name != "local":
            return
        data = json.loads(self.control_path.read_text(encoding="utf-8"))
        if alias:
            did = self.devices[alias].device_id
            data["failPatterns"] = {"v3/inbox/%s/" % did: "*", "v3/have/%s.json" % did: "*"}
        else:
            data["failPatterns"] = {}
            self.last_fault_end = time.monotonic()
        ControlFile.write(str(self.control_path), data)
        self.fault_log.append({"at": now_iso(), "fault": "peerIsolation", "peer": alias or ""})

    def stop_faults(self) -> None:
        if self.name == "local":
            ControlFile.write(str(self.control_path), {"outage": False, "failRate": 0.0, "failPatterns": {}})
        else:
            self.proxy.set("pass", reset_rate=0.0)
        self.fault_log.append({"at": now_iso(), "fault": "allStopped"})
        self.last_fault_end = time.monotonic()

    def forge_delivery(self, alias: str, present: bool) -> None:
        """An integrity fault: garbage in a device's inbox, later removed."""
        if self.name != "local":
            return
        d = self.cloud_dir / "v3" / "inbox" / self.devices[alias].device_id / "sync_forged_soak_0001"
        if present:
            d.mkdir(parents=True, exist_ok=True)
            (d / "ready.json").write_text("{not json", encoding="utf-8")
            (d / "package.enc").write_bytes(os.urandom(64))
        else:
            shutil.rmtree(str(d), ignore_errors=True)
        self.fault_log.append({"at": now_iso(), "fault": "forgedDelivery", "device": alias, "present": present})

    # ---- workload
    def retry(self, dev: Device, argv: List[str], attempts: int = 12, pause: float = 3.0,
              ok_codes=(0,)) -> Dict[str, Any]:
        res: Dict[str, Any] = {}
        for i in range(attempts):
            res = dev.relay(*argv)
            if res["rc"] in ok_codes:
                return res
            time.sleep(pause)
        return res

    def publish(self) -> Optional[Dict[str, Any]]:
        if self.in_flight is not None:
            return None
        dev = self.devices[self.owner]
        work = self._work_dir(dev)
        if work is None:
            return None
        (work / "notes.txt").write_text("publish %d by %s at %s\n" % (len(self.published) + 1, dev.alias, now_iso()),
                                        encoding="utf-8")
        res = self.retry(dev, ["publish", PROJECT, "--json"], attempts=6)
        js = res.get("json") or {}
        if res["rc"] == 0 and js.get("snapshotId"):
            rec = {"snapshotId": js["snapshotId"], "author": dev.alias, "authorDeviceId": js.get("deviceId"),
                   "contentSha256": js.get("contentSha256"), "deliveredTo": [d.get("alias") for d in js.get("deliveredTo") or []],
                   "deliveryErrors": len(js.get("deliveryErrors") or []), "status": js.get("status"), "at": now_iso()}
            self.published.append(rec)
            self.log("[%s] publish by %s -> %s (status %s, failed peers %d)" % (
                self.name, dev.alias, rec["snapshotId"], rec["status"], rec["deliveryErrors"]))
            return rec
        self.log("[%s] publish by %s failed rc=%s: %s" % (self.name, dev.alias, res["rc"], res["stderr"][-300:]))
        return None

    def _work_dir(self, dev: Device) -> Optional[Path]:
        try:
            led = L.load(dev.led)
        except Exception:
            return None
        for p in led.get("projects") or []:
            if p.get("id") == PROJECT:
                path = (p.get("paths") or {}).get(dev.alias) or (p.get("pathsByDeviceId") or {}).get(dev.device_id)
                return Path(path) if path else None
        return None

    def handoff(self, to_alias: str) -> bool:
        src = self.devices[self.owner]
        dst = self.devices[to_alias]
        res = self.retry(src, ["send", PROJECT, "--to", to_alias, "--json"], attempts=8)
        js = res.get("json") or {}
        if res["rc"] != 0 or not js.get("relayId"):
            self.log("[%s] send %s->%s failed rc=%s: %s" % (self.name, src.alias, to_alias, res["rc"], res["stderr"][-300:]))
            return False
        rid = js["relayId"]
        self.in_flight = {"relayId": rid, "from": src.alias, "to": to_alias, "sentAt": now_iso()}
        self.log("[%s] send %s -> %s relay %s" % (self.name, src.alias, to_alias, rid))
        time.sleep(3)
        argv = ["receive", rid, "--json"]
        if self._work_dir(dst) is None:
            argv = ["receive", rid, "--path", str(dst.work), "--json"]
        res = self.retry(dst, argv, attempts=10)
        if res["rc"] != 0 and "need_path" in (res["stderr"] + res["stdout"]):
            res = self.retry(dst, ["receive", rid, "--path", str(dst.work), "--json"], attempts=6)
        if res["rc"] != 0:
            alt = self.root / ("work_%s_%d" % (to_alias, len(self.handoffs)))
            self.log("[%s] receive %s in place failed (%s); retry --into %s" % (self.name, rid, res["stderr"][-200:], alt))
            res = self.retry(dst, ["receive", rid, "--into", str(alt), "--json"], attempts=6)
        ok = res["rc"] == 0
        self.handoffs.append({"relayId": rid, "from": src.alias, "to": to_alias, "received": ok,
                              "receivedAt": now_iso() if ok else "", "rc": res["rc"]})
        if ok:
            self.owner = to_alias
            self.received_count += 1
            self.in_flight = None
            self.log("[%s] receive %s by %s ok" % (self.name, rid, to_alias))
        else:
            self.log("[%s] receive %s by %s FAILED rc=%s: %s" % (self.name, rid, to_alias, res["rc"], res["stderr"][-400:]))
        return ok

    def ring(self) -> None:
        order = {"alpha": "beta", "beta": "gamma", "gamma": "alpha"}
        start = self.owner
        for _ in range(3):
            if not self.handoff(order[self.owner]):
                return
            time.sleep(10)
        assert self.owner == start

    # ---- convergence and verification
    def all_acked(self) -> bool:
        for h in self.handoffs:
            if not h["received"]:
                continue
            st = self.devices[h["from"]].state()
            info = (st.get("sentRelays") or {}).get(h["relayId"]) or {}
            if not info.get("acked"):
                return False
        return True

    def converged(self) -> Dict[str, Any]:
        final = self.published[-1]["snapshotId"] if self.published else ""
        missing = {a: (final not in d.cached()) for a, d in self.devices.items()} if final else {}
        unresolved = {a: sorted(d.unresolved_from_events().keys()) for a, d in self.devices.items()}
        acked = self.all_acked()
        ok = bool(final) and not any(missing.values()) and not any(unresolved.values()) and acked
        return {"ok": ok, "finalSnapshot": final, "missingFinal": missing, "unresolved": unresolved, "allAcked": acked}


def parse_events(path: Path) -> Dict[str, Any]:
    summary = None
    kinds: Dict[str, int] = {}
    max_ring = 0
    max_unresolved = 0
    stats_points = 0
    bad_lines = 0
    last_seq = 0
    seq_ok = True
    heap: List[List[float]] = []
    t_first = None
    with open(str(path), "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                bad_lines += 1
                continue
            k = ev.get("kind", "")
            kinds[k] = kinds.get(k, 0) + 1
            seq = int(ev.get("seq") or 0)
            if seq and seq != last_seq + 1:
                seq_ok = False
            last_seq = seq or last_seq
            if k == "stats":
                stats_points += 1
                try:
                    t = time.mktime(time.strptime(str(ev.get("at"))[:19], "%Y-%m-%dT%H:%M:%S"))
                    t_first = t if t_first is None else t_first
                    if ev.get("allocatedBlocks") is not None:
                        heap.append([t - t_first, float(ev["allocatedBlocks"])])
                except (TypeError, ValueError):
                    pass
                max_ring = max(max_ring, int(ev.get("ringSize") or 0))
                max_unresolved = max(max_unresolved, int(ev.get("unresolved") or 0))
            if k == "summary":
                summary = ev.get("summary")
    return {"summary": summary, "kinds": kinds, "maxRingSize": max_ring, "maxUnresolved": max_unresolved,
            "statsEvents": stats_points, "badLines": bad_lines, "lastSeq": last_seq, "seqContiguous": seq_ok,
            "heap": heap}


def run(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    log = Log(out / "driver.log")
    rng = random.Random(args.seed)
    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    if "webdav" in backends and not shutil.which("rclone"):
        log("rclone not found: webdav backend skipped")
        backends = [b for b in backends if b != "webdav"]
    env_info = {
        "startedAt": now_iso(), "seed": args.seed, "minutes": args.minutes, "backends": backends,
        "interval": args.interval, "maxBackoff": args.max_backoff, "python": sys.version.split()[0],
        "platform": platform.platform(), "rclone": "",
    }
    try:
        env_info["rclone"] = subprocess.run(["rclone", "version"], capture_output=True, text=True,
                                            timeout=20).stdout.splitlines()[0]
    except (OSError, IndexError, subprocess.SubprocessError):
        pass
    digest = subprocess.run([sys.executable, str(REPO_ROOT / "acceptance" / "source_digest.py")],
                            capture_output=True, text=True, cwd=str(REPO_ROOT))
    (out / "source-digest-at-soak-start.json").write_text(digest.stdout, encoding="utf-8")
    try:
        env_info["sourceDigest"] = json.loads(digest.stdout).get("sourceDigest")
    except ValueError:
        env_info["sourceDigest"] = ""
    log("soak start: %s" % json.dumps(env_info, ensure_ascii=False))

    nets = [Network(b, out, random.Random(rng.randrange(1 << 30)), log) for b in backends]
    for net in nets:
        net.setup()
        log("[%s] network ready: %s" % (net.name, {a: d.device_id for a, d in net.devices.items()}))
        for i, dev in enumerate(net.devices.values()):
            interval = args.interval if net.name == "local" else max(args.interval, 3.0)
            dev.start(interval, args.max_backoff, args.stats, args.seed + i)
        net.publish()

    stop_sampling = threading.Event()
    t0 = time.monotonic()

    def sampler() -> None:
        while not stop_sampling.is_set():
            elapsed = time.monotonic() - t0
            for net in nets:
                for dev in net.devices.values():
                    if dev.proc is not None and dev.proc.poll() is None:
                        kb = rss_kb(dev.proc.pid)
                        if kb is not None:
                            dev.rss.append([round(elapsed, 1), kb])
                            with open(str(dev.rss_csv), "a", encoding="utf-8") as fh:
                                fh.write("%.1f,%d\n" % (elapsed, kb))
            stop_sampling.wait(args.rss_every)

    threading.Thread(target=sampler, daemon=True).start()

    duration = args.minutes * 60.0
    for net in nets:
        net.set_random_rate(args.fail_rate)
    stop_faults = threading.Event()

    def fault_schedule() -> None:
        """Faults follow wall-clock windows independently of the (blocking) workload below."""
        outage_period, outage_len = 120.0, 20.0
        peer_window = (duration * 0.2, duration * 0.2 + min(180.0, duration * 0.1))
        forged_window = (duration * 0.13, duration * 0.4)
        busy_every, next_busy = 300.0, 90.0
        outage_on = peer_on = forged_on = False
        while not stop_faults.is_set():
            el = time.monotonic() - t0
            if el >= duration:
                break
            want = (el % outage_period) >= (outage_period - outage_len)
            if want != outage_on:
                for net in nets:
                    net.set_outage(want)
                outage_on = want
                log("outage %s" % ("ON" if want else "off"))
            want = peer_window[0] <= el < peer_window[1]
            if want != peer_on:
                for net in nets:
                    net.set_peer_fault("beta" if want else None)
                peer_on = want
                log("peer isolation fault on beta %s" % ("ON" if want else "off"))
            want = forged_window[0] <= el < forged_window[1]
            if want != forged_on:
                for net in nets:
                    net.forge_delivery("gamma", want)
                forged_on = want
                log("forged delivery for gamma %s" % ("present" if want else "removed"))
            if el >= next_busy:
                next_busy = el + busy_every
                for net in nets:
                    if net.name == "local":
                        with net.devices["beta"].hold_workspace_lock():
                            log("[%s] holding beta's workspace lock for 8 s" % net.name)
                            time.sleep(8)
                        net.fault_log.append({"at": now_iso(), "fault": "busyLock", "device": "beta", "seconds": 8})
            stop_faults.wait(0.5)
        for net in nets:
            if outage_on:
                net.set_outage(False)

    faults = threading.Thread(target=fault_schedule, daemon=True)
    faults.start()
    next_publish = t0 + args.publish_every
    next_ring = t0 + min(150.0, duration / 4)
    while True:
        now = time.monotonic()
        el = now - t0
        if el >= duration:
            break
        if now >= next_ring and el < duration - 120:
            next_ring = now + args.ring_every
            for net in nets:
                log("[%s] handoff ring starts (owner %s)" % (net.name, net.owner))
                net.ring()
        elif now >= next_publish:
            next_publish = now + args.publish_every
            for net in nets:
                net.publish()
        for net in nets:
            for dev in net.devices.values():
                if dev.proc is not None and dev.proc.poll() is not None and not getattr(dev, "_exit_logged", False):
                    dev._exit_logged = True
                    log("[%s] child %s exited early with %s" % (net.name, dev.alias, dev.proc.returncode))
        time.sleep(1.0)
    stop_faults.set()
    faults.join(timeout=30)

    # ---- wind down
    for net in nets:
        net.stop_faults()
        net.forge_delivery("gamma", False)
    log("faults stopped; final publish and convergence wait")
    fault_end = time.monotonic()
    for net in nets:
        if net.in_flight is not None:
            log("[%s] handoff still in flight: %s" % (net.name, net.in_flight))
        net.publish()
    converge_limit = 3.0 * args.max_backoff + 30.0
    conv: Dict[str, Any] = {}
    for net in nets:
        deadline = time.monotonic() + converge_limit
        state = net.converged()
        while not state["ok"] and time.monotonic() < deadline:
            time.sleep(2)
            state = net.converged()
        state["seconds"] = round(time.monotonic() - fault_end, 1)
        conv[net.name] = state
        log("[%s] convergence: %s" % (net.name, json.dumps(state, ensure_ascii=False)))
    stop_sampling.set()

    exits: Dict[str, Any] = {}
    for net in nets:
        for dev in net.devices.values():
            if dev.proc is None:
                continue
            if dev.proc.poll() is None:
                os.kill(dev.proc.pid, signal.SIGINT)
        for dev in net.devices.values():
            if dev.proc is None:
                continue
            try:
                code = dev.proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                dev.proc.kill()
                code = dev.proc.wait()
            exits["%s/%s" % (net.name, dev.alias)] = code
    log("child exit codes: %s" % exits)

    report: Dict[str, Any] = {"environment": env_info, "networks": {}, "thresholds": {
        "rssSlopeMbPerHour": 5.0, "finalRssFactor": 1.5, "ringMax": 200, "convergenceSec": 3 * args.max_backoff}}
    all_pass = True
    for net in nets:
        nrep: Dict[str, Any] = {"devices": {}, "published": net.published, "handoffs": net.handoffs,
                                "faults": net.fault_log, "convergence": conv[net.name],
                                "commandsFailed": sum(1 for c in net.commands if c["rc"] != 0),
                                "commands": len(net.commands)}
        if net.proxy is not None:
            nrep["proxyStats"] = dict(net.proxy.stats)
        authors = {p["snapshotId"]: p for p in net.published}
        for alias, dev in net.devices.items():
            ev = parse_events(dev.events)
            s = ev["summary"] or {}
            c = s.get("counters") or {}
            try:
                audit = json.loads(dev.audit.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                audit = {}
            # cache digests: every cached payload matches its signed contentSha256
            digest_bad = []
            for sid, d in dev.cached().items():
                try:
                    snap = json.loads((d / "snapshot.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    digest_bad.append(sid)
                    continue
                if sha256_file(d / "payload.tar.gz") != str(snap.get("contentSha256") or "").lower():
                    digest_bad.append(sid)
                if sid in authors and authors[sid].get("contentSha256") not in (None, snap.get("contentSha256")):
                    digest_bad.append(sid)
            st = dev.state()
            history_dups = []
            try:
                led = L.load(dev.led)
                for p in led.get("projects") or []:
                    tags = [h.get("relayId") or h.get("tag") for h in (p.get("history") or []) if isinstance(h, dict)]
                    history_dups += sorted({t for t in tags if t and tags.count(t) > 1})
            except Exception:
                pass
            pts = dev.rss
            m5 = [p for p in pts if p[0] >= 300.0]
            rss5 = m5[0][1] if m5 else (pts[0][1] if pts else None)
            rss_end = pts[-1][1] if pts else None
            tail = [p for p in pts if p[0] >= duration / 3.0]
            slope = slope_mb_per_hour(tail)
            heap = ev["heap"]
            heap5 = [h for h in heap if h[0] >= 300.0]
            heap_tail = [h for h in heap if h[0] >= duration / 3.0]
            blocks_slope = slope_mb_per_hour(heap_tail)  # same least squares; unit converted below
            dev_rep = {
                "deviceId": dev.device_id,
                "heapBlocksAtMinute5": int(heap5[0][1]) if heap5 else None,
                "heapBlocksAtEnd": int(heap[-1][1]) if heap else None,
                "heapBlocksSlopePerHourLastTwoThirds": round(blocks_slope * 1024.0, 1) if blocks_slope is not None else None,
                "exitCode": exits.get("%s/%s" % (net.name, alias)),
                "finalStatus": s.get("status"),
                "cycles": s.get("cycles"),
                "counters": c,
                "retriesScheduled": c.get("retriesScheduled"),
                "failedOperations": c.get("failedOperations"),
                "unresolvedAtEnd": s.get("unresolved"),
                "recentEventsInSummary": None,
                "maxRingSize": ev["maxRingSize"],
                "maxUnresolved": ev["maxUnresolved"],
                "eventKinds": ev["kinds"],
                "eventsSeqContiguous": ev["seqContiguous"],
                "badJsonLines": ev["badLines"],
                "rssSamples": len(pts),
                "rssKbAtMinute5": rss5,
                "rssKbAtEnd": rss_end,
                "rssKbMax": max((p[1] for p in pts), default=None),
                "rssSlopeMbPerHourLastTwoThirds": round(slope, 3) if slope is not None else None,
                "cachedSnapshots": len(dev.cached()),
                "publishedCachedHere": sum(1 for sid in authors if sid in dev.cached()),
                "cacheDigestMismatches": digest_bad,
                "receiptUploadsBySync": audit.get("receiptUploads"),
                "workspaceApplicationsBySync": audit.get("workspaceApplications"),
                "injectedFaults": audit.get("injectedFaults"),
                "storageCalls": audit.get("storageCalls"),
                "receivedRelays": sorted((st.get("receivedRelays") or {}).keys()),
                "ledgerHistoryDuplicates": history_dups,
            }
            checks = {
                "exit130": dev_rep["exitCode"] == 130,
                "summaryLine": bool(s),
                "noUnresolved": not s.get("unresolved"),
                "ringBounded": ev["maxRingSize"] <= 200,
                "noReceiptBySync": not audit.get("receiptUploads"),
                "noWorkspaceApplyBySync": audit.get("workspaceApplications") == 0,
                "cacheDigestsOk": not digest_bad,
                "noHistoryDuplicates": not history_dups,
                "rssSlopeOk": slope is None or slope < 5.0,
                "rssFinalOk": (rss5 is None or rss_end is None) or rss_end < 1.5 * rss5,
            }
            dev_rep["checks"] = checks
            dev_rep["pass"] = all(checks.values())
            all_pass = all_pass and dev_rep["pass"]
            nrep["devices"][alias] = dev_rep
        nrep["pass"] = conv[net.name]["ok"] and conv[net.name]["seconds"] <= 3 * args.max_backoff + 30 and \
            all(d["pass"] for d in nrep["devices"].values())
        nrep["handoffsReceived"] = sum(1 for h in net.handoffs if h["received"])
        all_pass = all_pass and nrep["pass"]
        report["networks"][net.name] = nrep
    report["pass"] = all_pass
    report["finishedAt"] = now_iso()
    (out / "soak-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    write_markdown(out / "soak-summary.md", report)
    for net in nets:
        if net.proxy is not None:
            net.proxy.stop()
        if net.server_cm is not None:
            try:
                net.server_cm.__exit__(None, None, None)
            except Exception:
                pass
    log("soak finished: pass=%s report=%s" % (all_pass, out / "soak-report.json"))
    return 0 if all_pass else 1


def write_markdown(path: Path, report: Dict[str, Any]) -> None:
    env = report["environment"]
    lines = ["# Phase B sync soak", "",
             "- started %s, finished %s, %s minutes, seed %s" % (env["startedAt"], report.get("finishedAt"), env["minutes"], env["seed"]),
             "- source digest `%s`; Python %s; %s; %s" % (env.get("sourceDigest"), env["python"], env["platform"], env.get("rclone")),
             "- interval %s s, max backoff %s s; overall pass: **%s**" % (env["interval"], env["maxBackoff"], report["pass"]),
             ""]
    for name, net in report["networks"].items():
        conv = net["convergence"]
        lines += ["## backend `%s` (pass: %s)" % (name, net["pass"]), "",
                  "- published %d snapshots, handoffs received %d/%d, driver commands %d (non-zero rc %d)" % (
                      len(net["published"]), net["handoffsReceived"], len(net["handoffs"]), net["commands"], net["commandsFailed"]),
                  "- convergence after faults stopped: ok=%s in %.1f s (final snapshot %s)" % (
                      conv["ok"], conv["seconds"], conv.get("finalSnapshot")),
                  ""]
        if net.get("proxyStats"):
            lines.append("- proxy: %s" % net["proxyStats"])
            lines.append("")
        lines += ["| device | exit | status | rounds | cached/forwarded/receipts | retries | failed ops (T/N/I/B) | RSS kB m5 -> end (max) | slope MB/h | ring max | pass |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for alias, d in net["devices"].items():
            c = d.get("counters") or {}
            fo = d.get("failedOperations") or {}
            lines.append("| %s | %s | %s | %s | %s/%s/%s | %s | %s/%s/%s/%s | %s -> %s (%s) | %s | %s | %s |" % (
                alias, d["exitCode"], d["finalStatus"], d["cycles"], c.get("snapshotsCached"), c.get("forwarded"),
                c.get("receiptsSynced"), d["retriesScheduled"], fo.get("transient_transport"), fo.get("needs_user_action"),
                fo.get("integrity"), fo.get("local_busy"), d["rssKbAtMinute5"], d["rssKbAtEnd"], d["rssKbMax"],
                d["rssSlopeMbPerHourLastTwoThirds"], d["maxRingSize"], d["pass"]))
        lines.append("")
        for alias, d in net["devices"].items():
            lines.append("- %s Python heap blocks (sys.getallocatedblocks): minute 5 %s -> end %s, slope %s blocks/h" % (
                alias, d.get("heapBlocksAtMinute5"), d.get("heapBlocksAtEnd"), d.get("heapBlocksSlopePerHourLastTwoThirds")))
        lines.append("")
        failed = {a: [k for k, v in d["checks"].items() if not v] for a, d in net["devices"].items()}
        lines.append("- failed checks: %s" % ({a: v for a, v in failed.items() if v} or "none"))
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--backends", default="local,webdav")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--max-backoff", type=float, default=30.0)
    ap.add_argument("--publish-every", type=float, default=60.0)
    ap.add_argument("--ring-every", type=float, default=600.0)
    ap.add_argument("--rss-every", type=float, default=15.0)
    ap.add_argument("--stats", type=float, default=15.0)
    ap.add_argument("--fail-rate", type=float, default=0.03)
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
