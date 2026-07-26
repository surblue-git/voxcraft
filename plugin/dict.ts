// ユーザー辞書の取得・保存と、その編集UI。
//
// サーバーの /dict を叩く。Obsidian の requestUrl を使うので CORS の制約を受けず、
// デスクトップでも Android でも同じコードで動く。

import { App, Modal, Notice, Setting, requestUrl } from "obsidian";

export interface DictData {
    replacements: Record<string, string>;
    symbols: Record<string, string>;
    path?: string;
    error?: string | null;
}

export interface HealthData {
    ready: boolean;
    model: string;
    resolvedModel?: string;
    device: string;
    compute: string;
    beamSize?: number;
    autoPunctuation?: boolean;
    silenceSec?: number;
    dictError?: string | null;
}

// ws://host:8760/ws → http://host:8760 （REST API のベースURL）
export function httpBase(wsUrl: string): string {
    const u = wsUrl.trim().replace(/^ws(s)?:\/\//i, (_m, s) => (s ? "https://" : "http://"));
    return u.replace(/\/ws\/?$/i, "");
}

export async function fetchHealth(wsUrl: string): Promise<HealthData> {
    const res = await requestUrl({ url: `${httpBase(wsUrl)}/health`, method: "GET" });
    return res.json as HealthData;
}

// /reconvert の応答（server/reconvert.py の戻り値と対）。
export interface ReconvertPayload {
    reading: string;
    segments: { reading: string; candidates: string[] }[];
    online: boolean;
}

// テキストの再変換候補を REST で取得する（WS 接続不要。録音外でも使える）。
export async function fetchReconvert(wsUrl: string, text: string): Promise<ReconvertPayload> {
    const res = await requestUrl({
        url: `${httpBase(wsUrl)}/reconvert`,
        method: "POST",
        contentType: "application/json",
        body: JSON.stringify({ text }),
        throw: false,
    });
    if (res.status >= 400) {
        const detail = (res.json && (res.json as { detail?: string }).detail) || `HTTP ${res.status}`;
        throw new Error(detail);
    }
    return res.json as ReconvertPayload;
}

export async function fetchDict(wsUrl: string): Promise<DictData> {
    const res = await requestUrl({ url: `${httpBase(wsUrl)}/dict`, method: "GET" });
    return res.json as DictData;
}

export async function saveDict(wsUrl: string, data: DictData): Promise<void> {
    const res = await requestUrl({
        url: `${httpBase(wsUrl)}/dict`,
        method: "POST",
        contentType: "application/json",
        body: JSON.stringify({ replacements: data.replacements, symbols: data.symbols }),
        throw: false,
    });
    if (res.status >= 400) {
        const detail = (res.json && (res.json as { detail?: string }).detail) || `HTTP ${res.status}`;
        throw new Error(detail);
    }
}

type Row = { key: string; value: string };

function toRows(map: Record<string, string>): Row[] {
    return Object.entries(map || {}).map(([key, value]) => ({ key, value }));
}

function toMap(rows: Row[]): Record<string, string> {
    const out: Record<string, string> = {};
    for (const r of rows) {
        const k = r.key.trim();
        if (k) out[k] = r.value;
    }
    return out;
}

export class DictModal extends Modal {
    private wsUrl: string;
    private reps: Row[] = [];
    private syms: Row[] = [];
    private loaded = false;
    private loadError: string | null = null;

    constructor(app: App, wsUrl: string) {
        super(app);
        this.wsUrl = wsUrl;
    }

    async onOpen(): Promise<void> {
        this.titleEl.setText("VoxCraft ユーザー辞書");
        this.contentEl.createEl("p", {
            text: "読み込み中…",
            cls: "setting-item-description",
        });
        try {
            const d = await fetchDict(this.wsUrl);
            this.reps = toRows(d.replacements);
            this.syms = toRows(d.symbols);
            this.loaded = true;
        } catch (e) {
            this.loadError = e instanceof Error ? e.message : String(e);
        }
        this.render();
    }

    private render(): void {
        const { contentEl } = this;
        contentEl.empty();

        if (!this.loaded) {
            contentEl.createEl("p", {
                text: `サーバーから辞書を取得できませんでした: ${this.loadError ?? "不明なエラー"}`,
            });
            contentEl.createEl("p", {
                text: `接続先: ${httpBase(this.wsUrl)} （サーバーが起動しているか確認してください）`,
                cls: "setting-item-description",
            });
            return;
        }

        contentEl.createEl("p", {
            text:
                "認識結果を望む表記に置き換える。キーは「Whisperが実際に出した綴り」を登録するのが確実" +
                "（読みではなく、出力をそのままコピーする）。保存すると即座に反映される（再起動不要）。",
            cls: "setting-item-description",
        });
        contentEl.createEl("p", {
            text:
                "注意: 短くて一般的な語をキーにすると本文を壊す。" +
                "例「詳細」は誤認識でもあり正しい語でもあるため、前後を含めた長いキーにする。",
            cls: "setting-item-description",
        });

        this.renderRows(contentEl, "置換（replacements）", this.reps,
            "Whisperの出力（例: 収集説明）", "正しい表記（例: 趣旨説明）");

        this.renderRows(contentEl, "記号語（symbols・単独で言ったときだけ変換）", this.syms,
            "Whisperの出力（例: 当点）", "記号（例: 、 / 改行）");

        new Setting(contentEl)
            .addButton((b) =>
                b
                    .setButtonText("保存")
                    .setCta()
                    .onClick(async () => {
                        try {
                            await saveDict(this.wsUrl, {
                                replacements: toMap(this.reps),
                                symbols: toMap(this.syms),
                            });
                            new Notice("VoxCraft: 辞書を保存しました（即時反映）");
                            this.close();
                        } catch (e) {
                            new Notice(`VoxCraft: 保存できません — ${e instanceof Error ? e.message : e}`, 8000);
                        }
                    })
            )
            .addButton((b) => b.setButtonText("キャンセル").onClick(() => this.close()));
    }

    private renderRows(parent: HTMLElement, title: string, rows: Row[],
                       keyPlaceholder: string, valPlaceholder: string): void {
        const heading = parent.createEl("h3", { text: `${title} — ${rows.length}件` });
        const list = parent.createDiv();
        list.style.maxHeight = "40vh";
        list.style.overflowY = "auto";

        const renderRow = (row: Row, i: number): Setting => {
            const s = new Setting(list)
                .addText((t) => {
                    t.setPlaceholder(keyPlaceholder).setValue(row.key)
                        .onChange((v) => { row.key = v; });
                    t.inputEl.style.minWidth = "12em";
                })
                .addText((t) => {
                    t.setPlaceholder(valPlaceholder).setValue(row.value)
                        .onChange((v) => { row.value = v; });
                    t.inputEl.style.minWidth = "12em";
                })
                .addExtraButton((b) =>
                    b.setIcon("trash").setTooltip("削除").onClick(() => {
                        rows.splice(i, 1);
                        this.render();
                    })
                );
            s.controlEl.style.flexWrap = "wrap";
            s.infoEl.remove(); // 名前欄は使わない（横幅を入力に回す）
            return s;
        };

        rows.forEach((row, i) => renderRow(row, i));

        new Setting(parent).addButton((b) =>
            b.setButtonText("＋ 追加").onClick(() => {
                const row: Row = { key: "", value: "" };
                rows.push(row);
                heading.setText(`${title} — ${rows.length}件`);
                // 全体を作り直すと一覧の先頭までスクロールが戻ってしまうため、
                // 新しい行だけをその場に足してそこへスクロール・フォーカスする。
                const s = renderRow(row, rows.length - 1);
                s.settingEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
                (s.settingEl.querySelector("input") as HTMLInputElement | null)?.focus();
            })
        );
    }
}
