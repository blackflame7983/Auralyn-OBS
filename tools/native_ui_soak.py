"""製品UIで保存・開始した実Hostを、隔離OBSへ実時間録画して観測する。"""
import argparse
import ctypes
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import time

from native_acceptance import NAME, connect
from native_support import Reference, runtime_env, snapshot


def recording_route(obs):
    mute = obs.request('GetInputMute', {'inputName': NAME})
    volume = obs.request('GetInputVolume', {'inputName': NAME})
    tracks = obs.request('GetInputAudioTracks', {'inputName': NAME})
    route = {'muted': mute['inputMuted'], 'volume': volume['inputVolumeMul'], 'tracks': tracks['inputAudioTracks']}
    if route['muted'] or abs(route['volume'] - 1) > 1e-6 or route['tracks'].get('1') is not True:
        raise RuntimeError('Owned OBS input is not audible on recording track 1')
    return route


def main(args):
    obs, meta = connect(args.obs_session)
    evidence = args.obs_session / 'runs' / ('ui-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    evidence.mkdir(parents=True)
    reference = None
    recording = False
    completed = False
    # HDMI音声を使う検証中だけ画面の省電力停止も抑える。永続的な電源設定は変えない。
    if not ctypes.windll.kernel32.SetThreadExecutionState(0x80000003):
        obs.close()
        raise RuntimeError('Temporary system/display power request failed')

    def invoke(command):
        process = subprocess.run(['node', str(args.cli), 'ipc-execute-command', '--raw',
            json.dumps({'command': command, 'args': {'config': None} if command == 'obs_output' else {}, 'appIdentifier': 'com.auralyn.test.obs223'}), '--json'],
            cwd=args.host_root, env=runtime_env(), capture_output=True, check=True, timeout=30)
        reply = json.loads(json.loads(process.stdout)['text'])
        if not reply.get('success'):
            raise RuntimeError('Host command failed: ' + command)
        return reply['result']

    def webview(script):
        process = subprocess.run(['node', str(args.cli), 'webview-execute-js', '--script', script,
                                  '--app-identifier', 'com.auralyn.test.obs223', '--json'],
                                 cwd=args.host_root, env=runtime_env(), capture_output=True, check=True, timeout=30)
        return json.loads(process.stdout)['text'].split('\n\n[Executed in window:', 1)[0]

    try:
        assert not obs.request('GetStreamStatus')['outputActive']
        assert not obs.request('GetRecordStatus')['outputActive']
        output = invoke('obs_output')
        assert output['supported'] and output['config']['enabled'] and output['state'] in ('sending', 'quiet') and not output['editable']
        monitor = invoke('live_output')['config']
        assert monitor['monitorMuted'] and monitor['monitorTrim'] == 0
        audio = invoke('get_audio_state')
        assert audio['is_running']
        webview("(() => { if(window.__obs223unlisten)window.__obs223unlisten(); window.__obs223levels={input:0,output:0,events:0}; return window.__TAURI__.event.listen('audio-level',e=>{const a=window.__obs223levels;a.input=Math.max(a.input,...e.payload.input);a.output=Math.max(a.output,...e.payload.output);a.events++;}).then(u=>{window.__obs223unlisten=u;return 'ready';});})()")
        endpoint = output['config']['endpoint']
        inputs = obs.request('GetInputList')['inputs']
        if not any(row['inputName'] == NAME for row in inputs):
            obs.request('CreateInput', {'sceneName': 'Native223', 'inputName': NAME, 'inputKind': 'auralyn_pcm_poc_v1',
                        'inputSettings': {'endpoint': endpoint}, 'sceneItemEnabled': True})
        else:
            obs.request('SetInputSettings', {'inputName': NAME, 'inputSettings': {'endpoint': endpoint}, 'overlay': True})
        obs.request('SetInputAudioMonitorType', {'inputName': NAME, 'monitorType': 'OBS_MONITORING_TYPE_NONE'})
        route = recording_route(obs)
        telemetry = args.obs_session / 'telemetry' / (endpoint + '.json')
        deadline = time.monotonic() + 10
        while not telemetry.exists():
            if time.monotonic() > deadline:
                raise RuntimeError('Receiver telemetry did not become ready')
            time.sleep(.1)
        time.sleep(1.2)
        baseline = invoke('get_engine_runtime_stats')
        assert baseline['enabledPluginCount'] == 1 and not baseline['globalBypass']
        bridge = json.loads(telemetry.read_text())
        host_session = json.loads((args.host_root / 'session.json').read_text())
        code_files = [args.host_root / 'bin' / (name + '.exe') for name in ['AuralynHost', 'audio_engine', 'plugin_scanner']]
        code_files += [args.obs_session / 'obs-portable/obs-plugins/64bit/auralyn-obs.dll']
        code_files += [Path(__file__).resolve(), Path(__file__).with_name('analyze_native.py'), args.host_root / 'tauri-config.json']
        workspace = args.host_root.resolve().parents[2]
        code_files += [workspace / name for name in ['src/components/features/AudioSettings/ObsPcmOutputSettings.tsx',
            'src/components/features/AudioSettings/ObsConnectionSettings.tsx', 'src/api/obsOutput.ts', 'src/i18n/messages.ts',
            'src-tauri/src/audio/obs_output.rs', 'src-tauri/src/audio_engine/obs_output.rs', 'src-tauri/src/audio_engine/obs_bridge.rs',
            'src-tauri/src/audio_engine/core.rs', 'src-tauri/src/audio_engine/core/startup.rs',
            'src-tauri/src/audio_engine/core/commands.rs', 'src-tauri/src/audio.rs', 'src-tauri/src/audio/conditions.rs',
            'src-tauri/src/ipc.rs', 'src-tauri/src/migration.rs', 'src-tauri/Cargo.toml']]
        manifest = {'files': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in code_files}}
        (evidence / 'code-manifest.json').write_text(json.dumps(manifest, indent=2))
        reference = Reference(endpoint, evidence / 'reference.f32')
        if not reference.ready.wait(15) or reference.error:
            raise RuntimeError('Reference capture did not start')
        obs.request('StartRecord')
        recording = True
        deadline = time.monotonic() + 10
        while not obs.request('GetRecordStatus')['outputActive']:
            if time.monotonic() > deadline:
                raise RuntimeError('OBS did not start recording')
            time.sleep(.1)
        begin = time.monotonic()
        result = {'durationRequestedSeconds': args.duration, 'recordingStartedUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'route': 'product UI saved setting -> actual Host/USB microphone/OTT -> isolated OBS',
                  'hostSession': host_session, 'baseline': baseline, 'baselineBridge': bridge, 'obsOutput': output,
                  'physicalMonitorMuted': True, 'obsRecordingRoute': route, 'audioState': audio,
                  'temporaryPowerRequest': 'system-and-display; restored on exit'}
        (evidence / 'run.json').write_text(json.dumps({k: v for k, v in result.items() if k not in ('baseline', 'hostSession')}, indent=2))
        print(json.dumps({'evidence': str(evidence), 'requestedSeconds': args.duration}), flush=True)
        next_log = begin
        with (evidence / 'timeline.jsonl').open('w', encoding='utf-8') as timeline:
            while time.monotonic() - begin < args.duration:
                if (evidence / 'stop.request').exists():
                    result['interrupted'] = True
                    break
                now = time.monotonic()
                if now >= next_log:
                    status = obs.request('GetRecordStatus')
                    engine = invoke('get_engine_runtime_stats')
                    output = invoke('obs_output')
                    route = recording_route(obs)
                    latest = json.loads(telemetry.read_text())
                    row = {'elapsedSeconds': now - begin, 'record': status, 'engine': engine, 'bridge': latest, 'obsOutput': output,
                           'referenceFrames': reference.frames, 'referenceDropped': reference.dropped, 'obsRecordingRoute': route}
                    timeline.write(json.dumps(row) + '\n')
                    timeline.flush()
                    (evidence / 'status.json').write_text(json.dumps(row))
                    assert status['outputActive'] and output['state'] in ('sending', 'quiet') and reference.error is None
                    print(json.dumps({'seconds': round(now - begin, 1), 'state': output['state'], 'queueMs': latest['queued'] / 48,
                                      'underruns': latest['underruns'] - bridge['underruns'], 'referenceDropped': reference.dropped}), flush=True)
                    next_log = now + 10
                time.sleep(.1)
        result['elapsedSeconds'] = time.monotonic() - begin
        result['recording'] = obs.request('StopRecord')
        recording = False
        result['finalEngine'] = invoke('get_engine_runtime_stats')
        result['finalBridge'] = json.loads(telemetry.read_text())
        result['finalObsOutput'] = invoke('obs_output')
        result['finalObsRecordingRoute'] = recording_route(obs)
        result['reference'] = reference.close()
        reference = None
        result['levels'] = json.loads(webview('JSON.stringify(window.__obs223levels)'))
        assert result['levels']['events'] > 0 and result['levels']['output'] == 0
        result['normalObsPreserved'] = snapshot(Path(meta['normalObsConfig'])) == json.loads((args.obs_session / 'normal-obs-before.json').read_text())
        result['codeManifestUnchanged'] = all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == value for p, value in manifest['files'].items())
        (evidence / 'result.json').write_text(json.dumps(result, indent=2))
        completed = True
        print(json.dumps({'elapsedSeconds': result['elapsedSeconds'], 'normalObsPreserved': result['normalObsPreserved'], 'codeManifestUnchanged': result['codeManifestUnchanged']}), flush=True)
    finally:
        if recording:
            try:
                obs.request('StopRecord')
            except RuntimeError:
                pass
        if reference:
            reference.close()
        if not completed:
            try:
                invoke('stop_audio')
            except (RuntimeError, OSError, subprocess.SubprocessError):
                pass
        obs.close()
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--host-root', type=Path, required=True)
    parser.add_argument('--obs-session', type=Path, required=True)
    parser.add_argument('--duration', type=int, default=3600)
    args = parser.parse_args()
    if not 10 <= args.duration <= 7200:
        parser.error('--duration must be 10..7200 seconds')
    main(args)
