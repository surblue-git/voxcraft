"""元テキストの文字位置を保ったまま読みへ割れるかのテスト（ネットワーク不要）。

タップした位置から文節を決めるには、読みへ変換したあとも元の位置へ戻れないといけない。
以前は辞書の逆引きが `text.replace(表記, 読み)` でテキストごと書き換えていたので、
その時点で対応が失われていた。ここで守るのは次の2点:

  - `text[span.start:span.end] == span.surface`（位置が正しい）
  - span を連結すると元テキストに戻る（隙間も重なりも無い）

この2つが成り立つ限り、候補で置換してもテキストは壊れない。
"""
from __future__ import annotations

from reconvert import _reading

# 逆引きは「表記の長い順」で渡される（userdict.get_reverse_replacements と同じ約束）。
REPS = [("データセンター", "でーたせんたー"), ("Olive", "おりーぶ"), ("IOWN", "あいおん")]


def _check_invariants(text: str, reps=REPS) -> list:
    spans = _reading.to_spans(text, reps)
    assert "".join(s.surface for s in spans) == text, "連結して元テキストに戻らない"
    for s in spans:
        assert text[s.start : s.end] == s.surface, f"位置がずれている: {s}"
    for a, b in zip(spans, spans[1:]):
        assert a.end == b.start, f"隙間か重なりがある: {a} → {b}"
    if spans:
        assert spans[0].start == 0 and spans[-1].end == len(text)
    return spans


def test_plain_japanese_keeps_positions():
    _check_invariants("この施策が業績に寄与しました。")


def test_dictionary_entry_keeps_its_span():
    text = "Oliveの決済を使いました。"
    spans = _check_invariants(text)
    hit = next(s for s in spans if s.surface == "Olive")
    assert (hit.start, hit.end) == (0, 5)
    assert hit.reading == "おりーぶ", hit.reading


def test_dictionary_entry_in_the_middle_and_end():
    text = "回線はIOWN"
    spans = _check_invariants(text)
    hit = next(s for s in spans if s.surface == "IOWN")
    assert (hit.start, hit.end) == (3, 7)
    assert hit.reading == "あいおん"


def test_longest_entry_wins():
    """「データセンター」が先に並ぶので、短い綴りに食われない。"""
    text = "データセンターの運用"
    spans = _check_invariants(text, [("データセンター", "でーたせんたー"), ("データ", "でーた")])
    assert spans[0].surface == "データセンター", spans[0]


def test_multiple_entries_in_one_sentence():
    spans = _check_invariants("OliveとIOWNの話をします。")
    surfaces = [s.surface for s in spans]
    assert "Olive" in surfaces and "IOWN" in surfaces


def test_no_dictionary_hit():
    _check_invariants("今日は会議があります。", reps=[])


def test_empty_text():
    assert _reading.to_spans("", REPS) == []


def test_reading_is_unaffected_by_position_tracking():
    """読みそのものは従来と同じ（分割・変換の挙動を変えていない）。"""
    text = "Oliveの決済"
    hira = "".join(s.reading for s in _reading.to_spans(text, REPS))
    assert hira.startswith("おりーぶ"), hira
    assert _reading.to_hiragana(text, REPS) == hira


def _run_all() -> int:
    functions = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for function in functions:
        try:
            function()
            print(f"ok   {function.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {function.__name__}: {exc}")
    print(f"\n{len(functions) - failed}/{len(functions)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
