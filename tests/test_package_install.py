"""隔離portableの導入・復帰・改変保護と、ローカル配布候補を確認する。"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import manage_install as install
import package_plugin as package


class PackageInstallTests(unittest.TestCase):
    def setUp(self):
        build = (ROOT / 'build').resolve(strict=True)
        self.folder = Path(tempfile.mkdtemp(prefix='package-test-', dir=build)).resolve(strict=True)
        self.assertTrue(self.folder.is_relative_to(build) and self.folder != build)
        self.root = self.folder / 'portable'
        (self.root / 'bin/64bit').mkdir(parents=True)
        (self.root / 'obs-plugins/64bit').mkdir(parents=True)
        (self.root / 'portable_mode.txt').write_text('')
        (self.root / 'bin/64bit/obs64.exe').write_bytes(b'test-only')
        (self.root / install.MARKER).write_text(install.MARKER_CONTENT, encoding='utf-8', newline='\n')
        self.other = self.root / 'obs-plugins/64bit/unrelated.dll'
        self.other.write_bytes(b'other plugin data')
        self.a = self.make_zip('a.zip', b'MZ-test-version-a', '0.1.0-poc')
        self.b = self.make_zip('b.zip', b'MZ-test-version-b', '0.2.0-poc')
        self.target = self.root / 'obs-plugins/64bit/auralyn-obs.dll'

    def tearDown(self):
        # このテストが作成した固有ディレクトリだけを削除する。
        build = (ROOT / 'build').resolve(strict=True)
        resolved = self.folder.resolve(strict=True)
        if resolved != build and resolved.is_relative_to(build):
            shutil.rmtree(resolved)

    def make_zip(self, name, data, version, *, extra=None, symlink=False):
        path = self.folder / name
        manifest = {'schemaVersion': 1, 'package': 'auralyn-obs', 'platform': 'windows-x64', 'obsVersion': '32.2.2',
                    'protocolMajor': 1, 'protocolMinor': 0, 'sourceKind': 'auralyn_pcm_poc_v1', 'version': version,
                    'binarySha256': package.digest(data), 'sourceArchiveSha256': 'a' * 64}
        with zipfile.ZipFile(path, 'w') as archive:
            info = zipfile.ZipInfo('auralyn-obs.dll')
            if symlink:
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, data)
            archive.writestr('manifest.json', json.dumps(manifest))
            for member in ['LICENSE', 'NOTICE.md', 'SOURCE.txt']:
                archive.writestr(member, 'test')
            if extra:
                archive.writestr(extra, 'not allowed')
        return path

    def contents(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_preview_install_idempotent_update_restore_remove_and_preservation(self):
        root = install.isolated_root(self.root)
        before = self.contents()
        _, _, preview = install.plan(root, 'install', self.a)
        self.assertIsNone(preview['before'])
        self.assertEqual(before, self.contents(), 'プレビューは書き込まない')
        install.apply(root, 'install', self.a)
        self.assertEqual(self.target.read_bytes(), b'MZ-test-version-a')
        first = install.current(root)[0]
        after_first = self.contents()
        result = install.apply(root, 'install', self.a)
        self.assertTrue(result['unchanged'])
        self.assertEqual(after_first, self.contents())
        install.apply(root, 'install', self.b)
        self.assertEqual(self.target.read_bytes(), b'MZ-test-version-b')
        self.assertEqual((root / install.BACKUPS / (first['binarySha256'] + '.dll')).read_bytes(), b'MZ-test-version-a')
        install.apply(root, 'restore', backup_sha=first['binarySha256'])
        self.assertEqual(self.target.read_bytes(), b'MZ-test-version-a')
        self.assertEqual(install.current(root)[0], first)
        install.apply(root, 'remove')
        self.assertFalse(self.target.exists())
        self.assertFalse((root / install.MANIFEST).exists())
        self.assertTrue((root / install.BACKUPS / (first['binarySha256'] + '.dll')).exists())
        self.assertEqual(self.other.read_bytes(), b'other plugin data')

    def test_foreign_or_changed_dll_and_missing_metadata_are_preserved(self):
        root = install.isolated_root(self.root)
        self.target.write_bytes(b'user-owned')
        with self.assertRaises(ValueError):
            install.apply(root, 'install', self.a)
        self.assertEqual(self.target.read_bytes(), b'user-owned')
        self.target.unlink()
        install.apply(root, 'install', self.a)
        self.target.write_bytes(b'user-modified')
        for action in ['install', 'remove']:
            with self.assertRaises(ValueError):
                install.apply(root, action, self.b)
        self.assertEqual(self.target.read_bytes(), b'user-modified')
        self.assertEqual(self.other.read_bytes(), b'other plugin data')

    def test_malicious_zip_and_root_escape_are_rejected(self):
        for index, name in enumerate(['../other.dll', 'a/../../other.dll', 'C:/outside.dll', '/outside.dll', 'a\\..\\bad.dll']):
            path = self.make_zip(f'bad-{index}.zip', b'MZ-test', '0.1.0-poc', extra=name)
            with self.assertRaises(ValueError):
                install.load_package(path)
        with self.assertRaises(ValueError):
            install.load_package(self.make_zip('link.zip', b'MZ-test', '0.1.0-poc', symlink=True))
        with self.assertRaises(ValueError):
            install.valid_manifest({'schemaVersion': True})
        with self.assertRaises(ValueError):
            install.plain_path(self.root.resolve(), '../outside')
        with self.assertRaises(ValueError):
            install.isolated_root(Path(tempfile.gettempdir()))
        (self.root / install.MARKER).write_text('wrong', encoding='utf-8')
        with self.assertRaises(ValueError):
            install.isolated_root(self.root)

    def make_directory_link(self, link, destination):
        try:
            link.symlink_to(destination, target_is_directory=True)
        except OSError:
            # Windowsでは開発者モードがなくてもjunctionを作れる場合がある。
            if os.name != 'nt':
                self.skipTest('この環境ではテスト用リンクを作成できません')
            allowed = {'SYSTEMROOT', 'WINDIR', 'PATH', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP'}
            env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
            script = '& { param([string]$LinkPath, [string]$TargetPath) New-Item -ItemType Junction -Path $LinkPath -Target $TargetPath | Out-Null }'
            created = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script, str(link), str(destination)], env=env, capture_output=True)
            if created.returncode != 0:
                self.skipTest('この環境ではテスト用リンクを作成できません')

    def test_backup_symlink_outside_portable_root_is_rejected(self):
        outside = self.folder / 'outside'
        outside.mkdir()
        link = self.root / install.BACKUPS
        self.make_directory_link(link, outside)
        root = install.isolated_root(self.root)
        install.apply(root, 'install', self.a)
        with self.assertRaises(ValueError):
            install.apply(root, 'install', self.b)
        self.assertEqual(self.target.read_bytes(), b'MZ-test-version-a')
        self.assertEqual(list(outside.iterdir()), [])

    def test_root_junction_cannot_redirect_to_another_portable(self):
        alias = self.folder / 'alias'
        self.make_directory_link(alias, self.root)
        with self.assertRaises(ValueError):
            install.isolated_root(alias)

    def test_failure_rolls_back_and_crash_recovery_refuses_foreign_changes(self):
        root = install.isolated_root(self.root)
        with mock.patch.object(install.os, 'fsync', side_effect=OSError('injected fsync failure')):
            with self.assertRaises(OSError):
                install.apply(root, 'install', self.a)
        self.assertTrue((root / install.LOCK).is_file())
        self.assertFalse(self.target.exists())
        with self.assertRaises(FileExistsError):
            install.exclusive_write(self.other, b'not allowed')
        self.assertEqual(self.other.read_bytes(), b'other plugin data')

        install.apply(root, 'install', self.a)
        before_manifest, before_data = install.current(root)
        actual = install.atomic_write
        failed = False

        def fail_once(path, data):
            nonlocal failed
            if path.name == install.MANIFEST and not failed:
                failed = True
                raise OSError('injected failure')
            return actual(path, data)

        with mock.patch.object(install, 'atomic_write', side_effect=fail_once):
            with self.assertRaises(OSError):
                install.apply(root, 'install', self.b)
        self.assertEqual(install.current(root), (before_manifest, before_data))
        self.assertFalse((root / install.JOURNAL).exists())
        install.backup(root, before_manifest, before_data)
        changed_manifest, changed_data = install.load_package(self.b)
        journal = {'schemaVersion': 1, 'beforeManifest': before_manifest, 'beforeSha256': before_manifest['binarySha256'],
                   'afterSha256': changed_manifest['binarySha256']}
        install.exclusive_write(root / install.JOURNAL, package.json_bytes(journal))
        self.target.write_bytes(b'user-changed-after-crash')
        with self.assertRaises(ValueError):
            install.apply(root, 'recover')
        self.assertTrue((root / install.JOURNAL).exists())
        self.assertEqual(self.target.read_bytes(), b'user-changed-after-crash')
        self.target.write_bytes(changed_data)
        install.apply(root, 'recover')
        self.assertEqual(install.current(root), (before_manifest, before_data))
        self.assertFalse((root / install.JOURNAL).exists())
        self.assertEqual(self.other.read_bytes(), b'other plugin data')

    def test_external_change_between_validation_and_update_is_not_overwritten(self):
        root = install.isolated_root(self.root)
        install.apply(root, 'install', self.a)
        create = install.exclusive_write

        def change_after_journal(path, data):
            create(path, data)
            if path.name == install.JOURNAL:
                self.target.write_bytes(b'user-change-during-update')

        with mock.patch.object(install, 'exclusive_write', side_effect=change_after_journal):
            with self.assertRaises(ValueError):
                install.apply(root, 'install', self.b)
        self.assertEqual(self.target.read_bytes(), b'user-change-during-update')
        self.assertEqual(self.other.read_bytes(), b'other plugin data')
        self.assertFalse((root / install.JOURNAL).exists())

    def test_package_uses_recorded_native_dll_and_only_allowlisted_source(self):
        evidence = ROOT / 'build/native-20261102-163539/runs/20261102-165229/code-manifest.json'
        evidence_copy = self.folder / 'evidence'
        evidence_copy.mkdir()
        for name in ['code-manifest.json', 'result.json', 'analysis.json']:
            shutil.copy2(evidence.parent / name, evidence_copy / name)
        evidence = evidence_copy / 'code-manifest.json'
        analysis_file = evidence_copy / 'analysis.json'
        analysis = json.loads(analysis_file.read_text(encoding='utf-8'))
        # 原本は変更せず、記録された各波形から追加チェックを再判定する。
        analysis['checks']['recordedAudioMatchesReference'] = (
            len([m for m in analysis['waveformMatches'] if m['rms'] > 0 and m['normalizedCorrelation'] >= .95]) >= 3)
        analysis_file.write_text(json.dumps(analysis), encoding='utf-8')
        output = self.folder / 'package'
        result = package.package(output, evidence, '0.1.0-test')
        manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
        source = output / result['sourceArchive']
        self.assertEqual(package.digest(source.read_bytes()), manifest['sourceArchiveSha256'])
        with zipfile.ZipFile(source) as archive:
            self.assertEqual(set(archive.namelist()), set(package.SOURCE_FILES))
            self.assertFalse(any('build/' in name or '__pycache__' in name for name in archive.namelist()))
            self.assertEqual(package.digest(archive.read('src/plugin.cpp')), manifest['sourceFilesSha256']['src/plugin.cpp'])
        binary = output / result['binaryArchive']
        actual_manifest, dll = install.load_package(binary)
        self.assertEqual(package.digest(dll), actual_manifest['binarySha256'])
        self.assertGreaterEqual(manifest['nativeEvidence']['elapsedSeconds'], 3600)
        files = package.read_sources()
        lock = json.loads(files['protocol.lock.json'])
        files['protocol/obs-pcm-v1.json'] = b'{}'
        with self.assertRaises(ValueError):
            package.check_protocol(files, lock)
        lock['major'] = True
        with self.assertRaises(ValueError):
            package.check_protocol(package.read_sources(), lock)
        run = json.loads((evidence_copy / 'result.json').read_text(encoding='utf-8'))
        run['elapsedSeconds'] = 12
        (evidence_copy / 'result.json').write_text(json.dumps(run), encoding='utf-8')
        with self.assertRaises(ValueError):
            package.package(self.folder / 'short-test-package', evidence_copy / 'code-manifest.json', '0.1.0-test')
        run['elapsedSeconds'] = 3600
        (evidence_copy / 'result.json').write_text(json.dumps(run), encoding='utf-8')
        with self.assertRaises(FileExistsError):
            package.package(output, evidence, '0.1.0-test')

    def test_evidence_rejects_failed_missing_silent_or_incomplete_audio(self):
        import copy
        source = ROOT / 'build/native-20261102-163539/runs/20261102-165229'
        for name in ['code-manifest.json', 'result.json', 'analysis.json']:
            shutil.copy2(source / name, self.folder / name)
        path = self.folder / 'analysis.json'
        valid = json.loads(path.read_text(encoding='utf-8'))
        valid['checks']['recordedAudioMatchesReference'] = True
        files = package.read_sources()
        dll = (ROOT / 'build/cmake/Release/auralyn-obs.dll').read_bytes()
        def check(value):
            path.write_text(json.dumps(value), encoding='utf-8')
            package.check_evidence(files, dll, self.folder / 'code-manifest.json')
        check(valid)
        mutations = [
            lambda x: x['checks'].__setitem__('bridgeErrorsZero', False),
            lambda x: x['checks'].pop('recordedAudioMatchesReference'),
            lambda x: x['checks'].__setitem__('bridgeErrorsZero', 1),
            lambda x: [m.__setitem__('rms', 0) for m in x['waveformMatches']],
            lambda x: [m.__setitem__('normalizedCorrelation', .9) for m in x['waveformMatches']],
            lambda x: x['audioPackets'].__setitem__('count', 0),
            lambda x: x['audioPackets'].__setitem__('frames', 0),
            lambda x: x['audioPackets'].__setitem__('seconds', 10),
            lambda x: x.__setitem__('elapsedSeconds', float('nan')),
        ]
        for mutate in mutations:
            candidate = copy.deepcopy(valid)
            mutate(candidate)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                check(candidate)

    def test_real_process_lock_and_recovery_after_forced_termination(self):
        root = install.isolated_root(self.root)
        install.apply(root, 'install', self.a)
        before = install.current(root)
        code = '''import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import manage_install as m
root = m.isolated_root(Path(sys.argv[2]))
original = m.atomic_write
def crash(path, data):
    original(path, data)
    if path.name == m.BINARY_NAME:
        os._exit(17)
m.atomic_write = crash
m.apply(root, 'install', Path(sys.argv[3]))
'''
        args = [sys.executable, '-c', code, str(ROOT / 'tools'), str(root), str(self.b)]
        crashed = subprocess.run(args, capture_output=True, timeout=30)
        self.assertEqual(crashed.returncode, 17, crashed.stderr.decode(errors='replace'))
        self.assertTrue((root / install.JOURNAL).is_file())
        self.assertTrue((root / install.LOCK).is_file())
        self.assertEqual(self.target.read_bytes(), b'MZ-test-version-b')
        recovered = subprocess.run([sys.executable, str(ROOT / 'tools/manage_install.py'),
                                    'recover', '--root', str(root), '--apply'], capture_output=True, timeout=30)
        self.assertEqual(recovered.returncode, 0, recovered.stderr.decode(errors='replace'))
        self.assertEqual(install.current(root), before)
        self.assertFalse((root / install.JOURNAL).exists())
        holder_code = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import manage_install as m
with m.operation_lock(m.isolated_root(Path(sys.argv[2]))):
    print('locked', flush=True)
    sys.stdin.readline()
'''
        holder = subprocess.Popen([sys.executable, '-u', '-c', holder_code, str(ROOT / 'tools'), str(root)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'locked')
            with self.assertRaisesRegex(ValueError, '実行中'):
                install.apply(root, 'remove')
            self.assertEqual(install.current(root), before)
        finally:
            holder.communicate(input='done\n', timeout=30)
        self.assertEqual(holder.returncode, 0)
        self.assertTrue((root / install.LOCK).is_file())


if __name__ == '__main__':
    unittest.main()
