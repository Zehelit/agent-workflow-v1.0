"""Deterministic artifact, installation ACLs and transactional update recovery."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT=Path(__file__).resolve().parents[2]
PYTHON=[sys.executable,'-X','utf8','-B']

def hashes(root):
    return {path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob('*') if path.is_file()}

class CandidatePackageTests(unittest.TestCase):
    def test_version_11_manifest_profile_and_runtime_agree(self):
        build, receipt = self.build('version-11')
        self.assertEqual(receipt['version'], '1.1')
        self.assertEqual(receipt['identity'], 'agent-workflow-v1-1')
        package = build / 'agent-workflow-v1-1'
        profile = json.loads((package/'candidate-profile.json').read_text(encoding='utf-8'))
        manifest = json.loads((package/'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(profile['version'], '1.1')
        self.assertEqual(manifest['version'], '1.1')
        self.assertEqual(profile['identity'], 'agent-workflow-v1-1')
        self.assertEqual(profile['display_name'], 'agent-workflow-v1.1')

    def test_version_11_new_run_preserves_previous_identity_records(self):
        build, receipt = self.build('identity-isolation')
        project = self.base / 'identity-project'
        legacy = project / ('.agent-workflow-v1-' + '0') / 'runs/old-run'
        legacy.mkdir(parents=True)
        (legacy / 'run-binding.json').write_bytes(b'{"historical":"immutable evidence"}')
        before = hashes(legacy)
        result = self.cli(build, receipt, '--project', str(project), '--run-id', 'identity-check',
                          'workflow', 'validate')
        self.assertEqual(json.loads(result.stdout)['identity'], 'agent-workflow-v1-1')
        current = project / '.agent-workflow-v1-1/runs/identity-check'
        self.assertTrue((current / 'run-binding.json').is_file())
        self.assertEqual(json.loads((current / 'run-binding.json').read_bytes())['identity'],
                         'agent-workflow-v1-1')
        self.assertEqual(hashes(legacy), before)

    def test_version_11_refuses_previous_manifest_identity_without_binding(self):
        build, receipt = self.build('identity-refusal')
        package = build / receipt['identity']
        manifest = package / 'manifest.json'
        data = json.loads(manifest.read_bytes())
        data['identity'] = 'agent-workflow-v1-' + '0'
        manifest.write_text(json.dumps(data), encoding='utf-8')
        receipt['manifest_sha256'] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        project = self.base / 'refusal-project'
        project.mkdir()
        before = hashes(project)
        self.cli(build, receipt, '--project', str(project), '--run-id', 'refuse-old',
                 'workflow', 'validate', good=False)
        self.assertEqual(hashes(project), before)
        self.assertFalse((project / '.agent-workflow-v1-1').exists())

    def test_version_11_reserves_previous_and_current_state_paths(self):
        path = ROOT / 'skills/agent-workflow-v1-1/scripts/workflow.py'
        spec = importlib.util.spec_from_file_location('identity_workflow', path)
        workflow = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(workflow)
        project = self.base / 'reserved-project'
        project.mkdir()
        engine = workflow.Workflow(project, self.base, role_catalog={})
        for namespace in ('.agent-workflow', '.agent-workflow-v1-' + '0', '.agent-workflow-v1-1'):
            with self.subTest(namespace=namespace):
                name = namespace + '/runs/foreign/state.json'
                with self.assertRaises(workflow.Error):
                    engine.file(name)
                with self.assertRaises(workflow.Error):
                    workflow.validate_hashes({name: '0' * 64}, '$.files')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='candidate-package-')
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)

    def build(self, name, source=ROOT):
        dest=self.base/name
        result=subprocess.run([*PYTHON,str(ROOT/'tooling/build-candidate.py'),'--source',str(source),'--output',str(dest)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        return dest, json.loads(result.stdout)

    def cli(self, build, receipt, *commands, good=True):
        package=build/'agent-workflow-v1-1'
        result=subprocess.run([*PYTHON,str(package/'scripts/candidate.py'),'--manifest',str(package/'manifest.json'),
            '--manifest-sha256',receipt['manifest_sha256'],'--version','1.1',*commands],capture_output=True,text=True,timeout=15)
        if good:self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        else:self.assertNotEqual(result.returncode,0,result.stdout)
        return result

    def test_two_clean_builds_are_byte_identical_identity_and_private_watcher(self):
        a,ar=self.build('a');b,br=self.build('b')
        self.assertEqual(ar,br)
        self.assertEqual(hashes(a),hashes(b))
        package=a/'agent-workflow-v1-1'
        skill=(package/'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('name: agent-workflow-v1-1',skill)
        interface=(package/'agents/openai.yaml').read_text(encoding='utf-8')
        self.assertIn('display_name: "agent-workflow-v1.1"',interface)
        self.assertIn('allow_implicit_invocation: false',interface)
        manifest=json.loads((package/'manifest.json').read_bytes())
        self.assertEqual(manifest['files']['private/limit-watcher/scripts/monitor.py'],hashlib.sha256((ROOT/'skills/agent-workflow-v1-1/private/limit-watcher/scripts/monitor.py').read_bytes()).hexdigest())
        with zipfile.ZipFile(a/'agent-workflow-v1-1.zip') as archive:
            self.assertIsNone(archive.testzip())
            self.assertTrue(all(name.startswith('agent-workflow-v1-1/') for name in archive.namelist()))
            self.assertFalse(any('__pycache__' in name or '/tests/' in name for name in archive.namelist()))
            skill_paths = {'SKILL.md', 'private/limit-watcher/SKILL.md'} | {
                'roles/' + role + '/SKILL.md' for role in
                ('architect', 'coordinator', 'implementer', 'researcher', 'verifier')}
            memory_paths = {'private/limit-watcher/MEMORY.md'} | {
                'roles/' + role + '/MEMORY.md' for role in
                ('architect', 'coordinator', 'implementer', 'researcher', 'verifier')}
            packaged = {name.removeprefix('agent-workflow-v1-1/') for name in archive.namelist()}
            self.assertEqual({name for name in packaged if name.endswith('SKILL.md')}, skill_paths)
            self.assertEqual({name for name in packaged if name.endswith('MEMORY.md')}, memory_paths)
            for name in skill_paths | memory_paths:
                self.assertEqual(manifest['files'][name], hashlib.sha256(
                    archive.read('agent-workflow-v1-1/' + name)).hexdigest())
        result=subprocess.run([*PYTHON,str(ROOT/'tooling/build-candidate.py'),'--output',str(a)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)

    def test_every_packaged_skill_has_its_own_explicit_invocation_policy(self):
        try:
            import yaml
        except ImportError:
            self.skipTest('PyYAML is required to parse Codex skill metadata')
        build, receipt = self.build('invocation-policy')
        package = build / 'agent-workflow-v1-1'
        manifest = json.loads((package / 'manifest.json').read_bytes())
        skills = sorted(package.rglob('SKILL.md'))
        self.assertEqual(len(skills), 7)
        for skill in skills:
            with self.subTest(skill=skill.relative_to(package).as_posix()):
                metadata = skill.parent / 'agents/openai.yaml'
                self.assertTrue(metadata.is_file(), 'a parent skill policy does not protect this skill')
                value = yaml.safe_load(metadata.read_text(encoding='utf-8'))
                self.assertIs(value['policy']['allow_implicit_invocation'], False)
                relative = metadata.relative_to(package).as_posix()
                self.assertEqual(manifest['files'][relative], hashlib.sha256(metadata.read_bytes()).hexdigest())

    def test_fresh_noop_update_edited_refusal_without_archive(self):
        build,receipt=self.build('original')
        dest=self.base/'skills/agent-workflow-v1-1'
        old=self.base/'skills/unrelated-reader'
        watcher=self.base/'skills/unrelated-skill'
        old.mkdir(parents=True);watcher.mkdir()
        (old/'SKILL.md').write_text('preserved unrelated skill')
        (watcher/'role.json').write_text('preserved unrelated role')
        preserved=(hashes(old),hashes(watcher))
        first=self.cli(build,receipt,'install','--destination',str(dest))
        self.assertTrue(json.loads(first.stdout)['changed'])
        original=hashes(dest)
        noop=self.cli(build,receipt,'install','--destination',str(dest))
        self.assertFalse(json.loads(noop.stdout)['changed'])
        self.assertEqual(hashes(dest),original)
        edited=dest/'SKILL.md';raw=edited.read_bytes();edited.write_bytes(raw+b'local user edit')
        before=hashes(dest)
        self.cli(build,receipt,'install','--destination',str(dest),'--update',good=False)
        self.assertEqual(hashes(dest),before)
        edited.write_bytes(raw)
        source=self.base/'changed-source'
        shutil.copytree(ROOT/'skills/agent-workflow-v1-1',source/'skills/agent-workflow-v1-1',ignore=shutil.ignore_patterns('__pycache__','tests'))
        changed=source/'skills/agent-workflow-v1-1/references/coexistence.md';changed.write_bytes(changed.read_bytes()+b'\nApproved staging update fixture.\n')
        updated,updated_receipt=self.build('updated',source)
        self.cli(updated,updated_receipt,'install','--destination',str(dest),good=False)
        self.assertEqual(hashes(dest),original)
        self.cli(updated,updated_receipt,'install','--destination',str(dest),'--update')
        self.assertNotEqual(hashes(dest),original)
        backup=dest.parent/'.agent-workflow-v1-1-rollback.zip'
        self.assertFalse(backup.exists())
        self.cli(updated,updated_receipt,'rollback','--destination',str(dest),good=False)
        self.assertFalse(list(dest.parent.glob('candidate-install-*')))
        self.assertEqual((hashes(old),hashes(watcher)),preserved)

    def test_generated_bytecode_does_not_block_an_owned_update(self):
        original, receipt = self.build('bytecode-original')
        dest = self.base / 'bytecode-skills/agent-workflow-v1-1'
        self.cli(original, receipt, 'install', '--destination', str(dest))
        cache = dest / 'scripts/__pycache__/workflow.cpython-312.pyc'
        cache.parent.mkdir()
        cache.write_bytes(b'disposable bytecode fixture')
        noop = self.cli(original, receipt, 'install', '--destination', str(dest), '--update')
        self.assertFalse(json.loads(noop.stdout)['changed'])

        source = self.base / 'bytecode-changed-source'
        shutil.copytree(ROOT / 'skills/agent-workflow-v1-1', source / 'skills/agent-workflow-v1-1',
                        ignore=shutil.ignore_patterns('__pycache__', 'tests'))
        changed = source / 'skills/agent-workflow-v1-1/references/coexistence.md'
        changed.write_bytes(changed.read_bytes() + b'\nBytecode update fixture.\n')
        updated, updated_receipt = self.build('bytecode-updated', source)
        applied = self.cli(updated, updated_receipt, 'install', '--destination', str(dest), '--update')
        self.assertTrue(json.loads(applied.stdout)['changed'])
        self.assertFalse(cache.exists())

        edited = dest / 'SKILL.md'
        edited.write_bytes(edited.read_bytes() + b'local user edit')
        self.cli(updated, updated_receipt, 'install', '--destination', str(dest), '--update', good=False)

    def test_failed_publication_restores_existing_package(self):
        original,receipt=self.build('transaction-original')
        dest=self.base/'transaction-skills/agent-workflow-v1-1'
        self.cli(original,receipt,'install','--destination',str(dest))
        before=hashes(dest)
        source=self.base/'transaction-source'
        shutil.copytree(ROOT/'skills/agent-workflow-v1-1',source/'skills/agent-workflow-v1-1',ignore=shutil.ignore_patterns('__pycache__','tests'))
        changed=source/'skills/agent-workflow-v1-1/references/coexistence.md'
        changed.write_bytes(changed.read_bytes()+b'\nTransaction test.\n')
        build,pin=self.build('transaction-updated',source)
        package=build/'agent-workflow-v1-1'
        spec=importlib.util.spec_from_file_location('transaction_candidate',package/'scripts/candidate.py')
        candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
        runtime=candidate.Runtime(argparse.Namespace(manifest=str(package/'manifest.json'),
            manifest_sha256=pin['manifest_sha256'],version='1.1',mode='install'))
        original_rename=Path.rename
        def fail_publish(path,target):
            if path.name=='package' and Path(target)==dest:
                raise PermissionError('injected publication denial')
            return original_rename(path,target)
        with mock.patch.object(Path,'rename',fail_publish):
            with self.assertRaisesRegex(PermissionError,'injected publication'):
                candidate.stage_install(runtime,dest,update=True)
        self.assertEqual(hashes(dest),before)
        self.assertFalse(list(dest.parent.glob('candidate-install-*')))
        self.assertFalse(list(dest.parent.glob('*.zip')))

    def test_failed_restoration_keeps_only_copy_for_recovery(self):
        original,receipt=self.build('recovery-original')
        dest=self.base/'recovery-skills/agent-workflow-v1-1'
        self.cli(original,receipt,'install','--destination',str(dest))
        before=hashes(dest)
        source=self.base/'recovery-source'
        shutil.copytree(ROOT/'skills/agent-workflow-v1-1',source/'skills/agent-workflow-v1-1',ignore=shutil.ignore_patterns('__pycache__','tests'))
        changed=source/'skills/agent-workflow-v1-1/references/coexistence.md'
        changed.write_bytes(changed.read_bytes()+b'\nRecovery test.\n')
        build,pin=self.build('recovery-updated',source)
        package=build/'agent-workflow-v1-1'
        spec=importlib.util.spec_from_file_location('recovery_candidate',package/'scripts/candidate.py')
        candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
        runtime=candidate.Runtime(argparse.Namespace(manifest=str(package/'manifest.json'),
            manifest_sha256=pin['manifest_sha256'],version='1.1',mode='install'))
        original_rename=Path.rename
        def deny_destination(path,target):
            if Path(target)==dest:
                raise PermissionError('injected destination denial')
            return original_rename(path,target)
        with mock.patch.object(Path,'rename',deny_destination):
            with self.assertRaises(PermissionError):
                candidate.stage_install(runtime,dest,update=True)
        pending=list(dest.parent.glob('candidate-install-*'))
        self.assertEqual(len(pending),1)
        self.assertEqual(hashes(pending[0]/'previous'),before)
        self.assertFalse(dest.exists())
        self.assertFalse(list(dest.parent.glob('*.zip')))

    def test_candidate_runs_directly_without_skill_or_watcher_launch(self):
        build,receipt=self.build('direct-entry')
        project=self.base/'direct-project'
        project.mkdir()
        result=self.cli(build,receipt,'--project',str(project),'--run-id','direct-entry',
            'workflow','validate')
        response=json.loads(result.stdout)
        self.assertEqual(response['identity'],'agent-workflow-v1-1')
        run=project/'.agent-workflow-v1-1/runs/direct-entry'
        self.assertTrue((run/'run-binding.json').is_file())
        self.assertFalse((run/'watcher').exists())
        self.assertFalse((run/'state.json').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL regression')
    def test_install_and_update_preserve_normal_user_access(self):
        icacls=Path(os.environ['SystemRoot'])/'System32'/'icacls.exe'
        identity=subprocess.run(['whoami'],capture_output=True,text=True,check=True).stdout.strip()
        def command(*args):
            result=subprocess.run([str(icacls),*map(str,args)],capture_output=True,text=True,check=True)
            return result.stdout
        def assert_inherited_full_access(path):
            lines=command(path).casefold().splitlines()
            user_lines=[line for line in lines if identity.casefold()+':(i)' in line]
            self.assertTrue(any('(f)' in line and '(deny)' not in line for line in user_lines),path)
        command(self.base,'/reset')
        dest=self.base/'skills/agent-workflow-v1-1'
        original,receipt=self.build('acl-original')
        self.cli(original,receipt,'install','--destination',str(dest))
        for path in (dest,dest/'SKILL.md',dest/'scripts/candidate.py'):
            assert_inherited_full_access(path)
        source=self.base/'acl-updated-source'
        shutil.copytree(ROOT/'skills/agent-workflow-v1-1',source/'skills/agent-workflow-v1-1',ignore=shutil.ignore_patterns('__pycache__','tests'))
        changed=source/'skills/agent-workflow-v1-1/references/coexistence.md'
        changed.write_bytes(changed.read_bytes()+b'\nACL update fixture.\n')
        updated,updated_receipt=self.build('acl-updated',source)
        self.cli(updated,updated_receipt,'install','--destination',str(dest),'--update')
        for path in (dest,dest/'SKILL.md',dest/'scripts/candidate.py'):
            assert_inherited_full_access(path)

if __name__=='__main__':unittest.main(verbosity=2)
