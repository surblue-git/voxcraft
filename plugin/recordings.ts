// 文字起こしモードで保存された録音の一覧・整理UI。
//
// 録音はサーバー機のディスクに溜まる（16kHzモノラルで約115MB/時）ので、
// 中身の確認と掃除ができる入口を用意する。フォルダを開けるのは、サーバーが
// このPC自身のときだけ（別マシンのパスは開けない）。

import { App, Modal, Notice, Setting, requestUrl } from "obsidian";

import { httpBase } from "./dict";

export interface RecordingItem {
    session: string;
    seconds: number;
    bytes: number;
    modified: number;
}

export interface RecordingList {
    dir: string;
    items: RecordingItem[];
}

export async function fetchRecordings(wsUrl: string): Promise<RecordingList> {
    const res = await requestUrl({ url: `${httpBase(wsUrl)}/recordings`, method: "GET" });
    return res.json as RecordingList;
}

export async function deleteRecordings(wsUrl: string, sessions: string[]): Promise<{ failed: { session: string; reason: string }[] }> {
    const res = await requestUrl({
        url: `${httpBase(wsUrl)}/recordings/delete`,
        method: "POST",
        contentType: "application/json",
        body: JSON.stringify({ sessions }),
        throw: false,
    });
    if (res.status >= 400) {
        const detail = (res.json && (res.json as { detail?: string }).detail) || `HTTP ${res.status}`;
        throw new Error(detail);
    }
    return res.json as { failed: { session: string; reason: string }[] };
}

// "20260726-091530" → "2026-07-26 09:15:30"
function formatSession(id: string): string {
    const m = id.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/);
    if (!m) return id;
    return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`;
}

function formatDuration(sec: number): string {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    return h > 0 ? `${h}時間${m}分` : m > 0 ? `${m}分${s}秒` : `${s}秒`;
}

function formatSize(bytes: number): string {
    const mb = bytes / (1024 * 1024);
    return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`;
}

// サーバーがこのPC自身のときだけ、保存フォルダをOSのファイラで開ける。
function isLocalServer(wsUrl: string): boolean {
    return /^wss?:\/\/(localhost|127\.0\.0\.1|\[::1\])[:/]/i.test(wsUrl.trim());
}

function openFolder(dir: string): void {
    try {
        const shell = (window as unknown as {
            require?: (m: string) => { shell?: { openPath?: (p: string) => Promise<string> } };
        }).require?.("electron")?.shell;
        if (!shell?.openPath) throw new Error("この環境では開けません");
        void shell.openPath(dir);
    } catch (e) {
        new Notice(`VoxCraft: フォルダを開けませんでした（${e instanceof Error ? e.message : String(e)}）`);
    }
}

export class RecordingsModal extends Modal {
    private wsUrl: string;
    private data: RecordingList | null = null;
    private loadError: string | null = null;

    constructor(app: App, wsUrl: string) {
        super(app);
        this.wsUrl = wsUrl;
    }

    async onOpen(): Promise<void> {
        this.contentEl.createEl("p", { text: "読み込み中…" });
        try {
            this.data = await fetchRecordings(this.wsUrl);
        } catch (e) {
            this.loadError = e instanceof Error ? e.message : String(e);
        }
        this.render();
    }

    private render(): void {
        const { contentEl } = this;
        contentEl.empty();
        contentEl.createEl("h3", { text: "文字起こしの録音" });

        if (this.loadError) {
            contentEl.createEl("p", { text: `一覧を取得できません: ${this.loadError}` });
            return;
        }
        const data = this.data;
        if (!data) return;

        contentEl.createEl("div", {
            text: `保存先: ${data.dir}`,
            cls: "setting-item-description",
        });

        const total = data.items.reduce((a, i) => a + i.bytes, 0);
        contentEl.createEl("div", {
            text: `${data.items.length} 件 / 合計 ${formatSize(total)}`,
            cls: "setting-item-description",
        });

        const header = new Setting(contentEl);
        if (isLocalServer(this.wsUrl)) {
            header.addButton((b) =>
                b.setButtonText("フォルダを開く").onClick(() => openFolder(data.dir))
            );
        } else {
            header.setDesc("保存先は別マシンのため、ここからは開けません。");
        }
        header.addButton((b) =>
            b.setButtonText("更新").onClick(() => {
                void this.reload();
            })
        );

        if (data.items.length === 0) {
            contentEl.createEl("p", { text: "録音はまだありません。" });
            return;
        }

        for (const item of data.items) {
            new Setting(contentEl)
                .setName(formatSession(item.session))
                .setDesc(`${formatDuration(item.seconds)} / ${formatSize(item.bytes)}`)
                .addButton((b) =>
                    b
                        .setButtonText("削除")
                        .setWarning()
                        .onClick(() => void this.confirmDelete(item))
                );
        }
    }

    private async reload(): Promise<void> {
        try {
            this.data = await fetchRecordings(this.wsUrl);
            this.loadError = null;
        } catch (e) {
            this.loadError = e instanceof Error ? e.message : String(e);
        }
        this.render();
    }

    // 消したら戻せないので、日時と長さを見せて一度確認する。
    private async confirmDelete(item: RecordingItem): Promise<void> {
        const label = `${formatSession(item.session)}（${formatDuration(item.seconds)} / ${formatSize(item.bytes)}）`;
        const ok = await new Promise<boolean>((resolve) => {
            const modal = new ConfirmModal(this.app, label, resolve);
            modal.open();
        });
        if (!ok) return;
        try {
            const res = await deleteRecordings(this.wsUrl, [item.session]);
            if (res.failed?.length) {
                new Notice(`VoxCraft: 削除できません（${res.failed[0].reason}）`);
            } else {
                new Notice("VoxCraft: 録音を削除しました。");
            }
        } catch (e) {
            new Notice(`VoxCraft: 削除に失敗しました（${e instanceof Error ? e.message : String(e)}）`);
        }
        await this.reload();
    }

    onClose(): void {
        this.contentEl.empty();
    }
}

class ConfirmModal extends Modal {
    private label: string;
    private resolve: (ok: boolean) => void;
    private answered = false;

    constructor(app: App, label: string, resolve: (ok: boolean) => void) {
        super(app);
        this.label = label;
        this.resolve = resolve;
    }

    onOpen(): void {
        const { contentEl } = this;
        contentEl.createEl("h3", { text: "録音を削除しますか？" });
        contentEl.createEl("p", { text: this.label });
        contentEl.createEl("p", {
            text: "削除すると、この録音からの復旧（音声の再認識）はできなくなります。",
            cls: "setting-item-description",
        });
        new Setting(contentEl)
            .addButton((b) =>
                b
                    .setButtonText("削除する")
                    .setWarning()
                    .onClick(() => {
                        this.answered = true;
                        this.resolve(true);
                        this.close();
                    })
            )
            .addButton((b) =>
                b.setButtonText("やめる").onClick(() => {
                    this.answered = true;
                    this.resolve(false);
                    this.close();
                })
            );
    }

    onClose(): void {
        this.contentEl.empty();
        if (!this.answered) this.resolve(false);
    }
}
