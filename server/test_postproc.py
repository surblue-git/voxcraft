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


def test_symbol_kagikakko_observed_spellings():
    # 実測 2026-08-05:「かぎかっこ」→『鍵かっこ』、「かぎかっことじ」→『カギカッコトジ』。
    # 単独で言えているのに記号にならず、読点付きで本文に落ちていた。
    assert postprocess("鍵かっこ", symbol_dictation=True) == "「"
    assert postprocess("カギカッコトジ", symbol_dictation=True) == "」"
    assert postprocess("鉤括弧", symbol_dictation=True) == "「"
    assert postprocess("鍵括弧閉じ", symbol_dictation=True) == "」"
    # 本物の語としての「鍵」は対象外（単独一致でも踏み込まない）。
    assert postprocess("鍵", symbol_dictation=True) == "鍵"
    assert postprocess("鍵をかけた", symbol_dictation=True) == "鍵をかけた"


def test_inline_bracket_words():
    # sudachipy 未導入環境では no-op（依存なしで実行可を保つためスキップ扱い）。
    from punctuate import available
    if not available():
        return

    def run(text):
        return postprocess(text, symbol_dictation=True, inline_symbols=True)

    # 実測 2026-08-05: 括弧は文中で言うので単独チャンクにならず、全体一致では
    # 一度も拾えなかった。以下はそのとき実際に本文へ落ちた文字列。
    assert run("このカギカッコ新バージョンは") == "この「新バージョンは"
    assert run("鍵カッコを入力はしてくれない") == "「を入力はしてくれない"
    assert run("いつも鍵かっこ閉じ、ダメですね") == "いつも」、ダメですね"
    assert run("ではかっこ、") == "では（、"
    # sudachi は連続するカタカナを1語にまとめるので、内側からも切り出す。
    assert run("ではこのバージョンカッコトジを試しましょう") == "ではこのバージョン）を試しましょう"
    # 「とじ」の濁点が落ちた実例。
    assert run("鍵かっことし") == "」"

    # 同音の本物の語は壊さない。読みが違うもの（カッコウ/カッコイイ）は元から
    # 当たらず、読みが同じもの（括弧/確固）は漢字表記なので対象外。
    for keep in (
        "それはかっこいい",
        "格好が悪い",
        "括弧を使う",
        "確固たる意志",
        "鍵をかけた",
        "カッコウが鳴いている",
        "バージョンアップした",
    ):
        assert run(keep) == keep, keep

    # カタカナが1語に固まった中は形態素の切れ目が無いので、音で語末を判断する。
    # 実測: 「かぎかっこ・かっこうの許嫁」が『カギカッコー…』『カギカッコウ…』と
    # 1語になり、カギカッコ を取ると本文の「カッコー」を食っていた。
    assert run("カギカッコーの言い名付け") == "カギカッコーの言い名付け"
    assert run("カギカッコウノイイナヅケ") == "カギカッコウノイイナヅケ"
    # 続きが別語なら従来どおり変換する（食う心配が無いため）。
    assert run("カギカッコカッコーノイイナヅケ") == "「カッコーノイイナヅケ"
    assert run("バージョンカギカッコトジ") == "バージョン」"

    # 口述以外では一切動かさない（既定は無効）。
    assert postprocess("このカギカッコ新バージョンは", symbol_dictation=True) == \
        "このカギカッコ新バージョンは"


def test_symbol_fullwidth_output_survives_ascii_normalization():
    # 記号読み上げが返す全角記号は U+FF01〜FF5E に入るものがあり、全角→半角の
    # 正規化を後ろに置くと「かっこ」と言ったのに ( になる（正規化は記号化より前）。
    assert postprocess("かっこ", symbol_dictation=True) == "（"
    assert postprocess("かっことじ", symbol_dictation=True) == "）"
    assert postprocess("びっくりまーく", symbol_dictation=True) == "！"
    assert postprocess("ころん", symbol_dictation=True) == "："
    # ユーザー登録の記号語でも同じ（全角で登録したら全角のまま出る）。
    assert postprocess("全角はてな", symbol_dictation=True, symbols={"全角はてな": "？"}) == "？"
    # 半角化そのものは辞書引きのために効いたまま。
    assert postprocess("Ａトック", replacements=[("Aトック", "ATOK")]) == "ATOK"


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
    # 句読点なしで癒着した定型句は幻覚とみなして除去する。
    out1 = postprocess("予定されているところでございますありがとうございます", strip_space=True)
    assert out1 == "予定されているところでございます"

    # 句読点で正しく区切られた締めの挨拶は本物の発話なので残す（安全側）。
    out2 = postprocess("以上です。ありがとうございました。", strip_space=True)
    assert out2 == "以上です。ありがとうございました。"


def test_auto_punctuation():
    # sudachipy 未導入環境では no-op（依存なしで実行可を保つためスキップ扱い）。
    from punctuate import available, add_punctuation
    if not available():
        print("SKIP test_auto_punctuation (sudachipy 未導入)")
        return
    out = add_punctuation("これはテストです明日会議があります")
    assert out == "これはテストです。明日会議があります。", out
    # 文中の連体「た」には打たない。
    assert add_punctuation("食べた人がいる") == "食べた人がいる。"
    # 接続助詞は読点。
    assert add_punctuation("行きますが時間がない") == "行きますが、時間がない。"
    # postprocess 経由でも動く。
    from postproc import postprocess
    assert postprocess("これはテストです明日会議があります",
                       auto_punctuate=True) == "これはテストです。明日会議があります。"


def test_vad_carries_tail_instead_of_discarding():
    """VADが無音とみなしたチャンク末尾も次へ繰り越す（発話を取りこぼさない）。"""
    try:
        import numpy as np
    except ImportError:
        print("SKIP test_vad_carries_tail_instead_of_discarding (numpy 未導入)")
        return
    from vad import VadChunker, _EnergyDetector

    sr = 16000
    ch = VadChunker(sample_rate=sr, silence_sec=0.5, max_chunk_sec=12.0,
                    min_speech_sec=0.3, vad_threshold=0.5, speech_pad_sec=0.2)
    ch._detector = _EnergyDetector(sr, 0.5)  # 判定を決定的にする

    loud = np.full(sr, 0.3, dtype=np.float32)             # 1秒の発話
    quiet = np.full(int(sr * 0.6), 0.001, dtype=np.float32)  # 0.6秒の無音
    blocks = (loud, quiet, loud)
    pushed = sum(len(b) for b in blocks)

    emitted = 0
    for block in blocks:
        for c in ch.push(block):
            emitted += len(c.audio)
    tail = ch.flush()
    if tail is not None:
        emitted += len(tail.audio)

    # 繰り越さない実装では末尾フレーム分が失われる。
    assert emitted == pushed, (emitted, pushed)


def test_dict_api_validation():
    """UIからの辞書保存は、文字列マップ・件数・長さ・文字コードを検証する。"""
    from userdict import (
        DictValidationError, MAX_ENTRIES, MAX_KEY_LEN, MAX_VALUE_LEN, _validate_map,
    )

    bad = [
        ("非文字列", {"a": 123}),
        ("マップでない", ["x"]),
        ("キーが長い", {"x" * (MAX_KEY_LEN + 1): "y"}),
        ("値が長い", {"x": "y" * (MAX_VALUE_LEN + 1)}),
        ("件数超過", {str(i): "v" for i in range(MAX_ENTRIES + 1)}),
        ("保存不能な文字", {"\ud881": "y"}),
    ]
    for label, obj in bad:
        try:
            _validate_map(obj, "replacements")
        except DictValidationError:
            continue
        raise AssertionError(f"{label} が拒否されなかった")

    # 正常系: 空キーは捨て、値はそのまま通る。
    ok = _validate_map({"収集説明": "趣旨説明", "  ": "x", " 協裁 ": "共催"}, "replacements")
    assert ok == {"収集説明": "趣旨説明", "協裁": "共催"}, ok


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
