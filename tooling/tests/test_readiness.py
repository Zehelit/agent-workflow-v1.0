"""A09 source-only regression probes; never native catalog/browser proof.

Uses disposable synthetic projects, the already-running Python, and actual
owned synthetic stdio children. PATH lookup is controlled where stated. Run
under normal Python and -O. No live config/profile, installation or browser.
"""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import math
import re
import subprocess
import sys
import tempfile
import time
import unittest
import venv
from unittest import mock

SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'preflight.py'
spec = importlib.util.spec_from_file_location('readiness_under_test', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SYNTHETIC = '''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request: continue
    result = ({'protocolVersion':'2024-11-05', 'capabilities':{},
               'serverInfo':{'name':'synthetic-readiness','version':'1'}}
              if request['method'] == 'initialize' else
              {'tools':[{'name':'synthetic_only', 'inputSchema':{'type':'object'}}]})
    print(json.dumps({'jsonrpc':'2.0', 'id':request['id'], 'result':result}), flush=True)
'''


def schema_errors(value, node, schema, path='$'):
    """Local checker for this schema vocabulary, not a full JSON Schema validator."""
    if '$ref' in node:
        target = schema
        for part in node['$ref'][2:].split('/'): target = target[part]
        return schema_errors(value, target, schema, path)
    errors = []
    check = lambda child: schema_errors(value, child, schema, path)
    for child in node.get('allOf', []): errors += check(child)
    if 'if' in node:
        errors += check(node.get('else', {}) if check(node['if']) else node.get('then', {}))
    if 'not' in node and not check(node['not']): errors.append(path+': not')
    if 'const' in node and (value != node['const'] or type(value) != type(node['const'])):
        errors.append(path+': const')
    if 'enum' in node and value not in node['enum']: errors.append(path+': enum')
    predicates = {'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list),
                  'string': lambda v: type(v) is str, 'integer': lambda v: type(v) is int,
                  'number': lambda v: type(v) is int or (type(v) is float and math.isfinite(v)),
                  'boolean': lambda v: type(v) is bool, 'null': lambda v: v is None}
    kinds = node.get('type', [])
    if isinstance(kinds, str): kinds = [kinds]
    if kinds and not any(predicates[k](value) for k in kinds): return errors+[path+': type']
    if isinstance(value, dict):
        errors += [path+': missing '+key for key in node.get('required', []) if key not in value]
        properties = node.get('properties', {})
        for key, child in value.items():
            if key in properties: errors += schema_errors(child, properties[key], schema, path+'.'+key)
            elif node.get('additionalProperties') is False: errors.append(path+': extra '+key)
    if isinstance(value, list):
        if not node.get('minItems', 0) <= len(value) <= node.get('maxItems', math.inf):
            errors.append(path+': count')
        for index, child in enumerate(value):
            errors += schema_errors(child, node.get('items', {}), schema, path+'['+str(index)+']')
    if isinstance(value, str):
        if not node.get('minLength', 0) <= len(value) <= node.get('maxLength', math.inf):
            errors.append(path+': length')
        if 'pattern' in node and re.search(node['pattern'], value) is None: errors.append(path+': pattern')
    if type(value) in (int, float):
        if value < node.get('minimum', -math.inf) or value > node.get('maximum', math.inf):
            errors.append(path+': bound')
    return errors


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='a09-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.container = pathlib.Path(self.temp.name)
        self.root = self.container/'Proje Türkçe boşluk'
        self.root.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k.casefold() != 'path'} | {'PATH': ''}
        self.config = {'shell_environment_policy': {'set': {'PATH': ''}},
                       'mcp_servers': {'invisible_playwright_mcp': {
                           'command': sys.executable,
                           'args': ['--python', sys._base_executable, '--no-python-downloads',
                                    '--from', 'invisible-playwright-mcp==0.70.10', 'invisible-playwright-mcp'],
                           'env': {'INVISIBLE_MCP_UPLOAD_DIRS': str(self.root)}}},
                       'plugins': {'invisible-playwright-mcp@feder-cr': {'enabled': True,
                                   'mcp_servers': {'invisible_playwright_mcp': {'enabled': False}}}}}
        self.source_pin = hashlib.sha256(SOURCE.read_bytes()).hexdigest()

    def tearDown(self):
        self.assertEqual(hashlib.sha256(SOURCE.read_bytes()).hexdigest(), self.source_pin)

    def gather(self, **kwargs):
        options = {'project_root': self.root, 'config': self.config,
                   'bare_env': self.env, 'configured_env': self.env,
                   'interpreter': sys.executable, 'timeout': 2}
        options.update(kwargs)
        # Only Python PATH launchers are provided. Other tool gaps are explicit;
        # Python engine identity and selected script subprocesses remain real.
        with mock.patch.object(module.shutil, 'which', side_effect=lambda name, **kw:
                               sys.executable if name in ('python', 'python3') else None), \
             mock.patch.object(module, 'observe_shell_discovery', return_value={
                 'scope': 'clean-child-shell-discovery', 'status': 'GAP',
                 'reason': 'Controlled PATH fixture; child-shell discovery not native proof'}):
            return module.gather_readiness(**options)

    def synthetic(self, source=SYNTHETIC, root=None):
        path = (root or self.root)/'sentetik sunucu.py'
        path.write_text(source, encoding='utf-8')
        return path

    def native_receipt(self, **updates):
        receipt = {'schemaVersion': 1, 'projectRoot': str(self.root),
                   'provider': 'invisible-playwright-mcp', 'scope': 'native-tool-catalog',
                   'observedAt': time.time(), 'files': [],
                   'provenance': {'schemaVersion': 1, 'observer': 'controlled test', 'method': 'synthetic fixture',
                                  'sessionId': 'not-a-native-session'},
                   'observations': [{'schemaVersion': 1, 'toolName': 'synthetic_fixture',
                                     'outcome': 'PASS', 'summary': 'Caller claim only',
                                     'claim': 'PASS', 'providerAuthenticated': True}]}
        receipt.update(updates)
        path = self.root/'caller receipt.json'
        path.write_text(json.dumps(receipt), encoding='utf-8')
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_two_project_paths_real_python_and_owned_stdio(self):
        for name in ('project with spaces', 'İkinci proje Türkçe ı'):
            with self.subTest(project=name):
                root = self.container/name
                root.mkdir()
                config = copy.deepcopy(self.config)
                config['mcp_servers']['invisible_playwright_mcp']['env']['INVISIBLE_MCP_UPLOAD_DIRS'] = str(root)
                before = json.dumps(config, sort_keys=True)
                report = self.gather(project_root=root, config=config, stdio_script=self.synthetic(root=root))
                self.assertEqual(report['projectRoot'], str(root.resolve()))
                self.assertEqual(report['interpreter']['status'], 'PASS')
                self.assertEqual(report['configuration']['status'], 'PASS')
                self.assertEqual(report['stdio']['status'], 'PASS', report['stdio'])
                self.assertEqual(report['stdio']['toolNames'], ['synthetic_only'])
                self.assertEqual([e['method'] for e in report['stdio']['events']], ['initialize', 'tools/list'])
                self.assertTrue(report['stdio']['cleanup']['stopped'])
                self.assertTrue(report['stdio']['cleanup']['pipeReadersStopped'])
                self.assertEqual(report['stdio']['cleanup']['exitCode'], 0)
                self.assertFalse(report['stdio']['descendantContainmentVerified'])
                self.assertFalse(report['nativeReadinessVerified'])
                self.assertEqual(report['status'], 'GAP')
                self.assertEqual(json.dumps(config, sort_keys=True), before)
                self.assertFalse((root/'.venv').exists())

    def test_missing_engine_file_and_present_file_are_both_native_gaps(self):
        for path in (self.root/'missing engine.exe', self.synthetic()):
            report = self.gather(engine_path=path)
            self.assertEqual(report['engine']['status'], 'GAP')
            self.assertEqual(report['status'], 'GAP')
            self.assertTrue(all(scope['status'] == 'GAP' for scope in report['nativeScopes']))
            self.assertFalse(report['nativeReadinessVerified'])

    def test_broken_alias_reaches_observation_failure(self):
        alias = self.root/'python3.exe'
        alias.write_bytes(b'broken alias, not executable')
        with mock.patch.object(module.shutil, 'which', side_effect=lambda name, **kw:
                               str(alias) if name == 'python3' else
                               sys.executable if name == 'python' else None):
            report = module.gather_readiness(self.root, self.config, self.env, self.env,
                                             sys.executable, timeout=2)
        self.assertEqual(report['interpreter']['status'], 'PASS')
        self.assertEqual(report['configuration']['status'], 'PASS')
        for shell in report['shells']:
            python = next(t for t in shell['tools'] if t['name'] == 'python')
            alias = next(t for t in shell['tools'] if t['name'] == 'python3')
            self.assertEqual(python['status'], 'PASS')
            self.assertEqual(alias['status'], 'FAIL')
        self.assertEqual(report['status'], 'GAP')
        self.assertTrue(all(c['impact'] == 'diagnostic' for c in report['checks']
                            if c['name'].endswith(':python3')))

    def test_stale_configured_path_separate_from_bare(self):
        stale = self.env | {'PATH': str(self.root/'missing PATH directory')}
        with mock.patch.object(module.shutil, 'which', side_effect=lambda name, path:
                               None if path == stale['PATH'] else
                               sys.executable if name in ('python', 'python3') else None):
            report = module.gather_readiness(self.root, self.config, self.env, stale,
                                             sys.executable, timeout=2)
        self.assertEqual(report['interpreter']['status'], 'PASS')
        self.assertEqual(report['shells'][0]['tools'][0]['status'], 'PASS')
        self.assertEqual(report['shells'][1]['tools'][0]['status'], 'GAP')
        self.assertEqual(report['shells'][1]['path'], stale['PATH'])
        self.assertEqual(report['status'], 'GAP')

    def test_successful_exit_without_version_remains_unknown(self):
        for output, status in [('', 'GAP'), ('v1.2.3 synthetic version fixture', 'PASS')]:
            with self.subTest(output=output), \
                 mock.patch.object(module.shutil, 'which', side_effect=lambda name, **kw:
                                   sys.executable if name == 'node' else None), \
                 mock.patch.object(module, 'bounded_command', return_value={
                     'status': 'PASS', 'exitCode': 0, 'stdout': output, 'stderr': ''}), \
                 mock.patch.object(module, 'observe_shell_discovery', return_value={
                     'scope': 'clean-child-shell-discovery', 'status': 'GAP', 'reason': 'controlled fixture'}):
                observed = module.shell_observations(self.env, self.root, sys.executable, 2, 'bare-shell-path')
                node = next(t for t in observed['tools'] if t['name'] == 'node')
                self.assertEqual(node['status'], status)
                self.assertEqual(node['versionOutput'], output)

    def test_duplicate_transport_guards_otherwise_valid_config(self):
        for plugin_duplicate in (False, True):
            config = copy.deepcopy(self.config)
            if plugin_duplicate:
                config['plugins']['invisible-playwright-mcp@feder-cr']['mcp_servers']['invisible_playwright_mcp']['enabled'] = True
            else:
                config['mcp_servers']['duplicate'] = copy.deepcopy(config['mcp_servers']['invisible_playwright_mcp'])
            report = self.gather(config=config)
            self.assertEqual(report['interpreter']['status'], 'PASS')
            self.assertEqual(report['configuration']['failedCheckId'], 'readiness-single-transport')
            self.assertEqual(report['status'], 'FAIL')

    def test_plugin_managed_transport_is_unverified_until_native_observation(self):
        config = {'plugins': {'invisible-playwright-mcp@feder-cr': {'enabled': True}}}
        report = self.gather(config=config)
        self.assertEqual(report['configuration']['status'], 'GAP')
        self.assertEqual(report['configuration']['mode'], 'plugin-managed')
        self.assertEqual(report['status'], 'GAP')
        self.assertFalse(report['nativeReadinessVerified'])

    def test_configuration_scope_command_base_and_pins(self):
        cases = {
            'readiness-upload-scope': lambda c: c['mcp_servers']['invisible_playwright_mcp']['env'].update(INVISIBLE_MCP_UPLOAD_DIRS=str(self.container)),
            'readiness-mcp-base': lambda c: c['mcp_servers']['invisible_playwright_mcp']['args'].__setitem__(1, str(self.root/'missing python.exe')),
            'readiness-mcp-command': lambda c: c['mcp_servers']['invisible_playwright_mcp'].update(command=str(self.root/'missing uvx.exe')),
            'readiness-mcp-arguments': lambda c: c['mcp_servers']['invisible_playwright_mcp']['args'].__setitem__(4, 'invisible-playwright-mcp')}
        for guard, mutate in cases.items():
            with self.subTest(guard=guard):
                config = copy.deepcopy(self.config)
                mutate(config)
                report = self.gather(config=config)
                self.assertEqual(report['interpreter']['status'], 'PASS')
                self.assertEqual(report['configuration']['failedCheckId'], guard)
                self.assertEqual(report['status'], 'FAIL')

    def test_same_file_spelling_does_not_require_reinstallation(self):
        original = self.root/'synthetic original.exe'
        original.write_bytes(b'disposable file identity fixture, never executed')
        alias = self.root/'aynı motor.exe'
        os.link(original, alias)
        try:
            self.assertNotEqual(str(original), str(alias))
            self.assertTrue(module.same_file(original, alias))
            self.assertEqual(module.file_identity(original)['inode'], module.file_identity(alias)['inode'])
        finally:
            alias.unlink()

    def test_clean_child_shell_discovery_observed_separately(self):
        observed = module.observe_shell_discovery(self.env, self.root, timeout=5)
        if os.name == 'nt' and os.environ.get('SystemRoot'):
            self.assertEqual(observed['status'], 'PASS', observed)
            self.assertEqual(observed['discovery']['path'], '')
            self.assertIsInstance(observed['discovery']['commands'], list)
            self.assertFalse(observed['callerInteractiveAliasesObserved'])
        else:
            self.assertEqual(observed['status'], 'GAP')

    def test_existing_project_venv_selected_base_unchanged(self):
        base = pathlib.Path(sys._base_executable)
        before = hashlib.sha256(base.read_bytes()).hexdigest()
        venv.EnvBuilder(with_pip=False).create(self.root/'.venv')
        report = self.gather(interpreter=None)
        selected = self.root/'.venv'/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        self.assertEqual(report['selection']['selectedInterpreter'], str(selected.resolve()))
        self.assertEqual(report['interpreter']['status'], 'PASS', report['interpreter'])
        self.assertEqual(hashlib.sha256(base.read_bytes()).hexdigest(), before)
        self.assertFalse(report['selection']['managedBaseMutation'])

    def test_missing_interpreter_refuses_stdio_without_launch(self):
        report = self.gather(interpreter=self.root/'missing python.exe', stdio_script=self.synthetic())
        self.assertEqual(report['interpreter']['status'], 'GAP')
        self.assertEqual(report['stdio']['failedCheckId'], 'readiness-stdio-interpreter')
        self.assertNotIn('pid', report['stdio'])

    def test_stdio_out_of_scope_refused_before_launch(self):
        path = self.container/'outside.py'
        path.write_text(SYNTHETIC, encoding='utf-8')
        report = self.gather(stdio_script=path)
        self.assertEqual(report['interpreter']['status'], 'PASS')
        self.assertEqual(report['stdio']['failedCheckId'], 'readiness-stdio-scope')
        self.assertNotIn('pid', report['stdio'])

    def test_stdio_timeout_refusal_protocol_and_nonterminal_clean_exit(self):
        cases = [
            ('timeout', 'import time; time.sleep(30)\n', 'GAP', None),
            ('refusal', "import sys,json\nr=json.loads(sys.stdin.readline()); print(json.dumps({'jsonrpc':'2.0','id':r['id'],'error':{'code':-1,'message':'refused'}}),flush=True)\n", 'FAIL', 'readiness-stdio-response'),
            ('bad-json', 'print("not JSON",flush=True)\n', 'FAIL', 'readiness-stdio-protocol'),
            ('incomplete-init', "import sys,json\nr=json.loads(sys.stdin.readline()); print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':{}}),flush=True)\n", 'FAIL', 'readiness-stdio-initialize'),
            ('nonzero-exit', SYNTHETIC+'sys.exit(7)\n', 'FAIL', None),
            ('refuse-exit', SYNTHETIC+'import time; time.sleep(30)\n', 'FAIL', None)]
        for name, source, status, guard in cases:
            with self.subTest(case=name):
                result = module.probe_stdio(self.synthetic(source), sys.executable, self.root, self.env, timeout=.3)
                self.assertEqual(result['status'], status, result)
                if guard: self.assertEqual(result['failedCheckId'], guard)
                self.assertTrue(result['cleanup']['stopped'], result)
                self.assertTrue(result['cleanup']['pipeReadersStopped'])
                self.assertFalse(result['nativeReadinessVerified'])

    def test_pinned_receipt_never_authenticates_native_claim(self):
        path, pin = self.native_receipt()
        report = self.gather(receipt_paths=[(path, pin)])
        attachment = report['nativeReceipts'][0]
        self.assertEqual(attachment['status'], 'ATTACHED_UNAUTHENTICATED')
        self.assertFalse(attachment['providerAuthenticated'])
        self.assertFalse(attachment['nativeReadinessVerified'])
        self.assertTrue(all(s['status'] == 'GAP' for s in report['nativeScopes']))
        self.assertEqual(report['status'], 'GAP')

    def pin_raw(self, raw):
        path = self.root/'caller receipt.json'
        path.write_text(raw, encoding='utf-8')
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def assert_receipt_rejected(self, raw, guard):
        path, pin = self.pin_raw(raw)
        before = path.read_bytes()
        with self.assertRaises(module.PreflightCheckError) as raised:
            module.attach_native_receipt(path, pin, self.root.resolve(), time.time(), 1800)
        self.assertEqual(raised.exception.check_id, guard)
        self.assertEqual(path.read_bytes(), before)

    def test_explicit_receipt_project_root_in_selected_cwd(self):
        path, _ = self.native_receipt()
        valid = json.loads(path.read_text(encoding='utf-8'))
        original = pathlib.Path.cwd()
        try:
            os.chdir(self.root)
            for root in ('missing', '', None, True, [], {}, ' \t\n'):
                with self.subTest(projectRoot=root):
                    changed = copy.deepcopy(valid)
                    if root == 'missing': changed.pop('projectRoot')
                    else: changed['projectRoot'] = root
                    self.assert_receipt_rejected(json.dumps(changed), 'readiness-native-receipt-project')
            changed = copy.deepcopy(valid); changed.pop('projectRoot')
            path, pin = self.pin_raw(json.dumps(changed))
            report = self.gather(receipt_paths=[(path, pin)])
            self.assertEqual(report['nativeReceipts'][0]['failedCheckId'], 'readiness-native-receipt-project')
            self.assertEqual(report['status'], 'FAIL')
        finally:
            os.chdir(original)

    def test_recursive_strict_receipt_json_and_large_integer(self):
        path, _ = self.native_receipt()
        raw = path.read_text(encoding='utf-8')
        invalid = [
            '{"provider":"alternate-browser",'+raw[1:],
            '{"projectRoot":"outside",'+raw[1:],
            raw.replace('"observer":', '"observer":"wrong", "observer":'),
            raw.replace('"toolName":', '"toolName":"wrong", "toolName":')]
        for token in ('NaN', 'Infinity', '-Infinity', '1e400'):
            invalid.extend([raw.replace('"provenance": {', '"provenance": {"extra":'+token+', '),
                            raw.replace('"observations": [{', '"observations": [{"extra":'+token+', ')])
        for malformed in invalid:
            with self.subTest(raw=malformed[:100]):
                self.assert_receipt_rejected(malformed, 'readiness-native-receipt-json')
        valid = json.loads(raw)
        valid['observedAt'] = 10**400
        self.assert_receipt_rejected(json.dumps(valid), 'readiness-native-receipt-freshness')
        self.assertTrue(module.finite_number(10**400))
        path, pin = self.pin_raw(invalid[-1])
        report = self.gather(receipt_paths=[(path, pin)])
        self.assertEqual(report['nativeReceipts'][0]['failedCheckId'], 'readiness-native-receipt-json')
        # Fail receipts retain no raw NaN/overflow value or private extra object.
        module.strict_json_loads(json.dumps(report, allow_nan=False), 'test-strict-output')
        self.assertNotIn('extra', report['nativeReceipts'][0])

    def test_versioned_bounded_observation_and_provenance_contract(self):
        path, _ = self.native_receipt()
        valid = json.loads(path.read_text(encoding='utf-8'))
        malformed = [[None], ['claim'], [{}], [{'claim': 'PASS', 'providerAuthenticated': True}],
                     [valid['observations'][0]]*129]
        for field, value in [('schemaVersion', True), ('toolName', ''), ('toolName', 42),
                             ('toolName', 'x'*129), ('outcome', 'UNKNOWN'), ('summary', ' '),
                             ('summary', 'x'*2049), ('claim', False), ('providerAuthenticated', 'true')]:
            observation = copy.deepcopy(valid['observations'][0]); observation[field] = value
            malformed.append([observation])
        for observations in malformed:
            changed = copy.deepcopy(valid); changed['observations'] = observations
            self.assert_receipt_rejected(json.dumps(changed), 'readiness-native-receipt-observations')
        for field, value in [('schemaVersion', True), ('observer', ' '), ('observer', 'x'*257),
                             ('method', 'x'*129), ('sessionId', None), ('extra', 'private')]:
            changed = copy.deepcopy(valid); changed['provenance'][field] = value
            self.assert_receipt_rejected(json.dumps(changed), 'readiness-native-receipt-provenance')
        path, pin = self.native_receipt(scope='native-browser-download')
        report = self.gather(receipt_paths=[(path, pin)])
        failed = report['nativeReceipts'][0]
        self.assertEqual(failed['failedCheckId'], 'readiness-native-receipt-download-artifact')
        self.assertEqual(failed['evidenceDisposition'], 'REFUSED_MISSING_DOWNLOADED_ARTIFACT')
        self.assertEqual(report['status'], 'FAIL')
        self.assertTrue(all(s['status'] == 'GAP' for s in report['nativeScopes']))

    def test_receipt_byte_bound_before_parse(self):
        self.assert_receipt_rejected(' '* (1024*1024+1), 'readiness-native-receipt-size')

    def test_stdio_nested_nonfinite_and_duplicate_protocol_refused(self):
        raw = '{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{},"capabilities":{},"protocolVersion":"v"}}'
        for malformed in (raw.replace('"serverInfo":{}', '"serverInfo":{"x":NaN}'),
                          raw.replace('"capabilities":{}', '"capabilities":{"x":1e400}'),
                          raw.replace('"id":1', '"id":2,"id":1'),
                          raw.replace('"serverInfo":{}', '"serverInfo":{"x":1,"x":2}')):
            source = 'import sys\nsys.stdin.readline()\nprint('+repr(malformed)+',flush=True)\n'
            result = module.probe_stdio(self.synthetic(source), sys.executable, self.root, self.env, timeout=.5)
            self.assertEqual(result['failedCheckId'], 'readiness-stdio-protocol')
            self.assertEqual(result['status'], 'FAIL')
            self.assertTrue(result['cleanup']['stopped'])
            self.assertTrue(result['cleanup']['pipeReadersStopped'])
            module.strict_json_loads(json.dumps(result, allow_nan=False), 'test-strict-output')

    def test_shell_and_python_json_nonfinite_or_duplicate_fail(self):
        identity = {'executable': sys.executable, 'baseExecutable': sys._base_executable,
                    'prefix': sys.prefix, 'basePrefix': sys.base_prefix, 'version': [3, 12, 1]}
        raw = json.dumps(identity)
        for malformed in ('{"executable":"wrong",'+raw[1:], raw[:-1]+',"extra":NaN}'):
            with mock.patch.object(module, 'bounded_command', return_value={
                    'status': 'PASS', 'stdout': malformed, 'stderr': '', 'exitCode': 0}):
                result = module.observe_python(sys.executable, self.env, self.root, 1)
            self.assertEqual(result['status'], 'FAIL')
            self.assertIn('readiness-python-json', result['reason'])
            self.assertNotIn('engine', result)
        with mock.patch.object(module.os, 'name', 'nt'), \
             mock.patch.object(module.shutil, 'which', return_value=sys.executable), \
             mock.patch.object(module, 'bounded_command', return_value={
                 'status': 'PASS', 'stdout': '{"path":"","commands":[{"x":Infinity}]}',
                 'stderr': '', 'exitCode': 0}):
            result = module.observe_shell_discovery(self.env, self.root, 1)
        self.assertEqual(result['status'], 'FAIL')
        self.assertIn('readiness-shell-json', result['reason'])
        self.assertNotIn('discovery', result)

    def test_cli_strict_serialization_failure_is_controlled_json(self):
        output = self.root/'strict failure.json'
        with mock.patch.object(module, 'gather_readiness', return_value={'status': 'GAP', 'extra': float('nan')}), \
             contextlib.redirect_stdout(io.StringIO()) as stream:
            code = module.main(['--readiness', '--project-root', str(self.root), '--output', str(output)])
        self.assertEqual(code, 1)
        report = module.strict_json_loads(output.read_bytes(), 'test-strict-output')
        self.assertEqual(report['status'], 'FAIL')
        self.assertIn('readiness-output-json', report['error'])
        self.assertFalse(report['nativeReadinessVerified'])
        schema = json.loads((SOURCE.parent/'runtime-manifest.schema.json').read_text(encoding='utf-8'))
        self.assertEqual(schema_errors(report, schema, schema), [])
        self.assertEqual(module.strict_json_loads(stream.getvalue(), 'test-strict-output')['status'], 'FAIL')

    def test_receipt_negative_guards_with_otherwise_valid_inputs(self):
        outside = self.container/'outside.txt'
        outside.write_text('outside synthetic only', encoding='utf-8')
        cases = [
            ({'projectRoot': str(self.container)}, 'readiness-native-receipt-project'),
            ({'observedAt': time.time()-3600}, 'readiness-native-receipt-freshness'),
            ({'observedAt': time.time()+3600}, 'readiness-native-receipt-freshness'),
            ({'observedAt': True}, 'readiness-native-receipt-freshness'),
            ({'files': [str(outside)]}, 'readiness-native-receipt-files'),
            ({'provider': 'alternate-browser'}, 'readiness-native-receipt-provider'),
            ({'scope': 'cli-stdio-synthetic'}, 'readiness-native-receipt-provider'),
            ({'provenance': {'observer': 'test'}}, 'readiness-native-receipt-provenance'),
            ({'observations': []}, 'readiness-native-receipt-observations'),
            ({'schemaVersion': True}, 'readiness-native-receipt-format')]
        for update, guard in cases:
            with self.subTest(guard=guard, update=update):
                path, pin = self.native_receipt(**update)
                report = self.gather(receipt_paths=[(path, pin)])
                self.assertEqual(report['configuration']['status'], 'PASS')
                self.assertEqual(report['nativeReceipts'][0]['failedCheckId'], guard)
                self.assertEqual(report['status'], 'FAIL')
        path, pin = self.native_receipt()
        for receipt_path, receipt_pin, guard in [
                (path, '0'*64, 'readiness-native-receipt-pin'),
                (outside, pin, 'readiness-native-receipt-scope')]:
            report = self.gather(receipt_paths=[(receipt_path, receipt_pin)])
            self.assertEqual(report['nativeReceipts'][0]['failedCheckId'], guard)
            self.assertEqual(report['status'], 'FAIL')

    def test_cli_normal_and_optimized_exit_and_schema_identity(self):
        schema = json.loads((SOURCE.parent/'runtime-manifest.schema.json').read_text(encoding='utf-8'))
        for optimized in (False, True):
            output = self.root/('manifest optimized.json' if optimized else 'manifest normal.json')
            command = [sys.executable, '-B'] + (['-O'] if optimized else []) + [str(SOURCE),
                       '--manifest', '--project-root', str(self.root), '--interpreter', sys.executable,
                       '--output', str(output), '--timeout', '1']
            result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stderr+result.stdout)
            report = json.loads(output.read_text(encoding='utf-8'))
            module.strict_json_loads(output.read_bytes(), 'test-strict-output')
            self.assertEqual(schema_errors(report, schema, schema), [])
            self.assertEqual(report['schema'], schema['properties']['schema']['const'])
            self.assertEqual(report['schemaVersion'], 1)
            self.assertFalse(report['nativeReadinessVerified'])
            self.assertEqual(report['status'], 'GAP')
            fail = subprocess.run(command+['--browser'], env=self.env, capture_output=True, text=True, timeout=30)
            self.assertEqual(fail.returncode, 1)
            failure = module.strict_json_loads(output.read_bytes(), 'test-strict-output')
            self.assertIn('readiness-cli-scope', failure['error'])
            self.assertEqual(schema_errors(failure, schema, schema), [])

    def test_attached_and_refused_receipts_match_schema_shapes(self):
        schema = json.loads((SOURCE.parent/'runtime-manifest.schema.json').read_text(encoding='utf-8'))
        path, pin = self.native_receipt()
        fixture = module.strict_json_loads(path.read_bytes(), 'test-strict-input')
        self.assertEqual(schema_errors(fixture, schema['$defs']['nativeReceiptInput'], schema), [])
        valid_report = self.gather(receipt_paths=[(path, pin)])
        self.assertEqual(schema_errors(valid_report, schema, schema), [])
        self.assertEqual(valid_report['nativeReceipts'][0]['observationCount'], 1)
        self.assertEqual(valid_report['nativeReceipts'][0]['status'], 'ATTACHED_UNAUTHENTICATED')
        for observations in ([None], ['claim'], [{}]):
            invalid = copy.deepcopy(fixture); invalid['observations'] = observations
            self.assertTrue(schema_errors(invalid, schema['$defs']['nativeReceiptInput'], schema))
        path, pin = self.native_receipt(scope='native-browser-download')
        failed_report = self.gather(receipt_paths=[(path, pin)])
        self.assertEqual(schema_errors(failed_report, schema, schema), [])
        module.strict_json_loads(json.dumps(failed_report, allow_nan=False), 'test-strict-output')


    def test_wrong_boolean_root_config_shape_and_relative_paths_fail_closed(self):
        for root in ('', True, None):
            with self.assertRaises(module.PreflightCheckError): self.gather(project_root=root)
        for replacement in (1, [], 'foreign'):
            broken = copy.deepcopy(self.config)
            broken['mcp_servers']['invisible_playwright_mcp']['enabled'] = replacement
            report = self.gather(config=broken)
            self.assertEqual(report['configuration']['status'], 'FAIL')
            self.assertEqual(report['configuration']['failedCheckId'], 'readiness-config-shape')
            self.assertFalse(report['nativeReadinessVerified'])
        for key, value, guard in [('command', '.', 'readiness-mcp-command'),
                                  ('command', '', 'readiness-mcp-command'),
                                  ('env', {'INVISIBLE_MCP_UPLOAD_DIRS': '.'}, 'readiness-upload-scope')]:
            broken = copy.deepcopy(self.config)
            broken['mcp_servers']['invisible_playwright_mcp'][key] = value
            report = self.gather(config=broken)
            self.assertEqual(report['configuration']['status'], 'FAIL')
            self.assertEqual(report['configuration']['failedCheckId'], guard)

    def test_download_records_actual_local_size_hash_without_authenticating_origin(self):
        artifact = self.root/'Türkçe indirilen artifact.json'
        data = '{"metin":"İstanbul ı ş"}\n'.encode('utf-8')
        artifact.write_bytes(data)
        path, pin = self.native_receipt(scope='native-browser-download', files=[str(artifact)])
        report = self.gather(receipt_paths=[(path, pin)])
        attached = report['nativeReceipts'][0]
        self.assertEqual(attached['status'], 'ATTACHED_UNAUTHENTICATED')
        self.assertEqual(attached['artifacts'], [{'resolvedPath': str(artifact.resolve()), 'size': len(data),
                                                 'sha256': hashlib.sha256(data).hexdigest()}])
        self.assertFalse(report['nativeReadinessVerified'])
        self.assertEqual(report['status'], 'GAP')
        artifact.write_bytes(b'')
        failed = self.gather(receipt_paths=[(path, pin)])
        self.assertEqual(failed['nativeReceipts'][0]['failedCheckId'], 'readiness-native-receipt-download-artifact')
        self.assertEqual(failed['nativeReceipts'][0]['evidenceDisposition'], 'REFUSED_MISSING_DOWNLOADED_ARTIFACT')
        schema = json.loads((SOURCE.parent/'runtime-manifest.schema.json').read_text(encoding='utf-8'))
        self.assertEqual(schema_errors(report, schema, schema), [])

    @unittest.skipUnless(os.name == 'nt', 'PowerShell local activation contract is Windows-specific')
    def test_activation_validates_before_process_local_mutation(self):
        powershell = module.shutil.which('powershell')
        if not powershell: self.skipTest('PowerShell unavailable')
        activation = SOURCE.parent/'Activate-Environment.ps1'
        probe = self.root/'activation probe.ps1'
        probe.write_text('''param([string]$Activation,[string]$Root,[string]$Interpreter)
$initialPath = $env:Path
$initialUv = $env:UV_PYTHON
try { . $Activation -ProjectRoot $Root -Interpreter $Interpreter | Out-Null; $outcome = 'PASS' }
catch { $outcome = 'REFUSED' }
@{outcome=$outcome; changedPath=($env:Path -ne $initialPath); changedUv=($env:UV_PYTHON -ne $initialUv); uv=$env:UV_PYTHON} | ConvertTo-Json -Compress
''', encoding='utf-8-sig')
        for interpreter, expected in [(sys.executable, 'PASS'), (str(self.root/'missing.exe'), 'REFUSED')]:
            result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                     '-File', str(probe), str(activation), str(self.root), interpreter],
                                    capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = module.strict_json_loads(result.stdout, 'test-activation-json')
            self.assertEqual(observed['outcome'], expected)
            self.assertEqual(observed['changedPath'], expected == 'PASS')
            if expected == 'REFUSED': self.assertFalse(observed['changedUv'])
            else: self.assertTrue(module.same_file(observed['uv'], sys.executable))


if __name__ == '__main__':
    unittest.main()
