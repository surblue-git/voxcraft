"""VAD（Voice Activity Detection）によるチャンク分割。

役割は「文の区切り（息継ぎ）を見つけてチャンクを確定する」ことだけ。
セッションの停止判断は一切しない ＝ どれだけ長く黙っていても待機は解除されない。

silero-onnx が使えればそれを、無ければ簡易エネルギーVADにフォールバックする。
入力は 16kHz / float32 モノラルの numpy 配列を想定。
"""
from __future__ import annotations

import time
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
    # 前チャンクの発話終わりから、このチャンクの発話始まりまでの無音（秒）。
    # 「息継ぎで読点を打つ」判断材料。最初のチャンクなど計測不能なら None。
    pause: float | None = None


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


# 暗騒音の追従。フレームは 512サンプル ＝ 32ms。
# 下がるのは速く（静かな部分にすぐ追いつく）、上がるのは遅く（発話で持ち上がらない）。
_NOISE_FALL = 0.05    # 時定数 約0.6秒
_NOISE_RISE = 0.003   # 時定数 約10秒
# 暗騒音の何倍を発話とみなすか。実測での選定は下の注記を参照。
_NOISE_RATIO = 3.0
# 完全な無音（PCループバックの停止中など）で床が0に落ちないための下限。
_NOISE_MIN_FLOOR = 0.0008  # -62 dBFS


class _EnergyDetector:
    """依存なしのフォールバック。短時間エネルギーのしきい値で判定する。

    絶対しきい値では録音レベルの違いに耐えられない（2026-08-06 実測）
    ---------------------------------------------------------------
    床は録音レベルによらない固定値なので、マイクと距離が変われば簡単に外れる。
    会見録音（会場の音をPCのマイクで収録）は 100ms RMS の中央値が -42.9 dBFS で、
    既定の床 0.016（-35.9 dBFS）の **7dB下**にあった。結果、12秒のあいだ一度も
    発話と判定されず、VadChunker._finalize が `not _has_speech` でバッファごと
    捨てる。⟨未認識⟩ が max_chunk_sec の整数倍で出るのはこれが理由:

        欠落  14:26-14:38   RMS -49.7 dBFS → 発話判定 0/375 フレーム
        欠落  32:01-32:13   RMS -47.6 dBFS → 発話判定 0/375 フレーム
        拾えた 15:00-15:12  RMS -33.0 dBFS → 発話判定 111/375 (29.6%)

    捨てられた区間を後から単独で認識すると**実際の発話が出る**。音は入っていた。

    adaptive=True で、直近の静かな部分（暗騒音）を追いかけて床をその比率に置く。
    **固定床より厳しくはならない**（min で上から抑えている）ので、今まで拾えて
    いた音は必ず拾う。逆に、静かな録音では床が下がって拾えるようになる。

    口述では使わない（自分の声を近接マイクで録る前提なので絶対床で足りているし、
    床が下がると吐息を拾って「はい」等の幻覚が増える方向に動くため）。
    """

    def __init__(self, sample_rate: int, threshold: float, adaptive: bool = False):
        self._sr = sample_rate
        # threshold(0-1) を RMS の絶対しきい値に緩く写像する。
        self._rms_floor = 0.006 + 0.02 * threshold
        self._adaptive = adaptive
        self._noise: float | None = None

    def is_speech(self, frame: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)) + 1e-9)
        if not self._adaptive:
            return rms >= self._rms_floor
        if self._noise is None:
            self._noise = rms
        elif rms < self._noise:
            self._noise += (rms - self._noise) * _NOISE_FALL
        else:
            self._noise += (rms - self._noise) * _NOISE_RISE
        floor = min(self._rms_floor, max(_NOISE_MIN_FLOOR, self._noise * _NOISE_RATIO))
        return rms >= floor

    def reset(self) -> None:
        """チャンク確定ごとに呼ばれる。暗騒音の推定は**持ち越す**。

        毎チャンク捨てると数百ミリ秒ごとに推定がやり直しになり、追従の意味が
        無くなる。silero 側の reset は LSTM 状態のクリアで、目的が違う。
        """


def build_detector(sample_rate: int, threshold: float, adaptive: bool = False):
    """利用可能なら silero、無ければエネルギーVADを返す。

    adaptive はエネルギーVADにだけ効く。silero は学習済みで音量に対して
    それなりに頑健なので、壊れている側（絶対しきい値）だけを直す。
    """
    try:
        return _SileroDetector(sample_rate, threshold)
    except Exception:
        return _EnergyDetector(sample_rate, threshold, adaptive=adaptive)


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
        adaptive_energy: bool = False,
    ):
        self._sr = sample_rate
        self._silence_frames = max(1, int(silence_sec * sample_rate / _FRAME))
        self._pad_frames = max(1, int(speech_pad_sec * sample_rate / _FRAME))
        # 強制確定のとき、どれだけ遡って「いちばん静かな位置」を探すか。
        self._maxlen_search_frames = max(1, int(maxlen_search_sec * sample_rate / _FRAME))
        self._max_samples = int(max_chunk_sec * sample_rate)
        self._min_samples = int(min_speech_sec * sample_rate)
        self._detector = build_detector(
            sample_rate, vad_threshold, adaptive=adaptive_energy
        )

        self._buf: list[np.ndarray] = []   # 現在のチャンク音声
        self._residual = np.zeros(0, dtype=np.float32)  # フレーム未満の端数
        self._silence_run = 0
        self._has_speech = False
        self._last_speech_idx = 0
        self._cur_len = 0
        self._carried = 0  # 前チャンクから繰り越した無音の長さ（最小長判定から除外する）
        self._stream_pos = 0  # フレーム化して処理済みのサンプル数（ストリーム上の現在位置）
        self._buf_start = 0   # _buf の先頭がストリーム上の何サンプル目か
        # 息継ぎ長の計測。チャンクの start/end は繰り越し無音を含むため使えない。
        # 「発話の終わり」と「次の発話の始まり」のストリーム位置を直接記録する。
        self._first_speech_pos: int | None = None  # 現バッファで最初に声が出た位置
        self._prev_speech_end: int | None = None   # 前チャンクで最後に声が出た位置

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
                if not self._has_speech:
                    self._first_speech_pos = self._stream_pos - _FRAME
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

        # 息継ぎ長 = 前チャンクの発話終わり → このチャンクの発話始まり（秒）。
        pause: float | None = None
        if self._first_speech_pos is not None and self._prev_speech_end is not None:
            pause = max(0.0, (self._first_speech_pos - self._prev_speech_end) / self._sr)
        # このチャンクの発話終わり位置を次回のために記録する。
        speech_end = self._buf_start + self._last_speech_idx * _FRAME
        self._prev_speech_end = speech_end

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
        return Chunk(audio=audio, reason=reason, start=start, end=end, pause=pause)

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
        self._first_speech_pos = None


def _merge(a: Chunk, b: Chunk) -> Chunk:
    """隣り合う2チャンクを1つにする（a の直後が b であること）。"""
    return Chunk(
        audio=np.concatenate([a.audio, b.audio]),
        reason=b.reason,   # 終わり方は後ろのチャンクのもの
        start=a.start,
        end=b.end,
        pause=a.pause,     # 息継ぎは「連結したかたまりの前」の無音
    )


class ChunkJoiner:
    """短いチャンクを次のチャンクと連結してから認識に回す（文字起こし専用）。

    なぜ必要か
    ----------
    Whisper は30秒窓のモデルで、1秒に満たない音声を単体で渡すと、残りを無音で
    埋めた入力に対して学習データの定型句（動画のアウトロ）を吐く。
    実測（2026-07-30, VAIO発表会47.5分, 1120チャンク）:

        1秒未満  29.5% が「ご視聴ありがとうございました」
        1〜2秒   12.9%
        2〜4秒    7.5%
        4秒以上   7.3%

    同じ音声でも前後3秒を足して認識し直すと幻覚は消え、本物の発話が出た（7/7）。
    つまり**短いまま単体で投げない**ことが根本的な対処になる。幻覚だけでなく、
    チャンク境界で語が割れることによる誤変換も同時に減る。
    副次効果として、Whisper は入力長によらず30秒窓ぶん計算するため、チャンク数が
    減るとGPU負荷もそのぶん下がる。

    口述（dictation）では使わない ＝ 既存の挙動は不変。

    連結の条件
    ----------
    - 直前に溜めたチャンクの終端と、次のチャンクの始端が**隙間なく続く**ときだけ連結する。
      VADが捨てた区間を挟む場合に繋ぐと、テキストと音声の対応（復旧の土台）がずれるため。
    - 実時間で max_hold_sec を超えて待たない。孤立した短い発話をいつまでも
      画面に出さないより、単体で認識して出したほうがよい（幻覚は asr 側の
      定型句ブロックが受け止める）。
    - break_sec 以上の息継ぎをまたぐ連結もしない。ここは話の切れ目なので、
      繋ぐとチャンクの内側に埋もれて段落分け（main.py）の材料が消える。
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        min_sec: float = 4.0,
        max_hold_sec: float = 2.0,
        break_sec: float = 2.0,
    ):
        self._sr = sample_rate
        self._min_samples = int(min_sec * sample_rate)
        self._max_hold_sec = max_hold_sec
        self._break_sec = break_sec
        self._pending: Chunk | None = None
        self._held_at = 0.0

    def push(self, chunk: Chunk, now: float | None = None) -> list[Chunk]:
        """チャンクを流し込み、認識に回してよいものを返す。"""
        now = time.monotonic() if now is None else now
        out: list[Chunk] = []
        held_at = now
        long_pause = chunk.pause is not None and chunk.pause >= self._break_sec

        if self._pending is not None:
            if self._pending.end == chunk.start and not long_pause:
                chunk = _merge(self._pending, chunk)
                held_at = self._held_at  # 待ち始めた時刻は引き継ぐ（延々と待たない）
            else:
                # 音声が途切れている（繋ぐと span と音声がずれる）か、
                # 話の切れ目（繋ぐと段落の境目が消える）。溜めていた分を先に出す。
                out.append(self._pending)
            self._pending = None

        if chunk.audio.size >= self._min_samples:
            out.append(chunk)
        else:
            self._pending = chunk
            self._held_at = held_at
        return out

    def tick(self, now: float | None = None) -> list[Chunk]:
        """待たせすぎた短いチャンクを吐き出す（音声を受け取るたびに呼ぶ）。"""
        if self._pending is None:
            return []
        now = time.monotonic() if now is None else now
        if now - self._held_at < self._max_hold_sec:
            return []
        return self.flush()

    def flush(self) -> list[Chunk]:
        """溜めているチャンクを無条件に出す（停止時に呼ぶ）。"""
        pending, self._pending = self._pending, None
        return [pending] if pending is not None else []
