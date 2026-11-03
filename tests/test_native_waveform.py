"""96→48の照合で整数未満の位置差を扱い、別音声を合格にしない。"""
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from scipy.io.wavfile import write
from scipy.signal import butter, resample_poly, sosfiltfilt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from analyze_native import waveform, waveform_arrived


class WaveformTests(unittest.TestCase):
    def test_fractional_alignment_and_unrelated_signal(self):
        rng = np.random.default_rng(223)
        signal = sosfiltfilt(butter(6, [80, 16000], btype='bandpass', fs=96000, output='sos'),
                            rng.standard_normal(96000 * 6)) * .05
        reference = np.column_stack((signal, signal)).astype('<f4')
        # 独立した既定FIRの変換を使い、参照の1入力sample先から録音する。
        converted = resample_poly(signal[1:], 1, 2)
        delayed = np.r_[np.zeros(6576), converted].astype('<f4')
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as folder:
            path = Path(folder) / 'fractional.wav'
            write(path, 48000, delayed)
            matches = [waveform(reference, path, t, 96000, 48000) for t in (2, 3, 4)]
            self.assertTrue(waveform_arrived(matches), matches)
            for match in matches:
                self.assertGreater(match['normalizedCorrelation'], .995)
                self.assertLess(abs(match['fileOffsetMs'] - (-137 + .5 / 48)), .002)
                self.assertLess(match['integerPositionCorrelation'], .98)
            write(path, 48000, (rng.standard_normal(48000 * 6) * .05).astype('<f4'))
            self.assertFalse(waveform_arrived([waveform(reference, path, t, 96000, 48000) for t in (2, 3, 4)]))
            write(path, 48000, np.zeros(48000 * 6, dtype='<f4'))
            self.assertTrue(waveform(reference, path, 2, 96000, 48000)['silent'])
