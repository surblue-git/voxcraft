"""認識結果テキストの後処理。

- 日本語と英数字の間の半角スペース除去（英語圏音声入力が勝手に入れる挙動の抑制）
- 記号読み上げ（「まる」「てん」「かいぎょう」等）の変換
- ユーザー辞書による置換（例: ウィンドウズ→Windows）
- Whisper が付けがちな前後の空白・重複記号の整理

外部依存なしで動く（純粋な文字列処理）。単体テストしやすいように関数を分離。
"""
from __future__ import annotations

import re
from typing import Mapping, Protocol, Sequence


class ReplacementApplier(Protocol):
    def apply(self, text: str) -> str: ...


ReplacementRules = Sequence[tuple[str, str]] | ReplacementApplier

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
    # 「かぎかっこ」は漢字化・カタカナ化が激しい（実測 2026-08-05:
    # 「かぎかっこ」→『鍵かっこ』、「かぎかっことじ」→『カギカッコトジ』）。
    # 他の記号語と同じく、観測した綴りをここへ足して吸収する。
    (("かぎかっことじ", "カギカッコトジ", "かぎ括弧閉じ", "カギカッコ閉じ",
      "鍵かっことじ", "鍵かっこ閉じ", "鍵括弧閉じ", "鉤括弧閉じ"), "」"),
    (("かぎかっこ", "カギカッコ", "かぎ括弧", "カギ括弧",
      "鍵かっこ", "鍵括弧", "鉤括弧"), "「"),
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


def apply_symbol_dictation(text: str, extra: Mapping[str, str] | None = None) -> str:
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


# 文の途中でも記号にする語。単独チャンク一致（apply_symbol_dictation）では拾えない。
#
# なぜ括弧だけ特別扱いが要るか
# ---------------------------
# 「まる」「てん」は文末で言うので自然に息継ぎが入り、単独チャンクになる。
# 括弧は**文の途中**で言って、そのまま中身を続けて喋る。息継ぎが無いので
# 前後の語と1チャンクに融合し、全体一致では永久に引っかからない。
# 実測 2026-08-05（すべて括弧を入力できなかった発話）:
#   「いつも鍵かっこ閉じ、ダメですね」  「このカギカッコ新バージョンは」
#   「ではかっこ、」                    「このバージョンカッコトジを試しましょう」
#
# 綴りではなく読みで照合するのは、表層が Whisper の気分で変わるため
# （鍵かっこ / 鍵カッコ / カギカッコ / 鍵括弧 → 読みはすべて カギカッコ）。
# 形態素の切れ目でしか照合しないので、語の途中を割ることはない。
#
# (読みの候補, 記号, かな表記に限るか, カタカナ連続の内側でも探すか)
#
# 最後の項目が要るのは、sudachi が連続するカタカナを1語の未知語にまとめるため
# （実測: 「バージョンカッコトジ」が丸ごと1形態素）。内側まで探さないと拾えないが、
# 素の「カッコ」で内側を探すと「カッコウ」の前半に当たってしまう。長くて紛れの無い
# 読みだけ内側を許す。
_INLINE_SYMBOLS: list[tuple[tuple[str, ...], str, bool, bool]] = [
    # 「とじ」が「とし」に濁点落ちする実例（『鍵かっことし』）も吸収する。
    (("カギカッコトジ", "カギカッコトシ"), "」", False, True),
    (("カギカッコ",), "「", False, True),
    (("カッコトジ", "カッコトシ"), "）", False, True),
    # 素の「カッコ」は同音語が多い。かな表記のときだけ記号にして、
    # 漢字で書かれた「括弧」「確固」は本物の語として温存する。
    # 「かっこいい」(カッコイイ) 「格好」(カッコウ) は読みが違うので元から当たらない。
    (("カッコ",), "（", True, False),
]
# 照合する形態素の最大連結数（カギ＋カッコ＋ト＋シ ＝ 4）。
_INLINE_MAX_RUN = 4

_KANA_RE = re.compile(r"^[ぁ-んァ-ヶー]+$")
_KATAKANA_RE = re.compile(r"^[ァ-ヶー]+$")

# カタカナ列の内側で一致しても、直後がこれらなら語の途中を切っている。
# 形態素の切れ目が無い（＝1語にまとめられた）ため、ここだけは音で判断する。
# 実測 2026-08-05:「かぎかっこ・かっこうの許嫁」が『カギカッコー…』『カギカッコウ…』と
# 1語になり、カギカッコ を取ると『「ー…』『「ウ…』と本文の語を食っていた。
_INLINE_CONTINUATION = "ーウゥ"


def _convert_katakana_run(run: str) -> str:
    """1語にまとめられたカタカナ列の内側から記号語を切り出す。

    カタカナ列は表層＝読みなので、位置合わせの心配なくそのまま照合できる。
    """
    out: list[str] = []
    index = 0
    while index < len(run):
        hit: tuple[str, int] | None = None
        # _INLINE_SYMBOLS は読みの長い順なので、最初の一致が最長一致になる。
        for readings, symbol, _kana_only, inside_run in _INLINE_SYMBOLS:
            if not inside_run:
                continue
            for reading in readings:
                if not run.startswith(reading, index):
                    continue
                tail = index + len(reading)
                # 直後が長音・ウ なら、実際は もっと長い語（カッコー/カッコウ）。
                # 判断が付かないので変換しない（本文を食うより残す側に倒す）。
                if tail < len(run) and run[tail] in _INLINE_CONTINUATION:
                    continue
                hit = (symbol, len(reading))
                break
            if hit:
                break
        if hit:
            out.append(hit[0])
            index += hit[1]
            continue
        out.append(run[index])
        index += 1
    return "".join(out)


def apply_inline_symbol_words(text: str) -> str:
    """文の途中にある記号語（括弧）を記号へ置き換える。sudachi 未導入なら no-op。"""
    if not text:
        return text
    from punctuate import morphemes

    morphs = morphemes(text)
    if not morphs:
        return text

    out: list[str] = []
    index = 0
    total = len(morphs)
    while index < total:
        hit: tuple[str, int] | None = None
        # 長い連結から試す（「カギカッコトジ」を「カギカッコ」より先に取る）。
        for length in range(min(_INLINE_MAX_RUN, total - index), 0, -1):
            run = morphs[index:index + length]
            reading = "".join(reading for _, reading in run)
            surface = "".join(surface for surface, _ in run)
            for readings, symbol, kana_only, _inside in _INLINE_SYMBOLS:
                if reading not in readings:
                    continue
                if kana_only and not _KANA_RE.match(surface):
                    continue
                hit = (symbol, length)
                break
            if hit:
                break
        if hit:
            out.append(hit[0])
            index += hit[1]
            continue
        surface = morphs[index][0]
        # 形態素として一致しなくても、カタカナの塊なら内側に埋もれていることがある。
        out.append(_convert_katakana_run(surface) if _KATAKANA_RE.match(surface) else surface)
        index += 1
    return "".join(out)


def apply_user_dict(text: str, replacements: ReplacementRules) -> str:
    """ユーザー辞書で置換する（例: ウィンドウズ→Windows）。

    Phase 1B の ReplacementPlan なら最長一致・単一走査で適用する。
    後方互換として従来の (読み, 表記) リストも受け付ける。
    """
    apply_once = getattr(replacements, "apply", None)
    if callable(apply_once):
        return apply_once(text)
    for key, val in replacements:
        if key and key in text:
            text = text.replace(key, val)
    return text


# 文末に勝手に挿入されがちな幻覚パターン。
# 「〜ございますありがとうございます」のように、句読点なしで本文へ癒着した定型句だけを狙う。
# 句読点で正しく区切られた「。ありがとうございました。」は本物の締めの挨拶のことが多いので
# 消さない（口述ツールでユーザーの発話を無言で落とさない＝安全側）。
_TRAILING_HALLUCINATIONS = [
    re.compile(r"(?<=ございます)ありがとうございます[。、]?$"),
    re.compile(r"(?<=ございます)ご視聴ありがとうございました[。、]?$"),
]


def strip_trailing_hallucinations(text: str) -> str:
    """文末に勝手に付加される「ありがとうございます」等の定型幻覚を除去する。"""
    for pattern in _TRAILING_HALLUCINATIONS:
        text = pattern.sub("", text)
    return text.strip()


class ParagraphBreaker:
    """文字起こしの本文を段落に割る（チャンクの前に空行を入れるかを決める）。

    なぜ「秒数だけ」で決めないか
    ---------------------------
    「N秒の無音で改行」は一見自然だが、しきい値が録音をまたいで通用しない。
    マイクが遠いと VAD が発話の途中で落ちるので、見かけの無音が長く・頻繁になるため。
    実測（2026-07-30）で同じ 2.0秒が:

        近接マイクの取材（6.8分）  : 2回   → ほとんど改行されない（1段落 約950字）
        遠いマイクの発表会（47.5分）: 269回 → 48字ごとにブツ切れる

    そこで**字数を主・息継ぎを従**にする。「min_chars を超えていて、かつ
    pause_sec 以上の息継ぎがある所」で改行し、息継ぎが来ないまま max_chars まで
    伸びたら区切りが無くても改行する。この規則だと上の2本とも 100〜300字程度に収まる
    （実測: 発表会 平均139字、取材 平均287字）。

    区切りはチャンクの境目にしか置けないので、直前のチャンクが文の途中で終わって
    いるときは見送って次の機会を待つ（「発想の変化と／いうところになっていて」のような
    割れ方を防ぐ）。判定は3段階:

        1. min_chars を超え、pause_sec 以上の息継ぎがあり、文が終わっている → 切る
        2. max_chars を超え、文が終わっている → 息継ぎは問わず切る
        3. hard_chars を超えた → 文の途中でも切る（最後の砦）

    min_chars=0 で無効（従来どおりのベタ打ち）。
    """

    # 文が終わっているとみなす文字。
    _SENTENCE_END = "。！？!?」』）"

    def __init__(
        self,
        min_chars: int = 120,
        pause_sec: float = 0.7,
        max_chars: int = 400,
        hard_chars: int = 0,
    ):
        self.min_chars = min_chars
        self.pause_sec = pause_sec
        self.max_chars = max_chars
        # 0 なら max_chars の2倍。文末が来ないまま延々と続くのを防ぐだけの値なので、
        # 通常は使われない（実測では原稿の読み上げでも max_chars 側で切れる）。
        self.hard_chars = hard_chars or max_chars * 2
        self._chars = 0
        self._ends_sentence = False

    def feed(self, text: str, pause: float | None) -> str:
        """このチャンクの前に入れる区切り（"" か "\\n\\n"）を返し、字数を進める。"""
        if not text:
            return ""
        sep = ""
        if self.min_chars > 0 and self._chars > 0:
            paused = self._chars >= self.min_chars and (pause or 0.0) >= self.pause_sec
            overflow = self._chars >= self.max_chars
            if ((paused or overflow) and self._ends_sentence) or self._chars >= self.hard_chars:
                sep = "\n\n"
                self._chars = 0
        self._chars += len(text)
        self._ends_sentence = text.rstrip()[-1:] in self._SENTENCE_END
        return sep


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
    inline_symbols: bool = False,
    replacements: ReplacementRules | None = None,
    symbols: Mapping[str, str] | None = None,
    auto_punctuate: bool = False,
) -> str:
    """確定チャンクに対する後処理をまとめて適用する。"""
    if not text:
        return text
    result = text.strip()
    result = strip_trailing_hallucinations(result)
    # 認識結果の全角英数を先に半角化する（「Ａトック」→「Aトック」を辞書で拾えるように）。
    # 記号化より前に置くのが要点。後ろに置くと、記号読み上げが出した全角記号
    # （（）！？：／ は U+FF01〜FF5E に入る）を直後に半角へ潰してしまい、
    # 「かっこ」と言ったのに ( になる。「 」 は別ブロックなので影響を受けない。
    result = normalize_fullwidth_ascii(result)
    if symbol_dictation:
        result = apply_symbol_dictation(result, symbols)
    # 単独チャンクとして拾えなかった括弧を、文の途中からも拾う（口述のみ）。
    if inline_symbols:
        result = apply_inline_symbol_words(result)
    if replacements:
        result = apply_user_dict(result, replacements)
    if strip_space:
        result = strip_ja_alnum_space(result)
    # 句読点の自動付与（sudachi 形態素ルール。未導入なら no-op）。辞書適用後に行う。
    if auto_punctuate:
        from punctuate import add_punctuation
        result = add_punctuation(result)
    result = collapse_symbols(result)
    return result
