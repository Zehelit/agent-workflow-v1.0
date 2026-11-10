"""Record portable runtime readiness without changing installed tooling.

Uses only Python stdlib. Native browser operation needs a separate test.
"""
import argparse, json, os, pathlib, queue, shutil
import re, subprocess, sys, threading, time, tomllib
import hashlib, math

PROJECT = pathlib.Path(__file__).resolve().parents[1]

class PreflightCheckError(RuntimeError):
    def __init__(self, check_id, message):
        self.check_id = check_id
        super().__init__(f'{check_id}: {message}')

def require(condition, check_id, message):
    """Runtime guards must remain active when Python runs with -O."""
    if not condition:
        raise PreflightCheckError(check_id, message)

READINESS_SCHEMA = 'agent-workflow.runtime-manifest/v1'
NATIVE_SCOPES = ('native-tool-catalog', 'native-browser-page', 'native-browser-file',
                 'native-browser-download', 'native-browser-close')
PYTHON_IDENTITY = ('import json,sys; print(json.dumps({"executable":sys.executable,'
                   '"baseExecutable":getattr(sys,"_base_executable",sys.executable),'
                   '"prefix":sys.prefix,"basePrefix":sys.base_prefix,'
                   '"version":list(sys.version_info[:3])}))')

def finite_number(value):
    # Integers are finite without float conversion, which can overflow.
    return type(value) is int or (type(value) is float and math.isfinite(value))

def strict_json_loads(data, check_id):
    """Reject ambiguous/nonfinite JSON at every depth; never echo receipt bytes."""
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('Duplicate object key')
            result[key] = value
        return result
    def number(text):
        value = float(text)
        if not math.isfinite(value): raise ValueError('Nonfinite number')
        return value
    def constant(text):
        raise ValueError('Nonfinite constant')
    try:
        return json.loads(data, object_pairs_hook=object_pairs,
                          parse_float=number, parse_constant=constant)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise PreflightCheckError(check_id, 'Strict JSON required: duplicate keys, nonfinite numbers or malformed JSON') from None

def bounded_text(value, limit):
    return type(value) is str and bool(value.strip()) and len(value) <= limit

def file_identity(path):
    """File identity, not spelling, distinguishes an alias from another engine."""
    path = pathlib.Path(path).resolve()
    stat = path.stat()
    return {'resolvedPath': str(path), 'device': stat.st_dev, 'inode': stat.st_ino,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

def same_file(left, right):
    try:
        return os.path.samefile(left, right)
    except (OSError, TypeError, ValueError):
        return False

def bounded_command(argv, env, cwd, timeout):
    """Version/identity observations; no environment mutation or installer calls."""
    try:
        result = subprocess.run(argv, env=env, cwd=cwd, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=timeout)
        return {'argv': list(map(str, argv)), 'exitCode': result.returncode,
                'stdout': result.stdout[:8192].strip(), 'stderr': result.stderr[:4096].strip(),
                'status': 'PASS' if result.returncode == 0 else 'FAIL'}
    except subprocess.TimeoutExpired:
        return {'argv': list(map(str, argv)), 'status': 'GAP', 'reason': 'timeout'}
    except (OSError, ValueError) as exc:
        return {'argv': list(map(str, argv)), 'status': 'FAIL', 'reason': str(exc)}

def observe_python(path, env, root, timeout):
    observation = bounded_command([str(path), '-B', '-c', PYTHON_IDENTITY], env, root, timeout)
    if observation['status'] == 'PASS':
        try:
            identity = strict_json_loads(observation['stdout'], 'readiness-python-json')
            require(isinstance(identity, dict) and isinstance(identity.get('version'), list)
                    and len(identity['version']) == 3
                    and all(type(n) is int for n in identity['version'])
                    and identity['version'][:2] >= [3, 11],
                    'readiness-python-version', 'Python 3.11+ identity required')
            require(same_file(identity['executable'], path), 'readiness-python-identity',
                    'Launcher response does not identify the executed engine')
            identity['file'] = file_identity(identity['executable'])
            identity['baseFile'] = file_identity(identity['baseExecutable'])
            observation['engine'] = identity
        except (OSError, ValueError, KeyError, TypeError, PreflightCheckError) as exc:
            observation.update(status='FAIL', reason=str(exc))
    return observation

def observe_shell_discovery(env, root, timeout):
    """Observe clean child-shell aliases/command resolution, never the caller UI."""
    if os.name != 'nt':
        return {'status': 'GAP', 'reason': 'PowerShell discovery unavailable on this platform',
                'scope': 'clean-child-shell-discovery'}
    shell = shutil.which('powershell.exe', path=next(
        (v for k, v in env.items() if k.casefold() == 'path'), ''))
    if not shell:
        system = env.get('SystemRoot') or env.get('SYSTEMROOT')
        candidate = (pathlib.Path(system)/'System32/WindowsPowerShell/v1.0/powershell.exe'
                     if system else None)
        shell = str(candidate) if candidate and candidate.is_file() else None
    if not shell:
        return {'status': 'GAP', 'scope': 'clean-child-shell-discovery',
                'reason': 'PowerShell executable unavailable; no alternate shell claimed'}
    script = ('[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); '
              '$rows = @(); foreach ($n in @("python","python3","pip","node","npm","npx","uv","uvx","git")) { '
              '$found = @(Get-Command -Name $n -All -ErrorAction SilentlyContinue); '
              'foreach ($c in $found) { $rows += [pscustomobject]@{name=$n; '
              'commandType=[string]$c.CommandType; definition=[string]$c.Definition; path=[string]$c.Path} } }; '
              '[pscustomobject]@{path=[string]$env:Path; commands=$rows} | ConvertTo-Json -Compress -Depth 4')
    result = bounded_command([shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
                             env, root, timeout)
    result['scope'] = 'clean-child-shell-discovery'
    result['callerInteractiveAliasesObserved'] = False
    if result['status'] == 'PASS':
        try:
            discovered = strict_json_loads(result['stdout'], 'readiness-shell-json')
            require(isinstance(discovered, dict) and isinstance(discovered.get('commands'), list)
                    and isinstance(discovered.get('path'), str),
                    'readiness-shell-discovery', 'Shell command/alias discovery response malformed')
            result['discovery'] = discovered
        except (ValueError, PreflightCheckError) as exc:
            result.update(status='FAIL', reason=str(exc))
    return result

def shell_observations(env, root, interpreter, timeout, scope):
    path = next((v for k, v in env.items() if k.casefold() == 'path'), '')
    result = {'scope': scope, 'provider': 'local-subprocess', 'path': path,
              'aliasEvidence': 'Clean child PowerShell command/alias discovery plus PATH launchers; caller interactive aliases unobserved',
              'shellDiscovery': observe_shell_discovery(env, root, timeout),
              'tools': []}
    for name in ('python', 'python3', 'pip', 'node', 'npm', 'npx', 'uv', 'uvx', 'git'):
        launcher = shutil.which(name, path=path)
        item = {'name': name, 'launcher': launcher, 'status': 'GAP'}
        if launcher:
            try:
                item['file'] = file_identity(launcher)
            except OSError as exc:
                item.update(status='FAIL', reason=str(exc))
            else:
                observation = (observe_python(launcher, env, root, timeout)
                               if name in ('python', 'python3') else
                               bounded_command([launcher, '--version'], env, root, timeout))
                item['observation'] = observation
                item['status'] = observation['status']
                if name not in ('python', 'python3') and item['status'] == 'PASS':
                    item['versionOutput'] = observation.get('stdout', '') or observation.get('stderr', '')
                    if not item['versionOutput']:
                        item.update(status='GAP', reason='Version output unavailable despite exit0')
                if name in ('python', 'python3') and item['status'] == 'PASS':
                    item['sameEngineAsSelected'] = same_file(
                        observation['engine']['executable'], interpreter)
                    if not item['sameEngineAsSelected']:
                        item.update(status='GAP', reason='PATH launcher selects a different engine')
        else:
            item['reason'] = 'Executable unavailable in this PATH'
        result['tools'].append(item)
    return result

def scoped_path(path, root, check_id, exists=True):
    value = pathlib.Path(path).resolve()
    require(value.is_relative_to(root), check_id, 'Path is outside the project: '+str(value))
    require(not exists or value.is_file(), check_id, 'Project file is unavailable: '+str(value))
    return value

def validate_browser_configuration(config, root):
    """Configuration constraints are evidence about config, never native visibility."""
    require(isinstance(config, dict), 'readiness-config-shape', 'Config object required')
    servers = config.get('mcp_servers', {})
    require(isinstance(servers, dict), 'readiness-config-shape', 'mcp_servers must be an object')
    plugins = config.get('plugins', {})
    require(isinstance(plugins, dict), 'readiness-config-shape', 'plugins must be an object')
    for item in servers.values():
        require(isinstance(item, dict) and ('enabled' not in item or type(item['enabled']) is bool)
                and isinstance(item.get('args', []), list) and all(isinstance(v, str) for v in item.get('args', [])),
                'readiness-config-shape', 'Transport object/string args/boolean enabled required')
    for plugin in plugins.values():
        require(isinstance(plugin, dict) and ('enabled' not in plugin or type(plugin['enabled']) is bool)
                and isinstance(plugin.get('mcp_servers', {}), dict),
                'readiness-config-shape', 'Plugin object/boolean enabled required')
        for item in plugin.get('mcp_servers', {}).values():
            require(isinstance(item, dict) and ('enabled' not in item or type(item['enabled']) is bool),
                    'readiness-config-shape', 'Plugin transport object/boolean enabled required')
    candidates = [name for name, item in servers.items() if isinstance(item, dict)
                  and item.get('enabled', True) and ('invisible-playwright-mcp' in
                  ' '.join([str(item.get('command', '')), *map(str, item.get('args', []))])
                  or name == 'invisible_playwright_mcp')]
    for name, plugin in plugins.items():
        if isinstance(plugin, dict) and plugin.get('enabled', True):
            for transport, item in plugin.get('mcp_servers', {}).items():
                if isinstance(item, dict) and item.get('enabled', True) and (
                        'invisible-playwright' in name or transport == 'invisible_playwright_mcp'):
                    candidates.append('plugin:'+name+':'+transport)
    managed_plugin = [name for name, plugin in plugins.items()
                      if 'invisible-playwright-mcp' in name and plugin.get('enabled', True)
                      and not plugin.get('mcp_servers')]
    if not candidates and len(managed_plugin) == 1:
        return {'scope': 'configuration-only', 'provider': 'invisible-playwright-mcp',
                'status': 'GAP', 'mode': 'plugin-managed', 'plugin': managed_plugin[0],
                'reason': 'Plugin transport and upload scope require native tool observation',
                'nativeVisibilityVerified': False}
    require(candidates == ['invisible_playwright_mcp'], 'readiness-single-transport',
            'Exactly one explicit invisible-playwright transport required: '+repr(candidates))
    server = servers['invisible_playwright_mcp']
    require(bounded_text(server.get('command'), 32768) and pathlib.Path(server['command']).is_absolute(),
            'readiness-mcp-command', 'Explicit absolute command required')
    command = pathlib.Path(server['command']).resolve()
    require(command.is_file(), 'readiness-mcp-command', 'Configured command missing')
    args = server.get('args', [])
    require(isinstance(args, list) and len(args) == 6 and args[0] == '--python'
            and args[2:4] == ['--no-python-downloads', '--from']
            and isinstance(args[4], str)
            and re.fullmatch(r'invisible-playwright-mcp==\d+\.\d+\.\d+', args[4])
            and args[5] == 'invisible-playwright-mcp',
            'readiness-mcp-arguments', 'Pinned non-downloading invisible-playwright arguments required')
    require(bounded_text(args[1], 32768) and pathlib.Path(args[1]).is_absolute(),
            'readiness-mcp-base', 'Explicit absolute base interpreter required')
    base = pathlib.Path(args[1]).resolve()
    require(base.is_file(), 'readiness-mcp-base', 'Configured base interpreter missing')
    require(isinstance(server.get('env', {}), dict), 'readiness-config-shape', 'Transport env object required')
    upload = server.get('env', {}).get('INVISIBLE_MCP_UPLOAD_DIRS', '')
    require(bounded_text(upload, 32768) and pathlib.Path(upload).is_absolute() and pathlib.Path(upload).resolve() == root,
            'readiness-upload-scope', 'Upload scope must equal this resolved project root')
    return {'scope': 'configuration-only', 'provider': 'invisible-playwright-mcp',
            'status': 'PASS', 'command': file_identity(command), 'basePython': file_identity(base),
            'arguments': args, 'uploadRoot': str(root), 'nativeVisibilityVerified': False}

def attach_native_receipt(path, sha256, root, now, max_age):
    """Validate a bounded caller-saved attachment without authenticating its claims."""
    path = scoped_path(path, root, 'readiness-native-receipt-scope')
    require(isinstance(sha256, str) and re.fullmatch('[0-9a-f]{64}', sha256) is not None,
            'readiness-native-receipt-pin', 'Explicit lowercase SHA256 pin required')
    with path.open('rb') as stream:
        data = stream.read(1024*1024+1)
    require(len(data) <= 1024*1024, 'readiness-native-receipt-size', 'Receipt exceeds 1 MiB')
    require(hashlib.sha256(data).hexdigest() == sha256, 'readiness-native-receipt-pin',
            'Receipt bytes differ from pin')
    receipt = strict_json_loads(data, 'readiness-native-receipt-json')
    require(isinstance(receipt, dict) and type(receipt.get('schemaVersion')) is int
            and receipt['schemaVersion'] == 1,
            'readiness-native-receipt-format', 'Versioned receipt object required')
    require(bounded_text(receipt.get('projectRoot'), 32768),
            'readiness-native-receipt-project', 'Explicit nonempty projectRoot string required')
    require(pathlib.Path(receipt['projectRoot']).resolve() == root,
            'readiness-native-receipt-project', 'Receipt belongs to another project')
    require(receipt.get('provider') == 'invisible-playwright-mcp'
            and receipt.get('scope') in NATIVE_SCOPES,
            'readiness-native-receipt-provider', 'Native invisible-playwright scope required')
    observed = receipt.get('observedAt')
    require(finite_number(observed) and now-max_age <= observed <= now,
            'readiness-native-receipt-freshness',
            'Native observation missing, future-dated or stale')
    provenance = receipt.get('provenance')
    require(isinstance(provenance, dict) and set(provenance) ==
            {'schemaVersion', 'observer', 'method', 'sessionId'}
            and type(provenance.get('schemaVersion')) is int and provenance['schemaVersion'] == 1
            and all(bounded_text(provenance.get(k), limit) for k, limit in
                    (('observer', 256), ('method', 128), ('sessionId', 256))),
            'readiness-native-receipt-provenance', 'Version 1 bounded observer, method and sessionId required')
    require(isinstance(receipt.get('files'), list) and len(receipt['files']) <= 128,
            'readiness-native-receipt-files',
            'Explicit project file list required (empty for catalog/page/close)')
    artifacts = []
    for file in receipt['files']:
        require(bounded_text(file, 32768), 'readiness-native-receipt-files', 'File path must be a bounded nonempty string')
        artifact = scoped_path(file, root, 'readiness-native-receipt-files')
        if receipt['scope'] == 'native-browser-download':
            require(artifact.stat().st_size > 0, 'readiness-native-receipt-download-artifact',
                    'Empty downloaded artifact cannot establish native download')
            artifacts.append({'resolvedPath': str(artifact), 'size': artifact.stat().st_size,
                              'sha256': file_identity(artifact)['sha256']})
    observations = receipt.get('observations')
    require(isinstance(observations, list) and 1 <= len(observations) <= 128,
            'readiness-native-receipt-observations', '1 to 128 versioned tool observation objects required')
    for observation in observations:
        require(isinstance(observation, dict) and
                {'schemaVersion', 'toolName', 'outcome', 'summary'} <= set(observation) <=
                {'schemaVersion', 'toolName', 'outcome', 'summary', 'claim', 'providerAuthenticated'}
                and type(observation.get('schemaVersion')) is int and observation['schemaVersion'] == 1
                and bounded_text(observation.get('toolName'), 128)
                and observation.get('outcome') in ('PASS', 'FAIL', 'GAP')
                and bounded_text(observation.get('summary'), 2048)
                and ('claim' not in observation or bounded_text(observation['claim'], 256))
                and ('providerAuthenticated' not in observation or type(observation['providerAuthenticated']) is bool),
                'readiness-native-receipt-observations', 'Version 1 bounded toolName, outcome and summary required')
    require(receipt['scope'] != 'native-browser-download' or receipt['files'],
            'readiness-native-receipt-download-artifact',
            'Downloaded artifact unavailable: empty files list cannot establish native download; native evidence remains GAP')
    return {'path': str(path), 'sha256': sha256, 'scope': receipt['scope'],
            'provider': receipt['provider'], 'observedAt': observed, 'ageSeconds': now-observed,
            'provenance': provenance, 'observationCount': len(observations),
            'artifacts': artifacts,
            'status': 'ATTACHED_UNAUTHENTICATED',
            'providerAuthenticated': False, 'nativeReadinessVerified': False,
            'trust': 'Caller-saved bytes and metadata; claims require independent native review'}

def probe_stdio(script, interpreter, root, env, timeout=5):
    """Explicit synthetic stdio target. No browser call or catalog visibility claim.

    The selected script is executed, not security sandboxed. Only our immediate
    child is terminated; detached descendant containment is the later A18 scope.
    """
    root = pathlib.Path(root).resolve()
    script = scoped_path(script, root, 'readiness-stdio-scope')
    require(script.suffix == '.py', 'readiness-stdio-target', 'Synthetic Python script required')
    require(finite_number(timeout) and 0 < timeout <= 30,
            'readiness-stdio-timeout', 'Probe timeout must be in (0,30] seconds')
    receipt = {'scope': 'cli-stdio-synthetic', 'provider': 'caller-selected-synthetic-script',
               'script': file_identity(script), 'status': 'FAIL', 'events': [],
               'nativeReadinessVerified': False, 'descendantContainmentVerified': False}
    proc = None
    inbox = queue.Queue()
    def read_stdout():
        while True:
            line = proc.stdout.readline(65537)
            if not line: break
            # A protocol violation closes the immediate child in finally.
            if len(line) > 65536:
                inbox.put({'_error': 'stdio line exceeds limit'}); return
            try:
                message = strict_json_loads(line, 'readiness-stdio-protocol')
                if not isinstance(message, dict): raise ValueError('JSON object required')
                inbox.put(message)
            except (ValueError, TypeError, PreflightCheckError) as exc:
                inbox.put({'_error': str(exc)}); return
        inbox.put({'_error': 'stdio closed before response'})
    def read_stderr():
        # Drain the pipe while retaining only a bounded excerpt.
        while True:
            line = proc.stderr.readline(4096)
            if not line: break
            if len(receipt.get('stderr', '')) < 4096:
                receipt['stderr'] = (receipt.get('stderr', '')+line)[:4096]
    def rpc(request_id, method):
        params = ({'protocolVersion': '2024-11-05', 'capabilities': {},
                   'clientInfo': {'name': 'portable-synthetic-readiness', 'version': '1'}}
                  if method == 'initialize' else {})
        proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': request_id,
                                     'method': method, 'params': params})+'\n'); proc.stdin.flush()
        deadline = time.monotonic()+timeout
        while True:
            remaining = deadline-time.monotonic()
            if remaining <= 0: raise TimeoutError(method+' timed out')
            try: response = inbox.get(timeout=remaining)
            except queue.Empty: raise TimeoutError(method+' timed out')
            require('_error' not in response, 'readiness-stdio-protocol', response.get('_error', ''))
            if response.get('id') != request_id: continue
            require(response.get('jsonrpc') == '2.0' and 'error' not in response
                    and isinstance(response.get('result'), dict),
                    'readiness-stdio-response', 'Protocol response missing or refused')
            receipt['events'].append({'method': method, 'response': response})
            return response['result']
    try:
        proc = subprocess.Popen([str(interpreter), '-B', str(script)], cwd=root, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8', errors='replace', bufsize=1)
        receipt['pid'] = proc.pid
        readers = [threading.Thread(target=read_stdout, daemon=True),
                   threading.Thread(target=read_stderr, daemon=True)]
        for reader in readers: reader.start()
        initialized = rpc(1, 'initialize')
        require(isinstance(initialized.get('serverInfo'), dict)
                and isinstance(initialized.get('capabilities'), dict)
                and isinstance(initialized.get('protocolVersion'), str),
                'readiness-stdio-initialize', 'Complete initialize result required')
        proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n'); proc.stdin.flush()
        listed = rpc(2, 'tools/list')
        require(isinstance(listed.get('tools'), list)
                and all(isinstance(tool, dict) and isinstance(tool.get('name'), str)
                        for tool in listed['tools']),
                'readiness-stdio-tools', 'tools/list must contain named tools')
        receipt['toolNames'] = [tool['name'] for tool in listed['tools']]
        receipt['status'] = 'PASS'
    except Exception as exc:
        receipt['reason'] = str(exc)
        receipt['status'] = 'GAP' if isinstance(exc, TimeoutError) else 'FAIL'
        if isinstance(exc, PreflightCheckError): receipt['failedCheckId'] = exc.check_id
    finally:
        if proc:
            action = 'already-exited'
            try:
                if proc.stdin and not proc.stdin.closed: proc.stdin.close()
                action = 'stdin-close'
                proc.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, OSError):
                if proc.poll() is None:
                    action = 'terminate-immediate-child'; proc.terminate()
                try: proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    action = 'kill-immediate-child'; proc.kill()
                    try: proc.wait(timeout=timeout)
                    except subprocess.TimeoutExpired: pass
            stopped = proc.poll() is not None
            receipt['cleanup'] = {'pid': proc.pid, 'exitCode': proc.returncode,
                                  'stopped': stopped, 'action': action,
                                  'scope': 'owned-immediate-child-only'}
            if not stopped: receipt.update(status='FAIL', cleanupError='Owned child not terminal')
            elif action != 'stdin-close' or proc.returncode != 0:
                if receipt['status'] == 'PASS':
                    receipt.update(status='FAIL', reason='Synthetic child did not exit cleanly on stdin close')
            for reader in readers: reader.join(timeout=min(timeout, 1))
            receipt['cleanup']['pipeReadersStopped'] = all(not reader.is_alive() for reader in readers)
            # Detached descendants can inherit pipes. Do not block closing a pipe
            # held by a reader; report this A18 coverage gap explicitly.
            if receipt['cleanup']['pipeReadersStopped']:
                for pipe in (proc.stdout, proc.stderr):
                    if pipe: pipe.close()
            elif receipt['status'] == 'PASS':
                receipt.update(status='GAP', reason='Inherited pipes remain open; descendant coverage unverified')
    return receipt

def gather_readiness(project_root, config=None, bare_env=None, configured_env=None,
                     interpreter=None, engine_path=None, stdio_script=None,
                     receipt_paths=(), timeout=5, max_receipt_age=1800):
    """Portable read-only observations; opt-in synthetic script is the only MCP launch."""
    require(finite_number(timeout) and 0 < timeout <= 30,
            'readiness-timeout', 'Timeout must be in (0,30] seconds')
    require(finite_number(max_receipt_age)
            and 0 < max_receipt_age <= 86400, 'readiness-receipt-age', 'Receipt age must be in (0,86400]')
    require(isinstance(project_root, (str, pathlib.Path)) and str(project_root).strip() != '',
            'readiness-project', 'Explicit nonempty project directory required')
    root = pathlib.Path(project_root).resolve()
    require(root.is_dir(), 'readiness-project', 'Existing project directory required')
    bare = dict(os.environ if bare_env is None else bare_env)
    configured = dict(bare if configured_env is None else configured_env)
    if config is not None and configured_env is None:
        require(isinstance(config, dict) and isinstance(config.get('shell_environment_policy', {}), dict),
                'readiness-config-shape', 'Config/shell policy object required')
        overrides = config.get('shell_environment_policy', {}).get('set', {})
        require(isinstance(overrides, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in overrides.items()),
                'readiness-config-shape', 'Shell overrides require string names/values')
        configured = {k: v for k, v in bare.items() if k.casefold() not in
                      {name.casefold() for name in overrides}} | overrides
    candidates = [root/'.venv'/('Scripts/python.exe' if os.name == 'nt' else 'bin/python'),
                  root/'venv'/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')]
    selected = pathlib.Path(interpreter) if interpreter is not None else next(
        (candidate for candidate in candidates if candidate.is_file()), pathlib.Path(sys.executable))
    selected = selected.resolve()
    now = time.time()
    report = {'schemaVersion': 1, 'schema': READINESS_SCHEMA, 'projectRoot': str(root),
              'observedAt': now, 'scope': 'portable-readiness', 'provider': 'local-stdlib-python',
              'status': 'GAP', 'checks': [], 'nativeReadinessVerified': False,
              'selection': {'rule': 'explicit interpreter > existing project .venv > existing project venv > running interpreter',
                            'selectedInterpreter': str(selected), 'createsEnvironment': False,
                            'managedBaseMutation': False},
              'nativeScopes': [{'scope': scope, 'provider': 'invisible-playwright-mcp',
                                'status': 'GAP', 'reason': 'Actual native tool observation required'}
                               for scope in NATIVE_SCOPES], 'nativeReceipts': []}
    def check(name, status, *, impact='required', **details):
        report['checks'].append({'name': name, 'status': status, 'impact': impact, **details})
    if selected.is_file():
        report['interpreter'] = observe_python(selected, configured, root, timeout)
    else:
        report['interpreter'] = {'status': 'GAP', 'reason': 'Selected interpreter unavailable'}
    check('selected-python', report['interpreter']['status'])
    report['shells'] = [shell_observations(bare, root, selected, timeout, 'bare-shell-path'),
                        shell_observations(configured, root, selected, timeout, 'configured-shell-path')]
    for shell in report['shells']:
        check(shell['scope']+':shell-discovery', shell['shellDiscovery']['status'], impact='diagnostic')
        for item in shell['tools']:
            check(shell['scope']+':'+item['name'], item['status'], impact='diagnostic')
    if config is None:
        report['configuration'] = {'scope': 'configuration-only', 'status': 'GAP',
                                   'reason': 'No explicit configuration supplied'}
    else:
        try:
            report['configuration'] = validate_browser_configuration(config, root)
            if 'basePython' in report['configuration']:
                report['configuration']['baseObservation'] = observe_python(
                    report['configuration']['basePython']['resolvedPath'], configured, root, timeout)
                if report['configuration']['baseObservation']['status'] != 'PASS':
                    report['configuration']['status'] = report['configuration']['baseObservation']['status']
        except (PreflightCheckError, TypeError, ValueError, OSError, KeyError) as exc:
            report['configuration'] = {'scope': 'configuration-only', 'status': 'FAIL', 'reason': str(exc)}
            if isinstance(exc, PreflightCheckError): report['configuration']['failedCheckId'] = exc.check_id
    check('browser-configuration', report['configuration']['status'])
    report['engine'] = {'scope': 'browser-engine-file-only', 'provider': 'invisible-playwright-mcp',
                        'status': 'GAP', 'reason': 'Engine capability requires actual native browser open'}
    if engine_path is not None:
        engine = pathlib.Path(engine_path).resolve()
        report['engine']['requestedPath'] = str(engine)
        if engine.is_file(): report['engine']['file'] = file_identity(engine)
        else: report['engine']['reason'] = 'Requested engine file unavailable; no installation attempted'
    check('browser-engine-native', 'GAP')
    if stdio_script is not None:
        try:
            require(report['interpreter']['status'] == 'PASS', 'readiness-stdio-interpreter',
                    'Synthetic probe requires a verified interpreter')
            report['stdio'] = probe_stdio(stdio_script, selected, root, configured, timeout)
        except (PreflightCheckError, OSError, ValueError) as exc:
            report['stdio'] = {'scope': 'cli-stdio-synthetic', 'status': 'FAIL', 'reason': str(exc)}
            if isinstance(exc, PreflightCheckError): report['stdio']['failedCheckId'] = exc.check_id
        check('synthetic-stdio', report['stdio']['status'])
    for path, pin in receipt_paths:
        try:
            report['nativeReceipts'].append(attach_native_receipt(path, pin, root, now, max_receipt_age))
        except (PreflightCheckError, OSError, ValueError, TypeError) as exc:
            failed = {'path': str(path), 'status': 'FAIL', 'reason': str(exc)}
            if isinstance(exc, PreflightCheckError): failed['failedCheckId'] = exc.check_id
            if getattr(exc, 'check_id', None) == 'readiness-native-receipt-download-artifact':
                failed['evidenceDisposition'] = 'REFUSED_MISSING_DOWNLOADED_ARTIFACT'
            report['nativeReceipts'].append(failed)
            check('native-receipt', 'FAIL', reason=str(exc))
    report['status'] = 'FAIL' if any(c['status'] == 'FAIL' and c['impact'] == 'required'
                                     for c in report['checks']) else 'GAP'
    report['endedAt'] = time.time()
    return report

def main(argv=None):
    parser = argparse.ArgumentParser(description='Portable runtime readiness observations')
    parser.add_argument('--browser', action='store_true', help='Retired legacy smoke mode; refused')
    parser.add_argument('--output', type=pathlib.Path)
    parser.add_argument('--readiness', action='store_true', help='Portable observations')
    parser.add_argument('--manifest', action='store_true', help='Alias for --readiness')
    parser.add_argument('--project-root', type=pathlib.Path, default=PROJECT)
    parser.add_argument('--config', type=pathlib.Path, help='Explicit read-only TOML config')
    parser.add_argument('--interpreter', type=pathlib.Path)
    parser.add_argument('--engine-path', type=pathlib.Path, help='File observation only')
    parser.add_argument('--stdio-probe', type=pathlib.Path, help='Explicit synthetic .py script inside project')
    parser.add_argument('--native-receipt', type=pathlib.Path, action='append', default=[])
    parser.add_argument('--native-receipt-sha256', action='append', default=[])
    parser.add_argument('--timeout', type=float, default=5)
    parser.add_argument('--max-receipt-age', type=float, default=1800)
    options = parser.parse_args(argv)
    try:
        require(not options.browser, 'readiness-cli-scope', 'Legacy browser smoke is retired')
        require(len(options.native_receipt) == len(options.native_receipt_sha256),
                'readiness-cli-receipts', 'Each receipt needs its SHA256 pin in matching order')
        config = (tomllib.loads(options.config.read_text(encoding='utf-8-sig'))
                  if options.config else None)
        report = gather_readiness(options.project_root, config=config, interpreter=options.interpreter,
                                  engine_path=options.engine_path, stdio_script=options.stdio_probe,
                                  receipt_paths=zip(options.native_receipt, options.native_receipt_sha256),
                                  timeout=options.timeout, max_receipt_age=options.max_receipt_age)
    except (PreflightCheckError, OSError, ValueError, TypeError) as exc:
        report = {'schemaVersion': 1, 'schema': READINESS_SCHEMA, 'status': 'FAIL',
                  'scope': 'portable-readiness', 'error': str(exc), 'nativeReadinessVerified': False}
    output = options.output or options.project_root/'runtime-manifest.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError):
        report = {'schemaVersion': 1, 'schema': READINESS_SCHEMA, 'status': 'FAIL',
                  'scope': 'portable-readiness', 'error': 'readiness-output-json: Strict finite JSON output required',
                  'nativeReadinessVerified': False}
        serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    output.write_text(serialized, encoding='utf-8')
    print(json.dumps({'status': report['status'], 'output': str(output)}, allow_nan=False))
    return 1 if report['status'] == 'FAIL' else 2


if __name__ == '__main__':
    sys.exit(main())
