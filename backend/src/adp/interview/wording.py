"""発言の言い回しの後処理（#77）。

画面の配置は変わるので、発言では画面の位置や操作に触れない（「上の質問にお答えください」
「下記の選択肢から選んでください」）。プロンプトで指示しても残ることがあるので、そうした文を
本文から取り除く。取り除いた文は記録し、eval でモデルの振る舞いとして数える。

意味のある文を消さないよう、対象は狭く取る:
- 位置の語（上・下・下記・以下など）の直前に漢字・カタカナが無い（「売上の」「以上の」は対象外）
- 指す先は質問・選択肢・ボタン・問いだけ（「以下の項目」「上記の候補」は対象外）
- 回答を求める言い方（〜ください・お願いします・どれ・どちら）がある文だけ
- 表・リスト・見出し・コードブロックの行には触れない。全部消えるときは元の本文を残す
"""

from __future__ import annotations

import re

_NOT_WORD = r"(?<![一-龥ァ-ヶー々])"
_TARGET = r"(質問|選択肢|ボタン|問い)"
POINTER = re.compile(
    _NOT_WORD
    + r"(上|下|上記|下記|以下|前記)(の|に(表示|出て|ある)\S{0,4}?)"
    + _TARGET
    + r"|(?<!」)(ボタン|選択肢)から"
)
"""画面の位置を指す言い方（「確定」ボタンからの案内は除く）。"""
_ASK = re.compile(r"ください|下さい|お願いします|お願いいたします|どれ|どちら|いずれか")
_NEGATION = re.compile(r"必要はありません|なくても|不要です")
_SENTENCE = re.compile(r"[^。！？!?]*(?:[。！？!?]|$)")
_SKIP_LINE = re.compile(r"^\s*(\||[-*+] |\d+[.)] |#|>)")


def is_position_sentence(sentence: str) -> bool:
    """画面の位置を指して回答を求める文か。"""
    return bool(
        POINTER.search(sentence) and _ASK.search(sentence) and not _NEGATION.search(sentence)
    )


def strip_position_sentences(text: str) -> tuple[str, list[str]]:
    """画面の位置を指す文を取り除く。戻り値は（本文, 取り除いた文）。"""
    removed: list[str] = []
    lines: list[str] = []
    in_code = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        if in_code or line.lstrip().startswith("```") or _SKIP_LINE.match(line):
            lines.append(line)
            continue
        kept = []
        for sentence in _SENTENCE.findall(line):
            if sentence.strip() and is_position_sentence(sentence):
                removed.append(sentence.strip())
            else:
                kept.append(sentence)
        lines.append("".join(kept).rstrip())
    if not removed:
        return text, []
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not out:  # 全部消えるなら、発言が空にならないよう元のまま残す
        return text, []
    return out, removed
