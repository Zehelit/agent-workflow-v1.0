# Pinned runtime binding and installation

Technical identity: `agent-workflow-v1-0`; visible name: `agent-workflow-v1.0`; implicit invocation: false. Only explicit selection adopts this candidate. Main-client model selection remains the user's; non-limit roles require exactly Sol/high, private watcher exactly Luna low or medium. No fallback is permitted.

Build with Python 3.11+ and the standard library:

```powershell
python -X utf8 -B tooling/build-candidate.py --output C:/staging/build-one
```

The output contains `agent-workflow-v1-0/`, a deterministic ZIP and `build-receipt.json`. Preserve the receipt's `manifest_sha256` in the accepted coordination/handoff evidence outside the mutable package. That caller-supplied pin is the trust anchor. The runtime verifies the manifest against it, then verifies every dependency before run binding, state writes, coordination steps and worker operations. Recomputing a changed manifest's SHA and passing the new value does not establish external attestation. Python checks byte integrity and identity; it cannot authenticate the human approval, provider/model metadata or a caller-controlled filesystem. A compromised executable/interpreter or caller who edits both binding and accepted external pin is outside this integrity contract.

Use global options before the mode. Source execution uses `skills/agent-workflow-v1-0/scripts/candidate.py` plus the built manifest. Installed execution uses the installed `scripts/candidate.py` and manifest, with the same preserved external hash. Each source/installed mapping uses a distinct run ID; the immutable binding includes resolved dependency and manifest paths, project, run, version and hashes. Moving a run to another helper mapping, replacing its package, editing/missing dependencies or reusing a run with a different pin is refused. Start a new run after an approved update.

```powershell
$pin = '<preserved manifest_sha256 from accepted build receipt>'
$entry = 'C:/staging/build-one/agent-workflow-v1-0/scripts/candidate.py'
$manifest = 'C:/staging/build-one/agent-workflow-v1-0/manifest.json'
$common = @('-X','utf8','-B',$entry,'--manifest',$manifest,'--manifest-sha256',$pin,'--version','1.0','--project','C:/project','--run-id','candidate-run-1')
python @common workflow validate
python @common workflow status
python @common coordination --input C:/project/batch.json
```

An authorized `workflow activate` or `bind-active-goal` starts the watcher and verifies its first healthy sample; `resume` opens a new epoch only after valid continuation. Active commands renew the lease, and `complete`/`halt` stop and verify the owned process. The explicit `watcher` commands remain available for diagnosis or sustained work outside an activated workflow.

`workflow` accepts the core typed state commands and `validate`; commands read JSON from `--input` or stdin. The old `native/install/export/migrate` core CLIs are unsupported for this candidate. `activate` authorization and `bind-active-goal` evidence must carry the selected run ID. `validate` verifies roles/dependencies and binds the namespace but does not activate a goal. A batch is `{ "batch_id": "inspect", "steps": [{ "key": "status", "command": "status", "payload": {} }] }`; at most 64 steps, one main writer, durable pending outcomes, no automatic retry or native dispatch. Typed current authority and acceptance remain necessary for substantive commands. Terminal inventory does not accept output. Quiescent stale work can be discarded with `finish accepted:false`; live writers cannot. Compaction requires a verified checkpoint and archives task/invalidation history.

The isolated root is `<project>/.agent-workflow-v1-0/runs/<run-id>/`. Operational state roots are reserved against file claims. The integrated watcher uses `watcher/` there, an explicit pinned schedule helper, immutable ownership and candidate-entry relaunch arguments. Wrong-run controls or direct private CLI bypass cannot renew/stop another worker. A quiet chat cannot renew the bounded lease; use an active candidate command at work boundaries. Package updates do not silently migrate running workers. There is no separate watcher skill or launcher. Private dependencies are loaded from the approved package.

Fresh install, no-op and owned update require an explicit destination ending `agent-workflow-v1-0`:

```powershell
python -X utf8 -B $entry --manifest $manifest --manifest-sha256 $pin --version 1.0 install --destination C:/staging/skills/agent-workflow-v1-0
python -X utf8 -B $entry --manifest $manifest --manifest-sha256 $pin --version 1.0 install --destination C:/staging/skills/agent-workflow-v1-0 --update
```

Updates require untouched owned installation bytes. The installer validates a staged package before replacing the existing directory. Failed publication restores the previous directory; successful updates remove transaction material. No permanent rollback archive or rollback command exists. If restoration itself fails, the transaction directory retains the only previous copy for explicit recovery; do not delete it until recovery is verified. These commands prepare files only: `activated:false`. Catalogue visibility, explicit native skill selection, effective provider model metadata and real product/pilot acceptance remain post-install checks.

On Windows, the installer resets the temporary staging root's ACL to inherit from the destination's parent before copying package files. It does not name a specific Windows account: a different user or a renamed account receives the rights already assigned to that user's `skills` parent. The parent must grant the intended user read and execute access. After installation, verify discovery and direct candidate execution from a normal, non-elevated Codex session; success in an elevated session alone is insufficient.
