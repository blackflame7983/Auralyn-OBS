"""統計取得集中と実機器スキャンを組み合わせ、既知の8件不足を再確認する。"""
import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

from native_support import Engine, runtime_env

ROOT = Path(__file__).resolve().parents[1]


def main():
    evidence = ROOT / 'build' / ('scan-stress-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    evidence.mkdir()
    engine_dir = evidence / 'scan-engine'
    engine_dir.mkdir()
    engine = Engine(ROOT.parent / 'VSTHost-obs223/src-tauri/target/debug/audio_engine.exe', engine_dir, 'scan-only')
    runs = []
    try:
        for index in range(4):
            folder = evidence / f'round-{index}'
            folder.mkdir()
            count = 0
            times = []
            with (folder / 'driver.log').open('wb') as output:
                child = subprocess.Popen([sys.executable, '-u', str(ROOT / 'tools/smoke.py'), '--sender',
                    str(ROOT.parent / 'VSTHost-obs223/src-tauri/target/debug/examples/obs_pcm_sender.exe'),
                    '--stress-stats', '--evidence-dir', str(folder)], cwd=ROOT, env=runtime_env(), stdout=output, stderr=subprocess.STDOUT)
                while child.poll() is None:
                    ready_log = folder / 'smoke-restart-stress.log'
                    ready = ready_log.exists() and 'AURALYN_SMOKE_READY' in ready_log.read_text(encoding='utf-8', errors='replace')
                    if index and ready:
                        began = time.monotonic()
                        rows = engine.devices()
                        times.append({'seconds': time.monotonic() - began, 'deviceCount': len(rows)})
                        count += 1
                    else:
                        time.sleep(.1)
                code = child.wait()
            summary_file = folder / 'smoke-restart-stress.json'
            summary = json.loads(summary_file.read_text()) if summary_file.exists() else {}
            row = {'round': index, 'concurrentScans': count, 'exitCode': code, 'summary': summary, 'scans': times}
            runs.append(row)
            (evidence / 'result.json').write_text(json.dumps(runs, indent=2))
            print(json.dumps({k: v for k, v in row.items() if k != 'scans'}), flush=True)
        print(json.dumps({'evidence': str(evidence), 'allPassed': all(row['exitCode'] == 0 for row in runs)}), flush=True)
    finally:
        engine.close()


if __name__ == '__main__':
    main()
