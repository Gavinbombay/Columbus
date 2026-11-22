#!/usr/bin/env python3
"""Diffuser fleet-worker exec adapter entrypoint for Columbus."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

from columbus.controller import ColumbusController, DEFAULT_BUDGET_CAP_USD, LaneConfig
from columbus.drivers.runpod import RunPodDriver
from columbus.secure_io import SecureIOError, load_json_stream


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="columbus_worker", description=__doc__, allow_abbrev=False)
    p.add_argument("--lane", default="columbus-runpod", help="Lane identifier")
    p.add_argument("--mode", choices=("pod", "serverless"), default="pod")
    p.add_argument(
        "--state-dir",
        default=os.environ.get("DIFFUSER_COLUMBUS_STATE_DIR", "/var/lib/diffuser-columbus"),
    )
    p.add_argument("--budget-cap-usd", type=float, default=DEFAULT_BUDGET_CAP_USD)
    p.add_argument("--max-usd-per-job", type=float, default=2.00)
    p.add_argument("--max-walltime", type=int, default=3600)
    p.add_argument("--idle-timeout", type=int, default=600)
    p.add_argument("--approval-ttl", type=int, default=1800)
    p.add_argument("--key-file", default=os.environ.get("COLUMBUS_RUNPOD_KEY_FILE", ""))
    return p


def main() -> int:
    args = build_parser().parse_args()
    cancelled = {"flag": False}

    def _on_signal(_signum, _frame):
        cancelled["flag"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    config = LaneConfig(
        lane_id=args.lane,
        mode=args.mode,
        state_root=Path(args.state_dir),
        budget_cap_usd=args.budget_cap_usd,
        max_usd_per_job=args.max_usd_per_job,
        max_walltime_secs=args.max_walltime,
        default_idle_timeout_secs=args.idle_timeout,
        default_approval_ttl_secs=args.approval_ttl,
    )
    key_path = Path(args.key_file) if args.key_file else None
    driver = RunPodDriver(key_file=key_path)
    controller = ColumbusController(config=config, driver=driver)

    try:
        request = load_json_stream(sys.stdin.buffer)
    except SecureIOError as exc:
        receipt = {"ok": False, "lane_id": config.lane_id, "error": str(exc)}
    except (OSError, ValueError, RecursionError):
        receipt = {"ok": False, "lane_id": config.lane_id, "error": "invalid_json"}
    else:
        receipt = controller.handle(request, is_cancelled=lambda: cancelled["flag"])

    print(json.dumps(receipt, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
