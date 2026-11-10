"""Independent A26 transition table; every engine writes only a disposable project."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('authorization_workflow', Path(__file__).resolve().parents[1] / 'scripts/workflow.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.now = 10000
        self.engine = m.Workflow(self.root, Path(self.tmp.name) / 'home', lambda: self.now)
        self.counter = 0

    def evidence(self, kind='explicit_task_authorization', quote='Start this objective now.'):
        self.counter += 1
        event = dict(event_id=f'event-{self.counter}', exact_quote=quote, kind=kind,
                     source_kind='user_message', source_id=f'message-{self.counter}', goal_id='goal-a',
                     thread_id='thread-a', run_id='run-a', scope=dict(kind='objective', id='scope-a', objective='deliver'), observed_at=self.now, source_created_at=self.now, original_turn_id='turn-'+str(self.now), observed_turn_id='turn-'+str(self.now))
        return dict(authorization=event, interpretation='Main classified a direct user instruction.',
                    source_message={k: event[k] for k in m.SOURCE_FIELDS},
                    app_goal_observation=dict(goal_id='goal-a', thread_id='thread-a', status='active',
                                              source_kind='get_goal', source_id='goal-tool-receipt', observed_at=self.now))

    def call(self, cmd, **payload):
        return self.engine.execute(cmd, payload)

    def activate(self, evidence=None):
        return self.call('activate', objective='deliver', **(evidence or self.evidence()),
                         models={'gpt-6.1-sol': ['high']}, slot_limit=4, inventory=[], observed_at=self.now)

    def usage(self):
        return self.call('usage', observed_at=self.now, rateLimits={
            'primary': {'usedPercent': 10.0, 'windowDurationMins': 300, 'resetsAt': 20000},
            'secondary': {'usedPercent': 20.0, 'windowDurationMins': 10080, 'resetsAt': 600000}})

    def halt(self, evidence=None):
        self.activate(evidence); self.usage()
        self.call('task', id='old_work', role='implementer', objective='deliver', criteria=['saved'], writes=['artifact.txt'])
        (self.root / 'artifact.txt').write_text('checkpoint content')
        self.call('stop')
        self.call('snapshot', writers_quiescent=True, files=['artifact.txt'], next_action='deliver')
        self.call('halt', observed_at=self.now, inventory=[], owned_processes_stopped=True, stop_evidence='synthetic stopped-tool receipt')
        self.now += 1
        self.usage()
        self.call('inventory', observed_at=self.now, inventory=[])

    def rejected_unchanged(self, command, payload):
        before = self.engine.path.read_bytes() if self.engine.path.exists() else None
        with self.assertRaises(m.Error):
            self.call(command, **payload)
        self.assertEqual(self.engine.path.read_bytes() if self.engine.path.exists() else None, before)

    def test_original_source_age_metadata_and_observation_do_not_authorize_replay(self):
        self.activate(); self.usage()
        old = self.evidence('explicit_stop', 'Stop the objective now.')
        old.pop('app_goal_observation')
        self.call('observe-authorization', **old)
        self.call('snapshot', writers_quiescent=True, next_action='continue')
        self.call('halt', inventory=[], observed_at=self.now, owned_processes_stopped=True, stop_evidence='stopped')
        self.now += 1; self.usage(); self.call('inventory', inventory=[], observed_at=self.now)
        continuation = self.evidence('explicit_goal_continuation', 'Continue this goal now.')
        continuation['authorization']['source_kind'] = 'user_goal_continuation'
        continuation['source_message']['source_kind'] = 'user_goal_continuation'
        self.call('resume', **continuation)
        self.assertEqual(self.call('status')['app_goal_observation']['status'], 'active')
        for mutation in ('observation', 'kind', 'source-id', 'thread', 'run'):
            with self.subTest(mutation=mutation):
                import copy
                replay = copy.deepcopy(old)
                replay['authorization'].update(event_id='replay-' + mutation, observed_at=self.now,
                                               observed_turn_id=continuation['authorization']['observed_turn_id'])
                if mutation == 'kind': replay['authorization']['kind'] = 'policy_change'
                if mutation == 'source-id':
                    replay['authorization']['source_id'] = 'disguised-old-source'
                    replay['source_message']['source_id'] = 'disguised-old-source'
                if mutation == 'thread': replay['authorization']['thread_id'] = 'another-thread'
                if mutation == 'run': replay['authorization']['run_id'] = 'another-run'
                self.rejected_unchanged('observe-authorization', replay)
        self.assertEqual(self.call('status')['status'], 'active')
        self.assertEqual(self.call('status')['app_goal_observation']['status'], 'active')
        state = m.read_json(self.engine.path)
        self.assertEqual(state['source_watermark']['source_id'], continuation['authorization']['source_id'])

    def test_fresh_observation_cannot_promote_old_original_source_or_metadata(self):
        for field, value in [('source_created_at', self.now - 301), ('source_created_at', self.now + 1),
                             ('source_created_at', True), ('original_turn_id', ''), ('observed_turn_id', False)]:
            with self.subTest(field=field, value=value):
                evidence = self.evidence(); evidence['authorization'][field] = value
                if field in m.SOURCE_FIELDS: evidence['source_message'][field] = value
                self.rejected_unchanged('activate', dict(objective='deliver', **evidence, models={}, slot_limit=4, inventory=[], observed_at=self.now))
        evidence = self.evidence(); evidence['source_message']['original_turn_id'] = 'different-original-turn'
        self.rejected_unchanged('activate', dict(objective='deliver', **evidence, models={}, slot_limit=4, inventory=[], observed_at=self.now))

    def test_cross_run_thread_observations_and_old_new_ids_fail_at_identity_guard(self):
        self.activate(); self.usage()
        for key in ('run_id', 'thread_id', 'goal_id'):
            evidence = self.evidence('policy_change'); evidence.pop('app_goal_observation')
            evidence['authorization'][key] = 'foreign'
            before = self.engine.path.read_bytes()
            with self.assertRaisesRegex(m.Error, 'run/thread identity mismatch'):
                self.call('observe-authorization', **evidence)
            self.assertEqual(self.engine.path.read_bytes(), before)
        self.now += 1
        evidence = self.evidence('policy_change'); evidence.pop('app_goal_observation')
        evidence['authorization']['source_created_at'] -= 1
        evidence['authorization']['original_turn_id'] = 'new-turn-with-old-source-time'
        evidence['source_message'] = {key: evidence['authorization'][key] for key in m.SOURCE_FIELDS}
        with self.assertRaisesRegex(m.Error, 'chronology watermark'):
            self.call('observe-authorization', **evidence)

    def test_mechanical_quota_stop_retains_app_goal_and_cannot_resume(self):
        self.activate()
        self.call('usage', observed_at=self.now, rateLimits={'primary': dict(usedPercent=90, windowDurationMins=300),
                                                          'secondary': dict(usedPercent=20, windowDurationMins=10080)})
        state = m.read_json(self.engine.path)
        self.assertEqual(state['status'], 'stopping')
        self.assertEqual(state['app_goal_observation']['status'], 'active')
        self.assertEqual(len(state['authorization_events']), 1)
        self.rejected_unchanged('resume', self.evidence('explicit_goal_continuation'))

    def test_current_provenance_ledger_cannot_be_replaced_with_reservation_only(self):
        self.activate(); self.usage()
        state = m.read_json(self.engine.path)
        state['authorization_events'] = []
        state.pop('source_watermark')
        m.write_json(self.engine.path, state)
        self.rejected_unchanged('task', dict(id='forged', role='implementer', objective='deliver', criteria=['checked']))

    def test_activation_rejects_legacy_strings_and_missing_fields(self):
        for invalid in (None, '', 'Continue full objective', True, [], {}):
            with self.subTest(invalid=invalid):
                evidence = self.evidence(); evidence['authorization'] = invalid
                self.rejected_unchanged('activate', dict(objective='deliver', **evidence, models={}, slot_limit=4, inventory=[], observed_at=self.now))
        for field in ('event_id', 'exact_quote', 'kind', 'source_kind', 'source_id', 'goal_id', 'thread_id', 'run_id', 'scope', 'observed_at'):
            with self.subTest(field=field):
                evidence=self.evidence(); del evidence['authorization'][field]
                self.rejected_unchanged('activate', dict(objective='deliver', **evidence, models={}, slot_limit=4, inventory=[], observed_at=self.now))

    def test_activation_rejects_invalid_timestamps_and_source(self):
        for at in (True, '10000', float('nan'), float('inf'), self.now + 1, self.now - 301):
            with self.subTest(at=at):
                evidence=self.evidence(); evidence['authorization']['observed_at']=at
                self.rejected_unchanged('activate', dict(objective='deliver', **evidence, models={}, slot_limit=4, inventory=[], observed_at=self.now))
        evidence=self.evidence(); evidence['authorization']['source_kind']='assistant_interpretation'
        self.rejected_unchanged('activate', dict(objective='deliver', **evidence, models={}, slot_limit=4, inventory=[], observed_at=self.now))

    def test_invalid_field_types_and_source_projection_fail_closed(self):
        for field, value in [('kind', []), ('source_kind', {}), ('scope', []), ('exact_quote', True),
                             ('scope', dict(kind=[], id='scope-a', objective='deliver'))]:
            with self.subTest(field=field):
                evidence=self.evidence(); evidence['authorization'][field]=value
                self.rejected_unchanged('activate', dict(objective='deliver', **evidence, models={}, slot_limit=4, inventory=[], observed_at=self.now))
        for field, value in [('source_message', 'quote'), ('interpretation', {}),
                             ('app_goal_observation', {}), ('app_goal_observation', dict(status=[]))]:
            with self.subTest(field=field):
                evidence=self.evidence(); evidence[field]=value
                self.rejected_unchanged('activate', dict(objective='deliver', **evidence, models={}, slot_limit=4, inventory=[], observed_at=self.now))

    def test_future_intent_and_policy_cannot_activate(self):
        for kind in ('future_goal_intent', 'policy_change', 'explicit_stop'):
            with self.subTest(kind=kind):
                self.rejected_unchanged('activate', dict(objective='deliver', **self.evidence(kind), models={}, slot_limit=4, inventory=[], observed_at=self.now))

    def test_transition_table_noncontinuations_never_resume_or_dispatch(self):
        self.halt()
        for kind, quote in [('future_goal_intent', 'Goal ile devam ettireceğim'),
                            ('policy_change', 'Şu anlık haftalık kullanımı görmezden gel.'),
                            ('independent_report_request', 'Bu olay için ayrı bir rapor yaz.')]:
            with self.subTest(kind=kind):
                evidence=self.evidence(kind, quote)
                if kind == 'independent_report_request':
                    evidence['authorization']['scope']=dict(kind='report', id='incident-report', objective='report incident')
                self.rejected_unchanged('resume', evidence)
                self.call('observe-authorization', **evidence)
                self.assertEqual(self.call('status')['status'], 'halted')
                self.rejected_unchanged('task', dict(id='unauthorized', role='implementer', objective='deliver', criteria=['done']))
                self.rejected_unchanged('prepare', dict(id='old_work'))
        self.rejected_unchanged('activate', dict(objective='report incident', **self.evidence('independent_report_request'), models={}, slot_limit=4, inventory=[], observed_at=self.now))

    def test_quote_misuse_and_interpretation_remain_separate(self):
        self.halt()
        evidence=self.evidence('explicit_goal_continuation', 'Şu anlık haftalık kullanımı görmezden gel.')
        evidence['authorization']['exact_quote'] += ' Continue full objective.'
        self.rejected_unchanged('resume', evidence)
        evidence=self.evidence('explicit_goal_continuation', 'Goalü şimdi devam ettir.')
        evidence['interpretation']='Continue the matched objective after checks.'
        self.call('resume', **evidence)
        state=m.read_json(self.engine.path)
        saved=state['authorization_events'][-1]
        self.assertEqual(saved['event']['exact_quote'], 'Goalü şimdi devam ettir.')
        self.assertEqual(saved['interpretation'], evidence['interpretation'])
        self.assertNotIn(saved['interpretation'], saved['event']['exact_quote'])

    def test_stale_reused_or_mismatched_continuations_preserve_bytes(self):
        self.halt()
        for field, value in [('observed_at', 10000), ('event_id', 'event-1'), ('source_id', 'message-1'), ('run_id', 'other-run'),
                             ('thread_id', 'other-thread'), ('goal_id', 'other-goal'),
                             ('scope', dict(kind='objective', id='other', objective='deliver'))]:
            with self.subTest(field=field):
                evidence=self.evidence('explicit_goal_continuation'); evidence['authorization'][field]=value
                self.rejected_unchanged('resume', evidence)

    def test_matching_continuation_resumes_once_and_allows_fresh_dispatch(self):
        self.halt()
        evidence=self.evidence('explicit_goal_continuation', 'Continue goal-a now.')
        self.assertEqual(self.call('resume', **evidence)['status'], 'active')
        self.assertEqual(m.read_json(self.engine.path)['tasks']['old_work']['state'], 'rejected')
        self.call('task', id='fresh_work', role='implementer', objective='deliver', criteria=['done'])
        prepared=self.call('prepare', id='fresh_work')
        self.assertEqual(prepared['tool'], 'collaboration.spawn_agent')
        self.assertEqual(prepared['arguments']['task_name'], 'fresh_work')
        self.rejected_unchanged('resume', evidence)

    def test_stored_app_goal_boundary_60_allows_task_and_prepare(self):
        self.activate(); self.usage()
        self.now += 60
        self.usage()
        self.call('inventory', observed_at=self.now, inventory=[])
        self.call('task', id='boundary', role='implementer', objective='deliver', criteria=['done'])
        self.assertEqual(self.call('prepare', id='boundary')['tool'], 'collaboration.spawn_agent')

    def test_stored_app_goal_revalidated_before_task_and_prepare(self):
        self.activate(); self.usage()
        self.call('task', id='ready', role='implementer', objective='deliver', criteria=['done'])
        self.now += 61
        self.usage()
        self.call('inventory', observed_at=self.now, inventory=[])
        saved = self.engine.path.read_bytes()
        for field, value in [('observed_at', 10000), ('observed_at', self.now + 1),
                             ('goal_id', 'other-goal'), ('thread_id', 'other-thread')]:
            with self.subTest(field=field, value=value):
                self.engine.path.write_bytes(saved)
                state = m.read_json(self.engine.path)
                state['app_goal_observation']['observed_at'] = self.now
                state['app_goal_observation'][field] = value
                m.write_json(self.engine.path, state)
                for command, payload in [('task', dict(id='new', role='implementer', objective='deliver', criteria=['done'])),
                                         ('prepare', dict(id='ready'))]:
                    self.rejected_unchanged(command, payload)
        self.engine.path.write_bytes(saved)
        self.call('app-goal', app_goal_observation=self.evidence()['app_goal_observation'])
        self.assertEqual(self.call('prepare', id='ready')['tool'], 'collaboration.spawn_agent')

    def check_source_replay(self, initial_kind):
        original = self.evidence()
        original['authorization']['source_kind'] = initial_kind
        original['source_message']['source_kind'] = initial_kind
        self.halt(original)
        for source_kind in ('user_message', 'user_goal_continuation'):
            with self.subTest(source_kind=source_kind):
                replay = self.evidence('explicit_goal_continuation', original['authorization']['exact_quote'])
                replay['authorization'].update(source_kind=source_kind, source_id=original['authorization']['source_id'])
                replay['source_message'] = {k: replay['authorization'][k] for k in m.SOURCE_FIELDS}
                self.rejected_unchanged('resume', replay)

    def test_user_message_cannot_replay_with_new_event_or_continuation_kind(self):
        self.check_source_replay('user_message')

    def test_user_continuation_cannot_replay_with_new_event_or_message_kind(self):
        self.check_source_replay('user_goal_continuation')

    def test_resume_requires_active_app_goal_not_helper_state(self):
        self.halt()
        for field, value in [('status', 'paused'), ('status', 'blocked'), ('goal_id', 'other'),
                             ('observed_at', 10000), ('source_kind', 'helper_status')]:
            with self.subTest(field=field, value=value):
                evidence=self.evidence('explicit_goal_continuation'); evidence['app_goal_observation'][field]=value
                self.rejected_unchanged('resume', evidence)
        evidence=self.evidence('explicit_goal_continuation'); del evidence['app_goal_observation']
        self.rejected_unchanged('resume', evidence)

    def test_resume_requires_usage_inventory_and_checkpoint_current_hashes(self):
        self.halt()
        saved=self.engine.path.read_bytes()
        for field in ('usage', 'inventory'):
            with self.subTest(field=field):
                state=m.read_json(self.engine.path); state[field]['at']=state['halted_at']; m.write_json(self.engine.path,state)
                self.rejected_unchanged('resume', self.evidence('explicit_goal_continuation'))
                self.engine.path.write_bytes(saved)
        (self.root/'artifact.txt').write_text('drift')
        self.rejected_unchanged('resume', self.evidence('explicit_goal_continuation'))

    def test_explicit_stop_freezes_prepared_and_new_dispatch(self):
        self.activate(); self.usage()
        self.call('task', id='ready', role='implementer', objective='deliver', criteria=['done'])
        self.call('observe-authorization', **self.evidence('explicit_stop', 'Dur.'))
        self.rejected_unchanged('prepare', dict(id='ready'))
        self.rejected_unchanged('task', dict(id='new', role='implementer', objective='deliver', criteria=['done']))

    def test_app_observation_cannot_resume_and_inactive_app_freezes_dispatch(self):
        self.activate(); self.usage()
        evidence=self.evidence(); evidence['app_goal_observation']['status']='paused'
        self.call('app-goal', app_goal_observation=evidence['app_goal_observation'])
        self.assertEqual(self.call('status')['status'], 'stopping')
        evidence['app_goal_observation']['status']='active'
        self.call('app-goal', app_goal_observation=evidence['app_goal_observation'])
        self.assertEqual(self.call('status')['status'], 'stopping')
        self.rejected_unchanged('task', dict(id='new', role='implementer', objective='deliver', criteria=['done']))

    def test_independent_report_activates_only_its_separate_scope(self):
        evidence=self.evidence('independent_report_request', 'Write an incident report.')
        evidence['authorization']['scope']=dict(kind='report', id='report-scope', objective='report incident')
        self.rejected_unchanged('activate', dict(objective='deliver', **evidence, models={}, slot_limit=4, inventory=[], observed_at=self.now))
        self.assertEqual(self.call('activate', objective='report incident', **evidence, models={}, slot_limit=4, inventory=[], observed_at=self.now)['status'], 'active')
        self.assertEqual(m.read_json(self.engine.path)['objective'], 'report incident')


if __name__ == '__main__':
    unittest.main()
