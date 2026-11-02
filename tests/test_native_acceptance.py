"""無音・誤ったOBS経路を到達確認の合格に含めない。"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from analyze_native import waveform_arrived
from native_ui_soak import recording_route


class NativeAcceptanceTests(unittest.TestCase):
    def test_three_non_silent_matching_windows_are_required(self):
        good = {'normalizedCorrelation': .99, 'rms': .01}
        self.assertTrue(waveform_arrived([good] * 3))
        self.assertFalse(waveform_arrived([good] * 2))
        for bad in [{'silent': True}, {'unavailable': True}, {'normalizedCorrelation': .9}]:
            self.assertFalse(waveform_arrived([good, bad, good]))

    def test_muted_zero_volume_or_missing_track_rejects_source(self):
        class Obs:
            def __init__(self, muted=False, volume=1, tracks=None):
                self.values = {'GetInputMute': {'inputMuted': muted},
                    'GetInputVolume': {'inputVolumeMul': volume},
                    'GetInputAudioTracks': {'inputAudioTracks': {'1': True} if tracks is None else tracks}}
            def request(self, command, args):
                return self.values[command]
        self.assertEqual(recording_route(Obs())['volume'], 1)
        for obs in [Obs(muted=True), Obs(volume=0), Obs(tracks={'1': False}), Obs(tracks={})]:
            with self.assertRaises(RuntimeError):
                recording_route(obs)


if __name__ == '__main__':
    unittest.main()
