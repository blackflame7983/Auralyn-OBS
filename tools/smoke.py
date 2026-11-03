"""通常OBSを変更せず、実libobsの最終mixへの到達・停止・再接続を検証する。"""
import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sender', type=Path, required=True)
    parser.add_argument('--obs-bin', type=Path, default=Path('C:/Program Files/obs-studio/bin/64bit'))
    parser.add_argument('--crash-first', action='store_true')
    parser.add_argument('--stress-stats', action='store_true')
    parser.add_argument('--evidence-dir', type=Path, help='Keep this run separate from earlier acceptance evidence')
    parser.add_argument('--first-rate', type=int, choices=[44100, 48000, 88200, 96000, 176400, 192000], default=48000)
    parser.add_argument('--second-rate', type=int, choices=[44100, 48000, 88200, 96000, 176400, 192000], default=44100)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    binaries = root / 'build/cmake/Release'
    endpoint = f'isolated-{os.getpid()}'
    mode = 'crash' if args.crash_first else 'restart'
    if args.stress_stats:
        mode += '-stress'
    evidence = args.evidence_dir.resolve() if args.evidence_dir else root / 'build'
    evidence.mkdir(parents=True, exist_ok=True)
    log = evidence / f'smoke-{mode}.log'
    needed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP',
              'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'PROGRAMDATA', 'PROGRAMFILES',
              'PROGRAMFILES(X86)', 'PROGRAMW6432', 'ALLUSERSPROFILE'}
    env = {key: value for key, value in os.environ.items() if key.upper() in needed}
    env['PATH'] = str(args.obs_bin) + os.pathsep + env.get('PATH', '')
    children = []
    try:
        with log.open('w', encoding='utf-8') as output:
            command = [str(binaries / 'obs_smoke.exe'), str(binaries / 'auralyn-obs.dll'), str(root), endpoint, '18', '2', str(args.obs_bin)]
            if args.stress_stats: command.append('stress-stats')
            smoke = subprocess.Popen(command, cwd=root, env=env,
                stdout=output, stderr=subprocess.STDOUT)
            children.append(smoke)
            deadline = time.monotonic() + 10
            while 'AURALYN_SMOKE_READY' not in log.read_text(encoding='utf-8', errors='replace'):
                if smoke.poll() is not None:
                    raise RuntimeError(f'libobs initialization failed: {smoke.returncode}; see {log}')
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'libobs did not become ready; see {log}')
                time.sleep(0.05)
            if args.crash_first:
                first = subprocess.Popen([str(args.sender.resolve()), endpoint, '20', str(args.first_rate), '500'],
                    cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                children.append(first)
                time.sleep(3)
                first.terminate()
                first.communicate(timeout=5)
            else:
                subprocess.run([str(args.sender.resolve()), endpoint, '6', str(args.first_rate), '500'], cwd=root, env=env, check=True, timeout=10)
            time.sleep(1)
            subprocess.run([str(args.sender.resolve()), endpoint, '6', str(args.second_rate), '-500'], cwd=root, env=env, check=True, timeout=10)
            smoke.wait(timeout=12)
        lines = log.read_text(encoding='utf-8', errors='replace').splitlines()
        summary = next(json.loads(line) for line in lines if line.startswith('{'))
        summary['mode'] = mode
        summary['exitCode'] = smoke.returncode
        summary['inputRatesHz'] = [args.first_rate, args.second_rate]
        text = '\n'.join(lines)
        summary['maxObsBufferMs'] = max((int(n) for n in re.findall(r'total audio buffering is now (\d+) milliseconds', text)), default=0)
        diagnostic = re.search(r'clock_diagnostics missed_locks=(\d+) resets=(\d+) stale=(\d+)', text)
        if diagnostic: summary.update(zip(['missedLocks','resets','stale'], map(int, diagnostic.groups())))
        (evidence / f'smoke-{mode}.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(summary))
        if smoke.returncode:
            raise RuntimeError(f'Final mix acceptance failed; see {log}')
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


if __name__ == '__main__':
    main()
