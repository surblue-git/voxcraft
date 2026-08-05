import { wordRangeAt } from "./select";

let failures = 0;

// text 内の「|」をカーソル位置とみなし、選ばれる範囲を文字列で返す。
function pick(marked: string): string | null {
    const pos = marked.indexOf("|");
    const text = marked.replace("|", "");
    const r = wordRangeAt(text, pos);
    return r ? text.slice(r.from, r.to) : null;
}

function eq(marked: string, expected: string | null): void {
    const got = pick(marked);
    if (got !== expected) {
        failures += 1;
        console.error(`FAIL ${marked} → expected ${JSON.stringify(expected)}, got ${JSON.stringify(got)}`);
    }
}

// 実際に詰まった例。「起用」を直したい。
eq("業績に起|用しました", "起用しました");
eq("業績に起用|しました", "起用しました");
eq("業績|に起用しました", "業績に");
// 送り仮名の側にカーソルがあっても、漢字まで遡る。
eq("業績に起用しま|した", "起用しました");

// 文字種の切り替わりが境界になる。
eq("次のスラ|イドで", "スライド");
eq("コーヒ|ーを飲む", "コーヒー");
eq("バージョン3|を使う", "3");
eq("ID|は42だ", "ID");

// カーソルは文字の“あいだ”。境界では左側の語を掴む（口述では語の直後に立つ）。
eq("スライド|で紹介", "スライド");
eq("|スライドで紹介", "スライド");

// 句読点・空白・改行は語に含めない。
eq("そうです。|次の話", "次の");
eq("そうです|。次の話", "そうです");
eq("行の終わり\n|次の行", "次の");

// 語が取れない位置。
eq("", null);
eq("。|、", null);

// 長すぎる連なりは語ではない（丸ごと置換される事故を防ぐ）。
eq("あ".repeat(30) + "|", null);
// 上限ぎりぎりは通す。
eq("ア".repeat(24) + "|", "ア".repeat(24));

// 送り仮名が長すぎるときは巻き込まない（助詞から先は別の語）。
eq("災|害におけるしえん", "災害");

if (failures > 0) {
    console.error(`\n${failures} 件失敗`);
    process.exit(1);
}
console.log("select.test.ts: すべて通過");
