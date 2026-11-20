"""`recover --rollback` of a pending send must fail closed when the cloud cannot be checked.

Rolling back a pending send drops the local `pendingSends` record, after which
the sender behaves as the owner again. That is only safe when the cloud shows
the handoff's ready marker is absent. If the check itself fails (network
down), the marker may well be published and claimable by the target, so
dropping the record would allow two owners (plan §2).
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from relaydesk import relay_engine, storage
from test_relay_engine import RelayEngineTestBase


class _LostReplyOnReady(storage.LocalDirProvider):
    """ready.json reaches the cloud, but the client sees a transport error."""

    def upload(self, local_path: str, remote_key: str):
        res = super().upload(local_path, remote_key)
        if remote_key.endswith("/ready.json"):
            raise storage.StorageError("模拟网络中断：connection reset by peer（服务器已写入）")
        return res


class _NetworkDown(storage.LocalDirProvider):
    def stat(self, remote_key: str):
        raise storage.StorageError("模拟网络中断：connection reset by peer")

    def list_objects(self, prefix: str = ""):
        raise storage.StorageError("模拟网络中断：connection reset by peer")

    def download(self, remote_key: str, local_path: str):
        raise storage.StorageError("模拟网络中断：connection reset by peer")


class RollbackCloudCheckTest(RelayEngineTestBase):
    def _pending_send_with_published_ready(self, st_b):
        self.create_git_project_on_a()
        with self.env_a():
            with mock.patch.object(storage, "make_provider", side_effect=lambda cfg: _LostReplyOnReady(cfg["root"])):
                with self.assertRaises(storage.StorageError):
                    relay_engine.send_project("demo", to_alias="b")
            state = json.loads((self.home_a / "relay_state.json").read_text(encoding="utf-8"))
        rid = state["pendingSends"]["demo"]["relayId"]
        ready = self.cloud_dir / "v3" / "inbox" / st_b["deviceId"] / rid / "ready.json"
        self.assertTrue(ready.is_file(), "precondition: the handoff is published")
        return rid

    def test_rollback_refused_when_cloud_check_fails(self):
        _, st_b = self.pair_both()
        rid = self._pending_send_with_published_ready(st_b)
        one_try = storage.RetryPolicy(attempts=1)
        with self.env_a():
            with mock.patch.object(storage, "READ_RETRY", one_try), \
                    mock.patch.object(storage, "make_provider", side_effect=lambda cfg: _NetworkDown(cfg["root"])):
                with self.assertRaises(relay_engine.RelayError) as ctx:
                    relay_engine.recover_relay(rid, action="rollback")
            self.assertEqual(ctx.exception.code, "rollback_cloud_check_failed")
            state = json.loads((self.home_a / "relay_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["pendingSends"]["demo"]["relayId"], rid, "pending send must be kept")

    def test_rollback_still_refused_when_published_and_cloud_reachable(self):
        _, st_b = self.pair_both()
        rid = self._pending_send_with_published_ready(st_b)
        with self.env_a():
            with self.assertRaises(relay_engine.RelayError) as ctx:
                relay_engine.recover_relay(rid, action="rollback")
            self.assertEqual(ctx.exception.code, "already_published")

    def test_rollback_allowed_when_cloud_confirms_absent(self):
        _, st_b = self.pair_both()
        rid = self._pending_send_with_published_ready(st_b)
        ready = self.cloud_dir / "v3" / "inbox" / st_b["deviceId"] / rid / "ready.json"
        ready.unlink()  # the lost write never happened after all
        with self.env_a():
            res = relay_engine.recover_relay(rid, action="rollback")
            self.assertTrue(res["ok"])
            state = json.loads((self.home_a / "relay_state.json").read_text(encoding="utf-8"))
        self.assertNotIn("demo", state.get("pendingSends") or {})


if __name__ == "__main__":
    unittest.main()
