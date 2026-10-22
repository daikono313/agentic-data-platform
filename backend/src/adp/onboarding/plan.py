"""源泉の取り込みの下書き: 追加案・クレンジングの定義・バスマトリクスの差分（#69）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from adp.catalog.catalog import ColumnProfile
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
    cleansing_spec: dict[str, Any] | None = None
    """cleansing を読んだもの（画面のフォームで直す。#150）。既存の定義が読めなければ None。"""
    columns: list[ColumnProfile] = Field(default_factory=list)
    """源泉の列のプロファイル（画面で、属性やクレンジングの判断材料として値の例を出す。#150）。"""
    column_kinds: dict[str, str] = Field(default_factory=dict)
    """源泉の列 → クレンジング後の型の種類（column_kind の値。#150）。"""
    date_derives: dict[str, str | None] = Field(default_factory=dict)
    """日時の列 → 日付の列として作る名前（ほかの列と重なる・列名に使えなければ None。#150）。"""
    concept_labels: dict[str, str] = Field(default_factory=dict)
    """追加後のオントロジーの概念の名前 → 表示名（関係を日本語で出すため。#150）。"""
    cleansing_exists: bool
    mermaid: str
    """追加後のオントロジーの図。"""
    bus: BusMatrix
    """追加後に作れる fct / dim の候補を加えたバスマトリクス。"""


def bus_with_candidates(bus: BusMatrix, ontology: Ontology, patch: OntologyPatch) -> BusMatrix:
    """出来事・状態の概念 → fct の候補行（関係でつながる dim に ✓）、もの・人の概念 → dim の
    候補列。"""
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
        if c.kind == "entity":
            continue  # 出来事と状態は fct の候補の行
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
