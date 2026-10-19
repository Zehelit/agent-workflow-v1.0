"""Non-atomic batch/restart counterexamples and matched executable local trace."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import test_workflow as fixtures

sys.modules['workflow'] = fixtures.m
spec = importlib.util.spec_from_file_location('coordination', Path(__file__).resolve().parents[1]/'scripts/coordination.py')
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
m = fixtures.m


def step(key, command, **payload): return dict(key=key, command=command, payload=payload)


class JournalTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    call = fixtures.WorkflowTests.call
    usage = fixtures.WorkflowTests.usage

    def adapter(self, fault=None): return c.Coordination(self.engine, fault)

    def test_partial_failure_and_restart_do_not_retry(self):
        steps = [step('one', 'task', id='one', role='implementer', objective='write one', criteria=['checked'], writes=['same.txt']),
                 step('two', 'task', id='two', role='implementer', objective='write two', criteria=['checked'], writes=['SAME.txt']),
                 step('never', 'prepare', id='one')]
        result = self.adapter().batch('partial', steps)
        self.assertIsNotNone(result['blocker'])
        journal = self.adapter().load(self.adapter().journal('partial'))
        self.assertEqual([s['outcome'] for s in journal['steps']], ['applied', 'failed'])
        before = self.engine.path.read_bytes()
        with patch.object(self.engine, 'execute', side_effect=AssertionError('blind retry')):
            self.assertIsNotNone(self.adapter().batch('partial', steps)['blocker'])
        self.assertEqual(before, self.engine.path.read_bytes())

    def test_lost_result_after_commit_stays_pending_across_restart(self):
        steps = [step('assign', 'task', id='one', role='implementer', objective='one', criteria=['checked'])]
        def fault(point, path):
            if point == 'batch.after_apply': raise OSError('lost result after applied mutation')
        with self.assertRaises(OSError): self.adapter(fault).batch('lost', steps)
        self.assertIn('one', m.read_json(self.engine.path)['tasks'])
        adapter = c.Coordination(m.Workflow(self.root, self.engine.home, lambda: self.now))
        with patch.object(adapter.engine, 'execute', side_effect=AssertionError('blind retry')):
            result = adapter.batch('lost', steps)
        self.assertEqual(result['results']['assign']['outcome'], 'pending')
        self.assertIsNotNone(result['blocker'])

    def test_completed_no_final_replay_and_request_identity(self):
        steps = [step('status', 'status')]
        result = self.adapter().batch('completed', steps)
        tokens = [result['token']]
        with patch.object(self.engine, 'execute', side_effect=AssertionError('repeat completed read')):
            self.assertEqual(self.adapter().batch('completed', steps, tokens)['results'], {})
        with self.assertRaises(m.Error): self.adapter().batch('completed', [step('other', 'status')])
        self.assertTrue({'blocker', 'session_identity', 'source_pin', 'evidence_path'} <= set(result))

    def test_raw_receipt_durable_even_when_inventory_stale(self):
        self.call('task', id='work', role='implementer', objective='one', criteria=['checked'])
        prep = self.call('prepare', id='work')
        self.call('inventory', inventory=[], observed_at=self.now,
                  observation=dict(source_kind='collaboration.list_agents', source_id='fixture-observation', receipt={'agents':[]}))
        self.call('dispatch-start', id='work', intent_id=prep['intent_id'])
        self.now += 61
        result = self.adapter().receipt('receipt_lost', 'work', prep['arguments'], '/root/work', {'task_name': '/root/work'}, 'actual caller receipt')
        self.assertIsNotNone(result['blocker'])
        journal = self.adapter().load(self.adapter().journal('receipt_lost'))
        self.assertEqual(journal['steps'][0]['request']['payload']['receipt'], {'task_name': '/root/work'})
        self.assertEqual(m.read_json(self.engine.path)['tasks']['work']['state'], 'uncertain')

    def test_read_pins_raw_journal_and_compact_output(self):
        (self.root/'input.txt').write_text('small input')
        result = self.adapter().batch('reads', [step('read', 'read', paths=['input.txt', 'absent.txt'])])
        self.assertNotIn('small input', json.dumps(result))
        raw = self.adapter().load(self.adapter().journal('reads'))['steps'][0]['result']['files']
        self.assertEqual(raw[0]['sha256'], m.digest(self.root/'input.txt'))
        self.assertIsNone(raw[1]['sha256'])


def matched_local_trace():
    """Run identical mechanical tasks/checks via serial or batch CLI calls.

    Both paths include assignment, input pin, prepare, fixture receipt recording,
    fixture terminal inventory, acceptance and closure. Native/model calls ZERO.
    Main native experiment must provide its own platform receipts separately.
    """
    report = {'native_calls': 0, 'real_model_calls_measured': 0,
              'receipts': 'synthetic fixture only, never native proof',
              'boundary_kind': 'modeled main decision boundaries; measured local subprocesses and bytes', 'paths': {}}
    with tempfile.TemporaryDirectory() as temporary:
        for mode in ('serial', 'batch'):
            root = Path(temporary)/mode; root.mkdir(); (root/'source.txt').write_text('same small job')
            now = m.time.time(); engine = m.Workflow(root, Path(temporary)/'home')
            engine.execute('activate', dict(objective='test', **fixtures.authorization(now), models={'gpt-6.1-sol':['high']}, slot_limit=4, inventory=[], observed_at=now))
            engine.execute('usage', dict(observed_at=now, rateLimits={'primary':{'usedPercent':10,'windowDurationMins':300},'secondary':{'usedPercent':10,'windowDurationMins':10080}}))
            steps = [step('inventory', 'inventory', inventory=[], observed_at=now),
                     step('one', 'task', id='one', role='researcher', objective='same small job one', criteria=['checked'], file_inputs=[dict(path='source.txt', revision='r1')]),
                     step('two', 'task', id='two', role='researcher', objective='same small job two', criteria=['checked'], file_inputs=[dict(path='source.txt', revision='r1')]),
                     step('prepare_one', 'prepare', id='one'), step('prepare_two', 'prepare', id='two'), step('status', 'status')]
            commands = 0; stdout_bytes = 0; stderr_bytes = 0; exits = []
            groups = [[s] for s in steps] if mode == 'serial' else [steps[:3], steps[3:]]
            index = 0
            while groups:
                group = groups.pop(0)
                payload_path = root/f'payload-{index}.json'
                if mode == 'serial':
                    payload_path.write_text(json.dumps(group[0]['payload']), encoding='utf-8')
                    command = [sys.executable, '-X', 'utf8', '-B', str(Path(m.__file__)), group[0]['command'], '--project', str(root), '--codex-home', str(engine.home), '--input', str(payload_path)]
                else:
                    payload_path.write_text(json.dumps(dict(batch_id=f'batch_{index}', steps=group)), encoding='utf-8')
                    command = [sys.executable, '-X', 'utf8', '-B', str(Path(c.__file__)), '--project', str(root), '--codex-home', str(engine.home), '--input', str(payload_path)]
                outcome = subprocess.run(command, capture_output=True)
                commands += 1; stdout_bytes += len(outcome.stdout); stderr_bytes += len(outcome.stderr); exits.append(outcome.returncode)
                if outcome.returncode: raise RuntimeError(outcome.stderr.decode('utf-8') + outcome.stdout.decode('utf-8'))
                index += 1
                if not groups and m.read_json(engine.path)['tasks']['one']['state'] == 'prepared':
                    prepared = m.read_json(engine.path)['tasks']
                    terminal = [step('record_'+key, 'record', id=key, submitted_arguments=prepared[key]['prepared'], session_id=key, receipt={'id':key,'fixture_only':True}) for key in ('one','two')]
                    terminal += [step('terminal_inventory', 'inventory', inventory=[dict(id=key, status='done') for key in ('one','two')], observed_at=m.time.time())]
                    terminal += [step('finish_'+key, 'finish', id=key, accepted=True, evidence='synthetic matched local fixture checked') for key in ('one','two')]
                    terminal += [step('snapshot', 'snapshot', writers_quiescent=True, next_action='matched local job delivered', files=['source.txt']), step('complete', 'complete')]
                    groups.extend([[s] for s in terminal] if mode == 'serial' else [terminal])
            state = m.read_json(engine.path)
            if {t['state'] for t in state['tasks'].values()} != {'accepted'} or state['status'] != 'complete': raise RuntimeError('matched trace did not complete identical tasks')
            report['paths'][mode] = dict(helper_commands=commands, modeled_main_boundaries=commands, stdout_bytes=stdout_bytes, stderr_bytes=stderr_bytes, exits=exits,
                                         task_ids=sorted(state['tasks']), states={k:t['state'] for k,t in state['tasks'].items()}, source_file_pin=m.digest(root/'source.txt'))
    return report


if __name__ == '__main__':
    if '--trace' in sys.argv: print(json.dumps(matched_local_trace(), indent=2))
    else: unittest.main()
