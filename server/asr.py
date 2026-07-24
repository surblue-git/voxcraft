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

# 丸ごと一致したら捨てる定番の幻覚（無音・BGM・吐息で頻出する決まり文句）。
_HALLUCINATIONS = {
    "はい", "はいはい", "はい。", "ん", "んー", "うん",
    "ありがとうございました", "ありがとうございました。",
    "ご視聴ありがとうございました", "ご視聴ありがとうございました。",
    "おやすみなさい", "バイバイ", "はい、", "です。",
    "チャンネル登録お願いします", "最後までご視聴いただきありがとうございます",
}


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


_MODEL_ALIASES = {
    "kotoba": "kotoba-tech/kotoba-whisper-v2.0-faster",
    "kotoba-v2": "kotoba-tech/kotoba-whisper-v2.0-faster",
    "kotoba-whisper-v2.0-faster": "kotoba-tech/kotoba-whisper-v2.0-faster",
    "turbo": "Deepdml/faster-whisper-large-v3-turbo",
    "large-v3-turbo": "Deepdml/faster-whisper-large-v3-turbo",
    "v3-turbo": "Deepdml/faster-whisper-large-v3-turbo",
    "large-v3": "Systran/faster-whisper-large-v3",
    "medium": "Systran/faster-whisper-medium",
    "small": "Systran/faster-whisper-small",
}


def resolve_model_name(name: str) -> str:
    """設定名・略称（例: turbo, v3-turbo, kotoba）を HuggingFace ID に解決する。"""
    cleaned = name.strip()
    return _MODEL_ALIASES.get(cleaned.lower(), cleaned)


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
        self.resolved_model = "?"

    def load(self) -> None:
        """モデルをロードする（起動時に呼ぶ）。"""
        _ensure_cuda_dll_dirs()
        from faster_whisper import WhisperModel  # 遅延インポート

        target_model = resolve_model_name(config.model)
        self.resolved_model = target_model
        device, compute = _resolve_device_compute()
        kwargs = {"device": device, "compute_type": compute}
        if config.flash_attention and device == "cuda":
            kwargs["flash_attention"] = True

        try:
            self._model = WhisperModel(target_model, **kwargs)
        except Exception as exc:  # GPU 初期化失敗（または flash_attention 非対応）時はフォールバック
            if "flash_attention" in kwargs:
                kwargs.pop("flash_attention")
                try:
                    self._model = WhisperModel(target_model, **kwargs)
                except Exception as inner_exc:
                    exc = inner_exc
                    self._model = None
            if self._model is None:
                print(f"[VoxCraft] {device}/{compute} 初期化失敗（{str(exc)[:120]}）。CPUに切替。")
                device, compute = "cpu", "int8"
                self._model = WhisperModel(target_model, device=device, compute_type=compute)

        self.device = device
        self.compute = compute

    @property
    def ready(self) -> bool:
        return self._model is not None

    def transcribe(self, audio: np.ndarray, hotwords: str | None = None) -> str:
        """float32 16kHz モノラル音声を認識してテキストを返す。

        吐息・無音由来の幻覚（「はい」等）を no_speech_prob / avg_logprob /
        丸ごと一致ブロックリストで抑制する。
        """
        if self._model is None:
            raise RuntimeError("model not loaded")

        from userdict import get_hallucinations

        with self._lock:
            segments, _info = self._model.transcribe(
                audio,
                language=config.language,
                task="transcribe",
                initial_prompt=config.initial_prompt or None,
                hotwords=hotwords or None,
                vad_filter=config.vad_filter,   # 内蔵VADで非発話部分を除去
                condition_on_previous_text=False,  # 幻覚の連鎖を防ぐ
                beam_size=config.beam_size,
            )
            kept: list[str] = []
            for seg in segments:
                nsp = getattr(seg, "no_speech_prob", 0.0) or 0.0
                lp = getattr(seg, "avg_logprob", 0.0) or 0.0
                if nsp > config.no_speech_threshold:
                    continue  # 吐息・無音の幻覚
                if lp < config.logprob_threshold:
                    continue  # 低確信
                kept.append(seg.text)
            text = "".join(kept).strip()

        # 丸ごと定番の幻覚なら捨てる（本文中に混ざった場合は残す）。
        user_halls = get_hallucinations()
        if text in _HALLUCINATIONS or text in user_halls:
            return ""
        return text


transcriber = Transcriber()
