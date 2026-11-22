"""ProviderDriver interface and typed job/estimate/resource records."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


class DriverError(Exception):
    """Base error raised by compute provider drivers."""


class TransientAllocationError(DriverError):
    """Raised when provider capacity/allocation fails transiently with $0 spent."""


@dataclass(frozen=True)
class JobSpec:
    mode: str  # "pod" or "serverless"
    provider: str = "runpod"
    gpu_type: str = "NVIDIA GeForce RTX 4090"
    gpu_count: int = 1
    cloud_type: str = "COMMUNITY"  # COMMUNITY or SECURE
    image: str = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
    docker_args: str = ""
    container_disk_gb: int = 20
    volume_gb: int = 0
    network_volume_id: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    endpoint_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    max_usd: float = 0.50
    max_walltime_secs: int = 600
    idle_timeout_secs: int = 300
    requesting_agent: str = "agent"

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "provider": self.provider,
            "gpu_type": self.gpu_type,
            "gpu_count": self.gpu_count,
            "cloud_type": self.cloud_type,
            "image": self.image,
            "docker_args": self.docker_args,
            "container_disk_gb": self.container_disk_gb,
            "volume_gb": self.volume_gb,
            "network_volume_id": self.network_volume_id,
            "env": dict(sorted(self.env.items())),
            "endpoint_id": self.endpoint_id,
            "payload": self.payload,
            "max_usd": round(float(self.max_usd), 6),
            "max_walltime_secs": int(self.max_walltime_secs),
            "idle_timeout_secs": int(self.idle_timeout_secs),
        }


@dataclass(frozen=True)
class CostEstimate:
    provider: str
    mode: str
    hourly_rate_usd: float
    estimated_max_usd: float
    resolved_target: str
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OwnedResource:
    resource_id: str
    name: str
    status: str
    uptime_secs: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LaunchResult:
    resource_id: str
    name: str
    status: str
    raw_meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PollResult:
    state: str  # "pending", "running", "completed", "failed", "cancelled", "timed_out"
    progress_token: str = ""
    output: Any = None
    error: str = ""
    uptime_secs: int = 0


class ProviderDriver(ABC):
    """Pluggable provider backend contract."""

    name: str = "base"

    @abstractmethod
    def get_balance(self) -> float:
        """Return live account USD balance (fail-closed on API or parse errors)."""

    @abstractmethod
    def estimate(self, spec: JobSpec) -> CostEstimate:
        """Return conservative max USD cost estimate for spec."""

    @abstractmethod
    def list_owned(self, prefix: str) -> List[OwnedResource]:
        """Return ONLY resources whose name starts with prefix."""

    @abstractmethod
    def create(self, spec: JobSpec, owned_name: str) -> LaunchResult:
        """Provision a pod or submit a serverless job tagged with owned_name."""

    @abstractmethod
    def poll(self, resource_id: str, spec: JobSpec) -> PollResult:
        """Poll execution status and progress."""

    @abstractmethod
    def terminate(self, resource_id: str, spec: JobSpec) -> None:
        """Terminate an owned pod or cancel an in-flight serverless job."""
