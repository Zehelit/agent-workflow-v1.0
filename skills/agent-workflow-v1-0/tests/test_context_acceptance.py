"""N06/N07 adversarial contracts, using disposable source-engine fixtures."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
import test_workflow as fixtures

m = fixtures.m


class ContextAcceptanceTests(unittest.TestCase):
    def setUp(self):
        fixtures.WorkflowTests.setUp(self)
        self.manifest = self.root / 'references.json'
        m.write_json(self.manifest, [])

    call = fixtures.WorkflowTests.call
    def task(self, key='work', **kwargs):
        inputs = list(kwargs.pop('file_inputs', []))
        inputs.append(dict(path='references.json', revision='refs-r1'))
        return fixtures.WorkflowTests.task(self, key, file_inputs=inputs, **kwargs)
    usage = fixtures.WorkflowTests.usage
    inv = fixtures.WorkflowTests.inv
    start = fixtures.WorkflowTests.start
    app_goal = fixtures.WorkflowTests.app_goal

    def state(self): return m.read_json(self.engine.path)

    def render(self, refs=None):
        before = self.engine.path.read_bytes()
        result = self.engine.render_context(self.state(), refs or [], self.now)
        self.assertEqual(before, self.engine.path.read_bytes())
        return result

    def summary(self, refs=None):
        path = self.root / 'current-context.json'
        m.write_json(path, self.render(refs))
        return dict(path=path.name, sha256=m.digest(path), manifest_path='references.json', manifest_sha256=m.digest(self.manifest))

    def rewrite(self, summary):
        path = self.root / 'current-context.json'
        m.write_json(path, summary)
        return dict(path=path.name, sha256=m.digest(path), manifest_path='references.json', manifest_sha256=m.digest(self.manifest))

    def reject(self, command, **payload):
        before = self.engine.path.read_bytes()
        with self.assertRaises(m.Error): self.call(command, **payload)
        self.assertEqual(before, self.engine.path.read_bytes())

    def producer(self):
        self.task(writes=['output.txt'], revision='r1', independent_review=True)
        self.start(); (self.root / 'output.txt').write_text('result')
        self.inv('work', status='done')

    def reviewer(self):
        self.task('review', role='verifier', review_targets=[dict(producer_id='work', revision='r1')])
        self.start('review'); self.inv('work', 'review', status='done')
        self.call('finish', id='review', accepted=True, evidence='review-report.md')

    def test_renderer_reconstructs_pinned_open_state_without_bodies(self):
        (self.root / 'input.txt').write_text('large report and tool log must stay external')
        self.task(file_inputs=[dict(path='input.txt', revision='source-r1')], context_refs=['input.txt'])
        refs = [dict(path='input.txt', sha256=m.digest(self.root / 'input.txt'), kind='decision')]
        result = self.render(refs)
        self.assertEqual(result, self.render(refs))
        self.assertEqual(result['authority'], self.state()['authorization'])
        self.assertEqual(result['open_tasks'][0]['file_inputs'], self.state()['tasks']['work']['file_inputs'])
        self.assertEqual(result['blockers'], [])
        self.assertNotIn('large report and tool log', json.dumps(result))
        self.assertFalse(result['dispatch_authorized'])
        self.assertEqual((result['cache_status'], result['token_measurement']), ('unknown', 'pending_N08'))
        self.assertEqual(result['state_sha256'], m.state_pin(self.state()))

    def test_renderer_cli_is_read_only_and_json_only_in_both_interpreters(self):
        self.task()
        before = self.engine.path.read_bytes()
        source = Path(__file__).resolve().parents[3] / 'tooling/render-active-run.py'
        argv = [sys.executable, '-X', 'utf8', '-B'] + (['-O'] if sys.flags.optimize else []) + [str(source),
                '--project', str(self.root), '--codex-home', str(self.engine.home),
                '--references', str(self.manifest), '--as-of', str(self.now)]
        result = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = m.strict_json(result.stdout)
        self.assertEqual(rendered['authority'], self.state()['authorization'])
        self.assertEqual(rendered['blockers'], [])
        self.assertEqual(before, self.engine.path.read_bytes())

    def test_unsupported_native_observation_becomes_blocker_and_unknown_execution(self):
        self.task(); self.start(); self.inv('work', status='done')
        state = self.state()
        state['inventory']['observation'] = dict(source_kind='collaboration.list_agents', source_id='bad',
                                              receipt={'agents': []})
        result = self.engine.render_context(state, [], self.now)
        self.assertIn('NATIVE_INVENTORY_UNSUPPORTED', {item['code'] for item in result['blockers']})
        self.assertEqual(result['task_observations']['tasks']['work']['execution']['state'], 'unknown')

    def test_stale_quote_relabel_and_blocker_omission_are_rejected(self):
        self.task()
        result = self.render()
        result['authority']['source_created_at'] = self.now + 1
        self.reject('prepare', id='work', current_context=self.rewrite(result))
        self.now += 61
        result = self.render()
        self.assertTrue(result['blockers'])
        result['blockers'] = []
        self.reject('prepare', id='work', current_context=self.rewrite(result))

    def test_missing_pin_and_counterevidence_cannot_disappear(self):
        self.task(context_refs=['missing-report.md'])
        refs = [dict(path='missing-report.md', sha256='0'*64, kind='counterevidence', task_id='work')]
        result = self.render(refs)
        codes = {item['code'] for item in result['blockers']}
        self.assertTrue({'COUNTEREVIDENCE', 'EVIDENCE_MISSING'} <= codes)
        result['evidence_references'] = []
        result['blockers'] = []
        self.reject('prepare', id='work', current_context=self.rewrite(result))

    def test_valid_counterevidence_cannot_be_omitted_from_repinned_summary(self):
        (self.root / 'rejection.md').write_text('known blocker; full report stays external')
        refs = [dict(path='rejection.md', sha256=m.digest(self.root / 'rejection.md'), kind='counterevidence')]
        m.write_json(self.manifest, refs)
        self.task()
        result = self.render(refs)
        self.assertIn('COUNTEREVIDENCE', {item['code'] for item in result['blockers']})
        result['evidence_references'] = []; result['blockers'] = []
        self.reject('prepare', id='work', current_context=self.rewrite(result))

    def test_manifest_cannot_be_replaced_to_launder_missing_evidence(self):
        self.task()
        pin = self.summary()
        m.write_json(self.manifest, [dict(path='absent.md', sha256='0'*64, kind='evidence')])
        pin['manifest_sha256'] = m.digest(self.manifest)
        with self.assertRaises(m.InputInvalidation): self.call('prepare', id='work', current_context=pin)
        self.assertTrue(self.state()['tasks']['work']['contract_invalid'])

    def test_pin_changed_after_render_blocks_dispatch(self):
        (self.root / 'source.md').write_text('original')
        refs = [dict(path='source.md', sha256=m.digest(self.root / 'source.md'), kind='decision')]
        m.write_json(self.manifest, refs)
        self.task(context_refs=['source.md'])
        pin = self.summary(refs)
        (self.root / 'source.md').write_text('changed')
        self.reject('prepare', id='work', current_context=pin)
        self.assertIn('EVIDENCE_STALE', {item['code'] for item in self.render(refs)['blockers']})

    def test_missing_dependency_is_invalid_source_and_pending_dependency_blocks(self):
        self.task('plan', role='coordinator')
        self.task(depends_on=['plan'])
        self.assertIn('DEPENDENCY_NOT_ACCEPTED', {item['code'] for item in self.render()['blockers']})
        state = self.state(); state['tasks']['work']['depends_on'] = ['absent']
        with self.assertRaises(m.Error): self.engine.render_context(state, [], self.now)

    def test_summary_never_replaces_native_guard_and_requires_fresh_dispatch_render(self):
        self.task()
        # The native observation is deliberately preserved before rendering.
        self.call('inventory', observed_at=self.now, inventory=[], observation=dict(
            source_kind='collaboration.list_agents', source_id='fixture', receipt={'agents': []}))
        old = self.summary()
        prepared = self.call('prepare', id='work', current_context=old)
        self.reject('dispatch-start', id='work', intent_id=prepared['intent_id'])
        self.reject('dispatch-start', id='work', intent_id=prepared['intent_id'], current_context=old)
        fresh = self.summary()
        self.assertEqual(self.call('dispatch-start', id='work', intent_id=prepared['intent_id'], current_context=fresh)['state'], 'uncertain')

    def test_summary_integrity_and_strict_nested_pin_types(self):
        self.task()
        pin = self.summary()
        (self.root / pin['path']).write_text('{}')
        self.reject('prepare', id='work', current_context=pin)
        for bad in (True, [], dict(path='current-context.json', sha256=False), dict(path='x', sha256='0'*64, extra=1)):
            self.reject('prepare', id='work', current_context=bad)

    def test_historical_stop_survives_new_continuation_without_overriding_it(self):
        self.call('stop'); self.call('snapshot', writers_quiescent=True, next_action='continue')
        self.call('halt', inventory=[], observed_at=self.now, owned_processes_stopped=True, stop_evidence='stopped')
        self.now += 1; self.usage(); self.inv()
        self.call('resume', **fixtures.authorization(self.now, kind='explicit_goal_continuation', event_id='new'))
        self.task()
        result = self.render()
        self.assertEqual(result['authority']['source_created_at'], self.now)
        self.assertEqual(result['authority']['observed_at'], self.now)
        self.assertNotIn('RUN_NOT_ACTIVE', {item['code'] for item in result['blockers']})

    def test_done_frees_execution_capacity_but_retains_writer_and_pending_acceptance(self):
        self.producer()
        observation = self.call('status')['task_observations']['tasks']['work']
        self.assertEqual(observation['execution']['state'], 'done')
        self.assertEqual(observation['acceptance']['state'], 'pending')
        self.assertEqual(observation['write_ownership'], 'retained')
        self.reject('finish', id='work', accepted=True, evidence='self-PASS')
        self.reject('task', id='other', role='implementer', objective='edit', criteria=['works'], writes=['output.txt'])
        self.task('one'); self.task('two'); self.task('three')
        for key in ('one', 'two', 'three'): self.call('prepare', id=key)

    def test_review_can_run_without_producer_acceptance_and_records_exact_identity(self):
        self.producer(); self.reviewer()
        before = self.call('status')['task_observations']['tasks']['work']
        self.assertEqual(before['acceptance']['state'], 'pending')
        self.call('finish', id='work', accepted=True, evidence='accepted-report.md', review_task='review')
        after = self.call('status')['task_observations']['tasks']['work']
        self.assertEqual(after['acceptance'], dict(state='accepted', independent=True, review_task='review', basis='independent_review'))
        self.assertEqual(after['write_ownership'], 'released')

    def test_direct_transitive_and_restored_review_acceptance_cycles_reject(self):
        self.producer()
        self.task('consumer', depends_on=['work'])
        for dependencies in (['work'], ['consumer']):
            self.reject('task', id='review', role='verifier', objective='review', criteria=['check'],
                        depends_on=dependencies, review_targets=[dict(producer_id='work', revision='r1')])
        self.task('review', role='verifier', review_targets=[dict(producer_id='work', revision='r1')])
        state = self.state(); state['tasks']['review']['depends_on'] = ['consumer']
        with self.assertRaisesRegex(m.Error, 'acceptance it reviews'): m.validate_state(state)

    def test_wrong_revision_and_restored_bytes_never_revive_review_acceptance(self):
        self.producer()
        self.reject('task', id='wrong', role='verifier', objective='review', criteria=['check'],
                    review_targets=[dict(producer_id='work', revision='r2')])
        self.reviewer()
        output = self.root / 'output.txt'; old = output.read_bytes(); output.write_text('late edit')
        with self.assertRaises(m.InputInvalidation):
            self.call('finish', id='work', accepted=True, evidence='old', review_task='review')
        output.write_bytes(old)
        self.reject('finish', id='work', accepted=True, evidence='old restored', review_task='review')
        projected = self.call('status')['task_observations']['tasks']
        self.assertEqual(projected['review']['acceptance']['state'], 'invalidated')
        self.assertFalse(projected['review']['acceptance']['independent'])

    def test_rejection_and_stale_execution_are_not_acceptance(self):
        self.task(); self.start(); self.inv('work', status='done')
        self.call('finish', id='work', accepted=False, evidence='defect-report.md')
        self.now += 61
        result = self.call('status')['task_observations']['tasks']['work']
        self.assertEqual(result['execution']['state'], 'unknown')
        self.assertFalse(result['execution']['fresh'])
        self.assertEqual(result['acceptance']['state'], 'rejected')
        self.assertIn('REJECTED_TASK', {item['code'] for item in self.render()['blockers']})

    def test_missing_reviewer_session_is_blocked(self):
        self.producer(); self.reviewer()
        state = self.state(); del state['tasks']['review']['session_id']
        m.write_json(self.engine.path, state)
        self.reject('finish', id='work', accepted=True, review_task='review', evidence='accept')

    def test_missing_native_producer_session_blocks_finish(self):
        self.producer(); self.reviewer()
        state = self.state(); del state['tasks']['work']['session_id']
        m.write_json(self.engine.path, state)
        self.reject('finish', id='work', accepted=True, review_task='review', evidence='accept')

    def test_missing_recorded_review_hashes_blocks_finish(self):
        self.producer(); self.reviewer()
        state = self.state(); del state['tasks']['review']['reviewed_hashes']
        m.write_json(self.engine.path, state)
        self.reject('finish', id='work', accepted=True, review_task='review', evidence='accept')

    def test_renderer_missing_and_schema1_state_rejects_without_write(self):
        source = Path(__file__).resolve().parents[3] / 'tooling/render-active-run.py'
        argv = [sys.executable, '-X', 'utf8', '-B'] + (['-O'] if sys.flags.optimize else []) + [str(source),
                '--project', str(self.root), '--codex-home', str(self.engine.home),
                '--references', str(self.manifest), '--as-of', str(self.now)]
        state = self.state(); state['schema_version'] = 1; m.write_json(self.engine.path, state)
        before = self.engine.path.read_bytes()
        result = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8')
        self.assertNotEqual(result.returncode, 0); self.assertEqual(before, self.engine.path.read_bytes())
        self.engine.path.unlink()
        result = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8')
        self.assertNotEqual(result.returncode, 0); self.assertFalse(self.engine.path.exists())

    def test_projection_cannot_certify_missing_producer_session(self):
        self.producer(); self.reviewer()
        self.call('finish', id='work', accepted=True, review_task='review', evidence='accept')
        state = self.state(); del state['tasks']['work']['session_id']
        m.validate_state(state)  # Retain readable historical records, without upgrading them.
        observation = self.engine.task_observations(state)['tasks']['work']
        self.assertEqual(observation['execution']['state'], 'unknown')
        result = observation['acceptance']
        self.assertFalse(result['independent'])
        self.assertEqual(result['basis'], 'historical_unrecorded')

    def test_independence_requires_both_distinct_ids_and_recorded_review_hashes(self):
        self.producer(); self.reviewer()
        self.call('finish', id='work', accepted=True, review_task='review', evidence='accept')
        original = self.state()
        for mutation in ('reviewer_missing', 'both_missing', 'same_session', 'review_hashes_missing'):
            with self.subTest(mutation=mutation):
                state = copy.deepcopy(original)
                if mutation in ('reviewer_missing', 'both_missing'): del state['tasks']['review']['session_id']
                if mutation == 'both_missing': del state['tasks']['work']['session_id']
                if mutation == 'same_session': state['tasks']['review']['session_id'] = state['tasks']['work']['session_id']
                if mutation == 'review_hashes_missing': del state['tasks']['review']['reviewed_hashes']
                m.validate_state(state)
                self.assertFalse(self.engine.task_observations(state)['tasks']['work']['acceptance']['independent'])
                self.assertFalse(self.engine.dependency_accepted(state, 'work'))

    def acceptance_refs(self):
        refs = []
        for key in ('work', 'review'):
            path = self.root / (key + '-evidence.md'); path.write_text('pinned evidence reference')
            refs.append(dict(path=path.name, sha256=m.digest(path), kind='evidence', task_id=key))
        m.write_json(self.manifest, refs)
        return refs

    def test_missing_historical_identity_blocks_consumer(self):
        refs = self.acceptance_refs(); self.producer(); self.reviewer()
        self.call('finish', id='work', accepted=True, review_task='review', evidence='accept')
        state = self.state(); del state['tasks']['work']['review']
        m.write_json(self.engine.path, state)
        self.task('consumer', depends_on=['work'])
        rendered = self.render(refs)
        self.assertEqual(rendered['task_observations']['tasks']['work']['acceptance']['basis'], 'historical_unrecorded')
        self.assertIn(dict(code='DEPENDENCY_NOT_ACCEPTED', task_id='consumer', detail='work'), rendered['blockers'])
        self.reject('prepare', id='consumer', current_context=self.summary(refs))
        self.reject('prepare', id='consumer')

    def test_historical_identity_loss_after_prepare_blocks_actual_dispatch(self):
        refs = self.acceptance_refs(); self.producer(); self.reviewer()
        self.call('finish', id='work', accepted=True, review_task='review', evidence='accept')
        self.task('consumer', depends_on=['work'])
        prepared = self.call('prepare', id='consumer', current_context=self.summary(refs))
        state = self.state(); del state['tasks']['review']['session_id']
        m.write_json(self.engine.path, state)
        self.assertIn(dict(code='DEPENDENCY_NOT_ACCEPTED', task_id='consumer', detail='work'), self.render(refs)['blockers'])
        self.reject('dispatch-start', id='consumer', intent_id=prepared['intent_id'], current_context=self.summary(refs))
        self.reject('record', id='consumer', submitted_arguments=prepared['arguments'], session_id='consumer', receipt={'id':'consumer'})


if __name__ == '__main__': unittest.main()
