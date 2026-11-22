"""Deterministic fake RunPod driver for offline Columbus tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from columbus.drivers.base import (
    CostEstimate,
    DriverError,
    JobSpec,
    LaunchResult,
    OwnedResource,
    PollResult,
    ProviderDriver,
    TransientAllocationError,
)
from columbus.secure_io import load_api_key


class FakeRunPodDriver(ProviderDriver):
    """Configurable offline fake driver with foreign-resource and failure injection."""

    name = "runpod"

    def __init__(
        self,
        balance_usd: float = 6.88,
        hourly_rate_usd: float = 0.40,
        key_file: Optional[Path] = None,
        foreign_pods: Optional[List[OwnedResource]] = None,
        transient_failures_before_success: int = 0,
        fail_terminate: bool = False,
        leave_orphan_after_terminate: bool = False,
        poll_sequence: Optional[List[PollResult]] = None,
        cost_per_run_usd: float = 0.05,
    ) -> None:
        self.balance_usd = balance_usd
        self.hourly_rate_usd = hourly_rate_usd
        self.key_file = key_file
        self.pods: Dict[str, OwnedResource] = {}
        for fp in foreign_pods or []:
            self.pods[fp.resource_id] = fp
        self.transient_failures_remaining = transient_failures_before_success
        self.fail_terminate = fail_terminate
        self.leave_orphan_after_terminate = leave_orphan_after_terminate
        self.poll_sequence = list(poll_sequence) if poll_sequence is not None else [
            PollResult(state="running", progress_token="run-1", uptime_secs=5),
            PollResult(state="completed", progress_token="done-1", output={"ok": True, "gpu": "RTX 4090"}, uptime_secs=12),
        ]
        self.cost_per_run_usd = cost_per_run_usd
        self.create_calls: List[Dict[str, Any]] = []
        self.terminate_calls: List[str] = []
        self.keys_seen: List[str] = []
        self._seq = 0

    def _check_key(self) -> None:
        if self.key_file is not None:
            self.keys_seen.append(load_api_key(self.key_file))

    def get_balance(self) -> float:
        self._check_key()
        return round(self.balance_usd, 6)

    def estimate(self, spec: JobSpec) -> CostEstimate:
        self._check_key()
        hourly = round(self.hourly_rate_usd * max(1, spec.gpu_count), 4)
        est = round(hourly * (float(spec.max_walltime_secs) / 3600.0), 4)
        return CostEstimate(
            provider=self.name,
            mode=spec.mode,
            hourly_rate_usd=hourly,
            estimated_max_usd=est,
            resolved_target=spec.endpoint_id if spec.mode == "serverless" else spec.gpu_type,
            notes="fake_estimate",
        )

    def list_owned(self, prefix: str) -> List[OwnedResource]:
        self._check_key()
        if not prefix.startswith("diffuser-columbus-"):
            raise DriverError("invalid_owned_prefix")
        return [p for p in self.pods.values() if p.name.startswith(prefix)]

    def create(self, spec: JobSpec, owned_name: str) -> LaunchResult:
        self._check_key()
        self.create_calls.append({"owned_name": owned_name, "mode": spec.mode, "gpu_type": spec.gpu_type})
        if self.transient_failures_remaining > 0:
            self.transient_failures_remaining -= 1
            raise TransientAllocationError("no longer any instances available")
        self._seq += 1
        rid = f"fake-{spec.mode}-{self._seq}"
        if spec.mode == "pod":
            self.pods[rid] = OwnedResource(resource_id=rid, name=owned_name, status="RUNNING", uptime_secs=1)
        self.balance_usd = max(0.0, round(self.balance_usd - self.cost_per_run_usd, 6))
        return LaunchResult(resource_id=rid, name=owned_name, status="RUNNING")

    def poll(self, resource_id: str, spec: JobSpec) -> PollResult:
        self._check_key()
        if self.poll_sequence:
            if len(self.poll_sequence) > 1:
                return self.poll_sequence.pop(0)
            return self.poll_sequence[0]
        return PollResult(state="completed", progress_token="done", output={"ok": True}, uptime_secs=10)

    def terminate(self, resource_id: str, spec: JobSpec) -> None:
        self._check_key()
        self.terminate_calls.append(resource_id)
        if self.fail_terminate:
            raise DriverError("terminate_failed")
        if spec.mode == "pod" and not self.leave_orphan_after_terminate:
            self.pods.pop(resource_id, None)
