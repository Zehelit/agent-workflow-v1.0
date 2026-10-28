"""Bounded model-free usage sampler. Never starts model sessions or resumes work."""
import argparse
import ctypes
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid

LEASE_SECONDS = 600
HEARTBEAT_SECONDS = 1
HEARTBEAT_MAX_AGE = 5
HISTORY_LIMIT = 20
USAGE_MAX_AGE = 300
USAGE_SOURCE = 'mcp__codex_app__get_usage_limits'
DIAGNOSTIC_CODES = {'monitor_validation', 'usage_receipt_expired', 'usage_receipt_invalid',
                    'usage_source_invalid', 'usage_bucket_missing', 'usage_windows_missing',
                    'usage_receipt_replay', 'usage_receipt_missing', 'usage_receipt_identity',
                    'usage_source_read_failed'}
DIAGNOSTIC_ERRORS = {'MonitorError', 'OSError', 'ValueError', 'RuntimeError', 'KeyError',
                     'TimeoutError', 'TypeError', 'FileNotFoundError', 'PermissionError',
                     'UnicodeDecodeError'}
SCHEMA_VERSION = 1
MAX_FINITE_INTEGER = int(sys.float_info.max)
MAX_PID = 0xffffffff if os.name == 'nt' else 0x7fffffff
WRITE_RETRY_SECONDS = 0.5
WRITE_RETRY_INTERVAL = 0.01
HEALTH_STATES = ('running', 'stopped')
HEALTH_REASONS = (None, 'explicit_stop', 'lease_expired', 'measurement_failure',
                  'usage_threshold', 'early_reserve', 'save_latched', 'worker_error')
_write_lock = threading.RLock()
_runtime = None


class MonitorError(RuntimeError):
    def __init__(self, message, code='monitor_validation'):
        super().__init__(message)
        self.code = code


def require(condition, message, code='monitor_validation'):
    if not condition:
        raise MonitorError(message, code)


def number(value):
    # math.isfinite converts integers to float and can itself raise OverflowError.
    if type(value) is int:
        return -MAX_FINITE_INTEGER <= value <= MAX_FINITE_INTEGER
    return type(value) is float and math.isfinite(value)


def write(path, value):
    with _write_lock:
        tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
        backup = path.with_name(path.name + '.bak-' + uuid.uuid4().hex)
        try:
            tmp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
            deadline = time.monotonic() + WRITE_RETRY_SECONDS
            while True:
                try:
                    if os.name == 'nt' and path.exists():
                        replace_existing_windows(path, tmp, backup)
                    else:
                        os.replace(tmp, path)
                    break
                except PermissionError as error:
                    # Windows readers can briefly deny replacement of the old file.
                    # Keep its complete bytes until replacement succeeds; permanent
                    # permissions and all other failures still surface promptly.
                    remaining = deadline - time.monotonic()
                    if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32, 33) or remaining <= 0:
                        raise
                    time.sleep(min(WRITE_RETRY_INTERVAL, remaining))
        finally:
            # Preserve both recovery files after an ambiguous native failure.
            if not backup.exists():
                tmp.unlink(missing_ok=True)


def replace_existing_windows(path, tmp, backup):
    """Replace an existing file with Windows ACL preservation and recovery bytes."""
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.ReplaceFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                 wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    api.ReplaceFileW.restype = wintypes.BOOL
    if api.ReplaceFileW(str(path), str(tmp), str(backup), 0, None, None):
        backup.unlink()
        return
    error = ctypes.WinError(ctypes.get_last_error())
    if backup.exists():
        if not path.exists():
            os.replace(backup, path)
        else:
            raise MonitorError('Windows replacement left a recovery backup') from error
    raise error


def strict_json(text):
    def duplicates(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    def finite_float(text):
        value = float(text)
        require(math.isfinite(value), 'nonfinite JSON')
        return value
    def finite_int(text):
        value = int(text)
        require(number(value), 'JSON integer outside finite numeric range')
        return value
    return json.loads(text, object_pairs_hook=duplicates, parse_float=finite_float, parse_int=finite_int,
                      parse_constant=lambda value: (_ for _ in ()).throw(MonitorError('nonfinite JSON')))


def read(path):
    with shared_reader(path) as handle:
        return strict_json(handle.read())


def shared_reader(path):
    """Open watcher state with FILE_SHARE_DELETE for concurrent publication."""
    if os.name != 'nt':
        return path.open('r', encoding='utf-8')
    import msvcrt
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except BaseException:
        api.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, 'r', encoding='utf-8')


def identity(pid):
    """Actual worker identity, not the Popen launcher PID. No process mutation."""
    require(type(pid) is int and 0 < pid <= MAX_PID, 'invalid PID')
    if os.name == 'nt':
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        api.GetProcessTimes.restype = wintypes.BOOL
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetExitCodeProcess.restype = wintypes.BOOL
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return {'state': 'absent' if ctypes.get_last_error() == 87 else 'unknown'}
        try:
            code = wintypes.DWORD()
            if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
                return {'state': 'unknown'}
            if code.value != 259:
                return {'state': 'absent'}
            times = [wintypes.FILETIME() for _ in range(4)]
            if not api.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return {'state': 'unknown'}
            start = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return {'state': 'live', 'pid': pid, 'startIdentity': 'windows-filetime:' + str(start)}
        finally:
            api.CloseHandle(handle)
    try:
        stat = Path('/proc', str(pid), 'stat').read_text()
        tail = stat[stat.rfind(')') + 2:].split()
        if tail[0] == 'Z':
            return {'state': 'absent'}
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return {'state': 'live', 'pid': pid, 'startIdentity': 'linux:' + boot + ':' + tail[19]}
    except FileNotFoundError:
        return {'state': 'absent'}
    except (OSError, IndexError):
        return {'state': 'unknown'}


def validate_windows(windows):
    require(type(windows) is dict, 'missing windows')
    result = {}
    for key in (300, 10080):
        window = windows.get(key, windows.get(str(key)))
        require(type(window) is dict, 'missing window')
        require(window.get('windowDurationMins') == key and type(window.get('windowDurationMins')) is int,
                'invalid window duration')
        require(number(window.get('usedPercent')) and 0 <= window['usedPercent'] <= 100, 'unknown usage')
        require(number(window.get('resetsAt')) and window['resetsAt'] > 0, 'unknown reset')
        # Persist only approved measurement fields, never account IDs or arbitrary server payloads.
        result[key] = {name: window[name] for name in ('windowDurationMins', 'usedPercent', 'resetsAt')}
    return result


class OwnedProcess:
    """Keep an OS ownership boundary for the Python worker and its descendants."""
    def __init__(self, argv, **kwargs):
        self.job = None
        self.api = None
        kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        if os.name == 'nt':
            # Assign before any user code can spawn a descendant (including a venv launcher).
            kwargs['creationflags'] |= 0x4  # CREATE_SUSPENDED
        if os.name != 'nt':
            kwargs['start_new_session'] = True
        self.proc = subprocess.Popen(argv, **kwargs)
        try:
            if os.name == 'nt':
                from ctypes import wintypes
                class Basic(ctypes.Structure):
                    _fields_ = [('perProcess', ctypes.c_int64), ('perJob', ctypes.c_int64),
                                ('flags', wintypes.DWORD), ('min', ctypes.c_size_t), ('max', ctypes.c_size_t),
                                ('active', wintypes.DWORD), ('affinity', ctypes.c_size_t),
                                ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
                class IO(ctypes.Structure):
                    _fields_ = [(name, ctypes.c_uint64) for name in ('readOps', 'writeOps', 'otherOps',
                                                                   'readBytes', 'writeBytes', 'otherBytes')]
                class Extended(ctypes.Structure):
                    _fields_ = [('basic', Basic), ('io', IO), ('processMemory', ctypes.c_size_t),
                                ('jobMemory', ctypes.c_size_t), ('peakProcess', ctypes.c_size_t),
                                ('peakJob', ctypes.c_size_t)]
                api = ctypes.WinDLL('kernel32', use_last_error=True)
                api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
                api.CreateJobObjectW.restype = wintypes.HANDLE
                api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
                api.SetInformationJobObject.restype = wintypes.BOOL
                api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
                api.AssignProcessToJobObject.restype = wintypes.BOOL
                api.CloseHandle.argtypes = [wintypes.HANDLE]
                self.api, self.job, self.limits = api, api.CreateJobObjectW(None, None), Extended()
                require(bool(self.job), 'owned process job unavailable')
                self.limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                require(api.SetInformationJobObject(self.job, 9, ctypes.byref(self.limits), ctypes.sizeof(self.limits)),
                        'owned process job limits unavailable')
                require(api.AssignProcessToJobObject(self.job, wintypes.HANDLE(int(self.proc._handle))),
                        'owned process job assignment unavailable')
                class ThreadEntry(ctypes.Structure):
                    _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('thread', wintypes.DWORD),
                                ('owner', wintypes.DWORD), ('basePriority', wintypes.LONG),
                                ('deltaPriority', wintypes.LONG), ('flags', wintypes.DWORD)]
                api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
                api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
                api.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
                api.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
                api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                api.OpenThread.restype = wintypes.HANDLE
                api.ResumeThread.argtypes = [wintypes.HANDLE]
                api.ResumeThread.restype = wintypes.DWORD
                snapshot = api.CreateToolhelp32Snapshot(4, 0)
                require(snapshot not in (None, ctypes.c_void_p(-1).value), 'owned process thread snapshot unavailable')
                resumed = False
                try:
                    entry = ThreadEntry()
                    entry.size = ctypes.sizeof(entry)
                    found = api.Thread32First(snapshot, ctypes.byref(entry))
                    while found:
                        if entry.owner == self.proc.pid:
                            thread = api.OpenThread(2, False, entry.thread)
                            require(bool(thread), 'owned process thread unavailable')
                            try:
                                require(api.ResumeThread(thread) == 1, 'owned process resume failed')
                                resumed = True
                            finally:
                                api.CloseHandle(thread)
                            break
                        found = api.Thread32Next(snapshot, ctypes.byref(entry))
                finally:
                    api.CloseHandle(snapshot)
                require(resumed, 'owned process initial thread unavailable')
        except Exception:
            self.close()
            raise

    def detach(self):
        """A verified persistent worker now owns its lease; do not terminate it."""
        if self.job:
            self.limits.basic.flags = 0
            require(self.api.SetInformationJobObject(self.job, 9, ctypes.byref(self.limits), ctypes.sizeof(self.limits)),
                    'worker ownership transfer failed')
            self.api.CloseHandle(self.job)
            self.job = None

    def close(self):
        if self.job:
            self.api.CloseHandle(self.job)
            self.job = None
        elif os.name != 'nt':
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


class UsageReading(dict):
    def __init__(self, windows, observed_at):
        super().__init__(windows)
        self.observed_at = observed_at


def validate_usage_time(observed_at, now):
    require(number(observed_at) and 0 <= now - observed_at <= USAGE_MAX_AGE,
            'native usage receipt is missing, future or stale', 'usage_receipt_expired')


def publish_usage(base, control, document, now=None):
    """Publish only approved fields from a coordinator-supplied native tool receipt."""
    now = time.time() if now is None else now
    validate_control(control)
    require(type(document) is dict and set(document) == {'source', 'sourceId', 'observedAt', 'result'},
            'invalid native receipt fields', 'usage_receipt_invalid')
    require(document['source'] == USAGE_SOURCE, 'native Codex usage tool required', 'usage_source_invalid')
    source_id = document['sourceId']
    require(type(source_id) is str and 0 < len(source_id.strip()) <= 200,
            'native tool receipt ID required', 'usage_receipt_invalid')
    validate_usage_time(document['observedAt'], now)
    require(document['observedAt'] > control.get('usageAfter', -1),
            'native usage must follow the stop observation', 'usage_receipt_replay')
    result = document['result']
    require(type(result) is dict, 'native usage result required', 'usage_receipt_invalid')
    buckets = result.get('rateLimitsByLimitId')
    if buckets is not None:
        require(type(buckets) is dict and type(buckets.get('codex')) is dict,
                'Codex usage bucket missing', 'usage_bucket_missing')
        rate = buckets['codex']
    else:
        rate = result.get('rateLimits')
    require(type(rate) is dict and rate.get('limitId', 'codex') == 'codex',
            'Codex usage bucket required', 'usage_bucket_missing')
    windows = {}
    for key in ('primary', 'secondary'):
        window = rate.get(key)
        require(type(window) is dict, 'both usage windows required', 'usage_windows_missing')
        duration = window.get('windowDurationMins')
        require(type(duration) is int and duration not in windows, 'invalid usage duration', 'usage_receipt_invalid')
        windows[duration] = window
    windows = validate_windows(windows)
    value = {'schemaVersion': SCHEMA_VERSION, 'runId': control['runId'], 'epoch': control['epoch'],
             'source': USAGE_SOURCE, 'sourceId': source_id, 'observedAt': document['observedAt'],
             'windows': {str(key): window for key, window in windows.items()}}
    path = base / 'usage-source.json'
    if path.exists():
        previous = read(path)
        require(type(previous) is dict and previous.get('runId') == control['runId'],
                'stored usage source belongs to another run or is invalid', 'usage_receipt_identity')
        if previous.get('runId') == control['runId']:
            if previous == value:
                return value
            require(previous.get('sourceId') != source_id and number(previous.get('observedAt')) and
                    value['observedAt'] > previous['observedAt'],
                    'native usage replay or rollback refused', 'usage_receipt_replay')
    write(path, value)
    return value


def ingest_usage_input(base, control, input_path, now=None):
    path = Path(input_path)
    require(path.is_file() and path.stat().st_size <= 1024 * 1024,
            'bounded native usage input required', 'usage_receipt_invalid')
    return publish_usage(base, control, read(path), now)


def measure(base=None, control=None, now=None):
    """Read an existing native receipt. Never launch or connect a Codex process."""
    require(base is not None and control is not None, 'native usage source required', 'usage_receipt_missing')
    now = time.time() if now is None else now
    path = base / 'usage-source.json'
    require(path.is_file(), 'native usage receipt missing', 'usage_receipt_missing')
    value = read(path)
    require(type(value) is dict and set(value) == {'schemaVersion', 'runId', 'epoch', 'source',
            'sourceId', 'observedAt', 'windows'} and type(value['schemaVersion']) is int and
            value['schemaVersion'] == SCHEMA_VERSION and value['source'] == USAGE_SOURCE,
            'invalid stored native receipt', 'usage_receipt_invalid')
    require(value['runId'] == control['runId'] and value['epoch'] == control['epoch'],
            'native usage run or epoch mismatch', 'usage_receipt_identity')
    require(type(value['sourceId']) is str and 0 < len(value['sourceId'].strip()) <= 200,
            'invalid stored receipt ID', 'usage_receipt_invalid')
    validate_usage_time(value['observedAt'], now)
    require(value['observedAt'] > control.get('usageAfter', -1),
            'native usage predates stop observation', 'usage_receipt_replay')
    require(type(value['windows']) is dict and set(value['windows']) == {'300', '10080'},
            'stored usage windows missing', 'usage_windows_missing')
    windows = validate_windows({int(key): window for key, window in value['windows'].items()})
    return UsageReading(windows, value['observedAt'])


def policy_card(control, now):
    exception = control.get('policyException')
    active = bool(exception and exception['runId'] == control['runId'] and
                  exception['validFrom'] <= now < exception['validUntil'])
    return {'runId': control['runId'], 'epoch': control['epoch'],
            'actualWindows': [300, 10080], 'decisionWindows': [300] if active else [300, 10080],
            'exception': exception, 'exceptionActive': active,
            'authorizationLifecycleChanged': False}


def validate_control(control):
    require(type(control) is dict and set(control) <= {
        'schemaVersion', 'active', 'expiresAt', 'runId', 'epoch', 'policyException',
        'continuationQuote', 'continuedAt', 'nativeTransition', 'usageAfter'
    }, 'invalid control fields')
    require(control.get('schemaVersion') == SCHEMA_VERSION and type(control.get('schemaVersion')) is int,
            'invalid control schema')
    require(type(control.get('active')) is bool, 'invalid active flag')
    require(number(control.get('expiresAt')) and control['expiresAt'] >= 0, 'invalid lease')
    if 'usageAfter' in control:
        require(number(control['usageAfter']) and control['usageAfter'] >= 0, 'invalid usage freshness floor')
    for key in ('runId', 'epoch'):
        require(type(control.get(key)) is str and 0 < len(control[key]) <= 200, 'invalid run identity')
    exception = control.get('policyException')
    if exception is not None:
        require(type(exception) is dict and set(exception) == {
            'runId', 'userQuote', 'validFrom', 'validUntil', 'decisionWindows'
        }, 'invalid exception fields')
        require(exception['runId'] == control['runId'], 'exception run mismatch')
        require(type(exception['userQuote']) is str and 0 < len(exception['userQuote'].strip()) <= 2000,
                'exact user quote required')
        require(type(exception['decisionWindows']) is list and exception['decisionWindows'] == [300] and
                type(exception['decisionWindows'][0]) is int, 'unsupported decision policy')
        require(number(exception['validFrom']) and number(exception['validUntil']) and
                exception['validFrom'] < exception['validUntil'], 'invalid exception validity')
    if 'continuationQuote' in control:
        require(type(control['continuationQuote']) is str and 0 < len(control['continuationQuote'].strip()) <= 2000,
                'invalid continuation quote')
        require(number(control.get('continuedAt')), 'invalid continuation time')
    require(('continuationQuote' in control) == ('continuedAt' in control), 'incomplete continuation record')
    if 'nativeTransition' in control:
        transition = control['nativeTransition']
        require(type(transition) is dict and set(transition) == {'updatedAt', 'sourceId'} and
                number(transition['updatedAt']) and transition['updatedAt'] >= 0 and
                type(transition['sourceId']) is str and bool(transition['sourceId'].strip()),
                'invalid native transition record')
    require(not ('continuationQuote' in control and 'nativeTransition' in control),
            'mixed continuation records')
    return control


def default_control(run_id=None, now=None):
    now = time.time() if now is None else now
    return {'schemaVersion': SCHEMA_VERSION, 'active': True, 'expiresAt': now + LEASE_SECONDS,
            'runId': run_id or uuid.uuid4().hex, 'epoch': uuid.uuid4().hex, 'policyException': None}


def load_control(base):
    return validate_control(read(base / 'control.json'))


def workflow_schedule(helper=None, helper_sha256=None):
    if _runtime is not None:
        _runtime.check_binding()
        helper = _runtime.paths["scripts/workflow.py"]
        helper_sha256 = _runtime.files["scripts/workflow.py"]
    require(helper is not None and helper_sha256 is not None, 'explicit pinned schedule helper required')
    workflow = Path(helper).resolve()
    if helper_sha256 is not None:
        import hashlib
        require(hashlib.sha256(workflow.read_bytes()).hexdigest() == helper_sha256, 'schedule helper pin mismatch')
    spec = importlib.util.spec_from_file_location('usage_workflow', workflow)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.usage_schedule


def sample(previous=None, control=None, measurer=None, now=None, schedule=None):
    """Retry unknown measurement once; policy never changes actual measurements."""
    if previous and previous.get('action') == 'SAVE_AND_STOP':
        return dict(previous)
    control = validate_control(control or default_control())
    measurer = measurer or measure
    schedule = schedule or workflow_schedule()
    error_type = None
    diagnostic_code = None
    for attempt in range(2):
        try:
            reading = measurer()
            windows = validate_windows(dict(reading) if isinstance(reading, UsageReading) else reading)
            current = time.time() if now is None else now
            at = getattr(reading, 'observed_at', current)
            validate_usage_time(at, current)
            card = policy_card(control, current)
            chosen = {key: windows[key] for key in card['decisionWindows']}
            prior = previous
            if prior and (not number(prior.get('at')) or not 0 < at - prior['at'] <= 600):
                prior = None
            if prior and prior.get('policy', {}).get('decisionWindows') != card['decisionWindows']:
                prior = None
            result = schedule(chosen, at, prior)
            require(result['action'] == 'SAVE_AND_STOP' or current < result['next_check_at'],
                    'native usage receipt passed its adaptive deadline', 'usage_receipt_expired')
            result['windows'] = {str(key): value for key, value in windows.items()}
            result['policy'] = card
            result['measurementAttempts'] = attempt + 1
            result['exitReason'] = ('early_reserve' if result['action'] == 'SAVE_AND_STOP' and
                                    result['remaining'] > 20 else 'usage_threshold' if
                                    result['action'] == 'SAVE_AND_STOP' else None)
            result['errorType'] = None
            result['diagnosticCode'] = None
            return result
        except (OSError, ValueError, RuntimeError, KeyError, TimeoutError, TypeError) as error:
            name = type(error).__name__
            error_type = name if name in DIAGNOSTIC_ERRORS else 'RuntimeError'
            code = error.code if isinstance(error, MonitorError) else None
            diagnostic_code = code if type(code) is str and code in DIAGNOSTIC_CODES else 'usage_source_read_failed'
    at = time.time() if now is None else now
    return {'at': at, 'action': 'SAVE_AND_STOP', 'reason': 'usage unavailable after retry',
            'exitReason': 'measurement_failure', 'errorType': error_type, 'measurementAttempts': 2,
            'diagnosticCode': diagnostic_code,
            'policy': policy_card(control, at)}


def measurement_cycle(base, control, previous, schedule, now=None):
    """One integrity check and one bounded receipt read, independent of heartbeat."""
    if _runtime is not None:
        _runtime.check_binding()
    result = sample(previous, control, lambda: measure(base, control, now), now, schedule)
    latch(base, result, control)
    write(base / 'latest.json', result)
    if result.get('errorType'):
        path = base / 'diagnostics.json'
        try:
            old = read(path) if path.exists() else {}
            events = old.get('events', []) if type(old) is dict else []
        except (OSError, ValueError, RuntimeError):
            events = []
        events = events if type(events) is list else []
        # Allowlisted codes and classes only: never persist exception text or tool bodies.
        events = [value for value in events if type(value) is dict and set(value) == {
            'at', 'stage', 'code', 'errorType', 'attempts'} and number(value['at']) and
            value['stage'] == 'native_usage_read' and type(value['code']) is str and
            value['code'] in DIAGNOSTIC_CODES and type(value['errorType']) is str and
            value['errorType'] in DIAGNOSTIC_ERRORS and type(value['attempts']) is int and
            value['attempts'] in (1, 2)][-19:]
        events.append({'at': result['at'], 'stage': 'native_usage_read',
                       'code': result['diagnosticCode'], 'errorType': result['errorType'],
                       'attempts': result['measurementAttempts']})
        write(path, {'schemaVersion': SCHEMA_VERSION, 'events': events})
    return result


def heartbeat_once(base, worker):
    current = load_control(base)
    require(current['runId'] == worker['runId'] and current['epoch'] == worker['epoch'],
            'heartbeat ownership changed')
    event(base, worker, 'running', control=current)


def usage_token(base):
    try:
        stat = (base / 'usage-source.json').stat()
        return stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return None


def latch(base, result, control):
    if result.get('action') == 'SAVE_AND_STOP':
        write(base / 'save-latch.json', {'schemaVersion': SCHEMA_VERSION, 'runId': control['runId'],
                                       'epoch': control['epoch'], 'result': result})


def latched_result(base, control=None):
    path = base / 'save-latch.json'
    if path.exists():
        saved = read(path)
        require(type(saved) is dict and type(saved.get('result')) is dict and
                saved.get('schemaVersion') == SCHEMA_VERSION and type(saved.get('runId')) is str and
                type(saved.get('epoch')) is str and saved['result'].get('action') == 'SAVE_AND_STOP' and
                number(saved['result'].get('at')), 'invalid SAVE latch')
        if control is None or saved.get('epoch') == control['epoch']:
            return saved['result']
        # An epoch change alone is never authorization to bypass a prior latch.
        continued = (control.get('continuedAt') if control.get('continuationQuote') else
                     control.get('nativeTransition', {}).get('updatedAt'))
        require(number(continued) and continued > saved['result'].get('at', 0),
                'SAVE requires a newer continuation transition')
        require(continued <= time.time(), 'continuation cannot be in future')
    latest = base / 'latest.json'
    if latest.exists():
        result = read(latest)
        require(type(result) is dict, 'invalid latest sample')
        if result.get('action') == 'SAVE_AND_STOP':
            epoch = result.get('policy', {}).get('epoch')
            if control is None or epoch == control['epoch'] or not (control.get('continuationQuote') or
                                                                      control.get('nativeTransition')):
                return result
            continued = (control.get('continuedAt') if control.get('continuationQuote') else
                         control['nativeTransition']['updatedAt'])
            require(continued > result.get('at', 0), 'SAVE requires a newer continuation transition')
    return None


def read_health(base):
    health = read(base / 'health.json')
    require(type(health) is dict, 'invalid health object')
    require(type(health.get('schemaVersion')) is int and health['schemaVersion'] == SCHEMA_VERSION,
            'invalid health schema')
    fields = {'schemaVersion', 'pid', 'startIdentity', 'workerToken', 'runId', 'epoch',
              'state', 'heartbeatAt', 'leaseExpiresAt', 'exitReason', 'errorType'}
    require(fields <= set(health) <= fields | {'history'}, 'invalid health fields')
    require(type(health.get('pid')) is int and 0 < health['pid'] <= MAX_PID, 'invalid health PID')
    for key in ('startIdentity', 'workerToken', 'runId', 'epoch'):
        require(type(health.get(key)) is str and bool(health[key]), 'invalid health identity')
    require(type(health.get('state')) is str and health['state'] in HEALTH_STATES, 'invalid health state')
    for key in ('heartbeatAt', 'leaseExpiresAt'):
        require(number(health.get(key)) and health[key] >= 0, 'invalid health time')
    require(health.get('exitReason') in HEALTH_REASONS, 'invalid health reason')
    require(health.get('errorType') is None or type(health.get('errorType')) is str,
            'invalid health error type')
    if 'history' in health:
        history = health['history']
        require(type(history) is list and len(history) <= HISTORY_LIMIT, 'invalid health history')
        for entry in history:
            require(type(entry) is dict and set(entry) == {'at', 'state', 'exitReason', 'errorType'},
                    'invalid health history entry')
            require(number(entry['at']) and entry['at'] >= 0, 'invalid health history time')
            require(type(entry['state']) is str and entry['state'] in HEALTH_STATES,
                    'invalid health history state')
            require(entry['exitReason'] in HEALTH_REASONS, 'invalid health history reason')
            require(entry['errorType'] is None or type(entry['errorType']) is str,
                    'invalid health history error type')
    return health


def marker_state(base, now=None):
    now = time.time() if now is None else now
    marker_path = base / 'running.json'
    try:
        health = read_health(base) if (base / 'health.json').exists() else {}
        if not marker_path.exists():
            return {'identityState': 'absent', 'liveIdentity': False, 'heartbeatFresh': False}
        marker = read(marker_path)
        require(type(marker) is dict and type(marker.get('pid')) is int and
                type(marker.get('startIdentity')) is str and type(marker.get('workerToken')) is str,
                'invalid worker marker')
        actual = identity(marker['pid'])
        matched = actual['state'] == 'live' and actual['startIdentity'] == marker['startIdentity']
        state = 'matched' if matched else 'pid_reused' if actual['state'] == 'live' else actual['state']
        heartbeat = health.get('heartbeatAt')
        fresh = bool(matched and health.get('workerToken') == marker['workerToken'] and
                     health.get('pid') == marker['pid'] and health.get('startIdentity') == marker['startIdentity'] and
                     health.get('runId') == marker.get('runId') and health.get('epoch') == marker.get('epoch') and
                     health.get('state') == 'running' and number(heartbeat) and 0 <= now - heartbeat <= HEARTBEAT_MAX_AGE)
        return {'identityState': state, 'liveIdentity': matched, 'heartbeatFresh': fresh, 'marker': marker}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return {'identityState': 'unknown', 'liveIdentity': False, 'heartbeatFresh': False}


def status(base, now=None):
    now = time.time() if now is None else now
    control = load_control(base) if (base / 'control.json').exists() else None
    worker = marker_state(base, now)
    result = latched_result(base, control)
    if result is None:
        result = read(base / 'latest.json') if (base / 'latest.json').exists() else {'action': 'READ_USAGE'}
        require(type(result) is dict, 'invalid latest sample')
        result = dict(result)
    at = result.get('at')
    due = result.get('next_check_at')
    same_epoch = bool(control and result.get('policy', {}).get('epoch') == control['epoch'] and
                      result.get('policy', {}).get('runId') == control['runId'])
    same_policy = bool(control and result.get('policy', {}).get('decisionWindows') ==
                       policy_card(control, now)['decisionWindows'])
    sample_fresh = bool(same_epoch and same_policy and number(at) and number(due) and
                        0 <= now - at <= 300 and now < due)
    if not sample_fresh and result.get('action') != 'SAVE_AND_STOP':
        result['action'] = 'READ_USAGE'
    if worker['identityState'] == 'unknown' and result.get('action') != 'SAVE_AND_STOP':
        result['action'] = 'HOLD'
    lease_valid = bool(control and control['active'] and now < control['expiresAt'] <= now + LEASE_SECONDS)
    worker_epoch = bool(control and worker.get('marker', {}).get('epoch') == control['epoch'] and
                        worker.get('marker', {}).get('runId') == control['runId'])
    result['health'] = {key: worker[key] for key in ('identityState', 'liveIdentity', 'heartbeatFresh')}
    result['health'].update({'leaseValid': lease_valid, 'sampleFresh': sample_fresh,
                            'workerEpochMatches': worker_epoch,
                            'healthy': worker['liveIdentity'] and worker['heartbeatFresh'] and worker_epoch and lease_valid and
                            sample_fresh and result.get('action') != 'SAVE_AND_STOP'})
    if (base / 'health.json').exists():
        try:
            health = read_health(base)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
            result['health'].update(identityState='unknown', liveIdentity=False,
                                   heartbeatFresh=False, healthy=False, errorType=type(error).__name__)
            if result.get('action') != 'SAVE_AND_STOP':
                result['action'] = 'HOLD'
        else:
            for key in ('exitReason', 'errorType', 'pid', 'startIdentity'):
                result['health'][key] = health.get(key)
    return result


def event(base, worker, state, reason=None, error_type=None, control=None):
    with _write_lock:
        old = read(base / 'health.json') if (base / 'health.json').exists() else {}
        require(type(old) is dict, 'invalid health object')
        history = old.get('history', [])
        if type(history) is not list:
            history = []
        history = [{key: item.get(key) for key in ('at', 'state', 'exitReason', 'errorType')}
                   for item in history[-HISTORY_LIMIT:] if type(item) is dict and
                   number(item.get('at')) and item.get('state') in ('running', 'stopped') and
                   item.get('exitReason') in (None, 'explicit_stop', 'lease_expired', 'measurement_failure',
                                              'usage_threshold', 'early_reserve', 'save_latched', 'worker_error') and
                   (item.get('errorType') is None or item.get('errorType') in
                    ('MonitorError', 'OSError', 'ValueError', 'RuntimeError', 'KeyError', 'Empty', 'TimeoutError',
                     'TypeError', 'BrokenPipeError', 'FileNotFoundError', 'PermissionError', 'UnicodeDecodeError'))]
        now = time.time()
        if state != old.get('state') or reason != old.get('exitReason') or error_type != old.get('errorType'):
            history = history[-(HISTORY_LIMIT - 1):] + [{'at': now, 'state': state,
                                                      'exitReason': reason, 'errorType': error_type}]
        value = {'schemaVersion': SCHEMA_VERSION, **worker, 'state': state, 'heartbeatAt': now,
                 'leaseExpiresAt': control.get('expiresAt') if control else None,
                 'exitReason': reason, 'errorType': error_type, 'history': history[-HISTORY_LIMIT:]}
        write(base / 'health.json', value)


def run(base, expected_epoch=None, worker_token=None):
    if _runtime is not None:
        _runtime.check_binding()
    control = load_control(base)
    if expected_epoch is not None:
        require(control['epoch'] == expected_epoch, 'worker epoch changed')
    saved = latched_result(base, control)
    if saved:
        return saved
    actual = identity(os.getpid())
    require(actual['state'] == 'live', 'worker identity unverifiable')
    worker = {'pid': actual['pid'], 'startIdentity': actual['startIdentity'],
              'workerToken': worker_token or uuid.uuid4().hex, 'runId': control['runId'], 'epoch': control['epoch']}
    lock = base / 'running.json'
    try:
        with lock.open('x', encoding='utf-8') as handle:
            json.dump(worker, handle)
    except FileExistsError:
        raise MonitorError('worker marker already claimed')
    terminal = 'worker_error'
    error_type = None
    stopped = threading.Event()
    def heartbeat():
        while not stopped.wait(HEARTBEAT_SECONDS):
            try:
                heartbeat_once(base, worker)
            except (OSError, ValueError, RuntimeError):
                # Main loop owns terminal reporting; a missed heartbeat is unhealthy.
                break
    pulse = threading.Thread(target=heartbeat, daemon=True)
    try:
        schedule = workflow_schedule()
        event(base, worker, 'running', control=control)
        pulse.start()
        previous = read(base / 'latest.json') if (base / 'latest.json').exists() else None
        if previous and previous.get('policy', {}).get('epoch') != control['epoch']:
            previous = None
        due = previous.get('next_check_at', 0) if previous and number(previous.get('at')) and 0 <= time.time() - previous['at'] <= 300 else 0
        last_source = usage_token(base)
        while True:
            control = load_control(base)
            require(control['epoch'] == worker['epoch'] and control['runId'] == worker['runId'], 'worker ownership changed')
            if not control['active']:
                terminal = 'explicit_stop'
                break
            if time.time() >= control['expiresAt']:
                terminal = 'lease_expired'
                break
            require(control['expiresAt'] <= time.time() + LEASE_SECONDS, 'lease exceeds bounded duration')
            saved = latched_result(base, control)
            if saved:
                terminal, error_type = saved.get('exitReason', 'save_latched'), saved.get('errorType')
                break
            if previous and previous.get('policy', {}).get('decisionWindows') != policy_card(control, time.time())['decisionWindows']:
                due = 0
            source = usage_token(base)
            if time.time() >= due or source != last_source:
                previous = measurement_cycle(base, control, previous, schedule)
                last_source = source
                if previous['action'] == 'SAVE_AND_STOP':
                    terminal, error_type = previous['exitReason'], previous['errorType']
                    break
                due = previous['next_check_at']
            stopped.wait(0.2)
    except Exception as error:
        error_type = type(error).__name__
        terminal = 'worker_error'
        raise
    finally:
        stopped.set()
        if pulse.is_alive():
            pulse.join(timeout=2)
        event(base, worker, 'stopped', terminal, error_type, control)
        # Never remove another worker's marker.
        if lock.exists() and read(lock).get('workerToken') == worker['workerToken']:
            lock.unlink()
    return {'exitReason': terminal, 'errorType': error_type}


def claim_start(base):
    """Serialize launch decisions; unknown or live identity always holds."""
    worker = marker_state(base)
    require(worker['identityState'] not in ('matched', 'unknown'), 'existing worker live or identity unverifiable')
    if (base / 'running.json').exists():
        # Absent/reused identity was read and verified. No process is killed.
        (base / 'running.json').unlink()


def configure(base, args, now):
    existing = load_control(base) if (base / 'control.json').exists() else None
    saved = latched_result(base, existing)
    if args.new_epoch:
        native = args.native_transition_at is not None or args.native_receipt is not None
        require(args.run_id and (bool(args.continuation_quote and args.continuation_quote.strip()) != native),
                'new epoch requires exactly one direct quote or native transition')
        if native:
            require(number(args.native_transition_at) and args.native_receipt and args.native_receipt.strip()
                    and args.native_transition_at <= now, 'complete, past native transition required')
        require(marker_state(base)['identityState'] not in ('matched', 'unknown'),
                'stop existing worker before new epoch')
        continued = args.native_transition_at if native else now
        require(not saved or continued > saved.get('at', 0), 'continuation must follow SAVE')
        control = default_control(args.run_id, now)
        if saved:
            control['usageAfter'] = saved['at']
        if native:
            control['nativeTransition'] = {'updatedAt': args.native_transition_at,
                                           'sourceId': args.native_receipt}
        else:
            control.update({'continuationQuote': args.continuation_quote, 'continuedAt': now})
    else:
        require(not (args.continuation_quote or args.native_transition_at is not None or args.native_receipt),
                'continuation evidence requires explicit new epoch')
        if saved:
            return None, saved
        control = existing or default_control(args.run_id, now)
        require(not args.run_id or args.run_id == control['runId'], 'run ID mismatch; explicit new epoch required')
    require(not (args.clear_exception and args.ignore_weekly_quote), 'conflicting exception options')
    if args.clear_exception:
        control['policyException'] = None
    if args.ignore_weekly_quote is not None:
        require(args.run_id == control['runId'] and args.ignore_weekly_quote.strip() and
                number(args.exception_expires_at) and now < args.exception_expires_at,
                'weekly exception requires matching run ID, exact quote and future expiry')
        control['policyException'] = {'runId': control['runId'], 'userQuote': args.ignore_weekly_quote,
                                      'validFrom': now, 'validUntil': args.exception_expires_at,
                                      'decisionWindows': [300]}
    else:
        require(args.exception_expires_at is None, 'exception expiry requires user quote')
    control['active'], control['expiresAt'] = True, now + LEASE_SECONDS
    validate_control(control)
    write(base / 'control.json', control)
    return control, None


def main(argv=None, runtime=None):
    global _runtime
    _runtime = runtime
    if runtime is None and (Path(__file__).resolve().parents[3] / 'candidate-profile.json').exists():
        print(json.dumps({'action': 'HOLD', 'reason': 'private candidate watcher requires candidate.py'}))
        return 2
    if runtime is not None:
        runtime.check_binding()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start', 'renew', 'stop', 'status', 'once', 'run', 'ingest'])
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--run-id')
    parser.add_argument('--new-epoch', action='store_true')
    parser.add_argument('--continuation-quote')
    parser.add_argument('--native-transition-at', type=float)
    parser.add_argument('--native-receipt')
    parser.add_argument('--ignore-weekly-quote')
    parser.add_argument('--exception-expires-at', type=float)
    parser.add_argument('--clear-exception', action='store_true')
    parser.add_argument('--usage-input', help='Native get_usage_limits receipt; does not invoke Codex')
    parser.add_argument('--worker-epoch', help=argparse.SUPPRESS)
    parser.add_argument('--worker-token', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if runtime is not None and args.command in ('start', 'renew', 'once', 'ingest'):
        require(args.run_id is None, 'candidate owns run ID')
        args.run_id = runtime.args.run_id
    base = Path(args.state_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    try:
        require(args.command == 'run' or not (args.worker_epoch or args.worker_token), 'worker flags require run')
        require(args.command not in ('status', 'stop', 'run') or not args.usage_input,
                'usage input unsupported for command')
        if args.command == 'ingest':
            require(args.usage_input and not (args.new_epoch or args.continuation_quote or
                    args.native_transition_at is not None or args.native_receipt or
                    args.ignore_weekly_quote or args.exception_expires_at or args.clear_exception),
                    'ingest requires only a native usage input')
            control = load_control(base) if (base / 'control.json').exists() else default_control(args.run_id)
            require(not args.run_id or args.run_id == control['runId'], 'usage input run mismatch')
            saved = latched_result(base, control)
            require(not saved, 'SAVE remains latched; resume requires fresh authorization')
            value = ingest_usage_input(base, control, args.usage_input)
            if not (base / 'control.json').exists():
                control.update(active=False, expiresAt=0)
                write(base / 'control.json', control)
            print(json.dumps({'published': True, 'source': USAGE_SOURCE, 'observedAt': value['observedAt'],
                              'activated': False, 'runId': control['runId'], 'epoch': control['epoch']}))
            return 0
        if args.command in ('status', 'stop', 'run'):
            require(not (args.run_id or args.new_epoch or args.continuation_quote or args.native_transition_at is not None or
                         args.native_receipt or args.ignore_weekly_quote or
                         args.exception_expires_at or args.clear_exception), 'policy flags unsupported for command')
        if args.command == 'status':
            result = status(base)
        elif args.command == 'stop':
            run_id = runtime.args.run_id if runtime is not None else None
            control = load_control(base) if (base / 'control.json').exists() else default_control(run_id)
            control['active'], control['expiresAt'] = False, 0
            write(base / 'control.json', control)
            result = {'stopRequested': True, 'saveLatched': latched_result(base, control) is not None,
                      'worker': marker_state(base)['identityState']}
        elif args.command == 'run':
            result = run(base, args.worker_epoch, args.worker_token)
        else:
            # Atomic launcher claim prevents concurrent starts before the worker marker exists.
            gate = base / 'launching.lock'
            try:
                with gate.open('x', encoding='utf-8') as handle:
                    json.dump(identity(os.getpid()), handle)
            except FileExistsError:
                raise MonitorError('launch operation already claimed; inspect owner before recovery')
            try:
                if args.command in ('start', 'once'):
                    worker = marker_state(base)
                    if worker['identityState'] == 'matched' and args.command == 'start' and not args.new_epoch:
                        # Start is idempotent for a verified worker, but cannot mutate its policy.
                        require(not (args.ignore_weekly_quote or args.clear_exception or (args.run_id and runtime is None)),
                                'use renew for live-worker policy changes')
                        result = status(base)
                        print(json.dumps(result, ensure_ascii=False))
                        return 0
                    claim_start(base)
                elif args.command == 'renew':
                    require(marker_state(base)['identityState'] != 'unknown', 'worker identity unverifiable')
                control, saved = configure(base, args, time.time())
                if args.usage_input and not saved:
                    ingest_usage_input(base, control, args.usage_input)
                if saved:
                    result = saved
                elif args.command == 'once':
                    previous = read(base / 'latest.json') if (base / 'latest.json').exists() else None
                    if previous and previous.get('policy', {}).get('epoch') != control['epoch']:
                        previous = None
                    result = measurement_cycle(base, control, previous, workflow_schedule())
                elif args.command == 'renew':
                    result = {'requested': 'renew', 'leaseSeconds': LEASE_SECONDS, **status(base)}
                else:
                    # Refuse before starting a worker if no current native receipt exists.
                    reading = measure(base, control)
                    preflight = sample(control=control, measurer=lambda: reading, schedule=workflow_schedule())
                    if preflight['action'] == 'SAVE_AND_STOP':
                        latch(base, preflight, control)
                        write(base / 'latest.json', preflight)
                    require(preflight['action'] != 'SAVE_AND_STOP', 'native usage preflight requested SAVE_AND_STOP')
                    worker_token = uuid.uuid4().hex
                    worker_argv = runtime.worker_prefix() + ['run'] if runtime is not None else [sys.executable, '-B', str(Path(__file__).resolve()), 'run', '--state-dir', str(base)]
                    worker_argv += ['--worker-epoch', control['epoch'], '--worker-token', worker_token]
                    # Keep a local traceback for an unexpected worker exit. The
                    # health receipt intentionally stores only the error class.
                    error_log = (base / 'worker-stderr.log').open('ab', buffering=0)
                    try:
                        owned = OwnedProcess(worker_argv, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=error_log)
                    finally:
                        error_log.close()
                    try:
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            live = marker_state(base)
                            if live['liveIdentity'] and live['heartbeatFresh'] and live['marker']['workerToken'] == worker_token:
                                break
                            if ((base / 'health.json').exists() and read_health(base).get('state') == 'stopped' and
                                    read_health(base).get('workerToken') == worker_token):
                                break
                            time.sleep(0.05)
                        live = marker_state(base)
                        terminal = read_health(base) if (base / 'health.json').exists() else {}
                        require((live['liveIdentity'] and live['heartbeatFresh'] and
                                 live.get('marker', {}).get('epoch') == control['epoch']) or
                                (terminal.get('state') == 'stopped' and terminal.get('epoch') == control['epoch'] and
                                 terminal.get('workerToken') == worker_token),
                                'worker startup identity or terminal receipt unavailable')
                        owned.detach()
                    except Exception:
                        control['active'], control['expiresAt'] = False, 0
                        write(base / 'control.json', control)
                        owned.close()
                        raise
                    result = {'requested': 'start', 'leaseSeconds': LEASE_SECONDS, **status(base)}
            finally:
                gate.unlink(missing_ok=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        if args.command == 'start' and type(locals().get('control')) is dict:
            try:
                if marker_state(base)['identityState'] == 'absent':
                    current = load_control(base)
                    if (current['runId'], current['epoch']) == (control['runId'], control['epoch']):
                        current.update(active=False, expiresAt=0)
                        write(base / 'control.json', current)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                pass  # Preserve the original failure; never touch an unknown/live owner.
        if args.command == 'run':
            traceback.print_exc(file=sys.stderr)
        # Keep operation diagnostics without recording arbitrary tool/exception bodies.
        code = error.code if isinstance(error, MonitorError) else None
        code = code if type(code) is str and code in DIAGNOSTIC_CODES else 'usage_source_read_failed'
        print(json.dumps({'action': 'HOLD', 'errorType': type(error).__name__, 'diagnosticCode': code, 'reason': str(error)
                          if isinstance(error, MonitorError) else 'monitor operation failed'}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
