"""MetricFlow の where（Jinja で展開される）に書く値のリテラル。

指標の生成（`adp.generation.semantic`）とチャートの元ファイル（`adp.charts`）で共有する。
どのモジュールにも依存しない。
"""

from __future__ import annotations

import math
import re
from typing import Any

SAFE_VALUE = re.compile(r"[^{}%\\\x00-\x1f\x7f-\x9f\u2028\u2029]{0,200}")
"""文字列の値に使える文字（Jinja の { } %・バックスラッシュ・制御文字・改行を除く。200 字まで）。"""


def literal(value: Any) -> str:
    """値を MetricFlow の where のリテラルにする。使えない値は ValueError。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        if not math.isfinite(value):
            raise ValueError(f"filter の値に使えない数値があります: {value!r}")
        return str(value)
    if isinstance(value, str):
        # MetricFlow は where を Jinja で展開するので、{ } % があるとエスケープの後で引用符や
        # 任意の SQL を作れる。値に使える文字を絞る（#55 のレビュー）
        if not SAFE_VALUE.fullmatch(value):
            raise ValueError(f"filter の値に使えない文字があります: {value!r}")
        return "'" + value.replace("'", "''") + "'"
    raise ValueError(f"filter の値に使えない型があります: {value!r}")


def condition(lhs: str, operator: str, values: list[Any]) -> str:
    """絞り込みの条件（BEAM の FilterOperator の値 = / != / in / not_in）を SQL にする。

    MetricFlow の where（左辺は `{{ Dimension(...) }}`）と、定期スナップショットの置き換える行の
    条件（左辺はソースの列。ADR 0008）で共有する。使えない値は ValueError。
    """
    lits = [literal(v) for v in values]
    if operator == "=":
        return f"{lhs} = {lits[0]}"
    if operator == "!=":
        return f"{lhs} <> {lits[0]}"
    if operator in ("in", "not_in"):
        return f"{lhs} {'in' if operator == 'in' else 'not in'} ({', '.join(lits)})"
    raise ValueError(f"使えない演算子です: {operator!r}")
