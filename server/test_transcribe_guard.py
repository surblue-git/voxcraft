"""文字起こし専用の音声根拠・リングバッファ・無音監視テスト。"""
import numpy as np

from transcribe_guard import (
    AudioRingBuffer,
    SilenceTracker,
    SpeechEvidence,
    filter_contextual_artifacts,
    speech_evidence,
    should_remove_between_context,
    standalone_contextual_artifact,
)

SR = 16000


def test_speech_evidence_requires_level_and_active_frames():
    audio = np.zeros(SR * 10, dtype=np.float32)
    # 10秒中1秒に、遠い発話を想定した小さな信号を置く。
    audio[SR * 4:SR * 5] = 0.004
    ev = speech_evidence(audio, SR)
    assert 0.09 <= ev.active_ratio <= 0.11
    assert ev.supports_retry(min_rms=0.001, min_active_ratio=0.03)


def test_low_level_consultation_does_not_trigger_retry():
    audio = np.full(SR * 24, 0.0008, dtype=np.float32)
    ev = speech_evidence(audio, SR)
    assert not ev.supports_retry(min_rms=0.0015, min_active_ratio=0.03)


def test_short_click_alone_does_not_trigger_retry():
    audio = np.zeros(SR * 12, dtype=np.float32)
    audio[100:200] = 0.5
    ev = speech_evidence(audio, SR)
    assert ev.peak == 0.5
    assert not ev.supports_retry(min_rms=0.0015, min_active_ratio=0.03)


def test_ring_buffer_uses_absolute_positions():
    ring = AudioRingBuffer(10)
    ring.append(np.arange(8, dtype=np.float32))
    ring.append(np.arange(8, 15, dtype=np.float32))
    assert ring.start == 5 and ring.end == 15
    assert np.array_equal(ring.slice(7, 12), np.arange(7, 12, dtype=np.float32))
    assert ring.slice(4, 8) is None


def test_ring_buffer_slice_can_cross_physical_wrap():
    ring = AudioRingBuffer(8)
    ring.append(np.arange(5, dtype=np.float32))
    ring.append(np.arange(5, 11, dtype=np.float32))
    assert ring.start == 3 and ring.end == 11
    assert np.array_equal(ring.slice(4, 10), np.arange(4, 10, dtype=np.float32))


def test_silence_tracker_resets_only_for_audible_audio():
    tracker = SilenceTracker(timeout_sec=300, audible_rms=0.0001, now=100)
    tracker.feed(np.zeros(1600, dtype=np.float32), now=200)
    assert tracker.last_audible_at == 100
    tracker.feed(np.full(1600, 0.001, dtype=np.float32), now=250)
    assert tracker.last_audible_at == 250
    assert not tracker.expired(549.9)
    assert tracker.expired(550.0)


def test_silence_from_the_start_is_reported_once_the_warning_delay_passes():
    # 取得先を間違えた録音は last_audible_at が初期値のまま動かない。
    tracker = SilenceTracker(timeout_sec=300, audible_rms=0.0001, now=100)
    tracker.feed(np.zeros(1600, dtype=np.float32), now=110)
    assert not tracker.silent_since_start(now=119.9, warn_sec=20)
    assert tracker.silent_since_start(now=120.0, warn_sec=20)
    assert not tracker.silent_since_start(now=999.0, warn_sec=0)  # 0で無効


def test_audio_that_arrived_once_never_triggers_the_start_warning():
    # 鳴っていない動画を流しているだけ、という正常な使い方まで警告しない。
    tracker = SilenceTracker(timeout_sec=300, audible_rms=0.0001, now=100)
    tracker.feed(np.full(1600, 0.001, dtype=np.float32), now=105)
    assert tracker.heard_any
    assert not tracker.silent_since_start(now=500.0, warn_sec=20)


def test_embedded_video_artifact_is_removed_without_losing_real_text():
    strong = SpeechEvidence(8.0, 0.02, 0.2, 0.6)
    text, removed = filter_contextual_artifacts(
        "こちらも省略します。次回予告次のスライドをご覧ください。", strong
    )
    assert text == "こちらも省略します。次のスライドをご覧ください。"
    assert removed == ["次回予告"]


def test_midstream_thanks_is_removed_but_real_ending_is_kept():
    strong = SpeechEvidence(30.0, 0.02, 0.2, 0.6)
    text, removed = filter_contextual_artifacts(
        "体験価値を提供します。ご視聴ありがとうございました。昨年は新施設を開設しました。",
        strong,
    )
    assert text == "体験価値を提供します。昨年は新施設を開設しました。"
    assert removed == ["ご視聴ありがとうございました"]
    assert filter_contextual_artifacts(
        "以上で発表を終わります。ご視聴ありがとうございました。", strong
    )[0] == "以上で発表を終わります。ご視聴ありがとうございました。"


def test_real_strong_standalone_phrase_is_kept():
    strong = SpeechEvidence(2.0, 0.02, 0.2, 0.6)
    text, removed = filter_contextual_artifacts("おやすみなさい。", strong)
    assert text == "おやすみなさい。"
    assert removed == []


def test_standalone_artifact_is_identified_for_delayed_context_check():
    assert standalone_contextual_artifact(" おやすみなさい。 ") == "おやすみなさい"
    assert standalone_contextual_artifact("皆様、おやすみなさい。") is None


def test_artifact_between_formal_handoff_and_introduction_is_removed():
    assert should_remove_between_context(
        "おやすみなさい",
        "それでは中山さんお願いいたします。",
        "皆様こんにちは中山でございます。",
    )


def test_real_standalone_phrase_is_not_removed_without_both_formal_neighbors():
    assert not should_remove_between_context(
        "おやすみなさい",
        "それでは、また明日。",
        "皆様もゆっくり休んでください。",
    )
    assert not should_remove_between_context(
        "おやすみなさい", "それでは中山さんお願いいたします。", ""
    )


def test_weak_standalone_artifact_is_removed():
    weak = SpeechEvidence(12.0, 0.0008, 0.02, 0.005)
    text, removed = filter_contextual_artifacts("次の動画でお会いしましょう", weak)
    assert text == ""
    assert removed == ["次の動画でお会いしましょう"]


def test_possible_real_ending_requires_weak_audio_to_remove():
    weak = SpeechEvidence(5.0, 0.0008, 0.02, 0.005)
    strong = SpeechEvidence(5.0, 0.02, 0.2, 0.6)
    assert filter_contextual_artifacts("以上で終わります。", weak)[0] == ""
    assert filter_contextual_artifacts("以上で終わります。", strong)[0] == "以上で終わります。"


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
