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


def test_symbol_standalone_variants():
    # 単独チャンクで言った記号（変種・Whisperの漢字化/句点付きも吸収）。
    assert postprocess("まる", symbol_dictation=True) == "。"
    assert postprocess("丸。", symbol_dictation=True) == "。"
    assert postprocess("てん", symbol_dictation=True) == "、"
    assert postprocess("改行", symbol_dictation=True) == "\n"


def test_symbol_trailing_kanji():
    # 文末が漢字「丸」でも句点化する。
    assert postprocess("今日は晴れです丸", symbol_dictation=True) == "今日は晴れです。"


def test_symbol_no_false_positive_midword():
    # 本文中の同綴りは壊さない（「困る」「かっこいい」）。
    assert postprocess("それは困る", symbol_dictation=True) == "それは困る"
    assert postprocess("それはかっこいい", symbol_dictation=True) == "それはかっこいい"


def test_symbol_standalone_homophones():
    # 単独で言った記号語をWhisperが同音異義漢字にしても拾う（実データより）。
    assert postprocess("開業", symbol_dictation=True) == "\n"   # かいぎょう
    assert postprocess("会場", symbol_dictation=True) == "\n"   # かいぎょう
    assert postprocess("回教", symbol_dictation=True) == "\n"   # かいぎょう
    assert postprocess("点", symbol_dictation=True) == "、"     # てん
    assert postprocess("丸", symbol_dictation=True) == "。"     # まる
    assert postprocess("終わる", symbol_dictation=True) == "。"  # まる


def test_symbol_homophone_not_standalone_kept():
    # 単独でなければ同音異義語は本物の語として温存（誤爆しない）。
    assert postprocess("会場は広い", symbol_dictation=True) == "会場は広い"
    assert postprocess("要点をまとめる", symbol_dictation=True) == "要点をまとめる"
    # 文中に埋もれた「点」は安全のため変換しない（本物の点を壊さないため）。
    assert postprocess("公表し、点、広く", symbol_dictation=True) == "公表し、点、広く"


def test_user_symbols_standalone():
    # ユーザー登録の記号語（単独チャンク）を記号化。改行別名も解釈。
    syms = {"当点": "、", "海業": "\n"}
    assert postprocess("当点", symbol_dictation=True, symbols=syms) == "、"
    assert postprocess("海業", symbol_dictation=True, symbols=syms) == "\n"
    # 単独でなければ温存（本文中の同綴りを壊さない）。
    assert postprocess("当点について", symbol_dictation=True, symbols=syms) == "当点について"


def test_user_dict_english():
    reps = [("ウィンドウズ", "Windows"), ("アンドロイド", "Android")]
    out = postprocess("ウィンドウズとアンドロイドを使う", replacements=reps, strip_space=True)
    assert out == "WindowsとAndroidを使う"


def test_fullwidth_normalized_then_dict():
    # Whisperが「Ａトック」(全角A) と出しても、半角化して辞書「Aトック」で拾える。
    reps = [("Aトック", "ATOK")]
    out = postprocess("Ａトックを使う", replacements=reps, strip_space=True)
    assert out == "ATOKを使う"


def test_collapse_duplicate_punctuation():
    out = postprocess("はい。。そうです", strip_space=True, symbol_dictation=False)
    assert out == "はい。そうです"


def test_empty():
    assert postprocess("") == ""


def test_userdict_hotwords_and_reverse():
    from userdict import _build_hotwords, _reverse_items_from
    items = [("じょうぷらほう", "情プラ法"), ("ウィンドウズ", "Windows")]
    hotwords = _build_hotwords(["Obsidian"], items)
    assert "Obsidian" in hotwords
    assert "情プラ法" in hotwords
    assert "Windows" in hotwords

    rev = _reverse_items_from(items)
    assert rev[0] == ("情プラ法", "じょうぷらほう") or rev[0] == ("Windows", "ウィンドウズ")


def test_reconvert_reverse_userdict():
    from reconvert import _reading
    hira = _reading.to_hiragana("情プラ法を使う")
    assert "じょうぷらほう" in hira


def test_strip_trailing_hallucinations():
    from postproc import postprocess
    out1 = postprocess("予定されているところでございますありがとうございます", strip_space=True)
    assert out1 == "予定されているところでございます"

    out2 = postprocess("以上です。ありがとうございました。", strip_space=True)
    assert out2 == "以上です。"


def _run_all():
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
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
