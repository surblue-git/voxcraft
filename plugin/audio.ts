// マイク録音 → 16kHz / PCM16LE モノラルへのダウンサンプリング。
//
// Obsidian は Electron(Desktop) / WebView(Mobile) の Chromium 上で動くため
// getUserMedia + AudioContext が使える。AudioWorklet は別ファイルのロードが必要で
// モバイルで面倒なので、互換性重視で ScriptProcessorNode を使う（非推奨だが全環境で動く）。

export type PcmHandler = (pcm16: ArrayBuffer) => void;

export class MicRecorder {
    private ctx: AudioContext | null = null;
    private stream: MediaStream | null = null;
    private source: MediaStreamAudioSourceNode | null = null;
    private processor: ScriptProcessorNode | null = null;
    private onPcm: PcmHandler;
    private targetRate: number;

    constructor(onPcm: PcmHandler, targetRate = 16000) {
        this.onPcm = onPcm;
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

        // 4096 サンプルごとにコールバック。入力1ch、出力1ch。
        this.processor = this.ctx.createScriptProcessor(4096, 1, 1);
        const inputRate = this.ctx.sampleRate;

        this.processor.onaudioprocess = (ev: AudioProcessingEvent) => {
            const input = ev.inputBuffer.getChannelData(0);
            const down = downsample(input, inputRate, this.targetRate);
            this.onPcm(floatToPcm16(down));
        };

        this.source.connect(this.processor);
        // ScriptProcessor は destination に繋がないと発火しない環境がある。
        // 無音を出さないよう GainNode(0) を挟んで destination へ。
        const mute = this.ctx.createGain();
        mute.gain.value = 0;
        this.processor.connect(mute);
        mute.connect(this.ctx.destination);
    }

    async stop(): Promise<void> {
        if (this.processor) {
            this.processor.disconnect();
            this.processor.onaudioprocess = null as unknown as never;
            this.processor = null;
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
