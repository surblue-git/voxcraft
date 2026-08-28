"""AI校正の門のテスト。

Sudachi を入れずに回せるよう、形態素解析は偽物を差し込む。読みは
テストが直接与えるので、ここで確かめているのは**判定の規則そのもの**。
"""
from decimal import Decimal

from refine_guard import (
    CONVERSATION,
    INTERVIEW,
    MANUSCRIPT,
    GuardStats,
    RefineGuard,
    Term,
    extract_numbers,
    is_subsequence,
    normalize_reading,
)


class FakeMorphology:
    """テキスト→読み／語 を表で与える偽物。未登録なら表層をそのまま読みにする。"""

    def __init__(self, readings=None, terms=None):
        self._readings = readings or {}
        self._terms = terms or {}

    @property
    def available(self) -> bool:
        return True

    def reading(self, text: str) -> str:
        return self._readings.get(text, text)

    def terms(self, text: str):
        return [Term(s, r) for s, r in self._terms.get(text, [])]


class UnavailableMorphology:
    available = False

    def reading(self, text): return ""

    def terms(self, text): return []


def _guard(readings=None, terms=None) -> RefineGuard:
    return RefineGuard(FakeMorphology(readings, terms))


# --- 読みの正規化 ---------------------------------------------------------

def test_normalize_reading_folds_the_allowed_differences():
    # 濁点・小書き・長音・記号・数字は畳む。
    assert normalize_reading("どうおんいきご") == normalize_reading("どうおんいぎご")
    assert normalize_reading("とうきょう") == normalize_reading("とーきょー")
    assert normalize_reading("きゃく") == normalize_reading("きやく")
    assert normalize_reading("くとうてん。") == normalize_reading("くとうてん")
    # カタカナで来ても同じ土俵に乗る。
    assert normalize_reading("スミシン") == normalize_reading("すみしん")


def test_normalize_reading_keeps_real_differences():
    assert normalize_reading("さんか") != normalize_reading("さんかい")
    assert normalize_reading("ていきょう") != normalize_reading("ごていきょう")


def test_is_subsequence():
    assert is_subsequence("あいう", "あXいYうZ")
    assert not is_subsequence("あいう", "あういう"[:3])
    assert is_subsequence("", "なんでも")


# --- 読みの門 -------------------------------------------------------------

def test_interview_passes_a_homophone_fix():
    """苦闘店 → 句読点。読みが変わらないので通る（これが本命）。"""
    guard = _guard({"苦闘店です": "くとうてんです", "句読点です": "くとうてんです"})
    assert guard.check("苦闘店です", "句読点です", INTERVIEW).ok


def test_interview_passes_a_dakuten_fix():
    guard = _guard({"動音域語": "どうおんいきご", "同音異義語": "どうおんいぎご"})
    assert guard.check("動音域語", "同音異義語", INTERVIEW).ok


def test_interview_rejects_a_paraphrase():
    """読みやすくはなるが、話者はそう言っていない。"""
    guard = _guard({
        "もっと提供したい": "もっとていきょうしたい",
        "自信を持ってご提供したい": "じしんをもってごていきょうしたい",
    })
    verdict = guard.check("もっと提供したい", "自信を持ってご提供したい", INTERVIEW)
    assert not verdict.ok
    assert "reading-changed" in verdict.reasons


def test_interview_rejects_filler_removal():
    """取材モードでは、フィラーを消すことも「話したとおり」ではなくなる。"""
    guard = _guard({
        "えーと、そうですね": "えーとそうですね",
        "そうですね": "そうですね",
    })
    verdict = guard.check("えーと、そうですね", "そうですね", INTERVIEW)
    assert not verdict.ok
    assert "reading-changed" in verdict.reasons


def test_conversation_allows_deletion_but_not_creation():
    # 実運用のブロックと同じくらいの長さで見る（短文では割合の門が意味を持たない）。
    before = "えーと、そうですね、あの、今期の見通しについてはですね、慎重に見ております"
    kept = "そうですね、今期の見通しについては慎重に見ております"
    added = "そうですね、今期の見通しについては慎重に見ておりますが、来期は増収です"
    guard = _guard({before: before, kept: kept, added: added})
    # 消すのは通る。
    assert guard.check(before, kept, CONVERSATION).ok
    # 作るのは通らない。
    verdict = guard.check(before, added, CONVERSATION)
    assert not verdict.ok
    assert "reading-added" in verdict.reasons


def test_manuscript_lets_the_reading_change():
    """原稿執筆モードでは文体を整えてよいので、読みの門は開けない。"""
    guard = _guard({
        "もっと提供したいと思ってて": "もっとていきょうしたいとおもってて",
        "より広く提供したいと考えています": "よりひろくていきょうしたいとかんがえています",
    })
    assert guard.check(
        "もっと提供したいと思ってて",
        "より広く提供したいと考えています",
        MANUSCRIPT,
    ).ok


# --- 数値の門 -------------------------------------------------------------

def test_extract_numbers_folds_kanji_and_units():
    assert extract_numbers("二千二十六年") == [Decimal(2026)]
    assert extract_numbers("1,200億円") == [Decimal(120_000_000_000)]
    assert extract_numbers("二千年") == extract_numbers("2000年")
    assert extract_numbers("1.5兆円") == [Decimal("1.5") * Decimal(10**12)]
    assert extract_numbers("十人") == [Decimal(10)]
    assert extract_numbers("五十万人") == [Decimal(500_000)]
    assert extract_numbers("言葉だけ") == []


def test_numbers_may_disappear_but_never_appear():
    guard = _guard()
    # 「一つ」→「ひとつ」で数値が消えるのは事故ではない。
    assert guard.check("一つあります", "ひとつあります", MANUSCRIPT).ok
    # 値が変わったら、入力に無い数値が現れたのと同じこと。
    verdict = guard.check("1,200億円の増収", "1,300億円の増収", MANUSCRIPT)
    assert not verdict.ok
    assert "number-invented" in verdict.reasons


def test_number_notation_change_is_not_a_rejection():
    guard = _guard()
    assert guard.check("二千年に始まった", "2000年に始まった", MANUSCRIPT).ok


# --- 語の創作禁止 ---------------------------------------------------------

def test_invented_proper_noun_is_rejected():
    guard = _guard(
        readings={"会見の内容": "かいけんのないよう", "トヨタの会見の内容": "とよたのかいけんのないよう"},
        terms={"会見の内容": [], "トヨタの会見の内容": [("トヨタ", "とよた")]},
    )
    verdict = guard.check("会見の内容", "トヨタの会見の内容", MANUSCRIPT)
    assert not verdict.ok
    assert "invented-term" in verdict.reasons


def test_term_in_the_glossary_may_be_introduced():
    guard = _guard(
        readings={"スミシンの決算": "すみしんのけっさん", "住信の決算": "すみしんのけっさん"},
        terms={"スミシンの決算": [("スミシン", "すみしん")], "住信の決算": [("住信", "すみしん")]},
    )
    assert guard.check("スミシンの決算", "住信の決算", INTERVIEW, glossary=["住信"]).ok


def test_term_matching_the_source_reading_may_be_introduced():
    """用語集に無くても、読みが入力にあれば「聞こえた語の書き直し」として通す。"""
    guard = _guard(
        readings={"スミシンの決算": "すみしんのけっさん", "住信の決算": "すみしんのけっさん",
                  "住信": "すみしん"},
        terms={"スミシンの決算": [("スミシン", "すみしん")], "住信の決算": [("住信", "すみしん")]},
    )
    assert guard.check("スミシンの決算", "住信の決算", INTERVIEW).ok


# --- 欠落の門 -------------------------------------------------------------

def test_short_spans_skip_the_ratio_gate_but_not_the_empty_check():
    """短い範囲では割合の門を当てない。当てると正しい訂正まで落ちる。"""
    guard = _guard({"スミシンの決算": "すみしんのけっさん", "住信の決算": "すみしんのけっさん",
                    "住信": "すみしん"})
    assert guard.check("スミシンの決算", "住信の決算", MANUSCRIPT).ok
    # ただし空にするのは、長さに関わらず却下。
    verdict = guard.check("スミシンの決算", "", MANUSCRIPT)
    assert not verdict.ok
    assert "too-short" in verdict.reasons


def test_wholesale_truncation_is_rejected():
    before = "本日はお集まりいただきありがとうございます。" * 4
    guard = _guard({before: before, "本日は。": "ほんじつは"})
    verdict = guard.check(before, "本日は。", MANUSCRIPT)
    assert not verdict.ok
    assert "too-short" in verdict.reasons


def test_filler_removal_survives_the_coverage_gate_in_manuscript_mode():
    before = "えー、本日はですね、あの、決算についてご説明いたします。えーと、まず売上からです。"
    after = "本日は決算についてご説明いたします。まず売上からです。"
    guard = _guard({before: before, after: after})
    verdict = guard.check(before, after, MANUSCRIPT)
    assert verdict.ok, verdict.details


# --- 形態素解析が無いとき ---------------------------------------------------

def test_refuses_to_judge_without_morphology():
    guard = RefineGuard(UnavailableMorphology())
    verdict = guard.check("なんでも", "なんでも", INTERVIEW)
    assert not verdict.ok
    assert verdict.reasons == ("morphology-unavailable",)


# --- 本物の Sudachi で通す（入っていなければ飛ばす） -------------------------
#
# 偽物では見つからない食い違いがここで出る。実際、数詞を読みに含めていた頃は
# 「二千億円 → 2000億円」が reading-changed で落ちていた（Sudachi は 2000 を
# 「にれいれいれい」、二千を「にせん」と読むため）。

def test_real_sudachi_end_to_end():
    from refine_guard import SudachiMorphology

    morph = SudachiMorphology()
    if not morph.available:
        print("     （Sudachi が無いので飛ばす）")
        return
    guard = RefineGuard(morph)

    passes = [
        ("会議で苦闘店の使い方を説明しました。", "会議で句読点の使い方を説明しました。", INTERVIEW),
        ("今期は二千億円の増収です。", "今期は2000億円の増収です。", INTERVIEW),
        ("スミシンの決算を確認しました。", "住信の決算を確認しました。", INTERVIEW),
    ]
    for before, after, profile in passes:
        verdict = guard.check(before, after, profile)
        assert verdict.ok, (before, after, verdict.reasons, verdict.details)

    rejects = [
        ("会議で苦闘店の使い方を説明しました。",
         "会議にて句読点の用法をご説明いたしました。", INTERVIEW, "reading-changed"),
        ("今期は1,200億円の増収です。", "今期は1,300億円の増収です。",
         INTERVIEW, "number-invented"),
        ("会見の内容をまとめました。", "トヨタの会見の内容をまとめました。",
         MANUSCRIPT, "invented-term"),
    ]
    for before, after, profile, reason in rejects:
        verdict = guard.check(before, after, profile)
        assert not verdict.ok, (before, after)
        assert reason in verdict.reasons, (before, verdict.reasons)


# --- 集計 -----------------------------------------------------------------

def test_stats_counts_reasons():
    guard = _guard({"あ": "あ", "い": "い"})
    stats = GuardStats()
    stats.add(guard.check("あ", "あ", INTERVIEW))
    stats.add(guard.check("あ", "い", INTERVIEW))
    assert stats.total == 2
    assert stats.passed == 1
    assert stats.rejected == 1
    assert stats.reasons["reading-changed"] == 1


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
