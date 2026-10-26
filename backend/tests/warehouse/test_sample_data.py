import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import duckdb
import pytest

SCRIPT = Path(__file__).parents[3] / "warehouse" / "scripts" / "generate_sample_data.py"


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_sample_data", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_deterministic(gen: ModuleType) -> None:
    assert gen.generate(200, 7) == gen.generate(200, 7)
    assert gen.generate(200, 7) != gen.generate(200, 8)


def test_load_covers_7w(gen: ModuleType, tmp_path: Path) -> None:
    db = tmp_path / "t.duckdb"
    gen.load(db, gen.generate(500, 1))
    con = duckdb.connect(str(db), read_only=True)

    def one(sql: str) -> object:
        row = con.sql(sql).fetchone()
        assert row is not None
        return row[0]

    assert one("select count(*) from raw.orders") == 500
    # Why: プロモーション適用あり / なしの両方がある
    assert one("select count(distinct promotion_id is null) from raw.orders") == 2
    # Where: 3 チャネルすべてに注文がある
    assert (
        one("select count(distinct s.channel) from raw.orders o join raw.stores s using (store_id)")
        == 3
    )
    # How: 店舗以外では現金払いがない
    assert (
        one(
            "select count(*) from raw.orders o join raw.stores s using (store_id)"
            " where s.channel <> 'store' and o.payment_method = 'cash'"
        )
        == 0
    )
    # 参照整合性: 明細の注文と商品は必ず存在する
    assert (
        one(
            "select count(*) from raw.order_items i left join raw.orders o using (order_id)"
            " left join raw.products p using (product_id)"
            " where o.order_id is null or p.product_id is null"
        )
        == 0
    )


def test_masters_do_not_depend_on_scale(gen: ModuleType) -> None:
    small, large = gen.generate(300, 42), gen.generate(3000, 42)
    assert small.products == large.products
    assert small.stores == large.stores
    assert small.promotions == large.promotions
    assert small.customers == large.customers[: len(small.customers)]


@pytest.fixture(scope="module")
def con(gen: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> duckdb.DuckDBPyConnection:
    db = tmp_path_factory.mktemp("db") / "t.duckdb"
    gen.load(db, gen.generate(3000, 42))
    return duckdb.connect(str(db), read_only=True)


def _one(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    row = con.sql(sql).fetchone()
    assert row is not None
    return row[0]


def test_no_sales_before_launch(con: duckdb.DuckDBPyConnection) -> None:
    assert (
        _one(
            con,
            "select count(*) from raw.order_items i join raw.orders o using (order_id)"
            " join raw.products p using (product_id)"
            " where cast(o.ordered_at as date) < p.launched_on",
        )
        == 0
    )


def test_returned_status_matches_returns(con: duckdb.DuckDBPyConnection) -> None:
    # returned の注文には必ず返品明細があり、返品明細があれば注文は returned
    assert (
        _one(
            con,
            "select count(*) from raw.orders o where (o.order_status = 'returned') <> exists("
            " select 1 from raw.returns r join raw.order_items i using (order_item_id)"
            " where i.order_id = o.order_id)",
        )
        == 0
    )
    # 返品は受け取り（配達）後
    assert (
        _one(
            con,
            "select count(*) from raw.returns r join raw.order_items i using (order_item_id)"
            " join raw.orders o using (order_id) left join raw.shipments s using (order_id)"
            " where r.returned_at <= coalesce(s.delivered_at, o.ordered_at)",
        )
        == 0
    )


def test_return_rate_differs_by_category(con: duckdb.DuckDBPyConnection) -> None:
    rows = dict(
        con.sql(
            "select p.category, count(r.return_id) / count(*) from raw.order_items i"
            " join raw.products p using (product_id)"
            " left join raw.returns r using (order_item_id) group by 1"
        ).fetchall()
    )
    assert rows["衣料"] > 3 * rows["食品"]


def test_load_drops_history_snapshots(gen: ModuleType, tmp_path: Path) -> None:
    """源泉を作り直すとき、属性の履歴の snapshot（dbt の schema「*_snapshots」）を消す（ADR 0009）。
    古い版が残ると、作り直した源泉と食い違う。"""
    db = tmp_path / "x.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema main_snapshots")
    con.execute("create table main_snapshots.snp_customers (customer_id int)")
    con.close()
    gen.load(db, gen.generate(50, 1))
    con = duckdb.connect(str(db))
    schemas = {
        s for (s,) in con.execute("select schema_name from information_schema.schemata").fetchall()
    }
    con.close()
    assert "main_snapshots" not in schemas and "raw" in schemas


def test_order_promotions(gen: ModuleType) -> None:
    """注文に適用されたプロモーション（多対多の切り口の例。ADR 0010）の不変条件。"""
    tables = gen.generate(3000, 42)
    promotions = {p[0]: p for p in tables.promotions}
    by_order: dict[object, list[object]] = {}
    for _, order_id, promotion_id in tables.order_promotions:
        by_order.setdefault(order_id, []).append(promotion_id)
    orders = {o[0]: o for o in tables.orders}
    # プロモーションの付いた注文ごとに行があり、1 件目は orders.promotion_id と同じ
    assert set(by_order) == {o[0] for o in tables.orders if o[4] is not None}
    assert all(orders[o][4] == ps[0] for o, ps in by_order.items())
    assert any(len(ps) == 2 for ps in by_order.values())
    for order_id, ps in by_order.items():
        assert len(ps) <= 2 and len(set(ps)) == len(ps)
        d = orders[order_id][3].date()
        assert all(promotions[p][4] <= d <= promotions[p][5] for p in ps)
        # 値引額は 1 件目の割引率だけで決まるので、2 つ目は値引きの無いポイント付与
        assert all(promotions[p][2] == "point_up" for p in ps[1:])
