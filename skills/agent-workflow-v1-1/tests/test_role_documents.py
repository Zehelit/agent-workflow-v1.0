"""Role skills and memories reach the assigned child without crossing role scope."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from test_workflow import authorization

spec = importlib.util.spec_from_file_location('role_document_workflow', Path(__file__).resolve().parents[1] / 'scripts/workflow.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class RoleDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'project').mkdir()
        self.package = self.root / 'package'
        shutil.copytree(m.PACKAGE / 'roles', self.package / 'roles')
        shutil.copytree(m.PACKAGE / 'private', self.package / 'private')
        self.patch = patch.object(m, 'PACKAGE', self.package)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.paths = {name: self.package / 'roles' / name for name in
                      ('coordinator', 'architect', 'implementer', 'researcher', 'verifier')}
        self.paths['limit_watcher'] = self.package / 'private/limit-watcher'
        for name, directory in self.paths.items():
            (directory / 'SKILL.md').write_text('skill-sentinel-' + name, encoding='utf-8')
            (directory / 'MEMORY.md').write_text('memory-sentinel-' + name, encoding='utf-8')

    def engine(self):
        return m.Workflow(self.root / 'project', self.root / 'home', lambda: 10000)

    def test_each_role_receives_only_its_own_skill_and_memory(self):
        engine = self.engine()
        for name in self.paths:
            with self.subTest(role=name):
                message = engine.catalog[name]['developer_instructions']
                self.assertIn('skill-sentinel-' + name, message)
                self.assertIn('memory-sentinel-' + name, message)
                for other in self.paths.keys() - {name}:
                    self.assertNotIn('skill-sentinel-' + other, message)
                    self.assertNotIn('memory-sentinel-' + other, message)

    def test_prepared_child_message_includes_persistent_role_notes(self):
        memory = self.paths['implementer'] / 'MEMORY.md'
        memory.write_text('Verified project fact: use the existing parser.', encoding='utf-8')
        engine = self.engine()
        engine.execute('activate', dict(objective='test', **authorization(10000),
            models={'gpt-6.1-sol': ['high']}, slot_limit=4, inventory=[], observed_at=10000))
        engine.execute('usage', dict(observed_at=10000, rateLimits={
            'primary': {'usedPercent': 10, 'windowDurationMins': 300},
            'secondary': {'usedPercent': 10, 'windowDurationMins': 10080}}))
        engine.execute('task', dict(id='frontend', role='implementer', objective='deliver', criteria=['works'], writes=[]))
        prepared = engine.execute('prepare', dict(id='frontend'))['arguments']
        self.assertEqual(prepared['fork_turns'], 'none')
        self.assertIn('skill-sentinel-implementer', prepared['message'])
        self.assertIn('Verified project fact: use the existing parser.', prepared['message'])
        self.assertIn('Verified project fact: use the existing parser.', self.engine().catalog['implementer']['developer_instructions'])

    def test_missing_or_empty_document_refuses_role_loading(self):
        for name in self.paths:
            for filename in ('SKILL.md', 'MEMORY.md'):
                path = self.paths[name] / filename
                original = path.read_bytes()
                for content in (None, b' \n'):
                    with self.subTest(role=name, file=filename, missing=content is None):
                        if content is None:
                            path.unlink()
                        else:
                            path.write_bytes(content)
                        with self.assertRaises(m.Error):
                            self.engine()
                        path.write_bytes(original)

    def test_role_cannot_reuse_another_roles_memory(self):
        path = self.package / 'roles/implementer.toml'
        path.write_text(path.read_text(encoding='utf-8').replace(
            'implementer/MEMORY.md', 'researcher/MEMORY.md'), encoding='utf-8')
        with self.assertRaises(m.Error):
            self.engine()


if __name__ == '__main__':
    unittest.main()
