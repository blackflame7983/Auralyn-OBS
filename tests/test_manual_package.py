"""手動ZIP配置の合成fixture。実機・OBS受入ではない。"""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import package_plugin as base
import prepare_release as release
import validate_manual_package as manual


class ManualPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'build')
        self.folder = Path(self.temp.name)
        self.old = self.make_package('old', b'MZ previous synthetic')
        self.current = self.make_package('current', b'MZ current synthetic')

    def tearDown(self):
        self.temp.cleanup()

    def make_package(self, name, dll):
        folder = self.folder / name
        folder.mkdir()
        source = b'synthetic source archive; not distribution evidence'
        manifest = {'localOnly': True, 'signed': False, 'publicationApproved': False,
                    'binarySha256': base.digest(dll), 'sourceArchive': manual.SOURCE_ZIP,
                    'sourceArchiveSha256': base.digest(source),
                    'generatedLocaleFilesSha256': {k: base.digest(v) for k, v in release.LOCALE_FILES.items()}}
        (folder / 'manifest.json').write_bytes(base.json_bytes(manifest))
        (folder / manual.SOURCE_ZIP).write_bytes(source)
        (folder / manual.BINARY_ZIP).write_bytes(release.binary_archive(manifest, dll, {'LICENSE': b'license', 'NOTICE.md': b'notice'}))
        self.sums(folder)
        return folder

    def sums(self, folder):
        (folder / 'SHA256SUMS.txt').write_text(''.join(f'{base.file_digest(folder / name)}  {name}\n' for name in sorted(manual.ARTIFACTS)), encoding='utf-8', newline='\n')

    def rewrite_zip(self, folder, change):
        path = folder / manual.BINARY_ZIP
        with zipfile.ZipFile(path) as archive:
            data = {item.filename: archive.read(item) for item in archive.infolist()}
        change(data)
        path.write_bytes(base.zip_bytes(data))
        self.sums(folder)

    def test_cli_full_cycle_and_preservation(self):
        original = {p: base.file_digest(p) for folder in (self.old, self.current) for p in folder.iterdir()}
        out = self.folder / 'evidence'
        result = subprocess.run([sys.executable, str(ROOT / 'tools/validate_manual_package.py'), '--current', str(self.current),
                                 '--previous', str(self.old), '--output', str(out)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        evidence = json.loads((out / 'evidence.json').read_text(encoding='utf-8'))
        self.assertEqual([step['step'] for step in evidence['steps']], ['install-previous', 'update-current', 'restore-previous', 'update-current-again', 'remove', 'reinstall-current'])
        self.assertIsNone(evidence['steps'][4]['installedSha256'])
        target = out / 'ProgramData/obs-studio/plugins/auralyn-obs'
        self.assertEqual((target / 'bin/64bit/auralyn-obs.dll').read_bytes(), b'MZ current synthetic')
        self.assertFalse((target / 'manifest.json').exists())
        self.assertEqual({p: base.file_digest(p) for p in original}, original)
        self.assertEqual(len(list((out / 'Backups').iterdir())), 3)

    def test_rejects_changed_binary_even_with_updated_sums(self):
        self.rewrite_zip(self.current, lambda data: data.__setitem__(manual.DLL, b'MZ tampered'))
        with self.assertRaises(ValueError): manual.validate(self.current, self.old, self.folder / 'out')
        self.assertFalse((self.folder / 'out').exists())

    def test_rejects_changed_source_reference_and_archive(self):
        self.rewrite_zip(self.current, lambda data: data.__setitem__('SOURCE.txt', b'other source'))
        with self.assertRaises(ValueError): manual.validate(self.current, self.old, self.folder / 'out')
        self.current = self.make_package('other', b'MZ other')
        (self.current / manual.SOURCE_ZIP).write_bytes(b'tampered')
        self.sums(self.current)
        with self.assertRaises(ValueError): manual.validate(self.current, self.old, self.folder / 'out')

    def test_rejects_traversal_duplicate_and_changed_manifest(self):
        self.rewrite_zip(self.current, lambda data: data.__setitem__('../escape', b'bad'))
        with self.assertRaises(ValueError): manual.validate(self.current, self.old, self.folder / 'out')
        self.current = self.make_package('other', b'MZ other')
        with zipfile.ZipFile(self.current / manual.BINARY_ZIP, 'a') as archive:
            archive.writestr(manual.DLL, b'MZ duplicate')
        self.sums(self.current)
        with self.assertRaises(ValueError): manual.validate(self.current, self.old, self.folder / 'out')
        self.current = self.make_package('third', b'MZ other')
        manifest = json.loads((self.current / 'manifest.json').read_text(encoding='utf-8'))
        manifest['extra'] = 'changed'
        (self.current / 'manifest.json').write_bytes(base.json_bytes(manifest))
        self.sums(self.current)
        with self.assertRaises(ValueError): manual.validate(self.current, self.old, self.folder / 'out')

    def test_rejects_symlink_case_alias_and_oversize(self):
        path = self.current / manual.BINARY_ZIP
        with zipfile.ZipFile(path) as archive:
            data = {entry.filename: archive.read(entry) for entry in archive.infolist()}
        def write_with(info, payload):
            with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for name, content in data.items():
                    if name != info.filename:
                        archive.writestr(name, content)
                archive.writestr(info, payload)
            self.sums(self.current)
        info = zipfile.ZipInfo(manual.DLL)
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        write_with(info, data[manual.DLL])
        with self.assertRaises(ValueError): manual.validate(self.current, self.old, self.folder / 'out')
        info = zipfile.ZipInfo(manual.DLL.upper())
        write_with(info, data[manual.DLL])
        with self.assertRaises(ValueError): manual.validate(self.current, self.old, self.folder / 'out')
        info = zipfile.ZipInfo(manual.DLL)
        write_with(info, b'x' * (manual.LIMIT + 1))
        with self.assertRaises(ValueError): manual.validate(self.current, self.old, self.folder / 'out')

    def test_refuses_existing_outside_and_linked_output(self):
        with self.assertRaises(ValueError): manual.validate(self.current, self.old, self.folder)
        with self.assertRaises((ValueError, FileNotFoundError)): manual.validate(self.current, self.old, Path(tempfile.gettempdir()) / 'outside-manual-output')
        alias = self.folder / 'alias'
        try:
            alias.symlink_to(self.folder, target_is_directory=True)
        except OSError:
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(self.folder)], capture_output=True)
            if result.returncode: self.skipTest('junction unavailable')
        try:
            with self.assertRaises(ValueError): manual.validate(self.current, self.old, alias / 'out')
        finally:
            if alias.is_symlink(): alias.unlink()
            else: alias.rmdir()


if __name__ == '__main__': unittest.main()
