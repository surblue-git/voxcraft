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

export class ReconvertModal extends Modal {
    private segments: ReconvertSegment[];
    private selection: number[];       // 各文節で選択中の候補 index
    private activeSeg = 0;              // 音声「N番」の対象となる文節
    private onSubmit: SelectHandler;
    private segEls: HTMLElement[] = [];

    constructor(app: App, segments: ReconvertSegment[], onSubmit: SelectHandler) {
        super(app);
        this.segments = segments;
        this.selection = segments.map(() => 0);
        this.onSubmit = onSubmit;
    }

    onOpen(): void {
        const { contentEl } = this;
        contentEl.addClass("voxcraft-reconvert");
        contentEl.createEl("h3", { text: "変換戻し — 候補を選択" });
        contentEl.createEl("p", {
            cls: "voxcraft-hint",
            text: "クリック / 数字キー / 音声「3番」で選択。文節はTabで移動。",
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

        new Setting(contentEl)
            .addButton((b) =>
                b.setButtonText("確定").setCta().onClick(() => this.submit())
            )
            .addButton((b) => b.setButtonText("キャンセル").onClick(() => this.close()));

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

    private submit(): void {
        const chosen = this.segments.map((seg, i) => seg.candidates[this.selection[i]]);
        this.close();
        this.onSubmit(chosen);
    }

    onClose(): void {
        this.contentEl.empty();
    }
}
