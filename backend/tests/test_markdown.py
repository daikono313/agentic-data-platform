"""文章を Markdown・Mermaid に載せるときの無害化（PR 本文・docs/ontology.md・図で共有。#110）。"""

from adp.markdown import cell, mermaid_label, safe


def test_safe() -> None:
    text = safe("Closes #12 cc @someone /Users/me/secret/path.txt ```x``` a#b")
    assert "#12" not in text and "@someone" not in text
    assert "/Users/me" not in text and "<path>" in text
    assert "```" not in text
    assert "a#b" in text  # 語の途中の # はそのまま


def test_cell() -> None:
    assert cell("a | b\nc #1") == "a \\| b c #​1"


def test_mermaid_label() -> None:
    assert mermaid_label('a "b"\n  c') == "a #quot;b#quot; c"
    assert mermaid_label("%%{init}%% `x` <y> #1") == (
        "#37;#37;#123;init#125;#37;#37; #96;x#96; #lt;y#gt; #35;1"
    )


def test_cell_neutralises_carriage_returns() -> None:
    """CR（CRLF）も表の行を終わらせるので空白にする（#126 のレビュー）。"""
    from adp.markdown import cell

    assert cell("a\r\nb\rc|d") == "a  b c\\|d"
