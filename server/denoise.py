"""会場ノイズの減算（文字起こし専用）。

なぜ要るか
----------
会場で録ると、音量ではなく **SNR** が落ちる。実測 2026-08-19:

| 録音 | 話声 | ノイズ床 | SNR |
|---|---|---|---|
| 20260818-110141 発表会（プレゼン部） | -36.1dB | -60.5dB | **24.4dB** |
| 20260818-110141 発表会（質疑部） | -30.4dB | -58.5dB | 28.1dB |
| 20260807-151651 インタビュー | -31.1dB | -70.2dB | 39.0dB |
| 20260728-110449 取材 | -34.3dB | -73.7dB | 39.4dB |

ピークは -8.2dB あって音量不足ではない。100-400Hz の静かなフレームが -25dB
（良い録音は -53dB）＝ **定常な広帯域ノイズ**が乗っている。

過去に空振りしたノイズゲート・ハイパスと違い、ここでやるのは
**帯域ごとにノイズ床を引く**（スペクトル減算）。ゲートは「小さい音を消す」だけで
ノイズに埋もれた声には効かないが、減算は声とノイズが重なった帯域にも効く。

効果（実測・turbo と large-v3 の一致率で評価。音が悪いほど2モデルは別々に推測する）
--------------------------------------------------------------------------
発表会プレゼン部（SNR 24.4dB / 5分・41チャンク）:

| 減算の強さ | turbo | large-v3 | 2モデル一致率 |
|---|---|---|---|
| なし | 1426字 | 1119字 | 59.6% |
| alpha 1.0 | 1534字 | 1389字 | 66.3% |
| **alpha 1.5** | 1524字 | 1468字 | **68.7%** |
| alpha 2.5 | 1511字 | 1414字 | 64.6%（実発話を無音判定で破棄し始める） |

質疑部（SNR 28.1dB）では **alpha 1.5 は行き過ぎ**で、0.75 が最良:

| 減算の強さ | 2モデル一致率 |
|---|---|
| なし | 83.3% |
| **alpha 0.75** | **84.6%** |
| alpha 1.5 | 81.9% |

だから強さは固定せず、**推定SNRから決める**。SNR 32dB 以上（＝良い録音）では
alpha 0 ＝ 何もしない。良い録音の挙動を変えないことを設計条件にしている。

コスト: 8秒チャンクあたり 31ms（認識 1483ms に対し +2%）。

ノイズ推定について
------------------
チャンク単体からノイズを推定してはいけない（8秒のほとんどが発話なので、
声そのものをノイズとして引いてしまう）。ストリームに届いた音を無音も含めて
溜め、**直近60秒の帯域ごとの下位20%**をノイズとみなす。

**窓を長くしてはいけない。** 発表会はプロモ映像を流す。音楽が続く区間には
「部屋の静けさ」の証拠が1つも無いので、窓が長いと**音楽をノイズとして覚え**、
映像が終わって話が再開したあとも、その形を声から引き続ける。実測（プレゼン部・
直前2.5分がプロモ映像）:

| 窓 | 冷えた状態から | 直前がプロモ映像 |
|---|---|---|
| 300秒 | 64.1% | **59.5%（減算なしと同じ＝効果が消える）** |
| **60秒** | **66.0%** | **64.7%** |

60秒なら、映像が終わってから1分ほどで部屋の音に戻る。上限（音声を丸ごと1回で
推定した場合）は 68.7% で、逐次・因果の制約のぶんだけ届かない。
"""
from __future__ import annotations

from collections import deque

import numpy as np

_N = 512
_HOP = 128
_SR_BLOCK = 1600  # プラグインの送出粒度（100ms @16kHz）
_STREAM_BIAS = 0.78  # 逐次推定が一括推定より低く出る比（実測・下の noise_profile 参照）
_WINDOW = np.hanning(_N).astype(np.float32)


def _frames(x: np.ndarray) -> np.ndarray:
    """窓かけ済みのフレーム列。長さが足りなければ空を返す。"""
    if x.size < _N:
        return np.empty((0, _N), dtype=np.float32)
    return np.lib.stride_tricks.sliding_window_view(x, _N)[::_HOP] * _WINDOW


def _istft(spec: np.ndarray, length: int) -> np.ndarray:
    frames = np.fft.irfft(spec, n=_N, axis=1) * _WINDOW
    out = np.zeros((len(frames) - 1) * _HOP + _N, dtype=np.float64)
    norm = np.zeros_like(out)
    square = (_WINDOW * _WINDOW).astype(np.float64)
    for i, f in enumerate(frames):
        out[i * _HOP:i * _HOP + _N] += f
        norm[i * _HOP:i * _HOP + _N] += square
    out /= np.maximum(norm, 1e-8)
    if out.size < length:  # 端数は元の長さに合わせて0で埋める
        out = np.concatenate((out, np.zeros(length - out.size)))
    return out[:length].astype(np.float32)


def _db(x: float) -> float:
    return 20.0 * float(np.log10(max(x, 1e-12)))


class NoiseSuppressor:
    """直近の音からノイズ床を推定し、チャンクから帯域ごとに引く。

    `feed` はストリームに届いた音を**全部**渡すこと（無音区間こそノイズ推定の材料）。
    `process` は認識に渡す直前のチャンクにだけ掛ける。
    """

    def __init__(
        self,
        sample_rate: int,
        *,
        window_sec: float = 60.0,   # 長いほど推定は安定するが、音楽を覚えてしまう（下記）
        max_alpha: float = 1.5,
        snr_full: float = 24.0,
        snr_none: float = 32.0,
        spectral_floor: float = 0.06,
        min_blocks: int = 50,
    ) -> None:
        self.sample_rate = sample_rate
        self.max_alpha = max_alpha
        self.snr_full = snr_full
        self.snr_none = snr_none
        self.spectral_floor = spectral_floor
        self.min_blocks = min_blocks
        # プラグインは100msごとに送ってくる。60秒ぶんで600ブロック。
        self._mags: deque[np.ndarray] = deque(maxlen=max(1, int(window_sec * 10)))
        # レベルはブロック(100ms)ではなくフレーム(32ms)で持つ。100msで均すと
        # 息継ぎの谷が埋まってSNRを低く見積もり、良い録音にまで減算が掛かる。
        self._levels: deque[float] = deque(
            maxlen=max(1, int(window_sec * sample_rate / _HOP))
        )
        self._carry = np.zeros(0, dtype=np.float32)

    # --- 推定 -------------------------------------------------------------

    def feed(self, audio: np.ndarray) -> None:
        x = np.asarray(audio, dtype=np.float32)
        if x.size == 0:
            return
        # フレーム長に満たない端数は次のブロックへ持ち越す（境界を落とさない）。
        buf = np.concatenate((self._carry, x)) if self._carry.size else x
        frames = _frames(buf)
        if frames.size:
            used = (len(frames) - 1) * _HOP + _N
            self._carry = buf[used - _N + _HOP:].copy()
            spec = np.abs(np.fft.rfft(frames, axis=1))
            # このブロックの帯域別レベル。下位25%を採るのは、発話が混ざった
            # ブロックでも「息継ぎの瞬間」を拾えるようにするため。
            self._mags.append(np.percentile(spec, 25, axis=0))
            self._levels.extend(
                np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
            )
        else:
            # 1フレームぶんも溜まっていない。まるごと次へ持ち越す。
            self._carry = buf.copy()

    @property
    def ready(self) -> bool:
        return len(self._mags) >= self.min_blocks

    def snr_db(self) -> float | None:
        """話声とノイズ床の差。材料が足りなければ None。"""
        if not self.ready:
            return None
        levels = np.array([v for v in self._levels if v > 0.0])
        if levels.size < self.min_blocks:
            return None

        return _db(float(np.percentile(levels, 90))) - _db(float(np.percentile(levels, 10)))

    def strength(self) -> float:
        """減算の強さ。良い録音（SNRが高い）では 0 ＝ 何もしない。"""
        snr = self.snr_db()
        if snr is None:
            return 0.0
        span = max(1e-6, self.snr_none - self.snr_full)
        return float(np.clip((self.snr_none - snr) / span, 0.0, 1.0) * self.max_alpha)

    def noise_profile(self) -> np.ndarray:
        """帯域ごとのノイズ振幅。直近の窓の下位20%を採る。

        ブロック内で下位25% → ブロック間で下位20%、と2段で採るので、
        音声を丸ごと1回で見た下位20%（alpha を決めた試験の推定）より**低く出る**。
        録音4本（発表・質疑・良い録音2本）で比は 0.73〜0.79 と安定していたので、
        中央の 0.78 で割って試験と同じ尺度に戻す。ここを揃えないと、同じ alpha が
        実際には弱い減算になる（実測で一致率 68.7% → 64.1% に目減りした）。
        """
        return np.percentile(np.stack(self._mags), 20, axis=0) / _STREAM_BIAS

    # --- 適用 -------------------------------------------------------------

    def process(self, audio: np.ndarray) -> tuple[np.ndarray, float]:
        """減算した音と、実際に使った強さを返す。強さ0なら元の配列をそのまま返す。"""
        alpha = self.strength()
        x = np.asarray(audio, dtype=np.float32)
        if alpha <= 0.0 or x.size < _N:
            return audio, 0.0
        # 末尾がフレームに満たないと、その分（最大24ms）が無音になる。
        # 認識に渡す音の末尾を削らないよう、0で埋めてから刻む。
        pad = (-(x.size - _N)) % _HOP
        padded = np.concatenate((x, np.zeros(pad, dtype=np.float32))) if pad else x
        frames = _frames(padded)
        if not frames.size:
            return audio, 0.0
        spec = np.fft.rfft(frames, axis=1)
        mag = np.abs(spec)
        # 引きすぎると子音が消えて「無音」と判定されるので、元の6%を床にする
        # （実測 alpha 2.5 で large-v3 が実発話を破棄し始めた）。
        clean = np.maximum(mag - alpha * self.noise_profile(), self.spectral_floor * mag)
        return _istft(clean * np.exp(1j * np.angle(spec)), x.size), alpha
