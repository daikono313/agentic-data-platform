"""生成した dbt モデルを、リポジトリの warehouse を複製した一時プロジェクトで実際に build する。"""

import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from adp.beam import load
from adp.catalog.catalog import Catalog
from adp.design import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.generation import generate_models
from adp.generation.conventions import check_warehouse
from adp.generation.project import ProjectInfo
from adp.generation.semantic import MetricQuery

ROOT = Path(__file__).parents[3]
FIXTURES = Path(__file__).parents[1] / "fixtures"
DBT = Path(sys.executable).parent / "dbt"


def _warehouse_copy(tmp: Path) -> Path:
    """生成前の warehouse と小さなサンプルデータ（eval と同じ作り方。#104）。"""
    from adp.evals.repair import prepare_warehouse

    return prepare_warehouse(tmp)


def _dbt(warehouse: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"DBT_PROFILES_DIR": str(warehouse)}
    if args[0] == "build" and not (warehouse / "dbt_packages" / "dbt_utils").exists():
        deps = subprocess.run(
            [str(DBT), "deps"], cwd=warehouse, env=env, capture_output=True, text=True, check=False
        )
        assert deps.returncode == 0, deps.stdout[-2000:]
    return subprocess.run(
        [str(DBT), *args], cwd=warehouse, env=env, capture_output=True, text=True, check=False
    )


def mf_query(warehouse: Path, q: MetricQuery, limit: int = 5) -> subprocess.CompletedProcess[str]:
    """生成した指標を元の要求どおりにクエリする（dbt parse 済みであること）。"""
    args = [str(DBT.parent / "mf"), "query", "--metrics", q.metric, "--limit", str(limit)]
    if q.group_by:
        args += ["--group-by", ",".join(q.group_by)]
    env = os.environ | {"DBT_PROFILES_DIR": str(warehouse)}
    return subprocess.run(args, cwd=warehouse, env=env, capture_output=True, text=True, check=False)


def conventions_violations(warehouse: Path) -> list[str]:
    """build 済みの warehouse のモデリング規約の違反（adp.generation.conventions）。"""
    return [str(v) for v in check_warehouse(warehouse)]


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _warehouse_copy(tmp_path_factory.mktemp("gen"))


def test_generated_models_build_and_match_reference(warehouse: Path) -> None:
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    design = DesignBuilder(CatalogSchema(catalog)).build(beam)
    assert design.blockers == []
    generation = generate_models(design, info, beam)

    # 共通ディメンションと staging は既存のものを再利用し、fct だけを生成する
    assert [f.path for f in generation.files] == [
        "models/marts/fct_order_lines.sql",
        "models/marts/fct_order_lines.yml",
        "tests/assert_fct_order_lines_reconciles_with_source.sql",
    ]
    assert {"dim_customer", "dim_product", "dim_store", "dim_promotion", "stg_orders"} <= set(
        generation.reused
    )
    assert generation.tasks == []
    info.write(generation.files)
    with pytest.raises(FileExistsError):
        info.write(generation.files)

    # 手書きのリファレンスも同じプロジェクトに入れて比較する
    ref_sql = (FIXTURES / "generation/reference/fct_order_lines.sql").read_text(encoding="utf-8")
    (warehouse / "models/marts/ref_fct_order_lines.sql").write_text(ref_sql, encoding="utf-8")

    result = _dbt(warehouse, "build")
    assert result.returncode == 0, result.stdout[-3000:]

    # 元の要求（日別・店舗別の売上、キャンセル除外）を Semantic Layer から取得できる
    assert [(q.metric, q.group_by) for q in generation.queries] == [
        ("revenue", ["metric_time__day", "store__store_name", "store__channel"])
    ]
    mf = DBT.parent / "mf"
    validate = subprocess.run(
        [str(mf), "validate-configs"],
        cwd=warehouse,
        capture_output=True,
        text=True,
        env=os.environ | {"DBT_PROFILES_DIR": str(warehouse)},
        check=False,
    )
    assert validate.returncode == 0, validate.stdout[-3000:]
    query = mf_query(warehouse, generation.queries[0])
    assert query.returncode == 0, query.stdout[-3000:] + query.stderr[-2000:]
    assert "store__store_name" in query.stdout

    # 生成したテスト（unit test・突合・区分値）が build の中で実行されている
    assert "test_fct_order_lines_example_1" in result.stdout
    assert "assert_fct_order_lines_reconciles_with_source" in result.stdout
    assert "accepted_values_fct_order_lines_payment_method" in result.stdout

    # 生成した fct も手で書いた共通の dim も、モデリング規約の検査を通る（#134）
    assert conventions_violations(warehouse) == []
    con = duckdb.connect(str(warehouse / "data" / "dev.duckdb"), read_only=True)
    columns = (
        "order_line_id, order_id, customer_key, product_key, store_key, promotion_key,"
        " ordered_on, ordered_at, payment_method, quantity, unit_price, sales_amount"
    )
    for a, b in (
        ("fct_order_lines", "ref_fct_order_lines"),
        ("ref_fct_order_lines", "fct_order_lines"),
    ):
        diff = con.sql(f"select {columns} from main.{a} except select {columns} from main.{b}")
        assert diff.fetchall() == [], f"{a} にだけある行があります"


def test_new_dimension_and_staging_are_generated(tmp_path: Path) -> None:
    """既存モデルが無い場合は staging と dim も生成し、build が通る。"""
    warehouse = _warehouse_copy(tmp_path)
    for name in ("dim_store", "stg_stores"):
        for p in [
            *(warehouse / "models").rglob(f"{name}.sql"),
            *(warehouse / "models").rglob(f"{name}.yml"),
        ]:
            p.unlink()
    # クレンジングの定義も無い状態から（既定の定義を一緒に生成する。#68）
    (warehouse / "cleansing" / "raw.stores.yml").unlink()
    for yml in (warehouse / "models").rglob("_*.yml"):
        text = yml.read_text(encoding="utf-8")
        for name in ("dim_store", "stg_stores"):
            text = _drop_model_entry(text, name)
        yml.write_text(text, encoding="utf-8")

    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    design = DesignBuilder(CatalogSchema(catalog)).build(
        load(FIXTURES / "beam" / "sales_by_store.yml")
    )
    generation = generate_models(design, info)
    paths = [f.path for f in generation.files]
    assert "models/staging/stg_stores.sql" in paths and "models/marts/dim_store.sql" in paths
    assert "cleansing/raw.stores.yml" in paths
    stg = next(f.content for f in generation.files if f.path.endswith("stg_stores.sql"))
    assert stg.startswith("-- adp.cleansing により生成") and "nullif(trim(store_name), '')" in stg
    dim_sql = next(f.content for f in generation.files if f.path.endswith("dim_store.sql"))
    assert "'不明'" in dim_sql and "{{ unknown_key() }}" in dim_sql
    info.write(generation.files)
    result = _dbt(warehouse, "build")
    assert result.returncode == 0, result.stdout[-3000:]
    assert conventions_violations(warehouse) == []


def _drop_model_entry(text: str, name: str) -> str:
    import yaml

    data = yaml.safe_load(text)
    for key in ("models",):
        if key in data:
            data[key] = [m for m in data[key] if m.get("name") != name]
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def test_existing_dim_missing_columns_become_tasks(warehouse: Path) -> None:
    from adp.beam import Attribute, Availability, SourceRef

    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    store = beam.events[0].dimension("store")
    assert store is not None
    store.attributes.append(
        Attribute(
            name="opened_on",
            label="開店日",
            source=SourceRef(table="raw.stores", column="opened_on"),
            availability=Availability.AVAILABLE,
        )
    )
    design = DesignBuilder(CatalogSchema(catalog)).build(beam)
    generation = generate_models(design, ProjectInfo(warehouse, catalog))
    tasks = [(t.kind, t.target) for t in generation.tasks]
    assert ("add_dim_columns", "dim_store") in tasks


def test_blockers_stop_generation(warehouse: Path) -> None:
    from adp.design import DesignIssue, IssueSeverity

    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    design = DesignBuilder(CatalogSchema(catalog)).build(
        load(FIXTURES / "beam" / "sales_by_store.yml")
    )
    design.issues.append(DesignIssue(severity=IssueSeverity.BLOCKER, message="x"))
    with pytest.raises(ValueError, match="確認が必要"):
        generate_models(design, ProjectInfo(warehouse, catalog))


def test_write_is_all_or_nothing(tmp_path: Path) -> None:
    from adp.generation import GeneratedFile

    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "b.sql").write_text("old")
    info = ProjectInfo(tmp_path, Catalog(tmp_path / "x.duckdb"))
    with pytest.raises(FileExistsError):
        info.write([GeneratedFile("models/a.sql", "a"), GeneratedFile("models/b.sql", "b")])
    assert not (tmp_path / "models" / "a.sql").exists()


def test_edge_cases_compile(tmp_path: Path) -> None:
    """式の自然キー、FK が粒度の fct、未宣言のソースでも dbt が通る。"""
    import duckdb as ddb

    from adp.beam import FactType
    from adp.design import (
        Column,
        ColumnRole,
        DimTable,
        FactTable,
        PhysicalDesign,
        SourceTable,
    )

    warehouse = _warehouse_copy(tmp_path)
    con = ddb.connect(str(warehouse / "data" / "dev.duckdb"))
    con.execute(
        "create table raw.coupons as select store_id, channel, 'C' || store_id as code"
        " from raw.stores"
    )
    con.close()
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    base = SourceTable(table="raw.coupons", model="stg_coupons", alias="t0")
    dim = DimTable(
        name="dim_store_channel",
        dimension="store_channel",
        label="店舗チャネル",
        key_column="store_channel_key",
        base=base,
        columns=[
            Column(
                name="store_channel",
                role=ColumnRole.NATURAL_KEY,
                expr="t0.store_id || '-' || t0.channel",
            ),
            Column(name="channel", role=ColumnRole.ATTRIBUTE, expr="t0.channel"),
        ],
    )
    fact = FactTable(
        name="fct_coupon_stores",
        event="e",
        label="クーポン",
        fact_type=FactType.TRANSACTION,
        grain="店舗チャネル",
        grain_columns=["store_channel_key"],
        base=base,
        columns=[
            Column(
                name="store_channel_key",
                role=ColumnRole.FOREIGN_KEY,
                expr="{{ dim_key(\"t0.store_id || '-' || t0.channel\") }}",
                references="dim_store_channel.store_channel_key",
            ),
            Column(name="code", role=ColumnRole.DEGENERATE, expr="t0.code"),
        ],
        primary_entity="coupon_store",
    )
    design = PhysicalDesign(beam="x", facts=[fact], dims=[dim], uses_dim_date=False)
    info = ProjectInfo(warehouse, catalog)
    generation = generate_models(design, info)
    assert "models/staging/_adp_coupons__sources.yml" in [f.path for f in generation.files]
    info.write(generation.files)
    result = _dbt(warehouse, "build", "--select", "+fct_coupon_stores", "+dim_store_channel")
    assert result.returncode == 0, result.stdout[-3000:]


def test_generated_revenue_matches_reference_metric(tmp_path: Path) -> None:
    """生成した revenue と、元データから直接集計した値（キャンセル除外の合計）が一致する。"""
    warehouse = _warehouse_copy(tmp_path)
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    design = DesignBuilder(CatalogSchema(catalog)).build(beam)
    info.write(generate_models(design, info, beam).files)
    assert _dbt(warehouse, "build").returncode == 0
    mf = DBT.parent / "mf"
    out = subprocess.run(
        [str(mf), "query", "--metrics", "revenue", "--csv", str(tmp_path / "r.csv")],
        cwd=warehouse,
        capture_output=True,
        text=True,
        env=os.environ | {"DBT_PROFILES_DIR": str(warehouse)},
        check=False,
    )
    assert out.returncode == 0, out.stdout[-2000:]
    total = float((tmp_path / "r.csv").read_text().splitlines()[1])
    con = duckdb.connect(str(warehouse / "data" / "dev.duckdb"), read_only=True)
    expected = con.sql(
        "select sum(i.quantity * i.unit_price - i.discount_amount) from raw.order_items i"
        " join raw.orders o using (order_id) where o.order_status <> 'cancelled'"
    ).fetchone()
    assert expected is not None and total == float(expected[0])


def test_generated_tests_catch_broken_sql(tmp_path: Path) -> None:
    """生成 SQL の計算や結合を壊すと、生成したテストが失敗する（変異テスト）。"""
    warehouse = _warehouse_copy(tmp_path)
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    design = DesignBuilder(CatalogSchema(catalog)).build(beam)
    info.write(generate_models(design, info, beam).files)
    assert _dbt(warehouse, "build").returncode == 0
    sql_path = warehouse / "models/marts/fct_order_lines.sql"
    original = sql_path.read_text(encoding="utf-8")

    sql_path.write_text(original.replace("- t0.discount_amount", "+ t0.discount_amount"))
    broken = _dbt(warehouse, "test", "--select", "test_type:unit")
    assert broken.returncode != 0, "計算式の誤りを unit test が検出できていない"

    sql_path.write_text(original.replace("{{ dim_key('t1.promotion_id') }}", "t1.promotion_id"))
    broken = _dbt(warehouse, "test", "--select", "test_type:unit")
    assert broken.returncode != 0, "キーの誤りを unit test が検出できていない"
