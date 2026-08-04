"""Versioned dictionary profiles and dictionary-set registry.

The registry owns the durable, extensible dictionary format.  ``userdict.py``
remains the compatibility facade used by the current server and plugin UI.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


SCHEMA_VERSION = 1
MAX_PROFILE_BYTES = 5 * 1024 * 1024
MAX_PROFILE_ENTRIES = 20_000
MAX_SYMBOLS = 2_000
MAX_LIST_ITEMS = 2_000
MAX_OBSERVED_LEN = 128
MAX_OUTPUT_LEN = 256
MAX_NOTE_LEN = 1_000

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_LINE_COMMENT = re.compile(r"(?m)//.*$")
_MISSING_COMMA = re.compile(r'"(?=\s*\r?\n\s*")')


class DictionarySchemaError(ValueError):
    """Raised when a dictionary profile or set catalog is invalid."""

    def __init__(self, message: str, diagnostics: list["Diagnostic"] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or []


class DictionaryRevisionConflict(ValueError):
    """Raised when a profile changed after a client read it."""

    def __init__(self, message: str, *, current_revision: str):
        super().__init__(message)
        self.current_revision = current_revision


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    message: str
    entry: int | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.entry is not None:
            result["entry"] = self.entry
        return result


@dataclass(frozen=True)
class ReplacementPlan:
    """Compiled longest-match, single-pass replacement plan."""

    items: tuple[tuple[str, str], ...]
    values: Mapping[str, str]
    pattern: re.Pattern[str] | None

    @classmethod
    def compile(cls, items: tuple[tuple[str, str], ...]) -> "ReplacementPlan":
        values = MappingProxyType(dict(items))
        pattern = (
            re.compile("|".join(re.escape(key) for key, _value in items))
            if items else None
        )
        return cls(items=items, values=values, pattern=pattern)

    def apply(self, text: str) -> str:
        if not text or self.pattern is None:
            return text
        return self.pattern.sub(lambda match: self.values[match.group(0)], text)


@dataclass(frozen=True)
class DictionarySnapshot:
    set_id: str
    set_name: str
    revision: str
    profile_ids: tuple[str, ...]
    profile_revisions: tuple[tuple[str, str], ...]
    writable_profile_id: str
    replacements: tuple[tuple[str, str], ...]
    reverse_replacements: tuple[tuple[str, str], ...]
    replacement_plan: ReplacementPlan
    symbols: Mapping[str, str]
    hotwords: tuple[str, ...]
    hotword_prompt: str
    hallucinations: frozenset[str]
    diagnostics: tuple[Diagnostic, ...]

    def metadata(self) -> dict[str, Any]:
        warnings = [item for item in self.diagnostics if item.severity == "warning"]
        return {
            "dictionarySetId": self.set_id,
            "dictionarySetName": self.set_name,
            "dictionaryRevision": self.revision,
            "dictionaryProfiles": list(self.profile_ids),
            "dictionaryProfileRevisions": dict(self.profile_revisions),
            "dictionaryWritableProfile": self.writable_profile_id,
            "dictionaryWarningCount": len(warnings),
            "dictionaryWarnings": [item.as_dict() for item in warnings[:100]],
        }


# Phase 1A name kept for import compatibility.
CompiledDictionary = DictionarySnapshot


def build_hotword_prompt(
    preferred: list[str] | tuple[str, ...],
    items: tuple[tuple[str, str], ...],
    *,
    limit: int = 50,
    max_chars: int = 90,
) -> str:
    """Build the bounded prompt used by kotoba-whisper."""
    values: list[str] = []
    seen: set[str] = set()
    for word in preferred:
        if isinstance(word, str) and word and word not in seen:
            seen.add(word)
            values.append(word)
    for observed, output in items:
        for word in (output, observed):
            if word and word not in seen and len(word) >= 2:
                seen.add(word)
                values.append(word)
            if len(values) >= limit:
                break
        if len(values) >= limit:
            break

    kept: list[str] = []
    total = 0
    for word in values:
        added = (1 if kept else 0) + len(word)
        if total + added > max_chars:
            break
        kept.append(word)
        total += added
    return " ".join(kept)


def load_json_relaxed(raw: str) -> Any:
    """Read strict JSON first, then accept the legacy file's small conveniences."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        cleaned = _LINE_COMMENT.sub("", raw)
        cleaned = _MISSING_COMMA.sub('",', cleaned)
        cleaned = _TRAILING_COMMA.sub(r"\1", cleaned)
        return json.loads(cleaned)


def _safe_text(value: Any, *, maximum: int) -> bool:
    if not isinstance(value, str) or len(value) > maximum:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _cycle_keys(replacements: dict[str, str]) -> set[str]:
    """Return keys participating in direct replacement cycles."""
    cyclic: set[str] = set()
    for origin in replacements:
        seen: list[str] = []
        current = origin
        while current in replacements and current not in seen:
            seen.append(current)
            current = replacements[current]
        if current in seen:
            cyclic.update(seen[seen.index(current):])
    return cyclic


def validate_profile(data: Any, *, expected_id: str | None = None) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []

    def error(code: str, message: str, entry: int | None = None) -> None:
        diagnostics.append(Diagnostic("error", code, message, entry))

    def warning(code: str, message: str, entry: int | None = None) -> None:
        diagnostics.append(Diagnostic("warning", code, message, entry))

    if not isinstance(data, dict):
        return [Diagnostic("error", "profile.type", "辞書のルートはオブジェクトである必要があります")]
    if data.get("schemaVersion") != SCHEMA_VERSION:
        error("profile.schema-version", f"schemaVersion は {SCHEMA_VERSION} である必要があります")

    profile_id = data.get("id")
    if not isinstance(profile_id, str) or not _ID_RE.fullmatch(profile_id):
        error("profile.id", "id は英小文字・数字・._-からなる1〜64文字で指定してください")
    elif expected_id is not None and profile_id != expected_id:
        error("profile.id-mismatch", f"ファイル名のID（{expected_id}）と辞書ID（{profile_id}）が一致しません")

    if not _safe_text(data.get("name"), maximum=80) or not data.get("name", "").strip():
        error("profile.name", "name は1〜80文字の文字列で指定してください")
    if "description" in data and not _safe_text(data.get("description"), maximum=500):
        error("profile.description", "description は500文字以内の文字列で指定してください")

    entries = data.get("entries")
    if not isinstance(entries, list):
        error("entries.type", "entries は配列である必要があります")
        entries = []
    elif len(entries) > MAX_PROFILE_ENTRIES:
        error("entries.limit", f"entries の上限は{MAX_PROFILE_ENTRIES}件です")

    active: dict[str, tuple[str, int]] = {}
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            error("entry.type", "エントリーはオブジェクトである必要があります", index)
            continue
        observed = item.get("observed")
        output = item.get("output")
        if not _safe_text(observed, maximum=MAX_OBSERVED_LEN) or not str(observed).strip():
            error("entry.observed", f"observed は1〜{MAX_OBSERVED_LEN}文字で指定してください", index)
            continue
        if not _safe_text(output, maximum=MAX_OUTPUT_LEN):
            error("entry.output", f"output は{MAX_OUTPUT_LEN}文字以内で指定してください", index)
            continue
        if "enabled" in item and not isinstance(item["enabled"], bool):
            error("entry.enabled", "enabled は真偽値で指定してください", index)
        if "hotword" in item and not isinstance(item["hotword"], bool):
            error("entry.hotword", "hotword は真偽値で指定してください", index)
        if "priority" in item and (not isinstance(item["priority"], int) or isinstance(item["priority"], bool)):
            error("entry.priority", "priority は整数で指定してください", index)
        if "note" in item and not _safe_text(item["note"], maximum=MAX_NOTE_LEN):
            error("entry.note", f"note は{MAX_NOTE_LEN}文字以内で指定してください", index)
        if item.get("enabled", True) is False:
            continue
        observed = observed.strip()
        previous = active.get(observed)
        if previous is not None:
            if previous[0] == output:
                warning("entry.duplicate", f"同じ置換が重複しています: {observed}", index)
            else:
                error(
                    "entry.conflict",
                    f"同じobservedに異なるoutputがあります: {observed}（{previous[0]} / {output}）",
                    index,
                )
        else:
            active[observed] = (output, index)
        if observed == output:
            warning("entry.identity", f"変換前後が同一です: {observed}", index)
        if len(observed) == 1:
            warning("entry.too-short", f"1文字のキーは本文を壊しやすいため注意してください: {observed}", index)

    replacement_map = {key: value for key, (value, _index) in active.items()}
    for key in sorted(_cycle_keys(replacement_map)):
        warning("entry.cycle", f"置換が循環しています: {key}", active[key][1])

    symbols = data.get("symbols", {})
    if not isinstance(symbols, dict):
        error("symbols.type", "symbols はオブジェクトである必要があります")
    elif len(symbols) > MAX_SYMBOLS:
        error("symbols.limit", f"symbols の上限は{MAX_SYMBOLS}件です")
    else:
        for key, value in symbols.items():
            if not _safe_text(key, maximum=MAX_OBSERVED_LEN) or not key.strip():
                error("symbols.key", "symbols のキーが不正です")
            if not _safe_text(value, maximum=MAX_OUTPUT_LEN):
                error("symbols.value", f"symbols[{key!r}] の値が不正です")

    for field in ("hotwords", "hallucinations"):
        values = data.get(field, [])
        if not isinstance(values, list):
            error(f"{field}.type", f"{field} は配列である必要があります")
        elif len(values) > MAX_LIST_ITEMS:
            error(f"{field}.limit", f"{field} の上限は{MAX_LIST_ITEMS}件です")
        else:
            for index, value in enumerate(values):
                if not _safe_text(value, maximum=MAX_OUTPUT_LEN) or not value.strip():
                    error(f"{field}.value", f"{field} は空でない文字列だけを指定できます", index)
    return diagnostics


def legacy_to_profile(data: Any, *, source_path: Path | None = None) -> dict[str, Any]:
    data = data if isinstance(data, dict) else {}
    replacements = data.get("replacements") if isinstance(data.get("replacements"), dict) else {}
    entries = [
        {"observed": key, "output": value, "enabled": True}
        for key, value in replacements.items()
        if isinstance(key, str) and key.strip() and isinstance(value, str)
    ]
    profile: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "id": "common",
        "name": "共通",
        "description": "旧 userdict.json から移行した共通辞書",
        "language": "ja",
        "entries": entries,
        "symbols": data.get("symbols") if isinstance(data.get("symbols"), dict) else {},
        "hotwords": data.get("hotwords") if isinstance(data.get("hotwords"), list) else [],
        "hallucinations": (
            data.get("hallucinations") if isinstance(data.get("hallucinations"), list) else []
        ),
    }
    if source_path is not None:
        try:
            digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        profile["migration"] = {
            "source": source_path.name,
            "sourceSha256": digest,
            "migratedAt": datetime.now(timezone.utc).isoformat(),
        }
    return profile


def _content_revision(data: Any) -> str:
    canonical = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


class DictionaryRegistry:
    def __init__(self, root: Path, legacy_path: Path):
        self.root = root
        self.profiles_dir = root / "profiles"
        self.sets_path = root / "sets.json"
        self.legacy_path = legacy_path
        self._write_lock = threading.RLock()

    def ensure_initialized(self, defaults: dict[str, Any]) -> None:
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        common_path = self.profile_path("common")
        if not common_path.exists():
            source = defaults
            if self.legacy_path.is_file():
                try:
                    source = load_json_relaxed(self.legacy_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    source = defaults
            self._write_json(common_path, legacy_to_profile(source, source_path=self.legacy_path))
        if not self.sets_path.exists():
            self._write_json(self.sets_path, {
                "schemaVersion": SCHEMA_VERSION,
                "sets": [{
                    "id": "default",
                    "name": "共通",
                    "description": "共通辞書のみを使用する既定セット",
                    "profiles": ["common"],
                    "writableProfile": "common",
                }],
            })

    def profile_path(self, profile_id: str) -> Path:
        if not _ID_RE.fullmatch(profile_id or ""):
            raise DictionarySchemaError("辞書IDの書式が不正です")
        return self.profiles_dir / f"{profile_id}.json"

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp, path)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _read_json(self, path: Path) -> Any:
        if path.stat().st_size > MAX_PROFILE_BYTES:
            raise DictionarySchemaError(f"ファイルサイズが上限（{MAX_PROFILE_BYTES} bytes）を超えています")
        try:
            return load_json_relaxed(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DictionarySchemaError(f"{path.name} を読めません: {exc}") from exc

    def load_profile(self, profile_id: str) -> dict[str, Any]:
        path = self.profile_path(profile_id)
        if not path.is_file():
            raise DictionarySchemaError(f"辞書が見つかりません: {profile_id}")
        data = self._read_json(path)
        diagnostics = validate_profile(data, expected_id=profile_id)
        errors = [item for item in diagnostics if item.severity == "error"]
        if errors:
            raise DictionarySchemaError(errors[0].message, diagnostics)
        return data

    def load_sets(self) -> dict[str, Any]:
        if not self.sets_path.is_file():
            raise DictionarySchemaError("sets.json が見つかりません")
        data = self._read_json(self.sets_path)
        if not isinstance(data, dict) or data.get("schemaVersion") != SCHEMA_VERSION:
            raise DictionarySchemaError(f"sets.json の schemaVersion は {SCHEMA_VERSION} である必要があります")
        sets = data.get("sets")
        if not isinstance(sets, list):
            raise DictionarySchemaError("sets は配列である必要があります")
        seen: set[str] = set()
        for item in sets:
            if not isinstance(item, dict):
                raise DictionarySchemaError("辞書セットはオブジェクトである必要があります")
            set_id = item.get("id")
            if not isinstance(set_id, str) or not _ID_RE.fullmatch(set_id) or set_id in seen:
                raise DictionarySchemaError("辞書セットのidが不正または重複しています")
            seen.add(set_id)
            profiles = item.get("profiles")
            if not isinstance(profiles, list) or not profiles:
                raise DictionarySchemaError(f"辞書セット {set_id} のprofilesが不正です")
            if any(not isinstance(value, str) or not _ID_RE.fullmatch(value) for value in profiles):
                raise DictionarySchemaError(f"辞書セット {set_id} に不正な辞書IDがあります")
            if len(set(profiles)) != len(profiles):
                raise DictionarySchemaError(f"辞書セット {set_id} に同じ辞書IDが重複しています")
            writable = item.get("writableProfile", profiles[-1])
            if not isinstance(writable, str) or writable not in profiles:
                raise DictionarySchemaError(
                    f"辞書セット {set_id} のwritableProfileはprofiles内から指定してください"
                )
        return data

    def signature(self, defaults: dict[str, Any]) -> tuple[tuple[str, int, int], ...]:
        self.ensure_initialized(defaults)
        paths = [self.sets_path, *sorted(self.profiles_dir.glob("*.json"))]
        return tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths)

    def compile_set(self, set_id: str = "default") -> DictionarySnapshot:
        catalog = self.load_sets()
        selected = next((item for item in catalog["sets"] if item["id"] == set_id), None)
        if selected is None:
            raise DictionarySchemaError(f"辞書セットが見つかりません: {set_id}")

        replacements: dict[str, str] = {}
        replacement_sources: dict[str, str] = {}
        symbols: dict[str, str] = {}
        symbol_sources: dict[str, str] = {}
        hotword_candidates: list[tuple[int, int, int, str]] = []
        hallucinations: set[str] = set()
        diagnostics: list[Diagnostic] = []
        profile_revisions: list[tuple[str, str]] = []
        revision_payload: dict[str, Any] = {"set": selected, "profiles": []}
        profile_ids = tuple(selected["profiles"])
        writable_profile_id = str(selected.get("writableProfile") or profile_ids[-1])
        for profile_index, profile_id in enumerate(profile_ids):
            profile = self.load_profile(profile_id)
            profile_revisions.append((profile_id, _content_revision(profile)))
            revision_payload["profiles"].append(profile)
            for entry_index, item in enumerate(profile["entries"]):
                if item.get("enabled", True) is False:
                    continue
                observed = item["observed"].strip()
                previous = replacements.get(observed)
                if previous is not None and previous != item["output"]:
                    diagnostics.append(Diagnostic(
                        "warning",
                        "set.replacement-override",
                        f"{observed}: {replacement_sources[observed]} の {previous} を "
                        f"{profile_id} の {item['output']} で上書きします",
                    ))
                replacements[observed] = item["output"]
                replacement_sources[observed] = profile_id
                if item.get("hotword"):
                    priority = item.get("priority", 0)
                    for word_index, word in enumerate((item["output"], observed)):
                        hotword_candidates.append((
                            int(priority), profile_index, entry_index * 2 + word_index, word,
                        ))
            for symbol, output in profile.get("symbols", {}).items():
                previous = symbols.get(symbol)
                if previous is not None and previous != output:
                    diagnostics.append(Diagnostic(
                        "warning",
                        "set.symbol-override",
                        f"記号語 {symbol}: {symbol_sources[symbol]} の {previous} を "
                        f"{profile_id} の {output} で上書きします",
                    ))
                symbols[symbol] = output
                symbol_sources[symbol] = profile_id
            # Explicit profile hotwords outrank entry-derived hotwords. Later
            # profiles are more specific and win ties.
            for word_index, word in enumerate(profile.get("hotwords", [])):
                hotword_candidates.append((10_000, profile_index, word_index, word))
            hallucinations.update(profile.get("hallucinations", []))

        ordered = tuple(sorted(replacements.items(), key=lambda pair: len(pair[0]), reverse=True))
        reverse = tuple(sorted(
            ((output, observed) for observed, output in ordered if output and observed),
            key=lambda pair: len(pair[0]),
            reverse=True,
        ))
        hotword_candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
        hotwords: list[str] = []
        hotword_seen: set[str] = set()
        for _priority, _profile_index, _order, word in hotword_candidates:
            if word and word not in hotword_seen:
                hotword_seen.add(word)
                hotwords.append(word)
        plan = ReplacementPlan.compile(ordered)
        return DictionarySnapshot(
            set_id=set_id,
            set_name=str(selected.get("name") or set_id),
            revision=_content_revision(revision_payload),
            profile_ids=profile_ids,
            profile_revisions=tuple(profile_revisions),
            writable_profile_id=writable_profile_id,
            replacements=ordered,
            reverse_replacements=reverse,
            replacement_plan=plan,
            symbols=MappingProxyType(dict(symbols)),
            hotwords=tuple(hotwords),
            hotword_prompt=build_hotword_prompt(hotwords, ordered),
            hallucinations=frozenset(hallucinations),
            diagnostics=tuple(diagnostics),
        )

    def profile_projection(self, profile_id: str = "common") -> dict[str, Any]:
        profile = self.load_profile(profile_id)
        replacements = {
            item["observed"].strip(): item["output"]
            for item in profile["entries"]
            if item.get("enabled", True) is not False
        }
        path = self.profile_path(profile_id)
        return {
            "replacements": replacements,
            "symbols": dict(profile.get("symbols", {})),
            "hotwords": list(profile.get("hotwords", [])),
            "hallucinations": list(profile.get("hallucinations", [])),
            "path": str(path),
            "legacyPath": str(self.legacy_path),
            "profileId": profile_id,
            "revision": _content_revision(profile),
        }

    def add_entry(
        self,
        profile_id: str,
        observed: str,
        output: str,
        *,
        expected_revision: str | None = None,
        hotword: bool = False,
        priority: int = 0,
        note: str = "",
    ) -> dict[str, Any]:
        """Append one safe replacement without overwriting concurrent edits."""
        with self._write_lock:
            profile = self.load_profile(profile_id)
            current_revision = _content_revision(profile)
            if expected_revision is not None and expected_revision != current_revision:
                raise DictionaryRevisionConflict(
                    "辞書が別の操作で更新されています。再読み込みしてから登録してください",
                    current_revision=current_revision,
                )

            normalized_observed = observed.strip() if isinstance(observed, str) else observed
            entries = profile.get("entries", [])
            for item in entries:
                if not isinstance(item, dict) or item.get("enabled", True) is False:
                    continue
                item_observed = item.get("observed")
                if not isinstance(item_observed, str) or item_observed.strip() != normalized_observed:
                    continue
                if item.get("output") == output:
                    return {
                        "ok": True,
                        "created": False,
                        "profileId": profile_id,
                        "revision": current_revision,
                        "entry": dict(item),
                    }
                raise DictionaryRevisionConflict(
                    f"{normalized_observed} は既に別の表記へ登録されています",
                    current_revision=current_revision,
                )

            entry: dict[str, Any] = {
                "observed": normalized_observed,
                "output": output,
                "enabled": True,
            }
            if hotword:
                entry["hotword"] = True
            if priority:
                entry["priority"] = priority
            if note:
                entry["note"] = note
            profile["entries"] = [*entries, entry]
            diagnostics = validate_profile(profile, expected_id=profile_id)
            errors = [item for item in diagnostics if item.severity == "error"]
            if errors:
                raise DictionarySchemaError(errors[0].message, diagnostics)

            path = self.profile_path(profile_id)
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, backup)
            self._write_json(path, profile)
            return {
                "ok": True,
                "created": True,
                "profileId": profile_id,
                "revision": _content_revision(profile),
                "entry": entry,
                "backupPath": str(backup),
            }

    def update_profile_maps(
        self,
        profile_id: str,
        replacements: dict[str, str],
        symbols: dict[str, str],
    ) -> None:
        with self._write_lock:
            profile = self.load_profile(profile_id)
            preserved = {
                (item.get("observed"), item.get("output")): item
                for item in profile["entries"]
                if isinstance(item, dict)
            }
            active_entries: list[dict[str, Any]] = []
            for observed, output in replacements.items():
                old = preserved.get((observed, output))
                entry = dict(old) if old is not None else {"observed": observed, "output": output}
                entry["observed"] = observed
                entry["output"] = output
                entry["enabled"] = True
                active_entries.append(entry)
            # The legacy editor cannot display disabled entries. Preserve them verbatim.
            active_entries.extend(
                dict(item) for item in profile["entries"]
                if isinstance(item, dict) and item.get("enabled") is False
            )
            profile["entries"] = active_entries
            profile["symbols"] = dict(symbols)
            diagnostics = validate_profile(profile, expected_id=profile_id)
            errors = [item for item in diagnostics if item.severity == "error"]
            if errors:
                raise DictionarySchemaError(errors[0].message, diagnostics)
            self._write_json(self.profile_path(profile_id), profile)

    def catalog(self) -> dict[str, Any]:
        profiles: list[dict[str, Any]] = []
        for path in sorted(self.profiles_dir.glob("*.json")):
            profile_id = path.stem
            try:
                data = self._read_json(path)
                diagnostics = validate_profile(data, expected_id=profile_id)
                entries = data.get("entries", []) if isinstance(data, dict) else []
                profiles.append({
                    "id": data.get("id", profile_id) if isinstance(data, dict) else profile_id,
                    "name": data.get("name", profile_id) if isinstance(data, dict) else profile_id,
                    "description": data.get("description", "") if isinstance(data, dict) else "",
                    "entries": len(entries) if isinstance(entries, list) else 0,
                    "enabledEntries": sum(
                        1 for item in entries
                        if isinstance(item, dict) and item.get("enabled", True) is not False
                    ) if isinstance(entries, list) else 0,
                    "modifiedNs": path.stat().st_mtime_ns,
                    "valid": not any(item.severity == "error" for item in diagnostics),
                    "diagnostics": [item.as_dict() for item in diagnostics],
                })
            except Exception as exc:  # A broken profile must not hide the healthy profiles.
                profiles.append({
                    "id": profile_id,
                    "name": profile_id,
                    "entries": 0,
                    "enabledEntries": 0,
                    "modifiedNs": path.stat().st_mtime_ns,
                    "valid": False,
                    "diagnostics": [{
                        "severity": "error",
                        "code": "profile.read",
                        "message": str(exc),
                    }],
                })
        sets: list[dict[str, Any]] = []
        for item in self.load_sets()["sets"]:
            view = dict(item)
            try:
                snapshot = self.compile_set(item["id"])
                view.update({
                    "valid": True,
                    "revision": snapshot.revision,
                    "profileRevisions": dict(snapshot.profile_revisions),
                    "diagnostics": [diagnostic.as_dict() for diagnostic in snapshot.diagnostics],
                })
            except (DictionarySchemaError, OSError) as exc:
                view.update({
                    "valid": False,
                    "revision": None,
                    "profileRevisions": {},
                    "diagnostics": [{
                        "severity": "error",
                        "code": "set.compile",
                        "message": str(exc),
                    }],
                })
            sets.append(view)
        return {"schemaVersion": SCHEMA_VERSION, "profiles": profiles, "sets": sets}
