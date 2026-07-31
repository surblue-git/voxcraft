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
    # 文字起こし・復旧で使うモデル（口述とは別に持つ）。空にすると model と同じ。
    # 50分の取材音声での実測（analyze_session.py）:
    #   kotoba  : 12,808字 / 直後反復 42件 / 平均logprob -0.327 / 257秒*
    #   turbo   : 17,190字 / 直後反復  7件 / 平均logprob -0.228 / 257秒
    #   large-v3: 17,681字 / 直後反復  7件 / 平均logprob -0.222 / 632秒
    # turbo と large-v3 は品質がほぼ同じで large-v3 が2.5倍遅いため turbo を既定にする。
    # kotoba は12秒チャンクを丸ごと空で返すことがあり、実測で録音の7.1%
    # （216秒＝12秒×18）が ⟨未認識⟩ として欠落していた。
    # 口述は短い発話が中心で挙動を変えない方針のため、こちらは model のまま。
    transcribe_model: str = _env("VOXCRAFT_TRANSCRIBE_MODEL", "turbo")
    # "auto"（GPUがあれば cuda、無ければ cpu）/ "cpu" / "cuda"
    device: str = _env("VOXCRAFT_DEVICE", "auto")
    # "auto"（cuda→int8_float16 / cpu→int8）/ "int8" / "float16" / "int8_float16" ...
    compute_type: str = _env("VOXCRAFT_COMPUTE_TYPE", "auto")
    language: str = _env("VOXCRAFT_LANG", "ja")
    # デコードのビーム幅。1(貪欲)が最速、5が精度寄り。
    # GPUでは並列化されるため 5 でも速度はほぼ変わらず（実測 8秒音声で 1.15s→1.17s）、
    # 誤変換が目に見えて減る（意見交換会/脅威/陥らない 等）ので既定は 5。
    # CPU運用で遅い場合は VOXCRAFT_BEAM_SIZE=1 に下げる。
    beam_size: int = int(_env("VOXCRAFT_BEAM_SIZE", "5"))

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

    # --- チャンク連結（文字起こしモード専用。口述には一切かからない） ---
    # この長さに満たないチャンクは、次のチャンクと連結してから認識する。
    # 短いチャンクを単体で Whisper に渡すと定型句の幻覚が出るため（vad.ChunkJoiner 参照）。
    # 実測（VAIO発表会47.5分）: 1秒未満のチャンクは29.5%が「ご視聴ありがとうございました」。
    transcribe_join_sec: float = float(_env("VOXCRAFT_JOIN_SEC", "4.0"))
    # ただし実時間でこれ以上は次を待たない（孤立した短い発話を画面に出さないため）。
    # 表示の遅れはこの秒数が上限になる。
    transcribe_join_hold_sec: float = float(_env("VOXCRAFT_JOIN_HOLD_SEC", "2.0"))
    # これ以上の息継ぎをまたぐ連結はしない。繋いでしまうと「話の切れ目」が
    # チャンクの内側に埋もれ、段落分けの材料が消えるため。
    transcribe_join_break_sec: float = float(_env("VOXCRAFT_JOIN_BREAK_SEC", "2.0"))

    # --- 段落分け（文字起こしモード専用。ベタ打ち防止） ---
    # 「一定の字数を超えていて、かつ息継ぎがある所」で空行を入れる。
    # 秒数だけで決めないのは、マイクが遠いとVADが発話の途中で落ちて見かけの無音が
    # 増えるため（実測: 同じ2.0秒が、近接マイクの取材では6.8分で2回、遠いマイクの
    # 発表会では47.5分で269回。前者は改行がほぼ入らず、後者は48字ごとにブツ切れる）。
    # 字数を主、息継ぎを従にすると、どちらの録音でも100〜300字程度の段落に収まる。
    # 0 にすると段落分けをしない（従来どおりのベタ打ち）。
    paragraph_chars: int = int(_env("VOXCRAFT_PARA_CHARS", "120"))
    # 段落の切れ目として認める息継ぎの下限（秒）。
    paragraph_pause_sec: float = float(_env("VOXCRAFT_PARA_PAUSE", "0.7"))
    # 息継ぎが来ないまま伸びた場合に、息継ぎを問わず（文末で）改行する字数。
    paragraph_max_chars: int = int(_env("VOXCRAFT_PARA_MAX", "400"))
    # 文末すら来ないまま伸びた場合に、文の途中でも改行する字数。0 で上の2倍。
    paragraph_hard_chars: int = int(_env("VOXCRAFT_PARA_HARD", "0"))

    # --- 高速化・GPU最適化 ---
    # CTranslate2 の FlashAttention 有効化（RTX 30xx/40xx 等で速度向上）。
    flash_attention: bool = _env("VOXCRAFT_FLASH_ATTENTION", "0") == "1"

    # --- 後処理 ---
    # 日本語と英数字の間に入る半角スペースを除去する。
    strip_ja_alnum_space: bool = _env("VOXCRAFT_STRIP_SPACE", "1") == "1"
    # 記号読み上げ（「まる」→「。」など）を有効化する。
    enable_symbol_dictation: bool = _env("VOXCRAFT_SYMBOLS", "1") == "1"
    # 句読点の自動付与（sudachipy 形態素ルール）。既定ON。
    # kotoba-whisper は自然発話にほぼ句読点を打たないため、認識後テキストへ
    # 「。」「、」を自動挿入する（句読点を発話せずに済む）。sudachipy 未導入なら自動で無効。
    enable_auto_punctuation: bool = _env("VOXCRAFT_AUTO_PUNCT", "1") == "1"
    # 認識ヒント語(hotwords)を Whisper に渡すか。既定OFF。
    # 辞書が育って hotwords が長くなると kotoba-whisper が認識結果を丸ごと空にする
    # 不具合があるため（実測: hotwords 約120字超で全チャンクが脱落）。
    # 用語の表記ゆれは replacements（後処理の文字列置換）で安全に矯正できる。
    use_hotwords: bool = _env("VOXCRAFT_HOTWORDS", "0") == "1"

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
