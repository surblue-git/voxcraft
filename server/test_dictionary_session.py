"""Phase 1B WebSocket dictionary handshake tests (no model or audio required)."""
from __future__ import annotations

import asyncio
import json

from main import dictionary_set_replacements_get, ws_endpoint


class FakeWebSocket:
    def __init__(self, commands: list[dict]):
        self.commands = list(commands)
        self.sent: list[dict] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def receive(self) -> dict:
        if self.commands:
            return self.commands.pop(0)
        return {"type": "websocket.disconnect"}


def _text_command(payload: dict) -> dict:
    return {"type": "websocket.receive", "text": json.dumps(payload)}


def test_start_resolves_and_reports_dictionary_snapshot():
    ws = FakeWebSocket([
        _text_command({
            "type": "start",
            "mode": "dictation",
            "source": "microphone",
            "dictionarySetId": "default",
        }),
        {"type": "websocket.disconnect"},
    ])

    asyncio.run(ws_endpoint(ws))

    assert ws.accepted
    started = next(message for message in ws.sent if message["type"] == "started")
    assert started["dictionarySetId"] == "default"
    assert started["dictionarySetName"]
    assert started["dictionaryRevision"]
    assert started["dictionaryProfiles"] == ["common"]
    assert started["dictionaryProfileRevisions"]["common"]
    assert started["dictionaryWritableProfile"] == "common"
    assert isinstance(started["dictionaryWarnings"], list)


def test_unknown_dictionary_set_fails_before_started():
    ws = FakeWebSocket([
        _text_command({
            "type": "start",
            "mode": "dictation",
            "dictionarySetId": "missing-set",
        }),
        {"type": "websocket.disconnect"},
    ])

    asyncio.run(ws_endpoint(ws))

    assert not any(message["type"] == "started" for message in ws.sent)
    error = next(message for message in ws.sent if message["type"] == "error")
    assert error["fatal"] is True
    assert "missing-set" in error["message"]


def _run_all() -> int:
    functions = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    failed = 0
    for function in functions:
        try:
            function()
            print(f"PASS {function.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {function.__name__}: {exc}")
    print(f"\n{len(functions) - failed}/{len(functions)} passed")
    return failed



def test_set_replacements_are_served_in_apply_order():
    """辞書セットの置換を、**適用順のまま**配ること。

    既存ノートへ辞書を当て直す機能（プラグインの「このノートに辞書を当てる」）が
    この順で先頭から当てる。並び替えを呼び出し側に任せると最長一致の規則が
    2箇所に分かれて、いつか食い違う。順序はここで固定する。
    """
    from userdict import get_dictionary_snapshot

    body = dictionary_set_replacements_get("default")
    snapshot = get_dictionary_snapshot("default")
    assert body["setId"] == "default"
    assert body["revision"] == snapshot.revision
    served = [tuple(pair) for pair in body["replacements"]]
    assert served == list(snapshot.replacements)
    # 長いキーが先（最長一致）。ここが崩れると短いキーが先に当たって結果が変わる。
    lengths = [len(observed) for observed, _output in served]
    assert lengths == sorted(lengths, reverse=True)


def test_set_replacements_rejects_unknown_set():
    from fastapi import HTTPException

    try:
        dictionary_set_replacements_get("存在しないセット")
    except HTTPException as exc:
        assert exc.status_code in (400, 500)
    else:
        raise AssertionError("未知のセットIDでも成功してしまった")

if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
