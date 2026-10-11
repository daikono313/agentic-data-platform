"""LLM・ユーザー・源泉の文章を Markdown と Mermaid に安全に載せる（PR 本文・docs/ontology.md・図）。

どのモジュールにも依存しない（ontology・design・vcs から使う）。文章はデータとして扱い、
構文や指示として解釈されないようにする。
"""

from __future__ import annotations

import re

_ABS_PATH = re.compile(r"(?:/Users|/home|/private|/tmp|[A-Z]:\\)[^\s`'\")]*")


def safe(text: str) -> str:
    """LLM やユーザーの文章を、公開リポジトリの PR やファイルに載せても安全にする。

    - `#123` や `@user` が issue の自動クローズやメンションにならないようにする
    - ローカルの絶対パスを伏せる
    - コードブロックを閉じられないようにする
    """
    text = _ABS_PATH.sub("<path>", text)
    text = re.sub(r"(?<!\w)([#@])(?=\w)", "\\1\u200b", text)
    return text.replace("```", "ˋˋˋ")


def cell(text: str) -> str:
    """Markdown の表のセル（safe に加えて、列の区切りと改行（CR・LF）を無効にする）。"""
    return safe(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


_MERMAID_ENTITIES = [
    ("#", "#35;"),  # 先に置き換える（後の実体参照の # を壊さないため）
    ('"', "#quot;"),
    ("<", "#lt;"),
    (">", "#gt;"),
    ("%", "#37;"),  # %%{init: …}%% の設定・%% のコメントを無効にする
    ("{", "#123;"),
    ("}", "#125;"),
    ("`", "#96;"),  # markdown の文字列
]


def mermaid_label(text: str) -> str:
    """Mermaid のラベル（引用符の中）に入れる文字列。

    構文・設定（%%{init}%%）・コメントとして解釈される文字を実体参照にし、改行は空白にする
    （ontology.yml や設計の説明はエージェントも書くため、データとして扱う）。
    """
    text = " ".join(text.split())
    for raw, entity in _MERMAID_ENTITIES:
        text = text.replace(raw, entity)
    return text
