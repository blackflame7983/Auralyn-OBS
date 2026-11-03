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
WIDE_TONES = (*TONES, 20000)
ALIAS_TONES = (24500, 26000, 30000, 40000)


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
    rate = getattr(args, 'sample_rate', RATE)
    kind = getattr(args, 'kind', 'passband')
    if rate not in (48000, 96000) or kind not in ('passband', 'alias') or (kind == 'alias' and rate != 96000):
        raise ValueError('alias probes require 96k; passband probes support 48k/96k')
    tones = ALIAS_TONES if kind == 'alias' else WIDE_TONES if rate == 96000 else TONES
    time = np.arange(args.seconds * rate, dtype=np.float64) / rate
    mono = (0.05 * sum(np.sin(2 * np.pi * frequency * time) for frequency in tones)).astype('<f4')
    data = np.column_stack((mono, mono)).astype('<f4').tobytes()
    with args.output.open('xb') as file:
        file.write(data)
    write_json(metadata, {'inputKind': 'alias-probe' if kind == 'alias' else 'multitone', 'sampleRateHz': rate, 'channels': 2,
                         'tonesHz': tones, 'peakPerTone': .05, 'seconds': args.seconds,
                         'inputSha256': hashlib.sha256(data).hexdigest()})


def psd(samples, rate=RATE):
    frequency, power = signal.welch(samples, fs=rate, axis=0, nperseg=rate, noverlap=rate // 2)
    return frequency, np.mean(power, axis=1)


def analyze(args):
    run = args.run.resolve(strict=True)
    stats = json.loads((run / 'stats.json').read_text(encoding='utf-8'))
    input_rate = stats.get('inputSampleRateHz', stats['sampleRateHz'])
    output_rate = stats.get('outputSampleRateHz', stats['sampleRateHz'])
    if input_rate not in (48000, 96000) or output_rate != RATE or stats['channels'] != 2:
        raise ValueError('only stereo 48k/96k input -> 48k output measurements are supported')
    before = np.fromfile(run / 'raw-input.f32', dtype='<f4').reshape(-1, 2)
    after = np.fromfile(run / 'after.f32', dtype='<f4').reshape(-1, 2)
    if (len(before) != stats['inputFrames'] or len(after) != stats['outputFrames']
            or not np.isfinite(before).all() or not np.isfinite(after).all()):
        raise ValueError('raw samples do not match finite model output')
    ppm = stats['ppm']
    if not np.isfinite(ppm) or abs(ppm) > 1000:
        raise ValueError('invalid modeled clock difference')
    a = before[input_rate:-input_rate]
    b = after[output_rate + int(stats['prefillFrames']):-output_rate]
    if len(a) < input_rate or len(b) < output_rate:
        raise ValueError('measurement is too short after trimming')
    rms_a, rms_b = [float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) for x in (a, b)]
    if min(rms_a, rms_b) <= 0:
        raise ValueError('nonzero audio is required for a level-matched comparison')
    frequency_a, pa = psd(a, input_rate)
    frequency_b, pb = psd(b, output_rate)
    gains, aliases = [], []
    alias_probe = False
    if args.probe:
        meta = json.loads(args.probe.read_text(encoding='utf-8'))
        tones = meta.get('tonesHz')
        alias_probe = meta.get('inputKind') == 'alias-probe'
        expected_tones = ALIAS_TONES if alias_probe else WIDE_TONES if input_rate == 96000 else TONES
        if (meta['inputSha256'] != sha(run / 'raw-input.f32') or tuple(tones or ()) != expected_tones
                or meta.get('sampleRateHz') != input_rate or meta.get('channels') != 2
                or alias_probe and input_rate != 96000):
            raise ValueError('probe metadata differs from the measured raw input')
        for tone in tones:
            # 制御の周波数変動を帯域内に含め、固定周波数からのズレを歪率と呼ばない。
            center = tone * (1 + ppm * 1e-6)
            if alias_probe:
                center = abs((center + output_rate / 2) % output_rate - output_rate / 2)
            power_a = float(pa[np.abs(frequency_a - tone) <= 80].sum())
            power_b = float(pb[np.abs(frequency_b - center) <= 80].sum())
            if min(power_a, power_b) <= 0:
                raise ValueError('missing probe tone power')
            (aliases if alias_probe else gains).append({'toneHz': tone, 'bandHalfWidthHz': 80,
                          'afterBandCenterHz': center, 'gainDb': float(10 * np.log10(power_b / power_a))})
    gain = rms_a / rms_b
    peak = max(float(np.max(np.abs(before))), float(np.max(np.abs(after))) * gain)
    common_gain = min(1.0, .99 / peak)
    args.output.mkdir(exist_ok=False)
    # 超音波の除去試験は、微小な残留音を増幅する試聴資料を作らない。
    if not alias_probe:
        sf.write(args.output / 'before-listen.wav', before * common_gain, input_rate, subtype='FLOAT')
        sf.write(args.output / 'after-listen.wav', after * (gain * common_gain), output_rate, subtype='FLOAT')
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), constrained_layout=True)
    for name, samples, frequency, power, rate in [('before', a, frequency_a, pa, input_rate), ('after', b, frequency_b, pb, output_rate)]:
        axes[0].semilogx(frequency[1:], 10 * np.log10(np.maximum(power[1:], 1e-20)), label=name)
        start = len(samples) // 2
        wave = samples[start:start + 960, 0]
        axes[1].plot(np.arange(len(wave)) / rate * 1000, wave, alpha=.65, label=name)
    axes[0].set(xlim=(20, input_rate / 2 if alias_probe else 22000), ylim=(-180, 0), xlabel='frequency (Hz)', ylabel='PSD (dB/Hz)',
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
        'sourceFilesSha256': {name: sha(ROOT / name) for name in ['src/pcm.hpp', 'src/sinc48.hpp', 'tests/pcm_tests.cpp', 'tools/pcm_quality.py'] if (ROOT / name).is_file()},
        'inputSampleRateHz': input_rate, 'outputSampleRateHz': output_rate,
        'ppm': ppm, 'beforeRms': rms_a, 'afterRms': rms_b,
        'rmsGainDb': float(20 * np.log10(rms_b / rms_a)), 'toneBandGains': gains,
        'aliasBandGains': aliases,
        'measuredChecks': {'passbandWithin005Db': all(abs(x['gainDb']) <= .05 for x in gains) if gains else None,
                           'aliasBelowMinus80Db': all(x['gainDb'] <= -80 for x in aliases) if aliases else None},
        'levelMatch': {'afterGainBeforeCommonAttenuation': gain if not alias_probe else None,
                       'commonGain': common_gain if not alias_probe else None,
                       'listeningFilesWritten': not alias_probe},
        'transportModelHealthy': stats['blocks'] > 0 and all(stats[k] == 0 for k in
                ['underruns', 'overruns', 'invalid', 'stale', 'unavailableFramesAfterStart']),
        'prefillFrames': stats['prefillFrames'], 'startupSourceSkippedFrames': stats['startupSourceSkippedFrames'],
        'modelStats': stats, 'listeningAccepted': None,
    }
    write_json(args.output / 'analysis.json', result)
    print(json.dumps({'output': str(args.output), 'transportModelHealthy': result['transportModelHealthy'],
                      'toneBandGains': gains, 'aliasBandGains': aliases, 'rmsGainDb': result['rmsGainDb']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    make = commands.add_parser('probe')
    make.add_argument('--output', type=Path, required=True)
    make.add_argument('--seconds', type=int, default=70)
    make.add_argument('--sample-rate', type=int, choices=[48000, 96000], default=48000)
    make.add_argument('--kind', choices=['passband', 'alias'], default='passband')
    make.set_defaults(run_command=probe)
    inspect = commands.add_parser('analyze')
    inspect.add_argument('--run', type=Path, required=True)
    inspect.add_argument('--output', type=Path, required=True)
    inspect.add_argument('--executable', type=Path, required=True)
    inspect.add_argument('--probe', type=Path, help='JSON metadata for the generated four-tone input')
    inspect.set_defaults(run_command=analyze)
    args = parser.parse_args()
    args.run_command(args)
