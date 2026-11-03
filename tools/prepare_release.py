"""指定した検証済みDLLの配布候補と、対応するソース入力を準備する。"""
import argparse
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

import package_plugin as base

ROOT = base.ROOT
EXTRA = ['tools/prepare_release.py', 'docs/RELEASE_BUILD.md', 'tests/test_prepare_release.py']
# 現行DLLは翻訳データを参照せず、表示文字列をコンパイルしている。
LOCALE_FILES = {'auralyn-obs/data/locale/en-US.ini': b'Plugin.Name="Auralyn PCM"\n'}
FORBIDDEN = {'.git', 'build', '.env', '__pycache__'}
BINARY_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.ico', '.icns', '.webp', '.bmp', '.svgz', '.pdf', '.zip', '.7z', '.gz', '.xz', '.dll', '.exe', '.lib', '.pdb', '.obj', '.wav', '.mp3', '.mp4', '.ttf', '.otf', '.woff', '.woff2', '.pem', '.key', '.pfx', '.p12', '.der', '.crt', '.cer'}


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, timeout=120).stdout


def plugin_sources(original):
    files = base.read_sources()
    revision = base.source_revision(files)
    if revision['state'] != 'clean-checkout-snapshot':
        raise ValueError('plugin checkout全体をcleanにしてください')
    commit = revision['commit']
    if original.get('sourceState') != 'clean-checkout-snapshot' or original.get('pluginSourceCommit') != commit:
        raise ValueError('指定パッケージのclean source commitと一致しません')
    tracked = set(git(ROOT, 'ls-files', '-z').decode('utf-8').split('\0'))
    for name in EXTRA:
        path = ROOT / name
        if name not in tracked or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError(f'追加ソースがtracked fileではありません: {name}')
        files[name] = path.read_bytes()
    for name, content in files.items():
        committed = git(ROOT, 'show', f'{commit}:{name}')
        if content.replace(b'\r\n', b'\n') != committed.replace(b'\r\n', b'\n'):
            raise ValueError(f'HEADとソースが不一致: {name}')
        if name not in EXTRA and original.get('sourceFilesSha256', {}).get(name) != base.digest(content):
            raise ValueError(f'指定パッケージのソースと不一致: {name}')
    if (git(ROOT, 'rev-parse', 'HEAD').decode().strip() != commit
            or git(ROOT, 'status', '--porcelain=v1', '-z', '--untracked-files=all')):
        raise ValueError('ソースの読み込み中にcheckoutが変更されました')
    return files, commit, revision


def sdk_sources(sdk):
    if git(sdk, 'rev-parse', 'HEAD').decode().strip() != base.OBS_SDK_COMMIT:
        raise ValueError('OBS SDK commitが不一致です')
    if git(sdk, 'status', '--porcelain=v1', '--untracked-files=no', '--ignore-submodules=none'):
        raise ValueError('OBS SDKのtracked sourceに変更があります')
    archive = git(sdk, 'archive', '--format=tar', base.OBS_SDK_COMMIT)
    files, excluded = {}, []
    entries = git(sdk, 'ls-files', '-s', '-z').decode('utf-8').split('\0')
    for entry in filter(None, entries):
        meta, path = entry.split('\t', 1)
        if meta.startswith('160000 '):
            excluded.append({'path': path, 'reason': 'submodule gitlink; content not included'})
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as tar:
        for member in tar:
            if member.isdir():
                continue
            name = PurePosixPath(member.name)
            if name.is_absolute() or any(x in ('', '.', '..') or x.lower() in FORBIDDEN for x in name.parts):
                raise ValueError('OBS source archiveに想定外のパスがあります')
            if member.issym():
                excluded.append({'path': str(name), 'reason': 'symbolic link not followed'})
                continue
            if not member.isfile():
                raise ValueError('OBS source archiveに想定外の種別があります')
            if name.suffix.lower() in BINARY_EXT or any('credential' in x.lower() or 'secret' in x.lower() for x in name.parts):
                excluded.append({'path': str(name), 'reason': 'binary asset or potentially sensitive filename'})
                continue
            data = tar.extractfile(member).read()
            try:
                if b'\x00' in data:
                    raise UnicodeError()
            except UnicodeError:
                if name.suffix.lower() in {'.h', '.hpp', '.in'} or name.name.upper().startswith(('COPYING', 'LICENSE', 'NOTICE')):
                    raise ValueError(f'必要な可能性のあるSDK入力がバイナリとして除外されます: {name}')
                excluded.append({'path': str(name), 'reason': 'binary content'})
                continue
            files[str(name)] = data
    for required in ['COPYING', 'libobs/obs.h', 'libobs/obs-config.h', 'libobs/obsconfig.h.in']:
        if required not in files:
            raise ValueError(f'必須SDK入力がありません: {required}')
    if git(sdk, 'rev-parse', 'HEAD').decode().strip() != base.OBS_SDK_COMMIT or git(sdk, 'status', '--porcelain=v1', '--untracked-files=no', '--ignore-submodules=none'):
        raise ValueError('SDK読み込み中に変更されました')
    return files, excluded


def build_inputs(build, sdk=None):
    cache = (build / 'CMakeCache.txt').read_text(encoding='utf-8')
    def value(key):
        match = re.search(r'^' + re.escape(key) + r':[^=]*=(.*)$', cache, re.M)
        if not match:
            raise ValueError(f'CMake設定がありません: {key}')
        return match.group(1).strip()
    if value('AURALYN_SINC48_POC') != 'ON' or value('CMAKE_GENERATOR_PLATFORM') != 'x64':
        raise ValueError('Sinc48/x64のビルド設定が必要です')
    configured_sdk = Path(value('OBS_SOURCE_DIR')).resolve()
    if sdk is not None and Path(sdk).resolve() != configured_sdk:
        raise ValueError('ビルド設定のSDKが固定checkoutと一致しません')
    compiler_files = list((build / 'CMakeFiles').glob('*/CMakeCXXCompiler.cmake'))
    if len(compiler_files) != 1:
        raise ValueError('compiler設定が一意ではありません')
    match = re.search(r'^set\(CMAKE_CXX_COMPILER "([^"]+)"\)', compiler_files[0].read_text(encoding='utf-8'), re.M)
    if not match:
        raise ValueError('compilerを確認できません')
    paths = {'compilerSha256': Path(match.group(1)), 'linkerSha256': Path(value('CMAKE_LINKER')),
             'generatedObsConfigSha256': build / 'include/obsconfig.h', 'obsImportSourceDllSha256': Path(value('OBS_BIN_DIR')) / 'obs.dll',
             'generatedImportDefSha256': build / 'obs.def', 'generatedImportLibSha256': build / 'obs.lib',
             'cmakeCacheSha256': build / 'CMakeCache.txt'}
    return configured_sdk, {'generator': value('CMAKE_GENERATOR'), 'platform': 'x64', 'sinc48': True,
            'measurement': 'current files; not proof of original build-time inputs',
            **{key: base.digest(path.read_bytes()) for key, path in paths.items()}}


def binary_archive(manifest, dll, files):
    return base.zip_bytes({'auralyn-obs/bin/64bit/' + base.BINARY_NAME: dll,
                           **LOCALE_FILES, 'manifest.json': base.json_bytes(manifest),
                           'LICENSE': files['LICENSE'], 'NOTICE.md': files['NOTICE.md'],
                           'SOURCE.txt': (f"Local unpublished source snapshot: {manifest['sourceArchive']}\n"
                                          f"SHA-256: {manifest['sourceArchiveSha256']}\n"
                                          f"OBS SDK: https://github.com/obsproject/obs-studio commit {base.OBS_SDK_COMMIT}\n").encode()})


def prepare(output, original_dir, evidence, sdk=None):
    output = output.resolve()
    if not output.is_relative_to((ROOT / 'build').resolve()) or output == (ROOT / 'build').resolve():
        raise ValueError('出力先はbuild配下の新規ディレクトリに限定します')
    original = json.loads((original_dir / 'manifest.json').read_text(encoding='utf-8'))
    files, commit, _ = plugin_sources(original)
    base.check_protocol(files, json.loads(files['protocol.lock.json']))
    binary_zip = original_dir / ('auralyn-obs-' + original['version'] + '-windows-x64.zip')
    import zipfile
    with zipfile.ZipFile(binary_zip) as archive:
        dll = archive.read(base.BINARY_NAME)
        if json.loads(archive.read('manifest.json')) != original:
            raise ValueError('指定パッケージのmanifestがZIPと一致しません')
    if not dll.startswith(b'MZ') or base.digest(dll) != original.get('binarySha256'):
        raise ValueError('指定パッケージのDLLが不一致です')
    verified = base.check_evidence(files, dll, evidence)
    if verified != original.get('nativeEvidence') or verified.get('receiverInterpolation') not in ('sinc48-poc', 'sinc96-to48-poc'):
        raise ValueError('指定パッケージの実機結果またはSinc方式が不一致です')
    sdk_path, inputs = build_inputs(ROOT / 'build/cmake', sdk)
    sdk_files, excluded = sdk_sources(sdk_path)
    source = base.zip_bytes({**{'plugin/' + k: v for k, v in files.items()}, **{'obs-sdk/' + k: v for k, v in sdk_files.items()}})
    source_name = 'auralyn-obs-local-source.zip'
    manifest = {'schemaVersion': 1, 'localOnly': True, 'signed': False, 'publicationApproved': False,
                'platform': 'windows-x64', 'obsVersion': '32.2.2', 'receiverInterpolation': verified['receiverInterpolation'],
                'audioScope': '48000/96000 Hz input to 48000 Hz Sinc; other rates use linear interpolation', 'pluginSourceCommit': commit,
                'pluginSourceState': 'clean-checkout-snapshot',
                'pluginFilesSha256': {k: base.digest(v) for k, v in sorted(files.items())},
                'obsSdkCommit': base.OBS_SDK_COMMIT, 'obsSdkFileCount': len(sdk_files), 'obsSdkExcludedFileCount': len(excluded), 'obsSdkExcluded': excluded,
                'obsSdkScope': 'tracked source only; binary assets, sensitive names and submodules excluded; not full OBS source',
                'binarySha256': base.digest(dll), 'nativeEvidence': verified, 'buildInputsObservedNow': inputs,
                'generatedLocaleFilesSha256': {name: base.digest(data) for name, data in LOCALE_FILES.items()},
                'sourceArchive': source_name, 'sourceArchiveSha256': base.digest(source),
                'limitations': 'No original build-time toolchain attestation; source obligations, excluded submodules/assets and release authorization require review. Rebuilt DLL has not passed a one-hour test.'}
    binary = binary_archive(manifest, dll, files)
    output.mkdir(parents=True, exist_ok=False)
    artifacts = {source_name: source, 'auralyn-obs-local-windows-x64.zip': binary, 'manifest.json': base.json_bytes(manifest)}
    for name, data in artifacts.items():
        (output / name).write_bytes(data)
    (output / 'SHA256SUMS.txt').write_text(''.join(f'{base.digest(data)}  {name}\n' for name, data in sorted(artifacts.items())), encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--original', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--sdk', type=Path, help='OBS checkout; default: CMakeCache OBS_SOURCE_DIR')
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.output, args.original, args.evidence, args.sdk), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f'準備失敗: {error}\n')
