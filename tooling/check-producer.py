"""Model-free producer handoff gate. PASS is never independent acceptance."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE_TESTS = ROOT / 'skills/agent-workflow-v1-1/tests'
TOOL_TESTS = ROOT / 'tooling/tests'

# Existing accepted counterexamples are selected, not reimplemented. A11 uses
# current-source copy fixtures with explicit dependency revalidation below.
GROUPS = {
    'core_guards': ['test_workflow', 'test_inputs', 'test_authorization', 'test_goal_binding',
                    'test_coordination', 'test_coordination_journal', 'test_context_acceptance'],
    'A04': ['test_inputs.InputTests.test_review_target_change_before_prepare_during_review_and_after_review',
            'test_inputs.InputTests.test_accepted_review_unsafe_output_is_durable_and_restore_cannot_revive_it',
            'test_inputs.InputTests.test_assignment_revision_replacement_never_rehashes_old_result'],
    'A09': ['test_readiness.ReadinessTests.test_pinned_receipt_never_authenticates_native_claim',
            'test_readiness.ReadinessTests.test_versioned_bounded_observation_and_provenance_contract',
            'test_readiness.ReadinessTests.test_recursive_strict_receipt_json_and_large_integer',
            'test_readiness.ReadinessTests.test_missing_engine_file_and_present_file_are_both_native_gaps'],
    'A11': ['test_monitor_integration.MonitorIntegrationTests.test_strict_json_every_frame_and_nested_boundary',
            'test_monitor_integration.MonitorIntegrationTests.test_huge_integer_every_rpc_position_retries_and_latches',
            'test_monitor_integration.MonitorIntegrationTests.test_malformed_health_holds_with_and_without_marker'],
    'producer_gate': ['test_producer'],
}
PIN_PATHS = [
    'skills/agent-workflow-v1-1/scripts/workflow.py', 'skills/agent-workflow-v1-1/scripts/coordination.py',
    'skills/agent-workflow-v1-1/tests/test_workflow.py', 'skills/agent-workflow-v1-1/tests/test_inputs.py',
    'skills/agent-workflow-v1-1/tests/test_authorization.py', 'skills/agent-workflow-v1-1/tests/test_goal_binding.py',
    'skills/agent-workflow-v1-1/tests/test_coordination.py', 'skills/agent-workflow-v1-1/tests/test_coordination_journal.py',
    'skills/agent-workflow-v1-1/tests/test_context_acceptance.py', 'skills/agent-workflow-v1-1/tests/test_monitor_integration.py',
    'skills/agent-workflow-v1-1/private/limit-watcher/scripts/monitor.py', 'tooling/preflight.py', 'tooling/runtime-manifest.schema.json', 'tooling/tests/test_readiness.py',
    'tooling/check-producer.py', 'skills/agent-workflow-v1-1/tools/render-active-run.py', 'tooling/tests/test_producer.py',
    'skills/agent-workflow-v1-1/SKILL.md', 'skills/agent-workflow-v1-1/references/context.md', 'skills/agent-workflow-v1-1/references/commands.md',
] + [path.relative_to(ROOT).as_posix() for path in sorted((ROOT/'skills/agent-workflow-v1-1/roles').glob('*.toml'))]


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def source_file(path):
    if (type(path) is not str or not path or '\\' in path or ':' in path
            or PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()
            or not PurePosixPath(path).parts or '..' in PurePosixPath(path).parts
            or path != PurePosixPath(path).as_posix()):
        raise ValueError('normalized safe project-relative source file required')
    if (PurePosixPath(path).parts[0].casefold() == '.agent-workflow'
            or any(part != part.rstrip(' .') or any(ord(char) < 32 for char in part)
                   or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)
                   for part in PurePosixPath(path).parts)):
        raise ValueError('reserved or aliased source path rejected')
    item = ROOT / path
    chain = []
    for ancestor in (item, *item.parents):
        chain.append(ancestor)
        if ancestor == ROOT: break
    for ancestor in reversed(chain):
        info = ancestor.lstat()
        if ancestor.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('linked source path rejected')
    if not item.is_file() or not item.resolve().is_relative_to(ROOT.resolve()) or item.stat().st_nlink != 1:
        raise ValueError('existing unlinked project-relative source file required')
    return item


def pins(changed_paths=()):
    paths = sorted(set(PIN_PATHS) | set(changed_paths))
    if len({path.casefold() for path in paths}) != len(paths):
        raise ValueError('case-alias source paths rejected')
    return {path: sha(source_file(path)) for path in paths}


def validate_metadata(metadata):
    if type(metadata) is not dict or set(metadata) != {'rejections', 'rework_seconds'}:
        raise ValueError('exact rejection/rework metadata required')
    seconds = metadata['rework_seconds']
    if seconds is not None and (type(seconds) not in (int, float) or not 0 <= seconds < float('inf')):
        raise ValueError('finite nonnegative measured rework seconds or null required')
    if type(metadata['rejections']) is not list: raise ValueError('rejection list required')
    writers, classes = {}, {}
    fields = {'defect_id', 'defect_class', 'reproduction', 'corrector_id', 'review_task', 'revision', 'source_pins'}
    for item in metadata['rejections']:
        if type(item) is not dict or not fields <= set(item) or set(item) - fields - {'root_plan_correction'}:
            raise ValueError('exact defect/reproduction/corrector/focused-review metadata required')
        if any(type(item[key]) is not str or not item[key].strip() for key in fields - {'source_pins'}):
            raise ValueError('nonempty rejection identity and reproduction required')
        if item['corrector_id'] == item['review_task']: raise ValueError('corrector cannot independently review itself')
        previous = writers.setdefault(item['defect_id'], item['corrector_id'])
        if previous != item['corrector_id']: raise ValueError('one defect requires one corrector writer')
        classes[item['defect_class']] = classes.get(item['defect_class'], 0) + 1
        if classes[item['defect_class']] >= 2 and (type(item.get('root_plan_correction')) is not str or not item['root_plan_correction'].strip()):
            raise ValueError('second same-class rejection requires short root plan correction')
        values = item['source_pins']
        if type(values) is not dict or not values or any(type(key) is not str or not key.strip()
                or type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value)
                for key, value in values.items()): raise ValueError('exact rejection source pins required')
    return metadata


def load_tests():
    for path in (CORE_TESTS, TOOL_TESTS):
        if str(path) not in sys.path: sys.path.insert(0, str(path))
    module = importlib.import_module('test_monitor_integration')
    historical = module.WORKFLOW_PIN
    current = sha(ROOT / 'skills/agent-workflow-v1-1/scripts/workflow.py')
    # Explicit current-source revalidation of accepted tests in disposable copy
    # fixtures, retaining the historical suite bytes and baseline pin in output.
    module.WORKFLOW_PIN = current
    return dict(test_path='skills/agent-workflow-v1-1/tests/test_monitor_integration.py',
                historical_dependency_sha256=historical, tested_dependency_sha256=current,
                method='in_memory_dependency_pin_override_for_disposable_source_copy')


def run_group(selectors, stream):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(selector) for selector in selectors)
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    return dict(ok=result.wasSuccessful() and result.testsRun > 0 and not result.skipped, tests=result.testsRun,
                failures=[case.id() for case, _ in result.failures], errors=[case.id() for case, _ in result.errors],
                skipped=[dict(test=case.id(), reason=reason) for case, reason in result.skipped])


def gate(changed_paths, shared_api, metadata, log_stream, expected_pins=None):
    validate_metadata(metadata)
    if (type(changed_paths) is not list or not changed_paths or any(type(path) is not str for path in changed_paths)
            or len(changed_paths) != len({path.casefold() for path in changed_paths})):
        raise ValueError('unique changed paths required')
    for path in changed_paths:
        source_file(path)
    if type(shared_api) is not str or not shared_api.strip(): raise ValueError('shared API contract required')
    before = pins(changed_paths)
    if expected_pins is not None and expected_pins != before: raise ValueError('producer source pins changed before gate')
    started = time.monotonic()
    revalidation = load_tests()
    results = {}
    with contextlib.redirect_stdout(log_stream), contextlib.redirect_stderr(log_stream):
        for name, selectors in GROUPS.items():
            results[name] = dict(selectors=selectors, **run_group(selectors, log_stream))
    after = pins(changed_paths)
    return dict(gate_version=1, producer_pass=all(group['ok'] for group in results.values()) and before == after,
                independent_acceptance=False, changed_paths=changed_paths, shared_api=shared_api,
                source_pins=before, source_unchanged=before == after, groups=results,
                tests=sum(group['tests'] for group in results.values()), optimized=sys.flags.optimize,
                elapsed_seconds=round(time.monotonic() - started, 3), rejection_rework=metadata,
                dependency_revalidation=revalidation, native_adoption='not_measured', cache_measurement='pending_N08')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--changed-path', action='append', required=True)
    parser.add_argument('--shared-api', required=True)
    parser.add_argument('--metadata', help='strict JSON rejection/rework metadata; omission means no observed rejection, cost unknown')
    parser.add_argument('--expected-pins', help='strict JSON exact source pin map for frozen handoff rerun')
    args = parser.parse_args(argv)
    spec = importlib.util.spec_from_file_location('producer_json', ROOT/'skills/agent-workflow-v1-1/scripts/workflow.py')
    engine = importlib.util.module_from_spec(spec); spec.loader.exec_module(engine)
    try:
        metadata = engine.read_json(Path(args.metadata)) if args.metadata else dict(rejections=[], rework_seconds=None)
        expected = engine.read_json(Path(args.expected_pins)) if args.expected_pins else None
        # Raw unittest/RPC logs only in a disposable temporary file, never JSON.
        import tempfile
        with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as log:
            result = gate(args.changed_path, args.shared_api, metadata, log, expected)
    except (ValueError, OSError) as exc:
        print(json.dumps(dict(gate_version=1, producer_pass=False, independent_acceptance=False, error=str(exc)), ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0 if result['producer_pass'] else 1


if __name__ == '__main__': raise SystemExit(main())
