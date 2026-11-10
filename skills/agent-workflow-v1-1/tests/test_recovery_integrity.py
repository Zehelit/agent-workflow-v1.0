"""Schema2 recovery integrity: disposable pinned packages and owned child exits."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


class RecoveryIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / 'package'
        shutil.copytree(Path(__file__).resolve().parents[1], self.package,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        spec = importlib.util.spec_from_file_location('disposable_workflow', self.package / 'scripts/workflow.py')
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        # A test-only copied entry adds fault injection before its manifest is pinned.
        # Production candidate.py stays unchanged and exposes no fault-control flags.
        candidate = self.package / 'scripts/candidate.py'
        original = candidate.read_text(encoding='utf-8')
        needle = ('return w, w.Workflow(self.project, PACKAGE, state_base=self.base, role_catalog=catalog,\n'
                  '                            provenance_guard=self.check_binding, run_id=self.args.run_id,\n'
                  '                            usage_policy_provider=lambda: native_decision_policy(self))')
        injected = ("engine = w.Workflow(self.project, PACKAGE, lambda: 10000, state_base=self.base, role_catalog=catalog, provenance_guard=self.check_binding, run_id=self.args.run_id)\n"
                    f"        fault_root = Path({str(self.root)!r})\n"
                    "        require(PACKAGE.is_relative_to(fault_root), 'disposable fixture root required')\n"
                    "        require(read(fault_root / 'DISPOSABLE_FAULT_TEST.json') == {'disposable': True}, 'disposable fixture marker required')\n"
                    "        if (fault_root / 'fault-spec.json').exists():\n"
                    "            engine.fault = w.FaultControl(fault_root, read(fault_root / 'fault-spec.json'))\n"
                    "        return w, engine")
        self.assertEqual(original.count(needle), 1)
        candidate.write_text(original.replace(needle, injected), encoding='utf-8')
        payload_roots = {'SKILL.md', 'candidate-profile.json', 'agents', 'roles', 'scripts', 'references', 'private', 'tools'}
        files = {p.relative_to(self.package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.package.rglob('*') if p.is_file() and p.relative_to(self.package).parts[0] in payload_roots
                 and '__pycache__' not in p.parts and p.suffix != '.pyc'}
        self.manifest = self.package / 'manifest.json'
        self.manifest.write_text(json.dumps(dict(schema_version=1, identity='agent-workflow-v1-1', version='1.1', files=files)), encoding='utf-8')
        self.pin = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.home = self.root / 'home'
        self.counter = 0
        self.now = 10000
        self.m.write_json(self.root / 'DISPOSABLE_FAULT_TEST.json', {'disposable': True})

    def evidence(self, identity='old'):
        event = dict(event_id='event-' + identity, exact_quote='Start this scoped objective now.',
                     kind='explicit_task_authorization', source_kind='user_message', source_id='message-' + identity,
                     goal_id='goal-' + identity, thread_id='thread-' + identity, run_id='run-' + identity,
                     scope=dict(kind='objective', id='scope-' + identity, objective='deliver'), observed_at=self.now, source_created_at=self.now, original_turn_id='turn-'+str(self.now), observed_turn_id='turn-'+str(self.now))
        return dict(authorization=event, interpretation='Classified direct user request.',
                    source_message={k: event[k] for k in self.m.SOURCE_FIELDS},
                    app_goal_observation=dict(goal_id=event['goal_id'], thread_id=event['thread_id'], status='active',
                                              source_kind='get_goal', source_id='tool-' + identity, observed_at=self.now))

    def project(self, checkpoints=0, pinned=False):
        self.counter += 1
        path = self.root / ('project-' + str(self.counter))
        path.mkdir()
        if pinned:
            command = self.candidate_command(path, 'status')
            result = subprocess.run(command, input='{}', capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        base = path / '.agent-workflow-v1-1/runs/run-old' if pinned else None
        engine = self.m.Workflow(path, self.home, lambda: self.now, state_base=base)
        engine.execute('activate', dict(objective='deliver', **self.evidence(), models={'gpt-6.1-sol': ['high']},
                                        slot_limit=4, inventory=[], observed_at=self.now))
        engine.execute('usage', dict(observed_at=self.now, rateLimits={
            'primary': dict(usedPercent=10, windowDurationMins=300),
            'secondary': dict(usedPercent=20, windowDurationMins=10080)}))
        (path / 'artifact.txt').write_text('recoverable content', encoding='utf-8')
        for _ in range(checkpoints):
            engine.execute('snapshot', dict(writers_quiescent=True, files=['artifact.txt'], next_action='revalidate'))
        return engine

    def inventory(self, path):
        return {p.relative_to(path).as_posix(): self.m.digest(p) for p in path.rglob('*') if p.is_file()}

    def receipt(self, **values):
        if os.environ.get('A12_EMIT_RECEIPTS') == '1':
            print(json.dumps({'evidence': 'A12_DISPOSABLE_FAULT', **values}, sort_keys=True), flush=True)


    def test_contradictory_same_id_current_authorization_rejects_without_losing_source(self):
        engine = self.project()
        state = self.m.read_json(engine.path)
        state['authorization_events'][0]['event']['source_id'] = 'different-ledger-message'
        state['authorization_events'][0]['source_message']['source_id'] = 'different-ledger-message'
        self.m.write_json(engine.path, state)
        for command in ('status', 'stop'):
            before = self.inventory(engine.root)
            with self.assertRaisesRegex(self.m.Error, 'contradicts same-ID replay ledger'):
                engine.execute(command, {})
            self.assertEqual(self.inventory(engine.root), before)

    def test_unknown_older_future_and_malformed_state_rejected_before_mutation(self):
        engine = self.project()
        original = self.m.read_json(engine.path)
        for change in ({'schema_version': 1}, {'schema_version': 0}, {'schema_version': 3}, {'schema_version': True}, {'schema_version': '1'},
                       {'tasks': []}, {'status': False}, {'inventory': {}}, {'usage': []}, {'unexpected': 'x'},
                       {'migration': {}}, {'legacy_authorization_events': []}, {'revalidation_required': True}, {'usage_errors': True}, {'authorization_event_ids': ['event-old', 'event-old']}):
            with self.subTest(change=change):
                bad = copy.deepcopy(original); bad.update(change)
                self.m.write_json(engine.path, bad)
                before = self.inventory(engine.root)
                with self.assertRaises(self.m.Error):
                    engine.execute('stop', {})
                self.assertEqual(self.inventory(engine.root), before)
        for raw in (b'{"status":', b'{"status":"inactive","status":"active","tasks":{}}',
                    b'{"schema_version":2,"status":"inactive","tasks":{},"created_at":NaN}', b'\xff'):
            engine.path.write_bytes(raw)
            before = self.inventory(engine.root)
            with self.assertRaises((self.m.Error, UnicodeError)):
                engine.execute('status', {})
            self.assertEqual(self.inventory(engine.root), before)

    def assert_recoverable(self, engine, previous, promoted):
        state = self.m.read_json(engine.path)
        self.m.validate_state(state)
        if promoted:
            self.assertNotEqual(state['checkpoint']['id'], previous['checkpoint']['id'])
            self.assertEqual(state['previous_checkpoint'], previous['checkpoint'])
        else:
            self.assertEqual(state['checkpoint'], previous['checkpoint'])
        for pointer in (state['checkpoint'], previous['checkpoint']):
            directory, manifest = engine.verify_checkpoint({'checkpoint': pointer})
            self.assertEqual((directory / 'files/artifact.txt').read_text(), 'recoverable content')
            self.assertEqual(self.m.digest(directory / 'files/artifact.txt'), manifest['files']['artifact.txt'])
        self.assertEqual((engine.root / 'artifact.txt').read_text(), 'recoverable content')

    def test_supported_snapshot_fault_matrix_keeps_old_or_new_pointer(self):
        for point in sorted(self.m.FaultControl.POINTS):
            for action in ('error', 'disk-full'):
                with self.subTest(point=point, action=action):
                    engine = self.project(checkpoints=2)
                    previous = self.m.read_json(engine.path)
                    spec = dict(point=point, action=action)
                    if point.startswith(('temp.', 'replace.')):
                        spec['path_suffix'] = engine.path.as_posix()
                    fault = self.m.FaultControl(self.root, spec); engine.fault = fault
                    promoted = point in {'replace.after', 'checkpoint.pointer.after', 'prune.before', 'prune.after'}
                    if point.startswith('prune.'):
                        receipt = engine.execute('snapshot', dict(writers_quiescent=True, files=['artifact.txt'], next_action='next'))
                        self.assertIn('cleanup_warning', receipt)
                    else:
                        with self.assertRaises(OSError):
                            engine.execute('snapshot', dict(writers_quiescent=True, files=['artifact.txt'], next_action='next'))
                    self.assertTrue(fault.fired)
                    self.assertFalse((engine.base / 'coordinator.lock').exists())
                    self.assert_recoverable(engine, previous, promoted)
                    current = self.m.read_json(engine.path)
                    self.receipt(kind='injected_snapshot', point=point, action=action,
                                 pointer='new' if promoted else 'old', previous_good=previous['checkpoint']['id'],
                                 committed=current['checkpoint']['id'], artifact_hash=self.m.digest(engine.root / 'artifact.txt'))

    def test_checkpoint_metadata_write_faults_and_truncated_candidate_keep_pointer(self):
        for name in ('owner.json', 'state.json', 'manifest.json'):
            for point in ('temp.before', 'temp.after', 'replace.before', 'replace.after'):
                with self.subTest(name=name, point=point):
                    engine = self.project(checkpoints=1)
                    before = self.m.read_json(engine.path)
                    # Checkpoint state is written before the main pointer file.
                    engine.fault = self.m.FaultControl(self.root, dict(point=point, action='error', path_suffix=name))
                    with self.assertRaises(OSError):
                        engine.execute('snapshot', dict(writers_quiescent=True, files=['artifact.txt'], next_action='next'))
                    self.assert_recoverable(engine, before, False)
        engine = self.project(checkpoints=1)
        before = self.m.read_json(engine.path)
        engine.fault = self.m.FaultControl(self.root, dict(point='temp.after', action='truncated', path_suffix=engine.path.as_posix()))
        with self.assertRaises(self.m.Error):
            engine.execute('snapshot', dict(writers_quiescent=True, files=['artifact.txt'], next_action='next'))
        self.assert_recoverable(engine, before, False)
        self.assertEqual(list(engine.base.glob('state.json.tmp-*')), [])


    def test_fault_controls_reject_live_package_unknown_points_and_missing_marker(self):
        marker = self.root / 'DISPOSABLE_FAULT_TEST.json'
        marker.unlink()
        with self.assertRaises((OSError, self.m.Error)):
            self.m.FaultControl(self.root, dict(point='temp.after', action='error'))
        self.m.write_json(marker, {'disposable': True})
        for spec in (dict(point='copy.during', action='error'), dict(point='replace.before', action='truncated'), dict(point='temp.after', action='power-loss')):
            with self.assertRaises(self.m.Error): self.m.FaultControl(self.root, spec)
        live_spec = importlib.util.spec_from_file_location('live_workflow_fault_check', Path(__file__).resolve().parents[1] / 'scripts/workflow.py')
        live = importlib.util.module_from_spec(live_spec); live_spec.loader.exec_module(live)
        with self.assertRaises(live.Error): live.FaultControl(self.root, dict(point='temp.after', action='error'))

    def test_disposable_marker_requires_exact_schema_and_boolean_true(self):
        marker = self.root / 'DISPOSABLE_FAULT_TEST.json'
        for bad in ({'disposable': 1}, {'disposable': 1.0}, {'disposable': 'true'}, {'disposable': False},
                    {'disposable': True, 'extra': True}, {}, {'other': True}, True, [True]):
            with self.subTest(marker=bad):
                self.m.write_json(marker, bad)
                before = self.inventory(self.root)
                with self.assertRaisesRegex(self.m.Error, 'explicit disposable fault-test marker required'):
                    self.m.FaultControl(self.root, dict(point='temp.after', action='error'))
                self.assertEqual(self.inventory(self.root), before)
        self.m.write_json(marker, {'disposable': True})
        self.assertFalse(self.m.FaultControl(self.root, dict(point='temp.after', action='error')).fired)

    def candidate_command(self, project, command):
        interpreter = getattr(sys, '_base_executable', sys.executable)
        return [interpreter, *(['-O'] if sys.flags.optimize else []), str(self.package / 'scripts/candidate.py'),
                '--manifest', str(self.manifest), '--manifest-sha256', self.pin, '--version', '1.1',
                '--project', str(project), '--run-id', 'run-old', 'workflow', command]

    def worker_identity(self, child, command):
        """Inspect only the worker attached to our still-live Popen handle."""
        self.assertIsNone(child.poll(), 'owned worker already exited')
        if os.name == 'nt':
            query = subprocess.run(['powershell', '-NoProfile', '-Command',
                f'Get-CimInstance Win32_Process -Filter "ProcessId={child.pid}" | '
                'Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress'],
                capture_output=True, text=True, check=True, timeout=10)
            identity = json.loads(query.stdout)
            self.assertEqual(identity['ProcessId'], child.pid)
            # Store filesystem redirection can expose two path spellings for
            # one executable; compare file identity rather than resolved text.
            self.assertTrue(os.path.samefile(identity['ExecutablePath'], command[0]), 'owned executable identity mismatch')
            import ctypes
            from ctypes import wintypes
            shell = ctypes.WinDLL('shell32', use_last_error=True)
            shell.CommandLineToArgvW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
            shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
            count = ctypes.c_int()
            argv = shell.CommandLineToArgvW(identity['CommandLine'], ctypes.byref(count))
            self.assertTrue(argv)
            try:
                actual = [argv[index] for index in range(count.value)]
            finally:
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.LocalFree.argtypes = (wintypes.HLOCAL,)
                kernel.LocalFree.restype = wintypes.HLOCAL
                kernel.LocalFree(ctypes.cast(argv, ctypes.c_void_p))
            self.assertEqual(actual, command)
            return identity
        actual = Path(f'/proc/{child.pid}/cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
        self.assertEqual(actual, command)
        self.assertEqual(Path(f'/proc/{child.pid}/exe').resolve(), Path(command[0]).resolve())
        return {'ProcessId': child.pid, 'ExecutablePath': command[0], 'CommandLine': actual}

    def terminate_paused_worker(self, child, command, reached, point):
        deadline = time.monotonic() + 8
        while not reached.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.03)
        self.assertTrue(reached.exists(), 'child did not reach supported cutpoint')
        marker = self.m.read_json(reached)
        self.assertEqual((marker['pid'], marker['point']), (child.pid, point))
        identity = self.worker_identity(child, command)
        child.terminate()
        output, error = child.communicate(timeout=5)
        self.assertNotEqual(child.returncode, 0, (output, error))
        self.assertIsNotNone(child.poll(), 'same worker handle must become terminal')
        return marker, identity

    def close_worker(self, child, command):
        if child.poll() is None:
            self.worker_identity(child, command)
            child.kill()
            child.communicate(timeout=5)
        self.assertIsNotNone(child.poll())
        if child.stdout: child.stdout.close()
        if child.stderr: child.stderr.close()

    def test_real_child_termination_at_every_supported_cutpoint(self):
        for point in sorted(self.m.FaultControl.POINTS):
            with self.subTest(point=point):
                (self.root / 'fault-spec.json').unlink(missing_ok=True)
                engine = self.project(checkpoints=2, pinned=True)
                before = self.m.read_json(engine.path)
                spec = dict(point=point, action='pause')
                if point.startswith(('temp.', 'replace.')):
                    spec['path_suffix'] = engine.path.as_posix()
                fault_file = self.root / 'fault-spec.json'; self.m.write_json(fault_file, spec)
                reached = self.root / 'FAULT_REACHED.json'; reached.unlink(missing_ok=True)
                # Windows venv launchers may spawn a different interpreter PID.
                # Launch the base interpreter directly so termination targets the worker.
                command = self.candidate_command(engine.root, 'snapshot')
                child = subprocess.Popen(command,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    child.stdin.write(json.dumps(dict(writers_quiescent=True, files=['artifact.txt'], next_action='after termination')))
                    child.stdin.close(); child.stdin = None
                    marker, identity = self.terminate_paused_worker(child, command, reached, point)
                    self.assertTrue((engine.base / 'coordinator.lock').exists())
                    promoted = point in {'replace.after', 'checkpoint.pointer.after', 'prune.before', 'prune.after'}
                    self.assert_recoverable(engine, before, promoted)
                    with self.assertRaisesRegex(self.m.Error, 'stale lock'):
                        engine.execute('status', {})
                    # Explicit operator inspection, confined to this terminated disposable child.
                    self.assertEqual((engine.base / 'coordinator.lock').read_text(), str(child.pid))
                    (engine.base / 'coordinator.lock').unlink()
                    restored = self.root / ('restored-' + str(self.counter))
                    engine.execute('restore', {'destination': str(restored)})
                    self.assertEqual((restored / 'files/artifact.txt').read_text(), 'recoverable content')
                    state = self.m.read_json(engine.path)
                    self.receipt(kind='real_child_termination', point=point, pid=child.pid,
                                 marker_pid=marker['pid'], exit_code=child.returncode, pointer='new' if promoted else 'old',
                                 old_checkpoint=before['checkpoint']['id'], committed_checkpoint=state['checkpoint']['id'],
                                 restored_hash=self.m.digest(restored / 'files/artifact.txt'), process_terminal=child.poll() is not None,
                                 process_identity=identity)
                finally:
                    self.close_worker(child, command)


if __name__ == '__main__':
    unittest.main()
