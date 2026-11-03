"""実マイク/OTTと隔離OBSで無音遷移・復帰・機器スキャンを録画検証する。"""
import argparse
import datetime
import json
from pathlib import Path
import re
import subprocess
import time

import numpy as np

from native_acceptance import NAME, ROOT, connect, prepare
from native_support import Engine, runtime_env, snapshot


def run(args):
    session = args.session or prepare()
    obs, meta = connect(session)
    evidence = session / ('scenarios-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    evidence.mkdir()
    engine = None
    recording = False
    phases = []
    events = []
    scans = []
    telemetry = session / 'telemetry' / (meta['endpoint'] + '.json')

    def bridge():
        return json.loads(telemetry.read_text()) if telemetry.exists() else {}

    def position():
        return obs.request('GetRecordStatus')['outputDuration'] / 1000

    def request(kind, payload=None):
        before = position()
        reply = engine.request(kind, payload)
        events.append({'command': kind, 'payload': payload, 'before': before, 'after': position(), 'reply': reply})
        return reply

    def phase(name, silent=False, seconds=4):
        start = position()
        time.sleep(seconds)
        end = position()
        row = {'name': name, 'silentExpected': silent, 'start': start, 'end': end, 'bridge': bridge(),
               'engine': engine.request('GetRuntimeStats')['payload'] if engine.process.poll() is None else None}
        phases.append(row)
        (evidence / 'progress.json').write_text(json.dumps({'phases': phases, 'scans': scans}))
        print(json.dumps({'phase': name, 'start': start, 'end': end, 'underruns': row['bridge'].get('underruns')}), flush=True)

    def new_engine(number, start_config=None):
        folder = evidence / f'engine-{number}'
        folder.mkdir()
        binary = args.engine or args.auralyn / 'src-tauri/target/debug/audio_engine.exe'
        child = Engine(binary.resolve(strict=True), folder, meta['endpoint'])
        try:
            child.request('LiveOutput', {'config': {'deviceId': None, 'monitorTrim': 0, 'broadcastTrim': 1, 'monitorMuted': True}})
            child.request('SetNoiseReduction', {'active': False, 'mode': None})
            plugin = child.request('LoadPlugin', {'path': str(args.vst)})['payload']
            if start_config is None:
                rows = child.devices()
                selected = json.loads(args.device.read_text(encoding='utf-8'))
                mic = next(d for d in rows if d['is_input'] and d['id'] == selected['id'])
                output = next(d for d in rows if not d['is_input'] and d['host'] == 'Wasapi' and args.output in d['name'])
                start_config = {'host': 'Wasapi', 'input': mic['name'], 'output': output['name'], 'input_id': mic['id'], 'output_id': output['id'],
                                'buffer_size': 512, 'sample_rate': 48000, 'channel_routing': {'inputChannels': [0, 0], 'outputRouting': {'mode': 'independent', 'channels': [0, 1]}}}
            child.request('Start', start_config)
            return child, start_config, plugin
        except BaseException:
            child.close()
            raise

    try:
        assert not obs.request('GetStreamStatus')['outputActive']
        assert not obs.request('GetRecordStatus')['outputActive']
        obs.request('CreateInput', {'sceneName': 'Native223', 'inputName': NAME, 'inputKind': 'auralyn_pcm_poc_v1',
                                  'inputSettings': {'endpoint': meta['endpoint']}, 'sceneItemEnabled': True})
        obs.request('SetInputAudioMonitorType', {'inputName': NAME, 'monitorType': 'OBS_MONITORING_TYPE_NONE'})
        engine, start_config, plugin = new_engine(1)
        time.sleep(2)
        obs.request('StartRecord')
        recording = True
        deadline = time.monotonic() + 10
        while not obs.request('GetRecordStatus')['outputActive']:
            if time.monotonic() > deadline:
                raise RuntimeError('Recording did not start')
            time.sleep(.1)
        phase('normal')
        request('SetGlobalMute', {'active': True})
        phase('global-mute', True)
        request('SetGlobalMute', {'active': False})
        phase('global-unmute')
        request('SetMute', {'id': plugin['id'], 'active': True})
        phase('vst-mute', True)
        request('SetMute', {'id': plugin['id'], 'active': False})
        phase('vst-unmute')

        # 本体の無音ゲートと内蔵モードを実engineで確認する。Hostのrack transactionとは区別する。
        config = lambda preset: {'active': True, 'preset': preset, 'amount': .2}
        request('ConfigureLiveModes', {'definitions': {'schemaVersion': 1, 'version': '1', 'singing': config('singing'), 'talk': config('broadcast')}, 'armed': False})
        for sequence, target in enumerate(['singing_on', 'talk_on'], 1):
            operation = f'obs223-mode-{sequence}'
            request('ModeSilence', {'request': {'action': 'begin', 'id': operation}})
            deadline = time.monotonic() + 5
            while not request('ModeSilence', {'request': {'action': 'snapshot', 'id': operation}})['payload']['ready']:
                if time.monotonic() > deadline:
                    raise RuntimeError('Mode silence did not become ready')
                time.sleep(.05)
            state = request('LiveMode', {'request': {'action': 'snapshot'}})['payload']
            request('LiveMode', {'request': {'action': target, 'intent': {'clientId': 'obs223-native', 'sequence': sequence, 'expectedEpoch': state['epoch']}}})
            phase(target + '-held', True)
            request('ModeSilence', {'request': {'action': 'finish', 'id': operation}})
            deadline = time.monotonic() + 5
            while request('ModeSilence', {'request': {'action': 'snapshot', 'id': operation}})['payload']['active']:
                if time.monotonic() > deadline:
                    raise RuntimeError('Mode silence did not release')
                time.sleep(.05)
            phase(target + '-released')

        request('Stop')
        phase('stopped', True)
        request('Start', start_config)
        phase('stream-restarted', seconds=5)
        old_pid = engine.process.pid
        engine.process.terminate()
        engine.process.wait(timeout=10)
        engine.close()
        events.append({'command': 'owned-engine-crash', 'pid': old_pid, 'after': position()})
        phase('engine-crashed', True)
        engine, _, plugin = new_engine(2, start_config)
        phase('engine-restarted', seconds=5)

        # 定常状態と機器スキャンを交互に測り、上流の不足とOBS側の不足を分ける。
        for index in range(args.scans):
            phase(f'scan-control-{index}', seconds=5)
            before_bridge = bridge()
            before_engine = engine.request('GetRuntimeStats')['payload']['queue_diagnostics']
            started = position()
            rows = engine.devices()
            time.sleep(1.2)  # 1Hzの受信telemetryが操作後まで進むのを待つ。
            after_bridge = bridge()
            after_engine = engine.request('GetRuntimeStats')['payload']['queue_diagnostics']
            item = {'index': index, 'start': started, 'end': position(), 'deviceCount': len(rows),
                    'bridgeDelta': {k: after_bridge[k] - before_bridge[k] for k in ['underruns', 'overruns', 'invalid', 'stale', 'resets', 'connections', 'missedLocks', 'clockRebases']},
                    'inputDelta': {k: after_engine[k] - before_engine[k] for k in ['underflowCallbacks', 'zeroFilledOutputFrames', 'recoveryCount']}}
            scans.append(item)
            print(json.dumps({'scan': item}), flush=True)
        phase('final-normal')
        stopped = obs.request('StopRecord')
        recording = False
        result = {'recording': stopped, 'phases': phases, 'events': events, 'scans': scans, 'levels': engine.levels,
                  'normalObsPreserved': snapshot(Path(meta['normalObsConfig'])) == json.loads((session / 'normal-obs-before.json').read_text())}
        (evidence / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps({'completed': str(evidence), 'normalObsPreserved': result['normalObsPreserved']}), flush=True)
    finally:
        if recording:
            try:
                obs.request('StopRecord')
            except RuntimeError:
                pass
        if engine:
            engine.close()
        obs.close()


def analyze(evidence):
    result = json.loads((evidence / 'result.json').read_text())
    recording = result['recording']['outputPath']
    checks = []
    for row in result['phases']:
        # OBSの保持音の排出を含め、操作直後500msを除いた2秒を評価する。
        raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(row['start'] + .5), '-i', recording,
                              '-t', '2', '-vn', '-f', 'f32le', '-ac', '2', '-ar', '48000', 'pipe:1'],
                             env=runtime_env(), capture_output=True, check=True).stdout
        samples = np.frombuffer(raw, dtype='<f4')
        peak = float(np.max(np.abs(samples))) if len(samples) else None
        rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else None
        passed = len(samples) == 192000 and (peak == 0 if row['silentExpected'] else peak > 1e-5)
        checks.append({'phase': row['name'], 'silentExpected': row['silentExpected'], 'peak': peak, 'rms': rms, 'passed': bool(passed)})
    logs = evidence.parent / 'obs-portable/config/obs-studio/logs'
    added = [int(value) for p in logs.glob('*.txt') for value in re.findall(r'total audio buffering is now (\d+) milliseconds', p.read_text(encoding='utf-8', errors='replace'))]
    packets = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'packet=pts,duration',
                              '-of', 'csv=p=0', recording], env=runtime_env(), capture_output=True, check=True)
    previous_end = None
    gaps = 0
    frames = 0
    for line in packets.stdout.decode('ascii').splitlines():
        pts, count = (int(value) for value in line.split(','))
        if previous_end is not None and previous_end != pts:
            gaps += 1
        previous_end = pts + count
        frames += count
    summary = {'checks': checks, 'allPhasesPassed': all(row['passed'] for row in checks),
               'audioPacketGaps': gaps, 'audioFrames': frames, 'audioSeconds': frames / 48000,
               'maxObsAddedBufferMs': max(added, default=0), 'scans': result.get('scans', []),
               'normalObsPreserved': result['normalObsPreserved'], 'physicalOutputPeak': result.get('levels', {}).get('output')}
    (evidence / 'analysis.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path)
    parser.add_argument('--analyze', type=Path)
    parser.add_argument('--scans', type=int, default=5)
    parser.add_argument('--auralyn', type=Path, default=ROOT.parent / 'VSTHost-obs223')
    parser.add_argument('--engine', type=Path, help='Frozen engine executable; otherwise use the workspace debug binary')
    parser.add_argument('--device', type=Path, default=ROOT.parent / 'VSTHost-obs223/test-results/issue223/native-camera/camera-device.json')
    parser.add_argument('--vst', type=Path, default=Path('C:/Program Files/Common Files/VST3/OTT.vst3'))
    parser.add_argument('--output', default='14T-N72')
    args = parser.parse_args()
    if args.analyze:
        analyze(args.analyze)
    else:
        if not 1 <= args.scans <= 20:
            parser.error('--scans must be 1..20')
        run(args)
