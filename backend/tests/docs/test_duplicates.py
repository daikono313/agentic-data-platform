"""同じ内容の段落が、ドキュメントの 2 か所以上（同じファイルの中も含む）に無いか（MECE。#114）。

同じ説明を 2 か所に書くと、片方だけ直されてずれる。1 か所に書き、ほかからはリンクする。
段落（空行で区切ったまとまり。箇条書きと表は 1 項目・1 行ずつ）を比べる。比べる前に、空白・
箇条書きの印・強調（**）・リンク先・表の 1 列目（C1 などの ID）を除く。短いもの（見出し・
表の見出し行・定型の一文）は対象外。コードブロックの中も見ない。
"""

import re
from collections import defaultdict

from tests.docs._docs import doc_files, doc_text, line_of, rel
from tests.docs._docs import without_fences as body_of

# 生成物（元の定義から作る。同じ説明が入るのは生成の仕組みのため）
GENERATED = {"docs/ontology.md"}

# これより短い段落は比べない（定型の一文や表の見出し行が重なるのは問題にしない）
MIN_CHARS = 80

_SPACE = re.compile(r"\s+")
_ITEM = re.compile(r"^(?:[-*+]|\d+\.)\s+")
_LINK_TARGET = re.compile(r"\]\([^)]*\)")


def _normalize(para: str) -> str:
    """空白・強調・リンク先を除く（相対パスの書き方だけが違うリンクも同じにする）。"""
    return _SPACE.sub("", _LINK_TARGET.sub("]", para.replace("**", "")))


def paragraphs(text: str) -> list[tuple[int, str]]:
    """（行番号, 正規化した段落）。表の行・箇条書きの項目は 1 つずつ、ほかは空行までを 1 段落。"""
    body = body_of(text)
    out: list[tuple[int, str]] = []
    start, buf = 0, list[str]()
    for i, line in enumerate(body.splitlines(), start=1):
        stripped = line.strip()
        item = _ITEM.match(stripped)
        if stripped.startswith("|") or item:
            if buf:
                out.append((start, " ".join(buf)))
                buf = []
            if item:  # 箇条書きは印を除き、続きの行（字下げ）は次の項目まで足す
                start, buf = i, [stripped[item.end() :]]
            else:  # 表は 1 列目（ID）を除く
                out.append((i, "|".join(stripped.strip("|").split("|")[1:])))
            continue
        if not stripped or stripped.startswith("#"):
            if buf:
                out.append((start, " ".join(buf)))
            buf = []
            continue
        if not buf:
            start = i
        buf.append(stripped)
    if buf:
        out.append((start, " ".join(buf)))
    return [(n, p) for n, p in ((n, _normalize(p)) for n, p in out) if len(p) >= MIN_CHARS]


def duplicated_paragraphs(docs: dict[str, str]) -> list[str]:
    """docs（パス → 本文）の中で、2 か所以上にある段落（`パス:行 / パス:行: 段落の先頭…`）。"""
    seen: dict[str, list[str]] = defaultdict(list)
    for path, text in docs.items():
        for line, para in paragraphs(text):
            seen[para].append(f"{path}:{line}")
    return [f"{' / '.join(where)}: {para[:60]}…" for para, where in seen.items() if len(where) > 1]


def test_no_duplicated_paragraphs() -> None:
    docs = {rel(d): doc_text(d) for d in doc_files() if rel(d) not in GENERATED}
    dup = duplicated_paragraphs(docs)
    assert not dup, (
        "同じ内容の段落がドキュメントの 2 か所以上にある（1 か所に書き、ほかからはリンクする）:\n"
        + "\n".join(dup)
    )


def test_paragraphs_ignore_fences_and_short_lines() -> None:
    text = "# 見出し\n\n短い。\n\n```\n" + "あ" * 100 + "\n```\n\n" + "い" * 90 + "\n"
    assert [p for _, p in paragraphs(text)] == ["い" * 90]


def test_line_numbers_point_at_the_paragraph() -> None:
    text = "# 見出し\n\n" + "う" * 90 + "\n"
    assert paragraphs(text)[0][0] == line_of(text, text.index("う"))


def test_a_paragraph_copied_to_another_doc_is_found() -> None:
    para = "え" * 90
    dup = duplicated_paragraphs({"a.md": f"# A\n\n{para}\n", "b.md": f"前置き\n\n{para}\n"})
    assert len(dup) == 1 and dup[0].startswith("a.md:3 / b.md:3")


def test_list_items_bold_links_and_table_ids_are_normalised() -> None:
    """箇条書きの 1 項目のコピー・強調の有無・リンク先の書き方・表の ID の違いでは見逃さない。"""
    item = "お" * 90
    a = f"- 前の項目\n- **{item}**（[設計](docs/design-doc.md)）\n- 後の項目\n"
    b = f"違う前置き\n\n{item}（[設計](design-doc.md)）\n"
    assert len(duplicated_paragraphs({"a.md": a, "b.md": b})) == 1
    row = "か" * 90
    assert (
        len(duplicated_paragraphs({"a.md": f"| C1 | {row} |\n", "b.md": f"| C9 | {row} |\n"})) == 1
    )
