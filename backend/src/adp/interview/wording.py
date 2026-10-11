"""発言の言い回しの後処理（#77）。

画面の配置は変わるので、発言では画面の位置や操作に触れない（「上の質問にお答えください」
「下記の選択肢から選んでください」）。プロンプトで指示しても残ることがあるので、そうした文を
本文から取り除く。取り除いた文は記録し、eval でモデルの振る舞いとして数える。
"""

from __future__ import annotations

import re

# 「確定」ボタンを押す案内や「画面上のボタン」の説明は対象外
POSITION = re.compile(
    r"(?<!画面)(上|下|上記|下記|以下|前記)(の|に(表示|出て|ある)\S{0,4}?)"
    r"(質問|選択肢|ボタン|確認|項目|候補|問い)"
    r"|(?<!」)ボタンから|選択肢から(選|お選)"
)
_SENTENCE = re.compile(r"[^。！？!?\n]*(?:[。！？!?]|\n|$)")


def strip_position_sentences(text: str) -> tuple[str, list[str]]:
    """画面の位置を指す文を取り除く。戻り値は（本文, 取り除いた文）。"""
    kept: list[str] = []
    removed: list[str] = []
    for sentence in _SENTENCE.findall(text):
        if POSITION.search(sentence):
            removed.append(sentence.strip())
        else:
            kept.append(sentence)
    if not removed:
        return text, []
    out = "".join(kept)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out, removed
