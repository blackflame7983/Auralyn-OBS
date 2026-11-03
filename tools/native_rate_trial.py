"""追加rateの実送信/共有メモリ/隔離OBS録画。合成信号であり実音声機器を使わない。"""
import argparse
import ctypes
import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import numpy as np
from scipy.signal import welch

from native_acceptance import ROOT, connect
from native_clock_trial import configure_obs_input, read_json, wait_recording_started
from native_support import Reference, runtime_env, snapshot
from protocol_contract import SUPPORTED_RATES, SINC_MODES, stream_minor

COUNTERS = ('underruns', 'overruns', 'invalid', 'stale', 'resets', 'missedLocks', 'clockRebases', 'connections')


def cleanup_trial(obs, recording, reference, child):
    errors = []
    def attempt(name, action):
        try:
            action()
        except Exception as error:
            errors.append(name + ':' + type(error).__name__)
    if recording:
        attempt('stop-record', lambda: obs.request('StopRecord'))
    if reference:
        attempt('reference-close', reference.close)
    if child:
        if child.poll() is None:
            attempt('sender-terminate', child.terminate)
        try:
            child.communicate(timeout=5)
        except Exception as error:
            errors.append('sender-reap:' + type(error).__name__)
            attempt('sender-kill', child.kill)
            attempt('sender-final-reap', lambda: child.communicate(timeout=5))
    return errors


def cleanup_session(obs):
    errors = []
    try:
        obs.close()
    except Exception as error:
        errors.append('obs-close:' + type(error).__name__)
    try:
        if not ctypes.windll.kernel32.SetThreadExecutionState(0x80000000):
            raise RuntimeError('Power request release failed')
    except Exception as error:
        errors.append('power-release:' + type(error).__name__)
    return errors


def valid_pcm_stream(stream):
    return (stream.get('codec_name') == 'pcm_s16le' and str(stream.get('sample_rate')) == '48000'
            and stream.get('channels') == 2 and stream.get('time_base') == '1/48000')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recording_metrics(path):
    media = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration:stream=codec_type,codec_name,sample_rate,channels,time_base', '-of', 'json', str(path)],
        env=runtime_env(), capture_output=True, check=True).stdout)
    audio = next(s for s in media['streams'] if s['codec_type'] == 'audio')
    if not valid_pcm_stream(audio):
        raise ValueError('Expected PCM16 stereo 48kHz with frame-based packet timestamps')
    packets = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
        'packet=pts,duration', '-of', 'csv=p=0', str(path)], env=runtime_env(), capture_output=True, check=True).stdout
    previous, gaps, frames = None, 0, 0
    for line in packets.decode().splitlines():
        pts, size = map(int, line.split(','))
        if previous is not None and previous != pts:
            gaps += 1
        previous = pts + size
        frames += size
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-ar', '48000', '-ac', '2',
        '-f', 'f32le', 'pipe:1'], env=runtime_env(), capture_output=True, check=True).stdout
    pcm = np.frombuffer(raw, dtype='<f4').reshape(-1, 2)
    frequency, power = welch(pcm[:, 0], fs=48000, nperseg=48000)
    return {'rate': int(audio['sample_rate']), 'channels': int(audio['channels']),
            'codec': audio['codec_name'], 'timeBase': audio['time_base'], 'frames': frames,
            'seconds': frames / int(audio['sample_rate']), 'timestampGaps': gaps,
            'finite': bool(np.isfinite(pcm).all()), 'peak': float(np.max(np.abs(pcm))),
            'rms': float(np.sqrt(np.mean(pcm.astype('f8') ** 2))),
            'oppositeStereoError': float(np.max(np.abs(pcm[:, 0] + pcm[:, 1]))),
            'dominantHz': float(frequency[np.argmax(power)])}


def trial(obs, meta, session, args, rate):
    evidence = session / ('synthetic-rate-' + str(rate) + '-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    evidence.mkdir(exist_ok=False)
    endpoint = meta['endpoint'] + '-' + str(rate)
    configure_obs_input(obs, endpoint)
    sources = [args.sender.resolve(), Path(__file__).resolve()]
    sources += [ROOT / n for n in ('src/pcm.hpp', 'src/sinc48.hpp', 'src/plugin.cpp', 'src/audio_clock.hpp',
                 'tools/native_support.py', 'tools/native_clock_trial.py', 'tools/protocol_contract.py',
                 'build/cmake/CMakeCache.txt')]
    sources += [session / 'obs-portable/obs-plugins/64bit/auralyn-obs.dll']
    sources += [args.source_workspace / n for n in ('src-tauri/src/audio_engine/obs_bridge.rs',
                 'src-tauri/examples/obs_pcm_sender.rs', 'docs/protocols/OBS_PCM_V1.md',
                 'docs/protocols/fixtures/obs-pcm-v1.json')]
    manifest = {str(p): digest(p) for p in sources}
    (evidence / 'code-manifest.json').write_text(json.dumps({'files': manifest}, indent=2))
    child = reference = None
    recording = False
    result = {'scope': 'synthetic 440Hz opposite-stereo -> actual Rust sender -> shared memory -> isolated OBS',
              'requestedSeconds': args.seconds, 'requestedRateHz': rate, 'expectedStreamMinor': stream_minor(rate),
              'physicalAudioDevicesUsed': False, 'microphoneOrVstAcceptance': False}
    try:
        child = subprocess.Popen([str(args.sender.resolve()), endpoint, str(args.seconds + 8), str(rate), '0'],
                                 env=runtime_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(3)
        if child.poll() is not None:
            raise RuntimeError('Owned synthetic sender exited before warmup completed')
        telemetry = session / 'telemetry' / (endpoint + '.json')
        baseline = read_json(telemetry)
        if baseline['producerRate'] != rate or baseline['interpolation'] != SINC_MODES.get(rate, 'linear-poc'):
            raise RuntimeError('The actual producer rate or receiver bank differs')
        reference = Reference(endpoint, evidence / 'reference.f32')
        if not reference.ready.wait(10) or reference.error:
            raise RuntimeError('Reference capture failed')
        obs.request('StartRecord')
        recording = True
        wait_recording_started(obs)
        begin, next_sample = time.monotonic(), 0
        samples = []
        while time.monotonic() - begin < args.seconds:
            elapsed = time.monotonic() - begin
            if elapsed >= next_sample:
                active = obs.request('GetRecordStatus')['outputActive']
                current = read_json(telemetry)
                samples.append({'elapsedSeconds': elapsed, 'recording': active, 'bridge': current})
                if not active or child.poll() is not None or reference.error or reference.dropped:
                    raise RuntimeError('Synthetic OBS trial lost capture or recording')
                print(json.dumps({'rate': rate, 'seconds': round(elapsed, 1),
                                  'underruns': current['underruns'] - baseline['underruns']}), flush=True)
                next_sample = elapsed + 10
            time.sleep(.1)
        result['elapsedSeconds'] = time.monotonic() - begin
        result['recording'] = obs.request('StopRecord')
        recording = False
        result['reference'] = reference.close()
        reference = None
        final = read_json(telemetry)
        result.update(baselineBridge=baseline, finalBridge=final, samples=samples)
        result['bridgeDelta'] = {k: final[k] - baseline[k] for k in COUNTERS}
        # 所有senderは正常停止まで待ち、機器や別プロセスへ触れない。
        child.communicate(timeout=12)
        if child.returncode != 0:
            raise RuntimeError('Synthetic sender did not stop normally')
        metrics = recording_metrics(Path(result['recording']['outputPath']))
        result['recordingMetrics'] = metrics
        added = [int(x) for p in (session / 'obs-portable/config/obs-studio/logs').glob('*.txt')
                 for x in re.findall(r'total audio buffering is now (\d+) milliseconds', p.read_text(encoding='utf-8', errors='replace'))]
        result['checks'] = {
            'durationReached': result['elapsedSeconds'] >= args.seconds,
            'recordingContinuousAtSamples': all(s['recording'] for s in samples),
            'pcmStereo48k': metrics['rate'] == 48000 and metrics['channels'] == 2,
            'durationWithinOneSecond': abs(metrics['seconds'] - result['elapsedSeconds']) < 1,
            'packetTimestampsContinuous': metrics['timestampGaps'] == 0,
            'finiteAudible440Hz': metrics['finite'] and abs(metrics['dominantHz'] - 440) <= 2 and .03 < metrics['rms'] < .04,
            'oppositeStereoPreserved': metrics['oppositeStereoError'] <= 1 / 32768 + 1e-6,
            'referenceComplete': result['reference']['error'] is None and result['reference']['dropped'] == 0,
            'streamRateAndMinor': result['reference']['rate'] == rate and result['reference']['streamMinor'] == stream_minor(rate),
            'bridgeErrorsZero': all(v == 0 for v in result['bridgeDelta'].values()),
            'interpolationStable': final['interpolation'] == baseline['interpolation'],
            'normalObsPreserved': snapshot(Path(meta['normalObsConfig'])) == read_json(session / 'normal-obs-before.json'),
            'obsAddedBufferZero': max(added, default=0) == 0,
            'frozenFilesUnchanged': all(digest(Path(k)) == v for k, v in manifest.items())}
        result['accepted'] = all(result['checks'].values())
        (evidence / 'result.json').write_text(json.dumps(result, indent=2))
        if not result['accepted']:
            raise RuntimeError('Synthetic rate trial failed; inspect retained result')
        print(json.dumps({'rate': rate, 'accepted': True, 'evidence': str(evidence)}), flush=True)
        return result
    except BaseException as error:
        result.update(accepted=False, failureType=type(error).__name__)
        (evidence / 'failure.json').write_text(json.dumps(result, indent=2))
        raise
    finally:
        primary_error = sys.exc_info()[0] is not None
        errors = cleanup_trial(obs, recording, reference, child)
        if errors:
            result.update(accepted=False, cleanupErrors=errors)
            (evidence / 'cleanup.json').write_text(json.dumps(result, indent=2))
            if (evidence / 'result.json').exists():
                (evidence / 'result.json').write_text(json.dumps(result, indent=2))
            if not primary_error:
                raise RuntimeError('Owned trial cleanup failed; inspect retained cleanup.json')


def main(args):
    session = args.session.resolve(strict=True)
    if not session.is_relative_to((ROOT / 'build').resolve()):
        raise ValueError('Use an isolated build session')
    obs, meta = connect(session)
    try:
        if obs.request('GetStreamStatus')['outputActive'] or obs.request('GetRecordStatus')['outputActive']:
            raise RuntimeError('Isolated OBS is already streaming or recording')
        if not ctypes.windll.kernel32.SetThreadExecutionState(0x80000003):
            raise RuntimeError('Temporary power request failed')
        results = [trial(obs, meta, session, args, rate) for rate in args.rates]
        (session / 'synthetic-rates-summary.json').write_text(json.dumps(
            {'accepted': all(r['accepted'] for r in results), 'rates': args.rates,
             'secondsPerRate': args.seconds, 'scope': 'synthetic IPC and actual isolated OBS; not device acceptance'}, indent=2))
    finally:
        primary_error = sys.exc_info()[0] is not None
        errors = cleanup_session(obs)
        if errors:
            (session / 'synthetic-rates-cleanup.json').write_text(json.dumps({'accepted': False, 'errors': errors}, indent=2))
            summary = session / 'synthetic-rates-summary.json'
            if summary.exists():
                value = read_json(summary)
                value.update(accepted=False, cleanupErrors=errors)
                summary.write_text(json.dumps(value, indent=2))
            if not primary_error:
                raise RuntimeError('Owned session cleanup failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--sender', type=Path, required=True)
    parser.add_argument('--source-workspace', type=Path, required=True)
    parser.add_argument('--rates', type=int, nargs='+', choices=SUPPORTED_RATES, default=[88200, 176400, 192000])
    parser.add_argument('--seconds', type=int, default=60)
    args = parser.parse_args()
    if not 15 <= args.seconds <= 120:
        parser.error('seconds must be 15..120')
    main(args)
