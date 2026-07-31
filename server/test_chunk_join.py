"""短いチャンクの連結（vad.ChunkJoiner）と定型句ブロック（asr.is_boilerplate）のテスト。

    python -m pytest test_chunk_join.py    # pytest があれば
    python test_chunk_join.py              # なくても直接実行できる

モデルのロードは不要（is_boilerplate は純粋な文字列判定）。
"""
import numpy as np

from asr import is_boilerplate
from postproc import ParagraphBreaker
from vad import Chunk, ChunkJoiner

SR = 16000


def chunk(start_sec: float, dur_sec: float, reason: str = "silence", pause=None) -> Chunk:
    n = int(dur_sec * SR)
    return Chunk(
        audio=np.zeros(n, dtype=np.float32),
        reason=reason,
        start=int(start_sec * SR),
        end=int(start_sec * SR) + n,
        pause=pause,
    )


# --- 連結 -------------------------------------------------------------------

def test_long_chunk_passes_through():
    j = ChunkJoiner(SR, min_sec=4.0)
    out = j.push(chunk(0, 5.0), now=0.0)
    assert len(out) == 1
    assert out[0].end == 5 * SR


def test_short_chunks_are_joined_until_min_sec():
    j = ChunkJoiner(SR, min_sec=4.0)
    assert j.push(chunk(0.0, 1.0), now=0.0) == []
    assert j.push(chunk(1.0, 1.0), now=0.1) == []
    assert j.push(chunk(2.0, 1.0), now=0.2) == []
    out = j.push(chunk(3.0, 1.5), now=0.3)
    assert len(out) == 1
    merged = out[0]
    # 位置は最初のチャンクの先頭から最後のチャンクの末尾まで。
    assert (merged.start, merged.end) == (0, int(4.5 * SR))
    # 音声も欠けていない（span と長さが一致する ＝ 復旧に使える）。
    assert merged.audio.size == merged.end - merged.start


def test_pause_is_kept_from_the_first_chunk():
    # 息継ぎ読点は「連結したかたまりの前の無音」で判断する。
    j = ChunkJoiner(SR, min_sec=4.0)
    j.push(chunk(0.0, 1.0, pause=1.2), now=0.0)
    out = j.push(chunk(1.0, 4.0, pause=0.1), now=0.1)
    assert out[0].pause == 1.2


def test_gap_is_not_bridged():
    # 間に捨てられた区間があるときに繋ぐと、テキストと音声の対応がずれる。
    j = ChunkJoiner(SR, min_sec=4.0)
    assert j.push(chunk(0.0, 1.0), now=0.0) == []
    out = j.push(chunk(9.0, 1.0), now=0.1)  # 連続していない
    assert len(out) == 1
    assert out[0].end == 1 * SR           # 溜めていた分だけが出る
    assert j.flush()[0].start == 9 * SR   # 新しい方は溜めに入っている


def test_hold_timeout_emits_short_chunk():
    j = ChunkJoiner(SR, min_sec=4.0, max_hold_sec=2.0)
    assert j.push(chunk(0.0, 0.5), now=100.0) == []
    assert j.tick(now=101.0) == []          # まだ待つ
    out = j.tick(now=102.5)                 # 時間切れ
    assert len(out) == 1 and out[0].audio.size == int(0.5 * SR)


def test_hold_timer_does_not_reset_on_merge():
    # 短いチャンクが続いても、待ち始めからの時間で打ち切る（無限に待たない）。
    j = ChunkJoiner(SR, min_sec=10.0, max_hold_sec=2.0)
    j.push(chunk(0.0, 0.5), now=100.0)
    j.push(chunk(0.5, 0.5), now=101.0)
    j.push(chunk(1.0, 0.5), now=101.9)
    out = j.tick(now=102.1)
    assert len(out) == 1 and out[0].audio.size == int(1.5 * SR)


def test_long_pause_is_not_merged_across():
    # 話の切れ目で繋ぐと、段落分けの材料（その息継ぎ）が内側に埋もれてしまう。
    j = ChunkJoiner(SR, min_sec=4.0, break_sec=2.0)
    assert j.push(chunk(0.0, 1.0), now=0.0) == []
    out = j.push(chunk(1.0, 1.0, pause=3.0), now=0.1)
    assert len(out) == 1 and out[0].end == 1 * SR    # 溜め分が先に出る
    assert j.flush()[0].pause == 3.0                 # 長い息継ぎは残る


def test_short_pause_is_merged():
    j = ChunkJoiner(SR, min_sec=4.0, break_sec=2.0)
    j.push(chunk(0.0, 1.0), now=0.0)
    out = j.push(chunk(1.0, 4.0, pause=0.8), now=0.1)
    assert len(out) == 1 and out[0].audio.size == int(5.0 * SR)


def test_flush_emits_pending():
    j = ChunkJoiner(SR, min_sec=4.0)
    j.push(chunk(0.0, 1.0), now=0.0)
    assert len(j.flush()) == 1
    assert j.flush() == []


# --- 定型句ブロック ---------------------------------------------------------

def test_boilerplate_whole_chunk():
    # 実測（VAIO発表会）で出た形をそのまま。
    assert is_boilerplate("ご視聴ありがとうございました")
    assert is_boilerplate("ご視聴ありがとうございました。")
    assert is_boilerplate("では、ご視聴ありがとうございました。")
    assert is_boilerplate("それでは、ご視聴ありがとうございました。")
    assert is_boilerplate("最後までご視聴いただきありがとうございます")
    assert is_boilerplate("チャンネル登録をお願いいたします。")
    assert is_boilerplate("次回はお楽しみに")
    assert is_boilerplate("お楽しみに")


def test_real_speech_is_kept():
    # 本文に混ざった場合は消さない（丸ごと一致に限る）。
    assert not is_boilerplate("信頼性フェクターをご視聴ください。")
    assert not is_boilerplate("ですご視聴ありがとうございました")
    # 取材で本当に言う言葉は残す。
    assert not is_boilerplate("ご清聴ありがとうございました")
    assert not is_boilerplate("ありがとうございました")
    assert not is_boilerplate("皆さんにお越しいただき、誠にありがとうございます。")
    assert not is_boilerplate("")


# --- 段落分け -----------------------------------------------------------------

def test_no_break_at_the_beginning():
    b = ParagraphBreaker(min_chars=120, pause_sec=0.7, max_chars=400)
    assert b.feed("あ" * 200, 5.0) == ""   # 1つ目のチャンクの前には入れない


def test_break_needs_both_chars_and_pause():
    b = ParagraphBreaker(min_chars=120, pause_sec=0.7, max_chars=400)
    b.feed("あ" * 129 + "。", None)
    assert b.feed("い" * 49 + "。", 0.3) == ""      # 字数は足りるが息継ぎが短い
    assert b.feed("う" * 50, 1.0) == "\n\n"        # 息継ぎが来たので切る


def test_no_break_in_the_middle_of_a_sentence():
    # 直前のチャンクが文の途中で終わっていたら、区切りは次の機会まで待つ。
    b = ParagraphBreaker(min_chars=120, pause_sec=0.7, max_chars=400)
    b.feed("あ" * 130, None)                        # 「。」で終わっていない
    assert b.feed("い" * 20 + "。", 1.0) == ""
    assert b.feed("う" * 20, 1.0) == "\n\n"         # 文が終わった次で切る


def test_short_paragraph_is_not_broken():
    b = ParagraphBreaker(min_chars=120, pause_sec=0.7, max_chars=400)
    b.feed("あ" * 30, None)
    assert b.feed("い" * 30, 5.0) == ""      # 長い息継ぎでも字数が足りなければ切らない


def test_max_chars_breaks_at_a_sentence_end_without_pause():
    # 息継ぎのない話し方（原稿の読み上げ）でも、文末が来れば切る。
    b = ParagraphBreaker(min_chars=120, pause_sec=0.7, max_chars=400)
    b.feed("あ" * 409 + "。", None)
    assert b.feed("い" * 10, 0.0) == "\n\n"


def test_max_chars_waits_for_the_sentence_to_end():
    b = ParagraphBreaker(min_chars=120, pause_sec=0.7, max_chars=400, hard_chars=800)
    b.feed("あ" * 410, None)                  # 文末が来ていない
    assert b.feed("い" * 10 + "。", 0.0) == ""
    assert b.feed("う" * 10, 0.0) == "\n\n"


def test_hard_chars_breaks_mid_sentence_as_a_last_resort():
    b = ParagraphBreaker(min_chars=120, pause_sec=0.7, max_chars=400, hard_chars=800)
    b.feed("あ" * 810, None)                  # 文末がずっと来ない
    assert b.feed("い" * 10, 0.0) == "\n\n"


def test_hard_chars_defaults_to_double():
    b = ParagraphBreaker(min_chars=120, pause_sec=0.7, max_chars=400)
    assert b.hard_chars == 800


def test_counter_resets_after_break():
    b = ParagraphBreaker(min_chars=120, pause_sec=0.7, max_chars=400)
    b.feed("あ" * 129 + "。", None)
    assert b.feed("い" * 10, 1.0) == "\n\n"
    assert b.feed("う" * 10, 1.0) == ""      # 直後は字数が足りない


def test_disabled_when_min_chars_is_zero():
    b = ParagraphBreaker(min_chars=0)
    b.feed("あ" * 500, None)
    assert b.feed("い" * 500, 9.0) == ""


def test_empty_text_does_not_break():
    b = ParagraphBreaker(min_chars=120, pause_sec=0.7, max_chars=400)
    b.feed("あ" * 200, None)
    assert b.feed("", 5.0) == ""


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
