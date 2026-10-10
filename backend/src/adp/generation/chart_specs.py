"""BEAM の指標要求から、既定のチャートの元ファイルを作る（#42）。

置き場所は `warehouse/chart_specs/<beam>.yml`。

SQL は書かない。ボードへの書き出しと検査は `adp.charts`（ADR 0002）が行う。

- 時間の切り口がある → 折れ線（x: 時間、color: 最初のカテゴリの切り口）
- 比率で時間の切り口が無い → 月別の折れ線にする（比率は推移で見ることが多いため）
- カテゴリの切り口だけ → 棒（x: 最初の切り口、color: 2 つ目の切り口。1 つなら上位 20 件）
- 切り口が無い → KPI
"""

from __future__ import annotations

import re
from typing import Any

from adp.beam import BeamModel, MetricRequest, MetricType
from adp.generation.semantic import MetricQuery

_TIME = re.compile(r"__(day|week|month|quarter|year)$")
BAR_LIMIT = 20


def _text(value: str) -> str:
    """タイトル・ラベルに使えない文字（テンプレートや HTML になりうる < > { }）を除く。"""
    return re.sub(r"[<>{}]", "", value).strip()


def _chart(m: MetricRequest, group_by: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    times = [g for g in group_by if _TIME.search(g)]
    categories = [g for g in group_by if g not in times]
    query: dict[str, Any] = {"metrics": [m.name]}
    chart: dict[str, Any] = {"query": m.name, "title": _text(m.label), "y_label": _text(m.label)}
    if not times and m.type == MetricType.RATIO and categories:
        times = ["metric_time__month"]
    if times:
        x, color = times[0], categories[:1]
        query["group_by"] = [x, *color]
        query["order"] = [x]
        chart |= {"type": "line", "x": x, "y": m.name}
        if color:
            chart["color"] = color[0]
    elif categories:
        x, color = categories[0], categories[1:2]
        query["group_by"] = [x, *color]
        chart |= {"type": "bar", "x": x, "y": m.name}
        if color:
            chart["color"] = color[0]
        else:
            query["order"] = [f"-{m.name}"]
            query["limit"] = BAR_LIMIT
            chart["sort"] = {"by": m.name, "order": "desc"}
    else:
        chart = {"type": "kpi", "query": m.name, "title": _text(m.label), "value": m.name}
    return query, chart


def chart_spec(beam: BeamModel, queries: list[MetricQuery]) -> dict[str, Any] | None:
    """確定した指標ごとに 1 つのチャートを持つボードの元ファイル。指標が無ければ None。"""
    by_name = {q.metric: q for q in queries}
    metric_queries: dict[str, Any] = {}
    charts: dict[str, Any] = {}
    for m in beam.metrics:
        q = by_name.get(m.name)
        if q is None:
            continue
        metric_queries[m.name], charts[m.name] = _chart(m, q.group_by)
    if not charts:
        return None
    return {
        "title": _text(beam.title) or beam.name,
        "metric_queries": metric_queries,
        "charts": charts,
        "rows": list(charts),
    }
