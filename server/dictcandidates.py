"""ノート1本から、辞書へ登録する候補（誤認識 → 正しい表記）を絞り込む。

なぜ要るか
----------
辞書登録で時間を食うのは入力ではなく、**その語が何なのかを知ること**。そして
取材ノートでは、正解は既に同じファイルの中に手で書かれている ——
録音の文字起こし本文とは別に、あなたが要約や見出しを書くから。

  本文: 「最近パスピーが流行っていますので」   ← 認識が壊した
  要約: 「パスキー」                          ← あなたが正しく書いた

この2つを突き合わせれば `パスピー → パスキー` が機械的に出る。

ただし素朴に突き合わせると、欲しいもの以外が大量に混ざる。実測（2026-08-21・
デジ庁会見のノート）で出てきた4種類:

  1. 真の誤認識    パスピー → パスキー                    ← これだけが欲しい
  2. 省略          マイナンバーカード → マイナカード      ← 登録すると本文が潰れる
  3. 言い換え      愛称番号 → PIN                         ← 同上
  4. 切り出しゴミ  以上 → 万以上                          ← 無害だが量が多い

**要約は「その語が実在する」ことを教えるものであって、「どう書くか」を決めるもの
ではない。** 略称も言い換えも要約には当たり前に出るので、そのまま右辺にはできない。

どう切るか
----------
門1（本文の自己整合）: 誤認識側の綴りが**正解側にも書かれている**なら、それは
    誤りではない。「マイナンバーカード」は要約にも7回出ているので、これを
    「マイナカード」へ倒す登録は本文29箇所を壊す。ここで落とす。
    併せて、正しい表記が誤認識側を**短くしただけ**（部分列）なら省略として落とす。

門2（音の距離）: 読みが遠すぎるものは言い換え。「愛称番号／PIN」は 0.18 で落ちる。
    **音の近さだけでは省略と誤認識を分離できない**ことに注意（実測で省略 0.80 が
    本物の誤認識 アジアティックコーナース→エージェンティックコマース 0.64 より高い）。
    だから門2は言い換えの足切り専用で、省略は門1に任せる。

残ったものは人間（またはLLM）が見る前提の候補で、確定ではない。登録の直前には
プラグイン側のプレビュー（plugin/dictpreview.ts）がもう一度、何箇所どう変わるかを出す。

使い方
------
  python dictcandidates.py "path/to/取材メモ.md"
  python dictcandidates.py ノート.md --terms 配布資料.txt --tsv 候補.tsv
  python dictcandidates.py ノート.md --min-ratio 0.5 --top 40
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

# 要約セクションの目印。ユーザーのノートは録音アンカーの時刻表記から手書きが始まる。
SUMMARY_MARK_RE = re.compile(r"%%\s*wx\s*\d+:\d+\s*%%")
FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)

# 語として繋いでよい品詞。名詞の連続だけを複合語とみなす。
_WORD_POS = {"名詞", "接頭辞"}
_SKIP_SURFACE_RE = re.compile(r"\A[\s、。！？!?…・「」『』（）()\[\]【】\-—ー~〜:：/／%]*\Z")

# 候補になり得る語の見た目。「という形」のような一般語を落とし、固有名詞・専門用語
# だけを残すための条件。かなだけの語と数字だけの語は誤爆が多いので除く。
_TERM_LIKE_RE = re.compile(r"[ァ-ヴー]{3,}|[A-Za-z]{2,}|[一-龥]{2,}")
# 数を含む語は辞書に入れない。「300以上→600以上」のような、直しようのない
# （そして直してはいけない）組が上位を占めるため。
_HAS_DIGIT_RE = re.compile(r"[0-9０-９]")

MIN_TERM_LEN = 3
MAX_TERM_LEN = 24
MAX_NGRAM = 4


def is_term_like(surface: str, reading: str) -> bool:
    """辞書に登録する価値のある語の見た目か。

    数字と一般語を落とすのが目的。実測（デジ庁会見のノート）でこの条件を入れる前は
    候補が9,579件になり、上位を「いう形→見やすいかたち」「Thank you.→1439」の
    ような組が占めていた。
    """
    if not (MIN_TERM_LEN <= len(surface) <= MAX_TERM_LEN):
        return False
    if _HAS_DIGIT_RE.search(surface):
        return False
    return bool(_TERM_LIKE_RE.search(surface))


@dataclass(frozen=True)
class Candidate:
    observed: str          # 本文に出ている（壊れた）綴り
    output: str            # 正しいと思われる綴り
    hits: int              # 本文での出現回数
    ratio: float           # 読みの一致率
    contexts: tuple[str, ...]
    ambiguous: bool = False   # 同じ observed に複数の正解候補が当たった

    @property
    def score(self) -> float:
        """直る量。同じ確からしさなら、たくさん出る語を上に出す。"""
        return self.hits * len(self.output) * self.ratio


# --- 読み ------------------------------------------------------------------

def reading_of(text: str) -> str:
    """読み（カタカナ）。sudachi が無い/取れない場合は表層をそのまま使う。"""
    from punctuate import to_reading

    return to_reading(text) or text


def sound_ratio(a: str, b: str) -> float:
    """読みの一致率。表層ではなく音で比べる（誤認識は音が残るため）。"""
    return SequenceMatcher(None, a, b).ratio()


# 部分列というだけでは省略と誤認識を分けられない。認識は1〜2文字を**足す**壊し方を
# よくするので（マイナアプリ→マイナ「ー」アプリ、マイナポータル→マイナ「ップ」ポータル）、
# それを省略と呼ぶと本物の誤認識を落としてしまう。実測（デジ庁会見の候補33件をLLMに
# 判定させたとき）、この条件が無いと採用すべき4件を「省略」として捨てていた。
# 一方、本物の省略は3文字以上まとめて落ちる（マイナ「ンバー」カード、「デジタル」認証アプリ）。
ABBREVIATION_MIN_DROP = 3


def looks_like_abbreviation(observed: str, output: str) -> bool:
    """output が observed を短くしただけか（「マイナンバーカード」→「マイナカード」）。

    plugin/dictpreview.ts の looksLikeAbbreviation と同じ規則。片方だけ直すと
    「CLIは出すのにUIは警告する」というねじれになるので、変えるときは両方直すこと。
    """
    if not observed or not output:
        return False
    if len(observed) - len(output) < ABBREVIATION_MIN_DROP:
        return False
    i = 0
    for ch in observed:
        if i < len(output) and ch == output[i]:
            i += 1
        if i == len(output):
            return True
    return False


# --- ノートの切り分けと語の取り出し ----------------------------------------

def split_note(text: str) -> tuple[str, str]:
    """ノートを (文字起こし本文, 手書きの正解側) に割る。

    目印は要約に書かれる録音アンカーの時刻表記（%%wx 0:00%%）。見つからなければ
    正解側は空で返し、呼び出し側が --terms を要求する。位置を推測して黙って
    間違えるより、無いと言うほうがよい。
    """
    body = FRONTMATTER_RE.sub("", text)
    m = SUMMARY_MARK_RE.search(body)
    if not m:
        return body, ""
    return body[:m.start()], body[m.start():]


# sudachi は入力が長すぎると例外を投げる（内部上限 49149バイト）。取材ノートの
# 本文は1時間で6万バイトを超えるので、必ず刻んでから渡す。刻まないと morphemes()
# が None を返し、**候補が黙って0件になる**（実装中に踏んだ）。
_TOKENIZER_MAX_BYTES = 20000


def _split_for_tokenizer(text: str, limit: int = _TOKENIZER_MAX_BYTES) -> list[tuple[str, int]]:
    """(断片, 元テキスト上の開始位置) に割る。語をまたいで切らないよう境界を選ぶ。"""
    pieces: list[tuple[str, int]] = []
    start = 0
    while start < len(text):
        end = start
        size = 0
        cut = -1
        while end < len(text):
            size += len(text[end].encode("utf-8"))
            if size > limit:
                break
            if text[end] in "\n。！？":
                cut = end + 1
            end += 1
        if end >= len(text):
            cut = len(text)
        elif cut <= start:
            cut = end                       # 区切りが無ければ仕方なく途中で切る
        pieces.append((text[start:cut], start))
        start = cut
    return pieces


def _tokens(text: str) -> list[tuple[str, str, int, bool]]:
    """(表層, 読み, 開始位置, 語になれるか) の列。

    「語になれるか」は品詞が名詞系かどうか。名詞の連続だけを語として繋ぐ。
    助詞や動詞を巻き込むと「という形」「のマイナー」のようなキーが候補に出て、
    登録すれば関係ない場所でも置換が起きるため。
    """
    from punctuate import morphemes_pos

    out: list[tuple[str, str, int, bool]] = []
    for piece, offset in _split_for_tokenizer(text):
        parsed = morphemes_pos(piece)
        if parsed is None:      # sudachi 未導入なら語を作れない
            return []
        pos = 0
        for surface, reading, pos0 in parsed:
            at = piece.find(surface, pos)
            if at < 0:
                at = pos
            pos = at + len(surface)
            if _SKIP_SURFACE_RE.match(surface):
                continue
            out.append((surface, reading or surface, offset + at, pos0 in _WORD_POS))
    return out


def ngrams(text: str, *, max_n: int = MAX_NGRAM) -> list[tuple[str, str, int]]:
    """連続する形態素をつないだ (表層, 読み, 開始位置) を列挙する。

    語の切れ目を形態素に合わせるのは、途中で切ったキーを登録すると関係ない
    場所でも置換が起きるため（実測: 「マイナー」は本文27箇所に当たり、その中の
    「マイナーバー」「マイナーアプリ」は直したい語が別だった）。
    """
    toks = _tokens(text)
    out: list[tuple[str, str, int]] = []
    for i in range(len(toks)):
        if not toks[i][3]:
            continue
        surface = ""
        reading = ""
        end = -1
        for n in range(max_n):
            if i + n >= len(toks):
                break
            s, r, at, is_word = toks[i + n]
            if not is_word:
                break
            # 元テキスト上で隣接していない（記号や改行を挟んだ）なら繋げない。
            if n and at != end:
                break
            surface += s
            reading += r
            end = at + len(s)
            if is_term_like(surface, reading):
                out.append((surface, reading, toks[i][2]))
    return out


def terms_from(text: str) -> Counter:
    """正解側テキストに出てくる語（表層）と出現回数。"""
    counts: Counter = Counter()
    for surface, _reading, _at in ngrams(text):
        counts[surface] += 1
    return counts


# --- 候補づくり -------------------------------------------------------------

def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def find_candidates(
    transcript: str,
    correct_text: str,
    *,
    extra_terms: Counter | None = None,
    known_good: frozenset[str] = frozenset(),
    known_pairs: frozenset[tuple[str, str]] = frozenset(),
    min_ratio: float = 0.7,
    max_contexts: int = 2,
    context_chars: int = 16,
) -> list[Candidate]:
    """本文の綴り × 正解側の語 の総当たりから、門1・門2を通ったものだけ返す。

    known_good は「辞書が正しいと宣言済みの表記」（既存エントリの output）。
    正解側テキストに書かれていなくても、一度正しいと決めた綴りは誤認識として
    出さない。門1は正解側テキストにある語しか守れないので、その穴を辞書で埋める。
    known_pairs は登録済みの (誤認識, 正しい表記)。もう入っているものは出さない。
    """
    terms = terms_from(correct_text)
    if extra_terms:
        terms.update(extra_terms)
    spans = ngrams(transcript)
    if not terms or not spans:
        return []

    # 読みのバイグラムで粗く絞ってから読みを比べる（総当たりだと数千万件になる）。
    index: dict[str, set[int]] = defaultdict(set)
    for i, (_s, reading, _at) in enumerate(spans):
        for bg in _bigrams(reading):
            index[bg].add(i)

    best: dict[str, tuple[float, str]] = {}
    others: dict[str, set[str]] = defaultdict(set)
    for term in terms:
        term_reading = reading_of(term)
        # 短い語は共有バイグラムが元々少ない。2個を要求すると「パスピー／パスキー」
        # （共有は「パス」だけ）が絞り込みの段階で消える。長さで要求を変える。
        min_shared = 1 if len(term_reading) <= 6 else 2
        near: Counter = Counter()
        for bg in _bigrams(term_reading):
            for i in index.get(bg, ()):
                near[i] += 1
        for i, shared in near.items():
            if shared < min_shared:
                continue
            surface, reading, _at = spans[i]
            if surface == term:
                continue                                   # 既に正しい
            if not 0.6 <= len(reading) / max(len(term_reading), 1) <= 1.7:
                continue
            ratio = sound_ratio(reading, term_reading)
            if ratio < min_ratio:
                continue                                   # 門2: 言い換え
            if surface in correct_text or surface in known_good:
                continue                                   # 門1: その綴りは正しい
            if (surface, term) in known_pairs:
                continue                                   # 既に登録済み
            if looks_like_abbreviation(surface, term):
                continue                                   # 門1: ただの省略
            previous = best.get(surface)
            if previous is None or ratio > previous[0]:
                if previous is not None:
                    others[surface].add(previous[1])
                best[surface] = (ratio, term)
            elif term != best[surface][1]:
                others[surface].add(term)

    out: list[Candidate] = []
    for surface, (ratio, term) in best.items():
        hits = _count(transcript, surface)
        if not hits:
            continue
        out.append(Candidate(
            observed=surface,
            output=term,
            hits=hits,
            ratio=ratio,
            contexts=tuple(_contexts(transcript, surface, max_contexts, context_chars)),
            ambiguous=_is_ambiguous(term, others.get(surface, set())),
        ))
    out.sort(key=lambda c: (-c.score, c.observed))
    return out


def _is_ambiguous(chosen: str, rivals: set[str]) -> bool:
    """本当に別の語が競合しているときだけ True。

    「マイナポータル」と「マイナポータルアプリ」のように片方が他方を含むだけの
    競合は、同じ語の長さ違いなので警告しない。全部に印が付くと印の意味が消える。
    """
    return any(r not in chosen and chosen not in r for r in rivals)


def _count(text: str, needle: str) -> int:
    """重なりなしの出現回数（サーバーの置換と同じ数え方）。"""
    if not needle:
        return 0
    total = 0
    at = text.find(needle)
    while at >= 0:
        total += 1
        at = text.find(needle, at + len(needle))
    return total


def _contexts(text: str, needle: str, limit: int, width: int) -> list[str]:
    out: list[str] = []
    at = text.find(needle)
    while at >= 0 and len(out) < limit:
        lo = max(0, at - width)
        hi = min(len(text), at + len(needle) + width)
        head = "…" if lo > 0 else ""
        tail = "…" if hi < len(text) else ""
        body = text[lo:at] + f"【{needle}】" + text[at + len(needle):hi]
        out.append(head + body.replace("\n", " ") + tail)
        at = text.find(needle, at + len(needle))
    return out


# --- 出力 -------------------------------------------------------------------

def render(cands: list[Candidate], *, top: int) -> str:
    lines = [f"# 辞書候補 {len(cands)}件（上位{min(top, len(cands))}件を表示）", ""]
    lines.append("| 本文の綴り | 正しいと思われる表記 | 本文 | 読み一致 | 注意 |")
    lines.append("|---|---|---:|---:|---|")
    for c in cands[:top]:
        warn = "**同じ綴りに複数の候補**" if c.ambiguous else ""
        if c.hits >= 5 and not warn:
            warn = "多数に当たる（キーが広すぎないか確認）"
        lines.append(f"| {c.observed} | {c.output} | {c.hits} | {c.ratio:.2f} | {warn} |")
    lines.append("")
    for c in cands[:top]:
        lines.append(f"### {c.observed} → {c.output}")
        for ctx in c.contexts:
            lines.append(f"- {ctx}")
        lines.append("")
    return "\n".join(lines)


PROMPT_HEADER = """\
あなたは日本語音声認識の誤りを見分ける校正者です。以下は取材の録音を文字起こしした
本文と、記者が手で書いた要約から機械的に作った「辞書登録の候補」です。
機械は音の近さでしか見ていないので、正しくない組が混ざっています。人手で使える形に
選別してください。

## 判断の基準

候補は必ず次の4種類のどれかです。**1だけが採用**で、残りは却下します。

1. 誤認識   — 認識が壊した綴り。例「パスピー」→「パスキー」。**これだけが採用**
2. 省略     — 要約で短く書いただけ。例「マイナンバーカード」→「マイナカード」。
              登録すると本文の正しい表記まで潰れるので**却下**
3. 言い換え — 話し言葉を書き言葉に直しただけ。例「愛称番号」→「PIN」。**却下**
4. 無関係   — たまたま音が似ているだけの別の語。**却下**

## 守ること

- **正しい表記は、下の「正解側テキスト」に実際に書かれている綴りだけを使う。**
  そこに無い綴りを創作しない。思いついた表記があっても、根拠が無ければ却下する。
- 機械が付けた「正しいと思われる表記」が**間違っていることがある**。文脈を読んで、
  正解側テキストの中の別の語のほうが合うなら、そちらへ直してよい。
- **本文そのものを書き直さない。** 出力は登録する組だけ。
- 迷ったら却下する。取りこぼしは次の取材で拾えるが、誤った登録は静かに本文を壊す。
- 「本文」の数字は、その綴りが本文に出てくる回数。多い綴りほど、誤って登録したときの
  被害が大きい。回数が多いものは特に慎重に。

## 出力

JSON配列だけを返してください。説明文は不要です。

[{"observed": "本文の綴り", "output": "正しい表記", "verdict": "採用", "reason": "短い理由"}]

verdict は "採用" か "却下"。却下したものも理由を付けて返してください。
"""


def build_prompt(
    cands: list[Candidate],
    correct_text: str,
    *,
    materials: str = "",
    top: int = 60,
) -> str:
    """外部LLMへ渡す照合プロンプトを組む。

    **音声は渡さない。** 取材の音そのものを外部へ出さずに済むのがこの設計の要点で、
    LLMに任せるのは「この崩れた綴りは、資料のどの語か」という照合だけ。それなら
    テキストで足りるし、LLMが本当に強いのもそこ。

    返ってきた答えは apply_verdicts() で必ず検証する。プロンプトで禁じただけでは、
    存在しない綴りを創作する余地が残るため。
    """
    lines = [PROMPT_HEADER, "", "## 正解側テキスト（記者が手で書いたもの）", "",
             correct_text.strip(), ""]
    if materials.strip():
        lines += ["## 配布資料", "", materials.strip(), ""]
    lines += ["## 候補", ""]
    for c in cands[:top]:
        lines.append(f"- observed=「{c.observed}」 / 機械の推測=「{c.output}」 / "
                     f"本文{c.hits}回 / 読み一致{c.ratio:.2f}"
                     + ("（別の語とも競合）" if c.ambiguous else ""))
        for ctx in c.contexts:
            lines.append(f"    - {ctx}")
    return "\n".join(lines)


def apply_verdicts(
    raw: str,
    *,
    transcript: str,
    allowed_text: str,
    known_good: frozenset[str] = frozenset(),
    known_pairs: frozenset[tuple[str, str]] = frozenset(),
    min_ratio: float = 0.7,
) -> tuple[list[tuple[str, str]], list[str]]:
    """LLMの答えを検証し、(採用する組, 落とした理由) を返す。

    LLMの出力をそのまま信じない。ここで落とすのは:
      - 本文に無い observed（存在しない誤りを直そうとしている）
      - 正解側テキストに書かれていない output（**綴りの創作**。これが一番危ない）
      - 省略・音が遠すぎる組（門1・門2をもう一度通す）
      - 既に登録済みの組
    プロンプトで禁じることと、機械で確かめることは別。
    """
    import json

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], [f"JSONとして読めません: {exc}"]
    if not isinstance(parsed, list):
        return [], ["JSON配列ではありません"]

    accepted: list[tuple[str, str]] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, dict):
            rejected.append(f"要素がオブジェクトではありません: {item!r:.60}")
            continue
        observed = str(item.get("observed", "")).strip()
        output = str(item.get("output", "")).strip()
        verdict = str(item.get("verdict", "")).strip()
        if verdict != "採用":
            continue
        if not observed or not output or observed == output:
            rejected.append(f"組として成立しません: {observed} → {output}")
            continue
        if observed in seen:
            rejected.append(f"同じ observed が重複: {observed}")
            continue
        if observed not in transcript:
            rejected.append(f"本文に存在しない綴り: {observed}")
            continue
        if output not in allowed_text:
            rejected.append(f"正解側に無い表記を作っています: {observed} → {output}")
            continue
        if observed in known_good:
            rejected.append(f"辞書が正しいと決めた綴りです: {observed}")
            continue
        if (observed, output) in known_pairs:
            rejected.append(f"登録済み: {observed} → {output}")
            continue
        if looks_like_abbreviation(observed, output):
            rejected.append(f"省略です（本文を潰します）: {observed} → {output}")
            continue
        ratio = sound_ratio(reading_of(observed), reading_of(output))
        if ratio < min_ratio:
            rejected.append(f"音が遠すぎます（{ratio:.2f}）: {observed} → {output}")
            continue
        seen.add(observed)
        accepted.append((observed, output))
    return accepted, rejected


def write_tsv(cands: list[Candidate], path: Path) -> None:
    rows = ["\t".join(["observed", "output", "hits", "ratio", "ambiguous", "context"])]
    for c in cands:
        rows.append("\t".join([
            c.observed, c.output, str(c.hits), f"{c.ratio:.3f}",
            "1" if c.ambiguous else "0",
            (c.contexts[0] if c.contexts else "").replace("\t", " "),
        ]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("note", type=Path, help="取材ノート（.md）")
    ap.add_argument("--terms", type=Path, nargs="*", default=[],
                    help="正解側に足すテキスト（配布資料・プレスリリース等）")
    ap.add_argument("--min-ratio", type=float, default=0.7,
                    help="読みの一致率の下限。下げると言い換えが混ざる（既定 0.7）")
    ap.add_argument("--dictionary-set", default="default",
                    help="既存の登録を除くために読む辞書セットID（none で読まない）")
    ap.add_argument("--top", type=int, default=30, help="表示件数")
    ap.add_argument("--tsv", type=Path, help="全候補の書き出し先（.tsv）")
    ap.add_argument("--out", type=Path, help="レポートの書き出し先（.md）")
    ap.add_argument("--prompt", type=Path,
                    help="外部LLMへ貼る照合プロンプトの書き出し先（.md）。音声は渡さない")
    ap.add_argument("--apply", type=Path,
                    help="LLMの答え（JSON）を検証して、登録する組だけを出す")
    args = ap.parse_args()

    try:
        text = args.note.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"エラー: ノートを読めません: {exc}", file=sys.stderr)
        return 1

    transcript, correct = split_note(text)
    extra: Counter = Counter()
    materials = ""
    for path in args.terms:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"エラー: {path} を読めません: {exc}", file=sys.stderr)
            return 1
        materials += raw + "\n"
        extra.update(terms_from(raw))
    if not correct.strip() and not extra:
        print(
            "エラー: 正解側のテキストがありません。\n"
            "  ノートに要約（%%wx 0:00%% 以降）が無い場合は --terms で資料を渡してください。",
            file=sys.stderr,
        )
        return 1

    known_good: frozenset[str] = frozenset()
    known_pairs: frozenset[tuple[str, str]] = frozenset()
    if args.dictionary_set != "none":
        try:
            from userdict import get_dictionary_snapshot

            snapshot = get_dictionary_snapshot(args.dictionary_set)
            known_pairs = frozenset(snapshot.replacements)
            known_good = frozenset(output for _observed, output in snapshot.replacements)
        except Exception as exc:  # noqa: BLE001 — 辞書が読めなくても候補は出せる
            print(f"# 辞書を読めませんでした（既存登録の除外なしで続行）: {exc}")

    print(f"# {args.note.name}")
    print(f"# 文字起こし {len(transcript)}字 / 手書き {len(correct)}字"
          + (f" / 資料の語 {len(extra)}種" if extra else "")
          + (f" / 登録済み {len(known_pairs)}件" if known_pairs else ""))
    cands = find_candidates(
        transcript, correct,
        extra_terms=extra,
        known_good=known_good,
        known_pairs=known_pairs,
        min_ratio=args.min_ratio,
    )
    if args.apply:
        try:
            answer = args.apply.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"エラー: {args.apply} を読めません: {exc}", file=sys.stderr)
            return 1
        accepted, rejected = apply_verdicts(
            answer,
            transcript=transcript,
            allowed_text=correct + "\n" + materials,
            known_good=known_good,
            known_pairs=known_pairs,
            min_ratio=args.min_ratio,
        )
        print(f"\n--- 検証: 採用 {len(accepted)}件 / 落とした {len(rejected)}件")
        for reason in rejected:
            print(f"  × {reason}")
        print("\n| 誤認識 | 正しい表記 | 本文 |")
        print("|---|---|---:|")
        for observed, output in accepted:
            print(f"| {observed} | {output} | {_count(transcript, observed)} |")
        print("\n登録はObsidianの「選択範囲を辞書に追加」から行ってください"
              "（登録直前にもう一度、何箇所どう変わるかが出ます）。")
        return 0

    report = render(cands, top=args.top)
    if args.prompt:
        args.prompt.parent.mkdir(parents=True, exist_ok=True)
        args.prompt.write_text(
            build_prompt(cands, correct, materials=materials, top=args.top),
            encoding="utf-8",
        )
        print(f"# LLM用プロンプト: {args.prompt}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"# レポート: {args.out}")
    else:
        print()
        print(report)
    if args.tsv:
        write_tsv(cands, args.tsv)
        print(f"# 全候補: {args.tsv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
