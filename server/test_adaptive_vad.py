"""エネルギーVADの適応床のテスト。

    python -m pytest test_adaptive_vad.py
    python test_adaptive_vad.py

守りたいのは2つだけ。

1. **従来より厳しくならない。** 適応床は固定床を上限に持つので、今まで発話と
   判定できていたフレームは必ず判定できる。これが崩れると、既存の録音・
   運用で拾えていた音が黙って落ちる。
2. **口述には掛からない。** 床が下がる方向に動くので、掛けると吐息を拾って
   「はい」等の幻覚が増える。口述の挙動は変えない方針。
"""
import numpy as np

from config import config
from main import _build_chunker
from vad import _FRAME, _NOISE_MIN_FLOOR, _NOISE_RATIO, _EnergyDetector


def _frame(rms: float) -> np.ndarray:
    """指定のRMSを持つフレーム（定数信号なので |x| = RMS）。"""
    return np.full(_FRAME, rms, dtype=np.float32)


def _legacy_floor(threshold: float = 0.5) -> float:
    return 0.006 + 0.02 * threshold


def test_adaptive_floor_is_never_stricter_than_the_fixed_one():
    # 暗騒音がいくら高くても、固定床を超えるしきい値にはならない。
    quiet = _EnergyDetector(16000, 0.5, adaptive=True)
    loud = _frame(0.2)  # 固定床の10倍以上をしばらく流して騒音推定を持ち上げる
    for _ in range(2000):
        quiet.is_speech(loud)
    # 固定床ちょうどのフレームは、従来どおり発話と判定される。
    assert quiet.is_speech(_frame(_legacy_floor()))


def test_quiet_speech_below_the_fixed_floor_is_detected():
    # 2026-08-06 の会見録音の再現: 音声 -42.9 dBFS、固定床 -35.9 dBFS。
    speech = 10 ** (-42.9 / 20)   # 0.00716
    noise = 10 ** (-60.0 / 20)    # 0.001
    assert speech < _legacy_floor()  # 前提: 固定床では拾えない

    fixed = _EnergyDetector(16000, 0.5, adaptive=False)
    assert not fixed.is_speech(_frame(speech))

    adaptive = _EnergyDetector(16000, 0.5, adaptive=True)
    for _ in range(200):  # 暗騒音を推定させる（200フレーム ＝ 6.4秒）
        adaptive.is_speech(_frame(noise))
    assert adaptive.is_speech(_frame(speech))


def test_background_noise_itself_is_not_speech():
    # 床が下がっても、暗騒音そのものは発話にしない（比率ぶんの余裕がある）。
    noise = 0.002
    det = _EnergyDetector(16000, 0.5, adaptive=True)
    for _ in range(200):
        det.is_speech(_frame(noise))
    assert not det.is_speech(_frame(noise))
    assert det.is_speech(_frame(noise * _NOISE_RATIO * 1.1))


def test_digital_silence_does_not_drop_the_floor_to_zero():
    # PCループバックの停止中など、完全な無音でも床は下限で止まる。
    det = _EnergyDetector(16000, 0.5, adaptive=True)
    for _ in range(500):
        det.is_speech(_frame(0.0))
    assert not det.is_speech(_frame(_NOISE_MIN_FLOOR * 0.5))


def test_reset_keeps_the_noise_estimate():
    # reset はチャンク確定ごとに呼ばれる。ここで推定を捨てると追従が成立しない。
    det = _EnergyDetector(16000, 0.5, adaptive=True)
    for _ in range(200):
        det.is_speech(_frame(0.001))
    det.reset()
    assert det.is_speech(_frame(10 ** (-42.9 / 20)))


def test_dictation_does_not_use_the_adaptive_floor():
    # 口述の挙動は変えない。文字起こしだけが適応床を使う。
    dictation = _build_chunker("dictation", "microphone")._detector
    if isinstance(dictation, _EnergyDetector):
        assert dictation._adaptive is False
    for source in ("microphone", "system", "system-client"):
        transcribe = _build_chunker("transcribe", source)._detector
        if isinstance(transcribe, _EnergyDetector):
            assert transcribe._adaptive is config.adaptive_energy_vad


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
