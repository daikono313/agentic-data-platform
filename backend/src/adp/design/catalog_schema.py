"""SchemaInfo の本番実装（ソースカタログを使う）。"""

from __future__ import annotations

from functools import cache

from adp.catalog.catalog import Catalog
from adp.design.builder import ForeignKey


class CatalogSchema:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self._describe = cache(catalog.describe_table)

    def columns(self, table: str) -> list[str]:
        try:
            return [c.name for c in self._describe(table).columns]
        except ValueError:
            return []

    def foreign_keys(self, table: str) -> list[ForeignKey]:
        try:
            detail = self._describe(table)
        except ValueError:
            return []
        result = []
        for fk in detail.foreign_key_candidates:
            schema, ref_table, ref_column = fk.references.split(".")
            result.append(ForeignKey(fk.column, f"{schema}.{ref_table}", ref_column))
        return result
