"""「変換戻し」= 再変換候補の取得。

流れ:
  1) 確定済みテキストを形態素解析して各語の読み（ひらがな）を復元（Sudachi）
  2) 読みを Google CGI API for Japanese Input に投げて文節ごとの変換候補を得る
  3) 文節と候補リストを返す（クライアントがモーダルで手/音声選択）

オフライン、または依存未導入のときは読みだけ返す（候補は空）。
Google CGI API は無料・非公式のため個人利用前提。廃止時は候補が空になるだけ。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib import parse, request

from config import config

# --- 読み抽出（Sudachi） --------------------------------------------------

_KATA_TO_HIRA = str.maketrans(
    {chr(c): chr(c - 0x60) for c in range(0x30A1, 0x30F7)}
)


def _kata_to_hira(text: str) -> str:
    return text.translate(_KATA_TO_HIRA)


class _Reading:
    """Sudachi があれば使い、無ければ入力をそのまま読みとして扱う。"""

    def __init__(self) -> None:
        self._tokenizer = None
        try:
            from sudachipy import dictionary  # type: ignore

            self._tokenizer = dictionary.Dictionary(dict="core").create()
        except Exception:
            self._tokenizer = None

    def to_hiragana(
        self,
        text: str,
        reverse_replacements: tuple[tuple[str, str], ...] | list[tuple[str, str]] | None = None,
    ) -> str:
        """テキスト全体の読み（ひらがな）を返す。

        ユーザー辞書の表記→読みを最優先で適用し、残りを Sudachi でひらがな化する。
        """
        if reverse_replacements is None:
            from userdict import get_reverse_replacements

            reverse_replacements = get_reverse_replacements()

        # 1. ユーザー辞書の逆引き（表記 → 読み）を適用
        for surface, hira in reverse_replacements:
            if surface and surface in text:
                text = text.replace(surface, hira)

        if self._tokenizer is None:
            # フォールバック: カタカナだけひらがな化して返す（漢字は読めない）。
            return _kata_to_hira(text)

        from sudachipy import tokenizer as _t  # type: ignore

        mode = _t.Tokenizer.SplitMode.C
        out = []
        for m in self._tokenizer.tokenize(text, mode):
            reading = m.reading_form()  # カタカナ
            out.append(_kata_to_hira(reading) if reading else m.surface())
        return "".join(out)


_reading = _Reading()


# --- Google CGI API for Japanese Input ------------------------------------

@dataclass
class Segment:
    """1文節分の変換結果。"""

    reading: str                       # ひらがな読み
    candidates: list[str] = field(default_factory=list)


def _google_transliterate(hiragana: str) -> list[Segment]:
    """ひらがな文字列を文節ごとの候補に変換する。

    API 応答は [[原文, [候補...]], ...] という JSON 配列。
    """
    params = parse.urlencode({"langpair": "ja-Hira|ja", "text": hiragana})
    url = f"{config.google_cgi_url}?{params}"
    req = request.Request(url, headers={"User-Agent": "VoxCraft/1.0"})
    with request.urlopen(req, timeout=config.http_timeout_sec) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    segments: list[Segment] = []
    for entry in data:
        reading = entry[0]
        candidates = list(entry[1]) if len(entry) > 1 else []
        segments.append(Segment(reading=reading, candidates=candidates))
    return segments


# --- 読みの揺らし ---------------------------------------------------------
#
# なぜ要るか
# ----------
# 再変換は「読みは合っているが漢字が違う」誤変換のための道具で、読み自体が
# 外れていると原理的に正解へ到達できない。ところが音声入力で本当に困るのは
# まさにその読みが外れる方で、しかも言い直しても同じように外れる（調音の癖や
# 濁点の弱さは再現するため）。実測 2026-08-05:
#   「では」→『ては』   テハ からは デハ の変換候補が出ない
#   「へんかん」→『へんか』（撥音の脱落）
# そこで読みを1箇所だけ機械的に揺らし、その読みの変換候補も一緒に見せる。
# 揺らしは列挙なのでローカルで完結し、判断はユーザーがする。

# 清音・濁音・半濁音のグループ。同じ行の別表記へ入れ替えたものを候補にする。
_VOICING_GROUPS = (
    "かが", "きぎ", "くぐ", "けげ", "こご",
    "さざ", "しじ", "すず", "せぜ", "そぞ",
    "ただ", "ちぢ", "つづ", "てで", "とど",
    "はばぱ", "ひびぴ", "ふぶぷ", "へべぺ", "ほぼぽ",
)
_VOICING_ALTERNATIVES: dict[str, tuple[str, ...]] = {}
for _group in _VOICING_GROUPS:
    for _char in _group:
        _VOICING_ALTERNATIVES[_char] = tuple(c for c in _group if c != _char)

# 促音は無声子音の前にしか立たない。ここを見ないと「てっは」「へっんか」のような
# 日本語に無い読みまで変換にかけ、返ってきた無意味な候補が本命を押し出す。
_SOKUON_FOLLOWERS = frozenset("かきくけこさしすせそたちつてとぱぴぷぺぽ")


def _is_plausible_reading(hira: str) -> bool:
    """日本語として成立しうる読みか。変換に投げる前のふるい。"""
    if not hira:
        return False
    if hira[0] in "んっー" or hira[-1] == "っ":
        return False
    for current, following in zip(hira, hira[1:]):
        if current in "っん" and following in "っん":
            return False
        if current == "っ" and following not in _SOKUON_FOLLOWERS:
            return False
    return True


def reading_variants(hira: str, limit: int) -> list[tuple[str, str]]:
    """読みを1箇所だけ揺らした候補を (読み, 種別) で返す。

    種別を返すのは、あとで採否の基準を変えるため。濁点の揺らしは「かな自体が
    答え」のことがあるが（ては→では）、撥音・促音の揺らしはかなのままでは
    ただのノイズで、変換できたときだけ意味がある。

    1箇所だけにするのは組み合わせ爆発を避けるためで、実測した誤りはどれも
    1箇所の揺らしで届く範囲だった。
    """
    if not hira:
        return []
    seen = {hira}
    by_kind: dict[str, list[str]] = {"voicing": [], "hatsuon": [], "sokuon": []}

    def add(candidate: str, kind: str) -> None:
        if not candidate or candidate in seen or not _is_plausible_reading(candidate):
            return
        seen.add(candidate)
        by_kind[kind].append(candidate)

    # 濁点・半濁点の付け外し（最頻。「では」↔「ては」）。
    for index, char in enumerate(hira):
        for alternative in _VOICING_ALTERNATIVES.get(char, ()):
            add(hira[:index] + alternative + hira[index + 1:], "voicing")

    # 撥音「ん」の脱落・混入（「へんかん」↔「へんか」）。
    for index, char in enumerate(hira):
        if char == "ん":
            add(hira[:index] + hira[index + 1:], "hatsuon")
    add(hira + "ん", "hatsuon")
    for index in range(1, len(hira)):
        add(hira[:index] + "ん" + hira[index:], "hatsuon")

    # 促音・長音の脱落・混入。
    for index, char in enumerate(hira):
        if char in "っー":
            add(hira[:index] + hira[index + 1:], "sokuon")
    for index in range(1, len(hira)):
        add(hira[:index] + "っ" + hira[index:], "sokuon")

    # 種別を回して混ぜる。濁点だけで上限を使い切ると撥音の誤りに届かなくなる。
    merged: list[tuple[str, str]] = []
    order = ("voicing", "hatsuon", "sokuon")
    position = 0
    while len(merged) < limit:
        progressed = False
        for kind in order:
            bucket = by_kind[kind]
            if position < len(bucket):
                merged.append((bucket[position], kind))
                progressed = True
                if len(merged) >= limit:
                    break
        if not progressed:
            break
        position += 1
    return merged


_HIRA_TO_KATA = str.maketrans(
    {chr(c): chr(c + 0x60) for c in range(0x3041, 0x3097)}
)
# 1つの揺らしから採る候補の上限。
_PER_VARIANT_LIMIT = 2


def _hira_to_kata(text: str) -> str:
    return text.translate(_HIRA_TO_KATA)


def _convert_variant(reading: str) -> list[str]:
    """揺らした読み1件を変換する。失敗は呼び出し側（variant_candidates）が握る。"""
    segments = _google_transliterate(reading)
    if not segments:
        return []
    if len(segments) == 1:
        return list(segments[0].candidates[:4])
    # 文節に割れたときは、各文節の第1候補を繋いだ1本だけ見せる（並べても選べない）。
    return ["".join(s.candidates[0] if s.candidates else s.reading for s in segments)]


def variant_candidates(
    hira: str,
    known: set[str],
    *,
    limit: int,
    max_candidates: int,
    convert=_convert_variant,
) -> list[str]:
    """揺らした読みから、本来の候補に足す表記を返す。"""
    variants = reading_variants(hira, limit)
    if not variants:
        return []

    def safe_convert(reading: str) -> list[str]:
        # 揺らしは補助。1件が落ちても（オフライン・APIの気分）本来の候補は返す。
        try:
            return convert(reading)
        except Exception:  # noqa: BLE001
            return []

    with ThreadPoolExecutor(max_workers=min(4, len(variants))) as pool:
        converted = list(pool.map(safe_convert, [reading for reading, _ in variants]))

    out: list[str] = []
    for (reading, kind), candidates in zip(variants, converted):
        # 濁点の揺らしは、かな自体がそのまま答えのことがある（ては→では）。
        entries = [reading, *candidates] if kind == "voicing" else list(candidates)
        # 変換できていない読み（＝APIがかなで返しただけ）はノイズ。
        kana = _hira_to_kata(reading)
        taken = 0
        for entry in entries:
            if kind != "voicing" and entry in (reading, kana):
                continue
            if not entry or entry in known:
                continue
            known.add(entry)
            out.append(entry)
            taken += 1
            if len(out) >= max_candidates:
                return out
            # 1つの揺らしで枠を使い切らない。本命が別の揺らしにいることの方が多い。
            if taken >= _PER_VARIANT_LIMIT:
                break
    return out


def reconvert(
    text: str,
    reverse_replacements: tuple[tuple[str, str], ...] | list[tuple[str, str]] | None = None,
) -> dict:
    """テキストの再変換候補を返す。

    戻り値: {
        "reading": "全体のひらがな読み",
        "segments": [{"reading": "...", "candidates": ["...", ...]}, ...],
        "online": bool,   # Google CGI が使えたか
    }
    """
    hira = _reading.to_hiragana(text, reverse_replacements)
    result = {"reading": hira, "segments": [], "online": False}

    if not config.use_google_cgi or not hira:
        result["segments"] = [{"reading": hira, "candidates": [text]}]
        return result

    try:
        segments = _google_transliterate(hira)
        result["segments"] = [
            {"reading": s.reading, "candidates": s.candidates} for s in segments
        ]
        result["online"] = True
    except Exception:
        # オフライン等: 読みだけ返し、候補は元テキストのみ。
        result["segments"] = [{"reading": hira, "candidates": [text]}]
        return result

    _append_variant_candidates(result, text, hira)
    return result


def _append_variant_candidates(result: dict, text: str, hira: str) -> None:
    """読みを揺らした変換候補を、第1文節の末尾へ足す。

    足す先を1文節に限るのは、文節に割れている＝長めの入力で、そこは誤変換
    （読みは合っている）の領域だから。読みが外れる誤認識は短い語で起きる。
    """
    if not config.reconvert_variants:
        return
    if len(text) > config.reconvert_variant_max_len:
        return
    segments = result.get("segments") or []
    if len(segments) != 1:
        return

    known = {text, *segments[0].get("candidates", [])}
    extra = variant_candidates(
        hira,
        known,
        limit=config.reconvert_variant_limit,
        max_candidates=config.reconvert_variant_candidates,
    )
    if not extra:
        return
    segments[0]["candidates"] = [*segments[0].get("candidates", []), *extra]
    # 何を試したかは診断で効くので残す（プラグインは未知のキーを無視する）。
    result["variants"] = [reading for reading, _ in reading_variants(
        hira, config.reconvert_variant_limit
    )]
