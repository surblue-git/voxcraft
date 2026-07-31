"""PC音声入力のフォーマット変換テスト。

    python -m pytest test_system_audio.py
    python test_system_audio.py
"""
import numpy as np

from system_audio import _StreamingConverter, _decode_pcm16_mono


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
