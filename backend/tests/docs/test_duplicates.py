"""同じ内容の段落が、2 か所以上のドキュメントに書かれていないか（MECE。#114）。

同じ説明を 2 か所に書くと、片方だけ直されてずれる。1 か所に書き、ほかからはリンクする。
段落（空行で区切ったまとまり。表は 1 行ずつ）を、空白を除いて比べる。短いもの（見出し・
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


def paragraphs(text: str) -> list[tuple[int, str]]:
    """（行番号, 空白を除いた段落）。表の行は 1 行ずつ、それ以外は空行までを 1 段落にする。"""
    body = body_of(text)
    out: list[tuple[int, str]] = []
    start, buf = 0, []
    for i, line in enumerate(body.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("|"):
            out.append((i, stripped))
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
    return [(n, _SPACE.sub("", p)) for n, p in out if len(_SPACE.sub("", p)) >= MIN_CHARS]


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
        "同じ内容の段落が 2 か所以上にある（1 か所に書き、ほかからはリンクする）:\n"
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
