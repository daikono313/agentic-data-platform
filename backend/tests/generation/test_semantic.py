from pathlib import Path

import pytest

from adp.beam import FilterOperator, MetricFilter, MetricRequest, MetricType, load
from adp.design import DesignBuilder
from adp.generation.semantic import SemanticError, filter_expr, metric_definitions, mf_name
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
