from pathlib import Path

import pytest

from adp.beam import FilterOperator, MetricFilter, MetricRequest, MetricType, load
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
        MetricRequest(name="qty", measure="quantity", **common),
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
    assert top["rev_7d"] | {} == top["rev_7d"] and top["rev_7d"]["window"] == "7 days"
    assert top["rev_7d"]["period_agg"] == "last" and top["rev_7d"]["input_metric"] == "rev_7d_input"
    assert top["diff"]["input_metrics"] == [{"name": "qty"}, {"name": "revenue"}]
    cumulative_query = next(q for q in defs.queries if q.metric == "rev_7d")
    assert cumulative_query.group_by == ["metric_time__day", "store__channel"]
    assert beam.metrics[1].type == MetricType.SIMPLE


def test_ratio_and_derived_keep_filters(setup) -> None:  # type: ignore[no-untyped-def]
    beam, design, _, fact = setup
    common = {"label": "x", "question": "q", "framed_question": "q", "event": fact.event}
    beam.metrics += [
        MetricRequest(name="qty", measure="quantity", **common),
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
    assert top["aov_online"]["filter"] == "{{ Dimension('store__channel') }} in ('online', 1)"
    assert top["diff"]["filter"] == "{{ Dimension('store__channel') }} = true"


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
    """semi_additive の measure は、足せない When の最新の時点だけを集計する（#134）。"""
    from adp.beam import Additivity

    beam, design, event, fact = setup
    quantity = event.measure("quantity")
    quantity.additivity = Additivity.SEMI_ADDITIVE
    quantity.non_additive_dimension = "date"
    common = {"label": "x", "question": "q", "framed_question": "q", "event": fact.event}
    beam.metrics += [MetricRequest(name="qty", measure="quantity", **common)]
    defs = metric_definitions(beam, design, fact)
    qty = next(m for m in defs.in_model if m["name"] == "qty")
    assert qty["non_additive_dimension"] == {"name": fact.agg_time_column, "window_agg": "max"}
    revenue = next(m for m in defs.in_model if m["name"] == "revenue")
    assert "non_additive_dimension" not in revenue

    quantity.non_additive_dimension = "store"
    with pytest.raises(SemanticError, match="足せない When"):
        metric_definitions(beam, design, fact)
