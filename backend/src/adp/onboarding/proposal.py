"""源泉の取り込み: オントロジーへの追加案（#69）。

追加案（OntologyPatch）は、既存のオントロジーに足す概念・関係・制約・ドメイン。適用した結果を
オントロジーの型と源泉との照合で検証する（誤りがあれば適用しない）。

案はエージェント（`propose_with_agent`）か、プロファイルからの決定的な規則（`propose_from_profile`）
で作る。エージェントが失敗したとき・CI では決定的な案を使う。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from adp.catalog.catalog import Catalog, TableProfile
from adp.ontology.check import check
from adp.ontology.models import (
    Attribute,
    Concept,
    Constraint,
    Domain,
    Ontology,
    Relation,
    duplicate_message,
)

TIME_HINT = re.compile(r"(_at|_on|_date)$")
IGNORED_COLUMNS = {"updated_at", "loaded_at", "created_at"}
"""属性にしない管理用の列。"""
OTHER_DOMAIN = Domain(name="other", label="その他")


class OntologyPatch(BaseModel):
    """既存のオントロジーへの追加（変更・削除はしない）。"""

    model_config = ConfigDict(extra="forbid")

    domains: list[Domain] = Field(default_factory=list)
    concepts: list[Concept] = Field(min_length=1)
    relations: list[Relation] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)


def apply(ontology: Ontology, patch: OntologyPatch) -> Ontology:
    """追加して検証した新しいオントロジー。重複・存在しない参照は ValueError。"""
    base = ontology.model_dump(mode="json", by_alias=True, exclude_none=True)
    add = patch.model_dump(mode="json", by_alias=True, exclude_none=True)
    for key in ("domains", "concepts", "relations", "constraints"):
        base[key] = [*base.get(key, []), *add.get(key, [])]
    return Ontology.model_validate(base)


def patch_errors(errors: list[str], patch: OntologyPatch, merged: Ontology) -> list[str]:
    """照合の誤りのうち、追加案で足した概念・関係に関わるもの（既存の部分は CI の検査に任せる）。

    merged は追加案を適用したオントロジー。業務の言葉の二重定義は、追加案の概念の属性が入って
    いるものを、そこから直接選ぶ（誤りの文言を読み直さない）。関係の誤りは関係の動詞で選ぶ
    （既存の関係と動詞が同じだと、その誤りも入る）。"""
    prefixes = [f"概念 {c.name}:" for c in patch.concepts]
    prefixes += [f"概念 {c.name} の" for c in patch.concepts]
    prefixes += [f"関係「{r.verb}」:" for r in patch.relations]
    names = {c.name for c in patch.concepts}
    duplicates = {
        duplicate_message(word, refs)
        for word, refs in merged.duplicate_words().items()
        if any(r.split(".", 1)[0] in names for r in refs)
    }
    return sorted({e for e in errors if e.startswith(tuple(prefixes))} | duplicates)


def validate_patch(
    ontology: Ontology, patch: OntologyPatch, catalog: Catalog, source: str
) -> list[str]:
    """追加案の誤り（空なら適用してよい）。既存の部分の食い違いは問わない。

    概念はすべて取り込む源泉のもので、モデルはまだ無い（作るのは生成）。既に概念のある源泉は
    取り込めない。
    """
    errors: list[str] = []
    if any(c.source == source for c in ontology.concepts):
        errors.append(f"{source} は既にオントロジーにあります")
    if not any(c.source == source for c in patch.concepts):
        errors.append(f"源泉 {source} を元にした概念がありません")
    for c in patch.concepts:
        if c.source != source:
            errors.append(
                f"概念 {c.name}: 取り込む源泉（{source}）以外の源泉 {c.source} は使えない"
            )
        if c.model is not None:
            errors.append(
                f"概念 {c.name}: model は書かない（fct / dim は生成で作り、書き込まれる）"
            )
    if errors:
        return errors
    try:
        merged = apply(ontology, patch)
    except ValueError as e:
        return [str(e)]
    return patch_errors(check(merged, catalog, None).errors, patch, merged)


def _singular(name: str) -> str:
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name


def _label(text: str, fallback: str) -> str:
    first = re.split(r"[（(。\n]", text, maxsplit=1)[0].strip()
    return first or fallback


def propose_from_profile(profile: TableProfile, ontology: Ontology) -> OntologyPatch:
    """プロファイルからの決定的な案。

    - 外部キー候補が既存の概念のキーを指していれば、その概念との関係にする
    - 日時の列と外部キーがあれば出来事（event）、無ければもの・人（entity）
    - ドメインは関係する最初の概念のもの（無ければ「その他」）
    """
    source = f"{profile.schema_name}.{profile.name}"
    names = {c.name for c in ontology.concepts}
    name = _singular(profile.name)
    while name in names:
        name += "_2"
    ids = [k for k in profile.primary_key_candidates if k.endswith("_id")]
    key = ids[0] if ids else (profile.primary_key_candidates or [profile.columns[0].name])[0]

    # 状態の概念は、出来事と同じ記録（源泉のキー）から作るので、関係は元の概念に付ける（ADR 0008）
    by_key = {(c.source, c.key): c for c in ontology.concepts if c.kind != "state"}
    links: list[tuple[str, Concept]] = []
    for fk in profile.foreign_key_candidates:
        table, _, column = fk.references.rpartition(".")
        target = by_key.get((table, column))
        if target is not None:
            links.append((fk.column, target))
    has_time = any(
        TIME_HINT.search(c.name) and c.name not in IGNORED_COLUMNS for c in profile.columns
    )
    kind: Literal["entity", "event"] = "event" if has_time and links else "entity"
    fk_columns = {col for col, _ in links}
    concept_label = _label(profile.description, profile.name)
    # 既存の属性と同じラベルは、業務の言葉の二重定義になる（#137）。概念の名前を前に付けて分ける
    used = {
        w for c in ontology.concepts for a in c.attributes for w in [a.label, *(a.synonyms or [])]
    }
    attributes = []
    for c in profile.columns:
        if c.name == key or c.name in fk_columns or c.name in IGNORED_COLUMNS:
            continue
        label = _label(c.description, c.name)
        if label in used:
            label = f"{concept_label}の{label}"
        if label in used:  # それでも重なれば列名で分ける
            label = f"{label}（{c.name}）"
        used.add(label)
        attributes.append(Attribute(name=c.name, label=label, column=c.name))
    domains: list[Domain] = []
    domain = links[0][1].domain if links else OTHER_DOMAIN.name
    if domain == OTHER_DOMAIN.name and OTHER_DOMAIN.name not in {d.name for d in ontology.domains}:
        domains.append(OTHER_DOMAIN)
    concept = Concept(
        name=name,
        label=concept_label,
        domain=domain,
        kind=kind,
        source=source,
        key=key,
        attributes=attributes,
    )
    relations = [
        Relation.model_validate(
            {
                "from": target.name,
                "to": name,
                "verb": "持つ" if kind == "entity" else "関わる",
                "via": f"{source}.{column}",
            }
        )
        for column, target in links
    ]
    return OntologyPatch(domains=domains, concepts=[concept], relations=relations)


def patch_from_json(data: dict[str, Any]) -> OntologyPatch:
    return OntologyPatch.model_validate(data)
