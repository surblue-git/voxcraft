"""英語の認識を、モデルを変えて速度と精度で比べる。

なぜ要るか
----------
[[voxcraft-latency-floor]] のとおり、認識時間はエンコーダ規模で決まる固定費で、
beam も VAD も効かない。速くする手段は「小さいモデルを別に持つ」しかない。
日本語は精度上どうしても large 級から降りられないが、**英語なら小さいモデルが
実用域に入る**はず——という見込みがずっと未検証のまま残っていた。

bilingual.py で英語区間が特定できたので、いま初めて**実際の取材音声の英語チャンク**で
測れる。合成音声やスマホ再生と違い、素材のバイアス（原稿あり・単独話者・言い淀みなし）が
かからないので、ここで出た数字はそのまま対面の見積もりに使える。

測るもの
--------
1. **同じモデルで ja と en のどちらが速いか。** トークン化の効率が違うので、
   モデルを変えなくても差が出る可能性がある。
2. **小さいモデルで英語がどこまで保つか。** turbo の出力を基準に語単位で照合する。

基準の限界
----------
turbo の出力は正解ではない。ここで出る WER は「turbo とどれだけ違うか」であって、
差が出た箇所は小さいモデルの誤りかもしれないし turbo の誤りかもしれない。
**小さいモデルを落とす判断には使えるが、合格の証明にはならない。**
採用するなら最後は本文を目で読むこと（--samples で並べて出す）。

使い方
------
  python bench_en.py 20260807-151651 --tsv ..\\full.tsv --limit 60
  python bench_en.py 20260807-151651 --tsv ..\\full.tsv --models turbo,small,base --samples 5
"""
from __future__ import annotations

import argparse
import csv
import gc
import re
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from asr import AsrOptions, Transcriber, resolve_model_name
from config import config
from recording import load_slice, session_duration_sec
from retranscribe import build_chunks
from userdict import get_dictionary_snapshot

# 既定で比べるモデル。いずれもローカルにキャッシュ済みのものだけを並べてある
# （.en 専用モデルは未取得。落とすかどうかは、この結果を見てから決める）。
DEFAULT_MODELS = "turbo,large-v3,small,base,tiny"

# 最初の数回はカーネルの初期化ぶんが乗るので計測から外す。
WARMUP = 3


def normalize(text: str) -> list[str]:
    """語単位で比べるための正規化。大小・句読点・連続空白の差は無視する。"""
    return re.sub(r"[^\w\s']", " ", text.lower()).split()


def wer(ref: str, hyp: str) -> float:
    """語単位の編集距離 ÷ 基準の語数。基準が空なら0扱い。"""
    r, h = normalize(ref), normalize(hyp)
    if not r:
        return 0.0
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw)))
        prev = cur
    return prev[-1] / len(r)


def pick_chunks(chunks, tsv: Path, *, lang: str, limit: int, min_sec: float):
    """bilingual.py が lang と判定した区間のうち、比較に足るものを選ぶ。

    短すぎるチャンクは、どのモデルでも当たり外れが大きく、速度も定数項に埋もれる。
    日英が同居した区間（両方の確率が高い）も、基準そのものが濁るので外す。
    """
    want: list[tuple[float, float]] = []
    with tsv.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            p_ja, p_en = float(row["p_ja"] or 0), float(row["p_en"] or 0)
            if min(p_ja, p_en) >= 0.15:
                continue
            if (p_en > p_ja) != (lang == "en"):
                continue
            if len(row[f"{lang}_text"]) < 20:
                continue
            want.append((float(row["start"]), float(row["end"])))

    keyed = {round(c.start / config.sample_rate, 2): c for c in chunks}
    out = []
    for start, end in want:
        chunk = keyed.get(round(start, 2))
        if chunk is not None and (end - start) >= min_sec:
            out.append(chunk)
        if len(out) >= limit:
            break
    return out


def run_model(model_name: str, chunks, *, language: str, dictionary):
    """1モデル・1言語で全チャンクを回し、1回ごとの所要時間と本文を返す。"""
    tr = Transcriber(model_name=model_name)
    tr.load()
    opts = replace(AsrOptions.transcription(), language=language)

    for chunk in chunks[:WARMUP]:
        tr.transcribe(chunk.audio, opts=opts, hallucinations=dictionary.hallucinations)

    times: list[float] = []
    texts: list[str] = []
    for chunk in chunks:
        t0 = time.perf_counter()
        res = tr.transcribe(
            chunk.audio, opts=opts, hallucinations=dictionary.hallucinations
        )
        times.append((time.perf_counter() - t0) * 1000.0)
        texts.append(res.text)

    device = tr.device
    # 次のモデルのために VRAM を空ける（6GB しかないので詰めると CPU に落ちる）。
    tr._model = None
    del tr
    gc.collect()
    return times, texts, device


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("session", help="セッションID（recordings/ 配下）")
    ap.add_argument("--tsv", type=Path, required=True,
                    help="bilingual.py が出した判定材料（英語区間の特定に使う）")
    ap.add_argument("--models", default=DEFAULT_MODELS, help="比べるモデル（カンマ区切り）")
    ap.add_argument("--limit", type=int, default=60, help="使う英語チャンク数")
    ap.add_argument("--min-sec", type=float, default=2.0, help="これより短いチャンクは使わない")
    ap.add_argument("--samples", type=int, default=0, help="本文を並べて出す件数")
    ap.add_argument("--keywords", default="",
                    help="取りこぼしを数える語（カンマ区切り。固有名詞・専門語）")
    ap.add_argument("--ja-baseline", action="store_true",
                    help="日本語チャンクでも先頭モデルを回す（en と速度を比べる正しい基準）")
    ap.add_argument("--dictionary-set", default="default")
    args = ap.parse_args()

    audio = load_slice(args.session, 0.0, session_duration_sec(args.session))
    dictionary = get_dictionary_snapshot(args.dictionary_set)
    all_chunks = build_chunks(audio)
    chunks = pick_chunks(
        all_chunks, args.tsv, lang="en", limit=args.limit, min_sec=args.min_sec
    )
    if len(chunks) <= WARMUP:
        print("エラー: 比較に足る英語チャンクがありません。", file=sys.stderr)
        return 1

    secs = sum(c.audio.size / config.sample_rate for c in chunks)
    print(
        f"# {args.session} / 英語チャンク {len(chunks)}件 計{secs:.0f}秒 "
        f"(中央 {np.median([c.audio.size / config.sample_rate for c in chunks]):.1f}秒)"
    )
    print(f"# 先頭{WARMUP}件は暖機として計測から除外\n")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    # 基準は turbo の英語。いまの文字起こしが使っているモデルそのもの。
    # 先頭モデルだけ ja でも回す。これは「英語音声を日本語だと思って認識した場合」＝
    # いまの挙動そのもので、速度の基準ではなく**誤認識の代償**を測るための行。
    runs: list[tuple[str, str, list[float], list[str], str]] = []
    for name in models:
        for language in (("en", "ja") if name == models[0] else ("en",)):
            label = f"{name} @{language}" + (
                "(英語音声)" if language == "ja" else ""
            )
            print(f"  計測中: {label} …", file=sys.stderr)
            try:
                times, texts, device = run_model(
                    name, chunks, language=language, dictionary=dictionary
                )
            except Exception as exc:
                print(f"  失敗 {label}: {str(exc)[:120]}", file=sys.stderr)
                continue
            runs.append((label, name, times, texts, device))

    ref = next((t for lb, _, _, t, _ in runs if lb == f"{models[0]} @en"), None)

    print(f"{'モデル':<18}{'装置':<6}{'中央ms':>8}{'平均ms':>8}{'RTF':>7}{'差(WER)':>9}")
    print("-" * 58)
    for label, _, times, texts, device in runs:
        t = np.array(times[WARMUP:])
        audio_sec = sum(c.audio.size / config.sample_rate for c in chunks[WARMUP:])
        rtf = t.sum() / 1000.0 / audio_sec
        if ref is None or label == f"{models[0]} @en":
            diff = "（基準）"
        else:
            scores = [wer(r, h) for r, h in zip(ref[WARMUP:], texts[WARMUP:]) if r.strip()]
            diff = f"{np.mean(scores) * 100:.1f}%" if scores else "—"
        print(
            f"{label:<18}{device:<6}{np.median(t):>8.0f}{t.mean():>8.0f}"
            f"{rtf:>7.2f}{diff:>9}"
        )

    # 「英語は日本語より速いのか」に答えるには、それぞれ**自分の言語の音声**で
    # 測るしかない。上の @ja は英語音声を誤って日本語で解いた行なので使えない。
    if args.ja_baseline:
        ja_chunks = pick_chunks(
            all_chunks, args.tsv, lang="ja", limit=args.limit, min_sec=args.min_sec
        )
        if len(ja_chunks) > WARMUP:
            times, _texts, device = run_model(
                models[0], ja_chunks, language="ja", dictionary=dictionary
            )
            t = np.array(times[WARMUP:])
            ja_sec = sum(c.audio.size / config.sample_rate for c in ja_chunks[WARMUP:])
            print(
                f"{models[0] + ' @ja(日本語音声)':<18}{device:<6}{np.median(t):>8.0f}"
                f"{t.mean():>8.0f}{t.sum() / 1000.0 / ja_sec:>7.2f}{'—':>9}"
                f"   ← 速度の比較対象はこちら（{len(ja_chunks)}件）"
            )

    # WER の平均は「どこを間違えたか」を隠す。取材で効くのは固有名詞・専門語で、
    # そこだけ落とすモデルは平均が良くても使えない。語ごとに取りこぼしを数える。
    if args.keywords and ref is not None:
        words = [w.strip().lower() for w in args.keywords.split(",") if w.strip()]
        print("\n--- 固有名詞・専門語の取りこぼし（基準に出た回数 → 各モデルで出た回数）")
        header = "".join(f"{lb.split(' @')[0][:9]:>11}" for lb, _, _, _, _ in runs)
        print(f"{'語':<16}{'基準':>6}{header}")
        for w in words:
            hit = [i for i in range(WARMUP, len(chunks)) if w in ref[i].lower()]
            if not hit:
                continue
            row = "".join(
                f"{sum(1 for i in hit if w in texts[i].lower()):>11}"
                for _, _, _, texts, _ in runs
            )
            print(f"{w:<16}{len(hit):>6}{row}")

    if args.samples and ref is not None:
        print("\n--- 本文の比較（差が大きい順）")
        scored = sorted(
            range(WARMUP, len(chunks)),
            key=lambda i: -max(
                (wer(ref[i], texts[i]) for _, _, _, texts, _ in runs[1:]), default=0.0
            ),
        )
        for i in scored[: args.samples]:
            print(f"\n  [{chunks[i].start / config.sample_rate:.1f}s]")
            for label, _, _, texts, _ in runs:
                print(f"    {label:<16} {texts[i][:150]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
