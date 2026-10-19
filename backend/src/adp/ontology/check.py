"""オントロジーと、源泉（カタログ）・整備済みモデル（dbt の manifest）との食い違い（#67）。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from adp.catalog.catalog import Catalog
from adp.ontology.models import Concept, Ontology
from adp.yaml_docs import load_yaml_docs, named


class CheckResult(BaseModel):
    errors: list[str]
    """直さないといけない食い違い（CI で落とす）。"""
    unmapped_sources: list[str]
    """カタログにあるが、どの概念の源泉にもなっていない源泉（未整理。#69 で使う）。"""
    unmapped_models: list[str] = []
    """どの概念にも対応していない共通のモデル（dim_date）。ほかの dim / fct は errors に出る
    （#114）。"""
    unmodeled_attributes: list[str] = []
    """もの・人・場所の概念の属性のうち、その dim に列が無いもの（`<dim>.<列>（ラベル）`。#137）。
    要求で使われたときに生成が dim に列を足すので、食い違いではなく表示だけ。"""


SHARED_MODELS = frozenset({"dim_date"})
"""どの概念にも属さない共通のモデル（暦）。"""


class _DbtModel(BaseModel):
    sources: set[str]
    """（staging などを経由して）読んでいる源泉（`<schema>.<table>`）。"""
    columns: dict[str, str]
    """yml に書いた列 → その説明。"""


def _dbt_models(manifest_path: Path) -> dict[str, _DbtModel] | None:
    """モデル名 → そのモデルが読んでいる源泉と列。"""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    nodes = manifest.get("nodes", {})
    sources = {
        k: f"{v.get('schema')}.{v.get('identifier') or v.get('name')}"
        for k, v in manifest.get("sources", {}).items()
    }
    parents: dict[str, list[str]] = manifest.get("parent_map", {})

    def upstream(node: str) -> set[str]:
        found: set[str] = set()
        seen: set[str] = set()
        stack = [node]
        while stack:
            for p in parents.get(stack.pop(), []):
                if p in seen:
                    continue
                seen.add(p)
                if p in sources:
                    found.add(sources[p])
                elif p.startswith("model."):
                    stack.append(p)
        return found

    return {
        n["name"]: _DbtModel(
            sources=upstream(k),
            columns={
                name: str((col or {}).get("description") or "")
                for name, col in (n.get("columns") or {}).items()
            },
        )
        for k, n in nodes.items()
        if n.get("resource_type") == "model"
    }


def check(ontology: Ontology, catalog: Catalog, manifest_path: Path | None) -> CheckResult:
    errors: list[str] = []
    tables = {t.qualified for t in catalog.list_sources()}
    columns: dict[str, set[str]] = {}

    def has_column(table: str, column: str) -> bool:
        if table not in tables:
            return False
        if table not in columns:
            columns[table] = {c.name for c in catalog.columns(table)}
        return column in columns[table]

    for c in ontology.concepts:
        if c.source not in tables:
            errors.append(f"概念 {c.name}: 源泉 {c.source} がカタログに無い")
            continue
        if not has_column(c.source, c.key):
            errors.append(f"概念 {c.name}: キー {c.source}.{c.key} が無い")
        elif not catalog.is_unique(c.source, c.key):
            errors.append(f"概念 {c.name}: キー {c.source}.{c.key} が一意でない")
        for a in c.attributes:
            if not has_column(c.source, a.column):
                errors.append(f"概念 {c.name} の属性 {a.name}: 列 {c.source}.{a.column} が無い")

    errors += ontology.duplicate_labels()

    by_name = {c.name: c for c in ontology.concepts}
    for r in ontology.relations:
        table, _, column = r.via.rpartition(".")
        src, dst = by_name[r.from_], by_name[r.to]
        if not has_column(table, column):
            errors.append(f"関係「{r.verb}」: 列 {r.via} が無い")
        elif not (
            (table == src.source and column == dst.key)
            or (table == dst.source and column == src.key)
        ):
            # 関係の列は、一方の源泉にある、もう一方のキー（外部キー）
            errors.append(
                f"関係「{r.verb}」: 列 {r.via} が、{src.source} にある {dst.key} か、"
                f"{dst.source} にある {src.key} のどちらでもない"
            )

    unmapped_models: list[str] = []
    unmodeled: list[str] = []
    if manifest_path is not None:
        models = _dbt_models(manifest_path)
        if models is None:
            errors.append("dbt の manifest を読めない（make dbt-build を実行する）")
        else:
            for c in ontology.concepts:
                if c.model is None:
                    continue
                if c.model not in models:
                    errors.append(f"概念 {c.name}: モデル {c.model} が dbt に無い")
                elif c.source not in models[c.model].sources:
                    read = ", ".join(sorted(models[c.model].sources)) or "なし"
                    errors.append(
                        f"概念 {c.name}: モデル {c.model} は源泉 {c.source} を読んでいない"
                        f"（読んでいる源泉: {read}）"
                    )
                elif c.kind == "entity":
                    # fct の列は measure の名前（数量 → movement_qty など）と、measure のラベル
                    # （「値引額(税抜)」など）から作る説明になるので、dim だけ見る
                    errors += _description_mismatches(c, models[c.model])
                    unmodeled += [
                        f"{c.model}.{a.column}（{a.label}）"
                        for a in c.attributes
                        if a.column not in models[c.model].columns
                    ]
            mapped_models = {c.model for c in ontology.concepts if c.model}
            unmapped = sorted(
                m for m in models if m.startswith(("dim_", "fct_")) and m not in mapped_models
            )
            unmapped_models = [m for m in unmapped if m in SHARED_MODELS]
            # 用途の分からない fact / dim を増やさない（#114。概念ごとにモデルは 1 つなので、同じ
            # 用途の 2 つ目のモデルは概念に結び付かず、ここで見つかる）。暦（dim_date）は対象外
            for m in unmapped:
                if m not in SHARED_MODELS:
                    errors.append(
                        f"モデル {m} がオントロジーのどの概念にも結び付いていない。用途が同じ既存の"
                        "モデルがあればそれを使い、無ければオントロジーにこのモデル用の概念を足す"
                    )

    mapped = {c.source for c in ontology.concepts}
    return CheckResult(
        errors=errors,
        unmapped_sources=sorted(tables - mapped),
        unmapped_models=unmapped_models,
        unmodeled_attributes=unmodeled,
    )


def _description_mismatches(concept: Concept, model: _DbtModel) -> list[str]:
    """dim の列（属性と同じ列名のもの）の説明が、属性のラベルで始まっていないもの（#137）。

    説明はラベルの後に補足（「（マスタなし）」「（個）」など）を付けてよい。"""
    errors = []
    for a in concept.attributes:
        text = model.columns.get(a.column)
        if text is not None and not (text == a.label or text.startswith(f"{a.label}（")):
            errors.append(
                f"{concept.model}.{a.column} の説明「{text}」が、オントロジーの属性 "
                f"{concept.name}.{a.name}「{a.label}」で始まっていない"
            )
    return errors


def beam_mismatches(ontology: Ontology, beam_dir: Path) -> list[str]:
    """BEAM 成果物（beam/*.yml）の属性のラベルが、同じ源泉の列のオントロジーの属性と違うもの
    （#137。オントロジーが業務の言葉の正本）。式（expr）で作る属性は、式が使う属性の列が 1 つの
    ときだけ見る（Ontology.expr_column）。出来事をまたいで
    共通のディメンションの属性は、設計の検査（design.builder）と同じく最初の 1 回だけ見る。

    ontology は beam に依存しない（backend/CLAUDE.md の依存の向き）ので、成果物は YAML のまま読む。
    """
    errors: list[str] = []
    docs = load_yaml_docs(beam_dir, "*.yml")
    for problem in docs.problems:
        errors.append(f"{problem.path.name}: 読めない（{problem.reason}）")
    for doc in docs.docs:
        seen: set[tuple[str, str]] = set()
        for event in named(doc.data.get("events")):
            for dim in named(event.get("dimensions")):
                for attr in named(dim.get("attributes")):
                    source = attr.get("source")
                    key = (str(dim["name"]), str(attr["name"]))
                    if not isinstance(source, dict) or key in seen:
                        continue
                    table = str(source.get("table"))
                    column = source.get("column") or ontology.expr_column(
                        table, str(source.get("expr") or "")
                    )
                    if not column:
                        continue
                    seen.add(key)
                    mismatch = ontology.word_mismatch(
                        table, str(column), str(attr.get("label") or "")
                    )
                    if mismatch:
                        errors.append(f"{doc.path.name}: {dim['name']}.{attr['name']}: {mismatch}")
    return errors
