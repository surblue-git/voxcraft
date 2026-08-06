// 認識サーバー（自宅PC）との WebSocket クライアント。
// Desktop では ws://localhost:8760/ws、Android では ws://<Tailscale IP>:8760/ws を想定。

export type AsrMode = "dictation" | "transcribe";
// system       = サーバー機の再生音をサーバー自身が WASAPI で取る
// system-client = この端末の再生音（ステレオミキサー等）を取って送る
export type AsrSource = "microphone" | "system" | "system-client";

// PC音声として扱う音源か（取得元が違うだけで、扱いはサーバー・クライアントとも共通）。
export function isSystemSource(source: AsrSource): boolean {
    return source === "system" || source === "system-client";
}

export interface StartResult {
    source: AsrSource;
    // サーバーが実際に遠いマイク用の連結を適用したか（設定が届かない事故に気づくため）。
    farMic: boolean;
    device?: string;
    inputSampleRate?: number;
    channels?: number;
    autoStopSec?: number;
    dictionarySetId: string;
    dictionarySetName: string;
    dictionaryRevision: string;
    dictionaryProfiles: string[];
    dictionaryProfileRevisions: Record<string, string>;
    dictionaryWritableProfile: string;
    dictionaryWarningCount: number;
    dictionaryWarnings: DictionaryDiagnostic[];
}

export interface DictionaryDiagnostic {
    severity: "warning" | "error";
    code: string;
    message: string;
    entry?: number;
}

export interface ServerMessage {
    type: "ready" | "started" | "chunk" | "refinement" | "stopped" |
        "reconvert" | "error" | "warning" | "partial" | "session" | "level" | "probe";
    code?: string; // warning の種別（"no_audio" 等）
    text?: string;
    // 口述チャンクの通し番号。probe（コマンド先読み）と chunk が同じ番号で対応する。
    seq?: number;
    reason?: string;
    reading?: string;
    segments?: { reading: string; candidates: string[] }[];
    online?: boolean;
    message?: string;
    // 文字起こしモードのみ: 録音セッションIDと、このチャンクに対応する音声の秒数範囲。
    // これがあると後から同じ音声を再認識して復旧できる。
    session?: string;
    start?: number;
    end?: number;
    dropped?: string[]; // サーバー側フィルタで捨てたテキスト（無言で消さないための通知）
    pause?: number; // 前チャンクの発話終わりからの無音（秒）。「息継ぎで読点」の判断材料
    source?: AsrSource;
    farMic?: boolean;
    device?: string;
    inputSampleRate?: number;
    channels?: number;
    autoStopSec?: number;
    dictionarySetId?: string;
    dictionarySetName?: string;
    dictionaryRevision?: string;
    dictionaryProfiles?: string[];
    dictionaryProfileRevisions?: Record<string, string>;
    dictionaryWritableProfile?: string;
    dictionaryWarningCount?: number;
    dictionaryWarnings?: DictionaryDiagnostic[];
    recovered?: boolean;
    revision?: number; // PC音声の範囲補正。大きい番号だけを適用して古い応答を無視する
    level?: number;
    fatal?: boolean;
}

export interface WsHandlers {
    onReady?: () => void;
    onPartial?: () => void; // 発話検出→認識開始の合図
    // 小さいモデルによるコマンド先読み。本文には使わない（精度が本命に劣る）。
    onProbe?: (text: string, msg: ServerMessage) => void;
    onChunk?: (text: string, msg: ServerMessage) => void;
    onRefinement?: (text: string, msg: ServerMessage) => void;
    onSession?: (id: string) => void;
    onReconvert?: (msg: ServerMessage) => void;
    onStopped?: (reason?: string) => void;
    onLevel?: (level: number) => void;
    onError?: (message: string, fatal: boolean) => void;
    // 録音は続くが注意が要る状態（取得先から音が来ていない等）。
    onWarning?: (message: string, code: string) => void;
    onClose?: () => void;
}

function safeClose(ws: WebSocket): void {
    try {
        ws.onopen = null;
        ws.onerror = null;
        ws.onclose = null;
        ws.onmessage = null;
        ws.close();
    } catch {
        /* noop */
    }
}

export class AsrSocket {
    private ws: WebSocket | null = null;
    private urls: string[];
    private handlers: WsHandlers;
    // レースで採用した URL（どの接続先が使われたかの表示に使う）。
    activeUrl: string | null = null;
    private pendingStart: {
        resolve: (result: StartResult) => void;
        reject: (error: Error) => void;
        timer: number;
    } | null = null;

    constructor(urls: string | string[], handlers: WsHandlers) {
        this.urls = (Array.isArray(urls) ? urls : [urls]).map((u) => u.trim()).filter(Boolean);
        this.handlers = handlers;
    }

    get connected(): boolean {
        return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
    }

    // 候補すべてへ同時接続を試み、最初に開いた接続を採用する（残りは破棄）。
    // 自宅ならLAN、外出先ならTailscaleが自然に勝つ。候補が1つならそのまま接続。
    connect(): Promise<void> {
        return new Promise((resolve, reject) => {
            const urls = this.urls;
            if (urls.length === 0) {
                reject(new Error("接続先が設定されていません"));
                return;
            }

            let settled = false;
            let pending = urls.length;
            const probes: WebSocket[] = [];

            const timer = window.setTimeout(() => {
                if (settled) return;
                settled = true;
                probes.forEach(safeClose);
                reject(new Error("接続タイムアウト"));
            }, 8000);

            const onFail = () => {
                pending -= 1;
                if (pending <= 0 && !settled) {
                    settled = true;
                    window.clearTimeout(timer);
                    reject(new Error("接続エラー"));
                }
            };

            for (const url of urls) {
                let ws: WebSocket;
                try {
                    ws = new WebSocket(url);
                } catch {
                    onFail();
                    continue;
                }
                ws.binaryType = "arraybuffer";
                probes.push(ws);

                ws.onopen = () => {
                    if (settled) {
                        safeClose(ws);
                        return;
                    }
                    settled = true;
                    window.clearTimeout(timer);
                    // 敗者（他候補）を閉じる。
                    for (const p of probes) if (p !== ws) safeClose(p);
                    this.adopt(ws, url);
                    resolve();
                };
                ws.onerror = () => {
                    if (settled) return;
                    onFail();
                };
                // 採用前の onclose では onClose ハンドラを呼ばない（adopt で張り直す）。
            }
        });
    }

    // レースの勝者を本採用し、メッセージ／切断ハンドラを張り直す。
    private adopt(ws: WebSocket, url: string): void {
        this.ws = ws;
        this.activeUrl = url;
        ws.onopen = null;
        ws.onerror = null;
        ws.onclose = () => {
            this.rejectStart("接続が切れました");
            this.handlers.onClose?.();
        };
        ws.onmessage = (ev: MessageEvent) => {
            if (typeof ev.data !== "string") return;
            let msg: ServerMessage;
            try {
                msg = JSON.parse(ev.data);
            } catch {
                return;
            }
            this.dispatch(msg);
        };
    }

    private dispatch(msg: ServerMessage): void {
        switch (msg.type) {
            case "ready":
                this.handlers.onReady?.();
                break;
            case "started": {
                const pending = this.pendingStart;
                if (pending) {
                    window.clearTimeout(pending.timer);
                    this.pendingStart = null;
                    pending.resolve({
                        source: msg.source ?? "microphone",
                        farMic: Boolean(msg.farMic),
                        device: msg.device,
                        inputSampleRate: msg.inputSampleRate,
                        channels: msg.channels,
                        autoStopSec: msg.autoStopSec,
                        dictionarySetId: msg.dictionarySetId ?? "default",
                        dictionarySetName: msg.dictionarySetName ?? "共通",
                        dictionaryRevision: msg.dictionaryRevision ?? "",
                        dictionaryProfiles: msg.dictionaryProfiles ?? ["common"],
                        dictionaryProfileRevisions: msg.dictionaryProfileRevisions ?? {},
                        dictionaryWritableProfile: msg.dictionaryWritableProfile ?? "common",
                        dictionaryWarningCount: msg.dictionaryWarningCount ?? 0,
                        dictionaryWarnings: msg.dictionaryWarnings ?? [],
                    });
                }
                break;
            }
            case "partial":
                this.handlers.onPartial?.();
                break;
            case "probe":
                this.handlers.onProbe?.(msg.text || "", msg);
                break;
            case "chunk":
                this.handlers.onChunk?.(msg.text || "", msg);
                break;
            case "refinement":
                this.handlers.onRefinement?.(msg.text || "", msg);
                break;
            case "session":
                if (msg.session) this.handlers.onSession?.(msg.session);
                break;
            case "reconvert":
                this.handlers.onReconvert?.(msg);
                break;
            case "stopped":
                this.handlers.onStopped?.(msg.reason);
                break;
            case "level":
                if (typeof msg.level === "number") this.handlers.onLevel?.(msg.level);
                break;
            case "warning":
                this.handlers.onWarning?.(msg.message || "", msg.code || "");
                break;
            case "error":
                if (msg.fatal) this.rejectStart(msg.message || "PC音声入力を開始できません");
                this.handlers.onError?.(msg.message || "unknown error", Boolean(msg.fatal));
                break;
        }
    }

    sendAudio(pcm16: ArrayBuffer): void {
        if (this.connected) this.ws!.send(pcm16);
    }

    sendStart(
        stripSpace: boolean,
        symbols: boolean,
        mode: AsrMode = "dictation",
        source: AsrSource = "microphone",
        device?: string,
        dictionarySetId = "default",
        farMic = false
    ): Promise<StartResult> {
        if (!this.connected) return Promise.reject(new Error("サーバーに接続されていません"));
        this.rejectStart("別の開始要求に置き換えられました");
        return new Promise((resolve, reject) => {
            const timer = window.setTimeout(() => {
                if (!this.pendingStart) return;
                this.pendingStart = null;
                reject(new Error("音声入力の開始がタイムアウトしました"));
            }, 15000);
            this.pendingStart = { resolve, reject, timer };
            // device は system-client のときだけ意味を持つ（表示名をそのまま返す）。
            // farMic はマイク入力の文字起こしにだけ効く（サーバー側で判定する）。
            this.send({
                type: "start", stripSpace, symbols, mode, source, device,
                dictionarySetId, farMic,
            });
        });
    }

    // モバイル回線の瞬断などで切れた直後の再接続用。start と違い、既存の
    // セッションID（＝サーバー側の同じ録音ファイル）へそのまま続きを積む。
    // サーバーは切断から一定時間だけ同じセッションを保持している。
    sendResume(
        session: string,
        stripSpace: boolean,
        symbols: boolean,
        source: AsrSource = "microphone",
        device?: string,
        dictionarySetId = "default",
        farMic = false
    ): Promise<StartResult> {
        if (!this.connected) return Promise.reject(new Error("サーバーに接続されていません"));
        this.rejectStart("別の開始要求に置き換えられました");
        return new Promise((resolve, reject) => {
            const timer = window.setTimeout(() => {
                if (!this.pendingStart) return;
                this.pendingStart = null;
                reject(new Error("録音の再開がタイムアウトしました"));
            }, 15000);
            this.pendingStart = { resolve, reject, timer };
            // 再接続でも連結器を作り直すので、farMic は毎回送る必要がある。
            this.send({
                type: "resume", session, stripSpace, symbols,
                mode: "transcribe", source, device, dictionarySetId, farMic,
            });
        });
    }

    sendStop(): void {
        this.send({ type: "stop" });
    }

    sendReconvert(text: string): void {
        this.send({ type: "reconvert", text });
    }

    // 候補選択中だけ応答速度優先へ切り替える（false で口述の既定値に戻す）。
    sendTune(fast: boolean): void {
        this.send({ type: "tune", fast });
    }

    // 言い直し待ちの間だけ、文脈のない単語1つを取りやすい設定へ切り替える。
    // false で口述の既定値へ戻す。
    sendTuneWord(word: boolean): void {
        this.send({ type: "tune", word });
    }

    private send(obj: unknown): void {
        if (this.connected) this.ws!.send(JSON.stringify(obj));
    }

    close(): void {
        this.rejectStart("接続を閉じました");
        if (this.ws) {
            this.ws.onclose = null;
            try {
                this.ws.close();
            } catch {
                /* noop */
            }
            this.ws = null;
        }
    }

    private rejectStart(message: string): void {
        const pending = this.pendingStart;
        if (!pending) return;
        window.clearTimeout(pending.timer);
        this.pendingStart = null;
        pending.reject(new Error(message));
    }
}
