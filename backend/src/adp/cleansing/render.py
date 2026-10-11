"""クレンジングの定義から、クレンジング層（stg）の SQL と dbt の yml を決定的に生成する（#68）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from adp.cleansing.models import CleansingSpec, ColumnRule, Defaults, Scalar

GENERATED = "adp.cleansing により生成"
NOTE = "手で編集しない（make cleansing）"
"""生成物の先頭に入れる印。この印があるファイルは定義から作り直す（手で編集しない）。"""


@dataclass(frozen=True)
class SourceColumn:
    name: str
    data_type: str


@dataclass(frozen=True)
class Rendered:
    sql_path: str
    """warehouse からの相対パス。"""
    sql: str
    yml_path: str
    yml: str


def literal(value: Scalar) -> str:
    """SQL のリテラル。文字列は単一引用符をエスケープする（Jinja の文字は定義の検証で拒否済み）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + value.replace("'", "''") + "'"


def _assert_no_jinja(spec: CleansingSpec, sql: str, yml: str) -> None:
    """生成物に、source() の呼び出し以外の Jinja・エスケープの文字が無いこと。

    定義の検証（値の文字の拒否）の二重化。
    """
    source_call = f"{{{{ source('{spec.dbt_source or spec.schema_name}', '{spec.table_name}') }}}}"
    for name, text in (("sql", sql.replace(source_call, "", 1)), ("yml", yml)):
        if any(ch in text for ch in "{}\\"):
            raise ValueError(f"{spec.source}: 生成した {name} に使えない文字（{{ }} \\）が含まれる")


def _is_text(data_type: str) -> bool:
    return data_type.upper().startswith(("VARCHAR", "TEXT", "STRING", "CHAR"))


def column_expr(column: SourceColumn, rule: ColumnRule | None, defaults: Defaults) -> str:
    """1 列のクレンジングの式。

    順序: 型変換 → タイムゾーン → trim → 大文字小文字 → 空文字を NULL → 値の対応表。
    """
    expr = column.name
    data_type = column.data_type
    if rule is not None and rule.cast is not None:
        expr = f"try_cast({expr} as {rule.cast})"
        data_type = rule.cast
    if rule is not None and rule.timezone is not None:
        src, dst = rule.timezone
        expr = f"timezone({literal(dst)}, timezone({literal(src)}, {expr}))"
    if _is_text(data_type):
        trim = rule.trim if rule is not None and rule.trim is not None else defaults.text.trim
        empty = (
            rule.empty_to_null
            if rule is not None and rule.empty_to_null is not None
            else defaults.text.empty_to_null
        )
        if trim:
            expr = f"trim({expr})"
        if rule is not None and rule.case is not None:
            expr = f"{rule.case}({expr})"
        if empty:
            expr = f"nullif({expr}, '')"
    if rule is not None and rule.values:
        whens = " ".join(f"when {literal(k)} then {literal(v)}" for k, v in rule.values.items())
        expr = f"case {expr} {whens} else {expr} end"
    return expr


def _derive_expr(expr: str, kind: str) -> str:
    if kind == "date":
        return f"cast({expr} as date)"
    if kind == "month":
        return f"cast(date_trunc('month', {expr}) as date)"
    return f"year({expr})"


def _exclude_sql(spec: CleansingSpec) -> list[str]:
    conditions = []
    for x in spec.exclude:
        if x.is_null is not None:
            conditions.append(f"{x.column} is {'' if x.is_null else 'not '}null")
        elif x.in_ is not None:
            conditions.append(f"{x.column} in ({', '.join(literal(v) for v in x.in_)})")
        elif x.equals is not None:
            conditions.append(f"{x.column} = {literal(x.equals)}")
    return conditions


def output_columns(spec: CleansingSpec, columns: list[SourceColumn]) -> list[str]:
    out: list[str] = []
    for c in columns:
        rule = spec.rule(c.name)
        if rule is not None and rule.drop:
            continue
        out.append(rule.output if rule else c.name)
        if rule is not None:
            out += [d.name for d in rule.derive]
    return out


def render(spec: CleansingSpec, defaults: Defaults, columns: list[SourceColumn]) -> Rendered:
    select: list[str] = []
    for c in columns:
        rule = spec.rule(c.name)
        if rule is not None and rule.drop:
            continue
        expr = column_expr(c, rule, defaults)
        name = rule.output if rule else c.name
        select.append(expr if expr == name else f"{expr} as {name}")
        for d in rule.derive if rule else []:
            select.append(f"{_derive_expr(expr, d.as_)} as {d.name}")

    lines = [
        f"-- {GENERATED}（warehouse/cleansing/{spec.source}.yml）。{NOTE}",
        "with source as (",
        f"    select * from {{{{ source('{spec.dbt_source or spec.schema_name}',"
        f" '{spec.table_name}') }}}}",
        "),",
        "",
        "cleansed as (",
        "    select",
        ",\n".join(f"        {s}" for s in select),
        "    from source",
    ]
    excludes = _exclude_sql(spec)
    if excludes:
        # 条件の列が NULL の行は除外しない（not (NULL) で消えないよう false にする）
        conditions = " or ".join(f"({x})" for x in excludes)
        lines.append(f"    where not coalesce({conditions}, false)")
    lines += [")", "", "select * from cleansed"]
    if spec.dedupe is not None:
        order = ", ".join(f"{o.column}{' desc' if o.desc else ''}" for o in spec.dedupe.order_by)
        lines.append(
            f"qualify row_number() over (partition by {', '.join(spec.dedupe.key)}"
            f" order by {order}) = 1"
        )
    sql = "\n".join(lines) + "\n"

    model: dict[str, Any] = {"name": spec.model_name}
    if spec.description:
        model["description"] = spec.description
    yml_columns: list[dict[str, Any]] = []
    for c in columns:
        rule = spec.rule(c.name)
        if rule is not None and rule.drop:
            continue
        name = rule.output if rule else c.name
        entry: dict[str, Any] = {"name": name}
        if rule is not None and rule.description:
            entry["description"] = rule.description
        tests: list[Any] = []
        if name == spec.key:
            tests += ["unique", "not_null"]
        elif rule is not None and rule.not_null:
            tests.append("not_null")
        if rule is not None and rule.accepted_values:
            tests.append({"accepted_values": {"arguments": {"values": list(rule.accepted_values)}}})
        if tests:
            entry["data_tests"] = tests
        if len(entry) > 1:
            yml_columns.append(entry)
        for d in rule.derive if rule else []:
            derived: dict[str, Any] = {"name": d.name}
            if d.description:
                derived["description"] = d.description
            if d.name == spec.key:
                derived["data_tests"] = ["unique", "not_null"]
            if len(derived) > 1:
                yml_columns.append(derived)
    if yml_columns:
        model["columns"] = yml_columns
    yml = f"# {GENERATED}（warehouse/cleansing/{spec.source}.yml）。{NOTE}\n"
    yml += yaml.safe_dump(
        {"version": 2, "models": [model]}, allow_unicode=True, sort_keys=False, width=1000
    )
    _assert_no_jinja(spec, sql, yml)
    return Rendered(
        sql_path=f"models/staging/{spec.model_name}.sql",
        sql=sql,
        yml_path=f"models/staging/{spec.model_name}.yml",
        yml=yml,
    )
