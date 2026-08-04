"""文字起こしセッションの再接続（resume）テスト。

モデルのロードを避けるため、mode="transcribe" を実際に ws_endpoint 経由で
開始する経路は通さない。_PENDING_SESSIONS の登録・失効・resume コマンドの
入口（未知セッションの拒否）だけを検証する。
"""
from __future__ import annotations

import asyncio
import json

import main
from main import _PENDING_SESSIONS, _hold_session_for_resume, ws_endpoint
from recording import SessionAudio, delete_session


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


def test_hold_registers_and_expiry_closes_when_not_resumed():
    async def scenario():
        session = SessionAudio(16000, session_id="20260101-000000-1")
        try:
            original_grace = main._RESUME_GRACE_SEC
            main._RESUME_GRACE_SEC = 0.05
            try:
                _hold_session_for_resume(session)
                assert session.session_id in _PENDING_SESSIONS
                await asyncio.sleep(0.15)
                assert session.session_id not in _PENDING_SESSIONS
                assert session._closed
            finally:
                main._RESUME_GRACE_SEC = original_grace
        finally:
            delete_session(session.session_id)

    asyncio.run(scenario())


def test_hold_then_pop_before_expiry_keeps_session_open():
    async def scenario():
        session = SessionAudio(16000, session_id="20260101-000000-2")
        try:
            _hold_session_for_resume(session)
            pending = _PENDING_SESSIONS.pop(session.session_id, None)
            assert pending is not None
            pending.expiry.cancel()
            assert pending.session is session
            assert not session._closed
        finally:
            session.close()
            delete_session(session.session_id)

    asyncio.run(scenario())


def test_resume_with_unknown_session_is_rejected():
    ws = FakeWebSocket([
        _text_command({"type": "resume", "session": "20260101-000000-doesnotexist"}),
        {"type": "websocket.disconnect"},
    ])

    asyncio.run(ws_endpoint(ws))

    assert not any(message["type"] == "started" for message in ws.sent)
    error = next(message for message in ws.sent if message["type"] == "error")
    assert error["fatal"] is True


class _FakeHqTranscriber:
    """hq_transcriber() の差し替え。ready=True にして実モデルのロードを避ける。"""
    ready = True


def test_disconnect_mid_transcribe_holds_session_and_resume_continues_same_file():
    """start(transcribe) → 予期せぬ切断 → resume が同じセッション（同じWAV）へ続くことを確認する。"""
    async def scenario():
        session_id = None
        original_hq = main.hq_transcriber
        main.hq_transcriber = lambda: _FakeHqTranscriber()
        try:
            ws1 = FakeWebSocket([
                _text_command({
                    "type": "start",
                    "mode": "transcribe",
                    "source": "microphone",
                    "dictionarySetId": "default",
                }),
                {"type": "websocket.disconnect"},
            ])
            await ws_endpoint(ws1)

            session_msg = next(m for m in ws1.sent if m["type"] == "session")
            session_id = session_msg["session"]
            # stop を送らずに切れた＝再接続待ちで保持されているはず。
            assert session_id in _PENDING_SESSIONS
            held_path = _PENDING_SESSIONS[session_id].session.path

            ws2 = FakeWebSocket([
                _text_command({
                    "type": "resume", "session": session_id, "dictionarySetId": "default",
                }),
                {"type": "websocket.disconnect"},
            ])
            await ws_endpoint(ws2)

            started = next(m for m in ws2.sent if m["type"] == "started")
            assert started["source"] == "microphone"
            # 再開では新しいセッションを作らない（"session" 通知を再送しない）。
            assert not any(m["type"] == "session" for m in ws2.sent)
            # ws2 も stop を送らず切れたので、同じファイルのまま再び保持される。
            assert session_id in _PENDING_SESSIONS
            assert _PENDING_SESSIONS[session_id].session.path == held_path
        finally:
            main.hq_transcriber = original_hq
            leftover = _PENDING_SESSIONS.pop(session_id, None) if session_id else None
            if leftover is not None:
                leftover.expiry.cancel()
                leftover.session.close()
            if session_id:
                delete_session(session_id)

    asyncio.run(scenario())


def test_resume_without_session_field_is_rejected():
    ws = FakeWebSocket([
        _text_command({"type": "resume"}),
        {"type": "websocket.disconnect"},
    ])

    asyncio.run(ws_endpoint(ws))

    assert not any(message["type"] == "started" for message in ws.sent)
    error = next(message for message in ws.sent if message["type"] == "error")
    assert error["fatal"] is True


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


if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
