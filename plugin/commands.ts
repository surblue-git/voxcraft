// 音声コマンドの判定。
//
// 確定チャンクを本文として挿入する前に、これがコマンド発話かどうかを判定する。
// 誤爆防止のため、コマンドは「そのチャンク全体がコマンド文である」ときだけ発動する
// （本文の途中に紛れた語には反応しない）。プレフィックス語（例:「コマンド」）を
// 必須にする設定も可能。

export type VoiceCommand =
    | { kind: "stop" }                                   // 入力終了
    | { kind: "undo" }                                   // 直前チャンク取り消し
    | { kind: "newline" }                                // 改行
    | { kind: "reconvert" }                              // 変換戻し
    | { kind: "replace"; from: string; to: string }      // 「AをBに修正」
    | { kind: "pick"; index: number }                    // 候補選択「3番」
    | { kind: "reconvertTarget"; target: string }        // 「Aを再変換」（読みで探して候補提示）
    | { kind: "reconvertSelection" }                     // 「これを再変換」（選択範囲）
    | { kind: "respeak" }                                // 「ここを言い直し」（次の発話で選択範囲を置換）
    | { kind: "confirm" }                                // 「確定」（候補モーダルの確定）
    | { kind: "cancel" }                                 // 「キャンセル」（モーダル/言い直しの解除）
    | null;

const STOP = ["入力終了", "音声入力終了", "終了", "ストップ"];
const UNDO = ["取り消し", "取消", "一文削除", "今のを削除", "元に戻して"];
const NEWLINE = ["改行", "次の行"];
const RECONVERT = ["変換戻し", "変換し直し", "変換やり直し", "再変換"];
// 言い直しの起動語。「言い直し」は誤認識されやすい（実測で「入れてほしい」
// 「言い出ほしい」「合意で惜しい」に化ける）ため、認識しやすい短い語も足す。
// どれも「選択範囲があるときだけ」コマンドになるので、本文を壊さない
// （main.ts 側で選択が空なら本文として挿入する）。
const RESPEAK = [
    "言い直し", "言い直して", "言い直す",
    "ここを言い直し", "ここを言い直して",
    "これを言い直し", "これを言い直して",
    "訂正", "ここを訂正", "これを訂正", "訂正して",
    "言い換え", "ここを言い換え", "差し替え", "ここを差し替え",
];
// 上のリストから漏れた言い直し起動語を拾う保険。誤爆の代償を小さくするため
// 「選択範囲がある」「短い発話」の両方を満たすときだけ main.ts が採用する。
const RESPEAK_LOOSE_RE = /^(?:ここ|これ|この)を?(?:言い|いい|訂正|ていせい|差し替|言い換)/;
// 「確定」「キャンセル」は候補モーダル等が開いているときだけ意味を持つ。
// main.ts 側で「処理できなければ本文として挿入」に倒すので、通常口述を壊さない。
const CONFIRM = ["確定", "決定"];
const CANCEL = ["キャンセル", "やめる"];

// 「XをYに修正/変換/直して」
const REPLACE_RE = /^(.+?)を(.+?)に(?:修正|変換|直して|してください|変えて)$/;
// 「Xを再変換」— 読みで文書中の誤変換を探して候補を出す。
//   衝突しない根拠:
//     「AをBに修正」→ この正規表現の末尾語にマッチしない（REPLACE_RE が処理）
//     「AをBに再変換」→ REPLACE_RE は末尾「修正|変換|…」に不一致、こちらは
//        target が「AをB に」を含む形になりうるため、target に「を」を含む場合も
//        lastIndexOf 探索で自然に失敗し実害なし（通常この言い回しは使わない）
//     「再変換」単独 → 先に RECONVERT の完全一致が拾う（既存挙動を維持）
//     「換」を任意にしているのは、チャンク境界で語尾が切れて「スミシンを再変」に
//        なる実例があったため（切れたまま本文に混ざると、次の検索を汚す）
const RECONVERT_TARGET_RE =
    /^(.+?)を(?:再変換?|変換し直し|変換しなおし|もう一度変換)(?:て|して)?$/;
// target がこれらなら「選択範囲の再変換」として扱う。
const SELECTION_WORDS = new Set(["これ", "ここ", "選択範囲", "選択部分"]);
// 「3番」「三番」「候補3」
const PICK_RE = /^(?:候補)?([0-9０-９一二三四五六七八九十]+)\s*番?$/;

const KANJI_NUM: Record<string, number> = {
    一: 1, 二: 2, 三: 3, 四: 4, 五: 5,
    六: 6, 七: 7, 八: 8, 九: 9, 十: 10,
};

function normalize(text: string): string {
    // 末尾の句読点・空白を落として素の発話に近づける。
    return text.trim().replace(/[。、．，\s]+$/u, "");
}

// カタカナ→ひらがな。音声「サンバン」「サンバー」を数字として読むために使う。
function toHiragana(text: string): string {
    return text.replace(/[ァ-ヶ]/g, (c) =>
        String.fromCharCode(c.charCodeAt(0) - 0x60)
    );
}

// 数の読み（モーダル操作中の緩い判定でのみ使う）。
// 伸ばし棒を落とした形（きゅー→きゅ）も引けるようにしてある。
const KANA_NUM: Record<string, number> = {
    いち: 1, に: 2, さん: 3, よん: 4, し: 4, ご: 5,
    ろく: 6, なな: 7, しち: 7, はち: 8, きゅう: 9, きゅ: 9, く: 9,
    じゅう: 10, じゅ: 10,
};

function toNumber(token: string): number | null {
    const zen = token.replace(/[０-９]/g, (c) =>
        String.fromCharCode(c.charCodeAt(0) - 0xfee0)
    );
    if (/^[0-9]+$/.test(zen)) return parseInt(zen, 10);
    if (token in KANJI_NUM) return KANJI_NUM[token];
    return null;
}

// 候補モーダルが開いている間だけ使う判定。
//
// この状態では発話は本文に入らない（main.ts が捕まえる）ので、誤爆の代償が無い。
// そのぶん取りこぼしを減らす側に振り、読み・伸ばし棒・「番」の脱落まで許す。
// 「サンバー」が本文に入ってしまった実例への対処。
export function parseModalCommand(rawText: string): VoiceCommand {
    const text = normalize(rawText).replace(/[\s・]+/gu, "");
    // カタカナで返ることもある（「カクテイ」）ので、ひらがなに寄せた形でも照合する。
    const hira = toHiragana(text);
    const isEither = (re: RegExp) => re.test(text) || re.test(hira);
    if (isEither(/^(?:確定|決定|かくてい|けってい|これでいい|おーけー|ok)$/i)) {
        return { kind: "confirm" };
    }
    if (isEither(/^(?:きゃんせる|やめる|止める|とめる|閉じる|とじる|戻る|もどる|中止|ちゅうし)$/)) {
        return { kind: "cancel" };
    }

    // 「候補」「番」を落とし、伸ばし棒（サンバー）も吸収してから数として読む。
    const body = hira
        .replace(/ー/g, "")
        .replace(/^(?:候補|こうほ)/, "")
        .replace(/(?:番目|ばんめ|番|ばん|ば)$/, "");
    const n = toNumber(body) ?? KANA_NUM[body];
    if (n !== undefined && n !== null) return { kind: "pick", index: n };
    return null;
}

// 起動語リストから漏れた「言い直し」を拾えるか（選択範囲があるときだけ使う）。
export function looksLikeRespeak(rawText: string): boolean {
    const text = normalize(rawText);
    return text.length <= 15 && RESPEAK_LOOSE_RE.test(text);
}

// prefix が空文字なら常に判定、非空なら「prefix …」で始まるチャンクのみ判定。
export function parseCommand(rawText: string, prefix = ""): VoiceCommand {
    let text = normalize(rawText);

    if (prefix) {
        const p = normalize(prefix);
        if (!text.startsWith(p)) return null;
        text = normalize(text.slice(p.length).replace(/^[、,\s]+/u, ""));
    }

    if (STOP.includes(text)) return { kind: "stop" };
    if (UNDO.includes(text)) return { kind: "undo" };
    if (NEWLINE.includes(text)) return { kind: "newline" };
    if (RECONVERT.includes(text)) return { kind: "reconvert" };
    if (RESPEAK.includes(text)) return { kind: "respeak" };
    if (CONFIRM.includes(text)) return { kind: "confirm" };
    if (CANCEL.includes(text)) return { kind: "cancel" };

    const pick = text.match(PICK_RE);
    if (pick) {
        const n = toNumber(pick[1]);
        if (n !== null) return { kind: "pick", index: n };
    }

    // REPLACE_RE より先に判定する（「Aを再変換」を「AをB…」と誤読させない）。
    const rt = text.match(RECONVERT_TARGET_RE);
    if (rt) {
        const target = rt[1].trim();
        if (SELECTION_WORDS.has(target)) return { kind: "reconvertSelection" };
        if (target) return { kind: "reconvertTarget", target };
    }

    const rep = text.match(REPLACE_RE);
    if (rep) {
        const from = rep[1].trim();
        const to = rep[2].trim();
        if (from && to) return { kind: "replace", from, to };
    }

    return null;
}
