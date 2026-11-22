"""Offline deterministic unit tests for Columbus controller, approval gate, and drivers."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from columbus.controller import ColumbusController, LaneConfig
from columbus.drivers.base import OwnedResource, PollResult
from columbus.drivers.fake import FakeRunPodDriver
from columbus.secure_io import atomic_write_json


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += secs


class ColumbusControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.key_file = self.root / "runpod_api_key"
        self.key_file.write_text("fake_key_v1_000000\n", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_controller(self, driver=None, **cfg_overrides) -> tuple[ColumbusController, FakeRunPodDriver]:
        drv = driver or FakeRunPodDriver(balance_usd=6.88, key_file=self.key_file)
        cfg = LaneConfig(
            lane_id="columbus-runpod",
            state_root=self.state_dir,
            budget_cap_usd=6.88,
            approval_poll_secs=1.0,
            job_poll_secs=1.0,
            alloc_retry_delay_secs=5.0,
            **cfg_overrides,
        )
        ctrl = ColumbusController(cfg, drv, clock=self.clock, sleeper=self.clock.sleep)
        return ctrl, drv

    def test_status_and_estimate_do_not_create(self) -> None:
        foreign = OwnedResource(resource_id="foreign-1", name="my-personal-pod", status="RUNNING")
        drv = FakeRunPodDriver(balance_usd=6.88, foreign_pods=[foreign], key_file=self.key_file)
        ctrl, _ = self._make_controller(drv)

        st = ctrl.handle({"action": "status"})
        self.assertTrue(st["ok"])
        self.assertEqual(st["balance_usd"], 6.88)
        self.assertEqual(st["owned_count"], 0)
        self.assertEqual(st["budget"]["budget_cap_usd"], 6.88)

        est = ctrl.handle({"action": "estimate", "mode": "pod", "max_walltime_secs": 300, "max_usd": 0.50})
        self.assertTrue(est["ok"])
        self.assertTrue(est["admissible"])
        self.assertEqual(len(drv.create_calls), 0)

    def test_stage_approve_execute_pod_lifecycle(self) -> None:
        ctrl, drv = self._make_controller()
        req = {
            "action": "stage",
            "mode": "pod",
            "gpu_type": "NVIDIA GeForce RTX 4090",
            "max_walltime_secs": 300,
            "max_usd": 0.50,
            "requesting_agent": "codex-sol",
        }
        staged = ctrl.handle(req)
        self.assertTrue(staged["ok"])
        self.assertEqual(staged["status"], "pending_approval")
        self.assertEqual(len(drv.create_calls), 0)

        tid = staged["ticket_id"]
        ctrl.approvals.approve(tid, note="approved for smoke test")

        exec_req = dict(req, action="exec", ticket_id=tid)
        res = ctrl.handle(exec_req)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["state"], "completed")
        self.assertTrue(res["cleanup_verified"])
        self.assertEqual(len(drv.create_calls), 1)
        self.assertEqual(len(drv.terminate_calls), 1)
        self.assertEqual(len(drv.list_owned("diffuser-columbus-")), 0)
        self.assertEqual(len(res["artifacts"]), 1)
        art_path = Path(res["artifacts"][0]["path"])
        self.assertEqual(stat.S_IMODE(os.stat(art_path).st_mode), 0o600)
        self.assertGreater(res["budget"]["spent_usd"], 0.0)

        # Replay of consumed approval must be rejected
        replay = ctrl.handle(exec_req)
        self.assertFalse(replay["ok"])
        self.assertEqual(replay["error"], "approval_already_consumed")

    def test_approval_spec_tamper_rejected(self) -> None:
        ctrl, drv = self._make_controller()
        staged = ctrl.handle({"action": "stage", "mode": "pod", "gpu_type": "RTX 4090", "max_usd": 0.25, "max_walltime_secs": 300})
        tid = staged["ticket_id"]
        ctrl.approvals.approve(tid)

        tampered = ctrl.handle({
            "action": "exec",
            "ticket_id": tid,
            "mode": "pod",
            "gpu_type": "NVIDIA H100 80GB",
            "max_usd": 0.25,
            "max_walltime_secs": 300,
        })
        self.assertFalse(tampered["ok"])
        self.assertEqual(tampered["error"], "approval_spec_hash_mismatch")
        self.assertEqual(len(drv.create_calls), 0)

    def test_approval_rejected_and_expired(self) -> None:
        ctrl, drv = self._make_controller()
        s1 = ctrl.handle({"action": "stage", "mode": "pod", "max_usd": 0.25, "max_walltime_secs": 300})
        ctrl.approvals.reject(s1["ticket_id"], reason="not needed")
        r1 = ctrl.handle({"action": "exec", "ticket_id": s1["ticket_id"], "mode": "pod", "max_usd": 0.25, "max_walltime_secs": 300})
        self.assertFalse(r1["ok"])
        self.assertIn("approval_not_granted:rejected", r1["error"])

        s2 = ctrl.handle({"action": "stage", "mode": "pod", "max_usd": 0.25, "max_walltime_secs": 300, "approval_ttl_secs": 60})
        self.clock.sleep(120)
        r2 = ctrl.handle({"action": "exec", "ticket_id": s2["ticket_id"], "mode": "pod", "max_usd": 0.25, "max_walltime_secs": 300})
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["error"], "approval_expired")
        self.assertEqual(len(drv.create_calls), 0)

    def test_budget_cap_and_balance_floor(self) -> None:
        ctrl, drv = self._make_controller()
        ctrl.ledger.record_spend("prior-run", 6.80)
        res = ctrl.handle({"action": "stage", "mode": "pod", "max_usd": 0.50, "max_walltime_secs": 1800})
        self.assertFalse(res["ok"])
        self.assertIn("budget_cap_exceeded", res["error"])

        drv_low = FakeRunPodDriver(balance_usd=0.10, key_file=self.key_file)
        ctrl_low, _ = self._make_controller(drv_low)
        res_low = ctrl_low.handle({"action": "stage", "mode": "pod", "max_usd": 0.50, "max_walltime_secs": 300})
        self.assertFalse(res_low["ok"])
        self.assertIn("balance_below_floor", res_low["error"])

    def test_transient_zero_dollar_allocation_retries_once(self) -> None:
        drv = FakeRunPodDriver(balance_usd=6.88, transient_failures_before_success=1, key_file=self.key_file)
        ctrl, _ = self._make_controller(drv)
        staged = ctrl.handle({"action": "stage", "mode": "pod", "max_usd": 0.25, "max_walltime_secs": 300})
        ctrl.approvals.approve(staged["ticket_id"])
        res = ctrl.handle({"action": "exec", "ticket_id": staged["ticket_id"], "mode": "pod", "max_usd": 0.25, "max_walltime_secs": 300})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["attempts"], 2)
        self.assertTrue(res["owned_name"].endswith("-g2"))

    def test_foreign_pod_never_terminated_and_quarantine_on_orphan(self) -> None:
        foreign = OwnedResource(resource_id="foreign-pod-99", name="keep-me", status="RUNNING")
        drv = FakeRunPodDriver(balance_usd=6.88, foreign_pods=[foreign], leave_orphan_after_terminate=True, key_file=self.key_file)
        ctrl, _ = self._make_controller(drv)
        staged = ctrl.handle({"action": "stage", "mode": "pod", "max_usd": 0.25, "max_walltime_secs": 300})
        ctrl.approvals.approve(staged["ticket_id"])
        res = ctrl.handle({"action": "exec", "ticket_id": staged["ticket_id"], "mode": "pod", "max_usd": 0.25, "max_walltime_secs": 300})
        self.assertFalse(res["ok"])
        self.assertFalse(res["cleanup_verified"])
        self.assertNotIn("foreign-pod-99", drv.terminate_calls)
        self.assertTrue(ctrl.quarantine_path.exists())

        blocked = ctrl.handle({"action": "stage", "mode": "pod", "max_usd": 0.25, "max_walltime_secs": 300})
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"], "lane_quarantined")

        ctrl.clear_quarantine()
        self.assertFalse(ctrl.quarantine_path.exists())

    def test_orphan_reaper_cleans_dead_pid_only(self) -> None:
        orphan = OwnedResource(resource_id="orphan-1", name="diffuser-columbus-columbus-runpod-deadbeef-g1", status="RUNNING")
        foreign = OwnedResource(resource_id="foreign-1", name="unrelated-pod", status="RUNNING")
        drv = FakeRunPodDriver(balance_usd=6.88, foreign_pods=[orphan, foreign], key_file=self.key_file)
        ctrl, _ = self._make_controller(drv)
        intent_path = ctrl.lane_dir / "seat-0.intent.json"
        atomic_write_json(intent_path, {
            "pid": 99999999,
            "lane_id": "columbus-runpod",
            "owned_name": orphan.name,
            "mode": "pod",
            "resource_id": orphan.resource_id,
        })
        dry = ctrl.reap(dry_run=True)
        self.assertEqual(len(dry["candidates"]), 1)
        self.assertEqual(len(drv.terminate_calls), 0)

        real = ctrl.reap(dry_run=False)
        self.assertTrue(real["ok"])
        self.assertEqual(real["reaped"], [orphan.name])
        self.assertEqual(drv.terminate_calls, ["orphan-1"])
        self.assertIn("foreign-1", drv.pods)
        self.assertFalse(intent_path.exists())

    def test_dynamic_key_rotation_and_permission_guard(self) -> None:
        ctrl, drv = self._make_controller()
        self.assertTrue(ctrl.handle({"action": "status"})["ok"])
        self.assertEqual(drv.keys_seen[-1], "fake_key_v1_000000")

        # Rotate key on disk without restarting controller
        self.key_file.write_text("fake_key_v2_rotated\n", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        self.assertTrue(ctrl.handle({"action": "status"})["ok"])
        self.assertEqual(drv.keys_seen[-1], "fake_key_v2_rotated")

        # Insecure permissions must fail closed
        os.chmod(self.key_file, 0o644)
        bad = ctrl.handle({"action": "status"})
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "api_key_insecure_permissions")

    def test_serverless_mode_and_park_unpark(self) -> None:
        drv = FakeRunPodDriver(
            balance_usd=6.88,
            key_file=self.key_file,
            poll_sequence=[PollResult(state="completed", progress_token="done", output={"result": 42})],
        )
        ctrl, _ = self._make_controller(drv, mode="serverless")
        ctrl.park("maintenance")
        parked = ctrl.handle({"action": "stage", "mode": "serverless", "endpoint_id": "ep-1", "max_usd": 0.25, "max_walltime_secs": 120})
        self.assertFalse(parked["ok"])
        self.assertEqual(parked["error"], "lane_parked")

        ctrl.unpark()
        staged = ctrl.handle({"action": "stage", "mode": "serverless", "endpoint_id": "ep-1", "payload": {"prompt": "hi"}, "max_usd": 0.25, "max_walltime_secs": 120})
        self.assertTrue(staged["ok"])
        ctrl.approvals.approve(staged["ticket_id"])
        res = ctrl.handle({"action": "exec", "ticket_id": staged["ticket_id"], "mode": "serverless", "endpoint_id": "ep-1", "payload": {"prompt": "hi"}, "max_usd": 0.25, "max_walltime_secs": 120})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["mode"], "serverless")
        self.assertEqual(res["state"], "completed")


if __name__ == "__main__":
    unittest.main()


class ColumbusEphemeralSshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.key_file = self.root / "runpod_api_key"
        self.key_file.write_text("fake_key_ssh_000000\n", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ephemeral_ssh_pod_command_and_cleanup(self) -> None:
        drv = FakeRunPodDriver(balance_usd=6.88, key_file=self.key_file)
        cfg = LaneConfig(
            lane_id="columbus-runpod",
            state_root=self.root / "state",
            budget_cap_usd=6.88,
            job_poll_secs=1.0,
        )
        ctrl = ColumbusController(cfg, drv, clock=self.clock, sleeper=self.clock.sleep)
        req = {
            "action": "stage",
            "mode": "pod",
            "gpu_type": "NVIDIA RTX A4000",
            "command": "nvidia-smi && echo COLUMBUS_SMOKE_OK",
            "max_walltime_secs": 300,
            "startup_timeout_secs": 180,
            "max_usd": 0.10,
        }
        staged = ctrl.handle(req)
        self.assertTrue(staged["ok"], staged)
        ctrl.approvals.approve(staged["ticket_id"])
        res = ctrl.handle(dict(req, action="exec", ticket_id=staged["ticket_id"]))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("COLUMBUS_SMOKE_OK", res["stdout_preview"])
        self.assertTrue(drv.create_calls[0]["has_ssh_key"])
        self.assertEqual(len(drv.exec_calls), 1)
        self.assertTrue(drv.exec_calls[0]["key_exists"])
        self.assertEqual(len(drv.terminate_calls), 1)
        # Verify temp ssh-* dir was cleaned up
        ssh_dirs = list((self.root / "state" / "columbus-runpod").glob("ssh-*"))
        self.assertEqual(ssh_dirs, [])


class ColumbusAuditHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.key_file = self.root / "runpod_api_key"
        self.key_file.write_text("fake_key_hardened_000\n", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_nv1_atomic_budget_reservation_blocks_overcommit(self) -> None:
        drv = FakeRunPodDriver(balance_usd=6.88, key_file=self.key_file)
        cfg = LaneConfig(lane_id="lane-a", state_root=self.root / "state", budget_cap_usd=1.00)
        ctrl = ColumbusController(cfg, drv, clock=self.clock, sleeper=self.clock.sleep)
        ctrl.ledger.reserve("lane-b-inflight", 0.90)
        res = ctrl.handle({"action": "stage", "mode": "pod", "max_usd": 0.50, "max_walltime_secs": 1800})
        self.assertFalse(res["ok"])
        self.assertIn("budget_cap_exceeded", res["error"])
        ctrl.ledger.release("lane-b-inflight")
        ok = ctrl.handle({"action": "stage", "mode": "pod", "max_usd": 0.50, "max_walltime_secs": 1800})
        self.assertTrue(ok["ok"], ok)

    def test_nv3_reaper_never_terminates_unverified_intent_id(self) -> None:
        foreign = OwnedResource(resource_id="foreign-victim-1", name="user-personal-pod", status="RUNNING")
        drv = FakeRunPodDriver(balance_usd=6.88, foreign_pods=[foreign], key_file=self.key_file)
        cfg = LaneConfig(lane_id="columbus-runpod", state_root=self.root / "state", budget_cap_usd=6.88)
        ctrl = ColumbusController(cfg, drv, clock=self.clock, sleeper=self.clock.sleep)
        atomic_write_json(ctrl.lane_dir / "seat-0.intent.json", {
            "pid": 99999999,
            "lane_id": "columbus-runpod",
            "owned_name": "diffuser-columbus-columbus-runpod-fake-g1",
            "mode": "pod",
            "resource_id": "foreign-victim-1",
        })
        res = ctrl.reap(dry_run=False)
        self.assertTrue(res["ok"])
        self.assertEqual(drv.terminate_calls, [])
        self.assertIn("foreign-victim-1", drv.pods)

    def test_nv4_symlink_api_key_rejected(self) -> None:
        sym = self.root / "symlink_key"
        os.symlink(self.key_file, sym)
        drv = FakeRunPodDriver(balance_usd=6.88, key_file=sym)
        cfg = LaneConfig(lane_id="columbus-runpod", state_root=self.root / "state")
        ctrl = ColumbusController(cfg, drv, clock=self.clock, sleeper=self.clock.sleep)
        res = ctrl.handle({"action": "status"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "api_key_open_failed")
