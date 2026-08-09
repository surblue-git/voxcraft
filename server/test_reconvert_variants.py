"""再変換の「読み揺らし」の単体テスト（ネットワーク不要）。

Google CGI へは行かず、変換関数を差し替えて挙動だけを確かめる。
"""
from __future__ import annotations

from reconvert import (
    _append_variant_candidates,
    reading_variants,
    variant_candidates,
)


def _kinds(hira: str, limit: int = 12) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for reading, kind in reading_variants(hira, limit):
        out.setdefault(kind, []).append(reading)
    return out


def test_voicing_variants():
    # 実測 2026-08-05:「では」と言って『ては』。テハ からは デハ の候補が出ない。
    kinds = _kinds("ては")
    assert "では" in kinds["voicing"]
    assert "てば" in kinds["voicing"]
    assert "てぱ" in kinds["voicing"]
    # 元の読みそのものは候補にしない。
    assert all(reading != "ては" for readings in kinds.values() for reading in readings)


def test_hatsuon_variants():
    # 「へんかん」と言っても「ん」が落ちて『へんか』になる（口述の癖として再現する）。
    assert "へんかん" in _kinds("へんか")["hatsuon"]
    assert "へんか" in _kinds("へんかん")["hatsuon"]


def test_sokuon_variants():
    assert "かっこ" in _kinds("かこ")["sokuon"]
    assert "かこ" in _kinds("かっこ")["sokuon"]


def test_implausible_readings_are_not_generated():
    """日本語に無い読みを変換に投げると、返る無意味な候補が本命を押し出す。

    実測: 「へんか」の揺らしに『へっんか』『へんっか』が混じり、その変換結果
    （減っんか／経っんか）が上限を食って『へんかん』→「変換」まで届かなかった。
    """
    all_readings = [reading for reading, _ in reading_variants("へんか", 20)]
    assert "へんかん" in all_readings
    for bad in ("へっんか", "へんっか"):
        assert bad not in all_readings
    # 促音は無声子音の前にしか立たない（「てっは」「きっよ」は日本語にない）。
    assert "てっは" not in [reading for reading, _ in reading_variants("ては", 20)]
    assert "きっよ" not in [reading for reading, _ in reading_variants("きよ", 20)]
    # 正しい位置の促音は残る。
    assert "かっこ" in [reading for reading, _ in reading_variants("かこ", 20)]


def test_variants_fire_even_when_the_reading_split_the_segments():
    """**読みが外れているからこそ文節が割れる。** 割れたら諦めてはいけない。

    実測 2026-08-09: 「同音異義語」を『動音域語』と誤認識すると読みは
    どうおんい**き**ご（正しくは …い**ぎ**ご）。誤った読みでは Google が
    2文節（どうおんいき / ご）に割り、候補は「同音域」止まりで正解に届かない。

    以前は `len(segments) != 1` で揺らしを打ち切っていたため、**揺らしが
    いちばん要る場面で発火していなかった**。判断は文節数ではなく長さで行う。
    """
    result = {
        "reading": "どうおんいきご",
        "segments": [
            {"start": 0, "end": 3, "reading": "どうおんいき", "candidates": ["同音域"]},
            {"start": 3, "end": 4, "reading": "ご", "candidates": ["語"]},
        ],
        "online": True,
    }
    _append_variant_candidates(result, "動音域語", "どうおんいきご")
    # 割れていた2文節は1つに畳まれ、全体を差し替える候補として並ぶ。
    assert len(result["segments"]) == 1, result["segments"]
    seg = result["segments"][0]
    assert (seg["start"], seg["end"]) == (0, 4)
    # 畳む前の第1候補（そのままの表記）は必ず先頭に残す。
    assert seg["candidates"][0] == "同音域語"
    assert len(seg["candidates"]) > 1


def test_variants_target_the_tapped_segment():
    """タップ経路では、叩いた文節の読みだけを揺らす。

    前半だけ「同音域」→「同音異義」に置換できれば、後ろの「語」と合わさって
    全体が直る。全体を差し替える必要はない。
    """
    def convert(reading: str) -> list[str]:
        return ["同音異義"] if reading == "どうおんいぎ" else []

    result = {
        "reading": "どうおんいきご",
        "segments": [
            {"start": 0, "end": 3, "reading": "どうおんいき", "candidates": ["同音域"]},
            {"start": 3, "end": 4, "reading": "ご", "candidates": ["語"]},
        ],
        "online": True,
    }
    import reconvert as _rc

    original = _rc._convert_variant
    _rc._convert_variant = convert
    try:
        _append_variant_candidates(result, "動音域語", "どうおんいきご", offset=1)
    finally:
        _rc._convert_variant = original
    # 文節は割れたまま（叩いた側だけに足す）。
    assert len(result["segments"]) == 2
    assert "同音異義" in result["segments"][0]["candidates"]
    assert result["segments"][1]["candidates"] == ["語"]


def test_long_reading_does_not_offer_bare_kana():
    """長い複合語では「かな自体が答え」が成り立たず、本命を押し下げるだけ。"""
    def convert(reading: str) -> list[str]:
        return [f"{reading}#"]

    out = variant_candidates(
        "どうおんいき", set(), limit=6, max_candidates=12, convert=convert
    )
    assert "とうおんいき" not in out, out
    # 短い機能語では従来どおり、かなそのものを候補に残す（ては→では）。
    short = variant_candidates("ては", set(), limit=6, max_candidates=12, convert=convert)
    assert "では" in short, short


def test_one_variant_does_not_fill_the_list():
    """本命は別の揺らしにいることが多い。1件で枠を使い切らせない。"""
    def convert(reading: str) -> list[str]:
        return [f"{reading}変換{n}" for n in range(1, 6)]

    out = variant_candidates("へんか", set(), limit=4, max_candidates=8, convert=convert)
    sources = {entry.split("変換")[0] for entry in out}
    assert len(sources) >= 3, out


def test_kinds_are_interleaved():
    """濁点だけで上限を使い切ると、撥音の誤りに永久に届かない。"""
    kinds = _kinds("へんか", limit=6)
    assert "voicing" in kinds and "hatsuon" in kinds


def test_variant_candidates_filters_noise():
    # 変換結果を差し替える（ネットワークへは行かない）。
    table = {
        "では": ["出は"],
        "てば": [],
        "てぱ": [],
        "てはん": ["てはん"],      # 変換できていない＝かなのまま
        "てんは": ["転派"],
    }

    def convert(reading: str) -> list[str]:
        return list(table.get(reading, []))

    known = {"ては", "手は"}
    out = variant_candidates("ては", known, limit=6, max_candidates=8, convert=convert)

    # 濁点の揺らしは、かな自体が答えのことがあるので必ず入れる。
    assert "では" in out
    assert "出は" in out
    # 撥音の揺らしは、変換できたものだけ。かなのままはノイズなので捨てる。
    assert "転派" in out
    assert "てはん" not in out
    # 既に出ている候補は重複させない。
    assert "手は" not in out


def test_variant_candidates_respects_cap():
    def convert(reading: str) -> list[str]:
        return [reading + "変換1", reading + "変換2"]

    out = variant_candidates("ては", set(), limit=6, max_candidates=3, convert=convert)
    assert len(out) == 3


def test_variant_candidates_survives_conversion_failure():
    """揺らしは補助。変換が落ちても本来の候補を壊さない。"""
    def convert(reading: str) -> list[str]:
        raise RuntimeError("offline")

    try:
        variant_candidates("ては", set(), limit=3, max_candidates=8, convert=convert)
    except RuntimeError:
        raise AssertionError("変換の失敗が呼び出し元まで漏れている")


def _run_all() -> int:
    functions = [
        value for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    ]
    failed = 0
    for function in functions:
        try:
            function()
            print(f"PASS {function.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {function.__name__}: {exc}")
    print(f"\n{len(functions) - failed}/{len(functions)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
