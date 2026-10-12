"""モデリング規約の検査（#134）。manifest と DB の結果は最小の形で組み立てる。"""

from pathlib import Path
from typing import Any

from adp.generation.conventions import check_warehouse, data_violations, structural_violations


def _model(name: str, columns: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "resource_type": "model",
        "name": name,
        "database": "dev",
        "schema": "main",
        "columns": {c: {"name": c} | spec for c, spec in columns.items()},
    }


def _test(model: str, name: str, column: str | None = None, **kwargs: Any) -> dict[str, Any]:
    namespace, _, short = name.rpartition(".")
    return {
        "resource_type": "test",
        "attached_node": f"model.adp.{model}",
        "column_name": column,
        "test_metadata": {"name": short, "namespace": namespace or None, "kwargs": kwargs},
    }


def _manifest(*nodes: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i, n in enumerate(nodes):
        key = f"model.adp.{n['name']}" if n["resource_type"] == "model" else f"test.adp.t{i}"
        out[key] = n
    return {"nodes": out}


DIM = _model(
    "dim_store",
    {
        "store_key": {"entity": {"type": "primary", "name": "store"}},
        "store_id": {"description": "店舗ID（自然キー）"},
        "channel": {},
    },
)
DIM_TESTS = [
    _test("dim_store", "unique", "store_key"),
    _test("dim_store", "not_null", "store_key"),
    _test("dim_store", "accepted_values", "channel", values=["store", "online", "不明"]),
]


def _fct(columns: dict[str, dict[str, Any]], *tests: dict[str, Any]) -> dict[str, Any]:
    return _manifest(DIM, *DIM_TESTS, _model("fct_sales", columns), *tests)


def test_conforming_models_have_no_structural_violations() -> None:
    manifest = _fct(
        {
            "sale_id": {},
            "store_key": {"entity": {"type": "foreign", "name": "store"}},
            "sold_on": {},
        },
        _test("fct_sales", "unique", "sale_id"),
        _test("fct_sales", "not_null", "sale_id"),
        _test("fct_sales", "not_null", "store_key"),
        _test("fct_sales", "relationships", "store_key", to="ref('dim_store')", field="store_key"),
        _test("fct_sales", "relationships", "sold_on", to="ref('dim_date')", field="date_day"),
    )
    assert structural_violations(manifest) == []


def test_missing_keys_and_tests_are_reported() -> None:
    manifest = _fct(
        {
            "sale_id": {},
            "line_no": {},
            "store_key": {},
            "ship_to_key": {"entity": {"type": "foreign", "name": "ship_to"}},
            "sold_on": {},
        },
        _test(
            "fct_sales",
            "dbt_utils.unique_combination_of_columns",
            combination_of_columns=["sale_id", "line_no"],
        ),
        _test("fct_sales", "not_null", "sale_id"),
        _test("fct_sales", "not_null", "ship_to_key"),
        _test(
            "fct_sales", "relationships", "ship_to_key", to="ref('dim_store')", field="store_key"
        ),
    )
    messages = [v.message for v in structural_violations(manifest)]
    assert messages == [
        "粒度の列 line_no に not_null がありません",
        "外部キー store_key に dim への relationships がありません",
        "外部キー store_key に not_null がありません（NULL は不明メンバーのキーにする）",
        "ship_to_key の役割（ship_to）は列の entity ではなく derived_semantics に書きます",
        "日付の列 sold_on に dim_date への relationships がありません",
    ]


def test_model_without_primary_key_and_reserved_dim_columns() -> None:
    dim = _model("dim_calendar", {"calendar_key": {}, "month": {}})
    messages = [str(v) for v in structural_violations(_manifest(dim))]
    assert messages == [
        "dim_calendar: 主キーに unique と not_null がありません（規約「テスト（必須）」）",
        "dim_calendar: 列名 month は時間の粒度の名前と衝突します（規約「semantic model」）",
    ]


class FakeDb:
    """information_schema と不明メンバーの行の問い合わせだけに答える。"""

    def __init__(
        self, types: dict[str, list[tuple[str, str]]], unknown: dict[str, list[tuple[Any, ...]]]
    ):
        self.types = types
        self.unknown = unknown

    def __call__(self, sql: str) -> list[tuple[Any, ...]]:
        for table, cols in self.types.items():
            if "information_schema" in sql and f"table_name = '{table}'" in sql:
                return list(cols)
            if "information_schema" not in sql and f'"{table}"' in sql:
                return self.unknown.get(table, [])
        return []


STORE_TYPES = [("store_key", "VARCHAR"), ("store_id", "VARCHAR"), ("channel", "VARCHAR")]


def test_unknown_member_row_and_naming() -> None:
    manifest = _manifest(DIM, *DIM_TESTS, _model("fct_sales", {"sale_id": {}}))
    db = FakeDb(
        {
            "dim_store": [*STORE_TYPES, ("opened_date", "DATE"), ("active", "BOOLEAN")],
            "fct_sales": [("sale_id", "VARCHAR"), ("sold_time", "TIMESTAMP")],
        },
        {"dim_store": [("k", None, None, None, True)]},
    )
    messages = [v.message for v in data_violations(manifest, db)]
    assert messages == [
        "日付の列 opened_date は _on で終えます",
        "フラグの列 active は is_ で始めます",
        "不明メンバーの行の文字列の属性 channel が NULL です（「不明」か業務上の値にする）",
        "不明メンバーの行の channel（None）が accepted_values にありません",
        "日時の列 sold_time は _at で終えます",
    ]


def test_unknown_member_row_must_exist_once() -> None:
    manifest = _manifest(DIM, *DIM_TESTS)
    assert [
        v.message for v in data_violations(manifest, FakeDb({"dim_store": STORE_TYPES}, {}))
    ] == ["不明メンバーの行（store_key が unknown_key()）が 1 行ではありません（0 行）"]
    ok = FakeDb({"dim_store": STORE_TYPES}, {"dim_store": [("k", None, "不明")]})
    assert data_violations(manifest, ok) == []


def test_missing_table_is_reported() -> None:
    assert [v.message for v in data_violations(_manifest(DIM), FakeDb({}, {}))] == [
        "テーブルがありません（make dbt-build を実行する）"
    ]


def test_unbuilt_project_is_a_violation(tmp_path: Path) -> None:
    assert [v.message for v in check_warehouse(tmp_path)] == [
        "dbt の manifest がありません（make dbt-build を実行する）"
    ]
