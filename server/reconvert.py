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


@dataclass
class Span:
    """元テキストの [start, end) と、その範囲の表層・読み。

    start/end は**元テキストの文字位置**。タップした位置がどの語かを決めるために、
    読みへ変換したあとも元の位置へ戻れるようにしている。
    """

    start: int
    end: int
    surface: str
    reading: str


class _Reading:
    """Sudachi があれば使い、無ければ入力をそのまま読みとして扱う。"""

    def __init__(self) -> None:
        self._tokenizer = None
        try:
            from sudachipy import dictionary  # type: ignore

            self._tokenizer = dictionary.Dictionary(dict="core").create()
        except Exception:
            self._tokenizer = None

    def _tokenize_span(self, text: str, offset: int) -> list[Span]:
        """辞書に当たらなかった範囲を形態素へ割る。offset は元テキスト上の開始位置。"""
        if not text:
            return []
        if self._tokenizer is None:
            # フォールバック: カタカナだけひらがな化する（漢字は読めない）。
            return [Span(offset, offset + len(text), text, _kata_to_hira(text))]

        from sudachipy import tokenizer as _t  # type: ignore

        out: list[Span] = []
        pos = offset
        for m in self._tokenizer.tokenize(text, _t.Tokenizer.SplitMode.C):
            surface = m.surface()
            reading = m.reading_form()  # カタカナ
            out.append(
                Span(pos, pos + len(surface), surface,
                     _kata_to_hira(reading) if reading else surface)
            )
            pos += len(surface)
        return out

    def to_spans(
        self,
        text: str,
        reverse_replacements: tuple[tuple[str, str], ...] | list[tuple[str, str]] | None = None,
    ) -> list[Span]:
        """元テキストの文字位置つきで (表層, 読み) に割る。

        **ユーザー辞書の逆引きでテキストを書き換えない**のが要点。以前は
        `text.replace(表記, 読み)` してから解析していたので、置換が起きた時点で
        元テキストの文字位置との対応が失われていた。読みの長さを測るだけなら
        それで足りるが、**タップした位置がどの語かを決めるには使えない**。

        そこで書き換える代わりに、辞書の表記に当たった範囲を「この範囲の読みはこれ」
        という Span として記録し、当たらなかった範囲だけ Sudachi に渡す。
        逆引きは表記の長い順に並んでいるので、前から最長一致で拾えばよい。
        """
        if reverse_replacements is None:
            from userdict import get_reverse_replacements

            reverse_replacements = get_reverse_replacements()
        entries = [(s, h) for s, h in reverse_replacements if s]

        out: list[Span] = []
        i = 0
        run = 0  # 辞書に当たらなかった範囲の開始位置
        while i < len(text):
            hit = next(((s, h) for s, h in entries if text.startswith(s, i)), None)
            if hit is None:
                i += 1
                continue
            surface, hira = hit
            out.extend(self._tokenize_span(text[run:i], run))
            out.append(Span(i, i + len(surface), surface, hira))
            i += len(surface)
            run = i
        out.extend(self._tokenize_span(text[run:], run))
        return out

    def to_morphemes(
        self,
        text: str,
        reverse_replacements: tuple[tuple[str, str], ...] | list[tuple[str, str]] | None = None,
    ) -> list[tuple[str, str]]:
        """(表層, ひらがな読み) の並びを返す（位置が要らない呼び出し向け）。"""
        return [(s.surface, s.reading) for s in self.to_spans(text, reverse_replacements)]

    def to_hiragana(
        self,
        text: str,
        reverse_replacements: tuple[tuple[str, str], ...] | list[tuple[str, str]] | None = None,
    ) -> str:
        """テキスト全体の読み（ひらがな）を返す。"""
        return "".join(r for _, r in self.to_morphemes(text, reverse_replacements))


_reading = _Reading()


def get_tokenizer():
    """読み込み済みの Sudachi トークナイザ（無ければ None）。

    辞書のロードは数百MB効くので、同じプロセスの他のモジュール
    （`refine_guard`）には作らせず、ここのものを貸す。
    """
    return _reading._tokenizer


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


def split_reading(
    morphemes: list[tuple[str, str]], limit: int | None = None
) -> list[str]:
    """読みを API に投げられる長さへ、**形態素境界で**割る。

    Google CGI は読みが 53字を超えると何も返さない（config.google_cgi_max_reading
    の注記を参照）。長い選択の再変換が無反応だったのはこれが原因で、超過を
    例外ではなく空配列で返すため誰も気づけなかった。

    形態素の途中で切らないのは、切られた語の変換候補が両側とも無意味になるため。
    1形態素だけで上限を超える場合（長いカタカナ語など）はやむを得ず切る。
    切らないと1文字も進まず、無限ループになる。
    """
    cap = limit or config.google_cgi_max_reading
    out: list[str] = []
    cur = ""
    for _surface, reading in morphemes:
        if not reading:
            continue
        if len(reading) > cap:
            # この形態素だけで上限超え。手前を確定してから機械的に割る。
            if cur:
                out.append(cur)
                cur = ""
            for i in range(0, len(reading), cap):
                out.append(reading[i : i + cap])
            continue
        if len(cur) + len(reading) > cap:
            out.append(cur)
            cur = reading
        else:
            cur += reading
    if cur:
        out.append(cur)
    return out


def transliterate_reading(hiragana: str, morphemes: list[tuple[str, str]]) -> list[Segment]:
    """読み全体を文節に変換する。長ければ分割して投げ、結果を繋ぐ。

    分割ぶんは並列に投げる（1本ずつだと選択が長いほど待たされる）。
    1本でも落ちたらまとめて失敗にする。半分だけ候補が付いた状態を返すと、
    ユーザーには「一部の語だけ変換できない」という理解不能な見え方になるため。
    """
    chunks = split_reading(morphemes)
    if len(chunks) <= 1:
        # 従来どおりの1回呼び出し（短い入力の挙動は変えない）。
        return _google_transliterate(hiragana)

    with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as pool:
        results = list(pool.map(_google_transliterate, chunks))

    out: list[Segment] = []
    for chunk, segs in zip(chunks, results):
        if not segs:
            # 上限内なのに空 ＝ API 側の不調。黙って欠けさせない。
            raise RuntimeError(f"変換に失敗した読みがある: {chunk[:12]}…")
        out.extend(segs)
    return out


@dataclass
class SegmentSpan:
    """1文節と、それが元テキストのどこを占めるか。"""

    start: int
    end: int
    reading: str
    candidates: list[str] = field(default_factory=list)


def segment_spans(text: str, reverse_replacements=None) -> list[SegmentSpan]:
    """テキストを文節に割り、**元テキストの文字位置つき**で返す。"""
    return spans_to_segments(text, _reading.to_spans(text, reverse_replacements))


def spans_to_segments(text: str, spans: list[Span]) -> list[SegmentSpan]:
    """解析済みの Span 列から、位置つきの文節を作る。

    タップした位置から文節を決めるための中核。手順:
      1. 読みの累積長 → 表層の累積位置 の対応表を作る
      2. 読み全体を文節に変換する（53字制限は `split_reading` が吸収する）
      3. 各文節の読み終端を対応表で表層位置に戻す

    **文節の境界が形態素の途中に落ちることがある**（実測では数字まわりで1割ほど）。
    そのときは直近の形態素境界へ丸める。丸めるので文節の範囲は必ず連続し、
    連結すると元テキストに戻る ＝ 置換してもテキストが壊れない。
    タップした語が隣の文節に入ることはあるが、それは選び直せば済む。

    `text` は位置の範囲を決めるためだけに使う（spans と同じテキストであること）。
    """
    if not spans:
        return []

    # 読みの累積位置 → 表層の位置
    read_at = [0]
    surf_at = [spans[0].start]
    for s in spans:
        read_at.append(read_at[-1] + len(s.reading))
        surf_at.append(s.end)
    to_surface = dict(zip(read_at, surf_at))

    hira = "".join(s.reading for s in spans)
    segments = transliterate_reading(hira, [(s.surface, s.reading) for s in spans])

    out: list[SegmentSpan] = []
    rpos = 0
    prev = spans[0].start
    for seg in segments:
        rpos += len(seg.reading)
        if rpos in to_surface:
            spos = to_surface[rpos]
        else:
            # 形態素の途中で割れた。直近の境界へ丸める（範囲を連続させるため）。
            nearest = min(read_at, key=lambda x: (abs(x - rpos), x))
            spos = to_surface[nearest]
        if spos <= prev:
            # 丸めた結果つぶれた文節は、直前へ足して捨てない。
            if out:
                out[-1].reading += seg.reading
                continue
            spos = min(prev + 1, spans[-1].end)
        out.append(SegmentSpan(prev, spos, seg.reading, list(seg.candidates)))
        prev = spos
    if prev < spans[-1].end and out:
        # 末尾の取りこぼし（句点など）は最後の文節に含める。
        out[-1].end = spans[-1].end
    return out


def segment_at(text: str, offset: int, reverse_replacements=None) -> SegmentSpan | None:
    """`offset`（元テキストの文字位置）を含む文節を返す。タップの受け口。"""
    for seg in segment_spans(text, reverse_replacements):
        if seg.start <= offset < seg.end:
            return seg
    return None


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

# 「揺らした読み（かな）そのものが答え」を候補に混ぜる上限の読み長。
# 「ては→では」のような短い機能語では成り立つが、複合語では成り立たない。
_KANA_ANSWER_MAX = 4


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
        # ただしそれが成り立つのは短い機能語だけ。長い複合語の読みをそのまま
        # 並べても選ばれることはなく、本命を下へ押しやるだけになる
        # （実測 2026-08-09: 「どうおんいき」の揺らしで『とうおんいき』
        # 『どうおんいぎ』がかなのまま2枠を占め、正解の「同音異義」が10番目に沈んだ）。
        kana_is_plausible = kind == "voicing" and len(reading) <= _KANA_ANSWER_MAX
        entries = [reading, *candidates] if kana_is_plausible else list(candidates)
        # 変換できていない読み（＝APIがかなで返しただけ）はノイズ。
        kana = _hira_to_kata(reading)
        taken = 0
        for entry in entries:
            if not kana_is_plausible and entry in (reading, kana):
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
    offset: int | None = None,
) -> dict:
    """テキストの再変換候補を返す。

    戻り値: {
        "reading": "全体のひらがな読み",
        "segments": [{"reading": "...", "candidates": ["...", ...]}, ...],
        "online": bool,   # Google CGI が使えたか
    }
    """
    spans = _reading.to_spans(text, reverse_replacements)
    hira = "".join(s.reading for s in spans)
    result = {"reading": hira, "segments": [], "online": False}
    # 候補が取れなかったときの形。位置は全体を1つの文節として返す。
    whole = [{"start": 0, "end": len(text), "reading": hira, "candidates": [text]}]

    if not config.use_google_cgi or not hira:
        result["segments"] = whole
        return result

    try:
        segments = spans_to_segments(text, spans)
        if not segments:
            # 上限内なのに空。ここを黙って通すと、プラグインは online=True の
            # 空モーダルを開いて何も起きない（長い選択が無反応だった経路）。
            raise RuntimeError("変換候補が空")
        # start/end は投げたテキスト上の文字位置。タップした位置から文節を
        # 決めるために使う。既存の呼び出しはこのキーを見ないので影響しない。
        result["segments"] = [
            {"start": s.start, "end": s.end, "reading": s.reading, "candidates": s.candidates}
            for s in segments
        ]
        result["online"] = True
    except Exception as exc:
        # オフライン・API不調・分割の失敗: 読みだけ返し、候補は元テキストのみ。
        # online=False なのでクライアントは「候補を取得できない」と案内できる。
        print(f"[VoxCraft] 再変換の候補を取得できません: {str(exc)[:80]}")
        result["segments"] = whole
        return result

    _append_variant_candidates(result, text, hira, offset=offset)
    return result


def _append_variant_candidates(
    result: dict, text: str, hira: str, offset: int | None = None
) -> None:
    """読みを揺らした変換候補を足す。

    **以前は「1文節に収まったときだけ」試していた。これが逆だった。**
    根拠は「文節に割れている＝長めの入力＝読みは合っている領域」だったが、
    実測（2026-08-09）でその前提が崩れた:

        「同音異義語」を『動音域語』と誤認識 → 読み どうおんい**き**ご
        正しい読み どうおんい**ぎ**ご とは濁点1つ違い
        誤った読みでは Google が 2文節（どうおんいき / ご）に割り、
        候補は「同音域」止まりで正解に**到達できない**

    つまり**読みが外れているからこそ文節が割れる**。割れたら諦める設計だったので、
    揺らしがいちばん要る場面で発火していなかった。判断は文節数ではなく長さで行う。

    `offset`（タップ位置）があるときは、叩いた文節の読みだけを揺らす。
    実測: 'どうおんいき' → 'どうおんいぎ' → 「同音異義」。これで前半だけ置換すれば
    「同音異義」＋「語」となり、全体を差し替えなくても直る。
    """
    if not config.reconvert_variants:
        return
    segments = result.get("segments") or []
    if not segments:
        return

    def _extra(reading: str, known: set[str]) -> list[str]:
        return variant_candidates(
            reading,
            known,
            limit=config.reconvert_variant_limit,
            max_candidates=config.reconvert_variant_candidates,
        )

    if offset is not None:
        # タップ経路: 叩いた文節だけを対象にする。
        idx = next(
            (
                i
                for i, s in enumerate(segments)
                if s.get("start") is not None and s["start"] <= offset < s["end"]
            ),
            None,
        )
        if idx is None:
            return
        seg = segments[idx]
        surface = text[seg["start"] : seg["end"]]
        if len(surface) > config.reconvert_variant_max_len:
            return
        extra = _extra(seg["reading"], {surface, *seg.get("candidates", [])})
        if not extra:
            return
        seg["candidates"] = [*seg.get("candidates", []), *extra]
        result["variants"] = [r for r, _ in reading_variants(
            seg["reading"], config.reconvert_variant_limit
        )]
        return

    if len(text) > config.reconvert_variant_max_len:
        return
    known = {text, *(c for s in segments for c in s.get("candidates", []))}
    extra = _extra(hira, known)
    if not extra:
        return
    if len(segments) == 1:
        segments[0]["candidates"] = [*segments[0].get("candidates", []), *extra]
    else:
        # 揺らしは「全体を読み直した結果」なので、割れた片方には足せない
        # （片方だけ置換すると語が壊れる）。短い入力に限っているので畳む。
        joined = "".join(
            s["candidates"][0] if s.get("candidates") else s["reading"] for s in segments
        )
        result["segments"] = [{
            "start": segments[0].get("start", 0),
            "end": segments[-1].get("end", len(text)),
            "reading": hira,
            "candidates": [joined, *extra],
        }]
    # 何を試したかは診断で効くので残す（プラグインは未知のキーを無視する）。
    result["variants"] = [reading for reading, _ in reading_variants(
        hira, config.reconvert_variant_limit
    )]
