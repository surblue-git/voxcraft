"""VoxCraft 認識サーバー（FastAPI + WebSocket）。

プロトコル（WebSocket, /ws）:
  クライアント → サーバー
    - バイナリフレーム: PCM16LE モノラル 16kHz の音声ブロック
    - テキストフレーム(JSON): 制御コマンド
        {"type": "start", "symbols": true, "stripSpace": true,
         "mode": "transcribe", "source": "system", "dictionarySetId": "default"}
        # source は "microphone" / "system"（サーバー機の再生音を自前で取る）/
        # "system-client"（クライアントが取った再生音をバイナリで送ってくる）。
        # "device" は source によって意味が変わる。"system" ならサーバー機の
        # どの入力先を開くか（/audio-devices の name。省略時は既定の出力の
        # ループバック）。"system-client" なら表示用の名前で、started へそのまま返す。
        {"type": "stop"}                     # 残り音声をflushして確定
        {"type": "reconvert", "text": "..."} # 再変換候補を要求
        {"type": "tune", "fast": true}       # 候補選択中だけ応答速度優先に切替（false で復帰）
        {"type": "tune", "word": true}       # 言い直し待ちの間だけ単語向けに切替（同上）

  サーバー → クライアント（すべて JSON テキストフレーム）
    - {"type": "ready"}                                # 接続確立
    - {"type": "started", "source": "system", "device": "...",
       "dictionarySetId": "default", "dictionaryRevision": "..."}
    - {"type": "partial", "reason": "silence|max_len"} # チャンク認識開始の合図（任意）
    - {"type": "probe", "seq": 7, "text": "入力キャンセル", "reading": "ニュウリョク…"}
      # 口述の短いチャンクだけ、小さいモデルで先に読んだ速報（125ms前後）。
      # クライアントがコマンドと判定したら即実行し、同じ seq の chunk を捨てる。
      # 本文としては絶対に使わない（精度がkotobaに劣るため）。
    - {"type": "chunk", "text": "確定テキスト", "seq": 7, "reading": "…"} # 確定チャンク
    - {"type": "refinement", "text": "補正後", "start": 0, "end": 30,
       "revision": 1}                                    # PC音声の範囲補正
    - {"type": "reconvert", "reading": "...", "segments": [...], "online": bool}
    - {"type": "warning", "code": "no_audio", "device": "...", "message": "..."}
      # PC音声で、開始から一度も音が来ていない（取得先の選び間違い）。1回だけ。
      # 録音は続く。自動停止（既定300秒）まで黙っていると何も残らないための予告。
    - {"type": "error", "message": "..."}

起動:
    python -m uvicorn main:app --host 0.0.0.0 --port 8760
  もしくは:
    python main.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from asr import AsrOptions, hq_transcriber, probe_transcriber, transcriber
from config import config
from dictionary_registry import (
    DictionaryRevisionConflict,
    DictionarySchemaError,
    DictionarySnapshot,
)
from postproc import ParagraphBreaker, postprocess
from recording import (
    RECORDINGS_DIR,
    SessionAudio,
    delete_session,
    list_sessions,
    load_slice,
)
from punctuate import available as punctuation_available
from punctuate import to_reading
from reconvert import reconvert
from refinement import RefinementPlanner, RefinementRange
from system_audio import SystemAudioError, WasapiLoopbackCapture
from transcribe_guard import (
    AudioRingBuffer,
    SilenceTracker,
    SpeechEvidence,
    filter_contextual_artifacts,
    speech_evidence,
    should_remove_between_context,
    standalone_contextual_artifact,
)
from userdict import (
    DictValidationError,
    add_profile_entry,
    add_profile_symbol,
    dictionary_catalog,
    get_dictionary_snapshot,
    get_error,
    read_profile,
    read_profile_raw,
    read_raw,
    validate_profile_raw,
    write_profile_raw,
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
        # コマンド先読み用モデル（口述の短いチャンクのみ。遅延ロード）。
        "commandProbe": config.command_probe,
        "commandProbeModel": config.command_probe_model,
        "commandProbeReady": probe_transcriber().ready,
        "autoPunctuation": config.enable_auto_punctuation and punctuation_available(),
        "silenceSec": config.silence_sec,
        "systemAudioAutoStopSec": config.transcribe_auto_stop_sec,
        "systemChunkSeconds": config.system_join_sec,
        "systemRefineEnabled": config.system_refine_enabled,
        "systemRefineWindowSeconds": config.system_refine_window_sec,
        "dictError": get_error(),
    }


@app.get("/audio-devices")
async def audio_devices() -> dict:
    """このサーバー機で PC音声として取り込める入力先の一覧。

    プラグインの設定UIが引く。**列挙は必ず子プロセスで行う。** PortAudio は
    アクセス違反で落ちることがあり（実測: 別PCから文字起こし中に設定画面を開いて
    `_portaudiowpatch.pyd` が 0xC0000005）、Python の例外にならないので
    try/except では守れない。プロセス内で呼ぶと録音中のサーバーごと落ちる。
    Windows 以外や PyAudioWPatch 未導入でも error を返すだけにする。
    """
    script = str(Path(__file__).with_name("system_audio.py"))
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, script, "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return {"devices": [], "error": f"入力デバイスを列挙できません: {exc}"}

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"devices": [], "error": "入力デバイスの列挙が応答しませんでした。"}

    if proc.returncode != 0:
        # 子プロセスがネイティブごと落ちた場合はここに来る（サーバーは無事）。
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        tail = detail[-1] if detail else f"終了コード {proc.returncode}"
        print(f"[VoxCraft] 入力デバイスの列挙に失敗: {tail}")
        return {"devices": [], "error": f"入力デバイスを列挙できません: {tail}"}

    try:
        return json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        return {"devices": [], "error": f"入力デバイスの一覧を解釈できません: {exc}"}


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
    except (DictValidationError, DictionarySchemaError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"保存できません: {exc}") from exc
    return {"ok": True, **counts}


@app.get("/dictionaries")
def dictionaries_get() -> dict:
    """新形式の辞書プロファイルと辞書セットの一覧を返す。"""
    try:
        return dictionary_catalog()
    except (DictionarySchemaError, OSError) as exc:
        raise HTTPException(status_code=500, detail=f"辞書一覧を読めません: {exc}") from exc


@app.post("/dictionaries/validate")
async def dictionaries_validate(payload: dict = Body(...)) -> dict:
    """辞書ファイルを保存せずに検証する。"""
    return validate_profile_raw(payload)


@app.get("/dictionaries/{profile_id}")
def dictionary_profile_get(profile_id: str) -> dict:
    """検証済みの辞書プロファイル本文を返す。"""
    try:
        return read_profile(profile_id)
    except DictionarySchemaError as exc:
        detail = str(exc)
        status = 404 if "見つかりません" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"辞書を読めません: {exc}") from exc


@app.post("/dictionaries/{profile_id}/entries")
async def dictionary_entry_post(profile_id: str, payload: dict = Body(...)) -> dict:
    """競合を検出しながら、辞書プロファイルへ置換を1件だけ追加する。"""
    expected_revision = payload.get("expectedRevision")
    if expected_revision is not None and not isinstance(expected_revision, str):
        raise HTTPException(status_code=400, detail="expectedRevision が不正です")
    try:
        return add_profile_entry(
            profile_id,
            payload.get("observed"),
            payload.get("output"),
            expected_revision=expected_revision,
            hotword=payload.get("hotword", False),
            priority=payload.get("priority", 0),
            note=payload.get("note", ""),
        )
    except DictionaryRevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "currentRevision": exc.current_revision},
        ) from exc
    except DictionarySchemaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"辞書を保存できません: {exc}") from exc


@app.post("/dictionaries/{profile_id}/symbols")
async def dictionary_symbol_post(profile_id: str, payload: dict = Body(...)) -> dict:
    """記号語（単独チャンク一致）を1件だけ追加する。置換とは別枠。

    「まる」と言ったのに『悪』と認識される類は、読みが違うので再変換では拾えない。
    観測した綴りをここへ入れると、以後は後処理で記号に直る。
    """
    expected_revision = payload.get("expectedRevision")
    if expected_revision is not None and not isinstance(expected_revision, str):
        raise HTTPException(status_code=400, detail="expectedRevision が不正です")
    observed = payload.get("observed")
    output = payload.get("output")
    if not isinstance(observed, str) or not observed.strip():
        raise HTTPException(status_code=400, detail="observed が不正です")
    if not isinstance(output, str) or not output:
        raise HTTPException(status_code=400, detail="output が不正です")
    try:
        return add_profile_symbol(
            profile_id,
            observed,
            output,
            expected_revision=expected_revision,
        )
    except DictionaryRevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "currentRevision": exc.current_revision},
        ) from exc
    except DictionarySchemaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"辞書を保存できません: {exc}") from exc


@app.get("/dictionaries/{profile_id}/dict")
def dictionary_profile_dict_get(profile_id: str) -> dict:
    """指定プロファイルを置換・記号語のフラット形式で返す（/dict と同じ編集UI用）。"""
    try:
        return read_profile_raw(profile_id)
    except DictionarySchemaError as exc:
        detail = str(exc)
        status = 404 if "見つかりません" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"辞書を読めません: {exc}") from exc


@app.post("/dictionaries/{profile_id}/dict")
async def dictionary_profile_dict_post(profile_id: str, payload: dict = Body(...)) -> dict:
    """指定プロファイルの置換・記号語を保存する（/dict と同じ検証・即時反映）。"""
    try:
        counts = write_profile_raw(
            profile_id, payload.get("replacements", {}), payload.get("symbols", {})
        )
    except (DictValidationError, DictionarySchemaError) as exc:
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
    dictionary = _resolve_dictionary_snapshot(payload.get("dictionarySetId", "default"))
    result = await asyncio.to_thread(reconvert, text, dictionary.reverse_replacements)
    return {**result, **dictionary.metadata()}


def _resolve_dictionary_snapshot(set_id) -> DictionarySnapshot:
    if not isinstance(set_id, str) or not set_id:
        raise HTTPException(status_code=400, detail="dictionarySetId が不正です")
    try:
        return get_dictionary_snapshot(set_id)
    except DictionarySchemaError as exc:
        raise HTTPException(status_code=400, detail=f"辞書セットを解決できません: {exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"辞書セットを読めません: {exc}") from exc


def _pcm16_to_float32(data: bytes) -> np.ndarray:
    """PCM16LE バイト列を float32(-1..1) に変換する。"""
    ints = np.frombuffer(data, dtype="<i2")
    return (ints.astype(np.float32) / 32768.0)


async def _transcribe_chunk(
    audio: np.ndarray,
    opts: AsrOptions,
    dictionary: DictionarySnapshot,
    *,
    hq: bool = False,
):
    """認識をスレッドプールで実行（イベントループを塞がない）。

    hq=True は文字起こし・復旧用の高品質モデル（既定 turbo）を使う。
    口述は従来どおり config.model のまま ＝ 挙動不変。
    ロードもスレッド側で行う（10秒前後かかるのでイベントループを塞がない）。
    """
    # hotwords は既定OFF（長いと kotoba-whisper が認識を空にするため。config 参照）。
    hotwords = dictionary.hotword_prompt if config.use_hotwords else None
    target = hq_transcriber() if hq else transcriber

    def _run():
        if hq:
            target.ensure_loaded()
        return target.transcribe(audio, hotwords, opts, dictionary.hallucinations)

    return await asyncio.to_thread(_run)


async def _probe_chunk(audio: np.ndarray, dictionary: DictionarySnapshot):
    """小さいモデルで「コマンドかどうか」だけを先に読む（口述の短いチャンク専用）。

    本文には使わないので辞書置換も句読点も掛けない。素の認識結果と読みだけ返す。
    """
    target = probe_transcriber()

    def _run():
        target.ensure_loaded()
        return target.transcribe(audio, None, AsrOptions.probe(), dictionary.hallucinations)

    return await asyncio.to_thread(_run)


def _log_chunk(kind: str, seq: int, raw: str, final: str, seconds: float | None = None) -> None:
    """認識結果をログに残す（VOXCRAFT_LOG_CHUNKS=1 のときだけ）。

    記号語やコマンドが効かないとき、効く／効かないを分けているのは「Whisperが
    実際に何と書いたか」であって、耳で聞いた発話ではない。これが見えないと
    辞書のキーも正規表現も当て推量になる（実測は config.log_chunk_text 参照）。

    後処理の前後を両方出すのは、辞書・記号化が仕事をしたかを1行で見分けるため。
    同じなら何も掛かっていない＝キーが一致していない、と即断できる。
    読みを併記するのは、コマンドのあいまい照合がそれを見ているため。
    """
    if not config.log_chunk_text:
        return
    where = f" {seconds:.1f}秒" if seconds is not None else ""
    line = f"[VoxCraft] chunk#{seq} {kind}{where} 認識={final!r}"
    if raw != final:
        line += f" 後処理前={raw!r}"
    reading = to_reading(final)
    if reading:
        line += f" 読み={reading}"
    print(line)


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
    dictionary = _resolve_dictionary_snapshot(payload.get("dictionarySetId", "default"))
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

    result = await _transcribe_chunk(audio, AsrOptions.recovery(), dictionary, hq=True)
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
        replacements=dictionary.replacement_plan,
        symbols=dictionary.symbols,
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
        **dictionary.metadata(),
    }


# PC音声として扱う音源。取得元がサーバー機（system）かクライアント機
# （system-client）かの違いだけで、分割・連結・補正・自動停止はすべて共通。
SYSTEM_SOURCES = ("system", "system-client")


def _build_chunker(mode: str, source: str = "microphone") -> VadChunker:
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
    if source in SYSTEM_SOURCES:
        # PC音声は応答コマンドを聞く必要がない。短い息継ぎで細切れにせず、
        # 8〜12秒程度の文脈を速報認識へ渡す。
        return VadChunker(
            sample_rate=config.sample_rate,
            silence_sec=config.system_silence_sec,
            max_chunk_sec=config.system_max_chunk_sec,
            min_speech_sec=0.1,
            vad_threshold=config.vad_threshold,
            speech_pad_sec=max(config.speech_pad_sec, 0.5),
        )
    # マイク文字起こしは切れ目なく喋り続ける音声が相手。
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


@dataclass
class _PendingSession:
    """接続が予期せず切れた文字起こしセッション。再開されるまで音声ファイルを保持する。"""
    session: SessionAudio
    expiry: asyncio.Task


# モバイル回線の瞬断などで WebSocket が切れても、同じセッションIDでの再接続を
# 一定時間だけ受け付ける（session_id → 保持中のセッション）。口述は session を
# 持たないためここには入らない。
_PENDING_SESSIONS: dict[str, _PendingSession] = {}
_RESUME_GRACE_SEC = 90.0


def _hold_session_for_resume(session: SessionAudio) -> None:
    """finish_input を経ずに切断された録音を、再接続に備えて一定時間だけ保持する。"""
    session_id = session.session_id

    async def _expire() -> None:
        try:
            await asyncio.sleep(_RESUME_GRACE_SEC)
        except asyncio.CancelledError:
            return
        pending = _PENDING_SESSIONS.pop(session_id, None)
        if pending is not None:
            pending.session.close()
            print(f"[VoxCraft] 再接続待ちのセッションを終了: {session_id}")

    _PENDING_SESSIONS[session_id] = _PendingSession(session, asyncio.create_task(_expire()))
    print(f"[VoxCraft] 接続切断: セッション{session_id}を{_RESUME_GRACE_SEC:.0f}秒だけ再開可能に保持")


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
    dictionary_snapshot: DictionarySnapshot | None = None
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
    system_device_label = ""  # 無音警告に出す取得先の名前
    input_finished = False
    finish_lock = asyncio.Lock()
    previous_transcript_text = ""
    pending_contextual_chunk: dict | None = None
    # 口述チャンクの通し番号。probe（先読み）と chunk（本命）を同じ番号で結び、
    # クライアントが「先読みでコマンドとして処理済み」の chunk を捨てられるようにする。
    chunk_seq = 0
    refinement_planner = RefinementPlanner(
        config.sample_rate,
        config.system_refine_window_sec,
        config.system_refine_min_sec,
    )
    refinement_delivered_end = 0

    await ws.send_text(json.dumps({"type": "ready"}))

    async def deliver_chunk(prepared: dict) -> None:
        """認識・文脈判定済みの1チャンクを順序どおりクライアントへ返す。"""
        nonlocal previous_transcript_text
        text = prepared["text"]
        result = prepared["result"]
        start = prepared["start"]
        end = prepared["end"]
        pause = prepared["pause"]
        recovery = prepared["recovery"]
        evidence = prepared["evidence"]

        if recovery:
            # 音声根拠を通過しても、Whisper側の確信度が低い結果や定型句は採用しない。
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
            # 除去位置を送って、クライアントが未認識マーカーを置くのを防ぐ。
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
                note_provisional_delivery(end, recovery)
            return
        if not text and not result.dropped:
            print(
                f"[VoxCraft] 空チャンク（認識結果なし）: "
                f"{start / config.sample_rate:.1f}-{end / config.sample_rate:.1f}秒 "
                f"({(end - start) / config.sample_rate:.1f}秒, reason={prepared['reason']})"
            )
            return
        if breaker is not None and not recovery:
            text = breaker.feed(text, pause) + text
        msg: dict = {"type": "chunk", "text": text}
        if prepared.get("seq") is not None:
            msg["seq"] = prepared["seq"]
        # 読みは口述のコマンド照合にしか使わない。誤変換されても音は残るので、
        # 表記の完全一致より当たる（「乳酸キャンセル」でもコマンドとして通る）。
        if mode == "dictation" and text:
            reading = to_reading(text)
            if reading:
                msg["reading"] = reading
        if recovery:
            msg["recovered"] = True
        if pause is not None:
            msg["pause"] = round(pause, 2)
        if session is not None:
            msg["session"] = session.session_id
            msg["start"] = round(start / config.sample_rate, 3)
            msg["end"] = round(end / config.sample_rate, 3)
        if result.dropped:
            msg["dropped"] = result.dropped
        await ws.send_text(json.dumps(msg))
        note_provisional_delivery(end, recovery)
        if text:
            previous_transcript_text = text

    async def resolve_pending_context(next_text: str | None) -> None:
        """保留した単独句を、両隣が揃えば判定し、末尾なら安全側で残す。"""
        nonlocal pending_contextual_chunk
        pending = pending_contextual_chunk
        if pending is None:
            return
        pending_contextual_chunk = None
        phrase = pending["artifact"]
        if next_text is not None and should_remove_between_context(
            phrase, previous_transcript_text, next_text
        ):
            pending["text"] = ""
            pending["result"].blocked.append(phrase)
            print(
                f"[VoxCraft] 前後文脈から定型句を除去: {phrase} "
                f"({pending['start'] / config.sample_rate:.1f}-"
                f"{pending['end'] / config.sample_rate:.1f}秒)"
            )
        await deliver_chunk(pending)

    async def emit_chunk(
        audio: np.ndarray,
        reason: str,
        start: int,
        end: int,
        pause: float | None = None,
        recovery: bool = False,
        evidence: SpeechEvidence | None = None,
        dictionary: DictionarySnapshot | None = None,
    ) -> None:
        nonlocal pending_contextual_chunk, chunk_seq
        if dictionary is None:
            raise RuntimeError("辞書スナップショットがありません")
        # 発話を検出しチャンクを確定 → 認識開始を通知（クライアントで「認識中…」表示）。
        await ws.send_text(json.dumps({"type": "partial", "reason": reason}))
        chunk_seq += 1
        seq = chunk_seq

        # コマンドの先読み。口述の短いチャンクだけ、本命の認識より先に
        # 小さいモデルで一度読んで送る（実測 base で125ms前後）。
        # ここで送るのは判断材料だけで、採用するかはクライアントが決める。
        # 外れた場合の代償は本文が 125ms 遅れることだけ（結果は捨てる）。
        if (
            config.command_probe
            and mode == "dictation"
            and not recovery
            and audio.size <= config.command_probe_max_sec * config.sample_rate
        ):
            try:
                probe = await _probe_chunk(audio, dictionary)
                if probe.text:
                    _log_chunk("probe", seq, probe.text, probe.text)
                    await ws.send_text(json.dumps({
                        "type": "probe",
                        "seq": seq,
                        "text": probe.text,
                        "reading": to_reading(probe.text) or "",
                    }))
            except Exception as exc:  # noqa: BLE001 — 先読みは補助。落ちても本文は流す。
                print(f"[VoxCraft] コマンド先読みに失敗（無視して続行）: {exc}")

        # mode は下のコマンド処理で書き換わる。ここは常に現在のモードを見る。
        chunk_opts = AsrOptions.recovery() if recovery else opts
        result = await _transcribe_chunk(
            audio, chunk_opts, dictionary, hq=(mode == "transcribe")
        )
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
            # 括弧の文中変換は口述だけ。録音の書き起こしを黙って書き換えない。
            inline_symbols=(
                symbols and mode == "dictation" and config.enable_inline_symbols
            ),
            replacements=dictionary.replacement_plan,
            symbols=dictionary.symbols,
            auto_punctuate=punctuate,
        )
        _log_chunk(
            "recovery" if recovery else mode,
            seq,
            result.text,
            text,
            (end - start) / config.sample_rate,
        )
        prepared = {
            "text": text, "result": result, "reason": reason,
            "start": start, "end": end, "pause": pause,
            "recovery": recovery, "evidence": evidence, "seq": seq,
        }
        await resolve_pending_context(text or None)
        artifact = standalone_contextual_artifact(text)
        if mode == "transcribe" and not recovery and artifact:
            # 次チャンクを受け取るまで、この句だけを保留する。通常チャンクの遅延はない。
            prepared["artifact"] = artifact
            pending_contextual_chunk = prepared
            return
        await deliver_chunk(prepared)

    async def emit_refinement(job: dict) -> None:
        """30秒前後のPC音声を再認識し、同じ範囲の差し替えを送る。"""
        audio = job["audio"]
        span: RefinementRange = job["range"]
        dictionary: DictionarySnapshot = job["dictionary"]
        result = await _transcribe_chunk(
            audio, AsrOptions.refinement(), dictionary, hq=True
        )
        raw_text = result.text
        if raw_text:
            evidence = speech_evidence(
                audio,
                config.sample_rate,
                active_rms=config.retry_active_rms,
            )
            raw_text, contextual_blocked = filter_contextual_artifacts(
                raw_text,
                evidence,
                weak_rms=config.retry_min_rms,
                weak_active_ratio=config.retry_active_ratio,
            )
            result.blocked.extend(contextual_blocked)
        text = postprocess(
            raw_text,
            strip_space=strip_space,
            # 動画中の「まる」を句点命令として扱わない。
            symbol_dictation=False,
            replacements=dictionary.replacement_plan,
            symbols=dictionary.symbols,
            auto_punctuate=punctuate,
        )
        if not text:
            print(
                f"[VoxCraft] PC音声の補正を不採用（空結果）: "
                f"{span.start / config.sample_rate:.1f}-"
                f"{span.end / config.sample_rate:.1f}秒"
            )
            return
        await ws.send_text(json.dumps({
            "type": "refinement",
            "text": text,
            "session": job["session"],
            "start": round(span.start / config.sample_rate, 3),
            "end": round(span.end / config.sample_rate, 3),
            "revision": span.revision,
        }))
        print(
            f"[VoxCraft] PC音声を補正: revision={span.revision} "
            f"{span.start / config.sample_rate:.1f}-"
            f"{span.end / config.sample_rate:.1f}秒 ({len(text)}字)"
        )

    # 認識は受信ループから切り離してキューで回す。
    # 直列に await すると、認識中（数秒）は ws.receive() が呼ばれず音声が溜まり、
    # 終わった瞬間にまとめて流れ込む＝結果が塊で出る。キューにすれば受信は止まらない。
    # 認識自体はモデルのロックで直列化されるため、ワーカーは1本で足りる（順序も保たれる）。
    queue: asyncio.Queue = asyncio.Queue()

    def schedule_refinement(*, flush: bool = False) -> None:
        """補正範囲の音声をリングバッファからコピーして認識キューへ積む。"""
        if (
            not config.system_refine_enabled
            or mode != "transcribe"
            or source not in SYSTEM_SOURCES
            or session is None
            or dictionary_snapshot is None
        ):
            return
        span = (
            refinement_planner.flush(refinement_delivered_end)
            if flush
            else refinement_planner.ready(refinement_delivered_end)
        )
        if span is None:
            return
        audio = audio_ring.slice(span.start, span.end)
        if audio is None:
            print(
                f"[VoxCraft] PC音声の補正範囲がバッファ外: "
                f"{span.start / config.sample_rate:.1f}-"
                f"{span.end / config.sample_rate:.1f}秒"
            )
            return
        queue.put_nowait({
            "kind": "refinement",
            "audio": audio,
            "range": span,
            "session": session.session_id,
            "dictionary": dictionary_snapshot,
        })

    def note_provisional_delivery(end: int, recovery: bool) -> None:
        """クライアントが置換可能になった速報範囲だけを補正対象へ進める。"""
        nonlocal refinement_delivered_end
        if recovery or mode != "transcribe" or source not in SYSTEM_SOURCES:
            return
        refinement_delivered_end = max(refinement_delivered_end, end)
        schedule_refinement()

    async def worker() -> None:
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                if isinstance(item, dict) and item.get("kind") == "refinement":
                    await emit_refinement(item)
                else:
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
        if dictionary_snapshot is None:
            raise RuntimeError("辞書スナップショットがありません")
        for c in chunks:
            queue.put_nowait((
                c.audio, c.reason, c.start, c.end, c.pause, False, None,
                dictionary_snapshot,
            ))

    def put_recovery(audio: np.ndarray, start: int, end: int, evidence: SpeechEvidence) -> None:
        if dictionary_snapshot is None:
            raise RuntimeError("辞書スナップショットがありません")
        queue.put_nowait((
            audio, "auto_recovery", start, end, None, True, evidence,
            dictionary_snapshot,
        ))

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

    async def start_system_audio(device_name: str = "") -> dict:
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

        capture = WasapiLoopbackCapture(
            config.sample_rate, on_audio, on_error, device_name or None
        )
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
            # 末尾に本物の単独発話が来た可能性を守るため、次文脈が無ければ残す。
            await resolve_pending_context(None)
            # 最後の速報位置が確定してから端数を補正し、その結果まで返して停止する。
            schedule_refinement(flush=True)
            await queue.join()
            await ws.send_text(json.dumps({"type": "stopped", "reason": reason}))

    async def watch_system_silence() -> None:
        """PC出力の実音が既定時間来なければ文字起こしを完了する。

        自動停止（既定300秒）まで黙っていると、取得先を間違えた録音は何も
        残らないまま終わる。開始から一度も音が来ていない場合だけ、手前で一度
        警告を出して選び直せるようにする。
        """
        warn_sec = config.transcribe_silent_warn_sec
        warned = False
        while silence_tracker is not None and not input_finished:
            now = asyncio.get_running_loop().time()
            remaining = silence_tracker.remaining(now)
            if remaining <= 0:
                print(
                    f"[VoxCraft] PC音声が{config.transcribe_auto_stop_sec:.0f}秒無音のため自動停止"
                )
                await finish_input("silence")
                return
            if not warned and silence_tracker.silent_since_start(now, warn_sec):
                warned = True
                where = f"「{system_device_label}」" if system_device_label else "PC音声"
                print(f"[VoxCraft] {where}から{warn_sec:.0f}秒間まったく音が来ていない")
                await ws.send_text(json.dumps({
                    "type": "warning",
                    "code": "no_audio",
                    "device": system_device_label,
                    "message": (
                        f"{where}から音が来ていません。"
                        "再生先と取得先が食い違っていないか確認してください。"
                    ),
                }))
            # 警告予定時刻を跨いで眠らない。300秒先の自動停止だけを見ていると
            # 5秒刻みになり、20秒の警告が最大25秒までずれる。
            wait = min(5.0, max(0.1, remaining))
            if not warned and warn_sec > 0:
                until_warn = silence_tracker.started_at + warn_sec - now
                if until_warn > 0:
                    wait = min(wait, max(0.1, until_warn))
            await asyncio.sleep(wait)

    async def apply_start(cmd: dict, *, resume_session: SessionAudio | None = None) -> None:
        """{"type": "start"} と {"type": "resume"} で共用するセッション初期化。

        resume_session が渡されたときは、既存の SessionAudio（同じWAVファイル）を
        そのまま使い続ける ＝ 新規セッションを作らない。それ以外はすべて通常の
        start と同じ初期化を行う（切断前後で認識器の内部状態を厳密に引き継ぐ
        必要はない。受信音声は途切れなくWAVへ積まれているので、復旧コマンドで
        取りこぼしを埋められる）。
        """
        nonlocal dictionary_snapshot, session, mode, source, chunker, joiner, breaker
        nonlocal opts, punctuate, silence_tracker, auto_stop_task, strip_space, symbols
        nonlocal input_finished, previous_transcript_text, pending_contextual_chunk
        nonlocal refinement_delivered_end, audio_ring, last_source_chunk_end
        nonlocal system_device_label

        requested_dictionary_set = cmd.get("dictionarySetId", "default")
        if not isinstance(requested_dictionary_set, str) or not requested_dictionary_set:
            await ws.send_text(json.dumps({
                "type": "error",
                "message": "dictionarySetId が不正です。",
                "fatal": True,
            }))
            return
        try:
            resolved_dictionary = get_dictionary_snapshot(requested_dictionary_set)
        except (DictionarySchemaError, OSError) as exc:
            await ws.send_text(json.dumps({
                "type": "error",
                "message": f"辞書セットを解決できません: {exc}",
                "fatal": True,
            }))
            return
        # この代入以後、ファイルが変更されてもキュー済みジョブは
        # resolved_dictionary 自体を保持するためセッション内で混在しない。
        dictionary_snapshot = resolved_dictionary
        created_session = False
        input_finished = False
        previous_transcript_text = ""
        pending_contextual_chunk = None
        refinement_planner.reset()
        refinement_delivered_end = 0
        silence_tracker = None
        audio_ring = AudioRingBuffer(
            max(1, int(max(
                config.retry_buffer_sec,
                # 認識待ちが重なっても、補正対象の音声を失わない余裕を持つ。
                config.system_refine_window_sec * 4,
            ) * config.sample_rate))
        )
        last_source_chunk_end = 0
        strip_space = bool(cmd.get("stripSpace", config.strip_ja_alnum_space))
        symbols = bool(cmd.get("symbols", config.enable_symbol_dictation))
        mode = "transcribe" if cmd.get("mode") == "transcribe" else "dictation"
        if resume_session is not None:
            # 再開は文字起こしセッションにしか存在しない。
            mode = "transcribe"
        requested_source = cmd.get("source", "microphone")
        source = (
            requested_source
            if requested_source in SYSTEM_SOURCES
            else "microphone"
        )
        if source in SYSTEM_SOURCES and mode != "transcribe":
            await ws.send_text(json.dumps({
                "type": "error",
                "message": "PC音声入力は文字起こしモードでのみ使用できます。",
                "fatal": True,
            }))
            return
        chunker = _build_chunker(mode, source)
        if mode == "transcribe":
            opts = AsrOptions.transcription()
            system_mode = source in SYSTEM_SOURCES
            joiner = ChunkJoiner(
                sample_rate=config.sample_rate,
                min_sec=(
                    config.system_join_sec if system_mode
                    else config.transcribe_join_sec
                ),
                max_hold_sec=(
                    config.system_join_hold_sec if system_mode
                    else config.transcribe_join_hold_sec
                ),
                break_sec=(
                    config.system_join_break_sec if system_mode
                    else config.transcribe_join_break_sec
                ),
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
            if resume_session is not None:
                # 同じWAVファイルへ引き続き追記する。新規セッションは作らない。
                session = resume_session
                print(f"[VoxCraft] 文字起こしモード: 録音を再開 {session.path}")
            elif session is None:
                # 復旧の素材として、このセッションの音声を丸ごと保存する。
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
        # device は system なら「サーバー機のどの入力先を開くか」、system-client なら
        # 「クライアントが開いた入力先の表示名」。null / 非文字列でも落ちないように
        # し、長さも切る（後者はログとステータス表示に出るだけの値）。
        requested_device = str(cmd.get("device") or "").strip()[:120]
        if source == "system":
            try:
                source_info = await start_system_audio(requested_device)
            except (SystemAudioError, OSError) as exc:
                if session is not None:
                    if created_session:
                        failed_session = session.session_id
                        session.close()
                        session = None
                        try:
                            delete_session(failed_session)
                        except (ValueError, FileNotFoundError, OSError):
                            pass
                    elif resume_session is not None:
                        # 再開の途中で失敗。録音済みの音声は消さず、確定するだけに留める。
                        session.close()
                        session = None
                await ws.send_text(json.dumps({
                    "type": "error", "message": str(exc), "fatal": True,
                }))
                return
            except Exception as exc:  # デバイス固有の PortAudio エラーも表示する
                if session is not None:
                    if created_session:
                        failed_session = session.session_id
                        session.close()
                        session = None
                        try:
                            delete_session(failed_session)
                        except (ValueError, FileNotFoundError, OSError):
                            pass
                    elif resume_session is not None:
                        session.close()
                        session = None
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": f"PC音声入力を開始できません: {exc}",
                    "fatal": True,
                }))
                return
        elif source == "system-client":
            # 音声はクライアントがバイナリで送ってくる。こちらは開くものが
            # 無いので、表示用のデバイス名を受け取って返すだけ。
            client_device = requested_device
            if client_device:
                source_info["device"] = client_device
            print(
                f"[VoxCraft] PC音声入力(クライアント側): "
                f"{client_device or 'デバイス名なし'}"
            )

        system_device_label = str(source_info.get("device", "") or "")
        if source in SYSTEM_SOURCES and config.transcribe_auto_stop_sec > 0:
            silence_tracker = SilenceTracker(
                config.transcribe_auto_stop_sec,
                config.transcribe_audible_rms,
                asyncio.get_running_loop().time(),
            )
            auto_stop_task = asyncio.create_task(watch_system_silence())
            source_info["autoStopSec"] = config.transcribe_auto_stop_sec
        await ws.send_text(json.dumps({
            "type": "started",
            "source": source,
            **source_info,
            **dictionary_snapshot.metadata(),
        }))
        print(
            f"[VoxCraft] 辞書セット固定: {dictionary_snapshot.set_id} "
            f"revision={dictionary_snapshot.revision} "
            f"profiles={','.join(dictionary_snapshot.profile_ids)}"
        )

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
                    await apply_start(cmd)

                elif ctype == "resume":
                    # モバイル回線の瞬断などで切れた直後の再接続。同じセッションIDの
                    # 音声ファイルへ引き続き追記し、テキストの続きとして扱う。
                    session_id = cmd.get("session")
                    if not isinstance(session_id, str) or not session_id:
                        await ws.send_text(json.dumps({
                            "type": "error", "message": "session が不正です。", "fatal": True,
                        }))
                        continue
                    pending = _PENDING_SESSIONS.pop(session_id, None)
                    if pending is None:
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "message": "この録音は再開できません（時間切れ、または既に再開済みです）。",
                            "fatal": True,
                        }))
                        continue
                    pending.expiry.cancel()
                    await apply_start(cmd, resume_session=pending.session)

                elif ctype == "stop":
                    await finish_input("manual")

                elif ctype == "reconvert":
                    active_dictionary = dictionary_snapshot or get_dictionary_snapshot("default")
                    payload = await asyncio.to_thread(
                        reconvert,
                        cmd.get("text", ""),
                        active_dictionary.reverse_replacements,
                    )
                    await ws.send_text(json.dumps({
                        "type": "reconvert",
                        **payload,
                        **active_dictionary.metadata(),
                    }))

                elif ctype == "tune":
                    # 一時的な寄せ替え。口述本体の既定値は触らず、用が済んだら必ず戻す。
                    #   fast=true … 候補モーダル中。短い発話に速く応える側へ。
                    #   word=true … 言い直し待ち。文脈のない単語1つを正しく取る側へ。
                    # どちらも解除は同じメッセージのフラグを下ろすこと。
                    if mode == "dictation":
                        if bool(cmd.get("fast", False)):
                            chunker.set_silence_sec(min(config.silence_sec, 0.25))
                            chunker.set_min_speech_sec(0.15)
                            opts = AsrOptions.command()
                            punctuate = False
                        elif bool(cmd.get("word", False)):
                            # 区切りは口述のまま。単語だけを短く言うので、
                            # ノイズ扱いで落とされないよう最小発話長だけ緩める。
                            chunker.set_silence_sec(config.silence_sec)
                            chunker.set_min_speech_sec(min(config.min_speech_sec, 0.15))
                            opts = AsrOptions.word()
                            # 単語に句点を付けない（「寄与。」を本文へ入れないため）。
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
        if session is not None:
            if mode == "transcribe" and not input_finished:
                # stop を経ずに切れた＝ネットワーク瞬断の可能性。すぐには確定せず、
                # 同じセッションIDでの再接続を一定時間だけ待つ。
                _hold_session_for_resume(session)
            else:
                # 録音は必ず閉じる（異常終了しても、そこまでの音声は再認識に使える）。
                session.close()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
