"""User model restrictions hold for disk definitions, memory and native exports."""
import copy
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('policy_workflow', Path(__file__).resolve().parents[1] / 'scripts/workflow.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.engine = m.Workflow(self.root, self.root / 'home')

    def test_catalog_rejects_unapproved_model_low_effort_and_retired_role(self):
        original = copy.deepcopy(self.engine.catalog)
        for change in ('model', 'default', 'allowed', 'old-key'):
            with self.subTest(change=change):
                self.engine.catalog = copy.deepcopy(original)
                role = 'implementer'
                if change == 'model': self.engine.catalog[role]['model'] = 'gpt-6-luna'
                if change == 'default': self.engine.catalog[role]['model_reasoning_effort'] = 'low'
                if change == 'allowed': self.engine.catalog[role]['workflow']['efforts'] = ['low', 'high']
                if change == 'old-key':
                    role = 'astra_coordinator'
                    self.engine.catalog[role] = copy.deepcopy(original['coordinator'])
                with self.assertRaises(m.Error): self.engine.settings(role, None)

    def test_disk_role_accepts_astra_but_rejects_unapproved_model(self):
        package = self.root / 'package'
        shutil.copytree(m.PACKAGE / 'roles', package / 'roles')
        path = package / 'roles' / 'architect.toml'
        path.write_text(path.read_text(encoding='utf-8').replace('gpt-6.1-sol', 'gpt-6-astra'), encoding='utf-8')
        with patch.object(m, 'PACKAGE', package):
            self.assertEqual(m.roles(self.root / 'home')['architect']['model'], 'gpt-6-astra')
            path.write_text(path.read_text(encoding='utf-8').replace('gpt-6-astra', 'gpt-6-luna'), encoding='utf-8')
            with self.assertRaises(m.Error): m.roles(self.root / 'home')

    def test_limit_role_is_terra_medium_and_rejects_catalog_tampering(self):
        package = self.root / 'watcher-package'
        shutil.copytree(m.PACKAGE / 'roles', package / 'roles')
        shutil.copytree(m.PACKAGE / 'private', package / 'private')
        with patch.object(m, 'PACKAGE', package):
            engine = m.Workflow(self.root, self.root / 'home')
        self.assertEqual(engine.settings('limit_watcher', None), {'model': 'gpt-6.1-terra', 'reasoning_effort': 'medium'})
        self.assertEqual(engine.settings('limit_watcher', 'medium')['reasoning_effort'], 'medium')
        with self.assertRaises(m.Error): engine.settings('limit_watcher', 'high')
        with self.assertRaises(m.Error): engine.settings('limit_watcher', 'low')
        engine.catalog['limit_watcher']['model'] = 'gpt-6.1-sol'
        with self.assertRaises(m.Error): engine.settings('limit_watcher', None)

    def test_direct_core_cli_cannot_bypass_pinned_candidate(self):
        command = [sys.executable, '-B', str(m.PACKAGE / 'scripts' / 'workflow.py'), 'native',
                   '--project', str(self.root), '--codex-home', str(self.root / 'home'), '--role', 'coordinator']
        target = self.root / 'coordinator.toml'
        result = subprocess.run(command + ['--output', str(target), '--available-model', 'gpt-6.1-sol'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'candidate requires scripts/candidate.py', result.stderr)
        self.assertFalse(target.exists())

if __name__ == '__main__': unittest.main()
