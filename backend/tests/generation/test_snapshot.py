"""定期スナップショット（状態）の生成した SQL と突合テストを、小さな入力で DuckDB に流して確かめる
（ADR 0008。dbt を使わず、ref と dim_key だけを置き換えて実行する）。

dbt build と手書きのリファレンスとの突合は tests/generation/test_dbt_codegen.py にある。ここでは
サンプルデータに無い入力（NULL・同じ時刻・主キーの無い源泉・別のテーブルの列の条件）を見る。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, cast

import duckdb
import pytest

from adp.beam import BeamModel, MetricFilter, TimeGrain, load
from adp.design import DesignBuilder, FactTable, ForeignKey
from adp.generation.dbt import WarehouseInfo, period_end, periodic_snapshot_sql
from adp.generation.tests import snapshot_reconciliation_test

FIXTURE = Path(__file__).parents[1] / "fixtures" / "beam" / "stock_levels.yml"


class _Schema:
    COLUMNS = {
        "raw.inventory": [
            "inventory_id",
            "store_id",
            "product_id",
            "moved_at",
            "movement_type",
            "quantity",
        ],
        "raw.stores": ["store_id", "store_name", "is_closed"],
        "raw.products": ["product_id", "category"],
    }
    FKS = {
        "raw.inventory": [
            ForeignKey("store_id", "raw.stores", "store_id"),
            ForeignKey("product_id", "raw.products", "product_id"),
        ]
    }

    def columns(self, table: str) -> list[str]:
        return self.COLUMNS.get(table, [])

    def foreign_keys(self, table: str) -> list[ForeignKey]:
        return self.FKS.get(table, [])


class _Info:
    def __init__(self, pk: str | None = "inventory_id") -> None:
        self.pk = pk

    def source_primary_key(self, table: str) -> str | None:
        return self.pk if table == "raw.inventory" else None


def _fact(beam: BeamModel | None = None) -> FactTable:
    design = DesignBuilder(_Schema()).build(beam or load(FIXTURE))
    assert design.blockers == [], [b.message for b in design.blockers]
    [fact] = design.facts
    return fact


def _render(sql: str) -> str:
    sql = re.sub(r"\{\{ ref\('stg_(\w+)'\) \}\}", r"stg_\1", sql)
    sql = re.sub(r"\{\{ ref\('(\w+)'\) \}\}", r"\1", sql)
    return re.sub(
        r"\{\{ dim_key\('([\w.]+)'\) \}\}",
        r"md5(coalesce(cast(\1 as varchar), '__unknown__'))",
        sql,
    )


Row = tuple[int | None, int, int, str | None, str, int | None]


def _db(rows: list[Row], stores: list[tuple[int, str, bool]] | None = None) -> Any:
    con = duckdb.connect()
    con.sql(
        "create table stg_inventory (inventory_id int, store_id int, product_id int,"
        " moved_at timestamp, movement_type varchar, quantity int)"
    )
    for r in rows:
        con.execute("insert into stg_inventory values (?, ?, ?, ?, ?, ?)", list(r))
    con.sql("create table stg_stores (store_id int, store_name varchar, is_closed boolean)")
    for s in stores or [(1, "a", False)]:
        con.execute("insert into stg_stores values (?, ?, ?)", list(s))
    con.sql("create table stg_products (product_id int, category varchar)")
    con.sql(
        "create table dim_date as select cast(d as date) as date_day"
        " from range(date '2024-01-01', date '2025-01-01', interval 1 day) t(d)"
    )
    return con


def _build(con: Any, fact: FactTable, info: _Info) -> list[tuple[Any, ...]]:
    con.sql(
        f"create or replace table {fact.name} as "
        + _render(periodic_snapshot_sql(fact, cast(WarehouseInfo, info)))
    )
    query = f"select snapshot_on, stock_qty from {fact.name} order by snapshot_on"
    return list(con.sql(query).fetchall())


def _reconcile(con: Any, fact: FactTable, info: _Info) -> list[str]:
    sql = _render(snapshot_reconciliation_test(fact, cast(WarehouseInfo, info)).content)
    return [r[0] for r in con.sql(sql).fetchall()]


def test_period_end_by_grain() -> None:
    """期間の終わりの日付（週は月曜始まり）。値は手で数えたもの。"""
    day = "timestamp '2024-02-14 10:00:00'"  # 水曜日
    expected = {
        TimeGrain.DAY: date(2024, 2, 14),
        TimeGrain.WEEK: date(2024, 2, 18),
        TimeGrain.MONTH: date(2024, 2, 29),
        TimeGrain.QUARTER: date(2024, 3, 31),
        TimeGrain.YEAR: date(2024, 12, 31),
    }
    for grain, end in expected.items():
        assert duckdb.sql("select " + period_end(day, grain)).fetchone() == (end,), grain


def test_running_total_with_reset_and_carry_forward() -> None:
    fact = _fact()
    con = _db(
        [
            (1, 1, 1, "2024-01-05 09:00", "receipt", 10),  # 最初の棚卸より前（数えない）
            (2, 1, 1, "2024-02-10 09:00", "count", 7),
            (3, 1, 1, "2024-02-20 09:00", "receipt", 5),
            (4, 1, 1, "2024-04-01 09:00", "disposal", -2),
        ]
    )
    rows = _build(con, fact, _Info())
    assert rows == [
        (date(2024, 2, 29), 12),
        (date(2024, 3, 31), 12),  # 記録なし → 引き継ぎ
        (date(2024, 4, 30), 10),
    ]
    assert _reconcile(con, fact, _Info()) == []


def test_null_quantity_count_does_not_reset() -> None:
    """数えた数の無い棚卸（quantity が NULL）は置き換えに使わない。突合テストも通る。"""
    fact = _fact()
    con = _db(
        [
            (1, 1, 1, "2024-01-10 09:00", "count", 7),
            (2, 1, 1, "2024-02-10 09:00", "receipt", 3),
            (3, 1, 1, "2024-02-20 09:00", "count", None),
        ]
    )
    assert _build(con, fact, _Info()) == [(date(2024, 1, 31), 7), (date(2024, 2, 29), 10)]
    assert _reconcile(con, fact, _Info()) == []


def test_same_time_is_ordered_without_primary_key() -> None:
    """主キーが分からなくても、同じ時刻の記録の順で結果が変わらない（値の列で並べる）。"""
    fact = _fact()
    rows: list[Row] = [
        (None, 1, 1, "2024-01-10 09:00", "count", 7),
        (None, 1, 1, "2024-01-10 09:00", "receipt", 3),
    ]
    first = _build(_db(rows), fact, _Info(pk=None))
    second = _build(_db(list(reversed(rows))), fact, _Info(pk=None))
    assert first == second
    con = _db(rows)
    _build(con, fact, _Info(pk=None))
    assert _reconcile(con, fact, _Info(pk=None)) == []


def test_reconciliation_detects_wrong_values_and_missing_rows() -> None:
    fact = _fact()
    con = _db(
        [
            (1, 1, 1, "2024-01-10 09:00", "count", 7),
            (2, 1, 1, "2024-03-10 09:00", "count", 4),
        ]
    )
    _build(con, fact, _Info())
    assert _reconcile(con, fact, _Info()) == []
    con.sql(f"update {fact.name} set stock_qty = 99 where snapshot_on = date '2024-03-31'")
    assert any("stock_qty が源泉から求めた値と違う" in r for r in _reconcile(con, fact, _Info()))
    con.sql(f"delete from {fact.name} where snapshot_on >= date '2024-03-31'")
    assert any("行が無いもの" in r for r in _reconcile(con, fact, _Info()))
    _build(con, fact, _Info())
    con.sql(f"delete from {fact.name} where snapshot_on = date '2024-02-29'")
    assert "期間が途切れている" in _reconcile(con, fact, _Info())


def test_reset_on_a_joined_table_column() -> None:
    """置き換える行の条件が別のテーブルの列でも、その結合を持つ SQL になる。"""
    beam = load(FIXTURE)
    event = beam.events[0]
    store = next(d for d in event.dimensions if d.ref == "store")
    store.attributes.append(
        store.attributes[0].model_copy(
            update={
                "name": "is_closed",
                "label": "閉店",
                "source": store.attributes[0].source.model_copy(update={"column": "is_closed"})
                if store.attributes[0].source
                else None,
            }
        )
    )
    measure = event.measure("stock_qty")
    assert measure is not None and measure.state is not None
    measure.state.reset = [
        MetricFilter.model_validate({"ref": "store.is_closed", "operator": "=", "values": [True]})
    ]
    fact = _fact(beam)
    assert [j.right.table for j in fact.joins] == ["raw.stores"]
    con = _db(
        [(1, 1, 1, "2024-01-10 09:00", "x", 5), (2, 1, 1, "2024-02-10 09:00", "x", 2)],
        stores=[(1, "a", True)],
    )
    assert _build(con, fact, _Info()) == [(date(2024, 1, 31), 5), (date(2024, 2, 29), 2)]


@pytest.mark.parametrize(
    "ref",
    ["movement_type.movement_type", "moved_at.month"],
)
def test_metric_on_record_level_items_is_blocked(ref: str) -> None:
    """記録 1 件ごとの項目（区分）は状態の fct に残らないので、切り口・絞り込みにできない。時点は
    よい。"""
    beam = load(FIXTURE)
    metric = beam.metrics[0]
    if ref.startswith("movement_type"):
        metric.filters = [
            MetricFilter.model_validate({"ref": ref, "operator": "=", "values": ["receipt"]})
        ]
    design = DesignBuilder(_Schema()).build(beam)
    blocked = [b.message for b in design.blockers if "記録 1 件ごとの項目" in b.message]
    assert bool(blocked) == ref.startswith("movement_type"), blocked


def test_event_and_state_facts_from_the_same_source() -> None:
    """同じ源泉から、出来事の fct（入出庫）と状態の fct（月末在庫）を同じ要求で作れる（概念が別）。
    同じ種類の fct が 2 つなら止める。"""
    from adp.beam import Additivity, StoryType

    beam = load(FIXTURE)
    state = beam.events[0]
    moves = state.model_copy(deep=True)
    moves.name, moves.table_name, moves.story_type = (
        "store_moves_stock",
        "stock_moves",
        StoryType.DISCRETE,
    )
    for m in moves.measures:
        m.state, m.additivity, m.non_additive_dimension = None, Additivity.ADDITIVE, None
    beam.events.append(moves)
    design = DesignBuilder(_Schema()).build(beam)
    assert not any("複数の fact" in b.message for b in design.blockers)
    twin = state.model_copy(deep=True)
    twin.name, twin.table_name = "store_counts_stock", "stock_counts"
    beam.events = [state, twin]
    design = DesignBuilder(_Schema()).build(beam)
    assert any("複数の fact" in b.message for b in design.blockers)
