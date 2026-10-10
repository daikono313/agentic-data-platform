"""Event Matrix: 行がイベント、列がディメンション。BEAM 成果物から導出する（保存はしない）。"""

from __future__ import annotations

from pydantic import BaseModel

from adp.beam.models import BeamModel, FactType, Level, W


class MatrixColumn(BaseModel):
    name: str
    label: str
    w: W
    conformed: bool


class MatrixRow(BaseModel):
    event: str
    label: str
    fact_type: FactType
    grain: str | None
    priority: Level | None
    impact: Level | None
    effort: Level | None
    measures: list[str]
    dimensions: dict[str, list[str]]
    """列名 → そのイベントでの役割（role が無ければ列名と同じ）。"""


class EventMatrix(BaseModel):
    columns: list[MatrixColumn]
    rows: list[MatrixRow]


def build_event_matrix(model: BeamModel) -> EventMatrix:
    columns: dict[str, MatrixColumn] = {}
    usage: dict[str, set[str]] = {}
    rows: list[MatrixRow] = []
    for event in model.events:
        dims: dict[str, list[str]] = {}
        for d in event.dimensions:
            columns.setdefault(
                d.name, MatrixColumn(name=d.name, label=d.label, w=d.w, conformed=False)
            )
            usage.setdefault(d.name, set()).add(event.name)
            dims.setdefault(d.name, []).append(d.ref)
        rows.append(
            MatrixRow(
                event=event.name,
                label=event.label,
                fact_type=event.fact_type,
                grain=event.grain,
                priority=event.priority,
                impact=event.impact,
                effort=event.effort,
                measures=[m.name for m in event.measures],
                dimensions=dims,
            )
        )
    w_order = list(W)
    ordered = sorted(columns.values(), key=lambda c: w_order.index(c.w))
    for c in ordered:
        c.conformed = len(usage[c.name]) > 1
    return EventMatrix(columns=ordered, rows=rows)
