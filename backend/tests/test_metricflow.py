"""MetricFlow の where のリテラル（指標の生成とチャートで共有。#55・#110）。"""

import math
from typing import Any

import pytest

from adp.metricflow import literal


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "true"),
        (False, "false"),
        (3, "3"),
        (1.5, "1.5"),
        ("app", "'app'"),
        ("o'k", "'o''k'"),
        ("東京 本店", "'東京 本店'"),
    ],
)
def test_literal(value: Any, expected: str) -> None:
    assert literal(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "{{ env_var('X') }}",
        "{% do x %}",
        "100%",
        "a\\b",
        "a\nb",
        "a\u2028b",
        "a\u2029b",
        "x" * 201,
        math.nan,
        math.inf,
        None,
        ["a"],
    ],
)
def test_unsafe_values_are_rejected(value: Any) -> None:
    with pytest.raises(ValueError):
        literal(value)
