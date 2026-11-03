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
            def readonly_object(function, path, error):
                # WindowsのGit objectだけがread-onlyで残る場合も、所有fixture内だけを扱う。
                target = Path(path).resolve(strict=True)
                if (not isinstance(error, PermissionError) or function not in (os.unlink, os.remove)
                        or not target.is_file() or not target.is_relative_to(resolved)):
                    raise error
                target.chmod(stat.S_IREAD | stat.S_IWRITE)
                function(path)
            shutil.rmtree(resolved, onexc=readonly_object)

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

    def synthetic_evidence(self):
        # validatorの単体fixture。実機1時間の証拠として保存・公開しない。
        folder = self.folder / 'synthetic-contract-fixture'
        folder.mkdir()
        counters = {key: 0 for key in ['underruns', 'overruns', 'invalid', 'stale', 'resets', 'connections', 'missedLocks', 'clockRebases']}
        counters['interpolation'] = 'sinc48-poc'
        files = {str(ROOT / name): package.digest((ROOT / name).read_bytes()) for name in package.COMPILED_SOURCE}
        files[str(folder / 'obs-plugins/64bit/auralyn-obs.dll')] = package.digest((ROOT / 'build/cmake/Release/auralyn-obs.dll').read_bytes())
        files[str(folder / 'audio_engine.exe')] = 'b' * 64
        manifest = {'scope': 'synthetic validator fixture, not physical acceptance', 'files': files}
        run = {'scope': manifest['scope'], 'durationRequestedSeconds': 3600, 'elapsedSeconds': 3600,
               'normalObsPreserved': True, 'physicalMonitorMuted': True,
               'reference': {'dropped': 0, 'error': None}, 'baselineBridge': counters, 'finalBridge': counters.copy()}
        names = ['requestedDurationReached', 'recordingContinuousAtSamples', 'recordingDurationWithinOneSecond',
                 'audioPacketTimestampsContinuous', 'audioDurationWithinOneSecond', 'bridgeErrorsZero',
                 'referenceCaptureComplete', 'obsAddedBufferZero', 'normalObsPreserved', 'physicalMonitorSilent',
                 'codeManifestUnchanged', 'recordedAudioMatchesReference']
        analysis = {'checks': dict.fromkeys(names, True), 'requestedSeconds': 3600, 'elapsedSeconds': 3600,
                    'audioPackets': {'count': 168750, 'frames': 172800000, 'seconds': 3600, 'timestampGaps': 0},
                    'maxObsAddedBufferMs': 0, 'waveformMatches': [{'seconds': t, 'rms': .1, 'normalizedCorrelation': .999} for t in [10, 1800, 3590]]}
        for name, value in [('code-manifest.json', manifest), ('result.json', run), ('analysis.json', analysis)]:
            (folder / name).write_text(json.dumps(value), encoding='utf-8')
        return folder

    def test_source_revision_distinguishes_clean_modified_and_untracked_files(self):
        repo = self.folder / 'source'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + str(repo / 'no-hooks'),
                                   '-c', 'user.name=Local Test', '-c', 'user.email=local-test@example.invalid',
                                   '-C', str(repo), *args], capture_output=True, check=True).stdout.decode().strip()
        git('init', '-q')
        (repo / 'demo.py').write_bytes(b'one\n')
        git('add', 'demo.py')
        git('commit', '-qm', 'test fixture')
        commit = git('rev-parse', 'HEAD')
        with mock.patch.object(package, 'ROOT', repo):
            self.assertEqual(package.source_revision({'demo.py': b'one\n'}),
                             {'commit': commit, 'state': 'clean-checkout-snapshot'})
            self.assertEqual(package.source_revision({'demo.py': b'another snapshot\n'})['state'],
                             'commit-plus-working-tree-snapshot')
            (repo / 'demo.py').write_bytes(b'changed source\n')
            self.assertEqual(package.source_revision({'demo.py': b'changed source\n'})['state'],
                             'commit-plus-working-tree-snapshot')
            git('add', 'demo.py')
            git('commit', '-qm', 'changed fixture')
            (repo / 'extra.py').write_bytes(b'untracked\n')
            self.assertEqual(package.source_revision({'demo.py': b'changed source\n'})['state'],
                             'commit-plus-working-tree-snapshot')

    def test_export_does_not_inherit_parent_repository_commit(self):
        export = self.folder / 'export'
        export.mkdir()
        with mock.patch.object(package, 'ROOT', export):
            self.assertEqual(package.source_revision({}),
                             {'commit': None, 'state': 'unversioned-source-snapshot'})

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

    def programdata_root(self):
        root = self.folder / 'ProgramData/obs-studio/plugins/auralyn-obs'
        (root / 'bin/64bit').mkdir(parents=True)
        (root / install.PROGRAMDATA_MARKER).write_text(install.PROGRAMDATA_MARKER_CONTENT, encoding='utf-8', newline='\n')
        return root

    def test_programdata_sandbox_lifecycle_conflicts_and_permissions(self):
        layout = 'programdata-sandbox'
        root = install.isolated_root(self.programdata_root(), layout)
        target = root / 'bin/64bit/auralyn-obs.dll'
        other = root / 'bin/64bit/unrelated.dll'
        other.write_bytes(b'other')
        _, _, preview = install.plan(root, 'install', self.a, layout=layout)
        self.assertIsNone(preview['before'])
        self.assertFalse(target.exists())
        install.apply(root, 'install', self.a, layout=layout)
        first = install.current(root, layout)[0]
        self.assertTrue(install.apply(root, 'install', self.a, layout=layout)['unchanged'])
        install.apply(root, 'install', self.b, layout=layout)
        install.apply(root, 'restore', backup_sha=first['binarySha256'], layout=layout)
        self.assertEqual(install.current(root, layout)[0], first)
        target.write_bytes(b'foreign')
        for action in ('install', 'remove', 'restore'):
            with self.assertRaises(ValueError):
                install.apply(root, action, self.b, first['binarySha256'], layout)
        self.assertEqual(target.read_bytes(), b'foreign')
        target.write_bytes(b'MZ-test-version-a')
        original = install.atomic_write
        def locked(path, data):
            if path == target:
                raise PermissionError('simulated locked DLL')
            return original(path, data)
        with mock.patch.object(install, 'atomic_write', side_effect=locked):
            with self.assertRaises(PermissionError):
                install.apply(root, 'install', self.b, layout=layout)
        self.assertEqual(install.current(root, layout)[0], first)
        self.assertFalse((root / install.JOURNAL).exists())
        with mock.patch.object(install, 'exclusive_write', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                install.apply(root, 'remove', layout=layout)
        self.assertEqual(install.current(root, layout)[0], first)
        install.apply(root, 'remove', layout=layout)
        self.assertFalse(target.exists())
        self.assertEqual(other.read_bytes(), b'other')

    def test_programdata_sandbox_recovery_and_root_guards(self):
        layout = 'programdata-sandbox'
        root = install.isolated_root(self.programdata_root(), layout)
        target = root / 'bin/64bit/auralyn-obs.dll'
        install.apply(root, 'install', self.a, layout=layout)
        before, data = install.current(root, layout)
        install.backup(root, before, data)
        after, after_data = install.load_package(self.b)
        journal = {'schemaVersion': 1, 'beforeManifest': before, 'beforeSha256': before['binarySha256'],
                   'afterSha256': after['binarySha256']}
        install.exclusive_write(root / install.JOURNAL, package.json_bytes(journal))
        target.write_bytes(b'foreign after interruption')
        with self.assertRaises(ValueError):
            install.apply(root, 'recover', layout=layout)
        self.assertTrue((root / install.JOURNAL).exists())
        target.write_bytes(after_data)
        result = subprocess.run([sys.executable, str(ROOT / 'tools/manage_install.py'), 'recover',
                                 '--root', str(root), '--layout', layout, '--apply'], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(install.current(root, layout), (before, data))
        self.assertFalse((root / install.JOURNAL).exists())
        with self.assertRaises(ValueError):
            install.isolated_root(self.root, layout)
        wrong = self.folder / 'wrong-root'
        (wrong / 'bin/64bit').mkdir(parents=True)
        (wrong / install.PROGRAMDATA_MARKER).write_text(install.PROGRAMDATA_MARKER_CONTENT, encoding='utf-8', newline='\n')
        with self.assertRaises(ValueError):
            install.isolated_root(wrong, layout)
        with self.assertRaises(ValueError):
            install.isolated_root(Path(os.environ.get('PROGRAMDATA', 'C:/ProgramData')) / 'obs-studio/plugins/auralyn-obs', layout)
        marker = root / install.PROGRAMDATA_MARKER
        marker.write_text('wrong', encoding='utf-8')
        with self.assertRaises(ValueError):
            install.isolated_root(root, layout)
        marker.write_text(install.PROGRAMDATA_MARKER_CONTENT, encoding='utf-8', newline='\n')
        (root / install.MARKER).write_text(install.MARKER_CONTENT, encoding='utf-8')
        with self.assertRaises(ValueError):
            install.isolated_root(root, layout)
        (root / install.MARKER).unlink()
        (self.root / install.PROGRAMDATA_MARKER).write_text(install.PROGRAMDATA_MARKER_CONTENT, encoding='utf-8')
        with self.assertRaises(ValueError):
            install.isolated_root(self.root)
        alias_parent = self.folder / 'alias'
        self.make_directory_link(alias_parent, self.folder / 'ProgramData')
        with self.assertRaises(ValueError):
            install.isolated_root(alias_parent / 'obs-studio/plugins/auralyn-obs', layout)

    def test_package_uses_matching_fixture_hashes_and_only_allowlisted_source(self):
        evidence = self.synthetic_evidence() / 'code-manifest.json'
        evidence_copy = self.folder / 'evidence'
        evidence_copy.mkdir()
        for name in ['code-manifest.json', 'result.json', 'analysis.json']:
            shutil.copy2(evidence.parent / name, evidence_copy / name)
        evidence = evidence_copy / 'code-manifest.json'
        analysis_file = evidence_copy / 'analysis.json'
        analysis = json.loads(analysis_file.read_text(encoding='utf-8'))
        # 単体fixtureの波形判定を再計算する。実音声の測定ではない。
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

    def test_96k_evidence_requires_measured_rates_and_probe_cleanup(self):
        folder = self.synthetic_evidence()
        result_path, analysis_path = folder / 'result.json', folder / 'analysis.json'
        run = json.loads(result_path.read_text())
        analysis = json.loads(analysis_path.read_text())
        for key in ('baselineBridge', 'finalBridge'):
            run[key].update(interpolation='sinc96-to48-poc', producerRate=96000)
        run['reference']['rate'] = 96000
        run.update(syntheticAsioProbe=True, voicemeeterRestored=True, cleanupErrors=[],
                   probeUnderflowDelta=0, probe={'error': None})
        analysis.update(referenceRateHz=96000, recordingRateHz=48000)
        analysis['checks'].update(probePlaybackContinuous=True, voicemeeterSettingsRestored=True)
        files = package.read_sources()
        dll = (ROOT / 'build/cmake/Release/auralyn-obs.dll').read_bytes()
        def check():
            result_path.write_text(json.dumps(run))
            analysis_path.write_text(json.dumps(analysis))
            return package.check_evidence(files, dll, folder / 'code-manifest.json')
        for rate, mode in package.SINC_MODES.items():
            if rate == 48000:
                continue
            for key in ('baselineBridge', 'finalBridge'):
                run[key].update(interpolation=mode, producerRate=rate)
            run['reference']['rate'] = rate
            run['reference']['streamMinor'] = 1 if rate in package.EXTENDED_RATES else 0
            analysis['referenceRateHz'] = rate
            self.assertEqual(check()['receiverInterpolation'], mode)
            if rate in package.EXTENDED_RATES:
                run['reference']['streamMinor'] = 0
                with self.assertRaisesRegex(ValueError, '通信1.1'):
                    check()
                run['reference']['streamMinor'] = 1
        analysis['recordingRateHz'] = 96000
        with self.assertRaisesRegex(ValueError, '実測値'):
            check()
        analysis['recordingRateHz'] = 48000
        run['voicemeeterRestored'] = False
        with self.assertRaisesRegex(ValueError, '復元'):
            check()
        run['voicemeeterRestored'] = True
        run['probeUnderflowDelta'] = 1
        with self.assertRaisesRegex(ValueError, '連続再生'):
            check()

    def test_post_analysis_requires_method_sources_inputs_and_all_segments(self):
        import copy
        folder = self.synthetic_evidence()
        result_path, analysis_path = folder / 'result.json', folder / 'analysis.json'
        run = json.loads(result_path.read_text())
        recording = folder / 'synthetic-recording.bin'
        recording.write_bytes(b'synthetic validator data, not audio acceptance')
        (folder / 'reference.f32').write_bytes(b'synthetic reference')
        run['recording'] = {'outputPath': str(recording)}
        result_path.write_text(json.dumps(run))
        manifest_path = folder / 'code-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files'][str(ROOT / 'tools/analyze_native.py')] = package.digest((ROOT / 'tools/analyze_native.py').read_bytes())
        manifest_path.write_text(json.dumps(manifest))
        analysis = json.loads(analysis_path.read_text())
        first = copy.deepcopy(analysis)
        first['checks']['recordedAudioMatchesReference'] = False
        for match in first['waveformMatches']:
            match['normalizedCorrelation'] = .897
        (folder / 'analysis-fraction-only.json').write_text(json.dumps(first))
        files = package.read_sources()
        for match in analysis['waveformMatches']:
            match.update(alignmentMethod='bounded-affine-150ms-six-segments-v1', optimizerConverged=True,
                         fittedRelativePpm=332, fixedRatioCorrelation=.897,
                         segments=[{'rms': .1, 'normalizedCorrelation': .999} for _ in range(6)])
        analysis['postAnalysis'] = {
            'method': 'bounded-affine-150ms-six-segments-v1', 'toolFile': 'tools/analyze_native_clock.py',
            'toolSha256': package.digest(files['tools/analyze_native_clock.py']),
            'baseToolSha256': package.digest(files['tools/analyze_native.py']),
            'maxFitPpm': 2000, 'comparisonOnly': True, 'captureFilesUsedReadOnly': True,
            'captureCodeManifestUnchanged': True, 'retainedFirstPass': 'analysis-fraction-only.json',
            **{key: package.file_digest(path) for key, path in {
                'captureManifestSha256': manifest_path, 'captureResultSha256': result_path,
                'firstPassAnalysisSha256': folder / 'analysis-fraction-only.json',
                'referenceSha256': folder / 'reference.f32', 'recordingSha256': recording}.items()}}
        dll = (ROOT / 'build/cmake/Release/auralyn-obs.dll').read_bytes()
        def check(value):
            analysis_path.write_text(json.dumps(value))
            package.check_evidence(files, dll, manifest_path)
        check(analysis)
        mutations = [
            lambda x: x.pop('postAnalysis'),
            lambda x: x['postAnalysis'].__setitem__('method', 'unknown'),
            lambda x: x['postAnalysis'].__setitem__('toolSha256', 'a' * 64),
            lambda x: x['postAnalysis'].__setitem__('baseToolSha256', 'a' * 64),
            lambda x: x['postAnalysis'].__setitem__('maxFitPpm', 4000),
            lambda x: x['postAnalysis'].__setitem__('referenceSha256', 'a' * 64),
            lambda x: x['postAnalysis'].__setitem__('recordingSha256', 'a' * 64),
            lambda x: x['postAnalysis'].__setitem__('firstPassAnalysisSha256', 'a' * 64),
            lambda x: x['waveformMatches'][0]['segments'][2].__setitem__('normalizedCorrelation', .9),
            lambda x: x['waveformMatches'][0]['segments'][2].__setitem__('rms', 0),
            lambda x: x['waveformMatches'][0]['segments'].pop(),
            lambda x: x['waveformMatches'][0].__setitem__('fittedRelativePpm', 2001),
            lambda x: x['waveformMatches'][0].pop('fixedRatioCorrelation'),
            lambda x: x['waveformMatches'][0].__setitem__('optimizerConverged', False),
            lambda x: x['waveformMatches'][0].__setitem__('normalizedCorrelation', .9),
            lambda x: x['waveformMatches'][0].__setitem__('fixedRatioCorrelation', .8),
        ]
        for mutate in mutations:
            invalid = copy.deepcopy(analysis)
            mutate(invalid)
            with self.assertRaises(ValueError):
                check(invalid)

    def test_evidence_rejects_failed_missing_silent_or_incomplete_audio(self):
        import copy
        source = self.synthetic_evidence()
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
        run_path = self.folder / 'result.json'
        recorded_run = json.loads(run_path.read_text(encoding='utf-8'))
        for mode in ['missing', 'changed', 'invalid']:
            candidate = copy.deepcopy(recorded_run)
            if mode == 'missing':
                candidate['baselineBridge'].pop('interpolation')
            elif mode == 'changed':
                candidate['finalBridge']['interpolation'] = 'linear-poc'
            else:
                candidate['baselineBridge']['interpolation'] = candidate['finalBridge']['interpolation'] = 'unknown'
            run_path.write_text(json.dumps(candidate), encoding='utf-8')
            with self.subTest(interpolation=mode), self.assertRaises(ValueError):
                check(valid)
        run_path.write_text(json.dumps(recorded_run), encoding='utf-8')
        manifest_path = self.folder / 'code-manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        header = str(ROOT / 'src/sinc48.hpp')
        for mode in ['missing', 'changed']:
            candidate = copy.deepcopy(manifest)
            if mode == 'missing':
                candidate['files'].pop(header)
            else:
                candidate['files'][header] = 'a' * 64
            manifest_path.write_text(json.dumps(candidate), encoding='utf-8')
            with self.subTest(sinc_header=mode), self.assertRaises(ValueError):
                check(valid)
        engine_key = next(key for key in manifest['files'] if key.lower().endswith('audio_engine.exe'))
        for mode in ['missing', 'invalid', 'ambiguous', 'case-alias', 'invalid-alias']:
            candidate = copy.deepcopy(manifest)
            if mode == 'missing':
                candidate['files'].pop(engine_key)
            elif mode == 'invalid':
                candidate['files'][engine_key] = 'not-a-sha256'
            elif mode == 'ambiguous':
                candidate['files']['other/audio_engine.exe'] = candidate['files'][engine_key]
            else:
                alias = engine_key.replace('\\', '/').swapcase()
                candidate['files'][alias] = 'a' * 64 if mode == 'case-alias' else 'not-a-sha256'
            manifest_path.write_text(json.dumps(candidate), encoding='utf-8')
            with self.subTest(host=mode), self.assertRaises(ValueError):
                check(valid)

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
