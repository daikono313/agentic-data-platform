"""確定（finalize）前の検証。インタビュー中の未完成な成果物は許容し、確定時にだけ厳しく見る。"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import TYPE_CHECKING

from pydantic import ValidationError

from adp.beam.models import (
    Additivity,
    Aggregation,
    Availability,
    ExampleKind,
    FactType,
    GapKind,
    MetricType,
    StateMethod,
    TimeGrain,
    W,
)

if TYPE_CHECKING:
    from adp.beam.models import (
        BeamModel,
        BusinessEvent,
        Dimension,
        Measure,
        MetricFilter,
        MetricRequest,
    )

TIME_GRAINS = {g.value for g in TimeGrain}
_GRAINS = list(TimeGrain)  # 細かい→粗いの順
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
    elif all(x.kind != ExampleKind.TYPICAL for x in event.examples):
        issues.append(
            f"{p} 典型（kind: typical）の具体例がありません（よくある 1 件を聞いてください）"
        )
    for i, x in enumerate(event.examples, 1):
        if x.kind != ExampleKind.TYPICAL and not x.note.strip():
            issues.append(
                f"{p} 具体例 {i}（{x.kind.value}）が典型とどこが違うか（note）を記録してください"
            )
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
        if (
            m.availability == Availability.AVAILABLE
            and m.source is None
            and not m.counts_rows
            and m.lag is None
        ):
            issues.append(f"{p} measure {m.name} のソースが指定されていません")
        if m.counts_rows and any(
            m.name in x.values and str(x.values[m.name]) != "1" for x in event.examples
        ):
            issues.append(
                f"{p} measure {m.name} は粒度 1 行の件数なので、具体例の値は 1 です（書かなくて"
                "よい）。1 件に複数を数えるなら、粒度を見直してください"
            )
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
        issues += _state_issues(event, m, p)
        issues += _lag_issues(event, m, p)
    issues += _stage_issues(event, p)
    issues += _bridge_issues(event, p)

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
        issues += _filter_ref_issues(event, metric.filters, f"{p} filter の")
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
            leaves, cycle = model.leaf_inputs(metric)
            if cycle:
                issues.append(f"{p} 元の指標をたどると {metric.name} に戻ります（循環しています）")
            refs = [*metric.group_by, *(f.ref for f in metric.filters)]
            # 切り口は MetricFlow が同じ名前のまま元の指標へ渡すので、出来事をまたぐなら、
            # どの出来事でも同じ名前で解決できる必要がある（#181）
            events = {event.name: event}
            # 時間軸の切り口は MetricFlow の metric_time になり（`mf_name`）、元の指標ごとにその
            # 出来事の時間軸で解決される。元の指標の出来事に時間軸があればよく、ref の名前は
            # そろわなくてよい（#185）
            metric_time = {
                r
                for r in metric.group_by
                if resolve_ref(event, r) == "time" and _shared_ref(event, r)
            }
            for via, name in leaves:
                src = metrics[name]
                src_event = model.event(src.event)
                if src_event is None or src_event.name == event.name:
                    continue
                events[src_event.name] = src_event
                where = name if via == name else f"{via} の元の {name}"
                for ref in refs:
                    if ref in metric_time:
                        time = src_event.time_dimension
                        if time is None:
                            issues.append(
                                f"{p} {ref} は時間軸で切りますが、元の指標 {where} のイベント "
                                f"{src_event.name} には時間軸（agg_time_dimension）がありません"
                            )
                        elif _finer(_query_grain(event, ref), time):
                            issues.append(
                                f"{p} 切り口 {ref} の粒度は、元の指標 {where} のイベント "
                                f"{src_event.name} の時間軸 {time.ref} の最小粒度"
                                f"（{time.time_grain}）より細かいので切れません。{time.time_grain} "
                                "以上の粒度で切ってください"
                            )
                    elif resolve_ref(src_event, ref) in (None, "measure"):
                        issues.append(
                            f"{p} {ref} は元の指標 {where} のイベント {src_event.name} にありません"
                        )
            unshared = [
                r for r in metric.group_by if r not in metric_time and _unshared(events.values(), r)
            ]
            for ref in unshared:
                issues.append(
                    f"{p} 切り口 {ref} は出来事（{'・'.join(events)}）ごとに別々に記録される項目"
                    "なので、出来事をまたぐ指標の切り口に使えません。どの出来事にも共通する"
                    "ディメンション（商品など）の属性で切ってください"
                )
            issues += _nested_filter_issues(model, metric, p)
        bridged = [d.ref for d in event.dimensions if d.bridge is not None]
        if (
            bridged
            and uses_measure
            and not any(r.partition(".")[0] in bridged for r in metric.group_by)
        ):
            # 按分した fct には、複数の値で切る指標だけを置く（ADR 0010。値で切らない指標は
            # 元の出来事の fct の既存の指標を使う）
            issues.append(
                f"{p} イベント {event.name} は複数の値の切り口（{', '.join(bridged)}）で按分した"
                "出来事なので、指標の切り口にそのディメンションを入れてください。値で切らない指標は"
                "元の出来事に置いてください"
            )
        for ref in metric.group_by:
            kind = resolve_ref(event, ref)
            if kind is None or kind == "measure":
                issues.append(f"{p} group_by {ref} がイベントのディメンションに存在しません")
            elif kind == "attribute" and _attribute_unavailable(event, ref):
                issues.append(f"{p} group_by {ref} はデータなしです")
            elif kind == "time":
                dim = event.dimension(ref.partition(".")[0])
                if dim is not None and _finer(_query_grain(event, ref), dim):
                    issues.append(
                        f"{p} 切り口 {ref} の粒度は、{dim.ref} の最小粒度（{dim.time_grain}）"
                        f"より細かいので切れません。{dim.time_grain} 以上の粒度で切るか、"
                        "記録の粒度（time_grain）を見直してください"
                    )
    return issues


def _bridge_issues(event: BusinessEvent, p: str) -> list[str]:
    """多対多の切り口（ブリッジ）の決まり（ADR 0010）。按分した fct を作れる形か。"""
    bridged = [d for d in event.dimensions if d.bridge is not None]
    if not bridged:
        return []
    issues: list[str] = []
    if len(bridged) > 1:
        issues.append(
            f"{p} 1 つの出来事に付く複数の値の切り口（bridge）は 1 つまでです"
            f"（{', '.join(d.ref for d in bridged)}）。切り口ごとに出来事を分けてください"
        )
    if event.fact_type != FactType.TRANSACTION:
        issues.append(
            f"{p} 複数の値の切り口（bridge）で按分できるのは、1 回ごとの出来事（discrete）"
            "だけです（時点の値や段階の日時は値の数で割れない）"
        )
    for d in bridged:
        if d.masterless:
            issues.append(
                f"{p} 複数の値の切り口 {d.ref} はマスタの無い値なので、按分した fct を作れません。"
                "値の一覧（マスタ）の表を business_key にしてください"
            )
        if d.ref in event.grain_keys:
            issues.append(
                f"{p} 複数の値の切り口 {d.ref} は粒度のキー（grain_keys）に入れません。粒度は元の"
                "出来事の 1 行のキーにします（値ごとの行は按分で作る）"
            )
        if d.role is None:
            issues.append(
                f"{p} 複数の値の切り口 {d.name} に役割の名前（role。例: applied_promotion）を"
                "付けてください（1 件目だけを指す同じディメンションと区別する）"
            )
    for m in event.measures:
        if m.aggregation == Aggregation.SUM and m.additivity != Additivity.ADDITIVE:
            issues.append(
                f"{p} measure {m.name} は足し上げできない値（{m.additivity}）なので、値の数で"
                "按分できません"
            )
        if m.aggregation == Aggregation.AVERAGE:
            issues.append(
                f"{p} measure {m.name} は平均（average）なので、按分した fct には置けません。"
                "按分した合計 ÷ 按分した件数の比率（ratio）の指標で表してください"
            )
        if m.lag is not None:
            issues.append(
                f"{p} measure {m.name} は段階の間の時間なので、按分した fct には置けません"
            )
        if m.aggregation == Aggregation.COUNT and not m.counts_rows:
            issues.append(
                f"{p} measure {m.name} は列の件数（count）なので、按分の意味が決まりません。"
                "出来事の件数（source を書かない count）か、個数の数え直し（count_distinct）に"
                "してください"
            )
    return issues


def _stage_issues(event: BusinessEvent, p: str) -> list[str]:
    """段階を経て進む出来事（evolving）は、When ごとに段階の順番（stage）を持つ（#140）。"""
    when = [d for d in event.dimensions if d.w == W.WHEN]
    staged = [d for d in when if d.stage is not None]
    if event.fact_type != FactType.ACCUMULATING_SNAPSHOT:
        return [
            f"{p} When {d.ref} の段階（stage）は、段階を経て進む出来事（story_type: evolving）で"
            "だけ指定します"
            for d in staged
        ]
    issues: list[str] = []
    if len(when) < 2:
        issues.append(
            f"{p} 段階を経て進む出来事なので、段階ごとの日時（When）を 2 つ以上記録してください"
            "（例: 注文日時・出荷日時）。源泉に日時が 1 つしか無ければ、1 回で終わる出来事"
            "（story_type: discrete）として記録してください"
        )
    for d in when:
        if d.stage is None:
            issues.append(f"{p} When {d.ref} が何番目の段階か（stage）が決まっていません")
    stages = [d.stage for d in staged if d.stage is not None]
    if len(stages) != len(set(stages)):
        issues.append(f"{p} 段階の順番（stage）が重なっています: {sorted(stages)}")
    return issues


def _lag_issues(event: BusinessEvent, m: Measure, p: str) -> list[str]:
    """段階の間の時間（lag）の measure は、evolving のイベントの 2 つの段階を前から後へ指す。"""
    if m.lag is None:
        return []
    if event.fact_type != FactType.ACCUMULATING_SNAPSHOT:
        return [
            f"{p} measure {m.name} の段階の間の時間（lag）は、段階を経て進む出来事"
            "（story_type: evolving）でだけ指定します"
        ]
    issues: list[str] = []
    if m.source is not None:
        issues.append(
            f"{p} measure {m.name} は段階の間の時間（lag）なので、source は書きません（段階の日時"
            "から計算します）"
        )
    if m.aggregation not in (Aggregation.AVERAGE, Aggregation.MIN, Aggregation.MAX):
        issues.append(
            f"{p} measure {m.name} は 1 件ごとにかかった時間なので、平均（average）・最短（min）・"
            f"最長（max）のどれかで集計してください（今は {m.aggregation}）"
        )
    if m.additivity == Additivity.SEMI_ADDITIVE:
        issues.append(
            f"{p} measure {m.name} は時点の値（semi_additive）ではありません。additivity を"
            " non_additive にしてください"
        )
    start, end = event.dimension(m.lag.start), event.dimension(m.lag.end)
    for ref, d in ((m.lag.start, start), (m.lag.end, end)):
        if d is None or d.w != W.WHEN:
            issues.append(f"{p} measure {m.name} の段階 {ref} が When にありません")
    if (
        start is not None
        and end is not None
        and start.stage is not None
        and end.stage is not None
        and start.stage >= end.stage
    ):
        issues.append(
            f"{p} measure {m.name} の段階の間の時間は、前の段階（{m.lag.start}）から後の段階"
            f"（{m.lag.end}）へ指定してください"
        )
    return issues


def _state_issues(event: BusinessEvent, m: Measure, p: str) -> list[str]:
    """定期的な状態（recurring）の measure は、記録からの値の求め方（state）を持つ（ADR 0008）。"""
    if event.fact_type != FactType.PERIODIC_SNAPSHOT:
        if m.state is None:
            return []
        return [
            f"{p} measure {m.name} の値の求め方（state）は、定期的な状態の記録"
            "（story_type: recurring）のイベントでだけ指定します"
        ]
    if m.availability == Availability.UNAVAILABLE:
        return []  # データの無い measure は作らない（gap に記録する）ので、求め方も要らない
    if m.state is None:
        return [
            f"{p} measure {m.name} は定期的な状態の値なので、記録からの求め方（state）を決めて"
            "ください（記録がその時点の値そのものなら last_value、増えた・減った数なら"
            " running_total）"
        ]
    issues: list[str] = []
    if m.additivity == Additivity.ADDITIVE:
        issues.append(
            f"{p} measure {m.name} は時点の値なので、期間をまたいで足せません。additivity を"
            " semi_additive にしてください"
        )
    if m.aggregation in (Aggregation.COUNT, Aggregation.COUNT_DISTINCT):
        issues.append(
            f"{p} measure {m.name} は件数（{m.aggregation}）なので、記録の値から状態を"
            "求められません。状態の数（在庫数など）の列を measure にしてください"
        )
    elif m.state.method == StateMethod.RUNNING_TOTAL and m.aggregation != Aggregation.SUM:
        issues.append(
            f"{p} measure {m.name} は増減を積み上げる（running_total）ので、集計方法は sum に"
            "してください"
        )
    if m.state.reset and m.state.method != StateMethod.RUNNING_TOTAL:
        issues.append(
            f"{p} measure {m.name} の置き換える行（reset）は、増減を積み上げる"
            "（running_total）ときだけ指定します"
        )
    issues += _filter_ref_issues(event, m.state.reset, f"{p} measure {m.name} の置き換える行の条件")
    return issues


def _filter_ref_issues(event: BusinessEvent, filters: list[MetricFilter], head: str) -> list[str]:
    """絞り込み・置き換える行の条件の参照が、イベントの属性か縮退ディメンションにあり、データが
    あるか（指標の filters と measure の state.reset で共通）。"""
    issues: list[str] = []
    for f in filters:
        if resolve_ref(event, f.ref) not in ("attribute", "degenerate"):
            issues.append(f"{head} {f.ref} がイベントの属性・縮退ディメンションにありません")
        elif _attribute_unavailable(event, f.ref):
            issues.append(f"{head} {f.ref} はデータなしです")
    return issues


def _query_grain(event: BusinessEvent, ref: str) -> TimeGrain | None:
    """時間の切り口（`<When ref>` / `<When ref>.<grain>`）で集計する粒度。省略時は When の
    time_grain（`adp.generation.semantic.mf_name` と同じ）。time_grain が未定なら None で判定しない
    （未定は別の課題になる）。"""
    dim_ref, _, attr = ref.partition(".")
    dim = event.dimension(dim_ref)
    return TimeGrain(attr) if attr else (dim.time_grain if dim else None)


def _finer(grain: TimeGrain | None, dim: Dimension) -> bool:
    """grain が、When の記録の最小粒度（time_grain）より細かいか。"""
    if grain is None or dim.time_grain is None:
        return False
    return _GRAINS.index(grain) < _GRAINS.index(dim.time_grain)


def _nested_filter_issues(model: BeamModel, metric: MetricRequest, p: str) -> list[str]:
    """比率・派生の指標の絞り込みは、元の指標ごとにその出来事の名前で書く（#178）。元の指標が
    さらに比率・派生なら、その指標からたどった先の最初の指標の出来事の名前で書き、MetricFlow は
    その名前のまま下の指標すべてへ渡す（`adp.generation.semantic._input`）。下の出来事が分かれる
    なら、どの出来事でも同じ名前で解決できる必要がある（#184。切り口の #181 と同じ判定）。
    参照が下の出来事にあるかは、呼び出し元が確かめる（#180）。"""
    metrics = {m.name: m for m in model.metrics}
    issues: list[str] = []
    for via in dict.fromkeys(metric.inputs):
        src = metrics.get(via)
        if src is None or src.type not in (MetricType.RATIO, MetricType.DERIVED):
            continue
        # 生成と同じく、元の指標ごとにたどり直す（leaf_inputs(metric) は元の指標をまたいで
        # 同じ指標を 1 回にまとめるので、元の指標ごとの出来事を取りこぼす）
        events: dict[str, BusinessEvent] = {}
        for _, name in model.leaf_inputs(src)[0]:
            leaf_event = model.event(metrics[name].event)
            if leaf_event is not None:
                events[leaf_event.name] = leaf_event
        for ref in dict.fromkeys(f.ref for f in metric.filters):
            if _unshared(events.values(), ref):
                issues.append(
                    f"{p} 絞り込み {ref} は、元の指標 {via}（比率・派生）の下の出来事"
                    f"（{'・'.join(events)}）ごとに別々に記録される項目なので使えません。"
                    "どの出来事にも共通するディメンション（商品など）の属性で絞り込むか、"
                    f"{via} の元の指標に絞り込みを付けてください"
                )
    return issues


def _unshared(events: Collection[BusinessEvent], ref: str) -> bool:
    """出来事が分かれ、どの出来事でも解決できるが、出来事ごとに名前が変わる参照か（マスタなし・
    縮退のディメンションなど。#181・#184）。どれかの出来事で解決できないものは、別の課題になる。"""
    return (
        len(events) > 1
        and all(resolve_ref(e, ref) not in (None, "measure") for e in events)
        and not all(_shared_ref(e, ref) for e in events)
    )


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
