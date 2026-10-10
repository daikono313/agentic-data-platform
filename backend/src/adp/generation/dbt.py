"""物理設計から dbt のモデル（SQL と YAML）を生成する。

型が決まっている部分はすべてテンプレートで決定的に作る。docs/modeling-conventions.md に従う。
既存のモデル（共通ディメンションなど）は作り直さず再利用し、列の追加が必要なものはタスクとして返す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import yaml

from adp.design import Column, ColumnRole, DimTable, FactTable, Join, PhysicalDesign, SourceTable
from adp.design.builder import dim_key_expr

UNKNOWN_LABEL = "不明"


class WarehouseInfo(Protocol):
    """生成に必要な既存の状態とソースの情報。"""

    def existing_models(self) -> set[str]: ...

    def source_columns(self, table: str) -> list[tuple[str, str]]:
        """(列名, 型) の一覧。"""
        ...

    def source_primary_key(self, table: str) -> str | None: ...

    def existing_columns(self, model: str) -> set[str]:
        """既存モデルの YAML に書かれている列。"""
        ...

    def declared_sources(self) -> set[str]:
        """dbt の sources に宣言済みのテーブル（`<schema>.<table>`）。"""
        ...


@dataclass
class GeneratedFile:
    path: str
    """warehouse/ からの相対パス。"""
    content: str


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


def _yaml(models: list[dict[str, Any]]) -> str:
    body = yaml.safe_dump(
        {"version": 2, "models": models}, allow_unicode=True, sort_keys=False, width=100
    )
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
    model = "stg_" + table.rpartition(".")[2]
    schema, _, source_table = table.rpartition(".")
    declared = table in info.declared_sources()
    # 未宣言のソースは、テーブルごとに別名のソースとして宣言する（既存の sources を編集しない）
    source_name = schema if declared else f"adp_{source_table}"
    cols = info.source_columns(table)
    sql = (
        "-- adp により生成\n"
        + _select([(c, c) for c, _ in cols])
        + f"\nfrom {{{{ source('{source_name}', '{source_table}') }}}}\n"
    )
    columns: list[dict[str, Any]] = []
    pk = info.source_primary_key(table)
    for c, _ in cols:
        entry: dict[str, Any] = {"name": c}
        if c == pk:
            entry["data_tests"] = ["unique", "not_null"]
        columns.append(entry)
    yml = _yaml([{"name": model, "description": f"{table} を整形", "columns": columns}])
    files = [
        GeneratedFile(f"models/staging/{model}.sql", sql),
        GeneratedFile(f"models/staging/{model}.yml", yml),
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
    return types.get(col, "").upper() == "VARCHAR"


def dim_files(dim: DimTable, info: WarehouseInfo) -> list[GeneratedFile]:
    alias_tables = {dim.base.alias: dim.base.table} | {
        j.right.alias: j.right.table for j in dim.joins
    }
    natural = dim.columns[0]
    select = [(dim.key_column, dim_key_expr(natural.expr))]
    select += [(c.name, c.expr) for c in dim.columns]
    unknown = ["{{ unknown_key() }}"] + [
        f"'{UNKNOWN_LABEL}'"
        if c.role == ColumnRole.ATTRIBUTE and _is_text(info, dim.base.table, c, alias_tables)
        else "null"
        for c in dim.columns
    ]
    sql = (
        f"-- adp により生成。{dim.label}ディメンション（SCD Type1）\n"
        + _select(select)
        + "\n"
        + _from_clause(dim.base, dim.joins)
        # 自然キーごとに 1 行にする（NULL の自然キーは不明メンバーと重複するので除く）
        + f"\nwhere {natural.expr} is not null"
        + "\nqualify row_number() over (partition by "
        + natural.expr
        + " order by "
        + natural.expr
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
    columns += [{"name": c.name, "description": c.description} for c in dim.columns]
    yml = _yaml(
        [
            {
                "name": dim.name,
                "description": f"{dim.label}ディメンション。SCD Type1。不明メンバーの行を含む",
                "columns": columns,
            }
        ]
    )
    return [
        GeneratedFile(f"models/marts/{dim.name}.sql", sql),
        GeneratedFile(f"models/marts/{dim.name}.yml", yml),
    ]


# --- fct -----------------------------------------------------------------------------------


def fact_files(fact: FactTable) -> list[GeneratedFile]:
    sql = (
        f"-- adp により生成\n-- 元イベント: {fact.label}（{fact.fact_type.value}）\n"
        f"-- 粒度: {fact.grain}\n"
        + _select([(c.name, c.expr) for c in fact.columns])
        + "\n"
        + _from_clause(fact.base, fact.joins)
        + "\n"
    )
    columns: list[dict[str, Any]] = []
    for c in fact.columns:
        entry: dict[str, Any] = {"name": c.name, "description": c.description}
        tests: list[Any] = []
        if fact.grain_columns == [c.name]:
            tests += ["unique", "not_null"]
        if c.role in (ColumnRole.FOREIGN_KEY, ColumnRole.TIME) and c.references:
            ref_model, _, ref_col = c.references.partition(".")
            if not c.nullable and "not_null" not in tests:
                tests.append("not_null")
            tests.append(
                {"relationships": {"arguments": {"to": f"ref('{ref_model}')", "field": ref_col}}}
            )
        if tests:
            entry["data_tests"] = tests
        columns.append(entry)
    model: dict[str, Any] = {
        "name": fact.name,
        "description": f"{fact.label}（{fact.fact_type.value}）。粒度: {fact.grain}",
        "columns": columns,
    }
    if len(fact.grain_columns) > 1:
        model["data_tests"] = [
            {
                "dbt_utils.unique_combination_of_columns": {
                    "arguments": {"combination_of_columns": fact.grain_columns}
                }
            }
        ]
    return [
        GeneratedFile(f"models/marts/{fact.name}.sql", sql),
        GeneratedFile(f"models/marts/{fact.name}.yml", _yaml([model])),
    ]


# --- 全体 ----------------------------------------------------------------------------------


def generate_models(design: PhysicalDesign, info: WarehouseInfo) -> Generation:
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
                            "既存の列は変更・削除しない。ソース: "
                            + ", ".join(
                                f"{c.name} = {c.expr}（{dim.base.table} が t0）"
                                for c in dim.columns
                                if c.name in missing
                            )
                        ),
                    )
                )
        else:
            result.files += dim_files(dim, info)

    for fact in design.facts:
        if fact.name in existing:
            result.tasks.append(
                AgentTask(
                    kind="fact_exists",
                    target=fact.name,
                    instruction=f"{fact.name} は既に存在する。上書きせず差分を PR に書く。",
                )
            )
        else:
            result.files += fact_files(fact)
    return result
