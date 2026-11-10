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
    def test_transient_windows_sharing_read_is_retried_without_changing_content(self):
        error = PermissionError('synthetic Windows sharing hold'); error.winerror = 32
        reader = m.shared_reader
        attempts = []
        def briefly_locked(path):
            attempts.append(path)
            if len(attempts)==1: raise error
            return reader(path)
        with patch.object(m, 'shared_reader', side_effect=briefly_locked):
            self.assertEqual(m.read(self.path), {'state':'old'})
        self.assertEqual(len(attempts),2)
        self.assertEqual(self.path.read_bytes(),self.old)

    def test_unclassified_read_permission_failure_is_not_retried(self):
        with patch.object(m, 'shared_reader', side_effect=PermissionError('not a sharing error')) as reader:
            with self.assertRaises(PermissionError): m.read(self.path)
        self.assertEqual(reader.call_count,1)

    def test_windows_replacement_missing_file_gap_is_reobserved(self):
        error=FileNotFoundError('synthetic Windows ReplaceFile gap');error.winerror=2
        real=m.shared_reader
        with patch.object(m,'shared_reader',side_effect=[error,real(self.path)]):
            self.assertEqual(m.read(self.path),{'state':'old'})
        self.assertEqual(self.path.read_bytes(),self.old)

    def test_unknown_identity_is_reobserved_without_starting_or_mutating(self):
        with patch.object(m,'marker_state',side_effect=[{'identityState':'unknown'},{'identityState':'matched'}]) as probe:
            self.assertEqual(m.observe_worker_identity(self.base)['identityState'],'matched')
        self.assertEqual(probe.call_count,2)
        self.assertEqual(self.path.read_bytes(),self.old)

    def test_permanent_unknown_identity_stays_unknown_after_bounded_wait(self):
        start=time.monotonic()
        with patch.object(m,'marker_state',return_value={'identityState':'unknown'}):
            self.assertEqual(m.observe_worker_identity(self.base,timeout=.02)['identityState'],'unknown')
        self.assertLess(time.monotonic()-start,.5)
        self.assertEqual(self.path.read_bytes(),self.old)

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
