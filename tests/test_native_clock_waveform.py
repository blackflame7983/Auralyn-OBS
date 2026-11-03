"""clock差の正例と、単一affineでは説明できない飛び・無音の負例。"""
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from scipy.io.wavfile import write
from scipy.signal import butter, resample_poly, sosfiltfilt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from analyze_native_clock import waveform, waveform_arrived


class ClockWaveformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(96223)
        signal = sosfiltfilt(butter(6, [80, 16000], btype='bandpass', fs=96000, output='sos'),
                            rng.standard_normal(96000 * 6)) * .05
        cls.reference = np.column_stack((signal, signal)).astype('<f4')
        cls.dense = resample_poly(resample_poly(signal, 1, 2), 16, 1, window=('kaiser', 10))

    def signal(self, ppm):
        n = np.arange(48000 * 6)
        positions = ((n - 6576) * (1 + ppm * 1e-6) + .5) * 16
        return np.interp(positions, np.arange(len(self.dense)), self.dense, left=0, right=0).astype('<f4')

    def matches(self, data, folder):
        path = Path(folder) / 'fixture.wav'
        write(path, 48000, data)
        return [waveform(self.reference, path, t, 96000, 48000) for t in (2, 3, 4)]

    def test_known_ratio_and_fractional_delay(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as folder:
            for ppm in (500, -2000, 2000):
                matches = self.matches(self.signal(ppm), folder)
                self.assertTrue(waveform_arrived(matches), (ppm, matches))
                for match in matches:
                    self.assertGreater(match['normalizedCorrelation'], .995)
                    self.assertLess(abs(match['fittedRelativePpm'] - ppm), 3)

    def test_out_of_range_unrelated_and_silence_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as folder:
            rng = np.random.default_rng(44223)
            for data in (self.signal(5000), (rng.standard_normal(48000 * 6) * .05).astype('<f4'),
                         np.zeros(48000 * 6, dtype='<f4')):
                self.assertFalse(waveform_arrived(self.matches(data, folder)))

    def test_64_frame_jumps_and_25ms_silence_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as folder:
            for offset in (.05, .075):
                # 150ms窓の25ms区切り/中心で64出力frameを飛ばす。1sample検出保証ではない。
                data = self.signal(0)
                at = round((2 + offset) * 48000)
                data[at:-64] = data[at + 64:]
                data[-64:] = 0
                self.assertFalse(waveform_arrived(self.matches(data, folder)))
            data = self.signal(0)
            data[round(2.05 * 48000):round(2.075 * 48000)] = 0
            self.assertFalse(waveform_arrived(self.matches(data, folder)))
