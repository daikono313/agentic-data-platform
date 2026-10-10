"""物理設計と BEAM の指標要求から、MetricFlow（latest spec）の定義を生成する。

規約は docs/modeling-conventions.md の「Semantic Layer」と
「BEAM の group_by → MetricFlow の参照名」。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adp.beam import (
    Aggregation,
    BeamModel,
    BusinessEvent,
    FilterOperator,
    MetricFilter,
    MetricRequest,
    MetricType,
    W,
)
from adp.design import ColumnRole, DimTable, FactTable, PhysicalDesign
from adp.design.builder import is_masterless

_AGG = {
    Aggregation.SUM: "sum",
    Aggregation.COUNT: "count",
    Aggregation.COUNT_DISTINCT: "count_distinct",
    Aggregation.AVERAGE: "average",
    Aggregation.MIN: "min",
    Aggregation.MAX: "max",
}


class SemanticError(Exception):
    """BEAM の参照を MetricFlow の名前に変換できない。"""


@dataclass
class MetricQuery:
    """生成後に実行して確認するクエリ（元の要求そのもの）。"""

    metric: str
    group_by: list[str]


def _fact_column_for(fact: FactTable, beam_ref: str) -> str | None:
    col = next((c for c in fact.columns if c.source_ref == beam_ref), None)
    return col.name if col else None


def mf_name(event: BusinessEvent, fact: FactTable, ref: str) -> str:
    """BEAM の参照（group_by / filter）を MetricFlow の名前に変換する。"""
    pe = fact.primary_entity
    if event.degenerate(ref):
        return f"{pe}__{ref}"
    dim_ref, _, attr = ref.partition(".")
    d = event.dimension(dim_ref)
    if d is None:
        raise SemanticError(f"{ref} がイベントにありません")
    if d.w == W.WHEN:
        grain = attr or (d.time_grain.value if d.time_grain else "day")
        time = event.time_dimension
        if time is not None and time.ref == d.ref:
            return f"metric_time__{grain}"
        col = next(
            (c.name for c in fact.columns if c.source_ref == d.ref and c.role == ColumnRole.TIME),
            None,
        )
        if col is None:
            raise SemanticError(f"{ref} の日付列が {fact.name} にありません")
        return f"{pe}__{col}__{grain}"
    if is_masterless(d):
        return f"{pe}__{d.ref}"
    if not attr:
        raise SemanticError(f"{ref} には属性名が必要です")
    return f"{d.ref}__{attr}"


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def filter_expr(event: BusinessEvent, fact: FactTable, f: MetricFilter) -> str:
    dim = "{{ Dimension('" + mf_name(event, fact, f.ref) + "') }}"
    values = [_literal(v) for v in f.values]
    if f.operator == FilterOperator.EQUALS:
        return f"{dim} = {values[0]}"
    if f.operator == FilterOperator.NOT_EQUALS:
        return f"{dim} <> {values[0]}"
    op = "in" if f.operator == FilterOperator.IN else "not in"
    return f"{dim} {op} ({', '.join(values)})"


def _filters(event: BusinessEvent, fact: FactTable, m: MetricRequest) -> str | None:
    exprs = [filter_expr(event, fact, f) for f in m.filters]
    return " and ".join(exprs) if exprs else None


def fact_semantics(fact: FactTable, event: BusinessEvent) -> dict[str, dict[str, Any]]:
    """fct の列ごとに足す semantic の設定（列名 → 追加するキー）。"""
    result: dict[str, dict[str, Any]] = {}
    for c in fact.columns:
        if c.role == ColumnRole.FOREIGN_KEY and c.source_ref:
            d = event.dimension(c.source_ref)
            name = d.ref if d else c.name.removesuffix("_key")
            result[c.name] = {"entity": {"type": "foreign", "name": name}}
        elif c.role == ColumnRole.DEGENERATE:
            if fact.grain_columns == [c.name]:
                result[c.name] = {"entity": {"type": "primary", "name": fact.primary_entity}}
            else:
                result[c.name] = {"dimension": {"type": "categorical"}}
        elif c.role == ColumnRole.TIME:
            result[c.name] = {"granularity": "day", "dimension": {"type": "time"}}
    return result


def fact_model_semantics(fact: FactTable, has_date_entity: bool) -> dict[str, Any]:
    """fct のモデル単位で足す設定。"""
    model: dict[str, Any] = {"semantic_model": {"enabled": True}}
    if fact.agg_time_column:
        model["agg_time_dimension"] = fact.agg_time_column
        if has_date_entity:
            model["derived_semantics"] = {
                "entities": [{"type": "foreign", "name": "date", "expr": fact.agg_time_column}]
            }
    if len(fact.grain_columns) != 1 or fact.grain_columns[0] not in {
        c.name for c in fact.columns if c.role == ColumnRole.DEGENERATE
    }:
        model["primary_entity"] = fact.primary_entity
    return model


def dim_semantics(dim: DimTable) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """生成する dim の (モデル単位の設定, 列ごとの設定)。"""
    model: dict[str, Any] = {"semantic_model": {"enabled": True}}
    if dim.roles:
        model["derived_semantics"] = {
            "entities": [{"type": "unique", "name": r, "expr": dim.key_column} for r in dim.roles]
        }
    columns: dict[str, dict[str, Any]] = {
        dim.key_column: {"entity": {"type": "primary", "name": dim.dimension}}
    }
    for c in dim.columns:
        if c.role == ColumnRole.ATTRIBUTE:
            columns[c.name] = {"dimension": {"type": "categorical"}}
    return model, columns


@dataclass
class MetricDefinitions:
    in_model: list[dict[str, Any]]
    """fct のモデル内に書く simple metric。"""
    top_level: list[dict[str, Any]]
    """ratio / derived / cumulative。"""
    queries: list[MetricQuery]


def metric_definitions(
    beam: BeamModel, design: PhysicalDesign, fact: FactTable
) -> MetricDefinitions:
    event = beam.event(fact.event)
    assert event is not None
    in_model: list[dict[str, Any]] = []
    top: list[dict[str, Any]] = []
    queries: list[MetricQuery] = []
    for m in [m for m in beam.metrics if m.event == fact.event]:
        base: dict[str, Any] = {
            "name": m.name,
            "label": m.label,
            "description": m.framed_question,
        }
        flt = _filters(event, fact, m)
        if m.type in (MetricType.SIMPLE, MetricType.CUMULATIVE):
            measure = event.measure(m.measure or "")
            if measure is None or fact.column(measure.name) is None:
                raise SemanticError(
                    f"metric {m.name} の measure {m.measure} が {fact.name} にありません"
                )
            simple: dict[str, Any] = {
                "type": "simple",
                "agg": _AGG[measure.aggregation],
                "expr": measure.name,
            }
            if flt:
                simple["filter"] = flt
            if m.type == MetricType.SIMPLE:
                in_model.append(base | simple)
            else:
                input_name = f"{m.name}_input"
                in_model.append({"name": input_name, "label": f"{m.label}（累計の元）"} | simple)
                cumulative: dict[str, Any] = base | {
                    "type": "cumulative",
                    "input_metric": input_name,
                    "period_agg": "last",
                }
                if m.window:
                    cumulative["window"] = m.window
                top.append(cumulative)
        elif m.type == MetricType.RATIO:
            top.append(
                base | {"type": "ratio", "numerator": m.numerator, "denominator": m.denominator}
            )
        elif m.type == MetricType.DERIVED:
            assert m.expr is not None
            inputs = sorted(set(re.findall(r"[a-z_][a-z0-9_]*", m.expr)))
            top.append(
                base
                | {
                    "type": "derived",
                    "expr": m.expr,
                    "input_metrics": [{"name": n} for n in inputs],
                }
            )
        group_by = [mf_name(event, fact, ref) for ref in m.group_by]
        if m.type == MetricType.CUMULATIVE and not any(
            g.startswith("metric_time") for g in group_by
        ):
            group_by.insert(0, "metric_time__day")
        queries.append(MetricQuery(m.name, group_by))
    return MetricDefinitions(in_model, top, queries)


def run_query(
    warehouse: Path, mf: Path, q: MetricQuery, limit: int = 5
) -> subprocess.CompletedProcess[str]:
    """生成した指標を元の要求どおりにクエリする（dbt parse 済みであること）。"""
    import os

    args = [str(mf), "query", "--metrics", q.metric, "--limit", str(limit)]
    if q.group_by:
        args += ["--group-by", ",".join(q.group_by)]
    env = os.environ | {"DBT_PROFILES_DIR": str(warehouse)}
    return subprocess.run(args, cwd=warehouse, env=env, capture_output=True, text=True, check=False)
