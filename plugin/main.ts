import { Editor, MarkdownView, Menu, Notice, Platform, Plugin } from "obsidian";
import { EditorView } from "@codemirror/view";

import { DICTATION_MIC, MicRecorder, RAW_MIC } from "./audio";
import { looksLikeRespeak, parseCommand, parseModalCommand } from "./commands";
import { DictModal, fetchDict, fetchReconvert, ReconvertPayload, saveDict } from "./dict";
import { ReconvertModal } from "./suggest";
import {
    AUTO,
    DEFAULT_SETTINGS,
    labelForUrl,
    migrateSettings,
    resolveUrls,
    VoxCraftSettingTab,
    VoxCraftSettings,
} from "./settings";
import {
    AudioSpan,
    RecoverModal,
    formatTime,
    recognizeRange,
    spanRangeFor,
} from "./recover";
import { RecordingsModal } from "./recordings";
import { AsrMode, AsrSocket, ServerMessage } from "./ws";
import { anchorExtension, setAnchor, clearAnchor, getAnchor } from "./anchor";
import { DictationToolbar } from "./toolbar";

// 右クリック相当（Mac では Ctrl+クリックも）か。メニュー用の操作なので録音トグルからは除外する。
function isSecondaryClick(evt: MouseEvent): boolean {
    return evt.button !== 0 || (Platform.isMacOS && evt.ctrlKey);
}

// その位置の直後が「コマンドの言い残し」か。
//
// 認識が途中で切れた命令（例「スミシンを再変」）はコマンドとして成立せず本文に残る。
// これを検索対象にすると、直そうとした語ではなく言い残しの方を置換してしまうため
// （実際に「スミシンを再変」が「住信を再変」になった）、後方検索から除外する。
function isCommandEcho(text: string, at: number): boolean {
    return /^を(?:再変|修正|変換|言い直|訂正)/.test(text.slice(at, at + 5));
}

// 「Aを再変換」で文書中を探すための表記一覧を作る。
// 発話どおりの表記を最優先にし、読みからの変換候補（文節が複数なら上位5件ずつの
// 組み合わせ・最大50通り）を続ける。誤変換の表記は候補に現れやすい。
function buildSurfaces(
    target: string,
    segments: { reading: string; candidates: string[] }[]
): string[] {
    const MAX = 50;
    let combos: string[] = [""];
    for (const seg of segments) {
        const cands = seg.candidates.slice(0, 5);
        if (cands.length === 0) continue;
        const next: string[] = [];
        for (const head of combos) {
            for (const c of cands) {
                next.push(head + c);
                if (next.length >= MAX) break;
            }
            if (next.length >= MAX) break;
        }
        combos = next;
    }
    const seen = new Set<string>();
    const out: string[] = [];
    for (const s of [target, ...combos]) {
        if (s && !seen.has(s)) {
            seen.add(s);
            out.push(s);
        }
    }
    return out;
}

export default class VoxCraftPlugin extends Plugin {
    settings: VoxCraftSettings;

    private socket: AsrSocket | null = null;
    private recorder: MicRecorder | null = null;
    private recording = false;
    private statusEl: HTMLElement;
    private ribbonEl: HTMLElement;

    // 口述対象として録音開始時に固定するエディタ（背面作業やノート切替の影響を受けない）。
    private cm: EditorView | null = null;
    // アンカーに追記したチャンク列（取り消し・変換戻しの対象特定に使う。アンカー相対）。
    private chunks: string[] = [];
    // 変換戻しの応答が返るまで対象範囲を覚えておく。
    private pendingReconvert: { from: number; to: number } | null = null;
    private reconvertModal: ReconvertModal | null = null;
    // 「ここを言い直し」で覚えた選択範囲。次の発話1回だけがこの範囲を置換する。
    private pendingRespeak: { from: number; to: number; text: string } | null = null;
    // コマンド実行等で発話の流れが切れた直後は、次のチャンクに息継ぎ読点を打たない。
    private suppressJoiner = true;
    // 入力キャンセルで削除した確定チャンク。「元に戻す」で再挿入する（口述のみ）。
    private canceled: string[] = [];
    // 画面下部の操作ツールバー（口述モードの録音中に表示）。
    private toolbar: DictationToolbar | null = null;
    // ツールバーのマイクボタンで停止したときはバーを残す（そこから再開できるように）。
    private keepToolbarOnStop = false;

    // 入力レベルメーター表示のスロットリング用。
    private lastMeterAt = 0;
    private transcribing = false;

    // 接続先メニューを開いた時刻。直後に飛んでくるクリックで録音を始めないための目印。
    private menuOpenedAt = 0;

    // ---- 文字起こしモード（自分の声での口述には一切関与しない） ----
    private mode: AsrMode = "dictation";
    // サーバーが音声を保存しているセッションID。復旧の材料。
    private session: string | null = null;
    // 挿入したテキストと、その元になった音声の位置（秒）。再認識の宛先を引くのに使う。
    private spans: AudioSpan[] = [];
    // 直前チャンクの音声終端。次チャンクとの間が空いていれば、そこは捨てられた音声。
    private lastAudioEnd = 0;

    async onload(): Promise<void> {
        await this.loadSettings();

        // 口述アンカー（CM6 拡張）を全エディタに登録。
        this.registerEditorExtension(anchorExtension);

        this.ribbonEl = this.addRibbonIcon("mic", "VoxCraft 音声入力の開始/停止", (evt) => {
            // 右クリック（Macは Ctrl+クリック）はメニュー専用。長押し直後の合成クリックも弾く。
            if (isSecondaryClick(evt)) return;
            if (Date.now() - this.menuOpenedAt < 700) return;
            this.toggleRecording();
        });
        // マイクのリボンを右クリック（モバイルは長押し）で接続先メニューを出す。
        this.registerDomEvent(this.ribbonEl, "contextmenu", (evt: MouseEvent) => {
            evt.preventDefault();
            this.menuOpenedAt = Date.now();
            this.openEndpointMenu(evt);
        });
        // Obsidian 本体のリボン用ハンドラが押下系イベントで発火する場合に備え、
        // 副ボタンのイベントはリボンに届く前（親のキャプチャ段階）で止める。
        const swallowSecondary = (evt: MouseEvent) => {
            const target = evt.target;
            if (!(target instanceof Node) || !this.ribbonEl.contains(target)) return;
            if (isSecondaryClick(evt)) evt.stopPropagation();
        };
        const ribbonParent = this.ribbonEl.parentElement ?? this.ribbonEl;
        this.registerDomEvent(ribbonParent, "mousedown", swallowSecondary, { capture: true });
        this.registerDomEvent(ribbonParent, "mouseup", swallowSecondary, { capture: true });
        this.registerDomEvent(ribbonParent, "auxclick", swallowSecondary, { capture: true });

        this.statusEl = this.addStatusBarItem();
        this.setStatus("停止中");

        this.addCommand({
            id: "toggle-recording",
            name: "音声入力の開始/停止",
            callback: () => this.toggleRecording(),
        });
        this.addCommand({
            id: "stop-recording",
            name: "音声入力を停止",
            callback: () => this.stopRecording(),
        });
        this.addCommand({
            id: "toggle-transcribe",
            name: "文字起こし（動画・会議）の開始/停止",
            callback: () => {
                if (this.recording) this.stopRecording();
                else void this.startRecording("transcribe");
            },
        });
        this.addCommand({
            id: "cancel-last-input",
            name: "直前の入力をキャンセル（一文削除）",
            callback: () => this.cancelLast(),
        });
        this.addCommand({
            id: "restore-canceled-input",
            name: "キャンセルした入力を元に戻す",
            callback: () => this.restoreCanceled(),
        });
        this.addCommand({
            id: "recover-selection",
            name: "選択範囲を音声から再認識（復旧）",
            callback: () => void this.recoverSelection(),
        });
        this.addCommand({
            id: "manage-recordings",
            name: "文字起こしの録音を整理",
            callback: () => this.openRecordingsModal(),
        });
        this.addCommand({
            id: "reconvert-last",
            name: "直前の入力を変換戻し",
            callback: () => this.requestReconvert(),
        });
        this.addCommand({
            id: "reconvert-selection",
            name: "選択範囲を再変換",
            callback: () => void this.reconvertSelection(),
        });
        // 音声の「ここを言い直し」は認識が化けると起動できない。手で確実に起動できる
        // 経路を用意する（ホットキーを割り当てられる）。選択はどうせ手で行う操作。
        this.addCommand({
            id: "respeak-selection",
            name: "選択範囲を言い直す（次の発話で置き換え）",
            callback: () => this.startRespeak(),
        });
        this.addCommand({
            id: "select-endpoint",
            name: "接続先を選択",
            callback: () => this.openEndpointMenu(),
        });
        this.addCommand({
            id: "edit-userdict",
            name: "ユーザー辞書を編集",
            callback: () => this.openDictModal(),
        });

        this.addSettingTab(new VoxCraftSettingTab(this.app, this));
    }

    async onunload(): Promise<void> {
        await this.teardownSession();
    }

    // ---- 録音セッション制御 ----

    private toggleRecording(): void {
        if (this.recording) this.stopRecording();
        else void this.startRecording();
    }

    private async startRecording(mode: AsrMode = "dictation"): Promise<void> {
        if (this.recording) return;
        const cm = this.getActiveCm();
        if (!cm) {
            new Notice("VoxCraft: 挿入先のノート（編集モード）を開いてください。");
            return;
        }

        const urls = resolveUrls(this.settings);
        if (urls.length === 0) {
            new Notice("VoxCraft: 接続先が未設定です。設定でエンドポイントを追加してください。");
            return;
        }

        this.setStatus(urls.length > 1 ? "接続中…（つながる方を選択）" : "接続中…");
        this.socket = new AsrSocket(urls, {
            onReady: () => {
                const url = this.socket?.activeUrl;
                const label = url ? labelForUrl(this.settings, url) : "";
                this.setStatus(label ? `待機中… 話してください（${label}）` : "待機中… 話してください");
            },
            onPartial: () => {
                this.transcribing = true;
                this.setStatus("認識中…");
            },
            onSession: (id) => {
                this.session = id;
            },
            onChunk: (text, msg) => {
                this.transcribing = false;
                this.handleChunk(text, msg);
            },
            onReconvert: (msg) => this.handleReconvert(msg),
            onError: (m) => new Notice(`VoxCraft サーバーエラー: ${m}`),
            onClose: () => {
                if (this.recording) {
                    new Notice("VoxCraft: サーバー接続が切れました。");
                    void this.teardownSession();
                    this.setStatus("停止中");
                }
            },
        });

        try {
            await this.socket.connect();
        } catch (e) {
            const tried = urls.map((u) => labelForUrl(this.settings, u)).join(" / ");
            new Notice(
                `VoxCraft: サーバーに接続できません（${tried}）。` +
                "認識サーバーが起動しているか、接続先の設定を確認してください。"
            );
            this.socket = null;
            this.setStatus("停止中");
            return;
        }

        this.mode = mode;
        this.socket.sendStart(
            this.settings.stripJaAlnumSpace,
            this.settings.symbolDictation,
            mode
        );

        this.recorder = new MicRecorder(
            (pcm) => this.socket?.sendAudio(pcm),
            (level) => this.showLevel(level),
            // 文字起こしは自分の声ではなく会場や再生音を拾う。ブラウザの前処理は
            // 近接した1人の声を前提にしているので、ここでは無効化して原音を送る。
            mode === "transcribe" ? RAW_MIC : DICTATION_MIC
        );
        try {
            await this.recorder.start();
        } catch (e) {
            new Notice("VoxCraft: マイクにアクセスできませんでした。");
            await this.teardownSession();
            this.setStatus("停止中");
            return;
        }

        // 口述対象を固定し、現在のカーソル位置にアンカーを立てる。
        this.cm = cm;
        this.chunks = [];
        this.canceled = [];
        this.pendingReconvert = null;
        this.pendingRespeak = null;
        this.suppressJoiner = true; // 最初のチャンクには息継ぎ読点を打たない
        setAnchor(cm, cm.state.selection.main.head);

        if (mode === "transcribe") {
            this.spans = [];
            this.lastAudioEnd = 0;
            this.session = null;
        }

        this.recording = true;
        this.ribbonEl.addClass("voxcraft-recording");
        this.setStatus(mode === "transcribe" ? "● 文字起こし中" : "● 録音中");
        // ツールバーは口述専用。文字起こしでは本文操作系が誤動作しないよう出さない。
        if (mode === "dictation") this.showToolbar();
        else this.hideToolbar();
    }

    stopRecording(): void {
        if (!this.recording) return;
        this.recording = false;
        this.ribbonEl.removeClass("voxcraft-recording");
        // ツールバーのマイクで止めたときはバーを残し、そこから再開できるようにする。
        this.toolbar?.setRecording(false);
        if (this.keepToolbarOnStop) this.keepToolbarOnStop = false;
        else this.hideToolbar();
        this.setStatus("停止処理中…");
        // 残り音声を確定させてから閉じる。
        this.socket?.sendStop();
        void this.recorder?.stop().then(() => {
            this.recorder = null;
        });
        // サーバーが末尾チャンクを返す猶予を置いてから閉じ、アンカーを片付ける。
        window.setTimeout(() => {
            this.socket?.close();
            this.socket = null;
            this.clearDictationAnchor();
            this.pendingRespeak = null;
            this.setStatus("停止中");
        }, 1500);
    }

    private async teardownSession(): Promise<void> {
        this.recording = false;
        this.ribbonEl?.removeClass("voxcraft-recording");
        this.hideToolbar();
        await this.recorder?.stop();
        this.recorder = null;
        this.socket?.close();
        this.socket = null;
        this.clearDictationAnchor();
        this.pendingRespeak = null;
    }

    private clearDictationAnchor(): void {
        if (this.cm && this.cm.dom.isConnected) clearAnchor(this.cm);
        this.cm = null;
    }

    // ---- 確定チャンクの処理 ----

    private handleChunk(text: string, msg?: ServerMessage): void {
        if (this.mode === "transcribe") {
            this.handleTranscribeChunk(text, msg);
            return;
        }
        // 候補モーダルが開いている間は、発話を本文へ入れずにモーダル操作として扱う。
        // 「3番」が「サンバー」と認識されて本文に混ざった実例への対処。
        // 解釈できなかった発話は捨てたことを必ず知らせる（無言で消さない）。
        if (this.reconvertModal) {
            const modalCmd = parseModalCommand(text);
            if (modalCmd) {
                this.runCommand(modalCmd);
            } else if (text.trim()) {
                new Notice(
                    `VoxCraft: 候補選択中のため本文に入れませんでした —「${text}」\n` +
                    "「3番」で選択、「確定」/「キャンセル」で閉じます。"
                );
            }
            this.suppressJoiner = true;
            return;
        }
        // 以下は従来どおりの口述処理（音声コマンドの判定を含む）。
        if (this.settings.enableCommands) {
            const cmd = parseCommand(text, this.settings.commandPrefix);
            // 「確定」「キャンセル」は対象（モーダル等）が無ければ本文として挿入する。
            if (cmd && this.runCommand(cmd)) {
                this.suppressJoiner = true;
                return;
            }
            // 起動語リストから漏れた「言い直し」は、選択範囲があるときだけ拾う。
            if (!cmd && this.hasSelection() && looksLikeRespeak(text)) {
                this.startRespeak();
                this.suppressJoiner = true;
                return;
            }
        }
        // 「ここを言い直し」の直後の発話は、アンカーではなく覚えた範囲を置換する。
        if (this.pendingRespeak) {
            this.applyRespeak(text);
            this.suppressJoiner = true;
            return;
        }
        this.insertText(this.withPauseComma(text, msg));
    }

    // 息継ぎ読点: 直前の発話から短い間（息継ぎ）で続いたチャンクを「、」でつなぐ。
    // 読点の位置＝話すときの間、という日本語の自然な対応をそのまま使う。
    // 長い沈黙（考え中）や、コマンドで流れが切れた直後には打たない。
    private withPauseComma(text: string, msg?: ServerMessage): string {
        const suppress = this.suppressJoiner;
        this.suppressJoiner = false;
        if (!this.settings.pauseComma || suppress || !text) return text;

        const pause = msg?.pause;
        if (typeof pause !== "number" || pause <= 0 || pause > 2.0) return text;
        // チャンク自体が記号・閉じ括弧などで始まるときは付けない（「、、」防止）。
        if (/^[、。！？!?…・：\n）」』]/.test(text)) return text;

        const cm = this.cm;
        if (!cm || !cm.dom.isConnected) return text;
        const anchor = getAnchor(cm);
        if (anchor === null || anchor <= 0) return text;
        // カーソル追従で挿入先が移る場合は文脈が切れているので付けない。
        if (this.settings.insertAt === "cursor" && cm.state.selection.main.head !== anchor) {
            return text;
        }
        // 直前の文字が句読点・改行・開き括弧・空白なら付けない。
        const prev = cm.state.doc.sliceString(anchor - 1, anchor);
        if (!prev || /[、。！？!?…・：\s\n「『（(]/.test(prev)) return text;

        return "、" + text;
    }

    // 文字起こしモードのチャンク処理。
    // 音声コマンドは判定しない（動画側の発話で勝手にコマンドが走るのを防ぐ）。
    // 捨てられた音声区間は欠落マーカーとして本文に残し、後から復旧できるようにする。
    private handleTranscribeChunk(text: string, msg?: ServerMessage): void {
        const start = msg?.start;
        const end = msg?.end;

        if (msg?.dropped?.length) {
            new Notice(
                `VoxCraft: ${msg.dropped.length}件のセグメントを低確信として除外しました` +
                "（サーバーログに全文あり）。"
            );
        }

        // 前チャンクの終端と今回の開始が離れていれば、その間の音声はどこにも出ていない。
        // 黙って消さずに位置を残す ＝ 選んで再認識すれば取り戻せる。
        if (start !== undefined && this.lastAudioEnd > 0 && start - this.lastAudioEnd >= 0.35) {
            const gapStart = this.lastAudioEnd;
            const marker = `⟨未認識 ${formatTime(gapStart)}–${formatTime(start)}⟩`;
            this.insertText(marker);
            this.spans.push({ text: marker, start: gapStart, end: start });
        }

        if (text) {
            this.insertText(text);
            if (start !== undefined && end !== undefined) {
                this.spans.push({ text, start, end });
                if (this.spans.length > 2000) this.spans.shift();
            }
        }
        if (end !== undefined) this.lastAudioEnd = end;
    }

    // コマンドを実行し、処理したら true を返す。false ならチャンクは本文として扱われる。
    private runCommand(cmd: NonNullable<ReturnType<typeof parseCommand>>): boolean {
        switch (cmd.kind) {
            case "stop":
                this.stopRecording();
                return true;
            case "undo":
                this.undoLast();
                return true;
            case "newline":
                this.insertText("\n");
                return true;
            case "reconvert":
                this.requestReconvert();
                return true;
            case "replace":
                this.replaceInDoc(cmd.from, cmd.to);
                return true;
            case "pick":
                // モーダルが無ければコマンドではない（「3番」等を本文として残す）。
                if (this.reconvertModal) {
                    this.reconvertModal.pickByVoice(cmd.index);
                    return true;
                }
                return false;
            case "reconvertTarget":
                void this.reconvertByTarget(cmd.target);
                return true;
            case "reconvertSelection":
                void this.reconvertSelection();
                return true;
            case "respeak":
                // 選択が無ければコマンドとして扱わない。「訂正」のような普通の語も
                // 起動語にできるのは、この条件で本文が壊れないから。
                if (!this.hasSelection()) return false;
                this.startRespeak();
                return true;
            case "confirm":
                // 候補モーダルが開いているときだけ意味を持つ。無ければ本文へ。
                if (this.reconvertModal) {
                    this.reconvertModal.confirmByVoice();
                    return true;
                }
                return false;
            case "cancel":
                if (this.reconvertModal) {
                    this.reconvertModal.close();
                    return true;
                }
                if (this.pendingRespeak) {
                    this.pendingRespeak = null;
                    this.setStatus(this.idleStatus());
                    new Notice("VoxCraft: 言い直しを解除しました。");
                    return true;
                }
                return false;
        }
        return false;
    }

    // アンカー位置に追記する。ユーザーが手動でカーソルを離していれば、
    // その編集位置は動かさず（follow=false）、アンカーにだけ差し込む。
    private insertText(text: string): void {
        const cm = this.cm;
        if (!cm || !cm.dom.isConnected) return;
        let anchor = getAnchor(cm);
        if (anchor === null) return;

        // カーソル追従（設定）: 口述モードでカーソルが同じエディタ内の別位置にあれば、
        // アンカーをそこへ移してから挿入する。「アンカー = 直近挿入の末尾」の関係は
        // 保たれるので、取り消し・変換戻しのアンカー相対ロジックはそのまま成立する。
        // カーソルが別ノートにある場合はこのエディタの selection は動いていないため、
        // 自然にアンカー据え置き（従来の安定性）になる。
        if (
            this.mode === "dictation" &&
            this.settings.insertAt === "cursor" &&
            cm.state.selection.main.head !== anchor
        ) {
            anchor = cm.state.selection.main.head;
            setAnchor(cm, anchor);
            // 移動をまたぐ取り消し・変換戻しは成立しないため、チャンク列は仕切り直す。
            this.chunks = [];
        }

        const at = Math.min(anchor, cm.state.doc.length);
        const follow = cm.state.selection.main.head === anchor;

        cm.dispatch({
            changes: { from: at, to: at, insert: text },
            selection: follow ? { anchor: at + text.length } : undefined,
            scrollIntoView: follow,
        });
        // アンカーは StateField 側で at+text.length へ自動前進する。

        this.chunks.push(text);
        if (this.chunks.length > 200) this.chunks.shift();
    }

    // 直前チャンク（アンカー直前の text.length 文字）を削除する。
    // 音声「取り消し」（silent）とツールバー/コマンドの「入力キャンセル」の共通実装。
    // 削除した文は canceled に積み、「元に戻す」で再挿入できる。文字起こしでは動かない
    // （動画側の本文を勝手に消さない）。
    private cancelLast(opts: { silent?: boolean } = {}): void {
        if (this.mode === "transcribe" && this.recording) {
            if (!opts.silent) new Notice("VoxCraft: 入力キャンセルは文字起こしでは使えません。");
            return;
        }
        const cm = this.cm;
        const last = this.chunks[this.chunks.length - 1];
        if (!cm || !cm.dom.isConnected || last === undefined) {
            if (!opts.silent) new Notice("VoxCraft: キャンセルできる入力がありません（音声入力中に使ってください）。");
            return;
        }
        const anchor = getAnchor(cm);
        if (anchor === null) return;

        const from = Math.max(0, anchor - last.length);
        const current = cm.state.doc.sliceString(from, anchor);
        if (current !== last) {
            new Notice("VoxCraft: 直前の入力が編集されているため取り消せません。");
            return;
        }
        this.chunks.pop();
        cm.dispatch({ changes: { from, to: anchor, insert: "" } });
        this.canceled.push(last);
        if (this.canceled.length > 50) this.canceled.shift();
        // ボタン操作で発話の流れは切れているので、次のチャンクに息継ぎ読点を打たない。
        this.suppressJoiner = true;
        if (!opts.silent) {
            const t = last.trim();
            const preview = t.length > 20 ? t.slice(0, 20) + "…" : t;
            new Notice(`VoxCraft: キャンセルしました —「${preview}」（「元に戻す」で復活）`);
        }
    }

    private undoLast(): void {
        this.cancelLast({ silent: true });
    }

    // 「元に戻す」: 入力キャンセルで消した文を、アンカー位置に再挿入する。
    private restoreCanceled(): void {
        const text = this.canceled[this.canceled.length - 1];
        if (text === undefined) {
            new Notice("VoxCraft: 元に戻せる入力がありません。");
            return;
        }
        const cm = this.cm;
        if (!cm || !cm.dom.isConnected || getAnchor(cm) === null) {
            new Notice("VoxCraft: 音声入力中のみ元に戻せます。");
            return;
        }
        this.canceled.pop();
        this.insertText(text);
        this.suppressJoiner = true;
    }

    // ---- 画面下部の操作ツールバー（口述専用） ----

    private showToolbar(): void {
        if (!this.settings.showToolbar) return;
        if (!this.toolbar) {
            this.toolbar = new DictationToolbar({
                onMicToggle: () => {
                    if (this.recording) {
                        this.keepToolbarOnStop = true;
                        this.stopRecording();
                    } else {
                        void this.startRecording();
                    }
                },
                onCancel: () => this.cancelLast(),
                onRestore: () => this.restoreCanceled(),
                onInsert: (text) => this.insertFromToolbar(text),
                onOpenDict: () => this.openDictModal(),
                onClose: () => this.hideToolbar(),
            });
        }
        this.toolbar.show();
        this.toolbar.setRecording(true);
    }

    private hideToolbar(): void {
        this.toolbar?.hide();
    }

    // ツールバーからの句読点・改行挿入。通常チャンクと同じ扱い（取り消し対象になる）。
    private insertFromToolbar(text: string): void {
        if (!this.recording || this.mode !== "dictation") {
            new Notice("VoxCraft: 音声入力中に使ってください。");
            return;
        }
        this.insertText(text);
        this.suppressJoiner = true;
    }

    // 「AをBに修正」: ノート全体からAをBに置換（過去テキストも対象）。
    private replaceInDoc(from: string, to: string): void {
        const cm = this.cm ?? this.getActiveCm();
        if (!cm) return;
        const doc = cm.state.doc.toString();
        const idx = doc.lastIndexOf(from);
        if (idx < 0) {
            new Notice(`VoxCraft: 「${from}」が見つかりませんでした。`);
            return;
        }
        cm.dispatch({ changes: { from: idx, to: idx + from.length, insert: to } });
    }

    // ---- 読みベース再変換（「Aを再変換」/ 選択範囲の再変換） ----

    // 現在の基本ステータス表記（非同期処理から戻すときに使う）。
    private idleStatus(): string {
        if (!this.recording) return "停止中";
        return this.mode === "transcribe" ? "● 文字起こし中" : "● 録音中";
    }

    // 「Aを再変換」: 誤変換でも読みは正しいことを利用する。
    // A の読みから得た変換候補（誤変換の表記もここに現れやすい）で文書を探し、
    // 見つかった箇所を候補モーダルで置き換える。
    private async reconvertByTarget(target: string): Promise<void> {
        const cm = this.cm && this.cm.dom.isConnected ? this.cm : this.getActiveCm();
        if (!cm) return;
        const url = this.activeUrl();
        if (!url) {
            new Notice("VoxCraft: 接続先が設定されていません。");
            return;
        }

        this.setStatus("変換候補を取得中…");
        let payload: ReconvertPayload;
        try {
            payload = await fetchReconvert(url, target);
        } catch (e) {
            this.setStatus(this.idleStatus());
            new Notice(`VoxCraft: 変換候補を取得できません（${e instanceof Error ? e.message : e}）`);
            return;
        }
        this.setStatus(this.idleStatus());
        if (!payload.online) {
            new Notice("VoxCraft: オフラインのため変換候補を取得できませんでした。");
            return;
        }

        const surfaces = buildSurfaces(target, payload.segments);
        const hit = this.findLastSurface(cm, surfaces);
        if (!hit) {
            new Notice(
                `VoxCraft: 「${target}」に相当する箇所が見つかりません。` +
                "該当箇所を選択して「選択範囲を再変換」を使ってください。"
            );
            return;
        }
        this.openReconvertModalFor(hit, cm.state.doc.sliceString(hit.from, hit.to), payload, cm);
    }

    // 表記候補を「直近の口述領域 → ノート全文」の順で後方検索する。
    private findLastSurface(
        cm: EditorView,
        surfaces: string[]
    ): { from: number; to: number } | null {
        const doc = cm.state.doc.toString();

        const searchIn = (winStart: number, winEnd: number, skipEcho: boolean) => {
            const win = doc.slice(winStart, winEnd);
            let best: { from: number; to: number } | null = null;
            for (const s of surfaces) {
                if (!s) continue;
                // 後方から順に見て、コマンドの言い残し以外の最初の出現を採る。
                let at = win.lastIndexOf(s);
                while (skipEcho && at >= 0) {
                    if (!isCommandEcho(win, at + s.length)) break;
                    at = at === 0 ? -1 : win.lastIndexOf(s, at - 1);
                }
                if (at < 0) continue;
                if (!best || winStart + at > best.from) {
                    best = { from: winStart + at, to: winStart + at + s.length };
                }
            }
            return best;
        };

        // 直近の口述領域（アンカーから今回のチャンク分だけ遡った範囲）。
        const anchor = getAnchor(cm);
        const winStart =
            anchor === null
                ? null
                : Math.max(0, Math.min(anchor, doc.length) - this.chunks.reduce((n, c) => n + c.length, 0));
        const winEnd = anchor === null ? null : Math.min(anchor, doc.length);

        // 直近 → 全文の順に、まず言い残しを避けて探す。
        if (winStart !== null && winEnd !== null) {
            const hit = searchIn(winStart, winEnd, true);
            if (hit) return hit;
        }
        const hit = searchIn(0, doc.length, true);
        if (hit) return hit;
        // どこにも無ければ言い残しも許して探す（本文が偶然「参加を修正」のような
        // 並びになっている場合に、直せないより直せる方を選ぶ）。
        return searchIn(0, doc.length, false);
    }

    // 選択範囲の再変換: タッチ/マウスで選んだ誤変換を候補から直す。
    // REST 経由なので録音していなくても使える（外来語・英字ミスの確実な逃げ道）。
    private async reconvertSelection(): Promise<void> {
        const cm = this.cm && this.cm.dom.isConnected ? this.cm : this.getActiveCm();
        if (!cm) {
            new Notice("VoxCraft: ノートを編集モードで開いてください。");
            return;
        }
        const sel = cm.state.selection.main;
        if (sel.empty) {
            new Notice("VoxCraft: 再変換したい範囲を選択してください。");
            return;
        }
        if (sel.to - sel.from > 200) {
            new Notice("VoxCraft: 選択が長すぎます（200文字まで）。");
            return;
        }
        const url = this.activeUrl();
        if (!url) {
            new Notice("VoxCraft: 接続先が設定されていません。");
            return;
        }

        const text = cm.state.doc.sliceString(sel.from, sel.to);
        this.setStatus("変換候補を取得中…");
        let payload: ReconvertPayload;
        try {
            payload = await fetchReconvert(url, text);
        } catch (e) {
            this.setStatus(this.idleStatus());
            new Notice(`VoxCraft: 変換候補を取得できません（${e instanceof Error ? e.message : e}）`);
            return;
        }
        this.setStatus(this.idleStatus());
        if (!payload.online) {
            new Notice("VoxCraft: オフラインのため変換候補を取得できませんでした。");
            return;
        }
        this.openReconvertModalFor({ from: sel.from, to: sel.to }, text, payload, cm);
    }

    // 新規経路共通: 候補モーダルを開き、確定時に検証付きで置換する。
    private openReconvertModalFor(
        range: { from: number; to: number },
        originalText: string,
        payload: ReconvertPayload,
        cm: EditorView
    ): void {
        const segments = payload.segments || [];
        if (segments.length === 0) {
            new Notice("VoxCraft: 変換候補が得られませんでした。");
            return;
        }
        const modal = new ReconvertModal(
            this.app,
            segments,
            (chosen) => this.applyRangedReplace(range, originalText, chosen.join(""), cm),
            {
                originalText,
                onRegister: (f, t) => void this.registerReplacement(f, t),
            }
        );
        this.adoptModal(modal);
    }

    // 候補モーダルを音声操作の受け皿として登録し、閉じるまで応答速度優先に切り替える。
    private adoptModal(modal: ReconvertModal): void {
        const origClose = modal.onClose.bind(modal);
        modal.onClose = () => {
            origClose();
            if (this.reconvertModal === modal) {
                this.reconvertModal = null;
                this.setFastMode(false);
            }
        };
        this.reconvertModal = modal;
        modal.open();
        this.setFastMode(true);
    }

    // 候補選択中だけ、サーバーを「短い発話に速く応える」設定へ寄せる。
    // 「3番」の反応が遅い（無音待ち0.5秒＋beam=5）ことへの対処。閉じたら必ず戻す。
    private setFastMode(on: boolean): void {
        if (this.mode !== "dictation") return;
        this.socket?.sendTune(on);
    }

    // 覚えていた範囲を検証してから置換する。モーダル操作中（録音継続中の追記等）に
    // 文書が変わって位置がズレても、表記の再検索で追従し、失敗時は本文を壊さない。
    private applyRangedReplace(
        range: { from: number; to: number },
        originalText: string,
        newText: string,
        cmIn: EditorView
    ): void {
        const cm = cmIn.dom.isConnected ? cmIn : this.getActiveCm();
        if (!cm || newText === originalText) return;
        const doc = cm.state.doc;
        let to = Math.min(range.to, doc.length);
        let from = Math.min(range.from, to);
        if (doc.sliceString(from, to) !== originalText) {
            const idx = doc.toString().lastIndexOf(originalText);
            if (idx < 0) {
                new Notice("VoxCraft: 対象が編集されたため置換できませんでした。");
                return;
            }
            from = idx;
            to = idx + originalText.length;
        }
        cm.dispatch({ changes: { from, to, insert: newText } });
    }

    // 「確定して辞書に登録」: 元表記→確定表記をサーバーの置換辞書へ追加する。
    // 以後は認識直後の後処理で自動修正される（保存は即時反映・再起動不要）。
    private async registerReplacement(from: string, to: string): Promise<void> {
        if (!from || !to || from === to) return;
        if (from.length < 2) {
            new Notice("VoxCraft: 1文字のキーは誤置換しやすいため登録しません（辞書画面から登録してください）。");
            return;
        }
        if (from.length > 64 || to.length > 128) {
            new Notice("VoxCraft: 登録できる長さを超えています（キー64字・値128字まで）。");
            return;
        }
        const url = this.activeUrl();
        if (!url) {
            new Notice("VoxCraft: 接続先が設定されていません。");
            return;
        }
        try {
            const d = await fetchDict(url);
            d.replacements[from] = to;
            await saveDict(url, d);
            new Notice(`VoxCraft: 辞書に登録しました — ${from} → ${to}（以後自動修正）`);
        } catch (e) {
            new Notice(`VoxCraft: 辞書に登録できません — ${e instanceof Error ? e.message : e}`, 8000);
        }
    }

    // ---- 言い直し（読み自体が壊れた完全誤認識の修正） ----

    // 口述対象のエディタに選択範囲があるか（言い直しコマンドの成立条件）。
    private hasSelection(): boolean {
        const cm = this.cm;
        if (!cm || !cm.dom.isConnected) return false;
        return !cm.state.selection.main.empty;
    }

    // 「ここを言い直し」: 選択範囲を覚え、次の発話1回だけをその範囲への置換にする。
    private startRespeak(): void {
        const cm = this.cm;
        if (!cm || !cm.dom.isConnected) {
            new Notice("VoxCraft: 録音中に、置き換えたい範囲を選択して使ってください。");
            return;
        }
        const sel = cm.state.selection.main;
        if (sel.empty) {
            new Notice("VoxCraft: 言い直したい範囲を選択してから「ここを言い直し」と言ってください。");
            return;
        }
        this.pendingRespeak = {
            from: sel.from,
            to: sel.to,
            text: cm.state.doc.sliceString(sel.from, sel.to),
        };
        this.setStatus("言い直し待ち — 次の発話で置換");
    }

    // 言い直しの発話を、覚えていた範囲に検証付きで適用する。
    private applyRespeak(text: string): void {
        const pr = this.pendingRespeak;
        this.pendingRespeak = null;
        this.setStatus(this.idleStatus());
        const cm = this.cm;
        if (!pr || !cm || !cm.dom.isConnected) {
            this.insertText(text);
            return;
        }
        const doc = cm.state.doc;
        let to = Math.min(pr.to, doc.length);
        let from = Math.min(pr.from, to);
        if (doc.sliceString(from, to) !== pr.text) {
            const idx = doc.toString().lastIndexOf(pr.text);
            if (idx < 0) {
                new Notice("VoxCraft: 言い直し対象が編集されていたため、通常どおり挿入します。");
                this.insertText(text);
                return;
            }
            from = idx;
            to = idx + pr.text.length;
        }
        cm.dispatch({ changes: { from, to, insert: text } });
        // アンカー相対の取り消し整合を壊さないため chunks には積まない（戻すなら Ctrl+Z）。
    }

    // ---- 復旧（音声からの再認識） ----

    // 選択したテキストの元になった音声区間を、精度優先でもう一度認識し直す。
    // 誤変換（例:「一昨日」→「昨日」）も、欠落マーカーの区間も、これで戻せる。
    private async recoverSelection(): Promise<void> {
        const cm = this.getActiveCm();
        if (!cm) {
            new Notice("VoxCraft: ノートを編集モードで開いてください。");
            return;
        }
        const sel = cm.state.selection.main;
        if (sel.empty) {
            new Notice("VoxCraft: 復旧したい範囲を選択してください。");
            return;
        }
        if (!this.session) {
            new Notice("VoxCraft: 復旧できる録音がありません（文字起こしモードで録音した分のみ対象）。");
            return;
        }
        const url = this.activeUrl();
        if (!url) {
            new Notice("VoxCraft: 接続先が設定されていません。");
            return;
        }

        const selected = cm.state.doc.sliceString(sel.from, sel.to);
        const range = spanRangeFor(this.spans, selected);
        if (!range) {
            new Notice("VoxCraft: 選択範囲に対応する音声が見つかりません（この録音の出力を選んでください）。");
            return;
        }

        this.setStatus("音声から再認識中…");
        try {
            const result = await recognizeRange(url, this.session, range.start, range.end);
            this.setStatus(this.recording ? "● 文字起こし中" : "停止中");
            new RecoverModal(this.app, selected, result, (text) => {
                cm.dispatch({ changes: { from: sel.from, to: sel.to, insert: text } });
                // 置き換えた範囲も、元の音声とひも付け直しておく（再度やり直せるように）。
                this.spans.push({ text, start: range.start, end: range.end });
            }).open();
        } catch (e) {
            this.setStatus(this.recording ? "● 文字起こし中" : "停止中");
            new Notice(`VoxCraft: 再認識に失敗しました（${e instanceof Error ? e.message : String(e)}）`);
        }
    }

    // ---- 変換戻し ----

    private requestReconvert(): void {
        const cm = this.cm;
        const last = this.chunks[this.chunks.length - 1];
        if (!cm || !cm.dom.isConnected) {
            new Notice("VoxCraft: 録音中に「変換戻し」を使ってください。");
            return;
        }
        const anchor = getAnchor(cm);
        if (anchor === null || last === undefined || !last.trim()) {
            new Notice("VoxCraft: 変換戻しの対象がありません。");
            return;
        }
        if (!this.socket?.connected) {
            new Notice("VoxCraft: サーバー未接続のため変換戻しできません。");
            return;
        }

        const from = Math.max(0, anchor - last.length);
        const targetText = cm.state.doc.sliceString(from, anchor);
        this.pendingReconvert = { from, to: anchor };
        this.socket.sendReconvert(targetText);
        this.setStatus("変換候補を取得中…");
    }

    private handleReconvert(msg: ServerMessage): void {
        this.setStatus(this.recording ? "● 録音中" : "停止中");
        const segments = msg.segments || [];
        const target = this.pendingReconvert;
        this.pendingReconvert = null;
        if (segments.length === 0 || !target) {
            new Notice("VoxCraft: 変換候補が得られませんでした。");
            return;
        }
        const modal = new ReconvertModal(this.app, segments, (chosen) => {
            this.applyReconvert(target, chosen.join(""));
        });
        this.adoptModal(modal);
    }

    private applyReconvert(target: { from: number; to: number }, newText: string): void {
        const cm = this.cm ?? this.getActiveCm();
        if (!cm) return;
        const to = Math.min(target.to, cm.state.doc.length);
        const from = Math.min(target.from, to);
        cm.dispatch({ changes: { from, to, insert: newText } });
        // 直前チャンクの記録も置換後の文字列に合わせておく（取り消し整合のため）。
        if (this.chunks.length > 0) this.chunks[this.chunks.length - 1] = newText;
    }

    // ---- 接続先の切り替え ----

    // 接続先メニューを表示する。evt があればその位置、無ければリボン脇に出す。
    private openEndpointMenu(evt?: MouseEvent): void {
        const menu = new Menu();
        const sel = this.settings.selection;

        menu.addItem((i) =>
            i
                .setTitle("自動（つながる方）")
                .setChecked(sel === AUTO)
                .onClick(() => void this.setSelection(AUTO))
        );
        menu.addSeparator();
        for (const ep of this.settings.endpoints) {
            const url = ep.url.trim();
            if (!url) continue;
            menu.addItem((i) =>
                i
                    .setTitle(ep.label || url)
                    .setChecked(sel === url)
                    .onClick(() => void this.setSelection(url))
            );
        }
        menu.addSeparator();
        menu.addItem((i) =>
            i
                .setTitle("設定を開く…")
                .setIcon("gear")
                .onClick(() => {
                    const setting = (this.app as unknown as {
                        setting?: { open?: () => void; openTabById?: (id: string) => void };
                    }).setting;
                    setting?.open?.();
                    setting?.openTabById?.("voxcraft");
                })
        );

        if (evt) {
            menu.showAtMouseEvent(evt);
        } else if (this.ribbonEl) {
            const r = this.ribbonEl.getBoundingClientRect();
            menu.showAtPosition({ x: r.right, y: r.top });
        } else {
            menu.showAtPosition({ x: 0, y: 0 });
        }
    }

    private async setSelection(sel: string): Promise<void> {
        this.settings.selection = sel;
        await this.saveSettings();
        const label = sel === AUTO ? "自動（つながる方）" : labelForUrl(this.settings, sel);
        new Notice(`VoxCraft: 接続先を「${label}」にしました。`);
    }

    // 現在つながっている接続先（未接続なら設定上の第一候補）。辞書APIの宛先に使う。
    activeUrl(): string | null {
        return this.socket?.activeUrl ?? resolveUrls(this.settings)[0] ?? null;
    }

    private openRecordingsModal(): void {
        const url = this.activeUrl();
        if (!url) {
            new Notice("VoxCraft: 接続先が設定されていません");
            return;
        }
        new RecordingsModal(this.app, url).open();
    }

    private openDictModal(): void {
        const url = this.activeUrl();
        if (!url) {
            new Notice("VoxCraft: 接続先が設定されていません");
            return;
        }
        new DictModal(this.app, url).open();
    }

    // ---- ユーティリティ ----

    // アクティブな Markdown エディタの CodeMirror6 ビューを得る。
    private getActiveCm(): EditorView | null {
        const view = this.app.workspace.getActiveViewOfType(MarkdownView);
        const editor = view?.editor as (Editor & { cm?: EditorView }) | undefined;
        return editor?.cm ?? null;
    }

    // 入力レベルをステータスバーにメーター表示（音声を拾えているかの確認用）。
    private showLevel(level: number): void {
        if (!this.recording || this.transcribing) return;
        const now = performance.now();
        if (now - this.lastMeterAt < 100) return; // 約10fpsに間引く
        this.lastMeterAt = now;
        const segs = 10;
        const filled = Math.max(0, Math.min(segs, Math.round(level * 40)));
        const bar = "█".repeat(filled) + "░".repeat(segs - filled);
        // 言い直し待ちの間は、メーター表示でその状態が消えないようにする。
        this.setStatus(this.pendingRespeak ? `言い直し待ち ${bar}` : `● 録音中 ${bar}`);
    }

    private setStatus(text: string): void {
        this.statusEl.setText(`🎙 VoxCraft: ${text}`);
    }

    async loadSettings(): Promise<void> {
        this.settings = migrateSettings(
            Object.assign({}, DEFAULT_SETTINGS, await this.loadData())
        );
    }

    async saveSettings(): Promise<void> {
        await this.saveData(this.settings);
    }
}
