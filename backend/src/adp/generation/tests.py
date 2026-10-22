"""生成した fct / dim に付けるテストを作る（docs/modeling-conventions.md の「テスト（必須）」）。

- 区分値: 種類の少ない文字列の列に accepted_values（データの変化に気づくための警告）
- 突合: ベースの staging と fct の件数（結合による行の増減の検出）と、ベースの列だけで
  計算できる measure の合計
- unit test: BEAM の具体例から入力行を組み立て、期待値は BEAM の定義から独立に計算する
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import duckdb

from adp.beam import BeamModel, BusinessEvent, EventExample, StateMethod, TimeGrain
from adp.catalog.types import is_text_type
from adp.design import ColumnRole, DimTable, FactTable
from adp.generation.dbt import GeneratedFile, WarehouseInfo

ACCEPTED_VALUES_MAX = 20
UNKNOWN = "__unknown__"
_COLUMN_REF = re.compile(r"^(t\d+)\.([A-Za-z_]\w*)$")


def surrogate_key(value: object) -> str:
    """dim_key() と同じハッシュ（dbt_utils.generate_surrogate_key、1 列）。"""
    text = UNKNOWN if value is None else str(value)
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# --- accepted_values -----------------------------------------------------------------------


def accepted_values(
    info: WarehouseInfo, table: str, column: str, include_unknown: bool = False
) -> dict[str, Any] | None:
    values = info.distinct_values(table, column, ACCEPTED_VALUES_MAX)
    if values is None:
        return None
    if include_unknown and "不明" not in values:
        values = [*values, "不明"]
    return {
        "accepted_values": {
            "arguments": {"values": values},
            "config": {"severity": "warn"},
        }
    }


def _alias_tables(t: FactTable | DimTable) -> dict[str, str]:
    return {t.base.alias: t.base.table} | {j.right.alias: j.right.table for j in t.joins}


def fact_column_tests(fact: FactTable, info: WarehouseInfo) -> dict[str, list[Any]]:
    tables = _alias_tables(fact)
    result: dict[str, list[Any]] = {}
    for c in fact.columns:
        m = _COLUMN_REF.match(c.expr)
        if c.role != ColumnRole.DEGENERATE or c.name in fact.grain_columns or m is None:
            continue
        types = dict(info.source_columns(tables[m.group(1)]))
        if not is_text_type(types.get(m.group(2), "")):
            continue
        test = accepted_values(info, tables[m.group(1)], m.group(2))
        if test:
            result[c.name] = [test]
    return result


def dim_column_tests(dim: DimTable, info: WarehouseInfo) -> dict[str, list[Any]]:
    tables = _alias_tables(dim)
    result: dict[str, list[Any]] = {}
    for c in dim.columns:
        m = _COLUMN_REF.match(c.expr)
        if c.role != ColumnRole.ATTRIBUTE or m is None:
            continue
        types = dict(info.source_columns(tables[m.group(1)]))
        if not is_text_type(types.get(m.group(2), "")):
            continue
        test = accepted_values(info, tables[m.group(1)], m.group(2), include_unknown=True)
        if test:
            result[c.name] = [test]
    return result


# --- 突合 ----------------------------------------------------------------------------------


def reconciliation_test(fact: FactTable) -> GeneratedFile:
    base_measures = [
        c
        for c in fact.columns
        if c.role == ColumnRole.MEASURE and set(re.findall(r"\b(t\d+)\.", c.expr)) <= {"t0"}
    ]
    sums_src = "".join(f", sum({c.expr}) as {c.name}" for c in base_measures)
    sums_fct = "".join(f", sum({c.name}) as {c.name}" for c in base_measures)
    # 浮動小数点の合計は足す順番で末尾が変わるので、相対誤差で比べる
    conds = " or ".join(
        ["s.n <> f.n"]
        + [
            f"abs(coalesce(s.{c.name}, 0) - coalesce(f.{c.name}, 0))"
            f" > 1e-9 * greatest(1, abs(coalesce(s.{c.name}, 0)))"
            for c in base_measures
        ]
    )
    sql = f"""-- adp により生成。{fact.name} の件数と measure の合計が、ベースのソースと一致すること
-- （結合による行の重複・欠落の検出）
with s as (
    select count(*) as n{sums_src}
    from {{{{ ref('{fact.base.model}') }}}} as t0
),

f as (
    select count(*) as n{sums_fct}
    from {{{{ ref('{fact.name}') }}}}
)

select *
from s, f
where {conds}
"""
    return GeneratedFile(f"tests/assert_{fact.name}_reconciles_with_source.sql", sql)


# --- unit test -----------------------------------------------------------------------------


def _default(data_type: str) -> object:
    t = data_type.upper()
    if any(k in t for k in ("INT", "DOUBLE", "DECIMAL", "FLOAT", "NUMERIC", "REAL")):
        return 1
    if "TIMESTAMP" in t:
        return "2024-01-01 00:00:00"
    if t == "DATE":
        return "2024-01-01"
    if t == "BOOLEAN":
        return False
    return "x"


def _cast(value: object, data_type: str) -> object:
    t = data_type.upper()
    if value is None:
        return None
    if "TIMESTAMP" in t and isinstance(value, str) and len(value) == 10:
        return f"{value} 00:00:00"
    if isinstance(value, date | datetime):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return value


def _example_targets(event: BusinessEvent, fact: FactTable) -> dict[str, tuple[str, str]]:
    """具体例のキー → (別名, 列) のうち、入力行に直接書けるもの。"""
    targets: dict[str, tuple[str, str]] = {}
    for c in fact.columns:
        m = _COLUMN_REF.match(c.expr) or (
            re.match(r"^cast\((t\d+)\.(\w+) as date\)$", c.expr)
            if c.role == ColumnRole.TIME
            else None
        )
        if c.source_ref and m:
            targets.setdefault(c.source_ref, (m.group(1), m.group(2)))
    for d in event.dimensions:
        if d.masterless:
            for a in d.attributes:
                if d.ref in targets:
                    targets[f"{d.ref}.{a.name}"] = targets[d.ref]
    return targets


def unit_test(
    beam: BeamModel, fact: FactTable, info: WarehouseInfo, example: EventExample, index: int
) -> dict[str, Any] | None:
    event = beam.event(fact.event)
    if event is None:
        return None
    tables = _alias_tables(fact)
    types = {a: dict(info.source_columns(t)) for a, t in tables.items()}
    needed: dict[str, set[str]] = {a: set() for a in tables}
    for c in fact.columns:
        for alias, col in re.findall(r"\b(t\d+)\.(\w+)", c.expr):
            needed[alias].add(col)
    for j in fact.joins:
        needed[j.left_alias].add(j.left_column)
        needed[j.right.alias].add(j.right_column)

    def rows(case: str) -> dict[str, dict[str, object]]:
        """case: example（具体例）/ unknown（FK の自然キーが NULL）"""
        result = {
            a: {c: _default(types[a].get(c, "")) for c in sorted(cols)}
            for a, cols in needed.items()
        }
        key = 1 if case == "example" else 2
        for j in fact.joins:  # 結合キーは同じ値にする
            result[j.left_alias][j.left_column] = key
            result[j.right.alias][j.right_column] = key
        for name in fact.grain_columns:
            grain_col = fact.column(name)
            if grain_col and (gm := _COLUMN_REF.match(grain_col.expr)):
                result[gm.group(1)][gm.group(2)] = key
        if case == "example":
            set_by_example: set[tuple[str, str]] = set()
            for k, (alias, col) in _example_targets(event, fact).items():
                if k in example.values:
                    result[alias][col] = _cast(example.values[k], types[alias].get(col, ""))
                    set_by_example.add((alias, col))
            # 具体例が結合キーの片側を決めたら、もう片側にも同じ値を入れる（結合が外れないように）
            for j in fact.joins:
                left, right = (j.left_alias, j.left_column), (j.right.alias, j.right_column)
                if left in set_by_example:
                    result[right[0]][right[1]] = result[left[0]][left[1]]
                elif right in set_by_example:
                    result[left[0]][left[1]] = result[right[0]][right[1]]
            _solve_example_measures(event, fact, example, result, types, set_by_example)
        else:
            # 結合キー以外の外部キーの自然キーを NULL にする（不明メンバーになることを検証）
            join_keys = {(j.left_alias, j.left_column) for j in fact.joins}
            join_keys |= {(j.right.alias, j.right_column) for j in fact.joins}
            for c in fact.columns:
                fk = re.search(r"(t\d+)\.(\w+)", c.expr)
                if (
                    c.role == ColumnRole.FOREIGN_KEY
                    and fk
                    and (fk.group(1), fk.group(2)) not in join_keys
                ):
                    result[fk.group(1)][fk.group(2)] = None
        return result

    cases = [rows("example"), rows("unknown")]
    expected = [_expected(event, fact, r, types) for r in cases]
    given = []
    for alias in tables:
        model = (
            fact.base.model
            if alias == fact.base.alias
            else next(j.right.model for j in fact.joins if j.right.alias == alias)
        )
        given.append({"input": f"ref('{model}')", "rows": [c[alias] for c in cases]})
    return {
        "name": f"test_{fact.name}_example_{index + 1}",
        "description": "BEAM の具体例から生成（期待値は BEAM の定義から独立に計算）。"
        "2 行目は外部キーの自然キーが NULL で、不明メンバーのキーになることを検証する",
        "model": fact.name,
        "given": given,
        "expect": {"rows": expected},
    }


def _expected(
    event: BusinessEvent,
    fact: FactTable,
    row: dict[str, dict[str, object]],
    types: dict[str, dict[str, str]],
) -> dict[str, object]:
    out: dict[str, object] = {}
    for c in fact.columns:
        m = _COLUMN_REF.match(c.expr)
        if c.role in (ColumnRole.DEGENERATE,) and m:
            out[c.name] = row[m.group(1)][m.group(2)]
        elif c.role == ColumnRole.FOREIGN_KEY and (fm := re.search(r"(t\d+)\.(\w+)", c.expr)):
            out[c.name] = surrogate_key(row[fm.group(1)][fm.group(2)])
        elif c.role == ColumnRole.TIME and (tm := re.search(r"(t\d+)\.(\w+)", c.expr)):
            v = row[tm.group(1)][tm.group(2)]
            out[c.name] = str(v)[:10] if v is not None else None
        elif c.role == ColumnRole.MEASURE:
            measure = event.measure(c.source_ref or "")
            if measure is None or measure.source is None:
                continue
            alias = next(
                (
                    a
                    for a, t in {fact.base.alias: fact.base.table}.items()
                    if t == measure.source.table
                ),
                None,
            ) or next(
                (j.right.alias for j in fact.joins if j.right.table == measure.source.table), None
            )
            if alias is None:
                continue
            source_expr = measure.source.column or measure.source.expr or ""
            out[c.name] = _evaluate(source_expr, row[alias], types[alias])
    return out


def _measure_alias(fact: FactTable, table: str) -> str | None:
    if fact.base.table == table:
        return fact.base.alias
    return next((j.right.alias for j in fact.joins if j.right.table == table), None)


def _solve_example_measures(
    event: BusinessEvent,
    fact: FactTable,
    example: EventExample,
    rows: dict[str, dict[str, object]],
    types: dict[str, dict[str, str]],
    fixed: set[tuple[str, str]],
) -> None:
    """具体例に式の measure の値があれば、その値になるように入力列を 1 つ決める。

    式が未知の列について 1 次式（売上 = 数量 × 単価 − 値引 など）のときに限り、
    2 点で評価して解く。解けなければ既定値のまま（期待値は定義から計算される）。
    """
    for m in event.measures:
        if m.source is None or m.source.expr is None or m.name not in example.values:
            continue
        target = example.values[m.name]
        alias = _measure_alias(fact, m.source.table)
        if alias is None or not isinstance(target, int | float) or isinstance(target, bool):
            continue
        row = rows[alias]
        unknowns = [
            c
            for c in re.findall(r"[A-Za-z_]\w*", m.source.expr)
            if c in row and (alias, c) not in fixed
        ]
        # 規約: 具体例に無い入力は、テストが成り立つ最小の値（値引 0 など）で補う
        for col in unknowns:
            if _default(types[alias].get(col, "")) == 1:
                row[col] = 0
        for col in dict.fromkeys(unknowns):
            f0 = _evaluate(m.source.expr, row | {col: 0}, types[alias])
            f1 = _evaluate(m.source.expr, row | {col: 1}, types[alias])
            if not isinstance(f0, int | float) or not isinstance(f1, int | float) or f1 == f0:
                continue
            x = (float(target) - f0) / (f1 - f0)
            value: object = int(x) if float(x).is_integer() else x
            if _evaluate(m.source.expr, row | {col: value}, types[alias]) == target:
                row[col] = value
                fixed.add((alias, col))
                break


def _evaluate(expr: str, values: dict[str, object], types: dict[str, str]) -> object:
    """BEAM の measure の式を、入力行の値で DuckDB に計算させる（生成 SQL とは独立）。"""
    cols = ", ".join(f'cast(? as {types.get(k) or "varchar"}) as "{k}"' for k in values)
    con = duckdb.connect()
    try:
        row = con.execute(f"select {expr} from (select {cols})", list(values.values())).fetchone()
    finally:
        con.close()
    assert row is not None
    value = row[0]
    if isinstance(value, Decimal):  # YAML に書けるようにする
        return int(value) if value == value.to_integral_value() else float(value)
    return value


# --- 定期スナップショット（ADR 0008） ----------------------------------------------------------


def snapshot_reconciliation_test(fact: FactTable, info: WarehouseInfo | None) -> GeneratedFile:
    """定期スナップショットの fct と源泉の突合。合計は期間をまたいで足せないので、次を見る。

    - 区切りごとに、最初の期間から最後の期間まで、期間の終わりの日付が途切れていない
    - 期間の最後の記録の値と一致する（last_value はすべての期間、running_total は最後の記録が
      置き換える行の期間）
    """
    from adp.generation.dbt import _from_clause, _select, period_end, snapshot_order_key

    snap = fact.snapshot
    assert snap is not None
    names = snapshot_partition(fact)
    cols = {c.name: c for c in fact.columns}
    checked = [
        m
        for m, method in snap.methods.items()
        if method == StateMethod.LAST_VALUE or m in snap.resets
    ]
    records = [(n, cols[n].expr) for n in names]
    records += [
        (snap.time_column, period_end(snap.recorded_at, snap.grain)),
        ("recorded_at", snap.recorded_at),
        ("record_order", snapshot_order_key(fact, info)),
    ]
    records += [(m, cols[m].expr) for m in checked]
    records += [(f"{m}__is_reset", f"coalesce({snap.resets[m]}, false)") for m in snap.resets]
    body = _select(records) + "\n" + _from_clause(fact.base, fact.joins)
    keys = [*names, snap.time_column]
    on = " and ".join(
        [f"r.{n} is not distinct from f.{n}" for n in names]
        + [f"r.{snap.time_column} = f.{snap.time_column}"]
    )
    parts = [
        "select '期間が途切れている' as reason\nfrom counts\nwhere n <> (\n"
        "    select count(*) from periods\n"
        "    where date_day between counts.first_on and counts.last_on\n)"
    ]
    for m in checked:
        cond = f"r.{m} is distinct from f.{m}"
        if snap.methods[m] == StateMethod.RUNNING_TOTAL:
            cond = f"r.{m}__is_reset and {cond}"
        parts.append(
            f"select '{m} が期間の最後の記録の値と違う' as reason\n"
            f"from last_records as r\n"
            f"inner join {{{{ ref('{fact.name}') }}}} as f on {on}\n"
            f"where {cond}"
        )
    group = f"\n    group by {', '.join(names)}" if names else ""
    partition = ", ".join(keys)
    sql = (
        f"-- adp により生成。{fact.name}（定期スナップショット）が、区切りごとに期間が途切れず、\n"
        "-- 期間の最後の記録の値と一致すること（ADR 0008）\n"
        "with counts as (\n"
        f"    select {''.join(f'{n}, ' for n in names)}count(*) as n,"
        f" min({snap.time_column}) as first_on, max({snap.time_column}) as last_on\n"
        f"    from {{{{ ref('{fact.name}') }}}}{group}\n),\n\n"
        "periods as (\n    select date_day\n    from {{ ref('dim_date') }}\n"
        f"    where date_day = {period_end('date_day', snap.grain)}\n),\n\n"
        "records as (\n"
        + "\n".join("    " + line for line in body.splitlines())
        + "\n),\n\nlast_records as (\n    select *\n    from records\n"
        f"    qualify row_number() over (partition by {partition}"
        " order by recorded_at desc, record_order desc) = 1\n)\n\n"
        + "\n\nunion all\n\n".join(parts)
        + "\n"
    )
    return GeneratedFile(f"tests/assert_{fact.name}_reconciles_with_source.sql", sql)


def snapshot_partition(fact: FactTable) -> list[str]:
    """定期スナップショットの区切りの列（粒度の列から期間の終わりの日付を除いたもの）。"""
    snap = fact.snapshot
    assert snap is not None
    return [n for n in fact.grain_columns if n != snap.time_column]


def _period_ends(last: date, grain: TimeGrain, n: int) -> list[date]:
    """last を含む期間の終わりの日付から、n 期間さかのぼった期間の終わりの日付（古い順）。

    期間の区切りは生成した SQL と同じ式（`period_end`）で DuckDB に計算させる。"""
    from adp.generation.dbt import period_end

    ends: list[date] = []
    cursor = last
    for _ in range(n):
        end = duckdb.sql("select " + period_end(f"date '{cursor.isoformat()}'", grain)).fetchone()
        assert end is not None
        ends.append(end[0])
        start = duckdb.sql(
            f"select cast(date_trunc('{'week' if grain == TimeGrain.WEEK else grain.value}',"
            f" date '{cursor.isoformat()}') as date)"
        ).fetchone()
        assert start is not None
        cursor = start[0] - timedelta(days=1)
    return list(reversed(ends))


def snapshot_unit_test(
    beam: BeamModel, fact: FactTable, info: WarehouseInfo, example: EventExample, index: int
) -> dict[str, Any] | None:
    """具体例の値 E と時点から、3 期間の記録の履歴を作り、期待値を独立に計算する（ADR 0008）。

    - 2 期間前: 増減（置き換える行より前なので数えない）と、置き換える行（E − 2）
    - 1 期間前: 記録なし（前の期間の値を引き継ぐ）
    - 当期: +5 と −3
    期待値は E − 2・E − 2・E。last_value は各記録をその時点の値として同じ結果になるようにする。
    入力に書けるのはベースのテーブルの列だけ（結合が要る fct は作らない）。
    """
    from adp.design.builder import filter_source

    event = beam.event(fact.event)
    snap = fact.snapshot
    if event is None or snap is None or fact.joins:
        return None
    base = fact.base.alias
    types = dict(info.source_columns(fact.base.table))

    def base_column(expr: str) -> str | None:
        m = re.search(rf"\b{base}\.(\w+)", expr)
        return m.group(1) if m and set(re.findall(r"\b(t\d+)\.", expr)) == {base} else None

    recorded = base_column(snap.recorded_at)
    pk = info.source_primary_key(fact.base.table)
    if recorded is None:
        return None
    # 置き換える行・そうでない行の、条件の列の値
    reset_values: dict[str, object] = {}
    other_values: dict[str, object] = {}
    for m in snap.resets:
        measure = event.measure(m)
        for f in measure.state.reset if measure and measure.state else []:
            src = filter_source(event, f.ref)
            if src is None or src.table != fact.base.table or src.column is None:
                return None
            known = info.distinct_values(src.table, src.column, ACCEPTED_VALUES_MAX) or []
            listed = [str(v) for v in f.values]
            outside = next((v for v in known if v not in listed), None)
            if f.operator.value in ("=", "in"):
                reset_values[src.column], other_values[src.column] = f.values[0], outside
            else:
                reset_values[src.column], other_values[src.column] = outside, f.values[0]
            if outside is None:
                return None
    time_col = fact.column(snap.time_column)
    raw_when = example.values.get(time_col.source_ref) if time_col and time_col.source_ref else None
    try:
        last_day = date.fromisoformat(str(raw_when)[:10]) if raw_when else date(2025, 3, 31)
    except ValueError:
        last_day = date(2025, 3, 31)
    p2, p1, p0 = _period_ends(last_day, snap.grain, 3)

    finals: dict[str, int] = {}
    for m in snap.methods:
        v = example.values.get(m)
        finals[m] = v if isinstance(v, int) and not isinstance(v, bool) and v >= 2 else 10

    def values(step: int, m: str) -> int:
        """記録ごとの measure の値（求め方ごと）。期間の値が E − 2・E − 2・E になるようにする。"""
        e = finals[m]
        method = snap.methods[m]
        if method == StateMethod.LAST_VALUE:
            return [1, e - 2, 1, e][step]
        if m in snap.resets:
            return [4, e - 2, 5, -3][step]
        return [0, e - 2, 5, -3][step]

    plan = [(p2, 9, False), (p2, 10, True), (p0, 10, False), (p0, 11, False)]
    parts = snapshot_partition(fact)
    natural: dict[str, object] = {}
    expected_keys: dict[str, object] = {}
    for n in parts:
        col = fact.column(n)
        name = base_column(col.expr) if col else None
        if col is None or name is None:
            return None
        value = _default(types.get(name, ""))
        natural[name] = value
        expected_keys[n] = surrogate_key(value) if col.role == ColumnRole.FOREIGN_KEY else value
    rows = []
    for step, (day, hour, is_reset) in enumerate(plan):
        row: dict[str, object] = dict(natural)
        row[recorded] = f"{day.isoformat()} {hour:02d}:00:00"  # 期間の終わりの日の記録
        if pk:
            row[pk] = step + 1
        for m in snap.methods:
            measure_col = fact.column(m)
            name = base_column(measure_col.expr) if measure_col else None
            if name is None:
                return None
            row[name] = values(step, m)
        row |= reset_values if is_reset else other_values
        rows.append(row)
    columns = sorted({k for r in rows for k in r})
    given_rows = [{c: r.get(c) for c in columns} for r in rows]
    spine = [p2, p1, p0, p0 - timedelta(days=1) if snap.grain != TimeGrain.DAY else p0]
    expect = [
        {
            **expected_keys,
            snap.time_column: d.isoformat(),
            **{m: finals[m] + offset for m in snap.methods},
        }
        for d, offset in ((p2, -2), (p1, -2), (p0, 0))
    ]
    return {
        "name": f"test_{fact.name}_example_{index + 1}",
        "description": "BEAM の具体例の値と時点から 3 期間の記録を作り、期待値を独立に計算する"
        "（2 期間前に置き換える行、1 期間前は記録なしで引き継ぎ、当期に増減。ADR 0008）",
        "model": fact.name,
        "given": [
            {"input": f"ref('{fact.base.model}')", "rows": given_rows},
            {
                "input": "ref('dim_date')",
                "rows": [{"date_day": d.isoformat()} for d in sorted(set(spine))],
            },
        ],
        "expect": {"rows": expect},
    }
