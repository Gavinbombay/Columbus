"""Mandatory human owner approval gate bound to canonical job spec SHA-256."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from columbus.secure_io import atomic_write_json, file_lock, make_private_dir, read_json

DEFAULT_APPROVAL_TTL_SECS = 1800  # 30 minutes


class ApprovalError(Exception):
    """Raised when human approval is missing, expired, rejected, or tampered."""


def canonical_spec_hash(spec_dict: Dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 digest of a job spec."""
    encoded = json.dumps(spec_dict, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ApprovalStore:
    """Durable, locked human-approval ticket store under <state_dir>/approvals/."""

    def __init__(self, state_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.state_dir = state_dir
        self.approvals_dir = state_dir / "approvals"
        self.lock_path = self.approvals_dir / ".approvals.lock"
        self.clock = clock

    def _ticket_path(self, ticket_id: str) -> Path:
        if not ticket_id or not all(c.isalnum() or c in "-_" for c in ticket_id):
            raise ApprovalError("invalid_ticket_id")
        return self.approvals_dir / f"{ticket_id}.json"

    def stage_ticket(
        self,
        spec_dict: Dict[str, Any],
        estimate_dict: Dict[str, Any],
        requesting_agent: str = "agent",
        approval_ttl_secs: int = DEFAULT_APPROVAL_TTL_SECS,
        ticket_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a pending approval ticket bound to canonical_spec_hash(spec_dict)."""
        if approval_ttl_secs < 10 or approval_ttl_secs > 86400:
            raise ApprovalError("invalid_approval_ttl")
        now = self.clock()
        tid = ticket_id or f"apr-{int(now)}-{secrets.token_hex(4)}"
        spec_sha = canonical_spec_hash(spec_dict)
        ticket = {
            "ticket_id": tid,
            "status": "pending_approval",
            "spec_sha256": spec_sha,
            "spec": spec_dict,
            "estimate": estimate_dict,
            "requesting_agent": str(requesting_agent)[:128],
            "created_at": now,
            "expires_at": now + approval_ttl_secs,
            "decided_at": None,
            "decided_by": None,
            "decision_note": "",
            "approved_spec_sha256": None,
            "consumed_at": None,
        }
        make_private_dir(self.approvals_dir)
        with file_lock(self.lock_path, blocking=True):
            path = self._ticket_path(tid)
            if path.exists():
                raise ApprovalError("ticket_already_exists")
            atomic_write_json(path, ticket)
        return ticket

    def get_ticket(self, ticket_id: str) -> Dict[str, Any]:
        path = self._ticket_path(ticket_id)
        if not path.exists():
            raise ApprovalError("ticket_not_found")
        ticket = read_json(path)
        if ticket.get("status") == "pending_approval" and self.clock() > float(ticket.get("expires_at", 0)):
            ticket["status"] = "expired"
        return ticket

    def list_tickets(self, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.approvals_dir.exists():
            return []
        now = self.clock()
        out: List[Dict[str, Any]] = []
        for path in sorted(self.approvals_dir.glob("*.json")):
            try:
                ticket = read_json(path)
            except Exception:
                continue
            if ticket.get("status") == "pending_approval" and now > float(ticket.get("expires_at", 0)):
                ticket["status"] = "expired"
            if status_filter and ticket.get("status") != status_filter:
                continue
            out.append(ticket)
        return out

    def approve(self, ticket_id: str, approved_by: str = "owner", note: str = "") -> Dict[str, Any]:
        make_private_dir(self.approvals_dir)
        with file_lock(self.lock_path, blocking=True):
            path = self._ticket_path(ticket_id)
            if not path.exists():
                raise ApprovalError("ticket_not_found")
            ticket = read_json(path)
            now = self.clock()
            if ticket.get("status") != "pending_approval":
                raise ApprovalError(f"ticket_not_pending:{ticket.get("status")}")
            if now > float(ticket.get("expires_at", 0)):
                ticket["status"] = "expired"
                atomic_write_json(path, ticket)
                raise ApprovalError("ticket_expired")
            ticket["status"] = "approved"
            ticket["decided_at"] = now
            ticket["decided_by"] = approved_by
            ticket["decision_note"] = note[:256]
            ticket["approved_spec_sha256"] = ticket["spec_sha256"]
            atomic_write_json(path, ticket)
            return ticket

    def reject(self, ticket_id: str, rejected_by: str = "owner", reason: str = "") -> Dict[str, Any]:
        make_private_dir(self.approvals_dir)
        with file_lock(self.lock_path, blocking=True):
            path = self._ticket_path(ticket_id)
            if not path.exists():
                raise ApprovalError("ticket_not_found")
            ticket = read_json(path)
            if ticket.get("status") != "pending_approval":
                raise ApprovalError(f"ticket_not_pending:{ticket.get("status")}")
            ticket["status"] = "rejected"
            ticket["decided_at"] = self.clock()
            ticket["decided_by"] = rejected_by
            ticket["decision_note"] = reason[:256]
            atomic_write_json(path, ticket)
            return ticket

    def verify_and_consume(self, ticket_id: str, spec_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Fail-closed verification immediately prior to provider resource creation."""
        expected_sha = canonical_spec_hash(spec_dict)
        make_private_dir(self.approvals_dir)
        with file_lock(self.lock_path, blocking=True):
            path = self._ticket_path(ticket_id)
            if not path.exists():
                raise ApprovalError("ticket_not_found")
            ticket = read_json(path)
            now = self.clock()
            status = ticket.get("status")
            if status != "approved":
                if status == "pending_approval" and now > float(ticket.get("expires_at", 0)):
                    ticket["status"] = "expired"
                    atomic_write_json(path, ticket)
                    raise ApprovalError("approval_expired")
                raise ApprovalError(f"approval_not_granted:{status}")
            if ticket.get("consumed_at") is not None:
                raise ApprovalError("approval_already_consumed")
            if now > float(ticket.get("expires_at", 0)):
                raise ApprovalError("approval_expired")
            if ticket.get("approved_spec_sha256") != expected_sha or ticket.get("spec_sha256") != expected_sha:
                raise ApprovalError("approval_spec_hash_mismatch")
            ticket["consumed_at"] = now
            atomic_write_json(path, ticket)
            return ticket

    def wait_for_approval(
        self,
        ticket_id: str,
        spec_dict: Dict[str, Any],
        poll_interval_secs: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Block until the owner approves, rejects, or the ticket expires."""
        while True:
            if is_cancelled and is_cancelled():
                raise ApprovalError("approval_cancelled")
            ticket = self.get_ticket(ticket_id)
            status = ticket.get("status")
            if status == "approved":
                return self.verify_and_consume(ticket_id, spec_dict)
            if status == "rejected":
                raise ApprovalError("approval_rejected")
            if status == "expired":
                raise ApprovalError("approval_expired")
            if status != "pending_approval":
                raise ApprovalError(f"approval_invalid_state:{status}")
            sleeper(poll_interval_secs)
