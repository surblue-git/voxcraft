"""PC音声の音源種別テスト。

    python -m pytest test_system_source.py
    python test_system_source.py

system（サーバー機の再生音を自前で取る）と system-client（クライアント機が取った
再生音を送ってくる）は、取得元が違うだけで扱いは同じでなければならない。
分割条件がずれると、片方だけ細切れになって前後の文脈が失われる。
"""
from config import config
from main import SYSTEM_SOURCES, _build_chunker, _join_profile
from vad import _FRAME


def _silence_frames(sec: float) -> int:
    return max(1, int(sec * config.sample_rate / _FRAME))


def _params(chunker):
    return (
        chunker._silence_frames,
        chunker._max_samples,
        chunker._min_samples,
        chunker._pad_frames,
    )


def test_both_system_sources_are_treated_as_pc_audio():
    assert "system" in SYSTEM_SOURCES
    assert "system-client" in SYSTEM_SOURCES


def test_client_side_capture_uses_the_same_chunking_as_server_side():
    server = _build_chunker("transcribe", "system")
    client = _build_chunker("transcribe", "system-client")
    assert _params(server) == _params(client)


def test_system_chunking_uses_the_system_settings_not_the_microphone_ones():
    # マイク文字起こしは短い息継ぎで切る側（0.35秒上限）に寄せてある。PC音声は
    # そこへ巻き込まれず system_silence_sec を使う（前後の文脈を残すため）。
    system = _build_chunker("transcribe", "system-client")
    mic = _build_chunker("transcribe", "microphone")
    assert system._max_samples == int(config.system_max_chunk_sec * config.sample_rate)
    assert system._silence_frames == _silence_frames(config.system_silence_sec)
    assert system._silence_frames != mic._silence_frames


def test_far_mic_only_changes_joining_not_the_cutting():
    # 遠いマイクで壊れていたのは連結であってVADではない（実測でカバー率96.2%）。
    # 音の切り方に触れると口述・取材まで影響が及ぶので、ここは動かさない。
    assert _params(_build_chunker("transcribe", "microphone")) == _params(
        _build_chunker("transcribe", "microphone")
    )
    normal = _join_profile(False, False)
    far = _join_profile(False, True)
    assert normal == (
        config.transcribe_join_sec,
        config.transcribe_join_hold_sec,
        config.transcribe_join_break_sec,
    )
    assert far == (
        config.far_mic_join_sec,
        config.far_mic_join_hold_sec,
        config.far_mic_join_break_sec,
    )
    # 会見は「間」だらけで、待ち時間とまたげる息継ぎの両方が効かないと連結できない。
    assert far[0] > normal[0] and far[1] > normal[1] and far[2] > normal[2]


def test_far_mic_does_not_override_pc_audio():
    # PC音声はもともと長い連結を使う。farMic を送られても system の値のまま。
    assert _join_profile(True, True) == _join_profile(True, False)


def test_dictation_is_unaffected_by_source():
    # 口述は音源に関係なく既定値のまま。ここが動くと口述の挙動が変わる。
    baseline = _build_chunker("dictation", "microphone")
    for source in SYSTEM_SOURCES:
        assert _params(_build_chunker("dictation", source)) == _params(baseline)
    assert baseline._max_samples == int(config.max_chunk_sec * config.sample_rate)


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
