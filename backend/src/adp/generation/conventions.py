"""整備済みの dim / fct がモデリング規約に合っているかを、決まった規則で確かめる（#134）。

規約の正本は docs/modeling-conventions.md。生成器はその規約どおりに作るが、手で書いた共通の
dim（`warehouse/models/marts/dim_*`）や、生成後に直されたモデルも同じ規約に合っているかを、
LLM を使わずに検査する（`make dbt-build` の最後。`python -m adp.generation.conventions`）。

dbt の manifest（テストと列の設定）と、ビルド済みのテーブル（列の型と不明メンバーの行）を見る。
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adp.beam import FactType
from adp.catalog.catalog import Catalog, CatalogUnavailable, quote_ident
from adp.catalog.types import is_text_type

Query = Callable[[str], list[tuple[Any, ...]]]

TIME_SPINE = "dim_date"
"""共通の暦。不明メンバーの規則の対象外（time spine を兼ねるため）。"""

RESERVED_DIM_COLUMNS = frozenset({"day", "week", "month", "quarter", "year"})


@dataclass(frozen=True)
class Violation:
    model: str
    rule: str
    """規約（docs/modeling-conventions.md）の節。"""
    message: str

    def __str__(self) -> str:
        return f"{self.model}: {self.message}（規約「{self.rule}」）"


@dataclass
class _Model:
    name: str
    node: dict[str, Any]
    column_tests: dict[str, dict[str, dict[str, Any]]]
    """列名 → テスト名 → 引数。"""
    model_tests: dict[str, dict[str, Any]]

    @property
    def is_dim(self) -> bool:
        return self.name.startswith("dim_")

    @property
    def is_accumulating(self) -> bool:
        """accumulating snapshot か（生成した fct の description に種類が入る）。"""
        return FactType.ACCUMULATING_SNAPSHOT.value in str(self.node.get("description", ""))

    @property
    def relation(self) -> str:
        n = self.node
        parts = (n["database"], n["schema"], n.get("alias") or n["name"])
        return ".".join(quote_ident(str(p)) for p in parts)

    @property
    def columns(self) -> dict[str, dict[str, Any]]:
        cols: dict[str, dict[str, Any]] = self.node.get("columns", {})
        return cols

    def tests(self, column: str) -> dict[str, dict[str, Any]]:
        return self.column_tests.get(column, {})


def _marts(manifest: dict[str, Any]) -> list[_Model]:
    nodes: dict[str, dict[str, Any]] = manifest.get("nodes", {})
    models = {
        k: _Model(v["name"], v, {}, {})
        for k, v in nodes.items()
        if v.get("resource_type") == "model" and v["name"].startswith(("dim_", "fct_"))
    }
    for v in nodes.values():
        if v.get("resource_type") != "test" or v.get("attached_node") not in models:
            continue
        meta = v.get("test_metadata") or {}
        name = meta.get("name")
        if not name:
            continue
        if meta.get("namespace"):
            name = f"{meta['namespace']}.{name}"
        kwargs: dict[str, Any] = meta.get("kwargs", {})
        model = models[v["attached_node"]]
        column = v.get("column_name")
        if column:
            model.column_tests.setdefault(column, {})[name] = kwargs
        else:
            model.model_tests[name] = kwargs
    return sorted(models.values(), key=lambda m: m.name)


def _primary_key(m: _Model) -> list[Violation]:
    if any({"unique", "not_null"} <= set(m.tests(c)) for c in m.columns):
        return []
    combo = m.model_tests.get("dbt_utils.unique_combination_of_columns")
    if combo is None:
        return [Violation(m.name, "テスト（必須）", "主キーに unique と not_null がありません")]
    keys: list[str] = combo.get("combination_of_columns", [])
    missing = [k for k in keys if "not_null" not in m.tests(k)]
    if missing:
        return [
            Violation(
                m.name,
                "テスト（必須）",
                f"粒度の列 {', '.join(missing)} に not_null がありません",
            )
        ]
    return []


def _references(m: _Model, column: str) -> str | None:
    rel = m.tests(column).get("relationships")
    if rel is None:
        return None
    to = str(rel.get("to", ""))
    found = re.search(r"ref\(\s*'(\w+)'\s*\)", to)
    return found.group(1) if found else to


def _foreign_keys(m: _Model, dim_entities: set[str]) -> list[Violation]:
    out: list[Violation] = []
    for c, spec in m.columns.items():
        if c.endswith("_key"):
            ref = _references(m, c)
            if ref is None or not ref.startswith("dim_"):
                out.append(
                    Violation(
                        m.name,
                        "テスト（必須）",
                        f"外部キー {c} に dim への relationships がありません",
                    )
                )
            if "not_null" not in m.tests(c) and not m.is_accumulating:
                out.append(
                    Violation(
                        m.name,
                        "キー",
                        f"外部キー {c} に not_null がありません（NULL は不明メンバーのキーにする）",
                    )
                )
            entity = spec.get("entity") or {}
            if entity.get("type") == "foreign" and entity.get("name") not in dim_entities:
                out.append(
                    Violation(
                        m.name,
                        "semantic model",
                        f"{c} の役割（{entity.get('name')}）は列の entity ではなく"
                        " derived_semantics に書きます",
                    )
                )
        elif c.endswith("_on") and _references(m, c) != TIME_SPINE:
            out.append(
                Violation(
                    m.name,
                    "ディメンション → dim と fct の列",
                    f"日付の列 {c} に dim_date への relationships がありません",
                )
            )
    return out


def _dim_entities(models: list[_Model]) -> set[str]:
    names: set[str] = set()
    for m in models:
        if m.is_dim:
            for spec in m.columns.values():
                entity = spec.get("entity") or {}
                if entity.get("type") == "primary":
                    names.add(str(entity.get("name")))
    return names


def structural_violations(manifest: dict[str, Any]) -> list[Violation]:
    """manifest だけで分かる違反（テスト・entity・列名）。"""
    models = _marts(manifest)
    dims = _dim_entities(models)
    out: list[Violation] = []
    for m in models:
        out += _primary_key(m)
        if not m.is_dim:
            out += _foreign_keys(m, dims)
        else:
            reserved = sorted(RESERVED_DIM_COLUMNS & set(m.columns))
            if reserved:
                out.append(
                    Violation(
                        m.name,
                        "semantic model",
                        f"列名 {', '.join(reserved)} は時間の粒度の名前と衝突します",
                    )
                )
    return out


def _literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _types(m: _Model, query: Query) -> dict[str, str]:
    n = m.node
    database, schema, name = (
        _literal(n["database"]),
        _literal(n["schema"]),
        _literal(n.get("alias") or n["name"]),
    )
    rows = query(
        "select column_name, data_type from information_schema.columns "
        f"where table_catalog = {database} and table_schema = {schema} "
        f"and table_name = {name} order by ordinal_position"
    )
    return {str(c): str(t).upper() for c, t in rows}


UNKNOWN_KEY = "md5('__unknown__')"
"""不明メンバーのキー（マクロ unknown_key() の値。規約「具体例 → unit test」）。"""


def _primary_key_column(m: _Model) -> str | None:
    for c, spec in m.columns.items():
        if (spec.get("entity") or {}).get("type") == "primary":
            return c
    return next((c for c in m.columns if {"unique", "not_null"} <= set(m.tests(c))), None)


def _unknown_member(m: _Model, types: dict[str, str], query: Query) -> list[Violation]:
    key = _primary_key_column(m)
    if key is None:
        return []  # 主キーの違反として _primary_key が報告する
    cols = list(types)
    rows = query(
        f"select {', '.join(quote_ident(c) for c in cols)} from {m.relation}"
        f" where {quote_ident(key)} = {UNKNOWN_KEY}"
    )
    if len(rows) != 1:
        return [
            Violation(
                m.name,
                "キー",
                f"不明メンバーの行（{key} が unknown_key()）が 1 行ではありません"
                f"（{len(rows)} 行）",
            )
        ]
    row = dict(zip(cols, rows[0], strict=True))
    out: list[Violation] = []
    # 自然キーは不明メンバーの行では NULL のまま（規約「キー」）
    natural = {c for c, spec in m.columns.items() if "自然キー" in str(spec.get("description"))}
    blank = [
        c
        for c in cols
        if c not in natural and c != key and is_text_type(types[c]) and row[c] is None
    ]
    if blank:
        out.append(
            Violation(
                m.name,
                "キー",
                f"不明メンバーの行の文字列の属性 {', '.join(blank)} が NULL です"
                "（「不明」か業務上の値にする）",
            )
        )
    for c in cols:
        accepted = m.tests(c).get("accepted_values")
        # dbt の accepted_values は NULL を対象にしない（NULL の文字列は上で報告する）
        if accepted is not None and row[c] is not None and row[c] not in accepted.get("values", []):
            out.append(
                Violation(
                    m.name,
                    "キー",
                    f"不明メンバーの行の {c}（{row[c]}）が accepted_values にありません",
                )
            )
    return out


def data_violations(manifest: dict[str, Any], query: Query) -> list[Violation]:
    """ビルド済みのテーブルを見て分かる違反（不明メンバーの行）。"""
    out: list[Violation] = []
    for m in _marts(manifest):
        types = _types(m, query)
        if not types:
            out.append(
                Violation(m.name, "レイヤー", "テーブルがありません（make dbt-build を実行する）")
            )
            continue
        if m.is_dim and m.name != TIME_SPINE:
            out += _unknown_member(m, types, query)
    return out


def check_warehouse(warehouse: Path) -> list[Violation]:
    """build 済みの dbt プロジェクトの違反。manifest が無ければ、それ自体を違反として返す。"""
    from adp.warehouse import warehouse_db

    path = warehouse / "target" / "manifest.json"
    if not path.exists():
        return [
            Violation(
                "manifest", "レイヤー", "dbt の manifest がありません（make dbt-build を実行する）"
            )
        ]
    manifest = json.loads(path.read_text(encoding="utf-8"))
    violations = structural_violations(manifest)
    try:
        with Catalog(warehouse_db(warehouse)).connect() as con:
            violations += data_violations(manifest, lambda sql: con.execute(sql).fetchall())
    except CatalogUnavailable as e:
        violations.append(Violation("warehouse", "レイヤー", str(e)))
    return violations


def main(argv: list[str] | None = None) -> int:
    violations = check_warehouse(Path(__file__).resolve().parents[4] / "warehouse")
    for v in violations:
        print(v)
    if violations:
        print(
            f"モデリング規約に合わない箇所が {len(violations)} 件あります"
            "（docs/modeling-conventions.md）"
        )
        return 1
    print("モデリング規約の検査: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
