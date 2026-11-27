"""Offline deterministic unit tests for Columbus controller, approval gate, and drivers."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from pathlib import Path

from columbus.controller import ColumbusController, LaneConfig
from columbus.drivers.base import DriverError, JobSpec, OwnedResource, PollResult, TransientAllocationError
from columbus.drivers.fake import FakeRunPodDriver
from columbus.drivers.runpod import RunPodDriver
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

    def _orphan_intent(self, ctrl, resource_id, name, **overrides):
        intent_path = ctrl.lane_dir / "seat-0.intent.json"
        atomic_write_json(intent_path, {
            "pid": 99999999,
            "lane_id": ctrl.config.lane_id,
            "owned_name": name,
            "mode": "pod",
            "resource_id": resource_id,
            **overrides,
        })
        return intent_path

    def test_reaper_preserves_same_name_pods(self) -> None:
        name = "diffuser-columbus-orphan-g1"
        orphan = OwnedResource("owned-id", name, "RUNNING")
        foreign = OwnedResource("other-id", name, "RUNNING")
        ctrl, drv = self._make_controller(FakeRunPodDriver(foreign_pods=[foreign, orphan]))
        intent_path = self._orphan_intent(ctrl, orphan.resource_id, name)
        self.assertTrue(ctrl.reap()["ok"])
        self.assertEqual(drv.terminate_calls, [orphan.resource_id])
        self.assertEqual(drv.pods, {foreign.resource_id: foreign})
        self.assertFalse(intent_path.exists())
        self.assertFalse(ctrl.quarantine_path.exists())

    def test_reaper_absent_recorded_id_preserves_same_name_pods(self) -> None:
        name = "diffuser-columbus-orphan-g1"
        foreign = OwnedResource("other-id", name, "RUNNING")
        ctrl, drv = self._make_controller(FakeRunPodDriver(foreign_pods=[foreign]))
        intent_path = self._orphan_intent(ctrl, "gone-id", name)
        self.assertTrue(ctrl.reap()["ok"])
        self.assertEqual(drv.terminate_calls, [])
        self.assertEqual(drv.pods, {foreign.resource_id: foreign})
        self.assertFalse(intent_path.exists())

    def test_reaper_missing_or_invalid_id_retains_intent(self) -> None:
        name = "diffuser-columbus-orphan-g1"
        foreign = OwnedResource("other-id", name, "RUNNING")
        ctrl, drv = self._make_controller(FakeRunPodDriver(foreign_pods=[foreign]))
        for resource_id in (None, "", 42):
            with self.subTest(resource_id=resource_id):
                intent_path = self._orphan_intent(ctrl, resource_id, name)
                self.assertFalse(ctrl.reap()["ok"])
                self.assertEqual(drv.terminate_calls, [])
                self.assertEqual(drv.pods, {foreign.resource_id: foreign})
                self.assertTrue(intent_path.exists())
                self.assertTrue(ctrl.quarantine_path.exists())

    def test_reaper_renamed_recorded_id_quarantines(self) -> None:
        name = "diffuser-columbus-orphan-g1"
        renamed = OwnedResource("owned-id", "different-name", "RUNNING")
        foreign = OwnedResource("other-id", name, "RUNNING")
        ctrl, drv = self._make_controller(FakeRunPodDriver(foreign_pods=[renamed, foreign]))
        intent_path = self._orphan_intent(ctrl, renamed.resource_id, name)
        self.assertFalse(ctrl.reap()["ok"])
        self.assertEqual(drv.terminate_calls, [])
        self.assertEqual(drv.pods, {renamed.resource_id: renamed, foreign.resource_id: foreign})
        self.assertTrue(intent_path.exists())
        self.assertTrue(ctrl.quarantine_path.exists())

    def test_reaper_preserves_live_pid_intent(self) -> None:
        name = "diffuser-columbus-orphan-g1"
        pod = OwnedResource("owned-id", name, "RUNNING")
        ctrl, drv = self._make_controller(FakeRunPodDriver(foreign_pods=[pod]))
        intent_path = self._orphan_intent(ctrl, pod.resource_id, name, pid=os.getpid())
        self.assertTrue(ctrl.reap()["ok"])
        self.assertEqual(drv.terminate_calls, [])
        self.assertTrue(intent_path.exists())
        self.assertFalse(ctrl.quarantine_path.exists())

    def test_reaper_lookup_failure_retains_intent(self) -> None:
        ctrl, drv = self._make_controller()
        intent_path = self._orphan_intent(ctrl, "owned-id", "diffuser-columbus-orphan-g1")
        with patch.object(drv, "get_pod", side_effect=DriverError("lookup_failed")):
            self.assertFalse(ctrl.reap()["ok"])
        self.assertEqual(drv.terminate_calls, [])
        self.assertTrue(intent_path.exists())
        self.assertTrue(ctrl.quarantine_path.exists())

    def test_reaper_failed_termination_retains_intent(self) -> None:
        pod = OwnedResource("owned-id", "diffuser-columbus-orphan-g1", "RUNNING")
        ctrl, drv = self._make_controller(FakeRunPodDriver(foreign_pods=[pod], fail_terminate=True))
        intent_path = self._orphan_intent(ctrl, pod.resource_id, pod.name)
        self.assertFalse(ctrl.reap()["ok"])
        self.assertEqual(drv.terminate_calls, [pod.resource_id])
        self.assertTrue(intent_path.exists())
        self.assertTrue(ctrl.quarantine_path.exists())

    def test_serverless_reaper_cancels_only_recorded_job(self) -> None:
        name = "diffuser-columbus-orphan-g1"
        foreign = OwnedResource("other-id", name, "RUNNING")
        ctrl, drv = self._make_controller(FakeRunPodDriver(foreign_pods=[foreign]))
        intent_path = self._orphan_intent(ctrl, "job-id", name, mode="serverless", endpoint_id="endpoint-id")
        with patch.object(drv, "terminate", wraps=drv.terminate) as terminate:
            self.assertTrue(ctrl.reap()["ok"])
            terminate.assert_called_once()
            resource_id, spec = terminate.call_args.args
            self.assertEqual(resource_id, "job-id")
            self.assertEqual(spec.endpoint_id, "endpoint-id")
            self.assertEqual(spec.mode, "serverless")
        self.assertEqual(drv.pods, {foreign.resource_id: foreign})
        self.assertFalse(intent_path.exists())

    def test_serverless_reaper_missing_endpoint_retains_intent(self) -> None:
        ctrl, drv = self._make_controller()
        intent_path = self._orphan_intent(ctrl, "job-id", "diffuser-columbus-orphan-g1", mode="serverless")
        self.assertFalse(ctrl.reap()["ok"])
        self.assertEqual(drv.terminate_calls, [])
        self.assertTrue(intent_path.exists())
        self.assertTrue(ctrl.quarantine_path.exists())

    def test_transient_allocation_name_collision_never_authorizes_termination(self) -> None:
        ctrl, drv = self._make_controller()
        staged = ctrl.handle({"action": "stage", "max_usd": 0.25, "max_walltime_secs": 300})
        ctrl.approvals.approve(staged["ticket_id"])

        def allocation_failure(spec, owned_name, ssh_public_key=None):
            drv.pods["other-id"] = OwnedResource("other-id", owned_name, "RUNNING")
            raise TransientAllocationError("capacity")

        with patch.object(drv, "create", side_effect=allocation_failure) as create:
            res = ctrl.handle({"action": "exec", "ticket_id": staged["ticket_id"], "max_usd": 0.25, "max_walltime_secs": 300})
        self.assertFalse(res["ok"])
        self.assertFalse(res["cleanup_verified"])
        self.assertEqual(create.call_count, 1)
        self.assertEqual(drv.terminate_calls, [])
        self.assertIn("other-id", drv.pods)
        self.assertTrue((ctrl.lane_dir / "seat-0.intent.json").exists())
        self.assertTrue(ctrl.quarantine_path.exists())

    def test_second_transient_allocation_failure_with_no_resources_clears_intent(self) -> None:
        ctrl, drv = self._make_controller(FakeRunPodDriver(transient_failures_before_success=2))
        staged = ctrl.handle({"action": "stage", "max_usd": 0.25, "max_walltime_secs": 300})
        ctrl.approvals.approve(staged["ticket_id"])
        res = ctrl.handle({"action": "exec", "ticket_id": staged["ticket_id"], "max_usd": 0.25, "max_walltime_secs": 300})
        self.assertFalse(res["ok"])
        self.assertTrue(res["cleanup_verified"])
        self.assertEqual(res["attempts"], 2)
        self.assertEqual(drv.terminate_calls, [])
        self.assertFalse((ctrl.lane_dir / "seat-0.intent.json").exists())
        self.assertFalse(ctrl.quarantine_path.exists())

    def test_ambiguous_create_failure_retains_intent(self) -> None:
        ctrl, drv = self._make_controller()
        staged = ctrl.handle({"action": "stage", "max_usd": 0.25, "max_walltime_secs": 300})
        ctrl.approvals.approve(staged["ticket_id"])
        with patch.object(drv, "create", side_effect=DriverError("create_response_unavailable")):
            res = ctrl.handle({"action": "exec", "ticket_id": staged["ticket_id"], "max_usd": 0.25, "max_walltime_secs": 300})
        self.assertFalse(res["ok"])
        self.assertEqual(drv.terminate_calls, [])
        self.assertTrue((ctrl.lane_dir / "seat-0.intent.json").exists())
        self.assertTrue(ctrl.quarantine_path.exists())

    def test_reaper_requires_confirmed_absence_after_termination(self) -> None:
        name = "diffuser-columbus-orphan-g1"
        pod = OwnedResource("owned-id", name, "RUNNING")
        foreign = OwnedResource("other-id", name, "RUNNING")
        ctrl, drv = self._make_controller(FakeRunPodDriver(foreign_pods=[pod, foreign], leave_orphan_after_terminate=True))
        intent_path = self._orphan_intent(ctrl, pod.resource_id, name)
        self.assertFalse(ctrl.reap()["ok"])
        self.assertEqual(drv.terminate_calls, [pod.resource_id])
        self.assertEqual(drv.pods, {pod.resource_id: pod, foreign.resource_id: foreign})
        self.assertTrue(intent_path.exists())
        self.assertTrue(ctrl.quarantine_path.exists())

    def test_execute_cleanup_ignores_name_collision(self) -> None:
        ctrl, drv = self._make_controller()
        staged = ctrl.handle({"action": "stage", "max_usd": 0.25, "max_walltime_secs": 300})
        ctrl.approvals.approve(staged["ticket_id"])
        original_create = drv.create

        def create_with_other_pod(spec, owned_name, ssh_public_key=None):
            drv.pods["other-id"] = OwnedResource("other-id", owned_name, "RUNNING")
            return original_create(spec, owned_name, ssh_public_key)

        with patch.object(drv, "create", side_effect=create_with_other_pod):
            res = ctrl.handle({"action": "exec", "ticket_id": staged["ticket_id"], "max_usd": 0.25, "max_walltime_secs": 300})
        self.assertTrue(res["ok"])
        self.assertTrue(res["cleanup_verified"])
        self.assertEqual(drv.terminate_calls, [res["resource_id"]])
        self.assertIn("other-id", drv.pods)
        self.assertFalse(ctrl.quarantine_path.exists())

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
        self.assertFalse(res["ok"])
        self.assertEqual(drv.terminate_calls, [])
        self.assertIn("foreign-victim-1", drv.pods)
        self.assertTrue(ctrl.quarantine_path.exists())
        self.assertTrue((ctrl.lane_dir / "seat-0.intent.json").exists())

    def test_nv4_symlink_api_key_rejected(self) -> None:
        sym = self.root / "symlink_key"
        os.symlink(self.key_file, sym)
        drv = FakeRunPodDriver(balance_usd=6.88, key_file=sym)
        cfg = LaneConfig(lane_id="columbus-runpod", state_root=self.root / "state")
        ctrl = ColumbusController(cfg, drv, clock=self.clock, sleeper=self.clock.sleep)
        res = ctrl.handle({"action": "status"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "api_key_open_failed")

    def test_http_error_body_read_is_bounded(self) -> None:
        class CountingBody:
            def close(self) -> None:
                pass

            def __init__(self) -> None:
                self.requested = []

            def read(self, size: int = -1) -> bytes:
                self.requested.append(size)
                return b"x" * (size if size >= 0 else 1024 * 1024)

        body = CountingBody()
        err = HTTPError("https://api.runpod.io/graphql", 500, "failure", {}, body)
        driver = RunPodDriver(key_file=self.key_file)

        with patch("columbus.drivers.runpod.urllib.request.urlopen", side_effect=err):
            with self.assertRaisesRegex(DriverError, "runpod_http_500"):
                driver._http_json("https://api.runpod.io/graphql")

        self.assertEqual(body.requested, [301])

    def test_runpod_get_pod_exact_id_and_missing_id(self) -> None:
        driver = RunPodDriver(key_file=self.key_file)
        pods = [
            {"id": "first", "name": "diffuser-columbus-pod"},
            {"id": "second", "name": "diffuser-columbus-pod"},
            {"id": "third", "name": "renamed-pod"},
        ]
        with patch.object(driver, "_graphql", return_value={"myself": {"pods": pods}}):
            self.assertEqual(driver.get_pod("second").resource_id, "second")
            self.assertEqual(driver.get_pod("third").name, "renamed-pod")
            self.assertIsNone(driver.get_pod("absent"))
            self.assertEqual(len(driver.list_owned("diffuser-columbus-")), 2)

    def test_runpod_get_pod_rejects_ambiguous_inventory(self) -> None:
        driver = RunPodDriver(key_file=self.key_file)
        for pods in (None, [{}], [{"id": "same"}, {"id": "same"}]):
            with self.subTest(pods=pods):
                with patch.object(driver, "_graphql", return_value={"myself": {"pods": pods}}):
                    with self.assertRaises(DriverError):
                        driver.get_pod("same")

    def test_runpod_serverless_cancel_uses_endpoint_and_job_id(self) -> None:
        driver = RunPodDriver(key_file=self.key_file)
        with patch.object(driver, "_http_json", return_value={"status": "CANCELLED"}) as http:
            driver.terminate("job-id", JobSpec(mode="serverless", endpoint_id="endpoint-id"))
            http.assert_called_once_with(
                f"{driver.serverless_base_url}/endpoint-id/cancel/job-id", method="POST", body={},
            )
