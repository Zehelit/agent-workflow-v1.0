"""Lifecycle runner: synthetic receipts, real candidate and owned subprocesses."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('lifecycle_check', ROOT/'tooling/run-lifecycle-check.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class LifecycleCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root/'project'; self.project.mkdir()
        self.build = self.root/'build'
        subprocess.run([sys.executable, '-B', str(ROOT/'tooling/build-candidate.py'), '--output', str(self.build)],
                       check=True, capture_output=True)
        self.receipt = json.loads((self.build/'build-receipt.json').read_text())
        self.package = self.build/'agent-workflow-v1-1'
        self.session = self.root/'session.json'

    def prepare(self, pin=None):
        return m.prepare(self.session, self.package/'scripts/candidate.py', self.package/'manifest.json',
                         pin or self.receipt['manifest_sha256'], '1.1', self.project, 'test-run')

    def observation(self, used=10):
        now = time.time()
        obs = dict(goal_id='g', thread_id='t', objective='test', goal_created_at=1,
                   status='active', source_kind='get_goal', source_id='synthetic-'+str(now), observed_at=now)
        binding = dict(binding_id='b', goal_id='g', thread_id='t', run_id='test-run',
                       scope=dict(kind='objective', id='s', objective='test'), objective='test',
                       goal_created_at=1, observed_at=now)
        activation = dict(native_binding=binding, app_goal_observation=obs, objective='test',
                          interpretation='Isolated fixture', procedural_check='Synthetic test only',
                          models={'gpt-6.1-sol':['high']}, slot_limit=4, inventory=[], observed_at=now)
        windows = dict(primary=dict(usedPercent=used, windowDurationMins=300, resetsAt=now+1000),
                       secondary=dict(usedPercent=10, windowDurationMins=10080, resetsAt=now+10000))
        return dict(activation=activation, app_goal_observation=obs, inventory=[], checkpoint_files=['work.txt'],
                    writers_quiescent=True, next_action='test', usage=dict(source='mcp__codex_app__get_usage_limits',
                    sourceId=obs['source_id'], observedAt=now, result={'rateLimits':windows}))

    def stop_owned(self):
        if self.session.exists():
            session=m.load_session(self.session)
            m.call(session, ['watcher','stop'])
            deadline=time.monotonic()+8
            while m.call(session,['watcher','status'])['health']['identityState']!='absent':
                if time.monotonic()>deadline: self.fail('owned worker exit unverified')
                time.sleep(.1)

    def test_prepare_verifies_pin_without_starting_worker(self):
        with patch.object(m.subprocess, 'run', side_effect=AssertionError('prepare must not spawn')):
            self.prepare()
        self.assertFalse((self.project/'.agent-workflow-v1-1').exists())
        self.assertEqual(m.load_session(self.session)['version'], '1.1')

    def test_tampered_entry_and_wrong_pin_refused(self):
        with self.assertRaises(m.CheckError): self.prepare('0'*64)
        entry = self.package/'scripts/candidate.py'
        entry.write_text(entry.read_text()+'\n# edit\n')
        with self.assertRaises(m.CheckError): self.prepare()

    def test_each_independent_health_condition_is_required(self):
        status = dict(at=100, action='CONTINUE', policy={'epoch':'e'}, health=dict(
            healthy=True, identityState='matched', liveIdentity=True, heartbeatFresh=True,
            leaseValid=True, sampleFresh=True, workerEpochMatches=True))
        self.assertTrue(m.healthy(status, 100))
        self.assertFalse(m.healthy(status, 101))
        for key in ('healthy','liveIdentity','heartbeatFresh','leaseValid','sampleFresh','workerEpochMatches'):
            changed = {**status, 'health':{**status['health'], key:False}}
            self.assertFalse(m.healthy(changed, 100), key)

    def test_refresh_margin_does_not_change_one_second_interval(self):
        self.assertEqual(m.refresh_by(100,60),145)
        self.assertEqual(m.refresh_by(100,1),100.75)

    def test_duplicate_and_nonfinite_input_rejected(self):
        for content in ('{"a":1,"a":2}','{"a":NaN}','{"a":1e999}'):
            path=self.root/'bad.json';path.write_text(content)
            with self.assertRaises((m.CheckError,ValueError)): m.read(path)

    def test_new_quota_reading_saves_without_technical_bypass(self):
        self.prepare();self.addCleanup(self.stop_owned)
        (self.project/'work.txt').write_text('durable')
        m.run(self.session,self.observation())
        stopped=m.observe(self.session,self.observation(90))
        self.assertEqual(stopped['result'],'NEED_POST_HALT_OBSERVATION')
        while time.time()<=m.load_session(self.session)['halted_at']: time.sleep(.005)
        with self.assertRaises(m.CheckError): m.resume(self.session,self.observation())

    def test_weekly_policy_setup_requests_a_new_host_observation_before_core_activation(self):
        self.prepare();self.addCleanup(self.stop_owned)
        data=self.observation()
        data['weekly_exception']=dict(exact_quote='Synthetic bounded policy.',valid_until=time.time()+1000)
        first=m.run(self.session,data)
        self.assertEqual(first['result'],'NEED_POLICY_OBSERVATION')
        self.assertEqual(m.load_session(self.session)['phase'],'policy_ready')
        time.sleep(.01)
        self.assertEqual(m.run(self.session,self.observation())['result'],'ACTIVE')

    def test_late_event_never_calls_stop(self):
        self.prepare()
        event = dict(event_id='x', goal_id='g', thread_id='t', source_id='synthetic',
                     event_at=time.time()-45, observed_at=time.time())
        with patch.object(m, 'call', side_effect=AssertionError('late event cannot stop')):
            self.assertEqual(m.timed_test(self.session,event,{},10,1)['result'],'INVALID_TIMING')

    def test_timed_two_rounds_keep_actual_stage_times_and_do_not_claim_native_pause(self):
        self.prepare()
        session=m.load_session(self.session)
        session.update(phase='active',goal_id='g',thread_id='t');m.write(self.session,session)
        class Clock:
            now=1000.0
            def time(self): return self.now
            def monotonic(self): return self.now
            def sleep(self,delay): self.now+=delay
        clock=Clock()
        status=dict(at=1000,action='CONTINUE',health=dict(healthy=True,identityState='matched',
                    liveIdentity=True,heartbeatFresh=True,leaseValid=True,sampleFresh=True,workerEpochMatches=True))
        def candidate(session,args,payload=None):
            if args==['watcher','status']: return status
            clock.now+=.1
            return {'status':'halted'}
        with patch.object(m,'time',clock),patch.object(m,'call',side_effect=candidate):
            for number in range(2):
                session=m.load_session(self.session);session['phase']='active';m.write(self.session,session)
                start=clock.now
                event=dict(event_id=str(number),goal_id='g',thread_id='t',event_at=start,observed_at=start,source_id='synthetic')
                result=m.timed_test(self.session,event,self.observation(),10,1)
                self.assertEqual(result['result'],'SAVE_REQUEST_ON_TIME')
                self.assertAlmostEqual(result['save_requested_at']-start,10,places=5)
                self.assertLess(result['save_returned_at'],result['snapshot_verified_at'])
                self.assertFalse(result['native_pause_verified'])
                self.assertIsNone(result['native_paused_at'])
                # A repeated same event is not reusable after returning to active.
                session=m.load_session(self.session);session['phase']='active';m.write(self.session,session)
                with self.assertRaises(m.CheckError): m.timed_test(self.session,event,self.observation(),10,1)

    def test_halt_requires_explicit_host_quiescence_before_stop(self):
        self.prepare(); session=m.load_session(self.session)
        data=self.observation();data['writers_quiescent']=False
        with patch.object(m,'call',side_effect=AssertionError('must refuse before stop')):
            with self.assertRaises(m.CheckError): m.halt(self.session,session,data)

    def test_normal_real_worker_refresh_and_natural_close(self):
        self.prepare(); self.addCleanup(self.stop_owned)
        (self.project/'work.txt').write_text('durable')
        started = m.run(self.session,self.observation())
        self.assertEqual(started['result'],'ACTIVE')
        refreshed = m.observe(self.session,self.observation())
        self.assertEqual(refreshed['result'],'HEALTHY')
        closed = m.close(self.session,self.observation())
        self.assertEqual(closed['result'],'COMPLETE')
        self.assertFalse(closed['save_latched'])
        self.assertEqual(closed['watcher']['health']['identityState'],'absent')

    def test_expired_start_retains_external_pin_then_same_run_retry(self):
        self.prepare(); self.addCleanup(self.stop_owned)
        old = self.observation(79); old['usage']['observedAt']-=70
        with self.assertRaises(m.CheckError): m.run(self.session,old)
        state=m.load_session(self.session)
        self.assertEqual(len(state['startup_attempt_sha256']),64)
        self.assertEqual(m.run(self.session,self.observation())['result'],'ACTIVE')

    def test_expired_worker_halts_then_post_halt_observation_resumes(self):
        self.prepare(); self.addCleanup(self.stop_owned)
        (self.project/'work.txt').write_text('durable')
        m.run(self.session,self.observation())
        session=m.load_session(self.session)
        # Expire an actual receipt in this disposable synthetic test only.
        source=self.project/'.agent-workflow-v1-1/runs/test-run/watcher/usage-source.json'
        saved=json.loads(source.read_text()); saved['observedAt']-=400
        source.write_text(json.dumps(saved))
        deadline=time.monotonic()+6
        while m.call(session,['watcher','status']).get('action')!='SAVE_AND_STOP':
            if time.monotonic()>deadline: self.fail('worker did not latch expiry')
            time.sleep(.1)
        self.assertEqual(m.observe(self.session,self.observation())['result'],'NEED_POST_HALT_OBSERVATION')
        while time.time()<=m.load_session(self.session)['halted_at']: time.sleep(.005)
        fresh=self.observation()
        resumed=m.resume(self.session,fresh)
        self.assertEqual(resumed['result'],'ACTIVE')
        self.assertEqual(m.call(session,['workflow','status'])['status'],'active')


if __name__ == '__main__': unittest.main()
