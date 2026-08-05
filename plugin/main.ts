import { Editor, MarkdownView, Menu, Notice, Platform, Plugin } from "obsidian";
import { findClusterBreak, Text } from "@codemirror/state";
import { EditorView } from "@codemirror/view";

import {
    AudioInputDevice,
    DICTATION_MIC,
    MicRecorder,
    RAW_MIC,
    resolveAudioInput,
} from "./audio";
import {
    looksLikeRespeak,
    matchByReading,
    needsConversion,
    parseCommand,
    parseModalCommand,
    parseProbeCommand,
    ReadingMatch,
} from "./commands";
import {
    addDictionaryEntry,
    addDictionarySymbol,
    DictModal,
    DictionarySetModal,
    fetchDictionaryCatalog,
    fetchReconvert,
    QuickAddDictionaryModal,
    ReconvertPayload,
} from "./dict";
import { ReconvertModal } from "./suggest";
import { symbolChoicesFor } from "./symbols";
import {
    AUTO,
    DEFAULT_SETTINGS,
    labelForUrl,
    loadSystemInput,
    migrateSettings,
    resolveUrls,
    saveSystemInput,
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
import { assessRefinementSafety, preserveParagraphBreaks } from "./refinement";
import { AsrMode, AsrSocket, AsrSource, isSystemSource, ServerMessage, WsHandlers } from "./ws";
import { anchorExtension, setAnchor, clearAnchor, getAnchor } from "./anchor";
import { wordRangeAt } from "./select";
import { keyboardExtension, isKeyboardSuppressed, setKeyboardSuppressed } from "./keyboard";
import { DictationToolbar } from "./toolbar";
import { ScreenWakeLock } from "./wakelock";

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

function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
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

interface ReconvertTraversal {
    targetKey: string;
    cm: EditorView;
    before: number;
    processed: number;
    doc: Text;
}

interface ReconvertModalContext {
    locationLabel?: string;
    onApplied?: (range: { from: number; to: number }) => void;
    onSkip?: () => void;
}

export default class VoxCraftPlugin extends Plugin {
    settings: VoxCraftSettings;

    private socket: AsrSocket | null = null;
    private recorder: MicRecorder | null = null;
    // サーバーから「開始から一度も音が来ていない」と言われた状態。ステータスに
    // 出し続けるための旗で、実際にチャンクが届いたら下ろす。
    private noAudioWarned = false;
    private recording = false;
    private starting = false;
    private stopping = false;
    // 文字起こし中にWebSocketが予期せず切れたときの自動再接続（モード分離: 口述には無関係）。
    private reconnecting = false;
    private statusEl: HTMLElement;
    private ribbonEl: HTMLElement;

    // 口述対象として録音開始時に固定するエディタ（背面作業やノート切替の影響を受けない）。
    private cm: EditorView | null = null;
    // アンカーに追記したチャンク列（取り消し・変換戻しの対象特定に使う。アンカー相対）。
    private chunks: string[] = [];
    // 変換戻しの応答が返るまで対象範囲を覚えておく。
    private pendingReconvert: { from: number; to: number; text: string } | null = null;
    private reconvertModal: ReconvertModal | null = null;
    // 同じ「Aを再変換」を繰り返したとき、直前に処理した箇所より前を探す。
    private reconvertTraversal: ReconvertTraversal | null = null;
    // 「ここを言い直し」で覚えた選択範囲。次の発話1回だけがこの範囲を置換する。
    // 直接代入せず setPendingRespeak() を通すこと（サーバーの認識設定と対で動く）。
    private pendingRespeak: { from: number; to: number; text: string } | null = null;
    // コマンド先読み（probe）で処理済みのチャンク番号。同じ番号の chunk は捨てる。
    // 先読みが外れた場合はここに入らないので、本文は従来どおり流れる。
    private consumedSeqs: number[] = [];
    // コマンド実行等で発話の流れが切れた直後は、次のチャンクに息継ぎ読点を打たない。
    private suppressJoiner = true;
    // 入力キャンセルで削除した確定チャンク。「入力復元」で再挿入する（口述のみ）。
    private canceled: string[] = [];
    // 画面下部の操作ツールバー（口述モードの録音中に表示）。
    private toolbar: DictationToolbar | null = null;
    // ツールバーのマイクボタンで停止したときはバーを残す（そこから再開できるように）。
    private keepToolbarOnStop = false;
    // 文字起こし中に画面を消させないための wake lock。
    private wakeLock = new ScreenWakeLock();

    // 入力レベルメーター表示のスロットリング用。
    private lastMeterAt = 0;
    private transcribing = false;

    // 接続先メニューを開いた時刻。直後に飛んでくるクリックで録音を始めないための目印。
    private menuOpenedAt = 0;

    // ---- 文字起こしモード（自分の声での口述には一切関与しない） ----
    private mode: AsrMode = "dictation";
    private source: AsrSource = "microphone";
    private sourceDevice = "";
    private autoStopSec = 0;
    private activeDictionarySetId = "";
    private activeDictionarySetName = "";
    private activeDictionaryRevision = "";
    private activeDictionaryWritableProfile = "";
    private activeDictionaryProfileRevisions: Record<string, string> = {};
    // サーバーが音声を保存しているセッションID。復旧の材料。
    private session: string | null = null;
    // 挿入したテキストと、その元になった音声の位置（秒）。再認識の宛先を引くのに使う。
    private spans: AudioSpan[] = [];
    // 直前チャンクの音声終端。次チャンクとの間が空いていれば、そこは捨てられた音声。
    private lastAudioEnd = 0;
    // PC音声の遅延補正。古い応答と、手編集を検出した際の通知連打を防ぐ。
    private lastRefinementRevision = 0;
    private refinementEditWarningShown = false;
    private refinementCoverageWarningShown = false;

    async onload(): Promise<void> {
        await this.loadSettings();

        // 口述アンカー（CM6 拡張）を全エディタに登録。
        this.registerEditorExtension(anchorExtension);
        // ソフトキーボード抑制（同じく CM6 拡張。既定は無効で、口述中だけ有効化する）。
        this.registerEditorExtension(keyboardExtension);

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

        // icon は主にモバイルのツールバー用。未指定だと「?」で並ぶ。
        this.addCommand({
            id: "toggle-recording",
            name: "音声入力の開始/停止",
            icon: "mic",
            callback: () => this.toggleRecording(),
        });
        this.addCommand({
            id: "stop-recording",
            name: "音声入力を停止",
            icon: "mic-off",
            callback: () => this.stopRecording(),
        });
        this.addCommand({
            id: "toggle-transcribe",
            name: "文字起こし（動画・会議）の開始/停止",
            icon: "file-audio",
            callback: () => {
                if (this.recording) this.stopRecording();
                else void this.startRecording("transcribe");
            },
        });
        this.addCommand({
            id: "toggle-system-transcribe",
            // コマンド名にどちらのPCの音かを必ず入れる。「PC音声の文字起こし」だけだと
            // 別PCから叩いたときにサーバー機の音が録れることに気づけない。
            // id は変えない（ユーザーが割り当てたホットキーが外れるため）。
            name: "PC音声の文字起こし【サーバー機】を開始/停止",
            icon: "monitor-speaker",
            checkCallback: (checking) => {
                if (Platform.isMobile) return false;
                if (!checking) {
                    if (this.recording) this.stopRecording();
                    else void this.startRecording("transcribe", "system");
                }
                return true;
            },
        });
        this.addCommand({
            id: "toggle-client-system-transcribe",
            name: "PC音声の文字起こし【この端末】を開始/停止",
            icon: "speaker",
            checkCallback: (checking) => {
                if (Platform.isMobile) return false;
                if (!checking) {
                    if (this.recording) this.stopRecording();
                    else void this.startRecording("transcribe", "system-client");
                }
                return true;
            },
        });
        this.addCommand({
            id: "cancel-last-input",
            name: "直前の入力をキャンセル（一文削除）",
            icon: "eraser",
            callback: () => this.cancelLast(),
        });
        this.addCommand({
            id: "restore-canceled-input",
            name: "キャンセルした入力を復元",
            icon: "rotate-ccw",
            callback: () => this.restoreCanceled(),
        });
        this.addCommand({
            id: "recover-selection",
            name: "選択範囲を音声から再認識（復旧）",
            icon: "history",
            callback: () => void this.recoverSelection(),
        });
        this.addCommand({
            id: "manage-recordings",
            name: "文字起こしの録音を整理",
            icon: "folder-open",
            callback: () => this.openRecordingsModal(),
        });
        this.addCommand({
            id: "reconvert-last",
            name: "直前の入力を変換戻し",
            icon: "refresh-cw",
            callback: () => this.requestReconvert(),
        });
        this.addCommand({
            id: "reconvert-selection",
            name: "選択範囲を再変換",
            icon: "type",
            callback: () => void this.reconvertSelection(),
        });
        // 音声の「ここを言い直し」は認識が化けると起動できない。手で確実に起動できる
        // 経路を用意する（ホットキーを割り当てられる）。選択はどうせ手で行う操作。
        this.addCommand({
            id: "respeak-selection",
            name: "選択範囲を言い直す（次の発話で置き換え）",
            icon: "repeat",
            callback: () => this.startRespeak(),
        });
        this.addCommand({
            id: "select-endpoint",
            name: "接続先を選択",
            icon: "server",
            callback: () => this.openEndpointMenu(),
        });
        this.addCommand({
            id: "select-dictionary-set",
            name: "辞書セットを選択",
            icon: "library",
            callback: () => void this.openDictionarySetModal(),
        });
        this.addCommand({
            id: "add-selection-to-dictionary",
            name: "選択範囲を辞書に追加",
            icon: "book-plus",
            callback: () => void this.openQuickAddModal(),
        });
        this.addCommand({
            id: "edit-userdict",
            name: "ユーザー辞書を編集",
            icon: "book-open",
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

    private async startRecording(
        mode: AsrMode = "dictation",
        source: AsrSource = "microphone"
    ): Promise<void> {
        if (this.recording || this.starting || this.stopping) return;
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

        // この端末のPC音声は、接続する前にデバイスを確定させる。ここで落としておけば
        // サーバー側に空の録音セッションを作らせずに済む。
        // デバイス列挙は待たされる（初回は許可ダイアログも出る）ので、その間に
        // もう一度コマンドが飛んでも二重に開始しないよう、先に旗を立てておく。
        this.starting = true;
        let clientInput: AudioInputDevice | null = null;
        if (source === "system-client") {
            clientInput = await this.resolveClientInput();
            if (!clientInput) {
                this.starting = false;
                return;
            }
        }

        this.mode = mode;
        this.source = source;
        this.sourceDevice = "";
        this.noAudioWarned = false;
        this.autoStopSec = 0;
        if (mode === "transcribe") {
            // onSession は started より先に届くため、開始要求より前に初期化する。
            this.spans = [];
            this.lastAudioEnd = 0;
            this.session = null;
            this.lastRefinementRevision = 0;
            this.refinementEditWarningShown = false;
            this.refinementCoverageWarningShown = false;
        }
        this.stopping = false;
        this.setStatus(urls.length > 1 ? "接続中…（つながる方を選択）" : "接続中…");
        this.socket = new AsrSocket(urls, this.buildSocketHandlers());

        try {
            await this.socket.connect();
        } catch (e) {
            const tried = urls.map((u) => labelForUrl(this.settings, u)).join(" / ");
            new Notice(
                `VoxCraft: サーバーに接続できません（${tried}）。` +
                "認識サーバーが起動しているか、接続先の設定を確認してください。"
            );
            this.socket = null;
            this.starting = false;
            this.setStatus("停止中");
            return;
        }

        try {
            const connectedUrl = this.socket.activeUrl;
            const dictionarySetId = connectedUrl
                ? this.dictionarySetIdFor(connectedUrl)
                : "default";
            // device は source で意味が変わる。system はサーバー機で開く入力先の
            // 指定（空ならサーバーの既定の出力）、system-client は表示名。
            const device = source === "system"
                ? (connectedUrl ? this.systemDeviceFor(connectedUrl) : "") || undefined
                : clientInput?.label;
            const started = await this.socket.sendStart(
                this.settings.stripJaAlnumSpace,
                this.settings.symbolDictation,
                mode,
                source,
                device,
                dictionarySetId
            );
            this.sourceDevice = started.device ?? "";
            this.autoStopSec = started.autoStopSec ?? 0;
            this.activeDictionarySetId = started.dictionarySetId;
            this.activeDictionarySetName = started.dictionarySetName;
            this.activeDictionaryRevision = started.dictionaryRevision;
            this.activeDictionaryWritableProfile = started.dictionaryWritableProfile;
            this.activeDictionaryProfileRevisions = started.dictionaryProfileRevisions;
            if (started.dictionaryWarningCount > 0) {
                new Notice(`VoxCraft: 辞書「${started.dictionarySetName}」に警告が${started.dictionaryWarningCount}件あります。`, 8000);
            }
        } catch (e) {
            new Notice(
                `VoxCraft: ${isSystemSource(source) ? "PC音声" : "マイク"}を開始できませんでした。` +
                `（${e instanceof Error ? e.message : e}）`
            );
            await this.teardownSession();
            this.setStatus("停止中");
            return;
        }

        // source === "system" のときだけ音声はサーバー自身が取る。それ以外
        // （マイク／この端末のPC音声）はここから PCM を送る。
        if (source !== "system") {
            this.recorder = new MicRecorder(
                (pcm) => this.socket?.sendAudio(pcm),
                (level) => this.showLevel(level),
                // 文字起こしは自分の声ではなく会場や再生音を拾う。ブラウザの前処理は
                // 近接した1人の声を前提にしているので、ここでは無効化して原音を送る。
                // ループバック入力に至っては、EC/NS/AGC は掛けるだけ音を壊す。
                mode === "transcribe" ? RAW_MIC : DICTATION_MIC,
                16000,
                clientInput?.deviceId ?? null
            );
            // 「録音中」の表示のまま実は音が来ていない、という壊れ方を必ず外へ出す。
            this.recorder.onStalled = () => this.reportStall();
            this.recorder.onGap = (sec) => this.reportGap(sec);
            try {
                await this.recorder.start();
            } catch (e) {
                new Notice(
                    clientInput
                        ? `VoxCraft: 入力デバイス「${clientInput.label}」を開けませんでした。` +
                          "他のアプリが占有しているか、デバイスが無効化された可能性がある。"
                        : "VoxCraft: マイクにアクセスできませんでした。"
                );
                this.socket.sendStop();
                await this.teardownSession();
                this.setStatus("停止中");
                return;
            }
        }

        // 口述対象を固定し、現在のカーソル位置にアンカーを立てる。
        this.cm = cm;
        this.chunks = [];
        this.reconvertTraversal = null;
        this.canceled = [];
        this.pendingReconvert = null;
        this.setPendingRespeak(null);
        this.suppressJoiner = true; // 最初のチャンクには息継ぎ読点を打たない
        setAnchor(cm, cm.state.selection.main.head);

        if (mode === "transcribe") {
            // 画面が消えると AudioContext ごと止まる（Android）。長時間まわす
            // 文字起こしだけ、その間の自動消灯を抑える。
            if (Platform.isMobile && this.settings.keepScreenOn) void this.acquireWakeLock();
        }

        this.recording = true;
        this.starting = false;
        this.ribbonEl.addClass("voxcraft-recording");
        this.setStatus(this.idleStatus());
        // ツールバーは口述専用。文字起こしでは本文操作系が誤動作しないよう出さない。
        if (mode === "dictation") this.showToolbar();
        else this.hideToolbar();
        // ここではフォーカスを付け直して、既に開いているキーボードを閉じさせる。
        this.refreshKeyboardSuppression(true);
    }

    stopRecording(): void {
        if (!this.recording) return;
        this.recording = false;
        this.stopping = true;
        void this.wakeLock.release();
        this.ribbonEl.removeClass("voxcraft-recording");
        // ツールバーのマイクで止めたときはバーを残し、そこから再開できるようにする。
        this.toolbar?.setRecording(false);
        if (this.keepToolbarOnStop) this.keepToolbarOnStop = false;
        else this.hideToolbar();
        this.refreshKeyboardSuppression();
        this.setStatus("停止処理中…");
        // マイクは最後のフレームを送り終えてから stop を送る。PC音声は stop を受けた
        // サーバー自身が入力を閉じる。どちらも stopped 応答までは接続を維持する。
        void this.requestStop();
    }

    private async requestStop(): Promise<void> {
        await this.recorder?.stop();
        this.recorder = null;
        if (this.socket?.connected) this.socket.sendStop();
        else this.finishStop();
    }

    // 設定に保存された入力デバイスを、いま実在するものへ解決する。
    // 見つからないまま既定マイクで始めてしまうと、PC音声のつもりで部屋の音を
    // 録ることになるので、解決できなければ開始しない。
    private async resolveClientInput(): Promise<AudioInputDevice | null> {
        const saved = loadSystemInput(this.app);
        if (!saved) {
            new Notice(
                "VoxCraft: この端末のPC音声の入力デバイスが未設定です。" +
                "設定 → VoxCraft →「PC音声（この端末）」で「ステレオ ミキサー」等を選んでください。" +
                "サーバー機の音を録りたい場合は【サーバー機】の方のコマンドを使ってください。"
            );
            return null;
        }
        let resolved: AudioInputDevice | null = null;
        try {
            resolved = await resolveAudioInput(saved);
        } catch {
            resolved = null;
        }
        if (!resolved) {
            new Notice(
                `VoxCraft: 入力デバイス「${saved.label || saved.deviceId}」が見つかりません。` +
                "設定で選び直してください。"
            );
            return null;
        }
        // ドライバ再インストール等で ID が変わったら、拾い直せた方へ更新しておく。
        if (resolved.deviceId !== saved.deviceId || resolved.label !== saved.label) {
            saveSystemInput(this.app, resolved);
        }
        return resolved;
    }

    private finishStop(): void {
        if (!this.stopping) return;
        this.stopping = false;
        this.transcribing = false;
        // サーバー主導の停止（無音での自動停止）でもここを通る。この端末で録っている
        // 場合は recorder が残ったままになるので、必ず閉じる。手動停止では
        // requestStop() で停止済みなので二重呼び出しにはならない。
        void this.recorder?.stop();
        this.recorder = null;
        this.socket?.close();
        this.socket = null;
        this.clearDictationAnchor();
        this.setPendingRespeak(null);
        this.setStatus("停止中");
    }

    private handleServerStopped(reason?: string): void {
        // PC音声の無音監視はサーバー側で止まるため、クライアントが停止操作中でなくても
        // UIと接続を終了状態へそろえる。手動停止は従来どおり finishStop() へ流す。
        if (reason === "silence" && this.recording) {
            this.recording = false;
            this.stopping = true;
            this.transcribing = false;
            void this.wakeLock.release();
            this.ribbonEl.removeClass("voxcraft-recording");
            this.toolbar?.setRecording(false);
            this.hideToolbar();
            this.refreshKeyboardSuppression();
            const minutes = Math.max(1, Math.round(this.autoStopSec / 60));
            new Notice(
                `VoxCraft: PC音声が${minutes}分間無音だったため、文字起こしを自動停止しました。`
            );
        }
        this.finishStop();
    }

    // AsrSocket のハンドラは初回接続・再接続のどちらでも同じものを使う
    // （違うのは接続先と start/resume のどちらを送るかだけ）。
    private buildSocketHandlers(): WsHandlers {
        return {
            onReady: () => {
                const url = this.socket?.activeUrl;
                const label = url ? labelForUrl(this.settings, url) : "";
                const preparing = this.source === "system" ? "PC音声を準備中…" : "待機中… 話してください";
                this.setStatus(label ? `${preparing}（${label}）` : preparing);
            },
            onPartial: () => {
                this.transcribing = true;
                this.setStatus("認識中…");
            },
            onProbe: (text, msg) => this.handleProbe(text, msg),
            onSession: (id) => {
                this.session = id;
            },
            onChunk: (text, msg) => {
                this.transcribing = false;
                // 実際に認識できた＝音は来ている。警告表示を下ろす。
                if (this.noAudioWarned) {
                    this.noAudioWarned = false;
                    this.setStatus(this.idleStatus());
                }
                this.handleChunk(text, msg);
            },
            onRefinement: (text, msg) => this.handleTranscribeRefinement(text, msg),
            onLevel: (level) => this.showLevel(level),
            onStopped: (reason) => this.handleServerStopped(reason),
            onReconvert: (msg) => this.handleReconvert(msg),
            onError: (m, fatal) => {
                // 開始前の fatal は sendStart()/sendResume() の catch で一度だけ表示する。
                if (!this.recording) return;
                new Notice(`VoxCraft サーバーエラー: ${m}`);
                if (fatal && isSystemSource(this.source)) this.stopRecording();
            },
            onWarning: (m, code) => {
                if (!this.recording || !m) return;
                // 取得先の取り違えは、自動停止まで待つと何も残らない。長めに出し、
                // ステータスにも残して、通知を見落としても気づけるようにする。
                new Notice(`VoxCraft: ${m}`, 15000);
                if (code === "no_audio") {
                    this.noAudioWarned = true;
                    this.setStatus(this.idleStatus());
                }
            },
            onClose: () => this.handleSocketClose(),
        };
    }

    // 接続が切れたときの分岐点。文字起こし中（セッションを持っている＝復旧可能）だけ
    // 自動再接続を試み、それ以外（口述・停止処理中・セッション開始前）は従来どおり
    // 即座に録音を終了する。口述の挙動には一切触れない。
    private handleSocketClose(): void {
        if (!this.recording && !this.stopping) return;
        if (!this.stopping && this.recording && this.mode === "transcribe" && this.session) {
            void this.reconnectTranscribe();
            return;
        }
        new Notice("VoxCraft: サーバー接続が切れました。");
        void this.teardownSession();
        this.setStatus("停止中");
    }

    // モバイル回線の瞬断等でWebSocketが切れた直後、マイクは止めずに再接続だけ試みる。
    // 再接続できたら同じセッションID宛に resume を送り、サーバー側の同じ録音ファイルへ
    // 続きを積んでもらう（復旧コマンドが引き続き使えるように session を変えない）。
    private async reconnectTranscribe(): Promise<void> {
        if (this.reconnecting) return;
        this.reconnecting = true;

        this.socket = null;
        const session = this.session;
        const source = this.source;
        const stripSpace = this.settings.stripJaAlnumSpace;
        const symbols = this.settings.symbolDictation;
        const device = this.sourceDevice || undefined;
        const disconnectedAt = Date.now();
        const delaysMs = [1000, 2000, 4000, 8000, 15000];
        const maxTotalMs = 3 * 60 * 1000; // 3分間は再接続を試み続ける
        this.setStatus("⚠ 接続が切れました。再接続中…");

        let attempt = 0;
        while (session && this.recording && !this.stopping && Date.now() - disconnectedAt < maxTotalMs) {
            await sleep(delaysMs[Math.min(attempt, delaysMs.length - 1)]);
            attempt += 1;
            if (!this.recording || this.stopping) break;

            const urls = resolveUrls(this.settings);
            const socket = new AsrSocket(urls, this.buildSocketHandlers());
            try {
                await socket.connect();
                const dictionarySetId = this.activeDictionarySetId || "default";
                const started = await socket.sendResume(
                    session, stripSpace, symbols, source, device, dictionarySetId
                );
                if (!this.recording || this.stopping) {
                    // 再接続の最中にユーザーが停止操作をしていた。この接続は使わない。
                    socket.close();
                    break;
                }
                this.socket = socket;
                this.sourceDevice = started.device ?? this.sourceDevice;
                this.autoStopSec = started.autoStopSec ?? this.autoStopSec;
                this.activeDictionarySetId = started.dictionarySetId;
                this.activeDictionarySetName = started.dictionarySetName;
                this.activeDictionaryRevision = started.dictionaryRevision;
                this.activeDictionaryWritableProfile = started.dictionaryWritableProfile;
                this.activeDictionaryProfileRevisions = started.dictionaryProfileRevisions;
                this.reconnecting = false;
                const gapSec = Math.max(1, Math.round((Date.now() - disconnectedAt) / 1000));
                new Notice(
                    `VoxCraft: サーバーに再接続しました（約${gapSec}秒の空白。` +
                    "その間の音声は録音されていません）。"
                );
                this.setStatus(this.idleStatus());
                return;
            } catch {
                socket.close();
            }
        }

        this.reconnecting = false;
        if (this.recording && !this.stopping) {
            new Notice("VoxCraft: サーバーへ再接続できませんでした。録音を停止します。");
            void this.teardownSession();
            this.setStatus("停止中");
        }
    }

    private async teardownSession(): Promise<void> {
        this.recording = false;
        this.starting = false;
        this.stopping = false;
        await this.wakeLock.release();
        this.ribbonEl?.removeClass("voxcraft-recording");
        this.hideToolbar();
        this.refreshKeyboardSuppression();
        await this.recorder?.stop();
        this.recorder = null;
        this.socket?.close();
        this.socket = null;
        this.clearDictationAnchor();
        this.setPendingRespeak(null);
        // チャンク番号はセッションごとに1から振り直される。持ち越すと次の録音で
        // 無関係なチャンクを「処理済み」として捨ててしまう。
        this.consumedSeqs = [];
    }

    private clearDictationAnchor(): void {
        if (this.cm && this.cm.dom.isConnected) clearAnchor(this.cm);
        this.cm = null;
    }

    // ---- 録音が途切れていないかの見張り ----

    // 画面を消させない。取れなかったときは黙って効かないので、必ず知らせる。
    private async acquireWakeLock(): Promise<void> {
        if (await this.wakeLock.acquire()) return;
        new Notice(
            "VoxCraft: 画面を点けたままにできませんでした。" +
            "端末の画面が消えると録音が止まります（設定 →「画面の自動消灯」を長めに）。"
        );
    }

    // 今まさに音が来ていない。録音中の表示のままにしない。
    private reportStall(): void {
        if (!this.recording) return;
        this.setStatus("⚠ 音声が止まっています");
        new Notice(
            this.source === "system-client"
                ? "VoxCraft: PC音声の入力が止まっています。" +
                  "入力デバイスが無効化されたか、他のアプリに奪われた可能性があります。"
                : "VoxCraft: マイクからの音声が止まっています。" +
                  "Obsidian を前面に戻し、画面を点けたままにしてください。"
        );
    }

    // 途切れが終わった。その間は録れていないので、長さごと知らせる。
    private reportGap(seconds: number): void {
        if (!this.recording) return;
        this.setStatus(this.idleStatus());
        const cause = this.source === "system-client"
            ? "入力デバイスの一時停止の可能性"
            : "画面オフ／バックグラウンドの可能性";
        new Notice(
            `VoxCraft: 音声が約${Math.round(seconds)}秒途切れました（${cause}）。` +
            "その間は録音・文字起こしされていません。"
        );
    }

    // ---- 確定チャンクの処理 ----

    // 小さいモデルによるコマンド先読み。本命の認識より1秒ほど早く着く。
    //
    // 認識時間は音声の長さにも beam 幅にもほとんど比例しない（Whisper が常に
    // 30秒ぶんのメル窓をエンコードするため）ので、コマンドを速くする唯一の道が
    // 「小さいモデルで先に一度読む」になる。実測 base で125ms、本命の kotoba は
    // 1.15秒。ここで拾えたぶんだけ、キャンセルや言い直しが1秒早く効く。
    //
    // 速報は本文には絶対に使わない。確信のある固定句だけを実行し、処理した
    // チャンク番号を覚えて、あとから届く本命チャンクを捨てる。
    // 外れたときは何もしない ＝ 本文は従来どおり本命の認識結果が入る。
    private handleProbe(text: string, msg: ServerMessage): void {
        if (this.mode !== "dictation" || !this.recording) return;
        if (typeof msg.seq !== "number" || !text.trim()) return;
        // 言い直し待ちの発話は「置き換える本文」そのもの。速報の緩い判定で
        // コマンドに横取りさせない（解除したいときは本命の認識結果で効く）。
        if (this.pendingRespeak) return;
        const reading = msg.reading || "";

        // 候補モーダル中は発話が本文に入らないので、緩い判定をそのまま使える。
        const cmd = this.reconvertModal
            ? parseModalCommand(text)
            : this.settings.enableCommands
                ? parseProbeCommand(
                    text,
                    this.settings.commandFuzzy ? reading : "",
                    this.settings.commandPrefix
                )
                : null;
        if (!cmd) return;
        // runCommand が false を返すのは「その状況では成立しない」場合（選択が無い
        // 「言い直し」など）。本命チャンクに委ねるため、消費済みにはしない。
        if (!this.runCommand(cmd)) return;

        this.suppressJoiner = true;
        this.consumedSeqs.push(msg.seq);
        if (this.consumedSeqs.length > 20) this.consumedSeqs.shift();
    }

    private handleChunk(text: string, msg?: ServerMessage): void {
        if (this.mode === "transcribe") {
            this.handleTranscribeChunk(text, msg);
            return;
        }
        // 先読みでコマンドとして処理済みのチャンク。本文には入れない。
        if (typeof msg?.seq === "number") {
            const at = this.consumedSeqs.indexOf(msg.seq);
            if (at >= 0) {
                this.consumedSeqs.splice(at, 1);
                this.suppressJoiner = true;
                return;
            }
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
        // 表記が外れただけの命令を読みで拾えたら、そのまま実行する。拾いきれない
        // 「惜しい外れ」は本文に入れたうえで、あとで実行を提案する（勝手に消さない）。
        let nearMiss: ReadingMatch | null = null;
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
            if (!cmd && this.settings.commandFuzzy) {
                const near = matchByReading(
                    text,
                    msg?.reading || "",
                    this.settings.commandPrefix
                );
                if (near?.confident && this.runCommand(near.cmd)) {
                    this.suppressJoiner = true;
                    new Notice(`VoxCraft: 「${text}」を「${near.phrase}」として実行しました。`);
                    return;
                }
                if (near) nearMiss = near;
            }
        }
        // 「ここを言い直し」の直後の発話は、アンカーではなく覚えた範囲を置換する。
        if (this.pendingRespeak) {
            this.applyRespeak(text);
            this.suppressJoiner = true;
            return;
        }
        this.insertText(this.withPauseComma(text, msg));
        if (nearMiss) this.offerNearMiss(text, nearMiss);
    }

    // 命令のつもりが本文として入ってしまった発話に、1タップの逃げ道を出す。
    //
    // 誤認識でコマンドが外れたときの本当の負担は「入った文字を手で消す」ことなので、
    // 挿入は従来どおり行ったうえで、押せば取り消して実行する通知を添える。
    // 押さなければただの通知として消える ＝ 本文は絶対に失われない。
    private offerNearMiss(text: string, near: ReadingMatch): void {
        const notice = new Notice("", 8000);
        notice.messageEl.setText(`VoxCraft: 「${text}」`);
        notice.messageEl.createEl("br");
        const button = notice.messageEl.createEl("button", {
            text: `「${near.phrase}」として実行`,
            cls: "voxcraft-notice-action",
        });
        button.addEventListener("click", () => {
            notice.hide();
            // 入れてしまった命令文を先に取り除いてからコマンドを実行する。順序が逆だと
            // 「入力キャンセル」が命令文ではなく1つ前の発話を消してしまう。
            // 命令文は「入力復元」の対象にしない（復元したいのは本文だけ）。
            const dropped = this.dropLastChunk();
            if ("error" in dropped) {
                new Notice(`VoxCraft: ${dropped.error}`);
                return;
            }
            this.suppressJoiner = true;
            if (!this.runCommand(near.cmd)) {
                new Notice(`VoxCraft: 「${near.phrase}」は今の状態では実行できません。`);
            }
        });
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

    // PC音声の速報範囲を、同じ音声を30秒前後まとめて認識した結果へ差し替える。
    // 追記後にユーザーが本文を編集していた場合は、変更を上書きせず補正を見送る。
    private handleTranscribeRefinement(text: string, msg: ServerMessage): void {
        if (this.mode !== "transcribe" || !isSystemSource(this.source) || !text) return;
        const start = msg.start;
        const end = msg.end;
        const revision = msg.revision;
        if (
            typeof start !== "number" ||
            typeof end !== "number" ||
            typeof revision !== "number" ||
            end <= start ||
            revision <= this.lastRefinementRevision
        ) {
            return;
        }
        if (this.session && msg.session && msg.session !== this.session) return;
        this.lastRefinementRevision = revision;

        const cm = this.cm;
        if (!cm || !cm.dom.isConnected || this.spans.length === 0) return;
        const EPS = 0.02;
        let first = -1;
        let last = -1;
        for (let i = 0; i < this.spans.length; i += 1) {
            const span = this.spans[i];
            if (span.end > start + EPS && span.start < end - EPS) {
                if (first < 0) first = i;
                last = i;
            }
        }
        if (first < 0 || last < first) return;
        // 補正境界は速報チャンクの境界であること。部分的に重なる範囲は壊さない。
        if (this.spans[first].start < start - EPS || this.spans[last].end > end + EPS) {
            return;
        }

        const anchor = getAnchor(cm);
        if (anchor === null) return;
        const trackedText = this.spans.map((span) => span.text).join("");
        const regionStart = anchor - trackedText.length;
        if (
            regionStart < 0 ||
            cm.state.doc.sliceString(regionStart, anchor) !== trackedText
        ) {
            if (!this.refinementEditWarningShown) {
                this.refinementEditWarningShown = true;
                new Notice(
                    "VoxCraft: 文字起こし本文が手動編集されているため、" +
                    "自動補正は上書きせず見送りました。"
                );
            }
            return;
        }

        let beforeLength = 0;
        for (let i = 0; i < first; i += 1) beforeLength += this.spans[i].text.length;
        let oldLength = 0;
        for (let i = first; i <= last; i += 1) oldLength += this.spans[i].text.length;
        const from = regionStart + beforeLength;
        const to = from + oldLength;
        const oldText = cm.state.doc.sliceString(from, to);
        const expected = this.spans.slice(first, last + 1).map((span) => span.text).join("");
        if (oldText !== expected) return;

        const safety = assessRefinementSafety(expected, text);
        if (!safety.safe) {
            if (!this.refinementCoverageWarningShown) {
                this.refinementCoverageWarningShown = true;
                new Notice("VoxCraft: 補正稿に大きな欠落を検出したため、速報稿を保持しました。");
            }
            console.warn("[VoxCraft] Incomplete refinement rejected", {
                reason: safety.reason,
                start,
                end,
            });
            return;
        }

        // 速報側の ParagraphBreaker が入れた空行を、補正後の最寄りの文末へ戻す。
        // これをしないと、30秒単位の補正が来るたびに段落がベタ打ちへ戻ってしまう。
        const replacement = preserveParagraphBreaks(text, expected);
        cm.dispatch({ changes: { from, to, insert: replacement } });
        this.spans.splice(first, last - first + 1, { text: replacement, start, end });
        // 文字起こし中は取消操作を使わないが、停止後の直前入力情報も実本文へそろえる。
        this.chunks = this.spans.slice(-200).map((span) => span.text);
    }

    // コマンドを実行し、処理したら true を返す。false ならチャンクは本文として扱われる。
    private runCommand(cmd: NonNullable<ReturnType<typeof parseCommand>>): boolean {
        switch (cmd.kind) {
            case "stop":
                this.stopRecording();
                return true;
            case "cancelInput":
                this.cancelLast();
                return true;
            case "restoreInput":
                this.restoreCanceled();
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
                // 「訂正」のような本文にも出る語は、選択があるときだけコマンドにする。
                // この条件があるおかげで一般語を起動語にできている。
                // 「ここを言い直し」のような命令とわかる言い方だけ、選択が無くても
                // カーソル位置の語を対象にしてよい（Androidで選択が難しいため）。
                if (!cmd.explicit && !this.hasSelection()) return false;
                this.startRespeak();
                return true;
            case "respeakTarget":
                void this.respeakByTarget(cmd.target);
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
                    this.setPendingRespeak(null);
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

        // 通常の口述やツールバー挿入が再開したら、連続再変換の遡りを終了する。
        this.reconvertTraversal = null;

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
    // 音声・ツールバー・コマンドパレットの「入力キャンセル」の共通実装。
    // 削除した文は canceled に積み、「元に戻す」で再挿入できる。文字起こしでは動かない
    // （動画側の本文を勝手に消さない）。
    // 直前チャンクを本文から取り除き、その文字列を返す（取れなければ null）。
    // 通知は出さない。呼び出し側が用途に応じたメッセージを出す。
    private dropLastChunk(): { text: string } | { error: string } {
        const cm = this.cm;
        const last = this.chunks[this.chunks.length - 1];
        if (!cm || !cm.dom.isConnected || last === undefined) {
            return { error: "キャンセルできる入力がありません（音声入力中に使ってください）。" };
        }
        const anchor = getAnchor(cm);
        if (anchor === null) return { error: "挿入位置を見失いました。" };

        const from = Math.max(0, anchor - last.length);
        if (cm.state.doc.sliceString(from, anchor) !== last) {
            return { error: "直前の入力が編集されているため取り消せません。" };
        }
        this.chunks.pop();
        cm.dispatch({ changes: { from, to: anchor, insert: "" } });
        return { text: last };
    }

    private cancelLast(): void {
        if (this.mode === "transcribe" && this.recording) {
            new Notice("VoxCraft: 入力キャンセルは文字起こしでは使えません。");
            return;
        }
        const dropped = this.dropLastChunk();
        if ("error" in dropped) {
            new Notice(`VoxCraft: ${dropped.error}`);
            return;
        }
        const last = dropped.text;
        this.canceled.push(last);
        if (this.canceled.length > 50) this.canceled.shift();
        // ボタン操作で発話の流れは切れているので、次のチャンクに息継ぎ読点を打たない。
        this.suppressJoiner = true;
        const t = last.trim();
        const preview = t.length > 20 ? t.slice(0, 20) + "…" : t;
        new Notice(`VoxCraft: キャンセルしました —「${preview}」（「入力復元」で復活）`);
    }

    // ツールバーの ⌫。選択範囲があればそれを、無ければカーソル直前の1文字を消す。
    //
    // モバイルで口述中はソフトキーボードを抑制しているため、これが無いと
    // 「入力キャンセル（一文まるごと）」以外に文字を消す手段が無くなる。
    // アンカーは anchor.ts の StateField が文書変更に追従するので触らなくてよい。
    private backspace(): void {
        const cm = this.cm && this.cm.dom.isConnected ? this.cm : this.getActiveCm();
        if (!cm) {
            new Notice("VoxCraft: ノートを編集モードで開いてください。");
            return;
        }
        const sel = cm.state.selection.main;
        let from = sel.from;
        const to = sel.to;
        if (sel.empty) {
            if (sel.head === 0) return;
            // 結合文字・サロゲートペア（絵文字など）を半分だけ消さない。
            const line = cm.state.doc.lineAt(sel.head);
            const col = sel.head - line.from;
            from = col === 0
                ? sel.head - 1  // 行頭なら改行そのものを消す
                : line.from + findClusterBreak(line.text, col, false);
        }
        if (from >= to) return;

        this.trimChunkRecord(cm, from, to);
        cm.dispatch({
            changes: { from, to, insert: "" },
            selection: { anchor: from },
            scrollIntoView: true,
        });
    }

    // 手で消した範囲を、取り消し用のチャンク記録側にも反映する。
    //
    // これをしないと chunks の末尾が実本文とズレ、次の「入力キャンセル」が
    // 「直前の入力が編集されているため取り消せません」で止まる。
    private trimChunkRecord(cm: EditorView, from: number, to: number): void {
        const last = this.chunks[this.chunks.length - 1];
        const anchor = getAnchor(cm);
        if (last === undefined || anchor === null) return;
        const chunkStart = anchor - last.length;
        // 直前チャンクの内側だけを消したなら、その分を記録からも削る。
        if (from >= chunkStart && to <= anchor) {
            const head = last.slice(0, from - chunkStart);
            const tail = last.slice(to - chunkStart);
            const next = head + tail;
            if (next) this.chunks[this.chunks.length - 1] = next;
            else this.chunks.pop();
            return;
        }
        // チャンクの外にはみ出す削除は、対応を追えない。取り消し履歴を畳んで、
        // 誤った範囲を消しにいくより「キャンセルできない」と言う側に倒す。
        this.chunks = [];
    }

    // 「入力復元」/「元に戻す」: 入力キャンセルで消した文をアンカー位置に再挿入する。
    private restoreCanceled(): void {
        const text = this.canceled[this.canceled.length - 1];
        if (text === undefined) {
            new Notice("VoxCraft: 復元できる入力がありません。");
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
        const t = text.trim();
        const preview = t.length > 20 ? t.slice(0, 20) + "…" : t;
        new Notice(`VoxCraft: 復元しました —「${preview}」`);
    }

    // ---- 画面下部の操作ツールバー（口述専用） ----

    private showToolbar(): void {
        if (!this.settings.showToolbar) return;
        if (!this.toolbar) {
            this.toolbar = new DictationToolbar(
                {
                    onMicToggle: () => {
                        if (this.recording) {
                            this.keepToolbarOnStop = true;
                            this.stopRecording();
                        } else {
                            void this.startRecording();
                        }
                    },
                    onCancel: () => this.cancelLast(),
                    onBackspace: () => this.backspace(),
                    onRestore: () => this.restoreCanceled(),
                    onInsert: (text) => this.insertFromToolbar(text),
                    // 「言い直し」は録音中のみ（次の発話が置換になる）。startRespeak が
                    // 未録音・未選択を Notice で案内する。「再変換」は REST 経由なので
                    // 録音していなくても使える。
                    onRespeak: () => this.startRespeak(),
                    onReconvert: () => void this.reconvertSelection(),
                    onKeyboardToggle: () => this.toggleKeyboard(),
                    onOpenDict: () => void this.openQuickAddModal(),
                    onClose: () => {
                        // バーを閉じるとキーボードを出す手段（⌨）も無くなるので、
                        // 抑制も一緒に解除する。
                        this.hideToolbar();
                        this.refreshKeyboardSuppression();
                    },
                },
                // キーボードの出し入れはモバイル専用の悩み。デスクトップでは出さない。
                { keyboardButton: Platform.isMobile }
            );
        }
        this.toolbar.show();
        this.toolbar.setRecording(true);
        this.toolbar.setKeyboardSuppressed(this.cm ? isKeyboardSuppressed(this.cm) : false);
    }

    private hideToolbar(): void {
        this.toolbar?.hide();
    }

    // ---- ソフトキーボードの抑制（モバイルの口述中のみ） ----

    // 設定・録音状態から「今キーボードを抑制すべきか」を決め、掛け直す。
    // refocus=true のときだけフォーカスを付け直す（＝今出ているキーボードを閉じる）。
    // 自動の解除でこれをやると、頼んでいないのにキーボードが開いてしまう。
    refreshKeyboardSuppression(refocus = false): void {
        const cm = this.cm;
        if (!cm || !cm.dom.isConnected) return;
        // ツールバーが出ていることを条件に含める。⌨ボタンが無い状態で抑制すると
        // キーボードを出す手段が無くなる。
        const want =
            Platform.isMobile &&
            this.settings.suppressKeyboard &&
            this.recording &&
            this.mode === "dictation" &&
            this.toolbar?.visible === true;
        setKeyboardSuppressed(cm, want, refocus);
        this.toolbar?.setKeyboardSuppressed(want);
    }

    // ツールバーの⌨ボタン。抑制中なら解除してキーボードを出し、出ているなら抑え直す。
    // Android はユーザー操作の文脈でない focus() ではキーボードを出さないので、
    // クリックハンドラの中で同期的に処理する（await を挟まない）。
    private toggleKeyboard(): void {
        const cm = this.cm && this.cm.dom.isConnected ? this.cm : this.getActiveCm();
        if (!cm) {
            new Notice("VoxCraft: ノートを編集モードで開いてください。");
            return;
        }
        const next = !isKeyboardSuppressed(cm);
        setKeyboardSuppressed(cm, next);
        this.toolbar?.setKeyboardSuppressed(next);
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
        const dictionary = this.activeDictionarySetName ? ` / 辞書: ${this.activeDictionarySetName}` : "";
        if (isSystemSource(this.source)) {
            const where = this.source === "system-client" ? "この端末" : "サーバー機";
            // 音が来ていないと分かっている間は、レベルメーターで上書きされても
            // 消えないようにここへ出す（通知を見落としても気づけるように）。
            const head = this.noAudioWarned ? "⚠ 音が来ていません" : "● PC音声を文字起こし中";
            return this.sourceDevice
                ? `${head}（${where}: ${this.sourceDevice}）${dictionary}`
                : `${head}（${where}）${dictionary}`;
        }
        return this.mode === "transcribe" ? `● 文字起こし中${dictionary}` : `● 録音中${dictionary}`;
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
            payload = await fetchReconvert(url, target, this.appliedDictionarySetId(url));
        } catch (e) {
            this.setStatus(this.idleStatus());
            new Notice(`VoxCraft: 変換候補を取得できません（${e instanceof Error ? e.message : e}）`);
            return;
        }
        this.setStatus(this.idleStatus());
        if (!payload.online && !this.offlineSymbolsOnly(target)) return;

        const surfaces = buildSurfaces(target, payload.segments);
        const targetKey = target.normalize("NFKC").replace(/\s+/gu, "");
        const previous = this.reconvertTraversal;
        const traversal =
            previous &&
            previous.targetKey === targetKey &&
            previous.cm === cm &&
            previous.doc === cm.state.doc
                ? previous
                : null;
        const before = traversal?.before ?? cm.state.doc.length;
        const hit = this.findLastSurface(cm, surfaces, before);
        if (!hit) {
            if (traversal?.processed) {
                new Notice(
                    `VoxCraft: 「${target}」は以前の一致を${traversal.processed}箇所確認しました。` +
                    "これより前の一致はありません。"
                );
                // 次回は直近からやり直せるよう、末尾到達時にだけ解除する。
                this.reconvertTraversal = null;
            } else {
                new Notice(
                    `VoxCraft: 「${target}」に相当する箇所が見つかりません。` +
                    "該当箇所を選択して「選択範囲を再変換」を使ってください。"
                );
            }
            return;
        }
        const occurrence = (traversal?.processed ?? 0) + 1;
        const advance = (range: { from: number; to: number }) => {
            this.reconvertTraversal = {
                targetKey,
                cm,
                before: range.from,
                processed: occurrence,
                doc: cm.state.doc,
            };
        };
        this.openReconvertModalFor(
            hit,
            cm.state.doc.sliceString(hit.from, hit.to),
            payload,
            cm,
            {
                locationLabel: `一致箇所: 直近から${occurrence}件目`,
                onApplied: advance,
                onSkip: () => advance(hit),
            }
        );
    }

    // 表記候補を「直近の口述領域 → ノート全文」の順で後方検索する。
    private findLastSurface(
        cm: EditorView,
        surfaces: string[],
        before = cm.state.doc.length
    ): { from: number; to: number } | null {
        const doc = cm.state.doc.toString();
        const searchEnd = Math.max(0, Math.min(before, doc.length));

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
        const winEnd =
            anchor === null ? null : Math.min(anchor, doc.length, searchEnd);

        // 直近 → 全文の順に、まず言い残しを避けて探す。
        if (winStart !== null && winEnd !== null) {
            const hit = searchIn(winStart, winEnd, true);
            if (hit) return hit;
        }
        const hit = searchIn(0, searchEnd, true);
        if (hit) return hit;
        // どこにも無ければ言い残しも許して探す（本文が偶然「参加を修正」のような
        // 並びになっている場合に、直せないより直せる方を選ぶ）。
        return searchIn(0, searchEnd, false);
    }

    // 選択範囲の再変換: タッチ/マウスで選んだ誤変換を候補から直す。
    // REST 経由なので録音していなくても使える（外来語・英字ミスの確実な逃げ道）。
    private async reconvertSelection(): Promise<void> {
        const cm = this.cm && this.cm.dom.isConnected ? this.cm : this.getActiveCm();
        if (!cm) {
            new Notice("VoxCraft: ノートを編集モードで開いてください。");
            return;
        }
        // 選択が無ければカーソル位置の語を対象にする（Androidで選択が難しいため）。
        // 再変換は候補を出すだけで、選ばなければ本文は変わらない。多少広く取っても
        // reconvert 側が文節に切り直すので、実害が無い。
        const range = this.targetRange(cm);
        if (!range) {
            new Notice(
                "VoxCraft: 再変換する場所が決まりません。" +
                "直したい語の中にカーソルを置くか、範囲を選択してください。"
            );
            return;
        }
        if (range.to - range.from > 200) {
            new Notice("VoxCraft: 選択が長すぎます（200文字まで）。");
            return;
        }
        this.reconvertTraversal = null;
        const url = this.activeUrl();
        if (!url) {
            new Notice("VoxCraft: 接続先が設定されていません。");
            return;
        }

        const text = cm.state.doc.sliceString(range.from, range.to);
        this.setStatus("変換候補を取得中…");
        let payload: ReconvertPayload;
        try {
            payload = await fetchReconvert(url, text, this.appliedDictionarySetId(url));
        } catch (e) {
            this.setStatus(this.idleStatus());
            new Notice(`VoxCraft: 変換候補を取得できません（${e instanceof Error ? e.message : e}）`);
            return;
        }
        this.setStatus(this.idleStatus());
        if (!payload.online && !this.offlineSymbolsOnly(text)) return;
        this.openReconvertModalFor(range, text, payload, cm);
    }

    // オフライン（Google CGI が使えない）ときに、それでもモーダルを開くか。
    //
    // 変換候補は諦めるしかないが、記号語の取り違え（「まる」→『悪』）はローカルの
    // 記号セットだけで直せる。オフラインでもサーバー本体はLANに居るので、選んだ
    // 記号の辞書登録もそのまま通る。記号を出せない対象のときだけ従来どおり打ち切る。
    private offlineSymbolsOnly(text: string): boolean {
        if (symbolChoicesFor(text, 1).length === 0) {
            new Notice("VoxCraft: オフラインのため変換候補を取得できませんでした。");
            return false;
        }
        new Notice("VoxCraft: オフラインのため変換候補はありません。記号だけ選べます。");
        return true;
    }

    // 新規経路共通: 候補モーダルを開き、確定時に検証付きで置換する。
    private openReconvertModalFor(
        range: { from: number; to: number },
        originalText: string,
        payload: ReconvertPayload,
        cm: EditorView,
        context: ReconvertModalContext = {}
    ): void {
        const segments = payload.segments || [];
        if (segments.length === 0) {
            new Notice("VoxCraft: 変換候補が得られませんでした。");
            return;
        }
        const modal = new ReconvertModal(
            this.app,
            segments,
            (chosen) => {
                const applied = this.applyRangedReplace(
                    range,
                    originalText,
                    chosen.join(""),
                    cm
                );
                if (applied) context.onApplied?.(applied);
            },
            {
                originalText,
                onRegister: (f, t) => void this.registerReplacement(f, t),
                onRegisterSymbol: (f, s) => void this.registerSymbol(f, s),
                locationLabel: context.locationLabel,
                onSkip: context.onSkip,
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
    ): { from: number; to: number } | null {
        const cm = cmIn.dom.isConnected ? cmIn : this.getActiveCm();
        if (!cm) return null;
        const doc = cm.state.doc;
        let to = Math.min(range.to, doc.length);
        let from = Math.min(range.from, to);
        if (doc.sliceString(from, to) !== originalText) {
            const idx = doc.toString().lastIndexOf(originalText);
            if (idx < 0) {
                new Notice("VoxCraft: 対象が編集されたため置換できませんでした。");
                return null;
            }
            from = idx;
            to = idx + originalText.length;
        }
        if (newText !== originalText) {
            cm.dispatch({ changes: { from, to, insert: newText } });
        }
        return { from, to: from + newText.length };
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
            const result = await this.addReplacementDirect(url, from, to);
            new Notice(
                result.created
                    ? `VoxCraft: 辞書に登録しました — ${from} → ${to}（次回の録音から反映）`
                    : `VoxCraft: すでに同じ辞書登録があります — ${from} → ${to}`
            );
        } catch (e) {
            new Notice(`VoxCraft: 辞書に登録できません — ${e instanceof Error ? e.message : e}`, 8000);
        }
    }

    // 記号候補を選んで「辞書に登録」したとき: 観測した綴り→記号を記号語辞書へ入れる。
    //
    // 置換辞書に入れないのが肝。『悪』→「。」を置換で持つと本文中の「悪」まで
    // 消える。記号語はチャンク全体が一致したときだけ効く（server/postproc.py の
    // apply_symbol_dictation）ので、1文字キーでも安全に登録できる。
    private async registerSymbol(from: string, symbol: string): Promise<void> {
        const observed = from.trim();
        if (!observed || !symbol) return;
        if (observed.length > 16) {
            new Notice("VoxCraft: 記号語として登録するには長すぎます（16字まで）。");
            return;
        }
        const url = this.activeUrl();
        if (!url) {
            new Notice("VoxCraft: 接続先が設定されていません。");
            return;
        }
        try {
            const target = await this.dictionaryTarget(url);
            const result = await addDictionarySymbol(
                url,
                target.profileId,
                observed,
                symbol,
                target.revision
            );
            new Notice(
                result.created
                    ? `VoxCraft: 記号語として登録しました — ${observed} → ${symbol}（次回の録音から反映）`
                    : `VoxCraft: すでに同じ記号語登録があります — ${observed} → ${symbol}`
            );
        } catch (e) {
            new Notice(`VoxCraft: 記号語を登録できません — ${e instanceof Error ? e.message : e}`, 8000);
        }
    }

    // ---- 言い直し（読み自体が壊れた完全誤認識の修正） ----

    // 言い直し待ちの出入り。サーバー側の認識設定もここで対にして切り替える。
    //
    // 言い直しの発話は「文脈のない単語1つ」になりがちで、口述用の initial_prompt
    // （文章向け）がそれを壊す。2026-08-05 実測（合成音声・kotoba）:
    //     「きよ」  prompt有→'キオ'  prompt無→'キヨ'
    //     「きよう」prompt有→'気を'  prompt無→'起用'
    // 文脈のある発話は prompt の有無で変わらないので、待っている間だけ外す。
    private setPendingRespeak(
        value: { from: number; to: number; text: string } | null
    ): void {
        const was = this.pendingRespeak !== null;
        this.pendingRespeak = value;
        const now = value !== null;
        if (was === now) return;
        // 録音していないときは送る先が無い（開始時に既定値から始まるので問題ない）。
        if (this.recording && this.mode === "dictation") this.socket?.sendTuneWord(now);
    }

    // 口述対象のエディタに選択範囲があるか（言い直しコマンドの成立条件）。
    private hasSelection(): boolean {
        const cm = this.cm;
        if (!cm || !cm.dom.isConnected) return false;
        return !cm.state.selection.main.empty;
    }

    // 「ここを〜」の対象範囲を決める。選択があればそれ、無ければカーソル位置の語。
    //
    // Android の Obsidian では単語のダブルタップ選択がまともに効かず、選択を
    // 前提にすると言い直し・再変換がその端末で使えない機能になってしまう。
    // カーソルを置くだけで対象が決まれば、タップ1回＋ボタン1回で届く。
    // 推定した範囲は必ず選択表示にして、何が対象になったかを見えるようにする。
    private targetRange(cm: EditorView): { from: number; to: number } | null {
        const sel = cm.state.selection.main;
        if (!sel.empty) return { from: sel.from, to: sel.to };
        const range = wordRangeAt(cm.state.doc.toString(), sel.head);
        if (!range) return null;
        cm.dispatch({ selection: { anchor: range.from, head: range.to } });
        return range;
    }

    // 「ここを言い直し」: 選択範囲を覚え、次の発話1回だけをその範囲への置換にする。
    private startRespeak(): void {
        const cm = this.cm;
        if (!cm || !cm.dom.isConnected) {
            new Notice("VoxCraft: 録音中に、置き換えたい範囲を選択して使ってください。");
            return;
        }
        const range = this.targetRange(cm);
        if (!range) {
            new Notice(
                "VoxCraft: 言い直す場所が決まりません。" +
                "直したい語の中にカーソルを置くか、範囲を選択してください。"
            );
            return;
        }
        const text = cm.state.doc.sliceString(range.from, range.to);
        this.setPendingRespeak({ from: range.from, to: range.to, text });
        // 何が対象になったかを必ず見せる。カーソルから推定した場合は特に、
        // 意図と違う範囲のまま喋られると本文が飛ぶ。
        this.setStatus(`言い直し待ち —「${text}」を次の発話で置換`);
    }

    // 「Xを言い直し」: 直す場所を、選択ではなく声で指す。
    //
    // 手が塞がっているとき（歩きながら・書きながら）に範囲選択を挟むのが一番の
    // 手間なので、言った語を本文から探して選び、そのまま置換待ちにする。
    // まず発話どおりの表記で探し、外れたら「Aを再変換」と同じ読み由来の表記候補で
    // もう一度探す（直したいのはたいてい誤変換された表記＝発話とは違う字面）。
    private async respeakByTarget(target: string): Promise<void> {
        const cm = this.cm;
        if (!cm || !cm.dom.isConnected) {
            new Notice("VoxCraft: 録音中に使ってください。");
            return;
        }

        let hit = this.findLastSurface(cm, [target]);
        if (!hit) {
            const url = this.activeUrl();
            if (url) {
                this.setStatus("言い直す場所を探しています…");
                try {
                    const payload = await fetchReconvert(
                        url,
                        target,
                        this.appliedDictionarySetId(url)
                    );
                    if (payload.online) {
                        hit = this.findLastSurface(cm, buildSurfaces(target, payload.segments));
                    }
                } catch {
                    /* 探せなければ下の案内に落ちる */
                }
                this.setStatus(this.idleStatus());
            }
        }
        if (!hit) {
            new Notice(
                `VoxCraft: 「${target}」が本文に見つかりません。` +
                "直したい範囲を選んで「ここを言い直し」と言ってください。"
            );
            return;
        }

        cm.dispatch({ selection: { anchor: hit.from, head: hit.to }, scrollIntoView: true });
        const found = cm.state.doc.sliceString(hit.from, hit.to);
        this.setPendingRespeak({ from: hit.from, to: hit.to, text: found });
        this.setStatus(`言い直し待ち —「${found}」を次の発話で置換`);
    }

    // 言い直しの発話を、覚えていた範囲に検証付きで適用する。
    private applyRespeak(text: string): void {
        const pr = this.pendingRespeak;
        this.setPendingRespeak(null);
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

        // 漢字だった箇所を言い直したのに、かなだけが返ってきた ＝ 変換が要る。
        // 「起用」を「きよ」と言い直して「キヨ」が入る、で止まらせない。
        if (needsConversion(pr.text, text)) {
            void this.offerConversion({ from, to: from + text.length }, text, cm);
        }
    }

    // 言い直しで入ったかな語を、そのまま変換候補モーダルに載せる。
    // ここまで来たら本文は既に置き換わっているので、候補を選ばなくても損はしない。
    private async offerConversion(
        range: { from: number; to: number },
        text: string,
        cm: EditorView
    ): Promise<void> {
        const url = this.activeUrl();
        if (!url) return;
        this.setStatus("変換候補を取得中…");
        try {
            const payload = await fetchReconvert(url, text, this.appliedDictionarySetId(url));
            this.setStatus(this.idleStatus());
            if (!payload.online) return;
            this.openReconvertModalFor(range, text, payload, cm);
        } catch {
            this.setStatus(this.idleStatus());
        }
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
            const result = await recognizeRange(
                url,
                this.session,
                range.start,
                range.end,
                2,
                this.appliedDictionarySetId(url)
            );
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
        this.pendingReconvert = { from, to: anchor, text: targetText };
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
        const modal = new ReconvertModal(
            this.app,
            segments,
            (chosen) => {
                this.applyReconvert(target, chosen.join(""));
            },
            {
                originalText: target.text,
                onRegister: (f, t) => void this.registerReplacement(f, t),
                onRegisterSymbol: (f, s) => void this.registerSymbol(f, s),
            }
        );
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

    dictionarySetIdFor(url: string): string {
        return this.settings.dictionarySetByEndpoint[url.trim()] || "default";
    }

    // サーバー機のどの入力先から PC音声を取るか。空ならサーバーの既定の出力。
    systemDeviceFor(url: string): string {
        return this.settings.systemDeviceByEndpoint[url.trim()] || "";
    }

    async setSystemDeviceFor(url: string, device: string): Promise<void> {
        const key = url.trim();
        if (!key) return;
        if (device) this.settings.systemDeviceByEndpoint[key] = device;
        else delete this.settings.systemDeviceByEndpoint[key];
        await this.saveSettings();
        new Notice(
            `VoxCraft: PC音声の入力先を「${device || "既定の出力"}」にしました` +
            (this.recording ? "（現在の録音には影響せず、次回から適用）" : "")
        );
    }

    async setDictionarySetFor(url: string, setId: string): Promise<void> {
        const key = url.trim();
        if (!key || !setId) return;
        this.settings.dictionarySetByEndpoint[key] = setId;
        await this.saveSettings();
        new Notice(
            `VoxCraft: 辞書セットを「${setId}」にしました` +
            (this.recording ? "（現在の録音には影響せず、次回から適用）" : "")
        );
    }

    private appliedDictionarySetId(url: string): string {
        if (this.socket?.activeUrl === url && this.activeDictionarySetId) {
            return this.activeDictionarySetId;
        }
        return this.dictionarySetIdFor(url);
    }

    private async dictionaryTarget(url: string): Promise<{
        setId: string;
        setName: string;
        profileId: string;
        profileName: string;
        revision?: string;
    }> {
        const catalog = await fetchDictionaryCatalog(url);
        const setId = this.appliedDictionarySetId(url);
        const set = catalog.sets.find((item) => item.id === setId && item.valid);
        if (!set) throw new Error(`辞書セット「${setId}」を利用できません`);
        const profileId = set.writableProfile || set.profiles[set.profiles.length - 1];
        const profile = catalog.profiles.find((item) => item.id === profileId);
        if (!profile?.valid) throw new Error(`登録先辞書「${profileId}」を利用できません`);
        return {
            setId,
            setName: set.name,
            profileId,
            profileName: profile.name,
            revision: set.profileRevisions[profileId],
        };
    }

    private async addReplacementDirect(url: string, from: string, to: string) {
        const observed = from.trim();
        const output = to.trim();
        if (observed.length < 2) {
            throw new Error("1文字のキーは誤置換しやすいため、2文字以上を指定してください");
        }
        if (observed.length > 128 || output.length > 256) {
            throw new Error("登録できる長さを超えています（キー128字・値256字まで）");
        }
        const target = await this.dictionaryTarget(url);
        return addDictionaryEntry(
            url,
            target.profileId,
            observed,
            output,
            target.revision
        );
    }

    private async openDictionarySetModal(): Promise<void> {
        const url = this.activeUrl();
        if (!url) {
            new Notice("VoxCraft: 接続先が設定されていません");
            return;
        }
        new DictionarySetModal(
            this.app,
            url,
            this.dictionarySetIdFor(url),
            (setId) => this.setDictionarySetFor(url, setId)
        ).open();
    }

    private async openQuickAddModal(): Promise<void> {
        const cm = this.cm && this.cm.dom.isConnected ? this.cm : this.getActiveCm();
        if (!cm) {
            new Notice("VoxCraft: ノートを編集モードで開いてください");
            return;
        }
        const selection = cm.state.selection.main;
        if (selection.empty) {
            new Notice("VoxCraft: 誤認識した表記を選択してから辞書追加を実行してください");
            return;
        }
        const observed = cm.state.doc.sliceString(selection.from, selection.to);
        if (observed.length > 128) {
            new Notice("VoxCraft: 選択が長すぎます（128文字まで）");
            return;
        }
        const url = this.activeUrl();
        if (!url) {
            new Notice("VoxCraft: 接続先が設定されていません");
            return;
        }
        try {
            const target = await this.dictionaryTarget(url);
            new QuickAddDictionaryModal(
                this.app,
                observed,
                `${target.setName} › ${target.profileName}`,
                async (from, to) => {
                    const result = await this.addReplacementDirect(url, from, to);
                    new Notice(
                        result.created
                            ? `VoxCraft: 辞書に登録しました — ${from} → ${to}（次回の録音から反映）`
                            : `VoxCraft: すでに同じ登録があります — ${from} → ${to}`
                    );
                }
            ).open();
        } catch (error) {
            new Notice(`VoxCraft: 辞書を準備できません — ${error instanceof Error ? error.message : error}`, 8000);
        }
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
        this.setStatus(this.pendingRespeak ? `言い直し待ち ${bar}` : `${this.idleStatus()} ${bar}`);
    }

    private setStatus(text: string): void {
        this.statusEl.setText(`🎙 VoxCraft: ${text}`);
        if (this.recording && this.activeDictionarySetName) {
            const profiles = Object.keys(this.activeDictionaryProfileRevisions).join(" + ");
            this.statusEl.setAttribute(
                "title",
                `辞書: ${this.activeDictionarySetName} (${this.activeDictionarySetId})\n` +
                `revision: ${this.activeDictionaryRevision}\n` +
                `profiles: ${profiles}\n登録先: ${this.activeDictionaryWritableProfile}`
            );
        } else {
            this.statusEl.removeAttribute("title");
        }
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
