"""生成した dbt モデルを、リポジトリの warehouse を複製した一時プロジェクトで実際に build する。"""

import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

from adp.beam import load
from adp.catalog.catalog import Catalog
from adp.design import ColumnRole, DesignBuilder
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
    # yml の説明はオントロジーのラベルと照合されるので、書く説明を指示に入れる（#137）
    task = next(t for t in generation.tasks if t.kind == "add_dim_columns")
    assert "説明「開店日」" in task.instruction


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


def test_semi_additive_metric_sums_only_the_latest_date(tmp_path: Path) -> None:
    """semi_additive の指標は、MetricFlow で区間ごとに最新の日付の値だけを足す（#134）。

    定期スナップショットの fct は #138 まで生成できないため、注文の fct で集計の仕組みを確かめる。
    """
    from adp.beam import Additivity, MetricRequest

    warehouse = _warehouse_copy(tmp_path)
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    event = beam.events[0]
    quantity = event.measure("quantity")
    assert quantity is not None
    quantity.additivity = Additivity.SEMI_ADDITIVE
    quantity.non_additive_dimension = "date"
    beam.metrics.append(
        MetricRequest(
            name="latest_quantity",
            label="最新日の数量",
            question="q",
            framed_question="q",
            event=event.name,
            measure="quantity",
        )
    )
    design = DesignBuilder(CatalogSchema(catalog)).build(beam)
    info.write(generate_models(design, info, beam).files)
    assert _dbt(warehouse, "build").returncode == 0

    def query(*group_by: str) -> list[tuple[str, ...]]:
        csv = tmp_path / "q.csv"
        args = ["query", "--metrics", "latest_quantity", "--csv", str(csv)]
        if group_by:
            args += ["--group-by", ",".join(group_by), "--order", ",".join(group_by)]
        out = subprocess.run(
            [str(DBT.parent / "mf"), *args],
            cwd=warehouse,
            capture_output=True,
            text=True,
            env=os.environ | {"DBT_PROFILES_DIR": str(warehouse)},
            check=False,
        )
        assert out.returncode == 0, out.stdout[-2000:]
        return [tuple(line.split(",")) for line in csv.read_text().splitlines()[1:]]

    total, monthly = query(), query("metric_time__month")
    fact = design.facts[0]
    t, d = fact.name, fact.agg_time_column
    con = duckdb.connect(str(warehouse / "data" / "dev.duckdb"), read_only=True)
    latest = con.sql(
        f"select sum(quantity) from main.{t} where {d} = (select max({d}) from main.{t})"
    ).fetchone()
    assert latest is not None and float(total[0][0]) == float(latest[0])
    by_month = con.sql(
        f"select sum(quantity) from main.{t} where {d} in"
        f" (select max({d}) from main.{t} group by date_trunc('month', {d}))"
        f" group by date_trunc('month', {d}) order by date_trunc('month', {d})"
    ).fetchall()
    assert len(by_month) > 1, "月が 1 つだけだと区間ごとの最新を確かめられない"
    assert [float(r[-1]) for r in monthly] == [float(r[0]) for r in by_month]


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


def test_periodic_snapshot_builds_and_matches_reference(tmp_path: Path) -> None:
    """定期的な状態（月末在庫）の fct を生成し、手で書いたリファレンスと一致する（ADR 0008）。

    在庫の記録は増減（入庫・移動出庫・廃棄）と棚卸（数えた数）が混ざる。増減を積み上げ、棚卸で
    数えた数に置き換え、記録の無い月は前の月の値を引き継ぐ。
    """
    warehouse = _warehouse_copy(tmp_path)
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    beam = load(FIXTURES / "beam" / "stock_levels.yml")
    design = DesignBuilder(CatalogSchema(catalog)).build(beam)
    assert design.blockers == []
    generation = generate_models(design, info, beam)
    assert [f.path for f in generation.files] == [
        "models/marts/fct_stock_levels.sql",
        "models/marts/fct_stock_levels.yml",
        "tests/assert_fct_stock_levels_reconciles_with_source.sql",
    ]
    info.write(generation.files)
    ref_sql = (FIXTURES / "generation/reference/fct_stock_levels.sql").read_text(encoding="utf-8")
    (warehouse / "models/marts/ref_fct_stock_levels.sql").write_text(ref_sql, encoding="utf-8")

    result = _dbt(warehouse, "build")
    assert result.returncode == 0, result.stdout[-3000:]
    # 生成したテスト（3 か月の履歴の unit test・期間の途切れと棚卸の値の突合）が実行されている
    assert "test_fct_stock_levels_example_1" in result.stdout
    assert "assert_fct_stock_levels_reconciles_with_source" in result.stdout

    con = duckdb.connect(str(warehouse / "data" / "dev.duckdb"), read_only=True)
    columns = "store_key, product_key, snapshot_on, stock_qty"
    assert con.sql("select count(*) from main.fct_stock_levels").fetchone() != (0,)
    for a, b in (
        ("fct_stock_levels", "ref_fct_stock_levels"),
        ("ref_fct_stock_levels", "fct_stock_levels"),
    ):
        diff = con.sql(f"select {columns} from main.{a} except select {columns} from main.{b}")
        assert diff.fetchall() == [], f"{a} にだけある行があります"
    con.close()

    # 月末在庫を月別に取れる（MetricFlow は区切りごとに期間の最後の日の値だけを足す）
    [q] = generation.queries
    assert q.group_by == ["metric_time__month", "store__store_name", "product__category"]
    query = mf_query(warehouse, q)
    assert query.returncode == 0, query.stdout[-3000:] + query.stderr[-2000:]
    assert conventions_violations(warehouse) == []


def test_factless_fact_counts_events(tmp_path: Path) -> None:
    """出来事の件数だけの要求（measure に列が無い）から、中身のない出来事の fact を作る（#139）。

    fct は measure の列を持たず、指標は行を数える（`agg: count`、`expr: 1`）。件数は源泉と一致する。
    """
    warehouse = _warehouse_copy(tmp_path)
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    beam = load(FIXTURES / "beam" / "shipment_counts.yml")
    assert beam.finalize_issues() == []
    design = DesignBuilder(CatalogSchema(catalog)).build(beam)
    assert design.blockers == []
    [fact] = design.facts
    assert not any(c.role == ColumnRole.MEASURE for c in fact.columns)
    generation = generate_models(design, info, beam)
    info.write(generation.files)
    yml = next(f for f in generation.files if f.path == "models/marts/fct_shipments.yml")
    [metric] = yaml.safe_load(yml.content)["models"][0]["metrics"]
    assert (metric["agg"], metric["expr"]) == ("count", "1")

    result = _dbt(warehouse, "build")
    assert result.returncode == 0, result.stdout[-3000:]
    assert "test_fct_shipments_example_1" in result.stdout
    assert conventions_violations(warehouse) == []

    query = mf_query(warehouse, MetricQuery("shipment_count", ["shipment__shipping_method"]))
    assert query.returncode == 0, query.stdout[-2000:] + query.stderr[-1000:]
    got = {
        parts[0]: int(parts[1])
        for line in query.stdout.splitlines()
        if len(parts := line.split()) == 2 and parts[1].isdigit()
    }
    con = duckdb.connect(str(warehouse / "data" / "dev.duckdb"), read_only=True)
    expected = dict(
        con.sql("select shipping_method, count(*) from raw.shipments group by 1").fetchall()
    )
    assert got == expected


def test_accumulating_snapshot_stage_lags(tmp_path: Path) -> None:
    """段階を経て進む出来事（注文 → 出荷 → 配達）の段階の日時と、段階の間の日数（ラグ）を
    生成する（#140）。ラグは段階の日時の列から計算し、源泉から独立に求めた値と一致する。"""
    from datetime import datetime

    warehouse = _warehouse_copy(tmp_path)
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    beam = load(FIXTURES / "beam" / "order_fulfillment_stages.yml")
    assert beam.finalize_issues() == []
    design = DesignBuilder(CatalogSchema(catalog)).build(beam)
    assert design.blockers == []
    [fact] = design.facts
    lags = {c.name: c.expr for c in fact.columns if c.name.endswith("_days")}
    assert lags == {
        "order_to_ship_days": "date_diff('day', cast(t1.ordered_at as timestamp),"
        " cast(t0.shipped_at as timestamp))",
        "ship_to_delivery_days": "date_diff('day', cast(t0.shipped_at as timestamp),"
        " cast(t0.delivered_at as timestamp))",
    }
    # 後の段階の日時は、その段階に達するまで NULL でよい
    assert {c.name: c.nullable for c in fact.columns if c.name.endswith("_on")} == {
        "ordered_on": False,
        "shipped_on": True,
        "delivered_on": True,
    }
    generation = generate_models(design, info, beam)
    info.write(generation.files)
    result = _dbt(warehouse, "build")
    assert result.returncode == 0, result.stdout[-3000:]
    assert "test_fct_fulfillments_example_1" in result.stdout
    assert conventions_violations(warehouse) == []
    for q in generation.queries:
        query = mf_query(warehouse, q)
        assert query.returncode == 0, query.stdout[-2000:] + query.stderr[-1000:]

    con = duckdb.connect(str(warehouse / "data" / "dev.duckdb"), read_only=True)
    raw = con.sql(
        "select s.shipment_id, o.ordered_at, s.shipped_at, s.delivered_at"
        " from raw.shipments s left join raw.orders o using (order_id)"
    ).fetchall()

    def days(a: datetime | None, b: datetime | None) -> int | None:
        return None if a is None or b is None else (b.date() - a.date()).days

    expected = {r[0]: (days(r[1], r[2]), days(r[2], r[3])) for r in raw}
    got = {
        r[0]: (r[1], r[2])
        for r in con.sql(
            "select shipment_id, order_to_ship_days, ship_to_delivery_days"
            " from main.fct_fulfillments"
        ).fetchall()
    }
    assert got == expected


def test_stage_lag_in_hours(tmp_path: Path) -> None:
    """時間の単位のラグは、時の境目の数（DuckDB の date_diff('hour')。#140）。"""
    from datetime import datetime

    from adp.beam import LagUnit

    warehouse = _warehouse_copy(tmp_path)
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    beam = load(FIXTURES / "beam" / "order_fulfillment_stages.yml")
    measure = beam.events[0].measure("ship_to_delivery_days")
    assert measure is not None and measure.lag is not None
    measure.lag.unit = LagUnit.HOUR
    beam.events[0].examples = []
    design = DesignBuilder(CatalogSchema(catalog)).build(beam)
    info.write(generate_models(design, info, beam).files)
    assert _dbt(warehouse, "build").returncode == 0
    con = duckdb.connect(str(warehouse / "data" / "dev.duckdb"), read_only=True)

    def hours(a: datetime, b: datetime) -> int:
        floor = [t.replace(minute=0, second=0, microsecond=0) for t in (a, b)]
        return int((floor[1] - floor[0]).total_seconds() // 3600)

    expected = {
        r[0]: hours(r[1], r[2])
        for r in con.sql(
            "select shipment_id, shipped_at, delivered_at from raw.shipments"
        ).fetchall()
    }
    got = dict(
        con.sql("select shipment_id, ship_to_delivery_days from main.fct_fulfillments").fetchall()
    )
    assert got == expected


def test_lag_unit_test_is_skipped_when_times_cannot_be_read() -> None:
    """段階の日時の型が分からない（入力行の既定値が日時でない）ときは、ラグの期待値を独立に
    計算できないので unit test を作らない（生成は止めない。#140）。"""
    from adp.beam import load as load_beam
    from adp.design import ForeignKey
    from adp.generation.tests import unit_test

    class Schema:
        def columns(self, table: str) -> list[str]:
            return {
                "raw.shipments": [
                    "shipment_id",
                    "order_id",
                    "shipping_method",
                    "shipped_at",
                    "delivered_at",
                ],
                "raw.orders": ["order_id", "ordered_at"],
            }.get(table, [])

        def foreign_keys(self, table: str) -> list[ForeignKey]:
            fk = ForeignKey("order_id", "raw.orders", "order_id")
            return [fk] if table == "raw.shipments" else []

    beam = load_beam(FIXTURES / "beam" / "order_fulfillment_stages.yml")
    design = DesignBuilder(Schema()).build(beam)
    assert design.blockers == []

    class Info:
        def source_columns(self, table: str) -> list[tuple[str, str]]:
            return []  # 型が分からない → 既定値 "x"

        def source_primary_key(self, table: str) -> str | None:
            return None

    example = beam.events[0].examples[0].model_copy(update={"values": {}})
    assert unit_test(beam, design.facts[0], Info(), example, 0) is None  # type: ignore[arg-type]


def test_missing_example_expects_unknown_member() -> None:
    """欠損の具体例（マスタのある属性がすべて空）は、外部キーの自然キーを空にし、不明メンバーの
    キーを期待値にする。結合キーは空にしない（結合が外れるため。#142）。"""
    from adp.beam import ExampleKind
    from adp.design import FactTable
    from adp.generation.tests import UNKNOWN, surrogate_key, unit_test
    from tests.design.test_builder import FakeSchema

    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    event = beam.events[0]
    example = event.examples[0].model_copy(deep=True)
    example.kind, example.note = ExampleKind.MISSING, "プロモーションなし"
    for k in [k for k in example.values if k.startswith("promotion.")]:
        example.values[k] = None
    example.values["promotion.promotion_name"] = None
    example.values["order_id"] = None  # 結合キー（明細 → 注文）
    design = DesignBuilder(FakeSchema()).build(beam)
    fact: FactTable = design.facts[0]

    class Info:
        def source_columns(self, table: str) -> list[tuple[str, str]]:
            return [(c, "INTEGER") for c in FakeSchema.COLUMNS.get(table, [])]

        def source_primary_key(self, table: str) -> str | None:
            return None

    ut = unit_test(beam, fact, Info(), example, 0)  # type: ignore[arg-type]
    assert ut is not None
    row = ut["expect"]["rows"][0]
    assert row["promotion_key"] == surrogate_key(None) == surrogate_key(UNKNOWN)
    base, orders = ut["given"][0]["rows"][0], ut["given"][1]["rows"][0]
    assert base["order_id"] is not None and base["order_id"] == orders["order_id"]
