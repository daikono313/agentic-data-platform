"""物理設計から dbt のモデル（SQL と YAML）を生成する。

型が決まっている部分はすべてテンプレートで決定的に作る。docs/modeling-conventions.md に従う。
既存のモデル（共通ディメンションなど）は作り直さず再利用し、列の追加が必要なものはタスクとして返す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from adp.beam import BeamModel, StateMethod, TimeGrain
from adp.catalog.types import column_kind, is_text_type
from adp.cleansing.models import CleansingSpec, dump_spec, load_defaults, load_spec, spec_path
from adp.cleansing.render import SourceColumn
from adp.cleansing.render import render as render_cleansing
from adp.design import Column, ColumnRole, DimTable, FactTable, Join, PhysicalDesign, SourceTable
from adp.design.builder import dim_key_expr, staging_model
from adp.generation.semantic import (
    AT_EVENT_SUFFIX,
    ROW_COUNT_EXPR,
    MetricQuery,
    SemanticError,
    dim_semantics,
    fact_model_semantics,
    fact_semantics,
    history_semantics,
    metric_definitions,
)
from adp.yaml_docs import named

UNKNOWN_LABEL = "不明"


class WarehouseInfo(Protocol):
    """生成に必要な既存の状態とソースの情報。"""

    @property
    def warehouse_dir(self) -> Path:
        """dbt プロジェクトのディレクトリ（クレンジングの定義を読む。#68）。"""
        ...

    def existing_models(self) -> set[str]: ...

    def source_columns(self, table: str) -> list[tuple[str, str]]:
        """(列名, 型) の一覧。"""
        ...

    def source_primary_key(self, table: str) -> str | None: ...

    def existing_columns(self, model: str) -> set[str]:
        """既存モデルの YAML に書かれている列。"""
        ...

    def declared_sources(self) -> dict[str, str]:
        """dbt の sources に宣言済みのテーブル（`<schema>.<table>`）と、その dbt の source 名。"""
        ...

    def distinct_values(self, table: str, column: str, limit: int) -> list[str] | None:
        """列の値の種類（NULL を除く）。limit を超える場合は None。"""
        ...


@dataclass
class GeneratedFile:
    path: str
    """warehouse/ からの相対パス。"""
    content: str
    replaces: str | None = None
    """既存のファイルを書き換える場合の、元の内容（adp が生成したファイルに指標を足すときだけ）。"""


@dataclass
class AgentTask:
    """テンプレートでは作れず、生成エージェントに任せる作業。"""

    kind: str
    target: str
    instruction: str


@dataclass
class Generation:
    files: list[GeneratedFile] = field(default_factory=list)
    tasks: list[AgentTask] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    queries: list[MetricQuery] = field(default_factory=list)
    """生成後に確認する指標のクエリ（元の要求）。"""


def _yaml(
    models: list[dict[str, Any]],
    metrics: list[dict[str, Any]] | None = None,
    unit_tests: list[dict[str, Any]] | None = None,
) -> str:
    data: dict[str, Any] = {"models": models}
    if metrics:
        data["metrics"] = metrics
    if unit_tests:
        data["unit_tests"] = unit_tests
    return _yaml_doc(data)


def _yaml_doc(data: dict[str, Any]) -> str:
    """生成する dbt の YAML（models・snapshots など）。"""
    body = yaml.safe_dump({"version": 2} | data, allow_unicode=True, sort_keys=False, width=100)
    return "# adp により生成\n" + body


def _from_clause(base: SourceTable, joins: list[Join]) -> str:
    lines = [f"from {{{{ ref('{base.model}') }}}} as {base.alias}"]
    for j in joins:
        lines.append(
            f"left join {{{{ ref('{j.right.model}') }}}} as {j.right.alias}"
            f" on {j.left_alias}.{j.left_column} = {j.right.alias}.{j.right_column}"
        )
    return "\n".join(lines)


def _select(columns: list[tuple[str, str]]) -> str:
    return "select\n" + ",\n".join(f"    {expr} as {name}" for name, expr in columns)


# --- staging -------------------------------------------------------------------------------


def staging_files(table: str, info: WarehouseInfo) -> list[GeneratedFile]:
    """源泉のクレンジング層（stg）。クレンジングの定義（warehouse/cleansing/）から生成する（#68）。

    定義が無ければ既定の定義（キーと、型ごとの既定のクレンジングだけ）を一緒に作る。
    """
    schema, _, source_table = table.rpartition(".")
    declared_as = info.declared_sources().get(table)
    declared = declared_as is not None
    # 宣言済みなら、その source 名を使う（生成 PR が別名で宣言した源泉も。#104）。
    # 未宣言のソースは、テーブルごとに別名のソースとして宣言する（既存の sources を編集しない）
    source_name = declared_as or f"adp_{source_table}"
    dbt_source = None if source_name == schema else source_name
    cleansing_dir = info.warehouse_dir / "cleansing"
    files: list[GeneratedFile] = []
    path = spec_path(cleansing_dir, table)
    if path.exists():
        # 定義の dbt_source は、宣言済みの source 名に合わせる（未宣言なら下で別名の source を
        # 宣言する）。変わるときは定義のファイルも書き換え、定義と stg を食い違わせない（#105）
        original = path.read_text(encoding="utf-8")
        loaded = load_spec(path)
        spec = loaded.model_copy(update={"dbt_source": dbt_source})
        if loaded.dbt_source != dbt_source:
            files.append(
                GeneratedFile(f"cleansing/{table}.yml", dump_spec(spec), replaces=original)
            )
    else:
        spec = CleansingSpec(
            source=table,
            dbt_source=dbt_source,
            description=f"{table} をクレンジング",
            key=info.source_primary_key(table),
        )
        files.append(GeneratedFile(f"cleansing/{table}.yml", dump_spec(spec)))
    columns = [SourceColumn(c, t) for c, t in info.source_columns(table)]
    rendered = render_cleansing(spec, load_defaults(cleansing_dir), columns)
    files += [
        GeneratedFile(rendered.sql_path, rendered.sql),
        GeneratedFile(rendered.yml_path, rendered.yml),
    ]
    if not declared:
        sources = yaml.safe_dump(
            {
                "version": 2,
                "sources": [
                    {"name": source_name, "schema": schema, "tables": [{"name": source_table}]}
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        )
        files.append(
            GeneratedFile(
                f"models/staging/_{source_name}__sources.yml", "# adp により生成\n" + sources
            )
        )
    return files


# --- dim -----------------------------------------------------------------------------------


def _is_text(info: WarehouseInfo, table: str, column: Column, alias_tables: dict[str, str]) -> bool:
    alias, _, col = column.expr.partition(".")
    if alias not in alias_tables or not col.isidentifier():
        return False
    types = dict(info.source_columns(alias_tables[alias]))
    return is_text_type(types.get(col, ""))


def _unknown_value(info: WarehouseInfo, dim: DimTable, column: Column) -> str:
    """不明メンバーの行の値（文字列の属性は「不明」、それ以外は null）。dim と履歴モデルで同じ。"""
    alias_tables = {dim.base.alias: dim.base.table} | {
        j.right.alias: j.right.table for j in dim.joins
    }
    text = column.role == ColumnRole.ATTRIBUTE and _is_text(
        info, dim.base.table, column, alias_tables
    )
    return f"'{UNKNOWN_LABEL}'" if text else "null"


def dim_files(dim: DimTable, info: WarehouseInfo, semantic: bool = False) -> list[GeneratedFile]:
    natural = dim.columns[0]
    select = [(dim.key_column, dim_key_expr(natural.expr))]
    select += [(c.name, c.expr) for c in dim.columns]
    unknown = ["{{ unknown_key() }}"] + [_unknown_value(info, dim, c) for c in dim.columns]
    sql = (
        f"-- adp により生成。{dim.label}ディメンション（SCD Type1）\n"
        + _select(select)
        + "\n"
        + _from_clause(dim.base, dim.joins)
        # 自然キーごとに 1 行にする（NULL の自然キーは不明メンバーと重複するので除く）
        + f"\nwhere {natural.expr} is not null"
        # 同じ自然キーの行が複数あっても、残す行が実行ごとに変わらないよう全列で順序を決める
        + "\nqualify row_number() over (partition by "
        + natural.expr
        + " order by "
        + ", ".join(c.expr for c in dim.columns)
        + ") = 1"
        + "\n\nunion all\n\n-- 不明メンバー\nselect "
        + ", ".join(unknown)
        + "\n"
    )
    columns: list[dict[str, Any]] = [
        {
            "name": dim.key_column,
            "description": "サロゲートキー",
            "data_tests": ["unique", "not_null"],
        }
    ]
    from adp.generation.tests import dim_column_tests

    extra = dim_column_tests(dim, info)
    columns += [
        {"name": c.name, "description": c.description}
        | ({"data_tests": extra[c.name]} if c.name in extra else {})
        for c in dim.columns
    ]
    model: dict[str, Any] = {
        "name": dim.name,
        "description": f"{dim.label}ディメンション。SCD Type1。不明メンバーの行を含む",
    }
    if semantic:
        model_sem, col_sem = dim_semantics(dim)
        model |= model_sem
        for entry in columns:
            entry.update(col_sem.get(entry["name"], {}))
    model["columns"] = columns
    yml = _yaml([model])
    return [
        GeneratedFile(f"models/marts/{dim.name}.sql", sql),
        GeneratedFile(f"models/marts/{dim.name}.yml", yml),
    ]


# --- 履歴（SCD Type 2。ADR 0009） ---------------------------------------------------------

HISTORY_SNAPSHOT_SCHEMA = "snapshots"
"""snapshot を書く dbt の schema（サンプルデータを作り直すときに消す）。"""
SNAPSHOT_DIR = "snapshots"
"""snapshot の YAML を置く warehouse/ の下のディレクトリ（dbt の既定の snapshot-paths）。"""


def history_files(
    dim: DimTable, info: WarehouseInfo, semantic: bool = False
) -> list[GeneratedFile]:
    """当時の値で見たい属性の snapshot と履歴モデル（ADR 0009）。

    snapshot はクレンジング層（stg）の行の版を記録する。stg に更新日時（updated_at）があればその
    時刻で、無ければ snapshot を実行した時刻で版を分ける。履歴モデルは版ごとに 1 行で、期間は
    valid_from 以上 valid_to 未満。最初の版は暦の始まりから（源泉の更新日時より前の出来事も結合
    できるように）、今の版は暦の終わりまでにし、どの版も暦の範囲に収める。暦の範囲は dim_date から
    取る（生成物の Jinja は決まった形だけ。adp.generation.project）。snapshot を始める前の変更は
    復元できない。
    """
    h = dim.history
    assert h is not None
    table = dim.base.table
    stg = staging_model(table)
    if not re.fullmatch(r"stg_[a-z0-9_]+", stg):  # snapshot の relation は dbt が描画する
        raise ValueError(f"snapshot の元にできないモデル名です: {stg}")
    types = dict(info.source_columns(table))
    # snapshot が読むのは stg なので、クレンジングで落とした列は使わない
    stg_columns = info.existing_columns(stg) or set(types)
    timestamp = "updated_at" in stg_columns and column_kind(types.get("updated_at", "")) in (
        "timestamp",
        "date",
    )
    strategy: dict[str, Any] = (
        {"strategy": "timestamp", "updated_at": "updated_at"}
        if timestamp
        else {"strategy": "check", "check_cols": "all"}
    )
    snapshot = {
        "snapshots": [
            {
                "name": h.snapshot,
                "description": f"{table} の行の版（属性の履歴の元。ADR 0009）",
                "relation": f"ref('{stg}')",
                "config": {"schema": HISTORY_SNAPSHOT_SCHEMA, "unique_key": h.natural_key}
                | strategy,
            }
        ]
    }
    key = h.natural_key
    version_key = f"{dim.dimension}_version_key"
    by_name = {c.name: c for c in dim.columns}
    select = [(version_key, "dbt_scd_id"), (dim.key_column, dim_key_expr(key))]
    select += [(c.name, c.expr) for c in h.columns]
    select += [
        (
            "valid_from",
            "case when version_no = 1 then calendar.starts_at"
            " else least(greatest(dbt_valid_from, calendar.starts_at), calendar.ends_at) end",
        ),
        ("valid_to", "least(coalesce(dbt_valid_to, calendar.ends_at), calendar.ends_at)"),
    ]
    unknown = ["{{ unknown_key() }}", "{{ unknown_key() }}"]
    unknown += [
        _unknown_value(
            info,
            dim,
            by_name.get(c.name) or c.model_copy(update={"expr": f"{dim.base.alias}.{c.expr}"}),
        )
        for c in h.columns
    ]
    unknown += ["calendar.starts_at", "calendar.ends_at"]
    sql = (
        f"-- adp により生成。{dim.label}の属性の履歴（SCD Type 2。ADR 0009）\n"
        "with snapshot_rows as (\n"
        f"    select * from {{{{ ref('{h.snapshot}') }}}}\n"
        f"    where {key} is not null\n"
        "    -- 同じ自然キー・同じ時刻の版は 1 つにする（stg に同じキーの行が複数ある場合）\n"
        f"    qualify row_number() over (partition by {key}, dbt_valid_from"
        " order by dbt_scd_id) = 1\n"
        "),\n\n"
        "versions as (\n"
        "    select\n"
        "        *,\n"
        f"        row_number() over (partition by {key} order by dbt_valid_from) as version_no\n"
        "    from snapshot_rows\n"
        "),\n\n"
        "calendar as (\n"
        "    select\n"
        "        cast(min(date_day) as timestamp) as starts_at,\n"
        "        cast(max(date_day) + interval 1 day as timestamp) as ends_at\n"
        "    from {{ ref('dim_date') }}\n"
        ")\n\n" + _select(select) + "\nfrom versions\ncross join calendar\n\nunion all\n\n"
        "-- 不明メンバー（期間は暦の全体）\nselect " + ", ".join(unknown) + "\nfrom calendar\n"
    )
    columns: list[dict[str, Any]] = [
        {
            "name": version_key,
            "description": "版のキー（dbt snapshot の dbt_scd_id）",
            "data_tests": ["unique", "not_null"],
        },
        {
            "name": dim.key_column,
            "description": f"サロゲートキー（{dim.name} と同じ）",
            "data_tests": ["not_null"],
        },
    ]
    columns += [{"name": c.name, "description": f"{c.description}（当時）"} for c in h.columns]
    columns += [
        {
            "name": "valid_from",
            "description": "版の始まり（この時刻を含む）",
            "data_tests": ["not_null"],
        },
        {
            "name": "valid_to",
            "description": "版の終わり（この時刻を含まない）",
            "data_tests": ["not_null"],
        },
    ]
    model: dict[str, Any] = {
        "name": h.model,
        "description": (
            f"{dim.label}の属性の履歴（SCD Type 2。ADR 0009）。"
            "期間は valid_from 以上 valid_to 未満。不明メンバーの行を含む"
        ),
        "data_tests": [
            {
                "dbt_utils.mutually_exclusive_ranges": {
                    "lower_bound_column": "valid_from",
                    "upper_bound_column": "valid_to",
                    "partition_by": dim.key_column,
                    "gaps": "not_allowed",
                }
            }
        ],
    }
    if semantic:
        model_sem, col_sem = history_semantics(dim)
        model |= model_sem
        for entry in columns:
            entry.update(col_sem.get(entry["name"], {}))
    model["columns"] = columns
    return [
        GeneratedFile(f"{SNAPSHOT_DIR}/{h.snapshot}.yml", _yaml_doc(snapshot)),
        GeneratedFile(f"models/marts/{h.model}.sql", sql),
        GeneratedFile(f"models/marts/{h.model}.yml", _yaml([model])),
    ]


# --- fct -----------------------------------------------------------------------------------


def fact_files(
    fact: FactTable,
    beam: BeamModel | None = None,
    design: PhysicalDesign | None = None,
    has_date_entity: bool = False,
    info: WarehouseInfo | None = None,
) -> tuple[list[GeneratedFile], list[MetricQuery]]:
    from adp.generation.tests import (
        fact_column_tests,
        reconciliation_test,
        snapshot_reconciliation_test,
        snapshot_unit_test,
        unit_test,
    )

    extra = fact_column_tests(fact, info) if info else {}
    head = (
        f"-- adp により生成\n-- 元イベント: {fact.label}（{fact.fact_type.value}）\n"
        f"-- 粒度: {fact.grain}\n"
    )
    if fact.snapshot is not None:
        sql = head + periodic_snapshot_sql(fact, info)
    else:
        sql = (
            head
            + _select([(c.name, c.expr) for c in fact.columns])
            + "\n"
            + _from_clause(fact.base, fact.joins)
            + "\n"
        )
    event = beam.event(fact.event) if beam else None
    col_sem = fact_semantics(fact, event) if event else {}
    columns: list[dict[str, Any]] = []
    for c in fact.columns:
        entry: dict[str, Any] = {"name": c.name, "description": c.description}
        entry.update(col_sem.get(c.name, {}))
        tests: list[Any] = []
        if fact.grain_columns == [c.name]:
            tests += ["unique", "not_null"]
        elif c.name in fact.grain_columns:
            tests.append("not_null")  # 複数列の粒度も、各列が NULL だと 1 行を特定できない
        if c.role in (ColumnRole.FOREIGN_KEY, ColumnRole.TIME) and c.references:
            ref_model, _, ref_col = c.references.partition(".")
            if not c.nullable and "not_null" not in tests:
                tests.append("not_null")
            tests.append(
                {"relationships": {"arguments": {"to": f"ref('{ref_model}')", "field": ref_col}}}
            )
        tests += extra.get(c.name, [])
        if tests:
            entry["data_tests"] = tests
        columns.append(entry)
    model: dict[str, Any] = {
        "name": fact.name,
        "description": f"{fact.label}（{fact.fact_type.value}）。粒度: {fact.grain}",
    }
    top_metrics: list[dict[str, Any]] = []
    queries: list[MetricQuery] = []
    if beam is not None and design is not None and event is not None:
        model |= fact_model_semantics(fact, has_date_entity, event)
        defs = metric_definitions(beam, design, fact)
        if defs.in_model:
            model["metrics"] = defs.in_model
        top_metrics, queries = defs.top_level, defs.queries
    model["columns"] = columns
    if len(fact.grain_columns) > 1:
        model["data_tests"] = [
            {
                "dbt_utils.unique_combination_of_columns": {
                    "arguments": {"combination_of_columns": fact.grain_columns}
                }
            }
        ]
    unit_tests: list[dict[str, Any]] = []
    if beam is not None and event is not None and info is not None:
        make_unit_test = snapshot_unit_test if fact.snapshot is not None else unit_test
        for i, example in enumerate(event.examples):
            if (ut := make_unit_test(beam, fact, info, example, i)) is not None:
                unit_tests.append(ut)
    return [
        GeneratedFile(f"models/marts/{fact.name}.sql", sql),
        GeneratedFile(f"models/marts/{fact.name}.yml", _yaml([model], top_metrics, unit_tests)),
        snapshot_reconciliation_test(fact, info)
        if fact.snapshot is not None
        else reconciliation_test(fact),
    ], queries


_PERIOD_LENGTH = {
    TimeGrain.WEEK: "7 day",
    TimeGrain.MONTH: "1 month",
    TimeGrain.QUARTER: "3 month",
    TimeGrain.YEAR: "1 year",
}


def period_end(expr: str, grain: TimeGrain) -> str:
    """日時の式を、その期間（週・月など）の終わりの日付にする（DuckDB。週は月曜始まり）。"""
    if grain == TimeGrain.DAY:
        return f"cast({expr} as date)"
    trunc = "week" if grain == TimeGrain.WEEK else grain.value
    return (
        f"cast(date_trunc('{trunc}', {expr}) + interval {_PERIOD_LENGTH[grain]}"
        " - interval 1 day as date)"
    )


def snapshot_order_key(fact: FactTable, info: WarehouseInfo | None) -> str:
    """同じ時刻の記録を並べる順の列（源泉の主キー。分からなければ null）。"""
    pk = info.source_primary_key(fact.base.table) if info else None
    return f"{fact.base.alias}.{pk}" if pk else "null"


def snapshot_order(fact: FactTable, descending: bool = False) -> str:
    """records の記録を並べる順。時刻 → 源泉の主キー → measure と置き換える行の印。

    主キーが分からなくても順が実行ごとに変わらないよう、値に使う列すべてで並べる（すべて同じ
    行は、どの順でも結果が同じ）。生成した fct と突合テストで同じ順を使う。"""
    snap = fact.snapshot
    assert snap is not None
    cols = ["recorded_at", "record_order", *snap.methods]
    cols += [f"{m}__is_reset" for m in snap.resets]
    return "order by " + ", ".join(f"{c} desc" if descending else c for c in cols)


def snapshot_records(fact: FactTable, info: WarehouseInfo | None) -> str:
    """記録ごとの区切りの列・期間の終わりの日付・時刻・順・measure・置き換える行の印の SELECT。

    置き換える行でも measure が NULL なら置き換えない（数えた数の無い棚卸は記録として使わない）。
    """
    from adp.generation.tests import snapshot_partition

    snap = fact.snapshot
    assert snap is not None
    cols = {c.name: c for c in fact.columns}
    records = [(n, cols[n].expr) for n in snapshot_partition(fact)]
    records += [
        (snap.time_column, period_end(snap.recorded_at, snap.grain)),
        ("recorded_at", snap.recorded_at),
        ("record_order", snapshot_order_key(fact, info)),
    ]
    records += [(m, cols[m].expr) for m in snap.methods]
    records += [
        (f"{m}__is_reset", f"coalesce({cond}, false) and {cols[m].expr} is not null")
        for m, cond in snap.resets.items()
    ]
    return _select(records) + "\n" + _from_clause(fact.base, fact.joins)


def periodic_snapshot_sql(fact: FactTable, info: WarehouseInfo | None) -> str:
    """定期スナップショットの fct の SQL（ADR 0008）。決まった形で作り、LLM には書かせない。

    1. records: 記録ごとに、区切りの列・期間の終わりの日付・measure・置き換える行の印を取る
    2. sequenced: 区切りごとに時刻（同じ時刻は源泉の主キー）の順に並べ、置き換えた回数を数える
    3. states: 記録の時点の値（last_value はその値、running_total は積み上げ。置き換える行が
       あれば、その行から積み直し、最初のその行より前は値を出さない）
    4. periods: 期間ごとに最後の記録の値
    5. grid: 区切りごとに、最初の値のある期間から最後の期間までの期間の終わりの日付（dim_date）
    6. 記録の無い期間は、前の期間の値を引き継ぐ
    """
    from adp.generation.tests import snapshot_partition

    snap = fact.snapshot
    assert snap is not None
    measures = [c for c in fact.columns if c.name in snap.methods]
    p_names = snapshot_partition(fact)
    by = f"partition by {', '.join(p_names)} " if p_names else ""

    def by_reset(measure: str) -> str:
        return f"partition by {', '.join([*p_names, f'{measure}__resets'])} "

    order = snapshot_order(fact)
    running = "rows between unbounded preceding and current row"
    sequenced = [
        f"        row_number() over ({by}{order}) as record_seq",
        *(
            f"        sum(case when {m}__is_reset then 1 else 0 end) over ({by}{order} {running})"
            f" as {m}__resets"
            for m in snap.resets
        ),
    ]
    states: list[str] = []
    for c in measures:
        method = snap.methods[c.name]
        if method == StateMethod.LAST_VALUE:
            states.append(f"        {c.name}")
        elif c.name in snap.resets:
            states.append(
                f"        case when {c.name}__resets > 0 then sum({c.name}) over"
                f" ({by_reset(c.name)}order by record_seq {running}) end as {c.name}"
            )
        else:
            states.append(
                f"        sum({c.name}) over ({by}order by record_seq {running}) as {c.name}"
            )
    keys = ", ".join([*p_names, snap.time_column])
    periods = [
        f"        arg_max({c.name}, record_seq) filter (where {c.name} is not null) as {c.name}"
        for c in measures
    ]
    having = " or ".join(f"count({c.name}) > 0" for c in measures)
    join = " and ".join(
        [f"grid.{n} is not distinct from periods.{n}" for n in p_names]
        + [f"grid.{snap.time_column} = periods.{snap.time_column}"]
    )
    grid_by = f"partition by {', '.join(f'grid.{n}' for n in p_names)} " if p_names else ""
    final = [f"    grid.{n}" for n in [*p_names, snap.time_column]]
    final += [
        f"    last_value(periods.{c.name} ignore nulls) over"
        f" ({grid_by}order by grid.{snap.time_column} {running}) as {c.name}"
        for c in measures
    ]
    start_cols = "".join(f"{n}, " for n in p_names)
    group = f"\n    group by {', '.join(p_names)}" if p_names else ""
    return (
        "-- 状態の値（ADR 0008）: 区切りごとに記録を時刻の順にたどり、期間の終わりの値にする。"
        "記録の無い期間は前の期間の値を引き継ぐ\n"
        "with records as (\n"
        + "\n".join("    " + line for line in snapshot_records(fact, info).splitlines())
        + "\n),\n\nsequenced as (\n    select\n        *,\n"
        + ",\n".join(sequenced)
        + "\n    from records\n),\n\nstates as (\n    select\n"
        + "".join(f"        {n},\n" for n in p_names)
        + f"        {snap.time_column},\n        record_seq,\n"
        + ",\n".join(states)
        + "\n    from sequenced\n),\n\nperiods as (\n    select\n"
        + "".join(f"        {n},\n" for n in [*p_names, snap.time_column])
        + ",\n".join(periods)
        + f"\n    from states\n    group by {keys}\n    having {having}\n),\n\n"
        "spine as (\n"
        f"    select date_day as {snap.time_column}\n"
        "    from {{ ref('dim_date') }}\n"
        f"    where date_day = {period_end('date_day', snap.grain)}\n"
        f"        and date_day <= (select max({snap.time_column}) from periods)\n),\n\n"
        "starts as (\n"
        f"    select {start_cols}min({snap.time_column}) as first_on\n"
        f"    from periods{group}\n),\n\n"
        "grid as (\n"
        "    select " + "".join(f"starts.{n}, " for n in p_names) + f"spine.{snap.time_column}\n"
        f"    from starts\n    inner join spine on spine.{snap.time_column} >= starts.first_on\n"
        ")\n\nselect\n" + ",\n".join(final) + f"\nfrom grid\nleft join periods on {join}\n"
    )


def _extend_existing_fact(
    fact: FactTable, beam: BeamModel, design: PhysicalDesign, info: WarehouseInfo
) -> tuple[list[GeneratedFile], list[MetricQuery]] | None:
    """既にある fct に、この要求の指標（と、その元になる measure の列）だけを足す（#46）。

    S2 のように共通の fct を再利用する場合。adp が生成した fct で、足す列が measure か属性で
    基のテーブル（t0）だけを参照するときに限る。できなければ None（エージェントに任せる）。
    既存の列・指標は変えない。
    """
    model_file = getattr(info, "model_file", None)
    if model_file is None or fact.snapshot is not None:
        return (
            None  # 定期スナップショットの SQL は、列を足すと作り直しになる（エージェントに任せる）
        )
    yml, sql = model_file(fact.name, ".yml"), model_file(fact.name, ".sql")
    if yml is None or sql is None:
        return None
    (yml_path, yml_text), (sql_path, sql_text) = yml, sql
    header = "-- adp により生成"
    base = f"from {{{{ ref('{fact.base.model}') }}}} as t0"
    if not yml_text.startswith("# adp により生成") or not sql_text.startswith(header):
        return None
    if base not in sql_text:
        return None
    data = yaml.safe_load(yml_text)  # 下で書き換えるので、キャッシュの dict（yaml_docs）は使わない
    model = next(
        (m for m in data.get("models", []) if isinstance(m, dict) and m.get("name") == fact.name),
        None,
    )
    if model is None or not model.get("semantic_model"):
        return None
    try:
        planned_files, queries = fact_files(fact, beam, design, True, info)
    except SemanticError:
        return None
    planned = yaml.safe_load(planned_files[1].content)
    planned_model = planned["models"][0]

    existing_metrics = {m["name"] for m in named(model.get("metrics"))}
    existing_metrics |= {m["name"] for m in named(data.get("metrics"))}
    new_in_model = [
        m for m in planned_model.get("metrics", []) if m["name"] not in existing_metrics
    ]
    # 足すのは新しい指標の元になる列だけ（粒度や entity は既存のものを使う）
    have = {c["name"] for c in named(model.get("columns"))}
    # 同じ式の列が既にあれば、別名の列を足さずにそれを使う（t0.quantity を 2 列にしない。#113）
    same_expr = _existing_select_exprs(sql_text)
    for m in new_in_model:
        expr = m.get("expr")
        col = fact.column(expr) if isinstance(expr, str) and expr not in have else None
        if col is None or col.role != ColumnRole.MEASURE or not _only_base(col.expr):
            continue  # 結合先の別名（t1 など）は、既存の fct と新しい設計で別のテーブルでありうる
        existing_name = same_expr.get(_norm_expr(col.expr))
        if existing_name in have and not any(
            col.name in str(x.get("filter") or "") for x in new_in_model
        ):
            m["expr"] = existing_name
    needed = {
        m["expr"]
        for m in new_in_model
        if isinstance(m.get("expr"), str) and m["expr"] != ROW_COUNT_EXPR  # 行の件数は列が要らない
    }
    missing = [c for c in fact.columns if c.name in needed and c.name not in have]
    if needed - have - {c.name for c in missing}:
        return None
    for c in missing:
        if c.role not in (ColumnRole.MEASURE, ColumnRole.ATTRIBUTE):
            return None
        if not _only_base(c.expr):
            return None
    new_top = [m for m in planned.get("metrics", []) or [] if m["name"] not in existing_metrics]
    if not missing and not new_in_model and not new_top:
        return [], queries

    files: list[GeneratedFile] = []
    if missing:
        lines = sql_text.rstrip("\n").split("\n")
        at = next(i for i, line in enumerate(lines) if line.startswith("from "))
        lines[at - 1] += ","
        added = [f"    {c.expr} as {c.name}" for c in missing]
        lines[at:at] = [",\n".join(added)]
        files.append(GeneratedFile(sql_path, "\n".join(lines) + "\n", replaces=sql_text))
        entries = {c["name"]: c for c in planned_model["columns"]}
        model["columns"] = [*model.get("columns", []), *(entries[c.name] for c in missing)]
    if new_in_model:
        model["metrics"] = [*(model.get("metrics") or []), *new_in_model]
    if new_top:
        data["metrics"] = [*(data.get("metrics") or []), *new_top]
    head = yml_text[: yml_text.index("\n") + 1]
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
    files.append(GeneratedFile(yml_path, head + body, replaces=yml_text))
    return files, queries


_SELECT_LINE = re.compile(r"^\s+(.+?)\s+as\s+(\w+),?\s*$", re.IGNORECASE)


def _only_base(expr: str) -> bool:
    """式が基のテーブル（t0）の列だけを参照するか。"""
    return all(alias == "t0" for alias in re.findall(r"\b(t\d+)\.", expr))


def _norm_expr(expr: str) -> str:
    return re.sub(r"\s+", " ", expr.strip()).lower()


def _existing_select_exprs(sql: str) -> dict[str, str]:
    """adp が生成した fct の SELECT 句（1 行 1 列）の、式 → 列名。"""
    out: dict[str, str] = {}
    for line in sql.split("\n"):
        if line.startswith("from "):
            break
        if (m := _SELECT_LINE.match(line)) is not None:
            out.setdefault(_norm_expr(m.group(1)), m.group(2))
    return out


# --- 全体 ----------------------------------------------------------------------------------


def _history(
    dim: DimTable, info: WarehouseInfo, existing: set[str], semantic: bool, result: Generation
) -> list[GeneratedFile]:
    """履歴モデルを作る。既にあれば足りない属性をエージェントへの作業にする（ADR 0009）。"""
    h = dim.history
    assert h is not None
    files = history_files(dim, info, semantic)
    # 同じ源泉の snapshot が既にあれば使う（行の版を丸ごと記録するので属性によらない）。履歴
    # モデルだけがあって snapshot が無ければ、snapshot を作り直す
    snapshot_path = f"{SNAPSHOT_DIR}/{h.snapshot}.yml"
    snapshot = (
        []
        if (info.warehouse_dir / snapshot_path).exists()
        else [f for f in files if f.path == snapshot_path]
    )
    if h.model not in existing:
        return snapshot + [f for f in files if f.path != snapshot_path]
    result.reused.append(h.model)
    have = info.existing_columns(h.model)
    missing = [c for c in h.columns if c.name not in have]
    if missing:
        result.tasks.append(
            AgentTask(
                kind="add_history_columns",
                target=h.model,
                instruction=(
                    f"既存の {h.model} に、当時の値で見たい属性"
                    f" {', '.join(c.name for c in missing)} を追加する（{h.snapshot} の列。"
                    "既存の列は変更・削除しない）。yml の説明は"
                    "「<ラベル>（当時）」、semantic model のディメンション名は「<列>"
                    f"{AT_EVENT_SUFFIX}」にする。"
                    + ", ".join(f"{c.name}（ラベル「{c.description}」）" for c in missing)
                ),
            )
        )
    return snapshot


def generate_models(
    design: PhysicalDesign, info: WarehouseInfo, beam: BeamModel | None = None
) -> Generation:
    """dbt モデルを生成する。beam を渡すと Semantic Layer（entity・指標）も生成する。"""
    semantic = beam is not None
    if design.blockers:
        raise ValueError(
            "確認が必要な課題があるため生成できません: "
            + " / ".join(i.message for i in design.blockers)
        )
    existing = info.existing_models()
    result = Generation()
    tables: set[str] = set()
    sources: list[tuple[SourceTable, list[Join]]] = [(f.base, f.joins) for f in design.facts]
    sources += [(d.base, d.joins) for d in design.dims if not d.exists]
    for base, joins in sources:
        tables.add(base.table)
        tables.update(j.right.table for j in joins)
    for table in sorted(tables):
        model = "stg_" + table.rpartition(".")[2]
        if model in existing:
            result.reused.append(model)
        else:
            result.files += staging_files(table, info)

    for dim in design.dims:
        if dim.history is not None:
            result.files += _history(dim, info, existing, semantic, result)
        if dim.name in existing:
            result.reused.append(dim.name)
            have = info.existing_columns(dim.name)
            missing = [c.name for c in dim.columns if c.name not in have]
            if missing:
                result.tasks.append(
                    AgentTask(
                        kind="add_dim_columns",
                        target=dim.name,
                        instruction=(
                            f"既存の {dim.name} に列 {', '.join(missing)} を追加する。"
                            "既存の列は変更・削除しない。yml の説明は「」の中のとおりに書く"
                            "（オントロジーのラベルと照合される）。ソース: "
                            + ", ".join(
                                f"{c.name} = {c.expr}"
                                f"（{dim.base.table} が t0。説明「{c.description}」）"
                                for c in dim.columns
                                if c.name in missing
                            )
                        ),
                    )
                )
        else:
            result.files += dim_files(dim, info, semantic)
            continue
        if semantic and dim.roles:
            result.tasks.append(
                AgentTask(
                    kind="add_role_entities",
                    target=dim.name,
                    instruction=(
                        f"既存の {dim.name} の semantic model に、役割 {', '.join(dim.roles)} の"
                        f" derived_semantics（type: unique, expr: {dim.key_column}）を追加する。"
                    ),
                )
            )

    for fact in design.facts:
        if fact.name in existing:
            extended = _extend_existing_fact(fact, beam, design, info) if beam is not None else None
            if extended is not None:
                files, queries = extended
                result.reused.append(fact.name)
                result.files += files
                result.queries += queries
                continue
            if fact.snapshot is not None:
                # 状態の SQL は決まった形でしか作らない（ADR 0008）。記録 1 件の式は渡さない
                result.tasks.append(
                    AgentTask(
                        kind="fact_exists",
                        target=fact.name,
                        instruction=f"{fact.name} は既に存在する定期スナップショット（状態）の"
                        " fct。SQL は変更しない。この要求の指標を、既存の measure の列だけを"
                        "使って既存の semantic model に追加する。必要な列が無ければ追加せず、"
                        "人に渡す",
                    )
                )
                continue
            result.tasks.append(
                AgentTask(
                    kind="fact_exists",
                    target=fact.name,
                    instruction=f"{fact.name} は既に存在する。既存の列と指標は変更・削除しない。"
                    + (
                        " この要求の指標（"
                        + ", ".join(m.name for m in beam.metrics if m.event == fact.event)
                        + "）を既存の semantic model に追加し、その元になる列が無ければ追加する: "
                        + ", ".join(f"{c.name} = {c.expr}" for c in fact.columns)
                        if beam is not None
                        else ""
                    ),
                )
            )
        else:
            files, queries = fact_files(
                fact, beam, design, has_date_entity="dim_date" in existing, info=info
            )
            result.files += files
            result.queries += queries
    return result
