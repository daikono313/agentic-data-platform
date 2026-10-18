"""確定（finalize）前の検証。インタビュー中の未完成な成果物は許容し、確定時にだけ厳しく見る。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import ValidationError

from adp.beam.models import (
    Additivity,
    Aggregation,
    Availability,
    FactType,
    GapKind,
    MetricType,
    TimeGrain,
    W,
)

if TYPE_CHECKING:
    from adp.beam.models import BeamModel, BusinessEvent, MetricRequest

TIME_GRAINS = {g.value for g in TimeGrain}
# 集計方法ごとに、宣言できる加法性
_ADDITIVITY_BY_AGG = {
    Aggregation.AVERAGE: {Additivity.NON_ADDITIVE},
    Aggregation.COUNT_DISTINCT: {Additivity.NON_ADDITIVE},
    Aggregation.MIN: {Additivity.NON_ADDITIVE, Additivity.SEMI_ADDITIVE},
    Aggregation.MAX: {Additivity.NON_ADDITIVE, Additivity.SEMI_ADDITIVE},
}
_DERIVED_EXPR = re.compile(r"^[a-z0-9_\s+\-*/().]+$")


def _revalidate(model: BeamModel) -> list[str]:
    try:
        type(model).model_validate(model.model_dump())
    except ValidationError as e:
        return [f"構造エラー: {err['loc']}: {err['msg']}" for err in e.errors()]
    return []


def resolve_ref(event: BusinessEvent, ref: str) -> str | None:
    """group_by / 具体例のキーを解決する。解決できれば種類を、できなければ None を返す。

    - measure 名 → "measure"
    - 縮退ディメンション名 → "degenerate"
    - `<dim ref>.<attribute>` → "attribute"
    - `<When ref>` / `<When ref>.<time grain>` → "time"
    """
    if event.measure(ref):
        return "measure"
    if event.degenerate(ref):
        return "degenerate"
    dim_ref, _, attr = ref.partition(".")
    dim = event.dimension(dim_ref)
    if dim is None:
        return None
    if dim.w == W.WHEN and (not attr or attr in TIME_GRAINS):
        return "time"
    if attr and any(a.name == attr for a in dim.attributes):
        return "attribute"
    return None


def _attribute_unavailable(event: BusinessEvent, ref: str) -> bool:
    dim_ref, _, attr = ref.partition(".")
    dim = event.dimension(dim_ref)
    attribute = next((a for a in dim.attributes if a.name == attr), None) if dim else None
    return attribute is not None and attribute.availability == Availability.UNAVAILABLE


def _event_issues(event: BusinessEvent, model: BeamModel) -> list[str]:
    p = f"[{event.name}]"
    issues: list[str] = []
    if not event.grain or not event.grain_keys:
        issues.append(f"{p} 粒度（grain と grain_keys）が決まっていません")
    for key in event.grain_keys:
        if not (event.degenerate(key) or event.dimension(key)):
            issues.append(
                f"{p} grain_keys の {key} が縮退ディメンションにも dimension にもありません"
            )
    if not event.measures:
        issues.append(f"{p} How many（measure）がありません")
    when = [d for d in event.dimensions if d.w == W.WHEN]
    if not when:
        issues.append(f"{p} When のディメンションがありません")
    for d in when:
        if d.time_grain is None:
            issues.append(f"{p} When {d.ref} の最小粒度（time_grain）が決まっていません")
    if len(when) > 1 and event.agg_time_dimension is None:
        issues.append(f"{p} When が複数あるので agg_time_dimension を決める必要があります")
    if event.agg_time_dimension and (
        (t := event.dimension(event.agg_time_dimension)) is None or t.w != W.WHEN
    ):
        issues.append(f"{p} agg_time_dimension {event.agg_time_dimension} が When にありません")
    if event.priority is None:
        issues.append(f"{p} 優先度が決まっていません")
    if not event.examples:
        issues.append(f"{p} 具体例がありません（1 件以上聞いて examples に記録してください）")
    if event.table_name is None:
        issues.append(f"{p} fct のテーブル名（table_name）が決まっていません")

    gaps = {
        g.related
        for g in model.gaps
        if g.kind == GapKind.DATA_UNAVAILABLE and g.event in (None, event.name)
    }
    for m in event.measures:
        if m.availability == Availability.UNKNOWN:
            issues.append(f"{p} measure {m.name} がソースで作れるか未確認です")
        if m.availability == Availability.AVAILABLE and m.source is None:
            issues.append(f"{p} measure {m.name} のソースが指定されていません")
        if m.availability == Availability.UNAVAILABLE and m.name not in gaps:
            issues.append(f"{p} measure {m.name} はデータなしですが gap に記録されていません")
        if m.additivity == Additivity.NON_ADDITIVE and m.aggregation == Aggregation.SUM:
            issues.append(f"{p} measure {m.name} は足し上げできないのに sum で集計しています")
        allowed = _ADDITIVITY_BY_AGG.get(m.aggregation)
        if allowed and m.additivity not in allowed:
            issues.append(
                f"{p} measure {m.name} は {m.aggregation} なので {m.additivity} にはできません"
            )
        if m.additivity == Additivity.SEMI_ADDITIVE:
            nad = event.dimension(m.non_additive_dimension) if m.non_additive_dimension else None
            if nad is None or nad.w != W.WHEN:
                issues.append(
                    f"{p} measure {m.name} は semi_additive なので、足せない When"
                    "（non_additive_dimension）を指定してください"
                )
            elif event.time_dimension is None or nad.ref != event.time_dimension.ref:
                issues.append(
                    f"{p} measure {m.name} の足せない When は、指標の時間軸"
                    "（agg_time_dimension）と同じにしてください"
                )
            if event.fact_type != FactType.PERIODIC_SNAPSHOT:
                issues.append(
                    f"{p} measure {m.name} は時点の値（semi_additive）なので、"
                    "定期的な状態の記録（story_type: recurring）のイベントに置いてください"
                )

    fact_keys: dict[str, set[str]] = {}
    for d in event.dimensions:
        if d.fact_key is None:
            issues.append(f"{p} {d.ref} のイベント側のキー（fact_key）が決まっていません")
        else:
            fact_keys.setdefault(d.name, set())
            key = d.fact_key.model_dump_json()
            if key in fact_keys[d.name]:
                issues.append(f"{p} {d.name} の役割どうしで fact_key が同じです（{d.ref}）")
            fact_keys[d.name].add(key)
        if d.w != W.WHEN and d.business_key is None:
            issues.append(f"{p} {d.ref} の自然キー（business_key）が決まっていません")
        for a in d.attributes:
            ref = f"{d.ref}.{a.name}"
            if a.availability == Availability.UNKNOWN:
                issues.append(f"{p} 属性 {ref} がソースで作れるか未確認です")
            if a.availability == Availability.AVAILABLE and a.source is None:
                issues.append(f"{p} 属性 {ref} のソースが指定されていません")
            if a.availability == Availability.UNAVAILABLE and ref not in gaps:
                issues.append(f"{p} 属性 {ref} はデータなしですが gap に記録されていません")

    for i, ex in enumerate(event.examples):
        for key in ex.values:
            if resolve_ref(event, key) is None:
                issues.append(f"{p} 具体例 {i + 1} のキー {key} がイベントにありません")
    return issues


def _metric_issues(model: BeamModel) -> list[str]:
    issues: list[str] = []
    metrics = {m.name: m for m in model.metrics}
    for metric in model.metrics:
        p = f"[metric {metric.name}]"
        event = model.event(metric.event)
        if event is None:
            issues.append(f"{p} イベント {metric.event} が存在しません")
            continue
        uses_measure = metric.type in (MetricType.SIMPLE, MetricType.CUMULATIVE)
        if uses_measure:
            measure = event.measure(metric.measure) if metric.measure else None
            if measure is None:
                issues.append(f"{p} measure {metric.measure} がイベントに存在しません")
            elif measure.availability == Availability.UNAVAILABLE:
                issues.append(f"{p} measure {measure.name} はデータなしのため作れません")
            elif (
                measure.additivity == Additivity.SEMI_ADDITIVE
                and metric.type == MetricType.CUMULATIVE
            ):
                issues.append(
                    f"{p} measure {measure.name} は時点の値（semi_additive）なので累計にできません"
                )
        if not uses_measure and metric.measure is not None:
            issues.append(f"{p} {metric.type} には measure を指定しません")
        if metric.type != MetricType.RATIO and (metric.numerator or metric.denominator):
            issues.append(f"{p} numerator / denominator は ratio のときだけ指定します")
        if metric.type == MetricType.RATIO:
            for side in (metric.numerator, metric.denominator):
                if side is None or side not in metrics or side == metric.name:
                    issues.append(f"{p} ratio の分子・分母 {side} が他の metric に存在しません")
        if metric.type == MetricType.DERIVED:
            if not metric.expr or not _DERIVED_EXPR.match(metric.expr):
                issues.append(f"{p} derived の expr は metric 名の四則演算にしてください")
            else:
                names = set(metric.inputs)
                unknown = names - set(metrics) or ({metric.name} & names)
                if unknown:
                    issues.append(
                        f"{p} derived の expr に使えない名前があります: {sorted(unknown)}"
                    )
        if metric.window and metric.type != MetricType.CUMULATIVE:
            issues.append(f"{p} window は cumulative のときだけ指定します")
        if any(resolve_ref(event, r) == "time" for r in metric.group_by) and (
            event.time_dimension is None
        ):
            issues.append(f"{p} 時間軸で集計するには agg_time_dimension が必要です")
        for f in metric.filters:
            if resolve_ref(event, f.ref) not in ("attribute", "degenerate"):
                issues.append(
                    f"{p} filter の {f.ref} がイベントの属性・縮退ディメンションにありません"
                )
        if metric.filter and not metric.filters:
            issues.append(f"{p} 絞り込み条件（{metric.filter}）が filters として記録されていません")
        if metric.type == MetricType.CUMULATIVE and f"{metric.name}_input" in metrics:
            issues.append(
                f"{p} 指標名 {metric.name}_input は累計の元の指標に使うため予約されています"
            )
        if metric.type in (MetricType.RATIO, MetricType.DERIVED):
            # 指標をまたぐ切り口・条件は、元になるすべての指標のイベントで解決できる必要がある
            # （元の指標がさらに比率・派生なら、その下の指標まで。MetricFlow は filter を下へ渡す。
            # #180）
            leaves, cycle = _leaf_inputs(metric, metrics)
            if cycle:
                issues.append(f"{p} 元の指標をたどると {metric.name} に戻ります（循環しています）")
            refs = [*metric.group_by, *(f.ref for f in metric.filters)]
            # 切り口は MetricFlow が同じ名前のまま元の指標へ渡すので、出来事をまたぐなら、
            # どの出来事でも同じ名前で解決できる必要がある（#181）
            events = {event.name: event}
            for via, name in leaves:
                src = metrics[name]
                src_event = model.event(src.event)
                if src_event is None or src_event.name == event.name:
                    continue
                events[src_event.name] = src_event
                where = name if via == name else f"{via} の元の {name}"
                for ref in refs:
                    if resolve_ref(src_event, ref) in (None, "measure"):
                        issues.append(
                            f"{p} {ref} は元の指標 {where} のイベント {src_event.name} にありません"
                        )
            unshared = [
                r
                for r in metric.group_by
                if len(events) > 1
                and all(resolve_ref(e, r) not in (None, "measure") for e in events.values())
                and not all(_shared_ref(e, r) for e in events.values())
            ]
            for ref in unshared:
                issues.append(
                    f"{p} 切り口 {ref} は出来事（{'・'.join(events)}）ごとに別々に記録される項目"
                    "なので、出来事をまたぐ指標の切り口に使えません。どの出来事にも共通する"
                    "ディメンション（商品など）の属性で切ってください"
                )
        for ref in metric.group_by:
            kind = resolve_ref(event, ref)
            if kind is None or kind == "measure":
                issues.append(f"{p} group_by {ref} がイベントのディメンションに存在しません")
            elif kind == "attribute" and _attribute_unavailable(event, ref):
                issues.append(f"{p} group_by {ref} はデータなしです")
    return issues


def _shared_ref(event: BusinessEvent, ref: str) -> bool:
    """出来事によらず同じ名前で解決できる参照か（マスタのあるディメンションの属性と、時間軸）。
    MetricFlow の名前への変換（`adp.generation.semantic.mf_name`）と判定をそろえる。"""
    kind = resolve_ref(event, ref)
    dim = event.dimension(ref.partition(".")[0])
    if kind == "attribute":
        return dim is not None and dim.w != W.WHEN and not dim.masterless
    if kind == "time":
        time = event.time_dimension
        return dim is not None and time is not None and time.ref == dim.ref
    return False


def _leaf_inputs(
    metric: MetricRequest, metrics: dict[str, MetricRequest]
) -> tuple[list[tuple[str, str]], bool]:
    """比率・派生の入れ子をたどった先の、measure を集計する指標（MetricFlow が切り口・絞り込みを
    解決する指標）を、（直接の元の指標, その指標）で。2 つ目は、たどると自分に戻るか（循環）。"""
    leaves: list[tuple[str, str]] = []
    seen: set[str] = set()
    cycle = False
    todo = [(n, n) for n in metric.inputs]
    while todo:
        via, name = todo.pop()
        src = metrics.get(name)
        if name == metric.name:
            cycle = True
        if name in seen or name == metric.name or src is None:
            continue
        seen.add(name)
        if src.type in (MetricType.RATIO, MetricType.DERIVED):
            todo += [(via, n) for n in src.inputs]
        else:
            leaves.append((via, name))
    return leaves, cycle


def _conformance_issues(model: BeamModel) -> list[str]:
    """同じ名前のディメンションが、イベントをまたいで食い違っていないか。"""
    first: dict[str, tuple[str, W, str]] = {}
    issues: list[str] = []
    for event in model.events:
        for d in event.dimensions:
            bk = d.business_key.model_dump_json() if d.business_key else ""
            if d.name not in first:
                first[d.name] = (event.name, d.w, bk)
                continue
            src, w, first_bk = first[d.name]
            if w != d.w:
                issues.append(f"ディメンション {d.name} が {w} と {d.w} の両方で使われています")
            if bk != first_bk:
                issues.append(
                    f"ディメンション {d.name} の自然キーが {src} と {event.name} で異なります"
                )
    return issues


def _gap_issues(model: BeamModel) -> list[str]:
    names = {e.name for e in model.events}
    issues = [
        f"gap のイベント {g.event} が存在しません"
        for g in model.gaps
        if g.event is not None and g.event not in names
    ]
    blocking = [g for g in model.gaps if g.kind in (GapKind.UNANSWERED, GapKind.CONFLICT)]
    return issues + [f"未解決: {g.description}" for g in blocking]


def finalize_issues(model: BeamModel) -> list[str]:
    structural = _revalidate(model)
    if structural:
        return structural
    issues: list[str] = []
    if not model.events:
        issues.append("ビジネスイベントがありません")
    if not model.metrics:
        issues.append("指標（metric）がありません")
    for event in model.events:
        issues += _event_issues(event, model)
    issues += _metric_issues(model)
    issues += _conformance_issues(model)
    issues += _gap_issues(model)
    return issues
