"""Prepare harnesses from their shipped descriptors in harnesses/<id>.json.

Each descriptor pins downloads by SHA-256 and lists install steps from a fixed set,
so preparing a harness never runs scripts supplied by the package. Preparation is
per harness: download, install, seal, then a capability check on the starter.
No global Codex plugins are changed. Checks never call a model.
"""
import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from exam_bench.common import write_json


def download(name, record):
    path = ROOT / '.local/downloads/harnesses' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        url = record['url']
        if '/releases/download/' in url:
            url += '?download=1'
        request = urllib.request.Request(url, headers={'User-Agent': '1c-agent-exam-benchmark'})
        partial = path.with_suffix(path.suffix + '.partial')
        with urllib.request.urlopen(request, timeout=120) as response, partial.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        partial.replace(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
        raise ValueError('Release checksum differs from pinned download: ' + name)
    return path


def unzip(path, target):
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if not (target / member.filename).resolve().is_relative_to(target.resolve()):
                raise ValueError('Archive path escapes extraction directory')
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Archive symlink refused')
        archive.extractall(target)
        if os.name != 'nt':
            # zipfile drops Unix permissions; restore the execute bits the release set.
            for member in archive.infolist():
                mode = (member.external_attr >> 16) & 0o777
                if mode & 0o111 and not member.is_dir():
                    (target / member.filename).chmod(mode | 0o600)


def untar(path, target):
    """tar.gz with the standard library's 'data' filter: no links out, no devices, no absolute paths."""
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path) as archive:
        archive.extractall(target, filter='data')


def pip_target(target, *requirements):
    # A cache-busting query avoids stale CDN bodies; pip still verifies the
    # official index hashes and HTTPS endpoint. Dependencies never enter .venv.
    script = '''import sys
from pip._internal.network.session import PipSession
original=PipSession.request
def request(self,method,url,*args,**kwargs):
 if 'files.pythonhosted.org/packages/' in url and '?' not in url:url+='?download=1'
 return original(self,method,url,*args,**kwargs)
PipSession.request=request
from pip._internal.cli.main import main
raise SystemExit(main(sys.argv[1:]))
'''
    subprocess.run([sys.executable, '-c', script, 'install', '--disable-pip-version-check', '--no-compile',
                    '--target', str(target), *map(str, requirements)], check=True)


def npm_locked(project, system=None):
    """Dependencies from the author's lockfile only; optional packages for other OS or CPU are skipped."""
    system = (system or target()).split('-')
    npm_os = {'windows': 'win32', 'macos': 'darwin', 'linux': 'linux'}.get(system[0], system[0])
    lock = json.loads((project / 'package-lock.json').read_text(encoding='utf-8'))
    for relative, item in lock['packages'].items():
        if not relative or ('os' in item and npm_os not in item['os']) or ('cpu' in item and system[1] not in item['cpu']):
            continue
        dest = project / relative
        if not dest.resolve().is_relative_to(project.resolve()):
            raise ValueError('Node dependency escapes project')
        blob = urllib.request.urlopen(item['resolved'], timeout=60).read()
        algorithm, expected = item['integrity'].split('-', 1)
        if base64.b64encode(hashlib.new(algorithm, blob).digest()).decode() != expected:
            raise ValueError('Node dependency differs from author lockfile')
        import io
        with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                path = dest / Path(member.name).relative_to('package')
                if not path.resolve().is_relative_to(dest.resolve()):
                    raise ValueError('Node package archive escapes target')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile(member).read())


CATALOGUE = ROOT / 'harnesses'
STEPS = {'copy', 'unzip', 'untar', 'unzip-source', 'copy-tree', 'verified-entrypoint', 'pip', 'pip-source', 'node', 'npm-lock', 'github-tree'}
BUILTIN_PROBES = {'unica', 'rlm', 'bsl-analyzer', 'bsl-ls', 'code-index', 'skill-script', 'toolkit'}


def target():
    system = {'Windows': 'windows', 'Darwin': 'macos', 'Linux': 'linux'}.get(platform.system(), platform.system().lower())
    machine = platform.machine().lower()
    return system + '-' + ('arm64' if machine in ('arm64', 'aarch64') else 'x64' if machine in ('amd64', 'x86_64') else machine)


def validate(doc, name):
    if doc.get('schema_version') != 1 or doc.get('id') != name:
        raise ValueError(f'{name}: descriptor id must match the file name')
    probe = doc.get('probe')
    if not (probe is None
            or isinstance(probe, str) and probe in BUILTIN_PROBES
            or isinstance(probe, dict) and isinstance(probe.get('tool'), str)):
        raise ValueError(f'{name}: unknown probe')
    for key, value in doc.get('targets', {}).items():
        for asset, record in value.get('downloads', {}).items():
            if not record.get('url', '').startswith('https://') or len(record.get('sha256', '')) != 64:
                raise ValueError(f'{name}: {asset} needs an https URL and SHA-256')
        for step in value.get('install', []):
            if len(set(step) & STEPS) != 1:
                raise ValueError(f'{name}: unsupported install step {sorted(step)}')
        if 'command' not in value.get('configuration', {}):
            raise ValueError(f'{name}: {key} configuration needs a command')
    return doc


def catalogue():
    return {p.stem: validate(json.loads(p.read_text(encoding='utf-8')), p.stem) for p in sorted(CATALOGUE.glob('*.json'))}


def descriptor(name):
    path = CATALOGUE / (name + '.json')
    if not path.is_file():
        raise ValueError('No shipped descriptor: ' + name)
    return validate(json.loads(path.read_text(encoding='utf-8')), name)


def inside(base, relative):
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError('Install step escapes the package directory')
    return path


def run_step(step, assets, package, source, node):
    kind = next(iter(set(step) & STEPS))
    value = step[kind]
    to = inside(package, step.get('to', '.'))
    if kind == 'copy':
        to.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(assets[value], to)
        if step.get('executable'):
            to.chmod(0o755)
    elif kind == 'unzip':
        unzip(assets[value], to)
    elif kind == 'untar':
        untar(assets[value], to)
    elif kind == 'unzip-source':
        unzip(assets[value], source)
    elif kind == 'copy-tree':
        shutil.copytree(next(source.glob(value)), to, dirs_exist_ok=True)
    elif kind == 'verified-entrypoint':
        manifest = json.loads(assets[step['manifest']].read_text(encoding='utf-8'))
        with tarfile.open(assets[value]) as archive:
            member = archive.getmember(manifest['entrypoint'])
            if not member.isfile():
                raise ValueError('Runtime entrypoint is not a regular file')
            binary = archive.extractfile(member).read()
        if hashlib.sha256(binary).hexdigest() != next(x['sha256'] for x in manifest['files'] if x['path'] == member.name):
            raise ValueError('Entrypoint checksum differs from the official manifest')
        to.write_bytes(binary)
        to.chmod(0o755)
    elif kind == 'pip':
        requirements = inside(ROOT, value)
        pip_target(to, '-r', requirements)
    elif kind == 'pip-source':
        pip_target(to, '--upgrade', '--no-deps', next(p for p in source.glob(value) if p.is_dir()))
    elif kind == 'node':
        if not node:
            raise ValueError('Нужен Node.js: установите его и повторите подготовку')
        to = inside(package, value); to.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(node, to); to.chmod(0o755)
    elif kind == 'npm-lock':
        npm_locked(inside(package, value))
    elif kind == 'github-tree':
        url = f'https://api.github.com/repos/{value}/git/trees/{step["commit"]}?recursive=1'
        tree = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'exam-benchmark'}), timeout=30))
        for item in tree['tree']:
            if item['type'] == 'blob' and item['path'].startswith(step['prefix']):
                path = inside(to, item['path']); path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(urllib.request.urlopen(f'https://raw.githubusercontent.com/{value}/{step["commit"]}/' + item['path'], timeout=30).read())


def legacy(base):
    """A folder holding anything besides package/ and source/ (a seal, a profile, another layout) is an
    earlier installation; those two alone are left by an interrupted attempt, which starts again."""
    return base.is_dir() and any(p.name not in ('package', 'source') for p in base.iterdir())


def legacy_message(name):
    return (f'Папка .local/harnesses/{name} занята установкой прежнего формата (перенос стенда). '
            'Переименуйте её, чтобы стенд подготовил харнес из описания.')


def install(name, node=None):
    """Install one harness for this OS; an already sealed installation is never overwritten."""
    doc = descriptor(name)
    spec = doc['targets'].get(target())
    if not spec:
        raise ValueError(f'Для {target()} у харнеса {name} пакета нет')
    base = ROOT / '.local/harnesses' / name
    registry_path = ROOT / '.local/harnesses/portable.json'
    records = json.loads(registry_path.read_text(encoding='utf-8')) if registry_path.is_file() else {}
    if name not in records and legacy(base):
        raise ValueError(legacy_message(name))
    if (base / 'package-manifest.json').is_file():
        from exam_bench.portable_harnesses import verify
        verify(name, require_probe=False)
        return False
    assets = {asset: download(asset, record) for asset, record in spec['downloads'].items()}
    for leftover in ('package', 'source'):
        shutil.rmtree(base / leftover, ignore_errors=True)  # an interrupted attempt starts again
    package = base / 'package'
    package.mkdir(parents=True)
    for step in spec['install']:
        run_step(step, assets, package, base / 'source', node)
    records[name] = spec['configuration']
    write_json(registry_path, records)
    from tools.check_native_harnesses import seal
    seal(name, target())
    return True


def prepare(name, node=None):
    """Install, then prove the harness works on the starter. True when it can be used for an exam."""
    install(name, node)
    if descriptor(name).get('probe') == 'toolkit':
        from tools.check_native_toolkit import check as toolkit
        return toolkit(600)
    from tools.check_native_harnesses import check
    return check(name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('names', nargs='*', help='Harness ids from harnesses/; all available for this OS when omitted')
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--toolkit', action='store_true', help='Also check MCP Toolkit: opens a disposable 1C session')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--node', type=Path, default=shutil.which('node'))
    args = parser.parse_args()
    available = [n for n, d in catalogue().items() if target() in d['targets']]
    if args.list:
        for n, d in catalogue().items():
            print(n, '-', d['label'], '' if n in available else f'(нет пакета для {target()})')
    names = args.names or available
    for name in names:
        if args.install:
            install(name, args.node)
        if args.check:
            if descriptor(name).get('probe') == 'toolkit':
                if args.toolkit:
                    from tools.check_native_toolkit import check as toolkit
                    toolkit(600)
            else:
                from tools.check_native_harnesses import check
                check(name)
    if not any((args.install, args.check, args.list)):
        parser.print_help()
