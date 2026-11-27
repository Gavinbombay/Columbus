# Columbus

Approval-gated, budget-bounded on-demand GPU and compute controller for **Diffuser**.

Columbus runs as a standalone external fleet-worker adapter for [Diffuser](https://github.com/Gavinbombay/Diffuser), allowing agents to request elastic cloud GPU pods or serverless jobs through the same standard Diffuser `/v1/fleet/jobs` API they use for local GPUs and Colab lanes.

## Key Properties

- **Mandatory Human Approval Gate**: Every billable job stages a `pending_approval` ticket bound to the canonical SHA-256 of the job spec. No cloud pod or serverless job starts until the owner explicitly runs `columbus_ctl.py approve <ticket_id>`. Tickets are single-use and expire cleanly if unapproved.
- **Pluggable Provider Architecture**: Clean `ProviderDriver` interface with a stdlib-only **RunPod** driver (`Pods` via GraphQL + ephemeral per-job `ed25519` SSH execution, plus `Serverless` via REST v2). Zero external Python package dependencies.
- **Hard Spend & Custody Guardrails**:
  - Cumulative USD budget ledger with atomic multi-lane reservations (`flock` + `0600` atomic writes).
  - Live account balance floor and per-job `max_usd` ceiling.
  - Dynamic per-job API key loading (`O_NOFOLLOW` + `fstat` `0600` check) so API keys rotate without restarting the worker.
  - 1x backoff retry on `$0` transient allocation failures after checking unchanged balance and absence of matching pod names; name matches never authorize cleanup.
  - Resource tagging (`diffuser-columbus-<lane>-<nonce>-g<n>`), ID-bound teardown and absence verification, lane quarantine on ambiguous cleanup, and an automated orphan reaper (`columbus_ctl.py reap`).
  - Optional RunPod Network Volume attachment and `HF_HUB_DISABLE_XET=1` default for large model weights.

## Quick Start

```sh
# 1. Store your RunPod API key outside the repository (0600)
install -d -m 700 ~/.config/columbus
umask 077 && printf "%s\n" "<YOUR_RUNPOD_API_KEY>" > ~/.config/columbus/runpod_api_key

# 2. Run the 32 offline deterministic unit tests (no network calls)
python3 -m unittest discover -s tests -v

# 3. Check live RunPod balance, owned pods, and budget ledger (read-only)
./columbus_ctl.py status

# 4. Approve a staged agent job ticket
./columbus_ctl.py tickets --status pending_approval
./columbus_ctl.py approve <ticket_id>
```

## Diffuser Fleet Enrollment

Add `columbus_worker.py` as an `exec` profile in your Diffuser fleet-worker config advertising `columbus.runpod.pod.exec/v1` and/or `columbus.runpod.serverless.exec/v1`, then submit standard `"async": true` jobs to Diffuser with `action: "status"`, `"estimate"`, `"stage"`, or `"exec"`.

If creation is interrupted before a provider ID is recorded, the reaper retains the intent and quarantines the lane. The owner must independently establish custody and reconcile provider state before resolving the intent. A matching pod name is not sufficient evidence.

See [DESIGN.md](DESIGN.md), [AUDIT.md](AUDIT.md), and [docs/SECURITY.md](docs/SECURITY.md) for architecture and security details.
