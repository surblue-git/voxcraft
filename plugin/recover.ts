// 文字起こしの復旧: 保存済みセッション音声の指定区間を、精度優先で認識し直す。
//
// 文字起こしモードでは各チャンクに音声の秒数範囲が付く（ws.ts の ServerMessage）。
// 消えた箇所・誤変換された箇所を選んでここを通せば、元の音声から取り戻せる。

import { App, Modal, Notice, Setting, requestUrl } from "obsidian";

import { httpBase } from "./dict";

export interface RecognizeResult {
    text: string;
    dropped?: string[];
    seconds?: number;
    dictionarySetId?: string;
    dictionaryRevision?: string;
}

// テキストと、それを生んだ音声の位置（秒）。ノート上の位置ではなく本文で対応づけるため、
// 手で編集された後でも「選択したテキストがどの音声から来たか」を辿れる。
export interface AudioSpan {
    text: string;
    start: number;
    end: number;
}

export async function recognizeRange(
    wsUrl: string,
    session: string,
    start: number,
    end: number,
    margin = 1.0,
    dictionarySetId = "default"
): Promise<RecognizeResult> {
    const res = await requestUrl({
        url: `${httpBase(wsUrl)}/recognize`,
        method: "POST",
        contentType: "application/json",
        body: JSON.stringify({ session, start, end, margin, dictionarySetId }),
        throw: false,
    });
    if (res.status >= 400) {
        const detail = (res.json && (res.json as { detail?: string }).detail) || `HTTP ${res.status}`;
        throw new Error(detail);
    }
    return res.json as RecognizeResult;
}

// 選択テキストに重なる span を探し、その音声範囲を合成して返す。
// 完全一致でなくても、部分的に含まれていれば拾う（選択が語の途中でも動くように）。
export function spanRangeFor(spans: AudioSpan[], selected: string): { start: number; end: number } | null {
    const needle = selected.trim();
    if (!needle) return null;
    const hits = spans.filter((s) => {
        const t = s.text.trim();
        if (!t) return false;
        return t.includes(needle) || needle.includes(t);
    });
    if (hits.length === 0) return null;
    return {
        start: Math.min(...hits.map((h) => h.start)),
        end: Math.max(...hits.map((h) => h.end)),
    };
}

// 秒 → "1:23.4"（欠落マーカーの表示用）。
export function formatTime(sec: number): string {
    const m = Math.floor(sec / 60);
    const s = sec - m * 60;
    return `${m}:${s.toFixed(1).padStart(4, "0")}`;
}

// 再認識の結果を見せて、置き換えるかどうかを選ばせる。
export class RecoverModal extends Modal {
    private before: string;
    private after: string;
    private dropped: string[];
    private onApply: (text: string) => void;

    constructor(
        app: App,
        before: string,
        result: RecognizeResult,
        onApply: (text: string) => void
    ) {
        super(app);
        this.before = before;
        this.after = result.text || "";
        this.dropped = result.dropped || [];
        this.onApply = onApply;
    }

    onOpen(): void {
        const { contentEl } = this;
        contentEl.createEl("h3", { text: "音声から再認識" });

        contentEl.createEl("div", { text: "現在", cls: "setting-item-description" });
        contentEl.createEl("pre", { text: this.before || "（なし）" });

        contentEl.createEl("div", { text: "再認識の結果", cls: "setting-item-description" });
        const edit = contentEl.createEl("textarea", { text: this.after });
        edit.rows = 5;
        edit.style.width = "100%";

        if (this.dropped.length > 0) {
            contentEl.createEl("div", {
                text: `※ 再認識でも ${this.dropped.length} 件のセグメントが低確信として除外されました。`,
                cls: "setting-item-description",
            });
        }

        new Setting(contentEl)
            .addButton((b) =>
                b
                    .setButtonText("置き換える")
                    .setCta()
                    .onClick(() => {
                        const text = edit.value;
                        if (!text.trim()) {
                            new Notice("VoxCraft: 置き換える内容が空です。");
                            return;
                        }
                        this.onApply(text);
                        this.close();
                    })
            )
            .addButton((b) => b.setButtonText("閉じる").onClick(() => this.close()));
    }

    onClose(): void {
        this.contentEl.empty();
    }
}
