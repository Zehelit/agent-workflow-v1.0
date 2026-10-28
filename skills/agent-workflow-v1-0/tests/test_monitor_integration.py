"""N04 accepted current-core/A11 integration using owned synthetic state/native receipts."""
import contextlib
import copy
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
WORKFLOW_PIN = hashlib.sha256((ROOT / 'skills/agent-workflow-v1-0/scripts/workflow.py').read_bytes()).hexdigest()


class MonitorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix='a11-integration-source-')
        root = Path(cls.fixture.name)
        workflow = ROOT / 'skills/agent-workflow-v1-0/scripts/workflow.py'
        if hashlib.sha256(workflow.read_bytes()).hexdigest() != WORKFLOW_PIN:
            cls.fixture.cleanup()
            raise RuntimeError('accepted current-core dependency changed; revalidation required')
        for relative in ('skills/agent-workflow-v1-0/private/limit-watcher/scripts/monitor.py', 'skills/agent-workflow-v1-0/scripts/workflow.py'):
            target = root / relative
            target.parent.mkdir(parents=True)
            target.write_bytes((ROOT / relative).read_bytes())
        source = root / 'skills/agent-workflow-v1-0/private/limit-watcher/scripts/monitor.py'
        spec = importlib.util.spec_from_file_location('monitor_integration', source)
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)
        cls.schedule = staticmethod(cls.m.workflow_schedule(root / 'skills/agent-workflow-v1-0/scripts/workflow.py', WORKFLOW_PIN))
        print('A04_A11_INTEGRATION_PINS=' + json.dumps({
            'monitor': hashlib.sha256(source.read_bytes()).hexdigest(),
            'workflow': WORKFLOW_PIN, 'optimized': sys.flags.optimize}), flush=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='a11-integration-state-')
        self.base = Path(self.temp.name)
        self.now = time.time()
        self.control = self.m.default_control('integration-run', self.now)
        self.m.write(self.base / 'control.json', self.control)

    def tearDown(self):
        self.temp.cleanup()

    def windows(self, primary=20, weekly=20):
        return {300: {'windowDurationMins': 300, 'usedPercent': primary, 'resetsAt': 9999999999},
                10080: {'windowDurationMins': 10080, 'usedPercent': weekly, 'resetsAt': 9999999999}}

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = self.m.main([*args, '--state-dir', str(self.base)])
        return code, json.loads(output.getvalue())

    def test_frozen_dependency_usage_api(self):
        for used, action, interval in ((20, 'CONTINUE', 300), (75, 'CHECKPOINT_SOON', 60),
                                      (79, 'CHECKPOINT_SOON', 60), (80, 'SAVE_AND_STOP', 30), (90, 'SAVE_AND_STOP', 30),
                                      (100, 'SAVE_AND_STOP', 30)):
            with self.subTest(used=used):
                result = self.schedule(self.windows(used), self.now)
                self.assertEqual(result['action'], action)
                self.assertEqual(result['check_interval_seconds'], interval)
                self.assertEqual(result['next_check_at'], self.now + interval)
        self.assertEqual(self.schedule(self.windows(20, 95), self.now)['action'], 'SAVE_AND_STOP')
        old = self.schedule(self.windows(40), self.now - 60)
        current = self.schedule(self.windows(60), self.now, old)
        self.assertEqual(current['action'], 'SAVE_AND_STOP')
        self.assertEqual(current['estimated_seconds_to_threshold'], 60)
        old['windows']['300']['resetsAt'] -= 1
        self.assertEqual(self.schedule(self.windows(60), self.now, old)['action'], 'CONTINUE')
        result = self.schedule({300: self.windows()[300]}, self.now)
        self.assertEqual(result['action'], 'CONTINUE')
        self.assertEqual(set(result['windows']), {'300'})

    def receipt_sample(self, text):
        attempts = []
        def read_receipt():
            attempts.append(True)
            value = self.m.strict_json(text)
            self.m.publish_usage(self.base, self.control, value, self.now)
            return self.m.measure(self.base, self.control, self.now)
        with patch.object(self.m.subprocess, 'Popen', side_effect=AssertionError('no Codex process')):
            result = self.m.sample(None, self.control, read_receipt, self.now, self.schedule)
        return result, len(attempts)

    def native_receipt(self):
        return {'source': self.m.USAGE_SOURCE, 'sourceId': 'native-fixture', 'observedAt': self.now,
                'result': {'rateLimits': {'primary': self.windows()[300], 'secondary': self.windows()[10080]}}}

    def test_duplicate_native_input_retries_and_latches_save(self):
        wire = json.dumps(self.native_receipt()).replace('"usedPercent": 20', '"usedPercent": 95, "usedPercent": 20', 1)
        result, count = self.receipt_sample(wire)
        self.assertEqual((count, result['measurementAttempts'], result['action']), (2, 2, 'SAVE_AND_STOP'))
        self.assertEqual(result['exitReason'], 'measurement_failure')
        self.m.latch(self.base, result, self.control)
        with patch.object(self.m, 'measure', side_effect=AssertionError('must not resample')) as call:
            for command in ('once', 'start', 'status', 'renew'):
                code, actual = self.cli(command)
                self.assertEqual((code, actual['action']), (0, 'SAVE_AND_STOP'))
            call.assert_not_called()

    def test_strict_native_json_and_nested_boundaries(self):
        frames = ['{"x":1,"x":2}', '{"ignored":{"x":NaN}}', '{"ignored":{"x":Infinity}}',
                  '{"ignored":{"x":-Infinity}}', '{"ignored":{"x":1e9999}}', '[]', 'null', '42']
        for frame in frames:
            with self.subTest(frame=frame):
                result, count = self.receipt_sample(frame)
                self.assertEqual((result['action'], result['measurementAttempts'], count), ('SAVE_AND_STOP', 2, 2))

    def test_native_tool_result_retains_only_approved_fields(self):
        value = self.native_receipt()
        value['result']['accountId'] = 'private-secret'
        value['result']['rateLimits']['primary']['accountId'] = 'private-secret'
        result, count = self.receipt_sample(json.dumps(value))
        self.assertEqual((result['action'], count), ('CONTINUE', 1))
        self.assertNotIn('private-secret', json.dumps(result))
        self.assertNotIn('private-secret', (self.base / 'usage-source.json').read_text())

    def test_huge_integer_native_inputs_never_become_measurements(self):
        for field in ('usedPercent', 'resetsAt'):
            value = self.native_receipt()
            value['result']['rateLimits']['primary'][field] = 10 ** 400
            result, count = self.receipt_sample(json.dumps(value))
            self.assertEqual((result['action'], count), ('SAVE_AND_STOP', 2))
            self.assertNotIn('windows', result)

    def test_malformed_health_holds_with_and_without_marker(self):
        actual = self.m.identity(os.getpid())
        self.assertEqual(actual['state'], 'live')
        marker = {key: actual[key] for key in ('pid', 'startIdentity')}
        marker.update(workerToken='synthetic-token', runId=self.control['runId'], epoch=self.control['epoch'])
        health = dict(marker, schemaVersion=1, state='running', heartbeatAt=self.now,
                      leaseExpiresAt=self.control['expiresAt'], exitReason=None, errorType=None, history=[])
        malformed = [[], None, False, 17, 'scalar']
        for key, bad in (('pid', True), ('pid', {'value': actual['pid']}), ('startIdentity', {}),
                         ('workerToken', []), ('runId', False), ('epoch', {'value': 'epoch'}),
                         ('heartbeatAt', {'value': self.now}), ('heartbeatAt', None),
                         ('heartbeatAt', True), ('leaseExpiresAt', []), ('leaseExpiresAt', float('inf')),
                         ('schemaVersion', True), ('state', []), ('errorType', {})):
            malformed.append(dict(health, **{key: bad}))
        self.m.write(self.base / 'latest.json', self.m.sample(None, self.control,
                     self.windows, self.now, self.schedule))
        for with_marker in (False, True):
            if with_marker:
                self.m.write(self.base / 'running.json', marker)
            for bad in malformed:
                with self.subTest(marker=with_marker, health=repr(bad)):
                    (self.base / 'health.json').write_text(json.dumps(bad), encoding='utf-8')
                    self.assertEqual(self.m.marker_state(self.base)['identityState'], 'unknown')
                    result = self.m.status(self.base)
                    self.assertEqual(result['action'], 'HOLD')
                    self.assertEqual(result['health']['identityState'], 'unknown')
                    self.assertFalse(result['health']['healthy'])
                    self.assertFalse(result['health']['liveIdentity'])
                    self.assertIn('errorType', result['health'])
                    before = (self.base / 'control.json').read_bytes()
                    with patch.object(self.m, 'measure', side_effect=RuntimeError('must not measure')) as call:
                        for command in ('start', 'once', 'renew'):
                            self.assertEqual(self.cli(command)[0], 2)
                        call.assert_not_called()
                    self.assertEqual((self.base / 'control.json').read_bytes(), before)
        print('A11_MALFORMED_HEALTH=' + json.dumps({'cases': len(malformed) * 2,
              'unknownHold': True, 'noMeasurement': True}), flush=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
