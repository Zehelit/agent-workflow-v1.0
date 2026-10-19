import importlib.util
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import shutil
import subprocess
import sys
import zipfile

spec = importlib.util.spec_from_file_location('workflow', Path(__file__).resolve().parents[1] / 'scripts/workflow.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def authorization(now, objective='test', kind='explicit_task_authorization', event_id='user-event-1'):
    event = dict(event_id=event_id, exact_quote='Continue this scoped task now.', kind=kind,
                 source_kind='user_message', source_id=event_id, goal_id='goal-1', thread_id='thread-1',
                 run_id='run-1', scope=dict(kind='objective', id='scope-1', objective=objective), observed_at=now, source_created_at=now, original_turn_id='turn-'+str(now), observed_turn_id='turn-'+str(now))
    return dict(authorization=event, interpretation='Main classified this direct request.',
                source_message={k: event[k] for k in m.SOURCE_FIELDS},
                app_goal_observation=dict(goal_id='goal-1', thread_id='thread-1', status='active',
                                          source_kind='get_goal', source_id='tool-goal-1', observed_at=now))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.now = 10000
        self.engine = m.Workflow(self.root, Path(self.tmp.name) / 'home', lambda: self.now)
        self.call('activate', objective='test', **authorization(self.now), models={
            'gpt-6.1-sol': ['medium','high','xhigh','max'], 'gpt-6-astra': ['low','medium','high']},
            slot_limit=4, inventory=[], observed_at=self.now)
        self.usage()

    def call(self, cmd, **p):
        return self.engine.execute(cmd, p)

    def usage(self, used=10):
        return self.call('usage', observed_at=self.now, rateLimits={'primary': {'usedPercent': used, 'windowDurationMins': 300}, 'secondary': {'usedPercent': 20, 'windowDurationMins': 10080}})

    def app_goal(self):
        return self.call('app-goal', app_goal_observation=authorization(self.now)['app_goal_observation'])

    def task(self, key='work', role='implementer', writes=None, **kw):
        return self.call('task', id=key, role=role, objective='deliver', criteria=['works'], writes=writes or [], **kw)

    def start(self, key='work'):
        args = self.call('prepare', id=key)['arguments']
        self.call('record', id=key, submitted_arguments=args, session_id=key, receipt={'id': key})
        self.inv(key)
        return args

    def inv(self, *keys, status='running'):
        return self.call('inventory', observed_at=self.now, inventory=[{'id': k, 'status': status} for k in keys])

    def snap(self, files=None):
        return self.call('snapshot', writers_quiescent=True, next_action='continue', files=files or [])

    def test_adaptive_due_reuses_measurement(self):
        self.assertEqual(self.call('usage-due')['action'], 'REUSE_MEASUREMENT')
        self.assertEqual(self.call('usage-due', substantial_work=True)['action'], 'READ_USAGE')
        self.now += 300
        self.assertEqual(self.call('usage-due')['action'], 'READ_USAGE')

    def test_adaptive_bands_and_expiry(self):
        self.now += 601
        self.assertEqual(self.usage(76)['check_interval_seconds'], 60)
        self.assertEqual(self.call('usage-due')['action'], 'REUSE_MEASUREMENT')
        self.now += 60
        self.app_goal()
        with self.assertRaisesRegex(m.Error, 'fresh usage above threshold required'): self.task()

    def test_fast_drain_early_recovery(self):
        self.now += 60
        r = self.usage(70)
        self.assertEqual(r['action'], 'SAVE_AND_STOP')
        self.assertGreater(r['remaining'], 10)
        with self.assertRaises(m.Error): self.task()
        self.assertEqual(self.call('usage-due')['action'], 'SAVE_AND_STOP')

    def test_weekly_drain_is_independent(self):
        self.now += 60
        r = self.call('usage', observed_at=self.now, rateLimits={
            'primary': {'usedPercent': 10, 'windowDurationMins': 300},
            'secondary': {'usedPercent': 80, 'windowDurationMins': 10080}})
        self.assertEqual(r['action'], 'SAVE_AND_STOP')

    def test_reset_does_not_reuse_old_drain(self):
        prev = {'at': 100, 'windows': {'300': {'usedPercent': 80, 'resetsAt': 200}}, 'rates': {'300': 1}}
        current = {300: {'usedPercent': 0, 'resetsAt': 400}, 10080: {'usedPercent': 10}}
        r = m.usage_schedule(current, 120, prev)
        self.assertEqual(r['action'], 'CONTINUE')
        self.assertEqual(r['check_interval_seconds'], 300)

    def test_checkpoint_warning_without_threshold_stop(self):
        self.now += 601
        self.assertEqual(self.usage(75)['action'], 'CHECKPOINT_SOON')
        self.assertEqual(self.usage(80)['action'], 'SERIAL_SHORT_WORK')

    def test_model_settings(self):
        self.task(role='researcher')
        args = self.call('prepare', id='work')['arguments']
        self.assertEqual((args['model'], args['reasoning_effort'], args['fork_turns']), ('gpt-6.1-sol', 'high', 'none'))
        for role in ('coordinator','architect','researcher','implementer','verifier'):
            self.assertEqual(self.engine.settings(role,None), {'model':'gpt-6.1-sol','reasoning_effort':'high'})
            for effort in ('low','medium','xhigh','max'):
                with self.assertRaises(m.Error): self.engine.settings(role,effort)
        with self.assertRaises(m.Error): self.engine.settings('astra_coordinator',None)

    def test_main_has_no_model_role(self):
        self.assertNotIn('main', self.engine.catalog)
        with tempfile.TemporaryDirectory() as d:
            now=m.time.time()
            result=m.Workflow(d, self.engine.home).execute('activate',dict(objective='x',**authorization(now, 'x'),models={},slot_limit=4,inventory=[],observed_at=now))
            self.assertEqual(result, {'status':'active'})

    def test_astra_coordination_dispatch_read_only(self):
        with self.assertRaises(m.Error): self.task('bad',role='coordinator',writes=['a.txt'])
        self.task('plan',role='coordinator')
        args=self.call('prepare',id='plan')['arguments']
        self.assertEqual((args['model'],args['reasoning_effort'],args['fork_turns']),('gpt-6.1-sol','high','none'))
        for effort in ('low','medium','xhigh'):
            with self.assertRaises(m.Error):self.engine.settings('coordinator',effort)

    def test_work_waits_for_accepted_astra_plan(self):
        self.task('plan',role='coordinator')
        self.task('implementation',depends_on=['plan'])
        with self.assertRaises(m.Error):self.call('prepare',id='implementation')
        self.start('plan');self.inv('plan',status='done')
        self.call('finish',id='plan',accepted=True,evidence='bounded plan reviewed')
        self.assertEqual(self.call('prepare',id='implementation')['arguments']['model'],'gpt-6.1-sol')

    def test_watcher_uses_luna_directly(self):
        self.engine.catalog['limit_watcher'] = dict(name='limit_watcher', model='gpt-6-luna',
            model_reasoning_effort='low',developer_instructions='Observe usage.',
            workflow={'efforts':['low','medium']})
        both={'gpt-5.6-terra':['low'],'gpt-6-luna':['low','medium']}
        self.assertEqual(self.engine.settings('limit_watcher',None)['model'],'gpt-6-luna')
        only_luna={'gpt-6-luna':['low','medium']}
        state=m.read_json(self.engine.path); state['models']=both; m.write_json(self.engine.path,state)
        self.task(role='limit_watcher')
        self.assertEqual(self.call('prepare',id='work')['arguments']['model'],'gpt-6-luna')

    def test_unavailable_watcher_and_other_roles_do_not_fallback(self):
        self.engine.catalog['limit_watcher'] = dict(name='limit_watcher',model='gpt-6-luna',
            model_reasoning_effort='low',developer_instructions='Observe usage.',
            workflow={'efforts':['low','medium']})
        state=m.read_json(self.engine.path); state['models']={'gpt-5.6-terra':['low']}; m.write_json(self.engine.path,state)
        self.task(role='limit_watcher')
        with self.assertRaises(m.Error): self.call('prepare',id='work')
        self.assertEqual(self.engine.settings('researcher',None)['model'],'gpt-6.1-sol')

    def test_missing_model_no_fallback(self):
        state = m.read_json(self.engine.path)
        state['models'] = {}
        m.write_json(self.engine.path, state)
        self.task()
        with self.assertRaises(m.Error): self.call('prepare', id='work')

    def test_write_conflict_casefold_and_escape(self):
        self.task(writes=['src/a.py'])
        with self.assertRaises(m.Error): self.task('other', writes=['SRC/A.py'])
        with self.assertRaises(m.Error): self.task('escape', writes=['../a.py'])
        with self.assertRaises(m.Error): self.task('internal', writes=['.agent-workflow/state.json'])

    def test_stale_file_before_dispatch(self):
        self.task(writes=['a.txt'])
        (self.root / 'a.txt').write_text('user edit')
        with self.assertRaises(m.Error): self.call('prepare', id='work')

    def test_capacity_counts_pending(self):
        for key in ['a','b','c']:
            self.task(key)
            self.call('prepare', id=key)
        self.task('d')
        with self.assertRaises(m.Error): self.call('prepare', id='d')

    def test_stale_usage(self):
        self.task()
        self.now += 301
        self.app_goal()
        with self.assertRaisesRegex(m.Error, 'fresh usage above threshold required'): self.call('prepare', id='work')

    def test_threshold_and_unknown(self):
        self.assertEqual(self.usage(90)['action'], 'SAVE_AND_STOP')
        with self.assertRaises(m.Error): self.task()

    def test_unknown_retry_once(self):
        self.assertEqual(self.call('usage')['action'], 'RETRY_USAGE_ONCE')
        self.assertEqual(self.call('usage')['action'], 'SAVE_AND_STOP')

    def test_caution_serial(self):
        self.usage(80)
        self.task('a'); self.start('a'); self.task('b')
        with self.assertRaises(m.Error): self.call('prepare', id='b')

    def test_receipt_mismatch(self):
        self.task(); self.call('prepare', id='work')
        with self.assertRaises(m.Error): self.call('record', id='work', submitted_arguments={}, session_id='x', receipt={'id':'x'})

    def test_finish_requires_observed_idle(self):
        self.task(); self.start()
        with self.assertRaises(m.Error): self.call('finish', id='work', accepted=True, evidence='test')
        self.inv('work', status='idle')
        self.assertEqual(self.call('finish', id='work', accepted=True, evidence='test')['state'], 'accepted')

    def test_independent_review_required(self):
        self.task(independent_review=True); self.start(); self.inv('work', status='idle')
        with self.assertRaises(m.Error): self.call('finish', id='work', accepted=True, evidence='self checked')

    def test_uncertain_dispatch_blocks(self):
        self.task(); self.call('prepare',id='work')
        self.call('dispatch_failed', id='work', evidence='timeout', certain_not_started=False)
        with self.assertRaises(m.Error): self.task('next')
        self.inv()
        self.call('reconcile',id='work',evidence='fresh host inventory',no_owned_work_running=True)
        self.task('next')

    def test_snapshot_restore_and_tamper(self):
        (self.root / 'a.txt').write_text('saved')
        cp = self.snap(['a.txt','deleted.txt'])
        self.assertTrue(cp['restore_verified'])
        dest = Path(self.tmp.name) / 'restore'
        self.call('restore',destination=str(dest))
        self.assertEqual((dest/'files/a.txt').read_text(),'saved')
        self.assertFalse((dest/'files/deleted.txt').exists())
        source = self.engine.base / 'checkpoints' / cp['checkpoint']['id'] / 'files/a.txt'
        source.write_text('damaged')
        with self.assertRaises(m.Error): self.call('restore',destination=str(Path(self.tmp.name)/'another'))

    def test_halt_resume(self):
        self.call('stop'); self.snap()
        self.call('halt',inventory=[],observed_at=self.now,owned_processes_stopped=True,stop_evidence='observed empty')
        with self.assertRaises(m.Error): self.call('resume',**authorization(self.now, kind='explicit_goal_continuation', event_id='continue-1'))
        self.now += 1; self.usage(); self.inv()
        self.assertEqual(self.call('resume',**authorization(self.now, kind='explicit_goal_continuation', event_id='continue-1'))['status'],'active')

    def test_changed_file_blocks_verified_halt(self):
        self.call('stop'); self.snap(['a.txt'])
        (self.root/'a.txt').write_text('late writer')
        with self.assertRaises(m.Error): self.call('halt',inventory=[],observed_at=self.now,owned_processes_stopped=True,stop_evidence='observed empty')

    def test_lock_prevents_second_writer(self):
        (self.engine.base/'coordinator.lock').write_text('owner')
        with self.assertRaises(m.Error): self.call('status')

    def test_readonly_verifier(self):
        with self.assertRaises(m.Error): self.task(role='verifier',writes=['a.txt'])

    def test_restore_metadata_name_collisions(self):
        names = ['recovery-state.json', 'recovery-manifest.json', '.saved-state.json', 'state.json']
        for name in names: (self.root/name).write_text('project content: '+name)
        self.snap(names)
        dest=Path(self.tmp.name)/'restore'
        self.call('restore', destination=str(dest))
        for name in names:
            self.assertEqual((dest/'files'/name).read_bytes(), (self.root/name).read_bytes())

    def test_case_insensitive_internal_reservation(self):
        with self.assertRaises(m.Error): self.task(writes=['.AGENT-WORKFLOW/state.json'])

    def test_parent_child_write_claim_conflict(self):
        self.task(writes=['future'])
        with self.assertRaises(m.Error): self.task('other',writes=['future/child'])

    def test_windows_alias_paths(self):
        for name in ['x.', 'con.txt', 'folder /x', 'nul']:
            with self.assertRaises(m.Error): self.engine.file(name)

    def test_linked_state_rejected(self):
        outside=Path(self.tmp.name)/'outside.json'
        outside.write_text('{"sentinel": true}')
        self.engine.path.unlink()
        try:
            self.engine.path.symlink_to(outside)
        except OSError:
            self.skipTest('symlink creation unavailable')
        with self.assertRaises(m.Error): self.call('status')
        self.assertEqual(outside.read_text(),'{"sentinel": true}')

    def test_snapshot_retention_preserves_unknown_directories(self):
        ids=[]
        for _ in range(4): ids.append(self.snap()['checkpoint']['id'])
        parent=self.engine.base/'checkpoints'
        self.assertEqual({p.name for p in parent.iterdir()},set(ids[-2:]))
        unknown=parent/('f'*32)
        unknown.mkdir(); (unknown/'user.txt').write_text('keep')
        self.snap()
        self.assertTrue((unknown/'user.txt').exists())

    def test_default_state_namespace_leaves_old_helper_untouched(self):
        self.assertEqual(self.engine.base, self.root / '.agent-workflow-v1-0')
        retired = self.root / '.agent-workflow'
        retired.mkdir()
        (retired / 'state.json').write_text('retired inert evidence')
        before = (retired / 'state.json').read_bytes()
        self.call('status')
        self.snap()
        self.assertEqual((retired / 'state.json').read_bytes(), before)
        self.assertEqual({p.name for p in retired.iterdir()}, {'state.json'})

    def test_pruning_owns_only_v1_checkpoints(self):
        self.snap()
        checkpoints = self.engine.base / 'checkpoints'
        foreign = checkpoints / ('e' * 32)
        foreign.mkdir()
        m.write_json(foreign / 'owner.json', {'owner': 'agent-workflow', 'project': str(self.root)})
        (foreign / 'evidence.txt').write_text('preserve historical evidence')
        for _ in range(3):
            pointer = self.snap()['checkpoint']
            owner = m.read_json(checkpoints / pointer['id'] / 'owner.json')
            self.assertEqual(owner, {'owner': 'agent-workflow-v1-0', 'project': str(self.root)})
        self.assertTrue((foreign / 'evidence.txt').is_file())
        self.assertEqual(len([p for p in checkpoints.iterdir() if p != foreign]), 2)

    def test_failed_snapshot_preserves_previous_pointer(self):
        good=self.snap()['checkpoint']
        (self.root/'a.txt').write_text('before')
        original=m.shutil.copy2
        def changing(source, dest, *a, **kw):
            result=original(source,dest,*a,**kw)
            if Path(source)==self.root/'a.txt': Path(source).write_text('changed')
            return result
        with patch.object(m.shutil,'copy2',side_effect=changing):
            with self.assertRaises(m.Error): self.snap(['a.txt'])
        self.assertEqual(m.read_json(self.engine.path)['checkpoint'],good)

    def test_missing_usage_does_not_unhalt(self):
        self.call('stop'); self.snap()
        self.call('halt',inventory=[],observed_at=self.now,owned_processes_stopped=True,stop_evidence='observed empty')
        self.call('usage'); self.call('usage')
        self.assertEqual(self.call('status')['status'],'halted')

    def test_dependency_change_blocks_dispatch(self):
        self.task(writes=['a.txt']); self.start(); (self.root/'a.txt').write_text('done')
        self.inv('work',status='idle'); self.call('finish',id='work',accepted=True,evidence='checked')
        self.task('consumer',depends_on=['work'])
        (self.root/'a.txt').write_text('user edit')
        with self.assertRaises(m.Error): self.call('prepare',id='consumer')

    def test_review_target_stable_across_review(self):
        self.task(writes=['a.txt'],independent_review=True); self.start()
        (self.root/'a.txt').write_text('first')
        self.inv('work',status='idle')
        self.task('review',role='verifier',review_targets=[dict(producer_id='work',revision=m.read_json(self.engine.path)['tasks']['work']['revision'])])
        self.start('review')
        (self.root/'a.txt').write_text('late edit')
        self.inv('work','review',status='idle')
        with self.assertRaises(m.Error): self.call('finish',id='review',accepted=True,evidence='checked')

    def test_positive_independent_acceptance(self):
        self.task(writes=['a.txt'],independent_review=True); self.start()
        (self.root/'a.txt').write_text('first')
        self.inv('work',status='idle'); self.task('review',role='verifier',review_targets=[dict(producer_id='work',revision=m.read_json(self.engine.path)['tasks']['work']['revision'])])
        self.start('review'); self.inv('work','review',status='idle')
        self.call('finish',id='review',accepted=True,evidence='checked')
        self.assertEqual(self.call('finish',id='work',accepted=True,evidence='passed',review_task='review')['state'],'accepted')

    def test_cancel_releases_undispatched_claim(self):
        self.task(writes=['a.txt'])
        self.call('cancel',id='work',reason='user changed scope')
        self.task('new_work',writes=['a.txt'])


    def test_direct_status_cli_requires_pinned_candidate_entry(self):
        before=self.engine.path.read_bytes()
        result=subprocess.run([sys.executable,str(m.PACKAGE/'scripts/workflow.py'),'status','--project',str(self.root)],input='',text=True,capture_output=True)
        self.assertEqual(result.returncode,1,result.stdout)
        error=json.loads(result.stderr)
        self.assertIn('scripts/candidate.py',error['error'])
        self.assertEqual(self.engine.path.read_bytes(),before)


    def test_pruning_rejects_linked_checkpoint(self):
        self.snap()
        outside=Path(self.tmp.name)/'unowned'
        outside.mkdir(); (outside/'keep.txt').write_text('keep')
        link=self.engine.base/'checkpoints'/('e'*32)
        try: link.symlink_to(outside,target_is_directory=True)
        except OSError: self.skipTest('directory symlink creation unavailable')
        result=self.snap()
        self.assertIn('cleanup_warning',result)
        self.assertEqual((outside/'keep.txt').read_text(),'keep')

    def assert_invalid_unchanged(self, command, payload, code=None, field=None):
        before = self.engine.path.read_bytes()
        before_hash = m.digest(self.engine.path)
        with self.assertRaises(m.Error) as raised:
            self.engine.execute(command, payload)
        if code:
            self.assertEqual(raised.exception.code, code)
        if field:
            self.assertEqual(raised.exception.field, field)
        self.assertEqual(self.engine.path.read_bytes(), before)
        self.assertEqual(m.digest(self.engine.path), before_hash)
        self.assertFalse((self.engine.base/'coordinator.lock').exists())

    def flag_payloads(self):
        return [
            ('finish', 'accepted', dict(id='work', accepted=True, evidence='observed result'), True),
            ('task', 'independent_review', dict(id='work', role='implementer', objective='deliver', criteria=['saved'], independent_review=True), False),
            ('usage-due', 'substantial_work', dict(substantial_work=True), False),
            ('dispatch_failed', 'certain_not_started', dict(id='work', evidence='tool failure', certain_not_started=True), False),
            ('reconcile', 'no_owned_work_running', dict(id='work', evidence='observed empty', no_owned_work_running=True), True),
            ('snapshot', 'writers_quiescent', dict(writers_quiescent=True, next_action='continue'), True),
            ('halt', 'owned_processes_stopped', dict(inventory=[], observed_at=self.now, owned_processes_stopped=True, stop_evidence='observed empty'), True),
            ('halt', 'degraded', dict(inventory=[], observed_at=self.now, owned_processes_stopped=True, stop_evidence='observed empty', degraded=True, reason='copy failed', next_action='repair'), False),
        ]

    def test_every_critical_flag_rejects_nonbool_without_state_write(self):
        for command, flag, payload, _ in self.flag_payloads():
            for invalid in ('false', 'true', 0, 1, None, [], {}):
                with self.subTest(command=command, flag=flag, invalid=invalid):
                    bad = dict(payload); bad[flag] = invalid
                    self.assert_invalid_unchanged(command, bad, 'FIELD_TYPE', '$.'+flag)

    def test_flag_missing_policy_and_exact_bool_positive_table(self):
        for command, flag, payload, required in self.flag_payloads():
            for value in (True, False):
                with self.subTest(command=command, flag=flag, value=value):
                    valid = dict(payload); valid[flag] = value
                    m.validate_command(command, valid)
            omitted = dict(payload); del omitted[flag]
            if required:
                self.assert_invalid_unchanged(command, omitted, 'FIELD_REQUIRED', '$.'+flag)
            else:
                m.validate_command(command, omitted)

    def test_finish_true_false_are_distinct_terminal_decisions(self):
        for key, accepted in [('yes', True), ('no', False)]:
            self.task(key); self.start(key); self.inv(key, status='done')
            result = self.call('finish', id=key, accepted=accepted, evidence='real result shape')
            self.assertEqual(result['state'], 'accepted' if accepted else 'rejected')

    def test_safe_optional_flags_preserve_defaults_and_bool_results(self):
        for key, review in [('reviewed', True), ('plain', False)]:
            self.task(key, independent_review=review)
            self.assertIs(m.read_json(self.engine.path)['tasks'][key]['independent_review'], review)
        self.assertEqual(self.call('usage-due', substantial_work=False)['action'], 'REUSE_MEASUREMENT')
        self.assertEqual(self.call('usage-due', substantial_work=True)['action'], 'READ_USAGE')
        baseline = self.engine.path.read_bytes()
        for payload, expected in [({}, 'uncertain'), ({'certain_not_started': False}, 'uncertain'), ({'certain_not_started': True}, 'rejected')]:
            self.engine.path.write_bytes(baseline)
            self.task('failed'); self.call('prepare', id='failed')
            self.assertEqual(self.call('dispatch_failed', id='failed', evidence='timeout receipt', **payload)['state'], expected)

    def test_required_safety_flags_false_fail_closed_true_passes(self):
        self.task(); self.call('prepare', id='work')
        self.call('dispatch_failed', id='work', evidence='timeout')
        self.inv()
        reconcile = dict(id='work', evidence='observed empty', no_owned_work_running=False)
        self.assert_invalid_unchanged('reconcile', reconcile)
        reconcile['no_owned_work_running'] = True
        self.assertEqual(self.engine.execute('reconcile', reconcile)['state'], 'rejected')
        self.assert_invalid_unchanged('snapshot', dict(writers_quiescent=False, next_action='continue'))
        self.call('stop'); self.snap()
        baseline = self.engine.path.read_bytes()
        halted = dict(inventory=[], observed_at=self.now, owned_processes_stopped=False, stop_evidence='observed empty')
        self.assert_invalid_unchanged('halt', halted)
        halted['owned_processes_stopped'] = True
        for degraded in (False, True):
            self.engine.path.write_bytes(baseline)
            result = self.engine.execute('halt', dict(halted, degraded=degraded, reason='copy failure', next_action='repair'))
            self.assertIs(result['recovery_verified'], not degraded)
            state = m.read_json(self.engine.path)
            self.assertEqual('recovery_error' in state, degraded)

    def test_all_commands_have_unknown_field_and_object_guards(self):
        for command in m.COMMAND_FIELDS:
            with self.subTest(command=command):
                self.assert_invalid_unchanged(command, {'typo_field': True}, 'FIELD_UNKNOWN', '$.typo_field')
                for bad in (None, [], True, 'payload', 1):
                    self.assert_invalid_unchanged(command, bad, 'FIELD_TYPE', '$')
        self.assert_invalid_unchanged('typo-command', {}, 'COMMAND_UNKNOWN', '$.command')

    def test_representative_required_and_nested_fields(self):
        cases = [
            ('task', dict(id='work', role='implementer', objective='deliver'), 'FIELD_REQUIRED', '$.criteria'),
            ('task', dict(id=1, role='implementer', objective='deliver', criteria=['ok']), 'FIELD_TYPE', '$.id'),
            ('task', dict(id='Work', role='implementer', objective='deliver', criteria=['ok']), 'FIELD_VALUE', '$.id'),
            ('task', dict(id='work', role='implementer', objective='deliver', criteria=[]), 'FIELD_VALUE', '$.criteria'),
            ('task', dict(id='work', role='implementer', objective='deliver', criteria=['ok'], writes='a.txt'), 'FIELD_TYPE', '$.writes'),
            ('task', dict(id='work', role='implementer', objective='deliver', criteria=['ok'], context_refs=[{}]), 'FIELD_TYPE', '$.context_refs[0]'),
            ('prepare', dict(id='work', effort=False), 'FIELD_TYPE', '$.effort'),
            ('record', dict(id='work', submitted_arguments={'modle':'x'}, session_id='work', receipt={'id':'work'}), 'FIELD_UNKNOWN', '$.submitted_arguments.modle'),
            ('record', dict(id='work', submitted_arguments={}, session_id=[], receipt={'id':'work'}), 'FIELD_TYPE', '$.session_id'),
            ('record', dict(id='work', submitted_arguments={}, session_id='work', receipt=True), 'FIELD_TYPE', '$.receipt'),
            ('inventory', dict(observed_at=self.now, inventory=[dict(id='x', status=True)]), 'FIELD_TYPE', '$.inventory[0].status'),
            ('inventory', dict(observed_at=self.now, inventory=[dict(id='x', status='idle', extra=1)]), 'FIELD_UNKNOWN', '$.inventory[0].extra'),
            ('restore', dict(destination=True), 'FIELD_TYPE', '$.destination'),
            ('snapshot', dict(writers_quiescent=True, next_action='save', files=[None]), 'FIELD_TYPE', '$.files[0]'),
        ]
        for command, payload, code, field in cases:
            with self.subTest(command=command, field=field):
                self.assert_invalid_unchanged(command, payload, code, field)
        payload = dict(objective='test', **authorization(self.now), models={}, slot_limit=4, inventory=[], observed_at=self.now)
        for container, key in [('authorization', 'source_typo'), ('source_message', 'source_typo'), ('app_goal_observation', 'source_typo')]:
            bad=copy.deepcopy(payload); bad[container][key]='x'
            self.assert_invalid_unchanged('activate', bad, 'FIELD_UNKNOWN', '$.'+container+'.'+key)
        bad=copy.deepcopy(payload); bad['authorization']['scope']['id_typo']='x'
        self.assert_invalid_unchanged('activate', bad, 'FIELD_UNKNOWN', '$.authorization.scope.id_typo')

    def test_slot_count_types_and_boundaries(self):
        payload = dict(objective='test', **authorization(self.now), models={}, slot_limit=4, inventory=[], observed_at=self.now)
        for flag in ('slot_limit', 'session_limit'):
            for invalid in (True, False, '4', 1.0, 0, -1, [], {}):
                with self.subTest(field=flag, invalid=invalid):
                    bad=dict(payload); bad[flag]=invalid
                    self.assert_invalid_unchanged('activate', bad, 'FIELD_VALUE' if type(invalid) is int else 'FIELD_TYPE', '$.'+flag)
            for invalid in (float('nan'), float('inf'), float('-inf')):
                bad=dict(payload); bad[flag]=invalid
                self.assert_invalid_unchanged('activate', bad, 'JSON_NONFINITE', '$.'+flag)
            for valid in (1, 4, 100):
                m.validate_command('activate', dict(payload, **{flag:valid}))
        self.assert_invalid_unchanged('activate', dict(payload, slot_limit=None), 'FIELD_TYPE', '$.slot_limit')
        m.validate_command('activate', dict(payload, session_limit=None))
        with tempfile.TemporaryDirectory() as directory:
            engine=m.Workflow(directory, self.engine.home, lambda: self.now)
            engine.execute('activate', dict(payload, slot_limit=1, session_limit=1))
            self.assertEqual(m.read_json(engine.path)['slot_limit'], 1)

    def test_timestamps_exclude_bool_negative_and_nonfinite(self):
        for invalid in (True, False, '10000', None, [], {}, -1, float('nan'), float('inf')):
            with self.subTest(invalid=invalid):
                code='JSON_NONFINITE' if type(invalid) is float else 'FIELD_VALUE' if type(invalid) is int else 'FIELD_TYPE'
                self.assert_invalid_unchanged('inventory', dict(inventory=[], observed_at=invalid), code, '$.observed_at')
        for observed in (self.now, float(self.now), self.now-60):
            self.call('inventory', inventory=[], observed_at=observed)
        self.assert_invalid_unchanged('inventory', dict(inventory=[], observed_at=self.now-61))
        self.assert_invalid_unchanged('inventory', dict(inventory=[], observed_at=self.now+1))
        self.assert_invalid_unchanged('inventory', dict(inventory=[], observed_at=10**1000), 'FIELD_VALUE', '$.observed_at')

    def test_malformed_usage_is_not_unknown_measurement(self):
        cases=[('rateLimits', []), ('rateLimits', 'unknown'), ('rateLimits', True),
               ('primary', []), ('primary', 'unknown'), ('primary', False),
               ('usedPercent', '10'), ('usedPercent', True), ('usedPercent', []), ('usedPercent', {}),
               ('usedPercent', -1), ('usedPercent', 101), ('usedPercent', float('nan')), ('usedPercent', float('inf')),
               ('windowDurationMins', True), ('windowDurationMins', 300.0), ('windowDurationMins', 0),
               ('resetsAt', True), ('resetsAt', -1), ('resetsAt', float('inf'))]
        for field, value in cases:
            payload=dict(observed_at=self.now, rateLimits=dict(primary=dict(usedPercent=10, windowDurationMins=300), secondary=dict(usedPercent=20, windowDurationMins=10080)))
            if field == 'rateLimits': payload[field]=value; path='$.rateLimits'
            elif field == 'primary': payload['rateLimits'][field]=value; path='$.rateLimits.primary'
            else: payload['rateLimits']['primary'][field]=value; path='$.rateLimits.primary.'+field
            with self.subTest(field=field, value=value):
                self.assert_invalid_unchanged('usage', payload, field=path)
        self.assert_invalid_unchanged('usage', dict(observed_at=True), 'FIELD_TYPE', '$.observed_at')
        self.assertEqual(self.call('usage')['action'], 'RETRY_USAGE_ONCE')

    def test_unknown_raw_usage_keeps_retry_save_lifecycle(self):
        baseline=self.engine.path.read_bytes()
        samples=[{}, {'rateLimits':None}, {'rateLimits':{}}, {'rateLimits':{'primary':None}},
                 {'observed_at':self.now, 'rateLimits':{'primary':{'usedPercent':None, 'windowDurationMins':300}}},
                 {'observed_at':self.now-301}, {'observed_at':self.now+1}]
        for payload in samples:
            with self.subTest(payload=payload):
                self.engine.path.write_bytes(baseline)
                self.assertEqual(self.engine.execute('usage', payload)['action'], 'RETRY_USAGE_ONCE')
                self.assertEqual(self.engine.execute('usage', payload)['action'], 'SAVE_AND_STOP')
                self.assertEqual(m.read_json(self.engine.path)['status'], 'stopping')

    def test_usage_numeric_boundaries_and_raw_metadata(self):
        for used in (0, 0.0, 100, 100.0):
            result=self.call('usage', observed_at=self.now, rateLimits={
                'limitId':'codex', 'credits':{'hasCredits':True},
                'primary':{'usedPercent':used, 'windowDurationMins':300, 'resetsAt':0, 'providerMetadata':{'future':True}},
                'secondary':{'usedPercent':0, 'windowDurationMins':10080}})
            self.assertEqual(result['remaining'], 100-used)

    def test_direct_apply_is_transactional_and_validates_before_mutation(self):
        state=m.read_json(self.engine.path); before=copy.deepcopy(state)
        with self.assertRaises(m.Error): self.engine.apply(state, 'finish', dict(id='missing', accepted='false', evidence='receipt'))
        self.assertEqual(state,before)
        state['status']='stopping'; before=copy.deepcopy(state)
        with self.assertRaises(m.Error): self.engine.apply(state, 'halt', dict(inventory=[], observed_at=self.now, owned_processes_stopped=False, stop_evidence='receipt'))
        self.assertEqual(state,before)

    def test_strict_parser_duplicate_and_nonfinite_paths(self):
        cases=[('{"accepted":true,"accepted":false}', 'JSON_DUPLICATE_KEY', '$.accepted'),
               ('{"scope":{"id":"a","id":"b"}}', 'JSON_DUPLICATE_KEY', '$.scope.id'),
               ('{"items":[{"usedPercent":NaN}]}', 'JSON_NONFINITE', '$.items[0].usedPercent'),
               ('{"at":Infinity}', 'JSON_NONFINITE', '$.at'),
               ('{"at":-Infinity}', 'JSON_NONFINITE', '$.at'),
               ('{"at":1e999}', 'JSON_NONFINITE', '$.at'),
               ('{"at":}', 'JSON_SYNTAX', '$')]
        for raw, code, field in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(m.Error) as raised: m.strict_json(raw)
                self.assertEqual((raised.exception.code, raised.exception.field),(code,field))
        self.assertEqual(m.strict_json('{"accepted":false,"nested":[{},[]]}'), {'accepted':False,'nested':[{},[]]})

    def test_direct_cli_inputs_refused_and_corrupt_state_unchanged(self):
        # Supported pinned-entry JSON coverage lives in candidate subprocess
        # tests. The old direct entry must refuse even malformed inputs before
        # reading or rewriting the stored state.
        command=[sys.executable, str(m.PACKAGE/'scripts/workflow.py'), 'status', '--project', str(self.root), '--codex-home', str(self.engine.home)]
        baseline=self.engine.path.read_bytes()
        for raw in ('{"typo":1}', '{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', '[]'):
            for source in ('stdin','file'):
                with self.subTest(raw=raw, source=source):
                    args=list(command)
                    if source=='file':
                        fixture=Path(self.tmp.name)/'input.json'; fixture.write_text(raw,encoding='utf-8-sig'); args += ['--input',str(fixture)]
                    result=subprocess.run(args,input=raw if source=='stdin' else '',text=True,capture_output=True)
                    self.assertEqual(result.returncode,1,result.stdout)
                    error=json.loads(result.stderr)
                    self.assertIn('code',error); self.assertIn('field',error)
                    self.assertIn('scripts/candidate.py',error['error'])
                    self.assertEqual(self.engine.path.read_bytes(),baseline)
        for raw in ('{"status":"active","status":"halted"}', '{"status":"active","at":Infinity}', '{"at":1e999}'):
            self.engine.path.write_text(raw)
            before=self.engine.path.read_bytes(); before_hash=m.digest(self.engine.path)
            with self.assertRaises(m.Error): self.call('status')
            self.assertEqual(self.engine.path.read_bytes(),before)
            self.assertEqual(m.digest(self.engine.path),before_hash)
        self.engine.path.write_bytes(baseline)

    def test_write_json_nonfinite_rejected_before_filesystem_mutation(self):
        original=self.engine.path.read_bytes()
        for invalid in (float('nan'), float('inf'), float('-inf')):
            with self.assertRaises(m.Error) as raised: m.write_json(self.engine.path, {'nested':[invalid]})
            self.assertEqual((raised.exception.code,raised.exception.field),('JSON_NONFINITE','$.nested[0]'))
            self.assertEqual(self.engine.path.read_bytes(),original)
            absent=Path(self.tmp.name)/'absent'/'value.json'
            with self.assertRaises(m.Error): m.write_json(absent, {'at':invalid})
            self.assertFalse(absent.parent.exists())

    def test_invalid_command_does_not_create_state_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=m.Workflow(directory,self.engine.home)
            with self.assertRaises(m.Error): engine.execute('activate',{'slot_limit':True})
            self.assertFalse(engine.base.exists())

    def test_direct_cli_options_are_refused_before_mutation(self):
        before=self.engine.path.read_bytes()
        script=str(m.PACKAGE/'scripts/workflow.py')
        cases=[('status',['--update']),
               ('status',['--output',str(self.root/'unexpected.txt')]),
               ('native',['--input','ignored.json']),
               ('export',[]),
               ('native',['--output',str(self.root/'unexpected.toml')])]
        for command, options in cases:
            result=subprocess.run([sys.executable,script,command,'--project',str(self.root),*options],input='',text=True,capture_output=True)
            self.assertEqual(result.returncode,1,result.stdout)
            error=json.loads(result.stderr)
            self.assertIn('scripts/candidate.py',error['error'])
            self.assertIn('code',error); self.assertIn('field',error)
            self.assertEqual(self.engine.path.read_bytes(),before)
        self.assertFalse((self.root/'unexpected.txt').exists())
        self.assertFalse((self.root/'unexpected.toml').exists())


if __name__ == '__main__': unittest.main()
