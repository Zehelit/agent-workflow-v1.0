"""Late receipt, uncertainty, and dispatch gate counterexamples (no native calls)."""
import copy
import unittest
import test_workflow as fixtures

m = fixtures.m


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        fixtures.WorkflowTests.setUp(self)
        self.inv()
    call = fixtures.WorkflowTests.call
    task = fixtures.WorkflowTests.task
    usage = fixtures.WorkflowTests.usage
    def inv(self, *keys, status='running'):
        items = [dict(id=key, status=status) for key in keys]
        native_status = {'completed':'synthetic fixture final'} if status == 'done' else 'interrupted' if status == 'stopped' else status
        return self.call('inventory', inventory=items, observed_at=self.now,
                         observation=dict(source_kind='collaboration.list_agents', source_id='test-fixture-native-observation',
                                          receipt={'agents':[dict(agent_name=key, agent_status=native_status) for key in keys]}))

    def pending(self):
        self.task(writes=['output.txt'])
        prepared = self.call('prepare', id='work')
        self.call('dispatch-start', id='work', intent_id=prepared['intent_id'])
        return prepared['arguments']

    def success(self, args, **changes):
        payload = dict(id='work', evidence='actual late receipt observed', no_owned_work_running=False,
                       session_id='/root/work', submitted_arguments=args,
                       receipt={'agent_id': 'native-agent-uuid', 'task_name': '/root/work'})
        payload.update(changes)
        return self.engine.execute('reconcile', payload)

    def unchanged_failure(self, callback):
        before = self.engine.path.read_bytes()
        with self.assertRaises(m.Error): callback()
        self.assertEqual(before, self.engine.path.read_bytes())

    def test_timeout_late_success_restart_never_duplicate(self):
        self.task(writes=['output.txt'])
        args = self.call('prepare', id='work')['arguments']
        self.call('dispatch_failed', id='work', evidence='native timeout')
        self.engine = m.Workflow(self.root, self.engine.home, lambda: self.now)
        self.unchanged_failure(lambda: self.call('prepare', id='work'))
        self.unchanged_failure(lambda: self.task('other'))
        self.inv('/root/work')
        self.assertEqual(self.success(args)['state'], 'running')
        self.unchanged_failure(lambda: self.success(args))
        self.inv('/root/work', status='done')
        self.assertEqual(self.call('finish', id='work', accepted=True, evidence='actual observed result')['state'], 'accepted')

    def test_native_boundary_rechecks_capacity_after_inventory_grows(self):
        self.task()
        prepared = self.call('prepare', id='work')
        self.inv('/root/foreign-a', '/root/foreign-b', '/root/foreign-c')
        self.unchanged_failure(lambda: self.call('dispatch-start', id='work', intent_id=prepared['intent_id']))
        self.assertEqual(m.read_json(self.engine.path)['tasks']['work']['state'], 'prepared')
        # After one actual observed slot is free, the own reservation is not
        # counted twice: two running children plus this dispatch are allowed.
        self.inv('/root/foreign-a', '/root/foreign-b')
        self.assertEqual(self.call('dispatch-start', id='work', intent_id=prepared['intent_id'])['state'], 'uncertain')

    def test_native_boundary_rechecks_session_limit_and_other_reservations(self):
        self.task()
        prepared = self.call('prepare', id='work')
        state = m.read_json(self.engine.path)
        state['session_limit'] = 1
        m.write_json(self.engine.path, state)
        self.inv('/root/idle-child', status='idle')
        self.unchanged_failure(lambda: self.call('dispatch-start', id='work', intent_id=prepared['intent_id']))
        self.inv()
        self.assertEqual(self.call('dispatch-start', id='work', intent_id=prepared['intent_id'])['state'], 'uncertain')
        self.setUp()
        self.task()
        prepared = self.call('prepare', id='work')
        self.task('other')
        self.call('prepare', id='other')
        self.inv('/root/foreign-a', '/root/foreign-b')
        self.unchanged_failure(lambda: self.call('dispatch-start', id='work', intent_id=prepared['intent_id']))
        self.inv()
        self.call('dispatch_failed', id='other', evidence='fixture never invoked native spawn', certain_not_started=True)
        self.assertEqual(self.call('dispatch-start', id='work', intent_id=prepared['intent_id'])['state'], 'uncertain')

    def test_dispatch_start_failure_evidence_preserved_without_retry(self):
        args = self.pending()
        self.assertEqual(self.call('dispatch_failed', id='work', evidence='actual native timeout at new boundary')['state'], 'uncertain')
        task = m.read_json(self.engine.path)['tasks']['work']
        self.assertEqual(task['dispatch_failure'], 'actual native timeout at new boundary')
        self.assertEqual(task['prepared'], args)
        self.engine = m.Workflow(self.root, self.engine.home, lambda: self.now)
        self.unchanged_failure(lambda: self.call('prepare', id='work'))
        self.unchanged_failure(lambda: self.task('other'))
        self.inv('/root/work')
        self.assertEqual(self.success(args)['state'], 'running')

    def test_dispatch_start_certain_failure_releases_owned_claim(self):
        self.pending()
        self.assertEqual(self.call('dispatch_failed', id='work', evidence='actual tool rejected before starting', certain_not_started=True)['state'], 'rejected')
        self.assertNotIn('prepared', m.read_json(self.engine.path)['tasks']['work'])
        self.task('replacement', writes=['output.txt'])
        self.unchanged_failure(lambda: self.call('dispatch-start', id='work', intent_id='old'))

    def test_restart_missing_receipt_missing_identity_or_session(self):
        args = self.pending()
        self.engine = m.Workflow(self.root, self.engine.home, lambda: self.now)
        self.inv('/root/work')
        for changes in ({'receipt': {}}, {'receipt': {'id': 'wrong'}}, {'receipt': 'opaque'}, {'session_id': '/root/other'}, {'submitted_arguments': {}}):
            self.unchanged_failure(lambda: self.success(args, **changes))
        self.assertEqual(m.read_json(self.engine.path)['tasks']['work']['state'], 'uncertain')

    def test_stale_future_duplicate_malformed_inventory(self):
        args = self.pending()
        for payload in (dict(inventory=[{'id': '/root/work', 'status': 'running'}], observed_at=self.now-61),
                        dict(inventory=[{'id': '/root/work', 'status': 'running'}], observed_at=self.now+1),
                        dict(inventory=[{'id': '/root/work', 'status': 'invented'}], observed_at=self.now),
                        dict(inventory=[{'id': '/root/work', 'status': 'running'}]*2, observed_at=self.now),
                        dict(inventory=[{'id': '/root/work', 'status': 'running', 'fake': True}], observed_at=self.now)):
            self.unchanged_failure(lambda: self.engine.execute('inventory', payload))
        self.inv('/root/work'); self.now += 61
        self.unchanged_failure(lambda: self.success(args))

    def test_missing_or_wrong_native_observation_rejected(self):
        args = self.pending()
        fixtures.WorkflowTests.inv(self, '/root/work')
        self.unchanged_failure(lambda: self.success(args))
        self.unchanged_failure(lambda: self.call('inventory', inventory=[dict(id='/root/work', status='running')], observed_at=self.now,
                                               observation=dict(source_kind='invented', source_id='fake', receipt={'id':'fake'})))

    def test_dispatch_start_requires_native_observation_and_usage(self):
        self.task(); prepared = self.call('prepare', id='work')
        fixtures.WorkflowTests.inv(self)
        self.unchanged_failure(lambda: self.call('dispatch-start', id='work', intent_id=prepared['intent_id']))
        self.inv(); self.usage(90)
        self.unchanged_failure(lambda: self.call('dispatch-start', id='work', intent_id=prepared['intent_id']))

    def test_fresh_unknown_or_absent_inventory_is_not_success(self):
        args = self.pending()
        self.inv()
        self.unchanged_failure(lambda: self.success(args))
        self.unchanged_failure(lambda: self.inv('/root/work', status='unknown'))

    def test_stop_and_save_block_dispatch_but_late_receipt_preserved(self):
        self.task()
        prepared = self.call('prepare', id='work')
        self.call('stop')
        self.unchanged_failure(lambda: self.call('dispatch-start', id='work', intent_id=prepared['intent_id']))
        self.setUp(); args = self.pending(); self.call('stop'); self.inv('/root/work')
        self.assertEqual(self.success(args)['run_status'], 'stopping')

    def test_writer_overlap_and_input_change_at_native_boundary(self):
        self.task(writes=['output.txt'])
        self.unchanged_failure(lambda: self.task('overlap', writes=['OUTPUT.txt']))
        prepared = self.call('prepare', id='work')
        (self.root/'output.txt').write_text('user edit')
        self.unchanged_failure(lambda: self.call('dispatch-start', id='work', intent_id=prepared['intent_id']))
        self.setUp(); (self.root/'input.txt').write_text('one')
        self.task(file_inputs=[dict(path='input.txt', revision='r1')])
        prepared = self.call('prepare', id='work')
        (self.root/'input.txt').write_text('two')
        with self.assertRaises(m.InputInvalidation): self.call('dispatch-start', id='work', intent_id=prepared['intent_id'])
        self.assertTrue(m.read_json(self.engine.path)['tasks']['work']['contract_invalid'])

    def test_dependency_output_changed_between_prepare_and_native_boundary(self):
        self.task('producer', writes=['output.txt'])
        args = self.call('prepare', id='producer')['arguments']
        self.call('record', id='producer', session_id='producer', submitted_arguments=args, receipt={'id':'producer'})
        (self.root/'output.txt').write_text('first result')
        self.inv('producer', status='done')
        self.call('finish', id='producer', accepted=True, evidence='checked first result')
        self.task('consumer', depends_on=['producer'])
        prepared = self.call('prepare', id='consumer')
        (self.root/'output.txt').write_text('edited later')
        with self.assertRaises(m.InputInvalidation): self.call('dispatch-start', id='consumer', intent_id=prepared['intent_id'])
        self.assertEqual(m.read_json(self.engine.path)['tasks']['consumer']['state'], 'prepared')


    def test_raw_inventory_projection_rejects_fake_mismatched_duplicate_ambiguous(self):
        self.pending()
        raw_variants = [{'agents':[dict(agent_name='/root/other', agent_status='running')]},
                        {'agents':[dict(agent_name='/root/work', agent_status='idle')]},
                        {'agents':[dict(agent_name='/root/work', agent_status='running')]*2},
                        {'agents':[dict(agent_name='/root/work', agent_status={'completed':'fixture', 'running':True})]},
                        {'agents':[dict(agent_name='/root/work', agent_status={'completed':123})]},
                        {'agents':[dict(agent_name='/root/work', agent_status='unknown')]},
                        {'agents':[dict(agent_name='/root/work', agent_status='running', fake=True)]}]
        for raw in raw_variants:
            self.unchanged_failure(lambda: self.call('inventory', inventory=[dict(id='/root/work', status='running')], observed_at=self.now,
                                                    observation=dict(source_kind='collaboration.list_agents', source_id='fixture-correlation', receipt=raw)))

    def test_pinned_inventory_evidence_once_and_changed_evidence_rejects(self):
        import json
        path = self.root/'native-inventory.json'
        path.write_text(json.dumps({'agents':[]}), encoding='utf-8')
        observation = dict(source_kind='collaboration.list_agents', source_id='caller-capture-only', evidence_path='native-inventory.json', sha256=m.digest(path))
        self.call('inventory', inventory=[], observed_at=self.now, observation=observation)
        self.task(); prepared = self.call('prepare', id='work')
        path.write_text(json.dumps({'agents':[dict(agent_name='unexpected', agent_status='running')]}), encoding='utf-8')
        self.unchanged_failure(lambda: self.call('dispatch-start', id='work', intent_id=prepared['intent_id']))
        self.assertNotIn('receipt', m.read_json(self.engine.path)['inventory']['observation'])


if __name__ == '__main__': unittest.main()
