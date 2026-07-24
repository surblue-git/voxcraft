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
    | null;

const STOP = ["入力終了", "音声入力終了", "終了", "ストップ"];
const UNDO = ["取り消し", "取消", "一文削除", "今のを削除", "元に戻して"];
const NEWLINE = ["改行", "次の行"];
const RECONVERT = ["変換戻し", "変換し直し", "変換やり直し", "再変換"];

// 「XをYに修正/変換/直して」
const REPLACE_RE = /^(.+?)を(.+?)に(?:修正|変換|直して|してください|変えて)$/;
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

function toNumber(token: string): number | null {
    const zen = token.replace(/[０-９]/g, (c) =>
        String.fromCharCode(c.charCodeAt(0) - 0xfee0)
    );
    if (/^[0-9]+$/.test(zen)) return parseInt(zen, 10);
    if (token in KANJI_NUM) return KANJI_NUM[token];
    return null;
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

    const pick = text.match(PICK_RE);
    if (pick) {
        const n = toNumber(pick[1]);
        if (n !== null) return { kind: "pick", index: n };
    }

    const rep = text.match(REPLACE_RE);
    if (rep) {
        const from = rep[1].trim();
        const to = rep[2].trim();
        if (from && to) return { kind: "replace", from, to };
    }

    return null;
}
