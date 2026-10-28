"""Bounded model-free usage sampler. Never starts model sessions or resumes work."""
import argparse
import ctypes
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import signal
import shutil
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
RPC_TIMEOUT = 20
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
    pass


def require(condition, message):
    if not condition:
        raise MonitorError(message)


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
    """Keep an OS ownership boundary for CLI descendants, without PID-based killing."""
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


def measure():
    exe = shutil.which('codex')
    require(exe is not None, 'codex CLI unavailable')
    owned = OwnedProcess([exe, 'app-server', '--stdio'], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            encoding='utf-8', errors='strict')
    proc = owned.proc
    messages = queue.Queue()
    def receive():
        try:
            for line in proc.stdout:
                try:
                    value = strict_json(line)
                    require(type(value) is dict, 'invalid RPC frame')
                    messages.put(value)
                except (ValueError, MonitorError) as error:
                    # Only our fixed validation messages are safe to forward.
                    messages.put({'readerFailed': str(error) if isinstance(error, MonitorError)
                                  else 'invalid RPC JSON'})
                    break
        except (OSError, UnicodeError):
            messages.put({'readerFailed': True})
        finally:
            messages.put({'readerClosed': True})
    reader = threading.Thread(target=receive, daemon=True)
    reader.start()
    def request(msg):
        proc.stdin.write(json.dumps(msg, ensure_ascii=False) + '\n')
        proc.stdin.flush()
        deadline = time.monotonic() + RPC_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('usage RPC timeout')
            value = messages.get(timeout=remaining)
            if value.get('readerFailed'):
                raise MonitorError(value['readerFailed'])
            require(not value.get('readerClosed'), 'usage RPC reader closed')
            if value.get('id') == msg['id']:
                require('error' not in value and type(value.get('result')) is dict, 'usage RPC failed')
                return value['result']
    try:
        request({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'limit_sampler', 'version': '2.0'}}})
        proc.stdin.write('{"method":"initialized","params":{}}\n')
        proc.stdin.flush()
        result = request({'id': 2, 'method': 'account/rateLimits/read'})
        buckets = result.get('rateLimitsByLimitId') or {}
        require(type(buckets) is dict, 'invalid usage buckets')
        rate = buckets.get('codex') or result.get('rateLimits') or {}
        require(type(rate) is dict, 'missing rate limits')
        windows = {}
        for window in (rate.get('primary'), rate.get('secondary')):
            if isinstance(window, dict):
                duration = window.get('windowDurationMins')
                require(type(duration) is int, 'invalid duration')
                require(duration not in windows, 'duplicate duration')
                windows[duration] = window
        return validate_windows(windows)
    finally:
        # Own only this child; terminate even on decoder, request, or validation failure.
        owned.close()
        proc.stdin.close()
        reader.join(timeout=1)
        proc.stdout.close()


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
        'continuationQuote', 'continuedAt', 'nativeTransition'
    }, 'invalid control fields')
    require(control.get('schemaVersion') == SCHEMA_VERSION and type(control.get('schemaVersion')) is int,
            'invalid control schema')
    require(type(control.get('active')) is bool, 'invalid active flag')
    require(number(control.get('expiresAt')) and control['expiresAt'] >= 0, 'invalid lease')
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
    for attempt in range(2):
        try:
            windows = validate_windows(measurer())
            at = time.time() if now is None else now
            card = policy_card(control, at)
            chosen = {key: windows[key] for key in card['decisionWindows']}
            prior = previous
            if prior and (not number(prior.get('at')) or not 0 < at - prior['at'] <= 600):
                prior = None
            if prior and prior.get('policy', {}).get('decisionWindows') != card['decisionWindows']:
                prior = None
            result = schedule(chosen, at, prior)
            result['windows'] = {str(key): value for key, value in windows.items()}
            result['policy'] = card
            result['measurementAttempts'] = attempt + 1
            result['exitReason'] = ('early_reserve' if result['action'] == 'SAVE_AND_STOP' and
                                    result['remaining'] > 20 else 'usage_threshold' if
                                    result['action'] == 'SAVE_AND_STOP' else None)
            result['errorType'] = None
            return result
        except (OSError, ValueError, RuntimeError, KeyError, queue.Empty, TimeoutError, TypeError) as error:
            error_type = type(error).__name__
    at = time.time() if now is None else now
    return {'at': at, 'action': 'SAVE_AND_STOP', 'reason': 'usage unavailable after retry',
            'exitReason': 'measurement_failure', 'errorType': error_type, 'measurementAttempts': 2,
            'policy': policy_card(control, at)}


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
                if _runtime is not None:
                    _runtime.check_binding()
                current = load_control(base)
                event(base, worker, 'running', control=current)
            except (OSError, ValueError, RuntimeError):
                # Main loop owns terminal reporting; a missed heartbeat is unhealthy.
                break
    pulse = threading.Thread(target=heartbeat, daemon=True)
    next_binding_check = 0
    try:
        event(base, worker, 'running', control=control)
        pulse.start()
        previous = read(base / 'latest.json') if (base / 'latest.json').exists() else None
        if previous and previous.get('policy', {}).get('epoch') != control['epoch']:
            previous = None
        due = previous.get('next_check_at', 0) if previous and number(previous.get('at')) and 0 <= time.time() - previous['at'] <= 300 else 0
        while True:
            if _runtime is not None and time.monotonic() >= next_binding_check:
                _runtime.check_binding()
                next_binding_check = time.monotonic() + 1
            control = load_control(base)
            require(control['epoch'] == worker['epoch'], 'worker epoch changed')
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
            if time.time() >= due:
                previous = sample(previous, control)
                # A SAVE obtained during stop/expiry is still retained. Never relax it.
                latch(base, previous, control)
                write(base / 'latest.json', previous)
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
    parser.add_argument('command', choices=['start', 'renew', 'stop', 'status', 'once', 'run'])
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--run-id')
    parser.add_argument('--new-epoch', action='store_true')
    parser.add_argument('--continuation-quote')
    parser.add_argument('--native-transition-at', type=float)
    parser.add_argument('--native-receipt')
    parser.add_argument('--ignore-weekly-quote')
    parser.add_argument('--exception-expires-at', type=float)
    parser.add_argument('--clear-exception', action='store_true')
    parser.add_argument('--worker-epoch', help=argparse.SUPPRESS)
    parser.add_argument('--worker-token', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if runtime is not None and args.command in ('start', 'renew', 'once'):
        require(args.run_id is None, 'candidate owns run ID')
        args.run_id = runtime.args.run_id
    base = Path(args.state_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    try:
        require(args.command == 'run' or not (args.worker_epoch or args.worker_token), 'worker flags require run')
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
                if saved:
                    result = saved
                elif args.command == 'once':
                    previous = read(base / 'latest.json') if (base / 'latest.json').exists() else None
                    if previous and previous.get('policy', {}).get('epoch') != control['epoch']:
                        previous = None
                    result = sample(previous, control)
                    latch(base, result, control)
                    write(base / 'latest.json', result)
                elif args.command == 'renew':
                    result = {'requested': 'renew', 'leaseSeconds': LEASE_SECONDS, **status(base)}
                else:
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
        if args.command == 'run':
            traceback.print_exc(file=sys.stderr)
        # Error class is diagnostic; never persist RPC payloads, credentials or arbitrary exception text.
        print(json.dumps({'action': 'HOLD', 'errorType': type(error).__name__, 'reason': str(error)
                          if isinstance(error, MonitorError) else 'monitor operation failed'}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
