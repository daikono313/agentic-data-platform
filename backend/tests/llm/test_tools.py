from adp.llm import tool_text


def test_tool_text() -> None:
    assert tool_text("ok") == {"content": [{"type": "text", "text": "ok"}]}
    assert tool_text("ng", error=True) == {
        "content": [{"type": "text", "text": "ng"}],
        "is_error": True,
    }
