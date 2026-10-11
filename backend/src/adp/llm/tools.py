"""エージェントの tool（MCP）の結果を組み立てる。"""

from __future__ import annotations

from typing import Any


def tool_text(text: str, *, error: bool = False) -> dict[str, Any]:
    """文字列 1 つの tool の結果。error なら、エージェントに直させる誤りとして返す。"""
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        result["is_error"] = True
    return result
