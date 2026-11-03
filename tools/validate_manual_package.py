"""未公開の手動配置ZIPを隔離フォルダーで検証する。"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import zipfile

from package_plugin import ROOT, digest, file_digest, json_bytes

BINARY_ZIP = 'auralyn-obs-local-windows-x64.zip'
SOURCE_ZIP = 'auralyn-obs-local-source.zip'
DLL = 'auralyn-obs/bin/64bit/auralyn-obs.dll'
LOCALE = 'auralyn-obs/data/locale/en-US.ini'
MEMBERS = {DLL, LOCALE, 'manifest.json', 'LICENSE', 'NOTICE.md', 'SOURCE.txt'}
ARTIFACTS = {BINARY_ZIP, SOURCE_ZIP, 'manifest.json'}
LIMIT = 10 * 1024 * 1024


def checked_output(value):
    build_path = ROOT / 'build'
    path = Path(value).absolute()
    # 存在する祖先を上から調べ、リンクやjunctionを辿らない。
    cursor = Path(path.anchor)
    for part in path.parts[1:-1]:
        if part in ('.', '..'):
            raise ValueError('出力パスが不正です')
        cursor /= part
        info = cursor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('出力パスにリンクまたは再解析ポイントがあります')
    build = build_path.resolve(strict=True)
    parent = path.parent.resolve(strict=True)
    if not parent.is_relative_to(build) or path == build or not parent.is_dir():
        raise ValueError('出力先はbuild配下の新規ディレクトリに限ります')
    if path.exists() or path.is_symlink():
        raise ValueError('出力先は未作成である必要があります')
    return parent / path.name


def read_package(folder):
    folder = Path(folder).resolve(strict=True)
    manifest_bytes = (folder / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or manifest.get('localOnly') is not True or manifest.get('signed') is not False or manifest.get('publicationApproved') is not False:
        raise ValueError('未公開・未署名の候補ではありません')
    if manifest.get('sourceArchive') != SOURCE_ZIP:
        raise ValueError('ソースアーカイブ名が不正です')
    lines = (folder / 'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines()
    sums = {}
    for line in lines:
        match = re.fullmatch(r'([0-9a-f]{64})  ([^/\\]+)', line)
        if not match or match[2] in sums:
            raise ValueError('SHA256SUMSの形式が不正です')
        sums[match[2]] = match[1]
    if set(sums) != ARTIFACTS or any(file_digest(folder / name) != value for name, value in sums.items()):
        raise ValueError('配布物のSHA256SUMSが一致しません')
    if manifest.get('sourceArchiveSha256') != sums[SOURCE_ZIP]:
        raise ValueError('ソースアーカイブのSHA-256が一致しません')
    with zipfile.ZipFile(folder / BINARY_ZIP) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(MEMBERS) or set(names) != MEMBERS or len({n.casefold() for n in names}) != len(names):
            raise ValueError('ZIPのメンバーが不正です')
        data = {}
        for entry in entries:
            mode = entry.external_attr >> 16
            if (entry.is_dir() or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG))
                    or entry.external_attr & 0x400 or entry.file_size > LIMIT):
                raise ValueError('ZIPのメンバー種別またはサイズが不正です')
            data[entry.filename] = archive.read(entry)
    if data['manifest.json'] != manifest_bytes:
        raise ValueError('ZIP内外のmanifestが一致しません')
    if not data[DLL].startswith(b'MZ') or digest(data[DLL]) != manifest.get('binarySha256'):
        raise ValueError('DLLのSHA-256または形式が不正です')
    if manifest.get('generatedLocaleFilesSha256') != {LOCALE: digest(data[LOCALE])}:
        raise ValueError('localeのSHA-256が一致しません')
    source_line = f'Local unpublished source snapshot: {SOURCE_ZIP}\nSHA-256: {sums[SOURCE_ZIP]}\n'.encode()
    if not data['SOURCE.txt'].startswith(source_line):
        raise ValueError('SOURCE.txtの参照が一致しません')
    return {'folder': str(folder), 'artifactsSha256': sums, 'dll': data[DLL], 'locale': data[LOCALE]}


def validate(current, previous, output):
    output = checked_output(output)
    for candidate in (current, previous):
        source = Path(candidate).resolve(strict=True)
        if output.is_relative_to(source):
            raise ValueError('入力候補内に出力できません')
    new, old = read_package(current), read_package(previous)
    output.mkdir()
    plugins = output / 'ProgramData/obs-studio/plugins'
    plugins.mkdir(parents=True)
    backups = output / 'Backups'
    backups.mkdir()
    stages = output / 'Stages'
    stages.mkdir()
    config = output / 'ProgramData/obs-studio/basic/config.ini'
    config.parent.mkdir(parents=True)
    config.write_bytes(b'isolated config sentinel\n')
    unrelated = plugins / 'unrelated-plugin/sentinel.txt'
    unrelated.parent.mkdir()
    unrelated.write_bytes(b'isolated unrelated plugin sentinel\n')
    sentinels = {str(p.relative_to(output)): digest(p.read_bytes()) for p in (config, unrelated)}
    installed = plugins / 'auralyn-obs'
    history = []

    def check(label, package):
        if package is None:
            if installed.exists(): raise ValueError('削除後に対象が残っています')
            actual = None
        else:
            paths = {'dll': installed / 'bin/64bit/auralyn-obs.dll', 'locale': installed / 'data/locale/en-US.ini'}
            files = {str(p.relative_to(installed)).replace('\\', '/') for p in installed.rglob('*') if p.is_file()}
            if files != {'bin/64bit/auralyn-obs.dll', 'data/locale/en-US.ini'}:
                raise ValueError('配置ファイルが不正です')
            actual = {k: file_digest(p) for k, p in paths.items()}
            if actual != {k: digest(package[k]) for k in paths}:
                raise ValueError('配置したファイルがZIPと一致しません')
        if any(file_digest(output / path) != sha for path, sha in sentinels.items()):
            raise ValueError('無関係なファイルが変更されました')
        history.append({'step': label, 'installedSha256': actual})

    def stage(name, package):
        path = stages / name
        (path / 'bin/64bit').mkdir(parents=True)
        (path / 'data/locale').mkdir(parents=True)
        (path / 'bin/64bit/auralyn-obs.dll').write_bytes(package['dll'])
        (path / 'data/locale/en-US.ini').write_bytes(package['locale'])
        return path

    stage('previous-initial', old).rename(installed); check('install-previous', old)
    prepared = stage('current-update-1', new)
    installed.rename(backups / 'previous-1'); prepared.rename(installed); check('update-current', new)
    installed.rename(backups / 'current-1'); (backups / 'previous-1').rename(installed); check('restore-previous', old)
    prepared = stage('current-update-2', new)
    installed.rename(backups / 'previous-2'); prepared.rename(installed); check('update-current-again', new)
    installed.rename(backups / 'current-2'); check('remove', None)
    stage('current-reinstall', new).rename(installed); check('reinstall-current', new)
    result = {'scope': 'isolated manual file copy and directory rename only; not normal OBS, installer, authorship or GPL fulfillment evidence',
              'current': {k: v for k, v in new.items() if k not in ('dll', 'locale')},
              'previous': {k: v for k, v in old.items() if k not in ('dll', 'locale')},
              'sentinelSha256': sentinels, 'steps': history}
    (output / 'evidence.json').write_bytes(json_bytes(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--current', required=True, type=Path)
    parser.add_argument('--previous', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(validate(args.current, args.previous, args.output), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(1, f'検証失敗: {error}\n')


if __name__ == '__main__':
    main()
