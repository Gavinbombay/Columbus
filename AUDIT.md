# Columbus Security Audit Summary

- **Auditor**: Codex Luna (`gpt-6-luna`, `reasoning effort: high`), Cloudflare Security Audit Skill
- **Initial Audited Ref**: `4f52216` (100% file coverage across `columbus/`, `columbus_worker.py`, `columbus_ctl.py`, `tests/test_columbus.py`)
- **Confirmed Vulnerabilities**: `0`
- **Needs-Validation Items Found & Resolved in `4334a6c`**:
  1. **NV-1 (Multi-lane budget race)**: Added atomic `BudgetLedger.reserve()` and `release()`/`record_spend()` under `flock` so concurrent lanes cannot overcommit the shared USD cap.
  2. **NV-2 (Bounded reads)**: Capped HTTP API reads at 4 MiB, `read_json()` at 2 MiB, and `files_out` SSH reads via streaming chunk loop with immediate `SIGKILL` if `MAX_FILE_BYTES` is exceeded.
  3. **NV-3 (Reaper ownership evidence)**: Removed unverified intent `resource_id` fallback in `reap()`; termination now requires matching provider-side `list_owned()` ownership evidence.
  4. **NV-4 (Race-free key load)**: Hardened `load_api_key()` to open once with `O_NOFOLLOW`, verify regular file and `0600` mode via `os.fstat(fd)`, and read from the same file descriptor.
  5. **Ephemeral SSH key cleanup edge**: Moved ephemeral keypair generation inside the enclosing `try/finally` block.
- **Verification**:
  - Offline test suite: `14/14` passing (`python3 -m unittest discover -s tests -v`).
  - Public hygiene scan (`gen85`): `0` raw hits, `0` unruled hits, `0` namegrep hits.
  - Live RunPod smoke test through Diffuser `:5820`: `exit_code: 0`, `cleanup_verified: true`, `0` leaked pods, `$0.0036` total test spend within `$6.88` cap.
