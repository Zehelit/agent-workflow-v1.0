"""Typed contracts: every guard is exercised with fresh source, usage and inventory."""
import copy
import json
import errno
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import test_workflow as fixtures

m = fixtures.m


class InputTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    call = fixtures.WorkflowTests.call
    task = fixtures.WorkflowTests.task
    usage = fixtures.WorkflowTests.usage
    inv = fixtures.WorkflowTests.inv
    start = fixtures.WorkflowTests.start

    def file_contract(self, key='work', path='input.txt', present=True):
        if present:
            (self.root / path).write_text('original', encoding='utf-8')
        self.task(key, file_inputs=[dict(path=path, revision='input-r1')], revision=key + '-r1')

    def rejected_unchanged(self, command, payload):
        before = self.engine.path.read_bytes()
        with self.assertRaises(m.Error) as raised:
            self.engine.execute(command, payload)
        self.assertEqual(self.engine.path.read_bytes(), before)
        return raised.exception

    def invalidated(self, command, payload, gate, ids):
        before = m.read_json(self.engine.path)
        with self.assertRaises(m.InputInvalidation) as raised:
            self.engine.execute(command, payload)
        receipt = raised.exception.receipt
        self.assertIn(gate, receipt['reason'])
        self.assertEqual(receipt['affected_task_ids'], ids)
        after = m.read_json(self.engine.path)
        for key in ids:
            self.assertTrue(after['tasks'][key].pop('contract_invalid'))
        self.assertEqual(after.pop('invalidations'), [receipt])
        self.assertEqual(after, before)
        return receipt

    def test_unchanged_present_absent_and_context_are_explicit(self):
        self.file_contract(present=False)
        self.task('context', context_refs=['input.txt', 'work', '../../historical'])
        self.start(); self.inv('work', status='idle')
        self.assertEqual(self.call('finish', id='work', accepted=True, evidence='checked')['state'], 'accepted')
        self.assertEqual(self.call('prepare', id='context')['tool'], 'collaboration.spawn_agent')
        state = m.read_json(self.engine.path)
        self.assertEqual(state['tasks']['work']['file_inputs'], [dict(path='input.txt', revision='input-r1', sha256=None, present=False)])
        self.assertFalse(state['tasks']['context']['file_inputs'])
        self.assertFalse(state['tasks']['context']['review_targets'])

    def test_edit_delete_create_before_prepare_durable_consumers(self):
        for mutation in ('edit', 'delete', 'create'):
            with self.subTest(mutation=mutation):
                self.setUp()
                self.file_contract(present=mutation != 'create')
                self.task('consumer', depends_on=['work'])
                path = self.root / 'input.txt'
                if mutation == 'delete': path.unlink()
                else: path.write_text('changed')
                receipt = self.invalidated('prepare', dict(id='work'), 'prepare: file input changed', ['consumer', 'work'])
                self.assertEqual((receipt['path'], receipt['revision']), ('input.txt', 'work-r1'))
                self.rejected_unchanged('prepare', dict(id='work'))

    def test_changed_after_prepare_rejected_at_record(self):
        self.file_contract()
        args = self.call('prepare', id='work')['arguments']
        (self.root / 'input.txt').write_text('after prepare')
        self.invalidated('record', dict(id='work', submitted_arguments=args, session_id='work', receipt={'id': 'work'}), 'record:', ['work'])

    def test_change_during_execution_rejected_at_finish(self):
        self.file_contract(); self.start(); self.inv('work', status='idle')
        (self.root / 'input.txt').unlink()
        self.invalidated('finish', dict(id='work', accepted=True, evidence='old result'), 'finish:', ['work'])

    def test_assignment_revision_replacement_never_rehashes_old_result(self):
        self.file_contract(); self.start(); self.inv('work', status='idle')
        state = m.read_json(self.engine.path)
        state['tasks']['work']['file_inputs'][0]['revision'] = 'input-r2'
        m.write_json(self.engine.path, state)
        self.invalidated('finish', dict(id='work', accepted=True, evidence='old result'), 'assignment revision or contract replaced', ['work'])

    def review(self):
        self.task(writes=['output.txt'], revision='producer-r1', independent_review=True)
        self.start(); (self.root / 'output.txt').write_text('result')
        self.inv('work', status='idle')
        self.task('review', role='verifier', review_targets=[dict(producer_id='work', revision='producer-r1')])

    def test_review_target_change_before_prepare_during_review_and_after_review(self):
        for gate in ('prepare', 'finish', 'accept review'):
            with self.subTest(gate=gate):
                self.setUp(); self.review()
                if gate != 'prepare':
                    self.start('review'); self.inv('work', 'review', status='idle')
                if gate == 'accept review':
                    self.call('finish', id='review', accepted=True, evidence='checked')
                (self.root / 'output.txt').write_text('late edit')
                if gate == 'prepare': payload = dict(id='review')
                elif gate == 'finish': payload = dict(id='review', accepted=True, evidence='old review')
                else: payload = dict(id='work', accepted=True, evidence='old output', review_task='review')
                self.invalidated('prepare' if gate == 'prepare' else 'finish', payload, gate + ': review target changed', ['review', 'work'])

    def test_review_revision_replacement_and_author_quiescence(self):
        self.task(writes=['output.txt'], revision='producer-r1'); self.start()
        self.rejected_unchanged('task', dict(id='review', role='verifier', objective='review', criteria=['checked'], review_targets=[dict(producer_id='work', revision='producer-r1')]))
        self.inv('work', status='idle')
        self.task('review', role='verifier', review_targets=[dict(producer_id='work', revision='producer-r1')])
        state = m.read_json(self.engine.path); state['tasks']['work']['revision'] = 'producer-r2'; m.write_json(self.engine.path, state)
        receipt = self.invalidated('prepare', dict(id='review'), 'producer revision changed', ['review', 'work'])
        self.assertEqual((receipt['expected'], receipt['actual']), ('producer-r1', 'producer-r2'))

    def test_producer_file_input_change_invalidates_review_and_downstream(self):
        (self.root / 'source.txt').write_text('source')
        self.task(writes=['output.txt'], revision='r1', file_inputs=[dict(path='source.txt', revision='source-r1')])
        self.start(); (self.root / 'output.txt').write_text('output'); self.inv('work', status='idle')
        self.task('review', role='verifier', review_targets=[dict(producer_id='work', revision='r1')])
        self.start('review'); self.inv('work', 'review', status='idle')
        self.task('downstream', depends_on=['review'])
        (self.root / 'source.txt').write_text('source edited during review')
        receipt = self.invalidated('finish', dict(id='review', accepted=True, evidence='stale review'), 'review producer: file input changed', ['downstream', 'review', 'work'])
        self.assertEqual(receipt['path'], 'source.txt')

    def test_planned_downstream_write_does_not_invalidate_design_input(self):
        source = self.root / 'app.mjs'
        source.write_text('baseline', encoding='utf-8')
        self.task('design', role='architect', writes=['design.md'],
                  file_inputs=[dict(path='app.mjs', revision='baseline-r1')])
        self.start('design')
        (self.root / 'design.md').write_text('change app.mjs', encoding='utf-8')
        self.inv('design', status='done')
        self.call('finish', id='design', accepted=True, evidence='design accepted')

        self.task('implementation', writes=['app.mjs'], depends_on=['design'], revision='implementation-r1',
                  independent_review=True)
        self.start('implementation')
        source.write_text('implemented', encoding='utf-8')
        self.inv('design', 'implementation', status='done')
        self.task('review', role='verifier', depends_on=['design'],
                  review_targets=[dict(producer_id='implementation', revision='implementation-r1')])
        prepared = self.call('prepare', id='review')['arguments']
        self.call('record', id='review', submitted_arguments=prepared,
                  session_id='review', receipt={'id': 'review'})
        self.inv('design', 'implementation', 'review', status='done')
        self.call('finish', id='review', accepted=True, evidence='independent review passed')
        self.call('finish', id='implementation', accepted=True,
                  evidence='implementation accepted', review_task='review')

    def test_downstream_write_claim_does_not_hide_pre_dispatch_input_change(self):
        source = self.root / 'app.mjs'
        source.write_text('baseline', encoding='utf-8')
        self.task('design', role='architect', writes=['design.md'],
                  file_inputs=[dict(path='app.mjs', revision='baseline-r1')])
        self.start('design')
        (self.root / 'design.md').write_text('design', encoding='utf-8')
        self.inv('design', status='done')
        self.call('finish', id='design', accepted=True, evidence='design accepted')
        self.task('implementation', writes=['app.mjs'], depends_on=['design'])
        source.write_text('unexpected edit before dispatch', encoding='utf-8')
        self.invalidated('prepare', dict(id='implementation'),
                         'dependency: file input changed', ['design', 'implementation'])

    def test_unrelated_review_target_write_cannot_mask_reader_input(self):
        source = self.root / 'shared.txt'
        source.write_text('original', encoding='utf-8')
        self.task('reader', revision='reader-r1',
                  file_inputs=[dict(path='shared.txt', revision='source-r1')])
        self.start('reader')
        self.task('writer', revision='writer-r1', writes=['shared.txt'])
        self.start('writer')
        source.write_text('changed by writer', encoding='utf-8')
        self.inv('reader', 'writer', status='done')
        self.task('review', role='verifier', review_targets=[
            dict(producer_id='writer', revision='writer-r1'),
            dict(producer_id='reader', revision='reader-r1')])
        self.invalidated('prepare', dict(id='review'),
                         'review producer: file input changed', ['reader', 'review'])

    def test_accepted_output_cannot_be_repinned_after_external_change(self):
        self.task('producer', revision='producer-r1', writes=['output.txt'])
        self.start('producer')
        output = self.root / 'output.txt'
        output.write_text('accepted output', encoding='utf-8')
        self.inv('producer', status='done')
        self.call('finish', id='producer', accepted=True, evidence='accepted result')
        output.write_text('external replacement', encoding='utf-8')
        self.invalidated('task', dict(id='review', role='verifier', objective='review',
                         criteria=['checked'], review_targets=[
                             dict(producer_id='producer', revision='producer-r1')]),
                         'assign review changed', ['producer'])

    def test_direct_apply_commits_only_invalidation_and_link_change_is_rejected(self):
        import os
        self.file_contract()
        state = m.read_json(self.engine.path)
        (self.root / 'input.txt').write_text('changed')
        with self.assertRaises(m.InputInvalidation): self.engine.apply(state, 'prepare', dict(id='work'))
        self.assertTrue(state['tasks']['work']['contract_invalid'])
        self.assertEqual(state['tasks']['work']['state'], 'ready')
        self.assertEqual(m.read_json(self.engine.path)['tasks']['work']['state'], 'ready')
        self.setUp(); self.file_contract()
        target = self.root / 'target.txt'; target.write_text('original')
        (self.root / 'input.txt').unlink()
        try: os.symlink(target, self.root / 'input.txt')
        except OSError as exc: self.skipTest('host cannot create disposable symlink: ' + str(exc))
        self.invalidated('prepare', dict(id='work'), 'input path unsafe', ['work'])

    def test_review_never_infers_task_from_context_or_legacy_inputs(self):
        self.task(writes=['output.txt'], independent_review=True); self.start(); self.inv('work', status='idle')
        self.task('review', role='verifier', context_refs=['work'])
        self.start('review'); self.inv('work', 'review', status='idle')
        self.call('finish', id='review', accepted=True, evidence='context only')
        self.rejected_unchanged('finish', dict(id='work', accepted=True, evidence='result', review_task='review'))
        self.rejected_unchanged('task', dict(id='legacy', role='implementer', objective='work', criteria=['checked'], inputs=['work']))

    def test_invalid_duplicate_casefold_escape_and_strict_schema(self):
        for inputs in ([dict(path='a.txt', revision='r'), dict(path='A.txt', revision='r')],
                       [dict(path='../a.txt', revision='r')], [dict(path='a.txt', revision=True)],
                       [dict(path='a.txt', revision='r', sha256='caller hash')], [dict(path='.agent-workflow/state.json', revision='r')]):
            with self.subTest(inputs=inputs):
                self.rejected_unchanged('task', dict(id='bad', role='implementer', objective='work', criteria=['checked'], file_inputs=inputs))
        with self.assertRaisesRegex(m.Error, 'duplicate JSON key'):
            m.strict_json('{"file_inputs":[{"path":"a","revision":"r","revision":"new"}]}')
        self.file_contract()
        self.rejected_unchanged('task', dict(id='bad', role='verifier', objective='review', criteria=['checked'], review_targets=[dict(producer_id='work', revision='r'), dict(producer_id='work', revision='r')]))

    def test_same_path_old_pins_do_not_invalidate_current_revision_or_descendant(self):
        for mutation in ('replace', 'delete', 'create'):
            with self.subTest(mutation=mutation):
                self.setUp()
                source = self.root / 'source.txt'
                if mutation != 'create': source.write_text('r1 bytes')
                for key in ('left', 'right'):
                    self.task(key, file_inputs=[dict(path='source.txt', revision='source-r1')])
                    self.task(key + '_child', depends_on=[key])
                if source.exists(): source.unlink()
                if mutation != 'delete': source.write_text('r2 bytes')
                self.task('fresh', file_inputs=[dict(path='source.txt', revision='source-r2')])
                self.task('fresh_child', depends_on=['fresh'])
                (self.root / 'stable.txt').write_text('stable')
                self.task('stable', file_inputs=[dict(path='stable.txt', revision='stable-r1')])
                self.task('context', context_refs=['source.txt', 'left'])
                receipt = self.invalidated('prepare', dict(id='left'), 'file input changed',
                                           ['left', 'left_child', 'right', 'right_child'])
                self.assertEqual(receipt['actual'], m.digest(source))
                self.rejected_unchanged('prepare', dict(id='left'))
                self.rejected_unchanged('prepare', dict(id='right'))
                self.start('fresh'); self.inv('fresh', status='idle')
                self.call('finish', id='fresh', accepted=True, evidence='current pins')
                self.assertEqual(self.call('prepare', id='fresh_child')['tool'], 'collaboration.spawn_agent')
                state = m.read_json(self.engine.path)
                self.assertEqual(len(state['invalidations']), 1)
                for key in ('fresh', 'fresh_child', 'stable', 'context'):
                    self.assertFalse(state['tasks'][key].get('contract_invalid', False))

    def test_accepted_review_unsafe_output_is_durable_and_restore_cannot_revive_it(self):
        for mutation in ('symlink', 'escape_link', 'hardlink', 'directory', 'delete'):
            with self.subTest(mutation=mutation):
                self.setUp(); self.review(); self.start('review'); self.inv('work', 'review', status='idle')
                self.call('finish', id='review', accepted=True, evidence='original reviewed')
                self.task('consumer', depends_on=['work'])
                self.task('review_consumer', depends_on=['review'])
                output = self.root / 'output.txt'
                original = output.read_bytes()
                target = (self.root.parent if mutation == 'escape_link' else self.root) / 'target.txt'
                target.write_bytes(original); output.unlink()
                if mutation in ('symlink', 'escape_link'): os.symlink(target, output)
                elif mutation == 'hardlink': os.link(target, output)
                elif mutation == 'directory': output.mkdir()
                with patch.object(m, 'digest', wraps=m.digest) as reads:
                    receipt = self.invalidated('finish', dict(id='work', accepted=True, evidence='old review', review_task='review'),
                                               'review target', ['consumer', 'review', 'review_consumer', 'work'])
                    if mutation != 'delete':
                        reads.assert_not_called()
                        self.assertTrue(receipt['actual'].startswith('unsafe: '))
                    else: self.assertIsNone(receipt['actual'])
                self.assertEqual(receipt['path'], 'output.txt')
                self.assertEqual(receipt['expected'], m.hashlib.sha256(original).hexdigest())
                if mutation == 'directory': output.rmdir()
                elif mutation != 'delete': output.unlink()
                output.write_bytes(original)
                error = self.rejected_unchanged('finish', dict(id='work', accepted=True, evidence='restored', review_task='review'))
                self.assertIn('explicit new assignment', str(error))
                state = m.read_json(self.engine.path)
                self.assertEqual(state['tasks']['work']['state'], 'running')
                self.assertEqual(state['tasks']['review']['state'], 'accepted')
                self.assertEqual(len(state['invalidations']), 1)

    def test_review_output_unsafe_at_prepare_record_and_verifier_finish(self):
        for gate in ('prepare', 'record', 'finish'):
            with self.subTest(gate=gate):
                self.setUp(); self.review()
                if gate == 'record': args = self.call('prepare', id='review')['arguments']
                elif gate == 'finish': self.start('review'); self.inv('work', 'review', status='idle')
                output = self.root / 'output.txt'; output.unlink(); output.mkdir()
                payload = dict(id='review')
                if gate == 'record': payload.update(submitted_arguments=args, session_id='review', receipt={'id':'review'})
                elif gate == 'finish': payload.update(accepted=True, evidence='old output')
                self.invalidated(gate, payload, gate + ': review target: output path unsafe', ['review', 'work'])

    def test_junction_parent_replacement_never_reads_output_target(self):
        if os.name != 'nt': return
        folder = self.root / 'artifacts'; folder.mkdir()
        self.task(writes=['artifacts/output.txt'], revision='r1', independent_review=True)
        self.start(); output = folder / 'output.txt'; output.write_text('original')
        self.inv('work', status='idle')
        self.task('review', role='verifier', review_targets=[dict(producer_id='work', revision='r1')])
        self.start('review'); self.inv('work', 'review', status='idle')
        self.call('finish', id='review', accepted=True, evidence='reviewed')
        original = output.read_bytes(); output.unlink(); folder.rmdir()
        target = self.root.parent / 'junction-target'; target.mkdir(); (target / 'output.txt').write_bytes(original)
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(folder), str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with patch.object(m, 'digest', wraps=m.digest) as reads:
                receipt = self.invalidated('finish', dict(id='work', accepted=True, evidence='old', review_task='review'),
                                           'output path unsafe', ['review', 'work'])
                reads.assert_not_called()
                self.assertEqual(receipt['path'], 'artifacts/output.txt')
        finally:
            os.rmdir(folder)  # Remove the junction itself; do not traverse its target.
        folder.mkdir(); (folder / 'output.txt').write_bytes(original)
        self.rejected_unchanged('finish', dict(id='work', accepted=True, evidence='restored', review_task='review'))

    def test_unrelated_io_fault_is_not_hidden_as_contract_invalidation(self):
        self.file_contract()
        with patch.object(m, 'digest', side_effect=OSError(errno.EIO, 'unrelated I/O failure')):
            before = self.engine.path.read_bytes()
            with self.assertRaises(OSError) as raised: self.call('prepare', id='work')
            self.assertEqual(raised.exception.errno, errno.EIO)
            self.assertEqual(self.engine.path.read_bytes(), before)
        self.assertNotIn('invalidations', m.read_json(self.engine.path))

    def test_hardlink_input_assignment_rejected_before_hashing(self):
        source = self.root / 'source.txt'; source.write_text('same bytes')
        os.link(source, self.root / 'alias.txt')
        with patch.object(m, 'digest', wraps=m.digest) as reads:
            error = self.rejected_unchanged('task', dict(id='bad', role='implementer', objective='work', criteria=['checked'],
                                                       file_inputs=[dict(path='alias.txt', revision='r1')]))
            reads.assert_not_called()
            self.assertIn('hard-linked', str(error))

    def test_unsafe_accepted_dependency_invalidates_exact_consumers(self):
        self.task(writes=['output.txt']); self.start()
        output = self.root / 'output.txt'; output.write_text('accepted bytes')
        self.inv('work', status='idle'); self.call('finish', id='work', accepted=True, evidence='checked')
        self.task('consumer', depends_on=['work']); self.task('descendant', depends_on=['consumer'])
        self.task('unrelated', context_refs=['output.txt', 'work'])
        target = self.root / 'target.txt'; target.write_bytes(output.read_bytes())
        output.unlink(); os.symlink(target, output)
        receipt = self.invalidated('prepare', dict(id='consumer'), 'dependency artifacts: output path unsafe',
                                   ['consumer', 'descendant', 'work'])
        self.assertEqual(receipt['path'], 'output.txt')
        self.assertEqual(m.read_json(self.engine.path)['tasks']['work']['state'], 'accepted')


if __name__ == '__main__': unittest.main()
