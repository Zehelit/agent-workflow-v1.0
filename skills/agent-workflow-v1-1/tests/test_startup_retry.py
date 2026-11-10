"""Disposable startup transactions: no account access or native goal mutations."""
import copy
import hashlib
import json
import time
import unittest
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import test_candidate_coexistence as fixtures


class StartupRetryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.CandidateCoexistenceTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.created = time.time() - 100
        self.sequence = 0
        self.attempt_pin = None
        self.addCleanup(self.stop_worker)

    def stop_worker(self):
        base = self.f.state() / 'watcher'
        if (base / 'control.json').exists():
            self.f.cli('watcher', 'stop')
            monitor = fixtures.load('startup_cleanup', self.f.package / 'private/limit-watcher/scripts/monitor.py')
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and monitor.marker_state(base)['identityState'] in {'matched', 'unknown'}:
                time.sleep(.05)
            self.assertFalse(monitor.marker_state(base)['liveIdentity'])

    def payload(self, mode='bind-active-goal'):
        now = time.time()
        p = dict(objective='deliver', models={}, slot_limit=1, inventory=[], observed_at=now,
                 interpretation='Synthetic startup authorization, same scope across retry.',
                 app_goal_observation=dict(goal_id='goal', thread_id='thread', objective='deliver',
                     goal_created_at=self.created, status='active', source_kind='get_goal',
                     source_id='goal-' + str(now), observed_at=now))
        if mode == 'bind-active-goal':
            p.update(native_binding=dict(binding_id='binding', goal_id='goal', thread_id='thread',
                run_id='candidate-run', scope=dict(kind='objective', id='scope', objective='deliver'),
                objective='deliver', goal_created_at=self.created, observed_at=now),
                procedural_check='Actual disposable fixture has no children or later restrictions.')
        else:
            event = dict(event_id='authorization', exact_quote='Start this isolated fixture.',
                kind='explicit_task_authorization', source_kind='user_message', source_id='user-fixture',
                source_created_at=self.created, original_turn_id='original', observed_turn_id='original',
                observed_at=now, goal_id='goal', thread_id='thread', run_id='candidate-run',
                scope=dict(kind='objective', id='scope', objective='deliver'))
            p.update(authorization=event, source_message={k:event[k] for k in
                ('source_kind','source_id','exact_quote','source_created_at','original_turn_id')})
        return p

    def receipt(self, expired=False, quota=False):
        self.sequence += 1
        path = self.f.project / ('usage-' + str(self.sequence) + '.json')
        value = dict(source='mcp__codex_app__get_usage_limits', sourceId='fixture-' + str(self.sequence),
            observedAt=time.time() - (70 if expired else 0), result=dict(rateLimits=dict(
                primary=dict(windowDurationMins=300, usedPercent=90 if quota else 79 if expired else 10, resetsAt=9999999999),
                secondary=dict(windowDurationMins=10080, usedPercent=10, resetsAt=9999999999))))
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def fail_initial(self, mode='bind-active-goal', quota=False):
        result = self.f.cli('workflow', mode, '--usage-input', str(self.receipt(expired=not quota, quota=quota)),
                            payload=self.payload(mode), good=False)
        self.attempt_pin = json.loads(result.stderr).get('startup_retry', {}).get('attempt_sha256')
        self.assertFalse((self.f.state()/'state.json').exists())
        latch = self.f.state()/'watcher/save-latch.json'
        self.assertTrue(latch.exists())
        return hashlib.sha256(latch.read_bytes()).hexdigest()

    def retry(self, mode='bind-active-goal', payload=None, usage=None, good=True):
        args = ['workflow', mode, '--usage-input', str(usage or self.receipt())]
        if self.attempt_pin:
            args += ['--startup-attempt-sha256', self.attempt_pin]
        result = self.f.cli(*args, payload=payload or self.payload(mode), good=good)
        if result.returncode:
            self.attempt_pin = json.loads(result.stderr).get('startup_retry', {}).get('attempt_sha256', self.attempt_pin)
        return result

    def test_initial_expiry_then_fresh_same_goal_retries_without_new_quote(self):
        digest = self.fail_initial()
        result = json.loads(self.retry().stdout)
        self.assertEqual(result['status'], 'active')
        self.assertTrue(result['watcher']['healthy'])
        self.assertEqual(hashlib.sha256((self.f.state()/'watcher/save-latch.json').read_bytes()).hexdigest(), digest)
        state = json.loads((self.f.state()/'state.json').read_bytes())
        self.assertEqual(state['authorization_events'], [])
        attempt = json.loads((self.f.state()/'activation-attempt.json').read_bytes())
        self.assertEqual(attempt['phase'], 'committed')

    def test_initial_human_activation_can_retry_without_reauthorizing_quote(self):
        self.fail_initial('activate')
        result = json.loads(self.retry('activate').stdout)
        self.assertTrue(result['watcher']['healthy'])
        state = json.loads((self.f.state()/'state.json').read_bytes())
        self.assertEqual(len(state['authorization_events']), 1)

    def test_quota_save_is_never_recovered_as_startup_expiry(self):
        digest = self.fail_initial(quota=True)
        self.retry(good=False)
        self.assertEqual(hashlib.sha256((self.f.state()/'watcher/save-latch.json').read_bytes()).hexdigest(), digest)
        self.assertFalse((self.f.state()/'state.json').exists())

    def test_changed_goal_or_scope_cannot_reuse_failed_attempt(self):
        self.fail_initial()
        for key in ('goal_id', 'thread_id', 'scope'):
            p = self.payload()
            if key == 'scope':
                p['native_binding']['scope']['id'] = 'other'
            else:
                p['native_binding'][key] = 'other'
                p['app_goal_observation'][key] = 'other'
            with self.subTest(key=key):
                self.retry(payload=p, good=False)
        self.assertFalse((self.f.state()/'state.json').exists())

    def test_prepared_attempt_and_changed_journal_fail_closed(self):
        self.fail_initial()
        path = self.f.state()/'activation-attempt.json'
        self.assertTrue(path.exists(), 'failure must retain a verifiable startup transaction')
        original = path.read_bytes()
        for change in ({'phase':'prepared'}, {'identity':{}}, {'failure':{}}, {'pre_state_sha256':'0'*64}):
            data=json.loads(original);data.update(change);path.write_text(json.dumps(data),encoding='utf-8')
            with self.subTest(change=change):
                self.retry(good=False)
        path.write_bytes(original)

    def test_invalid_retry_receipt_preserves_original_basis_for_next_retry(self):
        digest = self.fail_initial()
        invalid = self.f.project/'invalid.json';invalid.write_text('{bad', encoding='utf-8')
        self.retry(usage=invalid, good=False)
        self.assertEqual(hashlib.sha256((self.f.state()/'watcher/save-latch.json').read_bytes()).hexdigest(),digest)
        result=json.loads(self.retry().stdout)
        self.assertTrue(result['watcher']['healthy'])

    def test_lifecycle_lock_is_not_removed_or_bypassed(self):
        self.f.cli('workflow','validate')
        lock = self.f.state()/'lifecycle.lock';lock.write_text('unverifiable owner',encoding='utf-8')
        self.retry(good=False)
        self.assertEqual(lock.read_text(encoding='utf-8'),'unverifiable owner')
        self.assertFalse((self.f.state()/'state.json').exists())

    def test_manual_watcher_stop_cannot_interleave_with_other_process_lifecycle_owner(self):
        self.f.cli('workflow','validate')
        lock=self.f.state()/'lifecycle.lock';ready=self.f.project/'lock-ready'
        code="import os,sys,time;from pathlib import Path;p=Path(sys.argv[1]);fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.write(fd,b'owned concurrent lifecycle');Path(sys.argv[2]).write_text('ready');time.sleep(30)"
        proc=subprocess.Popen([sys.executable,'-c',code,str(lock),str(ready)])
        try:
            deadline=time.monotonic()+4
            while not ready.exists():
                self.assertIsNone(proc.poll())
                if time.monotonic()>deadline:self.fail('owner did not acquire lock')
                time.sleep(.01)
            self.f.cli('watcher','stop',good=False)
            self.assertEqual(lock.read_text(),'owned concurrent lifecycle')
            self.assertIsNone(proc.poll())
        finally:
            proc.terminate();proc.wait(timeout=4)

    def test_activation_never_returns_cached_health_after_worker_loss_at_commit(self):
        candidate=fixtures.load('commit_guard',self.f.entry)
        args=SimpleNamespace(project=str(self.f.project),run_id='candidate-run',manifest=str(self.f.manifest),
                             manifest_sha256=self.f.pin,version='1.1',mode='workflow')
        runtime=candidate.Runtime(args);w,engine=runtime.workflow()
        with patch.object(candidate,'watcher_ready',return_value={'health':{'healthy':True}}), \
             patch.object(candidate,'watcher_command',return_value={'health':{'healthy':False}}), \
             patch.object(candidate,'watcher_stop_verified',return_value={}):
            with self.assertRaises(candidate.CandidateError):
                candidate.execute_workflow_command(runtime,w,engine,'bind-active-goal',self.payload())

    def test_new_quota_save_during_retry_is_preserved_and_blocks_next_technical_retry(self):
        old=self.fail_initial()
        self.retry(usage=self.receipt(quota=True),good=False)
        path=self.f.state()/'watcher/save-latch.json';new=hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertNotEqual(old,new)
        self.assertEqual(json.loads(path.read_bytes())['result']['exitReason'],'usage_threshold')
        self.retry(good=False)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),new)


if __name__ == '__main__':
    unittest.main()
