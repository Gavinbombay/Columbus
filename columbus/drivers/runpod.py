"""RunPod provider driver (Pods via GraphQL + ephemeral SSH + Serverless via REST v2) using stdlib only."""

from __future__ import annotations

import base64
import json
import os
import select
import shlex
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from columbus.artifacts import MAX_FILE_BYTES, validate_artifact_name
from columbus.drivers.base import (
    CostEstimate,
    DriverError,
    ExecResult,
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
MAX_CAPTURED_LOG_BYTES = 2 * 1024 * 1024  # 2 MiB per stream
MAX_HTTP_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MiB API response cap

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
                raw_bytes = resp.read(MAX_HTTP_RESPONSE_BYTES + 1)
                if len(raw_bytes) > MAX_HTTP_RESPONSE_BYTES:
                    raise DriverError("runpod_response_size_limit")
                raw = raw_bytes.decode("utf-8")
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                # HTTPError bodies are controlled by the remote endpoint. Bound
                # the read itself; slicing afterward would still buffer an
                # arbitrarily large error response in memory.
                err_body = exc.read(301).decode("utf-8", errors="replace")[:300]
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
            disp = matched.get("displayName") or spec.gpu_type
            return CostEstimate(
                provider=self.name,
                mode="pod",
                hourly_rate_usd=round(hourly_f, 4),
                estimated_max_usd=est_max,
                resolved_target=str(matched.get("id", spec.gpu_type)),
                notes=f"{spec.cloud_type.upper()} {spec.gpu_count}x {disp}",
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
            ep_name = matched.get("name") or spec.endpoint_id
            return CostEstimate(
                provider=self.name,
                mode="serverless",
                hourly_rate_usd=hourly_f,
                estimated_max_usd=est_max,
                resolved_target=str(matched.get("id")),
                notes=f"serverless endpoint {ep_name}",
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

    def create(
        self,
        spec: JobSpec,
        owned_name: str,
        ssh_public_key: Optional[str] = None,
    ) -> LaunchResult:
        if not owned_name.startswith("diffuser-columbus-"):
            raise DriverError("invalid_owned_name")
        if spec.mode == "pod":
            env_map = {"HF_HUB_DISABLE_XET": "1", "COLUMBUS_JOB_TAG": owned_name}
            env_map.update(spec.env)
            if ssh_public_key:
                env_map["PUBLIC_KEY"] = ssh_public_key.strip()
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
                "startSsh": bool(ssh_public_key),
                "ports": "22/tcp",
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
                  ports { ip isIpPublic privatePort publicPort type }
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

            ssh_ep: Optional[Tuple[str, int]] = None
            ports = runtime.get("ports") if isinstance(runtime.get("ports"), list) else []
            for p in ports:
                if not isinstance(p, dict):
                    continue
                if int(p.get("privatePort") or 0) == 22 and p.get("isIpPublic") and p.get("ip") and p.get("publicPort"):
                    ssh_ep = (str(p["ip"]), int(p["publicPort"]))
                    break

            if desired in ("EXITED", "TERMINATED", "STOPPED"):
                return PollResult(
                    state="completed",
                    progress_token=f"{desired}:{uptime}:{last_change}",
                    output={"desiredStatus": desired, "uptimeInSeconds": uptime, "lastStatusChange": last_change},
                    uptime_secs=uptime,
                    ssh_endpoint=ssh_ep,
                )
            if runtime and (uptime > 0 or ssh_ep is not None):
                return PollResult(
                    state="running",
                    progress_token=f"running:{uptime}:{last_change}:{ssh_ep}",
                    uptime_secs=uptime,
                    ssh_endpoint=ssh_ep,
                )
            return PollResult(
                state="pending",
                progress_token=f"pending:{desired}:{last_change}",
                uptime_secs=uptime,
                ssh_endpoint=ssh_ep,
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

    def _ssh_base_cmd(self, ssh_endpoint: Tuple[str, int], identity_path: Path) -> List[str]:
        host, port = ssh_endpoint
        return [
            "ssh",
            "-i", str(identity_path),
            "-p", str(port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            f"root@{host}",
        ]

    def _wait_for_sshd(
        self,
        ssh_endpoint: Tuple[str, int],
        identity_path: Path,
        deadline: float,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> bool:
        probe_cmd = self._ssh_base_cmd(ssh_endpoint, identity_path) + ["true"]
        while time.time() < deadline:
            if is_cancelled and is_cancelled():
                return False
            res = subprocess.run(probe_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if res.returncode == 0:
                return True
            time.sleep(3)
        return False

    def run_pod_command(
        self,
        ssh_endpoint: Tuple[str, int],
        identity_path: Path,
        spec: JobSpec,
        walltime_remaining_secs: float,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> ExecResult:
        start = time.time()
        hard_deadline = start + max(10.0, walltime_remaining_secs)
        sshd_deadline = min(hard_deadline, start + 60.0)
        if not self._wait_for_sshd(ssh_endpoint, identity_path, sshd_deadline, is_cancelled=is_cancelled):
            if is_cancelled and is_cancelled():
                return ExecResult(state="cancelled", error="cancelled_during_sshd_wait")
            return ExecResult(state="failed", error="sshd_connect_timeout")

        script_b64 = base64.b64encode(spec.command.encode("utf-8")).decode("ascii")
        remote_cmd = f"echo {script_b64} | base64 -d > /tmp/columbus_run.sh && chmod +x /tmp/columbus_run.sh && bash /tmp/columbus_run.sh"
        full_cmd = self._ssh_base_cmd(ssh_endpoint, identity_path) + [remote_cmd]

        proc = subprocess.Popen(
            full_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert proc.stdout is not None and proc.stderr is not None
        os.set_blocking(proc.stdout.fileno(), False)
        os.set_blocking(proc.stderr.fileno(), False)

        stdout_buf = bytearray()
        stderr_buf = bytearray()
        last_activity = time.time()
        open_fds = {proc.stdout.fileno(): ("stdout", proc.stdout), proc.stderr.fileno(): ("stderr", proc.stderr)}
        abort_state = ""

        try:
            while open_fds:
                now = time.time()
                if is_cancelled and is_cancelled():
                    abort_state = "cancelled"
                    break
                if now >= hard_deadline:
                    abort_state = "timed_out"
                    break
                if (now - last_activity) >= spec.idle_timeout_secs:
                    abort_state = "idle_timeout"
                    break

                ready, _, _ = select.select(list(open_fds.keys()), [], [], 1.0)
                for fd in ready:
                    name, stream = open_fds[fd]
                    chunk = stream.read(65536)
                    if not chunk:
                        open_fds.pop(fd, None)
                        continue
                    last_activity = time.time()
                    buf = stdout_buf if name == "stdout" else stderr_buf
                    if len(buf) < MAX_CAPTURED_LOG_BYTES:
                        buf.extend(chunk[: MAX_CAPTURED_LOG_BYTES - len(buf)])

            if abort_state:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    proc.wait(timeout=5)
                return ExecResult(
                    state=abort_state,
                    exit_code=proc.returncode,
                    stdout=stdout_buf.decode("utf-8", errors="replace"),
                    stderr=stderr_buf.decode("utf-8", errors="replace"),
                    error=abort_state,
                )

            rc = proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait(timeout=5)

        files_out_bytes: Dict[str, bytes] = {}
        if rc == 0 and spec.files_out:
            for remote_path in spec.files_out:
                base_name = validate_artifact_name(Path(remote_path).name)
                cat_cmd = self._ssh_base_cmd(ssh_endpoint, identity_path) + [f"cat -- {shlex.quote(remote_path)}"]
                fproc = subprocess.Popen(cat_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
                assert fproc.stdout is not None
                fbuf = bytearray()
                oversized = False
                try:
                    while True:
                        chunk = fproc.stdout.read(65536)
                        if not chunk:
                            break
                        if len(fbuf) + len(chunk) > MAX_FILE_BYTES:
                            oversized = True
                            try:
                                os.killpg(fproc.pid, signal.SIGKILL)
                            except OSError:
                                pass
                            break
                        fbuf.extend(chunk)
                    frc = fproc.wait(timeout=30)
                finally:
                    if fproc.poll() is None:
                        try:
                            os.killpg(fproc.pid, signal.SIGKILL)
                        except OSError:
                            pass
                        fproc.wait(timeout=5)
                if not oversized and frc == 0:
                    files_out_bytes[base_name] = bytes(fbuf)

        return ExecResult(
            state="completed" if rc == 0 else "failed",
            exit_code=rc,
            stdout=stdout_buf.decode("utf-8", errors="replace"),
            stderr=stderr_buf.decode("utf-8", errors="replace"),
            error="" if rc == 0 else f"exit_code_{rc}",
            files_out_bytes=files_out_bytes,
        )

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
