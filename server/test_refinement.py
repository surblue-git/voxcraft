"""PC音声の補正ブロック計画のテスト。"""
from refinement import RefinementPlanner


SR = 16_000


def test_emits_non_overlapping_ranges_at_chunk_boundaries():
    planner = RefinementPlanner(SR, window_sec=30.0, min_sec=8.0)

    assert planner.ready(12 * SR) is None
    first = planner.ready(33 * SR)
    assert first is not None
    assert (first.start, first.end, first.revision) == (0, 33 * SR, 1)

    assert planner.ready(55 * SR) is None
    second = planner.ready(66 * SR)
    assert second is not None
    assert (second.start, second.end, second.revision) == (33 * SR, 66 * SR, 2)


def test_flush_keeps_short_tail_and_emits_long_tail():
    planner = RefinementPlanner(SR, window_sec=30.0, min_sec=8.0)
    first = planner.ready(31 * SR)
    assert first is not None

    assert planner.flush(37 * SR) is None
    tail = planner.flush(40 * SR)
    assert tail is not None
    assert (tail.start, tail.end, tail.revision) == (31 * SR, 40 * SR, 2)


def test_reset_restarts_revision_and_range():
    planner = RefinementPlanner(SR, window_sec=30.0, min_sec=8.0)
    assert planner.ready(30 * SR) is not None
    planner.reset()
    again = planner.ready(30 * SR)
    assert again is not None
    assert (again.start, again.end, again.revision) == (0, 30 * SR, 1)


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
