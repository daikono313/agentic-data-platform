"""オントロジーと、源泉（カタログ）・整備済みモデル（dbt の manifest）との食い違い（#67）。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from adp.catalog.catalog import Catalog
from adp.ontology.models import Ontology


class CheckResult(BaseModel):
    errors: list[str]
    """直さないといけない食い違い（CI で落とす）。"""
    unmapped_sources: list[str]
    """カタログにあるが、どの概念の源泉にもなっていない源泉（未整理。#69 で使う）。"""
    unmapped_models: list[str] = []
    """どの概念にも対応していない dim / fct（dim_date のような共通の暦は対象外にしてよい）。"""


def _model_sources(manifest_path: Path) -> dict[str, set[str]] | None:
    """モデル名 → そのモデルが（staging などを経由して）読んでいる源泉（`<schema>.<table>`）。"""
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

    return {n["name"]: upstream(k) for k, n in nodes.items() if n.get("resource_type") == "model"}


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
    if manifest_path is not None:
        models = _model_sources(manifest_path)
        if models is None:
            errors.append("dbt の manifest を読めない（make dbt-build を実行する）")
        else:
            for c in ontology.concepts:
                if c.model is None:
                    continue
                if c.model not in models:
                    errors.append(f"概念 {c.name}: モデル {c.model} が dbt に無い")
                elif c.source not in models[c.model]:
                    errors.append(
                        f"概念 {c.name}: モデル {c.model} は源泉 {c.source} を読んでいない"
                        f"（読んでいる源泉: {', '.join(sorted(models[c.model])) or 'なし'}）"
                    )
            mapped_models = {c.model for c in ontology.concepts if c.model}
            unmapped_models = sorted(
                m for m in models if m.startswith(("dim_", "fct_")) and m not in mapped_models
            )

    mapped = {c.source for c in ontology.concepts}
    return CheckResult(
        errors=errors,
        unmapped_sources=sorted(tables - mapped),
        unmapped_models=unmapped_models,
    )
