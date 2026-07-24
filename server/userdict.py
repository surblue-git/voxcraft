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
}

_lock = threading.Lock()
_cache: dict = {"mtime": None, "items": None, "symbols": None, "hotwords": None}

_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_LINE_COMMENT = re.compile(r"(?m)//.*$")


def _ensure_file() -> None:
    if not os.path.exists(_PATH):
        try:
            with open(_PATH, "w", encoding="utf-8") as f:
                json.dump(_DEFAULTS, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


def _parse(raw: str) -> dict:
    """厳密→寛容（末尾カンマ/コメント除去）の順にパースを試みる。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        cleaned = _TRAILING_COMMA.sub(r"\1", _LINE_COMMENT.sub("", raw))
        return json.loads(cleaned)  # まだ失敗するなら例外を上へ


def _items_from(reps: dict) -> list[tuple[str, str]]:
    items = [
        (k, v)
        for k, v in reps.items()
        if isinstance(k, str) and isinstance(v, str) and k
    ]
    items.sort(key=lambda kv: len(kv[0]), reverse=True)
    return items


def _symbols_from(syms: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in syms.items():
        if not isinstance(k, str) or not isinstance(v, str) or not k:
            continue
        out[k] = "\n" if v in _NEWLINE_ALIASES else v
    return out


def _reload() -> None:
    """ファイルを読み直して items / symbols / hotwords を再構築する。"""
    with open(_PATH, encoding="utf-8") as f:
        data = _parse(f.read())
    items = _items_from(data.get("replacements", {}) or {})
    symbols = _symbols_from(data.get("symbols", {}) or {})
    hotwords = _build_hotwords(items)
    _cache["items"] = items
    _cache["symbols"] = symbols
    _cache["hotwords"] = hotwords


def _build_hotwords(items: list[tuple[str, str]], limit: int = 40) -> str:
    vals, seen = [], set()
    for _k, v in items:
        if v and v not in seen and re.search(r"[A-Za-z0-9]", v):
            seen.add(v)
            vals.append(v)
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
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                print(
                    f"[VoxCraft] userdict.json を読めません（{exc}）。"
                    f"直前の辞書を使用します。末尾カンマ等を確認してください: {_PATH}"
                )
                if _cache["items"] is None:
                    _cache["items"] = _items_from(_DEFAULTS["replacements"])
                    _cache["symbols"] = _symbols_from(_DEFAULTS["symbols"])
                    _cache["hotwords"] = _build_hotwords(_cache["items"])
            _cache["mtime"] = mtime


def get_replacements() -> list[tuple[str, str]]:
    """(読み, 表記) のリストを長さ降順で返す。"""
    _refresh()
    return list(_cache["items"] or [])


def get_symbols() -> dict[str, str]:
    """単独チャンク時に記号化する {綴り: 記号} を返す。"""
    _refresh()
    return dict(_cache["symbols"] or {})


def get_hotwords() -> str:
    """辞書の英数字表記を Whisper のヒント語として渡す文字列。"""
    _refresh()
    return _cache["hotwords"] or ""
