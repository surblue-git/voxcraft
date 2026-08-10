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

// サーバー機で PC音声として取り込める入力先（/audio-devices の応答と対）。
export interface ServerAudioDevice {
    name: string;
    kind: "loopback" | "input";
    isDefault: boolean;
}

export async function fetchAudioDevices(
    wsUrl: string
): Promise<{ devices: ServerAudioDevice[]; error: string }> {
    const res = await requestUrl({ url: `${httpBase(wsUrl)}/audio-devices`, method: "GET" });
    const body = (res.json ?? {}) as { devices?: ServerAudioDevice[]; error?: string };
    return { devices: body.devices ?? [], error: body.error ?? "" };
}

// /reconvert の応答（server/reconvert.py の戻り値と対）。
export interface ReconvertPayload {
    reading: string;
    // start/end は送ったテキスト上の文字位置。タップした語を特定するために使う
    // （古いサーバーは返さないので任意）。
    segments: { reading: string; candidates: string[]; start?: number; end?: number }[];
    // offset を渡したときだけ返る。その位置を含む文節の番号（範囲外なら null）。
    at?: number | null;
    online: boolean;
    dictionarySetId?: string;
    dictionaryRevision?: string;
    dictionaryProfileRevisions?: Record<string, string>;
    dictionaryWritableProfile?: string;
}

export interface DictionaryDiagnostic {
    severity: "warning" | "error";
    code: string;
    message: string;
    entry?: number;
}

export interface DictionaryProfileSummary {
    id: string;
    name: string;
    description?: string;
    entries: number;
    enabledEntries: number;
    valid: boolean;
    diagnostics: DictionaryDiagnostic[];
}

export interface DictionarySetSummary {
    id: string;
    name: string;
    description?: string;
    profiles: string[];
    writableProfile?: string;
    valid: boolean;
    revision?: string | null;
    profileRevisions: Record<string, string>;
    diagnostics: DictionaryDiagnostic[];
}

export interface DictionaryCatalog {
    schemaVersion: number;
    profiles: DictionaryProfileSummary[];
    sets: DictionarySetSummary[];
}

export interface DictionaryEntryResult {
    ok: boolean;
    created: boolean;
    profileId: string;
    revision: string;
}

function errorDetail(res: { status: number; json?: unknown }): string {
    const detail = (res.json as { detail?: string | { message?: string } } | undefined)?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail.message === "string") return detail.message;
    return `HTTP ${res.status}`;
}

export async function fetchDictionaryCatalog(wsUrl: string): Promise<DictionaryCatalog> {
    const res = await requestUrl({
        url: `${httpBase(wsUrl)}/dictionaries`,
        method: "GET",
        throw: false,
    });
    if (res.status >= 400) throw new Error(errorDetail(res));
    return res.json as DictionaryCatalog;
}

export async function addDictionaryEntry(
    wsUrl: string,
    profileId: string,
    observed: string,
    output: string,
    expectedRevision?: string
): Promise<DictionaryEntryResult> {
    const res = await requestUrl({
        url: `${httpBase(wsUrl)}/dictionaries/${encodeURIComponent(profileId)}/entries`,
        method: "POST",
        contentType: "application/json",
        body: JSON.stringify({ observed, output, expectedRevision }),
        throw: false,
    });
    if (res.status >= 400) throw new Error(errorDetail(res));
    return res.json as DictionaryEntryResult;
}

// 記号語（単独チャンク一致）を1件追加する。置換辞書とは別枠なので経路も分ける。
// 1文字キー（例 悪→。）が許されるのは、チャンク全体一致でしか効かないため。
export async function addDictionarySymbol(
    wsUrl: string,
    profileId: string,
    observed: string,
    output: string,
    expectedRevision?: string
): Promise<DictionaryEntryResult> {
    const res = await requestUrl({
        url: `${httpBase(wsUrl)}/dictionaries/${encodeURIComponent(profileId)}/symbols`,
        method: "POST",
        contentType: "application/json",
        body: JSON.stringify({ observed, output, expectedRevision }),
        throw: false,
    });
    if (res.status >= 400) throw new Error(errorDetail(res));
    return res.json as DictionaryEntryResult;
}

// テキストの再変換候補を REST で取得する（WS 接続不要。録音外でも使える）。
export async function fetchReconvert(
    wsUrl: string,
    text: string,
    dictionarySetId = "default",
    // タップした位置（text 上の文字位置）。渡すと payload.at でその文節が返る。
    offset?: number
): Promise<ReconvertPayload> {
    const res = await requestUrl({
        url: `${httpBase(wsUrl)}/reconvert`,
        method: "POST",
        contentType: "application/json",
        body: JSON.stringify(
            offset === undefined
                ? { text, dictionarySetId }
                : { text, dictionarySetId, offset }
        ),
        throw: false,
    });
    if (res.status >= 400) {
        const detail = (res.json && (res.json as { detail?: string }).detail) || `HTTP ${res.status}`;
        throw new Error(detail);
    }
    return res.json as ReconvertPayload;
}

// profileId 省略時は "common"（旧 /dict と同じ辞書）。
export async function fetchProfileDict(wsUrl: string, profileId = "common"): Promise<DictData> {
    const res = await requestUrl({
        url: `${httpBase(wsUrl)}/dictionaries/${encodeURIComponent(profileId)}/dict`,
        method: "GET",
        throw: false,
    });
    if (res.status >= 400) throw new Error(errorDetail(res));
    return res.json as DictData;
}

export async function saveProfileDict(
    wsUrl: string,
    profileId: string,
    data: DictData
): Promise<void> {
    const res = await requestUrl({
        url: `${httpBase(wsUrl)}/dictionaries/${encodeURIComponent(profileId)}/dict`,
        method: "POST",
        contentType: "application/json",
        body: JSON.stringify({ replacements: data.replacements, symbols: data.symbols }),
        throw: false,
    });
    if (res.status >= 400) throw new Error(errorDetail(res));
}

export class DictionarySetModal extends Modal {
    constructor(
        app: App,
        private wsUrl: string,
        private currentId: string,
        private onSelect: (setId: string) => Promise<void>
    ) {
        super(app);
    }

    async onOpen(): Promise<void> {
        this.titleEl.setText("VoxCraft 辞書セットを選択");
        const status = this.contentEl.createEl("p", {
            text: "辞書一覧を読み込み中…",
            cls: "setting-item-description",
        });
        status.setAttribute("aria-live", "polite");
        try {
            const catalog = await fetchDictionaryCatalog(this.wsUrl);
            const sets = catalog.sets.filter((item) => item.valid);
            if (sets.length === 0) throw new Error("利用できる辞書セットがありません");
            status.setText("この接続先で、次回の録音・再変換・音声復旧に使う辞書を選びます。");
            let selected = sets.some((item) => item.id === this.currentId)
                ? this.currentId
                : sets[0].id;
            const detail = this.contentEl.createEl("p", { cls: "setting-item-description" });
            const renderDetail = () => {
                const set = sets.find((item) => item.id === selected)!;
                detail.setText(
                    `${set.description || set.name} / 構成: ${set.profiles.join(" + ")} / ` +
                    `登録先: ${set.writableProfile || set.profiles[set.profiles.length - 1]}`
                );
            };
            new Setting(this.contentEl)
                .setName("辞書セット")
                .addDropdown((dropdown) => {
                    for (const item of sets) dropdown.addOption(item.id, item.name);
                    dropdown.setValue(selected).onChange((value) => {
                        selected = value;
                        renderDetail();
                    });
                });
            renderDetail();
            new Setting(this.contentEl)
                .addButton((button) => button.setButtonText("適用").setCta().onClick(async () => {
                    await this.onSelect(selected);
                    this.close();
                }))
                .addButton((button) => button.setButtonText("キャンセル").onClick(() => this.close()));
        } catch (error) {
            status.setText(`辞書一覧を取得できませんでした: ${error instanceof Error ? error.message : error}`);
        }
    }
}

export class QuickAddDictionaryModal extends Modal {
    constructor(
        app: App,
        private observedInitial: string,
        private targetLabel: string,
        private onSubmit: (observed: string, output: string) => Promise<void>
    ) {
        super(app);
    }

    onOpen(): void {
        this.titleEl.setText("VoxCraft 辞書へ追加");
        this.contentEl.createEl("p", {
            text: `登録先: ${this.targetLabel}。Whisperが実際に出した表記を、望む表記へ置換します。`,
            cls: "setting-item-description",
        });
        let observed = this.observedInitial;
        let output = "";
        new Setting(this.contentEl)
            .setName("誤認識した表記")
            .setDesc("ノートに出力された文字をそのまま指定")
            .addText((text) => text.setValue(observed).onChange((value) => { observed = value; }));
        new Setting(this.contentEl)
            .setName("正しい表記")
            .setDesc("今後、自動的に置き換える文字")
            .addText((text) => {
                text.setPlaceholder("正しい表記").onChange((value) => { output = value; });
                window.setTimeout(() => text.inputEl.focus(), 0);
            });
        const status = this.contentEl.createEl("p", { cls: "setting-item-description" });
        status.setAttribute("aria-live", "polite");
        new Setting(this.contentEl)
            .addButton((button) => button.setButtonText("登録").setCta().onClick(async () => {
                const from = observed.trim();
                const to = output.trim();
                if (!from || !to) {
                    status.setText("誤認識した表記と正しい表記を入力してください。");
                    return;
                }
                if (from === to) {
                    status.setText("変換前と変換後が同じです。");
                    return;
                }
                status.setText("登録中…");
                try {
                    await this.onSubmit(from, to);
                    this.close();
                } catch (error) {
                    status.setText(error instanceof Error ? error.message : String(error));
                }
            }))
            .addButton((button) => button.setButtonText("キャンセル").onClick(() => this.close()));
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
    private profileId: string;
    // プロファイル一覧の取得に失敗しても、指定されたプロファイル単体の編集は続けられる。
    private profiles: DictionaryProfileSummary[] = [];
    private reps: Group[] = [];
    private syms: Group[] = [];
    private loaded = false;
    private loadError: string | null = null;

    constructor(app: App, wsUrl: string, initialProfileId = "common") {
        super(app);
        this.wsUrl = wsUrl;
        this.profileId = initialProfileId;
    }

    async onOpen(): Promise<void> {
        this.titleEl.setText("VoxCraft ユーザー辞書");
        // モーダル自身に印を付ける（contentEl.empty() では消えない）。
        this.contentEl.addClass("voxcraft-dict");
        this.contentEl.createEl("p", {
            text: "読み込み中…",
            cls: "setting-item-description",
        });
        try {
            this.profiles = (await fetchDictionaryCatalog(this.wsUrl)).profiles;
        } catch {
            this.profiles = [];
        }
        await this.loadProfile(this.profileId);
    }

    private async loadProfile(profileId: string): Promise<void> {
        this.profileId = profileId;
        this.loaded = false;
        this.loadError = null;
        try {
            const d = await fetchProfileDict(this.wsUrl, profileId);
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

        // プロファイルが複数あるときだけ切り替えを出す（1つだけなら従来通り無言で共通辞書）。
        if (this.profiles.length > 1) {
            new Setting(contentEl)
                .setName("編集する辞書")
                .addDropdown((dropdown) => {
                    for (const p of this.profiles) {
                        dropdown.addOption(p.id, p.valid ? p.name : `${p.name}（要修正）`);
                    }
                    dropdown.setValue(this.profileId);
                    dropdown.onChange((value) => { void this.loadProfile(value); });
                });
        }

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

        // 説明は畳んでおく。開いた瞬間に一覧が見えることを優先する
        // （モバイルでは数行の説明だけで画面が埋まってしまう）。
        const help = contentEl.createEl("details", { cls: "voxcraft-dict-help" });
        help.createEl("summary", { text: "書き方のヒント" });
        help.createEl("p", {
            text:
                "認識結果を望む表記に置き換える。キーは「Whisperが実際に出した綴り」を登録するのが確実" +
                "（読みではなく、出力をそのままコピーする）。保存すると即座に反映される（再起動不要）。",
        });
        help.createEl("p", {
            text:
                "同じ正解に対する誤認識は、1つの行に「、」区切りでまとめて書ける" +
                "（例: 再変更、再変化、再変感 → 再変換）。" +
                "注意: 短くて一般的な語をキーにすると本文を壊す。" +
                "例「詳細」は誤認識でもあり正しい語でもあるため、前後を含めた長いキーにする。",
        });

        this.renderRows(contentEl, "置換（replacements）", this.reps, {
            keyLabel: "誤認識した表記（「、」区切りで複数可）",
            keyPlaceholder: "例: 再変更、再変化",
            valueLabel: "正しい表記",
            valuePlaceholder: "例: 再変換",
        });

        this.renderRows(contentEl, "記号語（symbols・単独で言ったときだけ変換）", this.syms, {
            keyLabel: "誤認識した表記（「、」区切りで複数可）",
            keyPlaceholder: "例: 当点、とうてんてん",
            valueLabel: "記号",
            valuePlaceholder: "例: 、 / 改行",
        });

        // 保存は常に手の届く場所に置く（一覧が長くなると末尾まで遠い）。
        const actions = contentEl.createDiv({ cls: "voxcraft-dict-actions" });
        new Setting(actions)
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
                            await saveProfileDict(this.wsUrl, this.profileId, {
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

    // 1件を「誤認識… → 正しい表記」の1行に畳んで並べる。叩いた行だけ編集欄を開く。
    // 入力欄を出しっぱなしにすると1件で3行分の高さを食い、狭い画面では一覧にならない。
    private renderRows(parent: HTMLElement, title: string, rows: Group[],
                       labels: { keyLabel: string; keyPlaceholder: string;
                                 valueLabel: string; valuePlaceholder: string }): void {
        const section = parent.createDiv({ cls: "voxcraft-dict-section" });
        const heading = section.createEl("h3", { cls: "voxcraft-dict-heading" });
        const updateHeading = () => {
            heading.setText(`${title} — ${rows.length}行・${countKeys(rows)}キー`);
        };
        updateHeading();

        const search = section.createEl("input", {
            cls: "voxcraft-dict-search",
            type: "search",
            attr: { placeholder: "絞り込み（誤認識・正しい表記）", enterkeyhint: "search" },
        });
        const list = section.createDiv({ cls: "voxcraft-dict-list" });
        const none = section.createDiv({ cls: "voxcraft-dict-none" });
        const shown: { row: Group; el: HTMLElement }[] = [];

        const applyFilter = () => {
            const q = search.value.trim().toLowerCase();
            let hits = 0;
            for (const item of shown) {
                const hit = !q || `${item.row.keys} ${item.row.value}`.toLowerCase().includes(q);
                item.el.toggleClass("is-hidden", !hit);
                if (hit) hits += 1;
            }
            none.setText(shown.length === 0 ? "まだ登録がありません" : "該当する行がありません");
            none.toggleClass("is-hidden", hits > 0);
        };
        search.addEventListener("input", applyFilter);

        const buildRow = (row: Group) => {
            const el = list.createDiv({ cls: "voxcraft-dict-row" });
            const summary = el.createEl("button", {
                cls: "voxcraft-dict-summary",
                attr: { type: "button" },
            });
            const keysEl = summary.createSpan({ cls: "voxcraft-dict-keys" });
            summary.createSpan({ cls: "voxcraft-dict-arrow", text: "→" });
            const valueEl = summary.createSpan({ cls: "voxcraft-dict-value" });
            const edit = el.createDiv({ cls: "voxcraft-dict-edit" });

            const refresh = () => {
                const keys = row.keys.trim();
                const value = row.value.trim();
                keysEl.setText(keys || "（未入力）");
                keysEl.toggleClass("is-blank", !keys);
                valueEl.setText(value || "（未入力）");
                valueEl.toggleClass("is-blank", !value);
                updateHeading();
            };

            const field = (label: string, placeholder: string, value: string,
                           onInput: (v: string) => void): HTMLInputElement => {
                const wrap = edit.createDiv({ cls: "voxcraft-dict-field" });
                wrap.createEl("label", { cls: "voxcraft-dict-label", text: label });
                const input = wrap.createEl("input", {
                    cls: "voxcraft-dict-input",
                    type: "text",
                    attr: { placeholder, autocapitalize: "off", autocomplete: "off", spellcheck: "false" },
                });
                input.value = value;
                input.addEventListener("input", () => {
                    onInput(input.value);
                    refresh();
                });
                return input;
            };

            const keyInput = field(labels.keyLabel, labels.keyPlaceholder, row.keys,
                (v) => { row.keys = v; });
            field(labels.valueLabel, labels.valuePlaceholder, row.value,
                (v) => { row.value = v; });

            const rowActions = edit.createDiv({ cls: "voxcraft-dict-row-actions" });
            const remove = rowActions.createEl("button", { cls: "mod-warning", text: "この行を削除" });
            remove.addEventListener("click", () => {
                const at = rows.indexOf(row);
                if (at >= 0) rows.splice(at, 1);
                const seen = shown.findIndex((item) => item.row === row);
                if (seen >= 0) shown.splice(seen, 1);
                el.remove();
                updateHeading();
                applyFilter();
            });

            summary.addEventListener("click", () => {
                el.toggleClass("is-open", !el.hasClass("is-open"));
            });

            refresh();
            shown.push({ row, el });
            return { el, keyInput };
        };

        rows.forEach((row) => buildRow(row));
        applyFilter();

        const add = section.createDiv({ cls: "voxcraft-dict-add" }).createEl("button", {
            text: "＋ 追加",
        });
        add.addEventListener("click", () => {
            const row: Group = { keys: "", value: "" };
            rows.push(row);
            // 絞り込み中でも新しい行は見えないと困る。
            search.value = "";
            // 全体を作り直すと一覧の先頭までスクロールが戻ってしまうため、
            // 新しい行だけをその場に足してそこへスクロール・フォーカスする。
            const created = buildRow(row);
            created.el.addClass("is-open");
            applyFilter();
            created.el.scrollIntoView({ block: "nearest", behavior: "smooth" });
            created.keyInput.focus();
        });
    }
}
