"""文字起こしセッションの音声保持（復旧用）。

認識は必ず何かを取りこぼす。フィルタで捨てた分、VADが無音と誤判定した分、
そもそも誤変換された分は、テキストだけ見ても元に戻せない。
そこで文字起こしモードのときだけ**受信した生音声をすべてWAVに残し**、
テキストの各チャンクに「ストリーム上の何秒目か」を持たせる。
後から範囲を指定して同じ音声を精度優先で再認識すれば、脱落も誤変換も復旧できる。

口述（自分の声での音声入力）では一切使わない ＝ 録音は残らない。
"""
from __future__ import annotations

import re
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

# セッションIDは日時から作る固定書式。外部から受け取った値を
# そのままパスに使わないための検証にも同じ書式を使う。
_SESSION_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")

RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"


def _float32_to_pcm16(audio: np.ndarray) -> bytes:
    clipped = np.clip(audio, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


class SessionAudio:
    """セッション中の音声を追記していく WAV ライタ。"""

    def __init__(self, sample_rate: int, session_id: str | None = None):
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        self.sample_rate = sample_rate
        self.session_id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = RECORDINGS_DIR / f"{self.session_id}.wav"
        self._wav = wave.open(str(self.path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(sample_rate)
        self._closed = False

    def append(self, audio: np.ndarray) -> None:
        if self._closed:
            return
        self._wav.writeframes(_float32_to_pcm16(audio))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._wav.close()
        except Exception:
            pass


def resolve_session_path(session_id: str) -> Path:
    """セッションIDを検証してWAVのパスを返す（パス操作の混入を防ぐ）。"""
    if not _SESSION_RE.match(session_id or ""):
        raise ValueError("セッションIDの書式が不正です")
    path = RECORDINGS_DIR / f"{session_id}.wav"
    if not path.is_file():
        raise FileNotFoundError("この録音は残っていません")
    return path


def load_slice(session_id: str, start_sec: float, end_sec: float) -> np.ndarray:
    """保存済みWAVから指定区間を float32 で切り出す。"""
    path = resolve_session_path(session_id)
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        total = wav.getnframes()
        start = max(0, int(start_sec * rate))
        end = min(total, int(end_sec * rate))
        if end <= start:
            return np.zeros(0, dtype=np.float32)
        wav.setpos(start)
        raw = wav.readframes(end - start)
    ints = np.frombuffer(raw, dtype="<i2")
    return ints.astype(np.float32) / 32768.0


def session_duration_sec(session_id: str) -> float:
    path = resolve_session_path(session_id)
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate())
