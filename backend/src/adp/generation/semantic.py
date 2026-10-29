"""物理設計と BEAM の指標要求から、MetricFlow（latest spec）の定義を生成する。

規約は docs/modeling-conventions.md の「Semantic Layer」と
「BEAM の group_by → MetricFlow の参照名」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from adp.beam import (
    Additivity,
    Aggregation,
    BeamModel,
    BusinessEvent,
    Measure,
    MetricFilter,
    MetricRequest,
    MetricType,
    W,
)
from adp.design import ColumnRole, DimTable, FactTable, PhysicalDesign
from adp.metricflow import condition

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


def _time_column(fact: FactTable, ref: str) -> str | None:
    """BEAM の When の参照に対応する fct の日付列。"""
    col = fact.column_for(ref, ColumnRole.TIME)
    return col.name if col else None


def mf_name(event: BusinessEvent, fact: FactTable, ref: str) -> str:
    """BEAM の参照（group_by / filter）を MetricFlow の名前に変換する。

    出来事によらず同じ名前になるかの判定は `adp.beam.validation._shared_ref` とそろえる。"""
    pe = fact.primary_entity
    if event.degenerate(ref):
        # 粒度のキー（主 entity）は entity 名そのもので参照する
        return pe if fact.grain_columns == [ref] else f"{pe}__{ref}"
    dim_ref, _, attr = ref.partition(".")
    d = event.dimension(dim_ref)
    if d is None:
        raise SemanticError(f"{ref} がイベントにありません")
    if d.w == W.WHEN:
        grain = attr or (d.time_grain.value if d.time_grain else "day")
        time = event.time_dimension
        if time is not None and time.ref == d.ref:
            return f"metric_time__{grain}"
        col = _time_column(fact, d.ref)
        if col is None:
            raise SemanticError(f"{ref} の日付列が {fact.name} にありません")
        return f"{pe}__{col}__{grain}"
    if d.masterless:
        return pe if fact.grain_columns == [d.ref] else f"{pe}__{d.ref}"
    if not attr:
        raise SemanticError(f"{ref} には属性名が必要です")
    return f"{d.ref}__{attr}"


def filter_expr(event: BusinessEvent, fact: FactTable, f: MetricFilter) -> str:
    dim = "{{ Dimension('" + mf_name(event, fact, f.ref) + "') }}"
    try:
        return condition(dim, f.operator.value, list(f.values))
    except ValueError as e:
        raise SemanticError(str(e)) from e


def _filters(event: BusinessEvent, fact: FactTable, m: MetricRequest) -> str | None:
    exprs = [filter_expr(event, fact, f) for f in m.filters]
    return " and ".join(exprs) if exprs else None


def _input(
    beam: BeamModel, design: PhysicalDesign, m: MetricRequest, name: str, *, named: bool = False
) -> str | dict[str, Any]:
    """比率・派生の指標の元の指標。絞り込みは元の指標ごとに、その出来事の名前で書く。

    指標の filter は元の指標すべてに効くが、名前は 1 つの出来事でしか解決できない（返品の出来事の
    return__order_status は、注文の指標には無い。#178）。元の指標がさらに比率・派生なら、
    MetricFlow はこの名前のまま下の指標へ渡すので、たどった先の指標の出来事の名前で書く（途中の
    指標の置き場所は問わない）。参照がたどった先の出来事にあること、出来事が分かれるならどの
    出来事でも同じ名前になることは検証で確かめている（adp.beam.validation、#180・#184）。
    """
    if not m.filters:
        return {"name": name} if named else name
    src = next((x for x in beam.metrics if x.name == name), None)
    if src is not None and src.type in (MetricType.RATIO, MetricType.DERIVED):
        leaves = beam.leaf_inputs(src)[0]
        src = next((x for x in beam.metrics if leaves and x.name == leaves[0][1]), None)
    event = beam.event(src.event) if src else None
    fact = next((f for f in design.facts if event and f.event == event.name), None)
    if event is None or fact is None:
        raise SemanticError(f"metric {m.name} の元の指標 {name} の fact が見つかりません")
    return {"name": name, "filter": _filters(event, fact, m)}


def non_additive_dimension(
    event: BusinessEvent, fact: FactTable, measure: Measure
) -> dict[str, Any] | None:
    """semi_additive の measure（例: 残高）は、粒度の entity ごとに最新の時点の値だけを集計する。

    足せない When は agg_time の列に限る（metric_time の区間ごとに最新を取るため）。
    """
    if measure.additivity != Additivity.SEMI_ADDITIVE:
        return None
    col = _time_column(fact, measure.non_additive_dimension or "")
    if col is None or col != fact.agg_time_column:
        raise SemanticError(
            f"measure {measure.name} の足せない When（{measure.non_additive_dimension}）が"
            f" {fact.name} の agg_time の日付列ではありません"
        )
    group_by: list[str] = []
    for c in fact.columns:
        if c.name in fact.grain_columns and c.role == ColumnRole.FOREIGN_KEY and c.source_ref:
            d = event.dimension(c.source_ref)
            group_by.append(d.role or d.ref if d else c.name.removesuffix("_key"))
    nad: dict[str, Any] = {"name": col, "window_agg": "max"}
    if group_by:
        nad["group_by"] = group_by
    return nad


def fact_semantics(fact: FactTable, event: BusinessEvent) -> dict[str, dict[str, Any]]:
    """fct の列ごとに足す semantic の設定（列名 → 追加するキー）。"""
    result: dict[str, dict[str, Any]] = {}
    for c in fact.columns:
        if c.role == ColumnRole.FOREIGN_KEY and c.source_ref:
            d = event.dimension(c.source_ref)
            if d is not None and d.role:
                continue  # 役割の entity は model の derived_semantics に書く（role_entities）
            name = d.ref if d else c.name.removesuffix("_key")
            result[c.name] = {"entity": {"type": "foreign", "name": name}}
        elif c.role == ColumnRole.DEGENERATE:
            if fact.entity_column == c.name:
                result[c.name] = {"entity": {"type": "primary", "name": fact.primary_entity}}
            else:
                result[c.name] = {"dimension": {"type": "categorical"}}
        elif c.role == ColumnRole.TIME:
            result[c.name] = {"granularity": "day", "dimension": {"type": "time"}}
    return result


def role_entities(fact: FactTable, event: BusinessEvent) -> list[dict[str, Any]]:
    """role 付きディメンション（例: 配送先顧客 ship_to）の外部キーの entity。

    規約（「Semantic Layer」の role 付きディメンション）どおり、列ではなく derived_semantics に
    書く。
    """
    out: list[dict[str, Any]] = []
    for c in fact.columns:
        if c.role != ColumnRole.FOREIGN_KEY or not c.source_ref:
            continue
        d = event.dimension(c.source_ref)
        if d is not None and d.role:
            out.append({"type": "foreign", "name": d.role, "expr": c.name})
    return out


def fact_model_semantics(
    fact: FactTable, has_date_entity: bool, event: BusinessEvent | None = None
) -> dict[str, Any]:
    """fct のモデル単位で足す設定。"""
    model: dict[str, Any] = {"semantic_model": {"enabled": True}}
    entities = role_entities(fact, event) if event is not None else []
    if fact.allocation is not None and fact.allocation.source_entity is not None:
        # 按分した行から元の出来事の fct へ結合する（多対一。ADR 0010）
        (column,) = fact.allocation.source_grain
        entities.append({"type": "foreign", "name": fact.allocation.source_entity, "expr": column})
    if fact.agg_time_column:
        model["agg_time_dimension"] = fact.agg_time_column
        if has_date_entity:
            entities.append({"type": "foreign", "name": "date", "expr": fact.agg_time_column})
    if entities:
        model["derived_semantics"] = {"entities": entities}
    if fact.entity_column is None:
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


AT_EVENT_SUFFIX = "_at_event"
"""履歴モデルのディメンション名の接尾辞（当時の値。ADR 0009）。今の値の dim と同じ名前にすると、
entity の名前が同じなので両方とも問い合わせられなくなる（MetricFlow の検証では見つからない）。"""


def history_semantics(dim: DimTable) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """履歴モデル（ADR 0009）の (モデル単位の設定, 列ごとの設定)。

    dim と同じキーを natural の entity（名前は dim の primary の entity と同じ）にし、期間を
    validity_params にする。fct の外部の entity は、agg_time の日付がその期間に入る版に結合される。
    期間付きのモデルには primary の entity を置けないので、版の単位を primary_entity に書く。
    """
    assert dim.history is not None
    model: dict[str, Any] = {
        "semantic_model": {"enabled": True},
        "primary_entity": f"{dim.dimension}_version",
    }
    columns: dict[str, dict[str, Any]] = {
        dim.key_column: {"entity": {"type": "natural", "name": dim.dimension}},
        "valid_from": {
            "granularity": "day",
            "dimension": {"type": "time", "validity_params": {"is_start": True}},
        },
        "valid_to": {
            "granularity": "day",
            "dimension": {"type": "time", "validity_params": {"is_end": True}},
        },
    }
    for c in dim.history.columns:
        columns[c.name] = {"dimension": {"type": "categorical", "name": c.name + AT_EVENT_SUFFIX}}
    return model, columns


@dataclass
class MetricDefinitions:
    in_model: list[dict[str, Any]]
    """fct のモデル内に書く simple metric。"""
    top_level: list[dict[str, Any]]
    """ratio / derived / cumulative。"""
    queries: list[MetricQuery]


ROW_COUNT_EXPR = "1"
"""出来事の件数（行の数）の measure の式。列を持たず、行を数える（factless fact。#139）。"""


def measure_expr(measure: Measure) -> str:
    """simple 指標の expr（measure の列。出来事の件数なら行を数える式）。"""
    return ROW_COUNT_EXPR if measure.counts_rows else measure.name


_NOT_ALLOCATED = (Aggregation.COUNT_DISTINCT, Aggregation.MIN, Aggregation.MAX)
"""按分した fct でも重みを掛けない集計（ADR 0010）。"""


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
            "description": "\n".join(
                [m.framed_question]
                + ([f"目的: {m.purpose}"] if m.purpose else [])
                + [n for n in [beam.time_axis_note(m)] if n]
            ),
        }
        flt = _filters(event, fact, m)
        if m.type in (MetricType.SIMPLE, MetricType.CUMULATIVE):
            measure = event.measure(m.measure or "")
            if measure is None or (not measure.counts_rows and fact.column(measure.name) is None):
                raise SemanticError(
                    f"metric {m.name} の measure {m.measure} が {fact.name} にありません"
                )
            simple: dict[str, Any] = {
                "type": "simple",
                "agg": _AGG[measure.aggregation],
                "expr": measure_expr(measure),
            }
            if fact.allocation is not None:
                # 按分した fct（ADR 0010）: 件数は重みの合計。個数の数え直し・最小・最大は値ごとに
                # 足しても全体と一致しない（非加算）ことを説明に書く
                if measure.counts_rows:
                    simple |= {"agg": "sum", "expr": fact.allocation.weight_column}
                if measure.aggregation not in _NOT_ALLOCATED:
                    base["description"] += (
                        f"\n{fact.allocation.dimension} ごとの値は、"
                        + (
                            f"{fact.allocation.weight_source} の割合"
                            if fact.allocation.weight_source
                            else "付いた値の数"
                        )
                        + "で按分した値（値ごとに足すと全体と一致する）"
                    )
                else:
                    base["description"] += (
                        f"\n{fact.allocation.dimension} ごとの値は按分しない"
                        "（値ごとに足しても全体とは一致しない）"
                    )
            if flt:
                simple["filter"] = flt
            nad = non_additive_dimension(event, fact, measure)
            if nad and m.type == MetricType.CUMULATIVE:
                raise SemanticError(f"metric {m.name} は時点の値（semi_additive）の累計です")
            if nad:
                simple["non_additive_dimension"] = nad
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
            ratio = base | {
                "type": "ratio",
                "numerator": _input(beam, design, m, m.numerator or ""),
                "denominator": _input(beam, design, m, m.denominator or ""),
            }
            top.append(ratio)
        elif m.type == MetricType.DERIVED:
            assert m.expr is not None
            inputs = sorted(set(m.inputs))
            derived = base | {
                "type": "derived",
                "expr": m.expr,
                "input_metrics": [_input(beam, design, m, n, named=True) for n in inputs],
            }
            top.append(derived)
        group_by = [mf_name(event, fact, ref) for ref in m.group_by]
        if m.type == MetricType.CUMULATIVE and not any(
            g.startswith("metric_time") for g in group_by
        ):
            # 時間軸の記録の粒度で切る（それより細かくは切れない。#188）
            time = event.time_dimension
            grain = time.time_grain.value if time and time.time_grain else "day"
            group_by.insert(0, f"metric_time__{grain}")
        queries.append(MetricQuery(m.name, group_by))
    return MetricDefinitions(in_model, top, queries)
