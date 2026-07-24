"""faster-whisper による日本語音声認識ラッパ。

モデルは config で差し替え可能（kotoba-whisper-v2.0-faster / large-v3 / small ...）。
モデルのロードは重いので起動時に一度だけ行い、以後は使い回す。

device="auto" のとき、GPU(CUDA)が使えれば cuda + int8_float16 で動く。
GTX 1660 SUPER 程度でも CPU比 約10倍速（RTF 2.1 → 0.2）になる。
"""
from __future__ import annotations

import glob
import os
import site
import threading

import numpy as np

from config import config


def _ensure_cuda_dll_dirs() -> None:
    """pip の nvidia-*-cu12 wheel に入った CUDA DLL を読み込めるようにする。

    Windows では site-packages/nvidia/**/bin を DLL 探索パスへ追加しないと
    cublas64_12.dll 等が見つからず CUDA 実行に失敗する。
    """
    dirs: set[str] = set()
    bases = list(site.getsitepackages())
    user_site = site.getusersitepackages()
    if isinstance(user_site, str):
        bases.append(user_site)
    for base in bases:
        for p in glob.glob(os.path.join(base, "nvidia", "**", "bin"), recursive=True):
            dirs.add(p)
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except OSError:
            pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _resolve_device_compute() -> tuple[str, str]:
    """config の device/compute_type を実際の値に解決する（auto対応）。"""
    device = config.device
    compute = config.compute_type
    if device == "auto":
        try:
            import ctranslate2

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    if compute == "auto":
        compute = "int8_float16" if device == "cuda" else "int8"
    return device, compute


class Transcriber:
    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()  # faster-whisper は同時呼び出し非対応
        self.device = "?"
        self.compute = "?"

    def load(self) -> None:
        """モデルをロードする（起動時に呼ぶ）。"""
        _ensure_cuda_dll_dirs()
        from faster_whisper import WhisperModel  # 遅延インポート

        device, compute = _resolve_device_compute()
        try:
            self._model = WhisperModel(config.model, device=device, compute_type=compute)
        except Exception as exc:  # GPU 初期化失敗時は CPU へフォールバック
            print(f"[VoxCraft] {device}/{compute} 初期化失敗（{str(exc)[:120]}）。CPUに切替。")
            device, compute = "cpu", "int8"
            self._model = WhisperModel(config.model, device=device, compute_type=compute)
        self.device = device
        self.compute = compute

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
                beam_size=config.beam_size,
            )
            text = "".join(seg.text for seg in segments)
        return text.strip()


transcriber = Transcriber()
