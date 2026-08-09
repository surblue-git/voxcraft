// 変換戻しの候補選択モーダル。
// 文節ごとに候補を並べ、手（クリック/矢印キー/数字キー）でも
// 音声（「3番」）でも選べるようにする。音声選択のため index→候補確定の
// メソッドを外部から呼べる形にしている。

import { App, Modal, Setting } from "obsidian";

import { SymbolChoice, symbolChoicesFor } from "./symbols";

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
    // 記号候補を選んで登録したときに呼ばれる（元表記, 辞書へ入れる値）。
    // 置換辞書ではなく記号語辞書へ入れる必要があるため、経路を分ける。
    onRegisterSymbol?: (from: string, symbol: string) => void;
    // 「言い直す」を押したとき。読み自体が壊れている誤認識は、候補をいくら
    // 並べても正解が出てこない（「変換候補」を『変換個』と聞き取ったら、
    // 読みは「こ」なので「こうほ」の候補は原理的に現れない）。その行き止まりから
    // 抜ける道として、候補の隣に置く。録音中の口述でしか意味がないので任意。
    onRespeak?: () => void;
    // 同じ読みを連続して遡るときの現在位置。
    locationLabel?: string;
    // この一致を変更せず、次回の検索対象から外す。
    onSkip?: () => void;
}

export class ReconvertModal extends Modal {
    private segments: ReconvertSegment[];
    private selection: number[];       // 各文節で選択中の候補 index
    private activeSeg = 0;              // 音声「N番」の対象となる文節
    private onSubmit: SelectHandler;
    private segEls: HTMLElement[] = [];
    private opts: ReconvertModalOpts;
    // 候補に混ぜた記号（値→定義）。確定時に「記号語として登録」へ振り分ける。
    private symbolByValue = new Map<string, SymbolChoice>();

    constructor(
        app: App,
        segments: ReconvertSegment[],
        onSubmit: SelectHandler,
        opts: ReconvertModalOpts = {}
    ) {
        super(app);
        this.segments = segments;
        this.onSubmit = onSubmit;
        this.opts = opts;
        // 記号語の誤認識は読みが変わるので、読み由来の候補には正解が入っていない。
        // 短い単語ひとつを直すときだけ、記号を候補の末尾に足す。
        if (opts.originalText) this.appendSymbolCandidates(opts.originalText);
        this.selection = this.segments.map(() => 0);
        // 元表記が分かっているときは、それに一致する候補を初期選択にする
        // （文書中の何を直そうとしているかが一目で分かる）。
        if (opts.originalText) this.preselect(opts.originalText);
    }

    private appendSymbolCandidates(originalText: string): void {
        const seg = this.segments[0];
        if (!seg) return;
        const choices = symbolChoicesFor(originalText, this.segments.length, seg.candidates);
        if (choices.length === 0) return;
        for (const choice of choices) this.symbolByValue.set(choice.value, choice);
        this.segments = [
            { ...seg, candidates: [...seg.candidates, ...choices.map((c) => c.value)] },
            ...this.segments.slice(1),
        ];
    }

    // 記号は字面が小さく「\n」に至っては見えないので、読みを添えて表示する。
    private candidateLabel(value: string): string {
        return this.symbolByValue.get(value)?.label ?? value;
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
        if (this.opts.locationLabel) {
            contentEl.createEl("p", {
                cls: "voxcraft-hint",
                text: this.opts.locationLabel,
            });
        }
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
                    text: `${ci + 1}. ${this.candidateLabel(cand)}`,
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
        if ((this.opts.onRegister || this.opts.onRegisterSymbol) && this.opts.originalText) {
            buttons.addButton((b) =>
                b
                    .setButtonText("確定して辞書に登録")
                    .setTooltip(
                        "以後、同じ誤変換を自動で修正する" +
                        "（記号を選んだときは記号語として登録する）"
                    )
                    .onClick(() => this.submit(true))
            );
        }
        if (this.opts.onRespeak) {
            buttons.addButton((b) =>
                b
                    .setButtonText("言い直す")
                    .setTooltip(
                        "候補に正解が無いとき（読み自体が誤認識されている）。" +
                        "閉じて、次の発話でこの範囲を置き換える"
                    )
                    .onClick(() => {
                        this.close();
                        this.opts.onRespeak?.();
                    })
            );
        }
        if (this.opts.onSkip) {
            buttons.addButton((b) =>
                b
                    .setButtonText("この箇所をスキップ")
                    .setTooltip("変更せず、次の同じ再変換では一つ前の一致を探す")
                    .onClick(() => {
                        this.close();
                        this.opts.onSkip?.();
                    })
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
        if (!register) return;
        const original = this.opts.originalText;
        const joined = chosen.join("");
        if (!original || !joined || joined === original) return;

        // 記号を選んだ場合は置換辞書へ入れてはいけない（本文中の同綴りまで
        // 巻き添えにする）。単独チャンク一致だけで効く記号語として登録する。
        const symbol = this.symbolByValue.get(joined);
        if (symbol) {
            this.opts.onRegisterSymbol?.(original, symbol.store);
            return;
        }
        this.opts.onRegister?.(original, joined);
    }

    onClose(): void {
        this.contentEl.empty();
    }
}
