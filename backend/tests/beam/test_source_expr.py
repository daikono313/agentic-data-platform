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
        # Jinja の文字列のエスケープ（dim_key の引数の中で引用符を作る。#61 の再レビュー）
        r"concat(customer_id, '\x27 || (select string_agg(email, \x27,\x27) from raw.customers)"
        r" || \x27')",
        # コメントで行の残りを消す、位置で他のテーブルの列を指す
        "customer_id --",
        "customer_id /* x */ + 1",
        "customer_id + #1",
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
        "{{ dim_key(\"concat(id, '\\x27 || (select 1) || \\x27')\") }}",
    ):
        assert unsafe_jinja(bad), bad


@pytest.mark.parametrize(
    "expr",
    [
        "getenv('HOME')",
        "read_csv('/etc/passwd')",
        "current_setting('home_directory')",
        "coalesce(quantity, getenv('X'))",
        "md5(customer_name)",
        "version()",
        "query('select 1')",
        "getvariable('x')",
    ],
)
def test_only_allowed_functions(expr: str) -> None:
    """関数は許可したものだけ（ファイルの読み込み・設定や環境の読み出しなどは不可。#79）。"""
    with pytest.raises(ValueError, match="使えない関数"):
        SourceRef(table="raw.orders", expr=expr)


@pytest.mark.parametrize(
    "expr",
    [
        "date_trunc('month', ordered_at)",
        "extract(year from ordered_at)",
        "round(abs(unit_price - list_price), 2)",
        "nullif(lower(trim(status)), '')",
        "greatest(quantity, 0)",
        "if(quantity > 0, 1, 0)",
        # 演算子（sqlglot では関数の型）は関数として扱わない（#93 レビュー）
        "case when quantity > 0 and status <> 'cancelled' then quantity else 0 end",
        "not (a or b)",
        "a || b",
        "quantity % 2",
        # DuckDB でよく使う関数
        "strftime(ordered_at, '%Y-%m')",
        "date_part('year', ordered_at)",
        "hour(ordered_at)",
        "split_part(code, '-', 1)",
        "lpad(code, 5, '0')",
        "strpos(name, 'x')",
        "trunc(amount)",
        "regexp_matches(code, '^A')",
        "make_date(2024, 1, 1)",
    ],
)
def test_common_functions_are_allowed(expr: str) -> None:
    assert SourceRef(table="raw.orders", expr=expr).expr == expr
