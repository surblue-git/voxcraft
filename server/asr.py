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
from dataclasses import dataclass

import numpy as np

from config import config

# 丸ごと一致したら捨てる定番の幻覚（無音・BGM・吐息で頻出する決まり文句）。
_HALLUCINATIONS = {
    "はい", "はいはい", "はい。", "ん", "んー", "うん",
    "ありがとうございました", "ありがとうございました。",
    "ありがとうございます", "ありがとうございます。",
    "ご視聴ありがとうございました", "ご視聴ありがとうございました。",
    "おやすみなさい", "バイバイ", "はい、", "です。",
    "チャンネル登録お願いします", "最後までご視聴いただきありがとうございます",
}


@dataclass(frozen=True)
class AsrOptions:
    """認識1回ぶんの挙動。セッション（モード）ごとに切り替える。

    口述（dictation）は従来どおり config の既定値をそのまま使う ＝ 挙動不変。
    文字起こし（transcription）と復旧（recovery）だけ、取りこぼしを嫌って緩める。
    """

    vad_filter: bool
    no_speech_threshold: float
    logprob_threshold: float
    beam_size: int
    block_hallucinations: bool
    condition_on_previous: bool

    @staticmethod
    def dictation() -> "AsrOptions":
        """自分の声での音声入力。既存の挙動を1ミリも変えない。"""
        return AsrOptions(
            vad_filter=config.vad_filter,
            no_speech_threshold=config.no_speech_threshold,
            logprob_threshold=config.logprob_threshold,
            beam_size=config.beam_size,
            block_hallucinations=True,
            condition_on_previous=False,
        )

    @staticmethod
    def transcription() -> "AsrOptions":
        """動画・会議の文字起こし。脱落を最小化する側に倒す。

        - 二重VADをやめる（自前 VadChunker で既に切っているため内側は不要）
        - 低確信セグメントの破棄をほぼ無効化（正しい発話まで消えるのを防ぐ）
        - 「はい」等の丸ごと一致破棄をしない（会見の冒頭が消えるのを防ぐ）
        """
        return AsrOptions(
            vad_filter=False,
            no_speech_threshold=0.95,
            logprob_threshold=-3.0,
            beam_size=max(config.beam_size, 5),
            block_hallucinations=False,
            condition_on_previous=False,
        )

    @staticmethod
    def command() -> "AsrOptions":
        """候補モーダル操作中の短い発話（「3番」「確定」）。速度優先。

        本文には入らない状態なので精度より応答速度が要る。短い発話を
        「吐息の幻覚」として捨てられると選べなくなるため、破棄側は緩める。
        """
        return AsrOptions(
            vad_filter=config.vad_filter,
            no_speech_threshold=max(config.no_speech_threshold, 0.8),
            logprob_threshold=min(config.logprob_threshold, -1.5),
            beam_size=1,
            block_hallucinations=False,
            condition_on_previous=False,
        )

    @staticmethod
    def recovery() -> "AsrOptions":
        """録音済み音声からの再認識（復旧）。速度を捨てて精度に全振りする。"""
        return AsrOptions(
            vad_filter=False,
            no_speech_threshold=0.99,
            logprob_threshold=-5.0,
            beam_size=max(config.beam_size, 8),
            block_hallucinations=False,
            condition_on_previous=False,
        )


@dataclass
class TranscribeResult:
    """認識結果と、フィルタで捨てたテキスト。

    捨てた分を保持するのは「無言で消える」のを避けるため。ログにも残し、
    クライアントには警告として通知する。
    """

    text: str
    dropped: list[str]


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
    "turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
    "large-v3-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
    "v3-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
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

    def transcribe(
        self,
        audio: np.ndarray,
        hotwords: str | None = None,
        opts: AsrOptions | None = None,
    ) -> TranscribeResult:
        """float32 16kHz モノラル音声を認識する。

        吐息・無音由来の幻覚（「はい」等）を no_speech_prob / avg_logprob /
        丸ごと一致ブロックリストで抑制する。何を捨てたかは戻り値に残す。
        """
        if self._model is None:
            raise RuntimeError("model not loaded")

        from userdict import get_hallucinations

        o = opts or AsrOptions.dictation()
        dropped: list[str] = []

        with self._lock:
            segments, _info = self._model.transcribe(
                audio,
                language=config.language,
                task="transcribe",
                initial_prompt=config.initial_prompt or None,
                hotwords=hotwords or None,
                vad_filter=o.vad_filter,   # 内蔵VADで非発話部分を除去
                condition_on_previous_text=o.condition_on_previous,
                beam_size=o.beam_size,
            )
            kept: list[str] = []
            for seg in segments:
                nsp = getattr(seg, "no_speech_prob", 0.0) or 0.0
                lp = getattr(seg, "avg_logprob", 0.0) or 0.0
                if nsp > o.no_speech_threshold:
                    dropped.append(f"{seg.text.strip()}（no_speech={nsp:.2f}）")
                    continue  # 吐息・無音の幻覚
                if lp < o.logprob_threshold:
                    dropped.append(f"{seg.text.strip()}（logprob={lp:.2f}）")
                    continue  # 低確信
                kept.append(seg.text)
            text = "".join(kept).strip()

        # 丸ごと定番の幻覚なら捨てる（本文中に混ざった場合は残す）。
        if o.block_hallucinations:
            user_halls = get_hallucinations()
            if text in _HALLUCINATIONS or text in user_halls:
                dropped.append(f"{text}（幻覚ブロックリスト）")
                text = ""

        for d in dropped:
            print(f"[VoxCraft] 破棄: {d}")
        return TranscribeResult(text=text, dropped=dropped)


transcriber = Transcriber()
