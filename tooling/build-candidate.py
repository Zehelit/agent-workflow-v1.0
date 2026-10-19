"""Build deterministic agent-workflow-v1.0 payload and ZIP without installing."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

IDENTITY = 'agent-workflow-v1-0'
ROOT = Path(__file__).resolve().parents[1]

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def encode(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode('utf-8')

def payload_sources(source):
    package = source / 'skills/agent-workflow-v1-0'
    files = {}
    for path in sorted(package.rglob('*')):
        rel = path.relative_to(package)
        if path.is_file() and '__pycache__' not in rel.parts and path.suffix != '.pyc' and rel.parts[0] in {'SKILL.md', 'candidate-profile.json', 'agents', 'roles', 'scripts', 'references', 'private'}:
            files[rel.as_posix()] = path
    return files

def build(source, output):
    source, output = Path(source).resolve(), Path(output).absolute()
    if output.exists():
        raise ValueError('build destination must be new')
    if output.is_relative_to(source / 'skills/agent-workflow-v1-0'):
        raise ValueError('build destination overlaps payload source')
    files = payload_sources(source)
    for path in files.values():
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError('regular unlinked source required')
    manifest = dict(schema_version=1, identity=IDENTITY, version='1.0', files={name: sha(path) for name, path in sorted(files.items())})
    output.mkdir(parents=True)
    package = output / IDENTITY
    package.mkdir()
    for name, path in files.items():
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        if sha(target) != manifest['files'][name]:
            raise ValueError('source changed during build')
    (package / 'manifest.json').write_bytes(encode(manifest))
    archive = output / (IDENTITY + '.zip')
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
        for path in sorted(package.rglob('*')):
            if path.is_file():
                info = zipfile.ZipInfo(IDENTITY + '/' + path.relative_to(package).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                zipped.writestr(info, path.read_bytes(), compresslevel=9)
    receipt = dict(identity=IDENTITY, version='1.0', manifest_sha256=sha(package / 'manifest.json'), zip_sha256=sha(archive), files=len(files), activated=False)
    (output / 'build-receipt.json').write_bytes(encode(receipt))
    return receipt

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=str(ROOT))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.source, args.output)))
    except (OSError, ValueError) as error:
        print(json.dumps({'error': str(error)}))
        return 2
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
