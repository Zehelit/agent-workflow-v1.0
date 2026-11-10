"""Model-free, private-body-excluding native metadata accounting (N08).

CLI: extract-usage.py --manifest MANIFEST --output NEW_JSON [--canary-file FILE]
API: extract(manifest), publish(document, path, canaries), compare(left, right).
Sources are exact byte prefixes and inclusive line ranges. No cumulative stream
is a substitute for unique per-response usage; no pricing or quota inference.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys

VERSION = 'n08_scoped_telemetry_v1'
FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
          'output_tokens', 'reasoning_output_tokens', 'total_tokens')
PHASES = ('implementation', 'repair', 'independent_review', 'external_wait', 'closure')
SOURCE_REASONS = frozenset(('range_gaps_unmeasured', 'invalid_strict_json_or_record',
    'session_restart_or_multiple_headers', 'header_scope_mismatch', 'child_identity_mapping_mismatch',
    'response_chronology_nonmonotonic', 'response_identity_missing', 'duplicate_response_prior_invalid',
    'duplicate_response_scope_or_usage_conflict', 'duplicate_response_cross_range',
    'response_source_scope_ambiguous', 'timestamp_missing_or_invalid', 'usage_fields_missing_or_invalid',
    'usage_subset_violation', 'usage_total_violation', 'session_header_missing', 'response_records_missing'))
TOOL_REASONS = frozenset(('timestamp_missing_or_invalid', 'tool_identity_missing',
    'tool_output_representation_missing', 'duplicate_tool_output', 'duplicate_tool_call',
    'invalid_tool_representation', 'tool_pair_missing_or_chronology_invalid',
    'tool_call_output_kind_mismatch', 'tool_output_missing_or_outside_range', 'tool_outputs_missing'))
CUMULATIVE_REASONS = frozenset(('cumulative_baseline_missing', 'cumulative_reset_or_restart',
    'cumulative_fields_missing_or_invalid', 'cumulative_observations_missing',
    'diagnostic_only_never_cross_subtract'))
IDENTIFIER = re.compile(r'[A-Za-z0-9_.:/-]{1,180}\Z')


class Invalid(ValueError):
    """Errors deliberately contain only fixed public reason codes."""


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Invalid('duplicate_json_key')
            result[key] = value
        return result
    def nonfinite(_):
        raise Invalid('nonfinite_json')
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, UnicodeError) as exc:
        raise Invalid('invalid_strict_json') from exc


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False) + '\n'
    except (ValueError, TypeError, UnicodeError) as exc:
        raise Invalid('invalid_canonical_json') from exc


def sid(value):
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else None


def child_spawn(payload):
    value = payload.get('source')
    for key in ('subagent', 'thread_spawn'):
        value = value.get(key) if type(value) is dict else None
    return value if type(value) is dict else {}


def instant(value):
    if not isinstance(value, str):
        raise Invalid('timestamp_missing_or_invalid')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, OverflowError) as exc:
        raise Invalid('timestamp_missing_or_invalid') from exc


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace('+00:00', 'Z')


def usage(value):
    if not isinstance(value, dict) or any(type(value.get(k)) is not int or value[k] < 0 for k in FIELDS):
        raise Invalid('usage_fields_missing_or_invalid')
    out = {k: value[k] for k in FIELDS}
    if out['cached_input_tokens'] > out['input_tokens'] or out['cache_write_input_tokens'] > out['input_tokens'] or out['reasoning_output_tokens'] > out['output_tokens']:
        raise Invalid('usage_subset_violation')
    if out['total_tokens'] != out['input_tokens'] + out['output_tokens']:
        raise Invalid('usage_total_violation')
    return out


def utf8_size(value):
    if isinstance(value, str):
        return len(value.encode('utf-8')), 'raw_utf8_text'
    if isinstance(value, (list, dict)):
        # Preserve object insertion order: the persisted value's representation,
        # compatible with accepted N05, not canonical exported metadata order.
        return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')), 'compact_json_utf8'
    raise Invalid('tool_output_representation_missing')


def schema_validate(value, spec=None):
    """Validate the exact supported JSON Schema contract without dependencies.

    Reject unsupported contract keywords instead of silently skipping them.
    This project schema deliberately uses only this small standard subset.
    """
    if spec is None:
        spec = strict_json(Path(__file__).with_name('telemetry.schema.json').read_text(encoding='utf-8'))
    supported = {'$schema', '$id', 'title', 'description', 'type', 'properties',
                 'required', 'additionalProperties', 'items', 'enum', 'const',
                 'minimum', 'minItems', 'maxItems', 'minLength', 'pattern'}
    if set(spec) - supported:
        raise Invalid('unsupported_schema_keyword')
    types = spec.get('type', [])
    types = [types] if isinstance(types, str) else types
    checks = {'null': value is None, 'boolean': type(value) is bool,
              'integer': type(value) is int, 'number': type(value) in (int, float) and math.isfinite(value),
              'string': isinstance(value, str), 'object': type(value) is dict, 'array': type(value) is list}
    if types and not any(checks.get(t, False) for t in types):
        raise Invalid('schema_type')
    if 'const' in spec and value != spec['const'] or 'enum' in spec and value not in spec['enum']:
        raise Invalid('schema_value')
    if type(value) is dict:
        props = spec.get('properties', {})
        if not set(spec.get('required', [])) <= set(value):
            raise Invalid('schema_required')
        if spec.get('additionalProperties') is False and set(value) - set(props):
            raise Invalid('schema_additional_property')
        for k, v in value.items():
            if k in props:
                schema_validate(v, props[k])
    if type(value) is list:
        if len(value) < spec.get('minItems', 0) or len(value) > spec.get('maxItems', len(value)):
            raise Invalid('schema_min_items')
        for v in value:
            schema_validate(v, spec.get('items', {}))
    if isinstance(value, str):
        if len(value) < spec.get('minLength', 0) or 'pattern' in spec and not re.search(spec['pattern'], value):
            raise Invalid('schema_string')
    if type(value) in (int, float) and 'minimum' in spec and value < spec['minimum']:
        raise Invalid('schema_minimum')


def prefix(path, size=None, expected=None):
    path = Path(path)
    observed = path.stat().st_size
    size = observed if size is None else size
    if type(size) is not int or size <= 0 or size > observed:
        raise Invalid('prefix_size_invalid_or_unavailable')
    with path.open('rb') as handle:
        raw = handle.read(size)
    if len(raw) != size or not raw.endswith(b'\n'):
        raise Invalid('prefix_incomplete_record')
    pin = hashlib.sha256(raw).hexdigest()
    if expected is not None and expected != pin:
        raise Invalid('source_pin_mismatch')
    return raw, {'source_id': None, 'sha256': pin, 'scanned_prefix_bytes': size,
                 'records_scanned': raw.count(b'\n'), 'later_appends_excluded': True}


def null_totals():
    return {k: None for k in (*FIELDS, 'cache_excluded_input_tokens')}


def sum_usage(records):
    if not records:
        return null_totals()
    result = {k: sum(r['usage'][k] for r in records) for k in FIELDS}
    result['cache_excluded_input_tokens'] = result['input_tokens'] - result['cached_input_tokens']
    return result


def scan(config):
    required = {'source_id', 'path', 'thread_id', 'session_id', 'task_id', 'agent_path',
                'ranges', 'requested_model', 'requested_effort', 'main_client_selected'}
    allowed = required | {'prefix_bytes', 'sha256'}
    if type(config) is not dict or set(config) - allowed or not required <= set(config):
        raise Invalid('source_config_contract')
    for k in ('source_id', 'thread_id', 'session_id', 'task_id', 'agent_path'):
        if sid(config[k]) is None:
            raise Invalid('source_identifier_invalid')
    for k in ('requested_model', 'requested_effort'):
        if config[k] is not None and sid(config[k]) is None:
            raise Invalid('requested_settings_invalid')
    if type(config['main_client_selected']) is not bool:
        raise Invalid('main_selection_invalid')
    raw, pin = prefix(config['path'], config.get('prefix_bytes'), config.get('sha256'))
    pin['source_id'] = config['source_id']
    ranges = config['ranges']
    if not isinstance(ranges, list) or not ranges:
        raise Invalid('ranges_missing')
    end = 0
    for bounds in ranges:
        if type(bounds) is not list or len(bounds) != 2 or any(type(v) is not int for v in bounds) or bounds[0] <= end or bounds[1] < bounds[0] or bounds[1] > pin['records_scanned']:
            raise Invalid('ranges_invalid_overlapping_or_missing')
        end = bounds[1]
    inside = lambda n: any(a <= n <= b for a, b in ranges)
    errors, tool_errors, responses, calls, outputs = set(), set(), {}, {}, {}
    if len(ranges) > 1 and any(ranges[i][0] != ranges[i-1][1] + 1 for i in range(1, len(ranges))):
        errors.add('range_gaps_unmeasured')
    diagnostics = {'event_msg.total_token_usage': [], 'token_usage_record.thread_token_usage': []}
    contexts, header, last_at = [], None, None
    invalid_response_ids = set()
    for line, encoded in enumerate(raw.splitlines(), 1):
        try:
            record = strict_json(encoded)
            if type(record) is not dict or type(record.get('payload')) is not dict:
                raise Invalid('record_shape_invalid')
        except Invalid:
            errors.add('invalid_strict_json_or_record')
            continue
        p, kind = record['payload'], record.get('type')
        if kind == 'session_meta':
            h = {'thread_id': sid(p.get('id')), 'session_id': sid(p.get('session_id'))}
            spawn = child_spawn(p)
            h.update(parent_thread_id=sid(spawn.get('parent_thread_id')), agent_path=sid(spawn.get('agent_path')))
            if header is not None:
                errors.add('session_restart_or_multiple_headers')
            else:
                header = h
            if h['thread_id'] != config['thread_id'] or h['session_id'] != config['session_id']:
                errors.add('header_scope_mismatch')
            if not config['main_client_selected'] and (h['agent_path'] != config['agent_path'] or h['parent_thread_id'] != config['session_id']):
                errors.add('child_identity_mapping_mismatch')
        if kind == 'turn_context':
            contexts.append({'line': line, 'turn_id': sid(p.get('turn_id')), 'model': sid(p.get('model')), 'effort': sid(p.get('effort'))})
        if kind == 'event_msg' and p.get('type') == 'token_count':
            info = p.get('info')
            value = info.get('total_token_usage') if type(info) is dict else None
            diagnostics['event_msg.total_token_usage'].append((line, value))
        if kind == 'token_usage_record':
            diagnostics['token_usage_record.thread_token_usage'].append((line, p.get('thread_token_usage')))
            rid = sid(p.get('response_id'))
            try:
                at = instant(record.get('timestamp'))
                # Every selected observation advances the chronology watermark,
                # including duplicate IDs; deduplication is only accounting.
                if inside(line):
                    if last_at is not None and at < last_at:
                        errors.add('response_chronology_nonmonotonic')
                    last_at = max(last_at, at) if last_at is not None else at
                token = usage(p.get('usage'))
                identity = (p.get('thread_id'), p.get('session_id'), p.get('turn_id'), token)
                if rid is None or sid(p.get('turn_id')) is None:
                    raise Invalid('response_identity_missing')
                if inside(line) and rid in invalid_response_ids:
                    errors.add('duplicate_response_prior_invalid')
                # Conflicts anywhere in pinned prefix invalidate selected reuse.
                if rid in responses:
                    if responses[rid]['identity'] != identity:
                        errors.add('duplicate_response_scope_or_usage_conflict')
                    elif inside(line):
                        if not responses[rid]['selected']:
                            errors.add('duplicate_response_cross_range')
                    continue
                if inside(line) and (p.get('thread_id') != config['thread_id'] or p.get('session_id') != config['session_id']):
                    raise Invalid('response_source_scope_ambiguous')
                context = next((c for c in reversed(contexts) if c['turn_id'] == p['turn_id']), None)
                observed = {'model': context['model'] if context else None, 'effort': context['effort'] if context else None,
                            'context_line': context['line'] if context else None,
                            'effective_model': None, 'reason': 'turn_context_is_not_provider_effective_model_attestation' if context and context['model'] and context['effort'] else 'turn_context_model_or_effort_missing'}
                responses[rid] = {'identity': identity, 'selected': inside(line), 'public': {
                    'source_id': config['source_id'], 'task_id': config['task_id'], 'thread_id': p['thread_id'],
                    'session_id': p['session_id'], 'turn_id': p['turn_id'], 'response_id': rid, 'line': line,
                    'timestamp': record['timestamp'], 'usage': token, 'observed': observed}}
            except Invalid as exc:
                if rid is not None:
                    invalid_response_ids.add(rid)
                if inside(line) or rid in responses and responses[rid]['selected']:
                    errors.add(str(exc))
        if kind == 'response_item' and p.get('type') in ('function_call', 'custom_tool_call', 'function_call_output', 'custom_tool_call_output'):
            cid = sid(p.get('call_id'))
            if not inside(line):
                continue
            try:
                instant(record.get('timestamp'))
                if cid is None:
                    raise Invalid('tool_identity_missing')
                if p['type'].endswith('_output'):
                    size, representation = utf8_size(p.get('output'))
                    meta = {'source_id': config['source_id'], 'call_id': cid, 'line': line, 'timestamp': record['timestamp'],
                            'utf8_representation_bytes': size, 'representation': representation,
                            'response_id': None, 'response_mapping_reason': 'response_association_missing_or_ambiguous',
                            '_kind': p['type']}
                    if cid in outputs:
                        raise Invalid('duplicate_tool_output')
                    outputs[cid] = meta
                else:
                    if cid in calls:
                        raise Invalid('duplicate_tool_call')
                    calls[cid] = (line, record['timestamp'], p['type'])
            except (Invalid, UnicodeError, ValueError) as exc:
                tool_errors.add(str(exc) if isinstance(exc, Invalid) else 'invalid_tool_representation')
    if header is None:
        errors.add('session_header_missing')
    for cid, out in outputs.items():
        call = calls.get(cid)
        if call is None or call[0] >= out['line'] or instant(call[1]) > instant(out['timestamp']):
            tool_errors.add('tool_pair_missing_or_chronology_invalid')
        elif out['_kind'] != call[2] + '_output':
            tool_errors.add('tool_call_output_kind_mismatch')
        else:
            matching = [r['public'] for r in responses.values() if r['selected'] and
                        call[0] < r['public']['line'] < out['line'] and
                        instant(call[1]) <= instant(r['public']['timestamp']) <= instant(out['timestamp'])]
            if len(matching) == 1:
                out['response_id'] = matching[0]['response_id']
                out['response_mapping_reason'] = None
        del out['_kind']
    if set(calls) - set(outputs):
        tool_errors.add('tool_output_missing_or_outside_range')
    selected = [r['public'] for r in responses.values() if r['selected']]
    if not selected:
        errors.add('response_records_missing')
    cumulative = []
    for stream, samples in diagnostics.items():
        previous, reasons, selected_count = None, set(), 0
        for line, val in samples:
            try:
                val = usage(val)
                if inside(line):
                    selected_count += 1
                    if previous is None:
                        reasons.add('cumulative_baseline_missing')
                    elif any(val[k] < previous[k] for k in FIELDS):
                        reasons.add('cumulative_reset_or_restart')
                previous = val
            except Invalid:
                if inside(line):
                    reasons.add('cumulative_fields_missing_or_invalid')
                previous = None
        if not selected_count:
            reasons.add('cumulative_observations_missing')
        cumulative.append({'stream': stream, 'observations_in_range': selected_count,
                           'delta': None, 'reasons': sorted(reasons | {'diagnostic_only_never_cross_subtract'})})
    return {'source_id': config['source_id'], 'task_id': config['task_id'], 'thread_id': config['thread_id'],
            'session_id': config['session_id'], 'agent_path': config['agent_path'], 'ranges': ranges, 'pin': pin,
            'requested': {'model': config['requested_model'], 'effort': config['requested_effort'],
                          'main_client_selected': config['main_client_selected'],
                          'reason': 'main_client_selected_request_not_assigned' if config['main_client_selected'] else ('native_request_fields_observed' if config['requested_model'] and config['requested_effort'] else 'native_request_metadata_missing')},
            'header': header, 'responses': selected, 'response_count': len(selected) if not errors else None,
            'totals': sum_usage(selected) if not errors else null_totals(), 'reasons': sorted(errors),
            'tool_outputs': list(outputs.values()), 'tool_output_utf8_representation_bytes': sum(o['utf8_representation_bytes'] for o in outputs.values()) if not tool_errors and outputs else None,
            'tool_reasons': sorted(tool_errors | ({'tool_outputs_missing'} if not outputs else set())),
            'cumulative_streams': cumulative}


def phase_summary(intervals):
    if type(intervals) is not list:
        raise Invalid('phases_missing_or_invalid')
    normalized, active, reasons = [], [], set()
    for p in intervals:
        if type(p) is not dict or set(p) != {'phase', 'task_id', 'source_id', 'start', 'end', 'authority'} or p['phase'] not in PHASES:
            raise Invalid('phase_contract')
        if any(sid(p[k]) is None for k in ('task_id', 'source_id', 'authority')):
            raise Invalid('phase_identity_invalid')
        # A missing endpoint does not license malformed known endpoints.
        for k in ('start', 'end'):
            if p[k] is not None:
                instant(p[k])
        if p['start'] is None or p['end'] is None:
            normalized.append(dict(p, duration_seconds=None, reason='phase_capture_missing'))
            reasons.add('phase_capture_missing')
            continue
        a, b = instant(p['start']), instant(p['end'])
        if b < a:
            raise Invalid('phase_chronology_invalid')
        normalized.append(dict(p, duration_seconds=b-a, reason=None))
        active.append((a, b))
    def union(spans):
        merged = []
        for a, b in sorted(spans):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return sum(b-a for a, b in merged)
    per_agent = {}
    for p in normalized:
        if p['reason'] is None and p['phase'] != 'external_wait':
            per_agent.setdefault(p['source_id'], []).append((instant(p['start']), instant(p['end'])))
    missing = sorted(set(PHASES) - {p['phase'] for p in normalized})
    if missing:
        reasons.add('phase_types_unmapped')
    return {'intervals': normalized, 'observed_wall_union_seconds': union(active) if active else None,
            'observed_agent_interval_seconds_sum': sum(union(v) for v in per_agent.values()) if per_agent else None,
            'agent_active_seconds_sum': None, 'agent_active_reason': 'lifecycle_intervals_do_not_attest_active_compute_time',
            'complete_wall_union_seconds': union(active) if active and not reasons else None,
            'missing_phase_types': missing, 'reasons': sorted(reasons)}


def extract_run(manifest):
    if type(manifest) is not dict or set(manifest) != {'run_id', 'sources', 'phases', 'coverage_gaps'} or sid(manifest['run_id']) is None or type(manifest['sources']) is not list:
        raise Invalid('run_manifest_contract')
    sources = [scan(s) for s in manifest['sources']]
    if not sources or len({s['source_id'] for s in sources}) != len(sources):
        raise Invalid('source_missing_or_duplicate')
    selected, reasons = {}, set()
    for source in sources:
        reasons.update(source['reasons'])
        for r in source['responses']:
            rid = r['response_id']
            if rid in selected:
                # An ID in two scopes cannot be counted as two requests.
                prior = selected[rid]
                if any(prior[k] != r[k] for k in ('source_id', 'thread_id', 'session_id', 'turn_id', 'usage')):
                    reasons.add('cross_source_response_scope_or_usage_conflict')
            else:
                selected[rid] = r
    gaps = manifest['coverage_gaps']
    if not isinstance(gaps, list) or any(sid(g) is None for g in gaps):
        raise Invalid('coverage_gap_invalid')
    tools_valid = all(not s['tool_reasons'] for s in sources)
    phases = phase_summary(manifest['phases'])
    if any(p['source_id'] not in {s['source_id'] for s in sources} for p in phases['intervals']):
        raise Invalid('phase_source_unmapped')
    if any(p['task_id'] != next(s['task_id'] for s in sources if s['source_id'] == p['source_id']) for p in phases['intervals']):
        raise Invalid('phase_task_unmapped')
    complete = not gaps and not reasons and tools_valid and not phases['reasons']
    if not complete:
        phases['complete_wall_union_seconds'] = None
        phases['reasons'] = sorted(set(phases['reasons']) | {'run_coverage_incomplete'})
    return {'run_id': manifest['run_id'], 'sources': sources,
            'response_count': len(selected) if not reasons and selected else None,
            'totals': sum_usage(list(selected.values())) if not reasons else null_totals(),
            'tool_output_utf8_representation_bytes': sum(s['tool_output_utf8_representation_bytes'] for s in sources) if tools_valid else None,
            'reasons': sorted(reasons), 'coverage_gaps': gaps, 'coverage_complete': complete,
            'phases': phases, 'cost': {'amount': None, 'currency': None, 'reason': 'provider_billing_metadata_unavailable'},
            'cache_miss_cause': None, 'cache_miss_cause_reason': 'native_cache_cause_not_observed'}


def compare(left, right):
    def change(a, b):
        return b-a if type(a) in (int, float) and type(b) in (int, float) else None
    elapsed = change(left['phases']['observed_wall_union_seconds'], right['phases']['observed_wall_union_seconds'])
    tokens = change(left['totals']['total_tokens'], right['totals']['total_tokens'])
    cost = change(left['cost']['amount'], right['cost']['amount']) if left['cost'].get('currency') and left['cost'].get('currency') == right['cost'].get('currency') else None
    return {'left_run_id': left['run_id'], 'right_run_id': right['run_id'],
            'observed_wall_union_seconds_change': elapsed, 'raw_total_tokens_change': tokens,
            'cost_amount_change': cost, 'faster_but_more_raw_tokens': elapsed < 0 and tokens > 0 if elapsed is not None and tokens is not None else None,
            'faster_but_more_costly': elapsed < 0 and cost > 0 if elapsed is not None and cost is not None else None,
            'economic_acceptance': False, 'reason': 'bounded_observations_do_not_establish_economic_acceptance'}


def semantic_validate(document):
    """Recompute public arithmetic and null/reason semantics before publication.

    This validates the exported contract, not source authenticity. A caller may
    supply a schema-valid mutated document, so structural validation alone is
    insufficient. Native scan failures remain explicit, rather than forcing
    totals from diagnostic or incomplete evidence.
    """
    def require(ok):
        if not ok:
            raise Invalid('export_semantic_contract')

    def canonical_reasons(reasons):
        require(reasons == sorted(set(reasons)))

    runs = {}
    for run in document['runs']:
        require(run['run_id'] not in runs and bool(run['sources']))
        runs[run['run_id']] = run
        sources, selected, run_reasons = {}, {}, set()
        for source in run['sources']:
            source_id = source['source_id']
            require(source_id not in sources and source['pin']['source_id'] == source_id)
            sources[source_id] = source
            canonical_reasons(source['reasons'])
            canonical_reasons(source['tool_reasons'])
            require(set(source['reasons']) <= SOURCE_REASONS)
            require(set(source['tool_reasons']) <= TOOL_REASONS)
            run_reasons.update(source['reasons'])
            end = 0
            require(bool(source['ranges']))
            for a, b in source['ranges']:
                require(end < a <= b <= source['pin']['records_scanned'])
                end = b
            inside = lambda n: any(a <= n <= b for a, b in source['ranges'])
            if any(source['ranges'][i][0] > source['ranges'][i-1][1] + 1 for i in range(1, len(source['ranges']))):
                require('range_gaps_unmeasured' in source['reasons'])
            header = source['header']
            if header is None:
                require('session_header_missing' in source['reasons'])
            else:
                if any(header[k] != source[k] for k in ('thread_id', 'session_id')):
                    require('header_scope_mismatch' in source['reasons'])
                if not source['requested']['main_client_selected'] and (header['agent_path'] != source['agent_path'] or header['parent_thread_id'] != source['session_id']):
                    require('child_identity_mapping_mismatch' in source['reasons'])
            req = source['requested']
            require(all(req[k] is None or sid(req[k]) is not None for k in ('model', 'effort')))
            require(req['reason'] == ('main_client_selected_request_not_assigned' if req['main_client_selected'] else ('native_request_fields_observed' if req['model'] and req['effort'] else 'native_request_metadata_missing')))
            local, last_line, last_at = {}, 0, None
            for response in source['responses']:
                usage(response['usage'])
                require(all(response[k] == source[k] for k in ('source_id', 'task_id', 'thread_id', 'session_id')))
                rid, line = response['response_id'], response['line']
                require(rid not in local and inside(line) and line > last_line)
                at = instant(response['timestamp'])
                if last_at is not None and at < last_at:
                    require('response_chronology_nonmonotonic' in source['reasons'])
                last_at, last_line = at, line
                obs = response['observed']
                require(all(obs[k] is None or sid(obs[k]) is not None for k in ('model', 'effort')))
                require(obs['reason'] == ('turn_context_is_not_provider_effective_model_attestation' if obs['context_line'] and obs['model'] and obs['effort'] else 'turn_context_model_or_effort_missing'))
                require(obs['context_line'] is None and obs['model'] is None and obs['effort'] is None or obs['context_line'] is not None and obs['context_line'] < line)
                local[rid] = response
                if rid in selected:
                    if any(selected[rid][k] != response[k] for k in ('source_id', 'thread_id', 'session_id', 'turn_id', 'usage')):
                        run_reasons.add('cross_source_response_scope_or_usage_conflict')
                else:
                    selected[rid] = response
            if not local:
                require('response_records_missing' in source['reasons'])
            require(source['response_count'] == (None if source['reasons'] else len(local)))
            require(source['totals'] == (null_totals() if source['reasons'] else sum_usage(list(local.values()))))
            calls = set()
            for out in source['tool_outputs']:
                require(out['source_id'] == source_id and out['call_id'] not in calls and inside(out['line']))
                calls.add(out['call_id'])
                instant(out['timestamp'])
                if out['response_id'] is None:
                    require(out['response_mapping_reason'] == 'response_association_missing_or_ambiguous')
                else:
                    require(out['response_id'] in local and out['response_mapping_reason'] is None)
                    response = local[out['response_id']]
                    require(response['line'] < out['line'] and instant(response['timestamp']) <= instant(out['timestamp']))
            if not source['tool_outputs']:
                require('tool_outputs_missing' in source['tool_reasons'])
            require(source['tool_output_utf8_representation_bytes'] == (None if source['tool_reasons'] else sum(o['utf8_representation_bytes'] for o in source['tool_outputs'])))
            diagnostics = source['cumulative_streams']
            require(len(diagnostics) == 2 and {d['stream'] for d in diagnostics} == {'event_msg.total_token_usage', 'token_usage_record.thread_token_usage'})
            for diag in diagnostics:
                canonical_reasons(diag['reasons'])
                require(set(diag['reasons']) <= CUMULATIVE_REASONS)
                require('diagnostic_only_never_cross_subtract' in diag['reasons'])
                if not diag['observations_in_range']:
                    require('cumulative_observations_missing' in diag['reasons'])
        require(run['reasons'] == sorted(run_reasons))
        require(run['response_count'] == (None if run_reasons or not selected else len(selected)))
        require(run['totals'] == (null_totals() if run_reasons else sum_usage(list(selected.values()))))
        tools_valid = all(not s['tool_reasons'] for s in sources.values())
        require(run['tool_output_utf8_representation_bytes'] == (sum(s['tool_output_utf8_representation_bytes'] for s in sources.values()) if tools_valid else None))
        phases = phase_summary([{k: p[k] for k in ('phase', 'task_id', 'source_id', 'start', 'end', 'authority')} for p in run['phases']['intervals']])
        require(all(p['source_id'] in sources for p in phases['intervals']))
        require(all(p['task_id'] == sources[p['source_id']]['task_id'] for p in phases['intervals']))
        complete = not run['coverage_gaps'] and not run_reasons and tools_valid and not phases['reasons']
        if not complete:
            phases['complete_wall_union_seconds'] = None
            phases['reasons'] = sorted(set(phases['reasons']) | {'run_coverage_incomplete'})
        require(run['coverage_complete'] == complete and run['phases'] == phases)
        require(run['cost']['reason'] == 'provider_billing_metadata_unavailable')
        require(run['cache_miss_cause_reason'] == 'native_cache_cause_not_observed')
    for comparison in document['comparisons']:
        a, b = comparison['left_run_id'], comparison['right_run_id']
        require(a in runs and b in runs)
        require(comparison == compare(runs[a], runs[b]))
    for check in document['checks']:
        require(check['passed'] == (check['expected_response_count'] == check['observed_response_count'] and check['expected_tool_output_bytes'] == check['observed_tool_output_bytes']))


def extract(manifest):
    if type(manifest) is not dict or set(manifest) != {'runs', 'comparison_pairs', 'checks', 'evidence_pins'} or any(type(manifest[k]) is not list for k in manifest):
        raise Invalid('manifest_contract')
    runs = [extract_run(r) for r in manifest['runs']]
    if len({r['run_id'] for r in runs}) != len(runs):
        raise Invalid('duplicate_run_id')
    by_id = {r['run_id']: r for r in runs}
    comparisons = []
    for a, b in manifest['comparison_pairs']:
        if a not in by_id or b not in by_id:
            raise Invalid('comparison_run_unmapped')
        comparisons.append(compare(by_id[a], by_id[b]))
    document = {'schema': VERSION, 'runs': runs, 'comparisons': comparisons,
                'checks': manifest['checks'], 'evidence_pins': manifest['evidence_pins'],
                'independent_acceptance': False,
                'limitations': ['persisted_utf8_representation_not_wire_or_billing_bytes',
                    'completion_metadata_not_billable_api_requests', 'no_quota_per_operation_attribution',
                    'fresh_versus_long_native_cache_experiment_pending', 'source_tests_do_not_close_native_experiment',
                    'no_effective_model_or_cost_inference', 'private_bodies_excluded_not_classified_as_reasoning']}
    schema_validate(document)
    semantic_validate(document)
    return document


def publish(document, path, canaries=()):
    schema_validate(document)
    semantic_validate(document)
    text = canonical(document)
    # Reject metadata contamination before opening or replacing output. Bodies
    # excluded by construction are never reclassified or exported.
    if any(not isinstance(c, str) or not c for c in canaries):
        raise Invalid('canary_configuration_invalid')
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for k, v in value.items():
                yield k
                yield from strings(v)
        elif isinstance(value, list):
            for v in value:
                yield from strings(v)
    if any(c in s for s in strings(document) for c in canaries):
        raise Invalid('private_canary_export_rejected')
    path = Path(path)
    with path.open('x', encoding='utf-8', newline='\n') as handle:
        handle.write(text)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--canary-file')
    args = parser.parse_args()
    try:
        canaries = strict_json(Path(args.canary_file).read_text(encoding='utf-8')) if args.canary_file else []
        if not isinstance(canaries, list):
            raise Invalid('canary_configuration_invalid')
        manifest = strict_json(Path(args.manifest).read_text(encoding='utf-8'))
        document = extract(manifest)
        pin = publish(document, args.output, canaries)
        print(canonical({'status': 'PRODUCER_METADATA_ONLY', 'sha256': pin}).strip())
        return 0
    except (Invalid, OSError, TypeError, KeyError, OverflowError):
        # Never interpolate an exception: it can contain a private source value.
        print('{"status":"REJECTED","reason":"invalid_unavailable_or_private_export"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
