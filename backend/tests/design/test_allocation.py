"""複数の値の切り口（多対多）を、按分した fct にする（ADR 0010）。"""

from pathlib import Path

import pytest

from adp.beam import Aggregation, BeamModel, StoryType, load
from adp.design import ColumnRole, DesignBuilder, ForeignKey
from adp.generation.semantic import metric_definitions
from adp.generation.tests import reconciliation_test
from tests.design.test_builder import FakeSchema

FIXTURE = Path(__file__).parents[1] / "fixtures" / "beam" / "promotion_sales.yml"


class BridgeSchema(FakeSchema):
    COLUMNS = FakeSchema.COLUMNS | {
        "raw.order_promotions": ["order_promotion_id", "order_id", "promotion_id"]
    }
    FKS = FakeSchema.FKS | {
        "raw.order_promotions": [
            ForeignKey("order_id", "raw.orders", "order_id"),
            ForeignKey("promotion_id", "raw.promotions", "promotion_id"),
        ]
    }


@pytest.fixture
def beam() -> BeamModel:
    return load(FIXTURE)


def test_allocated_fact(beam: BeamModel) -> None:
    """粒度は元の 1 行 × 付いた値、ブリッジとの一対多の結合は 1 か所、合計には重みを掛ける。"""
    design = DesignBuilder(BridgeSchema()).build(beam)
    assert design.blockers == []
    [fact] = design.facts
    assert fact.grain_columns == ["order_line_id", "applied_promotion_key"]
    fan_out = [j for j in fact.joins if j.fan_out]
    assert [(j.right.table, j.right_column) for j in fan_out] == [
        ("raw.order_promotions", "order_id")
    ]
    weight = "1.0 / count(*) over (partition by t0.order_item_id)"
    columns = {c.name: c for c in fact.columns}
    assert columns["allocation_weight"].role == ColumnRole.WEIGHT
    assert columns["allocation_weight"].expr == weight
    assert columns["sales_amount"].expr.endswith(f") * {weight}")
    a = fact.allocation
    assert a is not None and a.dimension == "applied_promotion"
    assert a.source_grain == ["order_line_id"]
    assert a.source_measures["sales_amount"] == "t0.quantity * t0.unit_price - t0.discount_amount"
    # オントロジーではブリッジの源泉（プロモーションの適用）の概念に結び付く
    assert fact.concept_source == "raw.order_promotions"


def test_allocated_metrics_and_reconciliation(beam: BeamModel) -> None:
    """件数は重みの合計。突合テストは元の行の数・元の合計・行ごとの重みの合計 1 を見る。"""
    design = DesignBuilder(BridgeSchema()).build(beam)
    [fact] = design.facts
    metrics = {m["name"]: m for m in metric_definitions(beam, design, fact).in_model}
    assert metrics["promotion_order_lines"]["agg"] == "sum"
    assert metrics["promotion_order_lines"]["expr"] == "allocation_weight"
    assert metrics["promotion_sales"]["expr"] == "sales_amount"
    sql = reconciliation_test(fact).content
    assert "count(distinct (order_line_id))" in sql
    assert "sum(t0.quantity * t0.unit_price - t0.discount_amount) as sales_amount" in sql
    assert "abs(g.total - 1) > 1e-9" in sql


def test_not_allocated_aggregations_are_explained(beam: BeamModel) -> None:
    """個数の数え直し・最小・最大は重みを掛けず、値ごとに足しても全体と一致しないと説明に書く。"""
    event = beam.events[0]
    measure = event.measure("sales_amount")
    assert measure is not None
    measure.aggregation = Aggregation.MAX
    design = DesignBuilder(BridgeSchema()).build(beam)
    [fact] = design.facts
    assert fact.column("sales_amount").expr == "t0.quantity * t0.unit_price - t0.discount_amount"  # type: ignore[union-attr]
    metrics = {m["name"]: m for m in metric_definitions(beam, design, fact).in_model}
    assert "値ごとに足しても全体とは一致しない" in metrics["promotion_sales"]["description"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda e: setattr(e, "story_type", StoryType.RECURRING), "1 回ごとの出来事"),
        (lambda e: setattr(e.dimensions[2], "role", None), "役割の名前"),
        (lambda e: setattr(e.measures[0], "aggregation", Aggregation.AVERAGE), "平均（average）"),
    ],
)
def test_bridge_issues(beam: BeamModel, change, message: str) -> None:  # type: ignore[no-untyped-def]
    change(beam.events[0])
    assert any(message in i for i in beam.finalize_issues())


def test_metric_on_allocated_event_must_use_the_bridged_dimension(beam: BeamModel) -> None:
    """按分した出来事の指標は、複数の値の切り口で切る（値で切らない指標は元の出来事に置く）。"""
    beam.metrics[1].group_by = []
    assert any("指標の切り口にそのディメンションを入れて" in i for i in beam.finalize_issues())


def test_bridge_needs_the_fact_key_in_the_bridge_table(beam: BeamModel) -> None:
    data = beam.model_dump(mode="json")
    data["events"][0]["dimensions"][2]["fact_key"] = {
        "table": "raw.orders",
        "column": "promotion_id",
    }
    with pytest.raises(ValueError, match="ブリッジの表"):
        BeamModel.model_validate(data)


def test_more_bridge_issues(beam: BeamModel) -> None:
    """マスタの無い値・粒度のキーに入れた切り口・足し上げできない値は、按分できない。"""
    from adp.beam import Additivity, SourceRef

    event = beam.events[0]
    promotion = event.dimensions[2]
    promotion.business_key = SourceRef(table="raw.order_promotions", column="promotion_id")
    event.grain_keys = ["order_line_id", "applied_promotion"]
    measure = event.measure("sales_amount")
    assert measure is not None
    measure.additivity = Additivity.SEMI_ADDITIVE
    issues = beam.finalize_issues()
    assert any("マスタの無い値" in i for i in issues)
    assert any("粒度のキー（grain_keys）に入れません" in i for i in issues)
    assert any("足し上げできない値" in i for i in issues)


def test_measure_outside_the_base_table_is_a_blocker(beam: BeamModel) -> None:
    """1 行の元の表以外（注文やブリッジ）の値は、既に値ごと・注文ごとの値なので按分しない。"""
    from adp.beam import SourceRef

    measure = beam.events[0].measure("sales_amount")
    assert measure is not None
    measure.source = SourceRef(table="raw.orders", column="order_id")
    design = DesignBuilder(BridgeSchema()).build(beam)
    assert any("1 行の元の表の列ではないので" in i.message for i in design.blockers)


def test_existing_fact_is_not_rewritten(beam: BeamModel, tmp_path: Path) -> None:
    """按分した fct が既にある（名前のぶつかる既存の fct を含む）ときは、書き直させずに止める。"""
    from adp.generation import generate_models

    class Info:
        warehouse_dir = tmp_path

        def existing_models(self) -> set[str]:
            stg = {f"stg_{t}" for t in ("order_items", "orders", "order_promotions", "products")}
            stg |= {"stg_promotions", "dim_product", "dim_promotion", "dim_order_status"}
            return stg | {"fct_order_line_promotions"}

        def existing_columns(self, model: str) -> set[str]:
            return {"category", "promotion_name", "order_status"}

    design = DesignBuilder(BridgeSchema()).build(beam)
    with pytest.raises(ValueError, match="按分した fct として作れません"):
        generate_models(design, Info(), beam)  # type: ignore[arg-type]


def test_reconciliation_catches_wrong_weights_and_sums(beam: BeamModel) -> None:
    """生成した突合テストの SQL が、重みの合計のずれと合計の不一致で行を返す。"""
    import re

    import duckdb

    [fact] = DesignBuilder(BridgeSchema()).build(beam).facts
    sql = re.sub(r"\{\{ ref\('(\w+)'\) \}\}", r"\1", reconciliation_test(fact).content)
    con = duckdb.connect()
    con.execute(
        "create table stg_order_items as select * from (values (1, 2, 100, 0), (2, 1, 50, 0))"
        " t(order_item_id, quantity, unit_price, discount_amount)"
    )

    def fails(rows: str) -> bool:
        con.execute(
            "create or replace table fct_order_line_promotions as select * from (values "
            f"{rows}) t(order_line_id, applied_promotion_key, sales_amount, allocation_weight)"
        )
        return bool(con.execute(sql).fetchall())

    # 明細 1（200 円）は 2 つのプロモーションで半分ずつ、明細 2（50 円）は付かない
    assert not fails("(1, 'a', 100.0, 0.5), (1, 'b', 100.0, 0.5), (2, 'u', 50.0, 1.0)")
    assert fails("(1, 'a', 200.0, 1.0), (1, 'b', 200.0, 1.0), (2, 'u', 50.0, 1.0)")  # 二重計上
    assert fails("(1, 'a', 100.0, 0.5), (1, 'b', 100.0, 0.4), (2, 'u', 60.0, 1.0)")
    assert fails("(1, 'a', 100.0, 0.5), (1, 'b', 100.0, 0.5)")  # 元の行が欠ける


def _with_share(beam: BeamModel) -> BeamModel:
    event = beam.events[0]
    promotion = next(d for d in event.dimensions if d.bridge is not None)
    assert promotion.bridge is not None
    promotion.bridge.weight = "share"
    return beam


class ShareSchema(BridgeSchema):
    COLUMNS = BridgeSchema.COLUMNS | {
        "raw.order_promotions": ["order_promotion_id", "order_id", "promotion_id", "share"]
    }


def test_weight_from_the_share_column(beam: BeamModel) -> None:
    """割合の列があれば、元の行ごとの合計で割って重みにする。合計が 0 か、NULL・負の値があれば
    均等、ブリッジの無い行は 1（ADR 0010）。"""
    import duckdb

    design = DesignBuilder(ShareSchema()).build(_with_share(beam))
    assert design.blockers == []
    [fact] = design.facts
    assert fact.allocation is not None and fact.allocation.weight_source == "share"
    weight = next(c for c in fact.columns if c.name == "allocation_weight")
    assert "raw.order_promotions の share" in weight.description
    assert "share の割合で按分" in metric_definitions(beam, design, fact).in_model[0]["description"]
    con = duckdb.connect()
    rows = con.execute(
        f"select t0.order_item_id, t2.promotion_id, {weight.expr} as w from (values"
        " (1, 'a', 1), (1, 'b', 3), (2, 'a', null), (2, 'b', 1), (3, 'a', 0), (3, 'b', 0),"
        " (4, 'a', -1), (4, 'b', 2), (5, null, null))"
        " t(order_item_id, promotion_id, share), lateral (select order_item_id) t0(order_item_id),"
        " lateral (select promotion_id, share) t2(promotion_id, share)"
        " order by 1, 2"
    ).fetchall()
    assert [(k, p, round(w, 6)) for k, p, w in rows] == [
        (1, "a", 0.25),
        (1, "b", 0.75),
        (2, "a", 0.5),  # NULL がある
        (2, "b", 0.5),
        (3, "a", 0.5),  # 合計が 0
        (3, "b", 0.5),
        (4, "a", 0.5),  # 負の値がある
        (4, "b", 0.5),
        (5, None, 1.0),  # ブリッジの無い行
    ]


def test_missing_share_column_is_a_blocker(beam: BeamModel) -> None:
    design = DesignBuilder(BridgeSchema()).build(_with_share(beam))
    assert any(
        "割合の列 share が raw.order_promotions にありません" in b.message for b in design.blockers
    )


def test_allocated_unit_test_uses_the_share(beam: BeamModel) -> None:
    """unit test は、割合 1 : 3 の値を 2 つ付け、重み 0.25・0.75 で分かれることを見る。"""
    from adp.generation.tests import unit_test

    class Info:
        def source_columns(self, table: str) -> list[tuple[str, str]]:
            return [(c, "INTEGER") for c in ShareSchema.COLUMNS[table]]

    beam = _with_share(beam)
    [fact] = DesignBuilder(ShareSchema()).build(beam).facts
    event = beam.events[0]
    ut = unit_test(beam, fact, Info(), event.examples[0], 0)  # type: ignore[arg-type]
    assert ut is not None
    bridge = next(g for g in ut["given"] if g["input"] == "ref('stg_order_promotions')")
    assert [r["share"] for r in bridge["rows"]] == [1, 3]
    rows = ut["expect"]["rows"]
    assert [r["allocation_weight"] for r in rows] == [0.25, 0.75, 1]
    assert [r["sales_amount"] for r in rows] == [150.0, 450.0, rows[2]["sales_amount"]]
