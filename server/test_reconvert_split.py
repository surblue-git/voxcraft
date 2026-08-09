"""読みの分割の単体テスト（ネットワーク不要）。

Google CGI は読みが 53字を超えると**例外ではなく空配列**を返す。分割せずに投げると
候補ゼロのまま黙って終わり、プラグインは空のモーダルを開く——長い一文の再変換が
無反応だった原因がこれ。ここでは分割そのものの性質だけを確かめる。
"""
from __future__ import annotations

from reconvert import split_reading


def _m(*pairs: tuple[str, str]) -> list[tuple[str, str]]:
    return list(pairs)


def test_short_reading_stays_one_call():
    """上限内なら分割しない（短い入力の挙動を変えない）。"""
    ms = _m(("今日", "きょう"), ("は", "は"), ("会議", "かいぎ"))
    assert split_reading(ms, limit=53) == ["きょうはかいぎ"]


def test_never_exceeds_limit():
    ms = _m(*[("語", "あいうえお")] * 40)  # 読み200字
    parts = split_reading(ms, limit=53)
    assert all(len(p) <= 53 for p in parts), [len(p) for p in parts]


def test_concatenation_is_lossless():
    """繋ぎ直すと元の読みに戻る。1文字でも落ちたら候補がずれる。"""
    ms = _m(("海外", "かいがい"), ("セグメント", "せぐめんと"), ("において", "において"),
            ("も", "も"), ("全て", "すべて"), ("の", "の"), ("ユニット", "ゆにっと"),
            ("で", "で"), ("増収", "ぞうしゅう"), ("と", "と"), ("なりました", "なりました"))
    whole = "".join(r for _, r in ms)
    assert "".join(split_reading(ms, limit=12)) == whole


def test_splits_only_at_morpheme_boundaries():
    """形態素の途中で切らない。切ると両側の変換候補が無意味になる。"""
    ms = _m(("増収", "ぞうしゅう"), ("と", "と"), ("なりました", "なりました"))
    readings = {r for _, r in ms}
    for part in split_reading(ms, limit=6):
        # 各断片は形態素の読みを順に連結したものになっているはず
        rest = part
        while rest:
            head = next((r for r in readings if rest.startswith(r)), None)
            assert head is not None, f"形態素境界で切れていない: {part!r}"
            rest = rest[len(head):]


def test_single_long_morpheme_is_force_split():
    """1形態素だけで上限を超えるなら、やむを得ず割る（進まないと無限ループ）。"""
    ms = _m(("超長いカタカナ語", "あ" * 120))
    parts = split_reading(ms, limit=53)
    assert all(len(p) <= 53 for p in parts)
    assert "".join(parts) == "あ" * 120


def test_empty_readings_are_skipped():
    ms = _m(("　", ""), ("会議", "かいぎ"), ("", ""))
    assert split_reading(ms, limit=53) == ["かいぎ"]


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
