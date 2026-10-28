"""Explicit, pinned candidate entry. No provider calls or native model dispatch."""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

PACKAGE = Path(__file__).resolve().parents[1]
IDENTITY = 'agent-workflow-v1-0'
VERSION = '1.0'

class CandidateError(ValueError):
    pass

def require(condition, message):
    if not condition:
        raise CandidateError(message)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def read(path):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=pairs)

def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')

def unlinked(path):
    # Check every existing component, including ancestor junctions on Windows.
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            require(not item.is_symlink() and not getattr(item, 'is_junction', lambda: False)(), 'linked candidate path')
            if item.is_file():
                require(item.stat().st_nlink == 1, 'hard-linked candidate file')

def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class Runtime:
    def __init__(self, args):
        require(args.version == VERSION, 'candidate version mismatch')
        require(re.fullmatch(r'[a-f0-9]{64}', args.manifest_sha256 or ''), 'external manifest SHA-256 required')
        self.manifest = Path(args.manifest).absolute()
        self.pin = args.manifest_sha256
        unlinked(self.manifest)
        require(sha(self.manifest) == self.pin, 'manifest differs from external trust anchor')
        self.document = read(self.manifest)
        require(set(self.document) == {'schema_version', 'identity', 'version', 'files'}, 'invalid manifest fields')
        require(self.document['schema_version'] == 1 and self.document['identity'] == IDENTITY and self.document['version'] == VERSION, 'manifest version mismatch')
        self.files = self.document['files']
        require(type(self.files) is dict and self.files, 'manifest files required')
        self.paths = {}
        for name, digest in self.files.items():
            rel = Path(name)
            require(not rel.is_absolute() and '..' not in rel.parts and ':' not in name and rel.parts and name == rel.as_posix(), 'unsafe manifest file path')
            require(re.fullmatch(r'[a-f0-9]{64}', digest or ''), 'invalid dependency hash')
            target = PACKAGE / rel
            self.paths[name] = target.absolute()
        required = {'candidate-profile.json', 'SKILL.md', 'agents/openai.yaml', 'scripts/candidate.py', 'scripts/workflow.py', 'scripts/coordination.py', 'roles/coordinator.toml', 'private/limit-watcher/role.json', 'private/limit-watcher/limit_watcher.md', 'private/limit-watcher/scripts/monitor.py'}
        require(required <= set(self.paths), 'manifest omits required runtime dependency')
        self.verify_files()
        profile = read(self.paths['candidate-profile.json'])
        require(profile == dict(schema_version=1, version=VERSION, identity=IDENTITY, display_name='agent-workflow-v1.0', allow_implicit_invocation=False, namespace='.agent-workflow-v1-0'), 'candidate profile mismatch')
        watcher_role=read(self.paths['private/limit-watcher/role.json'])['roles']['limit_watcher']
        require(watcher_role['role_file'] == 'limit_watcher.md', 'watcher instruction must be pinned private resource')
        self.core=load_module('workflow', self.paths['scripts/workflow.py'])
        self.catalog=self.core.roles(PACKAGE, self.paths['private/limit-watcher/role.json'])
        require('limit_watcher' in self.catalog, 'private watcher role required')
        self.args = args
        self.base = None
        if args.mode != 'install':
            require(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', args.run_id or ''), 'short candidate run ID required')
            self.project = Path(args.project).absolute()
            unlinked(self.project)
            require(self.project.is_dir(), 'project must exist')
            self.project = self.project.resolve()
            self.base = self.project / profile['namespace'] / 'runs' / args.run_id
            unlinked(self.base)
            self.binding = dict(schema_version=1, identity=IDENTITY, version=VERSION, run_id=args.run_id,
                project=str(self.project), manifest_path=str(self.manifest.resolve()), manifest_sha256=self.pin,
                dependencies={name: dict(path=str(path.resolve()), sha256=self.files[name]) for name, path in sorted(self.paths.items())})
            self.check_binding()

    def verify_files(self):
        actual = set()
        for path in PACKAGE.rglob('*'):
            rel = path.relative_to(PACKAGE)
            if path.is_file() and '__pycache__' not in rel.parts and path.suffix != '.pyc' and rel.parts[0] in {'SKILL.md', 'candidate-profile.json', 'agents', 'roles', 'scripts', 'references', 'private', 'tools'}:
                actual.add(rel.as_posix())
        require(actual == set(self.paths) and len({name.casefold() for name in actual}) == len(actual), 'manifest dependency inventory mismatch')
        unlinked(self.manifest)
        require(sha(self.manifest) == self.pin, 'manifest differs from external trust anchor')
        for name, path in self.paths.items():
            unlinked(path)
            require(path.is_file() and sha(path) == self.files[name], 'dependency missing or changed: ' + name)

    def check_binding(self):
        self.verify_files()
        unlinked(self.base)
        binding = self.base / 'run-binding.json'
        if binding.exists():
            require(read(binding) == self.binding, 'immutable run binding mismatch')
        else:
            require(not self.base.exists() or not any(self.base.iterdir()), 'unbound nonempty run namespace')
        state = self.base / 'state.json'
        if state.exists():
            require(self.core.read_json(state).get('run_id', self.args.run_id) == self.args.run_id, 'stored workflow run mismatch')
        watcher = self.base / 'watcher'
        # The worker replaces health.json every second. Ownership is pinned by
        # control/running/ownership; monitor.status validates health separately
        # and holds on read errors without blocking stop or recovery commands.
        for name in ('control.json', 'running.json'):
            path = watcher / name
            unlinked(path)
            if path.exists():
                require(self.core.read_json(path).get('runId') == self.args.run_id, 'watcher ownership run mismatch')
        ownership = watcher / 'ownership.json'
        if ownership.exists():
            require(self.core.read_json(ownership) == self.binding, 'watcher dependency ownership mismatch')

    def bind(self):
        self.check_binding()
        path = self.base / 'run-binding.json'
        if not path.exists():
            self.base.mkdir(parents=True, exist_ok=True)
            try:
                with path.open('xb') as handle:
                    handle.write(encoded(self.binding))
            except FileExistsError:
                require(read(path) == self.binding, 'concurrent run binding mismatch')

    def workflow(self):
        self.bind()
        w = self.core
        sys.modules['workflow'] = w
        catalog = self.catalog
        return w, w.Workflow(self.project, PACKAGE, state_base=self.base, role_catalog=catalog, provenance_guard=self.check_binding, run_id=self.args.run_id)

    def worker_prefix(self):
        return [sys.executable, '-X', 'utf8', '-B', str(self.paths['scripts/candidate.py']),
            '--manifest', str(self.manifest), '--manifest-sha256', self.pin, '--version', VERSION,
            '--project', str(self.project), '--run-id', self.args.run_id, 'watcher']

def installation_files(root):
    unlinked(root)
    result = {}
    for path in root.rglob('*'):
        unlinked(path)
        if path.is_file() and path.name != '.installation.json':
            # Python may create bytecode beside installed modules even when the
            # package was installed without it. It is disposable runtime data,
            # not a user edit to an owned source file.
            if (path.parent.name == '__pycache__' and path.suffix == '.pyc' and
                    (path.parent.parent / (path.name.split('.', 1)[0] + '.py')).is_file()):
                continue
            result[path.relative_to(root).as_posix()] = sha(path)
    return result

def inherit_install_permissions(temporary_root):
    if os.name != 'nt':
        return
    system_root = os.environ.get('SystemRoot')
    require(system_root, 'Windows system root is unavailable')
    icacls = Path(system_root) / 'System32' / 'icacls.exe'
    result = subprocess.run([str(icacls), str(temporary_root), '/reset'],
                            capture_output=True, text=True, check=False)
    require(result.returncode == 0, 'cannot inherit destination permissions for staged installation')

def stage_install(runtime, destination, update=False):
    dest = Path(destination).absolute()
    unlinked(dest)
    require(dest.name == IDENTITY, 'candidate destination must end in ' + IDENTITY)
    require(not dest.is_relative_to(PACKAGE) and not PACKAGE.is_relative_to(dest), 'installation overlaps executing source')
    expected = {**runtime.files, 'manifest.json': runtime.pin}
    prior = None
    if dest.exists():
        require(dest.is_dir() and (dest / '.installation.json').is_file(), 'unowned existing installation')
        actual = installation_files(dest)
        prior = read(dest / '.installation.json')
        require(prior == dict(identity=IDENTITY, files=actual), 'installed content edited; preserve changes before update')
        if actual == expected:
            return dict(installed=str(dest), changed=False, activated=False)
        require(update, 'owned update requires --update')
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix='candidate-install-', dir=dest.parent))
    cleanup = True
    try:
        inherit_install_permissions(temp)
        staged = temp / 'package'
        staged.mkdir()
        runtime.verify_files()
        for name, path in runtime.paths.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        shutil.copyfile(runtime.manifest, staged / 'manifest.json')
        require(installation_files(staged) == expected, 'staged candidate verification failed')
        (staged / '.installation.json').write_bytes(encoded(dict(identity=IDENTITY, files=expected)))
        if dest.exists():
            old = temp / 'previous'
            dest.rename(old)
            cleanup = False
            try:
                staged.rename(dest)
            except BaseException:
                # If restoration itself fails, retain the only working copy
                # for explicit recovery instead of deleting it in cleanup.
                old.rename(dest)
                cleanup = True
                raise
            cleanup = True
        else:
            staged.rename(dest)
    finally:
        if cleanup:
            unlinked(temp)
            require(temp.resolve().parent == dest.parent.resolve() and temp.name.startswith('candidate-install-'), 'unsafe transaction cleanup')
            shutil.rmtree(temp)
    return dict(installed=str(dest), changed=True, activated=False)


def watcher_command(runtime, command, *options):
    """Call the pinned private sampler without mixing its JSON with workflow output."""
    runtime.bind()
    base = runtime.base / 'watcher'
    base.mkdir(exist_ok=True)
    ownership = base / 'ownership.json'
    if not ownership.exists():
        with ownership.open('xb') as handle:
            handle.write(encoded(runtime.binding))
    require(not any(value.split('=', 1)[0] in {'--state-dir', '--run-id'}
                    for value in options), 'watcher ownership options are provided by candidate entry')
    monitor = load_module('candidate_monitor', runtime.paths['private/limit-watcher/scripts/monitor.py'])
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = monitor.main([command, *options, '--state-dir', str(base)], runtime=runtime)
    result = json.loads(output.getvalue())
    require(code == 0, 'integrated watcher ' + command + ' failed: ' +
            str(result.get('reason') or result.get('errorType') or 'unknown'))
    return result


def watcher_ready(runtime, command, payload):
    options = ()
    if command == 'resume':
        if payload.get('native_ui_resume') is True:
            observation = payload['app_goal_observation']
            options = ('--new-epoch', '--native-transition-at', str(observation['updated_at']),
                       '--native-receipt', observation['source_id'])
        else:
            options = ('--new-epoch', '--continuation-quote', payload['authorization']['exact_quote'])
    watcher_command(runtime, 'start', *options)
    deadline = time.monotonic() + 45
    while True:
        sample = watcher_command(runtime, 'status')
        require(sample.get('action') != 'SAVE_AND_STOP', 'integrated watcher requested SAVE_AND_STOP')
        if sample.get('health', {}).get('healthy'):
            return sample
        require(sample.get('health', {}).get('liveIdentity') is True,
                'integrated watcher exited before a healthy sample')
        require(time.monotonic() < deadline, 'integrated watcher healthy sample timed out')
        time.sleep(0.2)


def watcher_stop_verified(runtime):
    watcher_command(runtime, 'stop')
    deadline = time.monotonic() + 8
    while True:
        sample = watcher_command(runtime, 'status')
        health = sample.get('health', {})
        if health.get('identityState') not in {'matched', 'unknown'} and not health.get('liveIdentity'):
            require(health.get('exitReason') in {None, 'explicit_stop', 'lease_expired', 'usage_threshold',
                                               'early_reserve', 'measurement_failure', 'save_latched', 'worker_error'},
                    'integrated watcher terminal receipt invalid')
            return sample
        require(time.monotonic() < deadline, 'integrated watcher did not exit after stop')
        time.sleep(0.2)


RECOVERY_COMMANDS = {'status', 'usage', 'usage-due', 'snapshot', 'halt', 'complete',
                     'inventory', 'observe-authorization', 'app-goal', 'restore', 'compact'}
ACTIVATION_COMMANDS = {'activate', 'bind-active-goal', 'resume'}
TERMINAL_COMMANDS = {'halt', 'complete'}


def active_state(engine):
    return engine.path.exists() and read(engine.path).get('status') == 'active'


def preflight_activation(w, engine, command, payload):
    # Validate full authorization and state transition in memory before launching a worker.
    current = read(engine.path) if engine.path.exists() else {'schema_version': w.SCHEMA_VERSION,
                                                                'status': 'inactive', 'tasks': {}}
    w.validate_state(current)
    engine.apply(current, command, payload)


def renew_for_work(runtime, command):
    sample = watcher_command(runtime, 'status')
    healthy = sample.get('health', {}).get('healthy') and sample.get('action') != 'SAVE_AND_STOP'
    if not healthy:
        require(command in RECOVERY_COMMANDS,
                'integrated watcher unhealthy; only recovery commands are allowed')
        return sample
    return watcher_command(runtime, 'renew')


def watcher_summary(sample):
    return {'action': sample.get('action'), 'remaining': sample.get('remaining'),
            'healthy': sample.get('health', {}).get('healthy', False),
            'exitReason': sample.get('health', {}).get('exitReason'),
            'errorType': sample.get('health', {}).get('errorType')}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--manifest-sha256', required=True, help='Externally preserved build receipt pin; never recompute after an unexpected change')
    parser.add_argument('--version', required=True)
    parser.add_argument('--project')
    parser.add_argument('--run-id')
    sub = parser.add_subparsers(dest='mode', required=True)
    workflow = sub.add_parser('workflow')
    workflow.add_argument('command')
    workflow.add_argument('--input')
    coordination = sub.add_parser('coordination')
    coordination.add_argument('--input', required=True)
    watcher = sub.add_parser('watcher')
    watcher.add_argument('watcher_args', nargs=argparse.REMAINDER)
    install = sub.add_parser('install')
    install.add_argument('--destination', required=True)
    install.add_argument('--update', action='store_true')
    args = parser.parse_args(argv)
    try:
        runtime = Runtime(args)
        if args.mode == 'install':
            result = stage_install(runtime, args.destination, args.update)
        elif args.mode == 'watcher':
            require(args.watcher_args, 'watcher command required')
            result = watcher_command(runtime, args.watcher_args[0], *args.watcher_args[1:])
        else:
            w, engine = runtime.workflow()
            if args.mode == 'coordination':
                request = w.read_json(Path(args.input))
                w.require(type(request) is dict and set(request) <= {'batch_id', 'steps', 'seen'} and {'batch_id', 'steps'} <= set(request), 'batch_id and steps required')
                if 'seen' in request:
                    w.check_field(request['seen'], 'strings', '$.seen')
                commands = {step.get('command') for step in request['steps'] if type(step) is dict}
                require(not commands & (ACTIVATION_COMMANDS | TERMINAL_COMMANDS),
                        'lifecycle transitions require direct workflow command')
                if active_state(engine):
                    sample = renew_for_work(runtime, 'status' if commands <= RECOVERY_COMMANDS else 'coordination')
                else:
                    sample = None
                adapter = load_module('candidate_coordination', runtime.paths['scripts/coordination.py'])
                result = adapter.Coordination(engine).batch(request['batch_id'], request['steps'], request.get('seen'))
                if sample is not None:
                    result['watcher'] = watcher_summary(sample)
                print(json.dumps(result, ensure_ascii=False))
                return 1 if result['blocker'] else 0
            if args.command == 'validate':
                require(not args.input, 'validate takes no input')
                result = dict(identity=IDENTITY, version=VERSION, run_id=args.run_id, state_base=str(engine.base), manifest_sha256=runtime.pin,
                    roles={name: engine.settings(name, None) for name in engine.catalog}, missing_dependencies=[])
            else:
                require(args.command in w.COMMAND_FIELDS, 'unsupported candidate workflow command')
                payload = w.read_json(Path(args.input)) if args.input else w.strict_json(sys.stdin.read() or '{}')
                if args.command in ACTIVATION_COMMANDS:
                    preflight_activation(w, engine, args.command, payload)
                    try:
                        sample = watcher_ready(runtime, args.command, payload)
                        result = engine.execute(args.command, payload)
                        result['watcher'] = watcher_summary(sample)
                    except BaseException:
                        watcher_stop_verified(runtime)
                        raise
                else:
                    if active_state(engine):
                        sample = renew_for_work(runtime, args.command)
                    else:
                        sample = None
                    result = engine.execute(args.command, payload)
                    if args.command in TERMINAL_COMMANDS and result.get('status') in {'halted', 'complete'}:
                        sample = watcher_stop_verified(runtime)
                    if sample is not None and args.command in {'status', 'complete', 'halt'}:
                        result['watcher'] = watcher_summary(sample)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (CandidateError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps(dict(error=str(exc), code=getattr(exc, 'code', 'CANDIDATE_REFUSED'), field=getattr(exc, 'field', '$'), **({'invalidation': exc.receipt} if hasattr(exc, 'receipt') else {})), ensure_ascii=False), file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
