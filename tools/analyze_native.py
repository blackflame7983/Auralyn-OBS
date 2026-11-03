"""完了済みの隔離OBS録画を集計する。録音音声そのものは出力しない。"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from scipy.signal import correlate, firwin, resample_poly
from scipy.optimize import minimize_scalar

from native_support import runtime_env

COUNTERS = ['blocks', 'underruns', 'overruns', 'invalid', 'stale', 'resets',
            'missedLocks', 'clockRebases', 'connections']


def waveform_arrived(matches):
    # 無音や照合不能な窓を到達成功に含めない。開始・中盤・終盤の3点以上が必要。
    return len(matches) >= 3 and all(
        not match.get('silent') and not match.get('unavailable')
        and match.get('normalizedCorrelation', 0) >= .95 for match in matches)


def waveform(reference, recording, seconds, rate, output_rate=None):
    # ファイルの開始位置差を含む相対照合。物理経路の総遅延ではない。
    output_rate = output_rate or rate
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(seconds), '-i', str(recording),
                          '-t', '0.15', '-vn', '-f', 'f32le', '-ac', '1', '-ar', str(output_rate), 'pipe:1'],
                         capture_output=True, check=True, env=runtime_env()).stdout
    signal = np.frombuffer(raw, dtype='<f4').astype('f8')
    if len(signal) < int(output_rate * .1):
        return {'seconds': seconds, 'unavailable': True}
    signal -= signal.mean()
    rms = float(np.sqrt(np.mean(signal ** 2)))
    if rms < 1e-7:
        return {'seconds': seconds, 'silent': True, 'rms': rms}
    start = max(0, int((seconds - 5) * rate))
    clip = np.array(reference[start:int((seconds + 5) * rate), 0], dtype='f8')
    if rate != output_rate:
        divisor = math.gcd(rate, output_rate)
        up, down = output_rate // divisor, rate // divisor
        # 独立したオフラインFIRで参照の帯域を揃える。物理SRCや遅延の測定ではない。
        taps = firwin(256 * max(up, down) + 1, .93 / max(up, down), window=('kaiser', 10))
        clip = resample_poly(clip, up, down, window=taps)
    correlation = correlate(clip, signal, mode='valid', method='fft')
    # 平均値と音量に影響されず、一致する短区間を探す。
    squares = np.r_[0., np.cumsum(clip ** 2)]
    sums = np.r_[0., np.cumsum(clip)]
    energy = squares[len(signal):] - squares[:-len(signal)]
    mean_energy = (sums[len(signal):] - sums[:-len(signal)]) ** 2 / len(signal)
    scores = correlation / np.sqrt(np.maximum(energy - mean_energy, 1e-20) * np.sum(signal ** 2))
    index = int(np.argmax(np.abs(scores)))
    # SRCの出力位相は整数sampleに一致しない。最良整数位置の±1sampleだけを
    # 独立FIRで細分化し、音量・波形を変えずに時間位置を揃える。
    margin = 32
    low, high = max(0, index - margin), min(len(clip), index + len(signal) + margin)
    fine = resample_poly(clip[low:high], 16, 1, window=('kaiser', 10))
    positions = (index - low + np.arange(len(signal))) * 16
    def aligned_score(fraction):
        candidate = np.interp(positions + fraction * 16, np.arange(len(fine)), fine)
        candidate -= candidate.mean()
        return float(np.dot(candidate, signal) / np.sqrt(max(np.dot(candidate, candidate), 1e-20) * np.dot(signal, signal)))
    aligned = minimize_scalar(lambda f: -abs(aligned_score(f)), bounds=(-1, 1), method='bounded')
    fraction = float(aligned.x)
    score = aligned_score(fraction)
    return {'seconds': seconds, 'normalizedCorrelation': score,
            'integerPositionCorrelation': float(scores[index]), 'fractionalAlignmentSamples': fraction, 'rms': rms,
            'fileOffsetMs': float(start * 1000 / rate + (index + fraction) * 1000 / output_rate - seconds * 1000)}


def analyze(run):
    result = json.loads((run / 'result.json').read_text(encoding='utf-8'))
    rows = [json.loads(line) for line in (run / 'timeline.jsonl').read_text(encoding='utf-8').splitlines()]
    # 正規Host APIのcamelCaseと単体engine IPCのsnake_caseを、集計時だけ統一する。
    for stats in [result['baseline'], result['finalEngine']] + [row['engine'] for row in rows]:
        if 'queueDiagnostics' in stats:
            stats['queue_diagnostics'] = stats['queueDiagnostics']
    session = run.parents[1]
    recording = Path(result['recording']['outputPath'])
    first, final = result['baselineBridge'], result['finalBridge']
    deltas = {key: final[key] - first[key] for key in COUNTERS}
    before, after = result['baseline']['queue_diagnostics'], result['finalEngine']['queue_diagnostics']
    queue_delta = {key: after[key] - before[key] for key in
                   ['receivedInputFrames', 'requestedOutputFrames', 'zeroFilledOutputFrames',
                    'underflowCallbacks', 'droppedOutputFrames', 'recoveryCount']}
    rate = result['reference']['rate']
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                            'format=duration:stream=codec_name,codec_type,sample_rate,channels,duration,duration_ts,time_base',
                            '-of', 'json', str(recording)], capture_output=True, check=True, env=runtime_env())
    media = json.loads(probe.stdout)
    audio_rate = int(next(s for s in media['streams'] if s['codec_type'] == 'audio')['sample_rate'])
    packets = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                              'packet=pts,duration', '-of', 'csv=p=0', str(recording)],
                             capture_output=True, check=True, env=runtime_env())
    previous_end = None
    packet_gaps = 0
    audio_frames = 0
    packet_count = 0
    for line in packets.stdout.decode('ascii').splitlines():
        pts, frames = (int(value) for value in line.split(','))
        if previous_end is not None and previous_end != pts:
            packet_gaps += 1
        previous_end = pts + frames
        audio_frames += frames
        packet_count += 1
    buffer_ms = []
    for log in (session / 'obs-portable/config/obs-studio/logs').glob('*.txt'):
        buffer_ms.extend(int(value) for value in re.findall(
            r'total audio buffering is now (\d+) milliseconds', log.read_text(encoding='utf-8', errors='replace')))
    observed_queue = [row['bridge']['queued'] * 1000 / rate for row in rows]
    reference = np.memmap(run / 'reference.f32', dtype='<f4', mode='r').reshape(-1, 2)
    duration = float(media['format']['duration'])
    times = sorted(set([min(10, duration / 5), duration / 2, max(1, duration - 3), 60]
                       + list(range(600, int(duration), 600))))
    matches = [waveform(reference, recording, seconds, rate, audio_rate) for seconds in times if seconds < duration - .2]
    manifest = run / 'code-manifest.json'
    unchanged = None
    if manifest.exists():
        files = json.loads(manifest.read_text(encoding='utf-8'))['files']
        unchanged = all(Path(name).is_file() and hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest
                        for name, digest in files.items())
    checks = {
        'requestedDurationReached': result['elapsedSeconds'] >= result['durationRequestedSeconds'] and not result.get('interrupted', False),
        'recordingContinuousAtSamples': all(row['record']['outputActive'] for row in rows),
        'recordingDurationWithinOneSecond': abs(duration - result['elapsedSeconds']) < 1,
        'audioPacketTimestampsContinuous': packet_count > 0 and packet_gaps == 0,
        'audioDurationWithinOneSecond': abs(audio_frames / audio_rate - result['elapsedSeconds']) < 1,
        'bridgeErrorsZero': all(deltas[key] == 0 for key in COUNTERS if key != 'blocks'),
        'referenceCaptureComplete': result['reference']['dropped'] == 0 and result['reference']['error'] is None,
        'obsAddedBufferZero': max(buffer_ms, default=0) == 0,
        'normalObsPreserved': result['normalObsPreserved'],
        'physicalMonitorSilent': result['physicalMonitorMuted'] and result['levels']['output'] == 0,
        'codeManifestUnchanged': unchanged,
        'recordedAudioMatchesReference': waveform_arrived(matches),
    }
    if result.get('syntheticAsioProbe'):
        checks['probePlaybackContinuous'] = result.get('probeUnderflowDelta') == 0 and result.get('probe', {}).get('error') is None
        checks['voicemeeterSettingsRestored'] = result.get('voicemeeterRestored') is True and not result.get('cleanupErrors')
    summary = {'requestedSeconds': result['durationRequestedSeconds'], 'elapsedSeconds': result['elapsedSeconds'],
               'recording': str(recording), 'media': media, 'checks': checks, 'bridgeDelta': deltas,
               'audioPackets': {'count': packet_count, 'timestampGaps': packet_gaps,
                                'frames': audio_frames, 'seconds': audio_frames / audio_rate},
               'referenceRateHz': rate, 'recordingRateHz': audio_rate,
               'queueObservedMs': {'min': min(observed_queue), 'max': max(observed_queue)},
               'maxObsAddedBufferMs': max(buffer_ms, default=0), 'referenceFrames': len(reference),
               'referenceDropped': result['reference']['dropped'], 'inputQueueDelta': queue_delta,
               'inputZeroFilledMs': queue_delta['zeroFilledOutputFrames'] * 1000 / rate,
               'waveformMatches': matches,
               'fileOffsetIsNotPhysicalLatency': True}
    minutes = np.array([row['elapsedSeconds'] / 60 for row in rows])
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True, constrained_layout=True)
    axes[0].plot(minutes, observed_queue, linewidth=1, label='Observed receiver queue')
    axes[0].axhline(50, linestyle='--', color='grey', label='Target: 50 ms')
    axes[0].set_ylabel('OBS queue (ms)'); axes[0].legend(loc='upper right')
    axes[1].plot(minutes, [row['bridge']['correctionPpm'] for row in rows], linewidth=1)
    axes[1].set_ylabel('Correction (ppm)')
    for key in ['underruns', 'overruns', 'resets', 'clockRebases']:
        axes[2].plot(minutes, [row['bridge'][key] - first[key] for row in rows], label='OBS ' + key)
    axes[2].step(minutes, [row['engine']['queue_diagnostics']['underflowCallbacks'] - before['underflowCallbacks']
                          for row in rows], where='post', color='#c05b21', label='Upstream input underflows (separate work)')
    axes[2].set_ylabel('Counter delta'); axes[2].set_xlabel('Elapsed time (minutes)'); axes[2].legend(loc='upper left')
    for axis in axes:
        axis.grid(alpha=.2)
    fig.suptitle(result.get('route', 'Engine -> OTT -> shared memory -> OBS recording'))
    fig.savefig(run / 'timeline.png', dpi=150); plt.close(fig)
    (run / 'analysis.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    analyze(parser.parse_args().run.resolve())
