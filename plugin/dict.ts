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

// 編集UIの1行 = 「正しい表記1つ + それに対応する誤認識キーの一覧（区切り文字列）」。
// サーバーの保存形式（キー→値のフラットなマップ）は変えず、UI側だけ値で束ねる。
// 同じ正解に複数の誤読を登録するのが実運用の主パターンなため。
type Group = { keys: string; value: string };

// キー欄の区切り: 読点・カンマ・スラッシュ・改行。
const KEY_SPLIT_RE = /[、,，／/\n]+/;

export function splitKeys(keys: string): string[] {
    return keys
        .split(KEY_SPLIT_RE)
        .map((s) => s.trim())
        .filter(Boolean);
}

// フラットなマップを「値ごとの行」へ束ねる（値の初出順を保つ）。
export function toGroups(map: Record<string, string>): Group[] {
    const order: string[] = [];
    const byValue = new Map<string, string[]>();
    for (const [k, v] of Object.entries(map || {})) {
        if (!byValue.has(v)) {
            byValue.set(v, []);
            order.push(v);
        }
        byValue.get(v)!.push(k);
    }
    return order.map((v) => ({ keys: byValue.get(v)!.join("、"), value: v }));
}

// 行の一覧をフラットなマップへ戻す。別の行に同じキーがあれば dup に集める
// （どちらか一方しか効かないため、保存前にユーザーへ知らせる）。
export function flattenGroups(groups: Group[]): { map: Record<string, string>; dup: string[] } {
    const out: Record<string, string> = {};
    const dup: string[] = [];
    for (const g of groups) {
        for (const k of splitKeys(g.keys)) {
            if (k in out && out[k] !== g.value && !dup.includes(k)) dup.push(k);
            out[k] = g.value;
        }
    }
    return { map: out, dup };
}

function countKeys(groups: Group[]): number {
    return groups.reduce((n, g) => n + splitKeys(g.keys).length, 0);
}

export class DictModal extends Modal {
    private wsUrl: string;
    private reps: Group[] = [];
    private syms: Group[] = [];
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
            this.reps = toGroups(d.replacements);
            this.syms = toGroups(d.symbols);
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
                "同じ正解に対する誤認識は、1つの行に「、」区切りでまとめて書ける" +
                "（例: 再変更、再変化、再変感 → 再変換）。" +
                "注意: 短くて一般的な語をキーにすると本文を壊す。" +
                "例「詳細」は誤認識でもあり正しい語でもあるため、前後を含めた長いキーにする。",
            cls: "setting-item-description",
        });

        this.renderRows(contentEl, "置換（replacements）", this.reps,
            "誤認識を「、」区切りで（例: 再変更、再変化）", "正しい表記（例: 再変換）");

        this.renderRows(contentEl, "記号語（symbols・単独で言ったときだけ変換）", this.syms,
            "誤認識を「、」区切りで（例: 当点、とうてんてん）", "記号（例: 、 / 改行）");

        new Setting(contentEl)
            .addButton((b) =>
                b
                    .setButtonText("保存")
                    .setCta()
                    .onClick(async () => {
                        const reps = flattenGroups(this.reps);
                        const syms = flattenGroups(this.syms);
                        const dup = [...reps.dup, ...syms.dup];
                        if (dup.length > 0) {
                            // 同じキーが複数の行にあると片方しか効かない。黙って上書きしない。
                            new Notice(
                                `VoxCraft: 同じキーが複数の行にあります — ${dup.join("、")}\n` +
                                "どの行に残すか整理してから保存してください。",
                                10000
                            );
                            return;
                        }
                        try {
                            await saveDict(this.wsUrl, {
                                replacements: reps.map,
                                symbols: syms.map,
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

    private renderRows(parent: HTMLElement, title: string, rows: Group[],
                       keyPlaceholder: string, valPlaceholder: string): void {
        const headingText = () =>
            `${title} — ${rows.length}行・${countKeys(rows)}キー`;
        const heading = parent.createEl("h3", { text: headingText() });
        const list = parent.createDiv();
        list.style.maxHeight = "40vh";
        list.style.overflowY = "auto";

        const renderRow = (row: Group, i: number): Setting => {
            const s = new Setting(list)
                .addText((t) => {
                    t.setPlaceholder(keyPlaceholder).setValue(row.keys)
                        .onChange((v) => { row.keys = v; });
                    // 複数キーを並べる欄なので、正解欄より広めに取る。
                    t.inputEl.style.minWidth = "18em";
                    t.inputEl.style.flexGrow = "1";
                })
                .addText((t) => {
                    t.setPlaceholder(valPlaceholder).setValue(row.value)
                        .onChange((v) => { row.value = v; });
                    t.inputEl.style.minWidth = "10em";
                })
                .addExtraButton((b) =>
                    b.setIcon("trash").setTooltip("この行（キー全部）を削除").onClick(() => {
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
                const row: Group = { keys: "", value: "" };
                rows.push(row);
                heading.setText(headingText());
                // 全体を作り直すと一覧の先頭までスクロールが戻ってしまうため、
                // 新しい行だけをその場に足してそこへスクロール・フォーカスする。
                const s = renderRow(row, rows.length - 1);
                s.settingEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
                (s.settingEl.querySelector("input") as HTMLInputElement | null)?.focus();
            })
        );
    }
}
