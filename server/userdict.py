"""ユーザー辞書（読み→表記の置換）。

英語・固有名詞などを「話した言葉→望む表記」で登録して育てられる。
例: ウィンドウズ→Windows、アンドロイド→Android。

userdict.json をユーザーが手で編集でき、保存すると次のチャンクから自動反映する
（サーバー再起動不要）。これが「学習」の第一歩。将来は変換戻しの確定結果から
自動追記する拡張も可能。
"""
from __future__ import annotations

import json
import os
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
_cache: dict = {"mtime": None, "items": []}


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


def get_replacements() -> list[tuple[str, str]]:
    """(読み, 表記) のリストを長さ降順で返す。ファイル更新時のみ読み直す。"""
    _ensure_file()
    try:
        mtime = os.path.getmtime(_PATH)
    except OSError:
        return _defaults_items()

    with _lock:
        if _cache["mtime"] != mtime:
            try:
                with open(_PATH, encoding="utf-8") as f:
                    data = json.load(f)
                reps = data.get("replacements", {})
                items = [
                    (k, v)
                    for k, v in reps.items()
                    if isinstance(k, str) and isinstance(v, str) and k
                ]
                items.sort(key=lambda kv: len(kv[0]), reverse=True)
            except (OSError, json.JSONDecodeError, AttributeError):
                items = _defaults_items()
            _cache["mtime"] = mtime
            _cache["items"] = items
        return list(_cache["items"])
