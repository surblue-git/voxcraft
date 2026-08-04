"""ユーザー辞書の互換ファサード。

正規データは dictionaries/profiles/*.json で管理する。既存サーバー・プラグインが
利用する関数と /dict API はこのモジュールで維持し、旧 userdict.json は初回だけ
common プロファイルへ非破壊移行する。

主な辞書機能:
- replacements: 語の置換（例 ウィンドウズ→Windows、じょうぷらほう→情プラ法）。本文中どこでも置換。
- symbols: 「単独で言った記号語」を記号に変換（例 当点→、、海業→改行）。
  Whisper は単独の記号語を同音異義漢字にしがち（てん→点/当店、かいぎょう→開業/海業）。
  観測した綴りをここに登録すれば記号になる。単独チャンク時のみ効くので誤爆しない。

新しい辞書ファイルも保存すると自動反映（再起動不要）。JSONの末尾カンマ・
//コメントは寛容に読む。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from dictionary_registry import (
    DictionarySnapshot,
    DictionaryRegistry,
    DictionarySchemaError,
    build_hotword_prompt,
    load_json_relaxed,
    validate_profile,
)

# 既定の辞書ファイル。VOXCRAFT_USERDICT で場所を上書き可能。
_PATH = os.environ.get("VOXCRAFT_USERDICT") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "userdict.json"
)
_DICTIONARIES_DIR = os.environ.get("VOXCRAFT_DICTIONARIES_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "dictionaries"
)
_REGISTRY = DictionaryRegistry(Path(_DICTIONARIES_DIR), Path(_PATH))

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


def _ensure_file() -> None:
    _REGISTRY.ensure_initialized(_DEFAULTS)


def _parse(raw: str) -> dict:
    """後方互換: 旧辞書と同じ寛容JSONパーサー。"""
    parsed = load_json_relaxed(raw)
    if not isinstance(parsed, dict):
        raise DictionarySchemaError("辞書のルートはオブジェクトである必要があります")
    return parsed


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
    """既定辞書セットを読み、既存ランタイム用キャッシュへコンパイルする。"""
    compiled = _REGISTRY.compile_set("default")
    items = list(compiled.replacements)
    symbols = _symbols_from(compiled.symbols)
    hallucinations = set(compiled.hallucinations)
    hotwords = compiled.hotword_prompt
    reverse_items = list(compiled.reverse_replacements)

    _cache["items"] = items
    _cache["symbols"] = symbols
    _cache["hotwords"] = hotwords
    _cache["hallucinations"] = hallucinations
    _cache["reverse_items"] = reverse_items


def _build_hotwords(
    custom_hotwords: list | None,
    items: list[tuple[str, str]],
    limit: int = 50,
    max_chars: int = 90,
) -> str:
    """認識ヒント語を組み立てる。

    total が長すぎると kotoba-whisper が認識結果を丸ごと空にするため、総文字数を
    安全域に収める（実測: 約120字超で全チャンク脱落。initial_prompt 分の余裕も見て
    既定 90字上限）。先頭（＝重要語）から詰めて上限で打ち切る。
    """
    return build_hotword_prompt(tuple(custom_hotwords or ()), tuple(items), limit=limit, max_chars=max_chars)


def _refresh() -> None:
    """辞書群が変わっていれば読み直す。失敗時は直前の内容を保持する。"""
    try:
        signature = _REGISTRY.signature(_DEFAULTS)
    except (OSError, DictionarySchemaError) as exc:
        signature = (("registry-error", 0, 0),)
        _cache["error"] = f"{type(exc).__name__}: {exc}"
    with _lock:
        if _cache["items"] is None or _cache["mtime"] != signature:
            try:
                _reload()
                _cache["error"] = None
            except (OSError, DictionarySchemaError, AttributeError, TypeError) as exc:
                msg = f"{type(exc).__name__}: {exc}"
                _cache["error"] = msg
                print(
                    f"[VoxCraft] 辞書セットを読めません（{msg}）。"
                    f"直前の辞書を使用します。辞書ファイルを確認してください: {_DICTIONARIES_DIR}"
                )
                if _cache["items"] is None:
                    _cache["items"] = _items_from(_DEFAULTS["replacements"])
                    _cache["symbols"] = _symbols_from(_DEFAULTS["symbols"])
                    _cache["hotwords"] = _build_hotwords(
                        _DEFAULTS.get("hotwords"), _cache["items"]
                    )
                    _cache["hallucinations"] = set()
                    _cache["reverse_items"] = _reverse_items_from(_cache["items"])
            _cache["mtime"] = signature


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


def get_dictionary_snapshot(set_id: str = "default") -> DictionarySnapshot:
    """指定セットを検証・コンパイルし、以後ファイル変更の影響を受けない形で返す。"""
    _ensure_file()
    return _REGISTRY.compile_set(set_id)


# --- 編集API（プラグインのUIから読み書きする） -------------------------------
#
# サーバーは 0.0.0.0 で待ち受け、認証を持たない。書き込みを受ける以上、
# 「文字列マップだけ・件数と長さに上限」を厳格に検証してから保存する。

MAX_ENTRIES = 500      # replacements / symbols それぞれの最大件数
MAX_KEY_LEN = 64       # キー1件の最大文字数
MAX_VALUE_LEN = 128    # 値1件の最大文字数


class DictValidationError(ValueError):
    """UIから渡された辞書が制限に反する場合に送出する。"""


def read_raw() -> dict:
    """既存UI用に common を従来の replacements / symbols 形式で返す。"""
    try:
        _ensure_file()
        result = _REGISTRY.profile_projection("common")
    except Exception:  # noqa: BLE001 - 壊れていても UI は開けるようにする
        _refresh()
        result = {
            "replacements": dict(_cache["items"] or []),
            "symbols": dict(_cache["symbols"] or {}),
            "path": str(_REGISTRY.profile_path("common")),
            "legacyPath": _PATH,
            "profileId": "common",
        }
    result["error"] = get_error()
    return result


def _validate_map(obj, name: str) -> dict[str, str]:
    if not isinstance(obj, dict):
        raise DictValidationError(f"{name} はオブジェクトである必要があります")
    if len(obj) > MAX_ENTRIES:
        raise DictValidationError(f"{name} の件数が上限（{MAX_ENTRIES}）を超えています: {len(obj)}")
    out: dict[str, str] = {}
    for k, v in obj.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise DictValidationError(f"{name} のキーと値は文字列である必要があります")
        key = k.strip()
        if not key:
            continue  # 空キーは黙って捨てる（UIの空行）
        if len(key) > MAX_KEY_LEN:
            raise DictValidationError(f"{name} のキーが長すぎます（{MAX_KEY_LEN}文字まで）: {key[:20]}…")
        if len(v) > MAX_VALUE_LEN:
            raise DictValidationError(f"{name} の値が長すぎます（{MAX_VALUE_LEN}文字まで）: {v[:20]}…")
        # UTF-8 にできない文字（孤立サロゲート等）は保存時に例外になるので先に弾く。
        for s in (key, v):
            try:
                s.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise DictValidationError(f"{name} に保存できない文字が含まれています") from exc
        out[key] = v
    return out


def write_raw(replacements, symbols) -> dict:
    """既存UIから common を更新する。拡張フィールドと無効項目は保持する。"""
    reps = _validate_map(replacements, "replacements")
    syms = _validate_map(symbols, "symbols")

    _ensure_file()
    _REGISTRY.update_profile_maps("common", reps, syms)

    with _lock:
        _cache["mtime"] = None  # 次の参照で確実に読み直す
    return {"replacements": len(reps), "symbols": len(syms)}


def read_profile_raw(profile_id: str) -> dict:
    """任意プロファイルを従来の replacements / symbols 形式で返す（編集UI用）。"""
    _ensure_file()
    result = _REGISTRY.profile_projection(profile_id)
    result["error"] = None
    return result


def write_profile_raw(profile_id: str, replacements, symbols) -> dict:
    """任意プロファイルの置換・記号語をUIから更新する。拡張フィールドと無効項目は保持する。"""
    reps = _validate_map(replacements, "replacements")
    syms = _validate_map(symbols, "symbols")

    _ensure_file()
    _REGISTRY.update_profile_maps(profile_id, reps, syms)

    if profile_id == "common":
        with _lock:
            _cache["mtime"] = None  # 次の参照で確実に読み直す
    return {"replacements": len(reps), "symbols": len(syms)}


def dictionary_catalog() -> dict:
    """管理UI向けの辞書プロファイル・セット一覧（本文は含めない）。"""
    _ensure_file()
    return _REGISTRY.catalog()


def read_profile(profile_id: str) -> dict:
    """検証済みの辞書プロファイル本文を返す。"""
    _ensure_file()
    return _REGISTRY.load_profile(profile_id)


def validate_profile_raw(data) -> dict:
    """保存せずに新形式の辞書を検証する。"""
    diagnostics = validate_profile(data)
    return {
        "valid": not any(item.severity == "error" for item in diagnostics),
        "diagnostics": [item.as_dict() for item in diagnostics],
    }


def add_profile_entry(
    profile_id: str,
    observed: str,
    output: str,
    *,
    expected_revision: str | None = None,
    hotword: bool = False,
    priority: int = 0,
    note: str = "",
) -> dict:
    """Add one replacement to a profile using optimistic concurrency."""
    _ensure_file()
    result = _REGISTRY.add_entry(
        profile_id,
        observed,
        output,
        expected_revision=expected_revision,
        hotword=hotword,
        priority=priority,
        note=note,
    )
    with _lock:
        _cache["mtime"] = None
    return result
