"""実Readerのオフライン出力を測定する。実OBS・実機・聴感の合格とは別の証拠。"""
import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from scipy import signal
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
RATE = 48000
TONES = (100, 1000, 10000, 19000)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    with path.open('x', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)


def probe(args):
    if not 4 <= args.seconds <= 300:
        raise ValueError('probe seconds must be 4..300')
    metadata = args.output.with_suffix(args.output.suffix + '.json')
    if args.output.exists() or metadata.exists():
        raise FileExistsError('probe and metadata must be new files')
    time = np.arange(args.seconds * RATE, dtype=np.float64) / RATE
    mono = (0.05 * sum(np.sin(2 * np.pi * frequency * time) for frequency in TONES)).astype('<f4')
    data = np.column_stack((mono, mono)).astype('<f4').tobytes()
    with args.output.open('xb') as file:
        file.write(data)
    write_json(metadata, {'inputKind': 'multitone', 'sampleRateHz': RATE, 'channels': 2,
                         'tonesHz': TONES, 'peakPerTone': .05, 'seconds': args.seconds,
                         'inputSha256': hashlib.sha256(data).hexdigest()})


def psd(samples):
    frequency, power = signal.welch(samples, fs=RATE, axis=0, nperseg=RATE, noverlap=RATE // 2)
    return frequency, np.mean(power, axis=1)


def analyze(args):
    run = args.run.resolve(strict=True)
    stats = json.loads((run / 'stats.json').read_text(encoding='utf-8'))
    if stats['sampleRateHz'] != RATE or stats['channels'] != 2:
        raise ValueError('only stereo 48k measurements are supported')
    before = np.fromfile(run / 'raw-input.f32', dtype='<f4').reshape(-1, 2)
    after = np.fromfile(run / 'after.f32', dtype='<f4').reshape(-1, 2)
    if (len(before) != stats['inputFrames'] or len(after) != stats['outputFrames']
            or not np.isfinite(before).all() or not np.isfinite(after).all()):
        raise ValueError('raw samples do not match finite model output')
    ppm = stats['ppm']
    if not np.isfinite(ppm) or abs(ppm) > 1000:
        raise ValueError('invalid modeled clock difference')
    a = before[RATE:-RATE]
    b = after[RATE + int(stats['prefillFrames']):-RATE]
    if min(len(a), len(b)) < RATE:
        raise ValueError('measurement is too short after trimming')
    rms_a, rms_b = [float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) for x in (a, b)]
    if min(rms_a, rms_b) <= 0:
        raise ValueError('nonzero audio is required for a level-matched comparison')
    frequency, pa = psd(a)
    _, pb = psd(b)
    gains = []
    if args.probe:
        meta = json.loads(args.probe.read_text(encoding='utf-8'))
        if meta['inputSha256'] != sha(run / 'raw-input.f32') or tuple(meta['tonesHz']) != TONES:
            raise ValueError('probe metadata differs from the measured raw input')
        for tone in TONES:
            # 制御の周波数変動を帯域内に含め、固定周波数からのズレを歪率と呼ばない。
            power_a = float(pa[np.abs(frequency - tone) <= 80].sum())
            power_b = float(pb[np.abs(frequency - tone * (1 + ppm * 1e-6)) <= 80].sum())
            if min(power_a, power_b) <= 0:
                raise ValueError('missing probe tone power')
            gains.append({'toneHz': tone, 'bandHalfWidthHz': 80,
                          'afterBandCenterHz': tone * (1 + ppm * 1e-6),
                          'gainDb': float(10 * np.log10(power_b / power_a))})
    gain = rms_a / rms_b
    peak = max(float(np.max(np.abs(before))), float(np.max(np.abs(after))) * gain)
    common_gain = min(1.0, .99 / peak)
    args.output.mkdir(exist_ok=False)
    sf.write(args.output / 'before-listen.wav', before * common_gain, RATE, subtype='FLOAT')
    sf.write(args.output / 'after-listen.wav', after * (gain * common_gain), RATE, subtype='FLOAT')
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), constrained_layout=True)
    for name, samples, power in [('before', a, pa), ('after', b, pb)]:
        axes[0].semilogx(frequency[1:], 10 * np.log10(np.maximum(power[1:], 1e-20)), label=name)
        start = len(samples) // 2
        wave = samples[start:start + 960, 0]
        axes[1].plot(np.arange(len(wave)) / RATE * 1000, wave, alpha=.65, label=name)
    axes[0].set(xlim=(20, 20000), ylim=(-160, 0), xlabel='frequency (Hz)', ylabel='PSD (dB/Hz)',
                title='Raw levels; broad tone bands include clock modulation')
    axes[1].set(xlabel='segment time (ms)', ylabel='amplitude',
                title='Independent middle file positions; not physical delay')
    for axis in axes:
        axis.legend(); axis.grid(True, alpha=.25)
    fig.savefig(args.output / 'comparison.png', dpi=130)
    plt.close(fig)
    result = {
        'scope': 'offline PCM file analysis; source model is declared in modelStats; not shared memory IPC, OBS app, physical audio, real-time cost or listening acceptance',
        'inputSha256': sha(run / 'raw-input.f32'), 'afterSha256': sha(run / 'after.f32'),
        'statsSha256': sha(run / 'stats.json'), 'executableSha256': sha(args.executable),
        'sourceFilesSha256': {name: sha(ROOT / name) for name in ['src/pcm.hpp', 'tests/pcm_tests.cpp', 'tools/pcm_quality.py']},
        'ppm': ppm, 'beforeRms': rms_a, 'afterRms': rms_b,
        'rmsGainDb': float(20 * np.log10(rms_b / rms_a)), 'toneBandGains': gains,
        'levelMatch': {'afterGainBeforeCommonAttenuation': gain, 'commonGain': common_gain},
        'transportModelHealthy': stats['blocks'] > 0 and all(stats[k] == 0 for k in
                ['underruns', 'overruns', 'invalid', 'stale', 'unavailableFramesAfterStart']),
        'prefillFrames': stats['prefillFrames'], 'startupSourceSkippedFrames': stats['startupSourceSkippedFrames'],
        'modelStats': stats, 'listeningAccepted': None,
    }
    write_json(args.output / 'analysis.json', result)
    print(json.dumps({'output': str(args.output), 'transportModelHealthy': result['transportModelHealthy'],
                      'toneBandGains': gains, 'rmsGainDb': result['rmsGainDb']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    make = commands.add_parser('probe')
    make.add_argument('--output', type=Path, required=True)
    make.add_argument('--seconds', type=int, default=70)
    make.set_defaults(run_command=probe)
    inspect = commands.add_parser('analyze')
    inspect.add_argument('--run', type=Path, required=True)
    inspect.add_argument('--output', type=Path, required=True)
    inspect.add_argument('--executable', type=Path, required=True)
    inspect.add_argument('--probe', type=Path, help='JSON metadata for the generated four-tone input')
    inspect.set_defaults(run_command=analyze)
    args = parser.parse_args()
    args.run_command(args)
