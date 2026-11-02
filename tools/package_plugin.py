"""実機検証済みDLLとローカルソースsnapshotを、未公開の配布候補にまとめる。"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = [
    '.gitattributes', '.gitignore', 'AGENTS.md', 'CMakeLists.txt', 'LICENSE', 'NOTICE.md', 'README.md',
    'protocol.lock.json', 'protocol/OBS_PCM_V1.md', 'protocol/obs-pcm-v1.json',
    'src/audio_clock.hpp', 'src/pcm.hpp', 'src/plugin.cpp', 'tests/obs_smoke.cpp', 'tests/pcm_tests.cpp',
    'tools/verify_protocol.py', 'tools/smoke.py', 'tools/native_acceptance.py', 'tools/native_support.py',
    'tools/obs_client.mjs', 'tools/analyze_native.py', 'tools/native_scenarios.py', 'tools/native_host_modes.py',
    'tools/native_host_session.py', 'tools/native_ui_soak.py', 'tools/scan_stress.py', 'tools/package_plugin.py', 'tools/manage_install.py',
    'tests/test_package_install.py', 'tests/test_native_acceptance.py', 'docs/INSTALL.md', 'docs/DISTRIBUTION_READINESS.md',
]
COMPILED_SOURCE = ['src/plugin.cpp', 'src/pcm.hpp', 'src/audio_clock.hpp']
BINARY_NAME = 'auralyn-obs.dll'
OBS_SDK_COMMIT = 'ba2f32bdf791005443988a4955e963663e16b1ed'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')


def zip_bytes(files):
    import io
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100644 << 16)
            archive.writestr(info, data)
    return out.getvalue()


def read_sources():
    result = {}
    for name in SOURCE_FILES:
        path = ROOT / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError(f'許可したソースがありません: {name}')
        result[name] = path.read_bytes()
    return result


def source_revision(files):
    """親repositoryのcommitや、変更済みsnapshotをclean sourceと表示しない。"""
    unversioned = {'commit': None, 'state': 'unversioned-source-snapshot'}
    def git_bytes(*args):
        return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True,
                              check=True, timeout=10).stdout
    def git(*args):
        return git_bytes(*args).decode('utf-8').strip()
    try:
        if Path(git('rev-parse', '--show-toplevel')).resolve() != ROOT.resolve():
            return unversioned
        commit = git('rev-parse', 'HEAD')
        if not re.fullmatch('[a-f0-9]{40}', commit):
            return unversioned
        tracked = set(git('ls-files', '-z').split('\0'))
        clean = not git('status', '--porcelain=v1', '--untracked-files=normal') and set(files) <= tracked
        if clean:
            # 明示リストはすべてtext。読み取り後にcommitされた場合も、archiveと別commitを結び付けない。
            clean = all(data.replace(b'\r\n', b'\n') == git_bytes('show', f'{commit}:{name}').replace(b'\r\n', b'\n')
                        for name, data in files.items())
            clean = clean and git('rev-parse', 'HEAD') == commit and not git('status', '--porcelain=v1', '--untracked-files=normal')
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, UnicodeError):
        return unversioned
    return {'commit': commit, 'state': 'clean-checkout-snapshot' if clean else 'commit-plus-working-tree-snapshot'}


def check_evidence(files, dll, evidence):
    evidence_bytes = evidence.read_bytes()
    result_bytes = evidence.with_name('result.json').read_bytes()
    analysis_bytes = evidence.with_name('analysis.json').read_bytes()
    recorded = json.loads(evidence_bytes)['files']
    run = json.loads(result_bytes)
    analysis = json.loads(analysis_bytes)
    def number(value):
        return type(value) in (int, float) and math.isfinite(value)

    checks = ['requestedDurationReached', 'recordingContinuousAtSamples',
              'recordingDurationWithinOneSecond', 'audioPacketTimestampsContinuous',
              'audioDurationWithinOneSecond', 'bridgeErrorsZero', 'referenceCaptureComplete',
              'obsAddedBufferZero', 'normalObsPreserved', 'physicalMonitorSilent',
              'codeManifestUnchanged', 'recordedAudioMatchesReference']
    if not isinstance(analysis.get('checks'), dict) or any(analysis['checks'].get(key) is not True for key in checks):
        raise ValueError('実機解析の必須チェックが不足または失敗しています')
    durations = [run.get('durationRequestedSeconds'), run.get('elapsedSeconds'),
                 analysis.get('requestedSeconds'), analysis.get('elapsedSeconds')]
    if any(not number(value) or value < 3600 for value in durations):
        raise ValueError('実機測定時間が不足または不正です')
    packets = analysis.get('audioPackets', {})
    count, frames, seconds = (packets.get(key) for key in ('count', 'frames', 'seconds'))
    if (type(count) is not int or count <= 0 or type(frames) is not int or frames <= 0
            or not number(seconds) or seconds <= 0 or abs(seconds - analysis['requestedSeconds']) > 1
            or abs(frames / 48000 - seconds) > 0.001):
        raise ValueError('音声パケット数または長さが不正です')
    matches = analysis.get('waveformMatches')
    if not isinstance(matches, list):
        raise ValueError('波形比較がありません')
    valid = [m for m in matches if isinstance(m, dict) and number(m.get('seconds'))
             and 0 <= m['seconds'] <= analysis['requestedSeconds']
             and number(m.get('rms')) and m['rms'] > 0
             and number(m.get('normalizedCorrelation')) and 0.95 <= m['normalizedCorrelation'] <= 1]
    if len({m['seconds'] for m in valid}) < 3:
        raise ValueError('3点以上の非無音波形の一致を確認できません')
    if (run.get('durationRequestedSeconds', 0) < 3600 or run.get('elapsedSeconds', 0) < 3600
            or run.get('interrupted') or run.get('normalObsPreserved') is not True
            or run.get('physicalMonitorMuted') is not True or run.get('reference', {}).get('dropped') != 0
            or run.get('reference', {}).get('error') is not None or analysis.get('maxObsAddedBufferMs') != 0
            or analysis.get('audioPackets', {}).get('timestampGaps') != 0):
        raise ValueError('1時間の実機結果を確認できません')
    for key in ['underruns', 'overruns', 'invalid', 'stale', 'resets', 'connections', 'missedLocks', 'clockRebases']:
        if run['finalBridge'][key] != run['baselineBridge'][key]:
            raise ValueError(f'1時間の実機結果で受信状態が変化しています: {key}')
    if not isinstance(recorded, dict):
        raise ValueError('実機manifestのfilesが不正です')
    normalized = {}
    for key, value in recorded.items():
        if not isinstance(key, str) or not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
            raise ValueError('実機manifestのファイルSHA-256が不正です')
        path = key.replace('\\', '/').lower()
        if path in normalized:
            raise ValueError('実機manifestに表記の異なる重複パスがあります')
        normalized[path] = value
    for name in COMPILED_SOURCE:
        key = str(ROOT / name).replace('\\', '/').lower()
        if normalized.get(key) != digest(files[name]):
            raise ValueError(f'実機検証時のソースと不一致: {name}')
    matches = [value for key, value in normalized.items() if key.endswith('/obs-plugins/64bit/auralyn-obs.dll')]
    if len(matches) != 1 or matches[0] != digest(dll):
        raise ValueError('実機検証時のDLLと不一致')
    engines = [value for key, value in normalized.items() if key.endswith('/audio_engine.exe')]
    if len(engines) != 1 or not isinstance(engines[0], str) or not re.fullmatch('[a-f0-9]{64}', engines[0]):
        raise ValueError('検証した本体実行ファイルを一意に確認できません')
    return {'manifestSha256': digest(evidence_bytes), 'resultSha256': digest(result_bytes),
            'analysisSha256': digest(analysis_bytes), 'elapsedSeconds': run['elapsedSeconds'],
            'hostExecutableSha256': engines[0]}


def check_protocol(files, lock):
    if (type(lock.get('major')) is not int or type(lock.get('minor')) is not int
            or (lock.get('major'), lock.get('minor'), lock.get('obsVersion'), lock.get('obsCommit')) != (1, 0, '32.2.2', OBS_SDK_COMMIT)):
        raise ValueError('検証済みのOBS/通信版と一致しません')
    if not re.fullmatch(r'[a-f0-9]{40}', lock.get('auralynBaseCommit', '')):
        raise ValueError('本体の基点commitが不正です')
    for name, key in [('protocol/OBS_PCM_V1.md', 'snapshotSha256'), ('protocol/obs-pcm-v1.json', 'fixtureSha256')]:
        normalized = files[name].decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')
        if digest(normalized) != lock.get(key):
            raise ValueError(f'通信仕様snapshotが一致しません: {name}')


def package(output, evidence, version):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.]+)?', version):
        raise ValueError('版の形式が不正です')
    output = output.resolve()
    if not output.is_relative_to((ROOT / 'build').resolve()):
        raise ValueError('ローカル配布候補の出力先はbuild配下に限定します')
    files = read_sources()
    dll = (ROOT / 'build/cmake/Release' / BINARY_NAME).read_bytes()
    if not dll.startswith(b'MZ'):
        raise ValueError('Windows DLLの形式を確認できません')
    verified_evidence = check_evidence(files, dll, evidence)
    lock = json.loads(files['protocol.lock.json'])
    check_protocol(files, lock)
    source = zip_bytes(files)
    revision = source_revision(files)
    stem = 'auralyn-obs-' + version
    source_name = stem + '-source.zip'
    binary_name = stem + '-windows-x64.zip'
    manifest = {
        'schemaVersion': 1, 'package': 'auralyn-obs', 'version': version, 'localOnly': True,
        'license': 'GPL-3.0-only', 'platform': 'windows-x64', 'obsVersion': lock['obsVersion'],
        'obsSdkCommit': lock['obsCommit'], 'protocolMajor': lock['major'], 'protocolMinor': lock['minor'],
        'sourceKind': 'auralyn_pcm_poc_v1', 'auralynBaseCommit': lock['auralynBaseCommit'],
        'auralynState': 'protocol-base-only; tested sender identified by native evidence', 'minimumAuralynVersion': None,
        'sourceState': revision['state'], 'pluginSourceCommit': revision['commit'], 'sourceArchive': source_name,
        'sourceArchiveSha256': digest(source), 'binarySha256': digest(dll),
        'sourceFilesSha256': {name: digest(data) for name, data in sorted(files.items())},
        'nativeEvidence': verified_evidence,
        'verifiedCompileSources': COMPILED_SOURCE,
        'provenanceLimit': 'The DLL and three plugin source files match the native-run manifest; full build input provenance and release source obligations remain to be reviewed.',
    }
    source_notice = (f'Local, unpublished source snapshot: {source_name}\nSHA-256: {digest(source)}\n'
                     f'OBS SDK: https://github.com/obsproject/obs-studio at {lock["obsCommit"]}\n'
                     f'Plugin source commit: {revision["commit"] or "unversioned"}\nSource state: {revision["state"]}\n'
                     'Local unpublished candidate; full release source obligations remain to be reviewed.\n').encode()
    binary = zip_bytes({'manifest.json': json_bytes(manifest), BINARY_NAME: dll,
                        'LICENSE': files['LICENSE'], 'NOTICE.md': files['NOTICE.md'], 'SOURCE.txt': source_notice})
    output.mkdir(parents=True, exist_ok=False)
    artifacts = {source_name: source, binary_name: binary, 'manifest.json': json_bytes(manifest)}
    for name, data in artifacts.items():
        (output / name).write_bytes(data)
    (output / 'SHA256SUMS.txt').write_text(''.join(f'{digest(data)}  {name}\n' for name, data in sorted(artifacts.items())), encoding='utf-8')
    return {'directory': str(output), 'binaryArchive': binary_name, 'sourceArchive': source_name,
            'binarySha256': digest(dll), 'sourceArchiveSha256': digest(source), 'localOnly': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New directory below build/')
    parser.add_argument('--evidence', type=Path, required=True, help='Native run code-manifest.json')
    parser.add_argument('--version', default='0.1.0-poc')
    args = parser.parse_args()
    try:
        print(json.dumps(package(args.output, args.evidence, args.version), ensure_ascii=True))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        parser.exit(1, f'パッケージ作成失敗: {error}\n')
