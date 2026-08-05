// 音声コマンドの判定。
//
// 確定チャンクを本文として挿入する前に、これがコマンド発話かどうかを判定する。
// 誤爆防止のため、コマンドは「そのチャンク全体がコマンド文である」ときだけ発動する
// （本文の途中に紛れた語には反応しない）。プレフィックス語（例:「コマンド」）を
// 必須にする設定も可能。

export type VoiceCommand =
    | { kind: "stop" }                                   // 入力終了
    | { kind: "cancelInput" }                            // 直前チャンクを入力キャンセル
    | { kind: "restoreInput" }                           // キャンセルしたチャンクを復元
    | { kind: "newline" }                                // 改行
    | { kind: "reconvert" }                              // 変換戻し
    | { kind: "replace"; from: string; to: string }      // 「AをBに修正」
    | { kind: "pick"; index: number }                    // 候補選択「3番」
    | { kind: "reconvertTarget"; target: string }        // 「Aを再変換」（読みで探して候補提示）
    | { kind: "reconvertSelection" }                     // 「これを再変換」（選択範囲）
    // 「ここを言い直し」（次の発話で対象範囲を置換）。
    // explicit=true は「本文には現れない、命令とわかる言い方」。この場合だけ、
    // 選択が無くてもカーソル位置の語を対象にしてよい（Androidで選択が困難なため）。
    // explicit=false は「訂正」のように本文にも出る語で、選択が無ければ本文として扱う。
    | { kind: "respeak"; explicit: boolean }
    | { kind: "respeakTarget"; target: string }          // 「Aを言い直し」（探して選び、次の発話で置換）
    | { kind: "confirm" }                                // 「確定」（候補モーダルの確定）
    | { kind: "cancel" }                                 // 「キャンセル」（モーダル/言い直しの解除）
    | null;

// 起動語は「表記」と「読み」を対にして持つ。
//
// 表記だけだと、音が合っていても認識の当て字が違うだけでコマンドが不成立になり、
// 命令がそのまま本文に落ちる（実例:「入力キャンセル」→「にゅりょくキャンセル」）。
// 読みを併記しておくと、サーバーが送ってくるチャンクの読みと編集距離で照合できる。
// 読みはすべてひらがな・長音符なしで書く（readingKey と同じ正規化に合わせる）。
interface CommandWord {
    word: string;
    reading: string;
}

const words = (...list: [string, string][]): CommandWord[] =>
    list.map(([word, reading]) => ({ word, reading }));

const STOP_WORDS = words(
    ["入力終了", "にゅうりょくしゅうりょう"],
    ["音声入力終了", "おんせいにゅうりょくしゅうりょう"],
    ["終了", "しゅうりょう"],
    ["ストップ", "すとっぷ"],
);
// 「取り消し」は一般語として単独で口述したい場合にも発火するため使わない。
// ツールバーと同じ名称にそろえ、発話全体が専用語のときだけ処理する。
const INPUT_CANCEL_WORDS = words(
    ["入力キャンセル", "にゅうりょくきゃんせる"],
    ["直前入力をキャンセル", "ちょくぜんにゅうりょくをきゃんせる"],
    ["今の入力をキャンセル", "いまのにゅうりょくをきゃんせる"],
);
const INPUT_RESTORE_WORDS = words(
    ["入力復元", "にゅうりょくふくげん"],
    ["入力を復元", "にゅうりょくをふくげん"],
    ["キャンセルを戻す", "きゃんせるをもどす"],
);
const NEWLINE_WORDS = words(["改行", "かいぎょう"], ["次の行", "つぎのぎょう"]);
const RECONVERT_WORDS = words(
    ["変換戻し", "へんかんもどし"],
    ["変換し直し", "へんかんしなおし"],
    ["変換やり直し", "へんかんやりなおし"],
    ["再変換", "さいへんかん"],
);
// 言い直しの起動語。「言い直し」は誤認識されやすい（実測で「入れてほしい」
// 「言い出ほしい」「合意で惜しい」に化ける）ため、認識しやすい短い語も足す。
//
// 起動語は2種類に分ける。本文を壊さない担保が違うため:
//   explicit … 命令とわかる言い方。本文にこの形で単独で現れることはまず無いので、
//              選択が無くてもカーソル位置の語を対象にしてよい。
//   plain    … 「訂正」のように本文にも出る語。選択があるときだけコマンドにする。
//              この条件があるおかげで、一般語を起動語にできている。
const RESPEAK_EXPLICIT_WORDS = words(
    ["言い直し", "いいなおし"], ["言い直して", "いいなおして"], ["言い直す", "いいなおす"],
    ["ここを言い直し", "ここをいいなおし"], ["ここを言い直して", "ここをいいなおして"],
    ["これを言い直し", "これをいいなおし"], ["これを言い直して", "これをいいなおして"],
    ["ここを訂正", "ここをていせい"], ["これを訂正", "これをていせい"],
    ["ここを言い換え", "ここをいいかえ"], ["ここを差し替え", "ここをさしかえ"],
);
const RESPEAK_PLAIN_WORDS = words(
    ["訂正", "ていせい"], ["訂正して", "ていせいして"],
    ["言い換え", "いいかえ"], ["差し替え", "さしかえ"],
);

const STOP = STOP_WORDS.map((w) => w.word);
const INPUT_CANCEL = INPUT_CANCEL_WORDS.map((w) => w.word);
const INPUT_RESTORE = INPUT_RESTORE_WORDS.map((w) => w.word);
const NEWLINE = NEWLINE_WORDS.map((w) => w.word);
const RECONVERT = RECONVERT_WORDS.map((w) => w.word);
const RESPEAK_EXPLICIT = RESPEAK_EXPLICIT_WORDS.map((w) => w.word);
const RESPEAK_PLAIN = RESPEAK_PLAIN_WORDS.map((w) => w.word);
// 上のリストから漏れた言い直し起動語を拾う保険。誤爆の代償を小さくするため
// 「選択範囲がある」「短い発話」の両方を満たすときだけ main.ts が採用する。
const RESPEAK_LOOSE_RE = /^(?:ここ|これ|この)を?(?:言い|いい|訂正|ていせい|差し替|言い換)/;
// 「確定」「キャンセル」は候補モーダル等が開いているときだけ意味を持つ。
// main.ts 側で「処理できなければ本文として挿入」に倒すので、通常口述を壊さない。
const CONFIRM_WORDS = words(["確定", "かくてい"], ["決定", "けってい"]);
const CANCEL_WORDS = words(["キャンセル", "きゃんせる"], ["やめる", "やめる"]);
const CONFIRM = CONFIRM_WORDS.map((w) => w.word);
const CANCEL = CANCEL_WORDS.map((w) => w.word);

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
// 「Xを言い直し」— 選択せずに、直す場所を声で指す。
//   これが無いと「スミシンを言い直し」がコマンドとして成立せず、命令文がそのまま
//   本文に落ちる（従来は「ここを」「これを」の固定句しか受け付けていなかった）。
//   衝突しない根拠:
//     「AをBに修正/変換」→ 末尾が一致しない（REPLACE_RE が処理）
//     「Aを再変換」      → RECONVERT_TARGET_RE を先に判定する
//     「ここを言い直し」  → RESPEAK の完全一致が先に拾う（従来どおりの選択範囲置換）
const RESPEAK_TARGET_RE =
    /^(.+?)を(?:言い直し|言い直す|いいなおし|訂正|ていせい|言い換え|いいかえ|差し替え|さしかえ)(?:て|して)?$/;
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
    if (INPUT_CANCEL.includes(text)) return { kind: "cancelInput" };
    if (INPUT_RESTORE.includes(text)) return { kind: "restoreInput" };
    if (NEWLINE.includes(text)) return { kind: "newline" };
    if (RECONVERT.includes(text)) return { kind: "reconvert" };
    if (RESPEAK_EXPLICIT.includes(text)) return { kind: "respeak", explicit: true };
    if (RESPEAK_PLAIN.includes(text)) return { kind: "respeak", explicit: false };
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

    const st = text.match(RESPEAK_TARGET_RE);
    if (st) {
        const target = st[1].trim();
        // 「ここを」「これを」は選択範囲の言い直し（従来の挙動）。
        if (SELECTION_WORDS.has(target)) return { kind: "respeak", explicit: true };
        if (target) return { kind: "respeakTarget", target };
    }

    const rep = text.match(REPLACE_RE);
    if (rep) {
        const from = rep[1].trim();
        const to = rep[2].trim();
        if (from && to) return { kind: "replace", from, to };
    }

    return null;
}

// ---- 読みでのあいまい照合 ----
//
// 表記の完全一致だけだと、音は合っているのに当て字が違うだけでコマンドが不成立になり、
// 命令文が本文に落ちる。サーバーが sudachi で付けた読みと、起動語の読みを
// 編集距離で比べれば、この取りこぼしの大半が救える。
//   「にゅりょくキャンセル」→ にゅりょくきゃんせる vs にゅうりょくきゃんせる = 距離1
//   「乳酸キャンセル」      → にゅうさんきゃんせる                          = 距離2
//   「サンバー」            → さんば（長音を落とす）vs さんばん             = 距離1
// 逆に「言い直し」→「入れてほしい」のような音ごと崩れた誤認識は距離が開くので
// 救えない。そこは従来どおり RESPEAK_LOOSE_RE と選択範囲の条件で拾う。

// 比較用のキー。ひらがなだけを残し、長音符と句読点・記号を落とす。
// 句読点を落とすのは、自動句読点がかな書きの誤認識の語中に「。」を差し込む実例が
// あるため（実測: にゅうりょくキャンセル → 「にゅうりょ。くキャンセル」）。
export function readingKey(text: string): string {
    return text
        .normalize("NFKC")
        .replace(/[ァ-ヶ]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0x60))
        .replace(/[^ぁ-ゖ]/gu, "");
}

function levenshtein(a: string, b: string): number {
    if (a === b) return 0;
    if (!a.length || !b.length) return a.length || b.length;
    let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
    for (let i = 1; i <= a.length; i += 1) {
        const cur = [i];
        for (let j = 1; j <= b.length; j += 1) {
            cur[j] = Math.min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1)
            );
        }
        prev = cur;
    }
    return prev[b.length];
}

// 語の長さに応じた許容距離。短い語ほど厳しくする。
// 「確定（かくてい）」と「関係（かんけい）」のような、2音違うだけの一般語が
// いくらでもある短い語で緩めると、本文が勝手にコマンドとして消えるため。
// 逆に「にゅうりょくきゃんせる」級の長い句は、そこまで似た一般語が無い。
function strictTolerance(len: number): number {
    // 7音以下は1音違いまで。この長さの語は一般語と当たりやすく、実測でも
    // 「修行（しゅぎょう）」が「終了（しゅうりょう）」と2音差だった。
    return len <= 7 ? 1 : Math.round(len * 0.25);
}

// 「惜しい外れ」として確認を出す上限。8文字未満では出さない（一般語と衝突して
// 通知だけが増える。実測: 「修行（しゅぎょう）」が「終了（しゅうりょう）」に2音差）。
function nearTolerance(len: number): number {
    return len < 8 ? strictTolerance(len) : strictTolerance(len) + 1;
}

export interface ReadingMatch {
    cmd: NonNullable<VoiceCommand>;
    phrase: string;   // 一致した起動語（通知に出す）
    distance: number;
    // true ならそのまま実行してよい。false は「惜しい外れ」で、本文に入れたうえで
    // 実行するかどうかをユーザーに聞く（勝手に本文を消さない）。
    confident: boolean;
}

const READING_TABLE: { words: CommandWord[]; cmd: NonNullable<VoiceCommand> }[] = [
    { words: STOP_WORDS, cmd: { kind: "stop" } },
    { words: INPUT_CANCEL_WORDS, cmd: { kind: "cancelInput" } },
    { words: INPUT_RESTORE_WORDS, cmd: { kind: "restoreInput" } },
    { words: NEWLINE_WORDS, cmd: { kind: "newline" } },
    { words: RECONVERT_WORDS, cmd: { kind: "reconvert" } },
    { words: RESPEAK_EXPLICIT_WORDS, cmd: { kind: "respeak", explicit: true } },
    { words: RESPEAK_PLAIN_WORDS, cmd: { kind: "respeak", explicit: false } },
    { words: CONFIRM_WORDS, cmd: { kind: "confirm" } },
    { words: CANCEL_WORDS, cmd: { kind: "cancel" } },
];

/**
 * 発話の読みを起動語の読みと突き合わせ、最も近いものを返す。
 *
 * reading はサーバーが付けたカタカナの読み。空なら（sudachi 未導入の環境など）
 * 照合しない ＝ 従来の完全一致だけが効く。prefix 指定時も照合しない
 * （接頭語ぶんの読みを差し引く判断が曖昧になるため、確実な完全一致に任せる）。
 */
export function matchByReading(
    rawText: string,
    reading: string,
    prefix = ""
): ReadingMatch | null {
    if (prefix || !reading) return null;
    const key = readingKey(reading) || readingKey(rawText);
    // 起動語はどれも短い。長い発話は本文なので相手にしない。
    if (!key || key.length > 24) return null;

    let best: ReadingMatch | null = null;
    for (const row of READING_TABLE) {
        for (const w of row.words) {
            const target = readingKey(w.reading);
            if (!target) continue;
            // 距離0（表記は違うが読みは同じ＝同音の誤変換）もここで拾う。
            // parseCommand が見ているのは表記なので、そちらは素通りしている。
            const d = levenshtein(key, target);
            if (d > nearTolerance(target.length)) continue;
            if (best && d >= best.distance) continue;
            best = {
                cmd: row.cmd,
                phrase: w.word,
                distance: d,
                confident: d <= strictTolerance(target.length),
            };
        }
    }
    return best;
}

/**
 * コマンド先読み（小さいモデルの速報）専用の判定。
 *
 * 速報は本文用の認識より精度が低いので、引数のない固定句だけを、
 * それも厳しい側の距離でしか受け付けない。「AをBに修正」のように
 * 引数の表記が要るものは、必ず本命の認識結果を待つ。
 */
export function parseProbeCommand(
    rawText: string,
    reading: string,
    prefix = ""
): NonNullable<VoiceCommand> | null {
    const exact = parseCommand(rawText, prefix);
    if (exact && ARGUMENT_FREE.has(exact.kind)) return exact;
    if (exact) return null; // 引数つきは速報で判断しない
    const near = matchByReading(rawText, reading, prefix);
    if (near && near.confident && ARGUMENT_FREE.has(near.cmd.kind)) return near.cmd;
    return null;
}

/**
 * 言い直しの結果に、まだ変換が要るか。
 *
 * 「漢字を含む語を言い直したのに、返ってきたのはかなだけ」＝ 読みは取れたが変換が
 * されていない状態。文脈のない単語を Whisper が漢字に起こすことはあまり無く
 * （実測「きよ」→「キヨ」）、ここで止めると結局もう一度「再変換」と言う羽目になる。
 * 元がかなだけの箇所（「ですます」等）は、かなで返るのが正しいので対象外。
 */
export function needsConversion(original: string, spoken: string): boolean {
    // 言い直しの対象は基本的に語1つ。これを超えるかな列は文なので、
    // 変換候補モーダルを出しても選びようがない（「きょうはいいてんきですね」等）。
    if (!spoken || spoken.length > 8) return false;
    const kanaOnly = /^[ぁ-ゖァ-ヶー]+$/u;
    return !kanaOnly.test(original) && kanaOnly.test(spoken);
}

const ARGUMENT_FREE = new Set<string>([
    "stop", "cancelInput", "restoreInput", "newline",
    "reconvert", "reconvertSelection", "respeak", "confirm", "cancel", "pick",
]);
