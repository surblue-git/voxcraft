import { App, Notice, Platform, PluginSettingTab, Setting } from "obsidian";
import type VoxCraftPlugin from "./main";
import { AudioInputDevice, listAudioInputs } from "./audio";
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
    insertAt: "anchor" | "cursor"; // 口述の挿入位置。anchor=固定アンカー（既定）/ cursor=カーソル追従
    pauseComma: boolean;        // 短い息継ぎでチャンクが切れたら「、」で接続する
    showToolbar: boolean;       // 口述中に画面下部の操作ツールバーを表示する
    suppressKeyboard: boolean;  // 口述中はソフトキーボードを出さない（モバイルのみ）
    keepScreenOn: boolean;      // 文字起こし中は画面を消さない（モバイルのみ）
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
    insertAt: "anchor",
    pauseComma: true,
    showToolbar: true,
    suppressKeyboard: true,
    keepScreenOn: true,
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
    if (s.insertAt !== "cursor") s.insertAt = "anchor";
    if (typeof s.pauseComma !== "boolean") s.pauseComma = true;
    if (typeof s.showToolbar !== "boolean") s.showToolbar = true;
    if (typeof s.suppressKeyboard !== "boolean") s.suppressKeyboard = true;
    if (typeof s.keepScreenOn !== "boolean") s.keepScreenOn = true;
    delete s.serverUrl;
    return s;
}

// 実際に接続を試みる URL 一覧を返す（selection に従う）。
export function resolveUrls(s: VoxCraftSettings): string[] {
    const all = s.endpoints.map((e) => e.url.trim()).filter(Boolean);
    if (s.selection === AUTO || !s.selection) return all;
    return all.includes(s.selection) ? [s.selection] : all;
}

// ---- PC音声（この端末）の入力デバイス ----
//
// 端末ごとに違うものなので、Vault の data.json には入れない。同期で他機へ渡ると
// 存在しない deviceId を指すことになり、録音が始まらない（あるいは別の機械の
// デバイス名が表示される）。localStorage は Vault 単位かつ端末ローカル。
const SYSTEM_INPUT_KEY = "voxcraft:system-input";

export function loadSystemInput(app: App): AudioInputDevice | null {
    const raw = app.loadLocalStorage(SYSTEM_INPUT_KEY);
    if (!raw || typeof raw !== "object") return null;
    const { deviceId, label } = raw as Partial<AudioInputDevice>;
    if (typeof deviceId !== "string" || !deviceId) return null;
    return { deviceId, label: typeof label === "string" ? label : "" };
}

export function saveSystemInput(app: App, choice: AudioInputDevice | null): void {
    app.saveLocalStorage(SYSTEM_INPUT_KEY, choice);
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

    // PC音声を「この端末で取って送る」ための入力デバイス選択。
    // Windows のステレオミキサー（既定では無効なので mmsys.cpl で有効化が要る）や
    // 仮想オーディオケーブルを選ぶと、再生音が普通の録音デバイスとして取れる。
    private displaySystemInput(containerEl: HTMLElement): void {
        containerEl.createEl("h3", { text: "PC音声（この端末）" });
        containerEl.createEl("p", {
            text:
                "コマンド「この端末のPC音声を文字起こし」で使う入力デバイス。" +
                "Windowsの「ステレオ ミキサー」（Win+R → mmsys.cpl → 録音タブ → " +
                "右クリックで「無効なデバイスの表示」→ 有効化）を選ぶと、この端末の" +
                "再生音をそのまま認識サーバーへ送れる。" +
                "サーバー機が別のPCでも、辞書と録音をサーバー側に一本化したまま使える。",
            cls: "setting-item-description",
        });
        containerEl.createEl("p", {
            text:
                "この設定は端末ごとに保存される（Vaultの設定同期には乗らない）。" +
                "デバイス名の一覧を出すためにマイクの許可を一度求めることがある。",
            cls: "setting-item-description",
        });

        const saved = loadSystemInput(this.app);
        new Setting(containerEl)
            .setName("入力デバイス")
            .setDesc("未設定のままだと、このコマンドは開始せずに設定を促す。")
            .addDropdown((d) => {
                const fill = (devices: AudioInputDevice[]) => {
                    d.selectEl.empty();
                    d.addOption("", "（未設定）");
                    for (const dev of devices) d.addOption(dev.deviceId, dev.label);
                    // 保存済みが一覧に無くても選択肢としては残す。ここで黙って
                    // 「未設定」に戻すと、設定を開いただけで選択が消える。
                    if (saved && !devices.some((x) => x.deviceId === saved.deviceId)) {
                        d.addOption(
                            saved.deviceId,
                            `${saved.label || saved.deviceId}（見つかりません）`
                        );
                    }
                    d.setValue(saved?.deviceId ?? "");
                };
                fill([]);
                d.onChange((v) => {
                    if (!v) {
                        saveSystemInput(this.app, null);
                        return;
                    }
                    const label = d.selectEl.selectedOptions[0]?.text ?? "";
                    saveSystemInput(this.app, { deviceId: v, label });
                });
                void listAudioInputs()
                    .then(fill)
                    .catch(() => {
                        new Notice("VoxCraft: 入力デバイスの一覧を取得できませんでした。");
                    });
            });
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

        // ---- PC音声（この端末）の入力デバイス ----
        if (!Platform.isMobile) this.displaySystemInput(containerEl);

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
            .setName("息継ぎで読点を打つ")
            .setDesc(
                "短い息継ぎ（2秒以内）で文が切れたとき、続きを「、」でつないで挿入する。" +
                "読点の位置は話すときの間がそのまま反映される。長い沈黙（考え中）には打たない。" +
                "文字起こしモードでは無効。"
            )
            .addToggle((t) =>
                t.setValue(this.plugin.settings.pauseComma).onChange(async (v) => {
                    this.plugin.settings.pauseComma = v;
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
            .setName("挿入位置をカーソルに追従")
            .setDesc(
                "OFF（既定）: 録音開始時に立てた固定アンカーへ追記し続ける（カーソル移動や背面作業の影響を受けない）。" +
                "ON: 口述中にカーソルを動かすと、次の発話からその位置に挿入される（誤タップで挿入位置が飛ぶ点に注意）。" +
                "文字起こしモードは常にアンカー固定。"
            )
            .addToggle((t) =>
                t.setValue(this.plugin.settings.insertAt === "cursor").onChange(async (v) => {
                    this.plugin.settings.insertAt = v ? "cursor" : "anchor";
                    await this.plugin.saveSettings();
                })
            );

        new Setting(containerEl)
            .setName("録音中に操作ツールバーを表示")
            .setDesc(
                "音声入力（口述）中、画面下部にマイク・入力キャンセル・入力復元・句読点・辞書などの" +
                "ボタンを表示する。モバイルで特に便利。文字起こしモードでは表示しない。"
            )
            .addToggle((t) =>
                t.setValue(this.plugin.settings.showToolbar).onChange(async (v) => {
                    this.plugin.settings.showToolbar = v;
                    await this.plugin.saveSettings();
                })
            );

        if (Platform.isMobile) {
            new Setting(containerEl)
                .setName("録音中はキーボードを出さない")
                .setDesc(
                    "口述中は画面を触ってもソフトキーボードが出ないようにする（ツールバーが隠れないため）。" +
                    "カーソル移動や範囲選択はそのままできる。入力したいときはツールバーの⌨ボタンで出す。"
                )
                .addToggle((t) =>
                    t.setValue(this.plugin.settings.suppressKeyboard).onChange(async (v) => {
                        this.plugin.settings.suppressKeyboard = v;
                        await this.plugin.saveSettings();
                        this.plugin.refreshKeyboardSuppression();
                    })
                );

            new Setting(containerEl)
                .setName("文字起こし中は画面を消さない")
                .setDesc(
                    "文字起こしの録音中、放置による画面の自動消灯を抑える（Android は画面が消えると録音が止まるため）。" +
                    "電源ボタンを押した場合や他アプリに切り替えた場合は OS が解除するので、そこでは止まる。" +
                    "口述（通常の音声入力）には掛からない。"
                )
                .addToggle((t) =>
                    t.setValue(this.plugin.settings.keepScreenOn).onChange(async (v) => {
                        this.plugin.settings.keepScreenOn = v;
                        await this.plugin.saveSettings();
                    })
                );
        }

        new Setting(containerEl)
            .setName("音声コマンドを有効化")
            .setDesc(
                "「入力キャンセル」「入力復元」「変換戻し」「AをBに修正」「Aを再変換」「ここを言い直し」「入力終了」等を認識する。"
            )
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
    }
}
