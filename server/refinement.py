"""PC音声の二段階文字起こしで使う補正ブロック計画。

速報チャンクは低遅延で本文へ送りつつ、一定量たまった音声を非重複の
ブロックとして再認識する。境界は速報チャンクの終端だけを使うため、
同じ範囲を二重に置換したり、語の途中で補正範囲を切ったりしない。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RefinementRange:
    start: int
    end: int
    revision: int


class RefinementPlanner:
    """確定済み音声位置から、次に補正すべき非重複範囲を作る。"""

    def __init__(self, sample_rate: int, window_sec: float, min_sec: float) -> None:
        self._window_samples = max(1, int(window_sec * sample_rate))
        self._min_samples = max(1, int(min_sec * sample_rate))
        self.reset()

    def reset(self) -> None:
        self._next_start = 0
        self._revision = 0

    @property
    def next_start(self) -> int:
        return self._next_start

    def ready(self, delivered_end: int) -> RefinementRange | None:
        """通常運転中、窓長に達したら現在のチャンク終端までを返す。"""
        if delivered_end - self._next_start < self._window_samples:
            return None
        return self._take(delivered_end)

    def flush(self, delivered_end: int) -> RefinementRange | None:
        """停止時、最小長以上残っていれば末尾も補正する。"""
        if delivered_end - self._next_start < self._min_samples:
            return None
        return self._take(delivered_end)

    def _take(self, end: int) -> RefinementRange:
        self._revision += 1
        result = RefinementRange(self._next_start, end, self._revision)
        self._next_start = end
        return result
