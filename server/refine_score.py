"""辞書を正解として、AI校正が既知の誤りを直せたかを数える。

なぜ辞書が正解になるのか
------------------------
辞書に登録済みの項目は、**実際の取材で観測された誤認識と、その正しい表記の対**である。
`SMTV → SMTB` が登録されているなら、本文の `SMTV` は誤りだと分かっている。
つまり手元のノートは、**そのまま正解ラベル付きの評価コーパス**になる。
テスト用の文章をこちらで作る必要はない（作ると、作った側の想定しか測れない）。

そしてこれは、再設計の中心的な問いをそのまま測る:

    **AI校正が当たるなら、その語はもう辞書に登録しなくてよいのではないか。**

そこで `--no-glossary` は、**用語集をモデルに渡さずに**走らせる。渡さずに直せた語は、
モデルが文脈から知っている語 —— 辞書の「モデルに教える」役割は要らない。
ただし**門の許可証としての登録は残る**（語の門は、用語集に無い固有名詞を通さない）。
外せるのは辞書の役割の一部であって、項目そのものではない。ここを混ぜない。

門の前と後を、別々に数える
--------------------------
モデルが直せたかどうかと、門が通したかどうかは別の話。特に固有名詞は、
用語集に無い表記を持ち込めない規則（`refine_guard` の語の門）があるので、
**モデルが正解を出しても門で止まる**ことがある。それは仕様どおりの挙動で、
「辞書はもう要らない」ではなく「辞書は置換器ではなく**許可証**として要る」
という結論になる。区別して数えないと、この違いが見えない。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence


@dataclass(frozen=True)
class KnownError:
    observed: str
    output: str


@dataclass
class EntryScore:
    """1件の登録項目について、何が起きたか。"""

    entry: KnownError
    present: int = 0    # 入力に現れた回数
    fixed: int = 0      # 正しい表記に変わった回数
    remained: int = 0   # 誤ったまま残った回数
    lost: int = 0       # 誤りは消えたが、正しい表記も出ていない
    broken: int = 0     # 入力に無かったのに、出力に誤った表記が現れた


@dataclass
class ErrorScore:
    """コーパス全体の集計。"""

    entries: dict[str, EntryScore] = field(default_factory=dict)

    def _slot(self, entry: KnownError) -> EntryScore:
        return self.entries.setdefault(entry.observed, EntryScore(entry))

    @property
    def present(self) -> int:
        return sum(e.present for e in self.entries.values())

    @property
    def fixed(self) -> int:
        return sum(e.fixed for e in self.entries.values())

    @property
    def remained(self) -> int:
        return sum(e.remained for e in self.entries.values())

    @property
    def lost(self) -> int:
        return sum(e.lost for e in self.entries.values())

    @property
    def broken(self) -> int:
        return sum(e.broken for e in self.entries.values())

    @property
    def kinds_present(self) -> int:
        return sum(1 for e in self.entries.values() if e.present)

    @property
    def fix_rate(self) -> float:
        return self.fixed / self.present if self.present else 0.0

    def add_block(self, before: str, after: str, errors: Sequence[KnownError],
                  count_key) -> None:
        """1ブロックぶんを足す。

        `count_key(text, observed)` は、そのキーがテキストに何回当たるかを返す。
        **サーバーと同じ1回走査・最長一致**で数えるために外から渡す
        （キーごとに独立に数えると、包含関係のある登録を二重に数えてしまう）。
        """
        for entry in errors:
            before_bad = count_key(before, entry.observed)
            if before_bad == 0:
                # 入力に無かった誤りを、出力が持ち込んでいないか。
                introduced = count_key(after, entry.observed)
                if introduced:
                    self._slot(entry).broken += introduced
                continue

            slot = self._slot(entry)
            after_bad = count_key(after, entry.observed)
            gained_good = after.count(entry.output) - before.count(entry.output)

            removed = max(0, before_bad - after_bad)
            fixed = max(0, min(removed, gained_good))

            slot.present += before_bad
            slot.fixed += fixed
            slot.remained += after_bad
            slot.lost += removed - fixed


def load_known_errors(replacements: Iterable[tuple[str, str]]) -> list[KnownError]:
    """辞書の (観測された誤り, 正しい表記) をそのまま正解として使う。"""
    return [KnownError(observed, output) for observed, output in replacements
            if observed and output and observed != output]


def plan_counter(plan):
    """`ReplacementPlan` の1回走査で、キーごとの出現数を数える関数を作る。

    0.23.0 で「件数は1回の走査で実際に当たった数で数える」と決めた規則を、
    評価側でも同じにする。同じノートで、独立に数えると78箇所、実際に変わるのは
    62箇所だった——という差がそのまま採点のずれになるため。
    """
    if plan is None or plan.pattern is None:
        return lambda text, key: text.count(key) if key else 0

    cache: dict[str, dict[str, int]] = {}

    def count(text: str, key: str) -> int:
        counts = cache.get(text)
        if counts is None:
            counts = {}
            for match in plan.pattern.finditer(text):
                hit = match.group(0)
                counts[hit] = counts.get(hit, 0) + 1
            # 走査した本文は使い回す（before/after を項目ごとに数え直さない）。
            if len(cache) > 64:
                cache.clear()
            cache[text] = counts
        return counts.get(key, 0)

    return count


# --- 辞書の健康診断 -------------------------------------------------------

def audit_dictionary(replacements, morphology) -> list[tuple[str, str]]:
    """**それ自体が正しい日本語**であるキーを挙げる。

    無条件置換がいちばん危ないのはここ。`現役 → 減益` は決算の文脈でしか
    正しくないのに、置換は文脈を見ないので「現役の行員」まで「減益の行員」に
    してしまう。0.23.0 のプレビューは登録時の事故を防ぐが、**登録済みの項目には
    効かない**（毎回の認識で黙って発火する）。

    判定は「Sudachi が1語として知っているか」。ASRの崩れた出力（`一軒米氏`
    `水尾銀行`）は複数の形態素に割れるので挙がらない。`ウィンドウズ` のような
    表記ゆれは1語なので挙がるが、置換しても壊れないので目で落とせばよい
    ——**見落とすより多めに挙げる**（`dictpreview.ts` と同じ倒し方）。
    """
    if not morphology.available:
        return []
    risky = []
    for observed, output in replacements:
        if not observed or not output:
            continue
        # 未知語を含む＝ASRの崩れ。それ自体が正しい語ではない。
        if morphology.terms(observed):
            continue
        if len(_morphemes(morphology, observed)) == 1:
            risky.append((observed, output))
    # 漢字を含むキーを先に出す。カタカナの表記ゆれ（アンドロイド → Android）は
    # 1語ではあるが、置換しても文章は壊れない。壊れるのは漢字語のほうで、
    # そちらは別の意味で普通に使われる（現役の行員、経営再建、余震が続く）。
    return sorted(risky, key=lambda kv: (not _has_kanji(kv[0]), kv[0]))


_KANJI = re.compile(r"[㐀-䶿一-鿿]")


def _has_kanji(text: str) -> bool:
    return bool(_KANJI.search(text))


def _morphemes(morphology, text: str) -> list[str]:
    tokenizer = getattr(morphology, "_tokenizer", None)
    if tokenizer is None:
        return [text]
    from sudachipy import tokenizer as _t  # type: ignore

    return [m.surface() for m in tokenizer.tokenize(text, _t.Tokenizer.SplitMode.C)]


def format_score(score: ErrorScore, accepted_score: ErrorScore | None = None,
                 top: int = 12, glossary_given: bool = True,
                 ambiguous: set | None = None) -> str:
    """報告用の文字列。門の前と後を並べる。"""
    if not score.present and not score.broken:
        return "  既知の誤りが入力に1件も無い（このノートでは測れない）"

    lines = [
        f"  入力に現れた既知の誤り  {score.present} 件 / {score.kinds_present} 種",
        f"  モデルが直した          {score.fixed} 件"
        f"（{score.fix_rate * 100:.1f}%）  ← 門の前。モデルの実力",
        f"  誤ったまま残った        {score.remained} 件",
    ]
    if score.lost:
        lines.append(f"  取りこぼし              {score.lost} 件"
                     "（誤りは消えたが、正しい表記も出ていない）")
    if score.broken:
        lines.append(f"  悪化                    {score.broken} 件"
                     "（入力に無い既知の誤りを持ち込んだ）")
    if accepted_score is not None:
        lines.append(f"  門を通って本文が直る    {accepted_score.fixed} 件"
                     f"（{accepted_score.fix_rate * 100:.1f}%）  ← 実際に効く数")
        blocked = score.fixed - accepted_score.fixed
        if blocked > 0:
            lines.append(f"    ※ モデルは直せたのに門で止まったぶんが {blocked} 件。"
                         "用語集に載せれば通る（辞書は置換器ではなく許可証として要る）。")

    fixed_entries = [e for e in score.entries.values() if e.fixed]
    if fixed_entries:
        lines.append(
            "  直せた語" if glossary_given
            # 門の許可証としての登録は残る。外せるのは「モデルに教える」役割のほう。
            else "  用語集を渡さなくても直せた語（登録は許可証としてだけ要る）")
        for e in sorted(fixed_entries, key=lambda e: -e.fixed)[:top]:
            lines.append(f"    {e.entry.observed} → {e.entry.output}  ×{e.fixed}")

    # 残った語を2つに分ける。曖昧なキー（それ自体が正しい日本語）が残るのは、
    # 直せなかったのではなく**正しく手を出さなかった**可能性が高い。混ぜると、
    # 正しい抑制が失点に見えてしまう（辞書は文脈を持たないので、正解側もここは
    # 区別できない。人が見るための印として出す）。
    ambiguous = ambiguous or set()
    stuck = [e for e in score.entries.values()
             if e.remained and e.entry.observed not in ambiguous]
    held = [e for e in score.entries.values()
            if e.remained and e.entry.observed in ambiguous]
    if stuck:
        lines.append("  直せなかった語（辞書が要る）")
        for e in sorted(stuck, key=lambda e: -e.remained)[:top]:
            lines.append(f"    {e.entry.observed} → {e.entry.output}  ×{e.remained}")
    if held:
        lines.append("  残ったが、正しい用法かもしれない語（要確認・失点として数えない）")
        for e in sorted(held, key=lambda e: -e.remained)[:top]:
            lines.append(f"    {e.entry.observed} → {e.entry.output}  ×{e.remained}")
    # 曖昧なキーを「直した」ぶんは、逆に壊した可能性がある。
    risky_fixed = [e for e in score.entries.values()
                   if e.fixed and e.entry.observed in ambiguous]
    if risky_fixed:
        lines.append("  曖昧なキーを直した（本当に誤りだったか、diff で確かめること）")
        for e in sorted(risky_fixed, key=lambda e: -e.fixed)[:top]:
            lines.append(f"    {e.entry.observed} → {e.entry.output}  ×{e.fixed}")
    return "\n".join(lines)
