"""ユーザー辞書（読み→表記の置換）。

英語・固有名詞などを「話した言葉→望む表記」で登録して育てられる。
例: ウィンドウズ→Windows、アンドロイド→Android。

userdict.json をユーザーが手で編集でき、保存すると次のチャンクから自動反映する
（サーバー再起動不要）。これが「学習」の第一歩。将来は変換戻しの確定結果から
自動追記する拡張も可能。

JSONの末尾カンマや // コメントは寛容に読む。パースに失敗しても、直前に読めた
辞書を保持しつつサーバーコンソールに警告を出す（無言で全無効化しない）。
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

# 初期辞書（ファイルが無いとき書き出す種）。カタカナキーは誤爆が少なく安全。
_DEFAULTS = {
    "_README": (
        "話した言葉→置き換え後の表記。英語やよく使う固有名詞を登録する。"
        "キーはカタカナ推奨（誤爆が少ない）。保存すると自動反映（再起動不要）。"
    ),
    "replacements": {
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
}

_lock = threading.Lock()
_cache: dict = {"mtime": None, "items": None}

# 末尾カンマ・行コメントの簡易除去（JSONを寛容にする）。
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_LINE_COMMENT = re.compile(r"(?m)//.*$")


def _ensure_file() -> None:
    if not os.path.exists(_PATH):
        try:
            with open(_PATH, "w", encoding="utf-8") as f:
                json.dump(_DEFAULTS, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


def _defaults_items() -> list[tuple[str, str]]:
    items = list(_DEFAULTS["replacements"].items())
    items.sort(key=lambda kv: len(kv[0]), reverse=True)
    return items


def _parse(raw: str) -> dict:
    """厳密→寛容（末尾カンマ/コメント除去）の順にパースを試みる。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        cleaned = _TRAILING_COMMA.sub(r"\1", _LINE_COMMENT.sub("", raw))
        return json.loads(cleaned)  # まだ失敗するなら例外を上へ


def _load_items() -> list[tuple[str, str]]:
    with open(_PATH, encoding="utf-8") as f:
        raw = f.read()
    data = _parse(raw)
    reps = data.get("replacements", {})
    items = [
        (k, v)
        for k, v in reps.items()
        if isinstance(k, str) and isinstance(v, str) and k
    ]
    items.sort(key=lambda kv: len(kv[0]), reverse=True)
    return items


def get_replacements() -> list[tuple[str, str]]:
    """(読み, 表記) のリストを長さ降順で返す。ファイル更新時のみ読み直す。"""
    _ensure_file()
    try:
        mtime = os.path.getmtime(_PATH)
    except OSError:
        return _cache["items"] or _defaults_items()

    with _lock:
        if _cache["mtime"] != mtime:
            try:
                _cache["items"] = _load_items()
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                # 無言で全無効化せず、直前の良い辞書を保持して警告を出す。
                print(
                    f"[VoxCraft] userdict.json を読めません（{exc}）。"
                    f"直前の辞書を使用します。JSONの末尾カンマ等を確認してください: {_PATH}"
                )
                if _cache["items"] is None:
                    _cache["items"] = _defaults_items()
            _cache["mtime"] = mtime
        return list(_cache["items"])


def get_hotwords(limit: int = 40) -> str:
    """辞書の表記（英語固有名詞など）を Whisper のヒント語として渡す文字列。

    「ATOK」「Windows」等を認識しやすくする補助。長すぎると逆効果なので上限を設ける。
    """
    vals = []
    seen = set()
    for _k, v in get_replacements():
        if v and v not in seen and re.search(r"[A-Za-z0-9]", v):
            seen.add(v)
            vals.append(v)
        if len(vals) >= limit:
            break
    return " ".join(vals)
