"""VoxCraft 認識サーバーの設定。

環境変数で上書きできる。既定値は自分用（CPU・日本語特化モデル）を想定。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    # --- ネットワーク ---
    host: str = _env("VOXCRAFT_HOST", "0.0.0.0")
    port: int = int(_env("VOXCRAFT_PORT", "8760"))

    # --- ASR モデル ---
    # kotoba-whisper-v2.0 の faster-whisper(CTranslate2) 版。
    # 精度優先なら "large-v3"、速度優先なら "small" 等に差し替え可能。
    model: str = _env("VOXCRAFT_MODEL", "kotoba-tech/kotoba-whisper-v2.0-faster")
    # "auto"（GPUがあれば cuda、無ければ cpu）/ "cpu" / "cuda"
    device: str = _env("VOXCRAFT_DEVICE", "auto")
    # "auto"（cuda→int8_float16 / cpu→int8）/ "int8" / "float16" / "int8_float16" ...
    compute_type: str = _env("VOXCRAFT_COMPUTE_TYPE", "auto")
    language: str = _env("VOXCRAFT_LANG", "ja")
    # デコードのビーム幅。1(貪欲)が最速、5が精度寄り。長文口述は1でも実用十分。
    beam_size: int = int(_env("VOXCRAFT_BEAM_SIZE", "1"))

    # --- 幻覚（吐息・無音を「はい」等と誤認識）対策 ---
    # faster-whisper 内蔵VADで、チャンク内の非発話部分を除去する。
    vad_filter: bool = _env("VOXCRAFT_VAD_FILTER", "1") == "1"
    # セグメントの no_speech_prob がこれを超えたら捨てる（吐息の幻覚除去）。
    no_speech_threshold: float = float(_env("VOXCRAFT_NO_SPEECH_THRESHOLD", "0.6"))
    # avg_logprob がこれ未満の低確信セグメントは捨てる。
    logprob_threshold: float = float(_env("VOXCRAFT_LOGPROB_THRESHOLD", "-1.0"))

    # --- 音声フォーマット（クライアントと合わせる） ---
    sample_rate: int = int(_env("VOXCRAFT_SAMPLE_RATE", "16000"))

    # --- VAD（区切り検出。停止判断はしない） ---
    # 無音がこの秒数続いたら「息継ぎ」とみなしチャンクを確定する。
    # どれだけ長く黙ってもセッション自体は切らない。
    # 小さいほど、記号語（まる/てん/かいぎょう）を短い間で独立させやすい。
    silence_sec: float = float(_env("VOXCRAFT_SILENCE_SEC", "0.5"))
    # チャンクが長くなりすぎた場合の強制確定（秒）。
    max_chunk_sec: float = float(_env("VOXCRAFT_MAX_CHUNK_SEC", "12.0"))
    # 発話とみなす最小長（秒）。これ未満の音はノイズとして捨てる。
    min_speech_sec: float = float(_env("VOXCRAFT_MIN_SPEECH_SEC", "0.3"))
    # silero-vad のしきい値（0-1、大きいほど厳しい）。
    vad_threshold: float = float(_env("VOXCRAFT_VAD_THRESHOLD", "0.5"))
    # 発話チャンク後方のパディング（秒）。語尾切れ（「です」「ます」が途切れる現象）を防ぐ。
    speech_pad_sec: float = float(_env("VOXCRAFT_SPEECH_PAD_SEC", "0.2"))

    # --- 高速化・GPU最適化 ---
    # CTranslate2 の FlashAttention 有効化（RTX 30xx/40xx 等で速度向上）。
    flash_attention: bool = _env("VOXCRAFT_FLASH_ATTENTION", "0") == "1"

    # --- 後処理 ---
    # 日本語と英数字の間に入る半角スペースを除去する。
    strip_ja_alnum_space: bool = _env("VOXCRAFT_STRIP_SPACE", "1") == "1"
    # 記号読み上げ（「まる」→「。」など）を有効化する。
    enable_symbol_dictation: bool = _env("VOXCRAFT_SYMBOLS", "1") == "1"

    # --- 再変換（変換戻し） ---
    # Google CGI API for Japanese Input を使う（無料・非公式・要オンライン）。
    use_google_cgi: bool = _env("VOXCRAFT_GOOGLE_CGI", "1") == "1"
    google_cgi_url: str = _env(
        "VOXCRAFT_GOOGLE_CGI_URL", "https://www.google.com/transliterate"
    )
    http_timeout_sec: float = float(_env("VOXCRAFT_HTTP_TIMEOUT", "5.0"))

    # 認識時の初期プロンプト（口語・句読点を促す）。
    initial_prompt: str = _env(
        "VOXCRAFT_INITIAL_PROMPT",
        "以下は日本語の口述です。句読点を適切に付けてください。",
    )

    aliases: dict = field(default_factory=dict)


config = Config()
