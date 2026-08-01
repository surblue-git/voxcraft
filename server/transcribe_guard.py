"""文字起こし専用の安全策。

欠落区間を無条件に Whisper へ渡すと、ほぼ無音の区間から定型句を生成してしまう。
ここでは録音上の音声根拠を先に測り、再認識に値する区間だけを選別する。
通常の口述モードからは使用しない。
"""
from __future__ import annotations

from dataclasses import dataclass
import re

import numpy as np


@dataclass(frozen=True)
class SpeechEvidence:
    duration_sec: float
    rms: float
    peak: float
    active_ratio: float

    def supports_retry(self, *, min_rms: float, min_active_ratio: float) -> bool:
        return self.rms >= min_rms and self.active_ratio >= min_active_ratio


def speech_evidence(
    audio: np.ndarray,
    sample_rate: int,
    *,
    frame_sec: float = 0.032,
    active_rms: float = 0.003,
) -> SpeechEvidence:
    """区間全体のRMSと、人声候補になる有音フレームの割合を返す。

    Silero VADの結果だけに依存しないのは、今回のような遠い小声をVADが無音扱い
    した区間こそ再認識対象だから。PCループバックではノイズ床が低いため、
    短時間RMSを使うと小声と実質無音を安定して分離できる。
    """
    x = np.asarray(audio, dtype=np.float32)
    if x.size == 0 or sample_rate <= 0:
        return SpeechEvidence(0.0, 0.0, 0.0, 0.0)
    frame = max(1, int(round(frame_sec * sample_rate)))
    usable = (x.size // frame) * frame
    if usable:
        framed = x[:usable].reshape(-1, frame).astype(np.float64)
        frame_rms = np.sqrt(np.mean(framed * framed, axis=1))
        active_ratio = float(np.mean(frame_rms >= active_rms))
    else:
        active_ratio = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)) >= active_rms)
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    peak = float(np.max(np.abs(x)))
    return SpeechEvidence(x.size / sample_rate, rms, peak, active_ratio)


class AudioRingBuffer:
    """ストリーム上の絶対サンプル位置で直近音声を取り出す固定長バッファ。"""

    def __init__(self, max_samples: int) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be positive")
        self.max_samples = max_samples
        self._audio = np.empty(max_samples, dtype=np.float32)
        self._size = 0
        self._write = 0
        self.end = 0

    @property
    def start(self) -> int:
        return self.end - self._size

    def append(self, audio: np.ndarray) -> None:
        x = np.asarray(audio, dtype=np.float32)
        if x.size == 0:
            return
        self.end += x.size
        if x.size >= self.max_samples:
            self._audio[:] = x[-self.max_samples:]
            self._size = self.max_samples
            self._write = 0
            return
        first = min(x.size, self.max_samples - self._write)
        self._audio[self._write:self._write + first] = x[:first]
        if first < x.size:
            self._audio[:x.size - first] = x[first:]
        self._write = (self._write + x.size) % self.max_samples
        self._size = min(self.max_samples, self._size + x.size)

    def slice(self, start: int, end: int) -> np.ndarray | None:
        """範囲がすべて残っていればコピーを返す。欠けていれば None。"""
        if start < self.start or end > self.end or end <= start:
            return None
        length = end - start
        oldest = (self._write - self._size) % self.max_samples
        lo = (oldest + start - self.start) % self.max_samples
        first = min(length, self.max_samples - lo)
        if first == length:
            return self._audio[lo:lo + length].copy()
        return np.concatenate((self._audio[lo:], self._audio[:length - first]))


class SilenceTracker:
    """PC音声が実質無音になってからの経過時間を追跡する。"""

    def __init__(self, timeout_sec: float, audible_rms: float, now: float) -> None:
        self.timeout_sec = timeout_sec
        self.audible_rms = audible_rms
        self.last_audible_at = now

    def feed(self, audio: np.ndarray, now: float) -> None:
        x = np.asarray(audio, dtype=np.float32)
        if x.size == 0:
            return
        rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
        if rms >= self.audible_rms:
            self.last_audible_at = now

    def remaining(self, now: float) -> float:
        return self.timeout_sec - (now - self.last_audible_at)

    def expired(self, now: float) -> bool:
        return self.timeout_sec > 0 and self.remaining(now) <= 0


# 実測で企業説明会の本文へ混入したYouTube系の定型句。実際に動画の締めで
# 発話される可能性もあるため、単独かつ十分な音声根拠がある場合は残す。
_OBVIOUS_VIDEO_ARTIFACTS = (
    "次回予告",
    "次の動画でお会いしましょう",
)
# 本物の動画末尾でも発話されるため常時禁止にはしないが、後ろに本文が続くなら
# Whisper がチャンク途中へ差し込んだ定型幻覚と判断できる句。
_MIDSTREAM_VIDEO_ARTIFACTS = (
    "ご視聴ありがとうございました",
)
_CONTEXTUAL_ARTIFACTS = (
    "おやすみなさい",
)
_POSSIBLY_REAL_ENDINGS = (
    "以上で終わります",
)

_FORMAL_HANDOFF_RE = re.compile(
    r"(?:それでは|では).{0,40}(?:お願い(?:いた)?します|お願いいたします)"
    r"|(?:お願い(?:いた)?します|お願いいたします)[。！？!?]*\Z"
)
_FORMAL_INTRO_RE = re.compile(
    r"\A.{0,12}(?:皆様|みなさま).{0,8}"
    r"(?:こんにちは|おはようございます|こんばんは)"
    r"|\A.{0,30}(?:でございます|と申します|本日(?:の|は))"
)


def standalone_contextual_artifact(text: str) -> str | None:
    """前後チャンクの確認が必要な、単独の疑わしい定型句を返す。"""
    normalized = text.strip(" 、。！？!?　")
    for phrase in _CONTEXTUAL_ARTIFACTS:
        if normalized == phrase:
            return phrase
    return None


def should_remove_between_context(
    phrase: str,
    previous_text: str,
    next_text: str,
) -> bool:
    """単独句が企業説明会の話者交代に挟まれた幻覚なら True。

    両隣が揃い、直前が登壇依頼、直後が正式な自己紹介の場合だけ除去する。
    「おやすみなさい」を常時禁止しないため、日常会話や動画の本物の挨拶は残る。
    """
    if phrase not in _CONTEXTUAL_ARTIFACTS:
        return False
    previous = previous_text.strip()
    following = next_text.strip()
    if not previous or not following:
        return False
    return bool(_FORMAL_HANDOFF_RE.search(previous) and _FORMAL_INTRO_RE.search(following))


def filter_contextual_artifacts(
    text: str,
    evidence: SpeechEvidence,
    *,
    weak_rms: float = 0.0015,
    weak_active_ratio: float = 0.03,
) -> tuple[str, list[str]]:
    """文字起こし結果から、音声と文脈の両方で不自然な定型句だけを除く。

    - 実文に癒着した動画定型句は、その句だけを除く。
    - 単独出力は、音声根拠が弱い場合だけ除く（本物の動画アウトロを守る）。
    - 「以上で終わります」のような会議で実在し得る句は特に保守的に扱う。
    """
    cleaned = text.strip()
    removed: list[str] = []
    weak_audio = evidence.rms < weak_rms or evidence.active_ratio < weak_active_ratio

    for phrase in _OBVIOUS_VIDEO_ARTIFACTS + _CONTEXTUAL_ARTIFACTS:
        if phrase not in cleaned:
            continue
        without = cleaned.replace(phrase, "").strip(" 、。！？!?　")
        embedded = bool(without)
        # 「おやすみなさい」は実在性が高いので、強い音声の単独発話は必ず残す。
        # 癒着していれば、前後の実文を守りつつ該当句だけを外す。
        if embedded or weak_audio:
            cleaned = cleaned.replace(phrase, "")
            removed.append(phrase)

    for phrase in _MIDSTREAM_VIDEO_ARTIFACTS:
        position = cleaned.find(phrase)
        if position < 0:
            continue
        following = cleaned[position + len(phrase):].strip(" 、。！？!?　")
        # 実際の締めの挨拶は残す。直後に別の本文が十分続く場合だけ癒着幻覚とみなす。
        if len(following) >= 12:
            cleaned = cleaned[:position] + cleaned[position + len(phrase):]
            removed.append(phrase)

    for phrase in _POSSIBLY_REAL_ENDINGS:
        if cleaned.strip(" 、。！？!?　") == phrase and weak_audio:
            cleaned = ""
            removed.append(phrase)

    # 句を抜いた境界に句読点が二重化した場合だけ整える。本文自体は触らない。
    cleaned = re.sub(r"([。！？!?])\1+", r"\1", cleaned)
    cleaned = cleaned.strip(" 、　")
    return cleaned, removed
