import { App, PluginSettingTab, Setting } from "obsidian";
import type VoxCraftPlugin from "./main";

export interface VoxCraftSettings {
    serverUrl: string;          // ws://host:port/ws
    stripJaAlnumSpace: boolean; // 日本語と英数字の間の半角スペース除去
    symbolDictation: boolean;   // 「まる」等の記号読み上げ
    enableCommands: boolean;    // 音声コマンドを有効化
    commandPrefix: string;      // 空なら常時判定、非空なら「この語で始まる発話」のみ
    autoReconvertLast: boolean; // 「変換戻し」時、直前チャンクを対象にする
}

export const DEFAULT_SETTINGS: VoxCraftSettings = {
    serverUrl: "ws://localhost:8760/ws",
    stripJaAlnumSpace: true,
    symbolDictation: true,
    enableCommands: true,
    commandPrefix: "",
    autoReconvertLast: true,
};

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

        new Setting(containerEl)
            .setName("認識サーバー URL")
            .setDesc(
                "自宅PCで動く認識サーバーの WebSocket アドレス。" +
                "Desktop は ws://localhost:8760/ws、Android は ws://<TailscaleのPC IP>:8760/ws。"
            )
            .addText((t) =>
                t
                    .setPlaceholder("ws://localhost:8760/ws")
                    .setValue(this.plugin.settings.serverUrl)
                    .onChange(async (v) => {
                        this.plugin.settings.serverUrl = v.trim();
                        await this.plugin.saveSettings();
                    })
            );

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
    }
}
