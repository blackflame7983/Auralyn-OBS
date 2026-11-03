"""Bananaの未使用AUXで試験信号を流す。周波数・機器・通常音量を変更しない。"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

import numpy as np


class Remote:
    def __init__(self, dll=Path('C:/Program Files (x86)/VB/Voicemeeter/VoicemeeterRemote64.dll')):
        self.api = ctypes.WinDLL(str(dll))
        for name in ['VBVMR_Login', 'VBVMR_Logout', 'VBVMR_IsParametersDirty']:
            getattr(self.api, name).argtypes = []
            getattr(self.api, name).restype = ctypes.c_long
        self.api.VBVMR_GetVoicemeeterType.argtypes = [ctypes.POINTER(ctypes.c_long)]
        self.api.VBVMR_GetVoicemeeterType.restype = ctypes.c_long
        self.api.VBVMR_GetParameterFloat.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_float)]
        self.api.VBVMR_GetParameterFloat.restype = ctypes.c_long
        self.api.VBVMR_SetParameterFloat.argtypes = [ctypes.c_char_p, ctypes.c_float]
        self.api.VBVMR_SetParameterFloat.restype = ctypes.c_long
        self.api.VBVMR_GetLevel.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.POINTER(ctypes.c_float)]
        self.api.VBVMR_GetLevel.restype = ctypes.c_long
        if self.api.VBVMR_Login() < 0:
            raise RuntimeError('Voicemeeter Remote login failed')
        time.sleep(.1)
        self.sync()

    def sync(self):
        if self.api.VBVMR_IsParametersDirty() < 0:
            raise RuntimeError('Voicemeeter is not available')

    def kind(self):
        value = ctypes.c_long()
        if self.api.VBVMR_GetVoicemeeterType(ctypes.byref(value)) != 0:
            raise RuntimeError('Voicemeeter type unavailable')
        return value.value

    def get(self, name):
        value = ctypes.c_float()
        if self.api.VBVMR_GetParameterFloat(name.encode(), ctypes.byref(value)) != 0:
            raise RuntimeError('Voicemeeter parameter unavailable: ' + name)
        return value.value

    def set(self, name, value):
        if self.api.VBVMR_SetParameterFloat(name.encode(), value) != 0:
            raise RuntimeError('Voicemeeter parameter update failed: ' + name)
        for _ in range(50):
            time.sleep(.02)
            self.sync()
            if self.get(name) == value:
                return
        raise RuntimeError('Voicemeeter parameter update timed out: ' + name)

    def levels(self):
        values = []
        for i in range(22):
            value = ctypes.c_float()
            if self.api.VBVMR_GetLevel(0, i, ctypes.byref(value)) != 0:
                raise RuntimeError('Voicemeeter level unavailable')
            values.append(value.value)
        return values

    def close(self):
        self.api.VBVMR_Logout()


class PatchGuard:
    """変更前を先に保存し、他者が変えた値を上書きせず所有値だけ戻す。"""
    def __init__(self, remote, folder):
        self.remote, self.folder = remote, folder
        self.original, self.expected, self.protected = {}, {}, {}
        if (folder / 'voicemeeter-journal.json').exists():
            raise FileExistsError('Keep the previous Voicemeeter journal; use a new trial folder')

    def save(self, extra=None):
        value = {'scope': 'temporary Banana AUX insert test only', 'original': self.original,
                 'expectedOwnedValues': self.expected, 'protectedTrialValues': self.protected, **(extra or {})}
        temporary = self.folder / 'voicemeeter-journal.json.tmp'
        with temporary.open('w', encoding='utf-8') as file:
            json.dump(value, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.folder / 'voicemeeter-journal.json')

    def prepare(self):
        r = self.remote
        r.sync()
        if (r.kind() != 2 or r.get('Option.sr') != 96000
                or r.get('Bus[0].device.sr') != 96000 or r.get('Patch.PostFxInsert') != 0):
            raise RuntimeError('This trial requires Banana 96kHz with pre-FX Insert; no setting was changed')
        self.original = {f'Patch.insert[{i}]': r.get(f'Patch.insert[{i}]') for i in range(22)}
        self.original['Strip[4].Mute'] = r.get('Strip[4].Mute')
        if any(self.original[f'Patch.insert[{i}]'] for i in range(22) if i not in (6, 7)):
            raise RuntimeError('An additional Insert route is already in use; no setting was changed')
        # 既存ASIO処理を引き継がない。入力が静かな間だけ試験用パッチを作る。
        for _ in range(10):
            if max(map(abs, r.levels())) > 1e-5:
                raise RuntimeError('Voicemeeter has active audio; need a quiet test window')
            time.sleep(.1)
        # 静音待ち中の手動変更は試験の所有値にしない。
        r.sync()
        if any(r.get(k) != v for k, v in self.original.items()):
            raise RuntimeError('Voicemeeter changed during preparation; no setting was changed')
        changes = [('Strip[4].Mute', 1), ('Patch.insert[6]', 0), ('Patch.insert[7]', 0),
                   ('Patch.insert[14]', 1), ('Patch.insert[15]', 1)]
        self.protected = {**self.original, **dict(changes)}
        self.save({'sampleRateHz': 96000})
        for name, value in changes:
            r.sync()
            if (r.get('Option.sr') != 96000 or r.get('Bus[0].device.sr') != 96000
                    or r.get('Patch.PostFxInsert') != 0
                    or any(r.get(k) != self.expected.get(k, v) for k, v in self.original.items())):
                raise RuntimeError('Voicemeeter changed before the next owned update')
            if self.original[name] == value:
                continue
            # fsyncの間の編集も書き込み直前に再確認し、不一致なら所有権を撤回する。
            self.expected[name] = value
            try:
                self.save()
                r.sync()
                if r.get(name) != self.original[name]:
                    raise RuntimeError('Voicemeeter changed before the owned update: ' + name)
            except BaseException:
                # APIを呼ぶ前の失敗では、外部の同値編集を自分の変更として戻さない。
                self.expected.pop(name, None)
                raise
            r.set(name, value)
        self.check()

    def check(self):
        self.remote.sync()
        if (self.remote.get('Option.sr') != 96000 or self.remote.get('Bus[0].device.sr') != 96000
                or self.remote.get('Patch.PostFxInsert') != 0):
            raise RuntimeError('Voicemeeter sample rate changed during the trial')
        for name, value in self.protected.items():
            if self.remote.get(name) != value:
                raise RuntimeError('Trial route changed externally: ' + name)

    def restore(self):
        restored, conflicts = [], []
        self.remote.sync()
        for name, value in reversed(list(self.expected.items())):
            current = self.remote.get(name)
            if current == value:
                self.remote.set(name, self.original[name])
                restored.append(name)
            elif current != self.original[name]:
                conflicts.append(name)
        self.save({'restored': restored, 'externalChangesNotOverwritten': conflicts,
                   'selectedSettingsRestored': all(self.remote.get(k) == v for k, v in self.original.items())})
        if conflicts:
            raise RuntimeError('Voicemeeter changed externally; see journal before further operations')


class Probe:
    """WASAPI共有モードの明示AUX出力だけに、周期的な帯域制限noiseを送る。"""
    def __init__(self, folder):
        deps = Path(__file__).resolve().parents[1] / 'build/audio-probe-deps/site-packages'
        if deps.is_dir():
            sys.path.insert(0, str(deps))
        import sounddevice as sd
        self.sd, self.folder = sd, folder
        self.stop, self.ready = threading.Event(), threading.Event()
        self.error, self.underflows, self.frames, self.stream = None, 0, 0, None
        choices = [(i, d) for i, d in enumerate(sd.query_devices()) if
                   sd.query_hostapis(d['hostapi'])['name'] == 'Windows WASAPI'
                   and d['name'].startswith('Voicemeeter AUX Input (') and d['max_output_channels'] >= 2]
        if len(choices) != 1:
            raise RuntimeError('Exactly one explicit Voicemeeter AUX WASAPI output is required')
        self.device, info = choices[0]
        self.rate = int(info['default_samplerate'])
        if self.rate not in (48000, 96000):
            raise RuntimeError('Probe output mix format is not 48k/96k')
        # 20秒周期のランダム位相波。継ぎ目も周期として成立し、単音の曖昧な相関を避ける。
        n = self.rate * 20
        rng = np.random.default_rng(22396)
        frequencies = np.fft.rfftfreq(n, 1 / self.rate)
        spectrum = rng.standard_normal(len(frequencies)) + 1j * rng.standard_normal(len(frequencies))
        spectrum[(frequencies < 80) | (frequencies > 16000)] = 0
        spectrum /= np.sqrt(np.maximum(frequencies, 80))
        signal = np.fft.irfft(spectrum, n=n)
        signal *= .08 / np.max(np.abs(signal))
        self.data = np.column_stack((signal, signal)).astype('<f4')
        raw = self.data.tobytes()
        with (folder / 'probe.f32').open('xb') as file:
            file.write(raw)
        self.metadata = {'kind': 'synthetic periodic bandlimited noise, not microphone/speech',
                         'sampleRateHz': self.rate, 'asioRateHz': 96000, 'channels': 2, 'peak': .08,
                         'sha256': hashlib.sha256(raw).hexdigest(), 'outputName': info['name'],
                         'outputHostApi': 'Windows WASAPI', 'sounddeviceVersion': sd.__version__,
                         'vmStrip': 4, 'asioChannels': [14, 15]}
        with (folder / 'probe.json').open('x', encoding='utf-8') as file:
            json.dump(self.metadata, file, indent=2)
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        try:
            with self.sd.RawOutputStream(device=self.device, samplerate=self.rate, channels=2,
                                         dtype='float32', blocksize=0, latency='high') as stream:
                self.stream = stream
                cursor = 0
                while not self.stop.is_set():
                    size = min(4096, len(self.data) - cursor)
                    if stream.write(self.data[cursor:cursor + size].tobytes()):
                        self.underflows += 1
                    self.frames += size
                    cursor = (cursor + size) % len(self.data)
                    self.ready.set()
        except Exception as error:
            if not self.stop.is_set():
                self.error = str(error)
                self.ready.set()

    def start(self):
        self.thread.start()
        if not self.ready.wait(10) or self.error:
            raise RuntimeError('Probe playback did not start: ' + str(self.error))

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(3)
        if self.thread.is_alive() and self.stream is not None:
            self.stream.abort()
            self.thread.join(3)
        if self.thread.is_alive():
            raise RuntimeError('Probe still active; keep AUX muted and consult journal')
        return {'frames': self.frames, 'underflows': self.underflows, 'error': self.error, **self.metadata}
