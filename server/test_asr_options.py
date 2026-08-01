"""モード別ASR設定の回帰テスト。"""
from asr import AsrOptions


def test_refinement_prefers_coverage_over_context_carry():
    opts = AsrOptions.refinement()
    assert not opts.vad_filter
    assert opts.beam_size >= 8
    assert not opts.condition_on_previous


if __name__ == "__main__":
    test_refinement_prefers_coverage_over_context_carry()
    print("all tests passed")
