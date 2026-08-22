// 辞書へ1件登録する前に、「その置換がいま開いているノートを何箇所どう書き換えるか」
// を先に見せるための計算。obsidian に依存しないので単体で試験できる。
//
// なぜ要るか
// ----------
// 辞書は `observed → output` の**無条件置換**で、登録すると以後の認識すべてに効く。
// 危ないのは誤認識を直すつもりで**正しい語を壊す**登録で、実例がこれ:
//
//   要約に手で「マイナカード」と略して書く → それを正解だと思って登録する
//   → 本文に22回正しく出ている「マイナンバーカード」が全部潰れる
//
// 登録時点では気づけず、気づくのは記事を書くとき。だから登録前に件数と実文脈を出す。
//
// サーバー側の意味論に合わせること
// --------------------------------
// 本番の置換は dictionary_registry.ReplacementPlan（全キーを繋いだ正規表現の
// 1回走査）なので、**置換結果は再走査されない**。ここも元テキストを左から
// 走査し、一致したら observed の長さだけ進める。
//
// 既存の登録との最長一致は見ていない。既存により長いキーがあると本番では
// そちらが勝ち、ここでの件数はわずかに多く出る。**多め＝安全側**に倒れるので、
// 危険を見落とす方向には誤らない。

export interface PreviewHit {
    /** 元テキスト上の一致位置。 */
    index: number;
    /** 一致箇所の前後（文脈用に切り出した生テキスト）。 */
    before: string;
    after: string;
    /** 前後が切り詰められたか（UI で … を出すため）。 */
    clippedBefore: boolean;
    clippedAfter: boolean;
}

export interface ReplacementPreview {
    /** 置換される箇所の数。 */
    count: number;
    /** 先頭から limit 件までの文脈。 */
    hits: PreviewHit[];
    /** count が hits より多い（表示しきれていない）。 */
    truncated: boolean;
    /** 正しい表記のほうが元テキストに既に出ている回数。0 なら未出現。 */
    outputAlreadyIn: number;
    /** output が observed を短くしただけ＝要約用の省略の疑い。 */
    looksLikeAbbreviation: boolean;
}

const DEFAULT_LIMIT = 6;
const DEFAULT_CONTEXT = 20;

/** 重なりなしの出現位置を、元テキストを左から1回走査して集める。 */
export function matchPositions(text: string, needle: string): number[] {
    const out: number[] = [];
    if (!text || !needle) return out;
    let from = 0;
    for (;;) {
        const at = text.indexOf(needle, from);
        if (at < 0) return out;
        out.push(at);
        from = at + needle.length;   // 置換結果は再走査しない（本番と同じ）
    }
}

// 部分列というだけでは省略と誤認識を分けられない。認識は1〜2文字を**足す**壊し方を
// よくするので（マイナアプリ→マイナ「ー」アプリ、マイナポータル→マイナ「ップ」ポータル）、
// それを省略と呼ぶと本物の誤認識まで警告してしまう。本物の省略は3文字以上まとめて
// 落ちる（マイナ「ンバー」カード、「デジタル」認証アプリ）。
// server/dictcandidates.py の ABBREVIATION_MIN_DROP と同じ値にすること。
const ABBREVIATION_MIN_DROP = 3;

/**
 * output が observed を縮めただけかを見る（「マイナンバーカード」→「マイナカード」）。
 *
 * 綴りの部分列＋落ちた文字数で判定する。読みの近さは使えない: 実測で省略（0.80）の
 * ほうが本物の誤認識（アジアティックコーナース→エージェンティックコマース 0.64）より
 * 高く出て、両者を分離できなかった。
 */
export function looksLikeAbbreviation(observed: string, output: string): boolean {
    if (!observed || !output) return false;
    if (observed.length - output.length < ABBREVIATION_MIN_DROP) return false;
    // サロゲートペアで添字がずれないよう、コードポイント単位で見る。
    const target = Array.from(output);
    let i = 0;
    for (const ch of observed) {
        if (ch === target[i]) i += 1;
        if (i === target.length) return true;
    }
    return false;
}

export function previewReplacement(
    text: string,
    observed: string,
    output: string,
    options: { limit?: number; context?: number } = {}
): ReplacementPreview {
    const limit = options.limit ?? DEFAULT_LIMIT;
    const context = options.context ?? DEFAULT_CONTEXT;
    const positions = matchPositions(text, observed);
    const hits: PreviewHit[] = [];
    for (const index of positions.slice(0, Math.max(0, limit))) {
        const from = Math.max(0, index - context);
        const to = Math.min(text.length, index + observed.length + context);
        hits.push({
            index,
            before: text.slice(from, index),
            after: text.slice(index + observed.length, to),
            clippedBefore: from > 0,
            clippedAfter: to < text.length,
        });
    }
    return {
        count: positions.length,
        hits,
        truncated: positions.length > hits.length,
        outputAlreadyIn: output ? matchPositions(text, output).length : 0,
        looksLikeAbbreviation: looksLikeAbbreviation(observed, output),
    };
}

// --- 辞書ぜんぶをノートに当てる ---------------------------------------------
//
// 辞書は認識のときにしか効かないので、育てても既にあるノートは直らない。
// あとから当て直すために、サーバーと**同じ意味論**で置換を再現する:
//
//   - 全キーを繋いだ1本の正規表現で、元テキストを左から1回だけ走査する
//   - キーの並びはサーバーが配った順（長い順）をそのまま使う。ここで並べ替えない
//     ＝ 最長一致の責任をサーバー側の1箇所に残す
//   - 置換した結果は再走査しない
//
// 件数は「1回の走査で実際に当たった数」で数える。キーごとに独立して数えると、
// 「デジタル認識アプリ」と「デジタル認識」のように包含関係のある登録を二重に
// 数えてしまい、**実際より多く変わるように見える**。

export interface DictionaryPair {
    observed: string;
    output: string;
}

export interface DictionaryHit {
    index: number;
    observed: string;
    output: string;
}

export interface DictionaryEntryReport {
    observed: string;
    output: string;
    count: number;
    hits: PreviewHit[];
}

export interface DictionaryRun {
    /** 置換後のテキスト。 */
    text: string;
    /** 実際に当たった箇所（元テキスト上の位置つき）。 */
    matches: DictionaryHit[];
    /** 当たったキーごとの内訳（多い順）。 */
    entries: DictionaryEntryReport[];
    /** 変わる箇所の総数。 */
    total: number;
}

function escapeForRegExp(s: string): string {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function runDictionary(
    text: string,
    pairs: DictionaryPair[],
    options: { limit?: number; context?: number } = {}
): DictionaryRun {
    const limit = options.limit ?? DEFAULT_LIMIT;
    const context = options.context ?? DEFAULT_CONTEXT;
    const usable = pairs.filter((p) => p.observed && p.output && p.observed !== p.output);
    if (!text || usable.length === 0) {
        return { text, matches: [], entries: [], total: 0 };
    }
    const values = new Map(usable.map((p) => [p.observed, p.output]));
    const pattern = new RegExp(usable.map((p) => escapeForRegExp(p.observed)).join("|"), "g");

    const matches: DictionaryHit[] = [];
    const out = text.replace(pattern, (found, at: number) => {
        const replacement = values.get(found) ?? found;
        matches.push({ index: at, observed: found, output: replacement });
        return replacement;
    });

    const byObserved = new Map<string, DictionaryHit[]>();
    for (const hit of matches) {
        const list = byObserved.get(hit.observed);
        if (list) list.push(hit);
        else byObserved.set(hit.observed, [hit]);
    }
    const entries: DictionaryEntryReport[] = [];
    for (const [observed, list] of byObserved) {
        entries.push({
            observed,
            output: list[0].output,
            count: list.length,
            hits: list.slice(0, limit).map((hit) => {
                const from = Math.max(0, hit.index - context);
                const to = Math.min(text.length, hit.index + observed.length + context);
                return {
                    index: hit.index,
                    before: text.slice(from, hit.index),
                    after: text.slice(hit.index + observed.length, to),
                    clippedBefore: from > 0,
                    clippedAfter: to < text.length,
                };
            }),
        });
    }
    entries.sort((a, b) => b.count - a.count || a.observed.localeCompare(b.observed));
    return { text: out, matches, entries, total: matches.length };
}
