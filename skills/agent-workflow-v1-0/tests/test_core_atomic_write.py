"""Windows state publication under real concurrent file readers."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'scripts/workflow.py'
spec = importlib.util.spec_from_file_location('core_atomic_workflow', SOURCE)
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


@unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics')
class CoreAtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='core-atomic-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        workflow.write_json(self.path, {'state': 'old'})
        self.old = self.path.read_bytes()

    def test_internal_reader_can_overlap_complete_state_replacement(self):
        with workflow.shared_binary(self.path) as held:
            self.assertEqual(held.read(), self.old)
            workflow.write_json(self.path, {'state': 'new'})
            held.seek(0)
            self.assertEqual(held.read(), self.old)
        self.assertEqual(workflow.read_json(self.path), {'state': 'new'})
        self.assertEqual(list(self.path.parent.glob('state.json.tmp-*')), [])
        self.assertEqual(list(self.path.parent.glob('state.json.bak-*')), [])

    def test_external_nonsharing_reader_fails_bounded_without_losing_state(self):
        started = time.monotonic()
        with self.path.open('rb'):
            with self.assertRaises(PermissionError):
                workflow.write_json(self.path, {'state': 'new'})
        self.assertGreaterEqual(time.monotonic() - started, 0.45)
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assertEqual(list(self.path.parent.glob('state.json.tmp-*')), [])


if __name__ == '__main__':
    unittest.main()
