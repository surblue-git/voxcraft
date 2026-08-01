"""保存済み録音と実運用の文字起こし結果を突き合わせ、精度改善の材料を出す。

文字起こしモードは音声を丸ごと残している（recording.py）ので、後から
「同じ音声を別条件で認識し直した結果」と「実際にノートへ入ったテキスト」を
比較できる。本スクリプトはその比較をローカルで完結させ、**人間（やLLM）が
読むべき箇所だけ**を抜き出したレポートに落とす。1時間の録音でも、レポートは
数十KBに収まる。

なぜ「通し認識」を参照にするか
------------------------------
実運用（transcribe モード）は VadChunker が 0.5秒無音・最大12秒で音声を刻み、
チャンクごとに condition_on_previous=False で認識している。つまり
**チャンク境界で語が切れる／文脈が途切れる**のが構造的な弱点で、ここが誤りの
主要因になりやすい。一方 AsrOptions.recovery() は閾値と beam が少し違うだけで、
切り方は同じ土俵に乗らない ＝ 差分がほとんど出ない。
そこで参照系は「WAVを丸ごと1回で認識」する。Whisper 本来の30秒窓と文脈が
効くので、実運用との差がそのまま「刻んだことによる損失」として現れる。

なぜ VAD を有効にするか
-----------------------
recovery() が vad_filter=False なのは、対象が「発話済みと分かっている切片」だから。
1時間の生セッションには長い無音が含まれ、無音区間は Whisper が幻覚を吐く定番の
場所なので、通し認識では VAD を効かせる（既定ON。--no-vad で外せる）。

出力する4つの観点
-----------------
  A. 差分      : 実運用テキスト vs 通し認識。刻んだことによる誤り・脱落が出る。
  B. 低確信    : 通し認識の avg_logprob / compression_ratio が悪いセグメント。
                 **両方が同じく間違う系**（固有名詞・専門用語）はAに出ないので、
                 このランキングで拾う。辞書 replacements / initial_prompt の種。
  C. 取りこぼし: 通し認識のセグメントに実運用の閾値を当て、捨てられる側を列挙。
                 「無音判定で消えた」実害を定量化する。
  D. 用語候補  : 頻出のカタカナ語・英数語。表記ゆれ・誤変換の当たりを付ける。

このスクリプトはサーバーのコードを一切変更せず、読み取りだけで動く。
稼働中のサーバーとは別プロセスなので、GPUメモリが競合する場合はサーバーを
止めてから実行すること。

使い方
------
  python analyze_session.py --list
  python analyze_session.py 20260728-101530 --note "D:/vault/会議.md"
  python analyze_session.py 20260728-101530 --note 会議.md --clips
  python analyze_session.py 20260728-101530 --limit-sec 300   # まず5分で試す
"""
from __future__ import annotations

import argparse
import bisect
import difflib
import json
import re
import sys
import time
import unicodedata
import wave
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import asr
from config import config
from postproc import normalize_fullwidth_ascii, postprocess
from recording import RECORDINGS_DIR, list_sessions, load_slice, resolve_session_path
from userdict import get_replacements, get_symbols

ANALYSIS_DIR = Path(__file__).resolve().parent / "analysis"


# --- 正規化 -----------------------------------------------------------------
# 比較の前に、両者で必ず食い違う要素（句読点・空白・改行・装飾記号）を落とす。
# 実運用テキストには postprocess() の句読点付与とクライアント側の息継ぎ読点が
# 乗っているため、素で diff すると句読点差分でレポートが埋まって使えない。
# 長音記号「ー」は語の一部なので絶対に落とさないこと。
_STRIP_CHARS = set(
    "。、，．,.!?！？「」『』〈〉《》【】〔〕（）()[]{}…‥・:：;；\"'“”‘’"
    "|/\\*#>=+_~〜～`^&$@%"
)


def normalize(text: str) -> tuple[str, list[int]]:
    """比較用に正規化した文字列と、元テキストへの添字対応を返す。

    NFKC は1文字が複数文字に展開されうる（㍉→ミリ）ので、展開後の各文字を
    すべて同じ元添字に対応付けて、後から元の生テキストを復元できるようにする。
    """
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch):
            if c.isspace() or c in _STRIP_CHARS:
                continue
            out.append(c.lower())
            idx.append(i)
    return "".join(out), idx


# --- ノート（実運用結果）の読み込み ------------------------------------------
_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.S)
_FENCE = re.compile(r"```.*?```", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WIKILINK = re.compile(r"\[\[([^\]|]*)(?:\|([^\]]*))?\]\]")
_LINE_PREFIX = re.compile(r"(?m)^\s{0,3}(?:[#>]+|[-*+]|\d+\.)\s+")
# 装飾記号は正規化でも落ちるが、レポートに生テキストを出す都合で先に消しておく
# （「**VoxCraft**」のまま表示されると差分が読みにくい）。
_EMPHASIS = re.compile(r"\*\*|~~|==|`")


def load_note(path: Path) -> str:
    """ノートから本文だけを取り出す（URL等の非発話テキストを混ぜない）。"""
    text = path.read_text(encoding="utf-8")
    text = _FRONTMATTER.sub("", text)
    text = _FENCE.sub("", text)
    text = _HTML_COMMENT.sub("", text)
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _WIKILINK.sub(lambda m: m.group(2) or m.group(1), text)
    text = _LINE_PREFIX.sub("", text)
    text = _EMPHASIS.sub("", text)
    return text


# --- 通し認識 ---------------------------------------------------------------
@dataclass
class Seg:
    start: float
    end: float
    text: str
    logprob: float
    no_speech: float
    compression: float
    # 参照テキスト（全セグメント連結）の中でこのセグメントが占める範囲。
    raw_start: int = 0
    raw_end: int = 0


def load_model(model_name: str, device: str | None, compute: str | None):
    """faster-whisper のモデルを直接ロードする。

    asr.Transcriber を使わないのは、あちらが text と dropped しか返さず、
    ここで必要な per-segment の avg_logprob / no_speech_prob / timestamp が
    取れないため。CUDA DLL 解決とデバイス判定だけ asr の実装を借りる
    （サーバー側と同じ条件でロードするため）。
    """
    asr._ensure_cuda_dll_dirs()
    from faster_whisper import WhisperModel

    resolved = asr.resolve_model_name(model_name)
    dev, comp = asr._resolve_device_compute()
    dev = device or dev
    comp = compute or comp
    try:
        model = WhisperModel(resolved, device=dev, compute_type=comp)
    except Exception as exc:
        print(f"[analyze] {dev}/{comp} 初期化失敗（{str(exc)[:120]}）。CPUに切替。", file=sys.stderr)
        dev, comp = "cpu", "int8"
        model = WhisperModel(resolved, device=dev, compute_type=comp)
    return model, resolved, dev, comp


def transcribe_whole(
    model,
    source,
    *,
    beam_size: int,
    vad: bool,
    condition: bool,
    total_sec: float,
    prompt: str | None = None,
) -> list[Seg]:
    """WAVを丸ごと1回で認識し、セグメントを素のまま集める。

    ここでは閾値による破棄を一切しない。何を捨てるかは後段で「実運用の閾値なら
    どうなったか」として評価するので、素材は全部持っておく必要がある。
    """
    segments, info = model.transcribe(
        source,
        language=config.language,
        task="transcribe",
        initial_prompt=prompt,
        vad_filter=vad,
        condition_on_previous_text=condition,
        beam_size=beam_size,
    )
    out: list[Seg] = []
    t0 = time.time()
    last_report = 0.0
    for seg in segments:
        out.append(
            Seg(
                start=float(seg.start),
                end=float(seg.end),
                text=(seg.text or "").strip(),
                logprob=float(getattr(seg, "avg_logprob", 0.0) or 0.0),
                no_speech=float(getattr(seg, "no_speech_prob", 0.0) or 0.0),
                compression=float(getattr(seg, "compression_ratio", 0.0) or 0.0),
            )
        )
        # 進捗（1時間の音声は数分〜十数分かかるので、黙って止まって見えないように）。
        if seg.end - last_report >= 60.0:
            last_report = seg.end
            elapsed = time.time() - t0
            rtf = elapsed / max(seg.end, 1e-6)
            remain = (total_sec - seg.end) * rtf
            print(
                f"[analyze] {fmt_time(seg.end)} / {fmt_time(total_sec)} "
                f"(RTF {rtf:.2f}, 残り約{remain / 60:.1f}分)",
                file=sys.stderr,
            )
    print(f"[analyze] 通し認識 完了: {len(out)} セグメント / {time.time() - t0:.0f}秒", file=sys.stderr)
    return out


def build_reference(segs: list[Seg], *, apply_dict: bool) -> str:
    """セグメント列から参照テキストを組み立て、各セグメントの占有範囲を記録する。

    実運用テキストには辞書置換（ウィンドウズ→Windows 等）が効いている。
    参照側に同じ置換をかけておかないと、辞書で既に直っている語がすべて差分として
    出てきてレポートが埋まる。既定で実運用と同じ後処理を通す。
    句読点付与(auto_punctuate)は正規化で落ちるので不要 ＝ sudachi 依存も避ける。
    """
    reps = get_replacements() if apply_dict else []
    syms = get_symbols() if apply_dict else {}
    parts: list[str] = []
    pos = 0
    for s in segs:
        t = s.text
        if apply_dict:
            t = postprocess(
                t,
                strip_space=config.strip_ja_alnum_space,
                symbol_dictation=config.enable_symbol_dictation,
                replacements=reps,
                symbols=syms,
                auto_punctuate=False,
            )
        else:
            t = normalize_fullwidth_ascii(t)
        s.raw_start = pos
        s.raw_end = pos + len(t)
        parts.append(t)
        pos = s.raw_end
    return "".join(parts)


# --- 差分 -------------------------------------------------------------------
@dataclass
class Hunk:
    kind: str          # 相違 / 脱落 / 余分
    at_sec: float      # 参照側での推定時刻（秒）。負なら不明。
    live: str          # 実運用テキスト側の該当箇所（生）
    ref: str           # 通し認識側の該当箇所（生）
    live_ctx: tuple[str, str] = ("", "")
    ref_ctx: tuple[str, str] = ("", "")


def slice_raw(raw: str, idx_map: list[int], lo: int, hi: int) -> str:
    """正規化後の範囲 [lo, hi) に対応する生テキストを取り出す。"""
    if lo >= hi:
        return ""
    return raw[idx_map[lo] : idx_map[hi - 1] + 1]


def context_raw(raw: str, idx_map: list[int], lo: int, hi: int, width: int) -> tuple[str, str]:
    """差分箇所の前後文脈を生テキストで返す（読んで場所が分かる程度に）。"""
    if not idx_map:
        return "", ""
    lo = min(max(lo, 0), len(idx_map))
    hi = min(max(hi, 0), len(idx_map))
    before_lo = max(0, lo - width)
    left = slice_raw(raw, idx_map, before_lo, lo) if lo > before_lo else ""
    after_hi = min(len(idx_map), hi + width)
    right = slice_raw(raw, idx_map, hi, after_hi) if after_hi > hi else ""
    return left.replace("\n", " "), right.replace("\n", " ")


def time_at(segs: list[Seg], starts: list[int], raw_pos: int) -> float:
    """参照テキスト上の位置から、そこを喋っていた時刻を引く。"""
    if not segs:
        return -1.0
    i = bisect.bisect_right(starts, raw_pos) - 1
    i = min(max(i, 0), len(segs) - 1)
    return segs[i].start


def diff_hunks(
    live_raw: str,
    ref_raw: str,
    segs: list[Seg],
    *,
    merge_gap: int,
    ctx_width: int,
    min_len: int,
) -> tuple[list[Hunk], float, dict]:
    live_n, live_idx = normalize(live_raw)
    ref_n, ref_idx = normalize(ref_raw)

    # autojunk=True は「頻出文字をjunk扱い」する挙動で、日本語の「の」「い」等が
    # 巻き添えになり対応付けが壊れる。必ず False。両者は9割方一致するので、
    # 一致ブロックが大きく、この規模でも実用速度で終わる。
    sm = difflib.SequenceMatcher(None, live_n, ref_n, autojunk=False)
    ops = [op for op in sm.get_opcodes() if op[0] != "equal"]

    # 近接した差分をまとめる（1文字ずつバラバラに並べても読めないため）。
    merged: list[list[int]] = []
    for _tag, i1, i2, j1, j2 in ops:
        if merged and i1 - merged[-1][1] <= merge_gap and j1 - merged[-1][3] <= merge_gap:
            merged[-1][1] = i2
            merged[-1][3] = j2
        else:
            merged.append([i1, i2, j1, j2])

    starts = [s.raw_start for s in segs]
    hunks: list[Hunk] = []
    for i1, i2, j1, j2 in merged:
        live_s = slice_raw(live_raw, live_idx, i1, i2).strip()
        ref_s = slice_raw(ref_raw, ref_idx, j1, j2).strip()
        if not live_s and not ref_s:
            continue
        if not live_s:
            kind = "脱落"      # 通し認識にはあるが実運用に無い ＝ 取りこぼし
        elif not ref_s:
            kind = "余分"      # 実運用にだけある ＝ 幻覚・重複の疑い
        else:
            kind = "相違"      # 同じ箇所を別の語に ＝ 誤変換の疑い
        # min_len は片側だけの微小差分（記号の残りかす等）を捨てるためのもの。
        # 両側に中身がある置換は1文字でも残す —「移動→異動」のような1文字の
        # 漢字誤変換は日本語で最も多い誤りで、ここを落とすと本題を見逃す。
        if kind != "相違" and max(len(live_s), len(ref_s)) < min_len:
            continue
        pos = ref_idx[j1] if j1 < len(ref_idx) else (ref_idx[-1] if ref_idx else 0)
        hunks.append(
            Hunk(
                kind=kind,
                at_sec=time_at(segs, starts, pos),
                live=live_s,
                ref=ref_s,
                live_ctx=context_raw(live_raw, live_idx, i1, i2, ctx_width),
                ref_ctx=context_raw(ref_raw, ref_idx, j1, j2, ctx_width),
            )
        )

    stats = {
        "live_chars": len(live_n),
        "ref_chars": len(ref_n),
        "hunks": len(hunks),
        "by_kind": dict(Counter(h.kind for h in hunks)),
    }
    return hunks, sm.ratio(), stats


# --- 表示ヘルパ -------------------------------------------------------------
def fmt_time(sec: float) -> str:
    if sec < 0:
        return "--:--:--"
    s = int(sec)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def fmt_ctx(ctx: tuple[str, str], mid: str) -> str:
    left, right = ctx
    mid = mid.replace("\n", " ") or "∅"
    return f"…{left}【{mid}】{right}…"


# --- 用語候補 ---------------------------------------------------------------
_KATAKANA = re.compile(r"[ァ-ヴー]{3,}")
_ALNUM = re.compile(r"[A-Za-z][A-Za-z0-9._-]{2,}")


def vocab_candidates(text: str, top: int) -> list[tuple[str, int]]:
    """頻出のカタカナ語・英数語を数える。

    通し認識と実運用の両方が同じく誤る語（固有名詞・専門用語）は差分に出ない。
    頻度の高い語を目で流せば「毎回この表記で間違えている」が見つかり、
    辞書 replacements に1行足すだけで全体が直る、という当たりが付けられる。
    """
    words = _KATAKANA.findall(text) + _ALNUM.findall(text)
    return Counter(w for w in words if len(w) >= 3).most_common(top)


# --- 繰り返し暴走の検出 -----------------------------------------------------
def find_repeats(
    ref_raw: str, segs: list[Seg], *, min_len: int = 4, max_len: int = 40
) -> list[tuple[float, str]]:
    """直後に同じ文字列が続く箇所（「比べると比べると」）を拾う。

    セグメント単位の compression_ratio では**セグメントをまたぐ繰り返し**を
    検出できない。実測では1セグメントが5秒程度と短く、暴走は隣接セグメントに
    分かれて現れるため、per-segment の指標は 2.4 を一度も超えなかった。
    そこで参照テキスト全体を走査して、隣接する反復を直接見つける。
    """
    norm, idx_map = normalize(ref_raw)
    starts = [s.raw_start for s in segs]
    found: list[tuple[float, str]] = []
    i = 0
    n = len(norm)
    while i < n:
        hit = 0
        # 長い反復を優先（「AAAA」を「AA」×2と報告しない）。
        for size in range(min_len, max_len + 1):
            if i + 2 * size > n:
                break
            if norm[i : i + size] == norm[i + size : i + 2 * size]:
                hit = size
        if hit:
            pos = idx_map[i] if i < len(idx_map) else 0
            found.append((time_at(segs, starts, pos), norm[i : i + hit]))
            i += 2 * hit
        else:
            i += 1
    return found


# --- ノートの範囲合わせ -----------------------------------------------------
def align_note_to_reference(
    live_raw: str, ref_raw: str, *, probe: int = 160, min_match: int = 12
) -> tuple[str, str]:
    """参照が音声の一部だけのとき、ノート側を対応する範囲に切り詰める。

    --limit-sec で先頭5分だけ認識した参照を、50分ぶんのノート全体と比べると
    一致率は無意味な値になり、レポートは差分だらけの紙くずになる（実測: 一致率
    11%、ハンク28件がすべて巨大な「相違」）。参照の末尾がノートのどこに当たるかを
    探して、そこで切る。
    """
    live_n, live_idx = normalize(live_raw)
    ref_n, _ = normalize(ref_raw)
    if not live_n or not ref_n:
        return live_raw, "空のテキスト"
    if len(live_n) <= len(ref_n) * 1.3:
        return live_raw, ""  # 長さが近い ＝ 全体同士の比較。切らない

    tail = ref_n[-probe:]
    sm = difflib.SequenceMatcher(None, live_n, tail, autojunk=False)
    m = sm.find_longest_match(0, len(live_n), 0, len(tail))
    if m.size < min_match:
        return live_raw, (
            f"⚠ 参照({len(ref_n)}字)がノート({len(live_n)}字)より大幅に短いのに、"
            f"対応位置を特定できませんでした（最長一致 {m.size}字）。"
            "A節の差分は信用できません。--limit-sec を外して全体を解析してください。"
        )
    cut = m.a + m.size
    raw_cut = live_idx[cut - 1] + 1 if 0 < cut <= len(live_idx) else len(live_raw)
    return live_raw[:raw_cut], (
        f"ℹ 音声が部分的なため、ノートを先頭 {cut} 字（全体 {len(live_n)} 字）に"
        f"切り詰めて比較しました（末尾 {m.size} 字で一致）。"
    )


# --- クリップ切り出し -------------------------------------------------------
def write_clip(session: str, start: float, end: float, out: Path, rate: int) -> None:
    audio = load_slice(session, start, end)
    if audio.size == 0:
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())


# --- レポート ---------------------------------------------------------------
def build_report(
    *,
    session: str,
    duration: float,
    model_info: dict,
    segs: list[Seg],
    hunks: list[Hunk],
    ratio: float | None,
    stats: dict | None,
    note_path: Path | None,
    ref_raw: str,
    top: int,
    max_hunks: int,
    align_note: str = "",
) -> str:
    L: list[str] = []
    A = L.append

    A(f"# 文字起こし精度レポート: {session}")
    A("")
    A(f"- 音声長: {fmt_time(duration)}")
    A(f"- モデル: `{model_info['model']}` / {model_info['device']} / {model_info['compute']}")
    A(f"- 通し認識: beam={model_info['beam']}, vad={model_info['vad']}, "
      f"condition_on_previous={model_info['condition']}")
    A(f"- セグメント数: {len(segs)}")
    if note_path:
        A(f"- 実運用テキスト: `{note_path}`")
    A("")

    # --- A. 差分 ---
    A("## A. 実運用 vs 通し認識 の差分")
    A("")
    if not hunks and ratio is None:
        A("（--note 未指定のためスキップ）")
        A("")
    else:
        if align_note:
            A(f"> {align_note}")
            A("")
        kinds = "、".join(f"{k} {v}件" for k, v in stats["by_kind"].items()) or "なし"
        A(f"一致率(正規化後): **{ratio:.3%}** / 差分ハンク: **{stats['hunks']}件**（{kinds}）")
        A(f"文字数: 実運用 {stats['live_chars']} / 通し認識 {stats['ref_chars']}")
        A("")
        A("- **脱落** = 通し認識にはあるが実運用に無い。")
        A("- **余分** = 実運用にだけある。")
        A("- **相違** = 同じ箇所が別の語。")
        A("")
        A("> **通し認識は「正解」ではない。** 実測では、蒸留モデル(kotoba-whisper)の"
          "通し認識のほうが固有名詞を誤り、反復も多かった（実運用が ATM 20回に対し"
          "通し認識は 11回＋「エテム」3回）。この節は**どちらが正しいかを示すものではなく、"
          "2系統が食い違った＝確認する価値がある箇所**の一覧として読むこと。"
          "確定した誤りを探すなら D節（頻出語）のほうが確実。")
        A("")
        shown = hunks[:max_hunks]
        for n, h in enumerate(shown, 1):
            A(f"### {n}. [{fmt_time(h.at_sec)}] {h.kind}")
            A("")
            A(f"- 実運用: {fmt_ctx(h.live_ctx, h.live)}")
            A(f"- 通し　: {fmt_ctx(h.ref_ctx, h.ref)}")
            A("")
        if len(hunks) > len(shown):
            A(f"（他 {len(hunks) - len(shown)} 件は --max-hunks を上げると出ます）")
            A("")

    # --- B. 低確信 ---
    A("## B. 低確信セグメント（両方が同じく誤っている可能性）")
    A("")
    A("差分に出ない誤り＝どちらの条件でも同じく誤変換した箇所は、ここに現れやすい。")
    A("`logprob` が低い / `compression` が高い（2.4超は繰り返し暴走の定番）を疑う。")
    A("")
    worst = sorted(segs, key=lambda s: s.logprob)[:top]
    A("| 時刻 | logprob | no_speech | compression | テキスト |")
    A("|---|---|---|---|---|")
    for s in worst:
        t = s.text.replace("|", "｜")[:60]
        A(f"| {fmt_time(s.start)} | {s.logprob:.2f} | {s.no_speech:.2f} | "
          f"{s.compression:.2f} | {t} |")
    A("")

    loops = [s for s in segs if s.compression >= 2.4]
    if loops:
        A(f"セグメント内の繰り返し（compression ≥ 2.4）: **{len(loops)} 件**")
        A("")
        for s in loops[:15]:
            A(f"- [{fmt_time(s.start)}] ({s.compression:.2f}) {s.text[:80]}")
        A("")

    # セグメントをまたぐ反復は compression では捕まらないので別途走査する。
    reps = find_repeats(ref_raw, segs)
    A(f"### 直後反復（繰り返し暴走）: **{len(reps)} 件**")
    A("")
    A("「比べると比べると」のように同じ語が直後に繰り返される箇所。"
      "セグメントをまたぐため compression では検出できない。")
    A("")
    if reps:
        for t, s in reps[:30]:
            A(f"- [{fmt_time(t)}] 「{s}」")
        if len(reps) > 30:
            A(f"- （他 {len(reps) - 30} 件）")
    else:
        A("（該当なし）")
    A("")

    # --- C. 実運用の閾値なら捨てられた分 ---
    A("## C. 実運用の閾値で捨てられる側のセグメント")
    A("")
    tr = asr.AsrOptions.transcription()
    di = asr.AsrOptions.dictation()
    for label, o in (("文字起こし", tr), ("口述（参考）", di)):
        dropped = [
            s for s in segs
            if s.no_speech > o.no_speech_threshold or s.logprob < o.logprob_threshold
        ]
        A(f"### {label}: no_speech>{o.no_speech_threshold} または "
          f"logprob<{o.logprob_threshold} → **{len(dropped)} / {len(segs)} 件**")
        A("")
        if not dropped:
            A("（該当なし）")
        for s in dropped[:20]:
            why = []
            if s.no_speech > o.no_speech_threshold:
                why.append(f"no_speech={s.no_speech:.2f}")
            if s.logprob < o.logprob_threshold:
                why.append(f"logprob={s.logprob:.2f}")
            A(f"- [{fmt_time(s.start)}] ({', '.join(why)}) {s.text[:70]}")
        if len(dropped) > 20:
            A(f"- （他 {len(dropped) - 20} 件）")
        A("")
    A("ここに**中身のある発話**が並ぶなら閾値が厳しすぎる。空文字や短い相槌ばかりなら妥当。")
    A("")

    # --- D. 用語候補 ---
    A("## D. 頻出の用語候補（辞書・initial_prompt の種）")
    A("")
    cands = vocab_candidates(ref_raw, top)
    A(" / ".join(f"{w}({c})" for w, c in cands) or "（該当なし）")
    A("")
    A("誤った表記が上位にあれば `dictionaries/profiles/common.json` の entries に足すだけで全体が直る。")
    A("**同じ語が2通りの綴りで並んでいたら要注意**（実測: 「リキット14/リキッド14」＝"
      "社名が半々で割れていた）。この節が最も確実に成果につながる。")
    A("")

    # --- 次の一手 ---
    A("## 次に何を触るか")
    A("")
    A("| 症状 | 打ち手 |")
    A("|---|---|")
    A("| C に中身のある発話が並ぶ | `AsrOptions.transcription()` の閾値を緩める（asr.py） |")
    A("| A の「脱落」が境界に集中 | `VOXCRAFT_SILENCE_SEC` / `MAX_CHUNK_SEC` / `SPEECH_PAD_SEC` |")
    A("| A の「相違」が固有名詞 | `dictionaries/profiles/common.json` の entries |")
    A("| B・D に専門用語の誤り | `VOXCRAFT_INITIAL_PROMPT` に語彙を足す |")
    A("| 全体的に苦しい | `VOXCRAFT_MODEL=large-v3` 等へ差し替えて再測定 |")
    A("| B の直後反復が多い | 蒸留モデル(kotoba)の長尺での弱点。`large-v3` で再測定して比較 |")
    A("")
    A("※ 口述(dictation)の挙動は変えないこと。調整は transcription 側だけで行う。")
    return "\n".join(L)


# --- main -------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description="録音と文字起こし結果を突き合わせて精度改善の材料を出す。"
    )
    p.add_argument("session", nargs="?", help="セッションID（例 20260728-101530）")
    p.add_argument("--list", action="store_true", help="保存済みの録音を一覧する")
    p.add_argument("--note", type=Path, help="実運用の文字起こし結果（.md / .txt）")
    p.add_argument("--out", type=Path, help="レポートの出力先（既定 analysis/<session>.md）")
    p.add_argument("--json", dest="json_out", type=Path, help="機械可読な結果の出力先")
    p.add_argument("--model", default=config.model, help="通し認識に使うモデル")
    p.add_argument("--device", help="cpu / cuda（既定は config の auto 解決）")
    p.add_argument("--compute", help="int8 / float16 / int8_float16 ...")
    p.add_argument("--beam", type=int, default=max(config.beam_size, 8),
                   help="通し認識のビーム幅（既定8＝精度寄り）")
    p.add_argument("--no-vad", action="store_true",
                   help="通し認識でVADを切る（無音区間の幻覚が増えるので非推奨）")
    p.add_argument("--condition", action="store_true",
                   help="condition_on_previous_text を有効化（文脈は効くが暴走リスク）")
    # 本番の transcription()/recovery() は initial_prompt を渡さない（turbo が崩れる）。
    # 解析も既定で揃える。プロンプトの影響を測りたいときだけ付ける。
    p.add_argument("--prompt", action="store_true",
                   help="config.initial_prompt を渡す（既定は渡さない＝本番と同条件）")
    p.add_argument("--no-dict", action="store_true",
                   help="参照側にユーザー辞書を適用しない（辞書の効き目を見たいとき）")
    p.add_argument("--limit-sec", type=float, help="先頭N秒だけ解析（お試し用）")
    p.add_argument("--reuse", action="store_true",
                   help="前回の通し認識結果(_segments.json)を再利用し、認識をやり直さない")
    p.add_argument("--top", type=int, default=40, help="低確信・用語候補の表示件数")
    p.add_argument("--max-hunks", type=int, default=60, help="差分の最大表示件数")
    p.add_argument("--context", type=int, default=24, help="差分の前後文脈の文字数")
    # 大きすぎると「脱落」と「余分」が1つのハンクに融合して「相違」に化ける。
    # 小さすぎると1語の言い直しが細切れに並ぶ。8前後が読みやすい。
    p.add_argument("--merge-gap", type=int, default=8, help="この距離内の差分はまとめる")
    p.add_argument("--min-len", type=int, default=2, help="これ未満の差分は無視する")
    p.add_argument("--clips", action="store_true", help="差分箇所の音声を切り出す")
    p.add_argument("--clip-pad", type=float, default=2.0, help="切り出しの前後余白（秒）")
    args = p.parse_args()

    if args.list or not args.session:
        items = list_sessions()
        if not items:
            print(f"録音がありません: {RECORDINGS_DIR}")
            return 0
        print(f"保存先: {RECORDINGS_DIR}")
        for i in items:
            mb = i["bytes"] / (1024 * 1024)
            print(f"  {i['session']}  {fmt_time(i['seconds'])}  {mb:8.1f} MB")
        if not args.session:
            print("\nセッションIDを指定してください。")
        return 0

    try:
        wav_path = resolve_session_path(args.session)
    except (ValueError, FileNotFoundError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    with wave.open(str(wav_path), "rb") as w:
        rate = w.getframerate()
        duration = w.getnframes() / float(rate)

    out = args.out or (ANALYSIS_DIR / f"{args.session}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    # 通し認識は数分〜数十分かかる。差分の見せ方を変えたいだけのときに毎回
    # やり直すのは無駄なので、セグメントは必ずJSONに残して --reuse で使い回す。
    cache = out.with_name(f"{out.stem}_segments.json")

    if args.reuse and cache.is_file():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        duration = saved.get("duration_sec", duration)
        model_info = saved["model"]
        segs = [
            Seg(
                start=d["start"], end=d["end"], text=d["text"],
                logprob=d["logprob"], no_speech=d["no_speech"],
                compression=d["compression"],
            )
            for d in saved["segments"]
        ]
        print(f"[analyze] 認識結果を再利用: {cache}（{len(segs)} セグメント）", file=sys.stderr)
    else:
        if args.reuse:
            print(f"[analyze] {cache} が無いので通常どおり認識します。", file=sys.stderr)
        # --limit-sec のときだけ配列で渡す。通常はパスを渡して faster-whisper に
        # ストリーミング復号させる（1時間ぶんを丸ごとメモリに載せないため）。
        if args.limit_sec:
            duration = min(duration, args.limit_sec)
            source = load_slice(args.session, 0.0, duration)
        else:
            source = str(wav_path)

        print(f"[analyze] {wav_path.name} ({fmt_time(duration)}) を解析します", file=sys.stderr)
        model, resolved, dev, comp = load_model(args.model, args.device, args.compute)
        print(f"[analyze] モデル {resolved} / {dev} / {comp}", file=sys.stderr)

        segs = transcribe_whole(
            model,
            source,
            beam_size=args.beam,
            vad=not args.no_vad,
            condition=args.condition,
            total_sec=duration,
            prompt=(config.initial_prompt or None) if args.prompt else None,
        )
        model_info = {
            "model": resolved, "device": dev, "compute": comp,
            "beam": args.beam, "vad": not args.no_vad, "condition": args.condition,
            "prompt": bool(args.prompt),
        }
        if segs:
            cache.write_text(
                json.dumps(
                    {
                        "session": args.session,
                        "duration_sec": duration,
                        "model": model_info,
                        "segments": [
                            {
                                "start": s.start, "end": s.end, "text": s.text,
                                "logprob": s.logprob, "no_speech": s.no_speech,
                                "compression": s.compression,
                            }
                            for s in segs
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            print(f"[analyze] 認識結果を保存: {cache}（--reuse で再利用可）", file=sys.stderr)

    if not segs:
        print("エラー: 認識結果が空です。音声かモデル設定を確認してください。", file=sys.stderr)
        return 1

    ref_raw = build_reference(segs, apply_dict=not args.no_dict)

    hunks: list[Hunk] = []
    ratio: float | None = None
    stats: dict | None = None
    align_msg = ""
    if args.note:
        if not args.note.is_file():
            print(f"エラー: ノートが見つかりません: {args.note}", file=sys.stderr)
            return 1
        live_raw = load_note(args.note)
        # 音声が部分的（--limit-sec）なのにノートは全体、という取り合わせは
        # そのまま比べると無意味なので、対応範囲へ切り詰める。
        live_raw, align_msg = align_note_to_reference(live_raw, ref_raw)
        if align_msg:
            print(f"[analyze] {align_msg}", file=sys.stderr)
        hunks, ratio, stats = diff_hunks(
            live_raw,
            ref_raw,
            segs,
            merge_gap=args.merge_gap,
            ctx_width=args.context,
            min_len=args.min_len,
        )
        print(f"[analyze] 差分 {len(hunks)} 件 / 一致率 {ratio:.3%}", file=sys.stderr)

    report = build_report(
        session=args.session,
        duration=duration,
        model_info=model_info,
        segs=segs,
        hunks=hunks,
        ratio=ratio,
        stats=stats,
        note_path=args.note,
        ref_raw=ref_raw,
        top=args.top,
        max_hunks=args.max_hunks,
        align_note=align_msg,
    )

    out.write_text(report, encoding="utf-8")
    print(f"[analyze] レポート: {out}", file=sys.stderr)

    # 通し認識の全文も残す（レポートには載せない＝読ませる分量を絞るため）。
    ref_out = out.with_name(f"{out.stem}_reference.txt")
    ref_out.write_text(ref_raw, encoding="utf-8")
    print(f"[analyze] 通し認識の全文: {ref_out}", file=sys.stderr)

    if args.json_out:
        payload = {
            "session": args.session,
            "duration_sec": duration,
            "model": model_info,
            "ratio": ratio,
            "stats": stats,
            "segments": [
                {
                    "start": s.start, "end": s.end, "text": s.text,
                    "logprob": s.logprob, "no_speech": s.no_speech,
                    "compression": s.compression,
                }
                for s in segs
            ],
            "hunks": [
                {"kind": h.kind, "at_sec": h.at_sec, "live": h.live, "ref": h.ref}
                for h in hunks
            ],
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[analyze] JSON: {args.json_out}", file=sys.stderr)

    if args.clips and hunks:
        clip_dir = out.with_name(f"{out.stem}_clips")
        n = 0
        for i, h in enumerate(hunks[:args.max_hunks], 1):
            if h.at_sec < 0:
                continue
            start = max(0.0, h.at_sec - args.clip_pad)
            end = min(duration, h.at_sec + args.clip_pad + 6.0)
            write_clip(
                args.session, start, end,
                clip_dir / f"{i:03d}_{fmt_time(h.at_sec).replace(':', '-')}.wav",
                rate,
            )
            n += 1
        print(f"[analyze] クリップ {n} 件: {clip_dir}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
