"""辞書候補の絞り込み（門1・門2）のテスト。

ここが緩むと、正しい語を潰す登録が候補の上位に出る。実際にやりかけた事故
（要約の略称「マイナカード」を正解にして本文29箇所を潰す）を固定しておく。
sudachi を使うテストは未導入環境では自動的に飛ばす。
"""
from collections import Counter

from dictcandidates import (
    Candidate,
    apply_verdicts,
    _is_ambiguous,
    _split_for_tokenizer,
    find_candidates,
    is_term_like,
    looks_like_abbreviation,
    ngrams,
    sound_ratio,
    split_note,
)


def _sudachi_ready() -> bool:
    from punctuate import available

    return available()


# --- 門1: 省略の検出 --------------------------------------------------------

def test_abbreviation_is_detected():
    # 要約でよく使う略し方。登録すると本文の正しい表記を全部潰す。
    assert looks_like_abbreviation("マイナンバーカード", "マイナカード")
    assert looks_like_abbreviation("デジタル認証アプリ", "認証アプリ")


def test_real_misrecognition_is_not_abbreviation():
    # 本物の誤認識は長さが同じか伸びるので、省略として落としてはいけない。
    assert not looks_like_abbreviation("パスピー", "パスキー")
    assert not looks_like_abbreviation("マイナンバーパード", "マイナンバーカード")
    assert not looks_like_abbreviation("アジアティックコーナース", "エージェンティックコマース")
    # 短くても部分列でなければ省略ではない（言い換え）。
    assert not looks_like_abbreviation("愛称番号", "PIN")


def test_inserted_characters_are_not_abbreviation():
    """認識が1〜2文字を足す壊し方を省略と呼ばないこと。

    部分列という条件だけだと、これらは全部「省略」に見える。実測（デジ庁会見の
    候補をLLMに判定させたとき）、採用すべき4件がこれで捨てられていた。
    """
    assert not looks_like_abbreviation("マイナーアプリ", "マイナアプリ")
    assert not looks_like_abbreviation("マイナップポータル", "マイナポータル")
    assert not looks_like_abbreviation("マイナルアプリ", "マイナアプリ")
    assert not looks_like_abbreviation("マイナーポータル", "マイナポータル")


def test_abbreviation_matches_plugin_rule():
    """plugin/dictpreview.ts の looksLikeAbbreviation と同じ答えになること。

    片方だけ直すと「CLIは候補に出すのにUIは警告する」というねじれになる。
    """
    cases = [
        ("マイナンバーカード", "マイナカード", True),
        ("パスピー", "パスキー", False),
        ("マイナーアプリ", "マイナアプリ", False),
        ("あいう", "かきく", False),
        ("あいう", "", False),
    ]
    for observed, output, expected in cases:
        assert looks_like_abbreviation(observed, output) is expected, (observed, output)


# --- 門2: 音の距離 ----------------------------------------------------------

def test_paraphrase_is_far_in_sound():
    # 言い換えは音がまったく違う。ここだけは音で切れる。
    assert sound_ratio("アイショウバンゴウ", "ピン") < 0.4


def test_sound_alone_cannot_separate_abbreviation():
    """音の近さだけでは省略と誤認識を分離できない、という前提の固定。

    この関係が崩れたら（省略のほうが低くなったら）門1を音に置き換えられるので、
    そのときはこのテストが落ちて設計を見直す合図になる。
    """
    abbreviation = sound_ratio("マイナンバーカード", "マイナカード")
    real_error = sound_ratio("アジアティックコーナース", "エージェンティックコマース")
    assert abbreviation > real_error


# --- 語の見た目 -------------------------------------------------------------

def test_term_like_rejects_numbers_and_short_words():
    assert is_term_like("デジタル認証", "デジタルニンショウ")
    assert is_term_like("Connect", "コネクト")
    # 数を含む語は直しようがない（「300以上→600以上」が候補上位を占めていた）。
    assert not is_term_like("300以上", "サンビャクイジョウ")
    assert not is_term_like("あ", "ア")


# --- ノートの切り分け -------------------------------------------------------

def test_split_note_uses_anchor_mark():
    note = "---\ntitle: x\n---\n文字起こしの本文です。\n%%wx 00:00%%上仮屋氏\n手書きの要約。"
    transcript, correct = split_note(note)
    assert "文字起こしの本文" in transcript
    assert "手書きの要約" in correct
    assert "手書きの要約" not in transcript


def test_split_note_without_mark_has_no_correct_side():
    transcript, correct = split_note("本文だけのノート。")
    assert transcript == "本文だけのノート。"
    assert correct == ""


# --- 長文の刻み -------------------------------------------------------------

def test_long_text_is_split_for_tokenizer():
    """sudachi の入力上限で黙って0件にならないこと（実装中に踏んだ）。"""
    text = ("これはとても長い本文です。" * 4000)
    pieces = _split_for_tokenizer(text, limit=20000)
    assert len(pieces) > 1
    # 位置がずれない＝断片を順に繋ぐと元に戻る。
    assert "".join(p for p, _at in pieces) == text
    for piece, at in pieces:
        assert text[at:at + len(piece)] == piece
        assert len(piece.encode("utf-8")) <= 20000 + 12


# --- 競合の印 ---------------------------------------------------------------

def test_ambiguous_ignores_length_variants():
    # 長さ違いは同じ語なので警告しない。全部に印が付くと印の意味が消える。
    assert not _is_ambiguous("マイナポータル", {"マイナポータルアプリ"})
    # 別の語が競合しているときだけ警告する。
    assert _is_ambiguous("マイナカード", {"マイナンバー"})


# --- 通し ------------------------------------------------------------------

def test_finds_real_error_and_rejects_abbreviation():
    if not _sudachi_ready():
        print("     （sudachi 未導入のためスキップ）")
        return
    transcript = (
        "マイナンバーカードを使ってログインします。"
        "スマホ搭載のマイナンバーカードも同じです。"
        "最近パスピーが流行っていますので便利です。"
    )
    correct = "マイナカードとパスキーの話。マイナンバーカードの利用。"
    cands = find_candidates(transcript, correct, min_ratio=0.7)
    pairs = {(c.observed, c.output) for c in cands}
    # 欲しいもの: 本物の誤認識。
    assert ("パスピー", "パスキー") in pairs, pairs
    # 出してはいけないもの: 正解側にも書かれている綴りを潰す登録。
    assert not any(o == "マイナンバーカード" for o, _ in pairs), pairs


def test_known_good_blocks_candidate_without_summary_evidence():
    """要約に無くても、辞書が正しいと決めた綴りは誤認識として出さない。"""
    if not _sudachi_ready():
        print("     （sudachi 未導入のためスキップ）")
        return
    transcript = "デジタル庁の発表です。デジタル庁が進めます。"
    correct = "デジタル認証の話。"
    loose = find_candidates(transcript, correct, min_ratio=0.7)
    assert any(c.observed == "デジタル庁" for c in loose)
    guarded = find_candidates(
        transcript, correct, min_ratio=0.7, known_good=frozenset({"デジタル庁"})
    )
    assert not any(c.observed == "デジタル庁" for c in guarded)


def test_known_pairs_are_not_suggested_again():
    if not _sudachi_ready():
        print("     （sudachi 未導入のためスキップ）")
        return
    transcript = "最近パスピーが流行っていますので便利です。"
    correct = "パスキーの話。"
    again = find_candidates(
        transcript, correct, min_ratio=0.7,
        known_pairs=frozenset({("パスピー", "パスキー")}),
    )
    assert not any(c.observed == "パスピー" for c in again)


def test_ngrams_do_not_cross_particles():
    """助詞を巻き込んだキーを作らないこと（登録すると誤爆する）。"""
    if not _sudachi_ready():
        print("     （sudachi 未導入のためスキップ）")
        return
    surfaces = {s for s, _r, _at in ngrams("今回のマイナアプリという形になります")}
    assert not any(s.startswith("の") or s.endswith("という") for s in surfaces), surfaces


# --- LLMの答えの検証 --------------------------------------------------------

def _verdict(observed: str, output: str, verdict: str = "採用") -> str:
    import json

    return json.dumps(
        [{"observed": observed, "output": output, "verdict": verdict, "reason": "test"}],
        ensure_ascii=False,
    )


def test_verdict_accepts_real_error():
    accepted, rejected = apply_verdicts(
        _verdict("パスピー", "パスキー"),
        transcript="最近パスピーが流行っています。",
        allowed_text="パスキーの話。",
    )
    assert accepted == [("パスピー", "パスキー")], (accepted, rejected)


def test_verdict_rejects_invented_spelling():
    """**一番危ない失敗**: LLMが正解側に無い綴りを作ってくる。

    プロンプトで禁じるだけでは足りないので、機械で確かめて落とす。
    """
    accepted, rejected = apply_verdicts(
        _verdict("パスピー", "パスキー認証"),
        transcript="最近パスピーが流行っています。",
        allowed_text="パスキーの話。",
    )
    assert accepted == []
    assert any("正解側に無い" in r for r in rejected), rejected


def test_verdict_rejects_spelling_absent_from_transcript():
    accepted, rejected = apply_verdicts(
        _verdict("存在しない語", "パスキー"),
        transcript="最近パスピーが流行っています。",
        allowed_text="パスキーの話。存在しない語。",
    )
    assert accepted == []
    assert any("本文に存在しない" in r for r in rejected), rejected


def test_verdict_rejects_abbreviation_even_if_llm_accepts():
    accepted, rejected = apply_verdicts(
        _verdict("マイナンバーカード", "マイナカード"),
        transcript="マイナンバーカードを使います。",
        allowed_text="マイナカードの話。",
    )
    assert accepted == []
    assert any("省略" in r for r in rejected), rejected


def test_verdict_skips_rejected_items_silently():
    accepted, rejected = apply_verdicts(
        _verdict("パスピー", "パスキー", verdict="却下"),
        transcript="最近パスピーが流行っています。",
        allowed_text="パスキーの話。",
    )
    assert accepted == []
    assert rejected == []      # LLMが自分で却下したものは検証の失敗ではない


def test_verdict_handles_broken_json():
    accepted, rejected = apply_verdicts(
        "これはJSONではありません", transcript="", allowed_text=""
    )
    assert accepted == []
    assert rejected and "JSON" in rejected[0]


def test_score_prefers_frequent_terms():
    a = Candidate("誤A", "正解語", hits=10, ratio=0.8, contexts=())
    b = Candidate("誤B", "正解語", hits=2, ratio=0.9, contexts=())
    assert a.score > b.score


def test_extra_terms_can_come_from_materials():
    if not _sudachi_ready():
        print("     （sudachi 未導入のためスキップ）")
        return
    transcript = "デジタル認識サービスを使います。デジタル認識サービスの話。"
    cands = find_candidates(
        transcript, "", extra_terms=Counter({"デジタル認証サービス": 1}), min_ratio=0.7
    )
    assert ("デジタル認識サービス", "デジタル認証サービス") in {
        (c.observed, c.output) for c in cands
    }


if __name__ == "__main__":
    import sys

    mod = sys.modules[__name__]
    failed = 0
    for name in [n for n in dir(mod) if n.startswith("test_")]:
        try:
            getattr(mod, name)()
            print(f"ok   {name}")
        except AssertionError:
            failed += 1
            print(f"FAIL {name}")
    print("失敗あり" if failed else "すべて成功")
    sys.exit(1 if failed else 0)
