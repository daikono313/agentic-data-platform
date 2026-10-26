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
