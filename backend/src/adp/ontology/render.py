"""オントロジーを Mermaid の図と Markdown にする（#67）。`docs/ontology.md` はここから生成する。"""

from __future__ import annotations

from adp.markdown import cell, mermaid_label
from adp.ontology.models import (
    KIND_LABELS,
    ROLE_LABELS,
    Attribute,
    Concept,
    Ontology,
    Relation,
    mermaid_id,
)

MAX_ATTRS_IN_DIAGRAM = 4
"""図に出す属性の数（多いと読めなくなる。全部は md の表に出す）。"""

HEADER = (
    "<!-- このファイルは ontology/ontology.yml から生成する（make ontology）。手で編集しない。 -->"
)


def _node(c: Concept) -> str:
    # 出来事（event）は角丸の長円、状態（state）は六角形、もの・人（entity）は角丸の四角
    label = mermaid_label(c.label)
    if c.kind == "event":
        return f'{mermaid_id(c.name)}(["{label}"])'
    if c.kind == "state":
        return f'{mermaid_id(c.name)}{{{{"{label}"}}}}'
    return f'{mermaid_id(c.name)}("{label}")'


def mermaid(ontology: Ontology) -> str:
    lines = ["flowchart LR"]
    for d in ontology.domains:
        members = [c for c in ontology.concepts if c.domain == d.name]
        if not members:
            continue
        lines.append(f'  subgraph d_{d.name}["{mermaid_label(d.label)}"]')
        for c in members:
            lines.append(f"    {_node(c)}")
            if c.attributes:
                shown = [mermaid_label(a.label) for a in c.attributes[:MAX_ATTRS_IN_DIAGRAM]]
                rest = len(c.attributes) - len(shown)
                if rest > 0:
                    shown.append(f"ほか {rest}")
                lines.append(f'    a_{c.name}["{"<br/>".join(shown)}"]:::attr')
        lines.append("  end")
    for c in ontology.concepts:
        if c.attributes:
            lines.append(f"  {mermaid_id(c.name)} -.- a_{c.name}")
    for r in ontology.relations:
        lines.append(f'  {mermaid_id(r.from_)} -- "{mermaid_label(r.verb)}" --> {mermaid_id(r.to)}')
    for i, x in enumerate(ontology.constraints):
        lines.append(f'  k_{i}["{mermaid_label(x.text)}"]:::constraint')
        lines.append(f"  k_{i} -.- {mermaid_id(x.concept)}")
    events = [mermaid_id(c.name) for c in ontology.concepts if c.kind == "event"]
    lines += [
        "  classDef attr fill:#f4f4f5,stroke:#a1a1aa,color:#3f3f46,font-size:11px",
        "  classDef constraint fill:#dcfce7,stroke:#22c55e,color:#14532d,font-size:11px",
        "  classDef event fill:#166534,stroke:#14532d,color:#ffffff",
        "  classDef entity fill:#1e3a8a,stroke:#172554,color:#ffffff",
        "  classDef state fill:#9a3412,stroke:#7c2d12,color:#ffffff",
    ]
    entities = [mermaid_id(c.name) for c in ontology.concepts if c.kind == "entity"]
    if events:
        lines.append(f"  class {','.join(events)} event")
    if entities:
        lines.append(f"  class {','.join(entities)} entity")
    states = [mermaid_id(c.name) for c in ontology.concepts if c.kind == "state"]
    if states:
        lines.append(f"  class {','.join(states)} state")
    return "\n".join(lines) + "\n"


def attribute_notes(a: Attribute, c: Concept, ontology: Ontology) -> list[str]:
    """属性の補足（役割・上の階層・同義語・正本）。docs/ontology.md と画面で同じものを出す
    （#137）。"""
    notes = []
    if a.role is not None:
        notes.append(ROLE_LABELS[a.role])
    if a.parent is not None:
        notes.append(f"上の階層は{next(p.label for p in c.attributes if p.name == a.parent)}")
    if a.synonyms:
        notes.append("同義語: " + "・".join(a.synonyms))
    if a.same_as is not None:
        concept, attr = a.same_as.split(".", 1)
        target = ontology.concept(concept)
        label = next(x.label for x in target.attributes if x.name == attr)
        notes.append(f"正本は{target.label}の{label}")
    return notes


def _attribute(a: Attribute, c: Concept, ontology: Ontology) -> str:
    return f"{a.label}（{'。'.join([f'`{a.column}`', *attribute_notes(a, c, ontology)])}）"


def multiplicity(r: Relation, ontology: Ontology) -> str:
    """関係の件数の対応の表示（例: 顧客 1 件に注文が複数、注文には必ず顧客がある。未確認なら -）。
    via を持つ側から見る（Ontology.relation_ends）。"""
    ends = ontology.relation_ends(r)
    if ends is None:
        return "-"
    one, many = ends
    parts = []
    if r.cardinality == "one_to_one":
        parts.append(f"{one.label} 1 件に{many.label}は 1 件まで")
    elif r.cardinality == "one_to_many":
        parts.append(f"{one.label} 1 件に{many.label}が複数")
    if r.optional is True:
        parts.append(f"{one.label}が決まっていない{many.label}がある")
    elif r.optional is False:
        parts.append(f"{many.label}には必ず{one.label}がある")
    return "、".join(parts) or "-"


def markdown(ontology: Ontology) -> str:
    domain = {d.name: d.label for d in ontology.domains}
    out = [
        HEADER,
        "",
        "# オントロジー",
        "",
        "業務の概念と、その属性・関係・制約。"
        "源泉が増えたら、まず `ontology/ontology.yml` を更新し、それを元に fact / dim を整理する。",
        "",
        "凡例: 四角 = もの・人・場所（entity → dim の候補）、長円 = 出来事（event → fact の候補）、"
        "六角形 = 状態（state → 定期スナップショットの fact の候補）、"
        "灰色 = 属性（一部）、緑 = 制約、矢印 = 関係",
        "",
        "```mermaid",
        mermaid(ontology).rstrip("\n"),
        "```",
        "",
        "## 概念",
        "",
        "| 概念 | 種類 | ドメイン | 源泉（キー） | モデル | 属性 |",
        "|---|---|---|---|---|---|",
    ]
    labels = {c.name: c.label for c in ontology.concepts}
    for c in ontology.concepts:
        kind = KIND_LABELS[c.kind]
        attrs = "、".join(_attribute(a, c, ontology) for a in c.attributes) or "-"
        model = f"`{c.model}`" if c.model else "未整備"
        if c.history_model:  # 当時の値の履歴モデルは dim の従属（ADR 0009）
            model += f"（履歴: `{c.history_model}`）"
        out.append(
            f"| {cell(c.label)} `{c.name}` | {kind} | {cell(domain[c.domain])} "
            f"| `{c.source}`（`{c.key}`） | {model} | {cell(attrs)} |"
        )
    if ontology.relations:
        out += [
            "",
            "## 関係",
            "",
            "件数の対応は、列（via）を持つ側から見た、相手との件数の関係。",
            "",
            "| から | 関係 | へ | 列 | 件数の対応 |",
            "|---|---|---|---|---|",
        ]
        for r in ontology.relations:
            ends = (cell(labels[r.from_]), cell(labels[r.to]))
            many = cell(multiplicity(r, ontology))
            out.append(f"| {ends[0]} | {cell(r.verb)} | {ends[1]} | `{r.via}` | {many} |")
    if ontology.constraints:
        out += ["", "## 制約", "", "| 概念 | 制約 |", "|---|---|"]
        for x in ontology.constraints:
            out.append(f"| {cell(labels[x.concept])} | {cell(x.text)} |")
    return "\n".join(out) + "\n"
