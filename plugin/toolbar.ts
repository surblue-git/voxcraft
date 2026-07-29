// 画面下部の口述ツールバー。
//
// モバイル（Android）では、音声コマンドの発話やコマンドパレットを開くより
// 画面のボタンを押す方が速くて確実。口述の録音開始時に表示し、
// マイク・入力キャンセル・元に戻す・句読点・改行・辞書を1タップで操作する。
// 文字起こしモードでは表示しない（本文操作系が誤動作しないように）。
//
// ボタンは pointerdown を preventDefault してエディタのフォーカスを奪わない
// （奪うとモバイルでキーボードが閉じ、カーソル位置も失われる）。
//
// キーボードが出るとバーはその下に隠れてしまう。対策は2つ:
//   - 口述中はキーボード自体を抑制する（keyboard.ts。⌨ボタンで出せる）
//   - それでも出ているときは visualViewport でキーボードの高さを測って上に載る

import { setIcon } from "obsidian";

export interface ToolbarCallbacks {
    onMicToggle: () => void;
    onCancel: () => void;
    onRestore: () => void;
    onInsert: (text: string) => void;
    onRespeak: () => void;
    onReconvert: () => void;
    onKeyboardToggle: () => void;
    onOpenDict: () => void;
    onClose: () => void;
}

// キーボードが出ていると見なす下端の余白（px）。ナビゲーションバー等の細い
// インセットを誤検出しない程度に大きく取る。
const KEYBOARD_MIN_INSET = 120;

export class DictationToolbar {
    private el: HTMLElement | null = null;
    private micBtn: HTMLButtonElement | null = null;
    private kbBtn: HTMLButtonElement | null = null;
    private recording = false;
    private keyboardSuppressed = false;
    private onViewport: (() => void) | null = null;

    // keyboardButton: ソフトキーボードの表示/抑制ボタンを出すか（モバイルのみ意味がある）。
    constructor(
        private cb: ToolbarCallbacks,
        private opts: { keyboardButton: boolean } = { keyboardButton: false }
    ) {}

    show(): void {
        if (this.el) return;
        const bar = document.body.createDiv({ cls: "voxcraft-toolbar" });
        bar.addEventListener("pointerdown", (e) => e.preventDefault());

        this.micBtn = this.iconBtn(bar, "mic", "音声入力のオン/オフ", () => this.cb.onMicToggle());
        this.micBtn.addClass("voxcraft-tb-mic");
        this.iconBtn(bar, "delete", "入力キャンセル（直前の一文を削除）", () => this.cb.onCancel());
        this.iconBtn(bar, "undo-2", "元に戻す（キャンセルした文を再挿入）", () => this.cb.onRestore());
        this.textBtn(bar, "、", "読点を挿入", () => this.cb.onInsert("、"));
        this.textBtn(bar, "。", "句点を挿入", () => this.cb.onInsert("。"));
        this.iconBtn(bar, "corner-down-left", "改行を挿入", () => this.cb.onInsert("\n"));
        // 選択範囲に対する修正操作。音声起動（「ここを言い直し」等）は認識に化ける
        // ことがあるため、モバイルでは選択→タップの方が確実。
        this.wordBtn(bar, "言い直し", "選択範囲を言い直す（次の発話で置き換え）", () =>
            this.cb.onRespeak()
        );
        this.wordBtn(bar, "再変換", "選択範囲を再変換（候補から選ぶ）", () =>
            this.cb.onReconvert()
        );
        // キーボードは口述中は抑制されている。出したいときだけここから出す。
        if (this.opts.keyboardButton) {
            this.kbBtn = this.iconBtn(bar, "keyboard", "キーボードを表示", () =>
                this.cb.onKeyboardToggle()
            );
            this.kbBtn.addClass("voxcraft-tb-kb");
        }
        this.iconBtn(bar, "book-plus", "ユーザー辞書に追加", () => this.cb.onOpenDict());
        this.iconBtn(bar, "x", "ツールバーを閉じる", () => this.cb.onClose());

        this.el = bar;
        this.applyRecording();
        this.applyKeyboard();
        this.trackViewport();
    }

    hide(): void {
        this.untrackViewport();
        this.el?.remove();
        this.el = null;
        this.micBtn = null;
        this.kbBtn = null;
    }

    get visible(): boolean {
        return this.el !== null;
    }

    setRecording(on: boolean): void {
        this.recording = on;
        this.applyRecording();
    }

    setKeyboardSuppressed(on: boolean): void {
        this.keyboardSuppressed = on;
        this.applyKeyboard();
    }

    private applyKeyboard(): void {
        if (!this.kbBtn) return;
        // 抑制中＝「ボタンを押せばキーボードが出る」状態を強調する。
        this.kbBtn.toggleClass("is-active", this.keyboardSuppressed);
        this.kbBtn.setAttribute(
            "aria-label",
            this.keyboardSuppressed ? "キーボードを表示" : "キーボードを隠す（口述中は出さない）"
        );
    }

    // キーボードが出ている間はバーがその下に隠れる（Android では画面自体は
    // 縮まないので fixed の bottom では逃げられない）。visualViewport から
    // キーボードの高さを取り、その上に載せる。
    private trackViewport(): void {
        const vv = window.visualViewport;
        if (!vv) return;
        this.onViewport = () => this.applyViewport();
        vv.addEventListener("resize", this.onViewport);
        vv.addEventListener("scroll", this.onViewport);
        this.applyViewport();
    }

    private untrackViewport(): void {
        const vv = window.visualViewport;
        if (!vv || !this.onViewport) return;
        vv.removeEventListener("resize", this.onViewport);
        vv.removeEventListener("scroll", this.onViewport);
        this.onViewport = null;
    }

    private applyViewport(): void {
        const vv = window.visualViewport;
        if (!vv || !this.el) return;
        // 表示領域の下端から画面下端までの距離＝キーボード等に覆われている高さ。
        // キーボードで画面が縮むタイプの端末ではここが 0 になり、従来どおりになる。
        const inset = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
        this.el.style.setProperty("--voxcraft-kb-inset", `${inset}px`);
        this.el.toggleClass("is-keyboard-up", inset >= KEYBOARD_MIN_INSET);
    }

    private applyRecording(): void {
        if (!this.micBtn) return;
        setIcon(this.micBtn, this.recording ? "mic" : "mic-off");
        this.micBtn.toggleClass("is-recording", this.recording);
        this.micBtn.setAttribute(
            "aria-label",
            this.recording ? "音声入力を停止" : "音声入力を再開"
        );
    }

    private iconBtn(
        parent: HTMLElement,
        icon: string,
        label: string,
        onClick: () => void
    ): HTMLButtonElement {
        const b = parent.createEl("button", { cls: "clickable-icon voxcraft-tb-btn" });
        setIcon(b, icon);
        b.setAttribute("aria-label", label);
        b.addEventListener("click", (e) => {
            e.preventDefault();
            onClick();
        });
        return b;
    }

    private textBtn(
        parent: HTMLElement,
        text: string,
        label: string,
        onClick: () => void
    ): HTMLButtonElement {
        const b = parent.createEl("button", {
            cls: "clickable-icon voxcraft-tb-btn voxcraft-tb-text",
            text,
        });
        b.setAttribute("aria-label", label);
        b.addEventListener("click", (e) => {
            e.preventDefault();
            onClick();
        });
        return b;
    }

    // 単語ラベルのボタン（「言い直し」等）。アイコンでは意味が伝わらない操作に使う。
    private wordBtn(
        parent: HTMLElement,
        text: string,
        label: string,
        onClick: () => void
    ): HTMLButtonElement {
        const b = this.textBtn(parent, text, label, onClick);
        b.addClass("voxcraft-tb-word");
        return b;
    }
}
