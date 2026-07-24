"""faster-whisper による日本語音声認識ラッパ。

モデルは config で差し替え可能（kotoba-whisper-v2.0-faster / large-v3 / small ...）。
モデルのロードは重いので起動時に一度だけ行い、以後は使い回す。
"""
from __future__ import annotations

import threading

import numpy as np

from config import config


class Transcriber:
    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()  # faster-whisper は同時呼び出し非対応

    def load(self) -> None:
        """モデルをロードする（起動時に呼ぶ）。"""
        from faster_whisper import WhisperModel  # 遅延インポート

        self._model = WhisperModel(
            config.model,
            device=config.device,
            compute_type=config.compute_type,
        )

    @property
    def ready(self) -> bool:
        return self._model is not None

    def transcribe(self, audio: np.ndarray) -> str:
        """float32 16kHz モノラル音声を認識してテキストを返す。"""
        if self._model is None:
            raise RuntimeError("model not loaded")

        with self._lock:
            segments, _info = self._model.transcribe(
                audio,
                language=config.language,
                task="transcribe",
                initial_prompt=config.initial_prompt or None,
                vad_filter=False,       # 区切りは自前 VAD で済ませている
                condition_on_previous_text=False,  # 幻覚の連鎖を防ぐ
                beam_size=5,
            )
            text = "".join(seg.text for seg in segments)
        return text.strip()


transcriber = Transcriber()
