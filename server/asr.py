"""faster-whisper による日本語音声認識ラッパ。

モデルは config で差し替え可能（kotoba-whisper-v2.0-faster / large-v3 / small ...）。
モデルのロードは重いので起動時に一度だけ行い、以後は使い回す。

device="auto" のとき、GPU(CUDA)が使えれば cuda + int8_float16 で動く。
GTX 1660 SUPER 程度でも CPU比 約10倍速（RTF 2.1 → 0.2）になる。
"""
from __future__ import annotations

import glob
import os
import re
import site
import threading
from dataclasses import dataclass, field

import numpy as np

from config import config

# 丸ごと一致したら捨てる定番の幻覚（無音・BGM・吐息で頻出する決まり文句）。
# これは口述（dictation）専用。文字起こしでは会見冒頭の「はい」等が本物なので使わない。
_HALLUCINATIONS = {
    "はい", "はいはい", "はい。", "ん", "んー", "うん",
    "ありがとうございました", "ありがとうございました。",
    "ありがとうございます", "ありがとうございます。",
    "ご視聴ありがとうございました", "ご視聴ありがとうございました。",
    "おやすみなさい", "バイバイ", "はい、", "です。",
    "チャンネル登録お願いします", "最後までご視聴いただきありがとうございます",
}

# 動画のアウトロ定型句。学習データ由来で、短いチャンク・無音で頻出する。
# 実測（2026-07-30, VAIO発表会47.5分）: 1120チャンク中206チャンクがこれで、
# ノート本文の21%（3,613字）を占めていた。206件すべてがチャンク丸ごとの出力で、
# 本文に癒着した例は0件。しかも no_speech_prob=0.000 / avg_logprob 平均-0.57 と
# 「自信のある」出力なので、**閾値では1件も落とせない**。丸ごと一致で捨てるしかない。
#
# 取材で本当に言う「ご清聴ありがとうございました」は意図的に含めない
# （発表の締めで実際に言う）。「ありがとうございました」単体も同じ理由で含めない
# （実例:「皆さんにお越しいただき、誠にありがとうございます」）。
_BOILERPLATE_CORE = "|".join([
    r"(?:最後まで)?ご視聴(?:いただき)?(?:誠に)?ありがとうございま(?:した|す)",
    r"チャンネル登録(?:を)?(?:よろしく)?お願い(?:いたし|し)ます",
    r"(?:次回[もは])?お楽しみに",
])
# 先頭に許すのは短いつなぎ言葉だけ（実測で「では、」「それでは、」が付く例があった）。
# 「です」等の実語は許さない ＝ 本物の発話を巻き込んで消さない。
_BOILERPLATE_RE = re.compile(
    rf"\A(?:では|それでは|はい|ええ|えー|あの)?[、。]?[\s　]*"
    rf"(?:{_BOILERPLATE_CORE})[。、！\s　]*\Z"
)


def is_boilerplate(text: str) -> bool:
    """チャンク丸ごとが動画のアウトロ定型句なら True。

    本文の途中に混ざった場合は False（本物の発話を消さないため丸ごと一致に限る）。
    """
    return bool(_BOILERPLATE_RE.match(text.strip()))


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
    # 口述専用の丸ごと一致ブロックリスト（「はい」「うん」等）。
    block_hallucinations: bool
    condition_on_previous: bool
    # Whisper へ渡す初期プロンプト。モードごとに変えられるようにしてある。
    # large-v3-turbo は config.initial_prompt を渡すとチャンクを丸ごと崩す
    # （実測: 30秒/13セグメント/197字 → 1セグメント/19字の無関係なテキスト）。
    # 通し認識では先頭の30秒窓にしか効かないため被害が見えないが、リアルタイムは
    # 全チャンクに乗るので致命的。hotwords が長いと空になる既知の不具合と同系統。
    initial_prompt: str | None = None
    # 動画のアウトロ定型句（is_boilerplate）をチャンク丸ごと一致で捨てるか。
    # 口述は従来の block_hallucinations で同じ文言を既に捨てているので False のまま
    # ＝ 挙動不変。文字起こし・復旧だけ True にする。
    block_boilerplate: bool = False

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
            initial_prompt=config.initial_prompt or None,
        )

    @staticmethod
    def transcription() -> "AsrOptions":
        """動画・会議の文字起こし。脱落を最小化する側に倒す。

        - 二重VADをやめる（自前 VadChunker で既に切っているため内側は不要）
        - 低確信セグメントの破棄をほぼ無効化（正しい発話まで消えるのを防ぐ）
        - 「はい」等の丸ごと一致破棄をしない（会見の冒頭が消えるのを防ぐ）
        - ただし動画のアウトロ定型句だけは捨てる（本物の発話とは絶対に被らない）
        - initial_prompt を渡さない（turbo が崩れる。上の注記を参照）。
          句読点は punctuate.py が後処理で付けるので、プロンプトに頼る必要はない。
        """
        return AsrOptions(
            vad_filter=False,
            no_speech_threshold=0.95,
            logprob_threshold=-3.0,
            beam_size=max(config.beam_size, 5),
            block_hallucinations=False,
            condition_on_previous=False,
            initial_prompt=None,
            block_boilerplate=True,
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
            initial_prompt=config.initial_prompt or None,
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
            initial_prompt=None,  # transcription() と同じ理由
            block_boilerplate=True,
        )

    @staticmethod
    def refinement() -> "AsrOptions":
        """PC音声の速報を30秒前後の文脈で認識し直す補正パス。"""
        return AsrOptions(
            # 補正内でVADを重ねると、速報にはあった短い発話まで落ちるため使わない。
            vad_filter=False,
            no_speech_threshold=0.99,
            logprob_threshold=-5.0,
            beam_size=max(config.beam_size, 8),
            block_hallucinations=False,
            # 実音声で短い述語を落としたため、前のWhisper窓へ依存させない。
            condition_on_previous=False,
            initial_prompt=None,
            block_boilerplate=True,
        )


@dataclass
class TranscribeResult:
    """認識結果と、フィルタで捨てたテキスト。

    捨てた分を保持するのは「無言で消える」のを避けるため。ログにも残し、
    クライアントには警告として通知する。

    blocked は動画のアウトロ定型句として捨てたぶん。dropped と分けているのは、
    こちらは1回の文字起こしで数百件出るため（実測206件/47.5分）、
    クライアントに毎回警告を出すと通知で埋まってしまうから。サーバーログにだけ残す。
    """

    text: str
    dropped: list[str]
    blocked: list[str] = field(default_factory=list)
    # 自動再認識の採否に使う。通常表示では使わず、音声根拠を通過した欠落区間だけ
    # 「Whisper自身の確信度も十分か」を二段目として確認する。
    avg_logprob: float | None = None
    max_no_speech_prob: float | None = None


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
    def __init__(self, model_name: str | None = None) -> None:
        self._model = None
        self._lock = threading.Lock()  # faster-whisper は同時呼び出し非対応
        self._load_lock = threading.Lock()  # 遅延ロードの二重実行を防ぐ
        # None なら config.model（＝口述と同じ）。既定引数のままなら従来と同一。
        self._model_name = model_name
        self.device = "?"
        self.compute = "?"
        self.resolved_model = "?"

    def load(self) -> None:
        """モデルをロードする（起動時に呼ぶ）。"""
        _ensure_cuda_dll_dirs()
        from faster_whisper import WhisperModel  # 遅延インポート

        target_model = resolve_model_name(self._model_name or config.model)
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

    def ensure_loaded(self) -> None:
        """未ロードならロードする（遅延ロード用。2回目以降は即返る）。

        文字起こし用モデルは、そのモードを使わないユーザーに起動コストと
        VRAM を払わせないため、起動時ではなく初回利用時に読み込む。
        """
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is None:
                self.load()

    def transcribe(
        self,
        audio: np.ndarray,
        hotwords: str | None = None,
        opts: AsrOptions | None = None,
        hallucinations: set[str] | frozenset[str] | None = None,
    ) -> TranscribeResult:
        """float32 16kHz モノラル音声を認識する。

        吐息・無音由来の幻覚（「はい」等）を no_speech_prob / avg_logprob /
        丸ごと一致ブロックリストで抑制する。何を捨てたかは戻り値に残す。
        """
        if self._model is None:
            raise RuntimeError("model not loaded")

        if hallucinations is None:
            from userdict import get_hallucinations

            active_hallucinations = get_hallucinations()
        else:
            active_hallucinations = hallucinations

        o = opts or AsrOptions.dictation()
        dropped: list[str] = []
        kept_logprobs: list[float] = []
        kept_no_speech: list[float] = []

        with self._lock:
            segments, _info = self._model.transcribe(
                audio,
                language=config.language,
                task="transcribe",
                initial_prompt=o.initial_prompt,
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
                kept_logprobs.append(float(lp))
                kept_no_speech.append(float(nsp))
            text = "".join(kept).strip()

        # 動画のアウトロ定型句なら捨てる（文字起こし・復旧のみ。丸ごと一致に限る）。
        blocked: list[str] = []
        if o.block_boilerplate and text and is_boilerplate(text):
            blocked.append(text)
            text = ""

        # 丸ごと定番の幻覚なら捨てる（本文中に混ざった場合は残す）。
        if o.block_hallucinations:
            if text in _HALLUCINATIONS or text in active_hallucinations:
                dropped.append(f"{text}（幻覚ブロックリスト）")
                text = ""

        for d in dropped:
            print(f"[VoxCraft] 破棄: {d}")
        return TranscribeResult(
            text=text,
            dropped=dropped,
            blocked=blocked,
            avg_logprob=(sum(kept_logprobs) / len(kept_logprobs) if kept_logprobs else None),
            max_no_speech_prob=(max(kept_no_speech) if kept_no_speech else None),
        )


transcriber = Transcriber()

# 文字起こし・復旧だけで使う高品質モデル。口述（dictation）は上の transcriber の
# ままで一切関与させない ＝ 既存の挙動は不変。
# 同じモデルが指定されたら二重にVRAMを使わないよう None にして使い回す。
_hq_transcriber: Transcriber | None = (
    Transcriber(config.transcribe_model)
    if config.transcribe_model
    and resolve_model_name(config.transcribe_model) != resolve_model_name(config.model)
    else None
)


def hq_transcriber() -> Transcriber:
    """文字起こし・復旧用のモデルを返す（未設定なら口述と同じものを使う）。"""
    return _hq_transcriber or transcriber
