from adp.llm import join_chunks, tool_text


def test_tool_text() -> None:
    assert tool_text("ok") == {"content": [{"type": "text", "text": "ok"}]}
    assert tool_text("ng", error=True) == {
        "content": [{"type": "text", "text": "ng"}],
        "is_error": True,
    }


def test_join_chunks() -> None:
    """本文のかたまりを空行でつなぐ。空のかたまりは捨て、頭のインデントは残す（#161）。"""
    assert (
        join_chunks(["- 数量: 54 個", "", " \n", "次は店舗です。"])
        == "- 数量: 54 個\n\n次は店舗です。"
    )
    assert join_chunks(["- a\n", "  - a-1\n  - a-2"]) == "- a\n\n  - a-1\n  - a-2"
    assert join_chunks(["同じ文です。", "同じ文です。"]) == "同じ文です。\n\n同じ文です。"
