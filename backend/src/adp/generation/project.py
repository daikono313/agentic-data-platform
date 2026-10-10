"""WarehouseInfo の本番実装（warehouse/ のファイルとソースカタログを見る）。"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

from adp.catalog.catalog import Catalog
from adp.generation.dbt import GeneratedFile


class ProjectInfo:
    def __init__(self, warehouse_dir: Path, catalog: Catalog) -> None:
        self.warehouse_dir = warehouse_dir
        self.catalog = catalog
        self._describe = cache(catalog.describe_table)

    def existing_models(self) -> set[str]:
        return {p.stem for p in (self.warehouse_dir / "models").rglob("*.sql")}

    def source_columns(self, table: str) -> list[tuple[str, str]]:
        return [(c.name, c.data_type) for c in self._describe(table).columns]

    def source_primary_key(self, table: str) -> str | None:
        pks = self._describe(table).primary_key_candidates
        stem = table.rpartition(".")[2]
        preferred = [p for p in pks if p.removesuffix("_id") in (stem, stem.rstrip("s"))]
        candidates = preferred or pks
        return candidates[0] if candidates else None

    def _yaml_docs(self) -> list[dict[str, Any]]:
        docs = []
        for p in (self.warehouse_dir / "models").rglob("*.yml"):
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                docs.append(data)
        return docs

    def existing_columns(self, model: str) -> set[str]:
        for doc in self._yaml_docs():
            for m in doc.get("models", []) or []:
                if isinstance(m, dict) and m.get("name") == model:
                    return {c["name"] for c in m.get("columns", []) if isinstance(c, dict)}
        return set()

    def declared_sources(self) -> set[str]:
        declared = set()
        for doc in self._yaml_docs():
            for src in doc.get("sources", []) or []:
                if isinstance(src, dict):
                    schema = src.get("schema", src.get("name"))
                    declared |= {f"{schema}.{t['name']}" for t in src.get("tables", [])}
        return declared

    def distinct_values(self, table: str, column: str, limit: int) -> list[str] | None:
        profile = self.catalog.profile_column(table, column, top=limit)
        if profile.distinct_count > limit:
            return None
        return sorted(v for v, _ in profile.top_values if v is not None)

    def write(self, files: list[GeneratedFile]) -> list[Path]:
        """生成ファイルを書く。1 つでも既存なら、何も書かずにエラーにする。"""
        conflicts = [f.path for f in files if (self.warehouse_dir / f.path).exists()]
        if conflicts:
            raise FileExistsError(f"既存のファイルは上書きしません: {', '.join(conflicts)}")
        written = []
        for f in files:
            path = self.warehouse_dir / f.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f.content, encoding="utf-8")
            written.append(path)
        return written
