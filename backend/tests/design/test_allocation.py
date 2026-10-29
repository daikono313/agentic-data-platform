"""複数の値の切り口（多対多）を、按分した fct にする（ADR 0010）。"""

from pathlib import Path
from typing import Any

import pytest

from adp.beam import Aggregation, BeamModel, DegenerateDimension, SourceRef, StoryType, load
from adp.design import ColumnRole, DesignBuilder, ForeignKey
from adp.design.check import load_ontology
from adp.generation.semantic import fact_model_semantics, metric_definitions
from adp.generation.tests import reconciliation_test
from adp.llm.settings import REPO_ROOT
from adp.ontology import Ontology
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


# --- 元の出来事の fct の entity（外部の entity。ADR 0010） ------------------------------------


def _source_entity(
    beam: BeamModel, ontology: Ontology | None = None
) -> tuple[str | None, dict[str, Any]]:
    design = DesignBuilder(BridgeSchema(), ontology=ontology).build(beam)
    fact = next(f for f in design.facts if f.allocation is not None)
    assert fact.allocation is not None
    event = beam.event(fact.event)
    return fact.allocation.source_entity, fact_model_semantics(fact, True, event)


def _ontology() -> Ontology:
    ontology = load_ontology(REPO_ROOT / "ontology" / "ontology.yml")
    assert ontology is not None
    return ontology


def test_source_entity_from_the_ontology(beam: BeamModel) -> None:
    """元の明細の fct（オントロジーの出来事の概念 order_line の fct_order_lines）の主 entity を、
    元の粒度の列を式にした外部の entity にする（按分した行から元の fct の属性で切れる）。"""
    entity, model = _source_entity(beam, _ontology())
    assert entity == "order_line"
    entities = model["derived_semantics"]["entities"]
    assert {"type": "foreign", "name": "order_line", "expr": "order_line_id"} in entities
    assert model["primary_entity"] == "order_line_promotion"


def test_source_entity_from_the_same_beam(beam: BeamModel) -> None:
    """同じ BEAM に元の出来事の fct（粒度が同じ源泉の縮退ディメンション 1 列）があれば、その
    主 entity を使う。"""
    data = beam.model_dump(mode="json")
    event = data["events"][0]
    data["events"].append(
        event
        | {
            "name": "customer_orders_product",
            "table_name": "order_items",
            "grain": "注文明細 1 行",
            "dimensions": [d for d in event["dimensions"] if d.get("bridge") is None],
        }
    )
    data["metrics"] = data["metrics"][:1]
    entity, _ = _source_entity(BeamModel.model_validate(data))
    assert entity == "order_item"


def test_no_source_entity_without_the_original_fact(beam: BeamModel) -> None:
    entity, model = _source_entity(beam)
    assert entity is None
    assert all(e["name"] != "order_line" for e in model["derived_semantics"]["entities"])


def test_source_entity_does_not_collide(beam: BeamModel) -> None:
    """按分した fct のほかの entity（ここでは主 entity）と同じ名前なら付けない。"""
    beam.events[0].table_name = "order_line"
    entity, _ = _source_entity(beam, _ontology())
    assert entity is None


def test_no_source_entity_for_a_composite_grain(beam: BeamModel) -> None:
    """元の粒度が複数の列なら、1 列の entity で表せないので付けない。"""
    event = beam.events[0]
    event.degenerate_dimensions.append(
        DegenerateDimension(
            name="order_id",
            label="注文番号",
            source=SourceRef(table="raw.order_items", column="order_id"),
        )
    )
    event.grain_keys = ["order_line_id", "order_id"]
    entity, _ = _source_entity(beam, _ontology())
    assert entity is None
