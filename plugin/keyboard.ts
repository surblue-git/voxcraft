// モバイルのソフトキーボード抑制（口述中のみ）。
//
// Android では画面を触るたびにキーボードが出て、画面下部の口述ツールバーを隠す。
// 口述の入力は CM のトランザクションで行うのでキーボードは要らない。そこで
// エディタの contenteditable に inputmode="none" を付け、キーボードだけを止める。
// フォーカス・カーソル・タップでのカーソル移動・範囲選択はそのまま生きるので、
// 「言い直し」「再変換」のための選択操作は普通にできる。
//
// キーボードが要るときはツールバーの⌨ボタンで一時解除する（勝手に出ないだけで、
// 出せなくなるわけではない）。

import { Extension, StateEffect, StateField } from "@codemirror/state";
import { EditorView } from "@codemirror/view";

const setSuppressEffect = StateEffect.define<boolean>();

const suppressField = StateField.define<boolean>({
    create() {
        return false;
    },
    update(value, tr) {
        for (const e of tr.effects) {
            if (e.is(setSuppressEffect)) return e.value;
        }
        return value;
    },
});

// contentDOM の属性は CM が毎更新で作り直すため、直接 setAttribute せず
// facet 経由で持たせる（そうしないと再描画のたびに剥がれる）。
const suppressAttrs = EditorView.contentAttributes.compute(
    [suppressField],
    (state): Record<string, string> =>
        state.field(suppressField, false) ? { inputmode: "none" } : {}
);

export const keyboardExtension: Extension = [suppressField, suppressAttrs];

export function isKeyboardSuppressed(cm: EditorView): boolean {
    return cm.state.field(suppressField, false) === true;
}

// 抑制の切り替え。属性を変えるだけでは「今出ているキーボード」は閉じず、
// 解除しても出てこないので、フォーカスを付け直して OS に判定させ直す。
//
// refocus=false は「次にタップしたときから効けばいい」場合に使う（録音停止時の
// 自動解除など。ここで付け直すと、頼んでいないのにキーボードが開いてしまう）。
// 付け直しはクリックハンドラと同じタスク内で同期的に行うこと。キーボードを出す
// 方向はユーザー操作の文脈でないと Android が focus() を無視するため、
// setTimeout で遅らせてはいけない。
export function setKeyboardSuppressed(cm: EditorView, on: boolean, refocus = true): void {
    if (!cm.dom.isConnected) return;
    if (isKeyboardSuppressed(cm) === on) return;
    cm.dispatch({ effects: setSuppressEffect.of(on) });
    if (!refocus) return;
    cm.contentDOM.blur();
    cm.focus();
}
