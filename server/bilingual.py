"""日英が混ざった録音を、チャンクごとに言語を選んで文字起こしし直す。

なぜ要るか
----------
逐次通訳の取材では、英語話者と通訳者が交互に話す。いまの文字起こしは言語を ja に
固定しているので（asr.py の `language=o.language or config.language`）、英語の区間は
「アジアティックコーナース」（agentic commerce）のようなカタカナの粥になるか、
運よく英語のまま出るかのどちらかで安定しない。記事で引用するには原語の英語が要る。

**逐次通訳なら日本語訳は通訳者が既に作っている。** だから欲しいのは翻訳ではなく、
引用できる英語原文のほう。それなら外部の翻訳APIに音声由来のテキストを出す判断を
避けられて、全部ローカルで閉じる。

やり方
------
retranscribe.py と同じ刻み方でチャンクを作り、1チャンクごとに3回モデルを呼ぶ:

  1. 言語判定（detect_language。エンコーダだけなので安い）
  2. ja 固定で認識
  3. en 固定で認識

そのうえで「どちらを採るか」を決める。**判定材料を全部 TSV に残すのが主目的**で、
規則そのものは後から変えられるようにしてある（--rule）。最初から規則を決め打ちすると、
外したときに何が原因か分からなくなるため。

規則を confidence だけで決めない理由
------------------------------------
avg_logprob は「モデルがどれだけ自信を持って出したか」であって「正しいか」ではない。
**言語を間違えたときの典型的な壊れ方が反復**（「アーメンスでアーメンスでアーメンスで」）で、
反復は次のトークンが極めて予測しやすいので **avg_logprob はむしろ高く出る**。
だから logprob 単独は危ない。反復の度合い（zlib で圧縮したときの縮み方）と
言語判定の確率を併せて残している。

使い方
------
  python bilingual.py 20260807-151651 --limit-sec 300 --out ..\\out.md
  python bilingual.py 20260807-151651 --tsv ..\\signals.tsv --out ..\\本文.md
  python bilingual.py 20260807-151651 --rule logprob --show-both
"""
from __future__ import annotations

import argparse
import sys
import time
import zlib
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

# ⟨未認識⟩ や日本語の診断を出すので、既定が cp932 のコンソールでも落ちないようにする
# （retranscribe.py と同じ理由。このスクリプトは run.ps1 を通さず直接叩かれる）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import asr
from asr import AsrOptions, resolve_model_name
from config import config
from postproc import postprocess
from recording import RECORDINGS_DIR, list_sessions, load_slice, session_duration_sec
from retranscribe import build_chunks, fmt_time
from transcribe_guard import filter_contextual_artifacts, speech_evidence
from userdict import get_dictionary_snapshot


@dataclass
class Row:
    """1チャンクぶんの判定材料。TSV の1行になる。"""

    n: int
    start: float
    end: float
    # 言語判定（Whisper のエンコーダが出す確率）
    lid: str
    p_lid: float
    p_ja: float
    p_en: float
    # ja 固定・en 固定それぞれの認識結果
    ja_text: str
    ja_logprob: float | None
    ja_blocked: bool
    en_text: str
    en_logprob: float | None
    en_blocked: bool

    @property
    def dur(self) -> float:
        return self.end - self.start


def latin_ratio(text: str) -> float:
    """空白を除いた文字のうち、ASCII英字の割合。

    ja 固定なのに英字だらけの結果は「言語固定が効かず英語が漏れた」印で、
    その区間が英語だったことの傍証になる。判定の答え合わせに使う。
    """
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(c.isascii() and c.isalpha() for c in chars) / len(chars)


def repetition(text: str) -> float:
    """反復の度合い。0に近いほど同じ語の繰り返し（＝壊れている）。

    zlib で圧縮したときの縮み方を見る。「アーメンスでアーメンスで…」のように
    同じ断片が並ぶと猛烈に縮む。言語を取り違えたときの壊れ方がこれなので、
    avg_logprob が高いのに実は壊れている場合を、ここで捕まえる。
    短い文字列は圧縮のヘッダが効いて値が暴れるので、呼び出し側で長さを見ること。
    """
    raw = text.encode("utf-8")
    if len(raw) < 24:
        return 1.0
    return len(zlib.compress(raw, 6)) / len(raw)


def _run_pass(transcriber, chunk_audio, opts, dictionary):
    """1チャンクを1言語で認識する。実運用（retranscribe）と同じ後段を通す。"""
    result = transcriber.transcribe(
        chunk_audio, opts=opts, hallucinations=dictionary.hallucinations
    )
    raw = result.text
    if raw:
        evidence = speech_evidence(
            chunk_audio, config.sample_rate, active_rms=config.retry_active_rms
        )
        raw, contextual = filter_contextual_artifacts(
            raw,
            evidence,
            weak_rms=config.retry_min_rms,
            weak_active_ratio=config.retry_active_ratio,
        )
        result.blocked.extend(contextual)
    return raw, result


def measure(chunks, *, dictionary, progress: bool = True) -> list[Row]:
    """チャンクごとに、言語判定・ja認識・en認識をまとめて取る。"""
    transcriber = asr.hq_transcriber()
    transcriber.ensure_loaded()
    ja_opts = replace(AsrOptions.transcription(), language="ja")
    en_opts = replace(AsrOptions.transcription(), language="en")

    rows: list[Row] = []
    t0 = time.time()
    for n, chunk in enumerate(chunks, 1):
        try:
            lid, p_lid, all_probs = transcriber.detect_language(chunk.audio)
        except Exception as exc:  # 判定だけ失敗しても本文は作れるので止めない
            print(f"[bilingual] 言語判定に失敗 (#{n}): {str(exc)[:80]}", file=sys.stderr)
            lid, p_lid, all_probs = "?", 0.0, {}

        ja_text, ja_res = _run_pass(transcriber, chunk.audio, ja_opts, dictionary)
        en_text, en_res = _run_pass(transcriber, chunk.audio, en_opts, dictionary)

        rows.append(
            Row(
                n=n,
                start=chunk.start / config.sample_rate,
                end=chunk.end / config.sample_rate,
                lid=lid,
                p_lid=p_lid,
                p_ja=all_probs.get("ja", 0.0),
                p_en=all_probs.get("en", 0.0),
                ja_text=ja_text,
                ja_logprob=ja_res.avg_logprob,
                ja_blocked=bool(ja_res.blocked),
                en_text=en_text,
                en_logprob=en_res.avg_logprob,
                en_blocked=bool(en_res.blocked),
            )
        )

        if progress and n % 25 == 0:
            done = rows[-1].end or 1e-6
            rtf = (time.time() - t0) / done
            left = (chunks[-1].end / config.sample_rate - done) * rtf / 60
            print(
                f"  [{fmt_time(done)}] {n}/{len(chunks)}チャンク "
                f"(RTF {rtf:.2f}, 残り約{left:.1f}分)",
                file=sys.stderr,
            )
    return rows


# --- どちらを採るか -------------------------------------------------------

def decide(row: Row, rule: str, *, margin: float, mix_threshold: float = 0.15) -> str:
    """このチャンクを ja と en のどちらで採るか。両方入っていれば "mix"。

    既定は lid（言語判定）。Whisper の言語判定は音そのものを見ていて、
    テキストの見た目に釣られない。logprob は反復に騙されるので単独では使わない。

    **"mix" が要る理由。** VAD は息継ぎで切るので、チャンクの切れ目は話者の交代と
    一致しない。通訳者の語尾と話者の English の出だしが1チャンクに入ると、
    ja側とen側が**別々の中身**を拾う（実測 2026-08-07 の 1:37 「私の戦略ですけれども、
    あるパターンを」 と "that we've seen throughout the history of payments."）。
    片方を選べばもう片方は消える。分割は当てにならないので、両方残して読む側に渡す。
    """
    if rule == "lid":
        if mix_threshold > 0 and min(row.p_ja, row.p_en) >= mix_threshold:
            # ja固定の結果が英字だらけなら、両者は同じ英語を二度書いただけ
            # （言語固定が効かず漏れた）。その場合は綴りの整った en に寄せる。
            if latin_ratio(row.ja_text) > 0.6:
                return "en"
            return "mix"
        return "en" if row.p_en > row.p_ja + margin else "ja"
    if rule == "logprob":
        ja = row.ja_logprob if row.ja_logprob is not None else -9.0
        en = row.en_logprob if row.en_logprob is not None else -9.0
        return "en" if en > ja + margin else "ja"
    if rule == "both":
        # 言語判定が en で、かつ en 側が壊れて（反復して）いないときだけ en。
        # 判定が僅差のときに反復した英語を掴まないための保険。
        if row.p_en <= row.p_ja + margin:
            return "ja"
        return "ja" if repetition(row.en_text) < 0.35 else "en"
    raise ValueError(f"未知の規則: {rule}")


def finalize(row: Row, lang: str, *, dictionary) -> str:
    """採用した側のテキストを本文用に整える。

    日本語の辞書（表記ゆれの置換・自動句読点）は日本語にしか当てない。
    英語に日本語向けの後処理を通しても直らないし、壊す余地しかないので。
    """
    if lang == "en":
        return " ".join(row.en_text.split())
    return postprocess(
        row.ja_text,
        strip_space=config.strip_ja_alnum_space,
        symbol_dictation=False,
        replacements=dictionary.replacement_plan,
        symbols=dictionary.symbols,
        auto_punctuate=config.enable_auto_punctuation,
    )


# --- 出力 -----------------------------------------------------------------

TSV_HEADER = [
    "n", "start", "end", "dur",
    "lid", "p_lid", "p_ja", "p_en",
    "ja_logprob", "ja_latin", "ja_rep", "ja_blocked", "ja_text",
    "en_logprob", "en_rep", "en_blocked", "en_text",
]


def write_tsv(rows: list[Row], path: Path) -> None:
    def cell(v) -> str:
        if v is None:
            return ""
        if isinstance(v, float):
            return f"{v:.3f}"
        # テキストにタブ・改行が混ざると列がずれるので潰す。
        return str(v).replace("\t", " ").replace("\n", " ")

    lines = ["\t".join(TSV_HEADER)]
    for r in rows:
        lines.append("\t".join(cell(v) for v in [
            r.n, r.start, r.end, r.dur,
            r.lid, r.p_lid, r.p_ja, r.p_en,
            r.ja_logprob, latin_ratio(r.ja_text), repetition(r.ja_text), r.ja_blocked, r.ja_text,
            r.en_logprob, repetition(r.en_text), r.en_blocked, r.en_text,
        ]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_body(
    rows: list[Row], *, rule: str, margin: float, dictionary, show_both: bool,
    mix_threshold: float,
) -> tuple[str, dict[str, int]]:
    """時刻つきの1チャンク1行で本文を組む。

    段落に流し込まないのは、この用途が「引用のために原語を確かめる」ことだから。
    時刻が残っていれば、怪しい行をすぐ音で確認できる。
    """
    out: list[str] = []
    counts = {"ja": 0, "en": 0, "mix": 0, "empty": 0}
    for r in rows:
        lang = decide(r, rule, margin=margin, mix_threshold=mix_threshold)

        # 両言語が入ったチャンクは、どちらかを捨てると発言そのものが消える。
        # 分割はできないので、印を付けて両方並べる。
        if lang == "mix":
            ja = finalize(r, "ja", dictionary=dictionary)
            en = finalize(r, "en", dictionary=dictionary)
            if not (ja or en):
                counts["empty"] += 1
                continue
            counts["mix"] += 1
            out.append(f"- `{fmt_time(r.start)}` ⚠MIX JA {ja}")
            out.append(f"- `{fmt_time(r.start)}` ⚠MIX **EN** {en}")
            continue

        text = finalize(r, lang, dictionary=dictionary)
        if not text:
            counts["empty"] += 1
            continue
        counts[lang] += 1
        tag = "**EN**" if lang == "en" else "JA"
        out.append(f"- `{fmt_time(r.start)}` {tag} {text}")
        if show_both:
            other = r.ja_text if lang == "en" else r.en_text
            if other.strip():
                out.append(f"    - ~~{' '.join(other.split())}~~")
    return "\n".join(out), counts


def report(rows: list[Row], *, margin: float, mix_threshold: float) -> None:
    """規則ごとに、どれだけ en を選ぶか・どこで食い違うかを出す。"""
    total = len(rows)
    secs = sum(r.dur for r in rows)
    print(f"\n--- 判定材料 {total}チャンク / {secs / 60:.1f}分")

    # 規則の比較は素の二択で見る（mix を挟むと en/ja の取り分が比べられない）。
    picks = {rule: [decide(r, rule, margin=margin, mix_threshold=0.0) for r in rows]
             for rule in ("lid", "logprob", "both")}
    for rule, p in picks.items():
        en = p.count("en")
        en_sec = sum(r.dur for r, k in zip(rows, p) if k == "en")
        print(f"  {rule:8s}: en {en:4d}件 ({en / total * 100:4.1f}%) / {en_sec / 60:.1f}分")

    disagree = sum(1 for a, b in zip(picks["lid"], picks["logprob"]) if a != b)
    print(f"  lid と logprob の食い違い: {disagree}件 ({disagree / total * 100:.1f}%)")

    # 1チャンクに日英が同居した分。片方を選ぶと反対側が消えるので両方残す対象。
    mixed = [r for r in rows if min(r.p_ja, r.p_en) >= mix_threshold]
    print(
        f"  日英が同居（両方 p>={mix_threshold}）: {len(mixed)}件 "
        f"({len(mixed) / total * 100:.1f}%) / {sum(r.dur for r in mixed) / 60:.1f}分"
    )

    # 答え合わせ: ja 固定なのに英字だらけ ＝ 言語固定が効かず英語が漏れた区間。
    # ここを lid が en と呼べているかで、判定の当たり具合が粗く分かる。
    leaked = [r for r in rows if latin_ratio(r.ja_text) > 0.6 and len(r.ja_text) > 20]
    if leaked:
        hit = sum(1 for r in leaked if r.p_en > r.p_ja + margin)
        print(
            f"  ja側が英字だらけ（英語が漏れた区間）{len(leaked)}件 → "
            f"lid も en と判定 {hit}件 ({hit / len(leaked) * 100:.0f}%)"
        )

    # 反復して壊れている側がどれだけあるか。logprob を信用できない根拠。
    ja_broken = sum(1 for r in rows if len(r.ja_text) > 24 and repetition(r.ja_text) < 0.35)
    en_broken = sum(1 for r in rows if len(r.en_text) > 24 and repetition(r.en_text) < 0.35)
    print(f"  反復で壊れた出力: ja {ja_broken}件 / en {en_broken}件")

    hi_conf_broken = [
        r for r in rows
        if len(r.en_text) > 24 and repetition(r.en_text) < 0.35
        and r.en_logprob is not None and r.ja_logprob is not None
        and r.en_logprob > r.ja_logprob
    ]
    if hi_conf_broken:
        print(
            f"  うち「壊れているのに logprob は ja より高い」en {len(hi_conf_broken)}件"
            "  ← logprob 単独が使えない証拠"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("session", nargs="?", help="セッションID（recordings/ 配下）")
    ap.add_argument("--list", action="store_true", help="保存済みの録音を一覧する")
    ap.add_argument("--limit-sec", type=float, help="先頭N秒だけ（お試し用）")
    ap.add_argument("--low-latency", action="store_true",
                    help="対面インタビュー用の短い連結で刻む（既定は長い連結）")
    ap.add_argument("--rule", default="lid", choices=("lid", "logprob", "both"),
                    help="どちらの言語を採るかの規則（既定 lid）")
    ap.add_argument("--margin", type=float, default=0.0,
                    help="en を選ぶのに要する差。上げると ja 寄りになる")
    ap.add_argument("--mix-threshold", type=float, default=0.15,
                    help="両言語ともこの確率を超えたら日英同居とみなし両方残す（0で無効）")
    ap.add_argument("--show-both", action="store_true",
                    help="採らなかった側も打ち消し線で並べる（答え合わせ用）")
    ap.add_argument("--out", type=Path, help="本文の書き出し先（.md）")
    ap.add_argument("--tsv", type=Path, help="判定材料の書き出し先（.tsv）")
    ap.add_argument("--dictionary-set", default="default", help="使う辞書セットID")
    args = ap.parse_args()

    if args.list or not args.session:
        items = list_sessions()
        if not items:
            print(f"録音がありません: {RECORDINGS_DIR}")
            return 0
        print(f"保存先: {RECORDINGS_DIR}")
        for i in items:
            print(f"  {i['session']}  {i['seconds'] / 60:6.1f}分  {i['bytes'] / 1048576:8.1f} MB")
        if not args.session:
            print("\nセッションIDを指定してください。")
        return 0

    try:
        duration = session_duration_sec(args.session)
    except (ValueError, FileNotFoundError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    if args.limit_sec:
        duration = min(duration, args.limit_sec)

    audio = load_slice(args.session, 0.0, duration)
    if audio.size == 0:
        print("エラー: 音声が空です。", file=sys.stderr)
        return 1
    dictionary = get_dictionary_snapshot(args.dictionary_set)
    print(
        f"# {args.session} {duration / 60:.1f}分 / "
        f"モデル {resolve_model_name(config.transcribe_model)} / 辞書 {dictionary.set_id}"
    )

    chunks = build_chunks(audio, low_latency=bool(args.low_latency))
    lens = np.array([c.audio.size / config.sample_rate for c in chunks])
    print(
        f"# {len(chunks)}チャンク 中央{np.median(lens):.1f}秒 / "
        f"4秒未満 {int((lens < 4).sum())}件({(lens < 4).mean() * 100:.0f}%)"
    )

    t0 = time.time()
    rows = measure(chunks, dictionary=dictionary)
    print(f"# 測定 {time.time() - t0:.0f}秒（1チャンクにつき 判定1回＋認識2回）")

    report(rows, margin=args.margin, mix_threshold=args.mix_threshold)

    if args.tsv:
        write_tsv(rows, args.tsv)
        print(f"\n# 判定材料を書き出しました: {args.tsv}")

    body, counts = build_body(
        rows, rule=args.rule, margin=args.margin,
        dictionary=dictionary, show_both=args.show_both,
        mix_threshold=args.mix_threshold,
    )
    print(
        f"\n--- 規則 {args.rule}（margin {args.margin}）で本文を作成: "
        f"JA {counts['ja']}行 / EN {counts['en']}行 / "
        f"⚠MIX {counts['mix']}件 / 空 {counts['empty']}件"
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(body, encoding="utf-8")
        print(f"# 本文を書き出しました: {args.out}")
    else:
        print("\n" + body[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
