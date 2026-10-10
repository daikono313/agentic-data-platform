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
        # Jinja（dbt が展開する。#61 のレビュー）
        "{{ run_query('create table pwn as select 1') }}",
        "{{ env_var('GH_TOKEN') }}",
        "'{{ env_var(\"X\") }}'",
        "{% set x = 1 %}quantity",
        # 閉じていない文字列・コメント（sqlglot の別の例外も ValueError にする）
        "'abc",
        "a /*",
    ],
)
def test_subqueries_aggregates_and_other_tables_are_rejected(expr: str) -> None:
    with pytest.raises(ValueError):
        SourceRef(table="raw.order_items", expr=expr)


def test_generated_files_reject_unexpected_jinja(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """生成物は、生成器が出す形の Jinja だけを書ける（最後の確認）。"""
    from adp.generation.project import unsafe_jinja

    ok = (
        "select {{ dim_key('t0.product_id') }} as product_key, {{ unknown_key() }}\n"
        "from {{ ref('stg_orders') }} as t0\n"
        "left join {{ source('raw', 'orders') }}\n"
        "filter: \"{{ Dimension('order_line__order_status') }} != 'cancelled'\"\n"
    )
    assert unsafe_jinja(ok) == []
    for bad in (
        "description: '{{ env_var(\"GH_TOKEN\") }}'",
        "{{ run_query('select 1') }}",
        "{% do run_query('x') %}",
        "{# comment #}",
        "{{ dim_key('{{ env_var(\"X\") }}') }}",
        "{{ ref('x') }}{{ ref(\"y\") }}",
    ):
        assert unsafe_jinja(bad), bad
