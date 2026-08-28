"""AI校正を、ローカルLLMで実際に測る。

なぜ測ってから決めるのか
------------------------
「ローカルの7〜14B級で、日本語の同音異義と固有名詞をどこまで直せるか」は、
やってみないと分からない。門（`refine_guard`）があるので**悪化はしない**が、
「効かない」ことは門では防げない。効くかどうかはここで測る。

指標はほとんど正解データ無しで取れる
------------------------------------
残存誤変換だけは人が数えるしかないが、それ以外は前後のテキストだけで出る:

  却下率と内訳    どの門で落ちたか（プロンプトとモデルの比較はこれが主指標）
  読み保存違反率  取材モードで 0% でなければ採用しない
  幻覚件数        入力に無い数値・語を持ち込んだ回数。0件でなければ採用しない
  変更量          何箇所ほんとうに直したか（0なら「安全だが効いていない」）
  所要時間        ブロックあたり。認識のレイテンシに影響しない範囲か

**変更量を必ず見ること。** 何もしないモデルは全部の門を通る。通過率だけを見ると
最優秀に見えてしまう（`--model stub` がまさにそれ）。

使い方
------
  # 配線の確認（モデル不要。全部通って変更0になるのが正しい）
  python bench_refine.py --text ノート.md --model stub

  # ローカルモデルで測る
  python bench_refine.py --text 会見.md --mode manuscript --model qwen3:8b
  python bench_refine.py --text 会見.md --mode interview  --model qwen3:14b --out 校正後.md

  # モデルを並べて比べる
  python bench_refine.py --text 会見.md --compare qwen3:8b qwen3:14b gemma3:12b

  # 保存済み録音から生テキストを作り直して測る（GPUが要る）
  python bench_refine.py --session 20260806-133113 --limit-sec 600 --model qwen3:8b
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from refine_guard import GuardStats, RefineGuard, extract_numbers
from refine_llm import PROFILE_BY_NAME, RefineResult, make_client
from refine_score import (ErrorScore, audit_dictionary, format_score,
                          load_known_errors, plan_counter)

DEFAULT_BASE_URL = "http://localhost:11434"
# 実運用の補正ブロック（30秒）で入る本文の見当。長すぎるとモデルが要約に倒れ、
# 短すぎると文脈が足りず同音異義を直せない。
DEFAULT_BLOCK_CHARS = 400


# --- 入力をブロックへ割る --------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[。！？!?])")


def split_blocks(text: str, block_chars: int = DEFAULT_BLOCK_CHARS) -> list[str]:
    """文末で切りながら、およそ block_chars ずつのブロックにする。

    語の途中で割らないのが要点。割ると、割れ目の同音異義がどちらのブロックからも
    直せなくなる（実運用の補正ブロックが速報チャンクの終端だけを使うのと同じ理由）。
    """
    blocks: list[str] = []
    for paragraph in re.split(r"\n{2,}", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        current = ""
        for sentence in _SENTENCE_END.split(paragraph):
            if not sentence:
                continue
            if current and len(current) + len(sentence) > block_chars:
                blocks.append(current.strip())
                current = sentence
            else:
                current += sentence
        if current.strip():
            blocks.append(current.strip())
    return blocks


# --- 1本ぶんの結果 --------------------------------------------------------

@dataclass
class BlockOutcome:
    before: str
    after: str
    seconds: float
    accepted: bool
    reasons: tuple[str, ...]
    details: tuple[str, ...]
    error: str = ""

    @property
    def text(self) -> str:
        """本文に採用されるテキスト。却下されたら生テキストが残る。"""
        return self.after if self.accepted else self.before

    @property
    def changed_chars(self) -> int:
        """どれだけ直したか。0なら安全だが効いていない。"""
        return _edit_size(self.before, self.after) if self.accepted else 0


@dataclass
class BenchReport:
    model: str
    mode: str
    stats: GuardStats = field(default_factory=GuardStats)
    outcomes: list[BlockOutcome] = field(default_factory=list)
    errors: int = 0
    # 既知の誤り（辞書 = 正解）の採点。門の前と後で別々に数える。
    score: ErrorScore = field(default_factory=ErrorScore)
    accepted_score: ErrorScore = field(default_factory=ErrorScore)

    def add(self, outcome: BlockOutcome) -> None:
        self.outcomes.append(outcome)
        if outcome.error:
            self.errors += 1

    @property
    def seconds(self) -> list[float]:
        return [o.seconds for o in self.outcomes if not o.error]

    @property
    def changed(self) -> int:
        return sum(o.changed_chars for o in self.outcomes)

    @property
    def touched_blocks(self) -> int:
        return sum(1 for o in self.outcomes if o.changed_chars > 0)


def _edit_size(before: str, after: str) -> int:
    """変更量のざっくりした見当（共通接頭・接尾を除いた長さ）。

    厳密な編集距離は要らない。「効いているか／何もしていないか」が分かればよい。
    """
    if before == after:
        return 0
    head = 0
    limit = min(len(before), len(after))
    while head < limit and before[head] == after[head]:
        head += 1
    tail = 0
    while tail < limit - head and before[-1 - tail] == after[-1 - tail]:
        tail += 1
    return max(len(before), len(after)) - head - tail


# --- 実行 -----------------------------------------------------------------

def run(blocks, client, profile, guard, glossary, verbose=False,
        known_errors=(), count_key=None, prompt_glossary=None) -> BenchReport:
    """`prompt_glossary` を省くと `glossary` をそのままモデルへ渡す。

    採点用の用語集（門が語を許可するのに使う）と、モデルへ渡す用語集を
    分けられるようにしてある。「用語集なしでどこまで直せるか」がこの実験の要点。
    """
    report = BenchReport(model=client.name, mode=profile.name)
    given = glossary if prompt_glossary is None else prompt_glossary
    for index, block in enumerate(blocks, start=1):
        result: RefineResult = client.refine(profile, block, given)
        if not result.ok:
            outcome = BlockOutcome(block, block, result.seconds, False,
                                   ("llm-error",), (result.error,), result.error)
            report.add(outcome)
            print(f"  [{index}/{len(blocks)}] エラー: {result.error}", file=sys.stderr)
            continue

        verdict = guard.check(block, result.text, profile, glossary)
        report.stats.add(verdict)
        outcome = BlockOutcome(block, result.text, result.seconds,
                               verdict.ok, verdict.reasons, verdict.details)
        report.add(outcome)

        if known_errors and count_key is not None:
            # 門の前＝モデルの実力、門の後＝実際に本文が直る数。
            report.score.add_block(block, result.text, known_errors, count_key)
            report.accepted_score.add_block(block, outcome.text, known_errors, count_key)

        if verbose:
            mark = "通" if verdict.ok else "却"
            note = "" if verdict.ok else "  " + " / ".join(verdict.details)
            print(f"  [{index}/{len(blocks)}] {mark} {result.seconds:5.2f}s "
                  f"変更{outcome.changed_chars:4d}字{note}")
    return report


def print_report(report: BenchReport, total_chars: int, scored: bool = False,
                 glossary_given: bool = True, ambiguous: set | None = None) -> None:
    stats = report.stats
    seconds = report.seconds
    print()
    print(f"■ {report.model}  /  モード: {report.mode}")
    print(f"  ブロック        {stats.total} 件（{total_chars:,} 字）")
    if stats.total:
        print(f"  採用            {stats.passed} 件（{stats.pass_rate * 100:.1f}%）")
        print(f"  却下            {stats.rejected} 件")
    if report.errors:
        print(f"  呼び出し失敗    {report.errors} 件")
    if stats.reasons:
        print("  却下の内訳")
        for reason, count in sorted(stats.reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {reason:<22} {count}")
    print(f"  直した量        {report.changed:,} 字 / {report.touched_blocks} ブロック")
    if report.changed == 0:
        print("    ※ 一切直していない。安全に見えるだけで、効いていない。")
    if seconds:
        print(f"  所要（中央値）  {statistics.median(seconds):.2f}s / ブロック"
              f"（合計 {sum(seconds):.1f}s）")

    if scored:
        print("  ── 既知の誤り（辞書 = 正解） ──")
        print(format_score(report.score, report.accepted_score,
                           glossary_given=glossary_given, ambiguous=ambiguous))

    # 採用基準（取材モードは幻覚0件・読み保存違反0件が条件）。
    if report.mode == "interview":
        fatal = sum(stats.reasons.get(r, 0) for r in
                    ("reading-changed", "number-invented", "invented-term"))
        print(f"  → 取材モードの採用条件: 違反 {fatal} 件"
              f"（{'満たす' if fatal == 0 else '満たさない'}）")
        if fatal:
            print("    ※ 違反はすべて門で止まっているので本文は汚れない。"
                  "却下が多すぎる＝そのモデルでは校正が通らない、という意味。")


def load_dictionary(set_id: str | None):
    """辞書を2つの役割で使う。

    用語集 —— AIが持ち込んでよい正しい表記（門の許可証）。
    正解   —— 登録済みの (誤り, 正しい表記) は、実際に観測された誤認識なので
              そのまま採点の正解になる。手元のノートが評価コーパスになる。
    """
    if not set_id:
        return [], [], None
    try:
        from userdict import get_dictionary_snapshot

        snapshot = get_dictionary_snapshot(set_id)
        glossary = sorted({output for _, output in snapshot.replacements if output})
        known = load_known_errors(snapshot.replacements)
        return glossary, known, snapshot.replacement_plan
    except Exception as exc:  # 辞書が無くても測れる
        print(f"辞書を読めなかった（続行する）: {exc}", file=sys.stderr)
        return [], [], None


def load_session_text(session: str, limit_sec: float | None, set_id: str | None) -> str:
    """保存済み録音から生テキストを作り直す（GPUが要る）。

    実運用と同じ刻み方で回すので、出てくる本文は次に同じ設定で録ったときに
    得られるものと同じになる（`retranscribe.py` と同じ経路をそのまま使う）。
    """
    from recording import load_slice, session_duration_sec
    from retranscribe import build_chunks, transcribe_chunks
    from userdict import get_dictionary_snapshot

    duration = session_duration_sec(session)
    if limit_sec:
        duration = min(duration, limit_sec)
    audio = load_slice(session, 0.0, duration)
    if audio.size == 0:
        raise ValueError(f"音声が空: {session}")
    chunks = build_chunks(audio)
    text, _stats = transcribe_chunks(
        chunks,
        audio=audio,
        dictionary=get_dictionary_snapshot(set_id or "default"),
        paragraphs=True,
    )
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description="AI校正をローカルLLMで測る")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", type=Path, help="生テキストのファイル（.md / .txt）")
    source.add_argument("--session", help="保存済み録音のセッションID（GPUが要る）")
    ap.add_argument("--limit-sec", type=float, default=None, help="--session のとき先頭N秒だけ")
    ap.add_argument("--mode", default="manuscript", choices=sorted(PROFILE_BY_NAME),
                    help="既定 manuscript（原稿執筆）")
    ap.add_argument("--model", default="stub", help="Ollama のモデル名。stub で配線だけ確認")
    ap.add_argument("--replay", type=Path, default=None,
                    help="保存済みの校正結果を採点し直す（`--- 8< ---` 区切り）。モデルは呼ばない")
    ap.add_argument("--compare", nargs="+", metavar="MODEL", help="複数モデルを並べて比べる")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--timeout-sec", type=float, default=120.0)
    ap.add_argument("--block-chars", type=int, default=DEFAULT_BLOCK_CHARS)
    ap.add_argument("--limit-blocks", type=int, default=0, help="先頭N ブロックだけ測る")
    ap.add_argument("--dictionary-set", default=None,
                    help="辞書セットID。用語集としても、採点の正解としても使う")
    ap.add_argument("--no-glossary", action="store_true",
                    help="用語集をモデルに渡さずに測る（辞書から外せる語を見つける）")
    ap.add_argument("--out", type=Path, default=None, help="採用後の本文の書き出し先")
    ap.add_argument("--diff", type=Path, default=None, help="変更箇所の一覧の書き出し先")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    guard = RefineGuard()
    if not guard.available:
        print("Sudachi が無いので門が動かない。`pip install -r requirements.txt` を先に。",
              file=sys.stderr)
        return 2

    if args.text:
        text = args.text.read_text(encoding="utf-8")
    else:
        text = load_session_text(args.session, args.limit_sec, args.dictionary_set)

    blocks = split_blocks(text, args.block_chars)
    if args.limit_blocks:
        blocks = blocks[: args.limit_blocks]
    if not blocks:
        print("本文が空。", file=sys.stderr)
        return 2

    profile = PROFILE_BY_NAME[args.mode]
    glossary, known_errors, plan = load_dictionary(args.dictionary_set)
    count_key = plan_counter(plan)
    # それ自体が正しい日本語のキーは、残っていても失点にしない（要確認として出す）。
    ambiguous = {observed for observed, _ in
                 audit_dictionary([(k.observed, k.output) for k in known_errors],
                                  guard._morph)}
    total_chars = sum(len(b) for b in blocks)
    models = args.compare if args.compare else [args.model]

    given = [] if args.no_glossary else glossary
    print(f"入力 {total_chars:,} 字 → {len(blocks)} ブロック"
          f"（{args.block_chars}字目安）／ モード: {profile.label}")
    print(f"用語集: モデルへ {len(given)} 語"
          f"{'（渡さない）' if args.no_glossary else ''}"
          f"／ 門の許可 {len(glossary)} 語／ 採点の正解 {len(known_errors)} 件")

    reports = []
    for model in models:
        print(f"\n--- {model} ---")
        client = make_client(model, args.base_url, args.timeout_sec, replay=args.replay)
        report = run(blocks, client, profile, guard, glossary, args.verbose,
                     known_errors=known_errors, count_key=count_key,
                     prompt_glossary=given)
        print_report(report, total_chars, scored=bool(known_errors),
                     glossary_given=not args.no_glossary, ambiguous=ambiguous)
        reports.append(report)

    if len(reports) > 1:
        print("\n■ 比較")
        print(f"  {'モデル':<22}{'採用率':>8}{'直した量':>10}{'既知の誤り':>12}{'中央値':>9}")
        for report in reports:
            median = statistics.median(report.seconds) if report.seconds else 0.0
            known = (f"{report.accepted_score.fixed}/{report.score.present}"
                     if report.score.present else "—")
            print(f"  {report.model:<22}{report.stats.pass_rate * 100:7.1f}%"
                  f"{report.changed:9,}字{known:>12}{median:8.2f}s")

    first = reports[0]
    if args.out:
        args.out.write_text("\n\n".join(o.text for o in first.outcomes), encoding="utf-8")
        print(f"\n本文を書き出した: {args.out}")
    if args.diff:
        args.diff.write_text(_render_diff(first), encoding="utf-8")
        print(f"変更の一覧を書き出した: {args.diff}")
    return 0


def _render_diff(report: BenchReport) -> str:
    """人が読んで「直っているか」を確かめるための一覧。

    残存誤変換だけは人が数えるしかないので、そこを数えやすい形にしておく。
    """
    lines = [f"# {report.model} / {report.mode}", ""]
    for index, outcome in enumerate(report.outcomes, start=1):
        if outcome.accepted and outcome.changed_chars == 0:
            continue
        head = f"## ブロック {index}"
        if not outcome.accepted:
            head += "（却下: " + ", ".join(outcome.reasons) + "）"
        lines += [head, ""]
        if outcome.details:
            lines += ["> " + d for d in outcome.details] + [""]
        lines += ["- 生: " + outcome.before, "- 校: " + outcome.after, ""]
        before_numbers = extract_numbers(outcome.before)
        after_numbers = extract_numbers(outcome.after)
        if before_numbers != after_numbers:
            lines += [f"- 数値: {before_numbers} → {after_numbers}", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
