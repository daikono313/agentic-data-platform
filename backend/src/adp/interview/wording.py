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

直前の段落（か段落のまとまり）と一字一句同じ地の文の段落（tool 呼び出しの前後でモデルが前置きを
書き直したもの）も取り除く（#161）。離れた場所の同じ段落・少しでも違うもの・表・リスト・コードは
残す（文の流れや見せた内容を失わないため。#175）。

英語だけの地の文（「Now the metrics.」「Fix: use expr only.」など、tool 呼び出しの合間のモデルの
作業メモ）も取り除く（#161）。日本語を含まず、英単語が 3 つ以上あり、作業メモに特有の語
（now・next・fix・retry・add など）を含む文だけを対象にする。商品名・値に普通に出る語（of・and・on）
では判定しない。ユーザーへの英語の質問（? で終わる文）は消さない。

確認の質問は、確かめてもらう中身がユーザーに見えているときだけ出せる（`hidden_confirmation`）。
具体例を記録してよいかの確認には値（日付・数量など。#163）、言い換えの確認には言い換えた問い
（「〜はいくつか」。#165）が要る。tool の結果や記録した内容はユーザーに見えないので、中身を書かずに
「この 2 件で」「この言い換えで」と聞いても確かめられない。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator

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
    r"|^確認をお待ち(しています|しております)"
    r"|(言い換え|理解|内容|問い|これ|それ)で合って[^。]{0,15}確認(中です|しています)"
    r"|(質問|選択肢)[^。]{0,6}(表示しました|確認しています))" + _END
)
"""回答を待っている・質問を出した・確かめている最中だと言うだけの文（「〜で合っているかを、
選択肢で確認しています」。#170）。業務の説明（「出荷は倉庫の確認待ちです」「顧客からの回答を
待っています」「帳簿と実在庫が合っているかを確認しています」「取引先の確認をお待ちしています」）と
分けるため、敬語の形か「回答」に限り、「〜からの」は除く。確認の状況は「言い換え・理解・内容で
合っているか」を確かめている形と、文頭の「確認をお待ちしています」だけ。"""
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
_SENTENCE = re.compile(r"(?:「[^」\n]*」|[^。！？!?])*(?:[。！？!?]|$)")
"""文。「」の中の句点・疑問符では切らない（切ると「」の閉じない断片が残る。#170）。"""
_QUOTED_QUESTION = re.compile(r"「([^」]*?)[？?]?」")
"""引用した部分。問い（「か」で終わる引用）を引用した文は、それが質問文に無ければ、位置の言葉や
締めの言い方があっても取り除かない（中身がユーザーに見えなくなるため。#170）。数えられる問いか
（_REFRAMED）は問わない: こちらは文を消してよいかの判定で、言い換えの確認に限らないため。"""
_STRUCTURE_LINE = re.compile(r"^\s*(\||[-*+] |\d+[.)] |#|>|```)")


def is_structure_line(line: str) -> bool:
    """表・リスト・見出し・引用・コードの区切りの行か（文の単位で手を入れない）。"""
    return bool(_STRUCTURE_LINE.match(line))


_EXAMPLE = r"(?<!事)例(?!外|え)[^。？?]{0,20}"
_EXAMPLE_CONFIRM = re.compile(
    _EXAMPLE + r"((記録して|使って|残して|にして|で)(よい|良い|いい|よろしい)|合って)"
)
"""具体例を記録してよいかの確認。「例外」「例えば」「事例」は除く。"""
_EXAMPLE_VALUE = re.compile(
    r"\d{4}\s*[-/年]\s*\d{1,2}|\d+\s*月\s*\d+\s*日|\d{1,2}:\d{2}|\d[\d,.]*\s*%"
    r"|\d[\d,.]*\s*(個|円|点|日|人|本|枚|回)|ID\s*[:：]?\s*\d"
)
"""具体例の値（日時・数量・金額・比率・ID）。「2 件」のような件数は値に数えない。"""
_REFRAME_CONFIRM = re.compile(
    r"言い換え[^？?]{0,40}?(合って|よい|良い|いい|よろしい)[^。？?\n]*[。？?]?"
)
"""言い換えた問いで合っているかの確認（「言い換えました。合っていますか？」も含む）。"""
_REFRAME_BARE = re.compile(
    r"(この|上の|次の|今の|先ほどの)?言い換え(は|で|が)?(これで)?(合って|よい|良い|いい|よろしい)"
    r"[^。？?\n]*[。？?]?"
)
"""中身を含まない確認の言い方（「この言い換えで合っていますか？」）。"""
_REFRAMED = re.compile(
    r"(いくつ|いくら|何[件人個日回円%]|どれ(だけ|くらい)|どのくらい|割合|比率|\S率|平均|合計|総額"
    r"|件数|数量|人数|個数|金額|日数|\S数)[^。\n？?]{0,25}(か|？|\?)|[÷／]"
)
"""言い換えた問い（数えられる問い。「月ごとの売上金額はいくらか」「返品数 ÷ 販売数」）。"""


def is_position_sentence(sentence: str) -> bool:
    """画面の位置を指して回答を求める文か。"""
    return bool(
        POINTER.search(sentence) and _ASK.search(sentence) and not _NEGATION.search(sentence)
    )


_JAPANESE = re.compile(r"[ぁ-んァ-ヶ一-龥々]")
_ENGLISH_WORD = re.compile(r"^[(`]?[A-Za-z][a-z]*(?:'[a-z]+)?[.,!?:;)`]*$")
_WORK_NOTE = re.compile(
    r"\b(now|next|let me|i'll|fix|fixing|retry|add|adding|need|needs|remove|move|same|record)\b",
    re.IGNORECASE,
)


def is_english_sentence(sentence: str) -> bool:
    """日本語を含まない英語の地の文（モデルの作業メモ）か（#161）。"""
    s = sentence.strip()
    if not s or _JAPANESE.search(s) or s.endswith(("?", "？")):
        return False
    words = [w for w in s.split() if _ENGLISH_WORD.match(w)]
    return len(words) >= 3 and bool(_WORK_NOTE.search(s))


def english_sentences(text: str) -> list[str]:
    """地の文（表・リスト・見出し・引用・コードの行を除く）のうち、英語の作業メモの文。"""
    return [
        x
        for line, prose in _lines(text)
        if prose
        for x in sentences(line)
        if is_english_sentence(x)
    ]


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


def is_example_confirmation(text: str) -> bool:
    """具体例を記録してよいかの確認か。"""
    return bool(_EXAMPLE_CONFIRM.search(text))


def has_example_values(text: str) -> bool:
    """具体例の値（日時・数量・金額・比率・ID）が書かれているか。"""
    return bool(_EXAMPLE_VALUE.search(text))


CONFIRMED = {
    "examples": "具体例の値（日時・場所・商品・数量など）",
    "reframe": "言い換えた問い（「月ごとの売上金額はいくらか」など。記録した framed_question）",
}
"""確認の質問で、ユーザーに見えている必要がある中身（hidden_confirmation の戻り値と、その説明）。"""


def hidden_confirmation(question: str, assistant: str, user: str = "") -> str | None:
    """確認の質問で、確かめてもらう中身がユーザーに見えていなければ、その種類（CONFIRMED のキー）。

    assistant はアシスタントが書いてユーザーに見えている直近の文（直前の発言と今の発言の本文）、
    user はその間のユーザーの発言。具体例はユーザーが自分で書いた値でもよいが、言い換えた問いは
    アシスタントが書いたものだけを見る（ユーザーの要求文の「どれだけ」「何日」で通さない）。
    言い換えの確認の後ろに続く別の質問（「〜で合っていますか？分母は何にしますか？」）も見ない。"""
    if is_example_confirmation(question):
        if not has_example_values("\n".join([question, assistant, user])):
            return "examples"
        return None
    confirm = _REFRAME_CONFIRM.search(question)
    if confirm:
        own = _REFRAME_BARE.sub("", question[: confirm.end()] + "\n" + assistant)
        if not _REFRAMED.search(own):
            return "reframe"
    return None


def _quotes_hidden_question(sentence: str, question: str) -> bool:
    """質問文に無い問いを引用しているか。"""
    shown = normalize(question)
    return any(
        q.endswith("か") and normalize(q) not in shown
        for q in (m.group(1).strip() for m in _QUOTED_QUESTION.finditer(sentence))
    )


def _drop_repeated_paragraphs(text: str) -> tuple[str, list[str]]:
    """直前の段落（か、直前に続く段落のまとまり。A B A B の後ろの A B）と一字一句同じ地の文の段落を
    取り除く（#161）。tool 呼び出しの前後でモデルが同じ前置きを書き直したもの。離れた場所の同じ
    段落・少しでも違う段落・8 文字未満の段落・表・リスト・コードは残す（文の流れや見せた内容を
    失わないため。#175）。戻り値は（本文, 取り除いた段落）。"""
    if "```" in text or "~~~" in text:
        return text, []
    paragraphs = re.split(r"\n{2,}", text)
    keys = [p.strip() for p in paragraphs]

    def droppable(i: int) -> bool:
        lines = paragraphs[i].strip("\n").split("\n")  # 頭の空白（インデントのコード）は残す
        return len(keys[i]) >= 8 and not any(
            is_structure_line(line) or line.startswith(("    ", "\t")) for line in lines
        )

    kept: list[int] = []
    removed: list[str] = []
    for i in range(len(keys)):
        kept.append(i)
        # 直前に続く k 個の段落と、その前の k 個が同じなら、後ろの k 個を取り除く
        for k in range(len(kept) // 2, 0, -1):
            tail, before = kept[-k:], kept[-2 * k : -k]
            same = [keys[t] == keys[b] for t, b in zip(tail, before, strict=True)]
            if all(same) and all(droppable(t) for t in tail):
                removed += [keys[t] for t in tail]
                del kept[-k:]
                break
    if not removed:
        return text, []
    return "\n\n".join(paragraphs[i] for i in kept), removed


def clean_reply(text: str, *, question: str | None = None) -> tuple[str, list[str]]:
    """画面の位置を指す文・英語の作業メモ（質問文 question があれば締めの一言も）を取り除き、
    そのあとで直前と一字一句同じ段落を取り除く。戻り値は（本文, 取り除いた文・段落）。"""
    removed: list[str] = []
    lines: list[str] = []
    for line, prose in _lines(text):
        if not prose:
            lines.append(line)
            continue
        kept = []
        for sentence in _SENTENCE.findall(line):
            if (
                sentence.strip()
                and not _quotes_hidden_question(sentence, question or "")
                and (
                    is_position_sentence(sentence)
                    or is_english_sentence(sentence)
                    or (question and is_closing_sentence(sentence))
                )
            ):
                removed.append(sentence.strip())
            else:
                kept.append(sentence)
        lines.append("".join(kept).rstrip())
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() if removed else text
    if not out and not question:  # 質問が無いのに全部消えるなら、発言が空にならないよう残す
        out, removed = text, []
    out, repeated = _drop_repeated_paragraphs(out)
    return out, removed + repeated


_PUNCT = re.compile(r"[\s、。!！?？「」『』（）()*`]|[.,](?!\d)")
"""比べるときに無視する文字。小数点・桁区切り（数字の前の . ,）は残す（1.5 と 15 を分ける）。"""


def normalize(text: str) -> str:
    """同じ文かを比べるための形（全角・半角、空白・句読点・括弧・強調の違いを無視する）。"""
    return _PUNCT.sub("", unicodedata.normalize("NFKC", text))


def _lines(text: str) -> Iterator[tuple[str, bool]]:
    """行と、地の文の行か（表・リスト・見出し・引用・コードブロックの行は False）。"""
    in_code = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        yield line, not in_code and not is_structure_line(line)


def sentences(text: str) -> list[str]:
    """文に分ける（行と、「」の外の句点・疑問符で切る）。"""
    return [x for line in text.split("\n") for x in _SENTENCE.findall(line) if x.strip()]


def prose_sentences(text: str) -> list[str]:
    """地の文（表・リスト・見出し・引用・コードの行を除く）の文を、比べる形（`normalize`）にしたもの。"""
    found = (normalize(x) for line, prose in _lines(text) if prose for x in sentences(line))
    return [x for x in found if x]
