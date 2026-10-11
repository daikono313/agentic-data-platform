"""BEAM の指標要求から既定のチャートを作る（#42）。"""

import pytest

from adp.beam import MetricType, load
from adp.charts import _validate_spec
from adp.generation.chart_specs import chart_spec
from adp.generation.semantic import MetricQuery
from tests.generation.test_dbt_codegen import FIXTURES

BEAM = load(FIXTURES / "beam" / "sales_by_store.yml")


def _one(group_by: list[str], type: MetricType = MetricType.SIMPLE, label: str = "売上金額"):  # type: ignore[no-untyped-def]
    m = BEAM.metrics[0].model_copy(update={"type": type, "label": label})
    beam = BEAM.model_copy(update={"metrics": [m]})
    spec = chart_spec(beam, [MetricQuery(m.name, group_by)])
    assert spec is not None
    _validate_spec("x", spec)  # adp.charts の許可リストを通る
    return spec["metric_queries"][m.name], spec["charts"][m.name]


@pytest.mark.parametrize(
    ("group_by", "type", "query", "chart"),
    [
        (
            ["metric_time__day", "store__store_name", "store__channel"],
            MetricType.SIMPLE,
            {"group_by": ["metric_time__day", "store__store_name"], "order": ["metric_time__day"]},
            {"type": "line", "x": "metric_time__day", "color": "store__store_name"},
        ),
        (
            ["order__ordered_at__month"],
            MetricType.SIMPLE,
            {"group_by": ["order__ordered_at__month"], "order": ["order__ordered_at__month"]},
            {"type": "line", "x": "order__ordered_at__month"},
        ),
        (
            ["store__store_name"],
            MetricType.SIMPLE,
            {"group_by": ["store__store_name"], "order": ["-revenue"], "limit": 20},
            {"type": "bar", "x": "store__store_name", "sort": {"by": "revenue", "order": "desc"}},
        ),
        (
            ["store__store_name", "store__channel"],
            MetricType.SIMPLE,
            {"group_by": ["store__store_name", "store__channel"]},
            {"type": "bar", "x": "store__store_name", "color": "store__channel"},
        ),
        (
            ["store__channel"],
            MetricType.RATIO,
            {"group_by": ["metric_time__month", "store__channel"], "order": ["metric_time__month"]},
            {"type": "line", "x": "metric_time__month", "color": "store__channel"},
        ),
        ([], MetricType.SIMPLE, {}, {"type": "kpi", "value": "revenue", "label": "売上金額"}),
        # 属性名が year でもカテゴリとして扱う
        (
            ["store__year"],
            MetricType.SIMPLE,
            {"group_by": ["store__year"], "order": ["-revenue"], "limit": 20},
            {"type": "bar", "x": "store__year"},
        ),
        # 粒度のキー（entity 名だけ）や累計
        (
            ["order"],
            MetricType.SIMPLE,
            {"group_by": ["order"], "order": ["-revenue"], "limit": 20},
            {"type": "bar", "x": "order"},
        ),
        (
            ["metric_time__day"],
            MetricType.CUMULATIVE,
            {"group_by": ["metric_time__day"], "order": ["metric_time__day"]},
            {"type": "line", "x": "metric_time__day"},
        ),
    ],
)
def test_default_chart(
    group_by: list[str], type: MetricType, query: dict[str, object], chart: dict[str, object]
) -> None:
    q, c = _one(group_by, type)
    assert q["metrics"] == ["revenue"]
    assert {k: v for k, v in q.items() if k != "metrics"} == query
    assert {k: v for k, v in c.items() if k in chart} == chart


def test_labels_cannot_inject_templates() -> None:
    _, c = _one(["store__store_name"], label="<b>売上</b>{{ 7*7 }}")
    assert c["title"] == "b売上/b 7*7"


def test_no_chart_without_queries() -> None:
    assert chart_spec(BEAM, []) is None


def test_line_chart_drops_series_for_many_values() -> None:
    """値の種類が多い切り口では、折れ線を色分けしない（系列が多すぎて読めないため。#79）。"""
    from adp.generation.chart_specs import LINE_SERIES_MAX, source_of

    m = BEAM.metrics[0]
    beam = BEAM.model_copy(update={"metrics": [m]})
    query = [MetricQuery(m.name, ["metric_time__month", "store__store_name"])]
    few = chart_spec(beam, query, lambda ref: 5)
    many = chart_spec(beam, query, lambda ref: LINE_SERIES_MAX + 1)
    unknown = chart_spec(beam, query, lambda ref: None)
    assert few and many and unknown
    assert few["charts"][m.name]["color"] == "store__store_name"
    assert "color" not in many["charts"][m.name]
    assert many["metric_queries"][m.name]["group_by"] == ["metric_time__month"]
    assert unknown["charts"][m.name]["color"] == "store__store_name"
    assert source_of(BEAM, "store__store_name") == ("raw.stores", "store_name")
    assert source_of(BEAM, "nope__x") is None
