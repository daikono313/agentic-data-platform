"""WarehouseInfo の本番実装（warehouse/ のファイルとソースカタログを見る）。"""

from __future__ import annotations

from functools import cache
from pathlib import Path

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

    def write(self, files: list[GeneratedFile]) -> list[Path]:
        written = []
        for f in files:
            path = self.warehouse_dir / f.path
            if path.exists():
                raise FileExistsError(f"既存のファイルは上書きしません: {f.path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f.content, encoding="utf-8")
            written.append(path)
        return written
