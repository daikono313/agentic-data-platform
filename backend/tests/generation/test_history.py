"""属性の履歴（snapshot と履歴モデル。ADR 0009）の生成。"""

from pathlib import Path

import pytest
import yaml

from adp.design import Column, ColumnRole, DimTable, HistoryDesign, SourceTable
from adp.generation.dbt import Generation, _history, history_files


class _Info:
    def __init__(self, root: Path, columns: list[tuple[str, str]], models: dict[str, set[str]]):
        self.warehouse_dir = root
        self._columns = columns
        self._models = models

    def source_columns(self, table: str) -> list[tuple[str, str]]:
        return self._columns

    def existing_models(self) -> set[str]:
        return set(self._models)

    def existing_columns(self, model: str) -> set[str]:
        return self._models.get(model, set())


def _dim(*attrs: str) -> DimTable:
    columns = [
        Column(name=a, role=ColumnRole.ATTRIBUTE, expr=a, description=f"{a}のラベル", nullable=True)
        for a in attrs
    ]
    return DimTable(
        name="dim_customer",
        dimension="customer",
        label="顧客",
        key_column="customer_key",
        base=SourceTable(table="raw.customers", alias="t0", model="stg_customers"),
        columns=[
            Column(name="customer_id", role=ColumnRole.NATURAL_KEY, expr="t0.customer_id"),
            *[c.model_copy(update={"expr": f"t0.{c.expr}"}) for c in columns],
        ],
        history=HistoryDesign(
            model="dim_customer_history",
            snapshot="snp_customers",
            natural_key="customer_id",
            columns=columns,
        ),
    )


TYPES = [("customer_id", "INTEGER"), ("region", "VARCHAR"), ("rank", "INTEGER")]


def test_history_files(tmp_path: Path) -> None:
    files = {
        f.path: f.content
        for f in history_files(
            _dim("region", "rank"),
            _Info(tmp_path, [*TYPES, ("updated_at", "TIMESTAMP")], {}),  # type: ignore[arg-type]
            semantic=True,
        )
    }
    assert list(files) == [
        "snapshots/snp_customers.yml",
        "models/marts/dim_customer_history.sql",
        "models/marts/dim_customer_history.yml",
    ]
    snapshot = yaml.safe_load(files["snapshots/snp_customers.yml"])["snapshots"][0]
    assert snapshot["relation"] == "ref('stg_customers')"
    assert snapshot["config"] | {} == {
        "schema": "snapshots",
        "unique_key": "customer_id",
        "strategy": "timestamp",
        "updated_at": "updated_at",
    }
    sql = files["models/marts/dim_customer_history.sql"]
    assert "{{ ref('snp_customers') }}" in sql and "{{ ref('dim_date') }}" in sql
    assert "dbt_scd_id as customer_version_key" in sql
    # 不明メンバー: キーは unknown_key()、文字列の属性は「不明」、それ以外は null
    assert "select {{ unknown_key() }}, {{ unknown_key() }}, '不明', null," in sql
    model = yaml.safe_load(files["models/marts/dim_customer_history.yml"])["models"][0]
    assert model["primary_entity"] == "customer_version"
    columns = {c["name"]: c for c in model["columns"]}
    assert columns["customer_key"]["entity"] == {"type": "natural", "name": "customer"}
    assert columns["region"]["dimension"] == {"type": "categorical", "name": "region_at_event"}
    assert columns["region"]["description"] == "regionのラベル（当時）"
    assert columns["valid_from"]["dimension"]["validity_params"] == {"is_start": True}
    assert columns["valid_to"]["dimension"]["validity_params"] == {"is_end": True}
    assert "dbt_utils.mutually_exclusive_ranges" in model["data_tests"][0]


def test_snapshot_strategy_follows_the_staging_columns(tmp_path: Path) -> None:
    """stg に更新日時が無ければ、snapshot を実行した時刻で版を分ける（strategy: check）。源泉に
    あっても、クレンジングの定義で落としていれば使わず、名前を変えていれば新しい名前を使う
    （snapshot は stg を読む）。"""

    def config(
        spec: str | None, updated: bool = True, updated_type: str = "TIMESTAMP"
    ) -> dict[str, object]:
        if spec is not None:
            (tmp_path / "cleansing").mkdir(exist_ok=True)
            (tmp_path / "cleansing" / "raw.customers.yml").write_text(spec, encoding="utf-8")
        columns = [*TYPES, ("updated_at", updated_type)] if updated else TYPES
        files = history_files(_dim("region"), _Info(tmp_path, columns, {}))  # type: ignore[arg-type]
        return dict(yaml.safe_load(files[0].content)["snapshots"][0]["config"])

    assert config(None, updated=False)["strategy"] == "check"
    assert config(None)["updated_at"] == "updated_at"
    base = "source: raw.customers\ncolumns:\n- name: updated_at\n"
    assert config(base + "  drop: true\n")["strategy"] == "check"
    assert config(base + "  cast: varchar\n")["strategy"] == "check"
    assert config(base + "  rename: changed_at\n")["updated_at"] == "changed_at"
    # 文字列の更新日時を、クレンジングで日時に変えていれば使う
    assert (
        config(base + "  cast: timestamp\n", updated_type="VARCHAR")["updated_at"] == "updated_at"
    )
    # 別の列を updated_at という名前にしていれば、それを使う
    renamed = "source: raw.customers\ncolumns:\n- name: rank\n  rename: updated_at\n  cast: date\n"
    assert config(renamed, updated=False)["updated_at"] == "updated_at"
    # 候補が 2 つなら、stg で updated_at という名前の列を選ぶ（源泉の列の並び順によらない）
    both = renamed + "- name: updated_at\n  rename: src_updated_at\n"
    assert config(both)["updated_at"] == "updated_at"
    with pytest.raises(ValueError, match="クレンジングの定義 raw.customers.yml を読めません"):
        config("source: [\n")


def test_existing_history_model_gets_missing_columns_by_agent(tmp_path: Path) -> None:
    """履歴モデルが既にあれば作り直さず、足りない属性をエージェントへの作業にする。snapshot が
    無ければ snapshot だけを作り直し、同じ源泉の snapshot が既にあれば、それを使う。"""
    info = _Info(tmp_path, TYPES, {"dim_customer_history": {"region"}})
    result = Generation()
    files = _history(_dim("region", "rank"), info, info.existing_models(), True, result)  # type: ignore[arg-type]
    assert [f.path for f in files] == ["snapshots/snp_customers.yml"]
    assert result.reused == ["dim_customer_history"]
    [task] = result.tasks
    assert task.kind == "add_history_columns" and "rank" in task.instruction

    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / "snp_customers.yml").write_text("snapshots: []\n")
    assert _history(_dim("region"), info, info.existing_models(), True, Generation()) == []  # type: ignore[arg-type]
    info = _Info(tmp_path, TYPES, {})
    files = _history(_dim("region"), info, set(), True, Generation())  # type: ignore[arg-type]
    assert [f.path for f in files] == [
        "models/marts/dim_customer_history.sql",
        "models/marts/dim_customer_history.yml",
    ]


def test_versions_cover_the_calendar_and_follow_the_event_time(tmp_path: Path) -> None:
    """実際の dbt で、属性が変わると版が 2 つになり、期間は暦の始まりから終わりまでを隙間なく
    覆う。規約の検査（期間・不明メンバー・名前の重なり）も通る（ADR 0009）。"""
    import os
    import subprocess
    import sys

    import duckdb

    from adp.beam import load
    from adp.catalog.catalog import Catalog
    from adp.design import DesignBuilder
    from adp.design.catalog_schema import CatalogSchema
    from adp.evals.repair import prepare_warehouse
    from adp.generation import generate_models
    from adp.generation.conventions import check_warehouse
    from adp.generation.project import ProjectInfo

    warehouse = prepare_warehouse(tmp_path)
    db = warehouse / "data" / "dev.duckdb"
    catalog = Catalog(db)
    beam = load(Path(__file__).parents[1] / "fixtures" / "beam" / "sales_by_store.yml")
    info = ProjectInfo(warehouse, catalog)
    info.write(generate_models(DesignBuilder(CatalogSchema(catalog)).build(beam), info, beam).files)
    dbt = Path(sys.executable).parent / "dbt"
    env = os.environ | {"DBT_PROFILES_DIR": str(warehouse)}

    def build() -> None:
        for args in (["deps"], ["build"]):
            result = subprocess.run(
                [str(dbt), *args],
                cwd=warehouse,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stdout[-3000:]

    build()
    con = duckdb.connect(str(db))
    # 元のどの更新日時よりも後に変える（timestamp 戦略は、更新日時が進んだ行だけを新しい版にする）
    (changed_at,) = con.execute(
        "select max(updated_at) + interval 1 day from raw.customers"
    ).fetchone()  # type: ignore[misc]
    con.execute(
        "update raw.customers set membership_rank = 'platinum', updated_at = ?"
        " where customer_id = 1",
        [changed_at],
    )
    con.close()
    build()
    con = duckdb.connect(str(db), read_only=True)
    rows = con.execute(
        "select membership_rank, valid_from, valid_to from main.dim_customer_history"
        " where customer_key = (select customer_key from main.dim_customer where customer_id = 1)"
        " order by valid_from"
    ).fetchall()
    start, end = con.execute(
        "select cast(min(date_day) as timestamp), cast(max(date_day) + interval 1 day as timestamp)"
        " from main.dim_date"
    ).fetchone()  # type: ignore[misc]
    con.close()
    assert len(rows) == 2 and rows[1][0] == "platinum"
    assert rows[0][1] == start and rows[0][2] == rows[1][1] == changed_at and rows[1][2] == end
    assert [str(v) for v in check_warehouse(warehouse)] == []


def test_history_sql_clamps_versions_to_the_calendar(tmp_path: Path) -> None:
    """生成した SQL を DuckDB で流す。暦の始まりより前に終わる版は除き、次の版を暦の始まりまで
    広げる。暦の後に始まる版も除き、前の版を暦の終わりまで伸ばす。源泉に starts_at のような
    名前の列があっても曖昧にならない。"""
    import re

    import duckdb

    sql = history_files(
        _dim("region", "starts_at"),
        _Info(tmp_path, [*TYPES, ("starts_at", "TIMESTAMP")], {}),  # type: ignore[arg-type]
    )[1].content
    sql = re.sub(r"\{\{ ref\('(\w+)'\) \}\}", r"\1", sql)
    sql = sql.replace("{{ unknown_key() }}", "'unknown'")
    sql = re.sub(r"\{\{ dim_key\('([\w.]+)'\) \}\}", r"cast(\1 as varchar)", sql)
    con = duckdb.connect()
    con.execute(
        "create table dim_date as select cast(d as date) as date_day"
        " from range(date '2010-01-01', date '2031-01-01', interval 1 day) t(d)"
    )
    con.execute(
        "create table snp_customers (customer_id int, region varchar, starts_at timestamp,"
        " dbt_scd_id varchar, dbt_valid_from timestamp, dbt_valid_to timestamp)"
    )
    con.execute(
        "insert into snp_customers values"
        " (1, 'a', null, 's1', '2005-01-01', '2008-01-01'),"  # 暦の前に終わる版
        " (1, 'b', null, 's2', '2008-01-01', '2015-01-01'),"
        " (1, 'c', null, 's3', '2015-01-01', '2035-01-01'),"
        " (1, 'd', null, 's4', '2035-01-01', null)"  # 暦の後に始まる版
    )
    rows = con.execute(
        f"select region, valid_from, valid_to from ({sql}) where customer_key = '1'"
        " order by valid_from"
    ).fetchall()
    assert [(r[0], str(r[1].date()), str(r[2].date())) for r in rows] == [
        ("b", "2010-01-01", "2015-01-01"),
        ("c", "2015-01-01", "2031-01-01"),
    ]


def test_history_coverage_counts_keys_without_versions(tmp_path: Path) -> None:
    """実際の DuckDB で、今の値の dim にあって版の無いキーも数える。dim の表が無ければ数えない。"""
    import duckdb

    from adp.generation.conventions import data_violations

    db = tmp_path / "dev.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "create table dim_date as select cast(d as date) as date_day"
        " from range(date '2010-01-01', date '2010-01-03', interval 1 day) t(d)"
    )
    con.execute("create table dim_store (store_key varchar, store_name varchar)")
    con.execute("insert into dim_store values ('a', 'A'), ('b', 'B'), ('u', '不明')")
    con.execute(
        "create table dim_store_history (store_version_key varchar, store_key varchar,"
        " store_name varchar, valid_from timestamp, valid_to timestamp)"
    )
    con.execute(
        "insert into dim_store_history values"
        " ('v1', 'a', 'A', '2010-01-01', '2010-01-03'),"
        " ('u', 'u', '不明', '2010-01-01', '2010-01-03')"
    )

    def model(name: str, columns: dict[str, dict[str, object]]) -> dict[str, object]:
        return {
            "resource_type": "model",
            "name": name,
            "database": "dev",
            "schema": "main",
            "columns": {c: {"name": c} | spec for c, spec in columns.items()},
        }

    history = model(
        "dim_store_history",
        {
            "store_version_key": {},
            "store_key": {"entity": {"type": "natural", "name": "store"}},
            "store_name": {},
            "valid_from": {},
            "valid_to": {},
        },
    )
    dim = model(
        "dim_store",
        {"store_key": {"entity": {"type": "primary", "name": "store"}}, "store_name": {}},
    )
    nodes = {
        f"model.adp.{n['name']}": n for n in (model("dim_date", {"date_day": {}}), dim, history)
    }

    def query(sql: str) -> list[tuple[object, ...]]:
        return con.execute(sql).fetchall()

    messages = [v.message for v in data_violations({"nodes": nodes}, query)]
    # 'b' は dim にあるが版が無い（不明メンバーの値 'u' は不明メンバーのキーではないので別の違反）
    assert any("覆っていないキーが 1 件" in m for m in messages), messages
    con.execute("drop table dim_store")
    messages = [v.message for v in data_violations({"nodes": nodes}, query)]
    assert not any("覆っていない" in m for m in messages), messages
