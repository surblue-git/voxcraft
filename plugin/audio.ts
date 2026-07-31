// マイク録音 → 16kHz / PCM16LE モノラルへのダウンサンプリング。
//
// UIメインスレッド非依存で音飛びを防ぐため AudioWorklet を優先使用。
// Blob URL により単一モジュール（main.js）のまま追加ファイルのロードなしで動作する。
// 万が一 AudioWorklet が使えない環境では ScriptProcessorNode にフォールバックする。

export type PcmHandler = (pcm16: ArrayBuffer) => void;
export type LevelHandler = (level: number) => void; // 0..1 の入力レベル

// この秒数ぶん PCM が途切れたら「止まっている」とみなす。
// AudioWorklet は 128 サンプル（約2.7ms）ごとに来るので、3秒は明らかな異常。
const STALL_MS = 3000;
const WATCHDOG_MS = 1000;

// ブラウザ側の音声前処理。既定（口述）は全部ONのまま。
// 会場PA越しの遠い声や残響には、近接した1人の声を想定したこれらの処理が
// 悪く働く（NSが残響ごと削り、AGCが音量を揺らす）ため、文字起こしでは切る。
export interface MicOptions {
    echoCancellation: boolean;
    noiseSuppression: boolean;
    autoGainControl: boolean;
}

export const DICTATION_MIC: MicOptions = {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
};

export const RAW_MIC: MicOptions = {
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: false,
};

const WORKLET_CODE = `
class VoxCraftAudioProcessor extends AudioWorkletProcessor {
    process(inputs, outputs, parameters) {
        const input = inputs[0];
        if (input && input.length > 0) {
            const channelData = input[0];
            if (channelData && channelData.length > 0) {
                // Float32Array のコピーをメッセージで送出
                this.port.postMessage(new Float32Array(channelData));
            }
        }
        return true;
    }
}
registerProcessor('voxcraft-audio-processor', VoxCraftAudioProcessor);
`;

export class MicRecorder {
    private ctx: AudioContext | null = null;
    private stream: MediaStream | null = null;
    private source: MediaStreamAudioSourceNode | null = null;
    private workletNode: AudioWorkletNode | null = null;
    private scriptProcessor: ScriptProcessorNode | null = null;
    private workletBlobUrl: string | null = null;
    private onPcm: PcmHandler;
    private onLevel: LevelHandler | null;
    private targetRate: number;
    private mic: MicOptions;

    // 録音が止まったことを知らせるための監視。無音のまま録れていた事故の再発防止で、
    // 「録音中の表示のまま実は死んでいる」状態を必ず外に出す。
    // onStalled: 今まさに止まっている（1回だけ）。onGap: 途切れが終わった（欠落秒数）。
    onStalled: (() => void) | null = null;
    onGap: ((seconds: number) => void) | null = null;
    private lastPcmAt = 0;
    private stalled = false;
    private watchdog: number | null = null;

    constructor(
        onPcm: PcmHandler,
        onLevel: LevelHandler | null = null,
        mic: MicOptions = DICTATION_MIC,
        targetRate = 16000
    ) {
        this.onPcm = onPcm;
        this.onLevel = onLevel;
        this.mic = mic;
        this.targetRate = targetRate;
    }

    get active(): boolean {
        return this.ctx !== null;
    }

    async start(): Promise<void> {
        if (this.ctx) return;
        this.stream = await navigator.mediaDevices.getUserMedia({
            audio: {
                channelCount: 1,
                echoCancellation: this.mic.echoCancellation,
                noiseSuppression: this.mic.noiseSuppression,
                autoGainControl: this.mic.autoGainControl,
            },
        });
        this.ctx = new AudioContext();
        this.source = this.ctx.createMediaStreamSource(this.stream);
        const inputRate = this.ctx.sampleRate;

        // AudioWorklet のロードを試みる
        let workletLoaded = false;
        if (this.ctx.audioWorklet) {
            try {
                const blob = new Blob([WORKLET_CODE], { type: "application/javascript" });
                const blobUrl = URL.createObjectURL(blob);
                this.workletBlobUrl = blobUrl;
                await this.ctx.audioWorklet.addModule(blobUrl);
                this.workletNode = new AudioWorkletNode(this.ctx, "voxcraft-audio-processor");

                this.workletNode.port.onmessage = (e: MessageEvent<Float32Array>) => {
                    this.handleInput(e.data, inputRate);
                };

                this.source.connect(this.workletNode);
                const mute = this.ctx.createGain();
                mute.gain.value = 0;
                this.workletNode.connect(mute);
                mute.connect(this.ctx.destination);
                workletLoaded = true;
            } catch (err) {
                console.warn("VoxCraft: AudioWorklet の起動に失敗したため ScriptProcessorNode へフォールバックします。", err);
                workletLoaded = false;
            }
        }

        // AudioWorklet 非対応、または起動失敗時は ScriptProcessorNode へフォールバック
        if (!workletLoaded) {
            this.scriptProcessor = this.ctx.createScriptProcessor(4096, 1, 1);
            this.scriptProcessor.onaudioprocess = (ev: AudioProcessingEvent) => {
                this.handleInput(ev.inputBuffer.getChannelData(0), inputRate);
            };

            this.source.connect(this.scriptProcessor);
            const mute = this.ctx.createGain();
            mute.gain.value = 0;
            this.scriptProcessor.connect(mute);
            mute.connect(this.ctx.destination);
        }

        this.lastPcmAt = Date.now();
        this.stalled = false;
        this.watchdog = window.setInterval(() => this.tick(), WATCHDOG_MS);
    }

    // PCM 1ブロックの処理。途切れの検出もここで行う。
    // 監視タイマーはバックグラウンドで間引かれるが、この経路は「実際に音が戻った
    // 瞬間」に必ず通るので、画面オフ中に空いた穴もその長さごと確実に拾える。
    private handleInput(input: Float32Array, inputRate: number): void {
        const now = Date.now();
        const gap = this.lastPcmAt ? now - this.lastPcmAt : 0;
        this.lastPcmAt = now;
        if (gap > STALL_MS) {
            this.stalled = false;
            this.onGap?.(gap / 1000);
        }
        if (this.onLevel) this.onLevel(rms(input));
        const down = downsample(input, inputRate, this.targetRate);
        this.onPcm(floatToPcm16(down));
    }

    // 止まったままになっていないかの定期確認（復帰も試みる）。
    private tick(): void {
        const ctx = this.ctx;
        if (!ctx) return;
        // 画面オフ等で suspend されたままなら起こし直す。
        if (ctx.state === "suspended") void ctx.resume().catch(() => undefined);
        if (this.stalled || Date.now() - this.lastPcmAt < STALL_MS) return;
        this.stalled = true;
        this.onStalled?.();
    }

    async stop(): Promise<void> {
        if (this.watchdog !== null) {
            window.clearInterval(this.watchdog);
            this.watchdog = null;
        }
        this.stalled = false;
        if (this.workletNode) {
            this.workletNode.port.onmessage = null;
            this.workletNode.disconnect();
            this.workletNode = null;
        }
        if (this.workletBlobUrl) {
            URL.revokeObjectURL(this.workletBlobUrl);
            this.workletBlobUrl = null;
        }
        if (this.scriptProcessor) {
            this.scriptProcessor.disconnect();
            this.scriptProcessor.onaudioprocess = null as unknown as never;
            this.scriptProcessor = null;
        }
        if (this.source) {
            this.source.disconnect();
            this.source = null;
        }
        if (this.stream) {
            this.stream.getTracks().forEach((t) => t.stop());
            this.stream = null;
        }
        if (this.ctx) {
            await this.ctx.close();
            this.ctx = null;
        }
    }
}

// 入力ブロックの RMS を 0..1 目安に写像（メーター表示用）。
function rms(input: Float32Array): number {
    let sum = 0;
    for (let i = 0; i < input.length; i++) sum += input[i] * input[i];
    return Math.sqrt(sum / input.length);
}

// 線形補間による簡易ダウンサンプリング（48k/44.1k → 16k）。
function downsample(input: Float32Array, inRate: number, outRate: number): Float32Array {
    if (outRate >= inRate) return input;
    const ratio = inRate / outRate;
    const outLen = Math.floor(input.length / ratio);
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) {
        const pos = i * ratio;
        const idx = Math.floor(pos);
        const frac = pos - idx;
        const a = input[idx];
        const b = idx + 1 < input.length ? input[idx + 1] : a;
        out[i] = a + (b - a) * frac;
    }
    return out;
}

// Float32(-1..1) → PCM16LE。
function floatToPcm16(input: Float32Array): ArrayBuffer {
    const buf = new ArrayBuffer(input.length * 2);
    const view = new DataView(buf);
    for (let i = 0; i < input.length; i++) {
        let s = Math.max(-1, Math.min(1, input[i]));
        s = s < 0 ? s * 0x8000 : s * 0x7fff;
        view.setInt16(i * 2, s, true);
    }
    return buf;
}
