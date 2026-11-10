"""Producer gate must propagate red tests, source drift and invalid rework."""
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('producer_gate_tests', ROOT/'tooling/check-producer.py')
p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)


class ProducerTests(unittest.TestCase):
    def metadata(self): return dict(rejections=[], rework_seconds=None)

    def rejection(self, **kwargs):
        return dict(defect_id='defect-1', defect_class='pin_drift', reproduction='selector detects restored stale bytes',
                    corrector_id='one-writer', review_task='fresh-review', revision='r2',
                    source_pins={'skills/agent-workflow-v1-1/scripts/workflow.py': '0'*64}, **kwargs)

    def fixture(self, red=False):
        module = types.ModuleType('producer_red_fixture')
        class Case(unittest.TestCase):
            def test_counterexample(self):
                self.assertFalse(red, 'deliberate red counterexample must block producer PASS')
        module.Case = Case
        return module

    def test_red_test_and_missing_selector_cannot_be_producer_pass(self):
        with patch.dict(sys.modules, {'producer_red_fixture': self.fixture(red=True)}):
            result = p.run_group(['producer_red_fixture.Case'], io.StringIO())
        self.assertFalse(result['ok']); self.assertEqual(result['tests'], 1); self.assertEqual(len(result['failures']), 1)
        result = p.run_group(['module_that_does_not_exist.Case'], io.StringIO())
        self.assertFalse(result['ok']); self.assertTrue(result['errors'])
        self.assertFalse(p.run_group([], io.StringIO())['ok'])

    def test_red_gate_exits_without_independent_acceptance_or_pass(self):
        with patch.dict(sys.modules, {'producer_red_fixture': self.fixture(red=True)}), \
                patch.object(p, 'GROUPS', {'red': ['producer_red_fixture.Case']}), \
                patch.object(p, 'load_tests', return_value={'synthetic_only': True}):
            result = p.gate(['tooling/check-producer.py'], 'test contract', self.metadata(), io.StringIO())
        self.assertFalse(result['producer_pass']); self.assertFalse(result['independent_acceptance'])
        self.assertTrue(result['groups']['red']['failures'])

    def test_pin_drift_and_wrong_frozen_pin_block_pass(self):
        before = {'source': '0'*64}; after = {'source': '1'*64}
        with patch.dict(sys.modules, {'producer_red_fixture': self.fixture()}), \
                patch.object(p, 'GROUPS', {'positive': ['producer_red_fixture.Case']}), \
                patch.object(p, 'load_tests', return_value={'synthetic_only': True}), \
                patch.object(p, 'pins', side_effect=[before, after]):
            result = p.gate(['tooling/check-producer.py'], 'test contract', self.metadata(), io.StringIO())
        self.assertFalse(result['producer_pass']); self.assertFalse(result['source_unchanged'])
        with self.assertRaisesRegex(ValueError, 'source pins changed'):
            p.gate(['tooling/check-producer.py'], 'test', self.metadata(), io.StringIO(), {})

    def test_one_defect_one_corrector_and_focused_independent_review(self):
        first = self.rejection()
        good = dict(rejections=[first], rework_seconds=1.5)
        self.assertEqual(p.validate_metadata(good), good)
        second = dict(first, corrector_id='second-writer', root_plan_correction='root adjusted scope')
        with self.assertRaisesRegex(ValueError, 'one corrector'): p.validate_metadata(dict(rejections=[first, second], rework_seconds=None))
        self_review = dict(first, review_task=first['corrector_id'])
        with self.assertRaisesRegex(ValueError, 'review itself'): p.validate_metadata(dict(rejections=[self_review], rework_seconds=None))

    def test_second_same_class_requires_root_plan_correction(self):
        first = self.rejection(); second = dict(first, defect_id='defect-2', revision='r3')
        with self.assertRaisesRegex(ValueError, 'root plan correction'):
            p.validate_metadata(dict(rejections=[first, second], rework_seconds=None))
        second['root_plan_correction'] = 'root narrows repair and updates the focused rereview boundary'
        p.validate_metadata(dict(rejections=[first, second], rework_seconds=None))

    def test_metadata_strict_bool_nonfinite_missing_and_unknown_fields(self):
        for seconds in (True, float('nan'), float('inf'), -1):
            with self.assertRaises(ValueError): p.validate_metadata(dict(rejections=[], rework_seconds=seconds))
        for metadata in ({}, dict(rejections=[], rework_seconds=None, self_pass=True),
                         dict(rejections=[{'defect_id': 'x'}], rework_seconds=None),
                         dict(rejections=[dict(self.rejection(), source_pins={'source':'not-a-pin'})], rework_seconds=None)):
            with self.assertRaises(ValueError): p.validate_metadata(metadata)

    def test_declared_changed_path_is_always_pinned(self):
        selector = p.GROUPS['A04'][0]
        with patch.object(p, 'GROUPS', {'A04': [selector]}):
            result = p.gate(['skills/agent-workflow-v1-1/references/operations.md'], 'union pin contract', self.metadata(), io.StringIO())
        self.assertTrue(result['producer_pass'])
        self.assertEqual(result['groups']['A04']['selectors'], [selector])
        self.assertEqual(result['source_pins']['skills/agent-workflow-v1-1/references/operations.md'], p.sha(ROOT/'skills/agent-workflow-v1-1/references/operations.md'))
        self.assertTrue(set(p.PIN_PATHS) <= set(result['source_pins']))

    def test_declared_markdown_drift_and_incomplete_expected_union_reject(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'source.py'; note = root/'change.md'
            source.write_text('fixed input'); note.write_text('before')
            def drift(selectors, stream):
                note.write_text('after')
                return dict(ok=True, tests=1, failures=[], errors=[], skipped=[])
            with patch.object(p, 'ROOT', root), patch.object(p, 'PIN_PATHS', ['source.py']), \
                    patch.object(p, 'GROUPS', {'drift': ['fixture']}), \
                    patch.object(p, 'load_tests', return_value={'disposable':True}), patch.object(p, 'run_group', side_effect=drift):
                before = p.pins(['change.md'])
                result = p.gate(['change.md'], 'union pin contract', self.metadata(), io.StringIO(), before)
                self.assertFalse(result['producer_pass']); self.assertFalse(result['source_unchanged'])
                self.assertEqual(result['source_pins'], before)
                with self.assertRaisesRegex(ValueError, 'source pins changed'):
                    p.gate(['change.md'], 'union', self.metadata(), io.StringIO(), {'source.py':p.sha(source)})

    def test_unsafe_missing_alias_and_linked_source_paths_reject(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'source.py').write_text('source'); (root/'nested').mkdir()
            with patch.object(p, 'ROOT', root), patch.object(p, 'PIN_PATHS', ['source.py']):
                for paths in (['.'], [''], ['absent.md'], ['../outside.md'], ['/absolute'], ['C:/escape'],
                              ['nested\\file'], ['./source.py'], ['nested/../source.py'], ['source.py','SOURCE.py'],
                              ['nested'], [False], [['source.py']]):
                    with self.subTest(paths=paths), self.assertRaises((ValueError, OSError)):
                        p.gate(paths, 'safe paths', self.metadata(), io.StringIO())
                for path in ('source.py.', 'source.py ', 'CON', 'nul.txt', '.agent-workflow/state.json', 'bad\x00name'):
                    with self.subTest(path=path), self.assertRaises(ValueError): p.source_file(path)
                import os
                os.link(root/'source.py', root/'alias.py')
                with self.assertRaises(ValueError): p.pins(['alias.py'])
                (root/'alias.py').unlink()
                with patch.object(p, 'PIN_PATHS', ['missing-fixed.py']), self.assertRaises(OSError): p.pins(['source.py'])

    def test_actual_A04_selector_failure_blocks_gate(self):
        p.load_tests()
        import test_inputs
        method = p.GROUPS['A04'][0].rsplit('.', 1)[1]
        def red(case): case.fail('injected actual A04 boundary failure')
        with patch.object(test_inputs.InputTests, method, red), patch.object(p, 'GROUPS', {'A04':[p.GROUPS['A04'][0]]}):
            result = p.gate(['tooling/check-producer.py'], 'actual selector propagation', self.metadata(), io.StringIO())
        self.assertFalse(result['producer_pass']); self.assertFalse(result['independent_acceptance'])
        self.assertEqual(len(result['groups']['A04']['failures']), 1)


if __name__ == '__main__': unittest.main()
