# Security Policy

## Threat Model & Controls

- **No Autonomous Cloud Spend**: Agents cannot provision billable compute without an explicit owner approval bound to the canonical SHA-256 of the job spec.
- **Credential Isolation**: Provider API keys are never stored in the repository, environment configs, or logs. They are read dynamically per request from a `0600` regular file (`O_NOFOLLOW` + `fstat` verified) and redacted from error strings.
- **Ephemeral SSH Identity**: RunPod Pod commands use a throwaway per-job `ed25519` keypair stored in a `0700` temp directory and deleted unconditionally on exit.
- **Owned-Only Cleanup**: Columbus terminates pods only by the provider ID returned by its create call. Reaping a recorded pod also requires its exact recorded name to match the provider inventory. Name prefixes and name matches alone never authorize termination. Serverless recovery cancels only the recorded job on the recorded endpoint, never the endpoint itself. Missing IDs, mismatched names, and ambiguous recovery retain the intent and quarantine the lane for owner investigation. This relies on trusted local intent state and stable provider resource IDs, not unique provider display names.

## Reporting Security Issues

Please report suspected vulnerabilities privately to the repository maintainer before opening a public issue.
