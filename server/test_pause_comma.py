"""息継ぎ読点まわりの単体テスト。

    python -m pytest test_pause_comma.py    # pytest があれば
    python test_pause_comma.py              # なくても直接実行できる

- VadChunker が「前の発話終わり→次の発話始まり」の無音長（pause）を測れること
- 文頭接続詞の直後に「、」が付くこと（sudachipy 導入時のみ）
"""
from __future__ import annotations

import numpy as np

from vad import VadChunker, _EnergyDetector

_SR = 16000


def _speech(sec: float) -> np.ndarray:
    # エネルギーVADが確実に「声」と判定する白色ノイズ。
    rng = np.random.RandomState(0)
    return (rng.randn(int(sec * _SR)) * 0.1).astype(np.float32)


def _silence(sec: float) -> np.ndarray:
    return np.zeros(int(sec * _SR), dtype=np.float32)


def _run_chunker(audio: np.ndarray) -> list:
    ch = VadChunker(
        sample_rate=_SR, silence_sec=0.4, max_chunk_sec=10.0,
        min_speech_sec=0.2, vad_threshold=0.5, speech_pad_sec=0.1,
    )
    # silero の有無で挙動が変わらないよう、決定的なエネルギーVADに固定する。
    ch._detector = _EnergyDetector(_SR, 0.5)
    chunks = []
    for i in range(0, len(audio), 1600):
        chunks.extend(ch.push(audio[i:i + 1600]))
    tail = ch.flush()
    if tail is not None:
        chunks.append(tail)
    return chunks


def test_pause_between_chunks():
    # 発話1秒 → 無音1秒 → 発話1秒 → 無音1秒。
    audio = np.concatenate([_speech(1.0), _silence(1.0), _speech(1.0), _silence(1.0)])
    chunks = _run_chunker(audio)
    assert len(chunks) == 2, [c.reason for c in chunks]
    # 最初のチャンクは前がないので計測不能。
    assert chunks[0].pause is None
    # 2つ目は約1秒の息継ぎ（フレーム粒度32msの誤差を許容）。
    assert chunks[1].pause is not None
    assert abs(chunks[1].pause - 1.0) < 0.15, chunks[1].pause


def test_pause_long_gap():
    audio = np.concatenate([_speech(1.0), _silence(3.0), _speech(1.0), _silence(1.0)])
    chunks = _run_chunker(audio)
    assert len(chunks) == 2
    assert chunks[1].pause is not None
    assert abs(chunks[1].pause - 3.0) < 0.15, chunks[1].pause


def test_conjunction_comma():
    from punctuate import add_punctuation, available
    if not available():  # sudachipy 未導入環境では対象外
        return
    assert add_punctuation("しかしそれは違います") == "しかし、それは違います。"
    assert add_punctuation("また この方法には利点があります".replace(" ", "")) == \
        "また、この方法には利点があります。"
    assert add_punctuation("つまり読点が大事です") == "つまり、読点が大事です。"
    assert add_punctuation("ただし例外があります") == "ただし、例外があります。"
    # 文中の接続詞には打たない。
    assert "、しかし" not in add_punctuation("これはしかし問題だ")
    # 既に読点があれば二重にしない。
    assert add_punctuation("しかし、それは違います") == "しかし、それは違います。"


if __name__ == "__main__":
    test_pause_between_chunks()
    test_pause_long_gap()
    test_conjunction_comma()
    print("all tests passed")
