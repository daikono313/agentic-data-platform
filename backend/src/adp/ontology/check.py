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


def _manifest_models(manifest_path: Path) -> set[str] | None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {
        n["name"] for n in manifest.get("nodes", {}).values() if n.get("resource_type") == "model"
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
        for a in c.attributes:
            if not has_column(c.source, a.column):
                errors.append(f"概念 {c.name} の属性 {a.name}: 列 {c.source}.{a.column} が無い")

    sources = {c.name: c.source for c in ontology.concepts}
    for r in ontology.relations:
        table, _, column = r.via.rpartition(".")
        if not has_column(table, column):
            errors.append(f"関係「{r.verb}」: 列 {r.via} が無い")
        elif table not in (sources[r.from_], sources[r.to]):
            errors.append(
                f"関係「{r.verb}」: 列 {r.via} が、両端の概念の源泉"
                f"（{sources[r.from_]} / {sources[r.to]}）のどちらにも無い"
            )

    if manifest_path is not None:
        models = _manifest_models(manifest_path)
        if models is None:
            errors.append("dbt の manifest を読めない（make dbt-build を実行する）")
        else:
            for c in ontology.concepts:
                if c.model is not None and c.model not in models:
                    errors.append(f"概念 {c.name}: モデル {c.model} が dbt に無い")

    mapped = {c.source for c in ontology.concepts}
    return CheckResult(errors=errors, unmapped_sources=sorted(tables - mapped))
