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

    def to_hiragana(self, text: str) -> str:
        """テキスト全体の読み（ひらがな）を返す。

        ユーザー辞書の表記→読みを最優先で適用し、残りを Sudachi でひらがな化する。
        """
        from userdict import get_reverse_replacements

        # 1. ユーザー辞書の逆引き（表記 → 読み）を適用
        for surface, hira in get_reverse_replacements():
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


def reconvert(text: str) -> dict:
    """テキストの再変換候補を返す。

    戻り値: {
        "reading": "全体のひらがな読み",
        "segments": [{"reading": "...", "candidates": ["...", ...]}, ...],
        "online": bool,   # Google CGI が使えたか
    }
    """
    hira = _reading.to_hiragana(text)
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
