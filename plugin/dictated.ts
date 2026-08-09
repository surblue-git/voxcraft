// 口述で入れたテキストの範囲を CodeMirror6 の状態として保持する拡張。
//
// 何のためか:
//   認識ミス（特に同音異義語）を直す入口を「その語をタップする」だけにしたい。
//   そのためには「どこが口述で入った文字か」を知っている必要がある。手で打った
//   箇所まで対象にすると、普通のカーソル移動ができなくなる。
//
// なぜ「未確定→確定」にしないか:
//   IME のような確定状態にすると締切ができる。ところが誤変換に気づくのは
//   たいてい数文あとで、そのときには確定済みになっている。締切を設けても
//   直せる力は増えないので、**このセッションで口述した範囲はいつでも対象**にする。
//
// なぜ確信度で絞らないか:
//   単語ごとの確信度は無料で取れるが、実測（2026-08-09）で拾えるのは
//   アライメントの不確かさであって意味の誤りではなかった。近接マイクでも
//   毎分6語に印が付き、その大半が誤りでない語になる。誤りを見つけるのは
//   人間の目のほうが正確なので、機械は範囲を覚えるところまでにする。
//
// 範囲は文書変更に追従する（mapPos）。手で書き換えた箇所は範囲から外れる。

import { EditorState, StateEffect, StateField, Extension, RangeSet } from "@codemirror/state";
import { Decoration, DecorationSet, EditorView } from "@codemirror/view";

export interface DictatedRange {
    from: number;
    to: number;
}

// 口述チャンクを1つ足す。
export const addDictatedEffect = StateEffect.define<DictatedRange>();
// セッション開始・終了などで全部捨てる。
export const clearDictatedEffect = StateEffect.define<null>();

// 隣り合う（または重なる）範囲は1本にまとめる。チャンクごとに足すと
// 数百本になり、走査もデコレーションも無駄に重くなるため。
function merge(ranges: DictatedRange[]): DictatedRange[] {
    if (ranges.length <= 1) return ranges;
    const sorted = [...ranges].sort((a, b) => a.from - b.from);
    const out: DictatedRange[] = [sorted[0]];
    for (const r of sorted.slice(1)) {
        const last = out[out.length - 1];
        if (r.from <= last.to) last.to = Math.max(last.to, r.to);
        else out.push({ ...r });
    }
    return out;
}

const dictatedField = StateField.define<DictatedRange[]>({
    create() {
        return [];
    },
    update(value, tr) {
        let next = value;
        for (const e of tr.effects) {
            if (e.is(clearDictatedEffect)) return [];
            if (e.is(addDictatedEffect)) next = [...next, e.value];
        }
        if (tr.docChanged) {
            // assoc: 開始は右へ（-1 だと直前への挿入を巻き込む）、終端は左へ
            // （+1 だと直後への挿入を飲み込む）。
            //
            // 範囲の中を手で書き換えても範囲は残る（両端は動かないため）。これは
            // 意図した挙動で、周囲は口述のままだから。「直した語だけ対象外にする」
            // ことに実益は無く、追跡を細かくする手間だけが増える。
            // 丸ごと消したときは to <= from になって落ちる。
            next = next
                .map((r) => ({
                    from: tr.changes.mapPos(r.from, 1),
                    to: tr.changes.mapPos(r.to, -1),
                }))
                .filter((r) => r.to > r.from);
        }
        return next === value ? value : merge(next);
    },
});

// 口述で入った範囲に控えめな下線を引く。「ここは叩ける」という手がかりで、
// 「ここが怪しい」という意味ではない（怪しさは機械には判定できない）。
const dictatedDecorations = EditorView.decorations.compute(
    [dictatedField],
    (state): DecorationSet => {
        const ranges = state.field(dictatedField, false) ?? [];
        const len = state.doc.length;
        const marks = ranges
            .map((r) => ({ from: Math.max(0, r.from), to: Math.min(r.to, len) }))
            .filter((r) => r.to > r.from)
            .map((r) => Decoration.mark({ class: "voxcraft-dictated" }).range(r.from, r.to));
        return marks.length ? RangeSet.of(marks, true) : Decoration.none;
    }
);

// ---- 言い直し待ちの対象 ----
//
// 「次の発話でここを置き換える」状態を本文の上で見せる。ステータスバーにも
// 文言は出しているが、モバイルではツールバーの下敷きになって気づけない。
// **何が置き換わるか**は本文を見て分かるべきなので、対象そのものを光らせる。

export const setRespeakTargetEffect = StateEffect.define<DictatedRange | null>();

const respeakField = StateField.define<DictatedRange | null>({
    create() {
        return null;
    },
    update(value, tr) {
        for (const e of tr.effects) {
            if (e.is(setRespeakTargetEffect)) return e.value;
        }
        if (value && tr.docChanged) {
            const from = tr.changes.mapPos(value.from, 1);
            const to = tr.changes.mapPos(value.to, -1);
            return to > from ? { from, to } : null;
        }
        return value;
    },
});

const respeakDecorations = EditorView.decorations.compute(
    [respeakField],
    (state): DecorationSet => {
        const r = state.field(respeakField, false);
        if (!r) return Decoration.none;
        const from = Math.max(0, r.from);
        const to = Math.min(r.to, state.doc.length);
        if (to <= from) return Decoration.none;
        return Decoration.set([
            Decoration.mark({ class: "voxcraft-respeak-target" }).range(from, to),
        ]);
    }
);

export const dictatedExtension: Extension = [
    dictatedField,
    dictatedDecorations,
    respeakField,
    respeakDecorations,
];

export function setRespeakTarget(cm: EditorView, range: DictatedRange | null): void {
    cm.dispatch({ effects: setRespeakTargetEffect.of(range) });
}

export function respeakTargetIn(state: EditorState): DictatedRange | null {
    return state.field(respeakField, false) ?? null;
}

// ---- ヘルパー（main.ts から使う） ----

export function markDictated(cm: EditorView, from: number, to: number): void {
    if (to <= from) return;
    cm.dispatch({ effects: addDictatedEffect.of({ from, to }) });
}

export function clearDictated(cm: EditorView): void {
    cm.dispatch({ effects: clearDictatedEffect.of(null) });
}

// State から直接読む版。EditorView を作れないテストからも使える。
export function dictatedRangesIn(state: EditorState): DictatedRange[] {
    return state.field(dictatedField, false) ?? [];
}

export function getDictatedRanges(cm: EditorView): DictatedRange[] {
    return dictatedRangesIn(cm.state);
}

// タップ位置の前後、これだけの文字数を上限として切り出す。
// 読みは表層より1.3〜1.4倍長い（実測: 47字→74字, 43→60, 50→67）ので、
// 前後40字＝最大80字なら読みは110字前後。サーバー側が53字ずつに割って
// 並列に投げるため、この長さでも待たされない。
export const TAP_WINDOW_MAX = 40;

// 文の切れ目。ここで切ると Google CGI の文節分割が素直になる。
const CLAUSE_BREAK = /[。．.！!？?、，,\n]/;

// 切り出した断片 chunk の中で、rel（タップ位置）を含む「節」の範囲を返す。
//
// 文全体ではなく節で切るのは、変換の精度と応答の両方に効くため。長すぎると
// 分割回数が増え、短すぎると文脈が足りず変換がぶれる。
export function clauseAround(chunk: string, rel: number): { from: number; to: number } {
    let from = 0;
    for (let i = Math.min(rel, chunk.length) - 1; i >= 0; i--) {
        if (CLAUSE_BREAK.test(chunk[i])) {
            from = i + 1;
            break;
        }
    }
    let to = chunk.length;
    for (let i = Math.max(rel, 0); i < chunk.length; i++) {
        if (CLAUSE_BREAK.test(chunk[i])) {
            // 区切り文字そのものは含める（「。」だけ取り残さない）。
            to = i + 1;
            break;
        }
    }
    // 区切りが密で潰れた場合は、切らずに断片ごと使う（空を返さない）。
    return to > from ? { from, to } : { from: 0, to: chunk.length };
}

// pos を含む口述範囲（無ければ null）。タップがこの中かどうかの判定に使う。
export function dictatedRangeAt(cm: EditorView, pos: number): DictatedRange | null {
    for (const r of getDictatedRanges(cm)) {
        if (r.from <= pos && pos < r.to) return r;
    }
    return null;
}
