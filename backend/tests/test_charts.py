"""dbt Charts × Semantic Layer（#41）。

指標と切り口だけで書いたチャートが MetricFlow の SQL で描画され、値が `mf query` と一致すること。
手書き・古いボードを検出すること。"""

import csv
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from adp.charts import ChartError, MetricQuery, check, compile_board, write_all
from tests.generation.test_dbt_codegen import DBT
from tests.test_dbt_charts import DCT, _dct
from tests.test_dbt_charts import warehouse as _base_warehouse  # noqa: F401  （fixture）

MF = DBT.parent / "mf"

SPEC = {
    "title": "店舗別の売上",
    "metric_queries": {
        "by_store": {
            "metrics": ["revenue"],
            "group_by": ["store__store_name"],
            "order": ["-revenue"],
            "limit": 5,
        }
    },
    "charts": {
        "stores": {"type": "bar", "query": "by_store", "x": "store__store_name", "y": "revenue"}
    },
    "rows": ["stores"],
}


@pytest.fixture(scope="module")
def warehouse(_base_warehouse: Path) -> Path:  # noqa: F811
    (_base_warehouse / "charts" / "store_sales.yml").unlink(
        missing_ok=True
    )  # PoC の手書きボードを除く
    (_base_warehouse / "charts" / "metrics").mkdir(exist_ok=True)
    (_base_warehouse / "charts" / "metrics" / "store_sales.yml").write_text(
        yaml.safe_dump(SPEC, allow_unicode=True), encoding="utf-8"
    )
    subprocess.run(
        [str(DBT), "parse"],
        cwd=_base_warehouse,
        env=os.environ | {"DBT_PROFILES_DIR": str(_base_warehouse)},
        check=True,
        capture_output=True,
    )
    write_all(_base_warehouse, MF)
    return _base_warehouse


def test_compiled_board_has_only_metricflow_sql(warehouse: Path) -> None:
    text = (warehouse / "charts" / "store_sales.yml").read_text(encoding="utf-8")
    assert text.startswith("# adp により生成")
    board = yaml.safe_load(text)
    sql = board["queries"]["by_store"]["sql"]
    assert '"dev"' not in sql  # DB ファイル名に依存しない
    assert "fct_order_lines" in sql and "LIMIT 5" in sql
    assert "metric_queries" not in board
    assert check(warehouse, MF) == []


def test_chart_values_match_mf_query(warehouse: Path, tmp_path: Path) -> None:
    out = tmp_path / "mf.csv"
    q = MetricQuery.parse("x", SPEC["metric_queries"]["by_store"])  # type: ignore[index]
    subprocess.run(
        [str(MF), "query", *q.args(), "--csv", str(out)],
        cwd=warehouse,
        env=os.environ | {"DBT_PROFILES_DIR": str(warehouse)},
        check=True,
        capture_output=True,
    )
    expected = [
        (r["store__store_name"], round(float(r["revenue"]))) for r in csv.DictReader(out.open())
    ]
    result = _dct(warehouse, "query", "charts/store_sales.yml", "by_store", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    import json

    payload = json.loads(result.stdout)
    rows = payload["data"]
    actual = [(r["store__store_name"], round(float(r["revenue"]))) for r in rows]
    assert actual == expected and len(actual) == 5
    assert _dct(warehouse, "render", "charts/store_sales.yml", "--format", "text").returncode == 0


def test_check_detects_hand_written_and_stale(warehouse: Path) -> None:
    hand = warehouse / "charts" / "hand.yml"
    hand.write_text("title: x\nsource: warehouse\nqueries:\n  q:\n    sql: select 1\n")
    board = warehouse / "charts" / "store_sales.yml"
    original = board.read_text(encoding="utf-8")
    board.write_text(original.replace("LIMIT 5", "LIMIT 6"), encoding="utf-8")
    try:
        problems = check(warehouse, MF)
        assert any("hand.yml: 手書き" in p for p in problems)
        assert any("store_sales.yml: 古く" in p for p in problems)
    finally:
        hand.unlink()
        board.write_text(original, encoding="utf-8")


def test_spec_validation(warehouse: Path) -> None:
    with pytest.raises(ChartError, match="SQL"):
        compile_board("x", {"queries": {"q": {"sql": "select 1"}}}, warehouse, MF)
    with pytest.raises(ChartError, match="使えないキー"):
        compile_board(
            "x", {"metric_queries": {"q": {"metrics": ["revenue"], "sql": "x"}}}, warehouse, MF
        )
    with pytest.raises(ChartError, match="作れません"):
        compile_board(
            "x", {"metric_queries": {"q": {"metrics": ["no_such_metric"]}}}, warehouse, MF
        )


def test_unsafe_boards_are_rejected(warehouse: Path) -> None:
    base = {"metric_queries": {"q": {"metrics": ["revenue"]}}}
    with pytest.raises(ChartError, match="URL"):
        compile_board(
            "x",
            base | {"charts": {"m": {"type": "geoshape", "geo_source": "http://x/a.json"}}},
            warehouse,
            MF,
        )
    with pytest.raises(ChartError, match="type: http"):
        compile_board("x", base | {"extra": {"q2": {"type": "http", "url": "x"}}}, warehouse, MF)


def test_extra_chart_sources_are_rejected(warehouse: Path) -> None:
    config = warehouse / "dbt_charts.yml"
    original = config.read_text(encoding="utf-8")
    config.write_text(original + "  evil:\n    type: duckdb\n    path: data/dev.duckdb\n")
    try:
        assert any("ソースは warehouse" in p for p in check(warehouse, MF))
    finally:
        config.write_text(original, encoding="utf-8")


def test_dct_available() -> None:
    assert DCT.exists()
