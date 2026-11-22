#!/usr/bin/env python3
"""Operator CLI for Columbus approvals, budget ledger, status, park, quarantine, and reaper."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from columbus.approval import ApprovalStore
from columbus.controller import BudgetLedger, ColumbusController, DEFAULT_BUDGET_CAP_USD, LaneConfig
from columbus.drivers.runpod import RunPodDriver


def _controller(args: argparse.Namespace) -> ColumbusController:
    state_root = Path(args.state_dir)
    key_file = Path(args.key_file) if getattr(args, "key_file", "") else None
    cfg = LaneConfig(
        lane_id=getattr(args, "lane", "columbus-runpod"),
        state_root=state_root,
        budget_cap_usd=getattr(args, "budget_cap_usd", DEFAULT_BUDGET_CAP_USD),
    )
    return ColumbusController(cfg, RunPodDriver(key_file=key_file))


def main() -> int:
    parser = argparse.ArgumentParser(prog="columbus_ctl", description=__doc__)
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("DIFFUSER_COLUMBUS_STATE_DIR", "/var/lib/diffuser-columbus"),
    )
    parser.add_argument("--lane", default="columbus-runpod")
    parser.add_argument("--key-file", default=os.environ.get("COLUMBUS_RUNPOD_KEY_FILE", ""))
    parser.add_argument("--budget-cap-usd", type=float, default=DEFAULT_BUDGET_CAP_USD)

    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Show live RunPod balance, owned pods, pending approvals, and budget")

    p_tickets = sub.add_parser("tickets", help="List approval tickets")
    p_tickets.add_argument("--status", default=None, help="Filter by status (e.g. pending_approval)")

    p_approve = sub.add_parser("approve", help="Approve a staged Columbus job ticket")
    p_approve.add_argument("ticket_id")
    p_approve.add_argument("--note", default="")

    p_reject = sub.add_parser("reject", help="Reject a staged Columbus job ticket")
    p_reject.add_argument("ticket_id")
    p_reject.add_argument("--reason", default="owner_rejected")

    sub.add_parser("budget", help="Show cumulative USD spend ledger")

    p_reap = sub.add_parser("reap", help="Reconcile orphaned owned RunPod resources")
    p_reap.add_argument("--dry-run", action="store_true")

    p_park = sub.add_parser("park", help="Park lane (block new allocations)")
    p_park.add_argument("--reason", default="operator_parked")

    sub.add_parser("unpark", help="Unpark lane")
    sub.add_parser("clear-quarantine", help="Clear lane quarantine after investigating provider state")

    args = parser.parse_args()
    state_root = Path(args.state_dir)

    if args.cmd == "status":
        out = _controller(args).status()
    elif args.cmd == "tickets":
        out = {"ok": True, "tickets": ApprovalStore(state_root).list_tickets(status_filter=args.status)}
    elif args.cmd == "approve":
        out = {"ok": True, "ticket": ApprovalStore(state_root).approve(args.ticket_id, note=args.note)}
    elif args.cmd == "reject":
        out = {"ok": True, "ticket": ApprovalStore(state_root).reject(args.ticket_id, reason=args.reason)}
    elif args.cmd == "budget":
        out = {"ok": True, "budget": BudgetLedger(state_root, args.budget_cap_usd).snapshot()}
    elif args.cmd == "reap":
        out = _controller(args).reap(dry_run=args.dry_run)
    elif args.cmd == "park":
        out = {"ok": True, **_controller(args).park(reason=args.reason)}
    elif args.cmd == "unpark":
        out = {"ok": True, **_controller(args).unpark()}
    elif args.cmd == "clear-quarantine":
        out = {"ok": True, **_controller(args).clear_quarantine()}
    else:
        raise SystemExit(2)

    print(json.dumps(out, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
