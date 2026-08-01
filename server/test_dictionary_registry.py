"""Phase 1A dictionary schema, migration, and compatibility tests."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from dictionary_registry import (
    DictionaryRegistry,
    DictionaryRevisionConflict,
    legacy_to_profile,
    load_json_relaxed,
    validate_profile,
)


DEFAULTS = {
    "replacements": {"ウィンドウズ": "Windows"},
    "symbols": {"当点": "、"},
    "hotwords": ["VoxCraft"],
    "hallucinations": ["ご視聴ありがとうございました"],
}


def _registry(tmp: str) -> tuple[DictionaryRegistry, Path, Path]:
    base = Path(tmp)
    legacy = base / "userdict.json"
    root = base / "dictionaries"
    return DictionaryRegistry(root, legacy), legacy, root


def test_non_destructive_legacy_migration():
    with TemporaryDirectory() as tmp:
        registry, legacy, root = _registry(tmp)
        raw = json.dumps(DEFAULTS, ensure_ascii=False, indent=2)
        legacy.write_text(raw, encoding="utf-8")

        registry.ensure_initialized({})

        assert legacy.read_text(encoding="utf-8") == raw
        profile = registry.load_profile("common")
        assert profile["schemaVersion"] == 1
        assert profile["migration"]["source"] == "userdict.json"
        assert profile["symbols"] == DEFAULTS["symbols"]
        assert profile["hotwords"] == DEFAULTS["hotwords"]
        assert profile["hallucinations"] == DEFAULTS["hallucinations"]
        assert profile["entries"] == [
            {"observed": "ウィンドウズ", "output": "Windows", "enabled": True}
        ]
        assert (root / "sets.json").is_file()


def test_compile_default_set_preserves_legacy_behavior():
    with TemporaryDirectory() as tmp:
        registry, legacy, _root = _registry(tmp)
        data = dict(DEFAULTS)
        data["replacements"] = {
            "短い": "短",
            "とても長い観測表記": "長",
        }
        legacy.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        registry.ensure_initialized({})

        compiled = registry.compile_set("default")

        assert compiled.profile_ids == ("common",)
        assert compiled.replacements == (
            ("とても長い観測表記", "長"),
            ("短い", "短"),
        )
        assert compiled.symbols == DEFAULTS["symbols"]
        assert compiled.hotwords == ("VoxCraft",)
        assert compiled.hallucinations == frozenset(DEFAULTS["hallucinations"])


def test_legacy_projection_update_preserves_extension_fields():
    with TemporaryDirectory() as tmp:
        registry, _legacy, _root = _registry(tmp)
        registry.ensure_initialized(DEFAULTS)
        profile = registry.load_profile("common")
        profile["entries"][0].update({"hotword": True, "priority": 90, "note": "製品名"})
        profile["entries"].append({
            "observed": "無効語",
            "output": "無効",
            "enabled": False,
            "note": "後で確認",
        })
        registry._write_json(registry.profile_path("common"), profile)

        registry.update_profile_maps(
            "common",
            {"ウィンドウズ": "Windows", "アンドロイド": "Android"},
            {"当店": "、"},
        )

        updated = registry.load_profile("common")
        windows = next(item for item in updated["entries"] if item["observed"] == "ウィンドウズ")
        disabled = next(item for item in updated["entries"] if item["observed"] == "無効語")
        assert windows["hotword"] is True
        assert windows["priority"] == 90
        assert windows["note"] == "製品名"
        assert disabled["enabled"] is False
        assert disabled["note"] == "後で確認"
        assert updated["symbols"] == {"当店": "、"}


def test_validation_reports_conflicts_and_risky_rules():
    profile = legacy_to_profile({"replacements": {}})
    profile["entries"] = [
        {"observed": "A", "output": "B"},
        {"observed": "A", "output": "C"},
        {"observed": "B", "output": "A"},
        {"observed": "同じ", "output": "同じ"},
    ]

    diagnostics = validate_profile(profile)
    codes = {item.code for item in diagnostics}

    assert "entry.conflict" in codes
    assert "entry.too-short" in codes
    assert "entry.cycle" in codes
    assert "entry.identity" in codes
    assert any(item.severity == "error" for item in diagnostics)


def test_relaxed_json_remains_available_for_migration():
    parsed = load_json_relaxed('''{
      // legacy comment
      "replacements": {"A": "B",},
      "symbols": {},
    }''')
    assert parsed["replacements"] == {"A": "B"}


def test_catalog_keeps_broken_profile_visible():
    with TemporaryDirectory() as tmp:
        registry, _legacy, root = _registry(tmp)
        registry.ensure_initialized(DEFAULTS)
        (root / "profiles" / "broken.json").write_text("{broken", encoding="utf-8")

        catalog = registry.catalog()

        by_id = {item["id"]: item for item in catalog["profiles"]}
        assert by_id["common"]["valid"] is True
        assert by_id["broken"]["valid"] is False
        assert by_id["broken"]["diagnostics"][0]["code"] == "profile.read"


def test_snapshot_is_immutable_and_revision_tracks_semantic_content():
    with TemporaryDirectory() as tmp:
        registry, _legacy, root = _registry(tmp)
        registry.ensure_initialized(DEFAULTS)
        common = registry.load_profile("common")
        common["entries"] = [
            {"observed": "A", "output": "common"},
            {"observed": "B", "output": "C"},
            {"observed": "AB", "output": "longest"},
        ]
        registry._write_json(registry.profile_path("common"), common)
        genre = {
            "schemaVersion": 1,
            "id": "genre",
            "name": "Genre",
            "entries": [{"observed": "A", "output": "genre"}],
            "symbols": {"記号語": "！"},
            "hotwords": [],
            "hallucinations": [],
        }
        registry._write_json(registry.profile_path("genre"), genre)
        registry._write_json(root / "sets.json", {
            "schemaVersion": 1,
            "sets": [{"id": "default", "name": "Test", "profiles": ["common", "genre"]}],
        })

        first = registry.compile_set("default")
        assert first.replacement_plan.apply("AB A B") == "longest genre C"
        assert first.replacement_plan.apply("A B") == "genre C"  # no cascading genre -> ...
        assert first.symbols["記号語"] == "！"
        assert any(item.code == "set.replacement-override" for item in first.diagnostics)

        try:
            first.symbols["記号語"] = "?"  # type: ignore[index]
            raise AssertionError("snapshot symbols were mutable")
        except TypeError:
            pass

        # Whitespace-only rewrites do not change the semantic revision.
        registry.profile_path("genre").write_text(
            json.dumps(genre, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        same = registry.compile_set("default")
        assert same.revision == first.revision

        # Existing snapshots keep their values after a file update; a new snapshot gets a new revision.
        genre["entries"][0]["output"] = "updated"
        registry._write_json(registry.profile_path("genre"), genre)
        second = registry.compile_set("default")
        assert first.replacement_plan.apply("A") == "genre"
        assert second.replacement_plan.apply("A") == "updated"
        assert second.revision != first.revision


def test_replacement_plan_is_single_pass_and_hotwords_use_priority():
    with TemporaryDirectory() as tmp:
        registry, _legacy, _root = _registry(tmp)
        registry.ensure_initialized(DEFAULTS)
        profile = registry.load_profile("common")
        profile["entries"] = [
            {"observed": "A", "output": "B"},
            {"observed": "B", "output": "C"},
            {"observed": "low", "output": "LOW", "hotword": True, "priority": 1},
            {"observed": "high", "output": "HIGH", "hotword": True, "priority": 50},
        ]
        profile["hotwords"] = ["EXPLICIT"]
        registry._write_json(registry.profile_path("common"), profile)

        snapshot = registry.compile_set("default")

        assert snapshot.replacement_plan.apply("A B") == "B C"
        assert snapshot.hotwords[:5] == ("EXPLICIT", "HIGH", "high", "LOW", "low")
        assert snapshot.hotword_prompt.startswith("EXPLICIT HIGH high LOW low")


def test_single_entry_add_is_idempotent_and_rejects_stale_or_conflicting_writes():
    with TemporaryDirectory() as tmp:
        registry, _legacy, root = _registry(tmp)
        registry.ensure_initialized(DEFAULTS)
        original = registry.profile_projection("common")

        created = registry.add_entry(
            "common", "オブシディアン", "Obsidian",
            expected_revision=original["revision"],
        )
        assert created["created"] is True
        assert Path(created["backupPath"]).is_file()
        assert registry.compile_set("default").replacement_plan.apply("オブシディアン") == "Obsidian"

        repeated = registry.add_entry(
            "common", "オブシディアン", "Obsidian",
            expected_revision=created["revision"],
        )
        assert repeated["created"] is False

        try:
            registry.add_entry(
                "common", "別の語", "別表記",
                expected_revision=original["revision"],
            )
            raise AssertionError("stale revision was accepted")
        except DictionaryRevisionConflict as exc:
            assert exc.current_revision == created["revision"]

        try:
            registry.add_entry(
                "common", "オブシディアン", "別表記",
                expected_revision=created["revision"],
            )
            raise AssertionError("conflicting replacement was accepted")
        except DictionaryRevisionConflict:
            pass

        assert (root / "profiles" / "common.json.bak").is_file()


def _run_all() -> int:
    functions = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    failed = 0
    for function in functions:
        try:
            function()
            print(f"PASS {function.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {function.__name__}: {exc}")
    print(f"\n{len(functions) - failed}/{len(functions)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
