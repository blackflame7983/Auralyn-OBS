"""隔離した実Tauri Hostの正規APIでVST一体切替を行い、実OBSへ録画する。"""
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import time

from native_acceptance import connect
from native_support import runtime_env, snapshot


def run(args):
    obs, meta = connect(args.obs_session)
    evidence = args.obs_session / ('host-modes-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    evidence.mkdir()
    calls = []
    phases = []
    recording = False
    target_verified = False

    def invoke(command, arguments=None):
        process = subprocess.run(['node', str(args.cli), 'ipc-execute-command', '--raw',
                                  json.dumps({'command': command, 'args': arguments or {}, 'appIdentifier': 'com.auralyn.test.obs223'}), '--json'],
                                 cwd=args.host_root, env=runtime_env(), capture_output=True, timeout=60)
        if process.returncode:
            raise RuntimeError('Tauri CLI failed: ' + command)
        wrapper = json.loads(process.stdout)
        reply = json.loads(wrapper['text'])
        calls.append({'command': command, 'arguments': arguments, 'reply': reply,
                      'recordSeconds': obs.request('GetRecordStatus')['outputDuration'] / 1000 if recording else None})
        (evidence / 'calls.json').write_text(json.dumps(calls, indent=2))
        if not reply.get('success'):
            raise RuntimeError('Host rejected ' + command + ': ' + str(reply.get('error')))
        return reply.get('result')

    def mode(request):
        return invoke('live_modes', {'request': request})

    def phase(name, silent):
        start = obs.request('GetRecordStatus')['outputDuration'] / 1000
        time.sleep(4)
        end = obs.request('GetRecordStatus')['outputDuration'] / 1000
        rack = invoke('session_rack', {'request': {'action': 'snapshot'}})
        stats = invoke('get_engine_runtime_stats')
        phases.append({'name': name, 'silentExpected': silent, 'start': start, 'end': end, 'rack': rack, 'engine': stats})
        print(json.dumps({'phase': name, 'plugins': len(rack.get('plugins', [])), 'muted': [p['muted'] for p in rack.get('plugins', [])]}), flush=True)

    try:
        subprocess.run(['node', str(args.cli), 'driver-session', 'start', '--host', '127.0.0.1', '--port', '9223', '--json'],
                       cwd=args.host_root, env=runtime_env(), capture_output=True, check=True, timeout=30)
        status_process = subprocess.run(['node', str(args.cli), 'driver-session', 'status', '--json'],
                                         cwd=args.host_root, env=runtime_env(), capture_output=True, check=True, timeout=30)
        status = json.loads(json.loads(status_process.stdout)['text'])
        (evidence / 'driver-status.json').write_text(json.dumps(status))
        assert status.get('connected') and status.get('identifier') == 'com.auralyn.test.obs223', 'Unexpected Tauri target'
        target_verified = True
        assert not obs.request('GetStreamStatus')['outputActive']
        assert not obs.request('GetRecordStatus')['outputActive']
        monitor = invoke('live_output', {'config': {'deviceId': None, 'monitorTrim': 0, 'broadcastTrim': 1, 'monitorMuted': True}})
        assert monitor['config']['monitorMuted'] and monitor['config']['monitorTrim'] == 0
        invoke('set_noise_reduction', {'active': False, 'mode': None})
        plugin = invoke('load_plugin', {'path': str(args.vst)})
        native = json.loads(args.native_result.read_text())
        start_config = next(x['payload'] for x in native['events'] if x['command'] == 'Start')
        host_config = {key: start_config[key] for key in ['input', 'output', 'host']}
        host_config.update(bufferSize=512, sampleRate=48000, inputId=start_config['input_id'], outputId=start_config['output_id'], channelRouting=start_config['channel_routing'])
        invoke('start_audio', host_config)
        vst_state = invoke('get_plugin_state', {'id': plugin})
        base = {'path': str(args.vst), 'name': 'OTT', 'vendor': 'Xfer Records', 'version': '', 'enabled': True, 'muted': False, 'gain': 1., 'state': vst_state}
        invoke('save_preset', {'name': 'obs223-singing', 'plugins': [base]})
        invoke('save_preset', {'name': 'obs223-talk', 'plugins': [dict(base, muted=True)]})
        state = mode({'action': 'snapshot'})
        config = {'active': False, 'preset': 'broadcast', 'amount': .2}
        state = mode({'action': 'save', 'expectedVersion': state['definitions']['version'], 'singing': config, 'talk': config})
        for target in ['singing', 'talk']:
            state = mode({'action': 'bind_profile', 'expectedVersion': state['definitions']['version'], 'mode': target, 'name': 'obs223-' + target})
        obs.request('StartRecord')
        recording = True
        deadline = time.monotonic() + 10
        while not obs.request('GetRecordStatus')['outputActive']:
            if time.monotonic() > deadline:
                raise RuntimeError('OBS recording did not start')
            time.sleep(.1)
        phase('host-normal', False)
        for sequence, target in enumerate(['talk', 'singing', 'talk', 'singing'], 1):
            state = mode({'action': 'snapshot'})
            state = mode({'action': target + '_on', 'intent': {'clientId': 'obs223-host', 'sequence': sequence, 'expectedEpoch': state['epoch']}})
            deadline = time.monotonic() + 30
            while state.get('transaction') or state.get('phase') != 'settled':
                if state.get('error') or (state.get('transaction') or {}).get('error'):
                    raise RuntimeError('Host mode transaction failed')
                if time.monotonic() > deadline:
                    raise RuntimeError('Host mode transaction timed out')
                state = mode({'action': 'snapshot'})
            assert state['applied'] == target
            phase('host-' + target + '-' + str(sequence), target == 'talk')
        stopped = obs.request('StopRecord')
        recording = False
        result = {'recording': stopped, 'phases': phases, 'physicalMonitor': invoke('live_output'),
                  'normalObsPreserved': snapshot(Path(meta['normalObsConfig'])) == json.loads((args.obs_session / 'normal-obs-before.json').read_text())}
        (evidence / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps({'completed': str(evidence)}), flush=True)
    finally:
        if recording:
            try:
                obs.request('StopRecord')
            except RuntimeError:
                pass
        try:
            if target_verified:
                invoke('stop_audio')
        finally:
            obs.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--obs-session', type=Path, required=True)
    parser.add_argument('--native-result', type=Path, required=True)
    parser.add_argument('--host-root', type=Path, default=Path(__file__).resolve().parents[2] / 'VSTHost-obs223')
    parser.add_argument('--vst', type=Path, default=Path('C:/Program Files/Common Files/VST3/OTT.vst3'))
    run(parser.parse_args())
