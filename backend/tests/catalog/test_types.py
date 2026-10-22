"""文字列の型の判定は、生成とクレンジングで同じ（#110）。"""

from pathlib import Path

import pytest

from adp.catalog.types import column_kind, is_text_type
from adp.cleansing.models import Defaults
from adp.cleansing.render import SourceColumn, column_expr
from adp.design import Column, ColumnRole
from adp.generation.dbt import _is_text

TEXT_TYPES = [
    "VARCHAR",
    "varchar",
    "TEXT",
    "VARCHAR(255)",
    "STRING",
    "CHAR(3)",
    "CHARACTER VARYING",
]
OTHER_TYPES = ["INTEGER", "DATE", "TIMESTAMP", "DECIMAL(18,2)", "VARCHAR[]", "BOOLEAN", ""]


@pytest.mark.parametrize("data_type", TEXT_TYPES)
def test_text_types(data_type: str) -> None:
    assert is_text_type(data_type)


@pytest.mark.parametrize("data_type", OTHER_TYPES)
def test_non_text_types(data_type: str) -> None:
    assert not is_text_type(data_type)


TRIM = Defaults.model_validate({"text": {"trim": True, "empty_to_null": False}})


class _Info:
    """生成が列の型を引くための最小の WarehouseInfo。"""

    def __init__(self, data_type: str) -> None:
        self.data_type = data_type

    def source_columns(self, table: str) -> list[tuple[str, str]]:
        return [("status", self.data_type)]

    @property
    def warehouse_dir(self) -> Path:
        return Path(".")


@pytest.mark.parametrize("data_type", ["TEXT", "VARCHAR(255)", "STRING", "VARCHAR"])
def test_generation_and_cleansing_agree_on_text(data_type: str) -> None:
    """実データの TEXT / VARCHAR(255) でも、dim の不明行は「不明」になり、stg は trim する。"""
    column = Column(name="status", role=ColumnRole.ATTRIBUTE, expr="o.status")
    assert _is_text(_Info(data_type), "raw.orders", column, {"o": "raw.orders"})  # type: ignore[arg-type]
    assert column_kind(data_type) == "text"
    expr = column_expr(SourceColumn("status", data_type), None, TRIM)
    assert "trim(status)" in expr


@pytest.mark.parametrize("data_type", ["INTEGER", "VARCHAR[]"])
def test_generation_and_cleansing_agree_on_non_text(data_type: str) -> None:
    column = Column(name="status", role=ColumnRole.ATTRIBUTE, expr="o.status")
    assert not _is_text(_Info(data_type), "raw.orders", column, {"o": "raw.orders"})  # type: ignore[arg-type]
    assert column_kind(data_type) != "text"
    assert column_expr(SourceColumn("status", data_type), None, TRIM) == "status"
