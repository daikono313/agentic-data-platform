"""確定（finalize）前の検証。インタビュー中の未完成な成果物は許容し、確定時にだけ厳しく見る。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import ValidationError

from adp.beam.models import (
    Additivity,
    Aggregation,
    Availability,
    GapKind,
    MetricType,
    TimeGrain,
    W,
)

if TYPE_CHECKING:
    from adp.beam.models import BeamModel, BusinessEvent

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
                names = set(re.findall(r"[a-z_][a-z0-9_]*", metric.expr))
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
        for ref in metric.group_by:
            kind = resolve_ref(event, ref)
            if kind is None or kind == "measure":
                issues.append(f"{p} group_by {ref} がイベントのディメンションに存在しません")
            elif kind == "attribute" and _attribute_unavailable(event, ref):
                issues.append(f"{p} group_by {ref} はデータなしです")
    return issues


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
