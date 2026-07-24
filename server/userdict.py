"""ユーザー辞書（読み→表記の置換 ＋ 記号語の登録）。

2種類を1ファイル(userdict.json)で管理する:
- replacements: 語の置換（例 ウィンドウズ→Windows、じょうぷらほう→情プラ法）。本文中どこでも置換。
- symbols: 「単独で言った記号語」を記号に変換（例 当点→、、海業→改行）。
  Whisper は単独の記号語を同音異義漢字にしがち（てん→点/当店、かいぎょう→開業/海業）。
  観測した綴りをここに登録すれば記号になる。単独チャンク時のみ効くので誤爆しない。

どちらも手編集で保存すると自動反映（再起動不要）。JSONの末尾カンマ・//コメントは寛容に読む。
"""
from __future__ import annotations

import json
import os
import re
import threading

# 既定の辞書ファイル。VOXCRAFT_USERDICT で場所を上書き可能。
_PATH = os.environ.get("VOXCRAFT_USERDICT") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "userdict.json"
)

# 記号セクションで「改行」を表す別名（値がこれなら "\n" に変換）。
_NEWLINE_ALIASES = {"改行", "かいぎょう", "newline", "\\n", "\n"}

_DEFAULTS = {
    "_README": (
        "replacements=語の置換（英語・日本語可、本文中どこでも）。"
        "symbols=単独で言った記号語→記号（例 当点→、、海業→改行）。"
        "hotwords=認識精度を上げるヒント語リスト（任意）。"
        "hallucinations=丸ごと一致時に無視する誤認識テキスト（任意）。"
        "実際にWhisperが出す綴りを登録するのが確実。保存で自動反映（再起動不要）。"
    ),
    "replacements": {
        "じょうぷらほう": "情プラ法",
        "ウィンドウズ": "Windows",
        "アンドロイド": "Android",
        "ギットハブ": "GitHub",
        "オブシディアン": "Obsidian",
        "パイソン": "Python",
        "ジェイソン": "JSON",
        "ユーアールエル": "URL",
        "エーピーアイ": "API",
        "エーアイ": "AI",
    },
    "symbols": {
        "当点": "、",
        "当店": "、",
        "海業": "改行",
    },
    "hotwords": [
        "Obsidian",
        "VoxCraft",
        "情プラ法",
    ],
    "hallucinations": [],
}

_lock = threading.Lock()
_cache: dict = {
    "mtime": None,
    "items": None,
    "symbols": None,
    "hotwords": None,
    "hallucinations": None,
    "reverse_items": None,
    "error": None,
}

_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_LINE_COMMENT = re.compile(r"(?m)//.*$")
# 値の閉じ引用符の直後（改行を挟んで次のキーの引用符が続く）にカンマが無いケース。
_MISSING_COMMA = re.compile(r'"(?=\s*\r?\n\s*")')


def _ensure_file() -> None:
    if not os.path.exists(_PATH):
        try:
            with open(_PATH, "w", encoding="utf-8") as f:
                json.dump(_DEFAULTS, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


def _parse(raw: str) -> dict:
    """厳密→寛容（コメント/カンマ抜け/末尾カンマを補正）の順にパースを試みる。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        cleaned = _LINE_COMMENT.sub("", raw)
        cleaned = _MISSING_COMMA.sub('",', cleaned)   # カンマ抜けを補う
        cleaned = _TRAILING_COMMA.sub(r"\1", cleaned)  # 末尾カンマを除く
        return json.loads(cleaned)  # まだ失敗するなら例外を上へ


def _items_from(reps: dict) -> list[tuple[str, str]]:
    items = [
        (k, v)
        for k, v in reps.items()
        if isinstance(k, str) and isinstance(v, str) and k
    ]
    items.sort(key=lambda kv: len(kv[0]), reverse=True)
    return items


def _reverse_items_from(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """表記 -> 読みの逆引きリスト（表記の長さ降順）。"""
    rev = [(v, k) for k, v in items if v and k]
    rev.sort(key=lambda kv: len(kv[0]), reverse=True)
    return rev


def _symbols_from(syms: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in syms.items():
        if not isinstance(k, str) or not isinstance(v, str) or not k:
            continue
        out[k] = "\n" if v in _NEWLINE_ALIASES else v
    return out


def _hallucinations_from(halls: list) -> set[str]:
    if not isinstance(halls, list):
        return set()
    return {h for h in halls if isinstance(h, str) and h.strip()}


def _reload() -> None:
    """ファイルを読み直して items / symbols / hotwords / hallucinations を再構築する。"""
    with open(_PATH, encoding="utf-8") as f:
        data = _parse(f.read())
    items = _items_from(data.get("replacements", {}) or {})
    symbols = _symbols_from(data.get("symbols", {}) or {})
    custom_hotwords = data.get("hotwords", [])
    hallucinations = _hallucinations_from(data.get("hallucinations", []))
    hotwords = _build_hotwords(custom_hotwords, items)
    reverse_items = _reverse_items_from(items)

    _cache["items"] = items
    _cache["symbols"] = symbols
    _cache["hotwords"] = hotwords
    _cache["hallucinations"] = hallucinations
    _cache["reverse_items"] = reverse_items


def _build_hotwords(custom_hotwords: list | None, items: list[tuple[str, str]], limit: int = 50) -> str:
    vals, seen = [], set()
    if isinstance(custom_hotwords, list):
        for w in custom_hotwords:
            if isinstance(w, str) and w and w not in seen:
                seen.add(w)
                vals.append(w)

    for k, v in items:
        for w in (v, k):
            if w and w not in seen and len(w) >= 2:
                seen.add(w)
                vals.append(w)
            if len(vals) >= limit:
                break
        if len(vals) >= limit:
            break
    return " ".join(vals)


def _refresh() -> None:
    """mtime が変わっていれば読み直す。失敗時は直前の内容を保持して警告。"""
    _ensure_file()
    try:
        mtime = os.path.getmtime(_PATH)
    except OSError:
        mtime = None
    with _lock:
        if _cache["items"] is None or _cache["mtime"] != mtime:
            try:
                _reload()
                _cache["error"] = None
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                msg = f"{type(exc).__name__}: {exc}"
                _cache["error"] = msg
                print(
                    f"[VoxCraft] userdict.json を読めません（{msg}）。"
                    f"直前の辞書を使用します。カンマ等を確認してください: {_PATH}"
                )
                if _cache["items"] is None:
                    _cache["items"] = _items_from(_DEFAULTS["replacements"])
                    _cache["symbols"] = _symbols_from(_DEFAULTS["symbols"])
                    _cache["hotwords"] = _build_hotwords(
                        _DEFAULTS.get("hotwords"), _cache["items"]
                    )
                    _cache["hallucinations"] = set()
                    _cache["reverse_items"] = _reverse_items_from(_cache["items"])
            _cache["mtime"] = mtime


def get_replacements() -> list[tuple[str, str]]:
    """(読み, 表記) のリストを長さ降順で返す。"""
    _refresh()
    return list(_cache["items"] or [])


def get_reverse_replacements() -> list[tuple[str, str]]:
    """(表記, 読み) のリストを表記の長さ降順で返す。"""
    _refresh()
    return list(_cache["reverse_items"] or [])


def get_symbols() -> dict[str, str]:
    """単独チャンク時に記号化する {綴り: 記号} を返す。"""
    _refresh()
    return dict(_cache["symbols"] or {})


def get_hotwords() -> str:
    """辞書の表記・読みを Whisper のヒント語として渡す文字列。"""
    _refresh()
    return _cache["hotwords"] or ""


def get_hallucinations() -> set[str]:
    """ユーザー追加の誤認識除去文字列セットを返す。"""
    _refresh()
    return set(_cache["hallucinations"] or set())


def get_error() -> str | None:
    """辞書が読めていない場合のエラー文字列（正常時は None）。"""
    _refresh()
    return _cache["error"]
