"""VoxCraft 認識サーバー（FastAPI + WebSocket）。

プロトコル（WebSocket, /ws）:
  クライアント → サーバー
    - バイナリフレーム: PCM16LE モノラル 16kHz の音声ブロック
    - テキストフレーム(JSON): 制御コマンド
        {"type": "start", "symbols": true, "stripSpace": true,
         "mode": "transcribe", "source": "system"}
        {"type": "stop"}                     # 残り音声をflushして確定
        {"type": "reconvert", "text": "..."} # 再変換候補を要求
        {"type": "tune", "fast": true}       # 候補選択中だけ応答速度優先に切替（false で復帰）

  サーバー → クライアント（すべて JSON テキストフレーム）
    - {"type": "ready"}                                # 接続確立
    - {"type": "started", "source": "system", "device": "..."}
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
import traceback

import numpy as np
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from asr import AsrOptions, hq_transcriber, transcriber
from config import config
from postproc import ParagraphBreaker, postprocess
from recording import (
    RECORDINGS_DIR,
    SessionAudio,
    delete_session,
    list_sessions,
    load_slice,
)
from punctuate import available as punctuation_available
from reconvert import reconvert
from system_audio import SystemAudioError, WasapiLoopbackCapture
from transcribe_guard import (
    AudioRingBuffer,
    SilenceTracker,
    SpeechEvidence,
    filter_contextual_artifacts,
    speech_evidence,
)
from userdict import (
    DictValidationError,
    get_error,
    get_hotwords,
    get_replacements,
    get_symbols,
    read_raw,
    write_raw,
)
from vad import ChunkJoiner, VadChunker

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
        # 文字起こし・復旧用モデル（遅延ロードなので未使用なら ready=false）。
        "transcribeModel": config.transcribe_model or config.model,
        "transcribeReady": hq_transcriber().ready,
        "beamSize": config.beam_size,
        "autoPunctuation": config.enable_auto_punctuation and punctuation_available(),
        "silenceSec": config.silence_sec,
        "systemAudioAutoStopSec": config.transcribe_auto_stop_sec,
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


async def _transcribe_chunk(audio: np.ndarray, opts: AsrOptions, *, hq: bool = False):
    """認識をスレッドプールで実行（イベントループを塞がない）。

    hq=True は文字起こし・復旧用の高品質モデル（既定 turbo）を使う。
    口述は従来どおり config.model のまま ＝ 挙動不変。
    ロードもスレッド側で行う（10秒前後かかるのでイベントループを塞がない）。
    """
    # hotwords は既定OFF（長いと kotoba-whisper が認識を空にするため。config 参照）。
    hotwords = get_hotwords() if config.use_hotwords else None
    target = hq_transcriber() if hq else transcriber

    def _run():
        if hq:
            target.ensure_loaded()
        return target.transcribe(audio, hotwords, opts)

    return await asyncio.to_thread(_run)


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

    result = await _transcribe_chunk(audio, AsrOptions.recovery(), hq=True)
    evidence = speech_evidence(
        audio,
        config.sample_rate,
        active_rms=config.retry_active_rms,
    )
    guarded_text, contextual_blocked = filter_contextual_artifacts(
        result.text,
        evidence,
        weak_rms=config.retry_min_rms,
        weak_active_ratio=config.retry_active_ratio,
    )
    result.blocked.extend(contextual_blocked)
    text = postprocess(
        guarded_text,
        strip_space=config.strip_ja_alnum_space,
        symbol_dictation=False,  # 復旧では「まる」等を記号に変換しない（原音に忠実に）
        replacements=get_replacements(),
        symbols=get_symbols(),
        auto_punctuate=config.enable_auto_punctuation,
    )
    if result.blocked:
        # 復旧でも定型句は返さない。選んだ区間が幻覚だった場合は結果が空になる
        # （＝「その音声にその文言は無い」が正しい答え）。理由はログに残す。
        print(f"[VoxCraft] 定型句をブロック（復旧）: {result.blocked[0]} ({session} {start}-{end}秒)")
    return {
        "text": text,
        "dropped": result.dropped,
        "blocked": result.blocked,
        "seconds": round(audio.size / config.sample_rate, 2),
    }


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
    # 短いチャンクの連結と段落分けは文字起こしモードだけ（None なら従来どおり）。
    joiner: ChunkJoiner | None = None
    breaker: ParagraphBreaker | None = None
    opts = AsrOptions.dictation()
    strip_space = config.strip_ja_alnum_space
    symbols = config.enable_symbol_dictation
    punctuate = config.enable_auto_punctuation
    session: SessionAudio | None = None
    source = "microphone"
    system_capture: WasapiLoopbackCapture | None = None
    system_audio_queue: asyncio.Queue | None = None
    system_consumer_task: asyncio.Task | None = None
    last_source_level_at = 0.0
    audio_ring = AudioRingBuffer(max(1, int(config.retry_buffer_sec * config.sample_rate)))
    last_source_chunk_end = 0
    silence_tracker: SilenceTracker | None = None
    auto_stop_task: asyncio.Task | None = None
    input_finished = False
    finish_lock = asyncio.Lock()

    await ws.send_text(json.dumps({"type": "ready"}))

    async def emit_chunk(
        audio: np.ndarray,
        reason: str,
        start: int,
        end: int,
        pause: float | None = None,
        recovery: bool = False,
        evidence: SpeechEvidence | None = None,
    ) -> None:
        # 発話を検出しチャンクを確定 → 認識開始を通知（クライアントで「認識中…」表示）。
        await ws.send_text(json.dumps({"type": "partial", "reason": reason}))
        # mode は下のコマンド処理で書き換わる。ここは常に現在のモードを見る。
        chunk_opts = AsrOptions.recovery() if recovery else opts
        result = await _transcribe_chunk(audio, chunk_opts, hq=(mode == "transcribe"))
        raw_text = result.text
        if mode == "transcribe" and raw_text:
            chunk_evidence = evidence or speech_evidence(
                audio,
                config.sample_rate,
                active_rms=config.retry_active_rms,
            )
            raw_text, contextual_blocked = filter_contextual_artifacts(
                raw_text,
                chunk_evidence,
                weak_rms=config.retry_min_rms,
                weak_active_ratio=config.retry_active_ratio,
            )
            if contextual_blocked:
                result.blocked.extend(contextual_blocked)
                print(
                    f"[VoxCraft] 文脈付き定型句を除去: {contextual_blocked} "
                    f"({start / config.sample_rate:.1f}-{end / config.sample_rate:.1f}秒)"
                )
        text = postprocess(
            raw_text,
            strip_space=strip_space,
            symbol_dictation=symbols,
            replacements=get_replacements(),
            symbols=get_symbols(),
            auto_punctuate=punctuate,
        )
        if recovery:
            # 音声根拠を通過しても、Whisper側の確信度が低い結果や定型句は採用しない。
            # 何も送らなければ、直後の通常チャンクを受けたクライアントが従来どおり
            # この区間を ⟨未認識 ...⟩ として残す。
            accepted = (
                bool(text)
                and result.avg_logprob is not None
                and result.avg_logprob >= config.retry_min_logprob
            )
            if not accepted:
                ev = evidence or SpeechEvidence(0.0, 0.0, 0.0, 0.0)
                print(
                    f"[VoxCraft] 自動再認識を不採用: "
                    f"{start / config.sample_rate:.1f}-{end / config.sample_rate:.1f}秒 "
                    f"rms={ev.rms:.5f} active={ev.active_ratio:.1%} "
                    f"logprob={result.avg_logprob} blocked={result.blocked[:1]}"
                )
                return
            print(
                f"[VoxCraft] 欠落を自動復旧: "
                f"{start / config.sample_rate:.1f}-{end / config.sample_rate:.1f}秒 "
                f"logprob={result.avg_logprob:.2f} text={text[:80]}"
            )
        if not text and result.blocked:
            # 動画のアウトロ定型句（「ご視聴ありがとうございました」等）。本文には出さない。
            # ただし位置は通しておく: 何も送らないとクライアントが音声の切れ目とみなして
            # ⟨未認識⟩ マーカーを置いてしまい、幻覚が別のノイズに化けるだけになる。
            # 件数が多い（実測206件/47.5分）ので dropped には入れず警告も出さない。
            print(
                f"[VoxCraft] 定型句をブロック: {result.blocked[0]} "
                f"({start / config.sample_rate:.1f}-{end / config.sample_rate:.1f}秒)"
            )
            if session is not None:
                await ws.send_text(json.dumps({
                    "type": "chunk", "text": "",
                    "session": session.session_id,
                    "start": round(start / config.sample_rate, 3),
                    "end": round(end / config.sample_rate, 3),
                }))
            return
        if not text and not result.dropped:
            # 認識が1セグメントも返さなかったチャンク。クライアント側は音声の
            # 連続性が切れたことから ⟨未認識⟩ を置くが、サーバー側に記録が無いと
            # 後から原因を追えない（実測: 50分の取材で12秒×18ぶんが消えたのに
            # ログには何も残っていなかった）。位置と長さを必ず残す。
            print(
                f"[VoxCraft] 空チャンク（認識結果なし）: "
                f"{start / config.sample_rate:.1f}-{end / config.sample_rate:.1f}秒 "
                f"({(end - start) / config.sample_rate:.1f}秒, reason={reason})"
            )
            return
        # 段落分け（文字起こしのみ）。ベタ打ちを避けるため、話の切れ目で空行を挟む。
        # クライアントは受け取ったテキストをそのまま挿入するので、区切りはここで付ける
        # ＝ プラグインを更新しなくても全端末で効く。
        if breaker is not None and not recovery:
            text = breaker.feed(text, pause) + text
        msg: dict = {"type": "chunk", "text": text}
        if recovery:
            msg["recovered"] = True
        # 前チャンクとの息継ぎ長（秒）。クライアントの「息継ぎで読点」判断に使う。
        if pause is not None:
            msg["pause"] = round(pause, 2)
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
                # str(exc) が空になる例外があり、実際にログが
                # 「チャンクの認識に失敗: 」だけで終わっていた。型と位置まで残す。
                where = ""
                if isinstance(item, tuple) and len(item) >= 4:
                    where = (f" 位置={item[2] / config.sample_rate:.1f}-"
                             f"{item[3] / config.sample_rate:.1f}秒")
                print(f"[VoxCraft] チャンクの認識に失敗: {type(exc).__name__}: {exc}{where}")
                traceback.print_exc()
            finally:
                queue.task_done()

    worker_task = asyncio.create_task(worker())

    def put(chunks: list) -> None:
        """認識キューへそのまま流す。"""
        for c in chunks:
            queue.put_nowait((c.audio, c.reason, c.start, c.end, c.pause, False, None))

    def put_recovery(audio: np.ndarray, start: int, end: int, evidence: SpeechEvidence) -> None:
        queue.put_nowait((audio, "auto_recovery", start, end, None, True, evidence))

    def enqueue(chunks: list) -> None:
        """確定チャンクを認識キューへ流す（文字起こしでは短いものを連結してから）。"""
        nonlocal last_source_chunk_end
        for chunk in chunks:
            gap_start = last_source_chunk_end
            gap_end = chunk.start
            gap_samples = gap_end - gap_start
            retry_gap = (
                mode == "transcribe"
                and gap_start > 0
                and gap_samples >= int(config.retry_gap_min_sec * config.sample_rate)
                and gap_samples <= int(config.retry_gap_max_sec * config.sample_rate)
            )
            if gap_end > gap_start and joiner is not None:
                # joiner が保持中の直前チャンクを、欠落再認識より先に必ず出す。
                put(joiner.flush())
            if retry_gap:
                gap_audio = audio_ring.slice(gap_start, gap_end)
                if gap_audio is not None:
                    evidence = speech_evidence(
                        gap_audio,
                        config.sample_rate,
                        active_rms=config.retry_active_rms,
                    )
                    if evidence.supports_retry(
                        min_rms=config.retry_min_rms,
                        min_active_ratio=config.retry_active_ratio,
                    ):
                        print(
                            f"[VoxCraft] 欠落を再認識候補へ: "
                            f"{gap_start / config.sample_rate:.1f}-{gap_end / config.sample_rate:.1f}秒 "
                            f"rms={evidence.rms:.5f} active={evidence.active_ratio:.1%}"
                        )
                        put_recovery(gap_audio, gap_start, gap_end, evidence)
                    else:
                        print(
                            f"[VoxCraft] 低音量の欠落は再認識しない: "
                            f"{gap_start / config.sample_rate:.1f}-{gap_end / config.sample_rate:.1f}秒 "
                            f"rms={evidence.rms:.5f} active={evidence.active_ratio:.1%}"
                        )
            put(joiner.push(chunk) if joiner is not None else [chunk])
            last_source_chunk_end = max(last_source_chunk_end, chunk.end)

    async def accept_audio(pcm: np.ndarray, *, report_level: bool = False) -> None:
        """全音源を共通の保存・VAD・認識経路へ入れる。"""
        nonlocal last_source_level_at
        if pcm.size == 0:
            return
        audio_ring.append(pcm)
        if silence_tracker is not None:
            silence_tracker.feed(pcm, asyncio.get_running_loop().time())
        if session is not None:
            session.append(pcm)
        enqueue(chunker.push(pcm))
        if joiner is not None:
            put(joiner.tick())

        if report_level:
            now = asyncio.get_running_loop().time()
            if now - last_source_level_at >= 0.1:
                last_source_level_at = now
                level = float(np.sqrt(np.mean(np.square(pcm, dtype=np.float64))))
                await ws.send_text(json.dumps({"type": "level", "level": round(level, 5)}))

    async def start_system_audio() -> dict:
        nonlocal system_capture, system_audio_queue, system_consumer_task
        if system_capture is not None:
            raise SystemAudioError("PC音声入力は既に開始しています。")
        loop = asyncio.get_running_loop()
        audio_queue: asyncio.Queue = asyncio.Queue()

        def on_audio(audio: np.ndarray) -> None:
            loop.call_soon_threadsafe(audio_queue.put_nowait, audio)

        def on_error(exc: Exception) -> None:
            loop.call_soon_threadsafe(audio_queue.put_nowait, exc)

        async def consume() -> None:
            while True:
                item = await audio_queue.get()
                try:
                    if item is None:
                        return
                    if isinstance(item, Exception):
                        await ws.send_text(json.dumps({
                            "type": "error", "message": str(item), "fatal": True,
                        }))
                        continue
                    await accept_audio(item, report_level=True)
                finally:
                    audio_queue.task_done()

        capture = WasapiLoopbackCapture(config.sample_rate, on_audio, on_error)
        consumer = asyncio.create_task(consume())
        try:
            info = await asyncio.to_thread(capture.start)
        except Exception:
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass
            raise
        system_capture = capture
        system_audio_queue = audio_queue
        system_consumer_task = consumer
        print(
            f"[VoxCraft] PC音声入力: {info.device} "
            f"({info.sample_rate}Hz/{info.channels}ch → {config.sample_rate}Hz/mono)"
        )
        return {
            "device": info.device,
            "inputSampleRate": info.sample_rate,
            "channels": info.channels,
        }

    async def stop_system_audio() -> None:
        nonlocal system_capture, system_audio_queue, system_consumer_task
        capture, audio_queue, consumer = (
            system_capture, system_audio_queue, system_consumer_task
        )
        system_capture = None
        system_audio_queue = None
        system_consumer_task = None
        if capture is not None:
            await asyncio.to_thread(capture.stop)
        if audio_queue is not None and consumer is not None and not consumer.done():
            # stop() がリサンプラーの末尾を on_audio へ渡した後に sentinel を置く。
            # これにより末尾まで accept_audio を通してから consumer が終了する。
            audio_queue.put_nowait(None)
            await consumer

    async def finish_input(reason: str = "manual") -> None:
        """音源停止→末尾確定→認識完了通知を、手動・自動停止で共用する。"""
        nonlocal input_finished, silence_tracker, auto_stop_task
        async with finish_lock:
            if input_finished:
                return
            input_finished = True
            silence_tracker = None
            current = asyncio.current_task()
            if auto_stop_task is not None and auto_stop_task is not current:
                auto_stop_task.cancel()
            auto_stop_task = None
            if source == "system":
                await stop_system_audio()
            tail = chunker.flush()
            if tail is not None:
                enqueue([tail])
            if joiner is not None:
                put(joiner.flush())
            # 自動停止でも、停止直前までの認識をすべて本文へ返してから閉じる。
            await queue.join()
            await ws.send_text(json.dumps({"type": "stopped", "reason": reason}))

    async def watch_system_silence() -> None:
        """PC出力の実音が既定時間来なければ文字起こしを完了する。"""
        while silence_tracker is not None and not input_finished:
            remaining = silence_tracker.remaining(asyncio.get_running_loop().time())
            if remaining <= 0:
                print(
                    f"[VoxCraft] PC音声が{config.transcribe_auto_stop_sec:.0f}秒無音のため自動停止"
                )
                await finish_input("silence")
                return
            await asyncio.sleep(min(5.0, max(0.1, remaining)))

    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.disconnect":
                break

            # --- 音声ブロック（バイナリ） ---
            if msg.get("bytes") is not None:
                pcm = _pcm16_to_float32(msg["bytes"])
                await accept_audio(pcm)
                continue

            # --- 制御コマンド（テキスト JSON） ---
            if msg.get("text") is not None:
                try:
                    cmd = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                ctype = cmd.get("type")

                if ctype == "start":
                    created_session = False
                    input_finished = False
                    silence_tracker = None
                    audio_ring = AudioRingBuffer(
                        max(1, int(config.retry_buffer_sec * config.sample_rate))
                    )
                    last_source_chunk_end = 0
                    strip_space = bool(cmd.get("stripSpace", config.strip_ja_alnum_space))
                    symbols = bool(cmd.get("symbols", config.enable_symbol_dictation))
                    mode = "transcribe" if cmd.get("mode") == "transcribe" else "dictation"
                    requested_source = cmd.get("source", "microphone")
                    source = "system" if requested_source == "system" else "microphone"
                    if source == "system" and mode != "transcribe":
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "message": "PC音声入力は文字起こしモードでのみ使用できます。",
                            "fatal": True,
                        }))
                        continue
                    chunker = _build_chunker(mode)
                    if mode == "transcribe":
                        opts = AsrOptions.transcription()
                        joiner = ChunkJoiner(
                            sample_rate=config.sample_rate,
                            min_sec=config.transcribe_join_sec,
                            max_hold_sec=config.transcribe_join_hold_sec,
                            break_sec=config.transcribe_join_break_sec,
                        )
                        breaker = ParagraphBreaker(
                            min_chars=config.paragraph_chars,
                            pause_sec=config.paragraph_pause_sec,
                            max_chars=config.paragraph_max_chars,
                            hard_chars=config.paragraph_hard_chars,
                        )
                        # 初回チャンクを待たせないよう、高品質モデルのロードを
                        # 先に走らせておく（音声が届くまでの間に間に合う）。
                        hq = hq_transcriber()
                        if not hq.ready:
                            asyncio.get_running_loop().run_in_executor(
                                None, hq.ensure_loaded
                            )
                        # 復旧の素材として、このセッションの音声を丸ごと保存する。
                        if session is None:
                            session = SessionAudio(config.sample_rate)
                            created_session = True
                            print(f"[VoxCraft] 文字起こしモード: 録音を保存 {session.path}")
                            await ws.send_text(json.dumps({
                                "type": "session", "session": session.session_id,
                            }))
                    else:
                        opts = AsrOptions.dictation()
                        joiner = None
                        breaker = None
                    punctuate = config.enable_auto_punctuation

                    source_info: dict = {}
                    if source == "system":
                        try:
                            source_info = await start_system_audio()
                        except (SystemAudioError, OSError) as exc:
                            if created_session and session is not None:
                                failed_session = session.session_id
                                session.close()
                                session = None
                                try:
                                    delete_session(failed_session)
                                except (ValueError, FileNotFoundError, OSError):
                                    pass
                            await ws.send_text(json.dumps({
                                "type": "error", "message": str(exc), "fatal": True,
                            }))
                            continue
                        except Exception as exc:  # デバイス固有の PortAudio エラーも表示する
                            if created_session and session is not None:
                                failed_session = session.session_id
                                session.close()
                                session = None
                                try:
                                    delete_session(failed_session)
                                except (ValueError, FileNotFoundError, OSError):
                                    pass
                            await ws.send_text(json.dumps({
                                "type": "error",
                                "message": f"PC音声入力を開始できません: {exc}",
                                "fatal": True,
                            }))
                            continue
                        if config.transcribe_auto_stop_sec > 0:
                            silence_tracker = SilenceTracker(
                                config.transcribe_auto_stop_sec,
                                config.transcribe_audible_rms,
                                asyncio.get_running_loop().time(),
                            )
                            auto_stop_task = asyncio.create_task(watch_system_silence())
                            source_info["autoStopSec"] = config.transcribe_auto_stop_sec
                    await ws.send_text(json.dumps({
                        "type": "started", "source": source, **source_info,
                    }))
                    # 辞書が壊れていたらクライアントに知らせる（無言で無効化しない）。
                    dict_err = get_error()
                    if dict_err:
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "message": f"辞書(userdict.json)を読めません: {dict_err}",
                        }))

                elif ctype == "stop":
                    await finish_input("manual")

                elif ctype == "reconvert":
                    payload = await asyncio.to_thread(reconvert, cmd.get("text", ""))
                    await ws.send_text(json.dumps({"type": "reconvert", **payload}))

                elif ctype == "tune":
                    # 候補モーダルを開いている間だけ「短い発話に速く応える」側へ寄せる。
                    # 口述本体の既定値は触らず、モーダルを閉じたら必ず元に戻す。
                    if mode == "dictation":
                        if bool(cmd.get("fast", False)):
                            chunker.set_silence_sec(min(config.silence_sec, 0.25))
                            chunker.set_min_speech_sec(0.15)
                            opts = AsrOptions.command()
                            punctuate = False
                        else:
                            chunker.set_silence_sec(config.silence_sec)
                            chunker.set_min_speech_sec(config.min_speech_sec)
                            opts = AsrOptions.dictation()
                            punctuate = config.enable_auto_punctuation

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - クライアントへ通知して閉じる
        try:
            await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
        except Exception:
            pass
    finally:
        if auto_stop_task is not None:
            auto_stop_task.cancel()
        try:
            await stop_system_audio()
        except Exception as exc:  # 終了処理は録音保存を妨げない
            print(f"[VoxCraft] PC音声入力の終了に失敗: {type(exc).__name__}: {exc}")
        worker_task.cancel()
        # 録音は必ず閉じる（異常終了しても、そこまでの音声は再認識に使える）。
        if session is not None:
            session.close()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
