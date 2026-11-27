# Columbus Security Audit

- **Auditor:** Codex Luna (`gpt-6-luna`, `reasoning effort: high`, Cloudflare Security Audit Skill)
- **Reviewed source:** `14081377f3e7c3440010f4ffdd96644fce57cc34` plus on-the-fly audit fix in `columbus/drivers/runpod.py` and regression test in `tests/test_columbus.py`.
- **Scope:** all 12 Python files under `columbus/`, `columbus_worker.py`, `columbus_ctl.py`, and `tests/test_columbus.py`.
- **Execution policy:** no network calls, RunPod API calls, or real API keys were used.

## Result

- **open_confirmed:** 0
- **open_needs_validation:** 0
- **verdict:** CLEAN_PASS

## Fixes Applied & Verified

1. **Unbounded HTTP error body read (`columbus/drivers/runpod.py:96`)**: Fixed on the fly during the Codex Luna high-effort pass; `RunPodDriver._http_json()` now reads at most 301 bytes from `HTTPError` bodies before redaction and formatting, verified by `test_http_error_body_read_is_bounded`.
2. **Prior hardening verified across all 12 scoped files (`4334a6c`)**:
   - Atomic multi-lane USD budget reservations (`BudgetLedger.reserve()` / `release()` / `record_spend()`).
   - Bounded HTTP responses (4 MiB), JSON file reads (2 MiB), and streaming `files_out` SSH reads (`MAX_FILE_BYTES` with immediate `SIGKILL`).
   - Provider-verified owned-only reaper teardown (no unverified intent ID fallback).
   - Race-free `O_NOFOLLOW` + `fstat()` `0600` API key loading and ephemeral SSH key cleanup inside `try/finally`.

## Historical Isolated Sandbox Validation

- Executed in OS-enforced `bwrap` sandbox (`--ro-bind / / --dev /dev --tmpfs /tmp --unshare-net --clearenv --setenv PATH /usr/bin:/bin --die-with-parent`): **`15/15` unit tests passing** with zero warnings and zero network access.
- Static checks: AST parse passed on all 12 Python files; `git diff --check` clean.
- Public hygiene scan (`gen85`): `0` raw hits, `0` unruled hits, `0` namegrep hits.

## Release Candidate Validation (`9dab877`)

- All 32 unit tests passed in an OS-enforced networkless sandbox.
- The suite covers ID-bound pod reaping, exact name verification, preservation of same-name pods, missing or ambiguous IDs, post-termination absence verification, and serverless cancellation by recorded endpoint and job ID.
- This validation supplements the historical audit above; it is not a new full security audit.
