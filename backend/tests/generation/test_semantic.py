from pathlib import Path

import pytest

from adp.beam import FilterOperator, MetricFilter, MetricRequest, MetricType, TimeGrain, load
from adp.design import DesignBuilder
from adp.generation.semantic import (
    SemanticError,
    fact_model_semantics,
    fact_semantics,
    filter_expr,
    metric_definitions,
    mf_name,
)
from tests.design.test_builder import FakeSchema

FIXTURE = Path(__file__).parents[1] / "fixtures" / "beam" / "sales_by_store.yml"


@pytest.fixture
def setup():  # type: ignore[no-untyped-def]
    beam = load(FIXTURE)
    design = DesignBuilder(FakeSchema()).build(beam)
    return beam, design, beam.events[0], design.facts[0]


def test_mf_names(setup) -> None:  # type: ignore[no-untyped-def]
    _, _, event, fact = setup
    assert mf_name(event, fact, "store.channel") == "store__channel"
    assert mf_name(event, fact, "order_id") == "order_line__order_id"
    assert mf_name(event, fact, "payment_method.payment_method_name") == (
        "order_line__payment_method"
    )
    assert mf_name(event, fact, "date") == "metric_time__day"
    assert mf_name(event, fact, "date.month") == "metric_time__month"
    with pytest.raises(SemanticError):
        mf_name(event, fact, "nope.x")


def test_filter_expressions(setup) -> None:  # type: ignore[no-untyped-def]
    _, _, event, fact = setup
    f = MetricFilter(ref="store.channel", operator=FilterOperator.NOT_IN, values=["app", "o'k"])
    assert filter_expr(event, fact, f) == (
        "{{ Dimension('store__channel') }} not in ('app', 'o''k')"
    )


def test_metric_types(setup) -> None:  # type: ignore[no-untyped-def]
    beam, design, _, fact = setup
    common = {"label": "x", "question": "q", "framed_question": "q", "event": fact.event}
    beam.metrics += [
        MetricRequest.model_validate(common | {"name": "qty", "measure": "quantity"}),
        MetricRequest.model_validate(
            common
            | {"name": "avg_price", "type": "ratio", "numerator": "revenue", "denominator": "qty"}
        ),
        MetricRequest.model_validate(
            common
            | {
                "name": "rev_7d",
                "type": "cumulative",
                "measure": "sales_amount",
                "window": "7 days",
                "group_by": ["store.channel"],
            }
        ),
        MetricRequest.model_validate(
            common | {"name": "diff", "type": "derived", "expr": "revenue - qty"}
        ),
    ]
    defs = metric_definitions(beam, design, fact)
    names = [m["name"] for m in defs.in_model]
    assert names == ["revenue", "qty", "rev_7d_input"]
    revenue = defs.in_model[0]
    assert revenue["agg"] == "sum" and revenue["expr"] == "sales_amount"
    assert revenue["filter"] == "{{ Dimension('order_line__order_status') }} <> 'cancelled'"
    top = {m["name"]: m for m in defs.top_level}
    assert top["avg_price"]["type"] == "ratio"
    assert top["avg_price"]["numerator"] == "revenue"  # 絞り込みが無ければ名前だけ
    assert top["rev_7d"] | {} == top["rev_7d"] and top["rev_7d"]["window"] == "7 days"
    assert top["rev_7d"]["period_agg"] == "last" and top["rev_7d"]["input_metric"] == "rev_7d_input"
    assert top["diff"]["input_metrics"] == [{"name": "qty"}, {"name": "revenue"}]
    cumulative_query = next(q for q in defs.queries if q.metric == "rev_7d")
    assert cumulative_query.group_by == ["metric_time__day", "store__channel"]
    assert beam.metrics[1].type == MetricType.SIMPLE
    # 時間軸を月次で記録しているなら、月別で問い合わせる（日別には切れない。#188）
    time = beam.event(fact.event).time_dimension
    time.time_grain = TimeGrain.MONTH
    defs = metric_definitions(beam, design, fact)
    cumulative_query = next(q for q in defs.queries if q.metric == "rev_7d")
    assert cumulative_query.group_by == ["metric_time__month", "store__channel"]


def test_ratio_and_derived_keep_filters(setup) -> None:  # type: ignore[no-untyped-def]
    beam, design, _, fact = setup
    common = {"label": "x", "question": "q", "framed_question": "q", "event": fact.event}
    beam.metrics += [
        MetricRequest.model_validate(common | {"name": "qty", "measure": "quantity"}),
        MetricRequest.model_validate(
            common
            | {
                "name": "aov_online",
                "type": "ratio",
                "numerator": "revenue",
                "denominator": "qty",
                "filters": [{"ref": "store.channel", "operator": "in", "values": ["online", 1]}],
            }
        ),
        MetricRequest.model_validate(
            common
            | {
                "name": "diff",
                "type": "derived",
                "expr": "revenue - qty",
                "filters": [{"ref": "store.channel", "operator": "=", "values": [True]}],
            }
        ),
    ]
    top = {m["name"]: m for m in metric_definitions(beam, design, fact).top_level}
    # 絞り込みは元の指標ごとに書く（出来事が分かれていても、それぞれの名前で解決する。#178）
    online = "{{ Dimension('store__channel') }} in ('online', 1)"
    assert "filter" not in top["aov_online"]
    assert top["aov_online"]["numerator"] == {"name": "revenue", "filter": online}
    assert top["aov_online"]["denominator"] == {"name": "qty", "filter": online}
    assert "filter" not in top["diff"]
    channel = "{{ Dimension('store__channel') }} = true"
    assert top["diff"]["input_metrics"] == [
        {"name": n, "filter": channel} for n in ("qty", "revenue")
    ]


def test_grain_key_maps_to_entity(setup) -> None:  # type: ignore[no-untyped-def]
    _, _, event, fact = setup
    assert mf_name(event, fact, "order_line_id") == "order_line"


def test_equals_requires_single_value() -> None:
    with pytest.raises(ValueError, match="1 つだけ"):
        MetricFilter(ref="store.channel", operator=FilterOperator.EQUALS, values=["a", "b"])


def test_validation_of_filters_and_reserved_names(setup) -> None:  # type: ignore[no-untyped-def]
    beam, _, _, fact = setup
    common = {"label": "x", "question": "q", "framed_question": "q", "event": fact.event}
    beam.metrics += [
        MetricRequest(name="plain", measure="quantity", filter="店舗だけ", **common),
        MetricRequest.model_validate(
            common | {"name": "rev_total", "type": "cumulative", "measure": "sales_amount"}
        ),
        MetricRequest(name="rev_total_input", measure="quantity", **common),
    ]
    issues = beam.finalize_issues()
    assert any("filters として記録されていません" in i for i in issues)
    assert any("rev_total_input は累計の元の指標" in i for i in issues)


@pytest.mark.parametrize("value", ["{{ env_var('X') }}", "50%", float("nan")])
def test_filter_values_are_validated_like_charts(setup, value) -> None:  # type: ignore[no-untyped-def]
    """生成する指標の filter も、チャートと同じ検査（#55）を通る（#110）。"""
    _, _, event, fact = setup
    f = MetricFilter.model_construct(
        ref="store.channel", operator=FilterOperator.EQUALS, values=[value]
    )
    with pytest.raises(SemanticError, match="filter の値に使えない"):
        filter_expr(event, fact, f)


def test_role_entities_go_to_derived_semantics() -> None:
    """role 付きディメンションの entity は列ではなく model の derived_semantics に書く（#134）。"""
    beam = load(FIXTURE)
    event = beam.events[0]
    customer = event.dimension("customer")
    assert customer is not None
    customer.role = "buyer"
    fact = DesignBuilder(FakeSchema()).build(beam).facts[0]
    key = next(c.name for c in fact.columns if c.source_ref == "buyer")
    assert "entity" not in fact_semantics(fact, event).get(key, {})
    entities = fact_model_semantics(fact, True, event)["derived_semantics"]["entities"]
    assert {"type": "foreign", "name": "buyer", "expr": key} in entities
    assert {"type": "foreign", "name": "date", "expr": fact.agg_time_column} in entities


def test_semi_additive_measure_takes_latest_date(setup) -> None:  # type: ignore[no-untyped-def]
    """semi_additive の measure は、粒度の entity ごとに最新の時点だけを集計する（#134）。"""
    from adp.beam import Additivity

    beam, _, event, _ = setup
    quantity = event.measure("quantity")
    quantity.additivity = Additivity.SEMI_ADDITIVE
    quantity.non_additive_dimension = "date"
    event.grain_keys = ["store", "product", "date"]
    design = DesignBuilder(FakeSchema()).build(beam)
    fact = design.facts[0]
    common = {"label": "x", "question": "q", "framed_question": "q", "event": fact.event}
    beam.metrics += [MetricRequest.model_validate(common | {"name": "qty", "measure": "quantity"})]
    defs = metric_definitions(beam, design, fact)
    qty = next(m for m in defs.in_model if m["name"] == "qty")
    assert qty["non_additive_dimension"] == {
        "name": fact.agg_time_column,
        "window_agg": "max",
        "group_by": ["product", "store"],
    }
    revenue = next(m for m in defs.in_model if m["name"] == "revenue")
    assert "non_additive_dimension" not in revenue

    beam.metrics[-1] = MetricRequest.model_validate(
        common | {"name": "qty", "type": "cumulative", "measure": "quantity", "window": "7 days"}
    )
    with pytest.raises(SemanticError, match="累計"):
        metric_definitions(beam, design, fact)
    beam.metrics.pop()
    quantity.non_additive_dimension = "store"
    beam.metrics += [MetricRequest.model_validate(common | {"name": "qty", "measure": "quantity"})]
    with pytest.raises(SemanticError, match="agg_time"):
        metric_definitions(beam, design, fact)


def test_purpose_goes_to_metric_description(setup) -> None:  # type: ignore[no-untyped-def]
    """聞いた目的を指標の説明に残す（#159）。無ければ言い換えた問いだけ。"""
    beam, design, _, fact = setup
    before = metric_definitions(beam, design, fact).in_model[0]["description"]
    assert before == beam.metrics[0].framed_question
    beam.metrics[0].purpose = "売上の伸びない店舗に販促をかける"
    after = metric_definitions(beam, design, fact).in_model[0]["description"]
    assert after == f"{before}\n目的: 売上の伸びない店舗に販促をかける"
