"""クレンジングの定義と、定義からのクレンジング層の生成（#68）。"""

from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import ValidationError

from adp.catalog.catalog import Catalog
from adp.cleansing.check import check_all, draft, validate
from adp.cleansing.models import CleansingSpec, Defaults, load_all, load_defaults
from adp.cleansing.render import SourceColumn, column_expr, render

REPO = Path(__file__).resolve().parents[3]
WAREHOUSE = REPO / "warehouse"


def _spec(**over: Any) -> CleansingSpec:
    return CleansingSpec.model_validate({"source": "raw.t", **over})


COLUMNS = [
    SourceColumn("id", "INTEGER"),
    SourceColumn("status", "VARCHAR"),
    SourceColumn("amount_text", "VARCHAR"),
    SourceColumn("created_at", "TIMESTAMP"),
    SourceColumn("updated_at", "TIMESTAMP"),
    SourceColumn("legacy", "VARCHAR"),
]
TEXT = Defaults.model_validate({"text": {"trim": True, "empty_to_null": True}})


@pytest.mark.parametrize(
    "over",
    [
        {"columns": [{"name": "status", "values": {"a": "{{ env_var('X') }}"}}]},
        {"columns": [{"name": "status", "accepted_values": ["a}"]}]},
        {"columns": [{"name": "status", "description": "x\\\\y"}]},
        {"exclude": [{"column": "status", "equals": "{% raw %}"}]},
        {"columns": [{"name": "status", "cast": "varchar); drop table x; --"}]},
        {"columns": [{"name": "status", "timezone": ["UTC", "Asia/Tokyo'); --"]}]},
        {"columns": [{"name": "Status"}]},
        {"columns": [{"name": "status"}, {"name": "status"}]},
        {"exclude": [{"column": "status", "is_null": True, "equals": "x"}]},
        {"columns": [{"name": "status", "sql": "select 1"}]},
        # #86 レビュー
        {
            "columns": [
                {
                    "name": "created_at",
                    "derive": [{"name": "d", "as": "date", "description": "{{ env_var('HOME') }}"}],
                }
            ]
        },
        {"columns": [{"name": "status", "accepted_values": [True, "x"]}]},  # YAML の yes / no
        {"exclude": [{"column": "status", "equals": float("nan")}]},
        {"exclude": [{"column": "status", "in": []}]},
        {"columns": [{"name": "status", "cast": "decimal(99, 99)"}]},
        {"columns": [{"name": "status", "cast": "decimal(5, 6)"}]},
        {"columns": [{"name": "status", "rename": "order"}]},
        {"model": "stg_other"},
    ],
)
def test_unsafe_or_invalid_definitions_are_rejected(over: dict[str, Any]) -> None:
    """定義は構造化された項目だけ。Jinja・任意の SQL・不正な識別子は通さない。"""
    with pytest.raises(ValidationError):
        _spec(**over)


def test_render_applies_rules_in_order() -> None:
    spec = _spec(
        key="id",
        columns=[
            {"name": "status", "case": "lower", "values": {"done": "completed", "it's": "x"}},
            {"name": "amount_text", "cast": "decimal(12, 2)", "rename": "amount"},
            {
                "name": "created_at",
                "timezone": ["UTC", "Asia/Tokyo"],
                "derive": [{"name": "created_on", "as": "date", "description": "作成日"}],
            },
            {"name": "legacy", "drop": True},
        ],
        exclude=[{"column": "status", "in": ["test", "dummy"]}, {"column": "id", "is_null": True}],
        dedupe={"key": ["id"], "order_by": [{"column": "updated_at", "desc": True}]},
    )
    r = render(spec, TEXT, COLUMNS)
    assert "case nullif(lower(trim(status)), '') when 'done' then 'completed'" in r.sql
    assert "when 'it''s' then 'x'" in r.sql  # 引用符のエスケープ
    assert "try_cast(amount_text as decimal(12, 2)) as amount" in r.sql
    assert "timezone('Asia/Tokyo', timezone('UTC', created_at)) as created_at" in r.sql
    assert (
        "cast(timezone('Asia/Tokyo', timezone('UTC', created_at)) as date) as created_on" in r.sql
    )
    assert "legacy" not in r.sql
    assert "where not coalesce((status in ('test', 'dummy')) or (id is null), false)" in r.sql
    assert "qualify row_number() over (partition by id order by updated_at desc) = 1" in r.sql
    assert "{{ source('raw', 't') }}" in r.sql
    assert "unique" in r.yml and "作成日" in r.yml


def test_render_runs_on_duckdb(tmp_path: Path) -> None:
    """生成した SQL を DuckDB で実行し、クレンジングの結果を確かめる。"""
    con = duckdb.connect()
    con.execute(
        "create table t as select * from (values"
        " (1, ' Done ', '10.5', timestamp '2024-01-01 20:00:00', timestamp '2024-01-01', 'x'),"
        " (1, 'done', '11', timestamp '2024-01-01 20:00:00', timestamp '2024-02-01', 'x'),"
        " (2, '', 'abc', timestamp '2024-01-02 00:00:00', timestamp '2024-01-01', 'x'),"
        " (3, 'test', '1', timestamp '2024-01-02 00:00:00', timestamp '2024-01-01', 'x'))"
        " v(id, status, amount_text, created_at, updated_at, legacy)"
    )
    spec = _spec(
        columns=[
            {"name": "status", "case": "lower", "values": {"done": "completed"}},
            {"name": "amount_text", "cast": "double", "rename": "amount"},
            {
                "name": "created_at",
                "timezone": ["UTC", "Asia/Tokyo"],
                "derive": [{"name": "created_on", "as": "date"}],
            },
            {"name": "legacy", "drop": True},
        ],
        exclude=[{"column": "status", "equals": "test"}],
        dedupe={"key": ["id"], "order_by": [{"column": "updated_at", "desc": True}]},
    )
    sql = render(spec, TEXT, COLUMNS).sql.replace("{{ source('raw', 't') }}", "t")
    rows = con.execute(sql.split("\n", 1)[1] + " order by id").fetchall()
    assert [(r[0], r[1], r[2], str(r[4])) for r in rows] == [
        (1, "completed", 11.0, "2024-01-02"),  # 更新日時の新しい行、UTC 20 時 → 東京の翌日
        (2, None, None, "2024-01-02"),  # 空文字 → NULL、変換できない値 → NULL
    ]


def test_validate_against_source_columns() -> None:
    spec = _spec(
        key="nope",
        columns=[{"name": "missing"}, {"name": "status", "rename": "id"}],
        exclude=[{"column": "gone", "is_null": True}],
        dedupe={"key": ["id"], "order_by": [{"column": "zzz"}]},
    )
    errors = validate(spec, COLUMNS)
    assert any("列 missing が源泉に無い" in e for e in errors)
    assert any("exclude の列 gone" in e for e in errors)
    assert any("重複" in e for e in errors)  # rename で id が 2 つ
    assert any("key nope" in e for e in errors)
    assert any("dedupe の列 zzz" in e for e in errors)


def test_defaults_can_be_overridden_per_column() -> None:
    rule = _spec(columns=[{"name": "status", "trim": False}]).rule("status")
    assert column_expr(SourceColumn("status", "VARCHAR"), rule, TEXT) == "nullif(status, '')"
    assert column_expr(SourceColumn("id", "INTEGER"), None, TEXT) == "id"


def test_repository_definitions_reproduce_previous_staging() -> None:
    """回帰: 定義から生成した stg が、以前の手書きの stg と同じ結果になる。

    以前の stg は源泉の列をそのまま選び、注文では注文日を切り出していた。
    """
    db = WAREHOUSE / "data" / "dev.duckdb"
    if not db.exists():
        pytest.skip("make setup が必要")
    defaults = load_defaults(WAREHOUSE / "cleansing")
    con = duckdb.connect(str(db), read_only=True)
    specs = load_all(WAREHOUSE / "cleansing")
    assert {s.model_name for s in specs} >= {
        "stg_customers",
        "stg_products",
        "stg_stores",
        "stg_promotions",
        "stg_orders",
        "stg_order_items",
    }
    for spec in specs:
        cols = [
            SourceColumn(n, t)
            for n, t in con.execute(
                "select column_name, data_type from information_schema.columns"
                " where table_schema = ? and table_name = ? order by ordinal_position",
                [spec.schema_name, spec.table_name],
            ).fetchall()
        ]
        new = (
            render(spec, defaults, cols)
            .sql.split("\n", 1)[1]
            .replace(f"{{{{ source('{spec.schema_name}', '{spec.table_name}') }}}}", spec.source)
        )
        orders = spec.table_name == "orders"
        derived = ", cast(ordered_at as date) as ordered_on" if orders else ""
        old = f"select *{derived} from {spec.source}"
        old_cols = [c.name for c in cols] + (["ordered_on"] if orders else [])
        cols_sql = ", ".join(old_cols)
        for a, b in ((new, old), (old, new)):
            diff = con.execute(
                f"select count(*) from (select {cols_sql} from ({a})"
                f" except all select {cols_sql} from ({b}))"
            ).fetchone()
            assert diff == (0,), spec.source
    con.close()


def test_repository_definitions_are_current() -> None:
    """warehouse/cleansing/ の定義から生成したものと、models/staging/ の中身が一致する。"""
    db = WAREHOUSE / "data" / "dev.duckdb"
    if not db.exists():
        pytest.skip("make setup が必要")
    result = check_all(WAREHOUSE, Catalog(db, WAREHOUSE / "target" / "manifest.json"))
    assert result.errors == []
    for r in result.rendered:
        assert (WAREHOUSE / r.sql_path).read_text(encoding="utf-8") == r.sql
        assert (WAREHOUSE / r.yml_path).read_text(encoding="utf-8") == r.yml


def test_orphan_generated_files_are_reported(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    (warehouse / "cleansing").mkdir(parents=True)
    (warehouse / "models" / "staging").mkdir(parents=True)
    (warehouse / "models" / "staging" / "stg_gone.sql").write_text("-- adp.cleansing により生成\n")
    (warehouse / "models" / "staging" / "stg_hand.sql").write_text("select 1\n")
    db = tmp_path / "x.duckdb"
    duckdb.connect(str(db)).execute("create schema raw").close()
    result = check_all(warehouse, Catalog(db))
    assert result.errors == [
        "models/staging/stg_gone.sql: 定義（warehouse/cleansing/）が無い生成物が残っている",
        "models/staging/stg_hand.sql: 定義（warehouse/cleansing/）が無い（stg は定義から生成する）",
    ]


def test_draft_from_profile(tmp_path: Path) -> None:
    db = tmp_path / "x.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema raw")
    con.execute(
        "create table raw.events as select * from (values"
        " (1, 'a', '2024-01-01 00:00:00', timestamp '2024-01-01'),"
        " (1, 'b', '2024-01-02 00:00:00', timestamp '2024-01-02'),"
        " (2, 'a', '2024-01-03 00:00:00', timestamp '2024-01-01'))"
        " v(event_id, kind, occurred_at, updated_at)"
    )
    con.close()
    spec = draft(Catalog(db).profile_table("raw.events"))
    assert spec.source == "raw.events" and spec.key == "event_id"
    assert spec.dedupe is not None and spec.dedupe.order_by[0].column == "updated_at"
    assert spec.rule("occurred_at").cast == "timestamp"  # type: ignore[union-attr]
    assert spec.rule("kind").accepted_values == ["a", "b"]  # type: ignore[union-attr]


def test_exclude_keeps_rows_with_null() -> None:
    """除外の条件の列が NULL の行は残す（#86 レビュー）。"""
    con = duckdb.connect()
    con.execute(
        "create table t as select * from (values (1, 'TEST'), (2, NULL), (3, 'ok')) v(id, code)"
    )
    spec = _spec(exclude=[{"column": "code", "equals": "TEST"}])
    cols = [SourceColumn("id", "INTEGER"), SourceColumn("code", "VARCHAR")]
    sql = render(spec, Defaults(), cols).sql.split("\n", 1)[1]
    sql = sql.replace("{{ source('raw', 't') }}", "t")
    assert [r[0] for r in con.execute(sql + " order by id").fetchall()] == [2, 3]


def test_key_on_derived_column_gets_tests() -> None:
    spec = _spec(
        key="created_on",
        columns=[{"name": "created_at", "derive": [{"name": "created_on", "as": "date"}]}],
    )
    yml = render(spec, TEXT, COLUMNS).yml
    assert "- name: created_on\n    data_tests:\n    - unique\n    - not_null" in yml


def test_rules_must_fit_the_column_type() -> None:
    spec = _spec(
        columns=[
            {"name": "id", "case": "lower"},
            {"name": "status", "timezone": ["UTC", "Asia/Tokyo"]},
            {"name": "amount_text", "cast": "integer", "values": {"1": "one"}},
            {"name": "legacy", "derive": [{"name": "y", "as": "year"}]},
            {"name": "created_at", "cast": "varchar", "trim": True},
        ]
    )
    errors = validate(spec, COLUMNS)
    assert len(errors) == 4, errors  # varchar に変換した列の trim は使える
    assert any("raw.t.id" in e for e in errors) and any("raw.t.legacy" in e for e in errors)


def test_generator_aligns_dbt_source_for_undeclared_sources() -> None:
    """未宣言の源泉で既存の定義（dbt_source なし）を使うとき、別名の source に合わせる。"""
    from adp.generation.dbt import staging_files

    class Info:
        warehouse_dir = WAREHOUSE

        def declared_sources(self) -> set[str]:
            return set()

        def source_columns(self, table: str) -> list[tuple[str, str]]:
            return [("order_id", "INTEGER"), ("ordered_at", "TIMESTAMP")]

        def source_primary_key(self, table: str) -> str | None:
            return "order_id"

    files = {f.path: f.content for f in staging_files("raw.orders", Info())}  # type: ignore[arg-type]
    assert "{{ source('adp_orders', 'orders') }}" in files["models/staging/stg_orders.sql"]
    assert "models/staging/_adp_orders__sources.yml" in files


def test_draft_skips_unrepresentable_columns(tmp_path: Path) -> None:
    db = tmp_path / "x.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema raw")
    con.execute(
        "create table raw.t as select * from"
        " (values (1, 'a', 'x' || chr(9) || 'y'), (2, 'b', 'z')) v(t_id, \"Kind\", tabbed)"
    )
    con.close()
    spec = draft(Catalog(db).profile_table("raw.t"))
    assert spec.key == "t_id" and spec.rule("Kind") is None and spec.rule("tabbed") is None
