// 登録前プレビューの試験。
//
// ここが狂うと「壊れる箇所を少なく見せる」＝ 事故を素通しさせるので、
// 件数の数え方（重なりなし・置換結果を再走査しない）を特に固定しておく。

import {
    matchPositions,
    looksLikeAbbreviation,
    previewReplacement,
    runDictionary,
} from "./dictpreview";

let failures = 0;

function check(name: string, ok: boolean): void {
    if (ok) {
        console.log(`  ok  ${name}`);
        return;
    }
    failures += 1;
    console.error(`  NG  ${name}`);
}

function eq<T>(name: string, actual: T, expected: T): void {
    const ok = JSON.stringify(actual) === JSON.stringify(expected);
    if (!ok) console.error(`      actual=${JSON.stringify(actual)} expected=${JSON.stringify(expected)}`);
    check(name, ok);
}

// ---- 出現位置 ----
eq("重なりなしで数える", matchPositions("ああああ", "ああ"), [0, 2]);
eq("見つからない", matchPositions("あいうえお", "かき"), []);
eq("空のキーは0件", matchPositions("あいうえお", ""), []);
eq("空の本文は0件", matchPositions("", "あ"), []);

// 本番（ReplacementPlan）は正規表現の1回走査なので、置換した結果は再走査されない。
// 「AA→A」を "AAAA" に当てると本番は2箇所。ここも2でなければならない。
eq("置換結果を再走査しない", matchPositions("AAAA", "AA"), [0, 2]);

// ---- 省略の検出 ----
// 実際にやりかけた事故。要約の略称をそのまま正解にすると本文が潰れる。
check("省略: マイナンバーカード→マイナカード", looksLikeAbbreviation("マイナンバーカード", "マイナカード"));
check("省略: デジタル認証アプリ→認証アプリ", looksLikeAbbreviation("デジタル認証アプリ", "認証アプリ"));
// 本物の誤認識は長さが同じか伸びるので当たらない＝警告で邪魔をしない。
check("誤認識は省略でない: パスピー→パスキー", !looksLikeAbbreviation("パスピー", "パスキー"));
check("誤認識は省略でない: アジアティック…→エージェンティック…",
    !looksLikeAbbreviation("アジアティックコーナース", "エージェンティックコマース"));
check("誤認識は省略でない: マイナンバーパード→マイナンバーカード",
    !looksLikeAbbreviation("マイナンバーパード", "マイナンバーカード"));
// 認識が1〜2文字を足す壊し方。部分列にはなるが省略ではない（実測でこれを
// 省略として捨てると、採用すべき候補を落とす）。
check("挿入型の誤認識は省略でない: マイナーアプリ→マイナアプリ",
    !looksLikeAbbreviation("マイナーアプリ", "マイナアプリ"));
check("挿入型の誤認識は省略でない: マイナップポータル→マイナポータル",
    !looksLikeAbbreviation("マイナップポータル", "マイナポータル"));
check("挿入型の誤認識は省略でない: マイナルアプリ→マイナアプリ",
    !looksLikeAbbreviation("マイナルアプリ", "マイナアプリ"));
// 部分列でなければ短くても省略ではない（言い換え）。
check("言い換えは省略でない: 愛称番号→PIN", !looksLikeAbbreviation("愛称番号", "PIN"));
check("同じ長さは省略でない", !looksLikeAbbreviation("あいう", "かきく"));
check("空は省略でない", !looksLikeAbbreviation("あいう", ""));

// ---- プレビュー本体 ----
const note = [
    "マイナンバーカードを使ってログインします。",
    "スマホ搭載のマイナンバーカードも同じです。",
    "マイナカードと略すこともあります。",
].join("\n");

const danger = previewReplacement(note, "マイナンバーカード", "マイナカード");
eq("正しい語を潰す登録は件数が出る", danger.count, 2);
check("省略として警告される", danger.looksLikeAbbreviation);
check("正解候補が本文に既にある", danger.outputAlreadyIn === 1);

const safe = previewReplacement(note, "パスピー", "パスキー");
eq("このノートに無ければ0件", safe.count, 0);
eq("0件なら文脈も無い", safe.hits.length, 0);
check("0件でも省略警告は出ない", !safe.looksLikeAbbreviation);

// 文脈は元テキストから切り出す（置換前の姿を見せる）。
const one = previewReplacement("あああマイナンバーカードいいい", "マイナンバーカード", "マイナカード", { context: 3 });
eq("前文脈", one.hits[0].before, "あああ");
eq("後文脈", one.hits[0].after, "いいい");
check("端まで出したら切り詰め印は立たない", !one.hits[0].clippedBefore && !one.hits[0].clippedAfter);

const clipped = previewReplacement("0123456789X9876543210", "X", "Y", { context: 2 });
eq("文脈は指定文字数で切る", [clipped.hits[0].before, clipped.hits[0].after], ["89", "98"]);
check("切り詰めたら印が立つ", clipped.hits[0].clippedBefore && clipped.hits[0].clippedAfter);

const many = previewReplacement("あ".repeat(20), "あ", "い", { limit: 3 });
eq("件数は全部数える", many.count, 20);
eq("表示は limit まで", many.hits.length, 3);
check("表示しきれないことが分かる", many.truncated);

// ---- 辞書ぜんぶをノートに当てる ----
// サーバーは全キーを長い順に繋いだ正規表現で1回だけ走査する。ここが食い違うと
// 「プレビューでは2箇所と言ったのに3箇所変わった」になるので、意味論を固定する。

const PAIRS = [
    // サーバーが配る順（キーの長い順）をそのまま渡す前提。
    { observed: "デジタル認識アプリ", output: "デジタル認証アプリ" },
    { observed: "マイナンバーパード", output: "マイナンバーカード" },
    { observed: "デジタル認識", output: "デジタル認証" },
    { observed: "マイナポタル", output: "マイナポータル" },
];

const NOTE = "デジタル認識アプリの話。デジタル認識の状況。マイナポタルへログイン。";

const run = runDictionary(NOTE, PAIRS);
eq("置換後のテキスト", run.text, "デジタル認証アプリの話。デジタル認証の状況。マイナポータルへログイン。");
eq("変わる箇所の総数", run.total, 3);

// 包含関係のあるキーを二重に数えない。長いほうが当たった位置で短いほうは数えない。
const longKey = run.entries.find((e) => e.observed === "デジタル認識アプリ");
const shortKey = run.entries.find((e) => e.observed === "デジタル認識");
eq("長いキーが当たる", longKey ? longKey.count : 0, 1);
eq("短いキーは残りだけ", shortKey ? shortKey.count : 0, 1);

// 当たらなかった登録は内訳に出ない（「登録数」ではなく「変わる数」を見せる）。
check("当たらない登録は出さない", !run.entries.some((e) => e.observed === "マイナンバーパード"));

// 置換した結果は再走査しない（サーバーの1回走査と同じ）。
const chain = runDictionary("AA", [
    { observed: "AA", output: "AB" },
    { observed: "AB", output: "CC" },
]);
eq("置換結果を再走査しない", chain.text, "AB");

// 外した登録は当たらない＝チェックを外して数え直せる。
const fewer = runDictionary(NOTE, PAIRS.filter((p) => p.observed !== "デジタル認識アプリ"));
eq("長いキーを外すと短いキーが拾う", fewer.text,
    "デジタル認証アプリの話。デジタル認証の状況。マイナポータルへログイン。");
eq("外しても総数は変わらないこともある", fewer.total, 3);

// 正規表現の特殊文字を含むキーでも壊れない。
const special = runDictionary("a+b と a+b", [{ observed: "a+b", output: "A＋B" }]);
eq("特殊文字のキー", special.text, "A＋B と A＋B");
eq("特殊文字のキーの件数", special.total, 2);

// 文脈は元テキストから切り出す。
const ctx = runDictionary("あああマイナポタルいいい", PAIRS, { context: 3 });
eq("文脈の前", ctx.entries[0].hits[0].before, "あああ");
eq("文脈の後", ctx.entries[0].hits[0].after, "いいい");

eq("空の辞書なら何も起きない", runDictionary(NOTE, []).total, 0);
eq("空の本文なら何も起きない", runDictionary("", PAIRS).total, 0);

if (failures > 0) {
    console.error(`\n${failures} 件失敗`);
    process.exit(1);
}
console.log("dictpreview.test.ts: すべて通過");
