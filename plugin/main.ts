import { Editor, MarkdownView, Menu, Notice, Platform, Plugin } from "obsidian";
import { EditorView } from "@codemirror/view";

import { DICTATION_MIC, MicRecorder, RAW_MIC } from "./audio";
import { parseCommand } from "./commands";
import { DictModal } from "./dict";
import { ReconvertModal } from "./suggest";
import {
    AUTO,
    DEFAULT_SETTINGS,
    labelForUrl,
    migrateSettings,
    resolveUrls,
    VoxCraftSettingTab,
    VoxCraftSettings,
} from "./settings";
import {
    AudioSpan,
    RecoverModal,
    formatTime,
    recognizeRange,
    spanRangeFor,
} from "./recover";
import { RecordingsModal } from "./recordings";
import { AsrMode, AsrSocket, ServerMessage } from "./ws";
import { anchorExtension, setAnchor, clearAnchor, getAnchor } from "./anchor";

// 右クリック相当（Mac では Ctrl+クリックも）か。メニュー用の操作なので録音トグルからは除外する。
function isSecondaryClick(evt: MouseEvent): boolean {
    return evt.button !== 0 || (Platform.isMacOS && evt.ctrlKey);
}

export default class VoxCraftPlugin extends Plugin {
    settings: VoxCraftSettings;

    private socket: AsrSocket | null = null;
    private recorder: MicRecorder | null = null;
    private recording = false;
    private statusEl: HTMLElement;
    private ribbonEl: HTMLElement;

    // 口述対象として録音開始時に固定するエディタ（背面作業やノート切替の影響を受けない）。
    private cm: EditorView | null = null;
    // アンカーに追記したチャンク列（取り消し・変換戻しの対象特定に使う。アンカー相対）。
    private chunks: string[] = [];
    // 変換戻しの応答が返るまで対象範囲を覚えておく。
    private pendingReconvert: { from: number; to: number } | null = null;
    private reconvertModal: ReconvertModal | null = null;

    // 入力レベルメーター表示のスロットリング用。
    private lastMeterAt = 0;
    private transcribing = false;

    // 接続先メニューを開いた時刻。直後に飛んでくるクリックで録音を始めないための目印。
    private menuOpenedAt = 0;

    // ---- 文字起こしモード（自分の声での口述には一切関与しない） ----
    private mode: AsrMode = "dictation";
    // サーバーが音声を保存しているセッションID。復旧の材料。
    private session: string | null = null;
    // 挿入したテキストと、その元になった音声の位置（秒）。再認識の宛先を引くのに使う。
    private spans: AudioSpan[] = [];
    // 直前チャンクの音声終端。次チャンクとの間が空いていれば、そこは捨てられた音声。
    private lastAudioEnd = 0;

    async onload(): Promise<void> {
        await this.loadSettings();

        // 口述アンカー（CM6 拡張）を全エディタに登録。
        this.registerEditorExtension(anchorExtension);

        this.ribbonEl = this.addRibbonIcon("mic", "VoxCraft 音声入力の開始/停止", (evt) => {
            // 右クリック（Macは Ctrl+クリック）はメニュー専用。長押し直後の合成クリックも弾く。
            if (isSecondaryClick(evt)) return;
            if (Date.now() - this.menuOpenedAt < 700) return;
            this.toggleRecording();
        });
        // マイクのリボンを右クリック（モバイルは長押し）で接続先メニューを出す。
        this.registerDomEvent(this.ribbonEl, "contextmenu", (evt: MouseEvent) => {
            evt.preventDefault();
            this.menuOpenedAt = Date.now();
            this.openEndpointMenu(evt);
        });
        // Obsidian 本体のリボン用ハンドラが押下系イベントで発火する場合に備え、
        // 副ボタンのイベントはリボンに届く前（親のキャプチャ段階）で止める。
        const swallowSecondary = (evt: MouseEvent) => {
            const target = evt.target;
            if (!(target instanceof Node) || !this.ribbonEl.contains(target)) return;
            if (isSecondaryClick(evt)) evt.stopPropagation();
        };
        const ribbonParent = this.ribbonEl.parentElement ?? this.ribbonEl;
        this.registerDomEvent(ribbonParent, "mousedown", swallowSecondary, { capture: true });
        this.registerDomEvent(ribbonParent, "mouseup", swallowSecondary, { capture: true });
        this.registerDomEvent(ribbonParent, "auxclick", swallowSecondary, { capture: true });

        this.statusEl = this.addStatusBarItem();
        this.setStatus("停止中");

        this.addCommand({
            id: "toggle-recording",
            name: "音声入力の開始/停止",
            callback: () => this.toggleRecording(),
        });
        this.addCommand({
            id: "stop-recording",
            name: "音声入力を停止",
            callback: () => this.stopRecording(),
        });
        this.addCommand({
            id: "toggle-transcribe",
            name: "文字起こし（動画・会議）の開始/停止",
            callback: () => {
                if (this.recording) this.stopRecording();
                else void this.startRecording("transcribe");
            },
        });
        this.addCommand({
            id: "recover-selection",
            name: "選択範囲を音声から再認識（復旧）",
            callback: () => void this.recoverSelection(),
        });
        this.addCommand({
            id: "manage-recordings",
            name: "文字起こしの録音を整理",
            callback: () => this.openRecordingsModal(),
        });
        this.addCommand({
            id: "reconvert-last",
            name: "直前の入力を変換戻し",
            callback: () => this.requestReconvert(),
        });
        this.addCommand({
            id: "select-endpoint",
            name: "接続先を選択",
            callback: () => this.openEndpointMenu(),
        });
        this.addCommand({
            id: "edit-userdict",
            name: "ユーザー辞書を編集",
            callback: () => this.openDictModal(),
        });

        this.addSettingTab(new VoxCraftSettingTab(this.app, this));
    }

    async onunload(): Promise<void> {
        await this.teardownSession();
    }

    // ---- 録音セッション制御 ----

    private toggleRecording(): void {
        if (this.recording) this.stopRecording();
        else void this.startRecording();
    }

    private async startRecording(mode: AsrMode = "dictation"): Promise<void> {
        if (this.recording) return;
        const cm = this.getActiveCm();
        if (!cm) {
            new Notice("VoxCraft: 挿入先のノート（編集モード）を開いてください。");
            return;
        }

        const urls = resolveUrls(this.settings);
        if (urls.length === 0) {
            new Notice("VoxCraft: 接続先が未設定です。設定でエンドポイントを追加してください。");
            return;
        }

        this.setStatus(urls.length > 1 ? "接続中…（つながる方を選択）" : "接続中…");
        this.socket = new AsrSocket(urls, {
            onReady: () => {
                const url = this.socket?.activeUrl;
                const label = url ? labelForUrl(this.settings, url) : "";
                this.setStatus(label ? `待機中… 話してください（${label}）` : "待機中… 話してください");
            },
            onPartial: () => {
                this.transcribing = true;
                this.setStatus("認識中…");
            },
            onSession: (id) => {
                this.session = id;
            },
            onChunk: (text, msg) => {
                this.transcribing = false;
                this.handleChunk(text, msg);
            },
            onReconvert: (msg) => this.handleReconvert(msg),
            onError: (m) => new Notice(`VoxCraft サーバーエラー: ${m}`),
            onClose: () => {
                if (this.recording) {
                    new Notice("VoxCraft: サーバー接続が切れました。");
                    void this.teardownSession();
                    this.setStatus("停止中");
                }
            },
        });

        try {
            await this.socket.connect();
        } catch (e) {
            const tried = urls.map((u) => labelForUrl(this.settings, u)).join(" / ");
            new Notice(
                `VoxCraft: サーバーに接続できません（${tried}）。` +
                "認識サーバーが起動しているか、接続先の設定を確認してください。"
            );
            this.socket = null;
            this.setStatus("停止中");
            return;
        }

        this.mode = mode;
        this.socket.sendStart(
            this.settings.stripJaAlnumSpace,
            this.settings.symbolDictation,
            mode
        );

        this.recorder = new MicRecorder(
            (pcm) => this.socket?.sendAudio(pcm),
            (level) => this.showLevel(level),
            // 文字起こしは自分の声ではなく会場や再生音を拾う。ブラウザの前処理は
            // 近接した1人の声を前提にしているので、ここでは無効化して原音を送る。
            mode === "transcribe" ? RAW_MIC : DICTATION_MIC
        );
        try {
            await this.recorder.start();
        } catch (e) {
            new Notice("VoxCraft: マイクにアクセスできませんでした。");
            await this.teardownSession();
            this.setStatus("停止中");
            return;
        }

        // 口述対象を固定し、現在のカーソル位置にアンカーを立てる。
        this.cm = cm;
        this.chunks = [];
        this.pendingReconvert = null;
        setAnchor(cm, cm.state.selection.main.head);

        if (mode === "transcribe") {
            this.spans = [];
            this.lastAudioEnd = 0;
            this.session = null;
        }

        this.recording = true;
        this.ribbonEl.addClass("voxcraft-recording");
        this.setStatus(mode === "transcribe" ? "● 文字起こし中" : "● 録音中");
    }

    stopRecording(): void {
        if (!this.recording) return;
        this.recording = false;
        this.ribbonEl.removeClass("voxcraft-recording");
        this.setStatus("停止処理中…");
        // 残り音声を確定させてから閉じる。
        this.socket?.sendStop();
        void this.recorder?.stop().then(() => {
            this.recorder = null;
        });
        // サーバーが末尾チャンクを返す猶予を置いてから閉じ、アンカーを片付ける。
        window.setTimeout(() => {
            this.socket?.close();
            this.socket = null;
            this.clearDictationAnchor();
            this.setStatus("停止中");
        }, 1500);
    }

    private async teardownSession(): Promise<void> {
        this.recording = false;
        this.ribbonEl?.removeClass("voxcraft-recording");
        await this.recorder?.stop();
        this.recorder = null;
        this.socket?.close();
        this.socket = null;
        this.clearDictationAnchor();
    }

    private clearDictationAnchor(): void {
        if (this.cm && this.cm.dom.isConnected) clearAnchor(this.cm);
        this.cm = null;
    }

    // ---- 確定チャンクの処理 ----

    private handleChunk(text: string, msg?: ServerMessage): void {
        if (this.mode === "transcribe") {
            this.handleTranscribeChunk(text, msg);
            return;
        }
        // 以下は従来どおりの口述処理（音声コマンドの判定を含む）。
        if (this.settings.enableCommands) {
            const cmd = parseCommand(text, this.settings.commandPrefix);
            if (cmd) {
                this.runCommand(cmd);
                return;
            }
        }
        this.insertText(text);
    }

    // 文字起こしモードのチャンク処理。
    // 音声コマンドは判定しない（動画側の発話で勝手にコマンドが走るのを防ぐ）。
    // 捨てられた音声区間は欠落マーカーとして本文に残し、後から復旧できるようにする。
    private handleTranscribeChunk(text: string, msg?: ServerMessage): void {
        const start = msg?.start;
        const end = msg?.end;

        if (msg?.dropped?.length) {
            new Notice(
                `VoxCraft: ${msg.dropped.length}件のセグメントを低確信として除外しました` +
                "（サーバーログに全文あり）。"
            );
        }

        // 前チャンクの終端と今回の開始が離れていれば、その間の音声はどこにも出ていない。
        // 黙って消さずに位置を残す ＝ 選んで再認識すれば取り戻せる。
        if (start !== undefined && this.lastAudioEnd > 0 && start - this.lastAudioEnd >= 0.35) {
            const gapStart = this.lastAudioEnd;
            const marker = `⟨未認識 ${formatTime(gapStart)}–${formatTime(start)}⟩`;
            this.insertText(marker);
            this.spans.push({ text: marker, start: gapStart, end: start });
        }

        if (text) {
            this.insertText(text);
            if (start !== undefined && end !== undefined) {
                this.spans.push({ text, start, end });
                if (this.spans.length > 2000) this.spans.shift();
            }
        }
        if (end !== undefined) this.lastAudioEnd = end;
    }

    private runCommand(cmd: NonNullable<ReturnType<typeof parseCommand>>): void {
        switch (cmd.kind) {
            case "stop":
                this.stopRecording();
                break;
            case "undo":
                this.undoLast();
                break;
            case "newline":
                this.insertText("\n");
                break;
            case "reconvert":
                this.requestReconvert();
                break;
            case "replace":
                this.replaceInDoc(cmd.from, cmd.to);
                break;
            case "pick":
                if (this.reconvertModal) this.reconvertModal.pickByVoice(cmd.index);
                break;
        }
    }

    // アンカー位置に追記する。ユーザーが手動でカーソルを離していれば、
    // その編集位置は動かさず（follow=false）、アンカーにだけ差し込む。
    private insertText(text: string): void {
        const cm = this.cm;
        if (!cm || !cm.dom.isConnected) return;
        const anchor = getAnchor(cm);
        if (anchor === null) return;

        const at = Math.min(anchor, cm.state.doc.length);
        const follow = cm.state.selection.main.head === anchor;

        cm.dispatch({
            changes: { from: at, to: at, insert: text },
            selection: follow ? { anchor: at + text.length } : undefined,
            scrollIntoView: follow,
        });
        // アンカーは StateField 側で at+text.length へ自動前進する。

        this.chunks.push(text);
        if (this.chunks.length > 200) this.chunks.shift();
    }

    // 直前チャンク（アンカー直前の text.length 文字）を削除する。
    private undoLast(): void {
        const cm = this.cm;
        const last = this.chunks[this.chunks.length - 1];
        if (!cm || last === undefined) return;
        const anchor = getAnchor(cm);
        if (anchor === null) return;

        const from = Math.max(0, anchor - last.length);
        const current = cm.state.doc.sliceString(from, anchor);
        if (current !== last) {
            new Notice("VoxCraft: 直前の入力が編集されているため取り消せません。");
            return;
        }
        this.chunks.pop();
        cm.dispatch({ changes: { from, to: anchor, insert: "" } });
    }

    // 「AをBに修正」: ノート全体からAをBに置換（過去テキストも対象）。
    private replaceInDoc(from: string, to: string): void {
        const cm = this.cm ?? this.getActiveCm();
        if (!cm) return;
        const doc = cm.state.doc.toString();
        const idx = doc.lastIndexOf(from);
        if (idx < 0) {
            new Notice(`VoxCraft: 「${from}」が見つかりませんでした。`);
            return;
        }
        cm.dispatch({ changes: { from: idx, to: idx + from.length, insert: to } });
    }

    // ---- 復旧（音声からの再認識） ----

    // 選択したテキストの元になった音声区間を、精度優先でもう一度認識し直す。
    // 誤変換（例:「一昨日」→「昨日」）も、欠落マーカーの区間も、これで戻せる。
    private async recoverSelection(): Promise<void> {
        const cm = this.getActiveCm();
        if (!cm) {
            new Notice("VoxCraft: ノートを編集モードで開いてください。");
            return;
        }
        const sel = cm.state.selection.main;
        if (sel.empty) {
            new Notice("VoxCraft: 復旧したい範囲を選択してください。");
            return;
        }
        if (!this.session) {
            new Notice("VoxCraft: 復旧できる録音がありません（文字起こしモードで録音した分のみ対象）。");
            return;
        }
        const url = this.activeUrl();
        if (!url) {
            new Notice("VoxCraft: 接続先が設定されていません。");
            return;
        }

        const selected = cm.state.doc.sliceString(sel.from, sel.to);
        const range = spanRangeFor(this.spans, selected);
        if (!range) {
            new Notice("VoxCraft: 選択範囲に対応する音声が見つかりません（この録音の出力を選んでください）。");
            return;
        }

        this.setStatus("音声から再認識中…");
        try {
            const result = await recognizeRange(url, this.session, range.start, range.end);
            this.setStatus(this.recording ? "● 文字起こし中" : "停止中");
            new RecoverModal(this.app, selected, result, (text) => {
                cm.dispatch({ changes: { from: sel.from, to: sel.to, insert: text } });
                // 置き換えた範囲も、元の音声とひも付け直しておく（再度やり直せるように）。
                this.spans.push({ text, start: range.start, end: range.end });
            }).open();
        } catch (e) {
            this.setStatus(this.recording ? "● 文字起こし中" : "停止中");
            new Notice(`VoxCraft: 再認識に失敗しました（${e instanceof Error ? e.message : String(e)}）`);
        }
    }

    // ---- 変換戻し ----

    private requestReconvert(): void {
        const cm = this.cm;
        const last = this.chunks[this.chunks.length - 1];
        if (!cm || !cm.dom.isConnected) {
            new Notice("VoxCraft: 録音中に「変換戻し」を使ってください。");
            return;
        }
        const anchor = getAnchor(cm);
        if (anchor === null || last === undefined || !last.trim()) {
            new Notice("VoxCraft: 変換戻しの対象がありません。");
            return;
        }
        if (!this.socket?.connected) {
            new Notice("VoxCraft: サーバー未接続のため変換戻しできません。");
            return;
        }

        const from = Math.max(0, anchor - last.length);
        const targetText = cm.state.doc.sliceString(from, anchor);
        this.pendingReconvert = { from, to: anchor };
        this.socket.sendReconvert(targetText);
        this.setStatus("変換候補を取得中…");
    }

    private handleReconvert(msg: ServerMessage): void {
        this.setStatus(this.recording ? "● 録音中" : "停止中");
        const segments = msg.segments || [];
        const target = this.pendingReconvert;
        this.pendingReconvert = null;
        if (segments.length === 0 || !target) {
            new Notice("VoxCraft: 変換候補が得られませんでした。");
            return;
        }
        const modal = new ReconvertModal(this.app, segments, (chosen) => {
            this.applyReconvert(target, chosen.join(""));
        });
        // モーダルが閉じたら音声「N番」の受け皿参照を解除する。
        const origClose = modal.onClose.bind(modal);
        modal.onClose = () => {
            origClose();
            if (this.reconvertModal === modal) this.reconvertModal = null;
        };
        this.reconvertModal = modal;
        modal.open();
    }

    private applyReconvert(target: { from: number; to: number }, newText: string): void {
        const cm = this.cm ?? this.getActiveCm();
        if (!cm) return;
        const to = Math.min(target.to, cm.state.doc.length);
        const from = Math.min(target.from, to);
        cm.dispatch({ changes: { from, to, insert: newText } });
        // 直前チャンクの記録も置換後の文字列に合わせておく（取り消し整合のため）。
        if (this.chunks.length > 0) this.chunks[this.chunks.length - 1] = newText;
    }

    // ---- 接続先の切り替え ----

    // 接続先メニューを表示する。evt があればその位置、無ければリボン脇に出す。
    private openEndpointMenu(evt?: MouseEvent): void {
        const menu = new Menu();
        const sel = this.settings.selection;

        menu.addItem((i) =>
            i
                .setTitle("自動（つながる方）")
                .setChecked(sel === AUTO)
                .onClick(() => void this.setSelection(AUTO))
        );
        menu.addSeparator();
        for (const ep of this.settings.endpoints) {
            const url = ep.url.trim();
            if (!url) continue;
            menu.addItem((i) =>
                i
                    .setTitle(ep.label || url)
                    .setChecked(sel === url)
                    .onClick(() => void this.setSelection(url))
            );
        }
        menu.addSeparator();
        menu.addItem((i) =>
            i
                .setTitle("設定を開く…")
                .setIcon("gear")
                .onClick(() => {
                    const setting = (this.app as unknown as {
                        setting?: { open?: () => void; openTabById?: (id: string) => void };
                    }).setting;
                    setting?.open?.();
                    setting?.openTabById?.("voxcraft");
                })
        );

        if (evt) {
            menu.showAtMouseEvent(evt);
        } else if (this.ribbonEl) {
            const r = this.ribbonEl.getBoundingClientRect();
            menu.showAtPosition({ x: r.right, y: r.top });
        } else {
            menu.showAtPosition({ x: 0, y: 0 });
        }
    }

    private async setSelection(sel: string): Promise<void> {
        this.settings.selection = sel;
        await this.saveSettings();
        const label = sel === AUTO ? "自動（つながる方）" : labelForUrl(this.settings, sel);
        new Notice(`VoxCraft: 接続先を「${label}」にしました。`);
    }

    // 現在つながっている接続先（未接続なら設定上の第一候補）。辞書APIの宛先に使う。
    activeUrl(): string | null {
        return this.socket?.activeUrl ?? resolveUrls(this.settings)[0] ?? null;
    }

    private openRecordingsModal(): void {
        const url = this.activeUrl();
        if (!url) {
            new Notice("VoxCraft: 接続先が設定されていません");
            return;
        }
        new RecordingsModal(this.app, url).open();
    }

    private openDictModal(): void {
        const url = this.activeUrl();
        if (!url) {
            new Notice("VoxCraft: 接続先が設定されていません");
            return;
        }
        new DictModal(this.app, url).open();
    }

    // ---- ユーティリティ ----

    // アクティブな Markdown エディタの CodeMirror6 ビューを得る。
    private getActiveCm(): EditorView | null {
        const view = this.app.workspace.getActiveViewOfType(MarkdownView);
        const editor = view?.editor as (Editor & { cm?: EditorView }) | undefined;
        return editor?.cm ?? null;
    }

    // 入力レベルをステータスバーにメーター表示（音声を拾えているかの確認用）。
    private showLevel(level: number): void {
        if (!this.recording || this.transcribing) return;
        const now = performance.now();
        if (now - this.lastMeterAt < 100) return; // 約10fpsに間引く
        this.lastMeterAt = now;
        const segs = 10;
        const filled = Math.max(0, Math.min(segs, Math.round(level * 40)));
        const bar = "█".repeat(filled) + "░".repeat(segs - filled);
        this.setStatus(`● 録音中 ${bar}`);
    }

    private setStatus(text: string): void {
        this.statusEl.setText(`🎙 VoxCraft: ${text}`);
    }

    async loadSettings(): Promise<void> {
        this.settings = migrateSettings(
            Object.assign({}, DEFAULT_SETTINGS, await this.loadData())
        );
    }

    async saveSettings(): Promise<void> {
        await this.saveData(this.settings);
    }
}
