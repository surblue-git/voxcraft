"""AI校正の出力を、通してよいか機械で判定する。

なぜプロンプトではなく門なのか
------------------------------
「勝手に書き換えるな」とプロンプトへ書いても、守られたかどうかは分からない。
取材で使う以上、必要なのは**守られたことを機械で確かめる手段**のほう。
そこで校正前後のテキストだけを見て、通す／却下するを決める門をここに置く。

貫いている一つの規則
--------------------
    **消す方向は通し、作る方向は通さない。**

校正・言い換え・要約・幻覚のどれであっても、危ないのは「音に無かったものが
本文に現れること」だけである。落ちたぶんはカバレッジの門（§欠落）が別に見る。
だから数値も固有名詞も**包含（output ⊆ input）で判定する**。等しさは要求しない
（「一つ」→「ひとつ」で数値が消えるのは、事故ではない）。

門は4つ
-------
1. 読み保存    同音異義の訂正は定義上、読みを変えない。言い換え・幻覚は必ず変える。
2. 数値の保存  出力の数値は入力にあったものだけ（漢数字・単位つきも同じ値に畳む）。
3. 語の創作禁止 出力の固有名詞・未知語は、入力にあるか／用語集の正解か／
               **その読みが入力の読みに含まれる**もののみ。
               3つめが「スミシン→住信」のような読みどおりの訂正を許す。
4. 欠落        速報稿の大半を捨てていないか（`plugin/refinement.ts` と同じLCS判定）。

モードは「どの門をどの強さで開けるか」でしかない（`PROFILES`）。

形態素解析（Sudachi）が無いときは**校正しない**。読みの門が動かないまま通すのは、
門が無いのと同じで、しかもあるように見えるぶん危ない。sudachipy は
requirements.txt に入っている必須依存なので、これは通常起こらない。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable, Protocol, Sequence


# --- 形態素の入口 ---------------------------------------------------------

@dataclass(frozen=True)
class Term:
    """創作されると困る語（固有名詞・数詞・辞書に無い語）。"""

    surface: str
    reading: str


class Morphology(Protocol):
    """読みと語を返せるもの。テストは偽物を差し込む。"""

    @property
    def available(self) -> bool: ...

    def reading(self, text: str) -> str: ...

    def terms(self, text: str) -> list[Term]: ...


_KATA_TO_HIRA = str.maketrans({chr(c): chr(c - 0x60) for c in range(0x30A1, 0x30F7)})


class SudachiMorphology:
    """Sudachi を1回だけ読み込んで、読みと語の両方を返す。

    `reconvert` が既にトークナイザを持っていればそれを借りる（辞書の二重ロードは
    数百MB単位で効くため）。無ければ自分で作る。
    """

    def __init__(self) -> None:
        self._tokenizer = None
        try:
            import reconvert  # type: ignore

            self._tokenizer = reconvert.get_tokenizer()
        except Exception:
            self._tokenizer = None
        if self._tokenizer is None:
            try:
                from sudachipy import dictionary  # type: ignore

                self._tokenizer = dictionary.Dictionary(dict="core").create()
            except Exception:
                self._tokenizer = None

    @property
    def available(self) -> bool:
        return self._tokenizer is not None

    def _tokenize(self, text: str):
        from sudachipy import tokenizer as _t  # type: ignore

        return self._tokenizer.tokenize(text, _t.Tokenizer.SplitMode.C)

    def reading(self, text: str) -> str:
        """数詞を除いた読み。

        数詞を残すと、正しい表記換えが読みの違いに化ける。Sudachi は「2000」を
        「にれいれいれい」、「二千」を「にせん」と読むので、同じ値でも読みが
        揃わない。数値の同一性は §数値の門が値として厳密に見るので、ここでは
        数詞ごと落として、それ以外の音だけを比べる。
        """
        if self._tokenizer is None or not text:
            return ""
        out = []
        for m in self._tokenize(text):
            pos = m.part_of_speech()
            if len(pos) > 1 and pos[1] == "数詞":
                continue
            reading = m.reading_form()
            out.append(_kata_to_hira(reading) if reading else m.surface())
        return "".join(out)

    def terms(self, text: str) -> list[Term]:
        if self._tokenizer is None or not text:
            return []
        out: list[Term] = []
        for m in self._tokenize(text):
            surface = m.surface()
            if not _is_term_candidate(surface):
                continue
            pos = m.part_of_speech()
            # 数詞は数値の門が値として見る（「1,200億」は未知語かつ数詞なので、
            # 除いておかないと同じ誤りが number-invented と invented-term の
            # 両方で立ち、却下の内訳が読めなくなる）。
            if len(pos) > 1 and pos[1] == "数詞":
                continue
            proper = len(pos) > 1 and pos[1] == "固有名詞"
            oov = bool(m.is_oov())
            if not (proper or oov):
                continue
            reading = m.reading_form()
            out.append(Term(surface, _kata_to_hira(reading) if reading else surface))
        return out


def _kata_to_hira(text: str) -> str:
    return text.translate(_KATA_TO_HIRA)


_HIRAGANA_ONLY = re.compile(r"^[ぁ-ゟー、。・…！？!?\s]*$")


def _is_term_candidate(surface: str) -> bool:
    """ひらがなだけの語は「創作された固有名詞」になりにくいので見ない。

    未知語判定はひらがなの口語（「そうそう」等）を大量に拾うため、そこを数えると
    却下率がノイズで埋まる。危ないのは漢字・カタカナ・ラテン文字を含む語のほう。
    """
    if len(surface) < 2:
        return False
    return not _HIRAGANA_ONLY.match(surface)


# --- 読みの正規化 ---------------------------------------------------------

# 読みの比較で無視する差分。ここに挙げたものだけが「許容差分」で、
# それ以外の読みの違いは言い換え＝却下になる。
_READING_DROP = re.compile(
    r"[\s、。，．,.・：:；;！!？?「」『』（）()\[\]〔〕【】…～~\-—–"
    r"0-9０-９〇零一二三四五六七八九十百千万億兆]"
)

# 濁点・半濁点を落として比べる（どうおんいきご ≒ どうおんいぎご）。
_DAKUTEN = str.maketrans({
    "が": "か", "ぎ": "き", "ぐ": "く", "げ": "け", "ご": "こ",
    "ざ": "さ", "じ": "し", "ず": "す", "ぜ": "せ", "ぞ": "そ",
    "だ": "た", "ぢ": "ち", "づ": "つ", "で": "て", "ど": "と",
    "ば": "は", "び": "ひ", "ぶ": "ふ", "べ": "へ", "ぼ": "ほ",
    "ぱ": "は", "ぴ": "ひ", "ぷ": "ふ", "ぺ": "へ", "ぽ": "ほ",
    "ゔ": "う",
})

# 小書きを大きくする（きゃ ≒ きや、あった ≒ あつた）。
_SMALL = str.maketrans({
    "ぁ": "あ", "ぃ": "い", "ぅ": "う", "ぇ": "え", "ぉ": "お",
    "っ": "つ", "ゃ": "や", "ゅ": "ゆ", "ょ": "よ", "ゎ": "わ",
})

# 各かなの母音。長音の書き分け（とうきょう / とーきょー / とおきょお）を
# 1つの形へ畳むために要る。濁点と小書きを落としたあとの清音だけを持つ。
_VOWEL = {}
for _row, _vowel in (
    ("あかさたなはまやらわ", "あ"),
    ("いきしちにひみり", "い"),
    ("うくすつぬふむゆる", "う"),
    ("えけせてねへめれ", "え"),
    ("おこそとのほもよろを", "お"),
):
    for _kana in _row:
        _VOWEL[_kana] = _vowel


def _fold_long_vowels(text: str) -> str:
    """長音の三通りの書き方を1つへ畳む。

    「ー」は直前のかなの母音に開き、お段のあとの「う」は「お」、え段のあとの
    「い」は「え」に寄せる。そのうえで同じ字の連続を1つにする。
    とうきょう / とーきょー / とおきょお がすべて同じ形になる。
    """
    out: list[str] = []
    for ch in text:
        previous = out[-1] if out else ""
        vowel = _VOWEL.get(previous, "")
        if ch == "ー":
            if not vowel:
                continue
            out.append(vowel)
        elif ch == "う" and vowel == "お":
            out.append("お")
        elif ch == "い" and vowel == "え":
            out.append("え")
        else:
            out.append(ch)
    return re.sub(r"(.)\1+", r"\1", "".join(out))


def normalize_reading(reading: str) -> str:
    """許容差分を畳んだ読み。

    数字は**ここでは落とす**。数値の同一性は §数値の門が値として厳密に見るので、
    読みの側で「にせん」と「2000」を突き合わせようとしなくてよい。
    """
    text = unicodedata.normalize("NFKC", _kata_to_hira(reading))
    text = _READING_DROP.sub("", text)
    return _fold_long_vowels(text.translate(_DAKUTEN).translate(_SMALL))


def is_subsequence(needle: str, haystack: str) -> bool:
    """needle が haystack の（順序を保った）部分列か。"""
    it = iter(haystack)
    return all(ch in it for ch in needle)


# --- 数値の抽出 -----------------------------------------------------------

_KANJI_DIGIT = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_SMALL_UNIT = {"十": 10, "百": 100, "千": 1000}
_BIG_UNIT = {"万": 10**4, "億": 10**8, "兆": 10**12}

_NUM_CHARS = "0-9０-９〇零一二三四五六七八九十百千万億兆"
_NUMBER_RE = re.compile(
    rf"[{_NUM_CHARS}](?:[{_NUM_CHARS},，]|\.(?=[0-9０-９])|．(?=[0-9０-９]))*"
)


def _parse_number(token: str) -> Decimal | None:
    """`1,200億` `二千二十六` `1.5兆` を同じ土俵の値へ畳む。"""
    text = unicodedata.normalize("NFKC", token).replace(",", "")
    total = Decimal(0)
    section = Decimal(0)
    current: Decimal | None = None
    digits = ""

    def flush_digits() -> None:
        nonlocal current, digits
        if digits:
            try:
                current = Decimal(digits)
            except InvalidOperation:
                current = None
            digits = ""

    for ch in text:
        if ch.isdigit() or ch == ".":
            digits += ch
            continue
        flush_digits()
        if ch in _KANJI_DIGIT:
            current = Decimal(_KANJI_DIGIT[ch])
        elif ch in _SMALL_UNIT:
            section += (current if current is not None else Decimal(1)) * _SMALL_UNIT[ch]
            current = None
        elif ch in _BIG_UNIT:
            value = section + (current if current is not None else Decimal(0))
            # 「億」だけが単独で来た場合は 1億 と読む。
            if value == 0:
                value = Decimal(1)
            total += value * _BIG_UNIT[ch]
            section = Decimal(0)
            current = None
        else:
            return None
    flush_digits()
    total += section + (current if current is not None else Decimal(0))
    return total


def extract_numbers(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    for match in _NUMBER_RE.finditer(text):
        value = _parse_number(match.group(0))
        if value is not None:
            values.append(value)
    return values


def _multiset_extra(candidate: Iterable, reference: Iterable) -> list:
    """candidate のうち reference に無いぶん（多重度も見る）。"""
    remaining: dict = {}
    for item in reference:
        remaining[item] = remaining.get(item, 0) + 1
    extra = []
    for item in candidate:
        if remaining.get(item, 0) > 0:
            remaining[item] -= 1
        else:
            extra.append(item)
    return extra


# --- 欠落（LCSカバレッジ） -------------------------------------------------

_COMPARE_DROP = re.compile(r"[\s、。！？!?・：:；;,.「」『』（）()\[\]…]")


def _normalize_for_comparison(text: str) -> str:
    return _COMPARE_DROP.sub("", unicodedata.normalize("NFKC", text))


def _lcs_length(left: str, right: str) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    for i in range(1, len(left) + 1):
        current = [0] * (len(right) + 1)
        li = left[i - 1]
        for j in range(1, len(right) + 1):
            if li == right[j - 1]:
                current[j] = previous[j - 1] + 1
            else:
                current[j] = current[j - 1] if current[j - 1] >= previous[j] else previous[j]
        previous = current
    return previous[len(right)]


def _coverage(reference: str, candidate: str) -> float:
    if not reference:
        return 1.0
    return _lcs_length(reference, candidate) / len(reference)


# --- モード ---------------------------------------------------------------

@dataclass(frozen=True)
class GuardProfile:
    """モードは「どの門をどの強さで開けるか」でしかない。"""

    name: str
    label: str
    # "identical": 許容差分のみ / "subsequence": 消すのは可・作るのは不可 / "off"
    reading: str
    numbers: bool = True
    terms: bool = True
    # 欠落判定（0 で無効）。既定値は plugin/refinement.ts と同じ。
    min_length_ratio: float = 0.78
    min_edge_coverage: float = 0.40
    min_total_coverage: float = 0.45


# 取材: 生テキストが正。表記の訂正だけを許す。読みは変わってはならない。
INTERVIEW = GuardProfile(
    name="interview",
    label="取材（表記の訂正のみ）",
    reading="identical",
)

# 対話: 話したとおりを残しつつ、フィラーの削除だけ許す。作る方向は通さない。
CONVERSATION = GuardProfile(
    name="conversation",
    label="対話（フィラー除去は抑えめ）",
    reading="subsequence",
    min_length_ratio=0.70,
)

# 原稿執筆: フィラー除去と文体の整えを許す。読みの門は開けられないぶん、
# 数値・語の創作禁止と欠落ガードだけが残る（＝ここが唯一の防波堤になる）。
MANUSCRIPT = GuardProfile(
    name="manuscript",
    label="原稿執筆（フィラー除去・整形）",
    reading="off",
    min_length_ratio=0.55,
    min_edge_coverage=0.30,
    min_total_coverage=0.40,
)

PROFILES = {p.name: p for p in (INTERVIEW, CONVERSATION, MANUSCRIPT)}

# 却下理由。分布を見てプロンプトとモデルの良し悪しを決めるので、
# 「なぜ落ちたか」を1語で言えるようにしておく。
REASONS = (
    "morphology-unavailable",
    "reading-changed",
    "reading-added",
    "number-invented",
    "invented-term",
    "too-short",
    "leading-content-lost",
    "trailing-content-lost",
    "content-diverged",
)


@dataclass(frozen=True)
class GuardVerdict:
    ok: bool
    reasons: tuple[str, ...] = ()
    details: tuple[str, ...] = ()

    def __bool__(self) -> bool:  # `if verdict:` で通ったかを見られるように
        return self.ok


class RefineGuard:
    """校正前後のテキストだけを見て、通す／却下するを決める。"""

    def __init__(self, morphology: Morphology | None = None) -> None:
        self._morph = morphology if morphology is not None else SudachiMorphology()

    @property
    def available(self) -> bool:
        return bool(self._morph.available)

    def check(
        self,
        before: str,
        after: str,
        profile: GuardProfile = INTERVIEW,
        glossary: Sequence[str] = (),
    ) -> GuardVerdict:
        """`glossary` は用語集の**正解表記**。ここに載っている語だけは持ち込める。"""
        if not self._morph.available:
            return GuardVerdict(False, ("morphology-unavailable",),
                                ("Sudachi が無いので読みの門が動かない",))

        reasons: list[str] = []
        details: list[str] = []

        self._check_reading(before, after, profile, reasons, details)
        if profile.numbers:
            self._check_numbers(before, after, reasons, details)
        if profile.terms:
            self._check_terms(before, after, glossary, reasons, details)
        self._check_coverage(before, after, profile, reasons, details)

        return GuardVerdict(not reasons, tuple(reasons), tuple(details))

    # -- 個々の門 --

    def _check_reading(self, before, after, profile, reasons, details) -> None:
        if profile.reading == "off":
            return
        source = normalize_reading(self._morph.reading(before))
        result = normalize_reading(self._morph.reading(after))
        if profile.reading == "identical":
            if source != result:
                reasons.append("reading-changed")
                details.append(f"読みが変わった: {_clip(source)} → {_clip(result)}")
            return
        # subsequence: 消すのは通し、作るのは通さない。
        if not is_subsequence(result, source):
            reasons.append("reading-added")
            details.append(f"読みに無かった音が増えた: {_clip(result)}")

    def _check_numbers(self, before, after, reasons, details) -> None:
        extra = _multiset_extra(extract_numbers(after), extract_numbers(before))
        if extra:
            reasons.append("number-invented")
            details.append("入力に無い数値: " + ", ".join(str(v) for v in extra[:5]))

    def _check_terms(self, before, after, glossary, reasons, details) -> None:
        licensed = {_fold(t.surface) for t in self._morph.terms(before)}
        licensed |= {_fold(s) for s in glossary}
        # 用語集の項目も、同じ分かち書きで許可する。`NTTドコモ・フィナンシャル
        # グループ` を登録してあるのに、出力の `NTTドコモ` だけが未登録の語として
        # 落ちる——という取りこぼしを防ぐ（実測で起きた）。
        for entry in glossary:
            licensed |= {_fold(t.surface) for t in self._morph.terms(entry)}
        # 読みどおりの訂正は許す（スミシン → 住信）。入力の読みに無い音は作れない。
        source_reading = normalize_reading(self._morph.reading(before))

        invented = []
        for term in self._morph.terms(after):
            if _fold(term.surface) in licensed:
                continue
            reading = normalize_reading(term.reading)
            if reading and reading in source_reading:
                continue
            invented.append(term.surface)
        if invented:
            reasons.append("invented-term")
            details.append("入力にも用語集にも無い語: " + ", ".join(invented[:5]))

    def _check_coverage(self, before, after, profile, reasons, details) -> None:
        if profile.min_length_ratio <= 0:
            return
        source = _normalize_for_comparison(before)[:1200]
        result = _normalize_for_comparison(after)[:1200]
        if not source:
            return
        # 空にするのは、統計を待たずに却下してよい唯一の場合。
        if not result:
            reasons.append("too-short")
            details.append("出力が空になった")
            return
        # 短い入力に割合の門を当てても、比が意味を持たない。「スミシンの決算 →
        # 住信の決算」は7字→5字＝0.71で、正しい訂正なのに落ちてしまう。
        # LCS判定に足るだけの材料がある場合にだけ当てる（閾値は refinement.ts と同じ）。
        if len(source) < 24:
            return
        if len(result) / len(source) < profile.min_length_ratio:
            reasons.append("too-short")
            details.append(f"短くなりすぎ: {len(source)} → {len(result)}字")
            return

        edge = min(48, max(16, int(len(source) * 0.25)))
        if _coverage(source[:edge], result[: min(len(result), edge * 2)]) < profile.min_edge_coverage:
            reasons.append("leading-content-lost")
            details.append("先頭が失われた")
        if _coverage(source[-edge:], result[-min(len(result), edge * 2):]) < profile.min_edge_coverage:
            reasons.append("trailing-content-lost")
            details.append("末尾が失われた")
        if _coverage(source, result) < profile.min_total_coverage:
            reasons.append("content-diverged")
            details.append("全体が別物になった")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _clip(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


# --- 集計 -----------------------------------------------------------------

@dataclass
class GuardStats:
    """ベンチで使う集計。却下理由の分布が主指標になる。"""

    total: int = 0
    passed: int = 0
    reasons: dict = field(default_factory=dict)

    def add(self, verdict: GuardVerdict) -> None:
        self.total += 1
        if verdict.ok:
            self.passed += 1
        for reason in verdict.reasons:
            self.reasons[reason] = self.reasons.get(reason, 0) + 1

    @property
    def rejected(self) -> int:
        return self.total - self.passed

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0
