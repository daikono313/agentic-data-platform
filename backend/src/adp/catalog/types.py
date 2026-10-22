"""源泉の列の型の判定（生成・クレンジング・検査で同じ判定を使う）。"""

from __future__ import annotations

import re

_TEXT_PREFIXES = ("VARCHAR", "TEXT", "STRING", "CHAR", "BPCHAR", "NVARCHAR", "NCHAR")


def is_text_type(data_type: str) -> bool:
    """文字列の型か。VARCHAR(255)・TEXT・STRING・CHARACTER VARYING なども含める。

    配列（`VARCHAR[]`）は文字列ではない。
    """
    t = data_type.strip().upper()
    return t.startswith(_TEXT_PREFIXES) and "[" not in t


_NUMBER = re.compile(
    r"U?(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|INT\d*|DOUBLE|FLOAT\d*|REAL|DECIMAL|NUMERIC)"
    r"(\(\d+(,\s*\d+)?\))?"
)


def column_kind(data_type: str) -> str:
    """列の型の種類（text / timestamp / date / number / boolean / other）。画面の型の表示・
    クレンジング・プロファイル・オントロジーの役割の照合で同じ判定を使う。

    is_text_type と同じく、型の名前だけで決める。
    """
    if is_text_type(data_type):
        return "text"
    t = data_type.upper().strip()
    if "[" in t:  # 配列（INTEGER[] など）は、要素の型にかかわらず other
        return "other"
    if t.startswith("TIMESTAMP"):
        return "timestamp"
    if t == "DATE":
        return "date"
    if _NUMBER.fullmatch(t):
        return "number"
    if t in ("BOOLEAN", "BOOL"):
        return "boolean"
    return "other"
