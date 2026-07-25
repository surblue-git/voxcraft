import { App, Notice, PluginSettingTab, Setting } from "obsidian";
import type VoxCraftPlugin from "./main";
import { DictModal, fetchHealth, httpBase } from "./dict";

export interface VoxEndpoint {
    label: string; // 表示名（例:「自宅LAN」「Tailscale」）
    url: string;   // ws://host:port/ws
}

export const AUTO = "auto"; // selection が "auto" なら候補すべてへ同時接続し、繋がった方を採用

export interface VoxCraftSettings {
    endpoints: VoxEndpoint[]; // 接続先候補（上から順に試すが、自動時は同時接続レース）
    selection: string;        // "auto" | エンドポイントの url（固定接続）
    stripJaAlnumSpace: boolean; // 日本語と英数字の間の半角スペース除去
    symbolDictation: boolean;   // 「まる」等の記号読み上げ
    enableCommands: boolean;    // 音声コマンドを有効化
    commandPrefix: string;      // 空なら常時判定、非空なら「この語で始まる発話」のみ
    autoReconvertLast: boolean; // 「変換戻し」時、直前チャンクを対象にする
    serverUrl?: string;         // 後方互換: 旧・単一URL（読み込み時に endpoints へ移行）
}

export const DEFAULT_SETTINGS: VoxCraftSettings = {
    endpoints: [{ label: "このPC", url: "ws://localhost:8760/ws" }],
    selection: AUTO,
    stripJaAlnumSpace: true,
    symbolDictation: true,
    enableCommands: true,
    commandPrefix: "",
    autoReconvertLast: true,
};

// 旧バージョン（単一 serverUrl）の設定を新モデルへ移行する。
export function migrateSettings(s: VoxCraftSettings): VoxCraftSettings {
    if ((!s.endpoints || s.endpoints.length === 0) && s.serverUrl) {
        s.endpoints = [{ label: "サーバー", url: s.serverUrl }];
    }
    if (!s.endpoints || s.endpoints.length === 0) {
        s.endpoints = [...DEFAULT_SETTINGS.endpoints];
    }
    if (!s.selection) s.selection = AUTO;
    delete s.serverUrl;
    return s;
}

// 実際に接続を試みる URL 一覧を返す（selection に従う）。
export function resolveUrls(s: VoxCraftSettings): string[] {
    const all = s.endpoints.map((e) => e.url.trim()).filter(Boolean);
    if (s.selection === AUTO || !s.selection) return all;
    return all.includes(s.selection) ? [s.selection] : all;
}

// URL に対応する表示名（見つからなければ URL そのもの）。
export function labelForUrl(s: VoxCraftSettings, url: string): string {
    const ep = s.endpoints.find((e) => e.url.trim() === url);
    return ep ? ep.label : url;
}

export class VoxCraftSettingTab extends PluginSettingTab {
    plugin: VoxCraftPlugin;

    constructor(app: App, plugin: VoxCraftPlugin) {
        super(app, plugin);
        this.plugin = plugin;
    }

    display(): void {
        const { containerEl } = this;
        containerEl.empty();
        containerEl.createEl("h2", { text: "VoxCraft 音声入力" });

        // ---- 接続先の選択 ----
        containerEl.createEl("h3", { text: "接続先" });

        new Setting(containerEl)
            .setName("接続先の選択")
            .setDesc(
                "「自動」なら、録音開始時に下の候補すべてへ同時接続し、最初に繋がった方を使う。" +
                "自宅ではLAN、外出先ではTailscaleが自動的に選ばれる。特定の候補に固定することもできる。" +
                "マイクのリボンアイコンを右クリック（長押し）、またはコマンド「接続先を選択」からもここを切り替えられる。"
            )
            .addDropdown((d) => {
                d.addOption(AUTO, "自動（つながる方）");
                for (const ep of this.plugin.settings.endpoints) {
                    if (ep.url.trim()) d.addOption(ep.url.trim(), `${ep.label}`);
                }
                d.setValue(this.plugin.settings.selection);
                d.onChange(async (v) => {
                    this.plugin.settings.selection = v;
                    await this.plugin.saveSettings();
                });
            });

        // ---- エンドポイント一覧の編集 ----
        containerEl.createEl("h3", { text: "接続先の候補（エンドポイント）" });
        containerEl.createEl("p", {
            text:
                "認識サーバー（自宅PC）のアドレスを候補として登録する。" +
                "例: 自宅LAN = ws://192.168.x.x:8760/ws、Tailscale = ws://100.x.x.x:8760/ws、" +
                "このPC自身 = ws://localhost:8760/ws。",
            cls: "setting-item-description",
        });

        this.plugin.settings.endpoints.forEach((ep, i) => {
            const setting = new Setting(containerEl)
                .setName(`候補 ${i + 1}`)
                .addText((t) =>
                    t
                        .setPlaceholder("表示名（例: 自宅LAN）")
                        .setValue(ep.label)
                        .onChange(async (v) => {
                            ep.label = v;
                            await this.plugin.saveSettings();
                        })
                )
                .addText((t) => {
                    t.setPlaceholder("ws://host:8760/ws")
                        .setValue(ep.url)
                        .onChange(async (v) => {
                            const prev = ep.url.trim();
                            ep.url = v;
                            // 固定選択していた URL を編集したら selection も追従。
                            if (this.plugin.settings.selection === prev) {
                                this.plugin.settings.selection = v.trim();
                            }
                            await this.plugin.saveSettings();
                        });
                    t.inputEl.style.minWidth = "22em";
                })
                .addExtraButton((b) =>
                    b
                        .setIcon("trash")
                        .setTooltip("この候補を削除")
                        .onClick(async () => {
                            const removed = this.plugin.settings.endpoints[i].url.trim();
                            this.plugin.settings.endpoints.splice(i, 1);
                            if (this.plugin.settings.selection === removed) {
                                this.plugin.settings.selection = AUTO;
                            }
                            await this.plugin.saveSettings();
                            this.display();
                        })
                );
            setting.controlEl.style.flexWrap = "wrap";
        });

        new Setting(containerEl).addButton((b) =>
            b
                .setButtonText("＋ 候補を追加")
                .onClick(async () => {
                    this.plugin.settings.endpoints.push({ label: "", url: "" });
                    await this.plugin.saveSettings();
                    this.display();
                })
        );

        // ---- サーバーの状態確認 ----
        containerEl.createEl("h3", { text: "サーバーの状態" });

        const statusEl = containerEl.createEl("p", {
            text: "「接続確認」を押すとサーバーの稼働状況を表示します。",
            cls: "setting-item-description",
        });

        new Setting(containerEl)
            .setName("接続確認")
            .setDesc("選択中の接続先に問い合わせて、モデル・デバイス・設定を表示する。")
            .addButton((b) =>
                b.setButtonText("接続確認").onClick(async () => {
                    const urls = resolveUrls(this.plugin.settings);
                    if (urls.length === 0) {
                        statusEl.setText("接続先が設定されていません。");
                        return;
                    }
                    statusEl.setText("確認中…");
                    const lines: string[] = [];
                    for (const url of urls) {
                        try {
                            const h = await fetchHealth(url);
                            const punct = h.autoPunctuation ? "自動句読点ON" : "自動句読点OFF";
                            lines.push(
                                `✅ ${labelForUrl(this.plugin.settings, url)} (${httpBase(url)}) — ` +
                                `${h.resolvedModel ?? h.model} / ${h.device}・${h.compute} / ` +
                                `beam=${h.beamSize ?? "?"} / ${punct}` +
                                (h.dictError ? ` / ⚠ 辞書エラー: ${h.dictError}` : "")
                            );
                        } catch (e) {
                            lines.push(
                                `❌ ${labelForUrl(this.plugin.settings, url)} (${httpBase(url)}) — ` +
                                `応答なし（${e instanceof Error ? e.message : e}）`
                            );
                        }
                    }
                    statusEl.setText(lines.join("\n"));
                    statusEl.style.whiteSpace = "pre-wrap";
                })
            );

        // ---- ユーザー辞書 ----
        containerEl.createEl("h3", { text: "ユーザー辞書" });
        containerEl.createEl("p", {
            text:
                "誤変換を望む表記に置き換える。サーバー上の userdict.json をここから編集でき、" +
                "保存すると再起動なしで反映される（Androidからも編集可）。",
            cls: "setting-item-description",
        });

        new Setting(containerEl)
            .setName("辞書を編集")
            .setDesc("置換（replacements）と記号語（symbols）の一覧を開く。")
            .addButton((b) =>
                b.setButtonText("辞書を開く").onClick(() => {
                    const urls = resolveUrls(this.plugin.settings);
                    const url = this.plugin.activeUrl() ?? urls[0];
                    if (!url) {
                        new Notice("VoxCraft: 接続先が設定されていません");
                        return;
                    }
                    new DictModal(this.app, url).open();
                })
            );

        // ---- 認識・整形オプション ----
        containerEl.createEl("h3", { text: "認識・整形" });

        new Setting(containerEl)
            .setName("英数字まわりの半角スペースを除去")
            .setDesc("日本語と英数字の間に勝手に入る半角スペースを取り除く。")
            .addToggle((t) =>
                t.setValue(this.plugin.settings.stripJaAlnumSpace).onChange(async (v) => {
                    this.plugin.settings.stripJaAlnumSpace = v;
                    await this.plugin.saveSettings();
                })
            );

        new Setting(containerEl)
            .setName("記号の読み上げ変換")
            .setDesc("「まる」→。、「てん」→、、「かいぎょう」→改行 などを変換する。")
            .addToggle((t) =>
                t.setValue(this.plugin.settings.symbolDictation).onChange(async (v) => {
                    this.plugin.settings.symbolDictation = v;
                    await this.plugin.saveSettings();
                })
            );

        new Setting(containerEl)
            .setName("音声コマンドを有効化")
            .setDesc("「取り消し」「変換戻し」「AをBに修正」「入力終了」等を認識する。")
            .addToggle((t) =>
                t.setValue(this.plugin.settings.enableCommands).onChange(async (v) => {
                    this.plugin.settings.enableCommands = v;
                    await this.plugin.saveSettings();
                })
            );

        new Setting(containerEl)
            .setName("コマンドのプレフィックス語")
            .setDesc(
                "誤爆を防ぐため、この語で始まる発話だけをコマンドとして扱う（例:「コマンド」）。" +
                "空欄なら発話全体がコマンド文のときに判定。"
            )
            .addText((t) =>
                t
                    .setPlaceholder("（空欄可）")
                    .setValue(this.plugin.settings.commandPrefix)
                    .onChange(async (v) => {
                        this.plugin.settings.commandPrefix = v.trim();
                        await this.plugin.saveSettings();
                    })
            );

        // ---- オーディオエンジン情報 ----
        containerEl.createEl("h3", { text: "マイク入力エンジン" });
        containerEl.createEl("p", {
            text:
                "音声処理エンジン: AudioWorklet 優先動作（UI描画と独立した高音質・低レイテンシスレッド）。" +
                "非対応ブラウザ・環境では ScriptProcessorNode へ自動フォールバックされます。",
            cls: "setting-item-description",
        });
    }
}
