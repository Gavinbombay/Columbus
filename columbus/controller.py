"""Columbus controller: budget ledger, approval-gated execution, ephemeral SSH, teardown, quarantine, and reaper."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from columbus.approval import ApprovalError, ApprovalStore, DEFAULT_APPROVAL_TTL_SECS, canonical_spec_hash
from columbus.artifacts import ArtifactError, write_artifacts
from columbus.drivers.base import (
    DriverError,
    JobSpec,
    ProviderDriver,
    TransientAllocationError,
)
from columbus.secure_io import (
    SecureIOError,
    atomic_write_json,
    file_lock,
    make_private_dir,
    pid_is_alive,
    read_json,
)

OWNED_PREFIX = "diffuser-columbus-"
DEFAULT_BUDGET_CAP_USD = 6.88
DEFAULT_MIN_BALANCE_FLOOR_USD = 0.25


class Refusal(Exception):
    """Fail-closed admission or safety refusal."""


@dataclass(frozen=True)
class LaneConfig:
    lane_id: str = "columbus-runpod"
    mode: str = "pod"  # "pod" or "serverless"
    state_root: Path = Path("/var/lib/diffuser-columbus")
    budget_cap_usd: float = DEFAULT_BUDGET_CAP_USD
    min_balance_floor_usd: float = DEFAULT_MIN_BALANCE_FLOOR_USD
    max_usd_per_job: float = 2.00
    max_walltime_secs: int = 3600
    default_startup_timeout_secs: int = 420
    default_idle_timeout_secs: int = 600
    default_approval_ttl_secs: int = DEFAULT_APPROVAL_TTL_SECS
    approval_poll_secs: float = 2.0
    job_poll_secs: float = 5.0
    alloc_retry_delay_secs: float = 30.0
    max_seats: int = 1


def _generate_ephemeral_ssh_keypair(parent_dir: Path) -> Tuple[Path, Path, str]:
    """Generate a single-use ed25519 keypair in a 0700 temp directory."""
    make_private_dir(parent_dir)
    key_dir = Path(tempfile.mkdtemp(prefix="ssh-", dir=str(parent_dir)))
    os.chmod(key_dir, 0o700)
    priv_path = key_dir / "id_ed25519"
    pub_path = key_dir / "id_ed25519.pub"
    res = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "columbus-ephemeral", "-f", str(priv_path), "-q"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if res.returncode != 0 or not priv_path.exists() or not pub_path.exists():
        shutil.rmtree(key_dir, ignore_errors=True)
        raise Refusal("ephemeral_ssh_keygen_failed")
    os.chmod(priv_path, 0o600)
    os.chmod(pub_path, 0o600)
    return key_dir, priv_path, pub_path.read_text(encoding="utf-8").strip()


class BudgetLedger:
    """Durable 0600 USD spend ledger enforcing the cumulative production test cap."""

    def __init__(self, state_root: Path, budget_cap_usd: float = DEFAULT_BUDGET_CAP_USD) -> None:
        self.state_root = state_root
        self.path = state_root / "budget_ledger.json"
        self.lock_path = state_root / ".budget.lock"
        self.budget_cap_usd = round(float(budget_cap_usd), 4)

    def _load_unlocked(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {
                "budget_cap_usd": self.budget_cap_usd,
                "spent_usd": 0.0,
                "reservations": {},
                "runs": [],
            }
        data = read_json(self.path)
        if not isinstance(data, dict):
            raise Refusal("budget_ledger_corrupt")
        data.setdefault("budget_cap_usd", self.budget_cap_usd)
        data.setdefault("spent_usd", 0.0)
        data.setdefault("reservations", {})
        data.setdefault("runs", [])
        return data

    def _reserved_total(self, data: Dict[str, Any]) -> float:
        res = data.get("reservations") or {}
        if not isinstance(res, dict):
            return 0.0
        return round(sum(max(0.0, float(v)) for v in res.values()), 6)

    def snapshot(self) -> Dict[str, Any]:
        make_private_dir(self.state_root)
        with file_lock(self.lock_path, blocking=True):
            data = self._load_unlocked()
            cap = min(self.budget_cap_usd, float(data.get("budget_cap_usd", self.budget_cap_usd)))
            spent = round(float(data.get("spent_usd", 0.0)), 6)
            reserved = self._reserved_total(data)
            return {
                "budget_cap_usd": round(cap, 4),
                "spent_usd": spent,
                "reserved_usd": reserved,
                "remaining_usd": round(max(0.0, cap - spent - reserved), 6),
                "run_count": len(data.get("runs", [])),
            }

    def check_admission(self, estimated_max_usd: float) -> Dict[str, Any]:
        snap = self.snapshot()
        if estimated_max_usd < 0:
            raise Refusal("invalid_cost_estimate")
        committed = snap["spent_usd"] + snap["reserved_usd"]
        if committed + estimated_max_usd > snap["budget_cap_usd"] + 1e-9:
            c_val = snap["budget_cap_usd"]
            raise Refusal(f"budget_cap_exceeded:committed={committed:.4f}+est={estimated_max_usd:.4f}>cap={c_val:.4f}")
        return snap

    def reserve(self, reservation_id: str, estimated_max_usd: float) -> Dict[str, Any]:
        if estimated_max_usd < 0:
            raise Refusal("invalid_cost_estimate")
        make_private_dir(self.state_root)
        with file_lock(self.lock_path, blocking=True):
            data = self._load_unlocked()
            cap = min(self.budget_cap_usd, float(data.get("budget_cap_usd", self.budget_cap_usd)))
            spent = round(float(data.get("spent_usd", 0.0)), 6)
            reserved = self._reserved_total(data)
            if spent + reserved + estimated_max_usd > cap + 1e-9:
                raise Refusal(f"budget_cap_exceeded:committed={spent+reserved:.4f}+est={estimated_max_usd:.4f}>cap={cap:.4f}")
            res_map = dict(data.get("reservations") or {})
            res_map[reservation_id] = round(float(estimated_max_usd), 6)
            data["reservations"] = res_map
            atomic_write_json(self.path, data)
            new_res = self._reserved_total(data)
            return {
                "budget_cap_usd": round(cap, 4),
                "spent_usd": spent,
                "reserved_usd": new_res,
                "remaining_usd": round(max(0.0, cap - spent - new_res), 6),
            }

    def release(self, reservation_id: str) -> None:
        make_private_dir(self.state_root)
        with file_lock(self.lock_path, blocking=True):
            data = self._load_unlocked()
            res_map = dict(data.get("reservations") or {})
            if reservation_id in res_map:
                res_map.pop(reservation_id, None)
                data["reservations"] = res_map
                atomic_write_json(self.path, data)

    def record_spend(
        self,
        job_tag: str,
        usd_spent: float,
        meta: Optional[Dict[str, Any]] = None,
        reservation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        make_private_dir(self.state_root)
        with file_lock(self.lock_path, blocking=True):
            data = self._load_unlocked()
            res_map = dict(data.get("reservations") or {})
            if reservation_id and reservation_id in res_map:
                res_map.pop(reservation_id, None)
            data["reservations"] = res_map
            delta = max(0.0, round(float(usd_spent), 6))
            data["spent_usd"] = round(float(data.get("spent_usd", 0.0)) + delta, 6)
            runs = list(data.get("runs", []))
            runs.append({
                "job_tag": job_tag,
                "usd_spent": delta,
                "at": time.time(),
                **(meta or {}),
            })
            data["runs"] = runs[-200:]
            atomic_write_json(self.path, data)
            cap = min(self.budget_cap_usd, float(data.get("budget_cap_usd", self.budget_cap_usd)))
            rem_res = self._reserved_total(data)
            return {
                "budget_cap_usd": round(cap, 4),
                "spent_usd": data["spent_usd"],
                "reserved_usd": rem_res,
                "remaining_usd": round(max(0.0, cap - data["spent_usd"] - rem_res), 6),
            }


class ColumbusController:
    """Approval-gated, budget-bounded compute controller for Diffuser."""

    def __init__(
        self,
        config: LaneConfig,
        driver: ProviderDriver,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.driver = driver
        self.clock = clock
        self.sleeper = sleeper
        self.state_root = config.state_root
        self.lane_dir = config.state_root / config.lane_id
        self.artifacts_dir = self.lane_dir / "artifacts"
        self.approvals = ApprovalStore(self.state_root, clock=clock)
        self.ledger = BudgetLedger(self.state_root, budget_cap_usd=config.budget_cap_usd)

    @property
    def quarantine_path(self) -> Path:
        return self.lane_dir / "quarantine.json"

    @property
    def park_path(self) -> Path:
        return self.lane_dir / "parked.json"

    def _require_accepting(self) -> None:
        if self.quarantine_path.exists():
            raise Refusal("lane_quarantined")
        if self.park_path.exists():
            raise Refusal("lane_parked")

    def park(self, reason: str = "operator_parked") -> Dict[str, Any]:
        make_private_dir(self.lane_dir)
        payload = {"parked": True, "reason": reason, "at": self.clock()}
        atomic_write_json(self.park_path, payload)
        return payload

    def unpark(self) -> Dict[str, Any]:
        if self.park_path.exists():
            self.park_path.unlink()
        return {"parked": False, "at": self.clock()}

    def _quarantine(self, reason: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        make_private_dir(self.lane_dir)
        payload = {"quarantined": True, "reason": reason, "details": details or {}, "at": self.clock()}
        atomic_write_json(self.quarantine_path, payload)
        return payload

    def clear_quarantine(self) -> Dict[str, Any]:
        if self.quarantine_path.exists():
            self.quarantine_path.unlink()
        return {"quarantined": False, "at": self.clock()}

    def parse_spec(self, request: Dict[str, Any]) -> JobSpec:
        mode = str(request.get("mode") or self.config.mode).strip().lower()
        if mode not in ("pod", "serverless"):
            raise Refusal(f"invalid_mode:{mode}")
        max_usd = float(request.get("max_usd", min(0.50, self.config.max_usd_per_job)))
        if max_usd <= 0 or max_usd > self.config.max_usd_per_job:
            raise Refusal("max_usd_out_of_bounds")
        max_wall = int(request.get("max_walltime_secs", min(600, self.config.max_walltime_secs)))
        if max_wall < 30 or max_wall > self.config.max_walltime_secs:
            raise Refusal("max_walltime_out_of_bounds")
        startup_secs = int(request.get("startup_timeout_secs", min(self.config.default_startup_timeout_secs, max_wall)))
        startup_secs = max(30, min(startup_secs, max_wall))
        idle_secs = int(request.get("idle_timeout_secs", min(self.config.default_idle_timeout_secs, max_wall)))
        idle_secs = max(10, min(idle_secs, max_wall))
        gpu_count = int(request.get("gpu_count", 1))
        if gpu_count < 1 or gpu_count > 8:
            raise Refusal("invalid_gpu_count")
        raw_env = request.get("env") or {}
        if not isinstance(raw_env, dict):
            raise Refusal("invalid_env")
        env = {str(k): str(v) for k, v in raw_env.items()}
        raw_files = request.get("files_out") or []
        if not isinstance(raw_files, list) or len(raw_files) > 2:
            raise Refusal("invalid_files_out")
        files_out = tuple(str(x) for x in raw_files)
        payload = request.get("payload") or {}
        if not isinstance(payload, dict):
            raise Refusal("invalid_payload")
        return JobSpec(
            mode=mode,
            provider=self.driver.name,
            gpu_type=str(request.get("gpu_type") or "NVIDIA GeForce RTX 4090"),
            gpu_count=gpu_count,
            cloud_type=str(request.get("cloud_type") or "COMMUNITY").upper(),
            image=str(request.get("image") or "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"),
            command=str(request.get("command") or ""),
            docker_args=str(request.get("docker_args") or ""),
            container_disk_gb=int(request.get("container_disk_gb", 20)),
            volume_gb=int(request.get("volume_gb", 0)),
            network_volume_id=str(request["network_volume_id"]) if request.get("network_volume_id") else None,
            env=env,
            files_out=files_out,
            endpoint_id=str(request.get("endpoint_id") or ""),
            payload=payload,
            max_usd=max_usd,
            max_walltime_secs=max_wall,
            startup_timeout_secs=startup_secs,
            idle_timeout_secs=idle_secs,
            requesting_agent=str(request.get("requesting_agent") or "agent"),
        )

    def status(self) -> Dict[str, Any]:
        balance = self.driver.get_balance()
        owned = self.driver.list_owned(OWNED_PREFIX)
        pending = self.approvals.list_tickets(status_filter="pending_approval")
        ledger_snap = self.ledger.snapshot()
        return {
            "ok": True,
            "lane_id": self.config.lane_id,
            "provider": self.driver.name,
            "balance_usd": balance,
            "min_balance_floor_usd": self.config.min_balance_floor_usd,
            "budget": ledger_snap,
            "owned_resources": [r.to_dict() for r in owned],
            "owned_count": len(owned),
            "pending_approvals": [
                {
                    "ticket_id": t["ticket_id"],
                    "requesting_agent": t["requesting_agent"],
                    "estimated_max_usd": t["estimate"].get("estimated_max_usd"),
                    "expires_at": t["expires_at"],
                }
                for t in pending
            ],
            "parked": self.park_path.exists(),
            "quarantined": self.quarantine_path.exists(),
        }

    def estimate(self, request: Dict[str, Any]) -> Dict[str, Any]:
        spec = self.parse_spec(request)
        est = self.driver.estimate(spec)
        balance = self.driver.get_balance()
        ledger_snap = self.ledger.snapshot()
        within_job_cap = est.estimated_max_usd <= spec.max_usd + 1e-9
        within_budget = (ledger_snap["spent_usd"] + est.estimated_max_usd) <= ledger_snap["budget_cap_usd"] + 1e-9
        above_floor = balance >= self.config.min_balance_floor_usd
        return {
            "ok": True,
            "lane_id": self.config.lane_id,
            "estimate": est.to_dict(),
            "spec_sha256": canonical_spec_hash(spec.to_canonical_dict()),
            "balance_usd": balance,
            "budget": ledger_snap,
            "admissible": within_job_cap and within_budget and above_floor,
        }

    def stage(self, request: Dict[str, Any]) -> Dict[str, Any]:
        self._require_accepting()
        spec = self.parse_spec(request)
        est = self.driver.estimate(spec)
        if est.estimated_max_usd > spec.max_usd + 1e-9:
            raise Refusal(f"estimate_exceeds_max_usd:{est.estimated_max_usd:.4f}>{spec.max_usd:.4f}")
        balance = self.driver.get_balance()
        if balance < self.config.min_balance_floor_usd:
            raise Refusal(f"balance_below_floor:{balance:.4f}<{self.config.min_balance_floor_usd:.4f}")
        self.ledger.check_admission(est.estimated_max_usd)
        ttl = int(request.get("approval_ttl_secs", self.config.default_approval_ttl_secs))
        ticket = self.approvals.stage_ticket(
            spec_dict=spec.to_canonical_dict(),
            estimate_dict=est.to_dict(),
            requesting_agent=spec.requesting_agent,
            approval_ttl_secs=ttl,
        )
        tid = ticket["ticket_id"]
        return {
            "ok": True,
            "status": "pending_approval",
            "lane_id": self.config.lane_id,
            "ticket_id": tid,
            "spec_sha256": ticket["spec_sha256"],
            "estimate": est.to_dict(),
            "expires_at": ticket["expires_at"],
            "approval_command": f"columbus_ctl.py approve {tid}",
        }

    def _verify_zero_owned(self, owned_name: str) -> bool:
        remaining = [r for r in self.driver.list_owned(OWNED_PREFIX) if r.name == owned_name]
        return len(remaining) == 0

    def _cleanup_owned(self, resource_id: str, owned_name: str, spec: JobSpec) -> bool:
        try:
            self.driver.terminate(resource_id, spec)
        except Exception as exc:
            self._quarantine("terminate_failed", {"resource_id": resource_id, "owned_name": owned_name, "error": str(exc)})
            return False
        if spec.mode == "pod":
            try:
                if not self._verify_zero_owned(owned_name):
                    self._quarantine("owned_pod_still_present", {"resource_id": resource_id, "owned_name": owned_name})
                    return False
            except Exception as exc:
                self._quarantine("cleanup_verify_ambiguous", {"resource_id": resource_id, "owned_name": owned_name, "error": str(exc)})
                return False
        return True

    def execute(
        self,
        request: Dict[str, Any],
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Full approval-gated execution lifecycle with durable intent, ephemeral SSH, and guaranteed teardown."""
        self._require_accepting()
        spec = self.parse_spec(request)
        spec_dict = spec.to_canonical_dict()
        est = self.driver.estimate(spec)
        if est.estimated_max_usd > spec.max_usd + 1e-9:
            raise Refusal(f"estimate_exceeds_max_usd:{est.estimated_max_usd:.4f}>{spec.max_usd:.4f}")

        balance_before = self.driver.get_balance()
        if balance_before < self.config.min_balance_floor_usd:
            raise Refusal(f"balance_below_floor:{balance_before:.4f}<{self.config.min_balance_floor_usd:.4f}")
        self.ledger.check_admission(est.estimated_max_usd)

        # Mandatory human owner approval gate
        ticket_id = request.get("ticket_id")
        if ticket_id:
            approval_ticket = self.approvals.verify_and_consume(str(ticket_id), spec_dict)
        else:
            wait_for_owner = bool(request.get("wait_for_approval", True))
            ttl = int(request.get("approval_ttl_secs", self.config.default_approval_ttl_secs))
            staged = self.approvals.stage_ticket(
                spec_dict=spec_dict,
                estimate_dict=est.to_dict(),
                requesting_agent=spec.requesting_agent,
                approval_ttl_secs=ttl,
            )
            ticket_id = staged["ticket_id"]
            if not wait_for_owner:
                return {
                    "ok": True,
                    "status": "pending_approval",
                    "lane_id": self.config.lane_id,
                    "ticket_id": ticket_id,
                    "spec_sha256": staged["spec_sha256"],
                    "estimate": est.to_dict(),
                    "expires_at": staged["expires_at"],
                }
            approval_ticket = self.approvals.wait_for_approval(
                ticket_id=ticket_id,
                spec_dict=spec_dict,
                poll_interval_secs=self.config.approval_poll_secs,
                sleeper=self.sleeper,
                is_cancelled=is_cancelled,
            )

        make_private_dir(self.lane_dir)
        lock_path = self.lane_dir / "seat-0.lock"
        intent_path = self.lane_dir / "seat-0.intent.json"

        with file_lock(lock_path, blocking=False):
            self._require_accepting()
            if intent_path.exists():
                raise Refusal("unresolved_prior_intent_run_reap")

            balance_before = self.driver.get_balance()
            if balance_before < self.config.min_balance_floor_usd:
                raise Refusal(f"balance_below_floor:{balance_before:.4f}")

            job_nonce = secrets.token_hex(4)
            reservation_id = f"{self.config.lane_id}-{job_nonce}"
            self.ledger.reserve(reservation_id, est.estimated_max_usd)

            use_ssh = spec.mode == "pod" and bool(spec.command)
            ssh_key_dir: Optional[Path] = None
            ssh_priv_path: Optional[Path] = None
            ssh_pub_key: Optional[str] = None
            owned_name = f"{OWNED_PREFIX}{self.config.lane_id}-{job_nonce}-g1"
            launch = None
            attempts = 0
            spend_recorded = False
            try:
                if use_ssh:
                    ssh_key_dir, ssh_priv_path, ssh_pub_key = _generate_ephemeral_ssh_keypair(self.lane_dir)
                intent = {
                    "pid": os.getpid(),
                    "lane_id": self.config.lane_id,
                    "owned_name": owned_name,
                    "mode": spec.mode,
                    "endpoint_id": spec.endpoint_id,
                    "ticket_id": approval_ticket["ticket_id"],
                    "spec_sha256": approval_ticket["spec_sha256"],
                    "resource_id": None,
                    "created_at": self.clock(),
                }
                atomic_write_json(intent_path, intent)
                while True:
                    attempts += 1
                    try:
                        launch = self.driver.create(spec, owned_name, ssh_public_key=ssh_pub_key)
                        break
                    except TransientAllocationError:
                        if attempts >= 2:
                            if intent_path.exists():
                                intent_path.unlink()
                            return {
                                "ok": False,
                                "lane_id": self.config.lane_id,
                                "error": "allocation_failed",
                                "attempts": attempts,
                                "cleanup_verified": True,
                            }
                        self.sleeper(self.config.alloc_retry_delay_secs)
                        recheck_bal = self.driver.get_balance()
                        owned_now = [r for r in self.driver.list_owned(OWNED_PREFIX) if r.name == owned_name]
                        if abs(recheck_bal - balance_before) > 1e-6 or owned_now or recheck_bal < self.config.min_balance_floor_usd:
                            if owned_now:
                                for r in owned_now:
                                    self._cleanup_owned(r.resource_id, owned_name, spec)
                            if intent_path.exists() and not self.quarantine_path.exists():
                                intent_path.unlink()
                            return {
                                "ok": False,
                                "lane_id": self.config.lane_id,
                                "error": "allocation_retry_aborted",
                                "attempts": attempts,
                            }
                        owned_name = f"{OWNED_PREFIX}{self.config.lane_id}-{job_nonce}-g2"
                        intent["owned_name"] = owned_name
                        atomic_write_json(intent_path, intent)
                    except Exception:
                        if intent_path.exists():
                            intent_path.unlink()
                        raise

                intent["resource_id"] = launch.resource_id
                atomic_write_json(intent_path, intent)

                started_at = self.clock()
                last_progress_at = started_at
                last_progress_marker = ""
                final_state = "running"
                final_output: Any = None
                final_error = ""
                exit_code: Optional[int] = None
                stdout_preview = ""
                artifact_files: Dict[str, bytes] = {}
                cleanup_verified = False

                try:
                    if use_ssh:
                        assert ssh_priv_path is not None
                        ssh_ep: Optional[Tuple[str, int]] = None
                        while True:
                            now = self.clock()
                            if is_cancelled and is_cancelled():
                                final_state = "cancelled"
                                final_error = "cancelled_during_startup"
                                break
                            if (now - started_at) >= spec.startup_timeout_secs:
                                final_state = "timed_out"
                                final_error = "pod_startup_timeout"
                                break
                            if (now - started_at) >= spec.max_walltime_secs:
                                final_state = "timed_out"
                                final_error = "walltime_exceeded"
                                break
                            poll_res = self.driver.poll(launch.resource_id, spec)
                            if poll_res.ssh_endpoint:
                                ssh_ep = poll_res.ssh_endpoint
                                break
                            if poll_res.state in ("failed", "cancelled", "timed_out"):
                                final_state = poll_res.state
                                final_error = poll_res.error or "pod_failed_during_startup"
                                break
                            self.sleeper(self.config.job_poll_secs)

                        if ssh_ep is not None:
                            remaining = max(10.0, spec.max_walltime_secs - (self.clock() - started_at))
                            exec_res = self.driver.run_pod_command(
                                ssh_endpoint=ssh_ep,
                                identity_path=ssh_priv_path,
                                spec=spec,
                                walltime_remaining_secs=remaining,
                                is_cancelled=is_cancelled,
                            )
                            final_state = exec_res.state
                            exit_code = exec_res.exit_code
                            final_error = exec_res.error
                            stdout_preview = exec_res.stdout[:800]
                            if exec_res.stdout:
                                artifact_files["stdout.log"] = exec_res.stdout.encode("utf-8")
                            if exec_res.stderr:
                                artifact_files["stderr.log"] = exec_res.stderr.encode("utf-8")
                            for fname, fbytes in exec_res.files_out_bytes.items():
                                if len(artifact_files) < 4:
                                    artifact_files[fname] = fbytes
                    else:
                        while True:
                            now = self.clock()
                            if is_cancelled and is_cancelled():
                                final_state = "cancelled"
                                final_error = "cancelled_by_caller"
                                break
                            if (now - started_at) >= spec.max_walltime_secs:
                                final_state = "timed_out"
                                final_error = "walltime_exceeded"
                                break
                            poll_res = self.driver.poll(launch.resource_id, spec)
                            if poll_res.progress_token and poll_res.progress_token != last_progress_marker:
                                last_progress_marker = poll_res.progress_token
                                last_progress_at = now
                            if poll_res.state in ("completed", "failed", "cancelled", "timed_out"):
                                final_state = poll_res.state
                                final_output = poll_res.output
                                final_error = poll_res.error
                                break
                            if (now - last_progress_at) >= spec.idle_timeout_secs:
                                final_state = "idle_timeout"
                                final_error = "idle_timeout"
                                break
                            self.sleeper(self.config.job_poll_secs)
                finally:
                    if spec.mode == "pod" or final_state not in ("completed", "failed", "cancelled", "timed_out"):
                        cleanup_verified = self._cleanup_owned(launch.resource_id, owned_name, spec)
                    else:
                        cleanup_verified = True
                    if cleanup_verified and intent_path.exists():
                        intent_path.unlink()
                elapsed_secs = max(0.0, self.clock() - started_at)
                balance_after = balance_before
                try:
                    balance_after = self.driver.get_balance()
                except Exception:
                    pass
                observed_delta = max(0.0, round(balance_before - balance_after, 6))
                est_runtime_usd = round(est.hourly_rate_usd * (elapsed_secs / 3600.0), 6)
                recorded_usd = observed_delta if observed_delta > 0 else min(est.estimated_max_usd, est_runtime_usd)
                budget_after = self.ledger.record_spend(
                    owned_name,
                    recorded_usd,
                    meta={"mode": spec.mode, "resource_id": launch.resource_id, "state": final_state},
                    reservation_id=reservation_id,
                )
                spend_recorded = True
            finally:
                if not spend_recorded:
                    self.ledger.release(reservation_id)
                if ssh_key_dir and ssh_key_dir.exists():
                    shutil.rmtree(ssh_key_dir, ignore_errors=True)

            if final_output is not None and "output.json" not in artifact_files and len(artifact_files) < 4:
                artifact_files["output.json"] = json.dumps(final_output, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")

            artifact_receipts: List[Dict[str, Any]] = []
            if artifact_files:
                artifact_receipts = write_artifacts(self.artifacts_dir / owned_name, artifact_files)

            return {
                "ok": final_state == "completed" and cleanup_verified,
                "lane_id": self.config.lane_id,
                "provider": self.driver.name,
                "mode": spec.mode,
                "ticket_id": approval_ticket["ticket_id"],
                "owned_name": owned_name,
                "resource_id": launch.resource_id,
                "state": final_state,
                "exit_code": exit_code,
                "stdout_preview": stdout_preview or None,
                "error": final_error or None,
                "attempts": attempts,
                "elapsed_secs": round(elapsed_secs, 2),
                "usd_spent": recorded_usd,
                "balance_before_usd": balance_before,
                "balance_after_usd": balance_after,
                "budget": budget_after,
                "cleanup_verified": cleanup_verified,
                "artifacts": artifact_receipts,
            }

    def reap(self, dry_run: bool = False) -> Dict[str, Any]:
        """Reconcile orphaned owned intents whose owner PID is no longer alive."""
        candidates: List[Dict[str, Any]] = []
        reaped: List[str] = []
        skipped: List[Dict[str, Any]] = []
        if not self.lane_dir.exists():
            return {"ok": True, "dry_run": dry_run, "candidates": [], "reaped": [], "skipped": []}

        lock_path = self.lane_dir / "seat-0.lock"
        intent_path = self.lane_dir / "seat-0.intent.json"
        if not intent_path.exists():
            return {"ok": True, "dry_run": dry_run, "candidates": [], "reaped": [], "skipped": []}

        try:
            with file_lock(lock_path, blocking=False):
                if not intent_path.exists():
                    return {"ok": True, "dry_run": dry_run, "candidates": [], "reaped": [], "skipped": []}
                intent = read_json(intent_path)
                pid = int(intent.get("pid") or 0)
                owned_name = str(intent.get("owned_name") or "")
                resource_id = str(intent.get("resource_id") or "")
                if pid_is_alive(pid):
                    skipped.append({"owned_name": owned_name, "reason": "pid_alive", "pid": pid})
                    return {"ok": True, "dry_run": dry_run, "candidates": [], "reaped": [], "skipped": skipped}
                candidates.append({"owned_name": owned_name, "resource_id": resource_id, "pid": pid})
                if dry_run:
                    return {"ok": True, "dry_run": True, "candidates": candidates, "reaped": [], "skipped": skipped}
                spec = JobSpec(mode=str(intent.get("mode") or "pod"), endpoint_id=str(intent.get("endpoint_id") or ""))
                if not owned_name.startswith(OWNED_PREFIX):
                    self._quarantine("malformed_intent_owned_name", {"owned_name": owned_name})
                    return {"ok": False, "dry_run": False, "candidates": candidates, "reaped": [], "skipped": skipped, "quarantined": True}
                owned_now = [r for r in self.driver.list_owned(OWNED_PREFIX) if r.name == owned_name]
                if not owned_now:
                    # Provider confirms zero owned resources with this exact tag; clear stale intent without blind terminate
                    intent_path.unlink()
                    reaped.append(owned_name)
                    return {"ok": True, "dry_run": False, "candidates": candidates, "reaped": reaped, "skipped": skipped}
                target_ids = [r.resource_id for r in owned_now]
                all_clean = True
                for rid in target_ids:
                    if not self._cleanup_owned(rid, owned_name, spec):
                        all_clean = False
                if all_clean and intent_path.exists():
                    intent_path.unlink()
                    reaped.append(owned_name)
                return {
                    "ok": all_clean,
                    "dry_run": False,
                    "candidates": candidates,
                    "reaped": reaped,
                    "skipped": skipped,
                    "quarantined": self.quarantine_path.exists(),
                }
        except SecureIOError as exc:
            if str(exc) == "lock_busy":
                skipped.append({"reason": "seat_lock_busy"})
                return {"ok": True, "dry_run": dry_run, "candidates": [], "reaped": [], "skipped": skipped}
            raise

    def handle(self, request: Dict[str, Any], is_cancelled: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
        try:
            if not isinstance(request, dict):
                raise Refusal("invalid_request")
            action = str(request.get("action") or "").strip().lower()
            if action in ("status", "health"):
                return self.status()
            if action == "estimate":
                return self.estimate(request)
            if action == "stage":
                return self.stage(request)
            if action == "exec":
                return self.execute(request, is_cancelled=is_cancelled)
            raise Refusal("invalid_action")
        except (Refusal, ApprovalError, ArtifactError, DriverError, SecureIOError) as exc:
            return {"ok": False, "lane_id": self.config.lane_id, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "lane_id": self.config.lane_id, "error": f"invalid_value:{exc}"}
