// 記号読み上げ（「まる」→「。」）の候補セット。
//
// なぜ再変換の候補に混ぜるのか:
//   記号語の誤認識は読み自体が変わる（「まる」→『悪』＝わる）ため、読みを経由する
//   再変換では絶対に「。」が候補に出てこない。言い直しても調音が同じなので再現する。
//   一方サーバー側は、チャンク全体が登録済みの綴りと一致したときだけ記号へ変換する
//   （server/postproc.py の apply_symbol_dictation）ので、観測した綴りを symbols へ
//   入れさえすれば二度と出ない。その登録導線が今まで辞書画面にしか無かった。
//
// value/store が別なのは改行だけ。本文へは "\n" を入れるが、辞書には人が読める
// 「改行」で持つ（server/userdict.py の _NEWLINE_ALIASES が "\n" へ戻す）。

export interface SymbolChoice {
    value: string;   // 本文へ差し込む文字
    store: string;   // symbols 辞書へ登録する値
    label: string;   // モーダルの表示（記号だけでは見分けにくいので読みを添える）
}

// server/postproc.py の _STANDALONE / _ENDERS が出力する記号と対。
// 口述で使う頻度の高い順（番号キー・音声「N番」で早い番号に載るように）。
export const SYMBOL_CHOICES: readonly SymbolChoice[] = [
    { value: "。", store: "。", label: "。 まる" },
    { value: "、", store: "、", label: "、 てん" },
    { value: "\n", store: "改行", label: "改行 かいぎょう" },
    { value: "「", store: "「", label: "「 かぎかっこ" },
    { value: "」", store: "」", label: "」 かぎかっことじ" },
    { value: "（", store: "（", label: "（ かっこ" },
    { value: "）", store: "）", label: "） かっことじ" },
    { value: "？", store: "？", label: "？ はてなまーく" },
    { value: "！", store: "！", label: "！ びっくりまーく" },
    { value: "・", store: "・", label: "・ なかぐろ" },
    { value: "…", store: "…", label: "… さんてん" },
    { value: "：", store: "：", label: "： ころん" },
    { value: "／", store: "／", label: "／ すらっしゅ" },
];

// 記号語として言ったのに語になった、とみなせる長さの上限。
// 記号の読みは「かぎかっことじ」でも1チャンクの短い発話なので、長い選択範囲に
// 記号候補を出しても邪魔になるだけ。
const MAX_TARGET_LEN = 6;

/**
 * 再変換の候補末尾に足す記号候補を返す。
 *
 * 出すのは「短い単一文節」のときだけ。文章の再変換に記号を並べても選ばれないし、
 * 番号がずれて本来の候補が選びにくくなる。
 */
export function symbolChoicesFor(
    originalText: string,
    segmentCount: number,
    existingCandidates: readonly string[] = [],
): SymbolChoice[] {
    const target = originalText.trim();
    if (segmentCount !== 1) return [];
    if (!target || target.length > MAX_TARGET_LEN) return [];
    // 既に記号そのものなら直す必要がない。
    if (SYMBOL_CHOICES.some((choice) => choice.value === target)) return [];

    const taken = new Set(existingCandidates);
    return SYMBOL_CHOICES.filter((choice) => !taken.has(choice.value));
}
