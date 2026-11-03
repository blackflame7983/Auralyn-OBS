"""OBS切断時の後始末と、録画元PCM形式の取り違えを実機なしで確認する。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from scipy.io.wavfile import write

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import native_rate_trial as trial


class NativeRateTests(unittest.TestCase):
    def test_failed_stop_still_closes_reference_and_reaps_sender(self):
        obs, reference, child = Mock(), Mock(), Mock()
        obs.request.side_effect = RuntimeError('OBS disconnected')
        child.poll.return_value = None
        child.communicate.side_effect = [subprocess.TimeoutExpired('owned sender', 5), (b'', b'')]
        errors = trial.cleanup_trial(obs, True, reference, child)
        reference.close.assert_called_once()
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.communicate.call_count, 2)
        self.assertEqual(len(errors), 2)

    def test_failed_client_close_still_releases_power_request(self):
        obs = Mock()
        obs.close.side_effect = OSError('owned client failed')
        with patch('native_rate_trial.ctypes.windll.kernel32.SetThreadExecutionState', return_value=1) as power:
            errors = trial.cleanup_session(obs)
        power.assert_called_once_with(0x80000000)
        self.assertEqual(errors, ['obs-close:OSError'])

    def test_pcm_recording_is_measured_and_compressed_sources_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as folder:
            path = Path(folder) / 'known-pcm.wav'
            samples = (np.sin(np.arange(48000) * 440 * 2 * np.pi / 48000) * 1600).astype('<i2')
            write(path, 48000, np.column_stack((samples, -samples)))
            result = trial.recording_metrics(path)
            self.assertEqual(result['codec'], 'pcm_s16le')
            self.assertEqual(result['timestampGaps'], 0)
            self.assertEqual(result['frames'], 48000)
            self.assertEqual(result['dominantHz'], 440)
            self.assertTrue(result['finite'])
            for codec, time_base in [('flac', '1/48000'), ('aac', '1/48000'), ('pcm_s16le', '1/1000')]:
                media = {'streams': [{'codec_type': 'audio', 'codec_name': codec, 'sample_rate': '48000',
                                      'channels': 2, 'time_base': time_base}]}
                with patch('native_rate_trial.subprocess.run', return_value=Mock(stdout=json.dumps(media).encode())):
                    with self.assertRaisesRegex(ValueError, 'Expected PCM16'):
                        trial.recording_metrics(path)
