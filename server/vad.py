"""VAD（Voice Activity Detection）によるチャンク分割。

役割は「文の区切り（息継ぎ）を見つけてチャンクを確定する」ことだけ。
セッションの停止判断は一切しない ＝ どれだけ長く黙っていても待機は解除されない。

silero-onnx が使えればそれを、無ければ簡易エネルギーVADにフォールバックする。
入力は 16kHz / float32 モノラルの numpy 配列を想定。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# silero-vad は 16kHz で 512 サンプル(=32ms)単位の推論を要求する。
_FRAME = 512


@dataclass
class Chunk:
    """確定した発話チャンク。"""

    audio: np.ndarray  # float32 モノラル 16kHz
    reason: str        # "silence" | "max_len"
    # ストリーム先頭からのサンプル位置。録音を残す運用で、テキストと音声を
    # 対応づけて後から同じ区間を再認識（復旧）するために使う。
    start: int = 0
    end: int = 0


class _SileroDetector:
    """silero-vad(onnxruntime) を使うフレーム単位の発話判定。"""

    def __init__(self, sample_rate: int, threshold: float):
        # 遅延インポート（未インストールでも簡易VADに落とせるように）。
        from silero_vad import load_silero_vad  # type: ignore

        self._model = load_silero_vad(onnx=True)
        self._sr = sample_rate
        self._threshold = threshold

    def is_speech(self, frame: np.ndarray) -> bool:
        import torch  # type: ignore

        with torch.no_grad():
            prob = self._model(torch.from_numpy(frame), self._sr).item()
        return prob >= self._threshold

    def reset(self) -> None:
        if hasattr(self._model, "reset_states"):
            self._model.reset_states()


class _EnergyDetector:
    """依存なしのフォールバック。短時間エネルギーのしきい値で判定する。"""

    def __init__(self, sample_rate: int, threshold: float):
        self._sr = sample_rate
        # threshold(0-1) を RMS の絶対しきい値に緩く写像する。
        self._rms_floor = 0.006 + 0.02 * threshold

    def is_speech(self, frame: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)) + 1e-9)
        return rms >= self._rms_floor

    def reset(self) -> None:  # noqa: D401 - フォールバックは状態を持たない
        pass


def build_detector(sample_rate: int, threshold: float):
    """利用可能なら silero、無ければエネルギーVADを返す。"""
    try:
        return _SileroDetector(sample_rate, threshold)
    except Exception:
        return _EnergyDetector(sample_rate, threshold)


class VadChunker:
    """ストリームで受け取った音声を息継ぎ単位のチャンクに切り出す。

    使い方:
        chunker = VadChunker(...)
        for pcm_block in stream:
            for chunk in chunker.push(pcm_block):
                transcribe(chunk.audio)
        # ユーザーが停止したとき:
        tail = chunker.flush()
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        silence_sec: float = 0.8,
        max_chunk_sec: float = 12.0,
        min_speech_sec: float = 0.3,
        vad_threshold: float = 0.5,
        speech_pad_sec: float = 0.2,
        maxlen_search_sec: float = 1.5,
    ):
        self._sr = sample_rate
        self._silence_frames = max(1, int(silence_sec * sample_rate / _FRAME))
        self._pad_frames = max(1, int(speech_pad_sec * sample_rate / _FRAME))
        # 強制確定のとき、どれだけ遡って「いちばん静かな位置」を探すか。
        self._maxlen_search_frames = max(1, int(maxlen_search_sec * sample_rate / _FRAME))
        self._max_samples = int(max_chunk_sec * sample_rate)
        self._min_samples = int(min_speech_sec * sample_rate)
        self._detector = build_detector(sample_rate, vad_threshold)

        self._buf: list[np.ndarray] = []   # 現在のチャンク音声
        self._residual = np.zeros(0, dtype=np.float32)  # フレーム未満の端数
        self._silence_run = 0
        self._has_speech = False
        self._last_speech_idx = 0
        self._cur_len = 0
        self._carried = 0  # 前チャンクから繰り越した無音の長さ（最小長判定から除外する）
        self._stream_pos = 0  # フレーム化して処理済みのサンプル数（ストリーム上の現在位置）
        self._buf_start = 0   # _buf の先頭がストリーム上の何サンプル目か

    def push(self, pcm: np.ndarray) -> list[Chunk]:
        """float32 音声ブロックを流し込み、確定したチャンクのリストを返す。"""
        chunks: list[Chunk] = []
        data = np.concatenate([self._residual, pcm.astype(np.float32)])
        n_frames = len(data) // _FRAME
        self._residual = data[n_frames * _FRAME:]

        for i in range(n_frames):
            frame = data[i * _FRAME:(i + 1) * _FRAME]
            speech = self._detector.is_speech(frame)

            if not self._buf:
                self._buf_start = self._stream_pos
            self._buf.append(frame)
            self._cur_len += _FRAME
            self._stream_pos += _FRAME

            if speech:
                self._has_speech = True
                self._silence_run = 0
                self._last_speech_idx = len(self._buf)
            else:
                self._silence_run += 1

            # 息継ぎ（無音の連続）で確定。
            if self._has_speech and self._silence_run >= self._silence_frames:
                chunk = self._finalize("silence")
                if chunk is not None:
                    chunks.append(chunk)
            # 長すぎるチャンクの強制確定。
            elif self._cur_len >= self._max_samples:
                chunk = self._finalize("max_len")
                if chunk is not None:
                    chunks.append(chunk)

        return chunks

    def set_silence_sec(self, sec: float) -> None:
        """息継ぎ判定の長さを稼働中に変える（候補モーダル操作中の短縮用）。

        バッファや繰り越しには触らないので、切り替えで音声を落とさない。
        """
        self._silence_frames = max(1, int(sec * self._sr / _FRAME))

    def set_min_speech_sec(self, sec: float) -> None:
        """発話とみなす最小長を稼働中に変える（「2番」のような短い発話用）。"""
        self._min_samples = int(sec * self._sr)

    def flush(self) -> Chunk | None:
        """停止時に、残っている音声を最後のチャンクとして確定する。"""
        if self._residual.size:
            if not self._buf:
                self._buf_start = self._stream_pos
            self._buf.append(self._residual)
            self._cur_len += self._residual.size
            self._stream_pos += self._residual.size
            self._residual = np.zeros(0, dtype=np.float32)
        return self._finalize("silence")

    def _finalize(self, reason: str) -> Chunk | None:
        # 繰り越した無音は「発話の長さ」に数えない（ノイズ判定を繰り越し前と同じ厳しさに保つ）。
        own_len = self._cur_len - self._carried
        if not self._buf or not self._has_speech or own_len < self._min_samples:
            self._reset_chunk()
            return None
        # 無音確定時は、最後の発話フレーム + パディング分だけに切り詰めて余分な長無音をカットする。
        # 強制確定（max_len）は「無音でない場所で切る」ことになるので、直近のいちばん静かな
        # 位置まで戻して切る。原稿読み上げのように息継ぎが短い話し方では全境界がここを通るため、
        # 語の途中で断ち切るとその語が壊れる。
        carry: list[np.ndarray] = []
        keep_count = len(self._buf)
        if reason == "silence" and self._last_speech_idx > 0:
            keep_count = min(len(self._buf), self._last_speech_idx + self._pad_frames)
        elif reason == "max_len":
            keep_count = self._quietest_cut()
        audio_buf = self._buf[:keep_count]
        carry = self._buf[keep_count:]

        audio = np.concatenate(audio_buf)
        start = self._buf_start
        end = start + audio.size
        self._reset_chunk()
        # 切り詰めた末尾は捨てずに次チャンクへ繰り越す。
        # VADが小声・語尾・フィラー（「ま、」等）を無音と誤判定した場合でも、
        # そこに実音声があれば次のチャンクに含まれるので発話を取りこぼさない。
        if carry:
            self._buf = list(carry)
            self._cur_len = sum(len(f) for f in carry)
            self._carried = self._cur_len
            self._buf_start = end
        self._detector.reset()
        return Chunk(audio=audio, reason=reason, start=start, end=end)

    def _quietest_cut(self) -> int:
        """強制確定の切れ目を、直近でいちばん音量の低いフレーム境界に寄せる。

        戻り値は前チャンクに残すフレーム数。残りは次チャンクへ繰り越されるので、
        音声そのものは一切失われない。
        """
        n = len(self._buf)
        win = min(self._maxlen_search_frames, n - 1)
        if win <= 0:
            return n
        best_idx = n - 1
        best_rms = float("inf")
        for i in range(n - win, n):
            frame = self._buf[i]
            rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
            if rms < best_rms:
                best_rms = rms
                best_idx = i
        # いちばん静かなフレームは前チャンクの末尾として残し、その次から繰り越す。
        return min(n, best_idx + 1)

    def _reset_chunk(self) -> None:
        self._buf = []
        self._silence_run = 0
        self._has_speech = False
        self._last_speech_idx = 0
        self._cur_len = 0
        self._carried = 0
