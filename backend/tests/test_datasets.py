"""データ一覧（#38）を、生成済みの指標を含む warehouse の複製で確かめる。"""

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from adp.api.app import create_app
from adp.api.service import SessionService
from adp.api.store import Store
from adp.beam import load
from adp.catalog.catalog import Catalog
from adp.datasets import DatasetIndex
from adp.design import DesignBuilder
from adp.design.catalog_schema import CatalogSchema
from adp.generation import generate_models
from adp.generation.project import ProjectInfo
from adp.interview import Interviewer
from adp.llm import LLMSettings, ScriptedRunner
from tests.generation.test_dbt_codegen import DBT, FIXTURES, _warehouse_copy


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("repo")
    warehouse = _warehouse_copy(root)
    catalog = Catalog(warehouse / "data" / "dev.duckdb")
    info = ProjectInfo(warehouse, catalog)
    beam = load(FIXTURES / "beam" / "sales_by_store.yml")
    info.write(generate_models(DesignBuilder(CatalogSchema(catalog)).build(beam), info, beam).files)
    (root / "beam").mkdir()
    (root / "beam" / "sales_by_store.yml").write_text(
        yaml.safe_dump(beam.model_dump(mode="json"), allow_unicode=True), encoding="utf-8"
    )
    env = os.environ | {"DBT_PROFILES_DIR": str(warehouse)}
    for args in (["deps"], ["parse"]):
        subprocess.run([str(DBT), *args], cwd=warehouse, env=env, check=True, capture_output=True)
    return root


def test_datasets_lists_metrics_models_sources_beams(repo: Path) -> None:
    catalog = Catalog(repo / "warehouse/data/dev.duckdb", repo / "warehouse/target/manifest.json")
    index = DatasetIndex(repo, catalog)
    data = index.all()
    assert data.errors == []
    revenue = next(m for m in data.metrics if m.name == "revenue")
    assert revenue.label == "売上金額" and revenue.model == "fct_order_lines"
    assert revenue.beam == "sales_by_store"
    names = {d.name: d.label for d in revenue.dimensions}
    kinds = {d.name: d.kind for d in revenue.dimensions}
    assert kinds["metric_time"] == "time" and kinds["store__channel"] == "categorical"
    assert next(d for d in revenue.dimensions if d.name == "store__channel").entity == "store"
    assert "metric_time" in names and "store__channel" in names
    assert names["store__channel"] != "store__channel"  # 日本語の表示名が付く
    assert {"fct_order_lines", "dim_customer", "dim_store"} <= {m.name for m in data.models}
    assert data.models[0].kind == "fct"
    orders = next(s for s in data.sources if s.name == "raw.orders")
    assert orders.row_count > 0 and orders.description
    assert (
        {c.name: c.data_type for c in orders.columns}["ordered_at"].upper().startswith("TIMESTAMP")
    )
    assert [b.name for b in data.beams] == ["sales_by_store"]


def test_dimensions_without_mf_process_or_db_lock(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """切り口はプロセス内の MetricFlow から取り、mf を起動せず DuckDB もロックしない（#43）。"""
    import duckdb

    calls = []
    real = subprocess.run

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    index = DatasetIndex(repo, None)
    revenue = next(m for m in index.all().metrics if m.name == "revenue")
    assert "store__store_name" in [d.name for d in revenue.dimensions]
    assert calls == []
    # dbt build などが書き込めること
    duckdb.connect(str(repo / "warehouse/data/dev.duckdb")).close()


def test_missing_build_is_reported(tmp_path: Path) -> None:
    (tmp_path / "warehouse").mkdir()
    index = DatasetIndex(tmp_path, Catalog(tmp_path / "x.duckdb"))
    data = index.all()
    assert data.metrics == [] and data.models == [] and data.sources == []
    assert any("make dbt-build" in e for e in data.errors)


def test_api_endpoint(repo: Path) -> None:
    async def noop(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    service = SessionService(
        Store(repo / ".adp/app.db"), Interviewer(ScriptedRunner([]), LLMSettings()), noop
    )
    index = DatasetIndex(repo, Catalog(repo / "warehouse/data/dev.duckdb"))
    client = TestClient(create_app(service, index))
    body = client.get("/datasets").json()
    assert {"metrics", "models", "sources", "beams", "errors"} <= set(body)
    assert TestClient(create_app(service)).get("/datasets").status_code == 503


def test_primary_entity_at_model_level_gets_labels() -> None:
    index = DatasetIndex(Path("."), None)
    manifest = {
        "semantic_models": [
            {
                "name": "fct_x",
                "primary_entity": "order_line",
                "entities": [],
                "dimensions": [
                    {"name": "order_status", "type": "categorical", "description": "注文ステータス"}
                ],
            }
        ]
    }
    assert index._dimension_labels(manifest)["order_line__order_status"][0] == "注文ステータス"


def test_broken_manifest_is_reported(tmp_path: Path) -> None:
    (tmp_path / "warehouse" / "target").mkdir(parents=True)
    (tmp_path / "warehouse" / "target" / "semantic_manifest.json").write_text("{broken")
    (tmp_path / "warehouse" / "target" / "manifest.json").write_text("{broken")
    data = DatasetIndex(tmp_path, Catalog(tmp_path / "x.duckdb")).all()
    assert data.metrics == [] and data.models == [] and data.errors


def test_source_profile(repo: Path) -> None:
    """源泉のプロファイル: 全列の統計・キーの候補・使っている整備済みモデル（#62）。"""
    catalog = Catalog(repo / "warehouse/data/dev.duckdb", repo / "warehouse/target/manifest.json")
    p = DatasetIndex(repo, catalog).profile("raw.order_items")
    assert p.row_count > 0 and p.description
    assert [c.column for c in p.profiles] == [c.name for c in p.columns]
    assert p.primary_key_candidates == ["order_item_id"]
    assert {f.column: f.references for f in p.foreign_key_candidates}["order_id"] == (
        "raw.orders.order_id"
    )
    assert "fct_order_lines" in p.used_by and all(m.startswith(("dim_", "fct_")) for m in p.used_by)
    product = next(c for c in p.profiles if c.column == "product_id")
    assert 0 < len(product.top_values) <= 5 and not product.is_unique


def test_source_profile_api(repo: Path) -> None:
    async def noop(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    service = SessionService(
        Store(repo / ".adp/app.db"), Interviewer(ScriptedRunner([]), LLMSettings()), noop
    )
    index = DatasetIndex(repo, Catalog(repo / "warehouse/data/dev.duckdb"))
    client = TestClient(create_app(service, index))
    body = client.get("/sources/raw.returns/profile").json()
    assert body["name"] == "returns" and body["profiles"]
    # 対象スキーマ外・識別子でない名前・存在しないテーブルは 404
    for bad in ("main.fct_order_lines", "raw.x;drop", "raw.nope"):
        assert client.get(f"/sources/{bad}/profile").status_code == 404, bad
    assert TestClient(create_app(service)).get("/sources/raw.returns/profile").status_code == 503


def test_source_profile_db_error_is_fixed_text(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DuckDB の例外は 500 にせず、本文を出さない定型文の 503 にする（guardrails §3）。"""
    import duckdb

    async def noop(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    def broken(self, table, top=5, detail=False):  # type: ignore[no-untyped-def]
        raise duckdb.BinderException("secret detail")

    monkeypatch.setattr(Catalog, "profile_table", broken)
    service = SessionService(
        Store(repo / ".adp/app.db"), Interviewer(ScriptedRunner([]), LLMSettings()), noop
    )
    index = DatasetIndex(repo, Catalog(repo / "warehouse/data/dev.duckdb"))
    res = TestClient(create_app(service, index)).get("/sources/raw.returns/profile")
    assert res.status_code == 503 and "secret" not in res.text


def test_bus_matrix(repo: Path) -> None:
    """fact × dimension のバスマトリクスを entity の結合関係から作る（#62）。"""
    bus = DatasetIndex(repo, None).all().bus
    groups = {g.name: g for g in bus.groups}
    assert groups["dim_store"].kind == "dim" and groups["dim_store"].label
    assert "store__channel" in [a.name for a in groups["dim_store"].attributes]
    assert [g.kind for g in bus.groups][-1] == "degenerate"  # fact 自身の属性は最後
    row = next(r for r in bus.rows if r.name == "fct_order_lines")
    assert {"dim_store", "dim_customer", "dim_date"} <= set(row.groups)
    assert all(g in groups for g in row.groups)


def test_bus_matrix_from_manifest() -> None:
    index = DatasetIndex(Path("."), None)
    manifest = {
        "semantic_models": [
            {
                "name": "dim_a",
                "description": "Aディメンション（Who）。",
                "entities": [{"name": "a", "type": "primary"}],
                "dimensions": [{"name": "x", "type": "categorical", "description": "属性X（注）"}],
            },
            {
                "name": "dim_b",
                "entities": [{"name": "b", "type": "primary"}],
                "dimensions": [{"name": "y", "type": "categorical", "label": "Y"}],
            },
            {
                "name": "fct_f",
                "description": "出来事",
                "entities": [{"name": "f", "type": "primary"}, {"name": "a", "type": "foreign"}],
                "dimensions": [],
            },
        ]
    }
    index._semantic_manifest = lambda: manifest  # type: ignore[method-assign]
    bus = index.bus_matrix()
    assert [(g.name, g.label) for g in bus.groups] == [("dim_a", "A"), ("dim_b", "dim_b")]
    assert bus.groups[0].attributes[0].label == "属性X"
    assert [(r.name, r.label, r.groups) for r in bus.rows] == [("fct_f", "出来事", ["dim_a"])]


def test_bus_matrix_join_rules() -> None:
    """unique entity・1 段の snowflake・同じ entity の dim 2 つ・primary の無い fct（#62）。"""
    index = DatasetIndex(Path("."), None)

    def dim(name: str, *ents: tuple[str, str]) -> dict[str, Any]:
        return {
            "name": name,
            "entities": [{"name": n, "type": t} for n, t in ents],
            "dimensions": [{"name": "attr", "type": "categorical"}],
        }

    manifest = {
        "semantic_models": [
            dim("dim_product", ("product", "primary"), ("category", "foreign")),
            dim("dim_category", ("category", "primary")),
            dim("dim_customer", ("customer", "primary")),
            dim("dim_customer_scd", ("customer", "primary")),
            dim("dim_sku", ("sku_row", "primary"), ("sku", "unique")),
            dim("dim_unrelated", ("x", "primary")),
            {
                "name": "fct_sales",
                "entities": [
                    {"name": "sale", "type": "primary"},
                    {"name": "product", "type": "foreign"},
                    {"name": "customer", "type": "foreign"},
                    {"name": "sku", "type": "foreign"},
                ],
                "dimensions": [{"name": "status", "type": "categorical"}],
            },
            {
                "name": "fct_noprimary",
                "entities": [{"name": "customer", "type": "foreign"}],
                "dimensions": [{"name": "status", "type": "categorical"}],
            },
            {"description": "name の無いモデルは無視する"},
        ]
    }
    bus = index.bus_matrix(manifest)
    rows = {r.name: r.groups for r in bus.rows}
    assert set(rows["fct_sales"]) == {
        "dim_product",
        "dim_category",
        "dim_customer",
        "dim_customer_scd",
        "dim_sku",
        "fct_sales",
    }
    assert rows["fct_noprimary"] == ["dim_customer", "dim_customer_scd"]
    assert [g.name for g in bus.groups if g.kind == "degenerate"] == ["fct_sales"]


def test_broken_bus_matrix_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    index = DatasetIndex(Path("."), None)

    def broken(manifest=None):  # type: ignore[no-untyped-def]
        raise KeyError("name")

    monkeypatch.setattr(index, "bus_matrix", broken)
    errors: list[str] = []
    assert index._bus(errors).rows == [] and errors


def test_ontology_api(repo: Path, tmp_path: Path) -> None:
    """GET /ontology: 図と食い違いを返す。yml が無ければ 404、形式の誤りは 422（#67）。"""
    import shutil

    async def noop(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    service = SessionService(
        Store(tmp_path / "app.db"), Interviewer(ScriptedRunner([]), LLMSettings()), noop
    )
    catalog = Catalog(repo / "warehouse/data/dev.duckdb", repo / "warehouse/target/manifest.json")
    client = TestClient(create_app(service, DatasetIndex(repo, catalog)))
    if (repo / "ontology").exists():
        shutil.rmtree(repo / "ontology")
    assert client.get("/ontology").status_code == 404
    (repo / "ontology").mkdir()
    src = Path(__file__).resolve().parents[2] / "ontology" / "ontology.yml"
    (repo / "ontology" / "ontology.yml").write_text(src.read_text(encoding="utf-8"))
    body = client.get("/ontology").json()
    assert body["mermaid"].startswith("flowchart")
    # raw.inventory は後から追加された源泉の例（オントロジーに未整理。#69）
    assert body["errors"] == [] and body["unmapped_sources"] == ["raw.inventory"]
    (repo / "ontology" / "ontology.yml").write_text("version: 1\ndomains: []\nconcepts: [1]\n")
    assert client.get("/ontology").status_code == 422
    shutil.rmtree(repo / "ontology")


def test_source_cleansing_api(repo: Path, tmp_path: Path) -> None:
    """GET /sources/{table}/cleansing: 今の定義と、プロファイルからの雛形（#68）。"""

    async def noop(beam, on_step):  # type: ignore[no-untyped-def]
        raise AssertionError

    service = SessionService(
        Store(tmp_path / "app.db"), Interviewer(ScriptedRunner([]), LLMSettings()), noop
    )
    catalog = Catalog(repo / "warehouse/data/dev.duckdb", repo / "warehouse/target/manifest.json")
    client = TestClient(create_app(service, DatasetIndex(repo, catalog)))
    body = client.get("/sources/raw.orders/cleansing").json()
    assert body["path"] == "warehouse/cleansing/raw.orders.yml"
    assert body["existing"] and "ordered_on" in body["existing"]
    assert "key: order_id" in body["draft"]
    body = client.get("/sources/raw.returns/cleansing").json()
    assert body["existing"] is None and "source: raw.returns" in body["draft"]
    assert client.get("/sources/main.x/cleansing").status_code == 404
