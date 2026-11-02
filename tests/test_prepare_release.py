"""未公開候補のclean sourceとSDK選択の境界を検証する。"""
import subprocess
import io
import json
import zipfile
import tempfile
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import prepare_release as release


class PrepareReleaseTests(unittest.TestCase):
    def test_sdk_wrong_commit_and_dirty_are_rejected(self):
        with mock.patch.object(release, 'git', return_value=b'wrong'):
            with self.assertRaises(ValueError):
                release.sdk_sources(Path('unused'))
        with mock.patch.object(release, 'git', side_effect=[release.base.OBS_SDK_COMMIT.encode(), b' M libobs/obs.h']):
            with self.assertRaises(ValueError):
                release.sdk_sources(Path('unused'))

    def test_clean_checkout_and_package_commit_required(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as folder:
            root = Path(folder)
            def git(*args):
                return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True).stdout
            git('init', '-q')
            for name in ['src/plugin.cpp', *release.EXTRA]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name, encoding='utf-8')
            git('add', '--all')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')
            commit = git('rev-parse', 'HEAD').decode().strip()
            source = {'src/plugin.cpp': (root / 'src/plugin.cpp').read_bytes()}
            original = {'sourceState': 'clean-checkout-snapshot', 'pluginSourceCommit': commit,
                        'sourceFilesSha256': {k: release.base.digest(v) for k, v in source.items()}}
            with mock.patch.object(release, 'ROOT', root), mock.patch.object(release.base, 'ROOT', root), \
                    mock.patch.object(release.base, 'read_sources', side_effect=lambda: dict(source)):
                files, found_commit, revision = release.plugin_sources(original)
                self.assertEqual(found_commit, commit)
                self.assertEqual(revision['state'], 'clean-checkout-snapshot')
                self.assertTrue(set(release.EXTRA) <= set(files))
                bad = dict(original, pluginSourceCommit='0' * 40)
                with self.assertRaises(ValueError):
                    release.plugin_sources(bad)
                bad = dict(original, sourceFilesSha256={'src/plugin.cpp': '0' * 64})
                with self.assertRaises(ValueError):
                    release.plugin_sources(bad)
                extra = root / release.EXTRA[0]
                extra.write_text('modified', encoding='utf-8')
                with self.assertRaises(ValueError):
                    release.plugin_sources(original)
                extra.write_text(release.EXTRA[0], encoding='utf-8')
                (root / 'untracked').write_text('user data', encoding='utf-8')
                with self.assertRaises(ValueError):
                    release.plugin_sources(original)

    def test_sdk_selection_must_match_cmake(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as folder:
            build = Path(folder)
            sdk = build / 'sdk'
            (build / 'CMakeFiles/fixture').mkdir(parents=True)
            (build / 'include').mkdir()
            (build / 'bin').mkdir()
            for name in ['cl.exe', 'link.exe', 'include/obsconfig.h', 'obs.def', 'obs.lib', 'bin/obs.dll']:
                (build / name).write_bytes(name.encode())
            (build / 'CMakeFiles/fixture/CMakeCXXCompiler.cmake').write_text(
                f'set(CMAKE_CXX_COMPILER "{(build / "cl.exe").as_posix()}")\n', encoding='utf-8')
            (build / 'CMakeCache.txt').write_text(
                'AURALYN_SINC48_POC:BOOL=ON\nCMAKE_GENERATOR_PLATFORM:INTERNAL=x64\n'
                f'OBS_SOURCE_DIR:PATH={sdk.as_posix()}\nCMAKE_LINKER:FILEPATH={(build / "link.exe").as_posix()}\n'
                f'OBS_BIN_DIR:PATH={(build / "bin").as_posix()}\nCMAKE_GENERATOR:INTERNAL=fixture\n', encoding='utf-8')
            self.assertEqual(release.build_inputs(build)[0], sdk.resolve())
            self.assertEqual(release.build_inputs(build, sdk)[0], sdk.resolve())
            with self.assertRaisesRegex(ValueError, 'SDK'):
                release.build_inputs(build, build / 'other-sdk')

    def test_binary_archive_contains_tracked_locale_and_hash(self):
        locale = release.LOCALE_FILES
        manifest = {'sourceArchive': 'source.zip', 'sourceArchiveSha256': 'a' * 64,
                    'generatedLocaleFilesSha256': {name: release.base.digest(data) for name, data in locale.items()}}
        files = {'LICENSE': b'license', 'NOTICE.md': b'notice'}
        archive_bytes = release.binary_archive(manifest, b'MZ-test', files)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            self.assertEqual(set(archive.namelist()), {'auralyn-obs/bin/64bit/auralyn-obs.dll',
                             'auralyn-obs/data/locale/en-US.ini', 'manifest.json', 'LICENSE', 'NOTICE.md', 'SOURCE.txt'})
            stored = json.loads(archive.read('manifest.json'))
            for name, data in locale.items():
                self.assertEqual(archive.read(name), data)
                self.assertEqual(stored['generatedLocaleFilesSha256'][name], release.base.digest(data))

    def test_output_escape_refused(self):
        with self.assertRaises(ValueError):
            release.prepare(Path(tempfile.gettempdir()), Path('unused'), Path('unused'))


if __name__ == '__main__':
    unittest.main()
