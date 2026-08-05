"""PC音声入力のフォーマット変換とデバイス選択のテスト。

    python -m pytest test_system_audio.py
    python test_system_audio.py
"""
import numpy as np

from system_audio import (
    SystemAudioError,
    WasapiLoopbackCapture,
    _decode_pcm16_mono,
    _enumerate_devices,
    _StreamingConverter,
)

WASAPI = 2
MME = 0


class _FakePyAudio:
    paWASAPI = WASAPI


class _FakeManager:
    """PyAudioWPatch の列挙APIだけを真似る。

    devices は index 順。isLoopbackDevice / hostApi / maxInputChannels で
    実機と同じ選別が起きるかを見る。
    """

    def __init__(self, devices, default_output_index=0):
        self._devices = devices
        self._default_output_index = default_output_index

    def get_host_api_info_by_type(self, host_api_type):
        if host_api_type != WASAPI:
            raise OSError("no such host api")
        return {"index": WASAPI, "defaultOutputDevice": self._default_output_index}

    def get_device_count(self):
        return len(self._devices)

    def get_device_info_by_index(self, index):
        return self._devices[index]

    def get_loopback_device_info_generator(self):
        return (d for d in self._devices if d.get("isLoopbackDevice"))


def _device(index, name, *, host_api=WASAPI, inputs=2, loopback=False, outputs=0):
    return {
        "index": index,
        "name": name,
        "hostApi": host_api,
        "maxInputChannels": inputs,
        "maxOutputChannels": outputs,
        "isLoopbackDevice": loopback,
        "defaultSampleRate": 48000.0,
    }


def test_stereo_is_downmixed_without_int16_overflow():
    stereo = np.array([
        [32767, 32767],
        [32767, -32768],
        [-16384, -16384],
    ], dtype="<i2")
    mono = _decode_pcm16_mono(stereo.tobytes(), 2)
    assert mono.dtype == np.float32
    assert np.allclose(mono, [32767 / 32768, -0.5 / 32768, -0.5], atol=1e-7)


def test_incomplete_multichannel_frame_is_ignored():
    samples = np.array([1000, 2000, 9999], dtype="<i2")
    mono = _decode_pcm16_mono(samples.tobytes(), 2)
    assert mono.size == 1
    assert np.isclose(mono[0], 1500 / 32768)


def test_streaming_resampler_keeps_long_term_sample_count():
    converter = _StreamingConverter(48000, 16000, 2)
    # 1秒を不揃いなブロックで渡し、チャンク境界で状態がリセットされないことを確認。
    stereo = np.zeros((48000, 2), dtype="<i2").tobytes()
    sizes = [4096, 7780, 12004, 8192, 16384, 47544]
    offset = 0
    parts = []
    for size in sizes:
        block = stereo[offset:offset + size]
        offset += len(block)
        if block:
            parts.append(converter.push(block))
    if offset < len(stereo):
        parts.append(converter.push(stereo[offset:]))
    parts.append(converter.flush())
    output = np.concatenate(parts)
    assert abs(output.size - 16000) <= 1


def _capture(device_name=None):
    return WasapiLoopbackCapture(16000, lambda _a: None, lambda _e: None, device_name)


def test_enumeration_lists_loopback_and_real_inputs_but_not_other_host_apis():
    manager = _FakeManager([
        _device(0, "スピーカー (Realtek)", inputs=0, outputs=2),
        _device(1, "スピーカー (Realtek) [Loopback]", loopback=True),
        _device(2, "ステレオ ミキサー (Realtek)"),
        # 同じ物理デバイスの MME 版。混ぜると一覧が重複するので落とす。
        _device(3, "ステレオ ミキサー (Realtek)", host_api=MME),
    ])
    found = _enumerate_devices(manager, _FakePyAudio)
    assert [(info["index"], kind) for info, kind in found] == [
        (1, "loopback"),
        (2, "input"),
    ]


def test_named_device_is_opened_instead_of_the_default_output():
    manager = _FakeManager([
        _device(0, "スピーカー (Realtek)", inputs=0, outputs=2),
        _device(1, "スピーカー (Realtek) [Loopback]", loopback=True),
        _device(2, "ステレオ ミキサー (Realtek)"),
    ])
    chosen = _capture("ステレオ ミキサー (Realtek)")._find_device(manager, _FakePyAudio)
    assert chosen["index"] == 2


def test_missing_named_device_fails_instead_of_falling_back_to_the_default():
    # 黙って既定へ落ちると、別の音を延々と文字起こしすることになる。
    manager = _FakeManager([
        _device(0, "スピーカー (Realtek)", inputs=0, outputs=2),
        _device(1, "スピーカー (Realtek) [Loopback]", loopback=True),
    ])
    try:
        _capture("消えたミキサー")._find_device(manager, _FakePyAudio)
    except SystemAudioError as exc:
        assert "消えたミキサー" in str(exc)
    else:
        raise AssertionError("見つからないデバイスが例外にならなかった")


def test_unnamed_capture_still_uses_the_default_output_loopback():
    manager = _FakeManager([
        _device(0, "スピーカー (Realtek)", inputs=0, outputs=2),
        _device(1, "スピーカー (Realtek) [Loopback]", loopback=True),
        _device(2, "ステレオ ミキサー (Realtek)"),
    ])
    chosen = _capture()._find_device(manager, _FakePyAudio)
    assert chosen["index"] == 1


if __name__ == "__main__":
    import sys

    mod = sys.modules[__name__]
    failed = 0
    for name in [n for n in dir(mod) if n.startswith("test_")]:
        try:
            getattr(mod, name)()
            print(f"ok   {name}")
        except AssertionError:
            failed += 1
            print(f"FAIL {name}")
    print("失敗あり" if failed else "すべて成功")
    sys.exit(1 if failed else 0)
