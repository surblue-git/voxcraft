// 変換戻しの候補選択モーダル。
// 文節ごとに候補を並べ、手（クリック/矢印キー/数字キー）でも
// 音声（「3番」）でも選べるようにする。音声選択のため index→候補確定の
// メソッドを外部から呼べる形にしている。

import { App, Modal, Setting } from "obsidian";

export interface ReconvertSegment {
    reading: string;
    candidates: string[];
}

type SelectHandler = (chosen: string[]) => void;

// 新規経路（「Aを再変換」「選択範囲を再変換」）向けの追加オプション。
// 既存の変換戻し呼び出しは省略のまま動く（後方互換）。
export interface ReconvertModalOpts {
    // 置換対象の元表記。「確定して辞書に登録」の登録キーと初期選択の照合に使う。
    originalText?: string;
    // 「確定して辞書に登録」ボタンを押したときに呼ばれる（元表記, 確定表記）。
    onRegister?: (from: string, to: string) => void;
}

export class ReconvertModal extends Modal {
    private segments: ReconvertSegment[];
    private selection: number[];       // 各文節で選択中の候補 index
    private activeSeg = 0;              // 音声「N番」の対象となる文節
    private onSubmit: SelectHandler;
    private segEls: HTMLElement[] = [];
    private opts: ReconvertModalOpts;

    constructor(
        app: App,
        segments: ReconvertSegment[],
        onSubmit: SelectHandler,
        opts: ReconvertModalOpts = {}
    ) {
        super(app);
        this.segments = segments;
        this.selection = segments.map(() => 0);
        this.onSubmit = onSubmit;
        this.opts = opts;
        // 元表記が分かっているときは、それに一致する候補を初期選択にする
        // （文書中の何を直そうとしているかが一目で分かる）。
        if (opts.originalText) this.preselect(opts.originalText);
    }

    // 元表記を文節候補の連結で貪欲に辿り、一致した候補を初期選択にする。
    private preselect(original: string): void {
        let rest = original;
        for (let si = 0; si < this.segments.length; si++) {
            const ci = this.segments[si].candidates.findIndex(
                (c) => c.length > 0 && rest.startsWith(c)
            );
            if (ci < 0) return; // 途中で辿れなくなったら既定(先頭候補)のまま
            this.selection[si] = ci;
            rest = rest.slice(this.segments[si].candidates[ci].length);
        }
    }

    onOpen(): void {
        const { contentEl } = this;
        contentEl.addClass("voxcraft-reconvert");
        contentEl.createEl("h3", { text: "変換戻し — 候補を選択" });
        contentEl.createEl("p", {
            cls: "voxcraft-hint",
            text:
                "クリック / 数字キー / 音声「3番」で選択。文節はTabで移動。" +
                "Enter または音声「確定」で確定、Esc または「キャンセル」で閉じる" +
                "（開いている間は発話が本文に入りません）。",
        });

        this.segments.forEach((seg, si) => {
            const segEl = contentEl.createDiv({ cls: "voxcraft-seg" });
            segEl.createSpan({ cls: "voxcraft-reading", text: seg.reading });
            const list = segEl.createDiv({ cls: "voxcraft-cands" });
            seg.candidates.forEach((cand, ci) => {
                const btn = list.createEl("button", {
                    cls: "voxcraft-cand",
                    text: `${ci + 1}. ${cand}`,
                });
                btn.onclick = () => {
                    this.activeSeg = si;
                    this.choose(ci);
                };
            });
            this.segEls.push(segEl);
            this.paintSeg(si);
        });

        const buttons = new Setting(contentEl)
            .addButton((b) =>
                b.setButtonText("確定").setCta().onClick(() => this.submit())
            );
        if (this.opts.onRegister && this.opts.originalText) {
            buttons.addButton((b) =>
                b
                    .setButtonText("確定して辞書に登録")
                    .setTooltip("以後、同じ誤変換を自動で修正する")
                    .onClick(() => this.submit(true))
            );
        }
        buttons.addButton((b) => b.setButtonText("キャンセル").onClick(() => this.close()));

        this.scope.register([], "Enter", () => {
            this.submit();
            return false;
        });
        for (let n = 1; n <= 9; n++) {
            this.scope.register([], String(n), () => {
                this.choose(n - 1);
                return false;
            });
        }
        this.scope.register([], "Tab", () => {
            this.activeSeg = (this.activeSeg + 1) % this.segments.length;
            this.highlightActive();
            return false;
        });
    }

    // 音声「N番」からの選択（1始まり）を外部から呼べる。
    pickByVoice(oneBased: number): void {
        this.choose(oneBased - 1);
    }

    // 音声「確定」から呼べる（「N番」→「確定」で音声のみで完結する）。
    confirmByVoice(): void {
        this.submit();
    }

    private choose(ci: number): void {
        const seg = this.segments[this.activeSeg];
        if (!seg || ci < 0 || ci >= seg.candidates.length) return;
        this.selection[this.activeSeg] = ci;
        this.paintSeg(this.activeSeg);
    }

    private paintSeg(si: number): void {
        const segEl = this.segEls[si];
        if (!segEl) return;
        const btns = Array.from(segEl.querySelectorAll(".voxcraft-cand"));
        btns.forEach((b, ci) => {
            b.toggleClass("is-selected", ci === this.selection[si]);
        });
        this.highlightActive();
    }

    private highlightActive(): void {
        this.segEls.forEach((el, i) =>
            el.toggleClass("is-active", i === this.activeSeg)
        );
    }

    private submit(register = false): void {
        const chosen = this.segments.map((seg, i) => seg.candidates[this.selection[i]]);
        this.close();
        this.onSubmit(chosen);
        if (register && this.opts.onRegister && this.opts.originalText) {
            const joined = chosen.join("");
            if (joined && joined !== this.opts.originalText) {
                this.opts.onRegister(this.opts.originalText, joined);
            }
        }
    }

    onClose(): void {
        this.contentEl.empty();
    }
}
