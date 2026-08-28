"""ベンチの配線のテスト。

モデルも Sudachi も立てずに、ブロック分割・集計・書き出しが正しいことを見る。
測りたいのはモデルの性能であって、ベンチ自身のバグではないため。
"""
from bench_refine import BenchReport, BlockOutcome, _edit_size, _render_diff, run, split_blocks
from refine_guard import INTERVIEW, MANUSCRIPT, RefineGuard
from refine_llm import RefineResult, StubClient
from test_refine_guard import FakeMorphology


# --- ブロック分割 ---------------------------------------------------------

def test_splits_at_sentence_ends_not_mid_word():
    text = "".join(f"これは{i}番目の文です。" for i in range(10))
    blocks = split_blocks(text, block_chars=40)
    assert len(blocks) > 1
    # どのブロックも文末で終わる＝語の途中で割っていない。
    assert all(b.endswith("。") for b in blocks), blocks
    # 落としも重複もしない。
    assert "".join(blocks) == text


def test_paragraphs_are_never_merged():
    text = "前の段落です。\n\n後の段落です。"
    assert split_blocks(text, block_chars=1000) == ["前の段落です。", "後の段落です。"]


def test_a_single_long_sentence_stays_whole():
    """句点が無い長文を、途中で割らない（割ると同音異義が直せなくなる）。"""
    text = "あ" * 900
    assert split_blocks(text, block_chars=400) == [text]


# --- 変更量 ---------------------------------------------------------------

def test_edit_size_is_zero_when_nothing_changed():
    assert _edit_size("同じ文です", "同じ文です") == 0


def test_edit_size_counts_only_the_changed_middle():
    assert _edit_size("これは苦闘店です", "これは句読点です") == 3


# --- 通し実行 -------------------------------------------------------------

class ScriptedClient:
    """ブロックごとに返す文字列を決め打ちする偽モデル。"""

    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)

    def refine(self, profile, text, glossary=()):
        return RefineResult(self._replies.pop(0), 0.5)


class FailingClient:
    name = "failing"

    def refine(self, profile, text, glossary=()):
        return RefineResult("", 0.1, "接続できない")


def test_rejected_blocks_keep_the_raw_text():
    """却下されたら生テキストが残る。黙って捨てない、黙って通さない。"""
    blocks = ["苦闘店です", "会見の内容"]
    client = ScriptedClient(["句読点です", "トヨタの会見の内容"])
    guard = RefineGuard(FakeMorphology(
        readings={"苦闘店です": "くとうてんです", "句読点です": "くとうてんです",
                  "会見の内容": "かいけんのないよう",
                  "トヨタの会見の内容": "とよたのかいけんのないよう"},
        terms={"会見の内容": [], "トヨタの会見の内容": [("トヨタ", "とよた")]},
    ))
    report = run(blocks, client, INTERVIEW, guard, glossary=())

    assert report.stats.total == 2
    assert report.stats.passed == 1
    assert report.outcomes[0].text == "句読点です"      # 採用
    assert report.outcomes[1].text == "会見の内容"      # 却下 → 生が残る
    assert "invented-term" in report.stats.reasons


def test_a_model_that_changes_nothing_looks_safe_but_is_not_useful():
    """通過率だけ見ると満点。だから直した量を必ず並べて出す。"""
    blocks = ["何も直らない文です。", "これも直らない文です。"]
    guard = RefineGuard(FakeMorphology())
    report = run(blocks, StubClient(), MANUSCRIPT, guard, glossary=())
    assert report.stats.pass_rate == 1.0
    assert report.changed == 0
    assert report.touched_blocks == 0


def test_llm_errors_are_counted_and_do_not_reach_the_guard():
    guard = RefineGuard(FakeMorphology())
    report = run(["本文"], FailingClient(), MANUSCRIPT, guard, glossary=())
    assert report.errors == 1
    assert report.stats.total == 0          # 門は呼ばれない
    assert report.outcomes[0].text == "本文"  # 生が残る
    assert report.seconds == []             # 失敗は所要時間に混ぜない


# --- 書き出し -------------------------------------------------------------

def test_diff_lists_changed_and_rejected_blocks_only():
    report = BenchReport(model="m", mode="interview")
    report.add(BlockOutcome("同じ", "同じ", 0.1, True, (), ()))
    report.add(BlockOutcome("苦闘店", "句読点", 0.1, True, (), ()))
    report.add(BlockOutcome("1,200億円", "1,300億円", 0.1, False,
                            ("number-invented",), ("入力に無い数値: 130000000000",)))
    out = _render_diff(report)
    assert "ブロック 1" not in out          # 変わっていないブロックは載せない
    assert "ブロック 2" in out
    assert "ブロック 3（却下: number-invented）" in out
    assert "- 数値:" in out                 # 数値の変化は目で追えるように出す


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
