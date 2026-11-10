# Scoped native usage measurement

N08 records public metadata mechanically. It neither calls a model nor assigns
quota consumption or prices. Producer tests and metadata reconstruction require
independent consequential review before acceptance. Fresh versus long context,
cache behavior, matched economic trials and installed adoption are separate gates.

Run `python -X utf8 -B tools/extract-usage.py --manifest MANIFEST --output NEW_JSON` from the skill directory in either the source checkout or installation. The tool and `tools/telemetry.schema.json` are pinned in the skill manifest.
The API is `extract(manifest)` and `publish(document, new_path, canaries=())`.
Outputs are canonical UTF-8 JSON with sorted keys, compact separators and one LF.
Existing output files are never overwritten. `--canary-file` reads a private JSON
array of exact strings; it is never copied to evidence. Every exported string and
key is checked before opening the output. Errors contain fixed reason codes only.

The manifest has exactly `runs`, `comparison_pairs`, `checks`, `evidence_pins`.
Each run has `run_id`, `sources`, `phases`, `coverage_gaps`. Each source declares
`source_id`, `path`, `thread_id`, `session_id`, `task_id`, `agent_path`, inclusive
`ranges`, `requested_model`, `requested_effort`, `main_client_selected`, with
optional `prefix_bytes`, `sha256`. A missing pin is captured at open; supplied pins
must match. Use supplied byte limits and SHA-256 for reproducibility after append.
Only complete JSONL prefixes are accepted. Strict JSON rejects duplicate keys and
nonfinite numbers. Ordered disjoint ranges, explicit source/header identity and
monotonic response chronology are required. Missing ranges reject the manifest;
range gaps or restarts null token sums and retain a reason.

`token_usage_record.usage` is authoritative per-response metadata. Response IDs
are counted once, with thread/session/turn/source scope. Conflicting duplicates,
foreign scopes, missing fields and invalid subset totals yield null with reasons.
An old response reused across a selected boundary cannot become a new completion.
Malformed same-ID observations elsewhere in the pinned prefix also invalidate
selected reuse, including malformed earlier records. Every selected timestamp,
including same-ID duplicates, advances a chronology watermark; deduplication
does not remove an observation from chronology checks.
Tokens must be nonnegative integers, excluding booleans. `total_tokens` equals
input plus output. Cached input and cache-write input are subsets of input;
reasoning output is a subset of output. Raw input and output are never augmented
by their subsets. Cache-excluded input equals input minus cached input; this is a
token count, not billing. Missing cache cause stays unknown.

`event_msg.info.total_token_usage` and
`token_usage_record.thread_token_usage` are independent cumulative streams.
Both remain diagnostics with null deltas, explicit baseline/reset/missing reasons,
and no fallback attribution. Their values are never merged or cross-subtracted.
Reset diagnostics do not invalidate complete independent per-response records.
Response counts measure observed completion-accounting IDs, not billed requests.

Private transcript, reasoning, tool input/output bodies, account/profile/config
fields and opaque messages are excluded by construction. Local private tool values
are read only to calculate representation lengths and identify native boundaries;
they are not exported, classified as hidden reasoning, or decrypted. Tool outputs
need unique call IDs with matching in-range calls and valid chronology. Strings
use their raw UTF-8 length; structured values use insertion-order compact JSON
UTF-8 with `ensure_ascii=False`. These lengths match accepted N05 representation
semantics and are neither serialized enclosing JSONL, wire nor billing bytes.

Requested model/effort come from exact native request receipts. Main client
selection is human-controlled, so requested main fields remain null. Turn-context
model/effort are observed settings with provenance limits, not provider effective
model attestation. Missing observed fields, effective models, costs and currency
remain null with reasons. No tariff, Standard proxy, cache-miss explanation or
five-hour usage-per-operation inference is introduced. Comparison preserves elapsed
and raw-token changes; actual cost comparison remains unknown. The synthetic
cost counterexample uses explicit comparable currency solely to prove a faster,
more costly alternative stays visible; it is not actual provider billing evidence.

Phases are `implementation`, `repair`, `independent_review`, `external_wait`,
`closure`, each with task/source, captured start/end and an authority identifier.
Capture gaps give null durations. Lifecycle windows include waits and polling and
do not prove active compute time: `agent_active_seconds_sum` remains null.
`observed_agent_interval_seconds_sum` sums unions separately for each agent;
`observed_wall_union_seconds` unions intervals across agents. External wait is
excluded from agent interval sums. These quantities are different when intervals
overlap. `coverage_complete` requires no declared gaps, token scope failures,
tool coverage failures or phase capture/type gaps. `complete_wall_union_seconds`
remains null whenever that coverage is incomplete, with `run_coverage_incomplete`
in phase reasons. Both remain independent of valid bounded token sums and
observed wall/lifecycle unions. Phase task/source joins must match the source.
Known phase endpoints must be valid timestamps even when the other endpoint is
missing; missing capture still gives a null duration.
The observed union is not the elapsed duration of an unobserved complete project.

For a forthcoming same-job native fresh compact versus public long-history pair,
root builds a manifest with two distinct run IDs, each arm's main marker range and
exact child-header sources, captured implementation/repair/review/wait/closure
intervals, then `comparison_pairs: [["fresh_compact", "public_long_history"]]`.
Use `prefix_bytes` and `sha256` from the completed native captures; receipt fields
supply requested model/effort. Native per-response records supply observed raw,
cached and cache-excluded input and output. Each source may include an entire
completed child prefix using `ranges: [[1, RECORD_COUNT]]`. Main marker ranges
must include both emitting calls and outputs to measure tool bytes. Missing
closure/phase captures go into `coverage_gaps` and null-ended phase intervals,
never estimates. `extract(manifest)` produces joined per-arm metadata and token
differences; `publish` seals it after closed-schema, semantic and configured-canary
checks. The semantic validator recomputes unique response sums, subset and raw
arithmetic, source/run counts, tool byte sums and mapping, source/range joins,
diagnostic-only streams, phase durations/unions, coverage/null/reason relationships
and comparisons. A structurally valid modified document cannot bypass these
relationships. Validation completes before output creation. It authenticates
internal relationships; it does not attest original metadata provenance.
The pair's job-equivalence, context treatment and native cache interpretation
still require its own root-owned protocol and independent review. The extractor
does not create the native reader sessions or declare the experiment accepted.

`telemetry.schema.json` closes every public object. The dependency-free validator
implements its stated standard JSON Schema subset and rejects unknown keywords.
Normal and optimized tests exercise actual extraction/publication paths; they do
not use assertions as production validation. Actual evidence pins identify exact
scanned prefixes, not a mutable whole-file claim. Metadata pins authenticate bytes,
not provider billing, receipt authenticity or effective model claims.

Historical evaluations are not a dependency of this tool. Each new measurement
must supply its own pinned manifest and receive independent acceptance.
