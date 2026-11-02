"""Host API検証用の設定・最小webview・sidecarを隔離して準備する。"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

from native_support import runtime_env


def main(args):
    if args.prepare_only:
        root = args.auralyn / 'test-results/issue223' / ('host-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
        root.mkdir(parents=True)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        config = {'identifier': 'com.auralyn.test.obs223', 'build': {'devUrl': f'http://127.0.0.1:{port}'}, 'bundle': {'externalBin': []},
                  'app': {'appDirectoriesOverride': {key: str(root / key) for key in ['config', 'data', 'localData', 'cache', 'log']},
                          'windows': [{'label': 'main', 'title': 'Auralyn OBS223 validation', 'visible': False}],
                          'security': {'capabilities': ['default', {'identifier': 'obs223-exit', 'windows': ['main'], 'permissions': ['process:allow-exit']}]}}}
        (root / 'tauri-config.json').write_text(json.dumps(config), encoding='utf-8')
        print(json.dumps({'root': str(root), 'buildConfig': str(root / 'tauri-config.json')}))
        return
    if not args.root or not args.obs_session:
        raise ValueError('Launch requires --root and --obs-session')
    root = args.root.resolve()
    config = json.loads((root / 'tauri-config.json').read_text(encoding='utf-8'))
    assert config['identifier'] == 'com.auralyn.test.obs223'
    for name in config['app']['appDirectoriesOverride'].values():
        path = Path(name).resolve()
        assert path.is_relative_to(root)
        path.mkdir(parents=True, exist_ok=True)
    port = int(config['build']['devUrl'].rsplit(':', 1)[1])
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))
    web = root / 'web'
    web.mkdir(exist_ok=True)
    (web / 'index.html').write_text('<!doctype html><html lang=ja><meta charset=utf-8><title>Auralyn OBS223 validation</title><body>OBS223 Host API validation</body></html>')
    binaries = root / 'bin'
    binaries.mkdir(exist_ok=True)
    hashes = {}
    for name in ['AuralynHost', 'audio_engine', 'plugin_scanner']:
        target = binaries / (name + '.exe')
        shutil.copy2(args.auralyn / 'src-tauri/target/debug' / target.name, target)
        hashes[target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    meta = json.loads((args.obs_session / 'session.json').read_text())
    env = runtime_env()
    env['AURALYN_OBS_POC_ENDPOINT'] = meta['endpoint']
    for key in ['APPDATA', 'LOCALAPPDATA']:
        path = root / key
        path.mkdir(exist_ok=True)
        env[key] = str(path)
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    if args.frontend:
        # 製品UIの通し確認では環境変数の試作送信を使わず、画面から保存設定を有効にする。
        env.pop('AURALYN_OBS_POC_ENDPOINT', None)
        server_command = ['node', str(args.auralyn / 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', str(port), '--strictPort']
        server_cwd = args.auralyn
    else:
        server_command = [sys.executable, '-m', 'http.server', str(port), '--bind', '127.0.0.1', '--directory', str(web)]
        server_cwd = web
    with (root / 'http.log').open('wb') as log:
        server = subprocess.Popen(server_command, cwd=server_cwd,
                                  env=env, stdout=log, stderr=log, startupinfo=startup)
    try:
        for attempt in range(50):
            try:
                with urllib.request.urlopen(config['build']['devUrl'], timeout=1) as response:
                    assert response.status == 200
                break
            except OSError:
                if server.poll() is not None or attempt == 49:
                    raise RuntimeError('Isolated web server did not start') from None
                time.sleep(.1)
        with (root / 'host.log').open('wb') as log:
            host = subprocess.Popen([str(binaries / 'AuralynHost.exe')], cwd=binaries, env=env, stdout=log, stderr=log, startupinfo=startup)
        session = {'hostPid': host.pid, 'httpPid': server.pid, 'port': port, 'root': str(root), 'hashes': hashes, 'frontend': args.frontend}
        (root / 'session.json').write_text(json.dumps(session, indent=2))
        print(json.dumps(session))
    except BaseException:
        server.terminate()
        server.wait(timeout=5)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--frontend', action='store_true', help='Use the actual product UI served by an isolated Vite process')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--obs-session', type=Path)
    parser.add_argument('--auralyn', type=Path, default=Path(__file__).resolve().parents[2] / 'VSTHost-obs223')
    main(parser.parse_args())
