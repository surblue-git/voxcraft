import { Editor, MarkdownView, Notice, Plugin } from "obsidian";
import { EditorView } from "@codemirror/view";

import { MicRecorder } from "./audio";
import { parseCommand } from "./commands";
import { ReconvertModal } from "./suggest";
import {
    DEFAULT_SETTINGS,
    VoxCraftSettingTab,
    VoxCraftSettings,
} from "./settings";
import { AsrSocket, ServerMessage } from "./ws";
import { anchorExtension, setAnchor, clearAnchor, getAnchor } from "./anchor";

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

    async onload(): Promise<void> {
        await this.loadSettings();

        // 口述アンカー（CM6 拡張）を全エディタに登録。
        this.registerEditorExtension(anchorExtension);

        this.ribbonEl = this.addRibbonIcon("mic", "VoxCraft 音声入力の開始/停止", () => {
            this.toggleRecording();
        });

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
            id: "reconvert-last",
            name: "直前の入力を変換戻し",
            callback: () => this.requestReconvert(),
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

    private async startRecording(): Promise<void> {
        if (this.recording) return;
        const cm = this.getActiveCm();
        if (!cm) {
            new Notice("VoxCraft: 挿入先のノート（編集モード）を開いてください。");
            return;
        }

        this.setStatus("接続中…");
        this.socket = new AsrSocket(this.settings.serverUrl, {
            onReady: () => this.setStatus("待機中… 話してください"),
            onChunk: (text) => this.handleChunk(text),
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
            new Notice(
                `VoxCraft: サーバーに接続できません（${this.settings.serverUrl}）。` +
                "認識サーバーが起動しているか確認してください。"
            );
            this.socket = null;
            this.setStatus("停止中");
            return;
        }

        this.socket.sendStart(
            this.settings.stripJaAlnumSpace,
            this.settings.symbolDictation
        );

        this.recorder = new MicRecorder((pcm) => this.socket?.sendAudio(pcm));
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

        this.recording = true;
        this.ribbonEl.addClass("voxcraft-recording");
        this.setStatus("● 録音中");
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

    private handleChunk(text: string): void {
        if (this.settings.enableCommands) {
            const cmd = parseCommand(text, this.settings.commandPrefix);
            if (cmd) {
                this.runCommand(cmd);
                return;
            }
        }
        this.insertText(text);
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

    // ---- ユーティリティ ----

    // アクティブな Markdown エディタの CodeMirror6 ビューを得る。
    private getActiveCm(): EditorView | null {
        const view = this.app.workspace.getActiveViewOfType(MarkdownView);
        const editor = view?.editor as (Editor & { cm?: EditorView }) | undefined;
        return editor?.cm ?? null;
    }

    private setStatus(text: string): void {
        this.statusEl.setText(`🎙 VoxCraft: ${text}`);
    }

    async loadSettings(): Promise<void> {
        this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
    }

    async saveSettings(): Promise<void> {
        await this.saveData(this.settings);
    }
}
