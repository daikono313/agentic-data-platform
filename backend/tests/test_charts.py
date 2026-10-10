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


def _with(**changes: object) -> dict[str, object]:
    spec = yaml.safe_load(yaml.safe_dump(SPEC, allow_unicode=True))
    spec.update(changes)
    return spec  # type: ignore[no-any-return]


def _chart(**changes: object) -> dict[str, object]:
    chart = dict(SPEC["charts"]["stores"])  # type: ignore[index]
    chart.update(changes)
    return {"stores": chart}


def _query(**changes: object) -> dict[str, object]:
    query = dict(SPEC["metric_queries"]["by_store"])  # type: ignore[index]
    query.update(changes)
    return {"by_store": query}


@pytest.mark.parametrize(
    ("spec", "match"),
    [
        # 手書き SQL の経路（#55 のレビュー）
        (_with(queries={"q": {"sql": "select 1"}}), "SQL"),
        (_with(charts=_chart(query="select 1 as revenue")), "SQL は書けません"),
        (_with(charts=_chart(query={"sql": "select 1"})), "SQL は書けません"),
        (_with(rows=[{"queries": {"q": {"sql": "select 1"}}, "charts": {}}]), "rows / cols"),
        (_with(rows=["../lib/base.yml"]), "チャート"),
        (_with(extends="../lib/base.yml"), "使えないキー"),
        (_with(charts={"base.yml": SPEC["charts"]["stores"]}), "チャート名"),  # type: ignore[index]
        (_with(metric_queries=_query(sql="x")), "使えないキー"),
        (_with(metric_queries=_query(where="1=1 or exists (select 1)")), "使えないキー"),
        # 外部への経路・HTML
        (_with(charts=_chart(geo_source="world-countries")), "使えないキー"),
        (_with(charts=_chart(type="geoshape")), "type"),
        (_with(html_policy="trusted-raw"), "使えないキー"),
        (_with(rows=[{"text": "<img src=x>"}]), "rows / cols"),
        (_with(title="<img src=x>"), "使えない文字"),
        (_with(charts=_chart(title="{{ x }}")), "使えない文字"),
        (_with(charts=_chart(source="//evil/x")), "URL|使えないキー"),
        (_with(charts=_chart(x="store__store_name || 1")), "列名"),
        # 入力の型
        (_with(metric_queries=_query(metrics="revenue")), "文字列のリスト"),
        (_with(metric_queries=_query(limit="x")), "正の整数"),
        (_with(metric_queries=_query(group_by=["a b"])), "使えない名前"),
        (_with(metric_queries=_query(filters="x")), "filters"),
        (
            _with(metric_queries=_query(filters=[{"dimension": "x", "op": "=", "value": 1}])),
            "dimension",
        ),
        (
            _with(
                metric_queries=_query(filters=[{"dimension": "store__x", "op": "like", "value": 1}])
            ),
            "op",
        ),
        (
            _with(
                metric_queries=_query(filters=[{"dimension": "store__x", "op": "in", "value": 1}])
            ),
            "リスト",
        ),
        (
            _with(
                metric_queries=_query(
                    filters=[{"dimension": "store__x", "op": "=", "value": {"a": 1}}]
                )
            ),
            "型",
        ),
        (_with(metric_queries=_query(start_time="2024-01-01; drop")), "YYYY-MM-DD"),
        (_with(metric_queries=_query(metrics=["no_such_metric"])), "作れません"),
    ],
)
def test_spec_validation(warehouse: Path, spec: dict[str, object], match: str) -> None:
    with pytest.raises(ChartError, match=match):
        compile_board("x", spec, warehouse, MF)


def test_filters_are_metricflow_dimensions(warehouse: Path) -> None:
    q = MetricQuery.parse(
        "q",
        {
            "metrics": ["revenue"],
            "filters": [
                {"dimension": "store__store_name", "op": "in", "value": ["O'Hare", "b"]},
                {"dimension": "store__store_name", "op": "!=", "value": "c"},
            ],
        },
    )
    assert q.filters == [
        "{{ Dimension('store__store_name') }} in ('O''Hare', 'b')",
        "{{ Dimension('store__store_name') }} <> 'c'",
    ]
    board = compile_board(
        "x",
        _with(
            metric_queries=_query(
                filters=[{"dimension": "store__store_name", "op": "!=", "value": "c"}]
            )
        ),
        warehouse,
        MF,
    )
    assert "<> 'c'" in board


def test_nested_layout_renders(warehouse: Path) -> None:
    spec = _with(rows=[{"cols": ["stores", "stores"]}])
    path = warehouse / "charts" / "nested.yml"
    path.write_text(compile_board("nested", spec, warehouse, MF), encoding="utf-8")
    try:
        result = _dct(warehouse, "render", "charts/nested.yml", "--format", "text")
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        path.unlink()


def test_unsafe_boards_are_rejected(warehouse: Path) -> None:
    with pytest.raises(ChartError, match="URL"):
        compile_board("x", _with(charts=_chart(title="http://x/a.json")), warehouse, MF)
    with pytest.raises(ChartError, match="type: http"):
        compile_board("x", _with(extra={"q2": {"type": "http", "url": "x"}}), warehouse, MF)


def test_check_scans_subdirectories(warehouse: Path) -> None:
    for rel in ("sub/a.yml", "b.yaml"):
        path = warehouse / "charts" / rel
        path.parent.mkdir(exist_ok=True)
        path.write_text("title: x\n")
    try:
        problems = check(warehouse, MF)
        assert any("sub/a.yml" in p for p in problems)
        assert any("b.yaml" in p for p in problems)
    finally:
        (warehouse / "charts" / "sub" / "a.yml").unlink()
        (warehouse / "charts" / "sub").rmdir()
        (warehouse / "charts" / "b.yaml").unlink()


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
