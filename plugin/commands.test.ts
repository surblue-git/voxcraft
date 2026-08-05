import {
    matchByReading,
    needsConversion,
    parseCommand,
    parseProbeCommand,
    readingKey,
} from "./commands";

let failures = 0;

function check(name: string, ok: boolean, detail = ""): void {
    if (!ok) {
        failures += 1;
        console.error(`FAIL ${name}${detail ? ` — ${detail}` : ""}`);
    }
}

function kind(cmd: ReturnType<typeof parseCommand>): string {
    return cmd ? cmd.kind : "(none)";
}

// ---- 読みキーの正規化 ----
// 自動句読点が語中に差し込む「。」（実測: にゅうりょ。くキャンセル）と、
// 長音符の有無で照合が外れないこと。
check("readingKey: 句読点を落とす", readingKey("ニュウリョ。クキャンセル") === "にゅうりょくきゃんせる");
check("readingKey: 長音符を落とす", readingKey("サンバー") === "さんば");

// ---- 既存の完全一致は不変 ----
check("完全一致: 入力キャンセル", kind(parseCommand("入力キャンセル")) === "cancelInput");
check("完全一致: 入力終了", kind(parseCommand("入力終了")) === "stop");
check("完全一致: ここを言い直し", kind(parseCommand("ここを言い直し")) === "respeak");

// ---- 言い直し起動語の explicit 区別 ----
// explicit なら選択が無くてもカーソル位置の語を対象にしてよい。
// 「訂正」のような本文にも出る語は、選択があるときだけコマンドにする（従来の担保）。
function explicitOf(text: string): boolean | null {
    const c = parseCommand(text);
    return c && c.kind === "respeak" ? c.explicit : null;
}
check("explicit: ここを言い直し", explicitOf("ここを言い直し") === true);
check("explicit: 言い直し", explicitOf("言い直し") === true);
check("explicit: これを訂正", explicitOf("これを訂正") === true);
check("plain: 訂正", explicitOf("訂正") === false);
check("plain: 言い換え", explicitOf("言い換え") === false);
check("plain: 差し替え", explicitOf("差し替え") === false);
// 「これを言い直し」は RESPEAK_TARGET_RE 経由でも explicit に落ちること。
check("explicit: これを言い直して", explicitOf("これを言い直して") === true);
check("完全一致: 三番", kind(parseCommand("三番")) === "pick");
check("完全一致: AをBに修正", kind(parseCommand("参加を惨禍に修正")) === "replace");
check("完全一致: Aを再変換", kind(parseCommand("スミシンを再変換")) === "reconvertTarget");
check("本文はコマンドにしない", parseCommand("今日は入力キャンセルの話をします") === null);

// ---- 「Xを言い直し」（新規） ----
const st = parseCommand("スミシンを言い直し");
check("Xを言い直し", st?.kind === "respeakTarget", kind(st));
check(
    "Xを言い直し: target",
    st?.kind === "respeakTarget" && st.target === "スミシン",
    JSON.stringify(st)
);
check("これを言い直し は選択範囲", kind(parseCommand("これを言い直し")) === "respeak");
check("ここを訂正 は選択範囲", kind(parseCommand("ここを訂正")) === "respeak");
// 「AをBに修正/変換」を横取りしないこと（REPLACE_RE / RECONVERT_TARGET_RE の担当）。
check("横取りしない: AをBに修正", kind(parseCommand("参加を惨禍に修正")) === "replace");
check("横取りしない: Aを再変換", kind(parseCommand("スミシンを再変換")) === "reconvertTarget");

// ---- 読みでのあいまい照合 ----
function fuzzy(text: string, reading: string) {
    return matchByReading(text, reading);
}

const m1 = fuzzy("ニュリョクキャンセル", "ニュリョクキャンセル");
check("あいまい: にゅりょくキャンセル", m1?.cmd.kind === "cancelInput" && m1.confident, JSON.stringify(m1));

const m2 = fuzzy("乳酸キャンセル", "ニュウサンキャンセル");
check("あいまい: 乳酸キャンセル", m2?.cmd.kind === "cancelInput" && m2.confident, JSON.stringify(m2));

// 自動句読点で壊れた形も通ること。
const m3 = fuzzy("にゅうりょ。くキャンセル", "ニュウリョ。クキャンセル");
check("あいまい: 句読点入り", m3?.cmd.kind === "cancelInput" && m3.confident, JSON.stringify(m3));

// 同音の誤変換（読みは完全に一致、表記だけ違う）。
const m4 = fuzzy("入力復言", "ニュウリョクフクゲン");
check("あいまい: 同音の誤変換", m4?.cmd.kind === "restoreInput" && m4.confident, JSON.stringify(m4));

// 読みが無ければ照合しない（sudachi 未導入のサーバー ＝ 従来どおり完全一致のみ）。
check("読みが無ければ照合しない", fuzzy("ニュリョクキャンセル", "") === null);
// プレフィックス指定時も照合しない（接頭語ぶんの読みを差し引けないため）。
check(
    "プレフィックス時は照合しない",
    matchByReading("ニュリョクキャンセル", "ニュリョクキャンセル", "コマンド") === null
);

// 普通の本文が誤爆しないこと。
check("誤爆しない: 長い本文", fuzzy(
    "この機能は入力の取り消しをやりやすくするためのものです",
    "コノキノウハニュウリョクノトリケシヲヤリヤスクスルタメノモノデス"
) === null);
check("誤爆しない: 関係", fuzzy("関係", "カンケイ") === null);
check("誤爆しない: 修行", fuzzy("修行", "シュギョウ") === null);

// 音ごと崩れた誤認識は救えない（正直な限界。RESPEAK_LOOSE_RE と選択範囲で拾う側）。
check("限界: 入れてほしい", fuzzy("入れてほしい", "イレテホシイ") === null);

// ---- 先読み（probe）の採否 ----
check(
    "probe: 固定句は採る",
    parseProbeCommand("ニュリョクキャンセル", "ニュリョクキャンセル")?.kind === "cancelInput"
);
check("probe: 完全一致の固定句も採る", parseProbeCommand("改行", "カイギョウ")?.kind === "newline");
// 引数つきは表記の精度が要るので速報では判断しない。
check("probe: AをBに修正は採らない", parseProbeCommand("参加を惨禍に修正", "サンカヲサンカニシュウセイ") === null);
check("probe: Xを言い直しは採らない", parseProbeCommand("スミシンを言い直し", "スミシンヲイイナオシ") === null);
check("probe: Aを再変換は採らない", parseProbeCommand("スミシンを再変換", "スミシンヲサイヘンカン") === null);
// 「惜しい外れ」は速報では実行しない（確認は本命の認識結果で出す）。
const nearOnly = fuzzy("入力復旧", "ニュウリョクフッキュウ");
check("惜しい外れ: 確信は付かない", nearOnly !== null && !nearOnly.confident, JSON.stringify(nearOnly));
check("probe: 惜しい外れは採らない", parseProbeCommand("入力復旧", "ニュウリョクフッキュウ") === null);

// ---- 言い直し結果に変換が要るかの判定 ----
// 実際に起きた流れ: 「起用」を選んで言い直し →「きよ」と発話 →「キヨ」が入る。
// ここで候補を出さないと、もう一度「再変換」と言う羽目になる。
check("要変換: 漢字→カタカナ", needsConversion("起用", "キヨ"));
check("要変換: 漢字→ひらがな", needsConversion("起用", "きよ"));
check("要変換: 漢字かな混じり→かな", needsConversion("言い直し", "いいなおし"));
// 元がかなだけなら、かなで返るのが正しい。
check("不要: かな→かな", !needsConversion("ですます", "でした"));
// 変換済みで返ってきたなら候補は要らない。
check("不要: 漢字→漢字", !needsConversion("起用", "寄与"));
check("不要: 漢字→漢字かな混じり", !needsConversion("起用", "寄与した"));
// 長い言い直しは文なので、単語の変換候補にはかけない。
check("不要: 長すぎるかな", !needsConversion("起用", "きょうはいいてんきですね"));
check("不要: 空", !needsConversion("起用", ""));

if (failures > 0) {
    console.error(`\n${failures} 件失敗`);
    process.exit(1);
}
console.log("commands.test.ts: すべて通過");
