// 口述範囲が文書変更に追従するかのテスト。
//
// CodeMirror の状態を実際に作って、編集した後の範囲を確かめる。
// ここが狂うと「手で直した箇所を叩くと候補が出る」「口述した箇所が叩けない」
// のような、原因の分かりにくい不具合になる。

import { EditorState } from "@codemirror/state";

import {
    addDictatedEffect,
    clearDictatedEffect,
    dictatedExtension,
    dictatedRangesIn,
    clauseAround,
} from "./dictated";

let failures = 0;

function make(doc: string, ranges: { from: number; to: number }[]): EditorState {
    let state = EditorState.create({ doc, extensions: [dictatedExtension] });
    state = state.update({
        effects: ranges.map((r) => addDictatedEffect.of(r)),
    }).state;
    return state;
}

// 口述範囲を「文字列の配列」で取り出す（読みやすさのため）。
function texts(state: EditorState): string[] {
    return dictatedRangesIn(state).map((r) => state.doc.sliceString(r.from, r.to));
}

function eq(label: string, got: string[], want: string[]): void {
    const a = JSON.stringify(got);
    const b = JSON.stringify(want);
    if (a !== b) {
        failures++;
        console.error(`FAIL ${label}\n  got  ${a}\n  want ${b}`);
    } else {
        console.log(`ok   ${label}`);
    }
}

// 1. そのまま保持される
{
    const s = make("これは口述です", [{ from: 3, to: 5 }]);
    eq("そのまま", texts(s), ["口述"]);
}

// 2. 前に文字を挿入すると、範囲は後ろへずれる（内容は変わらない）
{
    let s = make("これは口述です", [{ from: 3, to: 5 }]);
    s = s.update({ changes: { from: 0, to: 0, insert: "さて、" } }).state;
    eq("前に挿入", texts(s), ["口述"]);
}

// 3. 範囲の中を手で書き換えても範囲は残る。周囲は口述のままなので、
//    「直した語だけ対象外にする」ことに実益が無いという判断。
{
    let s = make("これは口述です", [{ from: 3, to: 5 }]);
    s = s.update({ changes: { from: 3, to: 5, insert: "記述" } }).state;
    eq("中身を置換しても残る", texts(s), ["記述"]);
}

// 3b. 範囲の中への挿入は範囲に含まれる（両端が動かないため）
{
    let s = make("これは口述です", [{ from: 3, to: 5 }]);
    s = s.update({ changes: { from: 4, to: 4, insert: "の記" } }).state;
    eq("中への挿入", texts(s), ["口の記述"]);
}

// 3c. 範囲の直後への挿入は取り込まない（手で書いた続きまで対象にしない）
{
    let s = make("これは口述", [{ from: 3, to: 5 }]);
    s = s.update({ changes: { from: 5, to: 5, insert: "ですね" } }).state;
    eq("直後への挿入は含めない", texts(s), ["口述"]);
}

// 4. 範囲を丸ごと消したら範囲も消える（空範囲を残さない）
{
    let s = make("これは口述です", [{ from: 3, to: 5 }]);
    s = s.update({ changes: { from: 3, to: 5, insert: "" } }).state;
    eq("削除", texts(s), []);
}

// 5. 隣り合うチャンクは1本にまとまる（チャンクごとに増やさない）
{
    const s = make("あいうえお", [
        { from: 0, to: 2 },
        { from: 2, to: 4 },
    ]);
    eq("隣接を併合", texts(s), ["あいうえ"]);
}

// 6. 離れたチャンクは別々のまま
{
    const s = make("あいうえお", [
        { from: 0, to: 2 },
        { from: 3, to: 5 },
    ]);
    eq("離れていれば別", texts(s), ["あい", "えお"]);
}

// 7. 明示的な全消し
{
    let s = make("これは口述です", [{ from: 3, to: 5 }]);
    s = s.update({ effects: clearDictatedEffect.of(null) }).state;
    eq("全消し", texts(s), []);
}

// 8. 範囲の直後に口述が続くと、伸びて1本になる
{
    let s = make("これは口述", [{ from: 3, to: 5 }]);
    s = s.update({ changes: { from: 5, to: 5, insert: "です" } }).state;
    s = s.update({ effects: addDictatedEffect.of({ from: 5, to: 7 }) }).state;
    eq("続けて口述", texts(s), ["口述です"]);
}

// ---- clauseAround: 送る窓の切り出し ----

// 「|」をタップ位置として、切り出される節を返す。
function clause(marked: string): string {
    const rel = marked.indexOf("|");
    const chunk = marked.replace("|", "");
    const r = clauseAround(chunk, rel);
    return chunk.slice(r.from, r.to);
}

function eqClause(marked: string, expected: string): void {
    const got = clause(marked);
    if (got !== expected) {
        failures++;
        console.error(`FAIL clauseAround ${marked}\n  got  ${got}\n  want ${expected}`);
    } else {
        console.log(`ok   clauseAround ${JSON.stringify(marked)}`);
    }
}

// 句点に挟まれた節だけを取る（前後の文は送らない）
eqClause("前の文です。この施策が業|績に寄与しました。次の文です。", "この施策が業績に寄与しました。");
// 読点でも切る（長い一文を丸ごと送らないため）
eqClause("まず前段があり、ここを叩|いた、そして後段。", "ここを叩いた、");
// 区切りが無ければ断片ごと
eqClause("区切りのない文字列を叩|いた", "区切りのない文字列を叩いた");
// 先頭を叩いた
eqClause("|この施策が寄与した。あと", "この施策が寄与した。");
// 末尾を叩いた（区切りが後ろに無い）
eqClause("前です。最後を叩|いた", "最後を叩いた");
// 改行でも切る
eqClause("一行目\n二行目を叩|いた\n三行目", "二行目を叩いた\n");
// 区切り文字そのものを叩いた（空を返さない）
eqClause("これです|。つぎ", "これです。");

if (failures > 0) {
    console.error(`\n${failures} test(s) failed`);
    process.exit(1);
}
console.log("\nall tests passed");
