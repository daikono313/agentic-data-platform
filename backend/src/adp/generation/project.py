"""WarehouseInfo の本番実装（warehouse/ のファイルとソースカタログを見る）。"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from adp.catalog.catalog import Catalog
from adp.generation.dbt import GeneratedFile

# 生成器が出す Jinja の形（これ以外は書かない。BEAM の文字列から env_var・run_query などを
# 差し込ませないための最後の確認。#46）
_ALLOWED_JINJA = [
    re.compile(r"ref\('[a-z_][a-z0-9_]*'\)"),
    re.compile(r"source\('[a-z_][a-z0-9_]*', '[a-z_][a-z0-9_]*'\)"),
    re.compile(r"unknown_key\(\)"),
    re.compile(r"dim_key\('[^'{}]*'\)|dim_key\(\"[^\"{}]*\"\)"),
    # YAML のシングルクォートの中では ' が '' になる
    re.compile(r"Dimension\(('|'')[a-z_][a-z0-9_]*\1\)"),
]
_JINJA = re.compile(r"\{\{(.*?)\}\}|\{%|%\}|\{#|#\}", re.DOTALL)


def unsafe_jinja(content: str) -> list[str]:
    """生成物の中の、生成器が出す形以外の Jinja。"""
    found = []
    for m in _JINJA.finditer(content):
        inner = (m.group(1) or "").strip()
        if not m.group(1) or not any(p.fullmatch(inner) for p in _ALLOWED_JINJA):
            found.append(m.group(0)[:60])
    # 許可した形を取り除いた後に、括弧が残っていれば不正（入れ子など）
    rest = _JINJA.sub("", content)
    if "{{" in rest or "}}" in rest:
        found.append("{{ / }}")
    return found


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

    def model_file(self, model: str, suffix: str) -> tuple[str, str] | None:
        """モデルの SQL（suffix=".sql"）か、モデルを定義している YAML（".yml"）。

        (warehouse/ からの相対パス, 内容)。
        """
        for p in sorted((self.warehouse_dir / "models").rglob(f"*{suffix}")):
            text = p.read_text(encoding="utf-8")
            if suffix == ".sql":
                if p.stem == model:
                    return str(p.relative_to(self.warehouse_dir)), text
                continue
            data = yaml.safe_load(text)
            if isinstance(data, dict) and any(
                isinstance(m, dict) and m.get("name") == model for m in data.get("models", []) or []
            ):
                return str(p.relative_to(self.warehouse_dir)), text
        return None

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
        """生成ファイルを書く。1 つでも既存なら、何も書かずにエラーにする。

        既存のファイルを書き換えてよいのは、元の内容（replaces）が今の内容と同じときだけ。
        """
        unsafe = [f"{f.path}: {j}" for f in files for j in unsafe_jinja(f.content)]
        if unsafe:
            raise ValueError("生成物に使えない Jinja があります: " + " / ".join(unsafe[:5]))
        conflicts = [
            f.path
            for f in files
            if (self.warehouse_dir / f.path).exists()
            and (
                f.replaces is None
                or (self.warehouse_dir / f.path).read_text(encoding="utf-8") != f.replaces
            )
        ]
        conflicts += [
            f.path
            for f in files
            if f.replaces is not None and not (self.warehouse_dir / f.path).exists()
        ]
        if conflicts:
            raise FileExistsError(f"既存のファイルは上書きしません: {', '.join(conflicts)}")
        written = []
        for f in files:
            path = self.warehouse_dir / f.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f.content, encoding="utf-8")
            written.append(path)
        return written
