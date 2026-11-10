# Command contract (A02)

The stdlib helper validates commands passing through `Workflow.execute` and `Workflow.apply`. It does not intercept native tools, authenticate tool receipts or user messages, stop processes, or independently observe effective models. Main still performs those operations and supplies evidence. A26 authorization and checkpoint/ownership/capacity guards remain in force.

State uses integer `schema_version: 2`. Unversioned, legacy, future and malformed state is rejected before mutation. There is no legacy migration/import utility. Start a new AGWF run with current typed authorization. Checkpoint snapshot and restore remain supported; historical evidence never authorizes work. Fault-injection tests use disposable fixture copies only.

## JSON and error policy

State files, `--input` files, stdin, checkpoint metadata and installer ownership JSON use the same strict parser. Duplicate keys are rejected at any depth, even when both values agree. `NaN`, `Infinity`, `-Infinity` and finite-looking literals that overflow to infinity (for example `1e999`) are rejected. Input files accept UTF-8 with an optional BOM. JSON root values must be objects for commands. Empty stdin supplies `{}`; it succeeds only where the command contract permits it. `write_json` validates and serializes with `allow_nan=False` before creating a directory or temporary file.

Runtime payloads are JSON objects with string keys. Lists remain lists; integers and floats are not booleans. Every present flag must have exact Python `bool` type (`true`/`false` in JSON). Strings, 0/1, null, lists and objects never stand in for flags. Required flags reject omission. Optional flags default to false only when absent; null is invalid. Numeric values are finite; count fields require integers >=1, timestamps accept integers/floats in [0, sys.float_info.max], and percentages are in [0,100]. The timestamp upper bound rejects enormous integers that would overflow clock arithmetic. Timestamps must also satisfy the command's freshness guards. There is no new maximum capacity: dispatch retains the existing main-inclusive slot accounting and child cap.

Payload validation runs before lock/directory creation and before command execution. `apply` works on a private JSON copy and promotes it only after its guards pass. Rejected commands preserve existing state bytes (and SHA-256), except the explicit durable INPUT_INVALIDATED receipt and contract_invalid flags described below; a rejected initial command does not create state. Failed snapshots may still leave an unpromoted owned checkpoint directory as before. Valid-but-unavailable usage is an intentional state transition, described below.

Errors from the helper exit with status 1 and emit this stderr object:

```json
{"error":"exact boolean required","code":"FIELD_TYPE","field":"$.accepted"}
```

| Code | Meaning | Field |
| --- | --- | --- |
| `COMMAND_UNKNOWN` | Unknown engine command | `$.command` |
| `FIELD_REQUIRED` | Missing required payload field or required utility option | Exact missing field |
| `FIELD_UNKNOWN` | Unknown payload field or command-inapplicable CLI option | Exact offending field |
| `FIELD_TYPE` | Wrong caller type | Exact field/list index |
| `FIELD_VALUE` | Invalid identifier, empty required text/list, numeric range or inventory status | Exact field/list index |
| `JSON_DUPLICATE_KEY` | Duplicate member in parsed JSON | Exact duplicate member, including nesting |
| `JSON_NONFINITE` | Nonfinite number/token anywhere, including raw metadata | Exact field/list index |
| `JSON_SYNTAX` | Invalid JSON syntax/encoding | `$` |
| `JSON_TYPE` | Non-JSON runtime value or non-string object key | Exact value/object |
| `COMMAND_REFERENCE` | Referenced task or required stored field does not exist | `$.id` (lookup context; error text names the missing key) |
| `COMMAND_CONSTRAINT` | Existing authorization/state/ownership/review/freshness guard | Relevant command field where available; `$.command` for a state-wide guard |
| `STATE_VERSION` / `STATE_TYPE` | Unsupported state version / non-object state root | `$.schema_version` / `$` |
| `IO_ERROR` / `INVALID_VALUE` | Filesystem failure / other value failure | `$` |

Paths use dot notation for simple keys, bracket notation for other keys and `[index]` for lists. Ordinary argparse errors (unknown CLI switch, unknown command, missing switch argument) retain argparse's stderr and status 2. Command-inapplicable recognized options have the structured `FIELD_UNKNOWN` error above. Neither mode uses `assert`, so validation also runs with `python -O`.

## Payload types

`text` means a nonempty string after trimming for validation; original content is preserved. `string` can be empty. `task_id` matches `[a-z][a-z0-9_]{0,63}`. `strings` is a list of nonempty strings (empty list allowed); `criteria` additionally requires at least one item. `task_ids` is a list of task IDs. `timestamp`, `count`, `percent` follow the numeric rules above. `?` allows explicit null; no other type allows null.

Every command and every structured nested object below rejects fields outside its required/optional catalogue. Two exceptions preserve raw provider output: receipt contents and non-measurement metadata in raw `rateLimits`/windows are opaque JSON. They still reject duplicate keys and nonfinite/non-JSON values. Unknown metadata is not interpreted as a command flag or evidence of a valid measurement.

| Nested type | Required fields | Optional / constraints |
| --- | --- | --- |
| `authorization` | `event_id`, `exact_quote`, `kind`, `source_kind`, `source_id`, `goal_id`, `thread_id`, `run_id`: text; `scope`: scope; `observed_at`, `source_created_at`: timestamp; `original_turn_id`, `observed_turn_id`: text | No other fields. A26 allowed kinds, direct-user source, scope equality, replay and age guards apply. |
| `scope` | `kind`, `id`, `objective`: text | `kind` is objective/report under A26 guards. |
| `source` | `source_kind`, `source_id`, `exact_quote`, `original_turn_id`: text; `source_created_at`: timestamp | Must equal the supplied authorization source projection. |
| `native_binding` | `binding_id`, `goal_id`, `thread_id`, `run_id`, `objective`: text; `scope`: scope; `goal_created_at`, `observed_at`: timestamp | N02 initial binding only. Scope must be objective and exactly equal objective; original goal creation must precede observation. No human-source fields. |
| `app_goal` | `goal_id`, `thread_id`, `status`, `source_kind`, `source_id`: text; `observed_at`: timestamp | Optional `objective`: text and `goal_created_at`: timestamp; both required and equal to the binding for native basis, including every refresh. A26 requires get_goal provenance, matching identity, age <=60 and appropriate active status. |
| `inventory` | List of objects with `id`, `status`: text | No additional item fields. Unique IDs; statuses running/interrupting/unknown/idle/stopped/done; observation age 0..60. |
| `models` | Object mapping nonempty model names to `strings` effort lists | Empty object/list allowed; prepare still requires exact observed model/effort. |
| `arguments` | No structural required fields | Only `task_name`: task_id; `fork_turns`: text; `message`: string; `model`, `reasoning_effort`: text. Record additionally requires complete equality to prepared arguments. Missing fields therefore fail that guard; they cannot dispatch. |
| `receipt` | Nonempty object, list or string | Entire contents are opaque JSON from the actual tool result; no authenticity claim. |
| `rate_limits?` | Object or null | Optional primary/secondary: window?; additional raw provider metadata is opaque JSON. |
| `window?` | Object or null | Optional `usedPercent`: percent?; `windowDurationMins`: count?; `resetsAt`: timestamp?; other metadata is opaque JSON. |

## Engine commands

Required/optional lists are exhaustive. Where no optional field is listed, the payload has exactly its required fields. Empty commands take `{}`.

| Command | Required payload fields | Optional payload fields / defaults |
| --- | --- | --- |
| `activate` | `objective`: text; `authorization`: authorization; `interpretation`: string; `source_message`: source; `app_goal_observation`: app_goal; `models`: models; `slot_limit`: count; `inventory`: inventory; `observed_at`: timestamp | `session_limit`: count? (absent/null means no extra open-session cap) |
| `bind-active-goal` | `objective`: text; `native_binding`: native_binding; `interpretation`, `procedural_check`: text; `app_goal_observation`: app_goal; `models`: models; `slot_limit`: count; `inventory`: inventory; `observed_at`: timestamp | `session_limit`: count?; only exact pristine schema2 inactive state with empty tasks and no other history/fields. |
| `observe-authorization` | `authorization`: authorization; `interpretation`: string; `source_message`: source | `app_goal_observation`: app_goal (accepted projection; this command does not refresh stored app observation) |
| `app-goal` | `app_goal_observation`: app_goal | None |
| `usage` | None | `observed_at`: timestamp; `rateLimits`: rate_limits?; omission denotes unavailable measurement |
| `usage-due` | None | `substantial_work`: bool, false |
| `inventory` | `inventory`: inventory; `observed_at`: timestamp | None |
| `task` | `id`: task_id; `role`: text; `objective`: text; `criteria`: criteria | `file_inputs`: file_inputs, []; `context_refs`: strings, []; `review_targets`: review_targets, []; `revision`: text, generated UUID when absent; `writes`: strings, []; `depends_on`: task_ids, []; `independent_review`: bool, false |
| `prepare` | `id`: task_id | `effort`: text; absent uses role default; `current_context`: context_pin for the N06 protocol |
| `record` | `id`: task_id; `submitted_arguments`: arguments; `session_id`: text; `receipt`: receipt | `effective_model`, `effective_effort`: text?; absent/null means unverified |
| `dispatch_failed` | `id`: task_id; `evidence`: text | `certain_not_started`: bool, false; omission/false means uncertain, never certain |
| `reconcile` | `id`: task_id; `evidence`: text; `no_owned_work_running`: bool | Requires true and fresh inventory with no active children |
| `finish` | `id`: task_id; `accepted`: bool; `evidence`: text | `review_task`: task_id, required by the guard when accepting a task needing independent review |
| `cancel` | `id`: task_id; `reason`: text | Only undispatched ready task |
| `stop` | None | None |
| `snapshot` | `writers_quiescent`: bool; `next_action`: text | `files`: strings, []; requires true |
| `restore` | `destination`: text | Must be a new destination outside project |
| `halt` | `inventory`: inventory; `observed_at`: timestamp; `owned_processes_stopped`: bool; `stop_evidence`: text | `degraded`: bool, false; `reason`, `next_action`: text, both required when degraded is true; process flag requires true |
| `resume` | `authorization`: authorization; `interpretation`: string; `source_message`: source; `app_goal_observation`: app_goal | None; matching post-halt continuation, fresh usage/inventory and current verified checkpoint required |
| `compact` | None | None; accepted/rejected terminal tasks, no active children and current verified checkpoint required |
| `complete` | None | None; compact's guards plus active run required |
| `status` | None | None; existing activated state required |

All path claims remain exact project-relative files under the existing Windows alias, boundary and symlink protections. Role names and effort strings retain the existing role catalogue and availability checks. Empty writes support read-only verifier/coordinator roles. Unknown task references fail; no implicit task creation occurs. `accepted: false` rejects quiescent work and `accepted: true` accepts it only after applicable review guards pass.

## Raw usage lifecycle

N06 `context_pin` is exactly `{path, sha256, manifest_path, manifest_sha256}` with nonempty project-relative paths and lowercase SHA-256 values. The manifest must be an existing immutable task file input. `prepare` verifies a fresh version1 rendering against current state and that separately pinned manifest. A summary-assisted prepared message binds this protocol; `dispatch-start` additionally accepts `current_context` and requires a newly rendered summary against the post-prepare state and the original manifest. All native guards remain active. See [context](context.md) for the renderer, evidence blockers, producer gate and limits.

N07 `status` retains its original task-state map and adds `task_observations: {contract_version: 1, tasks: ...}`. Native execution and artifact acceptance are distinct; done remains pending until finish guards pass. Independent finish records the existing optional `review` task link and requires both nonempty distinct native session identities. Independence also requires matching assigned and recorded review output hashes. Schema2 is unchanged. A review cannot depend directly or transitively on acceptance of its own pinned producer. Historical accepted independent work without complete reviewer/producer identities or review pins is exposed as `historical_unrecorded`; it cannot satisfy a fresh acceptance dependency at prepare, record or dispatch-start. The summary blocker and guarded helper use the same independence predicate; no automatic historical migration occurs.

Malformed caller types/ranges are errors and leave state bytes unchanged: for example `usedPercent: "10"`, boolean counts/timestamps, percentage 101, negative reset time, a list window or nonfinite metadata. Missing/null `rateLimits`, missing/null windows or measurement values, a well-typed reading missing either 300/10080-minute window, or an otherwise valid stale/future observation is unavailable usage. Such a valid `usage` command intentionally clears old usage and increments `usage_errors`: first `RETRY_USAGE_ONCE`, second `SAVE_AND_STOP` with stopping state unless already halted/complete. It never silently revives a halted run. Percentages 0 and 100 and finite fractional percentages are valid measurements; 100 triggers threshold recovery. Malformed usage does not increment the unknown-reading counter. Raw metadata fields are preserved; they cannot substitute for required valid windows.

## Supported pinned entry

All operations use `scripts/candidate.py --manifest <manifest> --manifest-sha256 <accepted-pin> --version 1.1`. Run modes additionally require `--project <absolute-project>` and `--run-id <fresh-run>`.

| Mode | Input | Behavior |
| --- | --- | --- |
| `workflow validate` | No input | Verify the accepted package and role policy; bind an inactive namespace |
| `workflow <typed-command>` | `--input <json-file>` or stdin | Execute the typed contracts documented above |
| `coordination` | `--input <json-file>` | Execute a bounded model-free batch; no automatic native dispatch |
| `watcher` | `start`, `renew`, `stop`, `status`, `once` | Use the private pinned sampler and owned run state |
| `install` | Explicit `--destination .../agent-workflow-v1-0`; optional `--update` | Verify payload and owned-installation guards |


Direct `workflow.py` CLI invocation is refused. It has no installer, export or native role-generation entry. Build distribution files with `tooling/build-candidate.py`; use only the pinned candidate installer for Codex installation. Historical schema conversion is not a candidate CLI command and must never bootstrap a retired installation.

## Input and state boundary

Correct boolean command formats retain their decisions in disposable replay. Optional safe false defaults remain available; malformed truthy values are rejected. Strict state parsing protects JSON syntax and finite values. String authorization and legacy state are rejected. Tests use disposable projects only.

## Typed assignment and source chronology (A04 / GI01)

N02 `bind-active-goal` attaches a current active native goal after a long initialization without fabricating or retimestamping a human source. It accepts only a missing state or exactly `{schema_version: 2, status: inactive, tasks: {}}` (JSON strings quoted normally). A migrated, completed, stopped, halted, previously operated or otherwise nonpristine helper cannot bind. The initial observation is real `get_goal` evidence with age 0..60 seconds; its goal/thread/objective/original creation and observation time must match the binding. Native goal creation may be arbitrarily old but cannot follow observation. Command objective and objective scope are equal; report scope and expanded scope reject. Separate nonempty interpretation and procedural-check evidence record the main's current-context and newer-restriction check; the helper cannot authenticate that check.

State stores `native_binding` as the current basis, with `native_binding_events` and `native_binding_ids` distinct from human authorization ledgers. Each event contains `binding`, `interpretation`, `procedural_check` and the original `app_goal_observation`. Binding ID is reserved against later human event IDs; run/scope IDs are reserved with existing history. The bound basis requires fresh objective/creation-matching app observations for usable work. Human authorization retains its exact source schema and 300-second original-source rule. Human resume replaces the current native basis while retaining its distinct native history; later human activation also retains that history. Malformed, missing, duplicate or contradictory native records/reservations fail closed, including after a human continuation. Stop/snapshot/halt still apply, and a fresh active app observation never automatically resumes a stopped helper.

Schema 2 rejects the deprecated task `inputs` field. Revalidation may explicitly preserve historical strings as `context_refs`; no context string is inspected for file or task meaning. `file_inputs` is a list of exact `{path, revision}` objects. Assignment normalizes each project-relative file path, rejects case-fold duplicates, escapes, reserved paths and links, and pins `sha256` plus `present` (null/false records absence). Immutable file inputs cannot overlap this task's owned writes: owned write files retain their separate pre-dispatch base hashes. A task has an explicit revision and an assignment hash covering its complete task contract, so replacing input/task revision or pins cannot accept old work by silently rehashing it.

`review_targets` is a list of exact `{producer_id, revision}` objects permitted only for verifiers. Every producer must exist at that exact revision and have a fresh observed idle/done/stopped session at assignment and review gates. Assignment pins the producer's exact output file hashes/presence. Context references never establish review authority. Prepared arguments include all typed pins. File inputs and producer contracts/outputs are rechecked at prepare, record, verifier finish and author acceptance. Accepted dependencies are also rechecked. The accepted/running state machine remains unchanged for A05; authors needing independent review may still remain running but observed quiescent while reviewed.

A stale contract raises `INPUT_INVALIDATED` with an `invalidation` receipt containing task_id, assignment revision, reason, exact changed path where applicable, expected/actual hash or revision, at and sorted affected_task_ids. Same-path consumers are seeded only when their own pinned hash/presence or assignment contract fails the observed value; an intact newer revision pinned to current bytes or current absence survives an older pin's invalidation. Invalidation then follows actual dependency/review edges. Context strings and path equality alone do not create edges. Their operational states stay unchanged; `contract_invalid: true` and the durable receipt prevent old output/review acceptance. Restoring old bytes cannot revive the old author or accepted review; repeating its guard preserves the receipt and state bytes.

Input and reviewed/dependency output observation rejects symlinks, junctions, hard-linked files, directory replacements and escapes before hashing. Unsafe output invalidates the producer and affected reviews/consumers before returning the error, at review assignment/prepare/record/finish and author acceptance. The receipt retains the expected hash (or null), exact relative path and an `actual` string beginning `unsafe: ` with the controlled observation reason; missing files instead record null. Unrelated I/O errors such as EIO propagate without being mislabeled as contract drift. This checks the observed path; it does not provide a filesystem-wide atomic snapshot against concurrent external replacement.

All unrelated partial changes are discarded. Direct `apply` retains only those invalidation records in the supplied state; execute atomically persists them before raising. There is no reset/rehash command: inspect the receipt and explicitly assign a fresh task ID and revision with fresh pins. Other failed guards retain their existing byte-preservation policy.

GI01 requires source_created_at and original_turn_id in both authorization and its exact source_message projection; observed_at and observed_turn_id describe observation separately. A source must precede observation and be no older than300 seconds when newly accepted. Source creation time, rather than observation time, must be after stop/halt/completion for continuation. The last accepted source watermark survives continuation and new activation; creation times cannot decrease, and equal times require the same original turn. Event IDs and original source IDs remain replay-reserved across kind, observation time, run/thread and continuation. Observe events must match the current goal/run/thread; an explicit stop also matches its objective scope. Future intent, policy and report observations record evidence but cannot grant current-objective execution or resume. Usage threshold stop is mechanical helper stopping; it does not change the app goal observation or create a user pause authorization.

These fields are supplied evidence, not provider authentication. This helper does not intercept direct native goal tools and never itself calls update_goal. The controlled historical-stop → newer continuation → same-turn context replay regression establishes helper rejection and an unchanged active app observation. N07 adds the versioned execution/acceptance source projection described above. Native fresh-child context/cache measurement and A06/GI02 pause integration remain separate pending evidence.
