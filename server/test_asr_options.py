"""モード別ASR設定の回帰テスト。"""
from dataclasses import replace

from asr import AsrOptions


def test_refinement_prefers_coverage_over_context_carry():
    opts = AsrOptions.refinement()
    assert not opts.vad_filter
    assert opts.beam_size >= 8
    assert not opts.condition_on_previous


def test_every_mode_defaults_to_config_language():
    """language は既定 None ＝ config.language に従う。

    None でなくなると、口述が English 実験の巻き添えで壊れる。ここを守れば
    「英語の実験中も日本語口述は無傷」が保証される（この不変条件のために
    language をグローバル env からモード別へ移した）。
    """
    for build in (
        AsrOptions.dictation, AsrOptions.transcription, AsrOptions.command,
        AsrOptions.word, AsrOptions.probe, AsrOptions.recovery,
        AsrOptions.refinement,
    ):
        assert build().language is None, build.__name__


def test_language_override_leaves_everything_else_intact():
    """言語だけ差し替えても、他のモード設定は1つも変わらない。"""
    base = AsrOptions.transcription()
    en = replace(base, language="en")
    assert en.language == "en"
    assert replace(en, language=None) == base


if __name__ == "__main__":
    test_refinement_prefers_coverage_over_context_carry()
    test_every_mode_defaults_to_config_language()
    test_language_override_leaves_everything_else_intact()
    print("all tests passed")
