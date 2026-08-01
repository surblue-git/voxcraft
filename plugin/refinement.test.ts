import { preserveParagraphBreaks } from "./refinement";

function equal(actual: string, expected: string): void {
    if (actual !== expected) {
        throw new Error(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
    }
}

equal(
    preserveParagraphBreaks("新しい第一文。新しい第二文。", "古い第一文。\n\n古い第二文。"),
    "新しい第一文。\n\n新しい第二文。",
);
equal(
    preserveParagraphBreaks("補正後の冒頭です。", "\n\n速報の冒頭です。"),
    "\n\n補正後の冒頭です。",
);
equal(
    preserveParagraphBreaks("第一です。第二です。第三です。", "甲です。\n\n乙です。\n\n丙です。"),
    "第一です。\n\n第二です。\n\n第三です。",
);
equal(
    preserveParagraphBreaks("改行のない補正稿です。", "改行のない速報稿です。"),
    "改行のない補正稿です。",
);

console.log("paragraph refinement tests passed");
