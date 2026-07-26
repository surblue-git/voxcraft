"""句読点の自動付与（B1: 形態素ルール）。

Whisper（kotoba）は自然発話にほとんど句読点を打たない。ユーザーが句読点を
発話しなくても済むよう、認識後テキストに「。」「、」を自動挿入する。

方針（精度優先＝誤挿入を避ける）:
  - 「。」: 丁寧述語（です/ます 系）や終助詞の直後に、次が内容語（名詞・動詞など＝
    新しい文の始まり）なら文の切れ目とみなして打つ。文末（チャンク末）の述語にも打つ。
  - 「、」: 逆接・理由の接続助詞（が/けど/ので/から/のに）の直後で、次が内容語なら打つ。
  - 句読点が無いと sudachi は文末述語を連体形と誤解析するため、活用形ではなく
    「述語語＋後続が内容語か」で境界判定する。

sudachipy が無い環境では無効（テキストをそのまま返す）。VAD 同様、任意依存で
入っていれば効く（requirements-extra）。
"""
from __future__ import annotations

import threading

# 文の切れ目で「。」を促す丁寧述語（助動詞）。普通形の だ/た は名詞修飾（連体）と
# 紛らわしいので文中挿入からは除外し、文末のみ別扱いにする。
_POLITE_ENDERS = {
    "です", "でした", "でしょう",
    "ます", "ません", "ました", "ませんでした",
}
# 文末（チャンク末）でのみ「。」を許す語（普通形・存在動詞など）。
_END_ENDERS = _POLITE_ENDERS | {
    "だ", "だった", "である", "た", "ない", "なかった",
    "いる", "ある", "する", "ください", "下さい",
}
# 直後に「、」を促す接続助詞（頻出の て/し/ながら/たり は入れない＝打ちすぎ防止）。
_JOIN_PARTICLES = {"が", "けど", "けれど", "けれども", "ので", "のに", "から"}

# 新しい文の始まり（＝境界の後ろ）とみなす品詞大分類。
_STARTER_POS = {"名詞", "代名詞", "副詞", "接続詞", "感動詞", "連体詞", "動詞", "形容詞", "形状詞"}

# 文頭にあれば直後に「、」を打つ接続表現。sudachi が接続詞と品詞付けする語は
# 品詞で拾えるが、つまり/例えば等は副詞扱いになるため表層で補う（文頭限定）。
_LEAD_CONNECTIVES = {
    "つまり", "たとえば", "例えば", "ちなみに", "要するに", "一方",
}

# 既に文末になっている記号（この後ろに「。」を足さない）。
_TERMINALS = set("。！？!?…」）)")

_lock = threading.Lock()
_tokenizer = None
_mode = None
_unavailable = False
_warned = False


def _get_tokenizer():
    """sudachi トークナイザを遅延生成。無ければ None（以後 no-op）。"""
    global _tokenizer, _mode, _unavailable, _warned
    if _tokenizer is not None or _unavailable:
        return _tokenizer
    with _lock:
        if _tokenizer is not None or _unavailable:
            return _tokenizer
        try:
            from sudachipy import dictionary, tokenizer
            _tokenizer = dictionary.Dictionary().create()
            _mode = tokenizer.Tokenizer.SplitMode.C
        except Exception as exc:  # noqa: BLE001
            _unavailable = True
            if not _warned:
                _warned = True
                print(f"[VoxCraft] 自動句読点は無効（sudachipy未導入: {exc}）。"
                      f"requirements-extra を入れると有効になります。")
    return _tokenizer


def available() -> bool:
    return _get_tokenizer() is not None


def _is_starter(pos0: str) -> bool:
    return pos0 in _STARTER_POS


def add_punctuation(text: str) -> str:
    """テキストに「。」「、」を自動挿入して返す（sudachi 未導入ならそのまま）。"""
    t = text.strip()
    if not t:
        return text
    tok = _get_tokenizer()
    if tok is None:
        return text

    morphs = list(tok.tokenize(t, _mode))
    if not morphs:
        return text

    out: list[str] = []
    n = len(morphs)
    for i, m in enumerate(morphs):
        surface = m.surface()
        out.append(surface)
        nxt = morphs[i + 1] if i + 1 < n else None

        # 既に記号ならスキップ。
        if surface and surface[-1] in _TERMINALS or surface in ("、",):
            continue

        pos = m.part_of_speech()
        nxt_pos0 = nxt.part_of_speech()[0] if nxt is not None else None
        nxt_surface = nxt.surface() if nxt is not None else None

        if nxt is not None and nxt_surface not in _TERMINALS and nxt_surface != "、":
            starter = _is_starter(nxt_pos0) or _is_conjunction_start(morphs, i + 1)
            # 「。」: 丁寧述語 or 終助詞 の後ろが新文の始まり。
            is_ender = (pos[0] == "助動詞" and surface in _POLITE_ENDERS) \
                or (pos[0] == "助詞" and pos[1] == "終助詞")
            if is_ender and starter:
                out.append("。")
                continue
            # 「、」: 逆接・理由の接続助詞の後ろが新文の始まり。
            if pos[0] == "助詞" and pos[1] == "接続助詞" and surface in _JOIN_PARTICLES and starter:
                out.append("、")
                continue
            # 「、」: 文頭の接続詞（しかし/また/つまり…）の直後。定番の用法で誤爆が少ない。
            # 文頭 ＝ チャンクの先頭、または「。」等で文が切れた直後に限る。
            if pos[0] == "接続詞" or surface in _LEAD_CONNECTIVES:
                prev_char = out[-2][-1] if len(out) >= 2 and out[-2] else None
                if prev_char is None or prev_char in _TERMINALS:
                    out.append("、")
                    continue

    result = "".join(out)

    # 文末（チャンク末）が述語で終わっていて記号が無ければ「。」を足す。
    if result and result[-1] not in _TERMINALS and result[-1] != "、":
        last = morphs[-1]
        lpos = last.part_of_speech()
        last_is_end = last.surface() in _END_ENDERS \
            or (lpos[0] == "助詞" and lpos[1] == "終助詞") \
            or (lpos[0] in ("動詞", "形容詞", "助動詞") and "終止形" in lpos[5])
        if last_is_end:
            result += "。"

    return result


def _is_conjunction_start(morphs, idx: int) -> bool:
    """sudachi が分割する接続語（でも/しかし等）を新文開始として拾う補助。"""
    s = morphs[idx].surface()
    # 「でも」= で(助動詞)+も、「だが」= だ+が 等、先頭2形態素で判定。
    two = s + (morphs[idx + 1].surface() if idx + 1 < len(morphs) else "")
    return two in ("でも", "だが", "しかし", "ただし", "けれど") or s in (
        "しかし", "ただし", "また", "そして", "それで", "つまり", "一方", "ちなみに",
    )
