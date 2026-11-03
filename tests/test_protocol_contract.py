"""共通fixtureとPythonのrate/minor解釈が一致し、未知の組合せを拒否する。"""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from protocol_contract import SUPPORTED_RATES, stream_minor, valid_stream
from manage_install import valid_manifest


class ProtocolTests(unittest.TestCase):
    def test_fixture_and_rejections(self):
        fixture = json.loads((ROOT / 'protocol/obs-pcm-v1.json').read_text(encoding='utf-8'))
        self.assertEqual({r['rate'] for r in fixture['rates']}, set(SUPPORTED_RATES))
        for row in fixture['rates']:
            rate, minor = row['rate'], row['streamMinor']
            self.assertEqual(stream_minor(rate), minor)
            self.assertTrue(valid_stream(minor, rate))
            self.assertTrue(valid_stream(1, rate))
            self.assertFalse(valid_stream(2, rate))
            if minor == 1:
                self.assertFalse(valid_stream(0, rate))
        for rate in (0, 123, 384000, 768000):
            self.assertFalse(valid_stream(0, rate))
            self.assertFalse(valid_stream(1, rate))
            with self.assertRaises(ValueError):
                stream_minor(rate)
        self.assertFalse(valid_stream(True, 48000))

    def test_installer_preserves_minor_and_rejects_unknown_versions(self):
        manifest = {'schemaVersion': 1, 'package': 'auralyn-obs', 'platform': 'windows-x64',
                    'obsVersion': '32.2.2', 'protocolMajor': 1, 'sourceKind': 'auralyn_pcm_poc_v1',
                    'version': '0.1.0-poc', 'binarySha256': 'a' * 64, 'sourceArchiveSha256': 'b' * 64}
        for minor in (0, 1):
            self.assertEqual(valid_manifest({**manifest, 'protocolMinor': minor})['protocolMinor'], minor)
        for minor in (-1, 2, True, None):
            with self.assertRaises(ValueError):
                valid_manifest({**manifest, 'protocolMinor': minor})
