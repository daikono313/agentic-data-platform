"""源泉の取り込みの下書き: 追加案・クレンジングの定義・バスマトリクスの差分（#69）。"""

from __future__ import annotations

from pydantic import BaseModel

from adp.datasets import BusAttribute, BusGroup, BusMatrix, BusRow
from adp.onboarding.proposal import TIME_HINT, OntologyPatch, apply
from adp.ontology.models import Ontology


class OnboardingDraft(BaseModel):
    source: str
    patch: OntologyPatch
    proposer: str
    """agent（エージェントの案）か profile（プロファイルからの決定的な案）。"""
    note: str | None = None
    """エージェントの案が使えなかった理由など。"""
    cleansing: str
    """クレンジングの定義（YAML）。既にあればそれ、無ければ雛形。"""
    cleansing_exists: bool
    mermaid: str
    """追加後のオントロジーの図。"""
    bus: BusMatrix
    """追加後に作れる fct / dim の候補を加えたバスマトリクス。"""


def bus_with_candidates(bus: BusMatrix, ontology: Ontology, patch: OntologyPatch) -> BusMatrix:
    """出来事の概念 → fct の候補行（関係でつながる dim に ✓）、もの・人の概念 → dim の候補列。"""
    merged = apply(ontology, patch)
    groups = list(bus.groups)
    rows = list(bus.rows)
    names = {g.name for g in groups}
    new = {c.name for c in patch.concepts}
    for c in patch.concepts:
        if c.kind == "entity":
            groups.append(
                BusGroup(
                    name=f"dim_{c.name}",
                    label=c.label,
                    kind="dim",
                    attributes=[
                        BusAttribute(name=f"{c.name}__{a.name}", label=a.label)
                        for a in c.attributes
                    ],
                    candidate=True,
                )
            )
            names.add(f"dim_{c.name}")
    concepts = {c.name: c for c in merged.concepts}
    for c in patch.concepts:
        if c.kind != "event":
            continue
        joined: list[str] = []
        for r in merged.relations:
            if c.name not in (r.from_, r.to):
                continue
            other = concepts[r.to if r.from_ == c.name else r.from_]
            if other.kind != "entity":
                continue
            dim = other.model or f"dim_{other.name}"
            if dim in names:
                joined.append(dim)
        # 日時の属性があれば日付のディメンションでも切れる
        if "dim_date" in names and any(TIME_HINT.search(a.column) for a in c.attributes):
            joined.append("dim_date")
        rows.append(BusRow(name=f"fct_{c.name}", label=c.label, groups=joined, candidate=True))
    # 既存の出来事が、新しいもの・人と関係でつながるなら、その行にも候補の ✓ を付ける
    for r in merged.relations:
        if not ({r.from_, r.to} & new):
            continue
        for a, b in ((r.from_, r.to), (r.to, r.from_)):
            entity, event = concepts[a], concepts[b]
            if entity.name in new and entity.kind == "entity" and event.model:
                rows = [
                    row.model_copy(update={"groups": [*row.groups, f"dim_{entity.name}"]})
                    if row.name == event.model
                    else row
                    for row in rows
                ]
    return BusMatrix(groups=groups, rows=rows)
