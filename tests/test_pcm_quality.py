"""オフライン測定の音量比・hash照合を既知のデジタル入力で確認する。"""
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import pcm_quality as quality


class QualityAnalysisTests(unittest.TestCase):
    def test_known_gain_is_measured_without_claiming_listening_acceptance(self):
        build = (ROOT / 'build').resolve(strict=True)
        folder = Path(tempfile.mkdtemp(prefix='quality-analysis-test-', dir=build)).resolve()
        self.assertTrue(folder.is_relative_to(build) and folder != build)
        try:
            probe = folder / 'probe.f32'
            with self.assertRaises(ValueError):
                quality.probe(SimpleNamespace(output=probe, seconds=3))
            self.assertFalse(probe.exists())
            quality.probe(SimpleNamespace(output=probe, seconds=4))
            before = np.fromfile(probe, dtype='<f4')
            run = folder / 'fixture'
            run.mkdir()
            shutil.copy2(probe, run / 'raw-input.f32')
            (before * .5).astype('<f4').tofile(run / 'after.f32')
            stats = {'model': 'synthetic known-gain fixture, not Reader output',
                     'sampleRateHz': 48000, 'channels': 2, 'ppm': 0,
                     'inputFrames': len(before) // 2, 'outputFrames': len(before) // 2,
                     'prefillFrames': 0, 'startupSourceSkippedFrames': 0, 'blocks': 100,
                     'underruns': 0, 'overruns': 0, 'invalid': 0, 'stale': 0,
                     'unavailableFramesAfterStart': 0}
            (run / 'stats.json').write_text(json.dumps(stats), encoding='utf-8')
            args = SimpleNamespace(run=run, output=folder / 'analysis', executable=Path(__file__),
                                   probe=probe.with_suffix('.f32.json'))
            quality.analyze(args)
            result = json.loads((args.output / 'analysis.json').read_text(encoding='utf-8'))
            self.assertAlmostEqual(result['rmsGainDb'], -6.020599913, places=5)
            self.assertAlmostEqual(result['levelMatch']['afterGainBeforeCommonAttenuation'], 2, places=6)
            self.assertTrue(all(abs(tone['gainDb'] + 6.020599913) < .001 for tone in result['toneBandGains']))
            self.assertIsNone(result['listeningAccepted'])
            with self.assertRaises(FileExistsError):
                quality.analyze(args)
            meta = json.loads(args.probe.read_text(encoding='utf-8'))
            meta['inputSha256'] = '0' * 64
            args.probe.write_text(json.dumps(meta), encoding='utf-8')
            args.output = folder / 'invalid'
            with self.assertRaisesRegex(ValueError, 'metadata differs'):
                quality.analyze(args)
            self.assertFalse(args.output.exists())
        finally:
            # この試験で作成した、build配下の固有ディレクトリだけを回収する。
            resolved = folder.resolve(strict=True)
            if resolved.is_relative_to(build) and resolved != build:
                shutil.rmtree(resolved)


if __name__ == '__main__':
    unittest.main()
