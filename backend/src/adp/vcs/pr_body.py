"""生成 PR の本文。

レビュアーが差分と BEAM 成果物だけで判断できるようにする（design-doc Goal 3）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from adp.beam import BeamModel, W, build_event_matrix
from adp.design import IssueSeverity, PhysicalDesign, er_diagram
from adp.generation import AgentTask

_W_LABEL = {
    W.WHO: "Who（誰が）",
    W.WHAT: "What（何を）",
    W.WHEN: "When（いつ）",
    W.WHERE: "Where（どこで）",
    W.WHY: "Why（なぜ）",
    W.HOW: "How（どのように）",
}


@dataclass
class Verification:
    """生成後の検証結果。"""

    dbt_build_passed: bool
    summary: str = ""
    queries: list[tuple[str, bool]] = field(default_factory=list)
    """(クエリの説明, 成功したか)"""
    repair_attempts: list[str] = field(default_factory=list)


@dataclass
class ChartPreview:
    """生成したチャート（#42）。"""

    spec: str
    """元ファイルのパス（warehouse/chart_specs/<board>.yml）。"""
    charts: list[tuple[str, str]] = field(default_factory=list)
    """(タイトル, 種類)"""
    image_url: str | None = None
    error: str | None = None
    skipped: str | None = None
    """作らなかった理由（既存のチャートがあるなど）。"""
    omitted: list[str] = field(default_factory=list)
    """描けなかったため除いた指標（ほかの指標のチャートは作った。#79）。"""


_TYPE_LABEL = {"line": "折れ線", "bar": "棒", "kpi": "KPI"}


_ABS_PATH = re.compile(r"(?:/Users|/home|/private|/tmp|[A-Z]:\\)[^\s`'\")]*")


def _safe(text: str) -> str:
    """LLM やユーザーの文章を、公開リポジトリの PR に載せても安全にする。

    - `#123` や `@user` が issue の自動クローズやメンションにならないようにする
    - ローカルの絶対パスを伏せる
    - コードブロックを閉じられないようにする
    """
    text = _ABS_PATH.sub("<path>", text)
    text = re.sub(r"(?<!\w)([#@])(?=\w)", "\\1\u200b", text)
    return text.replace("```", "ˋˋˋ")


def _cell(text: str) -> str:
    return _safe(text).replace("|", "\\|").replace("\n", " ")


def pr_title(beam: BeamModel) -> str:
    return f"feat(beam): {beam.title}"


def pr_body(
    beam: BeamModel,
    design: PhysicalDesign,
    files: list[str],
    verification: Verification,
    tasks: list[AgentTask] | None = None,
    chart: ChartPreview | None = None,
) -> str:
    lines: list[str] = []
    lines += ["## 要求", "", f"> {_cell(beam.request)}", ""]
    for m in beam.metrics:
        lines.append(f"- **{_cell(m.label)}**（`{m.name}`）: {_cell(m.framed_question)}")
        if m.group_by:
            lines.append(f"  - 切り口: {', '.join(f'`{g}`' for g in m.group_by)}")
        if m.filter:
            lines.append(f"  - 条件: {_cell(m.filter)}")
    lines.append("")

    for event in beam.events:
        lines += [f"## BEAM Canvas: {_cell(event.label)}（`{event.name}`）", ""]
        lines.append(f"- 種類: {event.fact_type.value}　粒度: {_cell(event.grain or '-')}")
        measures = ", ".join(f"{_cell(m.label)}（{m.aggregation.value}）" for m in event.measures)
        lines += [f"- How many: {measures}", "", "| W | ディメンション | 属性 |", "|---|---|---|"]
        for d in event.dimensions:
            attrs = ", ".join(
                a.label + ("（データなし）" if a.availability.value == "unavailable" else "")
                for a in d.attributes
            )
            lines.append(
                f"| {_W_LABEL[d.w]} | {_cell(d.label)}（`{d.ref}`） | {_cell(attrs) or '-'} |"
            )
        lines.append("")

    matrix = build_event_matrix(beam)
    if matrix.columns:
        lines += ["## Event Matrix", ""]
        header = (
            "| イベント | 種類 | 優先度 | " + " | ".join(c.label for c in matrix.columns) + " |"
        )
        lines += [header, "|" + "---|" * (3 + len(matrix.columns))]
        for row in matrix.rows:
            cells = ["✓" if c.name in row.dimensions else "" for c in matrix.columns]
            priority = row.priority.value if row.priority else "-"
            lines.append(
                f"| {row.label} | {row.fact_type.value} | {priority} | " + " | ".join(cells) + " |"
            )
        lines.append("")

    lines += ["## スター設計", "", "```mermaid", er_diagram(design).rstrip(), "```", ""]

    lines += ["## 生成したファイル", ""] + [f"- `{f}`" for f in files] + [""]

    if chart is not None:
        lines += ["## チャート", ""]
        if chart.skipped:
            lines += [f"- {_cell(chart.skipped)}", ""]
        elif chart.error:
            lines += [f"- ❌ チャートを作れませんでした: {_cell(chart.error)}", ""]
        else:
            lines.append(f"元ファイル `{chart.spec}`（SQL は MetricFlow が作る）")
            lines += [""] + [
                f"- {_cell(title)}（{_TYPE_LABEL.get(kind, kind)}）" for title, kind in chart.charts
            ]
            if chart.omitted:
                omitted = "、".join(_cell(t) for t in chart.omitted)
                lines += ["", f"- ⚠ 描けなかったため除いた指標: {omitted}"]
            if chart.image_url:
                lines += ["", f"![チャート]({chart.image_url})"]
            lines.append("")

    lines += ["## 検証", ""]
    lines.append(f"- dbt build: {'✅ 成功' if verification.dbt_build_passed else '❌ 失敗'}")
    if verification.summary:
        lines.append(f"  - {_cell(verification.summary)}")
    for desc, ok in verification.queries:
        lines.append(f"- 指標の取得 `{desc}`: {'✅' if ok else '❌'}")
    if verification.repair_attempts:
        lines.append(f"- 自己修復: {len(verification.repair_attempts)} 回")
        lines += [f"  - {_cell(a)}" for a in verification.repair_attempts]
    lines.append("")

    unresolved = [f"{g.kind.value}: {g.description}" for g in beam.gaps]
    unresolved += [i.message for i in design.issues if i.severity == IssueSeverity.WARNING]
    unresolved += [f"要対応（{t.kind}）: {t.instruction}" for t in tasks or []]
    if unresolved:
        lines += ["## 未解決事項", ""] + [f"- {_cell(u)}" for u in unresolved] + [""]

    lines += [
        "---",
        "このPRは auto_data_platform が BEAM✲ のヒアリング結果から生成しました。"
        f"要求の正本は `beam/{beam.name}.yml` です。",
    ]
    return "\n".join(lines) + "\n"
