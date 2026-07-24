"""postproc の単体テスト（依存なしで実行可能）。

    python -m pytest test_postproc.py    # pytest があれば
    python test_postproc.py              # なくても直接実行できる
"""
from postproc import postprocess, strip_ja_alnum_space


def test_strip_space_between_ja_and_alnum():
    assert strip_ja_alnum_space("今日は Python を書く") == "今日はPythonを書く"
    assert strip_ja_alnum_space("バージョン 3 です") == "バージョン3です"
    assert strip_ja_alnum_space("ID は 42 だ") == "IDは42だ"


def test_keep_space_between_alnum_words():
    # 英単語同士の空白は残す。
    assert strip_ja_alnum_space("New York に行く") == "New Yorkに行く"
    assert strip_ja_alnum_space("run all tests") == "run all tests"


def test_symbol_dictation():
    out = postprocess("これはテストです まる", strip_space=True, symbol_dictation=True)
    assert out == "これはテストです。"


def test_symbol_dictation_off_keeps_reading():
    out = postprocess("これはテストです まる", strip_space=True, symbol_dictation=False)
    assert "まる" in out


def test_collapse_duplicate_punctuation():
    out = postprocess("はい。。そうです", strip_space=True, symbol_dictation=False)
    assert out == "はい。そうです"


def test_empty():
    assert postprocess("") == ""


def _run_all():
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
