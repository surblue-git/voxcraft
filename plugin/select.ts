// カーソル位置から「直したい語」の範囲を推定する。
//
// なぜ要るか: Android の Obsidian では単語のダブルタップ選択がまともに効かない。
// 「ここを言い直し」「ここを再変換」は選択範囲が前提なので、選択できない端末では
// そもそも使えない機能になっていた。カーソルを置くだけで対象が決まれば、
// タップ1回＋ボタン1回で届く。
//
// 日本語には語の区切りが無いので、文字種の切り替わりを語の境界とみなす。
// 形態素解析ほど正確ではないが、この用途では十分に当たる:
//     業績に起用しました → 業績 / に / 起用 / しました
// さらに漢字の直後の送り仮名は同じ語として繋げる（起用しました）。少し広めに
// 取るのは実害が無いどころか有利で、言い直しの発話が長くなるぶん認識が良くなる
// （実測: 「きよ」単独は崩れるが「業績に寄与しました」は完全に取れる）。

const MAX_LEN = 24;      // これを超える範囲は語ではない（丸ごと消される事故を防ぐ）
const OKURIGANA_MAX = 4; // 漢字に繋げる仮名の上限。「しました」まで

type CharClass = "han" | "hiragana" | "katakana" | "latin" | "none";

function classify(ch: string): CharClass {
    if (/[一-鿿㐀-䶿々〇]/u.test(ch)) return "han";
    if (/[ぁ-ゖゝゞ]/u.test(ch)) return "hiragana";
    // 長音符（ー）はカタカナ語の一部として扱う。「スライド」「コーヒー」
    if (/[ァ-ヺーヽヾｦ-ﾟ]/u.test(ch)) return "katakana";
    if (/[0-9A-Za-z０-９Ａ-Ｚａ-ｚ]/u.test(ch)) return "latin";
    return "none"; // 句読点・空白・改行・記号は語に含めない
}

// pos を含む同一文字種の連なりを返す。
function runAt(text: string, pos: number): { from: number; to: number; cls: CharClass } | null {
    const cls = classify(text[pos]);
    if (cls === "none") return null;
    let from = pos;
    while (from > 0 && classify(text[from - 1]) === cls) from -= 1;
    let to = pos + 1;
    while (to < text.length && classify(text[to]) === cls) to += 1;
    return { from, to, cls };
}

/**
 * カーソル位置（pos）にある語の範囲を返す。語が取れなければ null。
 *
 * pos は文字と文字の“あいだ”を指すので、まず左側の文字を見る（口述では
 * カーソルが語の直後にあることが多い）。左が語でなければ右側を見る。
 */
export function wordRangeAt(
    text: string,
    pos: number
): { from: number; to: number } | null {
    if (!text) return null;
    const at = Math.max(0, Math.min(pos, text.length));

    // 左優先。境界に立っているときは直前の語を掴む。
    let run = at > 0 ? runAt(text, at - 1) : null;
    if (!run && at < text.length) run = runAt(text, at);
    if (!run) return null;

    let { from, to } = run;

    if (run.cls === "han") {
        // 送り仮名を巻き込む: 起用 + しました
        let end = to;
        while (end < text.length && classify(text[end]) === "hiragana") end += 1;
        if (end - to <= OKURIGANA_MAX) to = end;
    } else if (run.cls === "hiragana" && to - from <= OKURIGANA_MAX) {
        // 送り仮名の側にカーソルがある場合は、その漢字まで遡る。
        let start = from;
        while (start > 0 && classify(text[start - 1]) === "han") start -= 1;
        if (start < from) from = start;
    }

    if (to - from > MAX_LEN) return null;
    return { from, to };
}
