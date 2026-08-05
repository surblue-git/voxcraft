import { SYMBOL_CHOICES, symbolChoicesFor } from "./symbols";

function values(choices: { value: string }[]): string[] {
    return choices.map((c) => c.value);
}

function assert(condition: boolean, name: string): void {
    if (!condition) throw new Error(`FAIL ${name}`);
}

// 本題: 「まる」と言って『悪』になった1文字を直す場面で記号が並ぶ。
const forWaru = symbolChoicesFor("悪", 1, ["悪", "割る", "破る"]);
assert(values(forWaru).includes("。"), "「。」が候補に入る");
assert(values(forWaru).includes("\n"), "改行が候補に入る");
assert(values(forWaru).length === SYMBOL_CHOICES.length, "既存候補と重ならなければ全部出す");

// 文章の再変換では出さない（本来の候補の番号がずれるだけ）。
assert(
    symbolChoicesFor("この文章はとても長いので記号ではない", 1).length === 0,
    "長い対象には出さない",
);
assert(symbolChoicesFor("悪", 3).length === 0, "複数文節には出さない");

// 既に記号なら直す必要がない。
assert(symbolChoicesFor("。", 1).length === 0, "対象が記号そのものなら出さない");

// 変換候補と重複したものは落とす（同じ字面が2回並ばない）。
assert(
    !values(symbolChoicesFor("てん", 1, ["点", "、"])).includes("、"),
    "既存候補と重複する記号は落とす",
);

// 改行だけ、本文へ入れる値と辞書へ入れる値が違う。
const newline = SYMBOL_CHOICES.find((c) => c.value === "\n");
assert(newline?.store === "改行", "改行は辞書に「改行」で入る");
assert(
    SYMBOL_CHOICES.every((c) => c.value === "\n" || c.store === c.value),
    "改行以外は本文と辞書で同じ値",
);

console.log("symbols.test.ts: all passed");
