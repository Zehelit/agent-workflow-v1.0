"""Regression controls for quiescent rejection and invalidation-history compaction."""
import unittest
import test_workflow as fixtures
import test_inputs as input_fixtures

m=fixtures.m

class LifecycleCleanupTests(unittest.TestCase):
    setUp=fixtures.WorkflowTests.setUp
    call=fixtures.WorkflowTests.call
    task=fixtures.WorkflowTests.task
    usage=fixtures.WorkflowTests.usage
    inv=fixtures.WorkflowTests.inv
    start=fixtures.WorkflowTests.start
    file_contract=input_fixtures.InputTests.file_contract
    invalidated=input_fixtures.InputTests.invalidated

    def test_invalidated_quiescent_rejection_releases_owned_output(self):
        (self.root/'input.txt').write_text('original')
        self.task(writes=['output.txt'],file_inputs=[dict(path='input.txt',revision='r1')])
        self.start();self.inv('work',status='idle')
        (self.root/'input.txt').write_text('edited')
        self.invalidated('finish',dict(id='work',accepted=True,evidence='stale result'),'finish:', ['work'])
        self.assertEqual(self.call('finish',id='work',accepted=False,evidence='discard stale result')['state'],'rejected')
        self.task('replacement',writes=['output.txt'])
        state=m.read_json(self.engine.path)
        self.assertTrue(state['tasks']['work']['contract_invalid'])
        self.assertEqual(len(state['invalidations']),1)
        self.assertEqual(state['tasks']['replacement']['state'],'ready')

    def test_rejection_does_not_require_trusting_unsafe_output(self):
        self.task(writes=['output.txt']);self.start();self.inv('work',status='done')
        (self.root/'output.txt').mkdir()
        self.assertEqual(self.call('finish',id='work',accepted=False,evidence='discard unusable output')['state'],'rejected')

    def test_running_writer_still_cannot_be_rejected(self):
        self.task();self.start()
        before=self.engine.path.read_bytes()
        with self.assertRaises(m.Error):self.call('finish',id='work',accepted=False,evidence='writer remains active')
        self.assertEqual(self.engine.path.read_bytes(),before)

    def test_compact_archives_invalidations_without_dangling_current_references(self):
        self.file_contract()
        (self.root/'input.txt').write_text('edited')
        self.invalidated('prepare',dict(id='work'),'prepare:', ['work'])
        self.call('cancel',id='work',reason='replace invalid assignment')
        cp=self.call('snapshot',files=['input.txt'],writers_quiescent=True,next_action='compact terminal history')['checkpoint']
        archived=m.read_json(self.engine.base/'checkpoints'/cp['id']/'state.json')
        self.assertEqual(len(archived['invalidations']),1)
        self.assertIn('work',archived['tasks'])
        self.assertEqual(self.call('compact')['tasks'],0)
        current=m.read_json(self.engine.path)
        self.assertEqual(current['tasks'],{})
        self.assertFalse(current.get('invalidations'))
        m.validate_state(current)
        self.assertEqual(m.read_json(self.engine.base/'checkpoints'/cp['id']/'state.json'),archived)

if __name__=='__main__':unittest.main()
