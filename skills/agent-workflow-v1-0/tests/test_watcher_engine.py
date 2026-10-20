"""A11 synthetic-state tests; never read/copy account profiles or live monitor state."""
import contextlib
import functools
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
MONITOR_SOURCE = ROOT / 'skills/agent-workflow-v1-0/private/limit-watcher/scripts/monitor.py'
WORKFLOW_SOURCE = ROOT / 'skills/agent-workflow-v1-0/scripts/workflow.py'
# Current v1.0 core pin; fixtures supply their helper explicitly.
WORKFLOW_PIN = 'e1e463b347347f214b882385f1eb46fa26de03b1dea20afaba38f69b50deb5f9'


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def windows(primary=20, weekly=20):
    return {300: {'windowDurationMins': 300, 'usedPercent': primary, 'resetsAt': 9999999999},
            10080: {'windowDurationMins': 10080, 'usedPercent': weekly, 'resetsAt': 9999999999}}


class MonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix='a11-source-fixture-')
        cls.root = Path(cls.fixture.name)
        monitor_bytes, workflow_bytes = MONITOR_SOURCE.read_bytes(), WORKFLOW_SOURCE.read_bytes()
        if hashlib.sha256(workflow_bytes).hexdigest() != WORKFLOW_PIN:
            raise RuntimeError('shared workflow pin changed; coordinator must revalidate dependency')
        cls.monitor_path = cls.root / 'skills/agent-workflow-v1-0/private/limit-watcher/scripts/monitor.py'
        workflow_path = cls.root / 'skills/agent-workflow-v1-0/scripts/workflow.py'
        for path, content in ((cls.monitor_path, monitor_bytes), (workflow_path, workflow_bytes)):
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
        cls.m = load(cls.monitor_path, 'monitor_a11_fixture')
        cls.schedule = staticmethod(cls.m.workflow_schedule(helper=workflow_path, helper_sha256=WORKFLOW_PIN))
        cls.schedule_loader = staticmethod(cls.m.workflow_schedule)
        cls.m.workflow_schedule = functools.partial(cls.m.workflow_schedule,
            helper=workflow_path, helper_sha256=WORKFLOW_PIN)
        print('A11_SOURCE_PINS=' + json.dumps({'monitor': hashlib.sha256(monitor_bytes).hexdigest(),
                                              'workflow': WORKFLOW_PIN, 'python': sys.version,
                                              'optimized': sys.flags.optimize}), flush=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='a11-synthetic-state-')
        self.base = Path(self.temp.name)
        self.now = time.time()
        self.control = self.m.default_control('synthetic-run', self.now)
        self.m.write(self.base / 'control.json', self.control)

    def tearDown(self):
        self.temp.cleanup()

    def sample(self, p=20, w=20, previous=None, control=None, at=None, measurer=None):
        return self.m.sample(previous, control or self.control, measurer or (lambda: windows(p, w)),
                             self.now if at is None else at, self.schedule)

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = self.m.main([*args, '--state-dir', str(self.base)])
        return code, json.loads(output.getvalue())

    def marker(self, process=None, token='synthetic-token'):
        actual = self.m.identity(process.pid if process else os.getpid())
        self.assertEqual(actual['state'], 'live')
        value = {key: actual[key] for key in ('pid', 'startIdentity')}
        value.update(workerToken=token, runId=self.control['runId'], epoch=self.control['epoch'])
        self.m.write(self.base / 'running.json', value)
        self.m.event(self.base, value, 'running', control=self.control)
        self.m.write(self.base / 'latest.json', self.sample())
        return value

    def test_schedule_without_explicit_helper_and_pin_is_refused(self):
        with self.assertRaises(self.m.MonitorError):
            self.schedule_loader()
        with self.assertRaises(self.m.MonitorError):
            self.schedule_loader(helper=WORKFLOW_SOURCE)

    def test_thresholds_and_actual_two_windows(self):
        for remaining, action in ((80, 'CONTINUE'), (25, 'CHECKPOINT_SOON'), (21, 'CHECKPOINT_SOON'), (20, 'SAVE_AND_STOP'),
                                  (10, 'SAVE_AND_STOP'), (0, 'SAVE_AND_STOP')):
            with self.subTest(remaining=remaining):
                result = self.sample(100 - remaining)
                self.assertEqual(result['action'], action)
                self.assertEqual(set(result['windows']), {'300', '10080'})
        self.assertEqual(self.sample(20, 95)['exitReason'], 'usage_threshold')
        self.assertEqual(self.sample(80, 20)['exitReason'], 'usage_threshold')

    def test_standalone_stop_before_start_keeps_generated_identity(self):
        (self.base / 'control.json').unlink()
        code, result = self.cli('stop')
        self.assertEqual(code, 0)
        self.assertTrue(result['stopRequested'])
        control = self.m.load_control(self.base)
        self.assertTrue(control['runId'])
        self.assertFalse(control['active'])
        self.assertEqual(self.cli('stop')[0], 0)
        self.assertEqual(self.m.load_control(self.base), control)
        self.assertEqual(self.cli('status')[0], 0)

    def test_early_reserve_120_seconds_and_reset_boundary(self):
        previous = self.sample(40, at=self.now - 60)
        result = self.sample(60, previous=previous)
        self.assertEqual(result['exitReason'], 'early_reserve')
        self.assertGreater(result['remaining'], 10)
        self.assertLessEqual(result['estimated_seconds_to_threshold'], 120)
        previous['windows']['300']['resetsAt'] -= 1
        self.assertEqual(self.sample(60, previous=previous)['action'], 'CONTINUE')

    def test_missing_window_and_unknown_retry_once(self):
        for broken in ({300: windows()[300]}, windows(float('nan')), windows(True),
                       windows(-1), windows(101), {300: None, 10080: None}):
            with self.subTest(broken=repr(broken)):
                calls = []
                result = self.sample(measurer=lambda: calls.append(1) or broken)
                self.assertEqual(len(calls), 2)
                self.assertEqual(result['measurementAttempts'], 2)
                self.assertEqual(result['exitReason'], 'measurement_failure')
                self.assertEqual(result['action'], 'SAVE_AND_STOP')
                self.assertEqual(result['errorType'], 'MonitorError')

    def test_retry_success_and_exception_sanitization(self):
        calls = []
        def measure():
            calls.append(1)
            if len(calls) == 1:
                raise OSError('secret-account-credential')
            return windows()
        result = self.sample(measurer=measure)
        self.assertEqual(result['measurementAttempts'], 2)
        def fail():
            raise OSError('secret-account-credential')
        result = self.sample(measurer=fail)
        self.assertNotIn('secret-account', json.dumps(result))
        self.assertEqual(result['errorType'], 'OSError')

    def test_integer_overflow_rejects_before_conversion_and_retries_once(self):
        huge = 10 ** 400
        for value in (huge, -huge, True, float('inf'), float('nan')):
            self.assertFalse(self.m.number(value))
        for value in (0, 20, 9999999999, self.m.MAX_FINITE_INTEGER, sys.float_info.max):
            self.assertTrue(self.m.number(value))
        for value in (huge, -huge):
            with self.assertRaisesRegex(self.m.MonitorError, 'integer outside finite numeric range'):
                self.m.strict_json(json.dumps({'ignored': [{'number': value}]}))
        for field in ('usedPercent', 'resetsAt'):
            broken = windows()
            broken[300][field] = huge
            calls = []
            result = self.sample(measurer=lambda: calls.append(1) or broken)
            self.assertEqual((len(calls), result['measurementAttempts']), (2, 2))
            self.assertEqual((result['action'], result['errorType']), ('SAVE_AND_STOP', 'MonitorError'))
            self.assertNotIn('windows', result)

    def test_every_health_numeric_field_and_pid_bounds_fail_closed(self):
        self.marker()
        valid = self.m.read(self.base / 'health.json')
        for field in ('schemaVersion', 'pid', 'heartbeatAt', 'leaseExpiresAt'):
            for value in (10 ** 400, -(10 ** 400), True):
                with self.subTest(field=field, value=repr(value)):
                    self.m.write(self.base / 'health.json', dict(valid, **{field: value}))
                    result = self.m.status(self.base)
                    self.assertEqual(result['action'], 'HOLD')
                    self.assertEqual(result['health']['identityState'], 'unknown')
                    self.assertFalse(result['health']['healthy'])
                    self.assertEqual(result['health']['errorType'], 'MonitorError')
        self.m.write(self.base / 'health.json', dict(valid, pid=self.m.MAX_PID + 1))
        self.assertEqual(self.m.status(self.base)['action'], 'HOLD')
        for pid in (0, True, self.m.MAX_PID + 1, 10 ** 400):
            with self.assertRaises(self.m.MonitorError):
                self.m.identity(pid)

    def test_nested_history_validates_all_fields_and_preserves_produced_events(self):
        marker = self.marker()
        self.m.event(self.base, marker, 'stopped', 'explicit_stop', control=self.control)
        self.m.event(self.base, marker, 'running', control=self.control)
        valid = self.m.read_health(self.base)
        self.assertEqual([item['state'] for item in valid['history']], ['running', 'stopped', 'running'])
        self.assertTrue(self.m.status(self.base)['health']['healthy'])
        entry = dict(valid['history'][0])
        invalid = [{}, None, 'scalar', 1, [None], [[]], [dict(entry, at=[])],
                   [dict(entry, at=10 ** 400)], [dict(entry, at=-1)], [dict(entry, at=True)],
                   [dict(entry, state={})], [dict(entry, state='unknown')],
                   [dict(entry, exitReason=[])], [dict(entry, exitReason='unknown')],
                   [dict(entry, errorType={})], [dict(entry, extra='private-secret')],
                   [{key: value for key, value in entry.items() if key != 'errorType'}],
                   [entry] * (self.m.HISTORY_LIMIT + 1)]
        before = (self.base / 'control.json').read_bytes()
        for history in invalid:
            with self.subTest(history=repr(history)):
                self.m.write(self.base / 'health.json', dict(valid, history=history))
                result = self.m.status(self.base)
                self.assertEqual((result['action'], result['health']['identityState']), ('HOLD', 'unknown'))
                self.assertFalse(result['health']['healthy'])
                self.assertEqual(result['health']['errorType'], 'MonitorError')
                with patch.object(self.m, 'measure', side_effect=RuntimeError('must not measure')) as call:
                    for command in ('start', 'once', 'renew'):
                        self.assertEqual(self.cli(command)[0], 2)
                    call.assert_not_called()
                self.assertEqual((self.base / 'control.json').read_bytes(), before)
        self.m.write(self.base / 'health.json', valid)
        self.assertEqual(self.m.read_health(self.base)['history'], valid['history'])
        self.assertTrue(self.m.status(self.base)['health']['healthy'])
        optional = dict(valid)
        optional.pop('history')
        self.m.write(self.base / 'health.json', optional)
        self.assertTrue(self.m.status(self.base)['health']['healthy'])

    def test_exception_scope_expiry_and_actual_measurements(self):
        control = dict(self.control, policyException={'runId': 'synthetic-run', 'userQuote': 'Haftalığı bu koşumda yok say.',
                       'validFrom': self.now - 1, 'validUntil': self.now + 10, 'decisionWindows': [300]})
        result = self.sample(20, 99, control=control)
        self.assertEqual(result['action'], 'CONTINUE')
        self.assertEqual(result['windows']['10080']['usedPercent'], 99)
        self.assertEqual(result['policy']['actualWindows'], [300, 10080])
        self.assertFalse(result['policy']['authorizationLifecycleChanged'])
        self.assertEqual(self.sample(95, 99, control=control)['action'], 'SAVE_AND_STOP')
        self.assertEqual(self.sample(20, 99, control=control, at=self.now + 10)['action'], 'SAVE_AND_STOP')
        control['policyException'] = None
        self.assertEqual(self.sample(20, 99, control=control)['action'], 'SAVE_AND_STOP')

    def test_exception_and_control_runtime_guards_survive_optimization(self):
        invalid = [dict(self.control, active=1), dict(self.control, expiresAt=float('inf')),
                   dict(self.control, continuedAt=self.now), dict(self.control, schemaVersion=True)]
        template = {'runId': 'other-run', 'userQuote': 'quote', 'validFrom': 0, 'validUntil': 10, 'decisionWindows': [300]}
        invalid.append(dict(self.control, policyException=template))
        for key, value in (('runId', 'synthetic-run'),):
            template = dict(template, **{key: value})
        for key, value in (('userQuote', ' '), ('decisionWindows', [10080]), ('validUntil', 0)):
            invalid.append(dict(self.control, policyException=dict(template, **{key: value})))
        invalid.append(dict(self.control, policyException=dict(template, decisionWindows=[300.0])))
        for control in invalid:
            with self.subTest(control=control), self.assertRaises(self.m.MonitorError):
                self.m.validate_control(control)
        with self.assertRaises(self.m.MonitorError):
            self.m.read(self._file('duplicate.json', '{"x":1,"x":2}'))
        with self.assertRaises(self.m.MonitorError):
            self.m.read(self._file('nonfinite.json', '{"x":NaN}'))

    def _file(self, name, contents):
        path = self.base / name
        path.write_text(contents, encoding='utf-8')
        return path

    def test_stale_previous_rate_is_not_reused(self):
        old = self.sample(20, at=self.now - 601)
        old['rates'] = {'300': 100}
        self.assertEqual(self.sample(21, previous=old)['action'], 'CONTINUE')

    def test_save_latched_across_once_start_status_and_renew(self):
        saved = self.sample(95)
        self.m.latch(self.base, saved, self.control)
        self.m.write(self.base / 'latest.json', self.sample())
        with patch.object(self.m, 'measure', side_effect=RuntimeError('must not measure')):
            for command in ('once', 'start', 'status', 'renew'):
                code, result = self.cli(command)
                self.assertEqual(code, 0)
                self.assertEqual(result['action'], 'SAVE_AND_STOP')
        self.assertEqual(self.m.read(self.base / 'save-latch.json')['result'], saved)

    def test_historical_latest_save_and_epoch_alone_do_not_relax(self):
        saved = self.sample(95)
        self.m.write(self.base / 'latest.json', saved)
        self.assertEqual(self.cli('once')[1]['action'], 'SAVE_AND_STOP')
        self.m.latch(self.base, saved, self.control)
        self.m.write(self.base / 'control.json', dict(self.control, epoch='forged-epoch'))
        self.assertEqual(self.cli('renew')[0], 2)

    def test_fresh_post_save_continuation_and_stale_quote_rejection(self):
        saved = self.sample(95, at=self.now - 5)
        self.m.latch(self.base, saved, self.control)
        self.m.write(self.base / 'latest.json', saved)
        self.assertEqual(self.cli('once', '--new-epoch', '--run-id', 'new-run')[0], 2)
        stale = dict(self.control, epoch='forged', continuationQuote='old quote', continuedAt=saved['at'])
        with self.assertRaises(self.m.MonitorError):
            self.m.latched_result(self.base, stale)
        with patch.object(self.m, 'measure', return_value=windows()):
            code, result = self.cli('once', '--new-epoch', '--run-id', 'new-run', '--continuation-quote', 'Şimdi devam et.')
        self.assertEqual(code, 0)
        self.assertEqual(result['action'], 'CONTINUE')
        self.assertEqual(result['policy']['runId'], 'new-run')
        self.assertNotEqual(result['policy']['epoch'], self.control['epoch'])
        self.assertEqual(self.m.read(self.base / 'save-latch.json')['result'], saved)

    def test_new_native_transition_can_open_epoch_without_fake_user_quote(self):
        saved = self.sample(95, at=self.now - 5)
        self.m.latch(self.base, saved, self.control)
        self.m.write(self.base / 'latest.json', saved)
        stale = dict(self.control, epoch='forged',
                     nativeTransition={'updatedAt': saved['at'], 'sourceId': 'receipt-old'})
        with self.assertRaises(self.m.MonitorError):
            self.m.latched_result(self.base, stale)
        with patch.object(self.m, 'measure', return_value=windows()):
            code, result = self.cli('once', '--new-epoch', '--run-id', 'new-run',
                                    '--native-transition-at', str(self.now - 1),
                                    '--native-receipt', 'receipt-new')
        self.assertEqual(code, 0)
        self.assertEqual(result['action'], 'CONTINUE')
        new_control = self.m.load_control(self.base)
        self.assertNotIn('continuationQuote', new_control)
        self.assertEqual(new_control['nativeTransition']['sourceId'], 'receipt-new')

    def test_live_health_lease_sample_and_heartbeat_are_separate(self):
        self.marker()
        result = self.m.status(self.base)
        self.assertTrue(result['health']['healthy'])
        control = dict(self.control, expiresAt=self.now - 1)
        self.m.write(self.base / 'control.json', control)
        result = self.m.status(self.base)
        self.assertTrue(result['health']['liveIdentity'])
        self.assertTrue(result['health']['heartbeatFresh'])
        self.assertFalse(result['health']['leaseValid'])
        self.assertFalse(result['health']['healthy'])
        self.m.write(self.base / 'control.json', self.control)
        health = self.m.read(self.base / 'health.json')
        health['heartbeatAt'] = self.now - 6
        self.m.write(self.base / 'health.json', health)
        self.assertFalse(self.m.status(self.base)['health']['heartbeatFresh'])
        latest = self.m.read(self.base / 'latest.json')
        latest['at'] = self.now - 301
        self.m.write(self.base / 'latest.json', latest)
        result = self.m.status(self.base)
        self.assertEqual(result['action'], 'READ_USAGE')
        self.assertFalse(result['health']['sampleFresh'])

    def test_absent_marker_and_wrong_epoch_are_not_healthy(self):
        self.m.write(self.base / 'latest.json', self.sample())
        self.assertFalse(self.m.status(self.base)['health']['healthy'])
        self.marker()
        control = dict(self.control, epoch='new-epoch')
        self.m.write(self.base / 'control.json', control)
        result = self.m.status(self.base)
        self.assertFalse(result['health']['workerEpochMatches'])
        self.assertFalse(result['health']['sampleFresh'])

    def test_pid_reuse_and_unknown_identity_never_kill_or_duplicate(self):
        marker = self.marker()
        with patch.object(self.m, 'identity', return_value={'state': 'live', 'pid': marker['pid'], 'startIdentity': 'different'}):
            self.assertEqual(self.m.marker_state(self.base)['identityState'], 'pid_reused')
            self.assertFalse(self.m.status(self.base)['health']['healthy'])
            self.m.claim_start(self.base)
        self.assertEqual(self.m.identity(os.getpid())['state'], 'live')
        self.marker()
        before = (self.base / 'control.json').read_bytes()
        with patch.object(self.m, 'identity', return_value={'state': 'unknown'}):
            self.assertEqual(self.m.status(self.base)['action'], 'HOLD')
            for command in ('start', 'once', 'renew'):
                self.assertEqual(self.cli(command)[0], 2)
            self.assertEqual((self.base / 'control.json').read_bytes(), before)
        with self.assertRaises(self.m.MonitorError):
            self.m.claim_start(self.base)

    def test_bounded_diagnostic_history_drops_unapproved_fields(self):
        marker = self.marker()
        self.m.write(self.base / 'health.json', {'history': [{'at': self.now, 'state': 'running',
                     'exitReason': None, 'errorType': None, 'accountId': 'secret'}]})
        for index in range(30):
            self.m.event(self.base, marker, 'stopped' if index % 2 else 'running', 'explicit_stop' if index % 2 else None,
                         control=self.control)
        health = self.m.read(self.base / 'health.json')
        self.assertEqual(len(health['history']), self.m.HISTORY_LIMIT)
        self.assertNotIn('secret', json.dumps(health))
        self.assertEqual(set(health['history'][0]), {'at', 'state', 'exitReason', 'errorType'})

    def test_renew_and_explicit_stop_keep_save_and_make_no_measurement(self):
        with patch.object(self.m, 'measure', side_effect=RuntimeError('must not measure')):
            code, result = self.cli('renew')
            self.assertEqual(code, 0)
            control = self.m.load_control(self.base)
            self.assertGreater(control['expiresAt'], self.now + 599)
            self.assertLessEqual(control['expiresAt'], time.time() + 600)
            code, result = self.cli('stop')
        self.assertEqual(code, 0)
        self.assertTrue(result['stopRequested'])
        self.assertFalse(self.m.load_control(self.base)['active'])

    def test_real_disposable_worker_health_and_explicit_stop(self):
        driver = self._file('worker.py', 'import importlib.util,sys\n'
                f's=importlib.util.spec_from_file_location("m",{str(self.monitor_path)!r});m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n'
                f'm.workflow_schedule=__import__("functools").partial(m.workflow_schedule,helper=__import__("pathlib").Path({str(self.root / "skills/agent-workflow-v1-0/scripts/workflow.py")!r}),helper_sha256={WORKFLOW_PIN!r})\n'
                f'm.measure=lambda: {windows()!r}\n'
                'sys.exit(m.main(["run","--state-dir",sys.argv[1]]))\n')
        owned = self.m.OwnedProcess([sys.executable, str(driver), str(self.base)], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding='utf-8')
        proc = owned.proc
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if (self.base / 'latest.json').exists() and self.m.status(self.base)['health']['healthy']:
                    break
                time.sleep(0.05)
            result = self.m.status(self.base)
            self.assertTrue(result['health']['healthy'])
            self.assertEqual(self.m.identity(result['health']['pid'])['startIdentity'], result['health']['startIdentity'])
            self.assertNotEqual(result['health']['pid'], os.getpid())
            self.assertEqual(self.cli('start')[0], 0)
            self.assertEqual(self.m.read(self.base / 'running.json')['pid'], result['health']['pid'])
            self.assertEqual(self.cli('stop')[0], 0)
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, err)
            self.assertEqual(json.loads(out)['exitReason'], 'explicit_stop')
            self.assertEqual(self.m.read(self.base / 'health.json')['exitReason'], 'explicit_stop')
            self.assertFalse((self.base / 'running.json').exists())
            self.assertFalse(self.m.status(self.base)['health']['liveIdentity'])
            print('A11_REAL_WORKER=' + json.dumps({'workerPid': result['health']['pid'],
                  'launcherPid': proc.pid,
                  'startIdentity': result['health']['startIdentity'], 'exitCode': proc.returncode,
                  'terminal': 'explicit_stop'}), flush=True)
        finally:
            owned.close()
            for stream in (proc.stdout, proc.stderr):
                stream.close()

    def test_actual_start_handshake_reports_worker_and_stops(self):
        driver = self._file('start-driver.py', 'import importlib.util,sys\n'
                f's=importlib.util.spec_from_file_location("m",{str(self.monitor_path)!r});m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n'
                f'm.workflow_schedule=__import__("functools").partial(m.workflow_schedule,helper=__import__("pathlib").Path({str(self.root / "skills/agent-workflow-v1-0/scripts/workflow.py")!r}),helper_sha256={WORKFLOW_PIN!r})\n'
                f'm.measure=lambda: {windows()!r}\n'
                'sys.exit(m.main(sys.argv[1:]))\n')
        real_owner = self.m.OwnedProcess
        owners = []
        def factory(argv, **kwargs):
            script_index = argv.index(str(self.monitor_path))
            self.assertGreater(script_index, 0)
            owner = real_owner([*argv[:script_index], str(driver), *argv[script_index + 1:]], **kwargs)
            owners.append(owner)
            return owner
        try:
            with patch.object(self.m, 'OwnedProcess', factory):
                code, result = self.cli('start')
            self.assertEqual(code, 0, result)
            self.assertTrue(result['health']['liveIdentity'])
            self.assertTrue(result['health']['heartbeatFresh'])
            pid, start = result['health']['pid'], result['health']['startIdentity']
            self.assertEqual(self.m.identity(pid)['startIdentity'], start)
            self.assertEqual(self.cli('stop')[0], 0)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (self.base / 'running.json').exists():
                time.sleep(0.05)
            self.assertFalse((self.base / 'running.json').exists())
            self.assertEqual(self.m.read(self.base / 'health.json')['exitReason'], 'explicit_stop')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.m.identity(pid)['state'] != 'absent':
                time.sleep(0.05)
            self.assertEqual(self.m.identity(pid)['state'], 'absent')
            print('A11_START_HANDSHAKE=' + json.dumps({'launcherPid': owners[0].proc.pid,
                  'actualWorkerPid': pid, 'startIdentity': start, 'terminal': 'explicit_stop'}), flush=True)
        finally:
            self.cli('stop')
            for owner in owners:
                owner.proc.wait(timeout=5)

    def test_failed_worker_keeps_local_traceback_for_permission_diagnosis(self):
        driver = self._file('failing-worker.py', 'import importlib.util,sys\n'
                f's=importlib.util.spec_from_file_location("m",{str(self.monitor_path)!r});m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n'
                'def fail_event(*args, **kwargs):\n'
                '    raise PermissionError(13, "fixture health write denied")\n'
                'm.event=fail_event\n'
                'sys.exit(m.main(sys.argv[1:]))\n')
        real_owner = self.m.OwnedProcess
        owners = []
        def factory(argv, **kwargs):
            index = argv.index(str(self.monitor_path))
            owner = real_owner([*argv[:index], str(driver), *argv[index + 1:]], **kwargs)
            owners.append(owner)
            return owner
        try:
            with patch.object(self.m, 'OwnedProcess', factory):
                code, result = self.cli('start')
            self.assertEqual(code, 2, result)
            trace = (self.base/'worker-stderr.log').read_text(encoding='utf-8')
            self.assertIn('PermissionError', trace)
            self.assertIn('fixture health write denied', trace)
            self.assertIn('failing-worker.py', trace)
        finally:
            self.cli('stop')
            for owner in owners:
                owner.proc.wait(timeout=5)

    def test_start_timeout_rejects_historical_terminal_receipt_and_cleans_owner(self):
        marker = self.marker()
        (self.base / 'running.json').unlink()
        self.m.event(self.base, marker, 'stopped', 'explicit_stop', control=self.control)
        real_owner = self.m.OwnedProcess
        owners = []
        def factory(argv, **kwargs):
            owner = real_owner([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            owners.append(owner)
            return owner
        with patch.object(self.m, 'OwnedProcess', factory):
            code, result = self.cli('start')
        self.assertEqual(code, 2)
        self.assertEqual(result['action'], 'HOLD')
        self.assertFalse(self.m.load_control(self.base)['active'])
        self.assertIsNotNone(owners[0].proc.poll())
        self.assertFalse((self.base / 'launching.lock').exists())

    def test_cli_exception_requires_quote_run_and_expiry_and_clear_restores_default(self):
        self.assertEqual(self.cli('renew', '--ignore-weekly-quote', 'quote')[0], 2)
        with patch.object(self.m, 'measure', return_value=windows(20, 99)):
            code, result = self.cli('once', '--run-id', 'synthetic-run', '--ignore-weekly-quote',
                                    'Haftalığı bu koşumda yok say.', '--exception-expires-at', str(self.now + 100))
            self.assertEqual(code, 0)
            self.assertEqual(result['action'], 'CONTINUE')
            self.assertEqual(result['windows']['10080']['usedPercent'], 99)
            code, result = self.cli('once', '--clear-exception')
        self.assertEqual(code, 0)
        self.assertEqual(result['action'], 'SAVE_AND_STOP')
        self.assertEqual(result['policy']['decisionWindows'], [300, 10080])

    def test_policy_expiry_or_removal_invalidates_fresh_ignored_sample(self):
        control = dict(self.control, policyException={'runId': 'synthetic-run', 'userQuote': 'quote',
                       'validFrom': self.now - 1, 'validUntil': self.now + 10, 'decisionWindows': [300]})
        self.m.write(self.base / 'control.json', control)
        self.m.write(self.base / 'latest.json', self.sample(20, 99, control=control))
        self.assertTrue(self.m.status(self.base, self.now)['health']['sampleFresh'])
        self.assertFalse(self.m.status(self.base, self.now + 10)['health']['sampleFresh'])
        self.assertEqual(self.m.status(self.base, self.now + 10)['action'], 'READ_USAGE')
        self.assertEqual(self.cli('renew', '--clear-exception')[1]['action'], 'READ_USAGE')

    def test_background_policy_expiry_resamples_before_original_due(self):
        control = dict(self.control, policyException={'runId': 'synthetic-run', 'userQuote': 'quote',
                       'validFrom': self.now - 1, 'validUntil': self.now + 0.3, 'decisionWindows': [300]})
        self.m.write(self.base / 'control.json', control)
        calls = []
        with patch.object(self.m, 'measure', side_effect=lambda: calls.append(time.time()) or windows(20, 99)):
            result = self.m.run(self.base)
        self.assertEqual(result['exitReason'], 'usage_threshold')
        self.assertEqual(len(calls), 2)
        self.assertLess(calls[1] - calls[0], 2)

    def test_start_and_once_cannot_duplicate_live_worker_or_bypass_launch_gate(self):
        self.marker()
        self.assertEqual(self.cli('start')[0], 0)
        self.assertEqual(self.cli('once')[0], 2)
        self.assertEqual(self.cli('start', '--new-epoch', '--run-id', 'new-run', '--continuation-quote', 'continue')[0], 2)
        self._file('launching.lock', '{}')
        self.assertEqual(self.cli('renew')[0], 2)

    def test_corrupt_marker_is_unknown_and_future_or_stale_heartbeat_unhealthy(self):
        self._file('running.json', '{broken')
        self.assertEqual(self.m.marker_state(self.base)['identityState'], 'unknown')
        (self.base / 'running.json').unlink()
        self.marker()
        health = self.m.read(self.base / 'health.json')
        for timestamp in (time.time() + 100, self.now - 6):
            health['heartbeatAt'] = timestamp
            self.m.write(self.base / 'health.json', health)
            self.assertFalse(self.m.status(self.base)['health']['healthy'])

    def test_terminal_lease_measurement_and_early_save_are_distinct(self):
        for kind, expected in (('expired', 'lease_expired'), ('stopped', 'explicit_stop'),
                               ('missing', 'measurement_failure'), ('early', 'early_reserve')):
            with self.subTest(kind=kind):
                control = self.m.default_control('synthetic-' + kind, self.now)
                if kind == 'expired':
                    control['expiresAt'] = self.now - 1
                if kind == 'stopped':
                    control['active'] = False
                self.m.write(self.base / 'control.json', control)
                if kind == 'early':
                    old = self.sample(40, control=control, at=self.now - 60)
                    # The intended drain comparison needs a measurement now, rather
                    # than waiting for the old sample's scheduled future check.
                    old['next_check_at'] = self.now
                    self.m.write(self.base / 'latest.json', old)
                else:
                    (self.base / 'latest.json').unlink(missing_ok=True)
                (self.base / 'save-latch.json').unlink(missing_ok=True)
                with patch.object(self.m, 'measure', return_value={} if kind == 'missing' else windows(60)):
                    result = self.m.run(self.base)
                self.assertEqual(result['exitReason'], expected)
                self.assertEqual(self.m.read(self.base / 'health.json')['exitReason'], expected)
                self.assertFalse((self.base / 'running.json').exists())

    def rpc_fixture(self, behavior):
        script = self._file('rpc.py', 'import json,sys,time\n'
            'for line in sys.stdin:\n'
            ' value=json.loads(line)\n'
            ' if "id" not in value: continue\n'
            ' if value["id"]==1: result={"label":"Türkçe ı"}\n'
            f' else: {behavior}\n'
            ' print(json.dumps({"id":value["id"],"result":result},ensure_ascii=False),flush=True)\n')
        return script

    def test_utf8_jsonrpc_and_approved_measurement_fields(self):
        rate = {'rateLimits': {'accountId': 'raw-secret', 'primary': windows()[300], 'secondary': windows()[10080]}}
        script = self.rpc_fixture('result=' + repr(rate))
        real_owner = self.m.OwnedProcess
        created = []
        def factory(argv, **kwargs):
            self.assertEqual(kwargs['encoding'], 'utf-8')
            self.assertEqual(kwargs['errors'], 'strict')
            proc = real_owner([sys.executable, '-X', 'utf8', str(script)], **kwargs)
            created.append(proc)
            return proc
        with patch.object(self.m.shutil, 'which', return_value='synthetic-codex'), patch.object(self.m, 'OwnedProcess', factory):
            result = self.m.measure()
        self.assertEqual(result, windows())
        self.assertNotIn('raw-secret', json.dumps(result))
        self.assertIsNotNone(created[0].proc.poll())

    def test_rpc_timeout_cleans_owned_cli_and_descendant(self):
        pid_file = self.base / 'descendant-pid.json'
        script = self._file('timeout.py', 'import subprocess,sys,json,time\n'
             'time.sleep(.2)\n'
             'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\n'
             f'open({str(pid_file)!r},"w").write(json.dumps(child.pid))\n'
             'time.sleep(60)\n')
        real_owner = self.m.OwnedProcess
        created = []
        def factory(argv, **kwargs):
            proc = real_owner([sys.executable, str(script)], **kwargs)
            created.append(proc)
            return proc
        with patch.object(self.m.shutil, 'which', return_value='synthetic-codex'), patch.object(self.m, 'OwnedProcess', factory), \
                patch.object(self.m, 'RPC_TIMEOUT', 0.6):
            with self.assertRaises((TimeoutError, self.m.queue.Empty)):
                self.m.measure()
        self.assertIsNotNone(created[0].proc.poll())
        self.assertTrue(pid_file.exists())
        pid = json.loads(pid_file.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.m.identity(pid)['state'] != 'absent':
            time.sleep(0.05)
        self.assertEqual(self.m.identity(pid)['state'], 'absent')
        print('A11_RPC_CLEANUP=' + json.dumps({'launcherExit': created[0].proc.returncode,
              'descendantPid': pid, 'descendantState': 'absent'}), flush=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
