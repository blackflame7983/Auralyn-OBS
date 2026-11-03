"""build配下の隔離portable OBSだけで導入・更新・復帰・削除を試す。"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile

from package_plugin import BINARY_NAME, ROOT, digest, json_bytes

MARKER = '.auralyn-obs-isolated-test'
MARKER_CONTENT = 'Auralyn OBS isolated portable test only\n'
MANIFEST = '.auralyn-obs-install.json'
JOURNAL = '.auralyn-obs-transaction.json'
LOCK = '.auralyn-obs-install.lock'
BACKUPS = '.auralyn-obs-backups'
ZIP_FILES = {BINARY_NAME, 'manifest.json', 'LICENSE', 'NOTICE.md', 'SOURCE.txt'}
HASH = re.compile(r'^[a-f0-9]{64}$')
VERSION = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.]+)?$')


def plain_path(root, relative):
    path = root
    for part in Path(relative).parts:
        if part in ('', '.', '..') or ':' in part:
            raise ValueError('管理対象パスが不正です')
        path = path / part
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('シンボリックリンクや再解析ポイントは使えません')
    if not path.resolve(strict=False).is_relative_to(root):
        raise ValueError('管理対象が隔離rootの外にあります')
    return path


def isolated_root(value):
    build_path = ROOT / 'build'
    info = build_path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('buildのシンボリックリンクや再解析ポイントは使えません')
    build = build_path.resolve(strict=True)
    requested = value.absolute()
    if requested == build or not requested.is_relative_to(build):
        raise ValueError('この試作ではリポジトリbuild配下の隔離portable OBSだけを操作できます')
    # root自体や、その途中のjunctionも許可しない。
    root = plain_path(build, requested.relative_to(build)).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('隔離portable OBSのrootはディレクトリを指定してください')
    for name in ['portable_mode.txt', 'bin/64bit/obs64.exe', 'obs-plugins/64bit', MARKER]:
        path = plain_path(root, name)
        if not path.exists():
            raise ValueError(f'隔離portable OBSの確認に必要です: {name}')
    if not plain_path(root, 'portable_mode.txt').is_file() or not plain_path(root, 'bin/64bit/obs64.exe').is_file() or not plain_path(root, 'obs-plugins/64bit').is_dir():
        raise ValueError('portable OBSのディレクトリ構成が不正です')
    marker = plain_path(root, MARKER)
    if not marker.is_file() or marker.read_text(encoding='utf-8') != MARKER_CONTENT:
        raise ValueError('隔離対象の明示マーカーを確認できません')
    return root


def valid_manifest(value):
    if not isinstance(value, dict):
        raise ValueError('管理情報の形式が不正です')
    expected = {'schemaVersion': 1, 'package': 'auralyn-obs', 'platform': 'windows-x64', 'obsVersion': '32.2.2',
                'protocolMajor': 1, 'sourceKind': 'auralyn_pcm_poc_v1'}
    if any(type(value.get(key)) is not type(wanted) or value.get(key) != wanted for key, wanted in expected.items()):
        raise ValueError('版または管理対象が一致しません')
    if type(value.get('protocolMinor')) is not int or value['protocolMinor'] not in (0, 1):
        raise ValueError('対応していない通信minorです')
    if not isinstance(value.get('version'), str) or not VERSION.fullmatch(value['version']):
        raise ValueError('版の形式が不正です')
    for key in ['binarySha256', 'sourceArchiveSha256']:
        if not isinstance(value.get(key), str) or not HASH.fullmatch(value[key]):
            raise ValueError(f'{key}が不正です')
    return {key: value[key] for key in [*expected, 'protocolMinor', 'version', 'binarySha256', 'sourceArchiveSha256']}


def load_package(path):
    with zipfile.ZipFile(path, 'r') as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if set(names) != ZIP_FILES or len(names) != len(ZIP_FILES) or len({name.casefold() for name in names}) != len(names):
            raise ValueError('ZIPのファイル構成が不正です')
        for entry in entries:
            name = entry.filename
            if (entry.is_dir() or '/' in name or '\\' in name or ':' in name or name in ('.', '..')
                    or stat.S_ISLNK(entry.external_attr >> 16) or entry.file_size > 10 * 1024 * 1024):
                raise ValueError('ZIPに許可されないパスまたはファイルがあります')
        manifest = valid_manifest(json.loads(archive.read('manifest.json')))
        dll = archive.read(BINARY_NAME)
        if not dll.startswith(b'MZ') or digest(dll) != manifest['binarySha256']:
            raise ValueError('DLL形式またはSHA-256が一致しません')
        # ZIPのCRCも含めて全メンバーを確認する。ファイルを展開しない。
        for name in ZIP_FILES - {BINARY_NAME, 'manifest.json'}:
            archive.read(name)
    return manifest, dll


def current(root):
    target = plain_path(root, 'obs-plugins/64bit/' + BINARY_NAME)
    info = plain_path(root, MANIFEST)
    if not target.exists() and not info.exists():
        return None, None
    if not target.is_file() or not info.is_file():
        raise ValueError('所有情報とDLLが揃っていません。既存ファイルを保護するため中断しました')
    value = valid_manifest(json.loads(info.read_text(encoding='utf-8')))
    data = target.read_bytes()
    if digest(data) != value['binarySha256']:
        raise ValueError('導入済みDLLが変更されています。上書きや削除を中断しました')
    return value, data


def backup(root, old, data):
    directory = plain_path(root, BACKUPS)
    if directory.exists() and not directory.is_dir():
        raise ValueError('バックアップ先を使えません')
    directory.mkdir(exist_ok=True)
    sha = old['binarySha256']
    dll = plain_path(root, f'{BACKUPS}/{sha}.dll')
    metadata = plain_path(root, f'{BACKUPS}/{sha}.json')
    if dll.exists() and (not dll.is_file() or digest(dll.read_bytes()) != sha):
        raise ValueError('既存のバックアップが一致しません')
    if metadata.exists():
        saved = valid_manifest(json.loads(metadata.read_text(encoding='utf-8')))
        if saved['binarySha256'] != sha:
            raise ValueError('既存のバックアップ情報が一致しません')
    if not dll.exists():
        exclusive_write(dll, data)
    if not metadata.exists():
        exclusive_write(metadata, json_bytes(old))


def load_backup(root, sha):
    if not sha or not HASH.fullmatch(sha):
        raise ValueError('復帰対象のSHA-256を指定してください')
    dll = plain_path(root, f'{BACKUPS}/{sha}.dll')
    meta = plain_path(root, f'{BACKUPS}/{sha}.json')
    if not dll.is_file() or not meta.is_file():
        raise ValueError('指定したバックアップがありません')
    old = valid_manifest(json.loads(meta.read_text(encoding='utf-8')))
    data = dll.read_bytes()
    if old['binarySha256'] != sha or digest(data) != sha:
        raise ValueError('バックアップが変更されています')
    return old, data


def exclusive_write(path, data):
    # 書込み失敗時には今回作った途中ファイルだけを片付ける。既存ファイルは触らない。
    with path.open('xb') as out:
        identity = os.fstat(out.fileno())
        try:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            out.close()
            try:
                current = path.stat()
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    path.unlink()
            except OSError:
                pass
            raise


def atomic_write(path, data):
    # 一時ファイルは同じディレクトリへ置き、既存ファイルとの置換を原子的に行う。
    fd, name = tempfile.mkstemp(prefix='.auralyn-obs-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def plan(root, action, package=None, backup_sha=None):
    journal = plain_path(root, JOURNAL)
    if action != 'recover' and journal.exists():
        raise ValueError('中断した操作があります。recoverを先にプレビューしてください')
    if action == 'recover':
        if not journal.is_file():
            raise ValueError('復旧する操作はありません')
        saved = json.loads(journal.read_text(encoding='utf-8'))
        if set(saved) != {'schemaVersion', 'beforeManifest', 'beforeSha256', 'afterSha256'} or saved['schemaVersion'] != 1:
            raise ValueError('復旧情報が不正です')
        before = saved['beforeManifest']
        if before is not None:
            before = valid_manifest(before)
            if before['binarySha256'] != saved['beforeSha256']:
                raise ValueError('復旧元の情報が不正です')
            restored, data = load_backup(root, saved['beforeSha256'])
            if restored['binarySha256'] != before['binarySha256']:
                raise ValueError('復旧元が一致しません')
        else:
            if saved['beforeSha256'] is not None:
                raise ValueError('復旧元の情報が不正です')
            data = None
        for sha in [saved['beforeSha256'], saved['afterSha256']]:
            if sha is not None and (not isinstance(sha, str) or not HASH.fullmatch(sha)):
                raise ValueError('復旧先の情報が不正です')
        target = plain_path(root, 'obs-plugins/64bit/' + BINARY_NAME)
        if target.exists():
            if not target.is_file() or digest(target.read_bytes()) not in [saved['beforeSha256'], saved['afterSha256']]:
                raise ValueError('中断後にDLLが変更されています')
        info = plain_path(root, MANIFEST)
        if info.exists():
            existing = valid_manifest(json.loads(info.read_text(encoding='utf-8')))
            if existing['binarySha256'] not in [saved['beforeSha256'], saved['afterSha256']]:
                raise ValueError('中断後に管理情報が変更されています')
        return before, data, {'action': action, 'beforeSha256': saved['beforeSha256'], 'afterSha256': saved['afterSha256']}
    old, old_data = current(root)
    if action == 'install':
        if package is None:
            raise ValueError('配布候補ZIPを指定してください')
        new, data = load_package(package)
    elif action == 'restore':
        if old is None:
            raise ValueError('管理対象が導入されていません')
        new, data = load_backup(root, backup_sha)
    elif action == 'remove':
        if old is None:
            raise ValueError('管理対象が導入されていません')
        new, data = None, None
    else:
        raise ValueError('操作が不正です')
    preview = {'action': action, 'root': str(root), 'before': old, 'after': new, 'unchanged': old == new}
    return (old, old_data, new, data), None, preview


def restore_before(root, before, data):
    target = plain_path(root, 'obs-plugins/64bit/' + BINARY_NAME)
    metadata = plain_path(root, MANIFEST)
    if before is None:
        if target.exists(): target.unlink()
        if metadata.exists(): metadata.unlink()
    else:
        if not target.is_file() or digest(target.read_bytes()) != before['binarySha256']:
            atomic_write(target, data)
        if not metadata.is_file() or valid_manifest(json.loads(metadata.read_text(encoding='utf-8'))) != before:
            atomic_write(metadata, json_bytes(before))


@contextmanager
def operation_lock(root):
    # ロックファイルは残す。プロセス終了時にOSがロックだけを解放する。
    path = plain_path(root, LOCK)
    with path.open('a+b') as handle:
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError('別の操作が実行中です') from error
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def apply(root, action, package=None, backup_sha=None):
    with operation_lock(root):
        if action == 'recover':
            before, data, preview = plan(root, action)
            restore_before(root, before, data)
            plain_path(root, JOURNAL).unlink()
            return preview
        (old, old_data, new, data), _, preview = plan(root, action, package, backup_sha)
        if preview['unchanged']:
            return preview
        target = plain_path(root, 'obs-plugins/64bit/' + BINARY_NAME)
        metadata = plain_path(root, MANIFEST)
        journal = plain_path(root, JOURNAL)
        if old is not None:
            backup(root, old, old_data)
        # 異常終了後も元のファイルへ戻せるよう、更新前に復旧情報を固定する。
        entry = {'schemaVersion': 1, 'beforeManifest': old,
                 'beforeSha256': old['binarySha256'] if old else None,
                 'afterSha256': new['binarySha256'] if new else None}
        journal_bytes = json_bytes(entry)
        exclusive_write(journal, journal_bytes)
        try:
            if current(root) != (old, old_data):
                raise ValueError('プレビュー後に管理対象が変更されました')
        except BaseException:
            if journal.is_file() and journal.read_bytes() == journal_bytes:
                journal.unlink()
            raise
        try:
            if new is None:
                target.unlink()
                metadata.unlink()
            else:
                atomic_write(target, data)
                atomic_write(metadata, json_bytes(new))
            journal.unlink()
        except BaseException:
            # 外部変更があれば上書きせず、journalとbackupを残す。
            before, previous_data, _ = plan(root, 'recover')
            restore_before(root, before, previous_data)
            journal.unlink()
            raise
        return preview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'restore', 'remove', 'recover'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--package', type=Path)
    parser.add_argument('--backup', help='SHA-256 of previous DLL')
    parser.add_argument('--apply', action='store_true', help='Apply the previewed operation')
    args = parser.parse_args()
    try:
        root = isolated_root(args.root)
        if args.apply:
            result = apply(root, args.action, args.package, args.backup)
        else:
            _, _, result = plan(root, args.action, args.package, args.backup)
        print(json.dumps({'preview': not args.apply, **result}, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        parser.exit(1, f'操作できません: {error}\n')


if __name__ == '__main__':
    main()
