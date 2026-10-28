"""Atomic publication under actual Windows reader sharing and bounded failures."""
import importlib.util
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]/'private/limit-watcher/scripts/monitor.py'
spec = importlib.util.spec_from_file_location('monitor_atomic_tests', SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='watcher-atomic-')
        self.base = Path(self.temp.name).resolve()
        self.path = self.base/'health.json'
        self.path.write_text('{"state":"old"}', encoding='utf-8')
        self.old = self.path.read_bytes()

    def tearDown(self):
        if Path(self.temp.name).resolve() != self.base:
            raise RuntimeError('Disposable cleanup boundary changed')
        self.temp.cleanup()

    def assert_no_temp(self):
        self.assertEqual(list(self.base.glob('health.json.tmp-*')), [])

    def test_complete_unicode_publication(self):
        payload = {'state':'running', 'label':'Türkçe ı'}
        m.write(self.path, payload)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), payload)
        self.assert_no_temp()

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics')
    def test_actual_reader_releases_then_atomic_replace_succeeds(self):
        handle = self.path.open('r', encoding='utf-8')
        observed = []
        def release():
            time.sleep(0.1)
            observed.append(self.path.read_bytes())
            handle.close()
        reader = threading.Thread(target=release)
        reader.start()
        try:
            m.write(self.path, {'state':'new'})
        finally:
            reader.join(timeout=2)
            handle.close()
        self.assertFalse(reader.is_alive())
        self.assertEqual(observed, [self.old])
        self.assertEqual(json.loads(self.path.read_text()), {'state':'new'})
        self.assert_no_temp()

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics')
    def test_internal_reader_can_overlap_atomic_replace(self):
        with m.shared_reader(self.path) as handle:
            self.assertEqual(handle.read(), self.old.decode('utf-8'))
            m.write(self.path, {'state':'new'})
            handle.seek(0)
            self.assertEqual(handle.read(), self.old.decode('utf-8'))
        self.assertEqual(json.loads(self.path.read_text()), {'state':'new'})
        self.assertEqual(list(self.base.glob('health.json.bak-*')), [])
        self.assert_no_temp()

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics')
    def test_actual_permanent_reader_fails_bounded_and_preserves_old_bytes(self):
        started = time.monotonic()
        with self.path.open('r'):
            with self.assertRaises(PermissionError):
                m.write(self.path, {'state':'new'})
        duration = time.monotonic()-started
        self.assertGreaterEqual(duration, m.WRITE_RETRY_SECONDS*0.9)
        self.assertLess(duration, 2)
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assert_no_temp()

    def test_unclassified_permission_error_is_not_retried(self):
        error = PermissionError('not a known Windows sharing error')
        target, name = (m, 'replace_existing_windows') if os.name == 'nt' else (m.os, 'replace')
        with patch.object(target, name, side_effect=error) as replace:
            with self.assertRaises(PermissionError):
                m.write(self.path, {'state':'new'})
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assert_no_temp()

    def test_nonpermission_io_error_is_not_retried(self):
        target, name = (m, 'replace_existing_windows') if os.name == 'nt' else (m.os, 'replace')
        with patch.object(target, name, side_effect=OSError('disk unavailable')) as replace:
            with self.assertRaises(OSError):
                m.write(self.path, {'state':'new'})
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assert_no_temp()

    def test_invalid_json_never_replaces_last_good_file(self):
        with self.assertRaises(ValueError):
            m.write(self.path, {'value':math.nan})
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assert_no_temp()


if __name__ == '__main__':
    unittest.main(verbosity=2)
