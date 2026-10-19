"""Task-scoped agent coordination. No network, model calls or process termination."""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import tomllib
import uuid
import zipfile

PACKAGE = Path(__file__).resolve().parents[1]
ACTIVE = {"running", "interrupting", "unknown"}
TERMINAL = {"accepted", "rejected"}
AUTH_KINDS = {'explicit_task_authorization', 'explicit_goal_continuation',
              'future_goal_intent', 'policy_change', 'independent_report_request', 'explicit_stop'}
SCHEMA_VERSION = 2
SOURCE_FIELDS = ('source_kind', 'source_id', 'exact_quote', 'source_created_at', 'original_turn_id')


class Error(ValueError):
    def __init__(self, message, code='COMMAND_CONSTRAINT', field='$'):
        super().__init__(message)
        self.code = code
        self.field = field


class InputInvalidation(Error):
    def __init__(self, receipt):
        super().__init__('typed input contract invalidated: ' + receipt['reason'], 'INPUT_INVALIDATED', '$.id')
        self.receipt = receipt


class UnsafePath(Error):
    """An observed path violates the file boundary, rather than a workflow guard."""


def invalid(code, field, message):
    raise Error(message, code, field)


def field_path(path, key):
    return path + '.' + key if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) else path + '[' + json.dumps(key) + ']'


def json_value(value, path='$'):
    """Validate caller objects as JSON, including values in opaque tool receipts."""
    if value is None or type(value) in (bool, str, int):
        return
    if type(value) is float:
        if type(value) is float and not math.isfinite(value):
            invalid('JSON_NONFINITE', path, 'finite JSON number required')
        return
    if type(value) is list:
        for index, item in enumerate(value):
            json_value(item, f'{path}[{index}]')
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                invalid('JSON_TYPE', path, 'JSON object keys must be strings')
            json_value(item, field_path(path, key))
        return
    invalid('JSON_TYPE', path, 'JSON value required')


class JsonPairs(list):
    """Keep object members until their full field path can be checked."""


class JsonConstant:
    def __init__(self, token):
        self.token = token


def strict_json(raw):
    try:
        parsed = json.loads(raw, object_pairs_hook=JsonPairs, parse_constant=JsonConstant)
    except (ValueError, UnicodeError) as exc:
        invalid('JSON_SYNTAX', '$', 'invalid JSON: ' + str(exc))

    def unpack(value, path):
        if isinstance(value, JsonConstant):
            invalid('JSON_NONFINITE', path, 'nonfinite JSON token: ' + value.token)
        if isinstance(value, JsonPairs):
            result = {}
            for key, item in value:
                child = field_path(path, key)
                if key in result:
                    invalid('JSON_DUPLICATE_KEY', child, 'duplicate JSON key: ' + key)
                result[key] = unpack(item, child)
            return result
        if type(value) is list:
            return [unpack(item, f'{path}[{index}]') for index, item in enumerate(value)]
        json_value(value, path)
        return value

    return unpack(parsed, '$')


def object_fields(value, required, optional, path):
    if type(value) is not dict:
        invalid('FIELD_TYPE', path, 'object required')
    for key in value:
        if key not in required and key not in optional:
            invalid('FIELD_UNKNOWN', field_path(path, key), 'unknown field: ' + key)
    for key in required:
        if key not in value:
            invalid('FIELD_REQUIRED', field_path(path, key), 'required field: ' + key)
    for key, item in value.items():
        check_field(item, (required | optional)[key], field_path(path, key))


def check_field(value, kind, path):
    if kind.endswith('?'):
        if value is None:
            return
        kind = kind[:-1]
    if kind == 'bool':
        if type(value) is not bool:
            invalid('FIELD_TYPE', path, 'exact boolean required')
    elif kind in {'text', 'string', 'task_id'}:
        if type(value) is not str:
            invalid('FIELD_TYPE', path, 'string required')
        if kind != 'string' and not value.strip():
            invalid('FIELD_VALUE', path, 'nonempty string required')
        if kind == 'task_id' and not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', value):
            invalid('FIELD_VALUE', path, 'short task id required')
    elif kind in {'timestamp', 'count', 'percent'}:
        if type(value) not in (int, float) or (kind == 'count' and type(value) is not int):
            invalid('FIELD_TYPE', path, 'integer required' if kind == 'count' else 'finite number required')
        if type(value) is float and not math.isfinite(value):
            invalid('JSON_NONFINITE', path, 'finite number required')
        if (kind == 'count' and value < 1) or (kind == 'timestamp' and not 0 <= value <= sys.float_info.max) or (kind == 'percent' and not 0 <= value <= 100):
            invalid('FIELD_VALUE', path, 'number outside allowed range')
    elif kind in {'strings', 'criteria', 'task_ids'}:
        if type(value) is not list:
            invalid('FIELD_TYPE', path, 'list required')
        if kind == 'criteria' and not value:
            invalid('FIELD_VALUE', path, 'nonempty criteria required')
        for index, item in enumerate(value):
            check_field(item, 'task_id' if kind == 'task_ids' else 'text', f'{path}[{index}]')
    elif kind == 'models':
        if type(value) is not dict:
            invalid('FIELD_TYPE', path, 'model-to-effort-list object required')
        for key, efforts in value.items():
            check_field(key, 'text', path)
            check_field(efforts, 'strings', field_path(path, key))
    elif kind == 'inventory':
        if type(value) is not list:
            invalid('FIELD_TYPE', path, 'inventory must be a list')
        for index, item in enumerate(value):
            child = f'{path}[{index}]'
            object_fields(item, {'id': 'text', 'status': 'text'}, {}, child)
            if item['status'] not in ACTIVE | {'idle', 'stopped', 'done'}:
                invalid('FIELD_VALUE', child + '.status', 'unknown inventory status')
    elif kind == 'inventory_observation':
        object_fields(value, dict(source_kind='text', source_id='text'), dict(receipt='receipt', evidence_path='text', sha256='text'), path)
        require(value['source_kind'] == 'collaboration.list_agents', 'native list_agents observation required', path + '.source_kind')
        require(('receipt' in value and not any(k in value for k in ('evidence_path', 'sha256'))) or
                ('receipt' not in value and all(k in value for k in ('evidence_path', 'sha256'))), 'one raw receipt or pinned evidence path required', path)
        if 'sha256' in value:
            require(re.fullmatch('[a-f0-9]{64}', value['sha256']), 'inventory evidence SHA-256 required', path + '.sha256')
    elif kind == 'authorization':
        object_fields(value, {**{key: 'text' for key in ('event_id', 'exact_quote', 'kind', 'source_kind', 'source_id', 'goal_id', 'thread_id', 'run_id')},
                              'scope': 'scope', 'observed_at': 'timestamp', 'source_created_at': 'timestamp',
                              'original_turn_id': 'text', 'observed_turn_id': 'text'}, {}, path)
    elif kind == 'scope':
        object_fields(value, dict(kind='text', id='text', objective='text'), {}, path)
    elif kind == 'native_binding':
        object_fields(value, {**{key: 'text' for key in ('binding_id', 'goal_id', 'thread_id', 'run_id', 'objective')},
                              'scope': 'scope', 'goal_created_at': 'timestamp', 'observed_at': 'timestamp'}, {}, path)
    elif kind == 'source':
        object_fields(value, dict(source_kind='text', source_id='text', exact_quote='text', source_created_at='timestamp', original_turn_id='text'), {}, path)
    elif kind in {'file_inputs', 'review_targets', 'pinned_files', 'pinned_targets'}:
        if type(value) is not list:
            invalid('FIELD_TYPE', path, 'list required')
        for index, item in enumerate(value):
            child = f'{path}[{index}]'
            fields = dict(path='text', revision='text') if kind in {'file_inputs', 'pinned_files'} else dict(producer_id='task_id', revision='text')
            if kind == 'pinned_files':
                fields.update(sha256='string?', present='bool')
            if kind == 'pinned_targets':
                fields.update(output_hashes='hashes')
            object_fields(item, fields, {}, child)
    elif kind == 'hashes':
        validate_hashes(value, path)
    elif kind == 'app_goal':
        object_fields(value, {**{key: 'text' for key in ('goal_id', 'thread_id', 'status', 'source_kind', 'source_id')}, 'observed_at': 'timestamp'},
                      dict(objective='text', goal_created_at='timestamp'), path)
    elif kind == 'arguments':
        # Missing fields still reach the exact prepared-arguments equality guard.
        object_fields(value, {}, dict(task_name='task_id', fork_turns='text', message='string', model='text', reasoning_effort='text'), path)
    elif kind == 'context_pin':
        object_fields(value, dict(path='text', sha256='text', manifest_path='text', manifest_sha256='text'), {}, path)
        require(all(re.fullmatch('[a-f0-9]{64}', value[key]) for key in ('sha256', 'manifest_sha256')),
                'context and manifest SHA-256 required', path)
    elif kind == 'receipt':
        if type(value) not in (dict, list, str) or not value:
            invalid('FIELD_TYPE', path, 'nonempty raw tool receipt object, list or string required')
    elif kind == 'rate_limits':
        if type(value) is not dict:
            invalid('FIELD_TYPE', path, 'raw rateLimits object or null required')
        # Provider metadata remains opaque JSON; measurement fields are typed.
        for key in ('primary', 'secondary'):
            if key in value:
                check_field(value[key], 'window?', field_path(path, key))
    elif kind == 'window':
        if type(value) is not dict:
            invalid('FIELD_TYPE', path, 'usage window object or null required')
        for key, field_kind in dict(usedPercent='percent?', windowDurationMins='count?', resetsAt='timestamp?').items():
            if key in value:
                check_field(value[key], field_kind, field_path(path, key))
    else:
        raise RuntimeError('unknown contract kind: ' + kind)


AUTH_FIELDS = dict(authorization='authorization', interpretation='string', source_message='source')
INV_FIELDS = dict(inventory='inventory', observed_at='timestamp')
# Required, optional. Optional flags retain safe false defaults; absence is not a typed value.
COMMAND_FIELDS = {
    'activate': ({**AUTH_FIELDS, **INV_FIELDS, 'app_goal_observation': 'app_goal', 'objective': 'text', 'models': 'models', 'slot_limit': 'count'}, {'session_limit': 'count?'}),
    'bind-active-goal': ({**INV_FIELDS, 'native_binding': 'native_binding', 'interpretation': 'text', 'procedural_check': 'text',
                          'app_goal_observation': 'app_goal', 'objective': 'text', 'models': 'models', 'slot_limit': 'count'}, {'session_limit': 'count?'}),
    'observe-authorization': (AUTH_FIELDS, {'app_goal_observation': 'app_goal'}),
    'app-goal': ({'app_goal_observation': 'app_goal'}, {}),
    'usage': ({}, {'observed_at': 'timestamp', 'rateLimits': 'rate_limits?'}),
    'usage-due': ({}, {'substantial_work': 'bool'}),
    'inventory': (INV_FIELDS, dict(observation='inventory_observation')),
    'task': (dict(id='task_id', role='text', objective='text', criteria='criteria'), dict(file_inputs='file_inputs', context_refs='strings', review_targets='review_targets', revision='text', writes='strings', depends_on='task_ids', independent_review='bool')),
    'prepare': ({'id': 'task_id'}, {'effort': 'text', 'current_context': 'context_pin'}),
    'dispatch-start': (dict(id='task_id', intent_id='text'), {'current_context': 'context_pin'}),
    'record': (dict(id='task_id', submitted_arguments='arguments', session_id='text', receipt='receipt'), dict(effective_model='text?', effective_effort='text?')),
    'dispatch_failed': (dict(id='task_id', evidence='text'), {'certain_not_started': 'bool'}),
    'reconcile': (dict(id='task_id', evidence='text', no_owned_work_running='bool'),
                  dict(session_id='text', submitted_arguments='arguments', receipt='receipt', effective_model='text?', effective_effort='text?')),
    'finish': (dict(id='task_id', accepted='bool', evidence='text'), {'review_task': 'task_id'}),
    'cancel': (dict(id='task_id', reason='text'), {}),
    'stop': ({}, {}),
    'snapshot': (dict(writers_quiescent='bool', next_action='text'), {'files': 'strings'}),
    'restore': ({'destination': 'text'}, {}),
    'halt': ({**INV_FIELDS, 'owned_processes_stopped': 'bool', 'stop_evidence': 'text'}, dict(degraded='bool', reason='text', next_action='text')),
    'resume': ({**AUTH_FIELDS, 'app_goal_observation': 'app_goal'}, {}),
    'compact': ({}, {}), 'complete': ({}, {}), 'status': ({}, {}),
}


def validate_command(command, payload):
    if type(command) is not str or command not in COMMAND_FIELDS:
        invalid('COMMAND_UNKNOWN', '$.command', 'unknown command')
    json_value(payload)
    object_fields(payload, *COMMAND_FIELDS[command], '$')
    if command == 'halt' and payload.get('degraded', False):
        for key in ('reason', 'next_action'):
            if key not in payload:
                invalid('FIELD_REQUIRED', '$.' + key, 'degraded recovery reason and next action required')


def require(test, message, field='$.command'):
    if not test:
        raise Error(message, field=field)


def native_inventory(receipt):
    """Normalize only observed list_agents shapes; this is not authentication."""
    require(type(receipt) is dict and set(receipt) == {'agents'} and type(receipt['agents']) is list, 'raw list_agents receipt shape required')
    result = []
    for item in receipt['agents']:
        require(type(item) is dict and set(item) == {'agent_name', 'agent_status'}, 'raw agent identity/status required')
        check_field(item['agent_name'], 'text', '$.observation.receipt.agents[].agent_name')
        status = item['agent_status']
        if type(status) is str:
            require(status in {'running', 'idle', 'interrupted'}, 'unsupported native agent status')
            normalized = 'stopped' if status == 'interrupted' else status
        else:
            require(type(status) is dict and set(status) == {'completed'} and type(status['completed']) is str, 'unambiguous native completed status required')
            normalized = 'done'
        result.append(dict(id=item['agent_name'], status=normalized))
    require(len({item['id'] for item in result}) == len(result), 'duplicate native session')
    return result


def unlinked(path, boundary):
    """Reject symlinks/junctions before reading, writing or removing owned data."""
    if not path.is_relative_to(boundary):
        raise UnsafePath('path outside allowed directory')
    chain = []
    for item in [path, *path.parents]:
        chain.append(item)
        if item == boundary:
            break
    # Inspect ancestors first so checking a child never traverses a linked parent.
    for item in reversed(chain):
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            raise UnsafePath('linked paths unsupported')
    if not path.resolve().is_relative_to(boundary.resolve()):
        raise UnsafePath('resolved path escapes directory')


def remove_owned_tree(path, boundary):
    require(path != boundary and path.is_dir(), 'invalid cleanup target')
    unlinked(path, boundary)
    for child in path.rglob('*'):
        unlinked(child, boundary)
    shutil.rmtree(path)


def digest(path):
    if not path.is_file():
        return None
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def cut(fault, point, path):
    if fault is not None:
        fault(point, Path(path))


class FaultControl:
    """Explicit test-only cut points; enabled only inside a disposable package."""
    POINTS = {'temp.before', 'temp.after', 'replace.before', 'replace.after',
              'checkpoint.pointer.before', 'checkpoint.pointer.after',
              'prune.before', 'prune.after'}

    def __init__(self, root, spec):
        self.root = Path(root).resolve()
        unlinked(self.root, Path(self.root.anchor))
        unlinked(self.root / 'DISPOSABLE_FAULT_TEST.json', self.root)
        marker = read_json(self.root / 'DISPOSABLE_FAULT_TEST.json')
        require(type(marker) is dict and set(marker) == {'disposable'} and marker['disposable'] is True,
                'explicit disposable fault-test marker required')
        require(PACKAGE.is_relative_to(self.root) and (PACKAGE / 'SKILL.md').is_file()
                and all((PACKAGE / name).is_dir() for name in ('roles', 'scripts', 'references', 'agents')),
                'fault controls require a complete copied disposable package')
        object_fields(spec, dict(point='text', action='text'), dict(path_suffix='text'), '$.fault')
        require(spec['point'] in self.POINTS and spec['action'] in {'error', 'disk-full', 'truncated', 'pause'},
                'unsupported fault point or action')
        require(spec['action'] != 'truncated' or spec['point'] == 'temp.after', 'truncation requires temp.after')
        self.spec, self.fired = spec, False

    def __call__(self, point, path):
        require(path.resolve().is_relative_to(self.root), 'fault target outside disposable root')
        if self.fired or point != self.spec['point'] or not path.as_posix().endswith(self.spec.get('path_suffix', '')):
            return
        self.fired = True
        if self.spec['action'] == 'truncated':
            # Corrupt only the candidate; write_json validates it before replace.
            candidates = list(path.parent.glob(path.name + '.tmp-*'))
            require(len(candidates) == 1, 'one temporary candidate required')
            candidates[0].write_bytes(b'{"truncated":')
            return
        if self.spec['action'] == 'pause':
            (self.root / 'FAULT_REACHED.json').write_text(json.dumps({'point': point, 'path': str(path), 'pid': os.getpid()}), encoding='utf-8')
            while not (self.root / 'FAULT_RELEASE').exists():
                time.sleep(0.05)
            return
        raise OSError(errno.ENOSPC if self.spec['action'] == 'disk-full' else errno.EIO,
                      'injected ' + self.spec['action'] + ' at ' + point)


def json_file_bytes(data):
    """The exact UTF-8 bytes used for stored JSON, including platform newlines."""
    json_value(data)
    return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).replace('\n', os.linesep).encode('utf-8')


def write_json(path, data, fault=None):
    encoded = json_file_bytes(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        cut(fault, 'temp.before', path)
        temp.write_bytes(encoded)
        cut(fault, 'temp.after', path)
        # Detect a truncated candidate before it can replace the good copy.
        require(read_json(temp) == data, 'temporary JSON verification failed')
        cut(fault, 'replace.before', path)
        os.replace(temp, path)
        cut(fault, 'replace.after', path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path):
    return strict_json(path.read_text(encoding='utf-8-sig'))


def roles(codex_home, watcher_path=None):
    result = {}
    for path in sorted((PACKAGE / 'roles').glob('*.toml')):
        role = tomllib.loads(path.read_text(encoding='utf-8'))
        require(role['model_reasoning_effort'] in role['workflow']['efforts'], 'invalid default effort')
        require(path.stem != 'astra_coordinator', 'obsolete Astra role is prohibited')
        require(role['model'] == 'gpt-6.1-sol', 'user policy forbids non-Sol models, including Astra')
        require(role['model_reasoning_effort'] == 'high' and role['workflow']['efforts'] == ['high'], 'user policy requires Sol/high for non-limit roles')
        result[path.stem] = role
    watcher = Path(watcher_path) if watcher_path is not None else PACKAGE / 'private/limit-watcher/role.json'
    if watcher.exists():
        r = read_json(watcher)['roles']['limit_watcher']
        require(r['fixed_model'] == 'gpt-6-luna' and r['default_effort'] == 'low' and r['allowed_efforts'] == ['low', 'medium'], 'integrated limit role must preserve Luna low/medium')
        require(not Path(r['role_file']).is_absolute() and '..' not in Path(r['role_file']).parts, 'invalid watcher role path')
        instructions = (watcher.parent / r['role_file']).read_text(encoding='utf-8-sig')
        result['limit_watcher'] = dict(name='limit_watcher', description='Observe usage and request recovery.',
            model=r['fixed_model'], model_reasoning_effort=r['default_effort'],
            developer_instructions=instructions, workflow={'efforts': r['allowed_efforts']})
    return result


def usage_schedule(windows, observed_at, previous=None):
    """Bounded state, no model calls. Reserve 120 seconds for recovery at observed drain."""
    remaining = min(100 - w['usedPercent'] for w in windows.values())
    interval = 300 if remaining > 40 else 120 if remaining > 25 else 60 if remaining > 20 else 30
    horizon = None
    previous = previous or {}
    elapsed = observed_at - previous.get('at', observed_at)
    rates = {}
    for key, window in windows.items():
        old = previous.get('windows', {}).get(str(key), previous.get('windows', {}).get(key, {}))
        same_window = old and old.get('resetsAt') == window.get('resetsAt')
        delta = window['usedPercent'] - old.get('usedPercent', window['usedPercent'])
        prior_rate = previous.get('rates', {}).get(str(key), 0)
        rate = max(delta / elapsed, prior_rate * 0.5) if same_window and 0 < elapsed <= 600 and delta >= 0 else 0
        rates[str(key)] = rate
        if rate > 0:
            seconds = max(0, (90 - window['usedPercent']) / rate)
            horizon = seconds if horizon is None else min(horizon, seconds)
    if horizon is not None:
        interval = min(interval, max(1, int(horizon / 4)))
    early_stop = horizon is not None and horizon <= 120
    action = ('SAVE_AND_STOP' if remaining <= 10 or early_stop else
              'SERIAL_SHORT_WORK' if remaining <= 20 else
              'CHECKPOINT_SOON' if remaining <= 25 or (horizon is not None and horizon <= 600) else 'CONTINUE')
    return dict(at=observed_at, remaining=remaining, windows={str(key): value for key, value in windows.items()}, rates=rates,
                next_check_at=observed_at + interval, check_interval_seconds=interval,
                estimated_seconds_to_threshold=horizon, action=action)


def assignment_hash(task):
    contract = {key: task[key] for key in ('role', 'objective', 'criteria', 'revision', 'file_inputs', 'context_refs', 'review_targets', 'writes', 'depends_on', 'independent_review')}
    return hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def acceptance_dependencies(tasks, key):
    """Acceptance prerequisites only; review target edges are observation edges."""
    result = set()
    def visit(item):
        for dependency in tasks[item]['depends_on']:
            if dependency not in result:
                result.add(dependency)
                visit(dependency)
    visit(key)
    return result


def state_pin(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def validate_hashes(value, path):
    if type(value) is not dict:
        invalid('FIELD_TYPE', path, 'hash map required')
    folded = set()
    for name, sha in value.items():
        check_field(name, 'text', path)
        rel = Path(name)
        require(not rel.is_absolute() and '..' not in rel.parts and ':' not in name
                and rel.parts and rel.parts[0].casefold() not in {'.agent-workflow', '.agent-workflow-v1-0'}, 'unsafe stored file path', path)
        require(name == rel.as_posix() and name.casefold() not in folded, 'duplicate or nonnormalized stored path', path)
        folded.add(name.casefold())
        require(sha is None or type(sha) is str and re.fullmatch('[a-f0-9]{64}', sha), 'invalid stored hash', path)


def validate_state(s):
    """Exact stored types and references, independent of command freshness checks."""
    json_value(s)
    if type(s) is not dict:
        invalid('STATE_TYPE', '$', 'state object required')
    version = s.get('schema_version')
    if type(version) is not int or version != SCHEMA_VERSION:
        invalid('STATE_VERSION', '$.schema_version', 'schema_version 2 required; create a fresh approved v1.0 run')
    kinds = dict(status='text', objective='text', authorization='authorization', scope='scope', goal_id='text',
                 thread_id='text', run_id='text', app_goal_observation='app_goal', models='models',
                 slot_limit='count', session_limit='count?', created_at='timestamp', stopped_at='timestamp',
                 halted_at='timestamp', completed_at='timestamp', stop_evidence='text', next_action='text',
                 recovery_error='text', authorization_event_ids='strings', reserved_run_ids='strings', reserved_scope_ids='strings',
                 native_binding='native_binding', native_binding_ids='strings')
    compound = {'schema_version', 'tasks', 'inventory', 'usage', 'usage_errors', 'authorization_events',
                'checkpoint', 'previous_checkpoint', 'source_watermark', 'invalidations', 'native_binding_events'}
    for key in s:
        path = field_path('$', key)
        if key not in kinds and key not in compound:
            invalid('FIELD_UNKNOWN', path, 'unknown stored state field')
        if key in kinds:
            check_field(s[key], kinds[key], path)
    for key in ('status', 'tasks'):
        if key not in s:
            invalid('FIELD_REQUIRED', '$.' + key, 'required state field')
    require(s['status'] in {'inactive', 'active', 'stopping', 'halted', 'complete'}, 'invalid stored status')
    if type(s['tasks']) is not dict:
        invalid('FIELD_TYPE', '$.tasks', 'task object required')

    hashes = validate_hashes

    for key, task in s['tasks'].items():
        path = field_path('$.tasks', key)
        check_field(key, 'task_id', path)
        required = dict(role='text', objective='text', criteria='criteria', state='text')
        optional = dict(depends_on='task_ids', independent_review='bool', session_id='text',
                        prepared='arguments', effective_model='text?', effective_effort='text?', evidence='text',
                        receipt_hash='text', receipt='receipt', review='text')
        optional.update(revision='text', file_inputs='pinned_files', context_refs='strings', review_targets='pinned_targets', contract_invalid='bool', assignment_hash='text',
                        dispatch_intent='text', dispatch_prepared_at='timestamp', dispatch_failure='text', reconciliation='text')
        if type(task) is not dict:
            invalid('FIELD_TYPE', path, 'task object required')
        structural = {k: v for k, v in task.items() if k not in {'writes', 'output_hashes', 'reviewed_hashes', 'requested'}}
        object_fields(structural, required, optional, path)
        require(task['state'] in {'ready', 'prepared', 'running', 'uncertain', 'accepted', 'rejected'}, 'invalid stored task status', path)
        require('writes' in task, 'stored write hashes required', path + '.writes')
        hashes(task['writes'], path + '.writes')
        for name in ('output_hashes',):
            if name in task:
                hashes(task[name], path + '.' + name)
        if 'receipt_hash' in task:
            require(re.fullmatch('[a-f0-9]{64}', task['receipt_hash']), 'invalid receipt hash', path)
        if 'requested' in task:
            object_fields(task['requested'], dict(model='text', reasoning_effort='text'), {}, path + '.requested')
        if 'reviewed_hashes' in task:
            require(type(task['reviewed_hashes']) is dict, 'review hash map required', path)
            for reviewed, values in task['reviewed_hashes'].items():
                require(reviewed in s['tasks'], 'unknown reviewed task', path)
                hashes(values, path + '.reviewed_hashes')
        require(all(dep in s['tasks'] for dep in task.get('depends_on', [])), 'unknown stored dependency', path)
        require(all(k in task for k in ('revision', 'file_inputs', 'context_refs', 'review_targets', 'depends_on', 'independent_review', 'assignment_hash')), 'incomplete current task', path)
        require(re.fullmatch('[a-f0-9]{64}', task['assignment_hash']), 'invalid assignment hash', path)
        paths = [item['path'].casefold() for item in task['file_inputs']]
        targets = [item['producer_id'] for item in task['review_targets']]
        require(len(paths) == len(set(paths)) and len(targets) == len(set(targets)), 'duplicate typed input', path)
        for item in task['file_inputs']:
            hashes({item['path']: item['sha256']}, path + '.file_inputs')
            require(item['present'] == (item['sha256'] is not None), 'presence/hash mismatch', path)
        require(all(target in s['tasks'] and target != key for target in targets), 'unknown review target', path)
        require(not targets or task['role'] == 'verifier', 'only verifier may claim review targets', path)
        require(task['state'] != 'prepared' or 'prepared' in task, 'prepared arguments required', path)
        require(task['state'] != 'running' or all(k in task for k in ('session_id', 'requested', 'receipt_hash')), 'running receipt identity required', path)
        require(task['state'] != 'accepted' or all(k in task for k in ('evidence', 'output_hashes')), 'accepted output evidence required', path)
    visited, visiting = set(), set()
    def visit(key):
        require(key not in visiting, 'cyclic stored task references')
        if key in visited:
            return
        visiting.add(key)
        task = s['tasks'][key]
        require(len(task['depends_on']) == len(set(task['depends_on'])), 'duplicate stored dependency')
        for other in task['depends_on'] + [pin['producer_id'] for pin in task['review_targets']]:
            visit(other)
        visiting.remove(key)
        visited.add(key)
    for key in s['tasks']:
        visit(key)
    for key, task in s['tasks'].items():
        targets = {pin['producer_id'] for pin in task['review_targets']}
        require(not targets.intersection(acceptance_dependencies(s['tasks'], key)),
                'review cannot depend on the acceptance it reviews', field_path('$.tasks', key))
    if 'inventory' in s:
        object_fields(s['inventory'], dict(items='inventory', at='timestamp'), dict(observation='inventory_observation'), '$.inventory')
    if 'usage_errors' in s:
        require(type(s['usage_errors']) is int and s['usage_errors'] >= 0, 'nonnegative usage error integer required')
    if 'usage' in s:
        usage = s['usage']
        require(type(usage) is dict, 'usage object required')
        object_fields({k: v for k, v in usage.items() if k not in {'windows', 'rates'}},
                      dict(at='timestamp', remaining='percent', next_check_at='timestamp', check_interval_seconds='count',
                           estimated_seconds_to_threshold='timestamp?', action='text'), {}, '$.usage')
        require(type(usage.get('windows')) is dict and type(usage.get('rates')) is dict, 'usage maps required')
        for value in usage['windows'].values():
            check_field(value, 'window', '$.usage.windows')
        for value in usage['rates'].values():
            check_field(value, 'timestamp', '$.usage.rates')
    for name in ('checkpoint', 'previous_checkpoint'):
        if name in s:
            object_fields(s[name], dict(id='text', verified='bool', manifest_hash='text'), {}, '$.' + name)
            require(re.fullmatch('[a-f0-9]{32}', s[name]['id']) and re.fullmatch('[a-f0-9]{64}', s[name]['manifest_hash']), 'invalid checkpoint pointer')
    events = s.get('authorization_events', [])
    require(type(events) is list, 'authorization event list required')
    for index, entry in enumerate(events):
        object_fields(entry, dict(event='authorization', interpretation='string', source_message='source'), {}, f'$.authorization_events[{index}]')
        require(entry['source_message'] == {k: entry['event'][k] for k in SOURCE_FIELDS}, 'stored source projection mismatch')
    ids = s.get('authorization_event_ids', [])
    require(len(ids) == len(set(ids)) and all(entry['event']['event_id'] in ids for entry in events), 'inconsistent authorization replay ledger')
    sources = [entry['event']['source_id'] for entry in events]
    require(len(sources) == len(set(sources)) and len({entry['event']['event_id'] for entry in events}) == len(events), 'duplicate authorization replay identity')
    bindings = s.get('native_binding_events', [])
    binding_ids = s.get('native_binding_ids', [])
    require(type(bindings) is list, 'native binding ledger list required')
    require(len(binding_ids) == len(set(binding_ids)) and not set(binding_ids).intersection(ids), 'duplicate native binding replay identity')
    require(len(bindings) == len(binding_ids) and len(bindings) <= 1, 'native binding replay ledger mismatch')
    if bindings:
        require(s['status'] != 'inactive', 'native binding contradicts inactive state')
        for name in ('reserved_run_ids', 'reserved_scope_ids'):
            require(len(s.get(name, [])) == len(set(s.get(name, []))), 'duplicate native run or scope reservation')
    for index, entry in enumerate(bindings):
        object_fields(entry, dict(binding='native_binding', interpretation='text', procedural_check='text', app_goal_observation='app_goal'), {}, f'$.native_binding_events[{index}]')
        binding, observation = entry['binding'], entry['app_goal_observation']
        validate_native_binding(binding, observation)
        require(observation['status'] == 'active' and observation['observed_at'] == binding['observed_at'], 'initial native binding observation mismatch')
        require(binding['binding_id'] == binding_ids[index] and binding['run_id'] in s.get('reserved_run_ids', [])
                and binding['scope']['id'] in s.get('reserved_scope_ids', []), 'native binding reservation mismatch')
    require(len({entry['binding']['run_id'] for entry in bindings}) == len(bindings)
            and len({entry['binding']['scope']['id'] for entry in bindings}) == len(bindings), 'duplicate native binding run or scope')
    if 'native_binding' in s:
        require('authorization' not in s and any(entry['binding'] == s['native_binding'] for entry in bindings), 'current native binding must match its distinct provenance ledger')
        require(s['status'] != 'inactive', 'inactive helper cannot retain a current native binding')
        require(s['native_binding']['observed_at'] <= s.get('created_at', -1), 'native binding later than helper creation')
        if 'app_goal_observation' in s:
            validate_native_binding(s['native_binding'], s['app_goal_observation'])
    if type(s.get('authorization')) is dict:
        current = s['authorization']
        require(all(entry['event'] == current for entry in events if entry['event']['event_id'] == current['event_id']),
                'current authorization contradicts same-ID replay ledger', '$.authorization')
    for event in [entry['event'] for entry in events] + ([s['authorization']] if type(s.get('authorization')) is dict else []):
        require(event['kind'] in AUTH_KINDS and event['source_kind'] in {'user_message', 'user_goal_continuation'}
                and event['scope']['kind'] in {'objective', 'report'}, 'invalid stored authorization classification')
        if 'source_created_at' in event:
            require(event['source_created_at'] <= event['observed_at'], 'source chronology contradicts observation')
    if 'source_watermark' in s:
        object_fields(s['source_watermark'], dict(source_created_at='timestamp', original_turn_id='text', source_id='text'), {}, '$.source_watermark')
        require(events and s['source_watermark'] == {k: events[-1]['event'][k] for k in ('source_created_at', 'original_turn_id', 'source_id')}, 'source watermark mismatch')
    if events:
        require('source_watermark' in s, 'source watermark required')
        for previous, current in zip(events, events[1:]):
            before, after = previous['event'], current['event']
            require(after['source_created_at'] > before['source_created_at'] or
                    after['source_created_at'] == before['source_created_at'] and after['original_turn_id'] == before['original_turn_id'],
                    'source chronology contradiction')
    if 'invalidations' in s:
        require(type(s['invalidations']) is list, 'invalidation receipt list required')
        for item in s['invalidations']:
            object_fields(item, dict(task_id='task_id', revision='text', reason='text', path='string', expected='string?', actual='string?', affected_task_ids='task_ids', at='timestamp'), {}, '$.invalidations')
            require(all(key in s['tasks'] and s['tasks'][key].get('contract_invalid') for key in item['affected_task_ids']), 'invalidation task mismatch')
    if 'app_goal_observation' in s:
        require(s['app_goal_observation']['status'] in {'active', 'paused', 'blocked', 'complete', 'stopped'}
                and s['app_goal_observation']['source_kind'] == 'get_goal', 'invalid stored app goal observation')
    if s['status'] != 'inactive':
        require(all(k in s for k in ('objective', 'scope', 'goal_id', 'thread_id', 'run_id', 'app_goal_observation',
                                    'models', 'slot_limit', 'inventory', 'created_at', 'authorization_events', 'authorization_event_ids')),
                'incomplete operational state')
        basis = s.get('authorization', s.get('native_binding'))
        require(type(basis) is dict and all(basis[key] == s[key] for key in ('goal_id', 'thread_id', 'run_id'))
                and basis['scope'] == s['scope'], 'stored authorization identity or replay ledger mismatch')
        if s['status'] == 'active':
            authorized_at = basis['source_created_at'] if 'authorization' in s else basis['observed_at']
            boundaries = [s[key] for key in ('stopped_at', 'halted_at') if key in s]
            # A delayed but accepted stop consumes the current basis when observed.
            # Preserve its original source timestamp separately for replay checks.
            boundaries.extend(entry['event']['observed_at'] for entry in events
                              if entry['event']['kind'] == 'explicit_stop'
                              and all(entry['event'][key] == s[key] for key in ('goal_id', 'thread_id', 'run_id'))
                              and entry['event']['scope'] == s['scope'])
            require(all(authorized_at > boundary for boundary in boundaries),
                    'active authorization must be later than retained stop/halt history')
        if 'authorization' in s:
            require(s['authorization']['event_id'] in ids and any(entry['event'] == s['authorization'] for entry in events), 'active authorization must match current provenance ledger')
        else:
            require(s['objective'] == basis['objective'], 'native binding helper objective mismatch')


def validate_native_binding(binding, observation):
    """Stored native provenance remains checked even after a human replaces its basis."""
    require(binding['scope']['kind'] == 'objective' and binding['scope']['objective'] == binding['objective'], 'native binding scope must be exactly its objective')
    require(binding['goal_created_at'] <= binding['observed_at'], 'native goal creation later than observation')
    require(all(observation.get(key) == binding[key] for key in ('goal_id', 'thread_id', 'objective', 'goal_created_at')),
            'native goal identity, objective or creation mismatch')
    require(observation['source_kind'] == 'get_goal' and observation['observed_at'] >= binding['observed_at'], 'native binding get_goal provenance or chronology mismatch')


class Workflow:
    def __init__(self, project, codex_home, clock=time.time, fault=None, *, state_base=None, role_catalog=None, provenance_guard=None, run_id=None):
        self.root = Path(project).resolve()
        require(self.root.is_dir(), 'project must exist')
        self.home = Path(codex_home).resolve()
        self.base = Path(state_base) if state_base is not None else self.root / '.agent-workflow-v1-0'
        require(self.base.is_relative_to(self.root), 'state namespace outside project')
        unlinked(self.base, self.root)
        self.provenance_guard = provenance_guard
        self.run_id = run_id
        self.path = self.base / 'state.json'
        self.clock = clock
        self.catalog = roles(self.home) if role_catalog is None else role_catalog
        self.fault = fault

    def internal(self, *parts):
        path = self.base.joinpath(*parts)
        unlinked(path, self.root)
        return path

    def file(self, name):
        rel = Path(name)
        require(not rel.is_absolute() and '..' not in rel.parts and rel.parts, 'relative file path required')
        require(':' not in str(rel) and rel.parts[0].casefold() not in {'.agent-workflow', '.agent-workflow-v1-0'}, 'reserved path')
        require(all(part == part.rstrip(' .') and not re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part) for part in rel.parts), 'Windows-reserved path')
        path = self.root / rel
        unlinked(path, self.root)
        if path.is_dir():
            raise UnsafePath('claim exact files, not directories')
        try:
            info = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if info.st_nlink > 1:
                raise UnsafePath('hard-linked files unsupported')
        return path

    def inventory(self, p):
        age = self.clock() - p['observed_at']
        require(0 <= age <= 60, 'fresh observed inventory required', '$.observed_at')
        items = p['inventory']
        require(isinstance(items, list), 'inventory must be a list')
        ids = [a['id'] for a in items]
        require(len(ids) == len(set(ids)), 'duplicate session', '$.inventory')
        require(all(a['status'] in ACTIVE | {'idle', 'stopped', 'done'} for a in items), 'unknown inventory status')
        result = {'items': items, 'at': p['observed_at']}
        if 'observation' in p:
            result['observation'] = p['observation']
            self.check_inventory_observation(result)
        return result

    def check_inventory_observation(self, inv):
        if 'observation' not in inv:
            return
        observation = inv['observation']
        if 'receipt' in observation:
            receipt = observation['receipt']
        else:
            path = self.file(observation['evidence_path'])
            require(digest(path) == observation['sha256'], 'native inventory evidence pin changed')
            receipt = read_json(path)
        projected = native_inventory(receipt)
        require(sorted(projected, key=lambda item: item['id']) == sorted(inv['items'], key=lambda item: item['id']), 'raw native inventory disagrees with normalized items')

    def live(self, s):
        inv = s['inventory']
        require(0 <= self.clock() - inv['at'] <= 60, 'refresh inventory')
        self.check_inventory_observation(inv)
        items = {a['id']: a['status'] for a in inv['items']}
        for task in s['tasks'].values():
            if task.get('session_id') and task['state'] not in TERMINAL:
                items.setdefault(task['session_id'], 'unknown')
        return items

    def independent_acceptance(self, s, key):
        """Historical incomplete records remain readable, but cannot grant independence."""
        task = s['tasks'][key]
        review = s['tasks'].get(task.get('review'), {})
        return bool(task['state'] == 'accepted' and not task.get('contract_invalid')
                    and task.get('session_id') and review.get('session_id')
                    and task['session_id'] != review['session_id']
                    and review.get('state') == 'accepted' and review.get('role') == 'verifier'
                    and not review.get('contract_invalid')
                    and any(pin['producer_id'] == key and pin['revision'] == task['revision']
                            and pin['output_hashes'] == task.get('output_hashes')
                            and pin['output_hashes'] == review.get('reviewed_hashes', {}).get(key)
                            for pin in review.get('review_targets', [])))

    def dependency_accepted(self, s, key):
        task = s['tasks'][key]
        return (task['state'] == 'accepted' and not task.get('contract_invalid')
                and (not task['independent_review'] or self.independent_acceptance(s, key)))

    def task_observations(self, s):
        """Versioned read-only projection; native termination never accepts output."""
        validate_state(s)
        inv = s.get('inventory', {})
        fresh = 0 <= self.clock() - inv.get('at', -math.inf) <= 60
        if fresh:
            try:
                self.check_inventory_observation(inv)
            except Error:
                fresh = False
        observed = {item['id']: item['status'] for item in inv.get('items', [])} if fresh else {}
        result = {}
        for key, task in sorted(s['tasks'].items()):
            session = task.get('session_id')
            native = (observed.get(session, 'unknown') if session else
                      'not_started' if task['state'] in {'ready', 'prepared'} else 'unknown')
            acceptance = task['state'] if task['state'] in TERMINAL else 'pending'
            if task.get('contract_invalid'):
                acceptance = 'invalidated'
            review_id = task.get('review')
            independent = self.independent_acceptance(s, key)
            result[key] = dict(revision=task['revision'], execution=dict(state=native, session_id=session,
                observed_at=inv.get('at') if session else None, fresh=fresh if session else None),
                acceptance=dict(state=acceptance, independent=bool(independent), review_task=review_id,
                    basis='independent_review' if independent else
                          'historical_unrecorded' if acceptance == 'accepted' and task['independent_review'] else 'coordinator'),
                write_ownership='retained' if task['state'] not in TERMINAL and task['writes'] else 'released')
        return dict(contract_version=1, tasks=result)

    def render_context(self, s, references, as_of):
        """Selective data only. References pin evidence; bodies and logs stay external."""
        validate_state(s)
        check_field(as_of, 'timestamp', '$.as_of')
        require(type(references) is list and len(references) <= 256, 'bounded evidence references required')
        blockers, refs, seen = [], [], set()
        def block(code, task_id=None, path=None, detail=None):
            item = dict(code=code)
            if task_id is not None: item['task_id'] = task_id
            if path is not None: item['path'] = path
            if detail is not None: item['detail'] = detail
            if item not in blockers: blockers.append(item)
        for index, ref in enumerate(references):
            object_fields(ref, dict(path='text', sha256='text', kind='text'), dict(task_id='task_id'), f'$.references[{index}]')
            require(ref['kind'] in {'evidence', 'counterevidence', 'decision'}, 'unsupported reference kind')
            require(re.fullmatch('[a-f0-9]{64}', ref['sha256']), 'evidence SHA-256 required')
            require(ref['path'].casefold() not in seen, 'duplicate evidence reference')
            require('task_id' not in ref or ref['task_id'] in s['tasks'], 'unknown evidence task')
            seen.add(ref['path'].casefold())
            try:
                actual = self.observed_digest(ref['path'])
            except UnsafePath as exc:
                actual = 'unsafe: ' + str(exc)
            status = 'current' if actual == ref['sha256'] else 'missing' if actual is None else 'stale'
            refs.append(dict(ref, status=status))
            if status != 'current': block('EVIDENCE_' + status.upper(), ref.get('task_id'), ref['path'])
            if ref['kind'] == 'counterevidence': block('COUNTEREVIDENCE', ref.get('task_id'), ref['path'])
        basis = s.get('authorization', s.get('native_binding'))
        if not basis: block('AUTHORITY_MISSING')
        goal = s.get('app_goal_observation')
        if not goal or not 0 <= as_of - goal['observed_at'] <= 60: block('GOAL_OBSERVATION_STALE_OR_MISSING')
        elif goal['status'] != 'active': block('GOAL_NOT_ACTIVE')
        if s['status'] != 'active': block('RUN_NOT_ACTIVE')
        usage = s.get('usage', {})
        if not (0 <= as_of - usage.get('at', -math.inf) <= 300
                and as_of < usage.get('next_check_at', -math.inf) and usage.get('remaining', 0) > 10):
            block('USAGE_STALE_OR_UNAVAILABLE')
        inv = s.get('inventory', {})
        if not 0 <= as_of - inv.get('at', -math.inf) <= 60: block('INVENTORY_STALE_OR_MISSING')
        try:
            if inv: self.check_inventory_observation(inv)
        except Error as exc:
            block('NATIVE_INVENTORY_UNSUPPORTED', detail=str(exc))
        # On a copy only: discover the same invalidations as the guarded helper,
        # without mutating live state or reviving any earlier rejected contract.
        checked = strict_json(json.dumps(s, allow_nan=False))
        artifacts, tasks = [], []
        require(len(s['tasks']) <= 256, 'bounded current task set required')
        for key, task in sorted(s['tasks'].items()):
            try:
                self.check_inputs(checked, key, 'context')
                if 'output_hashes' in task:
                    self.output_hashes(checked, key, 'context: output', task['output_hashes'])
            except Error as exc:
                block('TASK_CONTRACT_BLOCKED', key, detail=str(exc))
            if task.get('contract_invalid'): block('CONTRACT_INVALIDATED', key)
            if task['state'] == 'rejected': block('REJECTED_TASK', key)
            if task['state'] == 'uncertain': block('DISPATCH_UNCERTAIN', key)
            for dependency in task['depends_on']:
                if not self.dependency_accepted(checked, dependency):
                    block('DEPENDENCY_NOT_ACCEPTED', key, detail=dependency)
            for ref in task['context_refs']:
                if ref.casefold() not in seen: block('CONTEXT_REFERENCE_UNPINNED', key, ref)
            if task['state'] in TERMINAL and not any(ref.get('task_id') == key for ref in refs):
                block('RESULT_EVIDENCE_UNSUPPORTED', key)
            for name, before in sorted(task['writes'].items()):
                try: actual = self.observed_digest(name)
                except UnsafePath as exc: actual = 'unsafe: ' + str(exc)
                artifacts.append(dict(task_id=key, revision=task['revision'], path=name, base_sha256=before,
                    output_sha256=task.get('output_hashes', {}).get(name), current_sha256=actual, changed=actual != before))
            if task['state'] != 'accepted' or checked['tasks'][key].get('contract_invalid'):
                tasks.append({**dict(id=key), **{name: task[name] for name in
                    ('role', 'objective', 'revision', 'criteria', 'depends_on', 'review_targets', 'file_inputs', 'context_refs')}})
        for receipt in checked.get('invalidations', []):
            block('INVALIDATION_COUNTEREVIDENCE', receipt['task_id'], receipt['path'] or None,
                  dict(revision=receipt['revision'], reason=receipt['reason'], expected=receipt['expected'], actual=receipt['actual'], at=receipt['at']))
        next_action = s.get('next_action')
        if not next_action:
            next_action = 'Resolve listed blockers using pinned references.' if blockers else 'Select an open task and use guarded prepare/dispatch-start.'
        # Projection freshness is evaluated at the declared observation time.
        projected = Workflow(self.root, self.home, lambda: as_of, state_base=self.base, role_catalog=self.catalog, provenance_guard=self.provenance_guard, run_id=self.run_id).task_observations(checked)
        return dict(context_version=1, as_of=as_of, state_sha256=state_pin(s),
            source_sha256=digest(Path(__file__)), objective=s.get('objective'), status=s['status'],
            identity={k: s.get(k) for k in ('goal_id', 'thread_id', 'run_id')}, authority=basis,
            app_goal_observation=goal, open_tasks=tasks, task_observations=projected,
            blockers=sorted(blockers, key=lambda item: json.dumps(item, sort_keys=True)),
            artifact_pins=artifacts, evidence_references=refs, next_action=next_action,
            dispatch_authorized=False, cache_status='unknown', token_measurement='pending_N08')

    def check_context(self, s, pin, task_id):
        # Reference inventory is an immutable typed input, not a list that can
        # be shortened inside a repinned summary to hide counterevidence.
        task = s['tasks'][task_id]
        require(any(item['path'] == pin['manifest_path'] and item['sha256'] == pin['manifest_sha256']
                    and item['present'] for item in task['file_inputs']), 'context manifest must be a pinned task input')
        manifest = self.file(pin['manifest_path'])
        require(digest(manifest) == pin['manifest_sha256'], 'current context manifest pin changed')
        summary_path = self.file(pin['path'])
        require(digest(summary_path) == pin['sha256'], 'current context pin changed')
        summary = read_json(summary_path)
        require(type(summary) is dict and type(summary.get('context_version')) is int
                and summary['context_version'] == 1, 'current context version required')
        at = summary.get('as_of')
        check_field(at, 'timestamp', '$.current_context.as_of')
        require(0 <= self.clock() - at <= 60, 'current context observation stale')
        expected = self.render_context(s, read_json(manifest), at)
        require(summary == expected, 'current context contradicts source/state; never relabel timestamps or omit blockers')
        require(not summary['blockers'], 'current context has unresolved blockers')
        return pin

    def usable(self, s):
        require(s['status'] == 'active', 'run is not active')
        basis = s.get('authorization', s.get('native_binding'))
        require(isinstance(basis, dict) and s.get('app_goal_observation', {}).get('status') == 'active',
                'typed authorization and active app goal observation required')
        require(all(basis.get(k) == s.get(k) for k in ('goal_id', 'thread_id', 'run_id')),
                'stored authorization identity mismatch')
        self.app_goal(s, basis)
        u = s.get('usage', {})
        require(0 <= self.clock() - u.get('at', 0) <= 300 and self.clock() < u.get('next_check_at', u.get('at', 0) + 300) and u.get('remaining', 0) > 10, 'fresh usage above threshold required')
        require(not any(t['state'] == 'uncertain' for t in s['tasks'].values()), 'reconcile uncertain dispatch first')

    def authorization_event(self, p, s, allowed, after=None, matching=False):
        """Validate supplied evidence structure; main authenticates and classifies the source."""
        event = p.get('authorization')
        require(isinstance(event, dict), 'typed authorization event required')
        for key in ('event_id', 'exact_quote', 'source_id', 'goal_id', 'thread_id', 'run_id'):
            require(isinstance(event.get(key), str) and bool(event[key].strip()), 'authorization ' + key + ' required')
        require(isinstance(event.get('kind'), str) and event['kind'] in AUTH_KINDS and event['kind'] in allowed, 'event kind cannot authorize this transition')
        require(isinstance(event.get('source_kind'), str) and event['source_kind'] in {'user_message', 'user_goal_continuation'}, 'direct user source required')
        require(isinstance(p.get('interpretation'), str), 'separate main interpretation required')
        source = p.get('source_message')
        require(isinstance(source, dict) and all(source.get(k) == event[k] for k in SOURCE_FIELDS),
                'exact quote must equal separate source message; never append interpretation')
        at = event.get('observed_at')
        require(isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at)
                and 0 <= self.clock() - at <= 300, 'fresh authorization observation required')
        created = event['source_created_at']
        require(0 <= self.clock() - created <= 300 and created <= at, 'fresh original source required; source cannot be later than its observation')
        require(after is None or created > after, 'continuation source must be later than stop/halt')
        watermark = s.get('source_watermark')
        require(not watermark or created > watermark['source_created_at'] or
                created == watermark['source_created_at'] and event['original_turn_id'] == watermark['original_turn_id'],
                'source older than accepted chronology watermark')
        require(event['event_id'] not in [*s.get('authorization_event_ids', []), *s.get('native_binding_ids', [])], 'authorization event reused')
        require(not any(entry['event']['source_id'] == event['source_id']
                        for entry in s.get('authorization_events', [])), 'authorization source message reused')
        scope = event.get('scope')
        require(isinstance(scope, dict) and isinstance(scope.get('kind'), str) and scope['kind'] in {'objective', 'report'}
                and all(isinstance(scope.get(k), str) and scope[k].strip() for k in ('id', 'objective')), 'explicit authorization scope required')
        if event['kind'] == 'independent_report_request':
            require(scope['kind'] == 'report', 'report request authorizes only report scope')
        if matching:
            require(all(event[k] == s.get(k) for k in ('goal_id', 'thread_id', 'run_id')) and scope == s.get('scope'),
                    'authorization does not match halted objective identity and scope')
        return json.loads(json.dumps(event))

    def app_goal(self, p, event, after=None):
        observation = p.get('app_goal_observation')
        require(isinstance(observation, dict), 'separate app goal observation required')
        if 'binding_id' in event:
            validate_native_binding(event, observation)
        require(all(observation.get(k) == event[k] for k in ('goal_id', 'thread_id')), 'app goal identity mismatch')
        require(isinstance(observation.get('status'), str) and observation['status'] in {'active', 'paused', 'blocked', 'complete', 'stopped'}, 'invalid app goal status')
        require(observation.get('source_kind') == 'get_goal' and isinstance(observation.get('source_id'), str)
                and observation['source_id'].strip(), 'app goal tool observation provenance required')
        at = observation.get('observed_at')
        require(isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at)
                and 0 <= self.clock() - at <= 60 and at >= event['observed_at']
                and (after is None or at > after), 'fresh app goal observation required')
        return json.loads(json.dumps(observation))

    def remember_authorization(self, s, event, interpretation, source):
        s.setdefault('authorization_event_ids', []).append(event['event_id'])
        s.setdefault('authorization_events', []).append({'event': event, 'interpretation': interpretation, 'source_message': source})
        s['source_watermark'] = {k: event[k] for k in ('source_created_at', 'original_turn_id', 'source_id')}

    def pin_files(self, inputs):
        result, seen = [], set()
        for item in inputs:
            path = self.file(item['path'])
            name = path.relative_to(self.root).as_posix()
            require(name.casefold() not in seen, 'duplicate case-fold file input', '$.file_inputs')
            seen.add(name.casefold())
            sha = digest(path)
            result.append(dict(path=name, revision=item['revision'], sha256=sha, present=sha is not None))
        return result

    def pin_targets(self, s, targets):
        result, seen = [], set()
        live = self.live(s) if targets else {}
        for item in targets:
            key = item['producer_id']
            require(key in s['tasks'] and key not in seen, 'unknown or duplicate review target', '$.review_targets')
            seen.add(key)
            target = s['tasks'][key]
            require(item['revision'] == target['revision'] and not target.get('contract_invalid'), 'review target revision invalid')
            require(target.get('session_id') and live.get(target['session_id']) in {'idle', 'done', 'stopped'}, 'review target must stop writing first')
            result.append(dict(producer_id=key, revision=item['revision'], output_hashes=self.output_hashes(s, key, 'assign review')))
        return result

    def invalidate(self, s, key, reason, path='', expected=None, actual=None):
        affected = {key}
        if path:
            # Seed only contracts whose own pins fail this observation. A newer
            # revision already pinned to current bytes must survive an old pin.
            unsafe = isinstance(actual, str) and actual.startswith('unsafe: ')
            for consumer, task in s['tasks'].items():
                for item in task['file_inputs']:
                    if item['path'].casefold() == path.casefold() and (
                            unsafe or item['sha256'] != actual or item['present'] != (actual is not None)
                            or assignment_hash(task) != task['assignment_hash']):
                        affected.add(consumer)
        changed = True
        while changed:
            changed = False
            for consumer, task in s['tasks'].items():
                if consumer in affected:
                    continue
                linked = set(task['depends_on']) | {target['producer_id'] for target in task['review_targets']}
                if linked & affected:
                    affected.add(consumer)
                    changed = True
        receipt = dict(task_id=key, revision=s['tasks'][key]['revision'], reason=reason, path=path,
                       expected=expected, actual=actual, affected_task_ids=sorted(affected), at=self.clock())
        for consumer in affected:
            s['tasks'][consumer]['contract_invalid'] = True
        s.setdefault('invalidations', []).append(receipt)
        raise InputInvalidation(receipt)

    def observed_digest(self, name):
        """Inspect safety before hashing; do not translate unrelated I/O faults."""
        try:
            return digest(self.file(name))
        except OSError as exc:
            if exc.errno not in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EACCES, errno.EPERM}:
                raise
            raise UnsafePath('file cannot be safely observed (errno=' + str(exc.errno) + ')') from exc

    def output_hashes(self, s, key, gate, expected=None):
        actual = {}
        for name in s['tasks'][key]['writes']:
            try:
                actual[name] = self.observed_digest(name)
            except UnsafePath as exc:
                self.invalidate(s, key, gate + ': output path unsafe', name,
                                expected.get(name) if expected is not None else None, 'unsafe: ' + str(exc))
        if expected is not None and actual != expected:
            name = next(n for n in sorted(set(actual) | set(expected)) if actual.get(n) != expected.get(n))
            self.invalidate(s, key, gate + ' changed', name, expected.get(name), actual.get(name))
        return actual

    def check_inputs(self, s, key, gate):
        task = s['tasks'][key]
        require(not task.get('contract_invalid'), 'task requires explicit new assignment after invalidation')
        actual_assignment = assignment_hash(task)
        if actual_assignment != task['assignment_hash']:
            self.invalidate(s, key, gate + ': assignment revision or contract replaced', expected=task['assignment_hash'], actual=actual_assignment)
        for item in task['file_inputs']:
            try:
                actual = self.observed_digest(item['path'])
            except UnsafePath as exc:
                self.invalidate(s, key, gate + ': input path unsafe', item['path'], item['sha256'], 'unsafe: ' + str(exc))
            if actual != item['sha256'] or item['present'] != (actual is not None):
                self.invalidate(s, key, gate + ': file input changed', item['path'], item['sha256'], actual)
        for pin in task['review_targets']:
            target = s['tasks'][pin['producer_id']]
            if target['revision'] != pin['revision'] or target.get('contract_invalid'):
                self.invalidate(s, pin['producer_id'], gate + ': producer revision changed', expected=pin['revision'], actual=target['revision'])
            # Revalidate the producer's own assignment while reviewing its output.
            self.check_inputs(s, pin['producer_id'], gate + ': review producer')
            self.output_hashes(s, pin['producer_id'], gate + ': review target', pin['output_hashes'])
            require(self.live(s).get(target.get('session_id')) in {'idle', 'done', 'stopped'}, 'review target must stop writing first')
        for dependency in task['depends_on']:
            target = s['tasks'][dependency]
            require(not target.get('contract_invalid'), 'dependency invalidated')
            require(target['state'] != 'accepted' or self.dependency_accepted(s, dependency),
                    'dependency lacks recorded independent acceptance')
            self.check_inputs(s, dependency, gate + ': dependency')
            self.output_hashes(s, dependency, gate + ': dependency artifacts', target.get('output_hashes'))

    def verify_checkpoint(self, s, current=False):
        cp = s.get('checkpoint')
        require(cp and cp.get('verified'), 'verified checkpoint required')
        require(re.fullmatch('[a-f0-9]{32}', cp['id']), 'invalid checkpoint identity')
        directory = self.internal('checkpoints', cp['id'])
        for child in directory.rglob('*'):
            unlinked(child, self.root)
        require(digest(directory / 'manifest.json') == cp['manifest_hash'], 'checkpoint manifest changed')
        manifest = read_json(directory / 'manifest.json')
        require(digest(directory / 'state.json') == manifest['state_hash'], 'checkpoint state changed')
        for name, expected in manifest['files'].items():
            self.file(name)
            require(digest(directory / 'files' / name) == expected, 'checkpoint file mismatch: ' + name)
            if current:
                require(digest(self.file(name)) == expected, 'project changed after checkpoint: ' + name)
        return directory, manifest

    def prune_checkpoints(self, s):
        keep = {s.get(k, {}).get('id') for k in ('checkpoint', 'previous_checkpoint')}
        root = self.internal('checkpoints')
        removed = 0
        if root.exists():
            for directory in root.iterdir():
                if directory.name in keep or not re.fullmatch('[a-f0-9]{32}', directory.name):
                    continue
                unlinked(directory, self.root)
                marker = directory / 'owner.json'
                if marker.is_file() and read_json(marker) == {'owner': 'agent-workflow-v1-0', 'project': str(self.root)}:
                    cut(self.fault, 'prune.before', directory)
                    remove_owned_tree(directory, root)
                    cut(self.fault, 'prune.after', directory)
                    removed += 1
        return removed

    def execute(self, command, p):
        if self.provenance_guard:
            self.provenance_guard()
        validate_command(command, p)
        if self.run_id is not None:
            for key in ('authorization', 'native_binding'):
                if key in p:
                    require(p[key]['run_id'] == self.run_id, 'candidate run identity mismatch')
            if self.path.exists():
                require(read_json(self.path).get('run_id', self.run_id) == self.run_id, 'candidate stored run mismatch')
        self.internal('state.json')
        if self.path.exists():
            validate_state(read_json(self.path))
        self.internal().mkdir(parents=True, exist_ok=True)
        self.internal('state.json')
        lock = self.internal('coordinator.lock')
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise Error('another coordinator or stale lock exists; inspect owner before removing lock')
        try:
            os.write(fd, str(os.getpid()).encode())
            s = read_json(self.path) if self.path.exists() else {'schema_version': SCHEMA_VERSION, 'status': 'inactive', 'tasks': {}}
            validate_state(s)
            try:
                result = self.apply(s, command, p)
            except InputInvalidation:
                write_json(self.path, s, self.fault)
                raise
            if command == 'snapshot':
                cut(self.fault, 'checkpoint.pointer.before', self.path)
            write_json(self.path, s, self.fault)
            if command == 'snapshot':
                cut(self.fault, 'checkpoint.pointer.after', self.path)
            if command in {'snapshot', 'compact'}:
                # Commit the new checkpoint pointer before removing old owned copies.
                try:
                    result['pruned_checkpoints'] = self.prune_checkpoints(s)
                except (Error, OSError, ValueError) as exc:
                    result['cleanup_warning'] = str(exc)
            return result
        finally:
            os.close(fd)
            lock.unlink()

    def apply(self, s, command, p):
        validate_command(command, p)
        # Failed semantic guards must not leak changes to direct callers either.
        json_value(s)
        validate_state(s)
        pending = strict_json(json.dumps(s, allow_nan=False))
        try:
            result = self._apply(pending, command, p)
        except InputInvalidation:
            # Persist only explicit invalidation records; failed guards cannot commit
            # unrelated partial state from the attempted command.
            for key, task in pending['tasks'].items():
                if task.get('contract_invalid'):
                    s['tasks'][key]['contract_invalid'] = True
            s['invalidations'] = pending['invalidations']
            validate_state(s)
            raise
        except KeyError as exc:
            invalid('COMMAND_REFERENCE', '$.id', 'unknown task or state field: ' + str(exc))
        validate_state(pending)
        s.clear()
        s.update(pending)
        return result

    def _apply(self, s, command, p):
        now = self.clock()
        if command == 'bind-active-goal':
            require(s == {'schema_version': SCHEMA_VERSION, 'status': 'inactive', 'tasks': {}},
                    'native binding requires truly pristine inactive schema2 state')
            binding = p['native_binding']
            app = self.app_goal(p, binding)
            require(app['status'] == 'active' and app['observed_at'] == binding['observed_at'], 'initial binding requires matching active native observation')
            require(p['objective'] == binding['objective'], 'objective must match native binding scope')
            inv = self.inventory(p)
            require(not any(a['status'] in ACTIVE for a in inv['items']), 'unresolved active children before binding')
            s.update(status='active', objective=p['objective'], native_binding=binding, scope=binding['scope'],
                     goal_id=binding['goal_id'], thread_id=binding['thread_id'], run_id=binding['run_id'], app_goal_observation=app,
                     models=p['models'], slot_limit=p['slot_limit'], session_limit=p.get('session_limit'), inventory=inv,
                     created_at=now, usage_errors=0, authorization_events=[], authorization_event_ids=[],
                     native_binding_ids=[binding['binding_id']], reserved_run_ids=[binding['run_id']], reserved_scope_ids=[binding['scope']['id']],
                     native_binding_events=[dict(binding=binding, interpretation=p['interpretation'], procedural_check=p['procedural_check'], app_goal_observation=app)])
            return {'status': 'active', 'basis': 'native_binding'}
        if command == 'activate':
            require(s['status'] in {'inactive', 'complete'}, 'resume or close existing run first')
            event = self.authorization_event(p, s, {'explicit_task_authorization', 'explicit_goal_continuation', 'independent_report_request'}, after=s.get('completed_at'))
            require(p.get('objective') == event['scope']['objective'], 'objective must match authorized scope')
            require(event['run_id'] not in s.get('reserved_run_ids', []) and event['scope']['id'] not in s.get('reserved_scope_ids', [])
                    and event['run_id'] != s.get('run_id') and event['scope']['id'] != s.get('scope', {}).get('id'), 'new activation requires separate run and scope')
            app = self.app_goal(p, event)
            require(app['status'] == 'active', 'app goal must be observed active before activation')
            inv = self.inventory(p)
            require(not any(a['status'] in ACTIVE for a in inv['items']), 'unresolved active children before activation')
            require(isinstance(p['slot_limit'], int) and p['slot_limit'] >= 1, 'invalid capacity')
            history = {k: s.get(k, []) for k in ('authorization_event_ids', 'authorization_events', 'reserved_run_ids', 'reserved_scope_ids', 'native_binding_ids', 'native_binding_events')}
            s.clear()
            s.update(history)
            s.update(schema_version=SCHEMA_VERSION, status='active', objective=p['objective'], authorization=event, scope=event['scope'],
                goal_id=event['goal_id'], thread_id=event['thread_id'], run_id=event['run_id'], app_goal_observation=app, tasks={},
                models=p['models'], slot_limit=p['slot_limit'], session_limit=p.get('session_limit'), inventory=inv,
                created_at=now, usage_errors=0)
            self.remember_authorization(s, event, p['interpretation'], p['source_message'])
            s['reserved_run_ids'].append(event['run_id'])
            s['reserved_scope_ids'].append(event['scope']['id'])
            return {'status': 'active'}
        if command == 'status':
            return {'schema_version': s['schema_version'], 'status': s['status'], 'objective': s.get('objective'),
                    'tasks': {k: t['state'] for k, t in s['tasks'].items()},
                    'task_observations': self.task_observations(s),
                    'app_goal_observation': s.get('app_goal_observation'), 'usage': s.get('usage'),
                    'checkpoint': s.get('checkpoint'), 'next_action': s.get('next_action')}
        require(s['status'] != 'inactive', 'activate first')
        if command == 'observe-authorization':
            event = self.authorization_event(p, s, {'future_goal_intent', 'policy_change', 'independent_report_request', 'explicit_stop'})
            require(all(event[k] == s.get(k) for k in ('goal_id', 'thread_id', 'run_id')), 'observation run/thread identity mismatch')
            if event['kind'] == 'explicit_stop':
                require(all(event[k] == s.get(k) for k in ('goal_id', 'thread_id', 'run_id')) and event['scope'] == s.get('scope'), 'stop scope mismatch')
                s.update(status='stopping', stopped_at=now)
            self.remember_authorization(s, event, p['interpretation'], p['source_message'])
            return {'status': s['status'], 'authorized_old_objective': False}
        if command == 'app-goal':
            basis = s.get('authorization', s.get('native_binding'))
            require(isinstance(basis, dict), 'typed authorization or native binding required')
            app = self.app_goal(p, basis)
            s['app_goal_observation'] = app
            if app['status'] != 'active' and s['status'] == 'active':
                s.update(status='stopping', stopped_at=now)
            return {'status': s['status'], 'app_goal_status': app['status']}
        if command == 'usage':
            rate = p.get('rateLimits') or {}
            windows = {v.get('windowDurationMins'): v for v in [rate.get('primary'), rate.get('secondary')] if isinstance(v, dict)}
            values = [windows.get(w, {}).get('usedPercent') for w in (300, 10080)]
            valid = (0 <= now - p.get('observed_at', 0) <= 300 and
                     all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 100 for v in values))
            if not valid:
                s.pop('usage', None)
                s['usage_errors'] = s.get('usage_errors', 0) + 1
                if s['usage_errors'] >= 2 and s['status'] not in {'halted', 'complete'}:
                    s['status'] = 'stopping'
                return {'action': 'SAVE_AND_STOP' if s['usage_errors'] >= 2 else 'RETRY_USAGE_ONCE'}
            schedule = usage_schedule(windows, p['observed_at'], s.get('usage'))
            s.update(usage=schedule, usage_errors=0)
            if schedule['action'] == 'SAVE_AND_STOP' and s['status'] not in {'halted', 'complete'}:
                s['status'] = 'stopping'
            return {k: v for k, v in schedule.items() if k not in {'windows', 'rates'}}
        if command == 'usage-due':
            u = s.get('usage', {})
            if s['status'] in {'stopping', 'halted', 'complete'}:
                return {'action': 'SAVE_AND_STOP' if s['status'] == 'stopping' else 'NO_WORK'}
            due = p.get('substantial_work', False) or not u or now < u.get('at', 0) or now >= u.get('next_check_at', 0)
            return {'action': 'READ_USAGE' if due else 'REUSE_MEASUREMENT',
                    'next_check_at': u.get('next_check_at'), 'remaining': u.get('remaining')}
        if command == 'inventory':
            s['inventory'] = self.inventory(p)
            return {'recorded': True}
        if command == 'task':
            self.usable(s)
            key = p['id']
            require(re.fullmatch(r'[a-z][a-z0-9_]{0,63}', key) and key not in s['tasks'], 'unique short task id required', '$.id')
            require(p['role'] in self.catalog, 'unknown subagent role', '$.role')
            require(p.get('objective') and p.get('criteria'), 'objective and criteria required')
            deps = p.get('depends_on', [])
            require(all(d in s['tasks'] for d in deps), 'unknown dependency', '$.depends_on')
            require(len(deps) == len(set(deps)), 'duplicate dependency', '$.depends_on')
            claims = {}
            for name in p.get('writes', []):
                path = self.file(name)
                key_path = str(path).casefold()
                owners = [other for t in s['tasks'].values() if t['state'] not in TERMINAL for other in t['writes']] + list(claims)
                for other in owners:
                    other_path = str(self.file(other)).casefold()
                    require(key_path != other_path and not key_path.startswith(other_path + os.sep) and not other_path.startswith(key_path + os.sep), 'file already owned or overlapping')
                claims[path.relative_to(self.root).as_posix()] = digest(path)
            require(p['role'] not in {'verifier', 'coordinator'} or not claims, 'role is read-only')
            require(not p.get('review_targets') or p['role'] == 'verifier', 'only verifier may claim review targets')
            file_inputs = self.pin_files(p.get('file_inputs', []))
            review_targets = self.pin_targets(s, p.get('review_targets', []))
            require(not {item['path'].casefold() for item in file_inputs} & {name.casefold() for name in claims}, 'immutable file inputs cannot overlap owned writes')
            s['tasks'][key] = dict(role=p['role'], objective=p['objective'], revision=p.get('revision', uuid.uuid4().hex),
                file_inputs=file_inputs, context_refs=p.get('context_refs', []), review_targets=review_targets,
                criteria=p['criteria'], writes=claims, depends_on=deps, state='ready', independent_review=p.get('independent_review', False))
            s['tasks'][key]['assignment_hash'] = assignment_hash(s['tasks'][key])
            return {'id': key, 'state': 'ready'}
        if command == 'prepare':
            self.usable(s)
            t = s['tasks'][p['id']]
            require(t['state'] == 'ready', 'task not ready', '$.id')
            require(all(self.dependency_accepted(s, d) for d in t['depends_on']), 'dependency not accepted')
            self.check_inputs(s, p['id'], 'prepare')
            if 'current_context' in p:
                self.check_context(s, p['current_context'], p['id'])
            for dependency in t['depends_on']:
                self.output_hashes(s, dependency, 'prepare: dependency artifacts', s['tasks'][dependency]['output_hashes'])
            live = self.live(s)
            pending = sum(t['state'] == 'prepared' for t in s['tasks'].values())
            count = sum(v in ACTIVE for v in live.values()) + pending
            require(count < min(5, s['slot_limit'] - 1), 'capacity exhausted')
            require(s.get('session_limit') is None or len(live) + pending < s['session_limit'], 'open session capacity exhausted')
            require(s['usage']['remaining'] > 20 or count == 0, 'caution: parallel work disabled')
            settings = self.settings(t['role'], p.get('effort'))
            require(settings['model'] in s['models'] and settings['reasoning_effort'] in s['models'][settings['model']], 'exact model/effort unavailable; no fallback')
            for name, expected in t['writes'].items():
                require(digest(self.file(name)) == expected, 'file changed since assignment: ' + name)
            if t['role'] == 'verifier':
                t['reviewed_hashes'] = {}
                for pin in t['review_targets']:
                    t['reviewed_hashes'][pin['producer_id']] = pin['output_hashes']
            context = {k: t[k] for k in ('objective', 'revision', 'file_inputs', 'context_refs', 'review_targets', 'criteria', 'writes')}
            context['authorized_scope'] = s['scope']
            context['authorization_identity'] = {k: s[k] for k in ('goal_id', 'thread_id', 'run_id')}
            context['project'] = str(self.root)
            if 'current_context' in p:
                # Prepared arguments bind use of this protocol, without adding
                # stored schema fields. Native dispatch requires a fresh rendering
                # after preparation changes state; the old summary cannot be reused.
                context['current_context_required'] = True
                context['current_context_manifest'] = {k: p['current_context'][k] for k in ('manifest_path', 'manifest_sha256')}
            context['project_instructions'] = str(self.root / 'AGENTS.md') if (self.root / 'AGENTS.md').is_file() else None
            prompt = self.catalog[t['role']]['developer_instructions'] + '\n' + json.dumps(context, ensure_ascii=False)
            args = dict(task_name=p['id'], fork_turns='none', message=prompt, **settings)
            t.update(state='prepared', prepared=args, dispatch_intent=uuid.uuid4().hex, dispatch_prepared_at=now)
            return {'tool': 'collaboration.spawn_agent', 'arguments': args, 'intent_id': t['dispatch_intent']}
        if command == 'record':
            t = s['tasks'][p['id']]
            require(t['state'] == 'prepared' and p['submitted_arguments'] == t['prepared'], 'dispatch settings mismatch', '$.submitted_arguments')
            self.usable(s)
            self.check_inputs(s, p['id'], 'record')
            require(p.get('session_id') and p.get('receipt'), 'real tool receipt required')
            require(not any(x.get('session_id') == p['session_id'] for x in s['tasks'].values()), 'session reused', '$.session_id')
            args = t.pop('prepared')
            t.update(state='running', session_id=p['session_id'], requested={k: args[k] for k in ('model', 'reasoning_effort')},
                effective_model=p.get('effective_model'), effective_effort=p.get('effective_effort'),
                receipt_hash=hashlib.sha256(json.dumps(p['receipt'], sort_keys=True).encode()).hexdigest())
            if ((p.get('effective_model') and p['effective_model'] != args['model']) or
                (p.get('effective_effort') and p['effective_effort'] != args['reasoning_effort'])):
                s['status'] = 'stopping'
            # A pre-spawn inventory cannot establish the child's current status.
            s['inventory']['at'] = 0
            return {'state': t['state'], 'run_status': s['status'], 'effective_verified': bool(p.get('effective_model') and p.get('effective_effort'))}
        if command == 'dispatch-start':
            # Durable uncertainty BEFORE the caller invokes the native tool.
            # A crash on either side of that boundary never permits blind retry.
            self.usable(s)
            live = self.live(s)
            require('observation' in s['inventory'], 'dispatch-start requires preserved native inventory observation')
            t = s['tasks'][p['id']]
            require(t['state'] == 'prepared' and t.get('dispatch_intent') == p['intent_id'], 'prepared intent identity required')
            self.check_inputs(s, p['id'], 'dispatch-start')
            prepared_context = strict_json(t['prepared']['message'].rsplit('\n', 1)[-1])
            if prepared_context.get('current_context_required'):
                require('current_context' in p, 'fresh current context required at native dispatch')
                require({k: p['current_context'][k] for k in ('manifest_path', 'manifest_sha256')}
                        == prepared_context['current_context_manifest'], 'prepared context manifest replaced')
            if 'current_context' in p:
                self.check_context(s, p['current_context'], p['id'])
            pending = sum(other['state'] == 'prepared' for key, other in s['tasks'].items() if key != p['id'])
            count = sum(value in ACTIVE for value in live.values()) + pending
            require(count < min(5, s['slot_limit'] - 1), 'capacity exhausted at native dispatch')
            require(s.get('session_limit') is None or len(live) + pending < s['session_limit'], 'open session capacity exhausted at native dispatch')
            for dependency in t['depends_on']:
                require(self.dependency_accepted(s, dependency), 'dependency not accepted')
                self.output_hashes(s, dependency, 'dispatch-start: dependency artifacts', s['tasks'][dependency]['output_hashes'])
            settings = self.settings(t['role'], t['prepared']['reasoning_effort'])
            require(all(t['prepared'][key] == value for key, value in settings.items()) and
                    settings['model'] in s['models'] and settings['reasoning_effort'] in s['models'][settings['model']], 'dispatch policy or exact capability changed')
            require(s['usage']['remaining'] > 20 or count == 0, 'caution: parallel work disabled')
            for name, expected in t['writes'].items():
                require(digest(self.file(name)) == expected, 'file changed before native dispatch: ' + name)
            t.update(state='uncertain', dispatch_failure='native outcome pending; do not retry')
            return {'state': 'uncertain', 'intent_id': p['intent_id'], 'tool': 'collaboration.spawn_agent', 'arguments': t['prepared']}
        if command == 'dispatch_failed':
            t = s['tasks'][p['id']]
            require(t['state'] in {'prepared', 'uncertain'} and t.get('prepared') and p.get('evidence'), 'pending dispatch and failure evidence required')
            # Preserve the exact intent so an actual late receipt can be matched.
            if p.get('certain_not_started'):
                t.pop('prepared')
            t['dispatch_failure'] = p['evidence']
            t['state'] = 'rejected' if p.get('certain_not_started') else 'uncertain'
            return {'state': t['state']}
        if command == 'reconcile':
            t = s['tasks'][p['id']]
            require(t['state'] == 'uncertain' and p.get('evidence'), 'uncertain task and evidence required')
            live = self.live(s)
            if any(key in p for key in ('session_id', 'submitted_arguments', 'receipt', 'effective_model', 'effective_effort')):
                require(all(key in p for key in ('session_id', 'submitted_arguments', 'receipt')), 'late success requires complete actual receipt and arguments')
                require(not p['no_owned_work_running'], 'late session reconciliation cannot claim no owned work')
                require(p['submitted_arguments'] == t.get('prepared'), 'late receipt dispatch settings mismatch')
                require('observation' in s['inventory'], 'late success requires preserved native inventory observation')
                require(s['inventory']['at'] >= t.get('dispatch_prepared_at', math.inf), 'inventory predates dispatch intent')
                observed = {item['id']: item['status'] for item in s['inventory']['items']}
                require(p['session_id'] in observed and observed[p['session_id']] != 'unknown', 'late session must be freshly observed')
                receipt = p['receipt']
                require(type(receipt) is dict and any(receipt.get(key) == p['session_id'] for key in ('id', 'agent_id', 'session_id', 'task_name')), 'actual receipt identity must match observed session')
                require(not any(x.get('session_id') == p['session_id'] for x in s['tasks'].values()), 'session reused')
                self.check_inputs(s, p['id'], 'reconcile')
                args = t.pop('prepared')
                t.update(state='running', session_id=p['session_id'], requested={k: args[k] for k in ('model', 'reasoning_effort')},
                         receipt_hash=hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest(),
                         effective_model=p.get('effective_model'), effective_effort=p.get('effective_effort'), reconciliation=p['evidence'])
                if s['status'] != 'active' or ((p.get('effective_model') and p['effective_model'] != args['model']) or
                    (p.get('effective_effort') and p['effective_effort'] != args['reasoning_effort'])):
                    s['status'] = 'stopping'
                return {'state': 'running', 'run_status': s['status'], 'reconciled_session': p['session_id']}
            require(not any(v in ACTIVE for v in live.values()) and p.get('no_owned_work_running') is True, 'resolve uncertain child first', '$.no_owned_work_running')
            t.pop('prepared', None)
            t.update(state='rejected', reconciliation=p['evidence'])
            return {'state': 'rejected'}
        if command == 'finish':
            t = s['tasks'][p['id']]
            require(t['state'] == 'running' and p.get('evidence'), 'running task and result evidence required')
            require(self.live(s).get(t['session_id']) in {'idle', 'done', 'stopped'}, 'writer still running or unknown')
            if not p['accepted']:
                # Discarding a quiescent result does not trust its input/output
                # bytes or review. Preserve invalidation receipts but release
                # this task's ownership; acceptance keeps every guard below.
                t.update(state='rejected', evidence=p['evidence'])
                return {'state': t['state']}
            self.check_inputs(s, p['id'], 'finish')
            if t['role'] == 'verifier':
                for key, expected in t.get('reviewed_hashes', {}).items():
                    self.output_hashes(s, key, 'finish: review target', expected)
            if p['accepted'] and t['independent_review']:
                review = s['tasks'].get(p.get('review_task'), {})
                require(t.get('session_id') and review.get('session_id') and review.get('role') == 'verifier'
                        and review.get('state') == 'accepted' and review['session_id'] != t['session_id'],
                        'independent accepted review with distinct native identities required', '$.review_task')
                require(any(pin['producer_id'] == p['id'] and pin['revision'] == t['revision']
                            and pin['output_hashes'] == review.get('reviewed_hashes', {}).get(p['id'])
                            for pin in review['review_targets']), 'review must pin the reviewed task revision and recorded hashes')
                self.check_inputs(s, p.get('review_task'), 'accept review')
                self.output_hashes(s, p['id'], 'accept review', review.get('reviewed_hashes', {}).get(p['id']))
                t['review'] = p['review_task']
            t.update(state='accepted' if p['accepted'] else 'rejected', evidence=p['evidence'], output_hashes=self.output_hashes(s, p['id'], 'finish'))
            return {'state': t['state']}
        if command == 'cancel':
            t = s['tasks'][p['id']]
            require(t['state'] == 'ready' and p.get('reason'), 'only undispatched task can be cancelled; reason required')
            t.update(state='rejected', evidence=p['reason'])
            return {'state': 'rejected'}
        if command == 'stop':
            s.update(status='stopping', stopped_at=now)
            return {'action': 'SAVE_AND_STOP', 'writers': [t['session_id'] for t in s['tasks'].values() if t.get('session_id') and t['writes'] and t['state'] not in TERMINAL]}
        if command == 'snapshot':
            require(p.get('writers_quiescent') is True and p.get('next_action'), 'quiescent writers and next action required', '$.writers_quiescent')
            names = set(p.get('files', [])) | {n for t in s['tasks'].values() for n in t['writes']}
            checked = {n: self.file(n) for n in sorted(names)}
            checkpoint_id = uuid.uuid4().hex
            directory = self.internal('checkpoints', checkpoint_id)
            directory.mkdir(parents=True)
            write_json(directory / 'owner.json', {'owner': 'agent-workflow-v1-0', 'project': str(self.root)}, self.fault)
            write_json(directory / 'state.json', s, self.fault)
            files = {}
            for name, path in checked.items():
                before = digest(path)
                if before is not None:
                    dest = directory / 'files' / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dest)
                    require(digest(dest) == before and digest(path) == before, 'file changed during snapshot: ' + name)
                files[name] = before
            manifest = {'files': files, 'next_action': p['next_action'], 'state_hash': digest(directory / 'state.json')}
            write_json(directory / 'manifest.json', manifest, self.fault)
            with tempfile.TemporaryDirectory(prefix='restore-check-', dir=self.base) as temp:
                restored = Path(temp)
                for name, expected in files.items():
                    if expected is not None:
                        dest = restored / 'files' / name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(directory / 'files' / name, dest)
                    require(digest(restored / 'files' / name) == expected, 'restore verification failed')
                shutil.copy2(directory / 'state.json', restored / 'state.json')
                require(digest(restored / 'state.json') == manifest['state_hash'], 'state restore failed')
            # Only promote after successful restore; leave the previous good copy intact.
            previous = s.get('checkpoint')
            s['checkpoint'] = {'id': checkpoint_id, 'verified': True, 'manifest_hash': digest(directory / 'manifest.json')}
            self.verify_checkpoint(s, current=True)
            if previous:
                s['previous_checkpoint'] = previous
            s['next_action'] = p['next_action']
            return {'checkpoint': s['checkpoint'], 'files': len(files), 'restore_verified': True}
        if command == 'restore':
            directory, manifest = self.verify_checkpoint(s)
            dest = Path(p['destination']).resolve()
            require(not dest.exists() and not dest.is_relative_to(self.root) and not self.root.is_relative_to(dest), 'new destination outside project required')
            dest.mkdir(parents=True)
            for name, expected in manifest['files'].items():
                if expected is not None:
                    target = dest / 'files' / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(directory / 'files' / name, target)
                    require(digest(target) == expected, 'restore mismatch')
            write_json(dest / 'recovery-manifest.json', manifest)
            shutil.copy2(directory / 'state.json', dest / 'recovery-state.json')
            require(digest(dest / 'recovery-state.json') == manifest['state_hash'], 'restored state mismatch')
            return {'destination': str(dest), 'verified': True}
        if command == 'halt':
            require(s['status'] == 'stopping', 'stop first')
            s['inventory'] = self.inventory(p)
            require(not any(v in ACTIVE for v in self.live(s).values()), 'owned agents not observed stopped')
            require(p.get('owned_processes_stopped') is True and p.get('stop_evidence'), 'observed process-stop evidence required', '$.owned_processes_stopped')
            if p.get('degraded'):
                require(p.get('reason') and p.get('next_action'), 'degraded recovery reason and next action required')
                s['recovery_error'] = p['reason']
                s['next_action'] = p['next_action']
            else:
                self.verify_checkpoint(s, current=True)
                s.pop('recovery_error', None)
            s.update(status='halted', halted_at=now, stop_evidence=p['stop_evidence'])
            return {'status': 'halted', 'recovery_verified': not p.get('degraded', False)}
        if command == 'resume':
            require(s['status'] == 'halted', 'halted run and new user continuation required')
            event = self.authorization_event(p, s, {'explicit_goal_continuation'}, after=max(s['halted_at'], s.get('stopped_at', 0)), matching=True)
            app = self.app_goal(p, event, after=s['halted_at'])
            require(app['status'] == 'active', 'helper resume cannot substitute for app goal continuation')
            require(s.get('usage', {}).get('at', 0) > s['halted_at'] and 0 <= now - s['usage']['at'] <= 300 and s['usage']['remaining'] > 10 and s['usage'].get('action') != 'SAVE_AND_STOP' and now < s['usage'].get('next_check_at', 0), 'fresh post-halt usage required')
            self.verify_checkpoint(s, current=True)
            require(s.get('inventory', {}).get('at', 0) > s['halted_at'], 'fresh post-halt inventory required')
            require(not any(v in ACTIVE for v in self.live(s).values()), 'live children before resume')
            # Old sessions/results are never resumed implicitly.
            for task in s['tasks'].values():
                if task['state'] not in TERMINAL:
                    task['state'] = 'rejected'
                    task.pop('prepared', None)
            s.update(status='active', authorization=event, app_goal_observation=app)
            s.pop('native_binding', None)
            self.remember_authorization(s, event, p['interpretation'], p['source_message'])
            s.pop('recovery_error', None)
            return {'status': 'active', 'next_action': s.get('next_action')}
        if command in {'compact', 'complete'}:
            require(all(t['state'] in TERMINAL for t in s['tasks'].values()), 'unfinished tasks remain')
            require(not any(v in ACTIVE for v in self.live(s).values()), 'active children remain')
            self.verify_checkpoint(s, current=True)
            if command == 'compact':
                s['tasks'] = {}
                # The verified checkpoint above retains both task records and
                # their invalidation receipts. Current state must not retain
                # references to tasks that were just archived.
                s.pop('invalidations', None)
            else:
                require(s['status'] == 'active', 'only active run can complete')
                s.update(status='complete', completed_at=now)
            return {'status': s['status'], 'tasks': len(s['tasks'])}
        if command == 'status':
            return {'status': s['status'], 'objective': s.get('objective'), 'tasks': {k: t['state'] for k, t in s['tasks'].items()},
                    'app_goal_observation': s.get('app_goal_observation'),
                    'usage': s.get('usage'), 'checkpoint': s.get('checkpoint'), 'next_action': s.get('next_action')}
        raise Error('unknown command')

    def settings(self, role, effort):
        require(role in self.catalog, 'role unavailable')
        r = self.catalog[role]
        if role == 'limit_watcher':
            require(r['model'] == 'gpt-6-luna' and r['model_reasoning_effort'] == 'low' and r['workflow']['efforts'] == ['low', 'medium'], 'integrated limit role policy mismatch')
        else:
            require(role != 'astra_coordinator' and r['model'] == 'gpt-6.1-sol' and r['model_reasoning_effort'] == 'high' and r['workflow']['efforts'] == ['high'], 'user policy requires Sol/high; Astra prohibited')
        effort = effort or r['model_reasoning_effort']
        require(effort in r['workflow']['efforts'], 'unsupported role effort')
        return {'model': r['model'], 'reasoning_effort': effort}


def main():
    require(False, 'candidate requires scripts/candidate.py with external manifest pin')


if __name__ == '__main__':
    try:
        main()
    except (Error, KeyError, OSError, ValueError) as exc:
        print(json.dumps({'error': str(exc), 'code': getattr(exc, 'code', 'IO_ERROR' if isinstance(exc, OSError) else 'INVALID_VALUE'),
                          'field': getattr(exc, 'field', '$'), **({'invalidation': exc.receipt} if isinstance(exc, InputInvalidation) else {})}, ensure_ascii=False, allow_nan=False), file=sys.stderr)
        sys.exit(1)
