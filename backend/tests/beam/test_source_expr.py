"""SourceRef.expr は、そのテーブルの列だけを使う 1 行ごとの式に限る（#46）。"""

import pytest

from adp.beam.models import SourceRef


@pytest.mark.parametrize(
    "expr",
    [
        "quantity * unit_price - discount_amount",
        "case when status = 'cancelled' then 0 else quantity end",
        "cast(ordered_at as date)",
        "coalesce(discount_amount, 0)",
    ],
)
def test_row_expressions_are_allowed(expr: str) -> None:
    assert SourceRef(table="raw.order_items", expr=expr).expr == expr


@pytest.mark.parametrize(
    "expr",
    [
        # 実際の LLM の E2E で出た式（返品テーブルをサブクエリで読む）
        "coalesce((select sum(r.quantity) from raw.returns r"
        " where r.order_item_id = order_items.order_item_id), 0)",
        "sum(quantity)",
        "count(*) over ()",
        "quantity; drop table raw.orders",
        "o.quantity",
        "select 1",
        "quantity +",
    ],
)
def test_subqueries_aggregates_and_other_tables_are_rejected(expr: str) -> None:
    with pytest.raises(ValueError):
        SourceRef(table="raw.order_items", expr=expr)
