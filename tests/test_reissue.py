# -*- coding: utf-8 -*-
"""Phase D: re-issuing an in-flight handoff after a key rotation, and the rotation guard.

``TestPhaseDReissueRepro`` was written first and run against the post-Phase-C
source (digest 657d9657...), where every test failed: there was no way to
re-issue a handoff whose target rotated its key (``send`` only reported
``handoff_recipient_key_changed``), and ``relay device --rotate-keys`` rotated
silently while a handoff was in flight.
"""
import contextlib
import io
import json
import os
import shutil
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from relaydesk import cli
from relaydesk import crypto
from relaydesk import delivery_state as DS
from relaydesk import relay_engine as E
from relaydesk import storage
from relaydesk import sync_runtime as SR
from test_key_rotation_redelivery import _RotationBase, _sha


def _run_cli(argv: List[str]) -> Dict[str, Any]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return {"rc": rc, "out": out.getvalue(), "err": err.getvalue()}


class _ReissueBase(_RotationBase):
    def cli(self, alias: str, *argv: str) -> Dict[str, Any]:
        with self.env(alias):
            return _run_cli(list(argv))

    def cloud_bytes(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for p in sorted(self.cloud_dir.rglob("*")):
            if p.is_file():
                out[str(p.relative_to(self.cloud_dir))] = _sha(p)
        return out


class TestPhaseDReissueRepro(_ReissueBase):
    # ---- a handoff in flight when its target rotated: the user had no way to re-issue it
    def test_inflight_handoff_after_target_rotation_can_be_reissued(self) -> None:
        sent = self.send("alpha", "beta")
        self.rotate("beta")
        self.reconfirm("alpha", "beta")
        with self.env("alpha"):
            res = E.send_project("demo", to_alias="beta", reissue=True)  # pre-fix: TypeError (no such option)
        self.assertNotEqual(res["relayId"], sent["relayId"])
        self.assertEqual(res["supersedesRelayId"], sent["relayId"])
        got = self.receive("beta", res["relayId"])
        self.assertEqual(got["snapshotId"], sent["snapshotId"])

    def test_cli_send_has_reissue_flag(self) -> None:
        self.send("alpha", "beta")
        self.rotate("beta")
        self.reconfirm("alpha", "beta")
        res = self.cli("alpha", "send", "demo", "--to", "beta", "--reissue", "--json")
        self.assertEqual(res["rc"], 0, res["err"])  # pre-fix: usage error, exit 1
        self.assertTrue(json.loads(res["out"])["reissued"])

    # ---- the rotation guard: rotating with a handoff in flight strands it
    def test_rotate_keys_refuses_while_handoff_in_flight(self) -> None:
        self.send("alpha", "beta")
        before = (self.devices["alpha"]["home"] / "identity.json").read_bytes()
        res = self.cli("alpha", "device", "--rotate-keys")
        self.assertNotEqual(res["rc"], 0, res["out"])  # pre-fix: rotated silently, exit 0
        self.assertIn("relay sync", res["err"])
        self.assertEqual((self.devices["alpha"]["home"] / "identity.json").read_bytes(), before)


class TestReissue(_ReissueBase):
    def inbox_files(self, alias: str, rid: str) -> Dict[str, str]:
        d = self.cloud_dir / "v3" / "inbox" / self.did[alias] / rid
        return {p.name: _sha(p) for p in sorted(d.iterdir())} if d.is_dir() else {}

    def test_recipient_rotation_reissue_end_to_end(self) -> None:
        sent = self.send("alpha", "beta")
        orig_objects = self.inbox_files("beta", sent["relayId"])
        self.rotate("beta")
        self.reconfirm("alpha", "beta")
        # beta can no longer claim the original; its sync and doctor say so, naming the sender's command
        res = self.sync("beta")
        self.assertIn("sealed_to_retired_key", [p["code"] for p in res["unresolved"]])
        with self.env("beta"):
            from relaydesk import diagnostics as DX
            doc = DX.run_doctor()
        f = [x for x in doc["findings"] if x["code"] == "incoming_sealed_to_retired_key"][0]
        self.assertEqual(f["command"], "relay send demo --to beta --reissue")
        # plain send still refuses and names the explicit command; nothing is re-issued automatically
        with self.env("alpha"), self.assertRaises(E.KeyChangedError) as cm:
            E.send_project("demo", to_alias="beta")
        self.assertEqual(cm.exception.action, "relay send demo --to beta --reissue")
        self.assertEqual(self.inbox_deliveries("beta").count(sent["relayId"]), 1)
        self.assertEqual(len([d for d in self.inbox_deliveries("beta") if d.startswith("r_")]), 1)
        with self.env("alpha"):
            new = E.send_project("demo", to_alias="beta", reissue=True)
        rid = new["relayId"]
        self.assertNotEqual(rid, sent["relayId"])
        self.assertEqual(new["snapshotId"], sent["snapshotId"])
        self.assertNotEqual(new["packageSha256"], sent["packageSha256"])  # new ciphertext
        self.assertEqual(self.inbox_files("beta", sent["relayId"]), orig_objects)  # original untouched
        st = self.state("alpha")
        self.assertEqual(st["sentRelays"][sent["relayId"]]["supersededBy"], rid)
        self.assertEqual(st["activeHandoffs"]["demo"], rid)
        self.assertEqual(self.project("alpha")["handoff"]["relayId"], rid)
        ready = json.loads((self.cloud_dir / "v3" / "inbox" / self.did["beta"] / rid / "ready.json").read_text("utf-8"))
        self.assertEqual(ready["supersedesRelayIds"], [sent["relayId"]])
        self.assertEqual(DS.norm_fp(ready["recipientKeyFingerprint"]), DS.norm_fp(self.identity("beta")["keyFingerprint"]))
        # beta: the inbox offers only the re-issue; sync no longer reports the replaced original
        with self.env("beta"):
            inbox = E.list_inbox()
        self.assertEqual([i["relayId"] for i in inbox["incoming"]], [rid])
        self.assertEqual(inbox["superseded"], [{"relayId": sent["relayId"], "supersededBy": rid}])
        res = self.sync("beta")
        self.assertEqual(res["status"], SR.SUCCESS, res["unresolved"])
        with self.env("beta"):
            doc = DX.run_doctor()
        self.assertNotIn("incoming_sealed_to_retired_key", [x["code"] for x in doc["findings"]])
        with self.env("gamma"), self.assertRaises(E.UnauthorizedReceiverError):  # non-targets can never claim
            E.receive_relay(rid, target_path=str(self.root / "gamma_claim"))
        got = self.receive("beta", rid)
        self.assertEqual(got["snapshotId"], sent["snapshotId"])
        receipt = json.loads((self.cloud_dir / "v3" / "inbox" / self.did["beta"] / rid / "receipt.json").read_text("utf-8"))
        self.assertEqual((receipt["relayId"], receipt["projectId"], receipt["snapshotId"], receipt["packageSha256"],
                          receipt["receiverDeviceId"]),
                         (rid, "demo", sent["snapshotId"], new["packageSha256"], self.did["beta"]))
        with self.env("beta"), self.assertRaises(E.RelayError) as cm:
            E.receive_relay(sent["relayId"], target_path=str(self.root / "again"))
        self.assertEqual(cm.exception.code, "handoff_superseded")
        res = self.sync("alpha")
        self.assertEqual(res["status"], SR.SUCCESS, res["unresolved"])
        st = self.state("alpha")
        self.assertTrue(st["sentRelays"][rid]["acked"])
        self.assertFalse(st["sentRelays"][sent["relayId"]]["acked"])  # the original stays as it was
        self.assertEqual(self.project("alpha")["device"], "beta")
        self.assertEqual(self.inbox_files("beta", sent["relayId"]), orig_objects)
        # the superseded original never blocks later work: hand back, then publish
        back = self.send("beta", "alpha")
        self.receive("alpha", back["relayId"])
        self.sync("beta")
        pub = self.publish()
        self.assertTrue(pub["snapshotId"])

    def test_sender_rotation_the_target_can_never_claim_both(self) -> None:
        sent = self.send("alpha", "beta")
        with self.env("alpha"):
            rot = _run_cli(["device", "--rotate-keys", "--force"])
        self.assertEqual(rot["rc"], 0, rot["err"])
        self.assertIn("--reissue", rot["err"])  # the consequence is printed
        with self.env("alpha"):
            E.pair_device()  # republish the descriptor with the new keys
        with self.env("alpha"), self.assertRaises(E.KeyChangedError) as cm:
            E.send_project("demo", to_alias="beta")
        self.assertEqual(cm.exception.code, "handoff_sender_key_rotated")
        with self.env("alpha"):
            new = E.send_project("demo", to_alias="beta", reissue=True)
        # beta has not re-confirmed alpha yet: it can still verify (and claims) the original
        self.receive("beta", sent["relayId"])
        self.reconfirm("beta", "alpha")
        with self.env("beta"), self.assertRaises(E.RelayError) as cm:
            E.receive_relay(new["relayId"], target_path=str(self.root / "second"))
        self.assertEqual(cm.exception.code, "superseded_original_claimed")
        self.assertFalse((self.root / "second").exists())
        # alpha: the original's verified receipt is the claim; the re-issue is withdrawn
        res = self.sync("alpha")
        st = self.state("alpha")
        self.assertTrue(st["sentRelays"][sent["relayId"]]["acked"])
        self.assertEqual(st["sentRelays"][new["relayId"]]["withdrawn"]["by"], sent["relayId"])
        self.assertNotIn("demo", st.get("activeHandoffs") or {})
        self.assertEqual(self.project("alpha")["device"], "beta")
        with self.env("alpha"):
            from relaydesk import diagnostics as DX
            codes = [f["code"] for f in DX.run_doctor()["findings"]]
        self.assertIn("handoff_withdrawn", codes)
        self.assertEqual(res["status"], SR.SUCCESS, res["unresolved"])

    def test_reissue_refusals_leave_everything_unchanged(self) -> None:
        with self.env("alpha"), self.assertRaises(E.RelayError) as cm:
            E.send_project("demo", to_alias="beta", reissue=True)
        self.assertEqual(cm.exception.code, "nothing_to_reissue")
        sent = self.send("alpha", "beta")
        before = self.cloud_bytes()
        with self.env("alpha"), self.assertRaises(E.RelayError) as cm:
            E.send_project("demo", to_alias="beta", reissue=True)
        self.assertEqual(cm.exception.code, "reissue_not_needed")  # keys unchanged: the original still works
        self.rotate("beta")
        self.sync("beta")   # beta's have is now signed with its new key
        self.sync("alpha")  # -> signature failure -> descriptor probe -> beta is key_changed on alpha
        with self.env("alpha"), self.assertRaises(E.KeyChangedError) as cm:  # not re-confirmed yet
            E.send_project("demo", to_alias="beta", reissue=True)
        self.assertEqual(cm.exception.code, "peer_key_changed")
        self.reconfirm("alpha", "beta")
        payload = self.cache_payload("alpha", sent["snapshotId"])
        saved = payload.read_bytes()
        payload.write_bytes(saved[:-10] + b"0123456789")
        with self.env("alpha"), self.assertRaises(E.RelayError) as cm:
            E.send_project("demo", to_alias="beta", reissue=True)
        self.assertEqual(cm.exception.code, "reissue_payload_missing")
        payload.write_bytes(saved)
        fake_receipt = self.cloud_dir / "v3" / "inbox" / self.did["beta"] / sent["relayId"] / "receipt.json"
        fake_receipt.write_text("{}", encoding="utf-8")
        with self.env("alpha"), self.assertRaises(E.RelayError) as cm:
            E.send_project("demo", to_alias="beta", reissue=True)
        self.assertEqual(cm.exception.code, "reissue_receipt_exists")
        fake_receipt.unlink()
        after = self.cloud_bytes()
        for key, digest in before.items():
            if not key.startswith("v3/have/") and not key.startswith("v3/devices/"):
                self.assertEqual(after.get(key), digest, key)
        st = json.loads((self.devices["alpha"]["home"] / "relay_state.json").read_text("utf-8"))
        self.assertNotIn("supersededBy", st["sentRelays"][sent["relayId"]])
        self.assertEqual(st["activeHandoffs"]["demo"], sent["relayId"])
        self.assertFalse(st.get("pendingSends"))

    def test_interrupted_reissue_continues_under_the_same_relay_id(self) -> None:
        sent = self.send("alpha", "beta")
        self.rotate("beta")
        self.reconfirm("alpha", "beta")
        with self.env("alpha", {"RELAY_DESK_FAULT_POINT": "reissue_after_package_upload"}):
            with self.assertRaises(RuntimeError):
                E.send_project("demo", to_alias="beta", reissue=True)
        pend = self.state("alpha")["pendingSends"]["demo"]
        self.assertEqual(pend["reissueOf"], sent["relayId"])
        with self.env("alpha"):
            from relaydesk import diagnostics as DX
            doc = DX.run_doctor()
        self.assertIn(("reissue_interrupted", "relay send demo --to beta --reissue"),
                      [(f["code"], f["command"]) for f in doc["findings"]])
        with self.env("alpha"), self.assertRaises(E.KeyChangedError):
            E.send_project("demo", to_alias="beta")  # a plain send never finishes a re-issue by itself
        with self.env("alpha"):
            new = E.send_project("demo", to_alias="beta", reissue=True)
        self.assertEqual(new["relayId"], pend["relayId"])
        self.assertEqual(len([d for d in self.inbox_deliveries("beta") if d.startswith("r_")]), 2)
        self.receive("beta", new["relayId"])
        self.sync("alpha")
        self.assertTrue(self.state("alpha")["sentRelays"][new["relayId"]]["acked"])

    def test_cli_text_and_api_reissue(self) -> None:
        sent = self.send("alpha", "beta")
        self.rotate("beta")
        self.reconfirm("alpha", "beta")
        res = self.cli("alpha", "send", "demo", "--to", "beta")
        self.assertEqual(res["rc"], 1)
        self.assertIn("relay send demo --to beta --reissue", res["err"])
        res = self.cli("alpha", "send", "demo", "--to", "beta", "--reissue")
        self.assertEqual(res["rc"], 0, res["err"])
        self.assertIn("取代 %s" % sent["relayId"], res["out"])


class TestRotationGuard(_ReissueBase):
    def identity_bytes(self, alias: str) -> bytes:
        return (self.devices[alias]["home"] / "identity.json").read_bytes()

    def test_guard_blocks_on_pending_receipt_and_unfinished_transaction(self) -> None:
        sent = self.send("alpha", "beta")
        with self.env("beta", {"RELAY_DESK_FAULT_POINT": "rx_before_receipt"}):
            with self.assertRaises(RuntimeError):
                E.receive_relay(sent["relayId"], target_path=str(self.root / "b"))
        before = self.identity_bytes("beta")
        res = self.cli("beta", "device", "--rotate-keys")
        self.assertEqual(res["rc"], 4)
        self.assertIn("relay receive %s" % sent["relayId"], res["err"])
        self.assertEqual(self.identity_bytes("beta"), before)
        with self.env("beta"):
            E.receive_relay(sent["relayId"])
        self.sync("alpha")
        res = self.cli("alpha", "device", "--rotate-keys")  # alpha's handoff is acked now: nothing blocks
        self.assertEqual(res["rc"], 0, res["err"])
        with self.env("alpha"):
            E.pair_device()  # republish alpha's descriptor with the new keys
        self.reconfirm("beta", "alpha")
        back = self.send("beta", "alpha")
        dest = self.root / "a_new"
        with self.env("alpha", {"RELAY_DESK_FAULT_POINT": "tx_after_journal_before_apply"}):
            with self.assertRaises(RuntimeError):
                E.receive_relay(back["relayId"], target_path=str(dest))
        res = self.cli("alpha", "device", "--rotate-keys")
        self.assertEqual(res["rc"], 4)
        self.assertIn("relay receive %s" % back["relayId"], res["err"])

    def test_no_blockers_rotates_and_json_output_unchanged(self) -> None:
        res = self.cli("gamma", "device", "--rotate-keys", "--json")
        self.assertEqual(res["rc"], 0, res["err"])
        self.assertTrue(json.loads(res["out"]))


if __name__ == "__main__":
    unittest.main()
