// 認識サーバー（自宅PC）との WebSocket クライアント。
// Desktop では ws://localhost:8760/ws、Android では ws://<Tailscale IP>:8760/ws を想定。

export type AsrMode = "dictation" | "transcribe";

export interface ServerMessage {
    type: "ready" | "chunk" | "stopped" | "reconvert" | "error" | "partial" | "session";
    text?: string;
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
}

export interface WsHandlers {
    onReady?: () => void;
    onPartial?: () => void; // 発話検出→認識開始の合図
    onChunk?: (text: string, msg: ServerMessage) => void;
    onSession?: (id: string) => void;
    onReconvert?: (msg: ServerMessage) => void;
    onError?: (message: string) => void;
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
            case "partial":
                this.handlers.onPartial?.();
                break;
            case "chunk":
                this.handlers.onChunk?.(msg.text || "", msg);
                break;
            case "session":
                if (msg.session) this.handlers.onSession?.(msg.session);
                break;
            case "reconvert":
                this.handlers.onReconvert?.(msg);
                break;
            case "error":
                this.handlers.onError?.(msg.message || "unknown error");
                break;
        }
    }

    sendAudio(pcm16: ArrayBuffer): void {
        if (this.connected) this.ws!.send(pcm16);
    }

    sendStart(stripSpace: boolean, symbols: boolean, mode: AsrMode = "dictation"): void {
        this.send({ type: "start", stripSpace, symbols, mode });
    }

    sendStop(): void {
        this.send({ type: "stop" });
    }

    sendReconvert(text: string): void {
        this.send({ type: "reconvert", text });
    }

    private send(obj: unknown): void {
        if (this.connected) this.ws!.send(JSON.stringify(obj));
    }

    close(): void {
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
}
