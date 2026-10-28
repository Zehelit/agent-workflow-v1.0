"""Native usage receipts without a Codex child server or live watcher startup."""
import ast
import copy
import contextlib
import io
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_usage_monitor', PACKAGE / 'private/limit-watcher/scripts/monitor.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
core_spec = importlib.util.spec_from_file_location('native_usage_schedule', PACKAGE / 'scripts/workflow.py')
core = importlib.util.module_from_spec(core_spec)
core_spec.loader.exec_module(core)


class NativeUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-usage-unit-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.now = 10000
        self.control = m.default_control('unit-run', self.now)
        self.control.update(active=False, expiresAt=0)
        m.write(self.base / 'control.json', self.control)

    def receipt(self, primary=20, weekly=20, at=None, source_id='tool-call-1'):
        return {'source': 'mcp__codex_app__get_usage_limits', 'sourceId': source_id,
                'observedAt': self.now if at is None else at, 'result': {
                    'rateLimitsByLimitId': {'codex': {
                        'primary': {'windowDurationMins': 300, 'usedPercent': primary, 'resetsAt': 999999},
                        'secondary': {'windowDurationMins': 10080, 'usedPercent': weekly, 'resetsAt': 999999}}},
                    'accountId': 'private-canary', 'credits': {'secret': 'private-canary'}}}

    def publish(self, receipt=None):
        return m.publish_usage(self.base, self.control, receipt or self.receipt(), self.now)

    def test_native_receipt_does_not_start_process_or_change_control(self):
        before = (self.base / 'control.json').read_bytes()
        with patch.object(m.subprocess, 'Popen', side_effect=AssertionError('no child server')), \
                patch.object(m, 'OwnedProcess', side_effect=AssertionError('no owned server')):
            self.publish()
            reading = m.measure(self.base, self.control, self.now)
        self.assertEqual(set(reading), {300, 10080})
        self.assertEqual(reading.observed_at, self.now)
        self.assertEqual((self.base / 'control.json').read_bytes(), before)
        self.assertNotIn('private-canary', (self.base / 'usage-source.json').read_text())

    def test_wrong_tool_future_and_old_observation_are_rejected(self):
        for change in ({'source': 'shell'}, {'observedAt': self.now + 1},
                       {'observedAt': self.now - 301}, {'sourceId': ''}):
            value = self.receipt(); value.update(change)
            with self.subTest(change=change), self.assertRaises(m.MonitorError):
                self.publish(value)
        self.assertFalse((self.base / 'usage-source.json').exists())

    def test_both_windows_required_and_multi_bucket_must_match(self):
        for kind in ('secondary', 'foreign'):
            value = self.receipt()
            rate = value['result']['rateLimitsByLimitId']['codex']
            if kind == 'secondary':
                rate['secondary'] = None
            else:
                value['result']['rateLimitsByLimitId'] = {'other': rate}
                value['result']['rateLimits'] = rate
            with self.subTest(kind=kind), self.assertRaises(m.MonitorError):
                self.publish(value)

    def test_receipt_replay_cannot_refresh_time_or_rollback(self):
        self.publish()
        old = (self.base / 'usage-source.json').read_bytes()
        self.publish()  # Identical source replay is a no-op.
        self.assertEqual((self.base / 'usage-source.json').read_bytes(), old)
        for value in (self.receipt(at=self.now + 1), self.receipt(at=self.now - 1, source_id='older')):
            with self.assertRaises(m.MonitorError):
                m.publish_usage(self.base, self.control, value, self.now + 2)
        self.assertEqual((self.base / 'usage-source.json').read_bytes(), old)

    def test_receipt_is_bound_to_run_and_epoch(self):
        self.publish()
        for key in ('runId', 'epoch'):
            control = dict(self.control, **{key: 'different'})
            with self.subTest(key=key), self.assertRaises(m.MonitorError):
                m.measure(self.base, control, self.now)

    def test_original_time_and_adaptive_expiry_are_preserved(self):
        self.publish(self.receipt(primary=79))
        result = m.sample(control=self.control, measurer=lambda: m.measure(self.base, self.control, self.now + 20),
                          now=self.now + 20, schedule=core.usage_schedule)
        self.assertEqual(result['at'], self.now)
        self.assertEqual(result['next_check_at'], self.now + 60)
        expired = m.sample(control=self.control, measurer=lambda: m.measure(self.base, self.control, self.now + 60),
                           now=self.now + 60, schedule=core.usage_schedule)
        self.assertEqual(expired['action'], 'SAVE_AND_STOP')
        self.assertEqual(expired['diagnosticCode'], 'usage_receipt_expired')

    def test_low_limit_uses_original_reading_and_latches(self):
        self.publish(self.receipt(primary=90))
        result = m.measurement_cycle(self.base, self.control, None, core.usage_schedule, self.now)
        self.assertEqual(result['action'], 'SAVE_AND_STOP')
        self.assertEqual(result['exitReason'], 'usage_threshold')
        self.assertTrue((self.base / 'save-latch.json').exists())

    def test_missing_measurement_has_bounded_safe_diagnostics(self):
        for _ in range(25):
            result = m.measurement_cycle(self.base, self.control, None, core.usage_schedule, self.now)
        self.assertEqual(result['action'], 'SAVE_AND_STOP')
        self.assertEqual(result['measurementAttempts'], 2)
        self.assertEqual(result['diagnosticCode'], 'usage_receipt_missing')
        diagnostics = json.loads((self.base / 'diagnostics.json').read_text())
        self.assertLessEqual(len(diagnostics['events']), 20)
        self.assertEqual(diagnostics['events'][-1]['code'], 'usage_receipt_missing')
        self.assertNotIn('private-canary', json.dumps(diagnostics))

    def test_malformed_receipt_does_not_leak_exception_text(self):
        (self.base / 'usage-source.json').write_text('private-canary invalid json')
        result = m.measurement_cycle(self.base, self.control, None, core.usage_schedule, self.now)
        self.assertEqual(result['action'], 'SAVE_AND_STOP')
        self.assertNotIn('private-canary', json.dumps(result))
        self.assertNotIn('private-canary', (self.base / 'diagnostics.json').read_text())

    def test_heartbeat_never_verifies_package_and_keeps_identity_bound(self):
        worker = {'pid': 123, 'startIdentity': 'unit', 'workerToken': 'unit',
                  'runId': self.control['runId'], 'epoch': self.control['epoch']}
        fake_runtime = Mock()
        fake_runtime.check_binding.side_effect = AssertionError('heartbeat must not hash package')
        with patch.object(m, '_runtime', fake_runtime), patch.object(m, 'event') as event:
            m.heartbeat_once(self.base, worker)
            event.assert_called_once()
        changed = dict(self.control, epoch='other')
        m.write(self.base / 'control.json', changed)
        with self.assertRaises(m.MonitorError):
            m.heartbeat_once(self.base, worker)

    def test_full_integrity_verification_occurs_once_per_measurement_cycle(self):
        self.publish()
        fake_runtime = Mock()
        with patch.object(m, '_runtime', fake_runtime):
            result = m.measurement_cycle(self.base, self.control, None, core.usage_schedule, self.now)
        self.assertEqual(result['action'], 'CONTINUE')
        fake_runtime.check_binding.assert_called_once_with()

    def test_measure_source_has_no_cli_lookup_or_server_creation(self):
        source = (PACKAGE / 'private/limit-watcher/scripts/monitor.py').read_text(encoding='utf-8-sig')
        tree = ast.parse(source)
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'measure')
        calls = [ast.unparse(n.func) for n in ast.walk(function) if isinstance(n, ast.Call)]
        self.assertNotIn('shutil.which', calls)
        self.assertNotIn('OwnedProcess', calls)
        self.assertNotIn('subprocess.Popen', calls)

    def test_refresh_does_not_reset_receipt_age_and_new_epoch_requires_new_input(self):
        self.publish()
        changed = dict(self.control, epoch='fresh-epoch')
        with self.assertRaises(m.MonitorError):
            m.measure(self.base, changed, self.now)
        with self.assertRaises(m.MonitorError):
            m.publish_usage(self.base, changed, self.receipt(), self.now)
        m.publish_usage(self.base, changed, self.receipt(at=self.now + 1, source_id='post-resume-call'), self.now + 1)
        self.assertEqual(m.measure(self.base, changed, self.now + 1).observed_at, self.now + 1)

    def test_resume_requires_observation_newer_than_stop(self):
        self.publish()
        changed = dict(self.control, epoch='fresh-epoch', usageAfter=self.now + 2)
        with self.assertRaises(m.MonitorError):
            m.publish_usage(self.base, changed, self.receipt(at=self.now + 1, source_id='before-stop'), self.now + 3)
        value = self.receipt(at=self.now + 3, source_id='after-stop')
        m.publish_usage(self.base, changed, value, self.now + 3)
        self.assertEqual(m.measure(self.base, changed, self.now + 3).observed_at, self.now + 3)

    def test_untrusted_diagnostics_are_not_retained(self):
        m.write(self.base / 'diagnostics.json', {'events': [{'at': self.now, 'stage': 'native_usage_read',
            'code': 'private-canary', 'errorType': 'MonitorError', 'attempts': 2}]})
        m.measurement_cycle(self.base, self.control, None, core.usage_schedule, self.now)
        self.assertNotIn('private-canary', (self.base / 'diagnostics.json').read_text())

    def test_policy_metadata_and_core_stop_threshold_agree(self):
        policy = json.loads((PACKAGE / 'private/limit-watcher/role.json').read_text())['policy']
        self.assertEqual(policy['stop_remaining_percent'], 20)
        self.assertEqual(core.usage_schedule({300: {'windowDurationMins': 300, 'usedPercent': 80,
                                                  'resetsAt': 999999}}, self.now)['action'], 'SAVE_AND_STOP')

    def test_native_input_file_is_bounded_and_cannot_publish_malformed_json(self):
        path = self.base / 'input.json'
        for raw in ('private-canary invalid json', '{"source":1,"source":2}', ' ' * (1024 * 1024 + 1)):
            path.write_text(raw)
            with self.assertRaises((ValueError, m.MonitorError)):
                m.ingest_usage_input(self.base, self.control, path, self.now)
            self.assertFalse((self.base / 'usage-source.json').exists())

    def test_ingest_only_publishes_data_and_never_starts_a_worker(self):
        path = self.base / 'input.json'
        path.write_text(json.dumps(self.receipt()))
        runtime = SimpleNamespace(check_binding=lambda: None, args=SimpleNamespace(run_id='unit-run'))
        output = io.StringIO()
        with patch.object(m, '_runtime', None), patch.object(m.time, 'time', return_value=self.now), \
                patch.object(m, 'OwnedProcess', side_effect=AssertionError('ingest must not launch')), \
                contextlib.redirect_stdout(output):
            code = m.main(['ingest', '--state-dir', str(self.base), '--usage-input', str(path)], runtime=runtime)
        self.assertEqual(code, 0, output.getvalue())
        self.assertFalse(m.read(self.base / 'control.json')['active'])
        self.assertFalse((self.base / 'running.json').exists())
        self.assertIs(json.loads(output.getvalue())['activated'], False)

    def test_missing_receipt_refuses_start_before_process_creation(self):
        runtime = SimpleNamespace(check_binding=lambda: None, args=SimpleNamespace(run_id='unit-run'))
        output = io.StringIO()
        with patch.object(m, '_runtime', None), patch.object(m, 'OwnedProcess') as child, \
                contextlib.redirect_stdout(output):
            code = m.main(['start', '--state-dir', str(self.base)], runtime=runtime)
        child.assert_not_called()
        self.assertEqual(code, 2)
        self.assertFalse(m.read(self.base / 'control.json')['active'])
        self.assertEqual(json.loads(output.getvalue())['diagnosticCode'], 'usage_receipt_missing')

    def test_candidate_bridge_preserves_usage_input_on_activation_and_resume(self):
        spec = importlib.util.spec_from_file_location('native_bridge_only', PACKAGE / 'scripts/candidate.py')
        candidate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(candidate)
        runtime = object()
        for command, payload in [('activate', {}), ('resume', {'native_ui_resume': True,
                'app_goal_observation': {'updated_at': self.now, 'source_id': 'native-goal-receipt'}})]:
            with self.subTest(command=command), patch.object(candidate, 'watcher_command',
                    side_effect=[{}, {'action': 'CONTINUE', 'health': {'healthy': True}}]) as command_call:
                candidate.watcher_ready(runtime, command, payload, 'current-usage.json')
                arguments = command_call.call_args_list[0].args
                self.assertEqual(arguments[-2:], ('--usage-input', 'current-usage.json'))
                if command == 'resume':
                    self.assertIn('--new-epoch', arguments)
                    self.assertIn('native-goal-receipt', arguments)


if __name__ == '__main__':
    unittest.main()
