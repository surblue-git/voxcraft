"""保存済みの録音から、文字起こしの本文を作り直す。

なぜ要るか
----------
文字起こしモードは音声を丸ごと残している（recording.py）。設定やコードを直したら、
**過去の録音をその設定で作り直せる**はずで、実際それが必要になる場面がある。

2026-08-06 の会見（会場の音をPCのマイクで収録・約2時間・4セッション）では、
定型句の幻覚に化けて捨てられたチャンクが 250件 / 544秒あった。ブロックリストが
効いたのでノートは汚れなかったが、**その9分ぶんの発言はどこにも残らなかった**。
音声は残っているので、連結の設定を直して通し直せば取り返せる。

analyze_session.py との違い
--------------------------
あちらは「実運用の結果と通し認識を突き合わせて、直す場所を探す」ための解析。
こちらは「新しい設定でノート本文そのものを作り直す」ための生成。通し認識では
なく**実運用と同じ刻み方**で回すので、出てくるテキストは次に同じ設定で録った
ときに得られるものと同じになる。

実時間を待たない
----------------
ChunkJoiner は待ち時間を実時間で測る。ここでは音声の位置から求めた擬似時刻を
渡すので、2時間の録音でも実時間を待たずに、live と同じ連結判断で回せる
（feed_wav.py は本物の WebSocket を通す代わりに実時間かかる。用途が違う）。

使い方
------
  python retranscribe.py --list
  python retranscribe.py 20260806-133113 --far-mic --out 会見.md
  python retranscribe.py 20260806-133113 --compare        # 現行 vs 遠いマイク
  python retranscribe.py 20260806-133113 --far-mic --limit-sec 600
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

# ⟨未認識⟩ を出すので、既定が cp932 のコンソールでも落ちないようにしておく
# （run.ps1 は PYTHONIOENCODING を立てるが、このスクリプトは直接叩かれる）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import asr
from asr import AsrOptions, resolve_model_name
from config import config
from postproc import ParagraphBreaker, postprocess
from recording import RECORDINGS_DIR, list_sessions, load_slice, session_duration_sec
from transcribe_guard import filter_contextual_artifacts, speech_evidence
from userdict import get_dictionary_snapshot
from vad import Chunk, ChunkJoiner, VadChunker

# main.ts と同じ規則。これ以上の空きは ⟨未認識⟩ として本文に残す。
GAP_SEC = 0.35

# 本文に残ってしまう定型句（ブロックリストの正規表現に載っていないもの）。
# 直す対象ではなく「どれだけ残ったか」を数えるための目安。
BOILERPLATE_MARKERS = [
    "ご視聴ありがとうございました", "ご覧いただきありがとうございました",
    "次の動画でお会いしましょう", "お会いしましょう", "今までの動画をご覧ください",
    "この動画をご覧ください", "次回予告", "チャンネル登録", "高評価", "お楽しみに",
    "お待ちしております", "お疲れ様でした", "おめでとうございます",
    "おやすみなさい", "おはようございます", "ブーブー",
]


def fmt_time(sec: float) -> str:
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


def build_chunks(audio: np.ndarray, *, far_mic: bool) -> list[Chunk]:
    """実運用（マイク文字起こし）と同じ刻み方でチャンクを作る。

    VAD の設定は main._build_chunker のマイク経路と同じ。遠いマイクでも音の
    切り方は変えない（壊れているのは連結だけなので。config.far_mic_join_sec 参照）。
    """
    chunker = VadChunker(
        sample_rate=config.sample_rate,
        silence_sec=min(config.silence_sec, 0.35),
        max_chunk_sec=min(config.max_chunk_sec, 12.0),
        min_speech_sec=0.1,
        vad_threshold=config.vad_threshold,
        speech_pad_sec=max(config.speech_pad_sec, 0.5),
    )
    joiner = ChunkJoiner(
        sample_rate=config.sample_rate,
        min_sec=config.far_mic_join_sec if far_mic else config.transcribe_join_sec,
        max_hold_sec=(
            config.far_mic_join_hold_sec if far_mic else config.transcribe_join_hold_sec
        ),
        break_sec=(
            config.far_mic_join_break_sec if far_mic else config.transcribe_join_break_sec
        ),
    )
    out: list[Chunk] = []
    block = config.sample_rate // 10  # 100ms（プラグインの送出粒度）
    for i in range(0, len(audio), block):
        # 実時間ではなく音声上の位置を「今」として渡す。連結の判断は live と同じ。
        now = (i + block) / config.sample_rate
        for chunk in chunker.push(audio[i : i + block]):
            out.extend(joiner.push(chunk, now=now))
        out.extend(joiner.tick(now=now))
    tail = chunker.flush()
    if tail is not None:
        out.extend(joiner.push(tail, now=len(audio) / config.sample_rate))
    out.extend(joiner.flush())
    return out


class Stats:
    def __init__(self) -> None:
        self.chunks = 0
        self.blocked = 0
        self.blocked_sec = 0.0
        self.empty = 0
        self.gaps = 0
        self.gap_sec = 0.0
        self.recovered = 0
        self.recovered_sec = 0.0


def _recover_gap(
    transcriber,
    audio: np.ndarray,
    start: int,
    end: int,
    *,
    dictionary,
) -> str:
    """VADが捨てた区間を、実運用と同じ条件で拾い直す（main.enqueue と同じ判断）。

    音声根拠（RMS・有音フレーム比率）を満たさない区間は触らない。ほぼ無音の
    相談やマイク離席を無理に文字化しても、出てくるのは幻覚だけなので。
    """
    span = audio[start:end]
    if span.size == 0:
        return ""
    evidence = speech_evidence(
        span, config.sample_rate, active_rms=config.retry_active_rms
    )
    if not evidence.supports_retry(
        min_rms=config.retry_min_rms, min_active_ratio=config.retry_active_ratio
    ):
        return ""
    result = transcriber.transcribe(
        span, opts=AsrOptions.recovery(), hallucinations=dictionary.hallucinations
    )
    # 音声根拠を通っても、確信度が低い結果と定型句は採用しない。
    if (
        not result.text
        or result.avg_logprob is None
        or result.avg_logprob < config.retry_min_logprob
    ):
        return ""
    return postprocess(
        result.text,
        strip_space=config.strip_ja_alnum_space,
        symbol_dictation=False,
        replacements=dictionary.replacement_plan,
        symbols=dictionary.symbols,
        auto_punctuate=config.enable_auto_punctuation,
    )


def transcribe_chunks(
    chunks: list[Chunk],
    *,
    audio: np.ndarray,
    dictionary,
    paragraphs: bool,
    recover_gaps: bool = True,
    mark_recovered: bool = True,
    progress: bool = True,
) -> tuple[str, Stats]:
    """チャンク列を、実運用と同じ後処理を通して本文に組み立てる。

    audio は build_chunks に渡したものと同じ配列。欠落区間の再認識で使う
    （ChunkJoiner は隣接チャンクしか繋がないので、チャンク間の隙間＝VADが
    捨てた区間がそのまま残っている）。
    """
    transcriber = asr.hq_transcriber()
    transcriber.ensure_loaded()
    opts = AsrOptions.transcription()
    breaker = (
        ParagraphBreaker(
            min_chars=config.paragraph_chars,
            pause_sec=config.paragraph_pause_sec,
            max_chars=config.paragraph_max_chars,
            hard_chars=config.paragraph_hard_chars,
        )
        if paragraphs
        else None
    )

    body: list[str] = []
    stats = Stats()
    last_end = 0.0
    prev_end_samples = 0
    t0 = time.time()
    for n, chunk in enumerate(chunks, 1):
        start = chunk.start / config.sample_rate
        end = chunk.end / config.sample_rate

        # VADが捨てた区間を、実運用と同じ条件で先に拾い直す。
        gap_samples = chunk.start - prev_end_samples
        if (
            recover_gaps
            and prev_end_samples > 0
            and int(config.retry_gap_min_sec * config.sample_rate)
            <= gap_samples
            <= int(config.retry_gap_max_sec * config.sample_rate)
        ):
            recovered = _recover_gap(
                transcriber, audio, prev_end_samples, chunk.start, dictionary=dictionary
            )
            if recovered:
                # 復旧分は本来 VAD が「発話なし」と判断した区間で、他より確度が低い。
                # 引用する前に音を確かめられるよう、時刻つきで印を残す（既定ON）。
                # ⟨未認識⟩ と同じ記法にしてあるので、まとめて検索・除去できる。
                body.append(
                    f"⟨復旧 {fmt_time(prev_end_samples / config.sample_rate)}–"
                    f"{fmt_time(start)} {recovered}⟩"
                    if mark_recovered
                    else recovered
                )
                stats.recovered += 1
                stats.recovered_sec += gap_samples / config.sample_rate
                last_end = start  # 埋まったので ⟨未認識⟩ は出さない
        prev_end_samples = max(prev_end_samples, chunk.end)

        result = transcriber.transcribe(
            chunk.audio, opts=opts, hallucinations=dictionary.hallucinations
        )
        raw = result.text
        if raw:
            evidence = speech_evidence(
                chunk.audio, config.sample_rate, active_rms=config.retry_active_rms
            )
            raw, contextual = filter_contextual_artifacts(
                raw,
                evidence,
                weak_rms=config.retry_min_rms,
                weak_active_ratio=config.retry_active_ratio,
            )
            result.blocked.extend(contextual)
        text = postprocess(
            raw,
            strip_space=config.strip_ja_alnum_space,
            symbol_dictation=False,  # 録音の書き起こしを記号命令で書き換えない
            replacements=dictionary.replacement_plan,
            symbols=dictionary.symbols,
            auto_punctuate=config.enable_auto_punctuation,
        )

        if not text:
            if result.blocked:
                # 定型句として捨てた範囲は ⟨未認識⟩ にしない（実運用と同じ扱い）。
                stats.blocked += 1
                stats.blocked_sec += end - start
            else:
                stats.empty += 1
            last_end = max(last_end, end)
            continue

        # 実運用ではクライアントが置く欠落マーカーを、ここで同じ規則で入れる。
        if last_end > 0 and start - last_end >= GAP_SEC:
            body.append(f"⟨未認識 {fmt_time(last_end)}–{fmt_time(start)}⟩")
            stats.gaps += 1
            stats.gap_sec += start - last_end
        if breaker is not None:
            text = breaker.feed(text, chunk.pause) + text
        body.append(text)
        stats.chunks += 1
        last_end = end

        if progress and n % 50 == 0:
            done = end or 1e-6
            rtf = (time.time() - t0) / done
            print(
                f"  [{fmt_time(end)}] {n}/{len(chunks)}チャンク "
                f"(RTF {rtf:.2f}, 残り約{(chunks[-1].end / config.sample_rate - end) * rtf / 60:.1f}分)",
                file=sys.stderr,
            )
    return "".join(body), stats


def report(label: str, text: str, stats: Stats, elapsed: float) -> None:
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    print(f"\n--- {label}")
    print(
        f"  本文 {len(text):,}字 / 段落 {len(paragraphs)}"
        + (f" / 平均{sum(len(p) for p in paragraphs) // len(paragraphs)}字" if paragraphs else "")
    )
    print(f"  採用チャンク {stats.chunks} / 空 {stats.empty}")
    print(f"  定型句として破棄 {stats.blocked}件 ({stats.blocked_sec:.0f}秒)")
    print(f"  欠落を自動復旧 {stats.recovered}件 ({stats.recovered_sec:.0f}秒)")
    print(f"  ⟨未認識⟩ {stats.gaps}件 ({stats.gap_sec:.0f}秒)")
    残り = {p: text.count(p) for p in BOILERPLATE_MARKERS if text.count(p)}
    total = sum(残り.values())
    print(f"  本文に残った定型句 {total}件" + (f" — {残り}" if 残り else ""))
    print(f"  所要 {elapsed:.0f}秒")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("session", nargs="?", help="セッションID（recordings/ 配下）")
    ap.add_argument("--list", action="store_true", help="保存済みの録音を一覧する")
    ap.add_argument("--far-mic", action="store_true",
                    help="遠いマイク（会見・発表会）の連結で回す")
    ap.add_argument("--compare", action="store_true",
                    help="現行の連結と遠いマイクの両方で回して比べる")
    ap.add_argument("--out", type=Path, help="本文の書き出し先（.md）")
    ap.add_argument("--limit-sec", type=float, help="先頭N秒だけ（お試し用）")
    ap.add_argument("--no-paragraphs", action="store_true", help="段落分けをしない")
    ap.add_argument("--no-recover", action="store_true",
                    help="欠落区間の自動再認識をしない（実運用は既定で行う）")
    ap.add_argument("--no-mark-recovered", action="store_true",
                    help="復旧した区間に ⟨復旧 …⟩ の印を付けない")
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
        f"モデル {resolve_model_name(config.transcribe_model)} / "
        f"辞書 {dictionary.set_id}"
    )

    profiles = [False, True] if args.compare else [bool(args.far_mic)]
    results: list[tuple[str, str, Stats]] = []
    for far_mic in profiles:
        label = "遠いマイク" if far_mic else "現行の連結"
        chunks = build_chunks(audio, far_mic=far_mic)
        lens = np.array([c.audio.size / config.sample_rate for c in chunks])
        print(
            f"\n# {label}: {len(chunks)}チャンク "
            f"中央{np.median(lens):.1f}秒 / 4秒未満 {int((lens < 4).sum())}件"
            f"({(lens < 4).mean() * 100:.0f}%)"
        )
        t0 = time.time()
        text, stats = transcribe_chunks(
            chunks,
            audio=audio,
            dictionary=dictionary,
            paragraphs=not args.no_paragraphs,
            recover_gaps=not args.no_recover,
            mark_recovered=not args.no_mark_recovered,
        )
        report(label, text, stats, time.time() - t0)
        results.append((label, text, stats))

    # 書き出しは「実際に使いたい方」＝最後に回した条件。
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(results[-1][1], encoding="utf-8")
        print(f"\n# 書き出しました（{results[-1][0]}）: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
