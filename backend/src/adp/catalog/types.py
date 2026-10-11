"""源泉の列の型の判定（生成・クレンジング・検査で同じ判定を使う）。"""

from __future__ import annotations

_TEXT_PREFIXES = ("VARCHAR", "TEXT", "STRING", "CHAR", "BPCHAR", "NVARCHAR", "NCHAR")


def is_text_type(data_type: str) -> bool:
    """文字列の型か。VARCHAR(255)・TEXT・STRING・CHARACTER VARYING なども含める。

    配列（`VARCHAR[]`）は文字列ではない。
    """
    t = data_type.strip().upper()
    return t.startswith(_TEXT_PREFIXES) and "[" not in t
