# Native usage receipts

Use only in an explicitly authorized, enabled AGWF run. A current explicit skill invocation requires startup for that scoped run; when native goal creation is forbidden, use manual monitoring as described in [operations](operations.md). Updating this package, discussing the skill or publishing usage data does not by itself re-enable a disabled workflow.

The main agent calls `mcp__codex_app__get_usage_limits` through the existing Codex connection. Python does not open a second app-server, select another CLI, read authentication files, attach to private transports or make model requests. The background worker watches supplied receipts and enforces freshness; the host tool still needs main-agent calls at work boundaries.

Create a private input JSON from an actual tool result:

```json
{
  "source": "mcp__codex_app__get_usage_limits",
  "sourceId": "actual-tool-call-receipt-id",
  "observedAt": 1800000000,
  "result": {
    "rateLimits": {
      "limitId": "codex",
      "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1800010000},
      "secondary": {"usedPercent": 30, "windowDurationMins": 10080, "resetsAt": 1800600000}
    }
  }
}
```

These numbers are examples. Preserve the actual tool observation time, receipt identity and values; do not fabricate them or replace the observation timestamp with file publication time. Prefer `rateLimitsByLimitId.codex` when the actual result supplies multiple buckets. A present multi-bucket map without Codex is refused rather than silently using a different bucket. Both duration windows and reset times are required. Copy only the needed rate fields into this private input; account/profile/credit fields are not needed. The published `watcher/usage-source.json` retains only approved window fields, observation/source identity and its bound run/epoch.

`workflow activate --input AUTH.json --usage-input USAGE.json` and `bind-active-goal` validate authority before any worker launch. `resume` also needs a new receipt supplied with `--usage-input`; an old epoch's receipt cannot be reused. Manual `watcher start --usage-input USAGE.json` follows the same data preflight. A missing or already expired receipt refuses the start before a process is created.

To publish a fresh reading during work, use `watcher ingest --usage-input USAGE.json` or the `--usage-input` option on a workflow/coordination command. `ingest` does not activate a workflow, start a worker, renew authorization or clear a SAVE latch. A new source file triggers evaluation without waiting for the old deadline. Status can briefly show the previous evaluation while the owned worker consumes it; wait for a fresh healthy status before consequential dispatch.

Main refreshes the actual tool reading before the adaptive deadline (maximum 300 seconds, shorter near thresholds or rapid drain). The worker never presents a re-read old file as a new measurement. If main cannot obtain fresh data, it retries validation once and latches SAVE_AND_STOP. A recorded `usage_receipt_expired` can then recover under the same already authorized active goal with `workflow resume` and `usage_recovery: true`; see [technical recovery](operations.md#expired-telemetry-recovery-under-existing-authority). Fresh data alone does not clear a latch, but this checked recovery does not require another user message. Real quota stops and explicit user stops retain their continuation requirements. Refresh at work boundaries with enough time for the next tool call; the deadline is not a recommended wait duration. Long uninterruptible calls remain a limit of host-tool refresh; this design does not claim unattended background access to the native tool.

Malformed input, wrong run/epoch, future/expired observations, backwards timestamps and changed replays are refused. Identical receipt publication is an idempotent no-op. Timestamp/source claims remain caller-supplied evidence, as with other native receipts; JSON shape alone is not provider authentication. Store raw evidence privately and never treat an arbitrary source label as independent proof.

Measurement failures expose an allowlisted `diagnosticCode` and error class in the sample/status and retain at most 20 structured entries in `watcher/diagnostics.json`. Exception text, authentication data and arbitrary tool bodies are excluded. Unexpected Python worker failures still have local `worker-stderr.log`; there is no discarded child-server stderr because the child server no longer exists.

Full package integrity checks run on each candidate command and each worker evaluation, including newly published input. Heartbeats only validate run/epoch control and publish health. They do not duplicate full package traversal/hashing. A changed dependency is refused at the next measurement boundary or command, not promised to be detected by every heartbeat.

Unit tests cover real receipt file publication/reading, strict JSON, expiry/replay/identity handling, thresholds, bounded diagnostics and the absence of process creation on the measurement path. They do not stand in for live Remote product acceptance. Keep the installed workflow disabled until the user explicitly re-enables it; maintenance must not launch live AGWF workers to manufacture that acceptance.
