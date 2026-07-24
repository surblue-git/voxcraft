"""認識結果テキストの後処理。

- 日本語と英数字の間の半角スペース除去（英語圏音声入力が勝手に入れる挙動の抑制）
- 記号読み上げ（「まる」「てん」「かいぎょう」等）の変換
- ユーザー辞書による置換（例: ウィンドウズ→Windows）
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

# 末尾トリム用（句読点・空白）。
_TRAIL = "。、 　\t"

# 「単独チャンク（少し間を置いて言った）」のときに記号化する読み。
# Whisper は単独の記号語を同音異義の漢字にしがち（かいぎょう→開業/会場/回教、
# てん→点、まる→丸/終わる）なので、それらの同音語も吸収する。
# 単独一致に限るため、本文中に紛れた同綴り（「かっこいい」「要点」等）は壊さない。
_STANDALONE: list[tuple[tuple[str, ...], str]] = [
    (("かいぎょう", "あたらしいぎょう", "改行", "開業", "会場", "回教"), "\n"),
    (("まる", "マル", "丸", "終わる"), "。"),
    (("てん", "テン", "点", "天"), "、"),
    (("かぎかっことじ",), "」"),
    (("かぎかっこ",), "「"),
    (("かっことじ",), "）"),
    (("かっこ",), "（"),
    (("びっくりまーく", "エクスクラメーション"), "！"),
    (("はてなまーく", "クエスチョン"), "？"),
    (("なかぐろ",), "・"),
    (("さんてん", "てんてん", "三点リーダー"), "…"),
    (("ころん",), "："),
    (("すらっしゅ",), "／"),
]

# 文末（間を置かず言い切った）でも変換する読み。誤爆が少ない綴りだけに絞る
# （末尾の「点」「開業」等は本物の語のことも多いので単独時のみに任せる）。
_ENDERS: list[tuple[tuple[str, ...], str]] = [
    (("まる", "マル", "丸"), "。"),
    (("てん", "テン"), "、"),
]


# 全角英数字・記号の一部 → 半角。「Ａトック」→「Aトック」等を辞書で拾えるように。
_FW_ASCII = {c: chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)}
_FW_ASCII[0x3000] = ord(" ")  # 全角スペース→半角


def normalize_fullwidth_ascii(text: str) -> str:
    """全角の英数字・記号を半角へ正規化する（日本語のかな漢字はそのまま）。"""
    return text.translate(_FW_ASCII)


def strip_ja_alnum_space(text: str) -> str:
    """日本語と英数字の間の半角スペースだけを除去する。

    英単語同士の間のスペース（例: "New York"）は残す。
    """
    return _JA_ALNUM_SPACE.sub("", text)


def apply_symbol_dictation(text: str, extra: dict[str, str] | None = None) -> str:
    """句読点・記号の読み上げを記号へ変換する（誤爆を避けるため限定的に）。

    方針:
      1. チャンク全体がその読みだけ（少し間を置いて言った）→ 記号に置換。
         extra（ユーザー登録の記号語 例 当点→、）も単独一致で使う。
      2. 文末が「まる/てん」等 → その文末読みだけを記号に置換。
    本文の途中に紛れた同綴りには反応しない（例「困る」「かっこいい」は無傷）。

    Whisper は句読点を自動付与もするため、これは明示的に言った場合の補助。
    """
    t = text.strip()
    if not t:
        return t

    bare = t.rstrip(_TRAIL)

    # 1) 単独チャンク一致（ユーザー登録の記号語を優先）。
    if extra and bare in extra:
        return extra[bare]
    for readings, sym in _ENDERS + _STANDALONE:
        if bare in readings:
            return sym

    # 2) 文末の文末記号（まる/てん）。
    for readings, sym in _ENDERS:
        for r in sorted(readings, key=len, reverse=True):
            if r in ("。", "、"):
                continue  # 既に記号ならそのまま
            if t.endswith(r):
                return t[: -len(r)].rstrip(_TRAIL) + sym

    return t


def apply_user_dict(text: str, replacements: list[tuple[str, str]]) -> str:
    """ユーザー辞書で置換する（例: ウィンドウズ→Windows）。

    replacements は (読み, 表記) のリスト。長いキーを先に適用する前提
    （呼び出し側で長さ降順にソート済みであること）。
    """
    for key, val in replacements:
        if key and key in text:
            text = text.replace(key, val)
    return text


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
    replacements: list[tuple[str, str]] | None = None,
    symbols: dict[str, str] | None = None,
) -> str:
    """確定チャンクに対する後処理をまとめて適用する。"""
    if not text:
        return text
    result = text.strip()
    if symbol_dictation:
        result = apply_symbol_dictation(result, symbols)
    # 辞書適用の前に全角英数を半角化（「Ａトック」→「Aトック」を拾えるように）。
    result = normalize_fullwidth_ascii(result)
    if replacements:
        result = apply_user_dict(result, replacements)
    if strip_space:
        result = strip_ja_alnum_space(result)
    result = collapse_symbols(result)
    return result
