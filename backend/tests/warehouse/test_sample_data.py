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
