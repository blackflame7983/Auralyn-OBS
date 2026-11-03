"""試験用パッチの所有・先行記録・部分失敗後の復元を、実機なしで検証する。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from voicemeeter_probe import PatchGuard


class FakeRemote:
    def __init__(self):
        self.values = {f'Patch.insert[{i}]': float(i in (6, 7)) for i in range(22)}
        self.values.update({'Strip[4].Mute': 0, 'Option.sr': 96000,
                            'Bus[0].device.sr': 96000, 'Patch.PostFxInsert': 0})
        self.writes, self.fail, self.folder, self.busy = [], None, None, False
    def sync(self): pass
    def kind(self): return 2
    def get(self, name): return self.values[name]
    def levels(self): return [float(self.busy)] * 22
    def set(self, name, value):
        record = json.loads((self.folder / 'voicemeeter-journal.json').read_text())
        # APIへ書く前に、意図と元の値が永続化されている必要がある。
        if value != record['original'][name]:
            assert record['expectedOwnedValues'][name] == value
        self.values[name] = value
        self.writes.append((name, value))
        if name == self.fail:
            self.fail = None
            raise RuntimeError('accepted then failed')


class PatchGuardTests(unittest.TestCase):
    def make(self, folder):
        remote = FakeRemote()
        remote.folder = Path(folder)
        return remote, PatchGuard(remote, remote.folder)

    @mock.patch('voicemeeter_probe.time.sleep')
    def test_partial_failure_restores_only_owned_values(self, _):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as folder:
            remote, guard = self.make(folder)
            original = dict(remote.values)
            remote.fail = 'Patch.insert[6]'
            with self.assertRaisesRegex(RuntimeError, 'accepted then failed'):
                guard.prepare()
            guard.restore()
            self.assertEqual(remote.values, original)
            self.assertTrue(json.loads((Path(folder) / 'voicemeeter-journal.json').read_text())['selectedSettingsRestored'])

    @mock.patch('voicemeeter_probe.time.sleep')
    def test_external_restore_is_not_overwritten_and_rate_is_not_changed(self, _):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as folder:
            remote, guard = self.make(folder)
            guard.prepare()
            remote.values['Strip[4].Mute'] = 0
            remote.values['Option.sr'] = 48000
            with self.assertRaises(RuntimeError):
                guard.check()
            count = len(remote.writes)
            guard.restore()
            self.assertNotIn(('Strip[4].Mute', 0), remote.writes[count:])
            self.assertEqual(remote.values['Option.sr'], 48000)
            self.assertEqual([remote.values[f'Patch.insert[{i}]'] for i in (6, 7, 14, 15)], [1, 1, 0, 0])

    @mock.patch('voicemeeter_probe.time.sleep')
    def test_external_change_to_target_during_quiet_wait_is_not_owned(self, sleep):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as folder:
            remote, guard = self.make(folder)
            sleep.side_effect = lambda _: remote.values.__setitem__('Patch.insert[6]', 0)
            with self.assertRaisesRegex(RuntimeError, 'changed during preparation'):
                guard.prepare()
            guard.restore()
            self.assertEqual(remote.values['Patch.insert[6]'], 0)
            self.assertEqual(remote.writes, [])

    @mock.patch('voicemeeter_probe.time.sleep')
    def test_external_change_during_journal_write_is_not_restored(self, _):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as folder:
            remote, guard = self.make(folder)
            save = guard.save
            def changed_save(extra=None):
                save(extra)
                if 'Patch.insert[6]' in guard.expected:
                    remote.values['Patch.insert[6]'] = 0
            with mock.patch.object(guard, 'save', side_effect=changed_save):
                with self.assertRaisesRegex(RuntimeError, 'before the owned update'):
                    guard.prepare()
            guard.restore()
            self.assertEqual(remote.values['Patch.insert[6]'], 0)
            self.assertEqual(remote.values['Strip[4].Mute'], 0)

    @mock.patch('voicemeeter_probe.time.sleep')
    def test_external_target_edit_with_failed_intent_save_is_not_owned(self, _):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as folder:
            remote, guard = self.make(folder)
            save = guard.save
            def failed_save(extra=None):
                if 'Patch.insert[6]' in guard.expected:
                    remote.values['Patch.insert[6]'] = 0
                    raise OSError('journal write failed before the API call')
                save(extra)
            with mock.patch.object(guard, 'save', side_effect=failed_save):
                with self.assertRaises(OSError):
                    guard.prepare()
            guard.restore()
            self.assertEqual(remote.values['Patch.insert[6]'], 0)
            self.assertEqual(remote.values['Strip[4].Mute'], 0)

    @mock.patch('voicemeeter_probe.time.sleep')
    def test_busy_route_and_existing_journal_are_rejected(self, _):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build') as folder:
            remote, guard = self.make(folder)
            remote.busy = True
            with self.assertRaisesRegex(RuntimeError, 'active audio'):
                guard.prepare()
            self.assertEqual(remote.writes, [])
            (Path(folder) / 'voicemeeter-journal.json').write_text('{}')
            with self.assertRaises(FileExistsError):
                PatchGuard(remote, Path(folder))
