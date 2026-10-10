"""確定（finalize）前の検証。インタビュー中の未完成な成果物は許容し、確定時にだけ厳しく見る。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adp.beam.models import Additivity, Aggregation, Availability, GapKind, MetricType, W

if TYPE_CHECKING:
    from adp.beam.models import BeamModel, BusinessEvent


def _event_issues(event: BusinessEvent, model: BeamModel) -> list[str]:
    p = f"[{event.name}]"
    issues: list[str] = []
    if not event.grain or not event.grain_keys:
        issues.append(f"{p} 粒度（grain と grain_keys）が決まっていません")
    if not event.measures:
        issues.append(f"{p} How many（measure）がありません")
    when = [d for d in event.dimensions if d.w == W.WHEN]
    if not when:
        issues.append(f"{p} When のディメンションがありません")
    elif all(d.time_grain is None for d in when):
        issues.append(f"{p} When の最小粒度（time_grain）が決まっていません")
    if event.priority is None:
        issues.append(f"{p} 優先度が決まっていません")

    recorded_gaps = {g.related for g in model.gaps if g.kind == GapKind.DATA_UNAVAILABLE}
    for m in event.measures:
        if m.availability == Availability.UNKNOWN:
            issues.append(f"{p} measure {m.name} がソースで作れるか未確認です")
        if m.availability == Availability.AVAILABLE and m.source is None:
            issues.append(f"{p} measure {m.name} のソースが指定されていません")
        if m.additivity == Additivity.NON_ADDITIVE and m.aggregation == Aggregation.SUM:
            issues.append(f"{p} measure {m.name} は足し上げできないのに sum で集計しています")
        if m.availability == Availability.UNAVAILABLE and m.name not in recorded_gaps:
            issues.append(f"{p} measure {m.name} はデータなしですが gap に記録されていません")
    for d in event.dimensions:
        for a in d.attributes:
            ref = f"{d.ref}.{a.name}"
            if a.availability == Availability.UNKNOWN:
                issues.append(f"{p} 属性 {ref} がソースで作れるか未確認です")
            if a.availability == Availability.AVAILABLE and a.source is None:
                issues.append(f"{p} 属性 {ref} のソースが指定されていません")
            if a.availability == Availability.UNAVAILABLE and ref not in recorded_gaps:
                issues.append(f"{p} 属性 {ref} はデータなしですが gap に記録されていません")
    for i, ex in enumerate(event.examples):
        for key in ex.values:
            dim_ref, _, attr = key.partition(".")
            dim = event.dimension(dim_ref)
            known = event.measure(key) is not None or (
                dim is not None and (not attr or any(a.name == attr for a in dim.attributes))
            )
            if not known:
                issues.append(f"{p} 具体例 {i + 1} のキー {key} が measure にも属性にもありません")
    return issues


def _metric_issues(model: BeamModel) -> list[str]:
    issues: list[str] = []
    metric_names = {m.name for m in model.metrics}
    for metric in model.metrics:
        p = f"[metric {metric.name}]"
        event = model.event(metric.event)
        if event is None:
            issues.append(f"{p} イベント {metric.event} が存在しません")
            continue
        if metric.type in (MetricType.SIMPLE, MetricType.CUMULATIVE):
            measure = event.measure(metric.measure) if metric.measure else None
            if measure is None:
                issues.append(f"{p} measure {metric.measure} がイベントに存在しません")
            elif measure.availability == Availability.UNAVAILABLE:
                issues.append(f"{p} measure {measure.name} はデータなしのため作れません")
        if metric.type == MetricType.RATIO:
            for side in (metric.numerator, metric.denominator):
                if side is None or side not in metric_names:
                    issues.append(f"{p} ratio の分子・分母 {side} が metric に存在しません")
        if metric.type == MetricType.DERIVED and not metric.expr:
            issues.append(f"{p} derived には expr が必要です")
        for ref in metric.group_by:
            dim_ref, _, attr = ref.partition(".")
            dim = event.dimension(dim_ref)
            attribute = next((a for a in dim.attributes if a.name == attr), None) if dim else None
            if attribute is None:
                issues.append(f"{p} group_by {ref} がイベントのディメンションに存在しません")
            elif attribute.availability == Availability.UNAVAILABLE:
                issues.append(f"{p} group_by {ref} はデータなしです")
    return issues


def _conformance_issues(model: BeamModel) -> list[str]:
    """同じ名前のディメンションが、イベントによって別の W として扱われていないか。"""
    seen: dict[str, W] = {}
    issues: list[str] = []
    for event in model.events:
        for d in event.dimensions:
            if d.name in seen and seen[d.name] != d.w:
                issues.append(
                    f"ディメンション {d.name} が {seen[d.name]} と {d.w} の両方で使われています"
                )
            seen.setdefault(d.name, d.w)
    return issues


def finalize_issues(model: BeamModel) -> list[str]:
    issues: list[str] = []
    if not model.events:
        issues.append("ビジネスイベントがありません")
    if not model.metrics:
        issues.append("指標（metric）がありません")
    for event in model.events:
        issues += _event_issues(event, model)
    issues += _metric_issues(model)
    issues += _conformance_issues(model)
    blocking = [g for g in model.gaps if g.kind in (GapKind.UNANSWERED, GapKind.CONFLICT)]
    issues += [f"未解決: {g.description}" for g in blocking]
    return issues
