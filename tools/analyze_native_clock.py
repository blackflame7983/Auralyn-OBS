"""凍結済み録画を、既知のclock補正範囲内の単一affineで事後照合する。"""
import argparse
import contextlib
import hashlib
import io
import json
import math
from pathlib import Path
import subprocess

import numpy as np
from scipy.optimize import differential_evolution
from scipy.signal import firwin, resample_poly

import analyze_native as base
from native_support import runtime_env

FIXED_WAVEFORM = base.waveform
FIXED_ARRIVED = base.waveform_arrived
MAX_PPM = 2000
METHOD = 'bounded-affine-150ms-six-segments-v1'


def file_sha(path):
    value = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def normalized(a, b):
    a, b = a - a.mean(), b - b.mean()
    return float(np.dot(a, b) / np.sqrt(max(np.dot(a, a) * np.dot(b, b), 1e-30)))


def waveform(reference, recording, seconds, rate, output_rate=None):
    output_rate = output_rate or rate
    fixed = FIXED_WAVEFORM(reference, recording, seconds, rate, output_rate)
    if fixed.get('silent') or fixed.get('unavailable'):
        return fixed
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(seconds), '-i', str(recording),
                          '-t', '0.15', '-vn', '-f', 'f32le', '-ac', '1', '-ar', str(output_rate), 'pipe:1'],
                         capture_output=True, check=True, env=runtime_env()).stdout
    signal = np.frombuffer(raw, dtype='<f4').astype('f8')
    start = max(0, int((seconds - 5) * rate))
    clip = np.array(reference[start:int((seconds + 5) * rate), 0], dtype='f8')
    if rate != output_rate:
        divisor = math.gcd(rate, output_rate)
        up, down = output_rate // divisor, rate // divisor
        taps = firwin(256 * max(up, down) + 1, .93 / max(up, down), window=('kaiser', 10))
        clip = resample_poly(clip, up, down, window=taps)
    index = round((seconds + fixed['fileOffsetMs'] / 1000 - start / rate) * output_rate)
    # 最良整数位置からの探索量も、150ms中の既知最大driftと1sampleに限定する。
    bound = 1 + len(signal) * MAX_PPM * 1e-6 / 2
    margin = math.ceil(2 * bound) + 32
    low, high = max(0, index - margin), min(len(clip), index + len(signal) + margin)
    fine = resample_poly(clip[low:high], 16, 1, window=('kaiser', 10))
    grid = np.arange(len(fine))
    n = np.arange(len(signal))
    center = (len(signal) - 1) / 2
    positions = index - low + n
    def candidate(parameters):
        fraction, milli_ratio = parameters
        return np.interp((positions + fraction + milli_ratio / 1000 * (n - center)) * 16, grid, fine)
    # 時間の局所的な伸縮2変数だけを合わせる。波形の加工・任意のwarpはしない。
    fit = differential_evolution(lambda p: -normalized(candidate(p), signal),
                                 [(-bound, bound), (-MAX_PPM / 1000, MAX_PPM / 1000)],
                                 seed=223, popsize=10, maxiter=100, tol=1e-9, polish=True)
    aligned = candidate(fit.x)
    segments = []
    for part in np.array_split(np.arange(len(signal)), 6):
        target = signal[part]
        segments.append({'normalizedCorrelation': normalized(aligned[part], target),
                         'rms': float(np.sqrt(np.mean(target ** 2)))})
    ppm = float(fit.x[1] * 1000)
    fraction_at_start = float(fit.x[0] - ppm * 1e-6 * center)
    return {**fixed, 'normalizedCorrelation': normalized(aligned, signal),
            'fixedRatioCorrelation': fixed['normalizedCorrelation'],
            'fittedRelativePpm': ppm, 'alignmentCenterOffsetSamples': float(fit.x[0]),
            'alignmentCenterBoundSamples': bound, 'alignmentMethod': METHOD,
            'optimizerConverged': bool(fit.success), 'segments': segments,
            'fileOffsetMs': float(start * 1000 / rate + (index + fraction_at_start) * 1000 / output_rate - seconds * 1000)}


def waveform_arrived(matches):
    # 150ms全体だけでなく、同じ単一fitによる6区間全ての一致を要求する。
    return FIXED_ARRIVED(matches) and all(
        m.get('alignmentMethod') == METHOD and m.get('optimizerConverged') is True
        and abs(m.get('fittedRelativePpm', float('inf'))) <= MAX_PPM
        and len(m.get('segments', [])) == 6
        and all(s['rms'] > 1e-7 and s['normalizedCorrelation'] >= .95 for s in m['segments'])
        for m in matches)


def analyze(run):
    before = run / 'analysis.json'
    retained = run / 'analysis-fraction-only.json'
    if not retained.exists():
        if not before.is_file() or json.loads(before.read_text(encoding='utf-8')).get('postAnalysis'):
            raise ValueError('元の固定比率解析を先に実行してください')
        retained.write_bytes(before.read_bytes())
    old_waveform, old_arrived = base.waveform, base.waveform_arrived
    try:
        base.waveform, base.waveform_arrived = waveform, waveform_arrived
        with contextlib.redirect_stdout(io.StringIO()):
            base.analyze(run)
    finally:
        base.waveform, base.waveform_arrived = old_waveform, old_arrived
    summary = json.loads(before.read_text(encoding='utf-8'))
    result = json.loads((run / 'result.json').read_text(encoding='utf-8'))
    summary['postAnalysis'] = {'method': METHOD, 'comparisonOnly': True, 'maxFitPpm': MAX_PPM,
                               'toolFile': 'tools/analyze_native_clock.py',
                               'toolSha256': file_sha(Path(__file__)),
                               'baseToolSha256': file_sha(Path(base.__file__)),
                               'retainedFirstPass': retained.name if retained.exists() else None,
                               'captureFilesUsedReadOnly': True,
                               'captureCodeManifestUnchanged': summary['checks']['codeManifestUnchanged'],
                               'captureManifestSha256': file_sha(run / 'code-manifest.json'),
                               'captureResultSha256': file_sha(run / 'result.json'),
                               'firstPassAnalysisSha256': file_sha(retained) if retained.exists() else None,
                               'referenceSha256': file_sha(run / 'reference.f32'),
                               'recordingSha256': file_sha(Path(result['recording']['outputPath']))}
    before.write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    analyze(parser.parse_args().run.resolve(strict=True))
