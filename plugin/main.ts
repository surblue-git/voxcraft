import { Editor, MarkdownView, Notice, Plugin } from "obsidian";

import { MicRecorder } from "./audio";
import { parseCommand } from "./commands";
import { ReconvertModal } from "./suggest";
import {
    DEFAULT_SETTINGS,
    VoxCraftSettingTab,
    VoxCraftSettings,
} from "./settings";
import { AsrSocket, ServerMessage } from "./ws";

// エディタへ挿入した1チャンク分の記録（取り消し用）。
interface Insertion {
    from: { line: number; ch: number };
    to: { line: number; ch: number };
    text: string;
}

export default class VoxCraftPlugin extends Plugin {
    settings: VoxCraftSettings;

    private socket: AsrSocket | null = null;
    private recorder: MicRecorder | null = null;
    private recording = false;
    private statusEl: HTMLElement;
    private ribbonEl: HTMLElement;

    // 挿入履歴（取り消し・変換戻しの対象特定に使う）。
    private history: Insertion[] = [];
    private reconvertModal: ReconvertModal | null = null;

    async onload(): Promise<void> {
        await this.loadSettings();

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
        const editor = this.getEditor();
        if (!editor) {
            new Notice("VoxCraft: 挿入先のノートを開いてください。");
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
        // サーバーが末尾チャンクを返す猶予を置いて閉じる。
        window.setTimeout(() => {
            this.socket?.close();
            this.socket = null;
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

    private insertText(text: string): void {
        const editor = this.getEditor();
        if (!editor) return;
        const from = editor.getCursor();
        editor.replaceRange(text, from);
        const to = editor.offsetToPos(editor.posToOffset(from) + text.length);
        editor.setCursor(to);
        this.history.push({ from, to, text });
        if (this.history.length > 100) this.history.shift();
    }

    private undoLast(): void {
        const editor = this.getEditor();
        const last = this.history.pop();
        if (!editor || !last) return;
        editor.replaceRange("", last.from, last.to);
        editor.setCursor(last.from);
    }

    private replaceInDoc(from: string, to: string): void {
        const editor = this.getEditor();
        if (!editor) return;
        const content = editor.getValue();
        const idx = content.lastIndexOf(from);
        if (idx < 0) {
            new Notice(`VoxCraft: 「${from}」が見つかりませんでした。`);
            return;
        }
        const start = editor.offsetToPos(idx);
        const end = editor.offsetToPos(idx + from.length);
        editor.replaceRange(to, start, end);
    }

    // ---- 変換戻し ----

    private requestReconvert(): void {
        const last = this.history[this.history.length - 1];
        if (!last || !last.text.trim()) {
            new Notice("VoxCraft: 変換戻しの対象がありません。");
            return;
        }
        if (!this.socket?.connected) {
            new Notice("VoxCraft: サーバー未接続のため変換戻しできません。");
            return;
        }
        this.socket.sendReconvert(last.text);
        this.setStatus("変換候補を取得中…");
    }

    private handleReconvert(msg: ServerMessage): void {
        this.setStatus(this.recording ? "● 録音中" : "停止中");
        const segments = msg.segments || [];
        if (segments.length === 0) {
            new Notice("VoxCraft: 変換候補が得られませんでした。");
            return;
        }
        const target = this.history[this.history.length - 1];
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

    private applyReconvert(target: Insertion | undefined, newText: string): void {
        const editor = this.getEditor();
        if (!editor || !target) return;
        // 直前挿入位置のテキストを置き換える。
        editor.replaceRange(newText, target.from, target.to);
        const newTo = editor.offsetToPos(editor.posToOffset(target.from) + newText.length);
        target.text = newText;
        target.to = newTo;
        editor.setCursor(newTo);
    }

    // ---- ユーティリティ ----

    private getEditor(): Editor | null {
        const view = this.app.workspace.getActiveViewOfType(MarkdownView);
        return view?.editor ?? null;
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
