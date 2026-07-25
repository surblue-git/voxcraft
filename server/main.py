"""VoxCraft 認識サーバー（FastAPI + WebSocket）。

プロトコル（WebSocket, /ws）:
  クライアント → サーバー
    - バイナリフレーム: PCM16LE モノラル 16kHz の音声ブロック
    - テキストフレーム(JSON): 制御コマンド
        {"type": "start", "symbols": true, "stripSpace": true}
        {"type": "stop"}                     # 残り音声をflushして確定
        {"type": "reconvert", "text": "..."} # 再変換候補を要求

  サーバー → クライアント（すべて JSON テキストフレーム）
    - {"type": "ready"}                                # 接続確立
    - {"type": "partial", "reason": "silence|max_len"} # チャンク認識開始の合図（任意）
    - {"type": "chunk", "text": "確定テキスト"}          # 確定チャンク
    - {"type": "reconvert", "reading": "...", "segments": [...], "online": bool}
    - {"type": "error", "message": "..."}

起動:
    python -m uvicorn main:app --host 0.0.0.0 --port 8760
  もしくは:
    python main.py
"""
from __future__ import annotations

import asyncio
import json

import numpy as np
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from asr import transcriber
from config import config
from postproc import postprocess
from punctuate import available as punctuation_available
from reconvert import reconvert
from userdict import (
    DictValidationError,
    get_error,
    get_hotwords,
    get_replacements,
    get_symbols,
    read_raw,
    write_raw,
)
from vad import VadChunker

app = FastAPI(title="VoxCraft ASR Server")


@app.on_event("startup")
def _startup() -> None:
    print(f"[VoxCraft] loading model: {config.model} (device={config.device}) ...")
    transcriber.load()
    print(f"[VoxCraft] model ready on {transcriber.device}/{transcriber.compute} "
          f"(beam={config.beam_size}).")


@app.get("/health")
def health() -> dict:
    return {
        "ready": transcriber.ready,
        "model": config.model,
        "resolvedModel": transcriber.resolved_model,
        "device": transcriber.device,
        "compute": transcriber.compute,
        "beamSize": config.beam_size,
        "autoPunctuation": config.enable_auto_punctuation and punctuation_available(),
        "silenceSec": config.silence_sec,
        "dictError": get_error(),
    }


@app.get("/dict")
def dict_get() -> dict:
    """ユーザー辞書を返す（プラグインの編集UI用）。"""
    return read_raw()


@app.post("/dict")
async def dict_post(payload: dict = Body(...)) -> dict:
    """ユーザー辞書を保存する（検証あり）。保存後は自動でリロードされる。

    サーバーは認証を持たないため、文字列マップであること・件数と長さの上限を
    厳格に検証してからでないと書き込まない。
    """
    try:
        counts = write_raw(payload.get("replacements", {}), payload.get("symbols", {}))
    except DictValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"保存できません: {exc}") from exc
    return {"ok": True, **counts}


def _pcm16_to_float32(data: bytes) -> np.ndarray:
    """PCM16LE バイト列を float32(-1..1) に変換する。"""
    ints = np.frombuffer(data, dtype="<i2")
    return (ints.astype(np.float32) / 32768.0)


async def _transcribe_chunk(audio: np.ndarray) -> str:
    """認識をスレッドプールで実行（イベントループを塞がない）。"""
    # hotwords は既定OFF（長いと kotoba-whisper が認識を空にするため。config 参照）。
    hotwords = get_hotwords() if config.use_hotwords else None
    return await asyncio.to_thread(transcriber.transcribe, audio, hotwords)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    chunker = VadChunker(
        sample_rate=config.sample_rate,
        silence_sec=config.silence_sec,
        max_chunk_sec=config.max_chunk_sec,
        min_speech_sec=config.min_speech_sec,
        vad_threshold=config.vad_threshold,
        speech_pad_sec=config.speech_pad_sec,
    )
    strip_space = config.strip_ja_alnum_space
    symbols = config.enable_symbol_dictation

    await ws.send_text(json.dumps({"type": "ready"}))

    async def emit_chunk(audio: np.ndarray, reason: str) -> None:
        # 発話を検出しチャンクを確定 → 認識開始を通知（クライアントで「認識中…」表示）。
        await ws.send_text(json.dumps({"type": "partial", "reason": reason}))
        text = await _transcribe_chunk(audio)
        text = postprocess(
            text,
            strip_space=strip_space,
            symbol_dictation=symbols,
            replacements=get_replacements(),
            symbols=get_symbols(),
            auto_punctuate=config.enable_auto_punctuation,
        )
        if text:
            await ws.send_text(json.dumps({"type": "chunk", "text": text}))

    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.disconnect":
                break

            # --- 音声ブロック（バイナリ） ---
            if msg.get("bytes") is not None:
                pcm = _pcm16_to_float32(msg["bytes"])
                for chunk in chunker.push(pcm):
                    await emit_chunk(chunk.audio, chunk.reason)
                continue

            # --- 制御コマンド（テキスト JSON） ---
            if msg.get("text") is not None:
                try:
                    cmd = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                ctype = cmd.get("type")

                if ctype == "start":
                    strip_space = bool(cmd.get("stripSpace", config.strip_ja_alnum_space))
                    symbols = bool(cmd.get("symbols", config.enable_symbol_dictation))
                    # 辞書が壊れていたらクライアントに知らせる（無言で無効化しない）。
                    dict_err = get_error()
                    if dict_err:
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "message": f"辞書(userdict.json)を読めません: {dict_err}",
                        }))

                elif ctype == "stop":
                    tail = chunker.flush()
                    if tail is not None:
                        await emit_chunk(tail.audio, tail.reason)
                    await ws.send_text(json.dumps({"type": "stopped"}))

                elif ctype == "reconvert":
                    payload = await asyncio.to_thread(reconvert, cmd.get("text", ""))
                    await ws.send_text(json.dumps({"type": "reconvert", **payload}))

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - クライアントへ通知して閉じる
        try:
            await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
        except Exception:
            pass


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
