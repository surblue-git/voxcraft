"""Windows の既定出力を WASAPI ループバックで取り込む。

ブラウザ/Electron の画面共有には依存せず、認識サーバーが動いている PC の
再生音を直接取得する。依存パッケージは遅延 import し、通常のマイク入力だけを
使う環境では Windows 以外でも従来どおりサーバーを起動できるようにする。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np


class SystemAudioError(RuntimeError):
    """PC 音声入力を開始できない、または継続できない。"""


@dataclass(frozen=True)
class SystemAudioInfo:
    device: str
    sample_rate: int
    channels: int


def _decode_pcm16_mono(data: bytes, channels: int) -> np.ndarray:
    """インターリーブ PCM16LE を float32 モノラルへ変換する。"""
    if channels < 1:
        raise ValueError("channels must be positive")
    samples = np.frombuffer(data, dtype="<i2")
    usable = samples.size - (samples.size % channels)
    if usable <= 0:
        return np.empty(0, dtype=np.float32)
    frames = samples[:usable].reshape(-1, channels).astype(np.float32)
    # ステレオ/サラウンドの全チャンネルを同じ重みで畳む。float32 化してから
    # 平均することで int16 の加算オーバーフローを避ける。
    mono = frames[:, 0] if channels == 1 else frames.mean(axis=1)
    return np.asarray(mono / 32768.0, dtype=np.float32)


class _StreamingConverter:
    def __init__(self, input_rate: int, output_rate: int, channels: int) -> None:
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.channels = channels
        self._resampler = None
        if input_rate != output_rate:
            try:
                import soxr
            except ImportError as exc:
                raise SystemAudioError(
                    "PC音声の変換に soxr が必要です。"
                    "server で pip install -r requirements.txt を実行してください。"
                ) from exc
            # チャンクごとに単発変換するとフィルター状態が切れてノイズと時刻ずれが
            # 生じるため、長時間用のストリーミング API をセッション中使い続ける。
            self._resampler = soxr.ResampleStream(
                input_rate,
                output_rate,
                1,
                dtype="float32",
                quality="HQ",
            )

    def push(self, data: bytes) -> np.ndarray:
        mono = _decode_pcm16_mono(data, self.channels)
        if mono.size == 0 or self._resampler is None:
            return mono
        return np.asarray(self._resampler.resample_chunk(mono, last=False), dtype=np.float32)

    def flush(self) -> np.ndarray:
        if self._resampler is None:
            return np.empty(0, dtype=np.float32)
        return np.asarray(
            self._resampler.resample_chunk(np.empty(0, dtype=np.float32), last=True),
            dtype=np.float32,
        )


class WasapiLoopbackCapture:
    """既定の Windows WASAPI 出力をコールバック方式で取得する。"""

    def __init__(
        self,
        target_rate: int,
        on_audio: Callable[[np.ndarray], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        self.target_rate = target_rate
        self.on_audio = on_audio
        self.on_error = on_error
        self._pyaudio_module = None
        self._manager = None
        self._stream = None
        self._converter: _StreamingConverter | None = None
        self._failed = False

    def _find_default_loopback(self, manager, pyaudio):
        try:
            wasapi = manager.get_host_api_info_by_type(pyaudio.paWASAPI)
            output = manager.get_device_info_by_index(wasapi["defaultOutputDevice"])
        except (KeyError, OSError) as exc:
            raise SystemAudioError("Windowsの既定の音声出力を取得できません。") from exc

        if output.get("isLoopbackDevice"):
            return output

        # PyAudioWPatch の対応 API を優先する。古い版でも動くよう、名前照合を
        # フォールバックとして残す。
        analogue = getattr(manager, "get_wasapi_loopback_analogue_by_dict", None)
        if analogue is not None:
            try:
                return analogue(output)
            except (LookupError, OSError):
                pass

        output_name = str(output.get("name", ""))
        for device in manager.get_loopback_device_info_generator():
            if output_name and output_name in str(device.get("name", "")):
                return device
        raise SystemAudioError(
            f"既定出力「{output_name or '不明'}」のWASAPIループバックを取得できません。"
        )

    def start(self) -> SystemAudioInfo:
        if self._stream is not None:
            raise SystemAudioError("PC音声入力は既に開始しています。")
        try:
            import pyaudiowpatch as pyaudio
        except ImportError as exc:
            raise SystemAudioError(
                "PC音声入力に PyAudioWPatch が必要です。"
                "server で pip install -r requirements.txt を実行してください。"
            ) from exc

        manager = pyaudio.PyAudio()
        try:
            device = self._find_default_loopback(manager, pyaudio)
            channels = int(device.get("maxInputChannels", 0))
            input_rate = int(round(float(device.get("defaultSampleRate", 0))))
            if channels < 1 or input_rate < 1:
                raise SystemAudioError("既定出力の音声形式を取得できません。")
            converter = _StreamingConverter(input_rate, self.target_rate, channels)

            def callback(in_data, _frame_count, _time_info, _status):
                try:
                    audio = converter.push(in_data)
                    if audio.size:
                        self.on_audio(audio)
                    return (None, pyaudio.paContinue)
                except Exception as exc:  # PortAudio スレッド外へ例外を運ぶ
                    if not self._failed:
                        self._failed = True
                        self.on_error(SystemAudioError(f"PC音声の取得が停止しました: {exc}"))
                    return (None, pyaudio.paAbort)

            stream = manager.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=input_rate,
                frames_per_buffer=1024,
                input=True,
                input_device_index=int(device["index"]),
                stream_callback=callback,
                start=False,
            )
            self._pyaudio_module = pyaudio
            self._manager = manager
            self._converter = converter
            self._stream = stream
            stream.start_stream()
            return SystemAudioInfo(str(device.get("name", "Windows既定出力")), input_rate, channels)
        except Exception:
            manager.terminate()
            self._manager = None
            self._converter = None
            self._stream = None
            raise

    def stop(self) -> None:
        stream, manager, converter = self._stream, self._manager, self._converter
        self._stream = None
        self._manager = None
        self._converter = None
        try:
            if stream is not None:
                try:
                    if stream.is_active():
                        stream.stop_stream()
                finally:
                    stream.close()
            if converter is not None and not self._failed:
                tail = converter.flush()
                if tail.size:
                    self.on_audio(tail)
        finally:
            if manager is not None:
                manager.terminate()
