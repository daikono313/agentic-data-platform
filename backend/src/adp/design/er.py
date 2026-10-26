"""物理設計から mermaid の ER 図を作る（PR 本文と UI に表示する）。"""

from __future__ import annotations

from adp.design.models import ColumnRole, PhysicalDesign
from adp.markdown import mermaid_label

_TYPE = {
    ColumnRole.PRIMARY_KEY: "key",
    ColumnRole.FOREIGN_KEY: "key",
    ColumnRole.MEASURE: "number",
    ColumnRole.WEIGHT: "number",
    ColumnRole.TIME: "date",
    ColumnRole.TIMESTAMP: "timestamp",
}


def er_diagram(design: PhysicalDesign) -> str:
    lines = ["erDiagram"]
    for dim in design.dims:
        lines.append(f"  {dim.name} {{")
        lines.append(f'    key {dim.key_column} PK "{mermaid_label(dim.label)}"')
        lines += [f'    string {c.name} "{mermaid_label(c.description)}"' for c in dim.columns]
        lines.append("  }")
    if design.uses_dim_date:
        lines.append("  dim_date {")
        lines.append('    date date_day PK "日付"')
        lines.append("  }")
    for fact in design.facts:
        lines.append(f"  {fact.name} {{")
        for c in fact.columns:
            mark = " PK" if c.name in fact.grain_columns else ""
            mark = " FK" if c.role == ColumnRole.FOREIGN_KEY else mark
            kind = _TYPE.get(c.role, "string")
            lines.append(f'    {kind} {c.name}{mark} "{mermaid_label(c.description)}"')
        lines.append("  }")
        for c in fact.columns:
            if c.references and c.role in (ColumnRole.FOREIGN_KEY, ColumnRole.TIME):
                target = c.references.split(".")[0]
                lines.append(f'  {target} ||--o{{ {fact.name} : "{c.name}"')
    return "\n".join(lines) + "\n"
