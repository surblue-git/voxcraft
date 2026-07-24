"""認識結果テキストの後処理。

- 日本語と英数字の間の半角スペース除去（英語圏音声入力が勝手に入れる挙動の抑制）
- 記号読み上げ（「まる」「てん」「かいぎょう」等）の変換
- Whisper が付けがちな前後の空白・重複記号の整理

外部依存なしで動く（純粋な文字列処理）。単体テストしやすいように関数を分離。
"""
from __future__ import annotations

import re

# 日本語（ひらがな・カタカナ・漢字・全角記号など）の文字クラス。
_JA = (
    "　-〿"   # 全角句読点・記号
    "぀-ゟ"   # ひらがな
    "゠-ヿ"   # カタカナ
    "㐀-䶿"   # 漢字拡張A
    "一-鿿"   # CJK統合漢字
    "＀-￯"   # 全角英数・半角カナ
)
_ALNUM = "A-Za-z0-9"

# 日本語に隣接する半角スペース1個以上を捕捉:
#   日本語␣英数字 / 英数字␣日本語 / 日本語␣日本語。
# 英単語同士の間のスペース（例: "New York"）にはマッチしない。
_JA_ALNUM_SPACE = re.compile(
    f"(?<=[{_JA}]) +(?=[{_ALNUM}])"
    f"|(?<=[{_ALNUM}]) +(?=[{_JA}])"
    f"|(?<=[{_JA}]) +(?=[{_JA}])"
)

# 記号読み上げ辞書。長い読みを先に並べる（前方一致の取りこぼし防止）。
SYMBOL_MAP: list[tuple[str, str]] = [
    ("かいぎょう", "\n"),
    ("あたらしいぎょう", "\n"),
    ("かぎかっことじ", "」"),
    ("かぎかっこ", "「"),
    ("かっことじ", "）"),
    ("かっこ", "（"),
    ("びっくりまーく", "！"),
    ("はてなまーく", "？"),
    ("なかぐろ", "・"),
    ("さんてん", "…"),
    ("てんてん", "…"),
    ("くてん", "。"),
    ("まる", "。"),
    ("とうてん", "、"),
    ("ころん", "："),
    ("すらっしゅ", "／"),
    ("てん", "、"),
]


def strip_ja_alnum_space(text: str) -> str:
    """日本語と英数字の間の半角スペースだけを除去する。

    英単語同士の間のスペース（例: "New York"）は残す。
    """
    return _JA_ALNUM_SPACE.sub("", text)


def apply_symbol_dictation(text: str) -> str:
    """句読点・記号の読み上げを記号へ変換する。

    Whisper は句読点を自動付与するため、これは補助（明示的に「まる」等と言った場合）。
    """
    result = text
    for reading, symbol in SYMBOL_MAP:
        result = result.replace(reading, symbol)
    return result


def collapse_symbols(text: str) -> str:
    """重複した句読点・空白を整理する。"""
    text = re.sub(r"、{2,}", "、", text)
    text = re.sub(r"。{2,}", "。", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def postprocess(
    text: str,
    *,
    strip_space: bool = True,
    symbol_dictation: bool = False,
) -> str:
    """確定チャンクに対する後処理をまとめて適用する。"""
    if not text:
        return text
    result = text.strip()
    if symbol_dictation:
        result = apply_symbol_dictation(result)
    if strip_space:
        result = strip_ja_alnum_space(result)
    result = collapse_symbols(result)
    return result
