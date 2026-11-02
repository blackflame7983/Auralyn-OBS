"""隔離OBSの別音源や古い接続名を、clock試験の録画へ混ぜない。"""
import unittest
from unittest.mock import Mock, patch
from native_clock_trial import configure_obs_input, wait_recording_started, NAME


class ObsFixture:
    def __init__(self, kind=None):
        self.kind = kind
        self.calls = []
    def request(self, command, args=None):
        self.calls.append((command, args))
        if command == 'GetInputList':
            return {'inputs': [] if self.kind is None else [{'inputName': NAME, 'inputKind': self.kind}]}
        return {'GetInputMute': {'inputMuted': False}, 'GetInputVolume': {'inputVolumeMul': 1},
                'GetInputAudioTracks': {'inputAudioTracks': {'1': True}}}.get(command, {})


class ObsInputTests(unittest.TestCase):
    def test_recording_waits_for_asynchronous_start(self):
        obs = Mock()
        obs.request.side_effect = [{'outputActive': False}, {'outputActive': False}, {'outputActive': True}]
        with patch('native_clock_trial.time.sleep') as sleep:
            wait_recording_started(obs)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(obs.request.call_count, 3)

    def test_recording_timeout_is_a_failure(self):
        obs = Mock()
        obs.request.return_value = {'outputActive': False}
        with self.assertRaises(RuntimeError):
            wait_recording_started(obs, timeout=0)

    def test_existing_pcm_source_replaces_stale_endpoint(self):
        obs = ObsFixture('auralyn_pcm_poc_v1')
        configure_obs_input(obs, 'new-owned-endpoint')
        updates = [args for command, args in obs.calls if command == 'SetInputSettings']
        self.assertEqual(updates, [{'inputName': NAME, 'inputSettings': {'endpoint': 'new-owned-endpoint'}, 'overlay': True}])
        self.assertFalse(any(command == 'CreateInput' for command, _ in obs.calls))

    def test_foreign_source_kind_is_not_changed(self):
        obs = ObsFixture('wasapi_input_capture')
        with self.assertRaises(RuntimeError):
            configure_obs_input(obs, 'owned-endpoint')
        self.assertEqual([command for command, _ in obs.calls], ['GetInputList'])

    def test_new_source_uses_explicit_endpoint(self):
        obs = ObsFixture()
        configure_obs_input(obs, 'owned-endpoint')
        created = next(args for command, args in obs.calls if command == 'CreateInput')
        self.assertEqual(created['inputSettings'], {'endpoint': 'owned-endpoint'})
        self.assertEqual(created['inputKind'], 'auralyn_pcm_poc_v1')


if __name__ == '__main__':
    unittest.main()
