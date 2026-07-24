// マイク録音 → 16kHz / PCM16LE モノラルへのダウンサンプリング。
//
// UIメインスレッド非依存で音飛びを防ぐため AudioWorklet を優先使用。
// Blob URL により単一モジュール（main.js）のまま追加ファイルのロードなしで動作する。
// 万が一 AudioWorklet が使えない環境では ScriptProcessorNode にフォールバックする。

export type PcmHandler = (pcm16: ArrayBuffer) => void;
export type LevelHandler = (level: number) => void; // 0..1 の入力レベル

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

    constructor(onPcm: PcmHandler, onLevel: LevelHandler | null = null, targetRate = 16000) {
        this.onPcm = onPcm;
        this.onLevel = onLevel;
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
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
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
                    const input = e.data;
                    if (this.onLevel) this.onLevel(rms(input));
                    const down = downsample(input, inputRate, this.targetRate);
                    this.onPcm(floatToPcm16(down));
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
                const input = ev.inputBuffer.getChannelData(0);
                if (this.onLevel) this.onLevel(rms(input));
                const down = downsample(input, inputRate, this.targetRate);
                this.onPcm(floatToPcm16(down));
            };

            this.source.connect(this.scriptProcessor);
            const mute = this.ctx.createGain();
            mute.gain.value = 0;
            this.scriptProcessor.connect(mute);
            mute.connect(this.ctx.destination);
        }
    }

    async stop(): Promise<void> {
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
