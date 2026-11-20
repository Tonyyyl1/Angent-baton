# -*- coding: utf-8 -*-
"""Fault injection helpers for the Phase B sync soak and the sync tests.

* :class:`FlakyProvider` wraps any ``StorageProvider`` and raises
  ``StorageError`` according to a schedule: a global outage, a random failure
  rate, and per-key-pattern failures (one peer's inbox or have manifest). It
  records every call and every uploaded key, so tests and the soak can assert
  that a sync process never uploads ``receipt.json``.
* :class:`ControlFile` lets a driver process change the schedule of a running
  child by rewriting a small JSON file.
* :class:`FaultProxy` is a stdlib TCP proxy in front of a local WebDAV/S3 server
  with modes ``pass``, ``reset`` (accept, then reset), ``latency`` and a random
  reset rate.

Standard library only; production code has no hooks for any of this.
"""
import json
import os
import random
import socket
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional


class FlakyProvider(object):
    """StorageProvider wrapper that injects ``StorageError`` (or any exception) on demand."""

    def __init__(self, inner: Any, rng: Optional[random.Random] = None,
                 error_factory: Optional[Callable[[str, str], BaseException]] = None,
                 record_limit: int = 100000, record: bool = True) -> None:
        self.inner = inner
        self.rng = rng or random.Random()
        self.outage = False
        self.fail_rate = 0.0
        self.fail_patterns: Dict[str, str] = {}      # substring -> ops ("*" or "upload,download,...")
        self.calls: List[tuple] = []
        self.uploads: List[str] = []
        self.injected = 0
        self.call_count = 0
        self.upload_count = 0
        self.receipt_uploads: List[str] = []   # always recorded: a sync process must never add one
        self.record = bool(record)             # full call/upload lists (tests); off for the soak
        self.record_limit = int(record_limit)
        self._error_factory = error_factory
        self.control: Optional["ControlFile"] = None

    def _error(self, op: str, key: str) -> BaseException:
        if self._error_factory is not None:
            return self._error_factory(op, key)
        from relaydesk import storage

        return storage.StorageError("injected transport failure (%s %s)" % (op, key))

    def _gate(self, op: str, key: str) -> None:
        if self.control is not None:
            self.control.apply(self)
        self.call_count += 1
        if self.record and len(self.calls) < self.record_limit:
            self.calls.append((op, key))
        hit = self.outage
        if not hit:
            for pattern, ops in self.fail_patterns.items():
                if pattern in key and (ops == "*" or op in ops.split(",")):
                    hit = True
                    break
        if not hit and self.fail_rate > 0 and self.rng.random() < self.fail_rate:
            hit = True
        if hit:
            self.injected += 1
            raise self._error(op, key)

    def list_objects(self, prefix: str = ""):
        self._gate("list", prefix)
        return self.inner.list_objects(prefix)

    def upload(self, local_path: str, remote_key: str):
        self._gate("upload", remote_key)
        info = self.inner.upload(local_path, remote_key)
        self.upload_count += 1
        if remote_key.endswith("/receipt.json"):
            self.receipt_uploads.append(remote_key)
        if self.record and len(self.uploads) < self.record_limit:
            self.uploads.append(remote_key)
        return info

    def download(self, remote_key: str, local_path: str):
        self._gate("download", remote_key)
        return self.inner.download(remote_key, local_path)

    def stat(self, remote_key: str):
        self._gate("stat", remote_key)
        return self.inner.stat(remote_key)

    def delete(self, remote_key: str):
        self._gate("delete", remote_key)
        return self.inner.delete(remote_key)


class ControlFile(object):
    """A JSON schedule written by the soak driver; re-read when its mtime changes (at most every 0.2 s).

    Fields: ``outage`` (bool), ``failRate`` (0..1), ``failPatterns`` ({substring: ops}).
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._mtime = None
        self._checked = 0.0
        self._data: Dict[str, Any] = {}

    def read(self) -> Dict[str, Any]:
        now = time.monotonic()
        if now - self._checked < 0.2:
            return self._data
        self._checked = now
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            return self._data
        if mtime != self._mtime:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self._data = json.load(fh)
                self._mtime = mtime
            except (OSError, ValueError):
                pass
        return self._data

    def apply(self, flaky: FlakyProvider) -> None:
        data = self.read()
        flaky.outage = bool(data.get("outage"))
        flaky.fail_rate = float(data.get("failRate") or 0.0)
        flaky.fail_patterns = dict(data.get("failPatterns") or {})

    @staticmethod
    def write(path: str, data: Dict[str, Any]) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)


class FaultProxy(object):
    """Threaded TCP proxy with switchable faults, for rclone talking to a local WebDAV/S3 server."""

    def __init__(self, target_port: int, listen_port: int = 0, seed: int = 0) -> None:
        self.target = ("127.0.0.1", int(target_port))
        self.mode = "pass"          # pass | reset | latency
        self.reset_rate = 0.0
        self.latency = 0.0
        self.rng = random.Random(seed)
        self.stats = {"connections": 0, "resets": 0, "passed": 0}
        self._lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", int(listen_port)))
        self._sock.listen(128)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "FaultProxy":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def set(self, mode: str = "pass", reset_rate: Optional[float] = None, latency: Optional[float] = None) -> None:
        with self._lock:
            self.mode = mode
            if reset_rate is not None:
                self.reset_rate = float(reset_rate)
            if latency is not None:
                self.latency = float(latency)

    @staticmethod
    def _rst(conn: socket.socket) -> None:
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with self._lock:
                mode, rate, latency = self.mode, self.reset_rate, self.latency
                self.stats["connections"] += 1
                drop = mode == "reset" or (rate > 0 and self.rng.random() < rate)
                if drop:
                    self.stats["resets"] += 1
                else:
                    self.stats["passed"] += 1
            if drop:
                self._rst(conn)
                continue
            threading.Thread(target=self._pipe_pair, args=(conn, latency if mode == "latency" else 0.0),
                             daemon=True).start()

    def _pipe_pair(self, client: socket.socket, latency: float) -> None:
        try:
            upstream = socket.create_connection(self.target, timeout=10)
        except OSError:
            self._rst(client)
            return
        if latency:
            time.sleep(latency)
        done = threading.Event()

        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                done.set()
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        t = threading.Thread(target=pump, args=(upstream, client), daemon=True)
        t.start()
        pump(client, upstream)
        t.join(timeout=30)
        for s in (client, upstream):
            try:
                s.close()
            except OSError:
                pass
