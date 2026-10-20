"""Real source/staged subprocess entry and owned-worker coexistence contracts."""
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
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
PYTHON = [sys.executable, '-X', 'utf8', '-B']

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


def stable_state_hashes(root):
    return {name: value for name, value in hashes(root).items() if not name.startswith('watcher/')}

class CandidateCoexistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='candidate-entry-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.old = self.project / '.agent-workflow'
        self.old.mkdir()
        (self.old / 'state.json').write_text('old-state-untouched')
        (self.old / 'coordinator.lock').write_text('old-lock-untouched')
        self.home = self.base / 'decoy-home'
        for name in ('skills/agent-workflow/scripts/workflow.py', 'skills/limit-watcher/role.json', 'skills/limit-watcher/scripts/monitor.py'):
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('decoy must never load or change')
        self.preserved = (hashes(self.old), hashes(self.home))
        build = subprocess.run([*PYTHON, str(ROOT / 'tooling/build-candidate.py'), '--output', str(self.base / 'build')], capture_output=True, text=True)
        self.assertEqual(build.returncode, 0, build.stderr + build.stdout)
        self.package = self.base / 'build/agent-workflow-v1-0'
        self.manifest = self.package / 'manifest.json'
        self.pin = json.loads(build.stdout)['manifest_sha256']
        self.source = ROOT / 'skills/agent-workflow-v1-0/scripts/candidate.py'
        self.entry = self.package / 'scripts/candidate.py'

    def assert_preserved(self):
        self.assertEqual((hashes(self.old), hashes(self.home)), self.preserved)

    def cli(self, *command, entry=None, run='candidate-run', manifest=None, pin=None, version='1.0', payload=None, raw=None, good=True, env_overrides=None):
        env = dict(os.environ, CODEX_HOME=str(self.home), PYTHONDONTWRITEBYTECODE='1')
        env.update(env_overrides or {})
        argv = [*PYTHON, str(entry or self.entry), '--manifest', str(manifest or self.manifest), '--manifest-sha256', pin or self.pin,
                '--version', version, '--project', str(self.project), '--run-id', run, *command]
        result = subprocess.run(argv, input=raw if raw is not None else json.dumps(payload) if payload is not None else '', capture_output=True, text=True, env=env, timeout=15)
        if good:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assert_preserved()
        return result

    def state(self, run='candidate-run'):
        return self.project / '.agent-workflow-v1-0/runs' / run

    def test_transient_health_read_does_not_block_binding_or_recovery(self):
        candidate = load('candidate_health_race', self.source)
        runtime = object.__new__(candidate.Runtime)
        runtime.base = self.state('health-race')
        runtime.base.mkdir(parents=True)
        runtime.binding = {'run_id': 'health-race'}
        runtime.args = SimpleNamespace(run_id='health-race')
        runtime.verify_files = lambda: None
        (runtime.base / 'run-binding.json').write_bytes(candidate.encoded(runtime.binding))
        watcher = runtime.base / 'watcher'
        watcher.mkdir()
        (watcher / 'health.json').write_text('{}', encoding='utf-8')
        original_read = candidate.read

        def read_with_health_denied(path):
            if path.name == 'health.json':
                raise PermissionError('transient Windows read denial')
            return original_read(path)

        with patch.object(candidate, 'read', side_effect=read_with_health_denied):
            runtime.check_binding()

    def test_native_resume_passes_transition_to_private_watcher_without_quote(self):
        candidate = load('candidate_native_bridge', self.source)
        runtime = object()
        payload = {'native_ui_resume': True,
                   'app_goal_observation': {'updated_at': 1234.5, 'source_id': 'native-receipt'}}
        with patch.object(candidate, 'watcher_command',
                          side_effect=[{}, {'action': 'CONTINUE', 'health': {'healthy': True}}]) as watcher:
            result = candidate.watcher_ready(runtime, 'resume', payload)
        self.assertTrue(result['health']['healthy'])
        self.assertEqual(watcher.call_args_list[0].args,
                         (runtime, 'start', '--new-epoch', '--native-transition-at', '1234.5',
                          '--native-receipt', 'native-receipt'))

    def activate(self, run='candidate-run', entry=None):
        # A current model-free fixture sample keeps this test independent of the
        # machine's Codex login while exercising the real pinned worker lifecycle.
        self.cli('workflow','status',run=run,entry=entry)
        monitor = load('candidate_fixture_monitor',self.package/'private/limit-watcher/scripts/monitor.py')
        workflow = load('candidate_fixture_workflow',self.package/'scripts/workflow.py')
        base = self.state(run)/'watcher'
        base.mkdir(exist_ok=True)
        control = monitor.default_control(run)
        monitor.write(base/'control.json',control)
        windows = {key:dict(windowDurationMins=key,usedPercent=10,resetsAt=9999999999) for key in (300,10080)}
        monitor.write(base/'latest.json',monitor.sample(control=control,measurer=lambda:windows,
                                                        schedule=workflow.usage_schedule))
        now = time.time()
        event = dict(event_id='initial', exact_quote='Do this isolated test.', kind='explicit_task_authorization', source_kind='user_message', source_id='test-human',
            goal_id='test-goal', thread_id='test-thread', run_id=run, scope=dict(kind='objective', id='test-scope', objective='test'),
            observed_at=now, source_created_at=now, original_turn_id='test-turn', observed_turn_id='test-turn')
        payload = dict(objective='test', authorization=event, interpretation='test fixture authorization', source_message={key:event[key] for key in ('source_kind','source_id','exact_quote','source_created_at','original_turn_id')},
            app_goal_observation=dict(goal_id='test-goal',thread_id='test-thread',status='active',source_kind='get_goal',source_id='test-receipt',observed_at=now),
            models={'gpt-6.1-sol':['high'],'gpt-6-luna':['low','medium']},slot_limit=4,inventory=[],observed_at=now)
        self.cli('workflow','activate',run=run,entry=entry,payload=payload)
        def cleanup():
            self.cli('watcher','stop',run=run,entry=entry)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and monitor.marker_state(base)['liveIdentity']:
                time.sleep(.05)
            self.assertFalse(monitor.marker_state(base)['liveIdentity'])
        self.addCleanup(cleanup)
        self.cli('workflow','usage',run=run,entry=entry,payload=dict(observed_at=time.time(),rateLimits=dict(primary=dict(usedPercent=10,windowDurationMins=300),secondary=dict(usedPercent=10,windowDurationMins=10080))))

    def test_source_and_installed_use_separate_full_run_namespaces(self):
        installed = self.base / 'skills/agent-workflow-v1-0'
        self.cli('install','--destination',str(installed),entry=self.source)
        installed_entry = installed / 'scripts/candidate.py'
        installed_manifest = installed / 'manifest.json'
        self.activate('source-run',self.source)
        self.cli('workflow','status',run='installed-run',entry=installed_entry,manifest=installed_manifest)
        self.cli('workflow','snapshot',run='source-run',entry=self.source,payload=dict(files=[],writers_quiescent=True,next_action='inspect'))
        request = self.project / 'batch.json'
        request.write_text(json.dumps(dict(batch_id='status_batch',steps=[dict(key='status',command='status',payload={})])))
        self.cli('coordination','--input',str(request),run='source-run',entry=self.source)
        self.assertTrue((self.state('source-run') / 'checkpoints').is_dir())
        self.assertTrue((self.state('source-run') / 'coordination/status_batch.json').is_file())
        self.assertTrue((self.state('installed-run') / 'state.json').is_file())
        before = stable_state_hashes(self.state('source-run'))
        self.cli('workflow','status',run='source-run',entry=installed_entry,manifest=installed_manifest,good=False)
        self.assertEqual(stable_state_hashes(self.state('source-run')),before)
        self.assertFalse(list(self.project.rglob('*.tmp-*')))
        self.assertFalse((self.state('source-run') / 'coordinator.lock').exists())

    def test_activation_starts_worker_and_completion_stops_it(self):
        self.activate()
        monitor=load('candidate_lifecycle_monitor',self.package/'private/limit-watcher/scripts/monitor.py')
        base=self.state()/'watcher'
        started=monitor.status(base)
        self.assertTrue(started['health']['healthy'])
        initial_expiry=monitor.load_control(base)['expiresAt']
        status=json.loads(self.cli('workflow','status').stdout)
        self.assertTrue(status['watcher']['healthy'])
        self.assertGreater(monitor.load_control(base)['expiresAt'],initial_expiry)
        self.cli('workflow','snapshot',payload=dict(files=[],writers_quiescent=True,next_action='complete'))
        self.cli('workflow','complete')
        self.assertEqual(json.loads((self.state()/'state.json').read_bytes())['status'],'complete')
        self.assertFalse(monitor.marker_state(base)['liveIdentity'])
        self.assertEqual(monitor.read_health(base)['exitReason'],'explicit_stop')

    def test_missing_usage_refuses_activation_without_active_state(self):
        run='missing-usage'
        now=time.time()
        event=dict(event_id='initial',exact_quote='Authorize this test.',kind='explicit_task_authorization',
                   source_kind='user_message',source_id='test-human',goal_id='test-goal',thread_id='test-thread',
                   run_id=run,scope=dict(kind='objective',id='test-scope',objective='test'),observed_at=now,
                   source_created_at=now,original_turn_id='test-turn',observed_turn_id='test-turn')
        payload=dict(objective='test',authorization=event,interpretation='test fixture authorization',
                     source_message={key:event[key] for key in ('source_kind','source_id','exact_quote','source_created_at','original_turn_id')},
                     app_goal_observation=dict(goal_id='test-goal',thread_id='test-thread',status='active',
                                               source_kind='get_goal',source_id='test-receipt',observed_at=now),
                     models={'gpt-6.1-sol':['high']},slot_limit=4,inventory=[],observed_at=now)
        result=self.cli('workflow','activate',run=run,payload=payload,good=False,
                        env_overrides={'PATH':''})
        self.assertIn('watcher',result.stderr)
        state=self.state(run)/'state.json'
        self.assertFalse(state.exists() and json.loads(state.read_bytes())['status']=='active')
        monitor=load('candidate_failure_monitor',self.package/'private/limit-watcher/scripts/monitor.py')
        self.assertFalse(monitor.marker_state(self.state(run)/'watcher')['liveIdentity'])

    def test_coordination_cannot_bypass_watcher_lifecycle(self):
        request=self.project/'batch.json'
        request.write_text(json.dumps(dict(batch_id='illegal',steps=[dict(key='start',command='activate',payload={})])))
        result=self.cli('coordination','--input',str(request),good=False)
        self.assertIn('direct workflow command',result.stderr)
        self.assertFalse((self.state()/'watcher/control.json').exists())

    def test_mismatch_missing_corruption_manifest_and_cli_bypass_fail_before_mutation(self):
        self.cli('workflow','status')
        before = hashes(self.project)
        self.cli('workflow','status',version='2.0',good=False)
        self.cli('workflow','status',pin='0'*64,good=False)
        for name in ('scripts/workflow.py','scripts/coordination.py','roles/coordinator.toml','private/limit-watcher/scripts/monitor.py','private/limit-watcher/role.json','private/limit-watcher/limit_watcher.md','scripts/candidate.py','candidate-profile.json'):
            path = self.package / name
            content = path.read_bytes()
            for missing in (False,True):
                if missing:
                    path.unlink()
                else:
                    path.write_bytes(content+b'changed')
                self.cli('workflow','status',good=False)
                self.assertEqual(hashes(self.project),before)
                path.write_bytes(content)
        raw = self.manifest.read_bytes()
        manifest = json.loads(raw)
        manifest['version'] = '2.0'
        self.manifest.write_text(json.dumps(manifest))
        self.cli('workflow','status',good=False)
        self.cli('workflow','status',pin=hashlib.sha256(self.manifest.read_bytes()).hexdigest(),good=False)
        self.manifest.write_bytes(raw)
        helper=self.package/'scripts/workflow.py'
        helper_raw=helper.read_bytes()
        helper.write_bytes(helper_raw+b'\n# self-repinned mutation\n')
        manifest=json.loads(raw)
        manifest['files']['scripts/workflow.py']=hashlib.sha256(helper.read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps(manifest))
        self.cli('workflow','status',pin=hashlib.sha256(self.manifest.read_bytes()).hexdigest(),good=False)
        self.assertEqual(hashes(self.project),before)
        helper.write_bytes(helper_raw)
        self.manifest.write_bytes(raw)
        for path in (self.source.parent/'workflow.py',self.source.parent/'coordination.py',self.package/'scripts/workflow.py',self.package/'scripts/coordination.py',self.package/'private/limit-watcher/scripts/monitor.py'):
            result = subprocess.run([*PYTHON,str(path),'status','--project',str(self.project),'--codex-home',str(self.home)],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(hashes(self.project),before)
        self.assert_preserved()

    def test_supported_source_and_staged_strict_inputs_leave_state_unchanged(self):
        cases=[('{"typo":1}','FIELD_UNKNOWN','$.typo'),
               ('{"x":1,"x":2}','JSON_DUPLICATE_KEY','$.x'),
               ('{"x":NaN}','JSON_NONFINITE','$.x'),
               ('{"x":1e999}','JSON_NONFINITE','$.x'),
               ('[]','FIELD_TYPE','$')]
        for entry,run in ((self.source,'source-malformed'),(self.entry,'staged-malformed')):
            self.cli('workflow','status',entry=entry,run=run)
            before=hashes(self.state(run))
            for raw,code,field in cases:
                for source in ('stdin','file'):
                    with self.subTest(entry=entry,raw=raw,source=source):
                        command=['workflow','status']
                        if source=='file':
                            fixture=self.project/'malformed-input.json'
                            fixture.write_text(raw,encoding='utf-8-sig')
                            command+=['--input',str(fixture)]
                        result=self.cli(*command,entry=entry,run=run,raw=raw if source=='stdin' else '',good=False)
                        error=json.loads(result.stderr)
                        self.assertEqual((error['code'],error['field']),(code,field))
                        self.assertEqual(hashes(self.state(run)),before)

    def test_run_authorization_stored_binding_and_file_reservation(self):
        self.activate()
        active=json.loads((self.state()/'state.json').read_bytes())
        event=dict(active['authorization'], run_id='wrong-input-run', kind='policy_change')
        before=stable_state_hashes(self.state())
        self.cli('workflow','observe-authorization',payload=dict(authorization=event,interpretation='mismatch fixture',source_message={key:event[key] for key in ('source_kind','source_id','exact_quote','source_created_at','original_turn_id')}),good=False)
        self.assertEqual(stable_state_hashes(self.state()),before)
        for reserved in ('.agent-workflow/state.json','.agent-workflow-v1-0/runs/foreign/state.json'):
            before = stable_state_hashes(self.state())
            self.cli('workflow','task',payload=dict(id='bad',role='implementer',objective='bad',criteria=['bad'],writes=[reserved]),good=False)
            self.assertEqual(stable_state_hashes(self.state()),before)
        binding = self.state()/'run-binding.json'
        raw = binding.read_bytes()
        data = json.loads(raw); data['run_id']='wrong-run';binding.write_text(json.dumps(data))
        before = stable_state_hashes(self.state())
        self.cli('workflow','status',good=False)
        self.assertEqual(stable_state_hashes(self.state()),before)
        binding.write_bytes(raw)
        state = self.state()/'state.json'
        state_raw = state.read_bytes()
        data = json.loads(state_raw);data['run_id']='wrong-run';state.write_text(json.dumps(data))
        before = stable_state_hashes(self.state())
        self.cli('workflow','status',good=False)
        self.assertEqual(stable_state_hashes(self.state()),before)
        state.write_bytes(state_raw)

    def test_spawned_worker_refuses_changed_helper(self):
        self.corrupt_worker=True
        self.test_worker_pins_and_wrong_run_renew_stop_refused()

    def test_source_worker_uses_pinned_source_helper(self):
        self.entry=self.source
        self.test_worker_pins_and_wrong_run_renew_stop_refused()

    def test_candidate_lifecycle_discard_and_archived_invalidations(self):
        self.activate()
        (self.project/'input.txt').write_text('before')
        self.cli('workflow','task',payload=dict(id='work',role='implementer',objective='test',criteria=['safe'],writes=['output.txt'],file_inputs=[dict(path='input.txt',revision='r1')]))
        prepared=json.loads(self.cli('workflow','prepare',payload=dict(id='work')).stdout)
        self.cli('workflow','record',payload=dict(id='work',submitted_arguments=prepared['arguments'],session_id='synthetic-session',receipt={'id':'synthetic-session'}))
        before=stable_state_hashes(self.state())
        self.cli('workflow','finish',payload=dict(id='work',accepted=False,evidence='still running'),good=False)
        self.assertEqual(stable_state_hashes(self.state()),before)
        self.cli('workflow','inventory',payload=dict(inventory=[dict(id='synthetic-session',status='idle')],observed_at=time.time()))
        (self.project/'input.txt').write_text('changed')
        self.cli('workflow','finish',payload=dict(id='work',accepted=True,evidence='stale'),good=False)
        self.cli('workflow','finish',payload=dict(id='work',accepted=False,evidence='discard stale'))
        self.cli('workflow','task',payload=dict(id='replacement',role='implementer',objective='replace',criteria=['safe'],writes=['output.txt']))
        self.cli('workflow','cancel',payload=dict(id='replacement',reason='test compaction'))
        snapshot=json.loads(self.cli('workflow','snapshot',payload=dict(files=['input.txt'],writers_quiescent=True,next_action='inspect')).stdout)
        archive=self.state()/'checkpoints'/snapshot['checkpoint']['id']/'state.json'
        archived=json.loads(archive.read_bytes())
        self.assertEqual(len(archived['invalidations']),1)
        self.assertIn('work',archived['tasks'])
        self.cli('workflow','compact')
        current=json.loads((self.state()/'state.json').read_bytes())
        self.assertEqual(current['tasks'],{})
        self.assertFalse(current.get('invalidations'))
        self.assertEqual(json.loads(archive.read_bytes()),archived)

    def test_stop_before_start_preserves_source_and_staged_run_identity(self):
        for entry, mapping in ((self.source, 'source'), (self.entry, 'staged')):
            for initialized in (False, True):
                run = mapping + ('-initialized-stop' if initialized else '-first-stop')
                with self.subTest(mapping=mapping, initialized=initialized):
                    if initialized:
                        self.cli('workflow', 'status', entry=entry, run=run)
                    control_path = self.state(run) / 'watcher/control.json'
                    self.assertFalse(control_path.exists())
                    self.cli('watcher', 'stop', entry=entry, run=run)
                    control = json.loads(control_path.read_bytes())
                    self.assertEqual(control['runId'], run)
                    self.assertFalse(control['active'])
                    self.cli('watcher', 'stop', entry=entry, run=run)
                    self.assertEqual(json.loads(control_path.read_bytes()), control)
                    self.cli('workflow', 'validate', entry=entry, run=run)
                    self.cli('workflow', 'status', entry=entry, run=run)
                    self.cli('watcher', 'status', entry=entry, run=run)

    def test_worker_pins_and_wrong_run_renew_stop_refused(self):
        self.cli('workflow','validate')
        monitor = load('candidate_monitor_fixture',self.package/'private/limit-watcher/scripts/monitor.py')
        workflow = load('candidate_schedule_fixture',self.package/'scripts/workflow.py')
        base = self.state()/'watcher';base.mkdir()
        control = monitor.default_control('candidate-run')
        monitor.write(base/'control.json',control)
        windows = {key:dict(windowDurationMins=key,usedPercent=10,resetsAt=9999999999) for key in (300,10080)}
        previous = monitor.sample(control=control,measurer=lambda:windows,schedule=workflow.usage_schedule)
        monitor.write(base/'latest.json',previous)
        # A fresh synthetic sample delays measurement; the real spawned worker never calls codex.
        started = False
        changed_helper=None
        expected_exit='explicit_stop'
        try:
            self.cli('watcher','start');started=True
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if (base/'running.json').exists() and monitor.marker_state(base)['heartbeatFresh']:
                    break
                time.sleep(.05)
            self.assertTrue(monitor.marker_state(base)['heartbeatFresh'])
            ownership=json.loads((base/'ownership.json').read_bytes())
            self.assertEqual(ownership['run_id'],'candidate-run')
            self.assertEqual(ownership['manifest_sha256'],self.pin)
            self.assertEqual(ownership['dependencies']['scripts/workflow.py']['path'],str((self.entry.parent/'workflow.py').resolve()))
            self.cli('watcher','renew')
            # Wrong-run marker/control cannot be changed even if copied to another run namespace.
            self.cli('workflow','validate',run='other-run')
            other=self.state('other-run')/'watcher';shutil.copytree(base,other)
            before=hashes(other)
            self.cli('watcher','renew',run='other-run',good=False)
            self.cli('watcher','stop',run='other-run',good=False)
            self.assertEqual(hashes(other),before)
            self.assertTrue(monitor.marker_state(base)['liveIdentity'])
            self.cli('watcher','--state-dir='+str(other),good=False)
            if getattr(self,'corrupt_worker',False):
                helper=self.package/'scripts/workflow.py'
                changed_helper=(helper,helper.read_bytes())
                helper.write_bytes(changed_helper[1]+b'\n# corruption after worker spawn\n')
                deadline=time.monotonic()+5
                while time.monotonic()<deadline and monitor.marker_state(base)['liveIdentity']:
                    time.sleep(.05)
                self.assertFalse(monitor.marker_state(base)['liveIdentity'])
                self.assertEqual(monitor.read_health(base)['exitReason'],'worker_error')
                expected_exit='worker_error'
        finally:
            if changed_helper:
                changed_helper[0].write_bytes(changed_helper[1])
            if started:
                self.cli('watcher','stop')
                deadline=time.monotonic()+5
                while time.monotonic()<deadline and monitor.marker_state(base)['liveIdentity']:
                    time.sleep(.05)
                self.assertFalse(monitor.marker_state(base)['liveIdentity'])
                self.assertEqual(monitor.read_health(base)['exitReason'],expected_exit)

if __name__=='__main__':
    unittest.main(verbosity=2)
