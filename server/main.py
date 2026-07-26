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

from asr import AsrOptions, transcriber
from config import config
from postproc import postprocess
from recording import (
    RECORDINGS_DIR,
    SessionAudio,
    delete_session,
    list_sessions,
    load_slice,
)
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


@app.post("/reconvert")
async def reconvert_post(payload: dict = Body(...)) -> dict:
    """テキストの再変換候補を返す（録音中でなくても使える REST 版）。

    WS の {"type": "reconvert"} と同じ reconvert() を呼ぶ。
    選択範囲の再変換や「Aを再変換」コマンドがこちらを使う。
    """
    text = str(payload.get("text", ""))
    if not text.strip():
        raise HTTPException(status_code=400, detail="text を指定してください")
    if len(text) > 1000:
        raise HTTPException(status_code=400, detail="text が長すぎます（1000文字まで）")
    return await asyncio.to_thread(reconvert, text)


def _pcm16_to_float32(data: bytes) -> np.ndarray:
    """PCM16LE バイト列を float32(-1..1) に変換する。"""
    ints = np.frombuffer(data, dtype="<i2")
    return (ints.astype(np.float32) / 32768.0)


async def _transcribe_chunk(audio: np.ndarray, opts: AsrOptions):
    """認識をスレッドプールで実行（イベントループを塞がない）。"""
    # hotwords は既定OFF（長いと kotoba-whisper が認識を空にするため。config 参照）。
    hotwords = get_hotwords() if config.use_hotwords else None
    return await asyncio.to_thread(transcriber.transcribe, audio, hotwords, opts)


@app.get("/recordings")
def recordings_list() -> dict:
    """保存済み録音の一覧と保存先フォルダを返す（プラグインの管理UI用）。"""
    return {"dir": str(RECORDINGS_DIR), "items": list_sessions()}


@app.post("/recordings/delete")
def recordings_delete(payload: dict = Body(...)) -> dict:
    """指定した録音を削除する。IDの書式検証を通ったものだけ消す。"""
    sessions = payload.get("sessions", [])
    if not isinstance(sessions, list):
        raise HTTPException(status_code=400, detail="sessions は配列で指定してください")
    deleted, failed = [], []
    for s in sessions:
        try:
            delete_session(str(s))
            deleted.append(s)
        except (ValueError, FileNotFoundError, OSError) as exc:
            # 録音中のファイルは掴まれていて消せない。理由を返して黙って失敗しない。
            failed.append({"session": s, "reason": str(exc)})
    return {"deleted": deleted, "failed": failed}


@app.post("/recognize")
async def recognize(payload: dict = Body(...)) -> dict:
    """保存済みセッション音声の指定区間を、精度優先で認識し直す（復旧）。

    文字起こしで消えた箇所・誤変換された箇所を、元の音声から取り戻すための入口。
    前後にマージンを付けて渡すので、チャンク境界で切れた語も復元されやすい。
    """
    session = str(payload.get("session", ""))
    try:
        start = float(payload.get("start", 0.0))
        end = float(payload.get("end", 0.0))
        margin = float(payload.get("margin", 1.0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="範囲の指定が不正です") from exc

    try:
        audio = load_slice(session, start - margin, end + margin)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if audio.size == 0:
        raise HTTPException(status_code=400, detail="その範囲に音声がありません")

    result = await _transcribe_chunk(audio, AsrOptions.recovery())
    text = postprocess(
        result.text,
        strip_space=config.strip_ja_alnum_space,
        symbol_dictation=False,  # 復旧では「まる」等を記号に変換しない（原音に忠実に）
        replacements=get_replacements(),
        symbols=get_symbols(),
        auto_punctuate=config.enable_auto_punctuation,
    )
    return {"text": text, "dropped": result.dropped, "seconds": round(audio.size / config.sample_rate, 2)}


def _build_chunker(mode: str) -> VadChunker:
    """モードに応じた分割器を作る。dictation は config の既定値そのまま。"""
    if mode != "transcribe":
        return VadChunker(
            sample_rate=config.sample_rate,
            silence_sec=config.silence_sec,
            max_chunk_sec=config.max_chunk_sec,
            min_speech_sec=config.min_speech_sec,
            vad_threshold=config.vad_threshold,
            speech_pad_sec=config.speech_pad_sec,
        )
    # 文字起こしは切れ目なく喋り続ける音声が相手。
    # 原稿の読み上げは息継ぎが短く、無音を長く待つと強制確定でしか切れなくなる。
    # 強制確定は語の途中を断ち切るので、短い息継ぎでも拾える側に寄せた方が
    # 待ち時間も精度も良くなる。語尾は口述より厚めに残す。
    return VadChunker(
        sample_rate=config.sample_rate,
        silence_sec=min(config.silence_sec, 0.35),
        max_chunk_sec=min(config.max_chunk_sec, 12.0),
        min_speech_sec=0.1,
        vad_threshold=config.vad_threshold,
        speech_pad_sec=max(config.speech_pad_sec, 0.5),
    )


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    # モードはセッション単位。既定は口述＝従来どおりで、グローバル設定は触らない。
    mode = "dictation"
    chunker = _build_chunker(mode)
    opts = AsrOptions.dictation()
    strip_space = config.strip_ja_alnum_space
    symbols = config.enable_symbol_dictation
    session: SessionAudio | None = None

    await ws.send_text(json.dumps({"type": "ready"}))

    async def emit_chunk(audio: np.ndarray, reason: str, start: int, end: int) -> None:
        # 発話を検出しチャンクを確定 → 認識開始を通知（クライアントで「認識中…」表示）。
        await ws.send_text(json.dumps({"type": "partial", "reason": reason}))
        result = await _transcribe_chunk(audio, opts)
        text = postprocess(
            result.text,
            strip_space=strip_space,
            symbol_dictation=symbols,
            replacements=get_replacements(),
            symbols=get_symbols(),
            auto_punctuate=config.enable_auto_punctuation,
        )
        if not text and not result.dropped:
            return
        msg: dict = {"type": "chunk", "text": text}
        # 録音を残している間は、テキストと音声の対応（秒）を添えて復旧できるようにする。
        if session is not None:
            msg["session"] = session.session_id
            msg["start"] = round(start / config.sample_rate, 3)
            msg["end"] = round(end / config.sample_rate, 3)
        if result.dropped:
            msg["dropped"] = result.dropped
        await ws.send_text(json.dumps(msg))

    # 認識は受信ループから切り離してキューで回す。
    # 直列に await すると、認識中（数秒）は ws.receive() が呼ばれず音声が溜まり、
    # 終わった瞬間にまとめて流れ込む＝結果が塊で出る。キューにすれば受信は止まらない。
    # 認識自体はモデルのロックで直列化されるため、ワーカーは1本で足りる（順序も保たれる）。
    queue: asyncio.Queue = asyncio.Queue()

    async def worker() -> None:
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                await emit_chunk(*item)
            except Exception as exc:  # noqa: BLE001 - 1チャンクの失敗で全体を止めない
                print(f"[VoxCraft] チャンクの認識に失敗: {exc}")
            finally:
                queue.task_done()

    worker_task = asyncio.create_task(worker())

    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.disconnect":
                break

            # --- 音声ブロック（バイナリ） ---
            if msg.get("bytes") is not None:
                pcm = _pcm16_to_float32(msg["bytes"])
                # 分割・認識より前に、受け取った音声はまず丸ごと残す。
                # ここで残しておけば、後段が何を捨てても後から取り戻せる。
                if session is not None:
                    session.append(pcm)
                for chunk in chunker.push(pcm):
                    queue.put_nowait((chunk.audio, chunk.reason, chunk.start, chunk.end))
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
                    mode = "transcribe" if cmd.get("mode") == "transcribe" else "dictation"
                    chunker = _build_chunker(mode)
                    if mode == "transcribe":
                        opts = AsrOptions.transcription()
                        # 復旧の素材として、このセッションの音声を丸ごと保存する。
                        if session is None:
                            session = SessionAudio(config.sample_rate)
                            print(f"[VoxCraft] 文字起こしモード: 録音を保存 {session.path}")
                            await ws.send_text(json.dumps({
                                "type": "session", "session": session.session_id,
                            }))
                    else:
                        opts = AsrOptions.dictation()
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
                        queue.put_nowait((tail.audio, tail.reason, tail.start, tail.end))
                    # 溜まっている分を全部出し切ってから停止を通知する。
                    await queue.join()
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
    finally:
        worker_task.cancel()
        # 録音は必ず閉じる（異常終了しても、そこまでの音声は再認識に使える）。
        if session is not None:
            session.close()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
