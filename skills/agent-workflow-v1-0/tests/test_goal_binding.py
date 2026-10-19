"""N02 disposable evidence: native binding is distinct from human authorization."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

spec = importlib.util.spec_from_file_location('binding_workflow', Path(__file__).resolve().parents[1] / 'scripts/workflow.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class GoalBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.now = 10000
        self.engine = m.Workflow(self.root, Path(self.tmp.name) / 'home', lambda: self.now)
        self.counter = 0

    def payload(self):
        binding = dict(binding_id='binding-a', goal_id='goal-a', thread_id='thread-a', run_id='run-a',
                       scope=dict(kind='objective', id='scope-a', objective='deliver'), objective='deliver',
                       goal_created_at=100, observed_at=self.now)
        observation = dict(goal_id='goal-a', thread_id='thread-a', objective='deliver', goal_created_at=100,
                           status='active', source_kind='get_goal', source_id='native-tool-receipt', observed_at=self.now)
        return dict(native_binding=binding, app_goal_observation=observation, objective='deliver',
                    interpretation='Main binds only the existing objective.',
                    procedural_check='Main checked current goal context and newer human restrictions.',
                    models={'gpt-6.1-sol': ['high']}, slot_limit=4, inventory=[], observed_at=self.now)

    def call(self, command, **p):
        return self.engine.execute(command, p)

    def bind(self):
        return self.call('bind-active-goal', **self.payload())

    def usage(self):
        return self.call('usage', observed_at=self.now, rateLimits={
            'primary': dict(usedPercent=10, windowDurationMins=300),
            'secondary': dict(usedPercent=20, windowDurationMins=10080)})

    def human(self, kind='explicit_goal_continuation'):
        self.counter += 1
        event = dict(event_id=f'event-{self.counter}', exact_quote='Continue this goal now.', kind=kind,
                     source_kind='user_goal_continuation', source_id=f'message-{self.counter}',
                     goal_id='goal-a', thread_id='thread-a', run_id='run-a',
                     scope=dict(kind='objective', id='scope-a', objective='deliver'), observed_at=self.now,
                     source_created_at=self.now, original_turn_id=f'turn-{self.now}', observed_turn_id=f'turn-{self.now}')
        return dict(authorization=event, interpretation='Main checked a new direct continuation.',
                    source_message={k: event[k] for k in m.SOURCE_FIELDS},
                    app_goal_observation=self.payload()['app_goal_observation'])

    def reject(self, command, payload):
        before = self.engine.path.read_bytes() if self.engine.path.exists() else None
        with self.assertRaises(m.Error):
            self.call(command, **payload)
        self.assertEqual(self.engine.path.read_bytes() if self.engine.path.exists() else None, before)

    def halt(self):
        self.call('stop')
        self.call('snapshot', writers_quiescent=True, next_action='deliver')
        self.call('halt', inventory=[], observed_at=self.now, owned_processes_stopped=True, stop_evidence='synthetic stopped receipt')

    def test_long_initialization_preserves_original_creation_and_no_human_source(self):
        self.assertEqual(self.bind()['basis'], 'native_binding')
        state = m.read_json(self.engine.path)
        self.assertEqual(state['native_binding']['goal_created_at'], 100)
        self.assertEqual(state['authorization_events'], [])
        self.assertNotIn('authorization', state)
        self.assertNotIn('source_watermark', state)
        for key in ('exact_quote', 'source_message', 'original_turn_id', 'source_created_at'):
            self.assertNotIn(key, state['native_binding'])
        self.usage()
        self.call('task', id='work', role='implementer', objective='deliver', criteria=['done'])
        self.assertEqual(self.call('prepare', id='work')['tool'], 'collaboration.spawn_agent')

    def test_retained_human_stop_cannot_be_reactivated_by_status_corruption(self):
        for basis in ('native', 'human'):
            for remove_stop_field in (False, True):
                with self.subTest(basis=basis, remove_stop_field=remove_stop_field):
                    project = Path(self.tmp.name) / (basis + str(remove_stop_field))
                    project.mkdir()
                    self.engine = m.Workflow(project, Path(self.tmp.name) / 'home', lambda: self.now)
                    if basis == 'native':
                        self.bind()
                    else:
                        self.call('activate', objective='deliver', **self.human('explicit_task_authorization'),
                                  models={'gpt-6.1-sol':['high']}, slot_limit=4, inventory=[], observed_at=self.now)
                    self.usage()
                    self.call('task', id='pending', role='implementer', objective='deliver', criteria=['done'])
                    self.now += 1
                    stop = self.human('explicit_stop')
                    stop['authorization']['exact_quote'] = 'Stop this goal now.'
                    stop['source_message']['exact_quote'] = 'Stop this goal now.'
                    self.call('observe-authorization', **stop)
                    state = m.read_json(self.engine.path)
                    self.assertEqual(state['status'], 'stopping')
                    state['status'] = 'active'
                    if remove_stop_field:
                        state.pop('stopped_at')
                    m.write_json(self.engine.path, state)
                    self.reject('status', {})
                    self.reject('task', dict(id='forbidden', role='implementer', objective='deliver', criteria=['done']))
                    self.reject('prepare', dict(id='pending'))

    def test_delayed_accepted_stop_remains_a_native_binding_boundary(self):
        for remove_stop_field in (False, True):
            with self.subTest(remove_stop_field=remove_stop_field):
                project = Path(self.tmp.name) / ('delayed-stop-' + str(remove_stop_field))
                project.mkdir()
                self.engine = m.Workflow(project, Path(self.tmp.name) / 'home', lambda: self.now)
                self.bind()
                self.usage()
                self.call('task', id='pending', role='implementer', objective='deliver', criteria=['done'])
                self.now += 1
                stop = self.human('explicit_stop')
                for event in (stop['authorization'], stop['source_message']):
                    event.update(exact_quote='Stop this goal now.', source_created_at=self.now-2,
                                 original_turn_id='delayed-original-source')
                self.call('observe-authorization', **stop)
                state = m.read_json(self.engine.path)
                self.assertEqual(state['status'], 'stopping')
                state['status'] = 'active'
                if remove_stop_field:
                    state.pop('stopped_at')
                m.write_json(self.engine.path, state)
                self.reject('status', {})
                self.reject('task', dict(id='forbidden', role='implementer', objective='deliver', criteria=['done']))
                self.reject('prepare', dict(id='pending'))

    def test_real_clock_long_delay_cli_preserves_creation(self):
        # The creation is deliberately more than 300 real seconds before attachment;
        # there is no retimestamped human source or accelerated freshness clock.
        repository = Path(__file__).resolve().parents[3]
        python = [sys.executable, '-X', 'utf8', '-B', *(['-O'] if sys.flags.optimize else [])]
        build = Path(self.tmp.name) / 'candidate-build'
        built = subprocess.run([*python, str(repository / 'tooling/build-candidate.py'),
                                '--output', str(build)], capture_output=True, text=True, timeout=30)
        self.assertEqual(built.returncode, 0, built.stderr)
        package = build / 'agent-workflow-v1-0'
        pin = json.loads(built.stdout)['manifest_sha256']
        now = time.time()
        payload = self.payload()
        for item in (payload['native_binding'], payload['app_goal_observation']):
            item.update(goal_created_at=now - 3600, observed_at=now)
        payload['observed_at'] = now
        source = self.root / 'binding.json'
        for entry, run in ((repository / 'skills/agent-workflow-v1-0/scripts/candidate.py', 'source-clock'),
                           (package / 'scripts/candidate.py', 'staged-clock')):
            with self.subTest(run=run):
                payload['native_binding']['run_id'] = run
                m.write_json(source, payload)
                input_bytes = source.read_bytes()
                result = subprocess.run([*python, str(entry), '--manifest', str(package / 'manifest.json'),
                    '--manifest-sha256', pin, '--version', '1.0', '--project', str(self.root), '--run-id', run,
                    'workflow', 'bind-active-goal', '--input', str(source)], capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                state = m.read_json(self.root / '.agent-workflow-v1-0/runs' / run / 'state.json')
                self.assertEqual(state['native_binding']['goal_created_at'], now - 3600)
                self.assertEqual(state['app_goal_observation']['goal_created_at'], now - 3600)
                self.assertEqual(state['authorization_events'], [])
                self.assertEqual(source.read_bytes(), input_bytes)
        self.assertFalse(self.engine.path.exists())

    def test_binding_rejects_status_freshness_identity_scope_and_provenance(self):
        cases = [('status', x) for x in ('paused', 'blocked', 'complete', 'stopped')]
        cases += [('observed_at', self.now-61), ('observed_at', self.now+1), ('goal_id', 'other'),
                  ('thread_id', 'other'), ('objective', 'deliver plus more'), ('goal_created_at', 101),
                  ('source_kind', 'policy_change'), ('source_id', '')]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                p = self.payload(); p['app_goal_observation'][field] = value
                self.reject('bind-active-goal', p)
        for mutation in ('future-creation', 'report', 'expanded', 'command-objective', 'blank-interpretation', 'blank-check', 'live-child'):
            with self.subTest(mutation=mutation):
                p = self.payload()
                if mutation == 'future-creation':
                    p['native_binding']['goal_created_at'] = p['app_goal_observation']['goal_created_at'] = self.now+1
                if mutation == 'report': p['native_binding']['scope']['kind'] = 'report'
                if mutation == 'expanded': p['native_binding']['scope']['objective'] = 'deliver plus more'
                if mutation == 'command-objective': p['objective'] = 'report incident'
                if mutation == 'blank-interpretation': p['interpretation'] = ' '
                if mutation == 'blank-check': p['procedural_check'] = ''
                if mutation == 'live-child': p['inventory'] = [dict(id='child', status='unknown')]
                self.reject('bind-active-goal', p)

    def test_human_stop_report_policy_sources_cannot_masquerade_as_binding(self):
        for kind in ('explicit_stop', 'independent_report_request', 'policy_change'):
            with self.subTest(kind=kind):
                p = self.payload(); p['native_binding'] = self.human(kind)['authorization']
                self.reject('bind-active-goal', p)
                p = self.payload(); p['source_message'] = self.human(kind)['source_message']
                self.reject('bind-active-goal', p)

    def test_nonpristine_inactive_and_migrated_state_reject(self):
        for extra in (dict(revalidation_required=True), dict(authorization_event_ids=['past-stop']),
                      dict(reserved_run_ids=['past-run']), dict(stopped_at=1), dict(halted_at=2),
                      dict(completed_at=3), dict(usage_errors=0),
                      dict(migration=dict(source_schema='1', source_hash='a'*64, semantic_hash='b'*64, archive='archive'))):
            with self.subTest(extra=extra):
                m.write_json(self.engine.path, dict(schema_version=2, status='inactive', tasks={}, **extra))
                self.reject('bind-active-goal', self.payload())

    def test_existing_active_stopping_halted_complete_cannot_bind(self):
        self.bind(); self.usage()
        self.reject('bind-active-goal', self.payload())
        self.call('stop'); self.reject('bind-active-goal', self.payload())
        self.call('snapshot', writers_quiescent=True, next_action='deliver')
        self.call('halt', inventory=[], observed_at=self.now, owned_processes_stopped=True, stop_evidence='stopped')
        self.reject('bind-active-goal', self.payload())
        self.now += 1; self.usage(); self.call('inventory', inventory=[], observed_at=self.now)
        self.call('resume', **self.human())
        self.call('snapshot', writers_quiescent=True, next_action='done'); self.call('complete')
        self.reject('bind-active-goal', self.payload())

    def test_refresh_rechecks_creation_objective_and_never_resumes_stop(self):
        self.bind(); self.usage(); self.now += 61
        self.reject('task', dict(id='stale', role='implementer', objective='deliver', criteria=['done']))
        for field, value in [('objective', 'broader'), ('goal_created_at', 101), ('thread_id', 'other')]:
            p = self.payload()['app_goal_observation']; p[field] = value
            self.reject('app-goal', dict(app_goal_observation=p))
        self.call('app-goal', app_goal_observation=self.payload()['app_goal_observation'])
        self.usage(); self.call('inventory', inventory=[], observed_at=self.now)
        self.call('task', id='fresh', role='implementer', objective='deliver', criteria=['done'])
        self.halt(); self.now += 1
        self.call('app-goal', app_goal_observation=self.payload()['app_goal_observation'])
        self.assertEqual(self.call('status')['status'], 'halted')
        self.reject('bind-active-goal', self.payload())
        self.reject('task', dict(id='forbidden', role='implementer', objective='deliver', criteria=['done']))

    def test_usage_uncertainty_and_inventory_gates_still_apply(self):
        self.bind()
        self.reject('task', dict(id='work', role='implementer', objective='deliver', criteria=['done']))
        self.usage()
        self.call('task', id='work', role='implementer', objective='deliver', criteria=['done'])
        self.now += 61
        self.call('app-goal', app_goal_observation=self.payload()['app_goal_observation'])
        self.reject('prepare', dict(id='work'))
        self.call('inventory', inventory=[], observed_at=self.now)
        self.call('prepare', id='work')
        self.call('dispatch_failed', id='work', evidence='unknown outcome', certain_not_started=False)
        self.reject('task', dict(id='uncertain', role='implementer', objective='deliver', criteria=['done']))

    def test_stop_halt_requires_new_human_continuation_once_and_keeps_native_history(self):
        self.bind(); self.usage()
        stop = self.human('explicit_stop'); stop.pop('app_goal_observation')
        self.call('observe-authorization', **stop)
        self.call('snapshot', writers_quiescent=True, next_action='deliver')
        self.call('halt', inventory=[], observed_at=self.now, owned_processes_stopped=True, stop_evidence='stopped')
        self.now += 1; self.usage(); self.call('inventory', inventory=[], observed_at=self.now)
        for kind in ('explicit_stop', 'policy_change', 'independent_report_request'):
            self.reject('resume', self.human(kind))
        old = self.human(); old['authorization']['source_created_at'] = self.now-1
        old['source_message'] = {k: old['authorization'][k] for k in m.SOURCE_FIELDS}
        self.reject('resume', old)
        evidence = self.human(); self.call('resume', **evidence)
        self.reject('resume', evidence)
        state = m.read_json(self.engine.path)
        self.assertNotIn('native_binding', state)
        self.assertEqual(state['native_binding_ids'], ['binding-a'])
        self.assertEqual(state['native_binding_events'][0]['binding']['goal_created_at'], 100)
        self.assertEqual(len(state['authorization_events']), 2)
        self.call('snapshot', writers_quiescent=True, next_action='done'); self.call('complete')
        self.now += 1
        evidence = self.human('explicit_task_authorization')
        evidence['authorization'].update(run_id='run-b', scope=dict(kind='objective', id='scope-b', objective='deliver'))
        self.call('activate', objective='deliver', **evidence, models={}, slot_limit=4, inventory=[], observed_at=self.now)
        self.assertEqual(m.read_json(self.engine.path)['native_binding_ids'], ['binding-a'])
        self.assertEqual(m.read_json(self.engine.path)['reserved_run_ids'], ['run-a', 'run-b'])

    def test_binding_identity_is_reserved_against_later_human_event_replay(self):
        self.bind(); self.usage(); self.halt()
        self.now += 1; self.usage(); self.call('inventory', inventory=[], observed_at=self.now)
        evidence = self.human(); evidence['authorization']['event_id'] = 'binding-a'
        self.reject('resume', evidence)

    def test_stored_current_and_historical_native_corruption_fails_closed(self):
        self.bind(); self.usage()
        pristine = self.engine.path.read_bytes()
        def change(s, mode):
            if mode == 'history-missing': s['native_binding_events'] = []
            elif mode == 'reservation-missing': s['reserved_run_ids'] = []
            elif mode == 'id-duplicate': s['native_binding_ids'].append('binding-a')
            elif mode == 'human-id-collision': s['authorization_event_ids'].append('binding-a')
            elif mode == 'dual-basis': s['authorization'] = self.human()['authorization']
            elif mode == 'current-objective': s['native_binding']['objective'] = 'broader'
            elif mode == 'current-creation': s['native_binding']['goal_created_at'] = 101
            elif mode == 'observation-creation': s['app_goal_observation']['goal_created_at'] = 101
            elif mode == 'observation-objective': s['app_goal_observation']['objective'] = 'broader'
            elif mode == 'helper-objective': s['objective'] = 'broader'
            else:
                entry = s['native_binding_events'][0]
                if mode == 'history-source': entry['app_goal_observation']['source_kind'] = 'user_message'
                elif mode == 'history-paused': entry['app_goal_observation']['status'] = 'paused'
                elif mode == 'history-created': entry['binding']['goal_created_at'] = self.now+1
                elif mode == 'history-report': entry['binding']['scope']['kind'] = 'report'
                elif mode == 'history-check': entry['procedural_check'] = ''
                elif mode == 'history-human-field': entry['binding']['exact_quote'] = 'invented'
        modes = ('history-missing', 'reservation-missing', 'id-duplicate', 'human-id-collision', 'dual-basis',
                 'current-objective', 'current-creation', 'observation-creation', 'observation-objective', 'helper-objective',
                 'history-source', 'history-paused', 'history-created', 'history-report', 'history-check', 'history-human-field')
        for mode in modes:
            with self.subTest(mode=mode):
                self.engine.path.write_bytes(pristine)
                state = m.read_json(self.engine.path); change(state, mode); m.write_json(self.engine.path, state)
                self.reject('status', {})
        self.engine.path.write_bytes(pristine)
        self.halt(); self.now += 1; self.usage(); self.call('inventory', inventory=[], observed_at=self.now)
        self.call('resume', **self.human())
        resumed = self.engine.path.read_bytes()
        for mode in ('history-source', 'history-created', 'history-report', 'history-check', 'reservation-missing'):
            self.engine.path.write_bytes(resumed)
            state = m.read_json(self.engine.path); change(state, mode); m.write_json(self.engine.path, state)
            self.reject('status', {})


if __name__ == '__main__':
    unittest.main()
