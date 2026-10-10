"""ソースデータのメタデータカタログ。

DuckDB の information_schema とデータのプロファイル、dbt の manifest にある説明をまとめて、
インタビュー中やコード生成時に「その指標・属性はどのソースのどの列で作れるか」を判断する材料にする。
接続は常に読み取り専用。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FK_MIN_MATCH_RATIO = 0.95


class CatalogUnavailable(Exception):
    """DB が使えない（dbt が書き込み中でロックされているなど）。"""


def _q(identifier: str) -> str:
    """識別子を二重引用符で囲む。DB から読んだ名前にも必ず使う。"""
    return '"' + identifier.replace('"', '""') + '"'


def _table(schema: str, name: str) -> str:
    return f"{_q(schema)}.{_q(name)}"


def _plural_forms(stem: str) -> set[str]:
    return {stem, stem + "s", stem + "es", stem[:-1] + "ies" if stem.endswith("y") else stem}


class ColumnInfo(BaseModel):
    name: str
    data_type: str
    description: str = ""


class TableInfo(BaseModel):
    schema_name: str
    name: str
    description: str = ""
    row_count: int

    @property
    def qualified(self) -> str:
        return f"{self.schema_name}.{self.name}"


class ForeignKeyCandidate(BaseModel):
    column: str
    references: str
    """参照先の `<schema>.<table>.<column>`。"""
    match_ratio: float
    """非 NULL 値のうち参照先に存在する割合。"""


class TableDetail(TableInfo):
    columns: list[ColumnInfo]
    primary_key_candidates: list[str]
    foreign_key_candidates: list[ForeignKeyCandidate]


class ColumnProfile(BaseModel):
    table: str
    column: str
    data_type: str
    row_count: int
    null_count: int
    distinct_count: int
    min: str | None
    max: str | None
    top_values: list[tuple[str | None, int]]
    is_unique: bool


class ColumnMatch(BaseModel):
    table: str
    column: str
    data_type: str
    description: str


class Catalog:
    def __init__(
        self,
        db_path: Path,
        manifest_path: Path | None = None,
        schemas: tuple[str, ...] = ("raw",),
    ) -> None:
        self.db_path = db_path
        self.schemas = schemas
        self._descriptions = _load_descriptions(manifest_path)

    @contextmanager
    def _connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        # 読み取り専用に加えて外部アクセス（COPY TO、ファイル読み込み、ATTACH）を禁止する
        last: Exception | None = None
        for attempt in range(4):
            try:
                con = duckdb.connect(
                    str(self.db_path),
                    read_only=True,
                    config={"enable_external_access": False},
                )
                break
            except duckdb.IOException as e:  # dbt などが書き込み中でロックされている
                last = e
                time.sleep(0.25 * (attempt + 1))
        else:
            raise CatalogUnavailable(
                "データベースが使用中です。しばらくしてから再実行してください。"
            ) from last
        try:
            yield con
        finally:
            con.close()

    def _resolve(self, table: str) -> tuple[str, str]:
        """`schema.table` か `table` を検証して分解する。SQL に埋め込むので識別子のみ許可。"""
        schema, _, name = table.rpartition(".")
        schema = schema or self.schemas[0]
        if not (_IDENT.match(schema) and _IDENT.match(name)) or schema not in self.schemas:
            raise ValueError(f"対象外のテーブルです: {table}")
        return schema, name

    def list_sources(self) -> list[TableInfo]:
        with self._connect() as con:
            rows = con.execute(
                "select table_schema, table_name from information_schema.tables"
                " where table_schema in (select unnest(?)) order by 1, 2",
                [list(self.schemas)],
            ).fetchall()
            result = []
            for schema, name in rows:
                count = _scalar(con, f"select count(*) from {_table(schema, name)}")
                result.append(
                    TableInfo(
                        schema_name=schema,
                        name=name,
                        description=self._descriptions.get((schema, name, None), ""),
                        row_count=int(count),
                    )
                )
            return result

    def _columns(self, con: duckdb.DuckDBPyConnection, schema: str, name: str) -> list[ColumnInfo]:
        rows = con.execute(
            "select column_name, data_type from information_schema.columns"
            " where table_schema = ? and table_name = ? order by ordinal_position",
            [schema, name],
        ).fetchall()
        if not rows:
            raise ValueError(f"テーブルが見つかりません: {schema}.{name}")
        return [
            ColumnInfo(
                name=c, data_type=t, description=self._descriptions.get((schema, name, c), "")
            )
            for c, t in rows
        ]

    def columns(self, table: str) -> list[ColumnInfo]:
        """列の名前・型・説明だけを返す（プロファイリングをしない軽い版）。"""
        schema, name = self._resolve(table)
        with self._connect() as con:
            return self._columns(con, schema, name)

    def describe_table(self, table: str) -> TableDetail:
        schema, name = self._resolve(table)
        with self._connect() as con:
            columns = self._columns(con, schema, name)
            count = int(_scalar(con, f"select count(*) from {_table(schema, name)}"))
            pk = [c.name for c in columns if count > 0 and _is_unique(con, schema, name, c.name)]
            fks = self._foreign_keys(con, schema, name, columns)
        return TableDetail(
            schema_name=schema,
            name=name,
            description=self._descriptions.get((schema, name, None), ""),
            row_count=count,
            columns=columns,
            primary_key_candidates=pk,
            foreign_key_candidates=fks,
        )

    def _foreign_keys(
        self,
        con: duckdb.DuckDBPyConnection,
        schema: str,
        name: str,
        columns: list[ColumnInfo],
    ) -> list[ForeignKeyCandidate]:
        """`<x>_id` の列の参照先候補。

        同じ名前の列を主キーに持つ他テーブルのうち、値の一致率が FK_MIN_MATCH_RATIO 以上のもの。
        テーブル名が列名と対応する（`customer_id` → `customers`）候補があればそれだけを返す。
        自テーブルの主キーでもある列は、名前が対応するテーブル以外を参照先にしない
        （親テーブルの主キーを子テーブルへの FK と誤認しないため）。
        """
        candidates = []
        for c in columns:
            if not c.name.endswith("_id"):
                continue
            own_pk = _is_unique(con, schema, name, c.name)
            named = _plural_forms(c.name.removesuffix("_id"))
            refs = con.execute(
                "select table_schema, table_name from information_schema.columns"
                " where column_name = ? and table_schema in (select unnest(?))"
                " and not (table_schema = ? and table_name = ?)",
                [c.name, list(self.schemas), schema, name],
            ).fetchall()
            found = []
            for rs, rt in refs:
                is_named = rt in named
                if own_pk and not is_named:
                    continue
                try:
                    if not _is_unique(con, rs, rt, c.name):
                        continue
                    ratio = _scalar(
                        con,
                        f"select coalesce(avg(case when r.{_q(c.name)} is null then 0 else 1.0"
                        f" end), 0) from {_table(schema, name)} t left join {_table(rs, rt)} r"
                        f" on cast(t.{_q(c.name)} as varchar) = cast(r.{_q(c.name)} as varchar)"
                        f" where t.{_q(c.name)} is not null",
                    )
                except duckdb.Error:
                    continue
                if float(ratio) >= FK_MIN_MATCH_RATIO:
                    found.append((is_named, f"{rs}.{rt}.{c.name}", float(ratio)))
            if any(n for n, _, _ in found):
                found = [f for f in found if f[0]]
            candidates += [
                ForeignKeyCandidate(column=c.name, references=ref, match_ratio=r)
                for _, ref, r in found
            ]
        return candidates

    def profile_column(self, table: str, column: str, top: int = 10) -> ColumnProfile:
        schema, name = self._resolve(table)
        if not _IDENT.match(column):
            raise ValueError(f"不正な列名です: {column}")
        with self._connect() as con:
            info = {c.name: c for c in self._columns(con, schema, name)}
            if column not in info:
                raise ValueError(f"列が見つかりません: {schema}.{name}.{column}")
            q, col = _table(schema, name), _q(column)
            row = con.execute(
                f"select count(*), count(*) - count({col}), count(distinct {col}),"
                f" cast(min({col}) as varchar), cast(max({col}) as varchar) from {q}"
            ).fetchone()
            assert row is not None
            values = con.execute(
                f"select cast({col} as varchar) v, count(*) n from {q}"
                f" group by 1 order by n desc, v limit {int(top)}"
            ).fetchall()
        total, nulls, distinct, lo, hi = row
        return ColumnProfile(
            table=f"{schema}.{name}",
            column=column,
            data_type=info[column].data_type,
            row_count=total,
            null_count=nulls,
            distinct_count=distinct,
            min=lo,
            max=hi,
            top_values=[(v, int(n)) for v, n in values],
            is_unique=total > 0 and nulls == 0 and distinct == total,
        )

    def search_columns(self, query: str) -> list[ColumnMatch]:
        """列名・説明・テーブル説明に、いずれかの語を含む列を返す（大文字小文字は区別しない）。"""
        terms = [t.lower() for t in query.split() if t]
        matches = []
        with self._connect() as con:
            rows = con.execute(
                "select table_schema, table_name, column_name, data_type"
                " from information_schema.columns where table_schema in (select unnest(?))"
                " order by 1, 2, ordinal_position",
                [list(self.schemas)],
            ).fetchall()
        for schema, table, column, dtype in rows:
            desc = self._descriptions.get((schema, table, column), "")
            table_desc = self._descriptions.get((schema, table, None), "")
            haystack = f"{table} {column} {desc} {table_desc}".lower()
            if any(t in haystack for t in terms):
                matches.append(
                    ColumnMatch(
                        table=f"{schema}.{table}", column=column, data_type=dtype, description=desc
                    )
                )
        return matches


def _is_unique(con: duckdb.DuckDBPyConnection, schema: str, name: str, column: str) -> bool:
    col = _q(column)
    return bool(
        _scalar(
            con,
            f"select count(*) > 0 and count(distinct {col}) = count(*) and count({col}) = count(*)"
            f" from {_table(schema, name)}",
        )
    )


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> Any:
    row = con.execute(sql).fetchone()
    assert row is not None
    return row[0]


def _load_descriptions(manifest_path: Path | None) -> dict[tuple[str, str, str | None], str]:
    """dbt の manifest.json から sources の説明を読む。キーは (schema, table, column or None)。"""
    if manifest_path is None or not manifest_path.exists():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result: dict[tuple[str, str, str | None], str] = {}
    nodes = list(manifest.get("sources", {}).values()) + [
        n for n in manifest.get("nodes", {}).values() if n.get("resource_type") == "model"
    ]
    for node in nodes:
        schema, table = node["schema"], node.get("identifier") or node.get("alias") or node["name"]
        result[(schema, table, None)] = node.get("description", "")
        for col in node.get("columns", {}).values():
            result[(schema, table, col["name"])] = col.get("description", "")
    return result
