// 口述の差し込み位置（アンカー）を CodeMirror6 の状態として保持する拡張。
//
// アンカー追記式のキモ:
//   - 喋った文はカーソルではなく「固定アンカー」に追記され続ける。
//   - ユーザーが手でカーソルを動かして過去テキストを編集しても、
//     アンカーは文書変更に自動追従する（mapPos）ためズレない。
//   - アンカー位置はマーカー装飾（.voxcraft-anchor）で可視化する。

import { StateEffect, StateField, Extension } from "@codemirror/state";
import { Decoration, DecorationSet, EditorView, WidgetType } from "@codemirror/view";

// アンカー位置（オフセット）を設定/解除するエフェクト。null で解除。
export const setAnchorEffect = StateEffect.define<number | null>();

// アンカー位置に置く小さなマーカー。
class AnchorWidget extends WidgetType {
    eq(): boolean {
        // 位置以外に状態を持たないので常に等価（再描画は range 側で決まる）。
        return true;
    }
    toDOM(): HTMLElement {
        const el = document.createElement("span");
        el.className = "voxcraft-anchor";
        el.setAttribute("aria-hidden", "true");
        return el;
    }
    ignoreEvent(): boolean {
        return true;
    }
}

// アンカーのオフセットを保持し、あらゆる文書変更に追従する StateField。
const anchorField = StateField.define<number | null>({
    create() {
        return null;
    },
    update(value, tr) {
        // 明示的な設定/解除を最優先で反映。
        for (const e of tr.effects) {
            if (e.is(setAnchorEffect)) return e.value;
        }
        // 文書変更へ追従。assoc=1 で「その位置への挿入は左側に留める」＝
        // アンカー地点に差し込んだ本文の後ろへアンカーが前進する。
        if (value !== null && tr.docChanged) {
            return tr.changes.mapPos(value, 1);
        }
        return value;
    },
});

// アンカー位置にマーカー装飾を1つ出す。
const anchorDecorations = EditorView.decorations.compute([anchorField], (state): DecorationSet => {
    const pos = state.field(anchorField, false);
    if (pos === null || pos === undefined) return Decoration.none;
    const at = Math.max(0, Math.min(pos, state.doc.length));
    return Decoration.set([
        Decoration.widget({ widget: new AnchorWidget(), side: 1 }).range(at),
    ]);
});

// プラグインから registerEditorExtension() に渡す拡張一式。
export const anchorExtension: Extension = [anchorField, anchorDecorations];

// ---- ヘルパー（main.ts から使う） ----

export function setAnchor(cm: EditorView, pos: number): void {
    cm.dispatch({ effects: setAnchorEffect.of(pos) });
}

export function clearAnchor(cm: EditorView): void {
    cm.dispatch({ effects: setAnchorEffect.of(null) });
}

export function getAnchor(cm: EditorView): number | null {
    const v = cm.state.field(anchorField, false);
    return v === undefined ? null : v;
}
