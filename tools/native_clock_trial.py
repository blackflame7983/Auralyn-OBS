"""engine・受信補間を固定し、隔離OBSへ実USB入力を送る明示検証。"""
import argparse
import ctypes
from ctypes import wintypes
import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / 'tools'))
from native_acceptance import NAME, connect
from native_support import Engine, Reference, snapshot
from native_ui_soak import recording_route


def read_json(path):
    for attempt in range(3):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(.01)


def check_clock(stats, expected):
    clock = stats['queue_diagnostics'].get('inputClock')
    if expected == 'on':
        if not clock or not clock['enabled'] or not clock['started'] or clock['failed']:
            raise RuntimeError('Expected input clock correction is not running')
    elif clock is not None:
        raise RuntimeError('Baseline unexpectedly enabled input clock correction')
    return clock


def configure_obs_input(obs, endpoint):
    inputs = obs.request('GetInputList')['inputs']
    existing = next((row for row in inputs if row['inputName'] == NAME), None)
    if existing is None:
        obs.request('CreateInput', {'sceneName': 'Native223', 'inputName': NAME, 'inputKind': 'auralyn_pcm_poc_v1',
                                  'inputSettings': {'endpoint': endpoint}, 'sceneItemEnabled': True})
    else:
        if existing['inputKind'] != 'auralyn_pcm_poc_v1':
            raise RuntimeError('Owned input name belongs to another source kind')
        obs.request('SetInputSettings', {'inputName': NAME, 'inputSettings': {'endpoint': endpoint}, 'overlay': True})
    obs.request('SetInputAudioMonitorType', {'inputName': NAME, 'monitorType': 'OBS_MONITORING_TYPE_NONE'})
    return recording_route(obs)


def wait_recording_started(obs, timeout=15):
    deadline = time.monotonic() + timeout
    while not obs.request('GetRecordStatus')['outputActive']:
        if time.monotonic() >= deadline:
            raise RuntimeError('OBS recording did not start within the deadline')
        time.sleep(.1)


def engine_cpu_seconds(engine):
    # 所有する子processのCPU時間だけを照会する。callbackの最悪時間の測定ではない。
    query = ctypes.windll.kernel32.GetProcessTimes
    query.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    query.restype = wintypes.BOOL
    created, exited, kernel, user = [wintypes.FILETIME() for _ in range(4)]
    if not query(wintypes.HANDLE(int(engine.process._handle)), ctypes.byref(created), ctypes.byref(exited),
                 ctypes.byref(kernel), ctypes.byref(user)):
        raise RuntimeError('Owned engine CPU time query failed')
    return sum((t.dwHighDateTime << 32) + t.dwLowDateTime for t in [kernel, user]) / 10_000_000


def main(args):
    session = args.obs_session.resolve(strict=True)
    if not session.is_relative_to((PLUGIN / 'build').resolve()):
        raise ValueError('OBS session must be in the isolated plugin build directory')
    obs, meta = connect(session)
    evidence = session / 'runs' / ('clock-' + args.expect_clock + '-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    evidence.mkdir(parents=True, exist_ok=False)
    engine = reference = None
    recording = False
    result = {'durationRequestedSeconds': args.duration, 'expectedInputClock': args.expect_clock,
              'route': 'USB microphone -> actual engine/OTT -> shared memory -> isolated OBS',
              'physicalMonitorMuted': True}
    try:
        if not ctypes.windll.kernel32.SetThreadExecutionState(0x80000003):
            raise RuntimeError('Temporary system/display power request failed')
        assert not obs.request('GetStreamStatus')['outputActive'] and not obs.request('GetRecordStatus')['outputActive']
        result['obsRecordingRoute'] = configure_obs_input(obs, meta['endpoint'])
        engine_root = evidence / 'engine'
        engine_root.mkdir()
        engine = Engine(args.engine.resolve(strict=True), engine_root, meta['endpoint'])
        devices = engine.devices()
        selected = read_json(args.camera)
        mic = next(row for row in devices if row['id'] == selected['id'] and row['is_input'])
        outputs = [row for row in devices if row['host'] == 'Wasapi' and not row['is_input'] and args.output in row['name']]
        if len(outputs) != 1:
            raise RuntimeError('Exactly one explicit output clock is required')
        output = outputs[0]
        engine.request('LiveOutput', {'config': {'deviceId': None, 'monitorTrim': 0, 'broadcastTrim': 1, 'monitorMuted': True}})
        engine.request('SetNoiseReduction', {'active': False, 'mode': None})
        plugin = engine.request('LoadPlugin', {'path': str(args.vst)})['payload']
        started = engine.request('Start', {'host': 'Wasapi', 'input': mic['name'], 'output': output['name'],
            'input_id': mic['id'], 'output_id': output['id'], 'buffer_size': 512, 'sample_rate': 48000,
            'channel_routing': {'inputChannels': [0, 0], 'outputRouting': {'mode': 'independent', 'channels': [0, 1]}}})['payload']
        time.sleep(args.warmup)
        baseline = engine.request('GetRuntimeStats')['payload']
        assert baseline['enabled_plugin_count'] == 1 and not baseline['global_bypass']
        assert baseline['queue_diagnostics']['state'] == 'running'
        check_clock(baseline, args.expect_clock)
        telemetry = session / 'telemetry' / (meta['endpoint'] + '.json')
        result.update(input=mic, outputClock=output, plugin=plugin, started=started, baseline=baseline,
                      baselineBridge=read_json(telemetry), warmupSeconds=args.warmup)
        if result['baselineBridge'].get('interpolation') != args.expect_interpolation:
            raise RuntimeError('The expected receiver interpolation is not running')
        result['baselineCpuSeconds'] = engine_cpu_seconds(engine)
        result['baselineObsReportedStats'] = obs.request('GetStats')
        sources = [args.engine.resolve(), Path(__file__).resolve(), session / 'obs-portable/obs-plugins/64bit/auralyn-obs.dll']
        sources += [PLUGIN / name for name in ['src/plugin.cpp', 'src/pcm.hpp', 'src/audio_clock.hpp', 'src/sinc48.hpp',
                    'CMakeLists.txt', 'build/cmake/CMakeCache.txt',
                    'tools/native_support.py', 'tools/native_ui_soak.py', 'tools/analyze_native.py']]
        sources += [args.source_workspace / name for name in ['src-tauri/Cargo.toml', 'src-tauri/Cargo.lock',
                    'src-tauri/src/audio_engine/mod.rs', 'src-tauri/src/audio_engine/input_clock.rs',
                    'src-tauri/src/audio_engine/queue_diagnostics.rs', 'src-tauri/src/audio_engine/queue_recovery.rs',
                    'src-tauri/src/audio_engine/core/startup.rs',
                    'src-tauri/src/audio_engine/core/lifecycle.rs', 'src-tauri/src/audio_engine/core/startup_callbacks.rs']
                   if (args.source_workspace / name).is_file()]
        manifest = {'files': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}}
        (evidence / 'code-manifest.json').write_text(json.dumps(manifest, indent=2))
        reference = Reference(meta['endpoint'], evidence / 'reference.f32')
        if not reference.ready.wait(15) or reference.error:
            raise RuntimeError('Reference capture failed')
        obs.request('StartRecord')
        recording = True
        wait_recording_started(obs)
        result['recordingStartedUtc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        begin = time.monotonic()
        (evidence / 'run.json').write_text(json.dumps({'duration': args.duration, 'expectClock': args.expect_clock,
            'recordingStartedUtc': result['recordingStartedUtc'], 'baselineBridge': result['baselineBridge']}, indent=2))
        print(json.dumps({'evidence': str(evidence), 'requestedSeconds': args.duration, 'inputClock': args.expect_clock}), flush=True)
        next_sample = begin
        with (evidence / 'timeline.jsonl').open('w', encoding='utf-8') as timeline:
            while time.monotonic() - begin < args.duration:
                if (evidence / 'stop.request').exists():
                    result['interrupted'] = True
                    break
                now = time.monotonic()
                if now >= next_sample:
                    stats = engine.request('GetRuntimeStats')['payload']
                    clock = check_clock(stats, args.expect_clock)
                    record = obs.request('GetRecordStatus')
                    bridge = read_json(telemetry)
                    if bridge.get('interpolation') != args.expect_interpolation:
                        raise RuntimeError('Receiver interpolation changed during the trial')
                    row = {'elapsedSeconds': now - begin, 'engine': stats, 'bridge': bridge, 'record': record,
                           'obsReportedStats': obs.request('GetStats'),
                           'obsRecordingRoute': recording_route(obs), 'referenceDropped': reference.dropped,
                           'referenceFrames': reference.frames, 'engineCpuSeconds': engine_cpu_seconds(engine)}
                    timeline.write(json.dumps(row) + '\n')
                    timeline.flush()
                    (evidence / 'status.json').write_text(json.dumps(row))
                    assert (record['outputActive'] and reference.error is None and engine.process.poll() is None
                            and stats['queue_diagnostics']['state'] == 'running')
                    queue = stats['queue_diagnostics']
                    print(json.dumps({'seconds': round(now - begin, 1), 'queueMs': queue['currentAudioMs'],
                        'inputUnderflows': queue['underflowCallbacks'] - baseline['queue_diagnostics']['underflowCallbacks'],
                        'inputZeroMs': (queue['zeroFilledOutputFrames'] - baseline['queue_diagnostics']['zeroFilledOutputFrames']) / 48,
                        'clockPpm': clock['correctionPpm'] if clock else None,
                        'obsUnderruns': bridge['underruns'] - result['baselineBridge']['underruns']}), flush=True)
                    next_sample = now + 10
                time.sleep(.1)
        result['elapsedSeconds'] = time.monotonic() - begin
        result['recording'] = obs.request('StopRecord')
        recording = False
        result['finalEngine'] = engine.request('GetRuntimeStats')['payload']
        result['finalCpuSeconds'] = engine_cpu_seconds(engine)
        result['finalObsReportedStats'] = obs.request('GetStats')
        result['finalBridge'] = read_json(telemetry)
        result['levels'] = engine.levels
        result['reference'] = reference.close()
        reference = None
        assert result['levels']['events'] > 0 and result['levels']['output'] == 0
        result['finalObsRecordingRoute'] = recording_route(obs)
        result['normalObsPreserved'] = snapshot(Path(meta['normalObsConfig'])) == read_json(session / 'normal-obs-before.json')
        result['codeManifestUnchanged'] = all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == sha for p, sha in manifest['files'].items())
        (evidence / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps({'completedSeconds': result['elapsedSeconds'], 'normalObsPreserved': result['normalObsPreserved'],
                          'codeManifestUnchanged': result['codeManifestUnchanged']}), flush=True)
    except BaseException as error:
        failure = {'accepted': False, 'exception': type(error).__name__}
        if engine and engine.process.poll() is None:
            try:
                failure['engineStats'] = engine.request('GetRuntimeStats')['payload']
            except (OSError, RuntimeError):
                pass
        (evidence / 'failure.json').write_text(json.dumps(failure, indent=2))
        raise
    finally:
        if recording:
            try:
                obs.request('StopRecord')
            except RuntimeError:
                pass
        if reference:
            reference.close()
        if engine:
            engine.close()
        obs.close()
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', type=Path, required=True)
    parser.add_argument('--source-workspace', type=Path, required=True)
    parser.add_argument('--obs-session', type=Path, required=True)
    parser.add_argument('--camera', type=Path, required=True)
    parser.add_argument('--expect-clock', choices=['on', 'off'], required=True)
    parser.add_argument('--expect-interpolation', choices=['linear-poc', 'sinc48-poc'], default='sinc48-poc')
    parser.add_argument('--duration', type=int, default=60)
    parser.add_argument('--warmup', type=int, default=15)
    parser.add_argument('--output', default='FS2333')
    parser.add_argument('--vst', type=Path, default=Path('C:/Program Files/Common Files/VST3/OTT.vst3'))
    args = parser.parse_args()
    if not 10 <= args.duration <= 7200 or not 2 <= args.warmup <= 60:
        parser.error('duration must be 10..7200 and warmup 2..60 seconds')
    main(args)
