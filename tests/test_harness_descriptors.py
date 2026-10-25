import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from exam_bench import portable_harnesses as native
from exam_bench.common import digest
from tools import check_native_harnesses as checks
from tools import prepare_native_harnesses as prepare

ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def one_harness(root):
    """A stand whose only harness, test-1, copies one downloaded file into its package."""
    (root / 'harnesses').mkdir(); (root / 'profiles').mkdir()
    asset = root / 'tool.exe'; asset.write_bytes(b'fixture tool')
    configuration = {'version': '1', 'label': 'Test', 'command': '{package}/tool.exe', 'registration': 'test'}
    (root / 'harnesses/test-1.json').write_text(json.dumps({'schema_version': 1, 'id': 'test-1', 'label': 'Test', 'probe': None,
        'targets': {'test-x64': {'downloads': {'tool.exe': {'url': 'https://example.org/tool.exe', 'sha256': digest(asset)}},
                                 'install': [{'copy': 'tool.exe', 'to': 'tool.exe'}], 'configuration': configuration}}}))
    with patch.object(prepare, 'ROOT', root), patch.object(prepare, 'CATALOGUE', root / 'harnesses'), \
         patch.object(checks, 'ROOT', root), patch.object(native, 'ROOT', root), \
         patch.object(prepare, 'target', return_value='test-x64'), patch.object(prepare, 'download', return_value=asset) as download:
        yield configuration, download


class Descriptors(unittest.TestCase):
    def test_generic_mcp_probe_is_supported(self):
        probe = {'tool': 'metadata_summary', 'arguments': {'kind': 'configuration'}, 'expect': 'Configuration.xml'}
        doc = {'schema_version': 1, 'id': 'x', 'probe': probe, 'targets': {}}
        self.assertEqual(prepare.validate(doc, 'x')['probe'], probe)

    def test_builtin_and_absent_probes_are_supported(self):
        for probe in (*prepare.BUILTIN_PROBES, None):
            with self.subTest(probe=probe):
                doc = {'schema_version': 1, 'id': 'x', 'probe': probe, 'targets': {}}
                prepare.validate(doc, 'x')
        prepare.validate({'schema_version': 1, 'id': 'x', 'targets': {}}, 'x')

    def test_invalid_probes_are_refused(self):
        for probe in ([], {}, {'tool': 1}, 'unknown', 3):
            with self.subTest(probe=probe), self.assertRaisesRegex(ValueError, 'unknown probe'):
                prepare.validate({'schema_version': 1, 'id': 'x', 'probe': probe, 'targets': {}}, 'x')

    def test_shipped_descriptors_are_valid_and_reference_their_downloads(self):
        docs = prepare.catalogue()
        self.assertIn('unica-0.14.0-rc.1', docs)
        for name, doc in docs.items():
            for target in doc['targets'].values():
                for step in target['install']:
                    kind = next(iter(set(step) & prepare.STEPS))
                    if kind in ('copy', 'unzip', 'unzip-source', 'verified-entrypoint'):
                        self.assertIn(step[kind], target['downloads'], name)
                    if kind == 'pip':
                        self.assertTrue((ROOT / step[kind]).is_file(), name)

    def test_unsafe_descriptors_are_refused(self):
        base = {'schema_version': 1, 'id': 'x', 'targets': {'windows-x64': {
            'downloads': {'a.zip': {'url': 'https://example.org/a.zip', 'sha256': '0' * 64}},
            'install': [{'unzip': 'a.zip'}], 'configuration': {'command': '{package}/x.exe'}}}}
        prepare.validate(base, 'x')
        with self.assertRaisesRegex(ValueError, 'match'):
            prepare.validate(base, 'y')
        script = json.loads(json.dumps(base)); script['targets']['windows-x64']['install'] = [{'run': 'setup.cmd'}]
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            prepare.validate(script, 'x')
        plain = json.loads(json.dumps(base)); plain['targets']['windows-x64']['downloads']['a.zip']['url'] = 'http://example.org/a.zip'
        with self.assertRaisesRegex(ValueError, 'https'):
            prepare.validate(plain, 'x')

    def test_one_harness_installs_from_its_descriptor_and_is_sealed(self):
        with tempfile.TemporaryDirectory() as td, one_harness(Path(td)) as (configuration, _):
            root = Path(td)
            self.assertTrue(prepare.install('test-1'))
            self.assertEqual((root / '.local/harnesses/test-1/package/tool.exe').read_bytes(), b'fixture tool')
            self.assertEqual(native.registry()['test-1'], configuration)
            self.assertEqual(json.loads((root / '.local/harnesses/test-1/profile.json').read_text())['target'], 'test-x64')
            self.assertFalse((root / 'profiles/test-1-portable.json').exists())  # Git is not touched by an install
            native.verify('test-1', require_probe=False)
            self.assertFalse(prepare.install('test-1'))  # sealed installs are never overwritten

    def test_interrupted_attempt_starts_again_but_an_earlier_installation_is_kept(self):
        with tempfile.TemporaryDirectory() as td, one_harness(Path(td)) as (_, download):
            base = Path(td) / '.local/harnesses/test-1'
            download.side_effect = TimeoutError('timed out')
            with self.assertRaises(TimeoutError):
                prepare.install('test-1')
            self.assertFalse(base.exists())  # a failed download leaves no folder
            download.side_effect = None
            with patch.object(prepare, 'run_step', side_effect=OSError('step failed')), self.assertRaises(OSError):
                prepare.install('test-1')
            self.assertEqual([p.name for p in base.iterdir()], ['package'])
            (base / 'source').mkdir(); (base / 'package/partial.bin').write_bytes(b'x')
            self.assertTrue(prepare.install('test-1'))
            self.assertEqual([p.name for p in (base / 'package').iterdir()], ['tool.exe'])
        with tempfile.TemporaryDirectory() as td, one_harness(Path(td)):
            base = Path(td) / '.local/harnesses/test-1'
            (base / 'package').mkdir(parents=True); (base / 'notes.txt').write_text('earlier format')
            with self.assertRaisesRegex(ValueError, 'прежнего формата'):
                prepare.install('test-1')
            self.assertTrue((base / 'notes.txt').is_file())


class PosixInstallSteps(unittest.TestCase):
    @unittest.skipIf(prepare.os.name == 'nt', 'POSIX permissions')
    def test_zip_keeps_execute_bits(self):
        import zipfile
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / 'a.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                info = zipfile.ZipInfo('bin/tool'); info.external_attr = 0o755 << 16
                z.writestr(info, b'#!/bin/sh\n'); z.writestr('README', b'x')
            prepare.unzip(archive, Path(td) / 'out')
            self.assertTrue((Path(td) / 'out/bin/tool').stat().st_mode & 0o100)
            self.assertFalse((Path(td) / 'out/README').stat().st_mode & 0o100)

    def test_tar_cannot_escape_the_package(self):
        import io, tarfile
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / 'a.tar.gz'
            with tarfile.open(archive, 'w:gz') as t:
                info = tarfile.TarInfo('../escape'); info.size = 1
                t.addfile(info, io.BytesIO(b'x'))
            with self.assertRaises(Exception):
                prepare.untar(archive, Path(td) / 'out')
            self.assertFalse((Path(td) / 'escape').exists())

    def test_npm_packages_for_other_systems_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / 'package-lock.json').write_text(json.dumps({'packages': {
                '': {}, 'node_modules/win': {'os': ['win32'], 'resolved': 'x', 'integrity': 'sha512-x'},
                'node_modules/arm': {'os': ['darwin'], 'cpu': ['arm64'], 'resolved': 'y', 'integrity': 'sha512-y'}}}))
            with patch.object(prepare.urllib.request, 'urlopen', side_effect=AssertionError('downloaded')):
                prepare.npm_locked(project, 'linux-x64')  # nothing applies to Linux x64
            with patch.object(prepare.urllib.request, 'urlopen', side_effect=RuntimeError('fetch')) as fetch:
                with self.assertRaises(RuntimeError):
                    prepare.npm_locked(project, 'macos-arm64')
                self.assertIn('y', str(fetch.call_args))


if __name__ == '__main__':
    unittest.main()
