# Columbus Design

## Overview

Columbus separates **agent compute requests** from **cloud provisioning authority**:

1. **Diffuser Router**: Agents submit standard async fleet jobs (`columbus.runpod.pod.exec/v1` or `columbus.runpod.serverless.exec/v1`).
2. **Admission & Estimation**: `ColumbusController` validates bounds (`max_usd`, `max_walltime_secs`, `startup_timeout_secs`, `idle_timeout_secs`), queries live provider balance and GPU pricing, and checks the shared `BudgetLedger`.
3. **Human Approval Gate (`columbus/approval.py`)**: Writes a `0600` ticket under `<state_dir>/approvals/` bound to `canonical_spec_hash(spec)`. Execution blocks (or returns `pending_approval` in two-phase `stage` + `exec` mode) until the owner runs `columbus_ctl.py approve <ticket_id>`. Approval is verified and consumed atomically under `flock`.
4. **Atomic Budget Reservation & Intent Custody (`columbus/controller.py`)**: Reserves `estimated_max_usd` in `budget_ledger.json` and writes `seat-0.intent.json` (PID + exact owned tag `diffuser-columbus-...`) before any provider `create()` call, then binds the returned resource ID to that intent.
5. **Ephemeral Per-Job SSH Execution (`columbus/drivers/runpod.py`)**: For RunPod Pod commands, generates a single-use `ed25519` keypair in a `0700` temp directory, passes the public key via `PUBLIC_KEY`, waits for `runtime.ports` TCP/22 with a dedicated `startup_timeout_secs`, runs the base64-encoded script over SSH while enforcing `idle_timeout_secs` and `max_walltime_secs`, streams bounded `files_out` artifacts (`0600`, SHA-256), and removes the keypair in `finally`.
6. **Owned-Only Teardown & Reaper**: Terminates only the recorded provider resource ID, verifies pod absence by exact ID, reconciles actual USD spend in `BudgetLedger`, and quarantines the lane on ambiguous cleanup. `columbus_ctl.py reap` reconciles dead-PID intents by recorded ID and exact pod name, or recorded serverless endpoint and job ID. An intent without a recorded ID is retained for manual owner investigation, never resolved by deleting name matches.
