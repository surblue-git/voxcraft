"""会場ノイズ減算のテスト（合成音で挙動を固定する）。"""
import numpy as np

from denoise import NoiseSuppressor

SR = 16000
rng = np.random.default_rng(20260819)


def _speech(sec: float, level: float = 0.05) -> np.ndarray:
    """母音っぽい倍音の並び。声の代わりに使う。"""
    t = np.arange(int(SR * sec)) / SR
    wave = sum(np.sin(2 * np.pi * f * t) / k for k, f in enumerate((180, 360, 720, 1440), 1))
    return (wave / np.abs(wave).max() * level).astype(np.float32)


def _noise(sec: float, level: float) -> np.ndarray:
    return (rng.normal(0.0, level, int(SR * sec))).astype(np.float32)


def _feed_stream(sup: NoiseSuppressor, audio: np.ndarray, block: int = SR // 10) -> None:
    """プラグインと同じ100msごとの送出を再現する。"""
    for i in range(0, audio.size, block):
        sup.feed(audio[i:i + block])


def _snr(x: np.ndarray) -> float:
    frame = 512
    f = x[:(x.size // frame) * frame].reshape(-1, frame).astype(np.float64)
    rms = np.sqrt((f * f).mean(axis=1))
    rms = rms[rms > 0]
    db = lambda v: 20 * np.log10(max(float(v), 1e-12))
    return db(np.percentile(rms, 90)) - db(np.percentile(rms, 10))


def _noisy_session() -> np.ndarray:
    """発話と無音が交互に来る、ノイズ床の高い録音。"""
    parts = []
    for _ in range(6):
        parts.append(_speech(2.0) + _noise(2.0, 0.004))
        parts.append(_noise(2.0, 0.004))
    return np.concatenate(parts)


def test_quiet_room_is_left_alone():
    # ノイズ床が低ければ何もしない（良い録音の挙動を変えない）。
    parts = []
    for _ in range(6):
        parts.append(_speech(2.0) + _noise(2.0, 0.00003))
        parts.append(_noise(2.0, 0.00003))
    audio = np.concatenate(parts)
    sup = NoiseSuppressor(SR)
    _feed_stream(sup, audio)
    assert sup.strength() == 0.0
    chunk = audio[:SR * 8]
    out, alpha = sup.process(chunk)
    assert alpha == 0.0
    assert out is chunk  # 元の配列をそのまま返す ＝ 一切触っていない


def test_noisy_room_gets_subtraction():
    audio = _noisy_session()
    sup = NoiseSuppressor(SR)
    _feed_stream(sup, audio)
    snr = sup.snr_db()
    assert snr is not None and snr < 32.0
    assert sup.strength() > 0.0
    out, alpha = sup.process(audio[:SR * 8])
    assert alpha > 0.0
    assert _snr(out) > _snr(audio[:SR * 8])


def test_length_is_preserved():
    audio = _noisy_session()
    sup = NoiseSuppressor(SR)
    _feed_stream(sup, audio)
    for sec in (0.6, 3.0, 8.0, 11.7):
        chunk = audio[:int(SR * sec)]
        out, _ = sup.process(chunk)
        assert out.size == chunk.size, sec


def test_speech_survives_the_subtraction():
    # 引きすぎて声まで消していないこと。発話区間のレベルは大きく落ちない。
    audio = _noisy_session()
    sup = NoiseSuppressor(SR)
    _feed_stream(sup, audio)
    out, _ = sup.process(audio[:SR * 2])
    before = float(np.sqrt(np.mean(audio[:SR * 2].astype(np.float64) ** 2)))
    after = float(np.sqrt(np.mean(out.astype(np.float64) ** 2)))
    assert after > before * 0.5


def test_nothing_happens_before_enough_audio():
    sup = NoiseSuppressor(SR)
    _feed_stream(sup, _noisy_session()[:SR * 2])   # 2秒＝20ブロックだけ
    assert not sup.ready
    assert sup.snr_db() is None
    assert sup.strength() == 0.0


def test_block_boundaries_do_not_lose_samples():
    # 100msはフレーム長(512)の整数倍ではない。持ち越しが効いていること。
    sup = NoiseSuppressor(SR)
    audio = _noisy_session()
    _feed_stream(sup, audio, block=1600)
    a = sup.noise_profile()
    sup2 = NoiseSuppressor(SR)
    _feed_stream(sup2, audio, block=4096)
    b = sup2.noise_profile()
    # 送出の粒度が変わってもノイズ推定はほぼ同じになる。
    assert np.allclose(a, b, rtol=0.35, atol=1e-4)


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
