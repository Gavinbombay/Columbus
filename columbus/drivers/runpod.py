"""RunPod provider driver (Pods via GraphQL + Serverless via REST v2) using stdlib only."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
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
from columbus.secure_io import load_api_key, redact_secrets

DEFAULT_GRAPHQL_URL = "https://api.runpod.io/graphql"
DEFAULT_SERVERLESS_BASE_URL = "https://api.runpod.ai/v2"
DEFAULT_KEY_FILE = Path.home() / ".config" / "columbus" / "runpod_api_key"
DEFAULT_SERVERLESS_CONSERVATIVE_HOURLY_USD = 2.50

TRANSIENT_ALLOC_MARKERS = (
    "no longer any instances available",
    "insufficient capacity",
    "no available gpu",
    "could not find any",
    "temporarily unavailable",
)


class RunPodDriver(ProviderDriver):
    """RunPod driver loading the API key dynamically per request to support rotation."""

    name = "runpod"

    def __init__(
        self,
        key_file: Optional[Path] = None,
        graphql_url: str = DEFAULT_GRAPHQL_URL,
        serverless_base_url: str = DEFAULT_SERVERLESS_BASE_URL,
        http_timeout_secs: int = 30,
    ) -> None:
        env_key = os.environ.get("COLUMBUS_RUNPOD_KEY_FILE")
        self.key_file = key_file or (Path(env_key) if env_key else DEFAULT_KEY_FILE)
        self.graphql_url = graphql_url.rstrip("/")
        self.serverless_base_url = serverless_base_url.rstrip("/")
        self.http_timeout_secs = http_timeout_secs

    def _api_key(self) -> str:
        return load_api_key(self.key_file)

    def _http_json(
        self,
        url: str,
        method: str = "POST",
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        key = self._api_key()
        data = json.dumps(body, allow_nan=False).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {key}",
            "User-Agent": "columbus-diffuser-adapter/0.1",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout_secs) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            safe = redact_secrets(err_body, (key,))
            raise DriverError(f"runpod_http_{exc.code}: {safe}") from exc
        except urllib.error.URLError as exc:
            safe = redact_secrets(str(exc), (key,))
            raise DriverError(f"runpod_network_error: {safe}") from exc

        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise DriverError("runpod_invalid_json_response") from exc
        if not isinstance(parsed, dict):
            raise DriverError("runpod_unexpected_response_type")
        return parsed

    def _graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        resp = self._http_json(self.graphql_url, method="POST", body=payload)
        errors = resp.get("errors")
        if errors:
            msg = "; ".join(str(e.get("message", e)) for e in errors if isinstance(e, dict))
            lower = msg.lower()
            if any(marker in lower for marker in TRANSIENT_ALLOC_MARKERS):
                raise TransientAllocationError(f"runpod_capacity: {msg[:240]}")
            raise DriverError(f"runpod_graphql_error: {msg[:240]}")
        data = resp.get("data")
        if not isinstance(data, dict):
            raise DriverError("runpod_missing_graphql_data")
        return data

    def get_balance(self) -> float:
        data = self._graphql("{ myself { clientBalance } }")
        myself = data.get("myself")
        if not isinstance(myself, dict) or myself.get("clientBalance") is None:
            raise DriverError("runpod_balance_unavailable")
        try:
            val = float(myself["clientBalance"])
        except (TypeError, ValueError) as exc:
            raise DriverError("runpod_balance_invalid") from exc
        if val < 0:
            raise DriverError("runpod_balance_negative")
        return val

    def estimate(self, spec: JobSpec) -> CostEstimate:
        if spec.mode == "pod":
            data = self._graphql(
                "{ gpuTypes { id displayName memoryInGb communityPrice securePrice communityCloud secureCloud } }"
            )
            gpu_types = data.get("gpuTypes")
            if not isinstance(gpu_types, list):
                raise DriverError("runpod_gpu_types_unavailable")
            target = spec.gpu_type.strip().lower()
            matched = None
            for g in gpu_types:
                if not isinstance(g, dict):
                    continue
                gid = str(g.get("id", "")).strip().lower()
                gname = str(g.get("displayName", "")).strip().lower()
                if target in (gid, gname):
                    matched = g
                    break
            if not matched:
                raise DriverError(f"runpod_unknown_gpu_type:{spec.gpu_type}")
            price_field = "securePrice" if spec.cloud_type.upper() == "SECURE" else "communityPrice"
            hourly = matched.get(price_field) or matched.get("communityPrice") or matched.get("securePrice")
            if hourly is None or float(hourly) <= 0:
                raise DriverError(f"runpod_gpu_price_unavailable:{spec.gpu_type}")
            hourly_f = float(hourly) * max(1, int(spec.gpu_count))
            est_max = round(hourly_f * (float(spec.max_walltime_secs) / 3600.0), 4)
            return CostEstimate(
                provider=self.name,
                mode="pod",
                hourly_rate_usd=round(hourly_f, 4),
                estimated_max_usd=est_max,
                resolved_target=str(matched.get("id", spec.gpu_type)),
                notes=f"{spec.cloud_type.upper()} {spec.gpu_count}x {matched.get("displayName", spec.gpu_type)}",
            )

        if spec.mode == "serverless":
            if not spec.endpoint_id:
                raise DriverError("runpod_serverless_endpoint_id_required")
            data = self._graphql("{ myself { endpoints { id name gpuIds } } }")
            endpoints = (data.get("myself") or {}).get("endpoints")
            if not isinstance(endpoints, list):
                raise DriverError("runpod_endpoints_unavailable")
            matched = next((e for e in endpoints if isinstance(e, dict) and e.get("id") == spec.endpoint_id), None)
            if not matched:
                raise DriverError(f"runpod_unknown_endpoint:{spec.endpoint_id}")
            hourly_f = DEFAULT_SERVERLESS_CONSERVATIVE_HOURLY_USD
            est_max = round(min(spec.max_usd, hourly_f * (float(spec.max_walltime_secs) / 3600.0)), 4)
            return CostEstimate(
                provider=self.name,
                mode="serverless",
                hourly_rate_usd=hourly_f,
                estimated_max_usd=est_max,
                resolved_target=str(matched.get("id")),
                notes=f"serverless endpoint {matched.get("name", spec.endpoint_id)}",
            )

        raise DriverError(f"unsupported_mode:{spec.mode}")

    def list_owned(self, prefix: str) -> List[OwnedResource]:
        if not prefix or not prefix.startswith("diffuser-columbus-"):
            raise DriverError("invalid_owned_prefix")
        data = self._graphql(
            "{ myself { pods { id name desiredStatus runtime { uptimeInSeconds } } } }"
        )
        myself = data.get("myself")
        if not isinstance(myself, dict):
            raise DriverError("runpod_pods_list_ambiguous")
        pods = myself.get("pods")
        if pods is None:
            return []
        if not isinstance(pods, list):
            raise DriverError("runpod_pods_list_ambiguous")
        owned: List[OwnedResource] = []
        for pod in pods:
            if not isinstance(pod, dict):
                raise DriverError("runpod_pod_entry_ambiguous")
            name = str(pod.get("name") or "")
            if not name.startswith(prefix):
                continue
            runtime = pod.get("runtime") if isinstance(pod.get("runtime"), dict) else {}
            owned.append(
                OwnedResource(
                    resource_id=str(pod.get("id") or ""),
                    name=name,
                    status=str(pod.get("desiredStatus") or "UNKNOWN"),
                    uptime_secs=int(runtime.get("uptimeInSeconds") or 0),
                )
            )
        return owned

    def create(self, spec: JobSpec, owned_name: str) -> LaunchResult:
        if not owned_name.startswith("diffuser-columbus-"):
            raise DriverError("invalid_owned_name")
        if spec.mode == "pod":
            env_map = {"HF_HUB_DISABLE_XET": "1", "COLUMBUS_JOB_TAG": owned_name}
            env_map.update(spec.env)
            env_list = [{"key": str(k), "value": str(v)} for k, v in sorted(env_map.items())]
            pod_input: Dict[str, Any] = {
                "cloudType": spec.cloud_type.upper(),
                "gpuCount": int(spec.gpu_count),
                "volumeInGb": int(spec.volume_gb),
                "containerDiskInGb": int(spec.container_disk_gb),
                "gpuTypeId": spec.gpu_type,
                "name": owned_name,
                "imageName": spec.image,
                "env": env_list,
            }
            if spec.docker_args:
                pod_input["dockerArgs"] = spec.docker_args
            if spec.network_volume_id:
                pod_input["networkVolumeId"] = spec.network_volume_id

            query = """
            mutation($input: PodFindAndDeployOnDemandInput!) {
              podFindAndDeployOnDemand(input: $input) {
                id
                name
                desiredStatus
                imageName
                machineId
              }
            }
            """
            data = self._graphql(query, {"input": pod_input})
            pod = data.get("podFindAndDeployOnDemand")
            if not isinstance(pod, dict) or not pod.get("id"):
                raise DriverError("runpod_pod_create_missing_id")
            return LaunchResult(
                resource_id=str(pod["id"]),
                name=str(pod.get("name") or owned_name),
                status=str(pod.get("desiredStatus") or "CREATED"),
                raw_meta={"machine_id": pod.get("machineId")},
            )

        if spec.mode == "serverless":
            if not spec.endpoint_id:
                raise DriverError("runpod_serverless_endpoint_id_required")
            url = f"{self.serverless_base_url}/{spec.endpoint_id}/run"
            resp = self._http_json(url, method="POST", body={"input": spec.payload})
            job_id = resp.get("id")
            if not job_id:
                raise DriverError("runpod_serverless_missing_job_id")
            return LaunchResult(
                resource_id=str(job_id),
                name=owned_name,
                status=str(resp.get("status") or "IN_QUEUE"),
            )

        raise DriverError(f"unsupported_mode:{spec.mode}")

    def poll(self, resource_id: str, spec: JobSpec) -> PollResult:
        if spec.mode == "pod":
            query = """
            query($input: PodFilter!) {
              pod(input: $input) {
                id
                name
                desiredStatus
                lastStatusChange
                runtime {
                  uptimeInSeconds
                  gpus { id gpuUtilPercent memoryUtilPercent }
                }
              }
            }
            """
            data = self._graphql(query, {"input": {"podId": resource_id}})
            pod = data.get("pod")
            if pod is None:
                return PollResult(state="completed", progress_token="pod_absent")
            if not isinstance(pod, dict):
                raise DriverError("runpod_poll_ambiguous")
            desired = str(pod.get("desiredStatus") or "").upper()
            last_change = str(pod.get("lastStatusChange") or "")
            runtime = pod.get("runtime") if isinstance(pod.get("runtime"), dict) else {}
            uptime = int(runtime.get("uptimeInSeconds") or 0)
            if desired in ("EXITED", "TERMINATED", "STOPPED"):
                return PollResult(
                    state="completed",
                    progress_token=f"{desired}:{uptime}:{last_change}",
                    output={"desiredStatus": desired, "uptimeInSeconds": uptime, "lastStatusChange": last_change},
                    uptime_secs=uptime,
                )
            if runtime and uptime > 0:
                return PollResult(
                    state="running",
                    progress_token=f"running:{uptime}:{last_change}",
                    uptime_secs=uptime,
                )
            return PollResult(
                state="pending",
                progress_token=f"pending:{desired}:{last_change}",
                uptime_secs=uptime,
            )

        if spec.mode == "serverless":
            url = f"{self.serverless_base_url}/{spec.endpoint_id}/status/{resource_id}"
            resp = self._http_json(url, method="GET")
            status = str(resp.get("status") or "").upper()
            exec_ms = int(resp.get("executionTime") or 0)
            if status == "COMPLETED":
                return PollResult(state="completed", progress_token=f"completed:{exec_ms}", output=resp.get("output"), uptime_secs=exec_ms // 1000)
            if status == "FAILED":
                return PollResult(state="failed", progress_token="failed", error=str(resp.get("error") or "serverless_failed"))
            if status == "CANCELLED":
                return PollResult(state="cancelled", progress_token="cancelled")
            if status == "TIMED_OUT":
                return PollResult(state="timed_out", progress_token="timed_out")
            if status == "IN_PROGRESS":
                return PollResult(state="running", progress_token=f"in_progress:{exec_ms}", uptime_secs=exec_ms // 1000)
            if status == "IN_QUEUE":
                return PollResult(state="pending", progress_token="in_queue")
            raise DriverError(f"runpod_serverless_unknown_status:{status}")

        raise DriverError(f"unsupported_mode:{spec.mode}")

    def terminate(self, resource_id: str, spec: JobSpec) -> None:
        if spec.mode == "pod":
            query = "mutation($input: PodTerminateInput!) { podTerminate(input: $input) }"
            self._graphql(query, {"input": {"podId": resource_id}})
            return
        if spec.mode == "serverless":
            url = f"{self.serverless_base_url}/{spec.endpoint_id}/cancel/{resource_id}"
            self._http_json(url, method="POST", body={})
            return
        raise DriverError(f"unsupported_mode:{spec.mode}")
