// 認識サーバー（自宅PC）との WebSocket クライアント。
// Desktop では ws://localhost:8760/ws、Android では ws://<Tailscale IP>:8760/ws を想定。

export interface ServerMessage {
    type: "ready" | "chunk" | "stopped" | "reconvert" | "error" | "partial";
    text?: string;
    reason?: string;
    reading?: string;
    segments?: { reading: string; candidates: string[] }[];
    online?: boolean;
    message?: string;
}

export interface WsHandlers {
    onReady?: () => void;
    onChunk?: (text: string) => void;
    onReconvert?: (msg: ServerMessage) => void;
    onError?: (message: string) => void;
    onClose?: () => void;
}

export class AsrSocket {
    private ws: WebSocket | null = null;
    private url: string;
    private handlers: WsHandlers;

    constructor(url: string, handlers: WsHandlers) {
        this.url = url;
        this.handlers = handlers;
    }

    get connected(): boolean {
        return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
    }

    connect(): Promise<void> {
        return new Promise((resolve, reject) => {
            try {
                this.ws = new WebSocket(this.url);
            } catch (e) {
                reject(e);
                return;
            }
            this.ws.binaryType = "arraybuffer";

            const timer = window.setTimeout(() => {
                reject(new Error("接続タイムアウト"));
                this.close();
            }, 8000);

            this.ws.onopen = () => {
                window.clearTimeout(timer);
                resolve();
            };
            this.ws.onerror = () => {
                window.clearTimeout(timer);
                reject(new Error("接続エラー"));
            };
            this.ws.onclose = () => {
                this.handlers.onClose?.();
            };
            this.ws.onmessage = (ev: MessageEvent) => {
                if (typeof ev.data !== "string") return;
                let msg: ServerMessage;
                try {
                    msg = JSON.parse(ev.data);
                } catch {
                    return;
                }
                this.dispatch(msg);
            };
        });
    }

    private dispatch(msg: ServerMessage): void {
        switch (msg.type) {
            case "ready":
                this.handlers.onReady?.();
                break;
            case "chunk":
                if (msg.text) this.handlers.onChunk?.(msg.text);
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

    sendStart(stripSpace: boolean, symbols: boolean): void {
        this.send({ type: "start", stripSpace, symbols });
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
