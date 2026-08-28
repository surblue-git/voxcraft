"""AI校正のモデル呼び出しと、モードごとのプロンプト。

置き場所
--------
既定はローカル（Ollama 互換の `/api/chat`）。「無料・ローカル認識」が製品の柱で、
取材内容が外へ出ないことが使える理由そのものなので、既定を外に出さない。

**リアルタイム経路には入れない。** 校正はブロック境界と停止時にだけ走らせる。
認識のレイテンシ予算に触らせないための約束で、`main.py` から使うときも同じ。

プロンプトは「モードの説明」でしかない
--------------------------------------
何を許すかを最終的に決めるのは `refine_guard` の門であって、この文言ではない。
ここは**当たりやすくするため**の指示で、守られたかどうかは必ず門で確かめる。
だからプロンプトを弱くしても危険は増えない（却下が増えるだけ）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol, Sequence
from urllib import error, request

from refine_guard import CONVERSATION, INTERVIEW, MANUSCRIPT, GuardProfile


# --- プロンプト -----------------------------------------------------------

_COMMON_RULES = """必ず守ること:
- 出力は校正後の本文だけ。前置き・説明・箇条書きの注釈を付けない。
- 事実を足さない。入力に無い数値・固有名詞・出来事を書かない。
- 要約しない。文を削らない。段落を勝手にまとめない。
- 判断に迷ったら、元の表記のままにする。"""

_GLOSSARY_HEADER = """この分野で正しい表記（読みが合っていればこの表記へ直す）:
{terms}"""

_PROMPTS = {
    "interview": """あなたは日本語の文字起こしの校正者です。話者の発言をそのまま残しながら、
**表記の誤りだけ**を直します。

直してよいもの:
- 同音異義語の誤変換（例: 苦闘店 → 句読点）
- 固有名詞・専門用語の表記（下の用語集にあるものだけ）
- 句読点の付け方、かっこの対応

**絶対に変えないもの**:
- 語順・言い回し・語尾。話者が言ったとおりの言葉を残す。
- フィラー（えー、あの、そうですね）。**消さない。**
- 話し言葉の崩れ。直さない。

読みが変わる書き換えは、すべて誤りです。""",

    "conversation": """あなたは日本語の文字起こしの校正者です。話したとおりの言葉を残しつつ、
読みやすさを少しだけ上げます。

直してよいもの:
- 同音異義語の誤変換
- 固有名詞・専門用語の表記（下の用語集にあるものだけ）
- 句読点の付け方
- **明らかなフィラーの削除のみ**（えー、あのー、えーと、その、まあ）
- 直後の言い直しで捨てられた語（「今期は、今期の見通しは」→「今期の見通しは」）

**変えないもの**:
- 残った部分の語順・言い回し・語尾。言い換えない。
- 話し言葉らしさ。書き言葉へ直さない。

言葉を足すことは、すべて誤りです。""",

    "manuscript": """あなたは日本語の原稿の校正者です。口述された話し言葉を、
そのまま読める原稿へ整えます。

してよいこと:
- 同音異義語の誤変換の訂正
- 固有名詞・専門用語の表記の統一（下の用語集にあるものだけ）
- フィラーの削除（えー、あの、えーと、その、まあ、なんか）
- 言い直し・重複の整理
- 話し言葉から書き言葉への調整（語尾、助詞の乱れ、係り受けの崩れ）
- 句読点の付与と、文の切れ目の整理

**してはいけないこと**:
- 内容を足す。書かれていない事実・数値・固有名詞を持ち込む。
- 要約する。一文でも削る。
- 話者の主張や結論を変える。

言い回しは整えてよいが、**言っていないことは書かない**。""",
}


def build_system_prompt(profile: GuardProfile, glossary: Sequence[str] = ()) -> str:
    """モードの説明と用語集。本文は user 側で渡す（system は使い回せる）。"""
    parts = [_PROMPTS[profile.name], "", _COMMON_RULES]
    if glossary:
        parts += ["", _GLOSSARY_HEADER.format(terms="、".join(glossary))]
    return "\n".join(parts)


# --- モデル ---------------------------------------------------------------

@dataclass(frozen=True)
class RefineResult:
    text: str
    seconds: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class RefineClient(Protocol):
    @property
    def name(self) -> str: ...

    def refine(self, profile: GuardProfile, text: str,
               glossary: Sequence[str] = ()) -> RefineResult: ...


class StubClient:
    """入力をそのまま返す。ベンチの配線を、モデルを立てずに確かめるために使う。

    門はすべて通るはずなので、`--model stub` で却下が出たらそれは門のバグ。
    """

    name = "stub"

    def refine(self, profile, text, glossary=()) -> RefineResult:
        return RefineResult(text, 0.0)


class ReplayClient:
    """保存済みの校正結果を読み直す。モデルは呼ばない。

    2つの用途がある。
    1. 門やプロンプトを変えたあと、**同じ出力を採点し直す**（モデルを回さずに
       門の変更だけを評価できる。GPUの1時間を毎回使わなくてよい）。
    2. Ollama 以外の経路で作った出力を、同じ物差しに載せる。

    区切りは行頭の `--- 8< ---`。ブロック数が合わないのは配線の誤りなので、
    黙って詰めずに例外にする。
    """

    SEPARATOR = "--- 8< ---"

    def __init__(self, path, label: str = "replay") -> None:
        from pathlib import Path

        raw = Path(path).read_text(encoding="utf-8")
        self._blocks = [part.strip() for part in raw.split(self.SEPARATOR)]
        self._index = 0
        self._label = label

    @property
    def name(self) -> str:
        return self._label

    def __len__(self) -> int:
        return len(self._blocks)

    def refine(self, profile, text, glossary=()) -> RefineResult:
        if self._index >= len(self._blocks):
            raise ValueError(
                f"保存済み出力が足りない（{len(self._blocks)}件しかない）。"
                "ブロックの割り方が測ったときと違う可能性がある。")
        body = self._blocks[self._index]
        self._index += 1
        return RefineResult(body, 0.0)


class OllamaClient:
    """Ollama 互換の `/api/chat` を叩く。

    温度は0固定。同じ入力で同じ結果に落ちること（べき等）が、
    保存済み録音に対して回帰を測るための前提になる。
    """

    def __init__(self, model: str, base_url: str, timeout_sec: float = 120.0,
                 num_ctx: int = 8192) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.num_ctx = num_ctx

    @property
    def name(self) -> str:
        return self.model

    def refine(self, profile, text, glossary=()) -> RefineResult:
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "messages": [
                {"role": "system", "content": build_system_prompt(profile, glossary)},
                {"role": "user", "content": text},
            ],
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        started = time.perf_counter()
        try:
            with request.urlopen(req, timeout=self.timeout_sec) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except error.URLError as exc:
            return RefineResult("", time.perf_counter() - started, f"接続できない: {exc}")
        except (ValueError, TimeoutError) as exc:
            return RefineResult("", time.perf_counter() - started, f"応答が読めない: {exc}")
        elapsed = time.perf_counter() - started

        content = (data.get("message") or {}).get("content", "")
        if not content:
            return RefineResult("", elapsed, "本文が空で返った")
        return RefineResult(_strip_wrapper(content), elapsed)


_FENCES = ("```", "~~~")


def _strip_wrapper(text: str) -> str:
    """モデルが付けがちなコードフェンスと前置きを落とす。

    プロンプトで禁じても混ざることがあるので、機械的に剥がす。剥がしきれない
    ぶんは門が「作った」と見なして却下するので、ここで無理はしない。
    """
    body = text.strip()
    for fence in _FENCES:
        if body.startswith(fence):
            lines = body.split("\n")
            if len(lines) >= 2:
                body = "\n".join(lines[1:])
            if body.rstrip().endswith(fence):
                body = body.rstrip()[: -len(fence)].rstrip()
            break
    return body.strip()


def make_client(model: str, base_url: str, timeout_sec: float = 120.0,
                replay=None) -> RefineClient:
    if replay is not None:
        return ReplayClient(replay, label=model if model != "stub" else "replay")
    return StubClient() if model == "stub" else OllamaClient(model, base_url, timeout_sec)


PROFILE_BY_NAME = {p.name: p for p in (INTERVIEW, CONVERSATION, MANUSCRIPT)}
