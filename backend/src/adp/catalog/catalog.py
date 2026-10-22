"""ソースデータのメタデータカタログ。

DuckDB の information_schema とデータのプロファイル、dbt の manifest にある説明をまとめて、
インタビュー中やコード生成時に「その指標・属性はどのソースのどの列で作れるか」を判断する材料にする。
接続は常に読み取り専用。
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import duckdb
from pydantic import BaseModel

from adp.yaml_docs import load_yaml_docs, named

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FK_MIN_MATCH_RATIO = 0.95
_PII_TOKENS = frozenset(
    [
        "email",
        "mail",
        "phone",
        "tel",
        "mobile",
        "fax",
        "address",
        "addr",
        "zip",
        "zipcode",
        "postcode",
        "birth",
        "birthday",
        "birthdate",
        "dob",
        "kana",
        "furigana",
        "username",
    ]
)
"""1 語で個人情報と推定する語（列名を語に分けて比べる）。"""
_PII_PAIRS = frozenset(
    {("e", "mail"), ("post", "code"), ("postal", "code"), ("zip", "code"), ("phone", "number")}
)
_PERSON = frozenset(
    [
        "customer",
        "cust",
        "user",
        "person",
        "member",
        "contact",
        "staff",
        "employee",
        "recipient",
        "client",
        "first",
        "last",
        "full",
        "given",
        "family",
    ]
)
_NOT_PII_LAST = frozenset(
    [
        "id",
        "key",
        "prefecture",
        "pref",
        "country",
        "region",
        "os",
        "flag",
        "verified",
        "type",
        "domain",
        "count",
    ]
)
"""最後の語がこれなら個人情報ではない（キー・区分・フラグ。例: shipping_address_prefecture）。"""
_PII_JA = re.compile(r"氏名|お名前|顧客名|電話|住所|郵便|生年月日|メール|フリガナ|ふりがな")


def _words(column: str) -> list[str]:
    """列名を小文字の語に分ける（snake_case・camelCase・末尾の数字。CUSTOMER_NAME も）。"""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", column)
    return [re.sub(r"\d+$", "", w) for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w]


def looks_like_pii(column: str) -> bool:
    """列名から個人情報と推定する（dbt の sources の meta.pii が無くても値を出さない。#81）。

    集計に使う区分（都道府県・地方・性別など）は含めない。迷う列は meta.pii で明示する。
    """
    if _PII_JA.search(column):
        return True
    words = _words(column)
    if not words or words[-1] in _NOT_PII_LAST or words[0] in {"is", "has"} or "opt" in words:
        return False
    if words == ["name"] or any(w in _PII_TOKENS for w in words):
        return True
    pairs = set(zip(words, words[1:], strict=False))
    return bool(pairs & _PII_PAIRS) or any(a in _PERSON and b == "name" for a, b in pairs)


_CAST_TYPE = re.compile(
    r"^(integer|bigint|double|varchar|date|timestamp|boolean|decimal\(\d{1,2},\s*\d{1,2}\))$"
)


class CatalogUnavailable(Exception):
    """DB が使えない（dbt が書き込み中でロックされているなど）。"""


def quote_ident(identifier: str) -> str:
    """識別子を二重引用符で囲む。DB から読んだ名前にも必ず使う。"""
    return '"' + identifier.replace('"', '""') + '"'


def _table(schema: str, name: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(name)}"


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


SAMPLE_MAX = 5
SAMPLE_VALUE_MAX = 100
"""sample_rows で返す 1 つの値の最大文字数（長い自由記述・JSON で結果が大きくならないように）。"""
MASKED = "（個人情報のため非表示）"
FREE_TEXT_MASKED = "（自由記述のため非表示）"
_FREE_TEXT = re.compile(
    r"(^|_)(memo|note|notes|comment|comments|remark|remarks|review|review_text|message|"
    r"body|free_text|inquiry|shipping_name|ship_to|recipient|contact|card|card_number|"
    r"credit_card|password|passwd|secret|token|my_number|mynumber)($|_)",
    re.IGNORECASE,
)
"""行の値として出さない列（自由記述は個人情報を含みうる。秘密情報）。sample_rows だけで伏せる。"""


class SampleRows(BaseModel):
    """テーブルの実際の行（インタビューの具体例用。個人情報の列は伏せる。#80）。"""

    table: str
    columns: list[str]
    rows: list[list[str | None]]
    masked_columns: list[str] = []


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
    monthly: list[tuple[str, int]] | None = None
    """日付・日時の列の月ごとの件数（YYYY-MM。0 件の月も含む）。プロファイル画面用（#71）。"""
    histogram: list[tuple[float, float, int]] | None = None
    """数値の列の分布（下限・上限・件数。等幅 HISTOGRAM_BINS 区間）。"""
    masked: bool = False
    """個人情報の列のため、値（最小・最大・よく出る値・分布）を出していない（#81）。"""


HISTOGRAM_BINS = 10
MONTHLY_MAX = 120
"""月ごとの件数を出す最大の月数（それより長い期間は出さない）。"""


class TableProfile(TableDetail):
    profiles: list[ColumnProfile]


class ColumnMatch(BaseModel):
    table: str
    column: str
    data_type: str
    description: str


class FactColumn(BaseModel):
    name: str
    description: str = ""


class FactMetric(BaseModel):
    name: str
    label: str
    expr: str | None = None
    agg: str | None = None


class FactSummary(BaseModel):
    """整備済みの fact（#113: 新しい指標は、まず既存の fact で作れないかを確かめる）。"""

    name: str
    description: str
    source: str | None = None
    """1 行の元になる源泉（ベースのテーブル。例: raw.inventory）。同じ源泉の fact は作らない。"""
    grain: str | None = None
    """1 行が何を表すか（model の説明の「粒度: …」から）。"""
    columns: list[FactColumn]
    """measure や切り口に使える列（dbt の model YAML の列と説明）。"""
    joins: list[str]
    """結合できる dim の entity（店舗・商品など）。"""
    metrics: list[FactMetric]
    """この fact の既存の指標。"""


class Catalog:
    def __init__(
        self,
        db_path: Path,
        manifest_path: Path | None = None,
        schemas: tuple[str, ...] = ("raw",),
    ) -> None:
        self.db_path = db_path
        self.schemas = schemas
        self.manifest_path = manifest_path
        self._descriptions = _load_descriptions(manifest_path)
        # manifest が無い（生成の worktree など）ときも、DB の隣の dbt プロジェクトの sources を読む
        self._pii = _load_pii(manifest_path) | _load_pii_yaml(db_path.parent.parent)

    def existing_facts(self) -> list[FactSummary]:
        """整備済みの fact と、その列・結合できる dim・既存の指標（dbt の manifest から）。

        manifest が無ければ空。semantic_manifest.json が無ければ、指標なしで manifest の fct だけ。
        値は読まない（列名・説明・指標の定義だけ）。
        """
        if self.manifest_path is None:
            return []
        manifest = _read_json(self.manifest_path)
        if manifest is None:
            return []
        semantic = _read_json(self.manifest_path.parent / "semantic_manifest.json") or {}
        nodes = {
            n.get("name"): n
            for n in _values(manifest.get("nodes"))
            if n.get("resource_type") == "model" and n.get("name")
        }
        sources = {
            k: f"{n.get('schema') or ''}.{n.get('identifier') or n.get('name') or ''}"
            for k, n in (manifest.get("sources") or {}).items()
            if isinstance(n, dict)
        }
        unique_ids = {n.get("unique_id"): name for name, n in nodes.items() if n.get("unique_id")}
        metrics = _metrics_by_model(_list(semantic.get("metrics")))
        semantic_models = {
            str(sm["name"]): sm
            for sm in _list(semantic.get("semantic_models"))
            if isinstance(sm.get("name"), str)
        }
        names = sorted(
            {n for n in semantic_models if n.startswith("fct_")}
            | {n for n in nodes if isinstance(n, str) and n.startswith("fct_")}
        )
        out = []
        for name in names:
            sm = semantic_models.get(name, {})
            node = nodes.get(name, {})
            description = str(sm.get("description") or node.get("description") or "")
            columns = [
                FactColumn(name=str(c["name"]), description=str(c.get("description") or ""))
                for c in _values(node.get("columns"))
                if c.get("name")
            ]
            if sm:
                joins = [
                    str(e["name"])
                    for e in _list(sm.get("entities"))
                    if e.get("type") == "foreign" and e.get("name") and e.get("name") != "date"
                ]
            else:
                joins = [c.name.removesuffix("_key") for c in columns if c.name.endswith("_key")]
            grain = re.search(r"粒度[:：]\s*(.+)", description)
            out.append(
                FactSummary(
                    name=name,
                    description=description,
                    source=_base_source(node, nodes, unique_ids, sources),
                    grain=grain.group(1).strip() if grain else None,
                    columns=columns,
                    joins=joins,
                    metrics=metrics.get(name, []),
                )
            )
        return out

    def is_pii(self, schema: str, name: str, column: str) -> bool:
        """個人情報の列か（sources の meta.pii、または列名からの推定。ID・キーは除く）。"""
        flagged = self._pii.get((schema, name, column))
        if flagged is not None:
            return flagged
        return looks_like_pii(column)

    @contextmanager
    def connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """読み取り専用の接続。ロック中は少し待って再試行し、だめなら CatalogUnavailable。"""
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
        with self.connect() as con:
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
        with self.connect() as con:
            return self._columns(con, schema, name)

    def describe_table(self, table: str) -> TableDetail:
        schema, name = self._resolve(table)
        with self.connect() as con:
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
                    col = quote_ident(c.name)
                    ratio = _scalar(
                        con,
                        f"select coalesce(avg(case when r.{col} is null then 0 else 1.0"
                        f" end), 0) from {_table(schema, name)} t left join {_table(rs, rt)} r"
                        f" on cast(t.{col} as varchar) = cast(r.{col} as varchar)"
                        f" where t.{col} is not null",
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

    def is_unique(self, table: str, column: str) -> bool:
        """列の値が NULL を含まず一意か（主キーとして使えるか）。"""
        schema, name = self._resolve(table)
        if not _IDENT.match(column):
            raise ValueError(f"不正な列名です: {column}")
        with self.connect() as con:
            return _is_unique(con, schema, name, column)

    def profile_column(self, table: str, column: str, top: int = 10) -> ColumnProfile:
        schema, name = self._resolve(table)
        if not _IDENT.match(column):
            raise ValueError(f"不正な列名です: {column}")
        with self.connect() as con:
            info = {c.name: c for c in self._columns(con, schema, name)}
            if column not in info:
                raise ValueError(f"列が見つかりません: {schema}.{name}.{column}")
            return _profile(
                con, schema, name, info[column], top, masked=self.is_pii(schema, name, column)
            )

    def sample_rows(
        self,
        table: str,
        where_column: str | None = None,
        value: str | None = None,
        limit: int = 3,
    ) -> SampleRows:
        """テーブルの実際の行を数件返す。where_column = value で絞れる（キーで関連する行をたどる）。

        値は文字列にして返す（長い値は切り詰める）。個人情報の列（is_pii）と、自由記述・秘密情報と
        推定する列は値を伏せる。絞り込みの値は SQL に埋め込まずパラメータで渡し、伏せた列での
        絞り込みは受け付けない（値の当て推量を防ぐ）。
        """
        if value is not None and not where_column:
            raise ValueError("value を指定したときは where_column も指定してください")
        schema, name = self._resolve(table)
        limit = max(1, min(int(limit), SAMPLE_MAX))
        with self.connect() as con:
            columns = [c.name for c in self._columns(con, schema, name)]
            pii = {c for c in columns if self.is_pii(schema, name, c)}
            free = {c for c in columns if c not in pii and _FREE_TEXT.search(c)}
            sql = f"select * from {quote_ident(schema)}.{quote_ident(name)}"
            params: list[Any] = []
            if where_column:
                if where_column not in columns:
                    raise ValueError(f"列が見つかりません: {schema}.{name}.{where_column}")
                if where_column in pii or where_column in free:
                    raise ValueError("個人情報・自由記述の列では絞り込めません")
                if value is None:
                    raise ValueError("where_column を指定したときは value も指定してください")
                sql += f" where cast({quote_ident(where_column)} as varchar) = ?"
                params.append(str(value))
            sql += f" limit {limit}"
            rows = con.execute(sql, params).fetchall()

        def show(col: str, v: Any) -> str | None:
            if v is None:
                return None
            if col in pii:
                return MASKED
            if col in free:
                return FREE_TEXT_MASKED
            text = str(v)
            return text if len(text) <= SAMPLE_VALUE_MAX else text[: SAMPLE_VALUE_MAX - 1] + "…"

        out = [[show(c, v) for c, v in zip(columns, row, strict=True)] for row in rows]
        return SampleRows(
            table=f"{schema}.{name}",
            columns=columns,
            rows=out,
            masked_columns=[c for c in columns if c in pii or c in free],
        )

    def profile_table(self, table: str, top: int = 5, detail: bool = False) -> TableProfile:
        """テーブルの全列のプロファイルと、主キー・外部キーの候補（源泉のプロファイル画面用）。

        detail: 月ごとの件数・分布も出す（プロファイル画面だけ。#71）。
        """
        schema, name = self._resolve(table)
        with self.connect() as con:
            columns = self._columns(con, schema, name)
            profiles = [
                _profile(
                    con,
                    schema,
                    name,
                    c,
                    top,
                    detail=detail,
                    masked=self.is_pii(schema, name, c.name),
                )
                for c in columns
            ]
            count = profiles[0].row_count if profiles else 0
            fks = self._foreign_keys(con, schema, name, columns)
        return TableProfile(
            schema_name=schema,
            name=name,
            description=self._descriptions.get((schema, name, None), ""),
            row_count=count,
            columns=columns,
            primary_key_candidates=[p.column for p in profiles if p.is_unique],
            foreign_key_candidates=fks,
            profiles=profiles,
        )

    def count_cast_failures(self, table: str, column: str, data_type: str) -> int:
        """値があるのに data_type に変換できない行の数（クレンジングの定義の事前確認。#68）。"""
        schema, name = self._resolve(table)
        if not _IDENT.match(column) or not _CAST_TYPE.fullmatch(data_type):
            raise ValueError(f"不正な列名か型です: {column} / {data_type}")
        col = quote_ident(column)
        with self.connect() as con:
            return int(
                _scalar(
                    con,
                    f"select count(*) from {_table(schema, name)}"
                    f" where {col} is not null and try_cast({col} as {data_type}) is null",
                )
            )

    def count_multi_valued(self, table: str, column: str, depends_on: str) -> int:
        """column の値のうち、depends_on の値が 2 つ以上あるものの数（階層の照合。子の値ごとに親の
        値は 1 つ。#137）。空の値は数えない。"""
        schema, name = self._resolve(table)
        for c in (column, depends_on):
            if not _IDENT.match(c):
                raise ValueError(f"不正な列名です: {c}")
        child, parent = quote_ident(column), quote_ident(depends_on)
        with self.connect() as con:
            return int(
                _scalar(
                    con,
                    f"select count(*) from (select {child} from {_table(schema, name)}"
                    f" where {child} is not null and {parent} is not null"
                    f" group by {child} having count(distinct {parent}) > 1)",
                )
            )

    def search_columns(self, query: str) -> list[ColumnMatch]:
        """列名・説明・テーブル説明に、いずれかの語を含む列を返す（大文字小文字は区別しない）。"""
        terms = [t.lower() for t in query.split() if t]
        matches = []
        with self.connect() as con:
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


def _profile(
    con: duckdb.DuckDBPyConnection,
    schema: str,
    name: str,
    column: ColumnInfo,
    top: int,
    detail: bool = False,
    masked: bool = False,
) -> ColumnProfile:
    q, col = _table(schema, name), quote_ident(column.name)
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
    profile = ColumnProfile(
        table=f"{schema}.{name}",
        column=column.name,
        data_type=column.data_type,
        row_count=total,
        null_count=nulls,
        distinct_count=distinct,
        min=lo,
        max=hi,
        top_values=[(v, int(n)) for v, n in values],
        is_unique=total > 0 and nulls == 0 and distinct == total,
    )
    if masked:
        # 個人情報の列は件数だけ（値・範囲・分布は画面にもエージェントにも出さない。#81）
        profile.min = profile.max = None
        profile.top_values = []
        profile.masked = True
        return profile
    if detail and total > nulls:
        kind = type_kind(column.data_type)
        try:
            if kind == "temporal":
                profile.monthly = _monthly(con, q, col)
            elif kind == "numeric":
                profile.histogram = _histogram(con, q, col)
        except duckdb.Error:  # 図が作れなくても、プロファイル全体は返す
            pass
    return profile


_NUMERIC = (
    "UTINYINT",
    "USMALLINT",
    "UHUGEINT",
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "DOUBLE",
    "FLOAT",
    "REAL",
    "DECIMAL",
    "UBIGINT",
    "UINTEGER",
)


def type_kind(data_type: str) -> Literal["temporal", "numeric"] | None:
    """列の型の種類。日付・日時なら temporal、数値なら numeric、それ以外（文字列など）は None。"""
    kind = data_type.upper()
    if kind == "DATE" or kind.startswith("TIMESTAMP"):
        return "temporal"
    if kind.startswith(_NUMERIC):
        return "numeric"
    return None


def _monthly(con: duckdb.DuckDBPyConnection, q: str, col: str) -> list[tuple[str, int]] | None:
    """月ごとの件数。件数の無い月（欠けている期間）も 0 で出す。"""
    rows = con.execute(
        f"with m as (select date_trunc('month', {col}) as month, count(*) as n from {q}"
        f" where {col} is not null and isfinite({col}) group by 1),"
        " span as (select min(month) lo, max(month) hi from m)"
        " select strftime(g.month, '%Y-%m'), coalesce(m.n, 0)"
        " from span, generate_series(span.lo, span.hi, interval 1 month) as g(month)"
        " left join m on m.month = g.month order by 1"
        f" limit {MONTHLY_MAX + 1}"
    ).fetchall()
    if len(rows) > MONTHLY_MAX:
        return None
    return [(str(m), int(n)) for m, n in rows]


def _histogram(
    con: duckdb.DuckDBPyConnection, q: str, col: str
) -> list[tuple[float, float, int]] | None:
    """等幅の区間ごとの件数。"""
    # NaN・±inf は数えない（区間の幅が決まらないため）
    finite = f"{col} is not null and isfinite({col}::double)"
    bounds = con.execute(
        f"select min({col})::double, max({col})::double, count(*) from {q} where {finite}"
    ).fetchone()
    if bounds is None or bounds[0] is None:
        return None
    lo, hi = float(bounds[0]), float(bounds[1])
    if lo == hi:
        return [(lo, hi, int(bounds[2]))]
    width = (hi - lo) / HISTOGRAM_BINS
    if not math.isfinite(width) or width <= 0:
        return None
    counts = dict(
        con.execute(
            f"select least(floor(({col}::double - ?) / ?)::int, ?) b, count(*) from {q}"
            f" where {finite} group by 1",
            [lo, width, HISTOGRAM_BINS - 1],
        ).fetchall()
    )
    return [
        (lo + i * width, lo + (i + 1) * width, int(counts.get(i, 0))) for i in range(HISTOGRAM_BINS)
    ]


def _is_unique(con: duckdb.DuckDBPyConnection, schema: str, name: str, column: str) -> bool:
    col = quote_ident(column)
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


def _load_pii(manifest_path: Path | None) -> dict[tuple[str, str, str], bool]:
    """dbt の sources の列の meta.pii（true / false）。キーは (schema, table, column)。"""
    manifest = _read_json(manifest_path) if manifest_path is not None else None
    if manifest is None:
        return {}
    result: dict[tuple[str, str, str], bool] = {}
    for node in _values(manifest.get("sources")):
        if "schema" not in node or not (node.get("identifier") or node.get("name")):
            continue
        schema, table = node["schema"], node.get("identifier") or node["name"]
        for col in _values(node.get("columns")):
            meta = col.get("meta") or (col.get("config") or {}).get("meta") or {}
            if "pii" in meta and col.get("name"):
                result[(schema, table, col["name"])] = bool(meta["pii"])
    return result


def _load_pii_yaml(warehouse: Path) -> dict[tuple[str, str, str], bool]:
    """dbt プロジェクトの models/ の sources の yml にある列の meta.pii（manifest より新しい）。"""
    if not (warehouse / "dbt_project.yml").exists():
        return {}
    result: dict[tuple[str, str, str], bool] = {}
    for _, src in load_yaml_docs(warehouse / "models").entries("sources"):
        schema = src.get("schema", src["name"])
        for table in named(src.get("tables")):
            name = table.get("identifier") or table["name"]
            for col in named(table.get("columns")):
                meta = col.get("meta") or (col.get("config") or {}).get("meta") or {}
                if isinstance(meta, dict) and "pii" in meta:
                    result[(str(schema), str(name), str(col["name"]))] = bool(meta["pii"])
    return result


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _list(value: Any) -> list[dict[str, Any]]:
    """dict の要素だけのリスト（manifest の欠けた値・None に耐える）。"""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _values(value: Any) -> list[dict[str, Any]]:
    return _list(list(value.values())) if isinstance(value, dict) else []


_BASE_REF = re.compile(r"from\s+\{\{\s*ref\(\s*['\"](\w+)['\"]\s*\)\s*\}\}\s+(?:as\s+)?t0\b", re.I)


def _base_source(
    node: dict[str, Any],
    nodes: dict[Any, dict[str, Any]],
    unique_ids: dict[Any, Any],
    sources: dict[str, str],
) -> str | None:
    """fct の 1 行の元になる源泉（t0 のクレンジング層 → その source）。決まらなければ None。"""
    deps = [d for d in ((node.get("depends_on") or {}).get("nodes") or []) if isinstance(d, str)]
    base = _BASE_REF.search(str(node.get("raw_code") or ""))
    models = [unique_ids[d] for d in deps if d in unique_ids]
    if base is not None:
        stg = base.group(1)
    elif len(models) == 1:
        stg = models[0]
    else:
        return None
    stg_deps = ((nodes.get(stg) or {}).get("depends_on") or {}).get("nodes") or []
    found = [sources[d] for d in stg_deps if d in sources]
    return found[0] if len(found) == 1 else None


def _metric_inputs(m: dict[str, Any]) -> list[str]:
    """比率・派生・累積の指標が使う指標の名前。"""
    params = m.get("type_params") or {}
    refs: list[Any] = [params.get("numerator"), params.get("denominator")]
    refs += params.get("metrics") or []
    refs.append((params.get("cumulative_type_params") or {}).get("metric"))
    out = []
    for r in refs:
        name = r.get("name") if isinstance(r, dict) else r
        if isinstance(name, str) and name:
            out.append(name)
    return out


def _metrics_by_model(metrics: list[dict[str, Any]]) -> dict[str, list[FactMetric]]:
    """指標を、元になる fct ごとに。比率・派生の指標は、入力の指標の fct すべてに載せる。"""
    owner: dict[str, set[str]] = {}
    by_name = {str(m["name"]): m for m in metrics if isinstance(m.get("name"), str)}
    for name, m in by_name.items():
        params = (m.get("type_params") or {}).get("metric_aggregation_params") or {}
        if isinstance(params.get("semantic_model"), str):
            owner[name] = {params["semantic_model"]}

    def models(name: str, seen: frozenset[str] = frozenset()) -> set[str]:
        if name in owner:
            return owner[name]
        m = by_name.get(name)
        if m is None or name in seen:
            return set()
        return set().union(*(models(i, seen | {name}) for i in _metric_inputs(m)))

    out: dict[str, list[FactMetric]] = {}
    for name, m in by_name.items():
        params = m.get("type_params") or {}
        agg = (params.get("metric_aggregation_params") or {}).get("agg")
        metric = FactMetric(
            name=name,
            label=str(m.get("label") or name),
            expr=params.get("expr") if isinstance(params.get("expr"), str) else None,
            agg=agg if isinstance(agg, str) else (str(m.get("type")) if m.get("type") else None),
        )
        for model in sorted(models(name)):
            out.setdefault(model, []).append(metric)
    return out


def _load_descriptions(manifest_path: Path | None) -> dict[tuple[str, str, str | None], str]:
    """dbt の manifest.json から sources の説明を読む。キーは (schema, table, column or None)。"""
    manifest = _read_json(manifest_path) if manifest_path is not None else None
    if manifest is None:
        return {}
    result: dict[tuple[str, str, str | None], str] = {}
    nodes = _values(manifest.get("sources")) + [
        n for n in _values(manifest.get("nodes")) if n.get("resource_type") == "model"
    ]
    for node in nodes:
        table = node.get("identifier") or node.get("alias") or node.get("name")
        if "schema" not in node or not table:
            continue
        schema = node["schema"]
        result[(schema, table, None)] = node.get("description") or ""
        for col in _values(node.get("columns")):
            if col.get("name"):
                result[(schema, table, col["name"])] = col.get("description") or ""
    return result
