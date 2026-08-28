"""既知の誤りの採点のテスト。

正解は辞書そのもの（実際に観測された誤認識と正しい表記の対）なので、
ここで確かめるのは**数えかた**が正しいこと。特に、包含関係のある登録を
二重に数えないこと（0.23.0 で「1回の走査で実際に当たった数」と決めた規則）。
"""
from dictionary_registry import ReplacementPlan
from refine_score import (
    ErrorScore,
    KnownError,
    audit_dictionary,
    format_score,
    load_known_errors,
    plan_counter,
)


def _counter(*items):
    """辞書と同じく、長いキーが先に来る並びで計画を作る。"""
    ordered = tuple(sorted(items, key=lambda kv: -len(kv[0])))
    return plan_counter(ReplacementPlan.compile(ordered))


def test_counts_a_fix():
    errors = [KnownError("SMTV", "SMTB")]
    count = _counter(("SMTV", "SMTB"))
    score = ErrorScore()
    score.add_block("SMTVの決算について。", "SMTBの決算について。", errors, count)
    assert (score.present, score.fixed, score.remained) == (1, 1, 0)
    assert score.fix_rate == 1.0


def test_counts_an_error_left_alone():
    errors = [KnownError("SMTV", "SMTB")]
    count = _counter(("SMTV", "SMTB"))
    score = ErrorScore()
    score.add_block("SMTVの決算について。", "SMTVの決算について。", errors, count)
    assert (score.present, score.fixed, score.remained) == (1, 0, 1)


def test_an_error_that_vanished_without_the_right_word_is_not_a_fix():
    """誤りが消えても正しい表記が出ていないなら、直ったとは数えない。"""
    errors = [KnownError("SMTV", "SMTB")]
    count = _counter(("SMTV", "SMTB"))
    score = ErrorScore()
    score.add_block("SMTVの決算について。", "同社の決算について。", errors, count)
    assert (score.fixed, score.remained, score.lost) == (0, 0, 1)


def test_introducing_a_known_error_counts_as_damage():
    errors = [KnownError("SMTV", "SMTB")]
    count = _counter(("SMTV", "SMTB"))
    score = ErrorScore()
    score.add_block("SMTBの決算について。", "SMTVの決算について。", errors, count)
    assert score.broken == 1
    assert score.present == 0


def test_nested_keys_are_counted_once():
    """包含関係のある登録を二重に数えない。

    独立に数えると『どこもSMTVネット銀行』と『SMTV』で2件になるが、
    サーバーの1回走査では長いほうだけが当たるので1件。
    """
    errors = [
        KnownError("どこもSMTVネット銀行", "ドコモSMTBネット銀行"),
        KnownError("SMTV", "SMTB"),
    ]
    count = _counter(("どこもSMTVネット銀行", "ドコモSMTBネット銀行"), ("SMTV", "SMTB"))
    score = ErrorScore()
    score.add_block("どこもSMTVネット銀行の話です。",
                    "ドコモSMTBネット銀行の話です。", errors, count)
    assert score.present == 1, score.entries
    assert score.fixed == 1
    assert score.entries["どこもSMTVネット銀行"].fixed == 1
    assert "SMTV" not in score.entries


def test_multiplicity_is_tracked():
    errors = [KnownError("ウィンドウズ", "Windows")]
    count = _counter(("ウィンドウズ", "Windows"))
    score = ErrorScore()
    score.add_block("ウィンドウズとウィンドウズの話。", "WindowsとWindowsの話。",
                    errors, count)
    assert (score.present, score.fixed) == (2, 2)


def test_partial_fix_is_split():
    errors = [KnownError("ウィンドウズ", "Windows")]
    count = _counter(("ウィンドウズ", "Windows"))
    score = ErrorScore()
    score.add_block("ウィンドウズとウィンドウズの話。", "Windowsとウィンドウズの話。",
                    errors, count)
    assert (score.present, score.fixed, score.remained) == (2, 1, 1)


def test_load_known_errors_skips_no_ops():
    known = load_known_errors([("SMTV", "SMTB"), ("同じ", "同じ"), ("", "X"), ("Y", "")])
    assert [(k.observed, k.output) for k in known] == [("SMTV", "SMTB")]


def test_format_reports_both_sides_of_the_gate():
    errors = [KnownError("SMTV", "SMTB")]
    count = _counter(("SMTV", "SMTB"))
    model = ErrorScore()
    model.add_block("SMTVの決算。", "SMTBの決算。", errors, count)
    accepted = ErrorScore()          # 門が却下したので本文は生のまま
    accepted.add_block("SMTVの決算。", "SMTVの決算。", errors, count)

    out = format_score(model, accepted)
    assert "モデルが直した          1 件" in out
    assert "門を通って本文が直る    0 件" in out
    assert "門で止まったぶんが 1 件" in out
    assert "SMTV → SMTB" in out


def test_format_says_when_there_is_nothing_to_measure():
    assert "測れない" in format_score(ErrorScore())


# --- 辞書の健康診断 -------------------------------------------------------

def test_audit_flags_keys_that_are_real_words():
    """それ自体が正しい日本語のキーだけを挙げる。

    `現役 → 減益` は決算の文脈でしか正しくない。無条件置換は文脈を見ないので
    「現役の行員」を「減益の行員」にしてしまう（実データで確認済み）。
    """
    from refine_guard import SudachiMorphology

    morph = SudachiMorphology()
    if not morph.available:
        print("     （Sudachi が無いので飛ばす）")
        return

    risky = dict(audit_dictionary([
        ("現役", "減益"),        # 普通に使う語 → 挙がるべき
        ("再建", "債権"),        # 同上
        ("蘇生", "組成"),        # 同上
        ("水尾銀行", "みずほ銀行"),  # ASRの崩れ（複数形態素）→ 挙がらない
        ("一軒米氏", "eKYC"),      # 同上
        ("SMTV", "SMTB"),        # 同上
    ], morph))
    assert "現役" in risky and "再建" in risky and "蘇生" in risky, risky
    assert "水尾銀行" not in risky and "一軒米氏" not in risky and "SMTV" not in risky, risky


def test_audit_puts_kanji_keys_first():
    """漢字語のほうが危ない。カタカナの表記ゆれは1語でも文章を壊さない。"""
    from refine_guard import SudachiMorphology

    morph = SudachiMorphology()
    if not morph.available:
        print("     （Sudachi が無いので飛ばす）")
        return
    order = [k for k, _ in audit_dictionary(
        [("アンドロイド", "Android"), ("現役", "減益")], morph)]
    assert order and order[0] == "現役", order


def test_ambiguous_keys_left_alone_are_not_counted_as_failures():
    """正しい用法を直さなかったことを、失点として出さない。"""
    errors = [KnownError("現役", "減益")]
    count = _counter(("現役", "減益"))
    score = ErrorScore()
    score.add_block("現役の行員に話を聞いた。", "現役の行員に話を聞いた。", errors, count)
    out = format_score(score, ambiguous={"現役"})
    assert "正しい用法かもしれない語" in out
    assert "直せなかった語" not in out


def _run_all() -> int:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
