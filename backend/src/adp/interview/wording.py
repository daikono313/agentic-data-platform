"""発言の言い回しの後処理（#77）。

画面の配置は変わるので、発言では画面の位置や操作に触れない（「上の質問にお答えください」
「下記の選択肢から選んでください」）。プロンプトで指示しても残ることがあるので、そうした文を
本文から取り除く。取り除いた文は記録し、eval でモデルの振る舞いとして数える。

意味のある文を消さないよう、対象は狭く取る:
- 位置の語（上・下・下記・以下など）の直前に漢字・カタカナが無い（「売上の」「以上の」は対象外）
- 指す先は質問・選択肢・ボタン・問いだけ（「以下の項目」「上記の候補」は対象外）
- 回答を求める言い方（〜ください・お願いします・どれ・どちら）がある文だけ
- 表・リスト・見出し・コードブロックの行には触れない。全部消えるときは元の本文を残す

質問（present_choices）を出した発言では、回答を促すだけの締めの一言（「回答をお待ちしています」
「選択肢から選ぶか、自由に書いてください」「選択肢を表示しました」）も取り除く。質問とボタンが
続けて表示されるので不要で、プロンプトで禁止しても別の言い方に置き換わるため（#158）。
本文が全部消えてもよい（質問とボタンだけが表示される）。
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
_END = r"(?:（[^）]*）)?[。！!]?$"
_WAITING = re.compile(
    r"((ご回答|回答|お答え|お返事|ご返事|ご確認)[^。]{0,6}"
    r"(お待ち(しています|します|しております)|待ち(です|ます)|待っています)"
    r"|(質問|選択肢)[^。]{0,6}表示しました)" + _END
)
"""回答を待っている・質問を出したと言うだけの文。業務の説明（「出荷は倉庫の確認待ちです」
「顧客からの回答を待っています」）と分けるため、敬語の形か「回答」に限り、「〜からの」は除く。"""
_FROM_SOMEONE = re.compile(r"から(の)?(ご|お)?(回答|返事|確認|答え)")
_ASKING_VERB = (
    r"(お選び|選んで|選択して|お答え|ご回答|回答して|ご記入|記入して|ご入力|入力して|書いて|お書き)"
    r"(ください|いただいても(大丈夫|構いません)(です)?)" + _END
)
_ASKING = re.compile(_ASKING_VERB)
_ASKING_ONLY = re.compile(r"^" + _ASKING_VERB)
_GENERIC = re.compile(
    r"自由に|選ぶか|近いもの|あてはまるもの|選択肢|あれば|いずれか|どれか"
    r"|か(どうか)?を?、?\s*(上の質問で|ボタンで)?(お選び|選んで|お答え)ください"
)
"""答え方を促す文。答える対象が汎用（「自由に」「選ぶか」「近いもの」「〜があれば」など）か、
質問の言い直し（「〜でよいか、お選びください」）か、対象の無い文（「お選びください」）だけを
対象にする。本文の別の依頼（「列名も書いてください」）や具体的な答え方（「特に無ければ
「今回は不要」を選んでください」）は残す。"""
_ASKING_MAX = 50
_DATA = re.compile(r"[0-9０-９]|\*\*|\|")
_SENTENCE = re.compile(r"[^。！？!?]*(?:[。！？!?]|$)")
_SKIP_LINE = re.compile(r"^\s*(\||[-*+] |\d+[.)] |#|>)")


def is_position_sentence(sentence: str) -> bool:
    """画面の位置を指して回答を求める文か。"""
    return bool(
        POINTER.search(sentence) and _ASK.search(sentence) and not _NEGATION.search(sentence)
    )


def is_closing_sentence(sentence: str) -> bool:
    """回答を促すだけの締めの一言か。"""
    s = sentence.strip()
    # 「」の中で文が切れた後ろ半分や、数字・強調を含む文（区切り無しで前の行の中身と
    # つながったもの）には触れない
    if ("」" in s and "「" not in s) or _DATA.search(s):
        return False
    if _WAITING.search(s):
        return not _FROM_SOMEONE.search(s)
    if len(s) > _ASKING_MAX or "「" in s or not _ASKING.search(s):
        return False
    return bool(_ASKING_ONLY.match(s) or _GENERIC.search(s))


def clean_reply(text: str, *, question: bool = False) -> tuple[str, list[str]]:
    """画面の位置を指す文（question があれば締めの一言も）を取り除く。
    戻り値は（本文, 取り除いた文）。"""
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
            if sentence.strip() and (
                is_position_sentence(sentence) or (question and is_closing_sentence(sentence))
            ):
                removed.append(sentence.strip())
            else:
                kept.append(sentence)
        lines.append("".join(kept).rstrip())
    if not removed:
        return text, []
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not out and not question:  # 質問が無いのに全部消えるなら、発言が空にならないよう残す
        return text, []
    return out, removed
